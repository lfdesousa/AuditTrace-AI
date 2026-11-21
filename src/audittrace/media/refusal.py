"""Classify a chat request this service refuses before any upstream call (pure)."""

from __future__ import annotations

from typing import Any

from audittrace.media.input_media_record import InputMediaRecord
from audittrace.media.link_guard import REASON_MEDIA_LINK, find_media_link
from audittrace.media.request_refusal import REFUSAL_MESSAGES, RequestRefusal

__all__ = ["REFUSAL_MESSAGES", "classify_refusal"]


def classify_refusal(
    messages: list[Any], record: InputMediaRecord | None
) -> RequestRefusal | None:
    """The first refusal of the request, or ``None``. Reads only; writes nothing.

    An image refusal comes first (the first refused image part), else the first
    media link.
    """
    if record is not None and record.refused:
        item = record.refused[0]
        return RequestRefusal(str(item.reason), item.message_index, item.part_index)
    link = find_media_link(messages)
    if link is not None:
        return RequestRefusal(REASON_MEDIA_LINK, link[0], link[1])
    return None
