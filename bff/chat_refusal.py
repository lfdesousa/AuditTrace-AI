"""One refusal the BFF itself gives on the chat route (BFF-CHAT-ROUTE-ERRORS)."""

from __future__ import annotations

import dataclasses
from typing import Any

from fastapi.responses import JSONResponse


@dataclasses.dataclass(frozen=True, slots=True)
class ChatRefusal:
    """A fixed answer: nothing in it is taken from a request or an upstream.

    The body is OpenAI's error object (``message``, ``type``, ``param``,
    ``code``) plus the additive ``status`` key, the shape of
    ``audittrace.routes.chat._openai_error_body``. ``challenge`` is the RFC 6750
    section 3 ``WWW-Authenticate`` value, set on a 401 only.
    """

    status: int
    code: str
    error_type: str
    message: str
    challenge: str | None = None

    def body(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "param": None,
                "code": self.code,
                "status": self.status,
            }
        }

    def response(self) -> JSONResponse:
        headers = {"WWW-Authenticate": self.challenge} if self.challenge else None
        response = JSONResponse(
            status_code=self.status, content=self.body(), headers=headers
        )
        response.refusal_code = self.code  # type: ignore[attr-defined]
        return response
