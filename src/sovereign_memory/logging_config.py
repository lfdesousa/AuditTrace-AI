"""Logging configuration for sovereign-memory-server.

Inspired by FluentPython (Luciano Ramalho) - logging as an aspect.
All logs go to stdout, no file logging.

The @log_call decorator is a unified observability aspect: for every
decorated call it emits a DEBUG log line (INPUT/OUTPUT/DURATION), opens
an OpenTelemetry span, and records a histogram metric. It works for
both sync and async functions.
"""

import asyncio
import itertools
import json
import logging
import sys
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from sovereign_memory import telemetry

# Monotonic counter used as `langgraph_step` so Langfuse can order nodes
# in its graph view. The Langfuse adapter requires step to be a number.
_langgraph_step_counter = itertools.count(1)


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


def _operation_name(func: Callable, args: tuple) -> str:
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


def log_call(
    logger: logging.Logger | None = None,
    include_input: bool = True,
    include_output: bool = True,
    include_duration: bool = True,
) -> Callable:
    """Observability aspect: log + trace + meter every decorated call.

    Emits:
      - DEBUG log lines for INPUT/OUTPUT/DURATION (stdout only)
      - An OpenTelemetry span named after the operation
      - A histogram metric `sovereign.operation.duration` (seconds)

    Works for both sync and async functions.
    """

    def decorator(func: Callable) -> Callable:
        log = logger or logging.getLogger(func.__module__)
        is_async = asyncio.iscoroutinefunction(func)

        def _log_input(op: str, args: tuple, kwargs: dict) -> None:
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

        def _set_span_input(span: Any, op: str, args: tuple, kwargs: dict) -> None:
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
                span.set_attribute("langgraph_step", next(_langgraph_step_counter))
            except Exception:  # pragma: no cover
                pass
            if not include_input:
                return
            # ALWAYS emit input.value, even for self-only methods (no positional
            # args after self) and no-arg free functions. The previous gate
            # ``if payload:`` skipped these calls entirely, leaving Langfuse's
            # Input panel rendering "undefined" for spans like
            # FileEpisodicService.load(self) — confirmed via live trace.
            # An empty ``{}`` is a meaningful display value ("called with no
            # args"); ``undefined`` is actively misleading.
            payload: dict[str, Any] = {}
            if len(args) > 1:
                payload["args"] = args[1:]
            if kwargs:
                payload["kwargs"] = kwargs
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
            async def async_wrapper(*args, **kwargs) -> Any:
                op = _operation_name(func, args)
                span_name = _friendly_span_name(op)
                _log_input(op, args, kwargs)
                start = time.perf_counter()
                err: str | None = None
                step_n = next(_langgraph_step_counter)
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
                        if span is not None:
                            # Defensive: LangfuseSpan (from the Langfuse SDK)
                            # does not implement OTel's ``record_exception``
                            # method. Only OTel spans do. Guard with hasattr
                            # + try so a telemetry-path failure never masks
                            # the original exception as a 500 (Phase 7 bug).
                            try:
                                if hasattr(span, "record_exception"):
                                    span.record_exception(e)
                            except Exception:  # pragma: no cover - defensive
                                pass
                        raise
                    finally:
                        _record(op, time.perf_counter() - start, err)

            return async_wrapper

        @wraps(func)
        def sync_wrapper(*args, **kwargs) -> Any:
            op = _operation_name(func, args)
            span_name = _friendly_span_name(op)
            _log_input(op, args, kwargs)
            start = time.perf_counter()
            err: str | None = None
            step_n = next(_langgraph_step_counter)
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
                    if span is not None:
                        try:
                            if hasattr(span, "record_exception"):
                                span.record_exception(e)
                        except Exception:  # pragma: no cover - defensive
                            pass
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
