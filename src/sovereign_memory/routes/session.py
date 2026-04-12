"""Session management routes.

DESIGN §15 Phase 2: routes that write session data depend on ``require_user``
so the row carries the caller's ``UserContext.user_id`` on the write path.
"""

import logging

from fastapi import APIRouter, Depends

from sovereign_memory.auth import require_scope, require_user
from sovereign_memory.config import get_settings
from sovereign_memory.dependencies import get_conversational_service
from sovereign_memory.identity import UserContext
from sovereign_memory.logging_config import log_call
from sovereign_memory.models import (
    SessionSaveRequest,
    SessionSummaryRequest,
    SessionSummaryResponse,
)
from sovereign_memory.services.conversational import ConversationalService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/save")
@log_call(logger=logger)
async def save_session(
    request: SessionSaveRequest,
    _auth: dict = Depends(require_scope("sovereign-ai:query")),
):
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
    _auth: dict = Depends(require_scope("sovereign-ai:query")),
    user: UserContext = Depends(require_user),
):
    """Save a session summary to the conversational memory layer.

    Equivalent of the legacy ``python3 memory.py session-save`` workflow —
    one row per session in the ``sessions`` table, used by Layer 3
    (conversational memory) to provide continuity across conversations.
    """
    session_id = conversational.save_session(
        user,
        project=request.project,
        summary=request.summary,
        key_points=request.key_points,
    )
    return SessionSummaryResponse(
        status="ok", session_id=session_id, project=request.project
    )
