"""The durable record of a refused chat turn (BFF-CHAT-ROUTE-ERRORS ADDENDUM-2, -3).

A turn the LibreChat BFF refuses never reaches ``/v1/chat/completions``, so
``_persist_interaction`` never writes its row. This route lets the BFF leave
that row: an ordinary ``interactions`` row with ``status="failed"`` and the
refusal's code as ``failure_class``. No new table, column or event class.

Two questions, answered separately, neither by how the path is spelled:

WHO WRITES. The route is mounted only when ``refusal_ingest_writer`` names a
SPIFFE identity (``server.create_app``), and it admits a request only when this
workload's own sidecar says that identity is the immediate peer
(``audittrace.mesh_peer``). The mesh policy that keeps other workloads off the
path stays, as a second line; it is no longer the only one. The request line
must also spell the path exactly: an encoded or doubled slash is not this
route.

WHOSE TURN. Never taken from the writer. The row's ``user_id`` is the ``sub``
of ``subject_token``, verified exactly as every other token of this server:
signature against the realm's signing key, issuer, AUDIENCE and EXPIRY; and it
must have been issued to the LibreChat login client. The token is never stored,
logged or echoed (a body that does not validate is answered with field names).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from jose import JWTError
from pydantic import ValidationError

from audittrace.auth import _decode_jwt_with_allowed_issuers, _get_jwks_keys
from audittrace.config import get_settings
from audittrace.db.models import InteractionRecord as InteractionRow
from audittrace.db.rls import set_current_user_id
from audittrace.dependencies import get_postgres_factory
from audittrace.integrity import content_hash as _content_hash
from audittrace.mesh_peer import HEADER, peer_spiffe_id
from audittrace.realm_keys import token_kid
from audittrace.routes._refusal_request import RefusalIngestRequest

logger = logging.getLogger(__name__)
router = APIRouter()

PATH = "/interactions/refusals"
SOURCE = "librechat-bff"
ROW_TYPE = "refused_turn"
_NOT_A_REALM_TOKEN = "subject_token is not a valid token of this realm's login client"


def require_writer(request: Request) -> None:
    """Admit the configured mesh peer only, on the exactly spelled path.

    Runs before the body is looked at. 404 for a path spelled any other way
    (the server decodes ``%2F``; the mesh does not); 403 for any other peer.
    """
    if request.scope.get("raw_path") != PATH.encode():
        raise HTTPException(status_code=404, detail="Not Found")
    expected = get_settings().refusal_ingest_writer
    if not expected or peer_spiffe_id(request.headers.get(HEADER)) != expected:
        logger.warning("refused_turn.writer_refused")
        raise HTTPException(status_code=403, detail="Forbidden")


async def _verified_subject(token: str) -> str:
    """The ``sub`` of a live token this realm issued to the login client, or 422."""
    settings = get_settings()
    try:
        keys = await _get_jwks_keys(settings.keycloak_jwks_url, kid=token_kid(token))
        claims = _decode_jwt_with_allowed_issuers(
            token,
            keys,
            audience=settings.jwt_audience,
            primary_issuer=settings.keycloak_issuer,
            extra_issuers=settings.keycloak_issuer_extras,
        )
    except JWTError as exc:
        raise HTTPException(status_code=422, detail=_NOT_A_REALM_TOKEN) from exc
    sub = claims.get("sub")
    # python-jose checks the audience only when the claim is present, so its
    # absence is refused here (gate condition C4); a present claim that does
    # not hold this server's identifier was already refused by the decode.
    if "aud" not in claims:
        raise HTTPException(status_code=422, detail=_NOT_A_REALM_TOKEN)
    if claims.get("azp") != settings.refusal_ingest_subject_client:
        raise HTTPException(status_code=422, detail=_NOT_A_REALM_TOKEN)
    if not isinstance(sub, str) or not sub.strip():
        raise HTTPException(status_code=422, detail=_NOT_A_REALM_TOKEN)
    return sub


async def _json_body(request: Request) -> Any:
    """The body, read here and not by the framework: the writer check has run
    by now, and a body that is not JSON is answered without its content."""
    try:
        return json.loads(await request.body())
    except ValueError:
        raise HTTPException(
            status_code=422, detail={"invalid_fields": ["body"]}
        ) from None


def _validated(payload: Any) -> RefusalIngestRequest:
    """The request model, or a 422 that names fields and never values."""
    try:
        return RefusalIngestRequest.model_validate(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(part) for part in e["loc"]) for e in exc.errors()}
        )
        raise HTTPException(
            status_code=422, detail={"invalid_fields": fields}
        ) from None


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
        # Migration 041: correlation metadata, unhashed by construction
        # (``content_hash`` reads only the integrity field tuples).
        "client_message_id": request.client_message_id,
    }
    return InteractionRow(**fields, content_hash=_content_hash(fields))


_BODY_SCHEMA = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": RefusalIngestRequest.model_json_schema()}
        },
    }
}


@router.post(
    PATH,
    status_code=201,
    dependencies=[Depends(require_writer)],
    openapi_extra=_BODY_SCHEMA,
)
async def record_refused_turn(http_request: Request) -> dict[str, Any]:
    """Write one failed-interaction row for a chat turn the BFF refused."""
    from audittrace.routes.chat import _current_trace_id_hex

    request = _validated(await _json_body(http_request))
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
