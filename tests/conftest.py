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
