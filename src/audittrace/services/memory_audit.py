"""ADR-062 §5 — first-class audit event on every ``/memory/*`` access.

"Reading the recorder is recorded": every GET/POST/PUT/DELETE against the
memory backoffice (episodic / procedural / semantic, plus the bulk
``POST /memory/index`` and the ``GET /memory/upload/status`` poll) emits an
owner-scoped, trace-linked audit row, reconstructable via ``GET
/interactions``. Closes ADR-062's contradiction 4 (CS-4 vs CS-2: recall was
already per-user filtered, but the backoffice read/write surface emitted no
first-class audit event at all).

Reuses the existing ``InteractionRecord`` shape 1:1 — mirrors
``services/scan_audit_consumer.py::_persist_audit`` (ADR-048 PR-B4) and
``routes/audit.py::_build_assessment_rows`` (ADR-058): the structured detail
rides in ``error_detail`` as JSON, so **no schema migration is needed**. The
only new thing is a value: ``event_class="memory_access"``, added to the
closed set in ``routes/memory_scan.py::_EVENT_CLASS_VALUES`` alongside the
existing ``interaction`` / ``security`` / ``assessment`` values — additive,
not a shape change (the audit-schema freeze,
``project_audit_schema_contract_freeze``).

Read vs write discipline (spec 2026-08-04-SPEC-ADR-062-five-layer-memory.md
§WU-A4 — "decide read=fail-open vs write=fail-closed and justify it"):

* **Reads** (``list`` / ``read``) are audited via FastAPI ``BackgroundTasks``
  — scheduled to run *after* the response has already been sent, on the
  same asyncio Task, so a slow or unavailable audit store never delays or
  denies the caller their own data. A failed background emit is logged
  loudly (``memory_audit.read_emit_failed``) — visible to ops, but the
  read itself already succeeded from the caller's point of view. Reads
  vastly outnumber writes (every ``recall_*`` tool call is effectively a
  read), so blocking availability on audit-store health here would trade a
  large, frequent surface for a marginal reconstructibility gain.
* **Writes/deletes** (``write`` / ``delete``) are audited INLINE, awaited
  before the handler returns. If the audit write raises, the exception
  propagates — FastAPI's registered ``Exception`` handler
  (``server.py::unhandled_exception_handler``) turns it into a 500, so the
  caller cannot walk away believing an *unaudited* mutation succeeded. This
  mirrors ``_persist_audit``'s "refuse to persist an unattributable audit
  row" discipline (ADR-058): a mutation that cannot be proven to have
  happened, from a reconstructibility standpoint, is not allowed to look
  like a clean success to the caller. Note this is NOT a full saga/rollback
  — the underlying S3/manifest/ChromaDB mutation has already committed by
  the time the audit emit runs (Phase A explicitly excludes distributed-
  transaction machinery as out of scope, see ADR-062 §9); the 500 is a
  caller-visible signal demanding investigation/retry, not a guarantee the
  domain write was undone.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import BackgroundTasks

from audittrace.db.models import InteractionRecord
from audittrace.dependencies import get_postgres_factory
from audittrace.identity import UserContext

logger = logging.getLogger(__name__)

# Additive value — see module docstring. Declared here (not re-derived at
# each call site) so routes/memory_scan.py's closed set and this module's
# writer can never independently drift on the literal string.
MEMORY_AUDIT_EVENT_CLASS = "memory_access"
MEMORY_AUDIT_PROJECT = "memory-governance"
MEMORY_AUDIT_SOURCE = "memory-audit"

MemoryOp = Literal["read", "list", "write", "delete"]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _current_trace_id_hex() -> str | None:
    """Lazy import of the chat route's trace-id helper.

    Deferred (rather than a module-level import) so this module never
    creates an import-time coupling to ``routes/chat.py`` — mirrors the
    same lazy-import pattern already used by ``routes/audit.py``'s
    ``create_assessment`` handler for the identical helper.
    """
    from audittrace.routes.chat import _current_trace_id_hex as _impl

    return _impl()


def _build_row(
    *,
    user: UserContext,
    op: MemoryOp,
    layer: str,
    collection: str | None,
    key: str | None,
    tier: str,
    outcome: str,
    trace_id: str | None,
    detail_extra: dict[str, Any] | None,
) -> InteractionRecord:
    if not user.user_id:
        # ADR-058 invariant: never persist an audit row with a NULL owner
        # (permanently unreadable under RLS — see
        # scan_audit_consumer.py:249-256 for the precedent). Every
        # /memory/* route requires ``Depends(require_user)``, so this
        # branch should be unreachable in production; fail loud rather
        # than silently drop an unattributable audit row.
        raise ValueError(
            f"memory-audit event for op={op!r} layer={layer!r} key={key!r} "
            "has no user_id; refusing to persist an unattributable audit row"
        )
    detail: dict[str, Any] = {
        "op": op,
        "layer": layer,
        "collection": collection,
        "key": key,
        "tier": tier,
    }
    if detail_extra:
        detail.update(detail_extra)
    return InteractionRecord(
        project=MEMORY_AUDIT_PROJECT,
        source=MEMORY_AUDIT_SOURCE,
        question=f"op={op} layer={layer} key={key or '-'}",
        answer="",
        timestamp=_now_iso(),
        user_id=user.user_id,
        trace_id=trace_id if trace_id is not None else _current_trace_id_hex(),
        status=outcome,
        failure_class=None if outcome == "success" else "memory_access_failed",
        error_detail=json.dumps(detail),
        event_class=MEMORY_AUDIT_EVENT_CLASS,
    )


async def emit_memory_audit_event(
    *,
    user: UserContext,
    op: MemoryOp,
    layer: str,
    collection: str | None = None,
    key: str | None = None,
    tier: str = "corpus",
    outcome: str = "success",
    trace_id: str | None = None,
    detail_extra: dict[str, Any] | None = None,
) -> None:
    """Persist one memory-access audit row.

    ``tier`` defaults to ``"corpus"`` — Phase A has not yet wired the
    per-user ``memory-private/{sub}/`` tier (ADR-062 §9 Phase B); every
    layer reads/writes the shared bucket/collections today (ADR-062 CS-3),
    so every Phase-A audit row is honestly labelled ``corpus`` rather than
    a not-yet-true ``private``.

    Callers on the WRITE/DELETE path should ``await`` this directly (see
    module docstring — fail-closed). Callers on the READ path should
    schedule :func:`emit_memory_audit_event_background` via
    ``BackgroundTasks.add_task`` instead of awaiting this function inline.
    """
    row = _build_row(
        user=user,
        op=op,
        layer=layer,
        collection=collection,
        key=key,
        tier=tier,
        outcome=outcome,
        trace_id=trace_id,
        detail_extra=detail_extra,
    )
    pg = get_postgres_factory()
    session_factory = pg.get_session_factory()
    async with session_factory() as db:
        db.add(row)
        await db.commit()


async def emit_memory_audit_event_background(
    *,
    user: UserContext,
    op: MemoryOp,
    layer: str,
    collection: str | None = None,
    key: str | None = None,
    tier: str = "corpus",
    outcome: str = "success",
    trace_id: str | None = None,
    detail_extra: dict[str, Any] | None = None,
) -> None:
    """Fail-open wrapper for the READ path — swallow + log any failure.

    Used exclusively as a ``BackgroundTasks.add_task`` target. Starlette
    runs background tasks after the response has been sent; an exception
    raised there is otherwise only visible as an "Exception in ASGI
    application" log line with no memory-specific context, so this wrapper
    catches broadly and logs the operation shape that failed to be
    recorded — the read already succeeded and returned to the caller, but
    is NOT reconstructable via ``GET /interactions`` until the underlying
    audit-store issue is fixed."""
    try:
        await emit_memory_audit_event(
            user=user,
            op=op,
            layer=layer,
            collection=collection,
            key=key,
            tier=tier,
            outcome=outcome,
            trace_id=trace_id,
            detail_extra=detail_extra,
        )
    except Exception:
        logger.exception(
            "memory_audit.read_emit_failed op=%s layer=%s collection=%s key=%s "
            "user_id=%s — read succeeded but is NOT reconstructable via "
            "/interactions until this is fixed",
            op,
            layer,
            collection,
            key,
            user.user_id,
        )


def schedule_read_audit(
    background_tasks: BackgroundTasks,
    *,
    user: UserContext,
    op: MemoryOp,
    layer: str,
    collection: str | None = None,
    key: str | None = None,
    tier: str = "corpus",
    detail_extra: dict[str, Any] | None = None,
) -> None:
    """Schedule a fail-open read audit event on ``background_tasks``.

    Thin convenience wrapper so route handlers don't each have to spell
    out ``background_tasks.add_task(emit_memory_audit_event_background,
    ...)`` with the full keyword-argument list. ``trace_id`` is resolved
    eagerly (from the still-active request span) rather than left to the
    background task, since OpenTelemetry span context does not reliably
    survive into a task that runs after the response has been sent
    (mirrors the defensive capture pattern already used by
    ``routes/chat.py``'s persist paths).
    """
    background_tasks.add_task(
        emit_memory_audit_event_background,
        user=user,
        op=op,
        layer=layer,
        collection=collection,
        key=key,
        tier=tier,
        trace_id=_current_trace_id_hex(),
        detail_extra=detail_extra,
    )
