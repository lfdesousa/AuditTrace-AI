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


#: Value schemas of the allow-lists below. ``_TEXT``: a scalar (string, number, boolean,
#: null), traced as written like any text; an object or an array there is a marker. ``_ANY``:
#: text-like definitions (tool and function definitions, ``metadata``, ``response_format``...)
#: traced as written (a stated limit of the contract note). ``_CONTENT``: left to
#: ``redact_content`` (a message's ``content``, a prediction's ``content``). A ``dict`` is an
#: object with exactly those keys (any other key is a marker); ``(_LIST, s)`` an array of
#: ``s``; ``(_MAP, s)`` an object with any keys (names bounded) whose values are ``s``;
#: ``(_ONE, a, b)`` a value that fits ``a`` or ``b`` (the first that fits is applied).
_TEXT = "text"
_ANY = "any"
_CONTENT = "content"
_LIST = "list"
_MAP = "map"
_ONE = "one"

#: The tool name and its JSON ``arguments`` (a string): text, traced as written.
_FUNCTION: dict[str, Any] = {"name": _TEXT, "arguments": _TEXT}

#: A message object in the OpenAI chat-completions request schema (developer, system, user,
#: assistant, tool and function messages) and the replayed assistant message of a client:
#: ``role``, ``content``, ``name``, ``tool_call_id``, ``refusal``, ``reasoning_content``
#: (the text some servers return and clients replay), ``audio`` {id} (the response audio
#: holds ``data``, ``transcript``, ``expires_at``: only the reference id is traced),
#: ``tool_calls`` [{id, type, function {name, arguments}}] and the deprecated
#: ``function_call`` {name, arguments}. Anything else, at any level, is a marker.
_MESSAGE_SCHEMA: dict[str, Any] = {
    "role": _TEXT,
    "content": _CONTENT,
    "name": _TEXT,
    "tool_call_id": _TEXT,
    "refusal": _TEXT,
    "reasoning_content": _TEXT,
    "audio": {"id": _TEXT},
    "tool_calls": (
        _LIST,
        {"id": _TEXT, "type": _TEXT, "index": _TEXT, "function": _FUNCTION},
    ),
    "function_call": _FUNCTION,
}

#: A chat-completions request body: the OpenAI fields plus what the service itself reads or
#: writes (``context_query``, ``project``, ``chat_template_kwargs`` for X-Thinking, whose
#: values must be scalars). A field outside the list is a marker where the request is traced.
_REQUEST_SCHEMA: dict[str, Any] = {
    **dict.fromkeys(
        (
            "model",
            "frequency_penalty",
            "logprobs",
            "max_completion_tokens",
            "max_tokens",
            "n",
            "parallel_tool_calls",
            "presence_penalty",
            "prompt_cache_key",
            "reasoning_effort",
            "safety_identifier",
            "seed",
            "service_tier",
            "store",
            "stream",
            "temperature",
            "top_logprobs",
            "top_p",
            "user",
            "verbosity",
            "context_query",
            "project",
        ),
        _TEXT,
    ),  # fmt: skip
    # definitions whose body is free-form JSON (a stated limit: text traced as written)
    **dict.fromkeys(("functions", "response_format", "tools"), _ANY),
    "function_call": (_ONE, _TEXT, {"name": _TEXT}),
    "tool_choice": (_ONE, _TEXT, {"type": _TEXT, "function": {"name": _TEXT}}),
    "stop": (_ONE, _TEXT, (_LIST, _TEXT)),
    "modalities": (_LIST, _TEXT),
    "logit_bias": (_MAP, _TEXT),
    "metadata": (_MAP, _TEXT),
    "stream_options": {"include_usage": _TEXT, "include_obfuscation": _TEXT},
    "web_search_options": {
        "search_context_size": _TEXT,
        "user_location": {
            "type": _TEXT,
            "approximate": dict.fromkeys(
                ("city", "country", "region", "timezone"), _TEXT
            ),
        },
    },
    "messages": _ANY,  # redacted message by message by ``redact_message_list``
    "audio": {"voice": _TEXT, "format": _TEXT},
    "prediction": {"type": _TEXT, "content": _CONTENT},
    "chat_template_kwargs": (_MAP, _TEXT),
}

#: The object a media content part holds, in the OpenAI part schemas: ``input_audio``
#: {data, format}, ``file`` {file_data, file_id, filename}, ``image_url`` {url, detail};
#: ``input_video`` is not an OpenAI part, the model server reads its source from ``data`` else
#: ``url`` (``_PAYLOAD_SLOTS``) plus a ``format``; ``input_audio`` also accepts ``url`` there.
#: A value that is not a scalar, or any other key, is a marker (payload slots as before).
_HOLDER_SCHEMA: dict[str, dict[str, Any]] = {
    "input_audio": dict.fromkeys(("data", "format", "url"), _TEXT),
    "input_video": dict.fromkeys(("data", "url", "format"), _TEXT),
    "file": dict.fromkeys(("file_data", "file_id", "filename"), _TEXT),
    "image_url": dict.fromkeys(("url", "detail"), _TEXT),
}


def _size(value: Any) -> str:
    if isinstance(value, str):
        return f" chars={len(value)}"
    if isinstance(value, (list, tuple)):
        return f" items={len(value)}"
    if isinstance(value, dict):
        return f" keys={len(value)}"
    return ""


def _marker(label: str, what: str, value: Any) -> str:
    return f"[REDACTED:media {label} {what} {type(value).__name__}{_size(value)}]"


def _apply(value: Any, schema: Any, label: str) -> Any:
    """``value`` made safe by ``schema`` (see ``_TEXT``): nothing outside it is traced.

    The schemas are finite, so the recursion is as deep as the schema and no deeper: whatever
    a value holds below the last level a schema names is a marker, whatever its depth.
    """
    if schema == _ANY or schema == _CONTENT:
        return value
    if schema == _TEXT:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return _marker(label, "value", value)
    if isinstance(schema, tuple) and schema[0] == _ONE:
        for alternative in schema[1:]:
            if _fits(value, alternative):
                return _apply(value, alternative, label)
        return _marker(label, "value", value)
    if isinstance(schema, tuple) and schema[0] == _LIST:
        if not isinstance(value, list):
            return _marker(label, "value", value)
        return [_apply(item, schema[1], label) for item in value]
    if isinstance(schema, tuple) and schema[0] == _MAP:
        if not isinstance(value, dict):
            return _marker(label, "value", value)
        return _apply_object(value, dict.fromkeys(_names(value), schema[1]), label)
    if not isinstance(value, dict):
        return _marker(label, "value", value)
    return _apply_object(value, schema, label)


def _fits(value: Any, schema: Any) -> bool:
    """Whether ``value`` has the outer shape ``schema`` names (a scalar, an array, an object)."""
    if schema == _TEXT:
        return value is None or isinstance(value, (str, int, float, bool))
    if isinstance(schema, tuple):
        return isinstance(value, list if schema[0] == _LIST else dict)
    return isinstance(value, dict)


def _names(mapping: dict[str, Any]) -> list[str]:
    """The keys of a mapping whose names pass the bound (those that do not are not allowed)."""
    return [key for key in mapping if _type_name(key) == key]


def _apply_object(
    mapping: dict[str, Any], schema: dict[str, Any], label: str
) -> dict[str, Any]:
    """A copy of ``mapping`` keeping the keys of ``schema`` (their values by ``_apply``).

    Every other key holds only a marker. Its name is kept when it passes the type-name bound
    (at most 32 plain identifier characters, so at most 24 bytes can ride in it: the
    accepted bound), else it becomes ``unknown`` (``unknown_2``, ... when several collide).
    """
    out: dict[str, Any] = {}
    unknown = 0
    for key, value in mapping.items():
        if key in schema:
            out[key] = _apply(
                value, schema[key], key if _type_name(key) == key else label
            )
            continue
        name = _type_name(key)
        if name == "unknown":
            unknown += 1
            name = "unknown" if unknown == 1 else f"unknown_{unknown}"
        while name in out:
            name = f"{name}_"
        out[name] = _marker(label, "key", value)
    return out


def redact_message_keys(message: dict[str, Any]) -> dict[str, Any]:
    """A message object reduced to the OpenAI message schema (``content`` left to its own rule)."""
    return _apply_object(message, _MESSAGE_SCHEMA, "message")


def redact_request_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """A chat request body reduced to the OpenAI request schema; messages one by one."""
    safe = _apply_object(payload, _REQUEST_SCHEMA, "request field")
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
        safe = _apply_object(holder, _HOLDER_SCHEMA[kind], kind)
        for slot in _PAYLOAD_SLOTS[kind]:
            if isinstance(safe.get(slot), str):
                safe[slot] = f"[REDACTED:media {kind} chars={len(safe[slot])}]"
        return redact_media_links({**part, kind: safe})
    if _is_known_text_part(part):
        if kind == "image_url" and isinstance(part.get("image_url"), dict):
            part = {
                **part,
                "image_url": _apply_object(
                    part["image_url"], _HOLDER_SCHEMA["image_url"], "image_url"
                ),
            }
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
