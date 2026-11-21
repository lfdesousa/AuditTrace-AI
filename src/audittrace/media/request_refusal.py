"""A chat request refused before any upstream call: a value, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from audittrace.media.digest import REASON_DATA, REASON_URL
from audittrace.media.link_guard import REASON_MEDIA_LINK

#: reason -> the explicit message; its keys are the closed set of refusal reasons
REFUSAL_MESSAGES: dict[str, str] = {
    REASON_URL: (
        "An image in this message is a link. This service only accepts images sent "
        "inline as a base64 data URL (data:image/...;base64,...); it does not fetch "
        "images from a URL. Attach the image file itself."
    ),
    REASON_DATA: (
        "An image in this message is not a valid base64 data URL "
        "(data:image/<type>;base64,<data>). Attach the image again."
    ),
    REASON_MEDIA_LINK: (
        "A media part in this message is a link. This service only accepts media sent "
        "inline; it does not fetch media from a URL. Attach the file itself."
    ),
}


@dataclass(frozen=True)
class RequestRefusal:
    reason: str
    message_index: int
    part_index: int

    @property
    def detail(self) -> str:
        """The audit row's ``error_detail``: the reason and the position, nothing else."""
        return f"{self.reason}: message {self.message_index} part {self.part_index}"

    def body(self) -> dict[str, Any]:
        """The OpenAI-shaped reply; an unknown reason raises (no unpublished reason)."""
        return {
            "error": {
                "message": REFUSAL_MESSAGES[self.reason],
                "type": "invalid_request_error",
                "param": "messages",
                "code": self.reason,
                "status": 400,
            }
        }
