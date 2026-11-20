"""Request body of ``POST /interactions/refusals`` (BFF-CHAT-ROUTE-ERRORS ADDENDUM-2)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RefusalIngestRequest(BaseModel):
    """One chat turn the LibreChat BFF refused before it reached the model.

    A closed vocabulary: nothing free-form is accepted and unknown fields are
    refused. There is NO user field. The user is derived by the server from
    ``subject_token``, the caller's own token, which the server verifies.
    """

    model_config = ConfigDict(extra="forbid")

    route: Literal["chat"]
    code: Literal[
        "invalid_token",
        "insufficient_scope",
        "auth_service_unavailable",
        "token_exchange_refused",
        "token_exchange_invalid",
        "upstream_unavailable",
        "internal_error",
    ]
    http_status: Literal[401, 403, 500, 502]
    subject_token: str = Field(min_length=1, repr=False)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    inbound_trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
