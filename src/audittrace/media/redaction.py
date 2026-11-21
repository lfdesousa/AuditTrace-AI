"""A copy of the messages that is safe for telemetry: media payloads replaced."""

from __future__ import annotations

from typing import Any

from audittrace.media.input_media_record import InputMediaRecord
from audittrace.media.link_guard import redact_media_links

#: part type -> the slots of that part that hold a payload (the model server reads an
#: audio / video source from ``data``, else from ``url``)
_PAYLOAD_SLOTS = {
    "input_audio": ("data", "url"),
    "input_video": ("data", "url"),
    "file": ("file_data",),
}


def _redact_part(part: dict[str, Any]) -> dict[str, Any]:
    kind = part.get("type")
    holder = part.get(kind) if isinstance(kind, str) else None
    if isinstance(kind, str) and isinstance(holder, dict) and kind in _PAYLOAD_SLOTS:
        safe = dict(holder)
        for slot in _PAYLOAD_SLOTS[kind]:
            if isinstance(safe.get(slot), str):
                safe[slot] = f"[REDACTED:media {kind} chars={len(safe[slot])}]"
        part = {**part, kind: safe}
    return redact_media_links(part)


def redact_media_payloads(
    messages: list[Any], record: InputMediaRecord | None
) -> list[Any]:
    """A copy for telemetry. Slots first, then links; images become their digest.

    The request itself is never modified (ADR-024): the model server receives the
    original messages byte for byte.
    """
    by_position = (
        {(i.message_index, i.part_index): i for i in record.items} if record else {}
    )
    out: list[Any] = []
    for mi, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            out.append(message)
            continue
        parts = [
            {
                "type": "image_url",
                "image_url": {"url": by_position[(mi, pi)].placeholder()},
            }
            if (mi, pi) in by_position
            else (_redact_part(part) if isinstance(part, dict) else part)
            for pi, part in enumerate(content)
        ]
        out.append({**message, "content": parts})
    return out
