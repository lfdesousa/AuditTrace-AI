"""Session management routes.

DESIGN §15 Phase 2: routes that write session data depend on ``require_user``
so the row carries the caller's ``UserContext.user_id`` on the write path.
"""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends

from audittrace.auth import require_scope, require_user
from audittrace.config import get_settings
from audittrace.dependencies import get_conversational_service
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.models import (
    SessionSaveRequest,
    SessionSummaryRequest,
    SessionSummaryResponse,
)
from audittrace.services.conversational import ConversationalService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/save")
@log_call(logger=logger)
async def save_session(
    request: SessionSaveRequest,
    _auth: dict[str, Any] = Depends(require_scope("audittrace:query")),
) -> dict[str, Any]:
    """Persist session interactions to the audit trail."""
    settings = get_settings()
    logger.debug(
        "save_session project=%s count=%d", request.project, len(request.interactions)
    )
    # TODO: Phase 1 - batch insert into PostgreSQL + update ChromaDB
    _ = settings  # noqa: F841 (reserved for Phase 1 wiring)
    return {
        "status": "ok",
        "project": request.project,
        "interactions_saved": len(request.interactions),
        "metadata": request.metadata,
    }


@router.post("/summary", response_model=SessionSummaryResponse)
@log_call(logger=logger)
async def save_session_summary(
    request: SessionSummaryRequest,
    conversational: ConversationalService = Depends(get_conversational_service),
    _auth: dict[str, Any] = Depends(require_scope("audittrace:query")),
    user: UserContext = Depends(require_user),
) -> SessionSummaryResponse:
    """Save a session summary to the conversational memory layer.

    Equivalent of the legacy ``python3 memory.py session-save`` workflow —
    one row per session in the ``sessions`` table, used by Layer 3
    (conversational memory) to provide continuity across conversations.
    """
    # ADR-030: the service requires an explicit session_id. Accept one
    # from the client when they know it (summarising a real chat), or
    # generate a UUID for standalone admin-style summaries.
    resolved_session_id = request.session_id or str(uuid.uuid4())
    session_id = conversational.save_session(
        user,
        project=request.project,
        summary=request.summary,
        key_points=request.key_points,
        session_id=resolved_session_id,
    )
    return SessionSummaryResponse(
        status="ok", session_id=session_id, project=request.project
    )
