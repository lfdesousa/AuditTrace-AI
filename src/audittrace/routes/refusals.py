"""The durable record of a refused chat turn (BFF-CHAT-ROUTE-ERRORS ADDENDUM-2).

A turn the LibreChat BFF refuses (the sign-in service refused or failed, the
memory server was unreachable, the caller lacked a scope) never reaches
``/v1/chat/completions``, so ``_persist_interaction`` never writes its row.
This route lets the BFF leave that row: an ordinary ``interactions`` row with
``status="failed"`` and the refusal's code as ``failure_class``. No new table,
column or event class.

WHO MAY WRITE is decided by the mesh, not here: the chart admits only the BFF's
service account to this path and keeps the gateway off it
(``charts/audittrace/templates/istio/authorizationpolicy-memory.yaml``), and
the route does not exist unless ``refusal_ingest_enabled`` is set (the chart
sets it; nothing else does). WHOSE turn it was is never taken from the writer:
the row's ``user_id`` is the ``sub`` of ``subject_token``, verified here
(signature, issuer). Expiry is NOT checked: an expired token is still proof of
who was refused. The token is never stored or logged.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from jose import JWTError, jwt

from audittrace.auth import _get_jwks_keys
from audittrace.config import get_settings
from audittrace.db.models import InteractionRecord as InteractionRow
from audittrace.db.rls import set_current_user_id
from audittrace.dependencies import get_postgres_factory
from audittrace.integrity import content_hash as _content_hash
from audittrace.routes._refusal_request import RefusalIngestRequest

logger = logging.getLogger(__name__)
router = APIRouter()

SOURCE = "librechat-bff"
ROW_TYPE = "refused_turn"


async def _verified_subject(token: str) -> str:
    """The ``sub`` of a token this realm signed, or HTTP 422."""
    settings = get_settings()
    allowed = {settings.keycloak_issuer, *settings.keycloak_issuer_extras}
    try:
        keys = await _get_jwks_keys(settings.keycloak_jwks_url)
        claims = jwt.decode(
            token,
            keys,
            algorithms=["RS256"],
            options={"verify_aud": False, "verify_exp": False},
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=422, detail="subject_token is not a token of this realm"
        ) from exc
    issuer, sub = claims.get("iss"), claims.get("sub")
    if not isinstance(issuer, str) or issuer not in allowed:
        raise HTTPException(
            status_code=422, detail="subject_token is not a token of this realm"
        )
    if not isinstance(sub, str) or not sub.strip():
        raise HTTPException(status_code=422, detail="subject_token names no subject")
    return sub


def _row(request: RefusalIngestRequest, user_id: str, trace_id: str | None) -> Any:
    fields: dict[str, Any] = {
        "project": "librechat",
        "source": SOURCE,
        "question": ROW_TYPE,
        "answer": request.code,
        "user_id": user_id,
        "session_id": request.session_id,
        "trace_id": trace_id,
        "timestamp": datetime.now().isoformat(),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "status": "failed",
        "failure_class": request.code,
        "error_detail": json.dumps(
            {
                "row_type": ROW_TYPE,
                "route": request.route,
                "code": request.code,
                "http_status": request.http_status,
                "inbound_trace_id": request.inbound_trace_id,
            }
        ),
        "model": None,
        "duration_ms": None,
        "event_class": None,
    }
    return InteractionRow(**fields, content_hash=_content_hash(fields))


@router.post("/interactions/refusals", status_code=201)
async def record_refused_turn(request: RefusalIngestRequest) -> dict[str, Any]:
    """Write one failed-interaction row for a chat turn the BFF refused."""
    from audittrace.routes.chat import _current_trace_id_hex

    if not get_settings().refusal_ingest_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    user_id = await _verified_subject(request.subject_token)
    # The row belongs to the refused user, not to the writer: bind the RLS
    # identity to the verified subject before the session opens (migration
    # 005's WITH CHECK compares user_id with it).
    set_current_user_id(user_id)
    row = _row(request, user_id, _current_trace_id_hex())
    async with get_postgres_factory().get_session_factory()() as db:
        db.add(row)
        await db.commit()
    logger.info("refused_turn recorded code=%s", request.code)
    return {"recorded": True, "failure_class": request.code}
