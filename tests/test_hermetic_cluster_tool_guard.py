"""Committed, falsifiable tests for the R3 hermetic cluster-tool guard
(tests/conftest.py) — ADDENDUM A1-1/A1-2, written after an independent
review REJECTed f72cdd7 for shipping the guard WITHOUT any committed test
(decisions-in/2026-10-11-REVIEW-bff-bump-ci-fix.md, B1 — "delete or neuter
any of them and the whole suite stays green, because nothing else in the
suite spawns kubectl").

Every test here calls the REAL guard functions in ``tests.conftest`` — none
of this logic is reimplemented. A FAKE, instantly-exiting, sentinel-logging
``kubectl``/``helm`` is placed first on ``PATH`` for every test (never a
real cluster tool), so disabling the guard is SAFE to observe here: the
fake binary just runs and leaves its sentinel file behind, instead of
anything touching a real cluster.

A1-1 requires: one test per spawn entry point; one test per helm
subcommand (both cluster-touching and safe); one test per allowlist half
(marker only, entry only, both, mismatch); and that disabling
``_enforce_hermetic`` turns at least one of these RED. The last one holds
for every ``pytest.raises(HermeticClusterToolError)`` test below — see
``test_guard_disabled_lets_the_real_tool_run`` for the direct, in-suite
proof, and the evidence folder's neuter table for the external
(hand-edit-the-source) proof the reviewer's own procedure used.
"""

from __future__ import annotations

import asyncio
import os
import pty
import stat
import subprocess
from pathlib import Path

import pytest

import tests.conftest as conftest
from tests.conftest import HermeticClusterToolError
from tests.hermetic_allowlist import ALLOWED_CLUSTER_TOOL_TESTS

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
    PATH. Returns {"kubectl": <sentinel path>, "helm": <sentinel path>}."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    sentinels = {
        "kubectl": _write_fake_tool(bin_dir, "kubectl"),
        "helm": _write_fake_tool(bin_dir, "helm"),
    }
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return sentinels


def _ran(sentinel: Path) -> bool:
    return sentinel.exists() and sentinel.stat().st_size > 0


# ───────────────────────── one test per spawn entry point ───────────────────


class TestSpawnEntryPoints:
    def test_subprocess_run_list_argv_raises(self, fake_tools):
        with pytest.raises(HermeticClusterToolError):
            subprocess.run(["kubectl", "get", "nodes"], check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_subprocess_popen_direct_raises(self, fake_tools):
        with pytest.raises(HermeticClusterToolError):
            subprocess.Popen(["kubectl", "get", "nodes"])
        assert not _ran(fake_tools["kubectl"])

    def test_subprocess_call_raises(self, fake_tools):
        with pytest.raises(HermeticClusterToolError):
            subprocess.call(["kubectl", "get", "nodes"])
        assert not _ran(fake_tools["kubectl"])

    def test_subprocess_check_call_raises(self, fake_tools):
        with pytest.raises(HermeticClusterToolError):
            subprocess.check_call(["kubectl", "get", "nodes"])
        assert not _ran(fake_tools["kubectl"])

    def test_subprocess_check_output_raises(self, fake_tools):
        with pytest.raises(HermeticClusterToolError):
            subprocess.check_output(["kubectl", "get", "nodes"])
        assert not _ran(fake_tools["kubectl"])

    def test_os_system_raises(self, fake_tools):
        with pytest.raises(HermeticClusterToolError):
            os.system("kubectl get nodes")
        assert not _ran(fake_tools["kubectl"])

    async def test_asyncio_create_subprocess_exec_raises(self, fake_tools):
        with pytest.raises(HermeticClusterToolError):
            await asyncio.create_subprocess_exec("kubectl", "get", "nodes")
        assert not _ran(fake_tools["kubectl"])

    async def test_asyncio_create_subprocess_shell_raises(self, fake_tools):
        with pytest.raises(HermeticClusterToolError):
            await asyncio.create_subprocess_shell("kubectl get nodes")
        assert not _ran(fake_tools["kubectl"])


# ───────────────────────── A1-2: evasion forms, now closed ───────────────────


class TestEvasionFormsNowClosed:
    def test_bytes_argv_raises(self, fake_tools):
        """B2 evasion: subprocess.run([b'kubectl', b'get', b'ns'])."""
        with pytest.raises(HermeticClusterToolError):
            subprocess.run([b"kubectl", b"get", b"ns"], check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_executable_kwarg_raises_even_with_decoy_argv0(self, fake_tools):
        """B2 evasion: argv[0] is a decoy; `executable=` names the REAL
        program actually invoked."""
        with pytest.raises(HermeticClusterToolError):
            subprocess.run(["x", "get", "ns"], executable="kubectl", check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_env_wrapper_raises(self, fake_tools):
        """B2 evasion: ['env', 'kubectl', 'get', 'ns']."""
        with pytest.raises(HermeticClusterToolError):
            subprocess.run(["env", "kubectl", "get", "ns"], check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_env_wrapper_with_var_assignments_raises(self, fake_tools):
        """env with leading VAR=value tokens before the real command."""
        with pytest.raises(HermeticClusterToolError):
            subprocess.run(
                ["env", "FOO=bar", "BAZ=qux", "kubectl", "get", "ns"], check=False
            )
        assert not _ran(fake_tools["kubectl"])

    def test_sh_dash_c_string_raises(self, fake_tools):
        """B2 evasion: ['sh', '-c', 'kubectl get ns']."""
        with pytest.raises(HermeticClusterToolError):
            subprocess.run(["sh", "-c", "kubectl get ns"], check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_bash_dash_c_string_raises(self, fake_tools):
        with pytest.raises(HermeticClusterToolError):
            subprocess.run(["bash", "-c", "kubectl get ns"], check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_shell_true_cd_and_kubectl_raises(self, fake_tools):
        """B2 evasion: shell=True "cd /tmp && kubectl get ns" — the FIRST
        segment ("cd /tmp") is not a violation; only the SECOND is. Proves
        every segment is checked, not just the first token of the whole
        string."""
        with pytest.raises(HermeticClusterToolError):
            subprocess.run("cd /tmp && kubectl get ns", shell=True, check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_shell_true_helm_template_then_helm_install_raises(self, fake_tools):
        """B2 evasion: shell=True "helm template x; helm install y z" — the
        first segment (template) is SAFE on its own; only the second
        (install) is a violation. Proves segment-by-segment checking."""
        with pytest.raises(HermeticClusterToolError):
            subprocess.run("helm template x; helm install y z", shell=True, check=False)
        assert not _ran(fake_tools["helm"])

    def test_shell_true_first_segment_alone_is_safe(self, fake_tools):
        """The control for the test above: `helm template x` ALONE must NOT
        raise — proves the guard isn't just pattern-matching "helm" anywhere
        in the string."""
        subprocess.run("helm template x", shell=True, check=False)
        assert _ran(fake_tools["helm"])

    def test_unparseable_shell_segment_with_kubectl_word_fails_closed(self, fake_tools):
        """A1-2: a segment that cannot be shlex-tokenized (unbalanced
        quote) but still contains the word "kubectl" must fail closed,
        not be silently skipped."""
        with pytest.raises(HermeticClusterToolError):
            subprocess.run('kubectl "unbalanced', shell=True, check=False)

    def test_unparseable_shell_segment_without_dangerous_word_is_allowed(self):
        """The fail-closed rule is scoped to the dangerous words, not to
        every unparseable string — an unrelated malformed string must not
        be blocked."""
        result = subprocess.run('echo "unbalanced', shell=True, check=False)
        assert result.returncode in (0, 1, 2)  # just must not raise


# ──────────────── A1-2: the raw exec*/posix_spawn*/spawn*/pty.spawn families ─


def _run_in_forked_child(fn) -> int:
    """Runs niladic `fn` in a forked child and returns its exit status.
    Required for the TRUE os.exec* family: on success they REPLACE the
    calling process image, so if a guard bug ever let one through, only
    this throwaway child would be replaced/exit — never the pytest worker
    running this test file. 42 = guard raised as expected; 43 = raised
    something else; 44 = returned without raising (and, for the
    non-replacing members of this family, without the real call ever
    getting far enough to replace/exit the child itself)."""
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
    """One test per DISTINCT calling convention (list-style path-first,
    list-style PATH-searched, *args-style, posix_spawn, the os.spawn*
    family, pty.spawn) — the reviewer's own B2 note groups `os.spawn*` as
    "the same class", so one representative member stands for the family;
    every other member is patched identically (see conftest.py) but not
    separately re-tested here."""

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
        """B2 explicitly names this one: "os.execvp (forked)"."""
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
        """B2 explicitly names this one. Does not replace the caller, so
        no fork needed for safety, but uses the same harness for
        consistency."""
        kubectl = os.path.join(os.path.dirname(fake_tools["kubectl"]), "kubectl")

        def _call():
            os.posix_spawn(kubectl, [kubectl, "get", "ns"], dict(os.environ))

        with pytest.raises(HermeticClusterToolError):
            _call()
        assert not _ran(fake_tools["kubectl"])

    def test_os_posix_spawnp_raises(self, fake_tools):
        """B2 explicitly names this one."""
        with pytest.raises(HermeticClusterToolError):
            os.posix_spawnp("kubectl", ["kubectl", "get", "ns"], dict(os.environ))
        assert not _ran(fake_tools["kubectl"])

    def test_os_spawnv_raises(self, fake_tools):
        """Representative of the os.spawn* family (B2: "the same class")."""
        with pytest.raises(HermeticClusterToolError):
            os.spawnv(os.P_WAIT, "kubectl", ["kubectl", "get", "ns"])
        assert not _ran(fake_tools["kubectl"])

    def test_pty_spawn_raises(self, fake_tools):
        """B2 explicitly names this one."""
        with pytest.raises(HermeticClusterToolError):
            pty.spawn(["kubectl", "get", "ns"])
        assert not _ran(fake_tools["kubectl"])


# ──────────────────── one test per helm subcommand (both classes) ───────────


@pytest.mark.parametrize(
    "subcommand", ["template", "lint", "dependency", "version", "show"]
)
def test_helm_safe_subcommand_does_not_raise(fake_tools, subcommand):
    subprocess.run(["helm", subcommand, "x"], check=False)
    assert _ran(fake_tools["helm"])


@pytest.mark.parametrize(
    "subcommand",
    ["upgrade", "status", "get", "install", "rollback", "uninstall", "list"],
)
def test_helm_cluster_subcommand_raises(fake_tools, subcommand):
    with pytest.raises(HermeticClusterToolError):
        subprocess.run(["helm", subcommand, "x"], check=False)
    assert not _ran(fake_tools["helm"])


def test_helm_with_kube_context_flag_before_cluster_subcommand_still_raises(
    fake_tools,
):
    """B2's own repro: `helm --kube-context ctx upgrade ...` — the
    SECOND token is a global flag, not the subcommand, so a naive
    argv[1]-only check would read "--kube-context" (not in the safe set,
    so this actually still raises — but by ACCIDENT, not by design). This
    test pins the observed behaviour so a future change to the safe-set
    matching logic cannot silently flip it to "safe" without failing here."""
    with pytest.raises(HermeticClusterToolError):
        subprocess.run(["helm", "--kube-context", "ctx", "upgrade", "x"], check=False)


def test_unrelated_command_is_never_blocked(fake_tools):
    """Sanity/regression control: an ordinary command is never touched."""
    result = subprocess.run(["echo", "hello"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "hello" in result.stdout


# ───────────────────────── A1-1: the allowlist's four halves ────────────────


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
    REAL `_enforce_hermetic` end-to-end (by swapping `conftest.
    _current_test_node` to a synthetic node and temporarily adding a
    throwaway entry to the REAL `ALLOWED_CLUSTER_TOOL_TESTS` dict via
    `monkeypatch.setitem` — restored automatically, so the committed
    allowlist file stays empty) — never a reimplementation of the logic."""

    # -- pure-function level (the four combinations, directly) --

    def test_pure_marker_only_denies(self):
        assert not conftest._opt_in_allows("n", "kubectl", "kubectl", {})

    def test_pure_entry_only_denies(self):
        assert not conftest._opt_in_allows("n", "kubectl", None, {"n": "kubectl"})

    def test_pure_both_matching_allows(self):
        assert conftest._opt_in_allows("n", "kubectl", "kubectl", {"n": "kubectl"})

    def test_pure_tool_mismatch_denies(self):
        assert not conftest._opt_in_allows("n", "kubectl", "helm", {"n": "kubectl"})

    # -- end-to-end, through the real fixture-populated state + real guard --

    def test_e2e_marker_only_raises(self, fake_tools, monkeypatch):
        node = _FakeNode("synthetic::marker-only", marker_tool="kubectl")
        monkeypatch.setattr(conftest, "_current_test_node", [node])
        with pytest.raises(HermeticClusterToolError):
            subprocess.run(["kubectl", "get", "ns"], check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_e2e_entry_only_raises(self, fake_tools, monkeypatch):
        node = _FakeNode("synthetic::entry-only", marker_tool=None)
        monkeypatch.setattr(conftest, "_current_test_node", [node])
        monkeypatch.setitem(
            ALLOWED_CLUSTER_TOOL_TESTS, "synthetic::entry-only", "kubectl"
        )
        with pytest.raises(HermeticClusterToolError):
            subprocess.run(["kubectl", "get", "ns"], check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_e2e_tool_mismatch_raises(self, fake_tools, monkeypatch):
        node = _FakeNode("synthetic::mismatch", marker_tool="helm")
        monkeypatch.setattr(conftest, "_current_test_node", [node])
        monkeypatch.setitem(
            ALLOWED_CLUSTER_TOOL_TESTS, "synthetic::mismatch", "kubectl"
        )
        with pytest.raises(HermeticClusterToolError):
            subprocess.run(["kubectl", "get", "ns"], check=False)
        assert not _ran(fake_tools["kubectl"])

    def test_e2e_both_matching_is_allowed(self, fake_tools, monkeypatch):
        node = _FakeNode("synthetic::both-match", marker_tool="kubectl")
        monkeypatch.setattr(conftest, "_current_test_node", [node])
        monkeypatch.setitem(
            ALLOWED_CLUSTER_TOOL_TESTS, "synthetic::both-match", "kubectl"
        )
        subprocess.run(["kubectl", "get", "ns"], check=False)
        assert _ran(fake_tools["kubectl"])

    def test_committed_allowlist_is_empty_for_kubectl(self):
        """The allowlist must start (and stay, absent a deliberate,
        reviewed exception) empty — no committed test in this suite
        legitimately needs the real cluster."""
        assert ALLOWED_CLUSTER_TOOL_TESTS == {}


# ──────────────── A1-1: proof the guard is not vacuous, in-suite ────────────


def test_guard_disabled_lets_the_real_tool_run(fake_tools, monkeypatch):
    """The direct, in-suite counterpart to the review's own neuter
    procedure (hand-disable `_enforce_hermetic`, re-run the suite, watch a
    currently-green test go red). Every `pytest.raises(HermeticClusterTool
    Error)` test above already goes RED under that exact edit — this test
    makes the CONTRAST explicit and fast to re-check without editing the
    source: with the guard disabled, the real (fake-but-sentinel-logging)
    kubectl actually executes instead of the call raising."""
    monkeypatch.setattr(conftest, "_enforce_hermetic", lambda *a, **kw: None)
    subprocess.run(["kubectl", "get", "nodes"], check=False)  # must NOT raise now
    assert _ran(fake_tools["kubectl"])
