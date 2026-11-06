"""Shared support for the LOG-TRACE-ID tests (``test_log_trace_id.py`` unit,
``test_log_trace_id_route.py`` integration): the PRODUCTION bootstrap
(``setup_logging`` then ``init_telemetry``, in the lifespan's order) with the
handler's stdout bound to a buffer, and the ContextVar binders the Filter reads.
Not a test module: nothing here is collected."""

from __future__ import annotations

import contextlib
import io
import json
import logging
from collections.abc import Iterator
from typing import Any

from opentelemetry import trace

from audittrace import logging_config, telemetry
from audittrace.db import rls
from audittrace.logging_config import setup_logging
from audittrace.services.console_store._context import bind_session_id

SUB = "user-sub-log-trace-id-0001"
SESSION = "session-log-trace-id-0001"
SERVICE = "audittrace-server-log-trace-id-tests"
PROBE_LOG = logging.getLogger("audittrace.test.log_trace_id")


@contextlib.contextmanager
def isolated_logging_state() -> Iterator[None]:
    """Save/restore the root handler list, level and ``_STDOUT_HANDLER``, and
    leave telemetry the way the session fixture built it.

    The restore is explicit (not ``monkeypatch``) so it also holds when a test
    neutered the handler's filters: the re-init below runs the self-test
    against the ORIGINAL handler.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_stdout_handler = logging_config._STDOUT_HANDLER
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        logging_config._STDOUT_HANDLER = saved_stdout_handler
        telemetry._reset_for_tests()
        telemetry.init_telemetry(
            service_name="audittrace-server-tests",
            otlp_endpoint="",
            tracing_enabled=True,
            metrics_enabled=True,
        )


def bootstrap(*, structured: bool = True) -> io.StringIO:
    """``setup_logging`` then ``init_telemetry`` in the lifespan's order, with
    the handler's stdout bound to a buffer. Returns the buffer."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        setup_logging(level="INFO", structured=structured)
    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name=SERVICE,
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=True,
    )
    return buf


@contextlib.contextmanager
def bound(user: str | None = None, session: str | None = None) -> Iterator[None]:
    """Bind the request ContextVars the Filter reads; clear them on exit."""
    if user is not None:
        rls.set_current_user_id(user)
    if session is not None:
        bind_session_id(session)
    try:
        yield
    finally:
        rls.set_current_user_id(None)
        bind_session_id(None)


def log_lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.startswith("{")]


def log_line(buf: io.StringIO, needle: str) -> dict[str, Any]:
    found = [ln for ln in log_lines(buf) if needle in ln["message"]]
    assert len(found) == 1, (needle, buf.getvalue())  # positive control
    return found[0]


def root_handler() -> logging.Handler:
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1, handlers
    return handlers[0]


def in_span() -> Any:
    """A span context manager from the globally installed provider."""
    return trace.get_tracer("log-trace-id-test").start_as_current_span("t")
