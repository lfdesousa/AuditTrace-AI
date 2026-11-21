"""Find a media part whose source is a link the model server would fetch or open."""

from __future__ import annotations

import re
from typing import Any

REASON_MEDIA_LINK = "media_link_not_supported"
#: llama-server fetches exactly ``http://`` / ``https://`` and opens ``file://``
_LINK = re.compile(r"\s*(?:https?|file)://", re.IGNORECASE)
#: part types the link rule does not look at (an image has its own rule)
_EXEMPT_TYPES = ("text", "image_url")


def _has_link(value: Any) -> bool:
    if isinstance(value, str):
        return _LINK.match(value) is not None
    if isinstance(value, dict):
        return any(_has_link(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_link(v) for v in value)
    return False


def find_media_link(messages: list[Any]) -> tuple[int, int] | None:
    """``(message_index, part_index)`` of the first non-text, non-image part holding a link."""
    for mi, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for pi, part in enumerate(content):
            if not isinstance(part, dict) or part.get("type") in _EXEMPT_TYPES:
                continue
            if _has_link(part):
                return mi, pi
    return None


def _without_links(value: Any) -> Any:
    if isinstance(value, str):
        return f"[REDACTED:link chars={len(value)}]" if _LINK.match(value) else value
    if isinstance(value, dict):
        return {k: _without_links(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_without_links(v) for v in value]
    return value


def redact_media_links(part: dict[str, Any]) -> dict[str, Any]:
    """A copy of the part in which every string the guard refuses on is replaced.

    The same predicate and the same part types as :func:`find_media_link`: what
    is refused as a link is never written to telemetry.
    """
    if part.get("type") in _EXEMPT_TYPES:
        return part
    replaced: dict[str, Any] = _without_links(part)
    return replaced
