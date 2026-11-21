"""Digest the image parts of a chat request (pure; CPU-bound: call it off the event loop)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from functools import partial
from typing import Any

from audittrace.media.image_part_record import (
    STATUS_RECORDED,
    STATUS_REFUSED,
    ImagePartRecord,
)
from audittrace.media.input_media_record import InputMediaRecord

REASON_URL = "image_url_not_supported"
REASON_DATA = "image_data_invalid"

#: the recorded role comes from a closed set; anything else is ``other``
_ROLES = frozenset({"system", "developer", "user", "assistant", "tool", "function"})
#: ``data:image/<subtype>(;<param>)*;base64`` (lower-case ``data:``, header only)
_HEADER = re.compile(r"data:(image/[A-Za-z0-9.+-]+)(?:;[^;,]+)*;base64")


def _decode(payload: str) -> bytes:
    """Strict standard-alphabet base64 (padding optional); ``b""`` when not exact.

    The model server's own decoder stops at the first character outside the
    alphabet; a lenient decode here would hash different bytes than the model
    reads, so anything that is not exactly base64 is refused instead.
    """
    try:
        return base64.b64decode(payload + "=" * (-len(payload) % 4), validate=True)
    except (binascii.Error, ValueError):
        return b""


def _one(
    message_index: int, part_index: int, role: str, part: dict[str, Any]
) -> ImagePartRecord:
    holder = part.get("image_url")
    url = holder.get("url") if isinstance(holder, dict) else None
    at = partial(
        ImagePartRecord, message_index=message_index, part_index=part_index, role=role
    )
    if not isinstance(url, str) or not url:
        empty = hashlib.sha256(b"").hexdigest()
        return at(
            source="missing",
            status=STATUS_REFUSED,
            reason=REASON_DATA,
            url_chars=0,
            url_sha256=empty,
        )
    refused = partial(
        at,
        status=STATUS_REFUSED,
        url_chars=len(url),
        url_sha256=hashlib.sha256(url.encode()).hexdigest(),
    )
    if not url.startswith("data:"):
        return refused(source="url", reason=REASON_URL)
    header, comma, payload = url.partition(",")
    match = _HEADER.fullmatch(header)
    data = _decode(payload) if match and comma else b""
    if match is None or not data:
        return refused(source="data_uri", reason=REASON_DATA)
    return at(
        source="data_uri",
        status=STATUS_RECORDED,
        media_type=match.group(1),
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def digest_input_media(messages: list[Any]) -> InputMediaRecord | None:
    """Every ``image_url`` part of every message, in document order; ``None`` if none."""
    items: list[ImagePartRecord] = []
    for mi, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        role = message.get("role")
        recorded_role = role if isinstance(role, str) and role in _ROLES else "other"
        for pi, part in enumerate(content):
            if isinstance(part, dict) and part.get("type") == "image_url":
                items.append(_one(mi, pi, recorded_role, part))
    return InputMediaRecord(tuple(items)) if items else None
