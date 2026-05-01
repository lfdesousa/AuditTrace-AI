"""Logging configuration for audittrace-server.

Inspired by FluentPython (Luciano Ramalho) - logging as an aspect.
All logs go to stdout, no file logging.

The @log_call decorator is a unified observability aspect: for every
decorated call it emits a DEBUG log line (INPUT/OUTPUT/DURATION), opens
an OpenTelemetry span, and records a histogram metric. It works for
both sync and async functions.
"""

from __future__ import annotations

import asyncio
import contextvars
import itertools
import json
import logging
import sys
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from audittrace import telemetry

# Per-trace step counter (backlog #02). A process-global counter interleaved
# step numbers across concurrent requests. The ContextVar scopes the counter
# to the current async context (one per chat request).
_LANGGRAPH_STEP: contextvars.ContextVar[itertools.count[int]] = contextvars.ContextVar(
    "langgraph_step"
)


def _next_step() -> int:
    """Return the next langgraph_step for the current trace context."""
    try:
        counter = _LANGGRAPH_STEP.get()
    except LookupError:
        counter = itertools.count(1)
        _LANGGRAPH_STEP.set(counter)
    return next(counter)


def reset_langgraph_step() -> None:
    """Reset the per-trace step counter. Call at the start of each request."""
    _LANGGRAPH_STEP.set(itertools.count(1))


class StructuredFormatter(logging.Formatter):
    """JSON formatter for structured logging to stdout."""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # OTel LoggingInstrumentor attaches these automatically when a span
        # is active; surface them so logs can be correlated with traces.
        for attr in ("otelTraceID", "otelSpanID", "otelServiceName"):
            if hasattr(record, attr):
                log_data[attr] = getattr(record, attr)
        for attr in ("request_id", "duration", "operation"):
            if hasattr(record, attr):
                log_data[attr] = getattr(record, attr)
        return json.dumps(log_data)


def setup_logging(
    level: str = "INFO",
    structured: bool = False,
) -> None:
    """Configure root logging to stdout only.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        structured: If True, emit JSON lines; otherwise plain format.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper()))
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(getattr(logging, level.upper()))

    if structured:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    root.addHandler(handler)


def _operation_name(func: Callable[..., Any], args: tuple[Any, ...]) -> str:
    if args and hasattr(args[0], "__class__") and not isinstance(args[0], type):
        cls = args[0].__class__.__name__
        if cls not in ("str", "int", "dict", "list", "tuple"):
            return f"{cls}.{func.__name__}"
    return f"{func.__module__}.{func.__name__}"


# Max chars stored in span input/output attributes — keeps Langfuse traces
# readable without bloating ClickHouse storage with full ADR documents.
_SPAN_ATTR_MAX_LEN = 4000


def _serialize_for_span(value: Any) -> str:
    """Serialise call args/result for span attributes (truncated, str-safe)."""
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > _SPAN_ATTR_MAX_LEN:
        text = text[:_SPAN_ATTR_MAX_LEN] + f"...[truncated {len(text)} chars]"
    return text


# Map operation name patterns to logical components for Langfuse dashboards.
# Each span gets a `sovereign.component` attribute so charts can group by it
# instead of raw function names.
_COMPONENT_MAP: dict[str, str] = {
    "EpisodicService": "memory.episodic",
    "ProceduralService": "memory.procedural",
    "ConversationalService": "memory.conversational",
    "SemanticService": "memory.semantic",
    "ContextBuilder": "memory.builder",
    "PostgresFactory": "db.postgres",
    "ChromaDBFactory": "db.chromadb",
    "URLPostgres": "db.postgres",
    "InMemoryPostgres": "db.postgres",
    "HTTPChromaDB": "db.chromadb",
    "MockPostgres": "db.postgres",
    "MockChromaDB": "db.chromadb",
    "DependencyContainer": "core.di",
    "routes.chat": "route.chat",
    "routes.context": "route.context",
    "routes.audit": "route.audit",
    "routes.session": "route.session",
    "routes.health": "route.health",
    "auth": "core.auth",
    "telemetry": "core.telemetry",
}


def _classify_component(op: str) -> str:
    """Map an operation name to a logical component label."""
    for marker, component in _COMPONENT_MAP.items():
        if marker in op:
            return component
    return "other"


# Friendly kebab-case span names so Langfuse's trace graph view renders a
# clean flow chart on the left side of the trace detail page. Order matters:
# more specific markers must come first.
_SPAN_NAME_MAP: list[tuple[str, str]] = [
    # Routes
    ("routes.chat.list_models", "models-list"),
    ("routes.chat.chat_completions", "chat-completions"),
    ("routes.chat._extract_query", "chat-extract-query"),
    ("routes.chat._merge_system_message", "chat-merge-system"),
    ("routes.context.get_context", "context-build"),
    ("routes.audit.list_interactions", "audit-list"),
    ("routes.audit.create_interaction", "audit-create"),
    ("routes.session.save_session", "session-save"),
    ("routes.health.health_check", "health-check"),
    ("routes.health.metrics", "metrics-read"),
    # Memory layer services
    ("FileEpisodicService.load", "memory-episodic-load"),
    ("FileEpisodicService.search", "memory-episodic-search"),
    ("FileEpisodicService.as_context", "memory-episodic-context"),
    ("FileProceduralService.load", "memory-procedural-load"),
    ("FileProceduralService.search", "memory-procedural-search"),
    ("FileProceduralService.as_context", "memory-procedural-context"),
    ("PostgresConversationalService.load_sessions", "memory-conversational-load"),
    ("PostgresConversationalService.save_session", "memory-conversational-save"),
    ("PostgresConversationalService.as_context", "memory-conversational-context"),
    ("ChromaSemanticService.search", "memory-semantic-search"),
    ("ChromaSemanticService.available_collections", "memory-semantic-collections"),
    # Context builder
    (
        "DefaultContextBuilder.build_system_context_with_stats",
        "memory-context-build-stats",
    ),
    ("DefaultContextBuilder.build_system_context", "memory-context-build"),
    # DB factories
    ("URLPostgresFactory.get_engine", "db-postgres-engine"),
    ("URLPostgresFactory.get_session_factory", "db-postgres-session-factory"),
    ("InMemoryPostgresFactory.get_engine", "db-postgres-engine"),
    ("InMemoryPostgresFactory.get_session_factory", "db-postgres-session-factory"),
    ("HTTPChromaDBFactory.get_client", "db-chromadb-client"),
    # Core
    ("DependencyContainer.register_factory", "di-register-factory"),
    ("DependencyContainer.get_factory", "di-get-factory"),
    ("DependencyContainer.create_instance", "di-create-instance"),
    ("DependencyContainer.get_instance", "di-get-instance"),
    ("register_default_dependencies", "di-bootstrap"),
    ("get_context_builder", "di-context-builder"),
    ("get_postgres_factory", "di-postgres-factory"),
    ("get_chromadb_factory", "di-chromadb-factory"),
    ("get_chromadb", "di-chromadb"),
    ("get_episodic_service", "di-episodic-service"),
    ("get_conversational_service", "di-conversational-service"),
]


def _friendly_span_name(op: str) -> str:
    """Map a verbose operation name to a kebab-case label for Langfuse graphs.

    Falls back to the original op name if no mapping matches so we never lose
    information for debugging.
    """
    for marker, label in _SPAN_NAME_MAP:
        if marker in op:
            return label
    return op


def _extract_user_id(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    """Resolve the calling user's id for span tagging (Phase-2 recon).

    Two sources, in precedence:
      1. A ``UserContext`` positional or keyword argument on the decorated
         call. Most memory-service methods accept it as the first positional
         argument per ADR-026, so finding it is O(1).
      2. The ``audittrace.db.rls._current_user_id`` ContextVar, which the
         auth middleware binds on every authenticated request.

    Returns ``None`` when neither yields an id (sentinel pre-auth calls,
    unit tests without a request scope). Imports are local to avoid
    a circular module dependency with ``audittrace.identity``/``db.rls``.
    """
    user_ctx_cls: Any = None
    try:
        from audittrace.identity import UserContext

        user_ctx_cls = UserContext
    except Exception:  # pragma: no cover - defensive: identity always importable
        pass

    if user_ctx_cls is not None:
        for arg in args:
            if isinstance(arg, user_ctx_cls):
                uid = getattr(arg, "user_id", None)
                if uid:
                    return str(uid)
        for value in kwargs.values():
            if isinstance(value, user_ctx_cls):
                uid = getattr(value, "user_id", None)
                if uid:
                    return str(uid)

    try:
        from audittrace.db.rls import current_user_id

        result = current_user_id()
        return str(result) if result is not None else None
    except Exception:  # pragma: no cover - defensive
        return None


def _record_span_error(span: Any, exc: Exception) -> None:
    """Record an exception on the active span (OTel or Langfuse).

    OTel spans expose ``record_exception``; Langfuse spans do not — they
    use ``update(level="ERROR", status_message=...)`` instead. Dispatch
    based on what the span object supports so the error surfaces in both
    backends without one masking the other.
    """
    if span is None:
        return
    try:
        if hasattr(span, "record_exception"):
            span.record_exception(exc)
        elif hasattr(span, "update"):
            span.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
    except Exception:  # pragma: no cover - defensive
        pass


def log_call(
    logger: logging.Logger | None = None,
    include_input: bool = True,
    include_output: bool = True,
    include_duration: bool = True,
) -> Callable[..., Any]:
    """Observability aspect: log + trace + meter every decorated call.

    Emits:
      - DEBUG log lines for INPUT/OUTPUT/DURATION (stdout only)
      - An OpenTelemetry span named after the operation
      - A histogram metric `sovereign.operation.duration` (seconds)

    Works for both sync and async functions.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        log = logger or logging.getLogger(func.__module__)
        is_async = asyncio.iscoroutinefunction(func)

        def _log_input(op: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
            if include_input and log.isEnabledFor(logging.DEBUG):
                log.debug(
                    f"INPUT {op}",
                    extra={
                        "operation": op,
                        "call_args": str(args[1:] if len(args) > 1 else ())[:500],
                        "call_kwargs": str(kwargs)[:500],
                    },
                )

        def _log_output(op: str, result: Any) -> None:
            if include_output and log.isEnabledFor(logging.DEBUG):
                log.debug(
                    f"OUTPUT {op}",
                    extra={
                        "operation": op,
                        "result": str(result)[:500] if result is not None else None,
                    },
                )

        def _record(op: str, duration: float, error: str | None) -> None:
            if include_duration and log.isEnabledFor(logging.DEBUG):
                log.debug(
                    f"DURATION {op}: {duration:.4f}s",
                    extra={"operation": op, "duration": duration},
                )
            telemetry.record_operation(op, duration, error)

        def _set_span_input(
            span: Any, op: str, args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> None:
            """Attach call arguments + component label to the span.

            Langfuse maps ``input.value`` to the trace UI's Input panel and
            renders custom attributes (``sovereign.component``) on the right
            so dashboards can group by component. ``langgraph.node`` triggers
            Langfuse's graph (flowchart) view.

            ``input.value`` is routed through ``telemetry.set_current_span_attributes``
            so the Langfuse SDK ``update_current_span(input=...)`` call lands
            on the first-class Input field of the active observation. Pre-fix
            we used ``span.set_attribute(...)`` directly, which only wrote to
            the underlying OTel attribute (nested deep in metadata.attributes)
            and left the Langfuse Input panel rendering ``undefined`` (ADR-024).
            """
            if span is None:
                return
            friendly = _friendly_span_name(op)
            try:
                span.set_attribute("sovereign.component", _classify_component(op))
                span.set_attribute("sovereign.operation", op)
                # LangGraph integration attributes — Langfuse's adapter
                # validates {langgraph_node: string, langgraph_step: number}
                # (underscores, not dots) and renders the trace as a graph
                # view in the left panel when both are present.
                span.set_attribute("langgraph_node", friendly)
                span.set_attribute("langgraph_step", _next_step())
                # Phase-2 reconstructibility: tag every span with the
                # caller's Keycloak sub when resolvable. Without this the
                # Langfuse dashboard cannot filter child observations by
                # user and the EU AI Act Art. 12 trail stops at the root.
                uid = _extract_user_id(args, kwargs)
                if uid:
                    span.set_attribute("langfuse.user.id", uid)
                    span.set_attribute("user.id", uid)
            except Exception:  # pragma: no cover
                pass
            if not include_input:
                return
            # Emit the richest payload we can so Langfuse's Input panel
            # shows real content on every span — never "undefined".
            # Method detection: args[0] is ``self`` only for BOUND methods.
            # Module-level functions (e.g. _extract_query(payload)) have
            # args[0] as the real first argument, so the old ``args[1:]``
            # dropped it silently. Detect with the same heuristic
            # _operation_name uses — args[0] is self if it's an instance
            # of a non-builtin class.
            starts_with_self = bool(
                args
                and hasattr(args[0], "__class__")
                and not isinstance(args[0], type)
                and args[0].__class__.__name__
                not in (
                    "str",
                    "int",
                    "dict",
                    "list",
                    "tuple",
                    "float",
                    "bool",
                    "NoneType",
                )
            )
            real_args = args[1:] if starts_with_self else args
            payload: dict[str, Any] = {}
            if real_args:
                payload["args"] = real_args
            if kwargs:
                payload["kwargs"] = kwargs
            if not payload:
                # No-arg call is a meaningful fact; tag it explicitly
                # instead of writing "{}" which Langfuse renders as empty.
                payload = {"called_with": "no arguments"}
            try:
                telemetry.set_current_span_attributes(
                    {"input.value": _serialize_for_span(payload)}
                )
            except Exception:  # pragma: no cover
                pass

        def _set_span_output(span: Any, result: Any) -> None:
            """Attach return value to the span as output.value attribute.

            Routed through ``telemetry.set_current_span_attributes`` so the
            Langfuse SDK Output panel populates (see ``_set_span_input`` docstring).

            ALWAYS emits output.value, including for ``result is None`` —
            returning None is a meaningful outcome (e.g. ``save()`` methods
            that mutate state and return nothing). The previous early-return
            on ``result is None`` left those spans rendering "undefined".
            """
            if span is None or not include_output:
                return
            try:
                telemetry.set_current_span_attributes(
                    {"output.value": _serialize_for_span(result)}
                )
            except Exception:  # pragma: no cover
                pass

        if is_async:

            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                op = _operation_name(func, args)
                span_name = _friendly_span_name(op)
                _log_input(op, args, kwargs)
                start = time.perf_counter()
                err: str | None = None
                step_n = _next_step()
                span_metadata = {
                    "langgraph_node": span_name,
                    "langgraph_step": step_n,
                    "sovereign.component": _classify_component(op),
                    "sovereign.operation": op,
                }
                with telemetry.start_span(span_name, metadata=span_metadata) as span:
                    _set_span_input(span, op, args, kwargs)
                    try:
                        result = await func(*args, **kwargs)
                        _log_output(op, result)
                        _set_span_output(span, result)
                        return result
                    except Exception as e:
                        err = type(e).__name__
                        log.error(
                            f"ERROR {op}: {e}",
                            exc_info=True,
                            extra={"operation": op},
                        )
                        _record_span_error(span, e)
                        raise
                    finally:
                        _record(op, time.perf_counter() - start, err)

            return async_wrapper

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            op = _operation_name(func, args)
            span_name = _friendly_span_name(op)
            _log_input(op, args, kwargs)
            start = time.perf_counter()
            err: str | None = None
            step_n = _next_step()
            span_metadata = {
                "langgraph_node": span_name,
                "langgraph_step": step_n,
                "sovereign.component": _classify_component(op),
                "sovereign.operation": op,
            }
            with telemetry.start_span(span_name, metadata=span_metadata) as span:
                _set_span_input(span, op, args, kwargs)
                try:
                    result = func(*args, **kwargs)
                    _log_output(op, result)
                    _set_span_output(span, result)
                    return result
                except Exception as e:
                    err = type(e).__name__
                    log.error(
                        f"ERROR {op}: {e}",
                        exc_info=True,
                        extra={"operation": op},
                    )
                    _record_span_error(span, e)
                    raise
                finally:
                    _record(op, time.perf_counter() - start, err)

        return sync_wrapper

    # Allow bare @log_call usage
    if callable(logger) and not isinstance(logger, logging.Logger):
        func, logger = logger, None
        return decorator(func)

    return decorator


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with standard configuration."""
    return logging.getLogger(name)
