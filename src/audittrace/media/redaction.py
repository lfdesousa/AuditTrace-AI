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

    The name is attacker-controlled. It is kept only when it is at most 32 characters of
    letters, digits, ``_``, ``-``, ``.``: that can still carry up to 24 bytes of base64, and
    that bound is the accepted one (it keeps the marker readable; a longer or odder name is
    replaced, so the channel is never wider than 32 characters).
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


def _placeholder_value(value: Any) -> dict[str, Any]:
    """A list element that is not a part object, reduced to its kind and its size.

    The OpenAI schema allows only objects in a ``content`` list, so a bare string, a number,
    a nested list or a null is a shape the service does not know: it is treated as an unknown
    part and never traced.
    """
    kind = type(value).__name__
    size = (
        f" chars={len(value)}"
        if isinstance(value, str)
        else f" items={len(value)}"
        if isinstance(value, (list, tuple))
        else ""
    )
    return {"type": "unknown", "redacted": f"[REDACTED:media unknown {kind}{size}]"}


def redact_content(content: Any) -> Any:
    """A message's ``content`` made safe for telemetry (the input is never modified).

    ``str`` is text, traced as written by design; ``None`` carries nothing (OpenAI allows a
    null content on an assistant message that has ``tool_calls``); a list is redacted part by
    part (an element that is not an object is an unknown part); any other container or scalar
    (a dict, a number) is not a shape the schema allows and is replaced whole by a marker that
    records its type and its size.
    """
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, list):
        return redact_content_parts(content)
    kind = type(content).__name__
    size = f" keys={len(content)}" if isinstance(content, dict) else ""
    return f"[REDACTED:media content {kind}{size}]"


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


def redact_content_parts(parts: list[Any]) -> list[Any]:
    """The parts of one message, each redacted as ``_redact_part`` says (non-dicts as they are).

    For the renderer every span attribute and log line passes: a message's ``content`` list
    never reaches telemetry with a media payload or a part the service cannot name (a
    non-object element included).
    """
    return [
        _redact_part(part) if isinstance(part, dict) else _placeholder_value(part)
        for part in parts
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
            out.append(
                message
                if not isinstance(message, dict)
                else {**message, "content": redact_content(content)}
            )
            continue
        parts = [
            {
                "type": "image_url",
                "image_url": {"url": by_position[(mi, pi)].placeholder()},
            }
            if (mi, pi) in by_position
            else (
                _redact_part(part)
                if isinstance(part, dict)
                else _placeholder_value(part)
            )
            for pi, part in enumerate(content)
        ]
        out.append({**message, "content": parts})
    return out
