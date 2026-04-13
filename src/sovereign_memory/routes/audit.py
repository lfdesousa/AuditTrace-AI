"""Audit and interactions route."""

import logging
from typing import Any

from fastapi import APIRouter, Depends

from sovereign_memory.auth import require_scope
from sovereign_memory.logging_config import log_call
from sovereign_memory.models import InteractionRecord

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/interactions")
@log_call(logger=logger)
async def list_interactions(
    project: str | None = None,
    limit: int = 100,
    offset: int = 0,
    _auth: dict[str, Any] = Depends(require_scope("sovereign-ai:audit")),
) -> dict[str, Any]:
    """List interaction records from audit trail."""
    # TODO: Phase 1 - PostgreSQL query
    return {"interactions": [], "total": 0, "limit": limit, "offset": offset}


@router.post("/interactions")
@log_call(logger=logger)
async def create_interaction(
    record: InteractionRecord,
    _auth: dict[str, Any] = Depends(require_scope("sovereign-ai:audit")),
) -> dict[str, Any]:
    """Create a new interaction audit record."""
    # TODO: Phase 1 - insert into PostgreSQL
    return record.model_dump()
