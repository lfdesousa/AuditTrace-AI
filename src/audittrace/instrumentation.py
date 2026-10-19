"""The single OTel-instrumentor attach chokepoint (SQLALCHEMY-SPANS).

Why this exists. ``BaseInstrumentor.instrument()`` is fail-open by design: when
the installed library sits outside the instrumentor's declared range it logs
one line and returns ``None``; when the instrumentor is already attached it
logs a warning and returns ``None``. Either way the caller sees no exception
and PostgreSQL silently emits zero spans (sqlalchemy 2.1.x vs the
instrumentor's ``< 2.1.0``, measured 2026-10-19).

Every ``*Instrumentor`` attach in ``src/`` therefore goes through
:func:`attach_instrumentor`, which

* returns exactly one of :class:`AttachResult` (attached, declined,
  refused_already_instrumented, error, disabled),
* writes one log line with the stable key ``instrumentation.attach`` and the
  fields ``instrumentor`` / ``result`` / ``conflict`` (INFO for attached and
  disabled, ERROR otherwise),
* increments ``audittrace.instrumentation.attached_total{instrumentor,result}``
  (no PII: both labels are code constants),
* raises :class:`InstrumentationError` for declined / refused / error when
  ``Settings.instrumentation_strict`` is true (default), and never raises for
  ``disabled`` (``tracing_enabled`` false).

Engines (C4). ``SQLAlchemyInstrumentor`` is a process singleton that refuses a
second ``instrument()``, and a global ``instrument()`` at startup is measured
connect-only (``db/postgres.py`` binds ``create_async_engine`` at import). The
chokepoint therefore owns the engine registration: it keeps a weak registry of
every sync engine, and on each new engine runs ``uninstrument()`` followed by
``instrument(engines=<all registered>)``. Every engine, not only the first,
gets statement spans.

Falsifiability: tests/test_instrumentation_attach.py (G3/G3b, neuters N7-N10),
tests/test_sqlalchemy_spans.py (G1, neuters N1-N3).
"""

from __future__ import annotations

import enum
import logging
import threading
import weakref
from collections import deque
from typing import Any

from opentelemetry import metrics
from opentelemetry.instrumentation.dependencies import (
    DependencyConflictError,
    get_dependency_conflicts,
)

logger = logging.getLogger(__name__)

LOG_KEY = "instrumentation.attach"

_attach_total = metrics.get_meter("audittrace.instrumentation").create_counter(
    name="audittrace.instrumentation.attached_total",
    description=(
        "OTel instrumentor attach outcomes. Labels: instrumentor, result "
        "(attached | declined | refused_already_instrumented | error | "
        "disabled). No PII."
    ),
)


# F1 (review 2026-10-19): ``create_app()`` runs at import time, BEFORE
# ``init_telemetry`` installs the real MeterProvider, and an OTel API proxy
# instrument drops every measurement taken before a provider is set. Outcomes
# recorded in that window are therefore held here and replayed by
# :func:`flush_pending_outcomes`, which ``init_telemetry`` calls right after
# ``set_meter_provider``. The LOG line stays immediate. Bounded, so a process
# that never installs a provider (metrics disabled) cannot grow it.
_PENDING: deque[tuple[str, str]] = deque(maxlen=256)
_state = {"meter_ready": False}


class AttachResult(enum.StrEnum):
    ATTACHED = "attached"
    DECLINED = "declined"
    REFUSED_ALREADY_INSTRUMENTED = "refused_already_instrumented"
    ERROR = "error"
    DISABLED = "disabled"


_FATAL_UNDER_STRICT = frozenset(
    {
        AttachResult.DECLINED,
        AttachResult.REFUSED_ALREADY_INSTRUMENTED,
        AttachResult.ERROR,
    }
)


class InstrumentationError(RuntimeError):
    """A strict-mode instrumentor attach did not attach (startup must stop)."""


_LOCK = threading.RLock()
# Names of the singleton instrumentors this chokepoint attached; lets a second
# identical attach (init_telemetry is idempotent) be a no-op instead of a
# "refused" while still refusing an instrumentor attached by anyone else.
_OWNED: set[str] = set()
# Every sync engine the chokepoint has been asked to instrument.
_SA_ENGINES: weakref.WeakSet[Any] = weakref.WeakSet()


def _report(name: str, result: AttachResult, conflict: str | None) -> None:
    detail = conflict or "none"
    level = (
        logging.INFO
        if result in (AttachResult.ATTACHED, AttachResult.DISABLED)
        else logging.ERROR
    )
    logger.log(
        level,
        "%s instrumentor=%s result=%s conflict=%s",
        LOG_KEY,
        name,
        result.value,
        detail,
        extra={"instrumentor": name, "result": result.value, "conflict": detail},
    )
    _count(name, result.value)


def _count(name: str, result: str) -> None:
    with _LOCK:
        if _state["meter_ready"]:
            _attach_total.add(1, {"instrumentor": name, "result": result})
        else:
            _PENDING.append((name, result))


def flush_pending_outcomes() -> None:
    """Replay outcomes recorded before the MeterProvider existed.

    Called by ``init_telemetry`` immediately after ``set_meter_provider``;
    from then on outcomes are counted directly.
    """
    with _LOCK:
        _state["meter_ready"] = True
        while _PENDING:
            name, result = _PENDING.popleft()
            _attach_total.add(1, {"instrumentor": name, "result": result})


def _policy(strict: bool | None, enabled: bool | None) -> tuple[bool, bool]:
    if strict is not None and enabled is not None:
        return strict, enabled
    from audittrace.config import get_settings

    settings = get_settings()
    return (
        settings.instrumentation_strict if strict is None else strict,
        settings.tracing_enabled if enabled is None else enabled,
    )


def attach_instrumentor(
    name: str,
    instrumentor: Any,
    *,
    strict: bool | None = None,
    enabled: bool | None = None,
    app: Any = None,
    engine: Any = None,
    **kwargs: Any,
) -> AttachResult:
    """Attach ``instrumentor`` and report the outcome (the only attach site).

    ``app`` selects ``instrument_app`` (FastAPI); ``engine`` selects the
    engine-registry path (SQLAlchemy). ``strict`` / ``enabled`` default to
    ``Settings.instrumentation_strict`` / ``Settings.tracing_enabled``.
    """
    strict_mode, tracing_on = _policy(strict, enabled)
    if not tracing_on:
        _report(name, AttachResult.DISABLED, None)
        return AttachResult.DISABLED

    result = AttachResult.ATTACHED
    conflict: str | None = None
    with _LOCK:
        try:
            if engine is not None:
                _SA_ENGINES.add(engine)
                if instrumentor.is_instrumented_by_opentelemetry:
                    instrumentor.uninstrument()
                instrumentor.instrument(
                    engines=list(_SA_ENGINES),
                    raise_exception_on_conflict=True,
                    **kwargs,
                )
            elif app is not None:
                found = get_dependency_conflicts(
                    instrumentor.instrumentation_dependencies()
                )
                if found is not None:
                    raise DependencyConflictError(found)
                if getattr(app, "_is_instrumented_by_opentelemetry", False):
                    result = AttachResult.REFUSED_ALREADY_INSTRUMENTED
                else:
                    instrumentor.instrument_app(app, **kwargs)
            elif instrumentor.is_instrumented_by_opentelemetry:
                if name not in _OWNED:
                    result = AttachResult.REFUSED_ALREADY_INSTRUMENTED
            else:
                instrumentor.instrument(raise_exception_on_conflict=True, **kwargs)
                _OWNED.add(name)
        except DependencyConflictError as exc:
            result = AttachResult.DECLINED
            conflict = exc.conflict._format_message(name)
        except Exception as exc:
            result = AttachResult.ERROR
            conflict = f"{type(exc).__name__}: {exc}"

    _report(name, result, conflict)
    if strict_mode and result in _FATAL_UNDER_STRICT:
        raise InstrumentationError(
            f"{LOG_KEY} {name} result={result.value} conflict={conflict or 'none'}; "
            "set AUDITTRACE_INSTRUMENTATION_STRICT=false only to run without it"
        )
    return result


def _reset_for_tests() -> None:
    """Forget the chokepoint's registries. Test-only."""
    with _LOCK:
        _OWNED.clear()
        _SA_ENGINES.clear()
        _PENDING.clear()
