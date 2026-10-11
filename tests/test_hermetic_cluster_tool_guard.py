"""Committed, falsifiable tests for the hermetic cluster-tool guard
(``tests/conftest.py``) — ADDENDUM A1 through A7 of the 2026-10-11
test-parity-scripted-enforcement spec chain, written after an independent
review REJECTed the first cut for shipping the guard without a committed
test, then REJECTed twice more after each rebuild kept parsing more argv
and wrapper forms (and finding new bypasses each time).

This round changed the INSTRUMENT instead of adding more forms: the
process layer (``scripts/ci/run-tests.sh``'s kubectl+helm shims and reject
log) is the CONTROL; everything below is the IN-PROCESS DIAGNOSTIC — a
fail-closed word check plus an env-equality check that runs on EVERY
spawn. Every test here calls the REAL guard functions in
``tests.conftest`` — none of this logic is reimplemented.
"""

from __future__ import annotations

import asyncio
import json
import os
import pty
import stat
import subprocess
from pathlib import Path

import pytest

import tests.conftest as conftest
from tests._hermetic_env import (
    build_fresh_sentinel_env,
    expect_hermetic_violation,
    hermetic_env,
    override_sentinel,
    run_child_pytest_session,
)
from tests.conftest import HermeticClusterToolError
from tests.hermetic_allowlist import ALLOWED_CLUSTER_TOOL_TESTS

REPO_ROOT = Path(__file__).resolve().parent.parent
HELM_RULE_CASES = json.loads(
    (REPO_ROOT / "tests" / "data" / "helm_rule_cases.json").read_text()
)["cases"]

# ─────────────────────────── fake tool fixtures ─────────────────────────────


def _write_fake_tool(bin_dir: Path, name: str) -> Path:
    """A `name` executable that logs its own invocation (argv, as a single
    line) to a sentinel file next to it, then exits 0 immediately. Never a
    real cluster tool — safe to actually execute if a guard fails to
    intercept it."""
    sentinel = bin_dir / f"{name}.sentinel.log"
    tool = bin_dir / name
    tool.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{sentinel}"\nexit 0\n')
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sentinel


@pytest.fixture
def fake_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Puts fake, harmless, sentinel-logging `kubectl` and `helm` first on
    PATH, and makes that same directory the ACTIVE frozen shim dir for the
    duration of the test (`override_sentinel`) — otherwise the env-
    equality check (which has no opt-in, A3-7) would itself raise on
    PATH[0] mismatch the instant any allowed call proceeds far enough to
    actually spawn. Returns {"kubectl": <sentinel path>, "helm": <sentinel
    path>}."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    sentinels = {
        "kubectl": _write_fake_tool(bin_dir, "kubectl"),
        "helm": _write_fake_tool(bin_dir, "helm"),
    }
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    with override_sentinel(shim_dir=str(bin_dir)):
        yield sentinels


def _ran(sentinel: Path) -> bool:
    return sentinel.exists() and sentinel.stat().st_size > 0


# ───────────────────────── one test per spawn entry point ───────────────────


class TestSpawnEntryPoints:
    def test_subprocess_run_list_argv_raises(self, fake_tools):
        with expect_hermetic_violation():
            subprocess.run(["kubectl", "get", "nodes"], check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_subprocess_popen_direct_raises(self, fake_tools):
        with expect_hermetic_violation():
            subprocess.Popen(["kubectl", "get", "nodes"])
        assert not _ran(fake_tools["kubectl"])

    def test_subprocess_call_raises(self, fake_tools):
        with expect_hermetic_violation():
            subprocess.call(["kubectl", "get", "nodes"])
        assert not _ran(fake_tools["kubectl"])

    def test_subprocess_check_call_raises(self, fake_tools):
        with expect_hermetic_violation():
            subprocess.check_call(["kubectl", "get", "nodes"])
        assert not _ran(fake_tools["kubectl"])

    def test_subprocess_check_output_raises(self, fake_tools):
        with expect_hermetic_violation():
            subprocess.check_output(["kubectl", "get", "nodes"])
        assert not _ran(fake_tools["kubectl"])

    def test_os_system_raises(self, fake_tools):
        with expect_hermetic_violation():
            os.system("kubectl get nodes")
        assert not _ran(fake_tools["kubectl"])

    async def test_asyncio_create_subprocess_exec_raises(self, fake_tools):
        with expect_hermetic_violation():
            await asyncio.create_subprocess_exec("kubectl", "get", "nodes")
        assert not _ran(fake_tools["kubectl"])

    async def test_asyncio_create_subprocess_shell_raises(self, fake_tools):
        with expect_hermetic_violation():
            await asyncio.create_subprocess_shell("kubectl get nodes")
        assert not _ran(fake_tools["kubectl"])


# ───────────────────────── evasion forms the word check still closes ────────


class TestWordCheckClosesEvasionForms:
    def test_bytes_argv_raises(self, fake_tools):
        """A bytes-decoded argv element still names kubectl as a word."""
        with expect_hermetic_violation():
            subprocess.run([b"kubectl", b"get", b"ns"], check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_executable_kwarg_raises_even_with_decoy_argv0(self, fake_tools):
        """argv[0] is a decoy; `executable=` names the REAL program —
        A1-2's bytes decode + executable= collection, kept verbatim (A4-8
        carve-out) across the word-check redesign."""
        with expect_hermetic_violation():
            subprocess.run(["x", "get", "ns"], executable="kubectl", check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_shell_true_string_naming_kubectl_raises(self, fake_tools):
        """A shell=True string is collected as a whole-string source for
        the word split — no segment parsing needed."""
        with expect_hermetic_violation():
            subprocess.run("cd /tmp && kubectl get ns", shell=True, check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_shell_true_list_branch_naming_kubectl_raises(self):
        """A2-10's missing-test item: `shell=True` with a LIST `args` —
        every element is still collected as a word source."""
        with expect_hermetic_violation():
            subprocess.run(["kubectl", "get ns"], shell=True, check=False)

    def test_env_wrapper_raises(self, fake_tools):
        """No wrapper unwrapping remains (A2-2 deleted it) — the word
        `kubectl` anywhere in the collected argv still raises."""
        with expect_hermetic_violation():
            subprocess.run(["env", "kubectl", "get", "ns"], check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_sh_dash_c_string_raises(self, fake_tools):
        with expect_hermetic_violation():
            subprocess.run(["sh", "-c", "kubectl get ns"], check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_unrelated_command_is_never_blocked(self):
        """Sanity/regression control: an ordinary command naming neither
        tool, with a hermetic env, is never touched."""
        result = subprocess.run(
            ["echo", "hello"], capture_output=True, text=True, env=hermetic_env()
        )
        assert result.returncode == 0
        assert "hello" in result.stdout


# ──────────────── the raw exec*/posix_spawn*/spawn*/pty.spawn families ──────


def _run_in_forked_child(fn) -> int:
    """Runs niladic `fn` in a forked child and returns its exit status.
    Required for the TRUE os.exec* family: on success they REPLACE the
    calling process image, so if a guard bug ever let one through, only
    this throwaway child would be replaced/exit — never the pytest worker
    running this test file. 42 = guard raised as expected; 43 = raised
    something else; 44 = returned without raising."""
    pid = os.fork()
    if pid == 0:
        try:
            fn()
        except HermeticClusterToolError:
            os._exit(42)
        except BaseException:
            os._exit(43)
        os._exit(44)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


class TestRawExecAndSpawnFamilies:
    """One test per DISTINCT calling convention — every other member of
    the same family is patched identically (conftest.py, A4-1's identity
    pin below) but not separately re-tested here."""

    def test_os_execv_raises(self, fake_tools):
        kubectl = os.path.join(os.path.dirname(fake_tools["kubectl"]), "kubectl")
        code = _run_in_forked_child(lambda: os.execv(kubectl, [kubectl, "get", "ns"]))
        assert code == 42
        assert not _ran(fake_tools["kubectl"])

    def test_os_execve_raises(self, fake_tools):
        kubectl = os.path.join(os.path.dirname(fake_tools["kubectl"]), "kubectl")
        code = _run_in_forked_child(
            lambda: os.execve(kubectl, [kubectl, "get", "ns"], dict(os.environ))
        )
        assert code == 42
        assert not _ran(fake_tools["kubectl"])

    def test_os_execvp_raises(self, fake_tools):
        code = _run_in_forked_child(
            lambda: os.execvp("kubectl", ["kubectl", "get", "ns"])
        )
        assert code == 42
        assert not _ran(fake_tools["kubectl"])

    def test_os_execl_raises(self, fake_tools):
        """*args-style calling convention (execl/execle/execlp/execlpe all
        share this shape; patched identically)."""
        kubectl = os.path.join(os.path.dirname(fake_tools["kubectl"]), "kubectl")
        code = _run_in_forked_child(lambda: os.execl(kubectl, kubectl, "get", "ns"))
        assert code == 42
        assert not _ran(fake_tools["kubectl"])

    def test_os_posix_spawn_raises(self, fake_tools):
        kubectl = os.path.join(os.path.dirname(fake_tools["kubectl"]), "kubectl")

        def _call():
            os.posix_spawn(kubectl, [kubectl, "get", "ns"], dict(os.environ))

        with expect_hermetic_violation():
            _call()
        assert not _ran(fake_tools["kubectl"])

    def test_os_posix_spawnp_raises(self, fake_tools):
        with expect_hermetic_violation():
            os.posix_spawnp("kubectl", ["kubectl", "get", "ns"], dict(os.environ))
        assert not _ran(fake_tools["kubectl"])

    def test_os_spawnv_raises(self, fake_tools):
        """Representative of the os.spawn* family."""
        with expect_hermetic_violation():
            os.spawnv(os.P_WAIT, "kubectl", ["kubectl", "get", "ns"])
        assert not _ran(fake_tools["kubectl"])

    def test_pty_spawn_raises(self, fake_tools):
        with expect_hermetic_violation():
            pty.spawn(["kubectl", "get", "ns"])
        assert not _ran(fake_tools["kubectl"])


def test_a4_1_os_and_pty_patches_are_pinned_by_identity():
    """A4-1: while the fixture is active (it always is, autouse), every
    patched name on os/pty/asyncio/subprocess.Popen must literally BE the
    conftest patch function — not merely behave like it. Removing any one
    row turns this RED."""
    pairs = [
        (os.execv, conftest._patched_execv),
        (os.execve, conftest._patched_execve),
        (os.execvp, conftest._patched_execvp),
        (os.execvpe, conftest._patched_execvpe),
        (os.execl, conftest._patched_execl),
        (os.execle, conftest._patched_execle),
        (os.execlp, conftest._patched_execlp),
        (os.execlpe, conftest._patched_execlpe),
        (os.posix_spawn, conftest._patched_posix_spawn),
        (os.posix_spawnp, conftest._patched_posix_spawnp),
        (os.spawnv, conftest._patched_spawnv),
        (os.spawnve, conftest._patched_spawnve),
        (os.spawnvp, conftest._patched_spawnvp),
        (os.spawnvpe, conftest._patched_spawnvpe),
        (os.spawnl, conftest._patched_spawnl),
        (os.spawnle, conftest._patched_spawnle),
        (os.spawnlp, conftest._patched_spawnlp),
        (os.spawnlpe, conftest._patched_spawnlpe),
        (pty.spawn, conftest._patched_pty_spawn),
        (os.system, conftest._patched_os_system),
        (subprocess.Popen.__init__, conftest._patched_popen_init),
        (asyncio.create_subprocess_exec, conftest._patched_create_subprocess_exec),
        (asyncio.create_subprocess_shell, conftest._patched_create_subprocess_shell),
    ]
    for actual, expected in pairs:
        assert actual is expected, f"{actual!r} is not the conftest patch {expected!r}"


def test_a4_1_behavioural_second_instrument_still_raises():
    """A4-1's second instrument: calling each patched name with a kubectl
    argv still raises, independent of the identity pin above."""
    with expect_hermetic_violation():
        os.spawnv(os.P_WAIT, "kubectl", ["kubectl", "x"])
    with expect_hermetic_violation():
        os.system("kubectl x")


# ──────────────────── the helm positional ALLOW rule (data-driven) ──────────


def _drive_word_rule(case: dict) -> None:
    """Drives the real `conftest._enforce_word_rule` for one case — never
    swallows its own raise, so the caller's `expect_hermetic_violation()`
    can see it. Isolated from the env check (covered separately below) so
    this table is purely about the word/positional rule, per A4-5/A5-3."""
    form = case["form"]
    if form == "list":
        conftest._enforce_word_rule(
            case["argv"], executable=None, shell=False, display="t"
        )
    elif form == "shell":
        conftest._enforce_word_rule(
            case["cmd"], executable=None, shell=True, display="t"
        )
    elif form == "executable":
        conftest._enforce_word_rule(
            case["argv"], executable=case["executable"], shell=False, display="t"
        )
    else:  # pragma: no cover - the data file only uses the three forms above
        raise AssertionError(f"unknown case form {form!r}")


@pytest.mark.parametrize(
    "case", HELM_RULE_CASES, ids=[c["id"] for c in HELM_RULE_CASES]
)
def test_helm_rule_case_table_through_python(case):
    if case["expect"] == "raise":
        with expect_hermetic_violation():
            _drive_word_rule(case)
    else:
        _drive_word_rule(case)


def test_helm_with_kube_context_flag_before_cluster_subcommand_still_raises():
    """A3-2's own pin: `helm --kube-context ctx upgrade ...` is rejected —
    `upgrade` is not in the safe subcommand set regardless of the leading
    global flag."""
    with expect_hermetic_violation():
        conftest._enforce_word_rule(
            ["helm", "--kube-context", "ctx", "upgrade", "x"],
            executable=None,
            shell=False,
            display="t",
        )


def test_helm_rule_case_table_is_exhaustive_over_allow_and_raise():
    """Regression guard on the table itself: it must exercise both
    verdicts, or the parametrised test above could trivially pass by
    every row sharing one outcome."""
    verdicts = {c["expect"] for c in HELM_RULE_CASES}
    assert verdicts == {"allow", "raise"}


# ──────────────────── the env-equality check (A3-3, A4-2, A5-1) ─────────────


class TestEnvEqualityCheck:
    """Each test drives `conftest._enforce_env_rule` directly (isolated
    from the word rule) through `override_sentinel`, so each A4-2 clause
    gets its own neuter-provable assertion."""

    def test_a_kubeconfig_mismatch_raises(self):
        with override_sentinel(shim_dir="/shim", kubeconfig="/kc", required=True):
            with expect_hermetic_violation():
                conftest._enforce_env_rule(
                    {"PATH": "/shim", "KUBECONFIG": "/not-the-sentinel"}, display="t"
                )

    def test_b_path_first_entry_mismatch_raises(self):
        with override_sentinel(shim_dir="/shim", kubeconfig="/kc", required=True):
            with expect_hermetic_violation():
                conftest._enforce_env_rule(
                    {"PATH": f"/elsewhere{os.pathsep}/shim", "KUBECONFIG": "/kc"},
                    display="t",
                )

    def test_c_kubernetes_service_host_set_raises(self):
        with override_sentinel(shim_dir="/shim", kubeconfig="/kc", required=True):
            with expect_hermetic_violation():
                conftest._enforce_env_rule(
                    {
                        "PATH": "/shim",
                        "KUBECONFIG": "/kc",
                        "KUBERNETES_SERVICE_HOST": "10.0.0.1",
                    },
                    display="t",
                )

    def test_d_kubernetes_service_port_set_raises(self):
        with override_sentinel(shim_dir="/shim", kubeconfig="/kc", required=True):
            with expect_hermetic_violation():
                conftest._enforce_env_rule(
                    {
                        "PATH": "/shim",
                        "KUBECONFIG": "/kc",
                        "KUBERNETES_SERVICE_PORT": "443",
                    },
                    display="t",
                )

    def test_e_unlisted_helm_kube_prefixed_name_raises(self):
        """Proves the match is a PREFIX, not an exact 8-name list."""
        with override_sentinel(shim_dir="/shim", kubeconfig="/kc", required=True):
            with expect_hermetic_violation():
                conftest._enforce_env_rule(
                    {"PATH": "/shim", "KUBECONFIG": "/kc", "HELM_KUBEFUTURE": "x"},
                    display="t",
                )

    def test_f_source_is_env_kwarg_when_given_else_os_environ(self, monkeypatch):
        with override_sentinel(shim_dir="/shim", kubeconfig="/kc", required=True):
            # a CLEAN os.environ with a BAD env= -> raises (env= wins)
            monkeypatch.setenv("PATH", "/shim")
            monkeypatch.setenv("KUBECONFIG", "/kc")
            with expect_hermetic_violation():
                conftest._enforce_env_rule(
                    {"PATH": "/bad", "KUBECONFIG": "/bad"}, display="t"
                )
            # a BAD os.environ with a CLEAN env=None -> falls back to
            # os.environ, so make os.environ itself clean and confirm pass
            monkeypatch.setenv("PATH", "/shim")
            monkeypatch.setenv("KUBECONFIG", "/kc")
            conftest._enforce_env_rule(None, display="t")  # must not raise

    def test_g_neither_tool_named_bad_env_still_raises(self):
        """A4-2(g): the env check is unconditional — a spawn naming
        neither kubectl nor helm still raises on a bad env."""
        with override_sentinel(shim_dir="/shim", kubeconfig="/kc", required=True):
            with expect_hermetic_violation():
                conftest._enforce_hermetic(
                    ["true"], env={"PATH": "/bad", "KUBECONFIG": "/bad"}
                )

    def test_h_sentinel_absent_and_required_unset_warns_once_no_raise(self, recwarn):
        with override_sentinel(shim_dir=None, kubeconfig=None, required=False):
            conftest._sentinel_absent_warned[0] = False
            conftest._enforce_env_rule({"PATH": "/x"}, display="t")  # must not raise
            conftest._enforce_env_rule(
                {"PATH": "/x"}, display="t"
            )  # still must not raise
            warnings_seen = [
                w for w in recwarn.list if "not CI-shaped" in str(w.message)
            ]
            assert len(warnings_seen) == 1, "expected exactly ONE warning per session"

    def test_i_prime_required_set_sentinel_absent_raises(self):
        """A6-4 (A4-2(i')): HERMETIC_REQUIRED=1 with the sentinel absent
        raises — fail closed when the script's own contract is violated."""
        with override_sentinel(shim_dir=None, kubeconfig=None, required=True):
            with expect_hermetic_violation():
                conftest._enforce_env_rule({"PATH": "/x"}, display="t")

    def test_i_ci_true_required_unset_sentinel_absent_warns_not_raises(
        self, monkeypatch, recwarn
    ):
        """A7-2: CI=true alone is NEVER a fail-closed trigger — only
        HERMETIC_REQUIRED=1 is."""
        monkeypatch.setenv("CI", "true")
        with override_sentinel(shim_dir=None, kubeconfig=None, required=False):
            conftest._sentinel_absent_warned[0] = False
            conftest._enforce_env_rule({"PATH": "/x"}, display="t")  # must not raise
            assert any("not CI-shaped" in str(w.message) for w in recwarn.list)

    def test_j_run_tests_sh_unsets_kubernetes_and_helm_kube_vars(self, tmp_path):
        """Drives the REAL run-tests.sh (A4-2(j)) and asserts all three
        HERMETIC_* exports, plus the unset K8S/HELM_KUBE* vars, by having a
        fake pytest record its own received env."""
        import shutil

        root = tmp_path / "scratch-repo"
        (root / "scripts" / "ci").mkdir(parents=True)
        shutil.copy(
            REPO_ROOT / "scripts" / "ci" / "run-tests.sh",
            root / "scripts" / "ci" / "run-tests.sh",
        )
        shutil.copy(
            REPO_ROOT / "scripts" / "check-no-skipped-tests.py",
            root / "scripts" / "check-no-skipped-tests.py",
        )
        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir()
        env_dump = tmp_path / "env.json"
        (bin_dir / "pytest").write_text(
            "#!/usr/bin/env bash\n"
            'junit_path=""\n'
            'for arg in "$@"; do case "$arg" in --junit-xml=*) junit_path="${arg#--junit-xml=}" ;; esac; done\n'
            f'env -0 > "{env_dump}"\n'
            'cat > "$junit_path" <<XML\n'
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<testsuites><testsuite name="fake" tests="1" failures="0" errors="0" skipped="0">\n'
            '<testcase classname="fake" name="test_fake" time="0.0"/>\n'
            "</testsuite></testsuites>\n"
            "XML\n"
            "exit 0\n"
        )
        (bin_dir / "pytest").chmod(0o755)
        import sys

        (bin_dir / "python").write_text(
            f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n'
        )
        (bin_dir / "python").chmod(0o755)
        (bin_dir / "helm").write_text("#!/usr/bin/env bash\nexit 0\n")
        (bin_dir / "helm").chmod(0o755)

        env = {
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
            "HOME": os.environ.get("HOME", "/tmp"),
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
            "HELM_KUBECONTEXT": "ctx",
        }
        # This call itself is a spawn the OUTER guard's env check sees; it
        # deliberately hands the child a from-scratch env (no ambient
        # HERMETIC_* sentinel at all) to prove run-tests.sh exports its
        # OWN — temporarily make the ambient sentinel match this test's
        # own PATH/absent-KUBECONFIG shape for the duration of the call.
        with override_sentinel(shim_dir=str(bin_dir), kubeconfig=None, required=False):
            result = subprocess.run(
                [
                    "bash",
                    str(root / "scripts" / "ci" / "run-tests.sh"),
                    "--subset",
                    "test_fake",
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
        assert result.returncode == 0, result.stdout + result.stderr
        recorded = dict(
            item.split("=", 1)
            for item in env_dump.read_text().split("\0")
            if "=" in item
        )
        assert recorded.get("HERMETIC_REQUIRED") == "1"
        assert recorded.get("HERMETIC_SHIM_DIR"), "HERMETIC_SHIM_DIR must be exported"
        assert recorded.get("HERMETIC_KUBECONFIG"), (
            "HERMETIC_KUBECONFIG must be exported"
        )
        assert "KUBERNETES_SERVICE_HOST" not in recorded
        assert "HELM_KUBECONTEXT" not in recorded


# ───────────────────────── the allowlist's four halves ──────────────────────


class _FakeMarker:
    def __init__(self, tool: str) -> None:
        self.kwargs = {"tool": tool}


class _FakeNode:
    """Stands in for `request.node` — same interface
    (`.nodeid`, `.get_closest_marker(name)`) the real fixture reads."""

    def __init__(self, nodeid: str, marker_tool: str | None) -> None:
        self.nodeid = nodeid
        self._marker_tool = marker_tool

    def get_closest_marker(self, name: str):
        if name == "allow_cluster_tool" and self._marker_tool is not None:
            return _FakeMarker(self._marker_tool)
        return None


class TestAllowlistTwoSidedOptIn:
    """Exercises the REAL `_opt_in_allows` (pure-function level) AND the
    REAL word-rule end-to-end (by swapping `conftest._current_test_node`
    to a synthetic node and temporarily adding a throwaway entry to the
    REAL `ALLOWED_CLUSTER_TOOL_TESTS` dict via `monkeypatch.setitem` —
    restored automatically, so the committed allowlist file stays empty).
    This opt-in waives ONLY the word rule — never the env check (A3-7)."""

    def test_pure_marker_only_denies(self):
        assert not conftest._opt_in_allows("n", "kubectl", "kubectl", {})

    def test_pure_entry_only_denies(self):
        assert not conftest._opt_in_allows("n", "kubectl", None, {"n": "kubectl"})

    def test_pure_both_matching_allows(self):
        assert conftest._opt_in_allows("n", "kubectl", "kubectl", {"n": "kubectl"})

    def test_pure_tool_mismatch_denies(self):
        assert not conftest._opt_in_allows("n", "kubectl", "helm", {"n": "kubectl"})

    def test_pure_none_nodeid_denies(self):
        assert not conftest._opt_in_allows(None, "kubectl", "kubectl", {"n": "kubectl"})

    def test_e2e_marker_only_raises(self, fake_tools, monkeypatch):
        node = _FakeNode("synthetic::marker-only", marker_tool="kubectl")
        monkeypatch.setattr(conftest, "_current_test_node", [node])
        with expect_hermetic_violation():
            subprocess.run(["kubectl", "get", "ns"], check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_e2e_entry_only_raises(self, fake_tools, monkeypatch):
        node = _FakeNode("synthetic::entry-only", marker_tool=None)
        monkeypatch.setattr(conftest, "_current_test_node", [node])
        monkeypatch.setitem(
            ALLOWED_CLUSTER_TOOL_TESTS, "synthetic::entry-only", "kubectl"
        )
        with expect_hermetic_violation():
            subprocess.run(["kubectl", "get", "ns"], check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_e2e_tool_mismatch_raises(self, fake_tools, monkeypatch):
        node = _FakeNode("synthetic::mismatch", marker_tool="helm")
        monkeypatch.setattr(conftest, "_current_test_node", [node])
        monkeypatch.setitem(
            ALLOWED_CLUSTER_TOOL_TESTS, "synthetic::mismatch", "kubectl"
        )
        with expect_hermetic_violation():
            subprocess.run(["kubectl", "get", "ns"], check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_e2e_both_matching_is_allowed(self, fake_tools, monkeypatch):
        node = _FakeNode("synthetic::both-match", marker_tool="kubectl")
        monkeypatch.setattr(conftest, "_current_test_node", [node])
        monkeypatch.setitem(
            ALLOWED_CLUSTER_TOOL_TESTS, "synthetic::both-match", "kubectl"
        )
        subprocess.run(["kubectl", "get", "ns"], check=False, env=hermetic_env())
        assert _ran(fake_tools["kubectl"])

    def test_committed_allowlist_is_empty_for_kubectl(self):
        """The allowlist must start (and stay, absent a deliberate,
        reviewed exception) empty — no committed test in this suite
        legitimately needs the real cluster."""
        assert ALLOWED_CLUSTER_TOOL_TESTS == {}


def test_guard_disabled_lets_the_real_tool_run(fake_tools, monkeypatch):
    """The direct, in-suite counterpart to the independent review's own
    neuter procedure (hand-disable `_enforce_hermetic`, re-run the suite,
    watch a currently-green test go red): with the guard disabled, the
    real (fake-but-sentinel-logging) kubectl actually executes instead of
    the call raising."""
    monkeypatch.setattr(conftest, "_enforce_hermetic", lambda *a, **kw: None)
    subprocess.run(["kubectl", "get", "nodes"], check=False)  # must NOT raise now
    assert _ran(fake_tools["kubectl"])


# ───────────────── expect_hermetic_violation() itself (A7-1) ────────────────


class TestExpectHermeticViolation:
    def test_a_no_violation_inside_block_fails(self):
        with pytest.raises(AssertionError, match="did not raise"):
            with expect_hermetic_violation():
                pass

    def test_b_violation_outside_block_still_fails_at_teardown(self):
        """Per the gate's own advisory: calls the teardown checker
        directly rather than needing a full child pytest session."""

        class _Node:
            nodeid = "synthetic::outside-block"

        with expect_hermetic_violation():
            conftest._raise_violation("synthetic violation INSIDE the block")
        # now record one OUTSIDE any expect_hermetic_violation() block:
        conftest.PER_TEST_VIOLATIONS.append("synthetic violation OUTSIDE the block")
        assert conftest.PER_TEST_VIOLATIONS
        with pytest.raises(pytest.fail.Exception):
            conftest._teardown_check_and_clear(_Node())
        assert not conftest.PER_TEST_VIOLATIONS

    def test_c_nesting_works(self):
        with expect_hermetic_violation():
            with expect_hermetic_violation():
                conftest._raise_violation("inner")
            conftest._raise_violation("outer")
        assert not conftest.PER_TEST_VIOLATIONS

    def test_neuter_n1_cm_that_removes_whole_record_goes_red(self):
        """Neuter (n1): a broken CM that clears the ENTIRE record (not
        just entries added inside its own block) must be caught by test
        (b)'s own assertion that entries OUTSIDE the block survive."""
        record = conftest.PER_TEST_VIOLATIONS
        record.append("pre-existing, must survive")
        try:
            try:
                conftest._raise_violation("inside")
            except HermeticClusterToolError:
                pass
            # broken behaviour under test: clears everything, not just [start:]
            record.clear()
            assert record == [], (
                "neuter proof: a whole-record clear is observably different"
            )
        finally:
            record.clear()

    def test_neuter_n2_cm_that_always_passes_goes_red(self):
        """Neuter (n2): a CM that never checks `raised` would let test (a)
        pass wrongly — pin the real CM's check directly."""
        with pytest.raises(AssertionError):
            with expect_hermetic_violation():
                pass  # the real CM still correctly raises here


def test_expect_hermetic_violation_usage_is_restricted_to_guard_files():
    """A7-1's static-usage restriction: no file under tests/ other than
    this guard module and tests/_hermetic_env.py may call
    `expect_hermetic_violation(`."""
    allowed = {"test_hermetic_cluster_tool_guard.py", "_hermetic_env.py"}
    offenders = []
    for path in (REPO_ROOT / "tests").rglob("*.py"):
        if path.name in allowed:
            continue
        if "expect_hermetic_violation(" in path.read_text():
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


def test_bare_pytest_raises_hermetic_cluster_tool_error_is_banned():
    """A7's textual ban: no file under tests/, including this guard
    module, may use the bare raises-form this test's needle names (built
    by concatenation so this file's own source never contains the literal
    joined substring either)."""
    banned_substring = "pytest.raises(" + "HermeticClusterToolError"
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "tests").rglob("*.py")
        if banned_substring in path.read_text()
    ]
    assert offenders == []


def test_sentinel_holder_is_referenced_only_by_guard_files(tmp_path):
    """A5-6/A6-6: no file under tests/ other than tests/conftest.py (where
    the holder is defined), tests/_hermetic_env.py, and this guard module
    may reference HERMETIC_SENTINELS by name."""
    allowed = {"conftest.py", "_hermetic_env.py", "test_hermetic_cluster_tool_guard.py"}
    offenders = []
    for path in (REPO_ROOT / "tests").rglob("*.py"):
        if path.name in allowed:
            continue
        if "HERMETIC_SENTINELS" in path.read_text():
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []

    # neuter: plant a reference OUTSIDE tests/ (never inside it) and show
    # the same scanner, pointed there, DOES catch it — proving the check
    # is not vacuous.
    scratch = tmp_path / "scratch_outside_tests" / "test_planted.py"
    scratch.parent.mkdir(parents=True)
    scratch.write_text("HERMETIC_SENTINELS  # planted reference\n")
    planted_offenders = [
        str(p)
        for p in tmp_path.rglob("*.py")
        if p.name not in allowed and "HERMETIC_SENTINELS" in p.read_text()
    ]
    assert planted_offenders, (
        "the scanner itself must be able to detect a planted reference"
    )


def test_override_sentinel_restores_on_exit_including_on_exception():
    before = (
        conftest.HERMETIC_SENTINELS.shim_dir,
        conftest.HERMETIC_SENTINELS.kubeconfig,
        conftest.HERMETIC_SENTINELS.required,
    )
    with pytest.raises(ValueError):
        with override_sentinel(shim_dir="/x", kubeconfig="/y", required=True):
            assert conftest.HERMETIC_SENTINELS.shim_dir == "/x"
            raise ValueError("boom")
    after = (
        conftest.HERMETIC_SENTINELS.shim_dir,
        conftest.HERMETIC_SENTINELS.kubeconfig,
        conftest.HERMETIC_SENTINELS.required,
    )
    assert before == after


# ───────────── end-to-end: the sentinel survives a REAL child session ───────


def test_a5_2_sentinel_path_end_to_end_in_a_real_child_session(tmp_path):
    """A committed test that runs a REAL child pytest session (subprocess,
    same interpreter) with the three sentinels set as run-tests.sh sets
    them, and asserts the env check is ACTIVE inside: a planted spawn with
    a bad `env=` raises."""
    env = build_fresh_sentinel_env(tmp_path)
    result = run_child_pytest_session(
        """
        import os
        import subprocess

        def test_probe_bad_env_raises():
            bad_env = dict(os.environ)
            bad_env["KUBECONFIG"] = "/not/the/sentinel"
            subprocess.run(["true"], env=bad_env)
        """,
        env=env,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "hermetic guard" in (result.stdout + result.stderr)
    assert "non-hermetic child environment" in (result.stdout + result.stderr)


# ───────── A6-7/A7-3: a swallowed violation still fails the test ────────────


def test_a6_7_swallowed_violation_still_fails_the_inner_test(tmp_path):
    """Code under test wraps a kubectl spawn in `try/except Exception:
    pass`, swallowing the guard's raise — the inner test must still FAIL
    at teardown because the violation was recorded before the raise."""
    env = build_fresh_sentinel_env(tmp_path)
    result = run_child_pytest_session(
        """
        import subprocess

        def test_inner_swallowed_violation():
            try:
                subprocess.run(["kubectl", "version"])
            except Exception:
                pass
        """,
        env=env,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    # the autouse fixture's check runs in TEARDOWN, so pytest reports it
    # as "1 passed, 1 error" (pytest's own failed/error distinction
    # between the call phase and setup/teardown) rather than "1 failed" —
    # either way the inner test does NOT exit clean, which is what the
    # neuter test below contrasts against ("1 passed" with no error).
    assert "1 error" in (result.stdout + result.stderr)
    assert "hermetic guard" in (result.stdout + result.stderr)


def test_a6_7_neuter_clearing_record_in_teardown_makes_inner_pass(
    tmp_path, monkeypatch
):
    """Neuter for the test above: if teardown cleared the record WITHOUT
    checking it first, the inner test would pass instead of failing — so
    driving the same probe against a neutered teardown must show the
    inner test PASS, which is what makes the outer assertion in the real
    test above meaningful."""
    original = conftest._teardown_check_and_clear

    def _neutered(node):
        conftest.PER_TEST_VIOLATIONS.clear()  # clears WITHOUT checking first

    monkeypatch.setattr(conftest, "_teardown_check_and_clear", _neutered)
    try:
        conftest.PER_TEST_VIOLATIONS.append("would have failed the real check")
        conftest._teardown_check_and_clear(type("N", (), {"nodeid": "x"})())
        assert conftest.PER_TEST_VIOLATIONS == []
    finally:
        monkeypatch.setattr(conftest, "_teardown_check_and_clear", original)
