"""Request-context readers for the console-store base — the ONLY source of
the security-relevant and traceability columns the base stamps.

Every value here is SERVER-DERIVED:

* ``user_sub`` — from the token-resolved :class:`~audittrace.identity.
  UserContext` (``require_user``), cross-checked against the Postgres-RLS
  request ContextVar (``audittrace.db.rls``) that the same dependency binds.
  A disagreement is fail-closed (:class:`ConsoleStoreScopeError`) BEFORE any
  session opens. Never from a request body.
* ``trace_id`` — the active OpenTelemetry span's trace id (32-hex), the same
  derivation ``routes/chat.py::_current_trace_id_hex`` uses for
  ``interactions.trace_id`` (EU AI Act Art 12 traceability, M5). ``None``
  when no span is active (telemetry no-op on the laptop default).
* ``session_id`` — a request-scoped ContextVar bound by the request layer
  (``bind_session_id``); ``None`` when the request layer has not bound one.
  WU-A ships the stamp; wiring each console route's ``X-Session-Id`` header
  into ``bind_session_id`` is the M5 retrofit vehicle.

None of these functions accept a caller-supplied override — there is no
parameter through which a body value could reach a stamped column.
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass

from opentelemetry import trace

from audittrace.db import rls
from audittrace.identity import UserContext
from audittrace.services.console_store._errors import ConsoleStoreScopeError

logger = logging.getLogger(__name__)

# Columns the BASE owns on every console row. A domain can neither declare
# them as key/value columns nor supply them through a body, a key, a value,
# or a hook return value — validate_domain() and the base's write helpers
# refuse them (ConsoleStoreDomainError / ConsoleStoreForbiddenFieldError).
RESERVED_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "user_sub",
        "created_at_ms",
        "updated_at_ms",
        "deleted_at_ms",
        "trace_id",
        "session_id",
    }
)

# Reserved columns the ORM model MUST carry (session_id is optional until the
# M5 retrofit adds it to each console table).
REQUIRED_MODEL_COLUMNS: tuple[str, ...] = (
    "id",
    "user_sub",
    "created_at_ms",
    "updated_at_ms",
    "deleted_at_ms",
    "trace_id",
)

_current_session_id: ContextVar[str | None] = ContextVar(
    "_console_store_session_id", default=None
)


@dataclass(frozen=True)
class WriteStamp:
    """Everything the base stamps on a write, resolved BEFORE the session
    opens so a refused request never touches the database."""

    user_sub: str
    trace_id: str | None
    session_id: str | None
    now_ms: int


def now_ms() -> int:
    return int(time.time() * 1000)


def bind_session_id(session_id: str | None) -> None:
    """Bind (or clear, with ``None``) the request's ``session_id`` for M5
    stamping. Called by the request layer, never by a domain."""
    _current_session_id.set(session_id)


def current_session_id() -> str | None:
    return _current_session_id.get()


def current_trace_id_hex() -> str | None:
    """Return the active OpenTelemetry trace id as 32-char hex, or ``None``
    when no valid span is active. Mirrors ``routes/chat.py::
    _current_trace_id_hex`` so console rows correlate with ``interactions``
    rows by the same key."""
    try:
        span = trace.get_current_span()
        ctx = span.get_span_context() if span is not None else None
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x")
    except Exception:  # pragma: no cover - defensive instrumentation read
        return None
    return None


def resolve_user_sub(user_context: UserContext) -> str:
    """Return the ``user_sub`` every query is scoped to — fail-closed.

    * An empty/blank ``user_id`` is refused (a query scoped to ``""`` would
      match nothing on Postgres but is a bug worth surfacing).
    * When the RLS ContextVar is bound (``require_user`` binds it on every
      authenticated request), it MUST equal ``user_context.user_id``. A
      mismatch means the identity object was not the one the token
      resolved — refused before any I/O.
    * When the ContextVar is unbound (background worker, unit test), the
      token-resolved ``user_id`` governs, and the Postgres implementation
      additionally pushes it as the RLS GUC itself, so the DB layer is
      scoped even without the request listener.

    Falsifiable: neuter the mismatch check and
    ``test_scope_mismatch_is_refused_before_any_write`` observes a row
    written under the wrong ``user_sub``.
    """
    sub = user_context.user_id
    if not isinstance(sub, str) or not sub.strip():
        raise ConsoleStoreScopeError("user_context.user_id is empty — refusing")
    bound = rls.current_user_id()
    if bound is not None and bound != sub:
        raise ConsoleStoreScopeError(
            "user_context.user_id disagrees with the RLS request ContextVar — "
            "refusing (identity must be the token-resolved one)"
        )
    return sub


def build_write_stamp(user_context: UserContext) -> WriteStamp:
    """Resolve every server-derived write column in one place."""
    return WriteStamp(
        user_sub=resolve_user_sub(user_context),
        trace_id=current_trace_id_hex(),
        session_id=current_session_id(),
        now_ms=now_ms(),
    )
