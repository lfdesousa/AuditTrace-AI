"""Request-context readers for the console-store base — the ONLY source of
the security-relevant and traceability columns the base stamps.

Every value here is SERVER-DERIVED:

* ``user_sub`` — from the token-resolved :class:`~audittrace.identity.
  UserContext` (``require_user``), cross-checked against the Postgres-RLS
  request ContextVar (``audittrace.db.rls``) that the same dependency binds.
  A disagreement is fail-closed (:class:`ConsoleStoreScopeError`) BEFORE any
  session opens. Never from a request body.
* ``trace_id`` — the active OpenTelemetry span's trace id (32-hex), the same
  derivation ``interactions.trace_id`` uses (EU AI Act Art 12 traceability,
  M5): ``current_trace_id_hex`` below IS ``telemetry.current_trace_id_hex``
  (X-TRACE-ID D1, one function, pinned by identity). ``None`` when no valid
  span is active.
* ``session_id`` — a request-scoped ContextVar bound by the request layer
  (``bound_session_id``); ``None`` when the request layer has not bound one.
  The two production request layers that resolve a session (``POST
  /v1/chat/completions`` and ``POST /mcp`` ``tools/call``) bind it (v1.31.0
  WP-6, SESSION-ID-BIND); wiring each console route's ``X-Session-Id``
  header is the M5 retrofit vehicle.

None of these functions accept a caller-supplied override — there is no
parameter through which a body value could reach a stamped column.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterator
from contextvars import ContextVar, Token
from dataclasses import dataclass

from audittrace.db import rls
from audittrace.identity import UserContext
from audittrace.services.console_store._errors import ConsoleStoreScopeError
from audittrace.telemetry import current_trace_id_hex as current_trace_id_hex

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


def bind_session_id(session_id: str | None) -> Token[str | None]:
    """Bind (or clear, with ``None``) the request's ``session_id`` for M5
    stamping and for the ``session_id`` field of every log line (the
    ``TraceContextFilter`` reads it). Called by the request layer, never by a
    domain.

    Returns the :class:`~contextvars.Token` so the caller can restore the
    PREVIOUS value with :func:`reset_session_id`; request code uses
    :func:`bound_session_id` instead of pairing the two by hand.
    """
    return _current_session_id.set(session_id)


def reset_session_id(token: Token[str | None]) -> None:
    """Undo one :func:`bind_session_id` (restores the value it replaced)."""
    _current_session_id.reset(token)


@contextlib.contextmanager
def bound_session_id(session_id: str | None) -> Iterator[None]:
    """Bind ``session_id`` for the duration of the block and restore the
    previous value on every exit path (SESSION-ID-BIND AC-6.3: nothing bleeds
    into the next request served on the same task).

    Falsifiable: drop the :func:`reset_session_id` call and
    ``test_ac_6_3_*`` observes the id still bound after the block.
    """
    token = bind_session_id(session_id)
    try:
        yield
    finally:
        reset_session_id(token)


def current_session_id() -> str | None:
    return _current_session_id.get()


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
