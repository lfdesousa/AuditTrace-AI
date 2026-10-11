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
# scripted-enforcement.md R3 closes that gap for good.
#
# Every stdlib process-spawn seam funnels through `subprocess.Popen` (which
# `subprocess.run`/`call`/`check_call`/`check_output` all build on
# internally) except `os.system` and the asyncio subprocess entry points,
# which spawn independently of `Popen` on POSIX. Patching all of them here
# (once, at collection time) means ANY test — not just ones that remember to
# import a guard — gets the check, and the check fires BEFORE the real
# process is ever created.
import asyncio  # noqa: E402

from tests.hermetic_allowlist import ALLOWED_CLUSTER_TOOL_TESTS  # noqa: E402

#: helm subcommands that never touch a live cluster (chart rendering /
#: static linting / vendoring only) — see the spec's R3 text verbatim.
_HELM_SAFE_SUBCOMMANDS = frozenset(
    {"template", "lint", "dependency", "version", "show"}
)

#: Set by the autouse fixture below for the duration of each test; read by
#: every patched spawn point so the violation message can name the test.
_current_test_node: list[object] = [None]


class HermeticClusterToolError(AssertionError):
    """Raised the instant a test reaches a real cluster-touching command."""


def _basename(token: object) -> str:
    try:
        return os.path.basename(str(token))
    except Exception:  # pragma: no cover - defensive, str() on anything works
        return str(token)


def _tool_and_subcommand(cmd: object) -> tuple[str, str | None]:
    """``cmd`` is either an argv list/tuple or a shell string (``shell=True``
    / ``os.system``). Returns ``(tool, subcommand)`` — ``subcommand`` is the
    SECOND token, or ``None`` if there isn't one."""
    if isinstance(cmd, (list, tuple)):
        tokens = [str(c) for c in cmd]
    else:
        tokens = str(cmd).split()
    if not tokens:
        return "", None
    tool = _basename(tokens[0])
    subcommand = tokens[1] if len(tokens) > 1 else None
    return tool, subcommand


def _is_cluster_touching(tool: str, subcommand: str | None) -> bool:
    if tool == "kubectl":
        return True
    if tool == "helm":
        return subcommand not in _HELM_SAFE_SUBCOMMANDS
    return False


def _enforce_hermetic(cmd: object) -> None:
    tool, subcommand = _tool_and_subcommand(cmd)
    if not _is_cluster_touching(tool, subcommand):
        return
    node = _current_test_node[0]
    nodeid = node.nodeid if node is not None else "<no active test>"
    marker = node.get_closest_marker("allow_cluster_tool") if node is not None else None
    allowed_tool = ALLOWED_CLUSTER_TOOL_TESTS.get(nodeid)
    marker_tool = marker.kwargs.get("tool") if marker is not None else None
    if (
        marker is not None
        and allowed_tool is not None
        and marker_tool == allowed_tool == tool
    ):
        return  # explicit, matching, two-sided opt-in
    raise HermeticClusterToolError(
        f"hermetic guard: {nodeid!r} reached a REAL cluster-touching "
        f"command ({cmd!r}) without stubbing it. `kubectl` and any "
        f"cluster-mutating/-reading `helm` subcommand are forbidden in the "
        f"test suite (SPEC 2026-10-11-SPEC-test-parity-scripted-"
        f"enforcement.md, R3) — stub the seam instead (e.g. inject a "
        f"`mesh_gate=_healthy_gate()` the way the sibling DeployRunner "
        f"tests do). A deliberate exception needs BOTH "
        f"@pytest.mark.allow_cluster_tool(tool={tool!r}) AND a matching "
        f"entry in tests/hermetic_allowlist.py — it does not have either."
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


def _patched_popen_init(self, args=(), *a, **kw):
    _enforce_hermetic(args)
    return _orig_popen_init(self, args, *a, **kw)


subprocess.Popen.__init__ = _patched_popen_init

_orig_os_system = os.system


def _patched_os_system(command):
    _enforce_hermetic(command)
    return _orig_os_system(command)


os.system = _patched_os_system

_orig_create_subprocess_exec = asyncio.create_subprocess_exec
_orig_create_subprocess_shell = asyncio.create_subprocess_shell


async def _patched_create_subprocess_exec(program, *args, **kw):
    _enforce_hermetic([program, *args])
    return await _orig_create_subprocess_exec(program, *args, **kw)


async def _patched_create_subprocess_shell(cmd, *args, **kw):
    _enforce_hermetic(cmd)
    return await _orig_create_subprocess_shell(cmd, *args, **kw)


asyncio.create_subprocess_exec = _patched_create_subprocess_exec
asyncio.create_subprocess_shell = _patched_create_subprocess_shell
asyncio.subprocess.create_subprocess_exec = _patched_create_subprocess_exec
asyncio.subprocess.create_subprocess_shell = _patched_create_subprocess_shell

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
