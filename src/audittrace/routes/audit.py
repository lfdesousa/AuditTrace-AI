"""Audit endpoint — human-facing view over the ``interactions`` table.

Separate from the model-facing ``recall_*`` tools (ADR-025): those serve
the LLM with per-layer snippets, while this endpoint returns structured
rows with pagination for operators / dashboards.

RLS handles per-user scoping automatically: ``require_user`` writes the
caller's Keycloak ``sub`` into the ``_current_user_id`` ContextVar, and
the ``after_begin`` listener (see ``db/rls.py``) emits the
``set_config('app.current_user_id', ...)`` GUC on every transaction so
the policies compare every row's ``user_id`` to the caller. No service-
layer ``WHERE user_id = ...`` is needed (and wouldn't be load-bearing
against a buggy caller anyway — RLS is the enforcement boundary).
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from audittrace.auth import require_scope, require_user
from audittrace.db.models import InteractionRecord as InteractionRow
from audittrace.db.models import SessionRecord as SessionRow
from audittrace.dependencies import get_postgres_factory
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.models import InteractionRecord

logger = logging.getLogger(__name__)

router = APIRouter()


def _row_to_dict(row: InteractionRow) -> dict[str, Any]:
    """Serialise a SQLAlchemy ``InteractionRow`` to a plain dict.

    Kept explicit (rather than piped through a Pydantic model) because
    ``InteractionRow.timestamp`` is stored as an ISO string and we want
    the response to mirror that without a parse/format round-trip.

    Includes the migration-007 failure-audit columns (``status``,
    ``failure_class``, ``error_detail``, ``duration_ms``) so the audit
    browser surfaces failed calls — without these the ADR-033 "every
    failure gets a row" promise is half-landed: the rows exist but the
    audit API pretends they're all successes.
    """
    return {
        "id": row.id,
        "project": row.project,
        "source": row.source,
        "question": row.question,
        "answer": row.answer,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "timestamp": row.timestamp,
        "session_id": row.session_id,
        "model": row.model,
        "user_id": row.user_id,
        "status": row.status,
        "failure_class": row.failure_class,
        "error_detail": row.error_detail,
        "duration_ms": row.duration_ms,
    }


@router.get("/interactions")
@log_call(logger=logger)
async def list_interactions(
    project: str | None = Query(None, description="Filter by project tag (ADR-029)."),
    user_id: str | None = Query(
        None,
        description=(
            "Filter by Keycloak sub. RLS already scopes results to the caller, "
            "so this narrows *within* the caller's own rows."
        ),
    ),
    session_id: str | None = Query(None, description="Filter by session id."),
    source: str | None = Query(
        None, description="Filter by agent source (opencode, curl, continue, …)."
    ),
    since: str | None = Query(
        None,
        description=(
            "ISO-8601 timestamp. Only rows with timestamp >= since are returned."
        ),
    ),
    status: str | None = Query(
        None,
        description=(
            "Filter by status: 'success' or 'failed' (migration 007 / ADR-033). "
            "Use 'failed' to enumerate rows where the chat path errored out."
        ),
    ),
    limit: int = Query(100, ge=1, le=1000, description="Max rows (1-1000)."),
    offset: int = Query(0, ge=0, description="Pagination offset."),
    _auth: dict[str, Any] = Depends(require_scope("audittrace:audit")),
    _user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """List interactions the caller is allowed to see (RLS-scoped).

    Returns ``{interactions: [...], total, limit, offset}`` — ``total`` is
    the row count matching the filters *for the caller*; it's what paginators
    need to render page counts.
    """
    try:
        pg = get_postgres_factory()
    except Exception as exc:
        logger.error("Audit endpoint unavailable — PostgresFactory not registered")
        raise HTTPException(status_code=503, detail="Audit store unavailable") from exc

    session_factory = pg.get_session_factory()
    with session_factory() as db:
        q = db.query(InteractionRow)
        if project is not None:
            q = q.filter(InteractionRow.project == project)
        if user_id is not None:
            q = q.filter(InteractionRow.user_id == user_id)
        if session_id is not None:
            q = q.filter(InteractionRow.session_id == session_id)
        if source is not None:
            q = q.filter(InteractionRow.source == source)
        if since is not None:
            q = q.filter(InteractionRow.timestamp >= since)
        if status is not None:
            q = q.filter(InteractionRow.status == status)

        total = q.count()
        rows = q.order_by(InteractionRow.id.desc()).offset(offset).limit(limit).all()

    return {
        "interactions": [_row_to_dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/interactions")
@log_call(logger=logger)
async def create_interaction(
    record: InteractionRecord,
    _auth: dict[str, Any] = Depends(require_scope("audittrace:audit")),
) -> dict[str, Any]:
    """Create a new interaction audit record.

    Still a stub — the chat route already writes every interaction to
    Postgres on the hot path via ``_persist_interaction``; an external
    POST is only useful if a non-chat source wants to seed the audit
    trail directly. Leaving the shape in place so the route contract
    does not change; wiring is a small follow-up.
    """
    # TODO: external audit-row ingestion (non-chat sources)
    return record.model_dump()


# ─────────────────────────────── Sessions ─────────────────────────────────
# Human-facing browser over the Layer-3 conversational memory. The LLM
# reads the same layer via the `recall_recent_sessions` memory tool
# (ADR-025), this is the counterpart for operators / auditors / dashboards.
# RLS is the enforcement boundary; this handler relies on it the same way
# ``list_interactions`` does — no explicit ``WHERE user_id = ...`` needed,
# the ``after_begin`` listener sets ``app.current_user_id`` and the policy
# on ``sessions`` filters to the caller's rows.


def _session_row_to_dict(row: SessionRow) -> dict[str, Any]:
    """Serialise a ``SessionRow`` to a plain dict for the REST response.

    Returns every column — Luis's 2026-04-18 directive: Day 1
    reconstructibility includes the session layer, not just interactions.
    ``key_points`` is stored as JSON text; we don't parse it here — the
    caller gets exactly what the summariser wrote.
    """
    return {
        "id": row.id,
        "project": row.project,
        "date": row.date,
        "summary": row.summary,
        "key_points": row.key_points,
        "model": row.model,
        "user_id": row.user_id,
        "summarized_at": (
            row.summarized_at.isoformat() if row.summarized_at is not None else None
        ),
    }


@router.get("/sessions")
@log_call(logger=logger)
async def list_sessions(
    project: str | None = Query(None, description="Filter by project tag (ADR-029)."),
    user_id: str | None = Query(
        None,
        description=(
            "Filter by Keycloak sub. RLS already scopes results to the caller, "
            "so this narrows *within* the caller's own rows."
        ),
    ),
    since: str | None = Query(
        None,
        description=(
            "ISO date string. Only sessions with ``date >= since`` are returned."
        ),
    ),
    summarised: bool | None = Query(
        None,
        description=(
            "true → only rows with ``summarized_at`` populated; "
            "false → only un-summarised rows; omit → both."
        ),
    ),
    limit: int = Query(100, ge=1, le=1000, description="Max rows (1-1000)."),
    offset: int = Query(0, ge=0, description="Pagination offset."),
    _auth: dict[str, Any] = Depends(require_scope("audittrace:audit")),
    _user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """List sessions the caller is allowed to see (RLS-scoped).

    Mirrors the shape of ``GET /interactions``:
    ``{sessions: [...], total, limit, offset}``. Ordered by ``date``
    DESC so the freshest sessions come first — matches how the
    Langfuse and Grafana dashboards want to read the data.
    """
    try:
        pg = get_postgres_factory()
    except Exception as exc:
        logger.error("Audit endpoint unavailable — PostgresFactory not registered")
        raise HTTPException(status_code=503, detail="Audit store unavailable") from exc

    session_factory = pg.get_session_factory()
    with session_factory() as db:
        q = db.query(SessionRow)
        if project is not None:
            q = q.filter(SessionRow.project == project)
        if user_id is not None:
            q = q.filter(SessionRow.user_id == user_id)
        if since is not None:
            q = q.filter(SessionRow.date >= since)
        if summarised is True:
            q = q.filter(SessionRow.summarized_at.is_not(None))
        elif summarised is False:
            q = q.filter(SessionRow.summarized_at.is_(None))

        total = q.count()
        rows = q.order_by(SessionRow.date.desc()).offset(offset).limit(limit).all()

    return {
        "sessions": [_session_row_to_dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
