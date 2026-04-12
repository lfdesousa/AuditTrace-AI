"""Context retrieval route — 4-layer memory augmentation (ADR-018).

DESIGN §15 Phase 2: depends on ``require_user`` so every context build
carries a concrete ``UserContext`` down through all four layers.
"""

import logging

from fastapi import APIRouter, Depends

from sovereign_memory.auth import require_scope, require_user
from sovereign_memory.dependencies import get_context_builder
from sovereign_memory.identity import UserContext
from sovereign_memory.logging_config import log_call
from sovereign_memory.models import ContextBuildResponse, ContextRequest
from sovereign_memory.services.context_builder import ContextBuilderService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/context", response_model=ContextBuildResponse)
@log_call(logger=logger)
async def get_context(
    request: ContextRequest,
    context_builder: ContextBuilderService = Depends(get_context_builder),
    _auth: dict = Depends(require_scope("sovereign-ai:context")),
    user: UserContext = Depends(require_user),
):
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
