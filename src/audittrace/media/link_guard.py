"""Find a media part whose source is a link the model server would fetch or open."""

from __future__ import annotations

import re
from typing import Any

from audittrace.media.json_walk import any_string, map_strings

REASON_MEDIA_LINK = "media_link_not_supported"
#: llama-server fetches exactly ``http://`` / ``https://`` and opens ``file://``
_LINK = re.compile(r"\s*(?:https?|file)://", re.IGNORECASE)
#: part types the link rule does not look at (an image has its own rule)
_EXEMPT_TYPES = ("text", "image_url")


def _is_link(value: str) -> bool:
    return _LINK.match(value) is not None


def find_media_link(messages: list[Any]) -> tuple[int, int] | None:
    """``(message_index, part_index)`` of the first non-text, non-image part holding a link."""
    for mi, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for pi, part in enumerate(content):
            if not isinstance(part, dict) or part.get("type") in _EXEMPT_TYPES:
                continue
            if any_string(part, _is_link):
                return mi, pi
    return None


def _hide_link(value: str) -> str:
    return f"[REDACTED:link chars={len(value)}]" if _is_link(value) else value


def redact_media_links(part: dict[str, Any]) -> dict[str, Any]:
    """A copy of the part in which every string the guard refuses on is replaced.

    The same predicate and the same part types as :func:`find_media_link`: what
    is refused as a link is never written to telemetry.
    """
    if part.get("type") in _EXEMPT_TYPES:
        return part
    replaced: dict[str, Any] = map_strings(part, _hide_link)
    return replaced
