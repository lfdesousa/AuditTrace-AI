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

# ─────────── R3 hermetic cluster-tool guard (PR #380, CI run 37878027812) ────
# Two tests in tests/test_deploy_runner.py called `runner.main(...)` without
# stubbing the mesh gate. The mesh gate's OWN `_run` seam (scripts/deploy/
# mesh.py) shells to a REAL `kubectl`. On the laptop (a live k3s cluster)
# that silently "worked"; in CI (no cluster) P0 aborted MESH UNSAFE and both
# tests flipped from pass to `assert 3 == 8` / `StopIteration`. Nothing
# detected the leak mechanically — SPEC 2026-10-11-SPEC-test-parity-
# scripted-enforcement.md R3, tightened by ADDENDUM A1 (A1-1/A1-2) after an
# independent review REJECT (decisions-in/2026-10-11-REVIEW-bff-bump-ci-fix.md,
# B1/B2) found the first cut both VACUOUS (no committed test exercised it —
# deleting it changed nothing) and EVADABLE (bytes argv, `executable=`, `env`/
# `sh -c`/`shell=True` wrapper forms, and the raw `os.exec*`/`posix_spawn*`/
# `spawn*`/`pty.spawn` families all slipped through unpatched).
#
# What is ACTUALLY patched, and what is proven by a committed test in
# tests/test_hermetic_cluster_tool_guard.py (B4 — this paragraph states only
# what that file measures, never "every" or "any" seam in the abstract):
#   * subprocess.Popen.__init__ (so run/call/check_call/check_output, which
#     all construct a Popen internally, are covered too) — tested with list
#     argv, bytes argv, and the `executable=` kwarg.
#   * os.system (a shell string) — tested with a kubectl-containing string.
#   * asyncio.create_subprocess_exec / create_subprocess_shell — tested with
#     a kubectl list argv and a kubectl shell string respectively.
#   * os.execv / os.execve / os.execvp / os.execl (one test per distinct
#     calling convention: list-style path-first, list-style PATH-searched,
#     *args-style) via a fork-isolated harness (os.exec* REPLACES the
#     calling process on success, so the harness runs the call in a forked
#     CHILD and inspects its exit status — a guard bug can only take down
#     the throwaway child, never the test runner).
#   * os.posix_spawn / os.posix_spawnp, and os.spawnv (representative of the
#     os.spawn* family — all of them fork+exec or posix_spawn internally).
#   * pty.spawn.
#   * The `env [VAR=val ...] <tool> ...` wrapper, `sh -c "<string>"` / `bash
#     -c "<string>"`, and a `shell=True` string split on `;`, `&&`, `||`,
#     `|` and newlines with EVERY segment checked (not just the first) — an
#     unparseable segment that still contains the word `kubectl` or `helm`
#     fails closed rather than being silently skipped.
#   * The two-sided allowlist opt-in (`_opt_in_allows`), proven for all four
#     combinations (marker only, entry only, both matching, tool mismatch)
#     against the REAL dict object and the REAL enforcement function.
import asyncio  # noqa: E402
import pty  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402

from tests.hermetic_allowlist import ALLOWED_CLUSTER_TOOL_TESTS  # noqa: E402

#: helm subcommands that never touch a live cluster (chart rendering /
#: static linting / vendoring only) — see the spec's R3 text verbatim.
_HELM_SAFE_SUBCOMMANDS = frozenset(
    {"template", "lint", "dependency", "version", "show"}
)

#: Set by the autouse fixture below for the duration of each test; read by
#: every patched spawn point so the violation message can name the test.
_current_test_node: list[object] = [None]

#: shell metacharacters a `shell=True` string / a `sh -c`/`bash -c` argument
#: is split on before each segment is checked independently (A1-2).
_SHELL_OPERATOR_SPLIT = re.compile(r"&&|\|\||;|\||\n")

#: fail-closed fallback (A1-2): a segment that cannot be tokenized (shlex
#: raises, e.g. an unbalanced quote) is still treated as a violation if it
#: contains either word, rather than silently passing.
_DANGEROUS_WORD_RE = re.compile(r"\b(kubectl|helm)\b")


class HermeticClusterToolError(AssertionError):
    """Raised the instant a test reaches a real cluster-touching command."""


def _decode(token: object) -> str:
    if isinstance(token, bytes):
        return token.decode("utf-8", errors="surrogateescape")
    return str(token)


def _basename(token: object) -> str:
    try:
        return os.path.basename(_decode(token))
    except Exception:  # pragma: no cover - defensive, str() on anything works
        return str(token)


def _is_cluster_touching(tool: str, subcommand: str | None) -> bool:
    if tool == "kubectl":
        return True
    if tool == "helm":
        return subcommand not in _HELM_SAFE_SUBCOMMANDS
    return False


def _shell_segments(s: str) -> list[str]:
    return [seg for seg in _SHELL_OPERATOR_SPLIT.split(s) if seg.strip()]


def _segment_tool_subcommand(segment: str) -> tuple[str, str | None] | None:
    """Tokenizes one shell segment. Returns ``None`` (never a false "safe")
    when the segment cannot be tokenized at all — the caller fails closed
    on that, per A1-2."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return None
    if not tokens:
        return "", None
    return _basename(tokens[0]), (tokens[1] if len(tokens) > 1 else None)


def _raise_if_cluster_touching(
    tool: str, subcommand: str | None, *, display: str
) -> None:
    if not _is_cluster_touching(tool, subcommand):
        return
    node = _current_test_node[0]
    nodeid = node.nodeid if node is not None else "<no active test>"
    marker = node.get_closest_marker("allow_cluster_tool") if node is not None else None
    marker_tool = marker.kwargs.get("tool") if marker is not None else None
    if _opt_in_allows(nodeid, tool, marker_tool, ALLOWED_CLUSTER_TOOL_TESTS):
        return
    raise HermeticClusterToolError(
        f"hermetic guard: {nodeid!r} reached a REAL cluster-touching "
        f"command ({display}) without stubbing it. `kubectl` and any "
        f"cluster-mutating/-reading `helm` subcommand are forbidden in the "
        f"test suite (SPEC 2026-10-11-SPEC-test-parity-scripted-"
        f"enforcement.md R3 + ADDENDUM A1) — stub the seam instead (e.g. "
        f"inject a `mesh_gate=_healthy_gate()` the way the sibling "
        f"DeployRunner tests do). A deliberate exception needs BOTH "
        f"@pytest.mark.allow_cluster_tool(tool={tool!r}) AND a matching "
        f"entry in tests/hermetic_allowlist.py — it does not have either."
    )


def _opt_in_allows(
    nodeid: str,
    tool: str,
    marker_tool: str | None,
    allowlist: dict[str, str],
) -> bool:
    """The two-sided opt-in (A1-1's four cases): the marker's declared tool,
    the allowlist entry's tool, AND the tool actually being invoked must
    ALL three agree, or the guard still denies (a marker/allowlist pair
    that agrees with each other but not with what is actually running is
    the "tool mismatch" case — it must deny too). A pure function so
    tests/test_hermetic_cluster_tool_guard.py can exercise all four
    combinations directly against this exact logic."""
    allowed_tool = allowlist.get(nodeid)
    return (
        marker_tool is not None
        and allowed_tool is not None
        and marker_tool == allowed_tool == tool
    )


def _check_segment_or_fail_closed(segment: str, *, display: str) -> None:
    parsed = _segment_tool_subcommand(segment)
    if parsed is None:
        if _DANGEROUS_WORD_RE.search(segment):
            raise HermeticClusterToolError(
                f"hermetic guard: {display} — shell segment {segment!r} could "
                f"not be tokenized but contains a forbidden word; failing "
                f"closed (SPEC ... ADDENDUM A1-2)."
            )
        return
    tool, subcommand = parsed
    _raise_if_cluster_touching(
        tool, subcommand, display=f"{display} (segment {segment!r})"
    )


def _check_shell_string(s: str, *, display: str) -> None:
    for segment in _shell_segments(s):
        _check_segment_or_fail_closed(segment, display=display)


def _env_and_sh_unwrapped(tokens: list[str]) -> list[str]:
    """Peels a leading ``env [VAR=val ...]`` wrapper off an argv token
    list (A1-2). Returns the tokens unchanged if there is no such wrapper."""
    if not tokens or _basename(tokens[0]) != "env":
        return tokens
    i = 1
    while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
        i += 1
    return tokens[i:]


def _enforce_hermetic_argv(argv: list[object], *, display_prefix: str = "") -> None:
    """The list/tuple-argv path (Popen-style AND the exec/spawn families,
    which all hand us a full argv including argv[0]). Unwraps `env` and
    `sh|bash -c "<string>"` wrappers (A1-2) before checking."""
    tokens = [_decode(t) for t in argv]
    tokens = _env_and_sh_unwrapped(tokens)
    if not tokens:
        return
    tool = _basename(tokens[0])
    if tool in ("sh", "bash") and len(tokens) >= 3 and tokens[1] == "-c":
        _check_shell_string(tokens[2], display=f"{display_prefix}{tool} -c")
        return
    subcommand = tokens[1] if len(tokens) > 1 else None
    _raise_if_cluster_touching(
        tool, subcommand, display=f"{display_prefix}{' '.join(tokens)!r}"
    )


def _enforce_hermetic(cmd: object, *, executable: object | None = None) -> None:
    """The Popen/os.system/asyncio entry point. ``cmd`` is an argv
    list/tuple (possibly bytes elements), or a shell string (``shell=True``
    / ``os.system`` / ``create_subprocess_shell``). ``executable=`` (A1-2),
    when given, is the REAL program invoked — argv[0] is then just a
    display name the program sees as its own, not what actually runs."""
    if executable is not None:
        tool = _basename(executable)
        rest = [_decode(t) for t in cmd] if isinstance(cmd, (list, tuple)) else []
        subcommand = rest[1] if len(rest) > 1 else None
        _raise_if_cluster_touching(
            tool, subcommand, display=f"executable={executable!r} args={cmd!r}"
        )
        return
    if isinstance(cmd, (list, tuple)):
        _enforce_hermetic_argv(list(cmd))
        return
    if isinstance(cmd, bytes):
        cmd = _decode(cmd)
    if isinstance(cmd, str):
        _check_shell_string(cmd, display=f"shell string {cmd!r}")
        return
    # Anything else (an unrecognised command shape): best-effort, fail
    # closed only if its text form names a forbidden tool outright.
    if _DANGEROUS_WORD_RE.search(repr(cmd)):
        raise HermeticClusterToolError(
            f"hermetic guard: unrecognised command shape {cmd!r} names a "
            f"forbidden tool; failing closed."
        )


@pytest.fixture(autouse=True)
def _hermetic_cluster_tool_guard(request):
    """Stash the running test's node so the patched spawn points below can
    name it in a violation message, and can look up its marker."""
    _current_test_node[0] = request.node
    try:
        yield
    finally:
        _current_test_node[0] = None


_orig_popen_init = subprocess.Popen.__init__


def _patched_popen_init(self, args=(), *a, executable=None, shell=False, **kw):
    if shell and isinstance(args, (list, tuple)) and args:
        _enforce_hermetic(args[0], executable=executable)
    else:
        _enforce_hermetic(args, executable=executable)
    return _orig_popen_init(self, args, *a, executable=executable, shell=shell, **kw)


subprocess.Popen.__init__ = _patched_popen_init

_orig_os_system = os.system


def _patched_os_system(command):
    _enforce_hermetic(command)
    return _orig_os_system(command)


os.system = _patched_os_system

_orig_create_subprocess_exec = asyncio.create_subprocess_exec
_orig_create_subprocess_shell = asyncio.create_subprocess_shell


async def _patched_create_subprocess_exec(program, *args, **kw):
    _enforce_hermetic([program, *args], executable=kw.get("executable"))
    return await _orig_create_subprocess_exec(program, *args, **kw)


async def _patched_create_subprocess_shell(cmd, *args, **kw):
    _enforce_hermetic(cmd)
    return await _orig_create_subprocess_shell(cmd, *args, **kw)


asyncio.create_subprocess_exec = _patched_create_subprocess_exec
asyncio.create_subprocess_shell = _patched_create_subprocess_shell
asyncio.subprocess.create_subprocess_exec = _patched_create_subprocess_exec
asyncio.subprocess.create_subprocess_shell = _patched_create_subprocess_shell

# ── the raw os.exec*/os.posix_spawn*/os.spawn* families + pty.spawn (A1-2) ──
# These never go through subprocess.Popen. os.execv/execve/execl/execlp (and
# every other exec* variant, which CPython's os.py funnels through these
# two at the C level) REPLACE the calling process image on success, so a
# guard bug here could kill more than the one test — see the fork-isolated
# harness in tests/test_hermetic_cluster_tool_guard.py, which is what
# actually proves these are covered (B4: this comment claims coverage of
# exactly these named functions, nothing broader).

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


def _check_path_and_argv(path_like: object, argv: list[object]) -> None:
    tool = _basename(path_like)
    decoded = [_decode(a) for a in argv]
    subcommand = decoded[1] if len(decoded) > 1 else None
    _raise_if_cluster_touching(tool, subcommand, display=f"{path_like!r} argv={argv!r}")


def _patched_execv(path, args):
    _check_path_and_argv(path, list(args))
    return _orig_execv(path, args)


def _patched_execve(path, args, env):
    _check_path_and_argv(path, list(args))
    return _orig_execve(path, args, env)


def _patched_execvp(file, args):
    _check_path_and_argv(file, list(args))
    return _orig_execvp(file, args)


def _patched_execvpe(file, args, env):
    _check_path_and_argv(file, list(args))
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
    _check_path_and_argv(path, list(argv))
    return _orig_posix_spawn(path, argv, env, **kw)


def _patched_posix_spawnp(file, argv, env, **kw):
    _check_path_and_argv(file, list(argv))
    return _orig_posix_spawnp(file, argv, env, **kw)


def _patched_spawnv(mode, path, args):
    _check_path_and_argv(path, list(args))
    return _orig_spawnv(mode, path, args)


def _patched_spawnve(mode, path, args, env):
    _check_path_and_argv(path, list(args))
    return _orig_spawnve(mode, path, args, env)


def _patched_spawnvp(mode, file, args):
    _check_path_and_argv(file, list(args))
    return _orig_spawnvp(mode, file, args)


def _patched_spawnvpe(mode, file, args, env):
    _check_path_and_argv(file, list(args))
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
