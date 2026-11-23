"""One image content part of a chat request, as the audit row records it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

STATUS_RECORDED = "recorded"
STATUS_REFUSED = "refused"


@dataclass(frozen=True)
class ImagePartRecord:
    """Position, declared type, decoded size and sha256 of one ``image_url`` part.

    A refused part carries the reason and the length and sha256 of the URL string
    as sent (an identifier, never the URL itself: it can hold a signature).
    """

    message_index: int
    part_index: int
    role: str
    source: str  # data_uri | url | missing
    status: str  # recorded | refused
    media_type: str | None = None
    size: int | None = None
    sha256: str | None = None
    reason: str | None = None
    url_chars: int | None = None
    url_sha256: str | None = None

    def as_dict(self) -> dict[str, Any]:
        head: dict[str, Any] = {
            "kind": "image",
            "message_index": self.message_index,
            "part_index": self.part_index,
            "role": self.role,
            "source": self.source,
            "status": self.status,
        }
        if self.status == STATUS_RECORDED:
            return {
                **head,
                "media_type": self.media_type,
                "bytes": self.size,
                "sha256": self.sha256,
            }
        return {
            **head,
            "reason": self.reason,
            "url_chars": self.url_chars,
            "url_sha256": self.url_sha256,
        }

    def placeholder(self) -> str:
        """What a trace shows instead of the image: its digest, never its bytes."""
        if self.status == STATUS_RECORDED:
            return (
                f"[image sha256={self.sha256} bytes={self.size} type={self.media_type}]"
            )
        return f"[image refused: {self.reason} url_sha256={self.url_sha256}]"
