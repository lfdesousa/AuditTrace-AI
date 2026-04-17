"""Context retrieval route — 4-layer memory augmentation (ADR-018).

DESIGN §15 Phase 2: depends on ``require_user`` so every context build
carries a concrete ``UserContext`` down through all four layers.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends

from audittrace.auth import require_scope, require_user
from audittrace.dependencies import get_context_builder
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.models import ContextBuildResponse, ContextRequest
from audittrace.services.context_builder import ContextBuilderService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/context", response_model=ContextBuildResponse)
@log_call(logger=logger)
async def get_context(
    request: ContextRequest,
    context_builder: ContextBuilderService = Depends(get_context_builder),
    _auth: dict[str, Any] = Depends(require_scope("audittrace:context")),
    user: UserContext = Depends(require_user),
) -> ContextBuildResponse:
    """Retrieve relevant context from all 4 memory layers."""
    context_string, layer_stats = context_builder.build_system_context_with_stats(
        user,
        project=request.project,
        query=request.query,
    )
    return ContextBuildResponse(
        context_string=context_string,
        layer_stats=layer_stats,
        query=request.query,
        project=request.project,
    )
