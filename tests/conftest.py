"""Test configuration and fixtures.

Logging and telemetry are configured once per test session so that the
@log_call aspect emits the same structured output it does in production
(all to stdout, per ADR-014.4). `caplog` can then assert on those records.
"""

import logging
import os
import subprocess  # noqa: S404 — used only for chart-rendering test injection below

import pytest
from fastapi.testclient import TestClient

# `tests/neuter_fixture/` (and its src/-layout sibling,
# `tests/neuter_fixture_src/` -- review round 2's structural-fix item 4)
# are STATIC TEMPLATES for the fast neuter harness's self-proofs (SPEC v3
# §11): each `test_guarded.py` does `from guarded import ...`, a module
# that only exists once the fixture is copied into a throwaway git repo
# (never this one). Collecting either here would break every `make test`
# run with an ImportError. The self-proof tests copy these files into
# `tmp_path` and spawn a FRESH pytest subprocess there, whose rootdir has
# no `conftest.py` of its own -- this `collect_ignore` only ever applies
# to collection rooted at THIS repo.
collect_ignore = ["neuter_fixture", "neuter_fixture_src"]

# Clear stale evidence from a PRIOR session's neuter-harness tests
# (tests/_neuter_test_evidence.py) at the start of every session -- it's
# gitignored scratch space, not evidence anyone reads back later.
from tests._neuter_test_evidence import (  # noqa: E402
    cleanup_root as _cleanup_neuter_test_evidence,
)

_cleanup_neuter_test_evidence()

# ─────────── Chart-rendering subprocess injection (FQDN-only chart) ───────────
# ADR-045 (amended 2026-05-19) made the chart FQDN-only — `externalLLM.host`
# and `observability.external.{langfuse,tempo,loki}Host` are `required`
# whenever the corresponding feature flag is on (the default). 13+ tests
# shell out to `helm template` to render the chart and inspect manifests;
# threading the four --set flags through every test would be brittle drift
# bait. Instead we monkey-patch subprocess.run *here* so any invocation that
# matches `<helm> template ...` gets the FQDN flags appended transparently.
# Tests that already pass them get harmless duplication (last --set wins).
_FQDN_INJECT_FLAGS = (
    "--set",
    "externalLLM.host=llm.test.invalid",
    "--set",
    "observability.external.langfuseHost=langfuse.test.invalid",
    "--set",
    "observability.external.tempoHost=tempo.test.invalid",
    "--set",
    "observability.external.lokiHost=loki.test.invalid",
)
_FQDN_KEYS = (
    "externalLLM.host=",
    "observability.external.langfuseHost=",
    "observability.external.tempoHost=",
    "observability.external.lokiHost=",
)


def _maybe_inject_fqdn_flags(args):
    """If ``args`` is a `helm template ...` argv, append the four FQDN
    --set flags so the chart's `required` guards are satisfied. Idempotent:
    if any of the four FQDN --set flags are already present, leave args
    alone (the test is being deliberate)."""
    if not isinstance(args, (list, tuple)):
        return args
    a = list(args)
    if len(a) < 2:
        return args
    # Match by argv[1] == "template" AND argv[0] looks like a helm binary.
    if a[1] != "template":
        return args
    arg0 = str(a[0])
    if not (arg0.endswith("/helm") or arg0 == "helm"):
        return args
    joined = " ".join(str(x) for x in a)
    if any(k in joined for k in _FQDN_KEYS):
        return args  # already FQDN-aware
    return a + list(_FQDN_INJECT_FLAGS)


_orig_subprocess_run = subprocess.run


def _patched_subprocess_run(*args, **kwargs):
    if args:
        args = (_maybe_inject_fqdn_flags(args[0]),) + args[1:]
    elif "args" in kwargs:
        kwargs["args"] = _maybe_inject_fqdn_flags(kwargs["args"])
    return _orig_subprocess_run(*args, **kwargs)


subprocess.run = _patched_subprocess_run

# ─────────── Hermetic cluster-tool guard: process layer + in-process diagnostic ────
# PR #380, CI run 37878027812: two tests in tests/test_deploy_runner.py called
# runner.main(...) without stubbing the mesh gate. The mesh gate's own `_run`
# seam (scripts/deploy/mesh.py) shelled to a REAL kubectl. On the laptop (a
# live k3s cluster) that silently "worked"; in CI (no cluster) P0 aborted
# MESH UNSAFE and both tests flipped from pass to failure. Nothing detected
# the leak mechanically.
#
# SPEC 2026-10-11-SPEC-test-parity-scripted-enforcement.md R3, carried
# through ADDENDUM A1 (committed tests), then rebuilt twice more after the
# independent review found new bypasses each round it was asked to parse
# argv forms and enumerate wrappers (env/sh -c/shell=True/...). ADDENDUM A2
# through A7 changed the INSTRUMENT instead of adding more forms:
#
#   * The PROCESS layer is now the control (scripts/ci/run-tests.sh): a
#     shim dir placed first on PATH ships BOTH a `kubectl` and a `helm`
#     that reject anything the chart-rendering/linting subset doesn't need,
#     logging every call to a reject log the script fails on if non-empty.
#     This catches any child that resolves the tool through PATH — shell
#     constructs, child interpreters, every wrapper — without parsing
#     anything.
#   * The code below is the IN-PROCESS layer: a DIAGNOSTIC, not the
#     control. It is a fail-closed WORD check (collect every string a
#     spawn call carries, split into words, raise if `kubectl` appears
#     anywhere, or if `helm` appears without the call being one of the
#     explicitly positional-safe chart-only invocations) PLUS an env
#     equality check that runs on EVERY spawn regardless of which tool it
#     names (a rebuilt child environment could run any program that then
#     shells to a cluster tool). What it can MISS (`kube""ctl`, a
#     versioned binary name, a child interpreter, ...) is accepted and
#     owned by the shim — tests/test_hermetic_cluster_tool_guard.py
#     measures both halves separately.
#
# Every raise is also RECORDED (per-test, or session-level when no test
# node is active) before it propagates, so a test whose own code swallows
# the exception still fails at teardown — tests/_hermetic_env.py's
# `expect_hermetic_violation` context manager is the one sanctioned way
# to consume a deliberately-triggered record entry; a bare `pytest.raises`
# on the guard's error type is banned in tests/ (a static test enforces
# it).
import asyncio  # noqa: E402
import pty  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import warnings  # noqa: E402

from tests.hermetic_allowlist import ALLOWED_CLUSTER_TOOL_TESTS  # noqa: E402

#: helm subcommands that never touch a live cluster (chart rendering /
#: static linting / vendoring only) — the spec's positional ALLOW rule.
_HELM_SAFE_SUBCOMMANDS = frozenset(
    {"template", "lint", "dependency", "version", "show"}
)
#: `dependency` is restricted to `list` — `build`/`update` touch the
#: network and rewrite Chart.lock / charts/*.tgz; no test needs them.
_HELM_DEPENDENCY_SAFE_ARGS = frozenset({"list"})
_HELM_UNSAFE_ARG_PREFIXES = ("--validate", "--dry-run", "--kube")

#: word-split boundary: any character outside [A-Za-z0-9_.-].
_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9_.-]+")

_HELM_KUBE_ENV_PREFIX = "HELM_KUBE"
_KUBERNETES_ENV_NAMES = ("KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT")


class HermeticClusterToolError(AssertionError):
    """Raised the instant a test spawn names a forbidden cluster tool as a
    word, or would hand its child a non-hermetic environment."""


class _SentinelHolder:
    """The frozen hermetic sentinel values this pytest session started
    with. ``tests/_hermetic_env.py::override_sentinel()`` is the only
    sanctioned way to change these once frozen — a static test in
    ``tests/test_hermetic_cluster_tool_guard.py`` asserts no other file
    under ``tests/`` references this holder by name."""

    __slots__ = ("shim_dir", "kubeconfig", "required")

    def __init__(self) -> None:
        self.shim_dir: str | None = None
        self.kubeconfig: str | None = None
        self.required: bool = False

    def freeze_from_environ(self) -> None:
        self.shim_dir = os.environ.get("HERMETIC_SHIM_DIR") or None
        self.kubeconfig = os.environ.get("HERMETIC_KUBECONFIG") or None
        self.required = os.environ.get("HERMETIC_REQUIRED") == "1"


#: Frozen the instant this module is imported — deliberately textually
#: ABOVE the AUDITTRACE_* wipe block below, as defence in depth (untested
#: by design: this ordering has no committed test of its own; the
#: `AUDITTRACE_` prefix neuter below is what proves the NAMES must avoid
#: the wipe, which is the part that IS tested).
HERMETIC_SENTINELS = _SentinelHolder()
HERMETIC_SENTINELS.freeze_from_environ()

#: Per-test violation record. Every in-process raise appends here FIRST,
#: then raises — so code under test that swallows the exception
#: (`except Exception: pass`) still leaves evidence the autouse fixture's
#: teardown checks. Allowlisted calls never reach this.
PER_TEST_VIOLATIONS: list[str] = []

#: Violations recorded while no test node is active (import time,
#: collection, or session-scoped fixture setup) — checked at session end.
_SESSION_VIOLATIONS: list[str] = []

#: Set by the autouse fixture below for the duration of each test.
_current_test_node: list[object] = [None]

#: Fires at most once per session (A4-2(h): "exactly ONE warning").
_sentinel_absent_warned: list[bool] = [False]


def _decode(token: object) -> str:
    if isinstance(token, bytes):
        return token.decode("utf-8", errors="surrogateescape")
    return str(token)


def _basename(token: object) -> str:
    try:
        return os.path.basename(_decode(token))
    except Exception:  # pragma: no cover - defensive, str() on anything works
        return str(token)


def _collect_words(*sources: object) -> set[str]:
    """Every word any of ``sources`` carries: each list/tuple element
    decoded, or the source itself if it's a single string/bytes value,
    split on any character outside [A-Za-z0-9_.-], basenamed."""
    words: set[str] = set()
    for source in sources:
        if source is None:
            continue
        strings = (
            [_decode(x) for x in source]
            if isinstance(source, (list, tuple))
            else [_decode(source)]
        )
        for s in strings:
            for raw in _WORD_SPLIT_RE.split(s):
                if raw:
                    words.add(os.path.basename(raw))
    return words


def _helm_call_is_allowed(
    cmd: object, *, shell: bool, executable: object | None
) -> bool:
    """The positional ALLOW rule: a list-form (never shell=True) helm
    invocation whose argv[1] is one of the safe subcommands, whose
    `dependency` (if any) is followed only by `list`, none of whose
    arguments start with --validate/--dry-run/--kube(config), and whose
    `executable=` (if given) is itself helm."""
    if shell or not isinstance(cmd, (list, tuple)) or not cmd:
        return False
    tokens = [_decode(t) for t in cmd]
    if _basename(tokens[0]) != "helm":
        return False
    if len(tokens) < 2 or tokens[1] not in _HELM_SAFE_SUBCOMMANDS:
        return False
    if tokens[1] == "dependency" and (
        len(tokens) < 3 or tokens[2] not in _HELM_DEPENDENCY_SAFE_ARGS
    ):
        return False
    if any(t.startswith(_HELM_UNSAFE_ARG_PREFIXES) for t in tokens[1:]):
        return False
    if executable is not None and _basename(executable) != "helm":
        return False
    return True


def _opt_in_allows(
    nodeid: str | None,
    tool: str,
    marker_tool: str | None,
    allowlist: dict[str, str],
) -> bool:
    """The two-sided opt-in: the marker's declared tool, the allowlist
    entry's tool, AND the tool actually being invoked must ALL three
    agree, or the guard still denies. Waives only the word-rule check
    below — never the env check (no opt-in at the process layer)."""
    if nodeid is None:
        return False
    allowed_tool = allowlist.get(nodeid)
    return (
        marker_tool is not None
        and allowed_tool is not None
        and marker_tool == allowed_tool == tool
    )


def _node_marker_tool() -> tuple[str | None, str | None]:
    node = _current_test_node[0]
    if node is None:
        return None, None
    marker = node.get_closest_marker("allow_cluster_tool")
    return node.nodeid, (marker.kwargs.get("tool") if marker is not None else None)


def _record_violation(message: str) -> None:
    if _current_test_node[0] is not None:
        PER_TEST_VIOLATIONS.append(message)
    else:
        _SESSION_VIOLATIONS.append(message)


def _raise_violation(message: str) -> None:
    _record_violation(message)
    raise HermeticClusterToolError(message)


def _enforce_word_rule(
    cmd: object, *, executable: object | None, shell: bool, display: str
) -> None:
    words = _collect_words(cmd, executable)
    nodeid, marker_tool = _node_marker_tool()
    if "kubectl" in words:
        if _opt_in_allows(nodeid, "kubectl", marker_tool, ALLOWED_CLUSTER_TOOL_TESTS):
            return
        _raise_violation(
            f"hermetic guard: {nodeid!r} names kubectl ({display}) — forbidden "
            f"in the test suite (SPEC 2026-10-11-SPEC-test-parity-scripted-"
            f"enforcement.md R3, ADDENDUM A2+). Stub the seam instead (e.g. "
            f"inject a `mesh_gate=_healthy_gate()` the way the sibling "
            f"DeployRunner tests do). A deliberate exception needs BOTH "
            f"@pytest.mark.allow_cluster_tool(tool='kubectl') AND a matching "
            f"entry in tests/hermetic_allowlist.py — the kubectl allowlist "
            f"stays empty."
        )
        return
    if "helm" in words and not _helm_call_is_allowed(
        cmd, shell=shell, executable=executable
    ):
        if _opt_in_allows(nodeid, "helm", marker_tool, ALLOWED_CLUSTER_TOOL_TESTS):
            return
        _raise_violation(
            f"hermetic guard: {nodeid!r} names helm outside the safe "
            f"chart-only subcommand set {{{', '.join(sorted(_HELM_SAFE_SUBCOMMANDS))}}} "
            f"({display}). Stub the seam, or opt in with BOTH "
            f"@pytest.mark.allow_cluster_tool(tool='helm') AND a matching "
            f"tests/hermetic_allowlist.py entry."
        )


def _resolve_child_env(env: dict | None) -> dict:
    return dict(env) if env is not None else dict(os.environ)


def _env_violations(env: dict) -> list[str]:
    problems: list[str] = []
    if env.get("KUBECONFIG") != HERMETIC_SENTINELS.kubeconfig:
        problems.append(
            f"KUBECONFIG={env.get('KUBECONFIG')!r} (expected sentinel "
            f"{HERMETIC_SENTINELS.kubeconfig!r})"
        )
    path_value = env.get("PATH") or ""
    path_first = path_value.split(os.pathsep)[0] if path_value else ""
    if path_first != HERMETIC_SENTINELS.shim_dir:
        problems.append(
            f"PATH[0]={path_first!r} (expected shim dir {HERMETIC_SENTINELS.shim_dir!r})"
        )
    for name in _KUBERNETES_ENV_NAMES:
        if name in env:
            problems.append(f"{name} is set")
    for name in env:
        if name.startswith(_HELM_KUBE_ENV_PREFIX):
            problems.append(f"{name} is set")
    return problems


def _enforce_env_rule(env: dict | None, *, display: str) -> None:
    """Runs on EVERY spawn, regardless of which tool (if any) it names —
    a rebuilt child env could run a program that itself shells to a
    cluster tool. Fails closed only when HERMETIC_REQUIRED=1; otherwise,
    if the sentinel is simply absent, this emits ONE session warning and
    does not raise (a developer running bare pytest is not CI-shaped, but
    the word check and the rest of the suite must still run)."""
    if HERMETIC_SENTINELS.shim_dir and HERMETIC_SENTINELS.kubeconfig:
        problems = _env_violations(_resolve_child_env(env))
        if problems:
            _raise_violation(
                f"hermetic guard: {display} would receive a non-hermetic "
                f"child environment: {'; '.join(problems)}."
            )
        return
    if HERMETIC_SENTINELS.required:
        _raise_violation(
            f"hermetic guard: {display} — HERMETIC_REQUIRED=1 but "
            f"HERMETIC_SHIM_DIR/HERMETIC_KUBECONFIG are absent."
        )
        return
    if not _sentinel_absent_warned[0]:
        _sentinel_absent_warned[0] = True
        warnings.warn(
            "hermetic guard: HERMETIC_SHIM_DIR/HERMETIC_KUBECONFIG are not "
            "set — this run is not CI-shaped (scripts/ci/run-tests.sh sets "
            "them). The kubectl/helm word check still applies; the child-"
            "environment equality check does not.",
            stacklevel=3,
        )


def _enforce_hermetic(
    cmd: object,
    *,
    executable: object | None = None,
    shell: bool = False,
    env: dict | None = None,
    display_prefix: str = "",
) -> None:
    display = f"{display_prefix}{cmd!r}"
    if executable is not None:
        display += f" executable={executable!r}"
    _enforce_word_rule(cmd, executable=executable, shell=shell, display=display)
    _enforce_env_rule(env, display=display)


def _violation_teardown_message(nodeid: str, violations: list[str]) -> str:
    return (
        f"hermetic guard: {len(violations)} violation(s) were recorded "
        f"during {nodeid!r} even though the test's own code did not "
        f"propagate the error (e.g. a swallowed except Exception): "
        + "; ".join(violations)
    )


def _teardown_check_and_clear(node: object) -> None:
    violations = list(PER_TEST_VIOLATIONS)
    PER_TEST_VIOLATIONS.clear()
    if violations:
        pytest.fail(_violation_teardown_message(node.nodeid, violations), pytrace=False)


def pytest_sessionfinish(session: object, exitstatus: int) -> None:
    """A6-7/A7-4: violations recorded with no active test node (import,
    collection, or session-scoped fixture setup) are checked here."""
    if _SESSION_VIOLATIONS:
        violations = list(_SESSION_VIOLATIONS)
        print(
            f"\nhermetic guard: {len(violations)} violation(s) were recorded "
            f"outside any active test node: {violations!r}",
            file=sys.stderr,
        )
        session.exitstatus = 1


@pytest.fixture(autouse=True)
def _hermetic_cluster_tool_guard(request):
    """Stashes the running test's node (so patched spawn points can name
    it and look up its marker), resets the per-test violation record, and
    at teardown fails the test if anything was recorded even though the
    test's own code swallowed the raise."""
    _current_test_node[0] = request.node
    PER_TEST_VIOLATIONS.clear()
    yield
    _current_test_node[0] = None
    _teardown_check_and_clear(request.node)


_orig_popen_init = subprocess.Popen.__init__


def _patched_popen_init(
    self, args=(), *a, executable=None, shell=False, env=None, **kw
):
    _enforce_hermetic(args, executable=executable, shell=shell, env=env)
    return _orig_popen_init(
        self, args, *a, executable=executable, shell=shell, env=env, **kw
    )


subprocess.Popen.__init__ = _patched_popen_init

_orig_os_system = os.system


def _patched_os_system(command):
    _enforce_hermetic(command, shell=True)
    return _orig_os_system(command)


os.system = _patched_os_system

_orig_create_subprocess_exec = asyncio.create_subprocess_exec
_orig_create_subprocess_shell = asyncio.create_subprocess_shell


async def _patched_create_subprocess_exec(program, *args, **kw):
    _enforce_hermetic(
        [program, *args], executable=kw.get("executable"), env=kw.get("env")
    )
    return await _orig_create_subprocess_exec(program, *args, **kw)


async def _patched_create_subprocess_shell(cmd, *args, **kw):
    _enforce_hermetic(cmd, shell=True, env=kw.get("env"))
    return await _orig_create_subprocess_shell(cmd, *args, **kw)


asyncio.create_subprocess_exec = _patched_create_subprocess_exec
asyncio.create_subprocess_shell = _patched_create_subprocess_shell
asyncio.subprocess.create_subprocess_exec = _patched_create_subprocess_exec
asyncio.subprocess.create_subprocess_shell = _patched_create_subprocess_shell

# ── the raw os.exec*/os.posix_spawn*/os.spawn* families + pty.spawn ──
# os.execv/execve/execl/... (and every other exec* variant CPython funnels
# through these at the C level) REPLACE the calling process image on
# success, so a guard bug here could kill more than the one test — see the
# fork-isolated harness in tests/test_hermetic_cluster_tool_guard.py, which
# is what proves these are covered (identity-pinned by A4-1's test too).

_orig_execv = os.execv
_orig_execve = os.execve
_orig_execvp = os.execvp
_orig_execvpe = os.execvpe
_orig_execl = os.execl
_orig_execle = os.execle
_orig_execlp = os.execlp
_orig_execlpe = os.execlpe
_orig_posix_spawn = os.posix_spawn
_orig_posix_spawnp = os.posix_spawnp
_orig_spawnv = os.spawnv
_orig_spawnve = os.spawnve
_orig_spawnvp = os.spawnvp
_orig_spawnvpe = os.spawnvpe
_orig_spawnl = os.spawnl
_orig_spawnle = os.spawnle
_orig_spawnlp = os.spawnlp
_orig_spawnlpe = os.spawnlpe
_orig_pty_spawn = pty.spawn


def _check_path_and_argv(
    path_like: object, argv: list[object], env: dict | None = None
) -> None:
    _enforce_hermetic(list(argv), executable=path_like, env=env)


def _patched_execv(path, args):
    _check_path_and_argv(path, list(args))
    return _orig_execv(path, args)


def _patched_execve(path, args, env):
    _check_path_and_argv(path, list(args), env=env)
    return _orig_execve(path, args, env)


def _patched_execvp(file, args):
    _check_path_and_argv(file, list(args))
    return _orig_execvp(file, args)


def _patched_execvpe(file, args, env):
    _check_path_and_argv(file, list(args), env=env)
    return _orig_execvpe(file, args, env)


def _patched_execl(path, *args):
    _check_path_and_argv(path, list(args))
    return _orig_execl(path, *args)


def _patched_execle(path, *args):
    _check_path_and_argv(path, list(args))
    return _orig_execle(path, *args)


def _patched_execlp(file, *args):
    _check_path_and_argv(file, list(args))
    return _orig_execlp(file, *args)


def _patched_execlpe(file, *args):
    _check_path_and_argv(file, list(args))
    return _orig_execlpe(file, *args)


def _patched_posix_spawn(path, argv, env, **kw):
    _check_path_and_argv(path, list(argv), env=env)
    return _orig_posix_spawn(path, argv, env, **kw)


def _patched_posix_spawnp(file, argv, env, **kw):
    _check_path_and_argv(file, list(argv), env=env)
    return _orig_posix_spawnp(file, argv, env, **kw)


def _patched_spawnv(mode, path, args):
    _check_path_and_argv(path, list(args))
    return _orig_spawnv(mode, path, args)


def _patched_spawnve(mode, path, args, env):
    _check_path_and_argv(path, list(args), env=env)
    return _orig_spawnve(mode, path, args, env)


def _patched_spawnvp(mode, file, args):
    _check_path_and_argv(file, list(args))
    return _orig_spawnvp(mode, file, args)


def _patched_spawnvpe(mode, file, args, env):
    _check_path_and_argv(file, list(args), env=env)
    return _orig_spawnvpe(mode, file, args, env)


def _patched_spawnl(mode, path, *args):
    _check_path_and_argv(path, list(args))
    return _orig_spawnl(mode, path, *args)


def _patched_spawnle(mode, path, *args):
    _check_path_and_argv(path, list(args))
    return _orig_spawnle(mode, path, *args)


def _patched_spawnlp(mode, file, *args):
    _check_path_and_argv(file, list(args))
    return _orig_spawnlp(mode, file, *args)


def _patched_spawnlpe(mode, file, *args):
    _check_path_and_argv(file, list(args))
    return _orig_spawnlpe(mode, file, *args)


def _patched_pty_spawn(argv, *a, **kw):
    argv_list = [argv] if isinstance(argv, (str, bytes)) else list(argv)
    _check_path_and_argv(argv_list[0] if argv_list else "", argv_list)
    return _orig_pty_spawn(argv, *a, **kw)


os.execv = _patched_execv
os.execve = _patched_execve
os.execvp = _patched_execvp
os.execvpe = _patched_execvpe
os.execl = _patched_execl
os.execle = _patched_execle
os.execlp = _patched_execlp
os.execlpe = _patched_execlpe
os.posix_spawn = _patched_posix_spawn
os.posix_spawnp = _patched_posix_spawnp
os.spawnv = _patched_spawnv
os.spawnve = _patched_spawnve
os.spawnvp = _patched_spawnvp
os.spawnvpe = _patched_spawnvpe
os.spawnl = _patched_spawnl
os.spawnle = _patched_spawnle
os.spawnlp = _patched_spawnlp
os.spawnlpe = _patched_spawnlpe
pty.spawn = _patched_pty_spawn

# Test isolation: clear all AUDITTRACE_* env vars so a developer's local .env
# (with real PostgreSQL credentials etc.) doesn't leak into tests.
# This must run BEFORE importing audittrace.config so the .env-skip
# logic in config._ENV_FILE sees AUDITTRACE_ENV=test.
#
# Exception: AUDITTRACE_TEST_POSTGRES_URL is test-only wiring (used by
# tests/test_rls_isolation.py to pick a real Postgres instance over
# the SQLite default). It's not app config, so it must survive the wipe —
# otherwise CI and local-dev RLS integration tests silently fall back
# to a stale compose URL and skip / fail.
# AUDITTRACE_NEUTER_LOCK (SPEC v3 §10 SF-2) is the fast neuter harness's
# heavy-cap lock-path override -- every harness test points it at a
# `tmp_path` file so the suite never contends with (or is blocked by) the
# real, shared lock a concurrent `make test`/pool run would hold.
# (The pool's OTHER per-neuter env vars -- NEUTER_PATHCHECK_{MODULE,EXPECT,
# LOG}, NEUTER_FAKE_PG_STATE -- deliberately do NOT start with "AUDITTRACE_"
# at all, precisely so they survive this wipe in ANY target repo's own
# conftest.py, this one included, without needing an allowlist entry here.
# A prior revision prefixed them "AUDITTRACE_NEUTER_*" and allow-listed
# them here; that broke the moment the harness targeted a DIFFERENT repo
# with the identical AUDITTRACE_*-wipe convention but no matching
# allowlist entry (the T1 oracle's frozen target) -- discovered by
# dogfooding the harness against this repo first (A11.1).)
_TEST_ONLY_ALLOWLIST = {
    "AUDITTRACE_TEST_POSTGRES_URL",
    "AUDITTRACE_NEUTER_LOCK",
}
for _key in [
    k
    for k in os.environ
    if k.startswith("AUDITTRACE_") and k not in _TEST_ONLY_ALLOWLIST
]:
    del os.environ[_key]
os.environ["AUDITTRACE_ENV"] = "test"

from audittrace import dependencies, telemetry  # noqa: E402
from audittrace.dependencies import (  # noqa: E402
    create_test_container,
    reset_container,
)
from audittrace.identity import (  # noqa: E402
    UserContext,
    sentinel_user_context,
)
from audittrace.logging_config import setup_logging  # noqa: E402
from audittrace.server import create_app  # noqa: E402
from audittrace.services.memory import MockMemoryService  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def configure_observability() -> None:
    """Enable DEBUG logging + no-op OTel for the whole test session."""
    setup_logging(level="DEBUG", structured=False)
    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="audittrace-server-tests",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=True,
    )


@pytest.fixture(autouse=True)
def _reset_global_container():
    """Isolate tests from each other's container state."""
    reset_container()
    yield
    reset_container()


@pytest.fixture(autouse=True)
def _propagate_logs(caplog):
    """Make caplog see records from our module loggers at DEBUG level."""
    caplog.set_level(logging.DEBUG)
    yield


@pytest.fixture
def test_container():
    container = create_test_container()
    yield container
    reset_container()


@pytest.fixture
def app(test_container):
    dependencies.container = test_container
    return create_app()


@pytest.fixture
def client(app):
    # Using TestClient as a context manager fires the lifespan handler,
    # which exercises setup_logging + telemetry init + FastAPIInstrumentor.
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mock_memory_service():
    service = MockMemoryService()
    yield service
    service.reset()


# ─────────────────────────── Phase 2 — UserContext ──────────────────────────
# ADR-026 §15. Every memory service method takes
# ``user_context: UserContext`` as the first positional argument. Tests that
# don't care about multi-user semantics use this sentinel-backed fixture so
# the plumbing target is honoured without touching identity concerns.


@pytest.fixture
def user_context() -> UserContext:
    """Admin sentinel UserContext — used by every service test as the
    default identity when the test is not exercising per-user isolation."""
    return sentinel_user_context()
