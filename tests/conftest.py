"""Test configuration and fixtures.

Logging and telemetry are configured once per test session so that the
@log_call aspect emits the same structured output it does in production
(all to stdout, per ADR-014.4). `caplog` can then assert on those records.
"""

import logging
import os

import pytest
from fastapi.testclient import TestClient

# Test isolation: clear all SOVEREIGN_* env vars so a developer's local .env
# (with real PostgreSQL credentials etc.) doesn't leak into tests.
# This must run BEFORE importing sovereign_memory.config so the .env-skip
# logic in config._ENV_FILE sees SOVEREIGN_ENV=test.
for _key in [k for k in os.environ if k.startswith("SOVEREIGN_")]:
    del os.environ[_key]
os.environ["SOVEREIGN_ENV"] = "test"

from sovereign_memory import dependencies, telemetry  # noqa: E402
from sovereign_memory.dependencies import (  # noqa: E402
    create_test_container,
    reset_container,
)
from sovereign_memory.identity import (  # noqa: E402
    UserContext,
    sentinel_user_context,
)
from sovereign_memory.logging_config import setup_logging  # noqa: E402
from sovereign_memory.server import create_app  # noqa: E402
from sovereign_memory.services.memory import MockMemoryService  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def configure_observability() -> None:
    """Enable DEBUG logging + no-op OTel for the whole test session."""
    setup_logging(level="DEBUG", structured=False)
    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="sovereign-memory-server-tests",
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
