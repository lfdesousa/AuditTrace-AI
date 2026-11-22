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


def placeholder_message(message: Any) -> dict[str, Any]:
    """A ``messages`` element that is not an object, reduced to its kind and its size.

    The schema allows only message objects. Whatever else is in the list (a list of parts, a
    bare string, a number) is a shape the service does not know and is never traced.
    """
    kind = type(message).__name__
    size = (
        f" chars={len(message)}"
        if isinstance(message, str)
        else f" items={len(message)}"
        if isinstance(message, (list, tuple))
        else ""
    )
    return {"redacted": f"[REDACTED:media message {kind}{size}]"}


#: The keys of a message object in the OpenAI chat-completions request schema (messages of
#: role developer/system/user/assistant/tool/function): ``role``, ``content``, ``name``,
#: ``tool_calls``, ``tool_call_id``, ``refusal``, ``function_call``, ``audio``. ``content`` is
#: redacted by ``redact_content``; ``tool_calls`` and ``function_call`` hold the tool name and
#: its JSON ``arguments`` (text, traced as written like any text); ``audio`` is a reference
#: ({"id": ...}), not a payload. Any other key is fail-closed: its value is never traced.
_MESSAGE_KEYS = frozenset(
    {
        "role",
        "content",
        "name",
        "tool_calls",
        "tool_call_id",
        "refusal",
        "function_call",
        "audio",
    }
)

#: The fields of a chat-completions request body in the OpenAI schema, plus the two the
#: service itself reads (``context_query``, ``project``). A request field not listed here
#: is replaced by a marker in telemetry; ``messages`` is redacted message by message.
_REQUEST_KEYS = frozenset(
    {
        "messages", "model", "audio", "frequency_penalty", "function_call", "functions",
        "logit_bias", "logprobs", "max_completion_tokens", "max_tokens", "metadata",
        "modalities", "n", "parallel_tool_calls", "prediction", "presence_penalty",
        "prompt_cache_key", "reasoning_effort", "response_format", "safety_identifier",
        "seed", "service_tier", "stop", "store", "stream", "stream_options",
        "temperature", "tool_choice", "tools", "top_logprobs", "top_p", "user",
        "verbosity", "web_search_options", "context_query", "project",
    }
)  # fmt: skip


def _size(value: Any) -> str:
    if isinstance(value, str):
        return f" chars={len(value)}"
    if isinstance(value, (list, tuple)):
        return f" items={len(value)}"
    if isinstance(value, dict):
        return f" keys={len(value)}"
    return ""


def _redact_unknown_keys(
    mapping: dict[str, Any], allowed: frozenset[str], label: str
) -> dict[str, Any]:
    """A copy of ``mapping`` in which every key outside ``allowed`` holds only a marker.

    The key name is kept when it passes the type-name bound (at most 32 plain identifier
    characters, so at most 24 bytes can ride in it: the accepted bound), else it becomes
    ``unknown`` (``unknown_2``, ... when several collide). Nothing is walked: any depth.
    """
    out: dict[str, Any] = {}
    unknown = 0
    for key, value in mapping.items():
        if key in allowed:
            out[key] = value
            continue
        name = _type_name(key)
        if name == "unknown":
            unknown += 1
            name = "unknown" if unknown == 1 else f"unknown_{unknown}"
        while name in out:
            name = f"{name}_"
        out[name] = f"[REDACTED:media {label} key {type(value).__name__}{_size(value)}]"
    return out


def redact_message_keys(message: dict[str, Any]) -> dict[str, Any]:
    """A message object with every key outside the OpenAI message keys replaced by a marker."""
    return _redact_unknown_keys(message, _MESSAGE_KEYS, "message")


def redact_request_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """A chat request body with every field outside the OpenAI schema replaced by a marker."""
    safe = _redact_unknown_keys(payload, _REQUEST_KEYS, "request field")
    if isinstance(safe.get("messages"), list):
        safe["messages"] = redact_message_list(safe["messages"])
    return safe


def redact_message_list(messages: list[Any]) -> list[Any]:
    """The elements of a ``messages`` list made safe: non-objects replaced, object keys filtered.

    For the renderer, which redacts each message object's ``content`` itself when it reaches it.
    """
    return [
        redact_message_keys(message)
        if isinstance(message, dict)
        else placeholder_message(message)
        for message in messages
    ]


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
            if not isinstance(message, dict):
                out.append(placeholder_message(message))
            else:
                safe = redact_message_keys(message)
                if "content" in message:
                    safe["content"] = redact_content(content)
                out.append(safe)
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
        out.append({**redact_message_keys(message), "content": parts})
    return out
