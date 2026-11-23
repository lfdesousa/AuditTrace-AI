"""Evidence routes: one composed ``audittrace.evidence.v1`` document per audit row.

``GET /evidence/messages/{client_message_id}`` and ``GET /evidence/interactions/{id}``
serve the reconstruct-this-answer view the console renders: the row, its tool calls, the
integrity verdict recomputed from the stored row, and who can read it.

**Authorisation.** Scope ``audittrace:audit`` plus ``require_user``; the caller's ``sub``
comes from the verified token, never from the request. Row-level security is the
enforcement boundary (``interactions`` / ``tool_calls`` policies, migration 005); on top of
it every query carries ``user_id = <caller>`` (defence in depth, and what makes the SQLite
test dialect meaningful). A row that does not exist, a row that is not the caller's and an
id that does not have the accepted shape all answer the SAME 404 (status and body): no
existence oracle. No scope lifts the policy; there is no cross-user view.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Security
from sqlalchemy import func, select

from audittrace.auth import require_user, validate_jwt
from audittrace.db.models import InteractionRecord as InteractionRow
from audittrace.db.models import ToolCall as ToolCallRow
from audittrace.dependencies import get_postgres_factory
from audittrace.identity import UserContext
from audittrace.logging_config import log_call
from audittrace.routes._id_bounds import possible_interaction_id
from audittrace.routes._message_id_bind import message_id_rejection
from audittrace.services.evidence.compose import evidence_document

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/evidence")

#: The ONE body of every not-found answer: the framework's own, so an id that never
#: reaches the handler (a dot segment, an encoded slash) answers byte-identically too.
_NOT_FOUND = "Not Found"


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=_NOT_FOUND)


async def _document(
    *,
    by: str,
    value: str,
    caller_id: str,
    row_filter: Any,
) -> dict[str, Any]:
    try:
        pg = get_postgres_factory()
    except Exception as exc:
        logger.error("Evidence endpoint unavailable — PostgresFactory not registered")
        raise HTTPException(status_code=503, detail="Audit store unavailable") from exc

    async with pg.get_session_factory()() as db:
        own = InteractionRow.user_id == caller_id
        candidates = (
            await db.execute(
                select(func.count()).select_from(InteractionRow).where(own, row_filter)
            )
        ).scalar_one()
        row = (
            (
                await db.execute(
                    select(InteractionRow)
                    .where(own, row_filter)
                    .order_by(InteractionRow.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            raise _not_found()
        tool_rows = list(
            (
                await db.execute(
                    select(ToolCallRow)
                    .where(
                        ToolCallRow.interaction_id == row.id,
                        ToolCallRow.user_id == caller_id,
                    )
                    .order_by(ToolCallRow.started_at.asc(), ToolCallRow.id.asc())
                )
            )
            .scalars()
            .all()
        )
        # Serialise while the session is open (the audit routes' rule).
        return evidence_document(
            row=row,
            tool_call_rows=tool_rows,
            caller_id=caller_id,
            lookup_by=by,
            lookup_value=value,
            candidates=candidates,
        )


@router.get("/messages/{client_message_id}")
@log_call(logger=logger)
async def evidence_by_message(
    client_message_id: str,
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["audittrace:audit"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Evidence for the caller's newest audit row of one console message.

    ``lookup.candidates`` counts the caller's rows carrying the id (a regenerate mints a
    new id; a continue may reuse one). An id that does not have the accepted shape is the
    same 404 as an unknown one.
    """
    if message_id_rejection(client_message_id) is not None:
        raise _not_found()
    return await _document(
        by="client_message_id",
        value=client_message_id,
        caller_id=user.user_id,
        row_filter=InteractionRow.client_message_id == client_message_id,
    )


@router.get("/interactions/{interaction_id}")
@log_call(logger=logger)
async def evidence_by_interaction(
    interaction_id: str,
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["audittrace:audit"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Evidence for one of the caller's audit rows by its id (any row type: chat,
    ``mcp``, ``memory-audit``, a refused turn).

    The id is taken as text and checked here: anything that cannot be a row key (not
    ASCII digits, below 1, above the 32-bit key range) is the same 404 as an unknown id,
    never a 422 and never a driver error."""
    key = possible_interaction_id(interaction_id)
    if key is None:
        raise _not_found()
    return await _document(
        by="interaction_id",
        value=str(key),
        caller_id=user.user_id,
        row_filter=InteractionRow.id == key,
    )
