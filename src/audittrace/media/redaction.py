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


#: the content-part types whose content is text the caller wrote, with the one key that
#: holds it (OpenAI chat: ``text`` and the assistant's ``refusal``; ``image_url`` is
#: digested or refused before telemetry, and what is left of it is link-guarded here).
#: Together with ``_PAYLOAD_SLOTS`` these are ALL the part types the service knows; a part
#: of any other type is fail-closed, never trusted to be text (#474 F-d).
_TEXT_SLOTS = {"text": "text", "refusal": "refusal", "image_url": "image_url"}
_SLOT_TYPES = {"text": str, "refusal": str, "image_url": dict}
_TYPE_NAME_MAX = 32


def _type_name(kind: Any) -> str:
    """The part's type when it is a short plain identifier, else ``unknown``.

    The name is attacker-controlled; keeping it only when it cannot carry a payload
    (at most 32 characters of letters, digits, ``_``, ``-``, ``.``) keeps the placeholder
    readable without being a channel.
    """
    if (
        isinstance(kind, str)
        and 0 < len(kind) <= _TYPE_NAME_MAX
        and kind.isascii()
        and kind.replace("_", "").replace("-", "").replace(".", "").isalnum()
    ):
        return kind
    return "unknown"


def _placeholder(part: dict[str, Any]) -> dict[str, Any]:
    """A part reduced to its type name and its number of keys (no walk: any depth)."""
    name = _type_name(part.get("type"))
    return {"type": name, "redacted": f"[REDACTED:media {name} keys={len(part)}]"}


def _is_known_media_part(part: dict[str, Any]) -> bool:
    kind = part.get("type")
    return isinstance(kind, str) and kind in _PAYLOAD_SLOTS


def _is_known_text_part(part: dict[str, Any]) -> bool:
    kind = part.get("type")
    if not isinstance(kind, str) or kind not in _TEXT_SLOTS:
        return False
    slot = _TEXT_SLOTS[kind]
    return set(part) <= {"type", slot} and (
        slot not in part or isinstance(part[slot], _SLOT_TYPES[kind])
    )


def _redact_part(part: dict[str, Any]) -> dict[str, Any]:
    """Slots of a known media part; text-bearing parts as they are; the rest whole."""
    kind = part.get("type")
    if isinstance(kind, str) and kind in _PAYLOAD_SLOTS:
        holder = part.get(kind)
        if not (isinstance(holder, dict) and set(part) <= {"type", kind}):
            return _placeholder(part)
        safe = dict(holder)
        for slot in _PAYLOAD_SLOTS[kind]:
            if isinstance(safe.get(slot), str):
                safe[slot] = f"[REDACTED:media {kind} chars={len(safe[slot])}]"
        return redact_media_links({**part, kind: safe})
    if _is_known_text_part(part):
        return redact_media_links(part)
    return _placeholder(part)


def redact_unknown_content_parts(parts: list[Any]) -> list[Any]:
    """The parts with every part of an UNKNOWN type (or a text part with extras) replaced.

    The narrow variant for spans and logs that render the messages themselves: a known
    media part (audio, video, file) is left as it is there, the residual R-1 of the
    contract note; only what the service cannot name is removed.
    """
    return [
        part
        if not isinstance(part, dict)
        or _is_known_media_part(part)
        or _is_known_text_part(part)
        else _placeholder(part)
        for part in parts
    ]


def redact_unknown_message_parts(messages: list[Any]) -> list[Any]:
    """A copy of ``messages`` for a span attribute, with unknown content parts replaced."""
    return [
        {**message, "content": redact_unknown_content_parts(message["content"])}
        if isinstance(message, dict) and isinstance(message.get("content"), list)
        else message
        for message in messages
    ]


def redact_media_payloads(
    messages: list[Any], record: InputMediaRecord | None
) -> list[Any]:
    """A copy for telemetry. Slots first, then links; images become their digest.

    A part of a type the service does not know is replaced whole (``_placeholder``).

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
