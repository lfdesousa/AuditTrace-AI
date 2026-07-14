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

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security
from sqlalchemy import func, select

from audittrace.auth import require_user, validate_jwt
from audittrace.db.models import InteractionRecord as InteractionRow
from audittrace.db.models import SessionRecord as SessionRow
from audittrace.dependencies import get_postgres_factory
from audittrace.identity import UserContext
from audittrace.integrity import content_hash as _content_hash
from audittrace.logging_config import log_call
from audittrace.models import (
    AssessmentIngestRequest,
    InteractionListResponse,
    InteractionRecord,
    SessionListResponse,
)

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
    audit API pretends they're all successes. Migration 008 adds
    ``trace_id`` for single-query Postgres↔Tempo correlation.
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
        "trace_id": row.trace_id,
        # Migration 012 (ADR-048 PR-B1): NULL on rows pre-dating the
        # column; PR-B4 backfills chat-completion rows with
        # ``"interaction"`` and writes ``"security"`` for content-control
        # verdict rows.
        "event_class": row.event_class,
        # Migration 015 (ADR-058 WS-A1): DB-server-assigned insert clock,
        # independent of the application writer. Serialised to ISO-8601;
        # NULL only on the duck-typed schema-drift stand-in, never on a
        # real row (the column is NOT NULL with a server default).
        "created_at": row.created_at.isoformat() if row.created_at else None,
        # Migration 017 (ADR-058 WS-A3): content-integrity hash; a
        # mismatch on recomputation means the row was tampered with.
        "content_hash": row.content_hash,
    }


@router.get("/interactions", response_model=InteractionListResponse)
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
    event_class: str | None = Query(
        None,
        description=(
            "Filter by event_class: 'interaction' | 'security' | 'assessment' "
            "(ADR-048 / ADR-058). Pull a whole recorded self-assessment with "
            "event_class=assessment & session_id=<assessment_id>."
        ),
    ),
    limit: int = Query(100, ge=1, le=1000, description="Max rows (1-1000)."),
    offset: int = Query(0, ge=0, description="Pagination offset."),
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["audittrace:audit"]),
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
    async with session_factory() as db:
        stmt = select(InteractionRow)
        if project is not None:
            stmt = stmt.where(InteractionRow.project == project)
        if user_id is not None:
            stmt = stmt.where(InteractionRow.user_id == user_id)
        if session_id is not None:
            stmt = stmt.where(InteractionRow.session_id == session_id)
        if source is not None:
            stmt = stmt.where(InteractionRow.source == source)
        if since is not None:
            stmt = stmt.where(InteractionRow.timestamp >= since)
        if status is not None:
            stmt = stmt.where(InteractionRow.status == status)
        if event_class is not None:
            stmt = stmt.where(InteractionRow.event_class == event_class)

        total = (
            await db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        rows = (
            (
                await db.execute(
                    stmt.order_by(InteractionRow.id.desc()).offset(offset).limit(limit)
                )
            )
            .scalars()
            .all()
        )

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
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["audittrace:audit"]),
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


# ───────────────────── Recursive self-audit (ADR-058) ─────────────────────
# The recorder records the evidence of its OWN security review — the rules
# of engagement, the questions + verdicts, the findings, the deferrals — as
# first-class ``event_class="assessment"`` rows, through its own front door
# under a dedicated least-privilege scope. Owner-scoped by the same RLS as
# everything else; correlated by ``assessment_id`` (stored in ``session_id``
# so the existing session filter groups a whole assessment for free). The
# structured detail rides in ``error_detail`` JSON (mirrors
# ``scan_audit_consumer``), so NO schema migration is needed.


def _build_assessment_rows(
    request: AssessmentIngestRequest, user_id: str, trace_id: str | None
) -> list[InteractionRow]:
    """Fan one assessment into a header row plus one child per item.

    ``question``/``answer`` carry the human-legible line so ``_row_to_dict``
    renders without JSON parsing; the machine-readable structure rides in
    ``error_detail`` as JSON. Every row shares ``event_class="assessment"``,
    the owner ``user_id``, and ``assessment_id`` in ``session_id``.
    """
    now = datetime.now().isoformat()
    aid = request.assessment_id
    # The model-default columns are set explicitly so the WS-A3 content hash
    # matches what is persisted (SQLAlchemy applies column defaults at flush,
    # not at construction time).
    base: dict[str, Any] = {
        "project": request.project,
        "source": request.source,
        "user_id": user_id,
        "session_id": aid,
        "event_class": "assessment",
        "trace_id": trace_id,
        "timestamp": now,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "status": "success",
        "failure_class": None,
        "model": None,
        "duration_ms": None,
    }

    def _row(question: str, answer: str, detail: dict[str, Any]) -> InteractionRow:
        fields = {
            **base,
            "question": question,
            "answer": answer,
            "error_detail": json.dumps(detail),
        }
        return InteractionRow(**fields, content_hash=_content_hash(fields))

    rows: list[InteractionRow] = [
        _row(
            "assessment_header",
            aid,
            {
                "row_type": "assessment_header",
                "assessment_id": aid,
                "frameworks": request.frameworks,
                "rules_of_engagement": request.rules_of_engagement,
                "teardown": request.teardown,
            },
        )
    ]
    for q in request.questions:
        rows.append(
            _row(
                q.question,
                q.verdict,
                {
                    "row_type": "assessment_question",
                    "assessment_id": aid,
                    "method": q.method,
                    "verdict": q.verdict,
                },
            )
        )
    for f in request.findings:
        rows.append(
            _row(
                f.title,
                f.severity,
                {
                    "row_type": "assessment_finding",
                    "assessment_id": aid,
                    "finding_id": f.finding_id,
                    "severity": f.severity,
                    "detail": f.detail,
                },
            )
        )
    for d in request.deferrals:
        rows.append(
            _row(
                d.item,
                d.reason or "",
                {
                    "row_type": "assessment_deferral",
                    "assessment_id": aid,
                    "reason": d.reason,
                },
            )
        )
    return rows


@router.post("/assessments")
@log_call(logger=logger)
async def create_assessment(
    request: AssessmentIngestRequest,
    _auth: dict[str, Any] = Security(
        validate_jwt, scopes=["audittrace:assessment:ingest"]
    ),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Record a security self-assessment as first-class audit events (ADR-058).

    The recorder becomes a witness to its own governability: the assessment
    is written through the recorder's own front door, under a dedicated
    least-privilege scope (``audittrace:assessment:ingest``, distinct from
    the broad ``audittrace:audit`` read scope), as owner-scoped,
    trace-linked ``assessment`` rows. Recording it this way IS the
    assessment's rules-of-engagement evidence.
    """
    from audittrace.routes.chat import _current_trace_id_hex

    try:
        pg = get_postgres_factory()
    except Exception as exc:
        logger.error("Assessment ingest unavailable — PostgresFactory not registered")
        raise HTTPException(status_code=503, detail="Audit store unavailable") from exc

    rows = _build_assessment_rows(request, user.user_id, _current_trace_id_hex())
    session_factory = pg.get_session_factory()
    async with session_factory() as db:
        db.add_all(rows)
        await db.commit()

    return {
        "assessment_id": request.assessment_id,
        "rows_written": len(rows),
        "event_class": "assessment",
    }


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
        # #344 — trace_id of the background summariser run that produced this
        # row, so a reader of the audit API can pivot straight to the Tempo/
        # Langfuse trace of the summariser's model call.
        "trace_id": row.trace_id,
    }


@router.get("/sessions", response_model=SessionListResponse)
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
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["audittrace:audit"]),
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
    async with session_factory() as db:
        stmt = select(SessionRow)
        if project is not None:
            stmt = stmt.where(SessionRow.project == project)
        if user_id is not None:
            stmt = stmt.where(SessionRow.user_id == user_id)
        if since is not None:
            stmt = stmt.where(SessionRow.date >= since)
        if summarised is True:
            stmt = stmt.where(SessionRow.summarized_at.is_not(None))
        elif summarised is False:
            stmt = stmt.where(SessionRow.summarized_at.is_(None))

        total = (
            await db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        rows = (
            (
                await db.execute(
                    stmt.order_by(SessionRow.date.desc()).offset(offset).limit(limit)
                )
            )
            .scalars()
            .all()
        )

    return {
        "sessions": [_session_row_to_dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
