"""Explicit client error for an upstream "unsupported content part" rejection.

llama-server answers HTTP 400 with the body
``{"error": {"code": 400, "message": "unsupported content[].type", ...}}``
when a message carries a content part whose ``type`` it does not accept
(for example the OpenAI ``file`` part a "Upload to Provider" attach sends).
Today that surfaces as ``llama-server returned status 400``. This module
recognises that ONE rejection, by identity, and builds a clear OpenAI-shaped
error that names the supported alternative. The request itself is never
modified (ADR-024); only the error text the client sees changes.

Falsifiability of each guard (see ``tests/test_chat_unsupported_content_error*.py``
and ``tests/test_upstream_errors.py``):

* The match is EQUALITY on the message and the status. ``in`` / ``startswith``
  / ``strip()`` / case-folding each redden a near-miss case.
* ``code`` must be the int 400: coercing it (``int(code)`` / ``str(code)``)
  reddens the ``"400"`` and ``400.0`` cases; the status check is separate, so
  dropping it reddens status 500 with a body code of 400.
* Only ``type`` values matching ``[A-Za-z0-9_.-]{1,32}`` are ever named;
  no content, filename or data is read, logged or echoed.
* ``classify_unsupported_content`` owns the single WARNING; the body matcher
  is silent (the tools non-stream arm already has the loop's own WARNING).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: The exact message llama-server emits (``server-common.cpp``); matched by equality.
LLAMA_UNSUPPORTED_CONTENT_TYPE_MESSAGE = "unsupported content[].type"
#: Stable, machine-readable ``error.code`` returned to clients.
UNSUPPORTED_CONTENT_CODE = "unsupported_content_type"

_UPSTREAM_STATUS = 400
_MAX_NAMED_TYPES = 3
_MAX_BODY_CHARS = 400
_PART_TYPE_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}")

_MESSAGE_PREFIX = "This model can't receive files or images directly in the message"
_MESSAGE_SUFFIX = "Use 'Upload as Text' instead."


@dataclass(frozen=True)
class UnsupportedContentHit:
    """A recognised rejection: the client body and the audit-row detail."""

    error_body: dict[str, Any]
    error_detail: str


def match_unsupported_content_body(body: object) -> bool:
    """True when a PARSED upstream body is exactly llama-server's rejection.

    Silent (no logging). ``code`` must be the int 400 (llama-server copies the
    HTTP status into the body); ``message`` is compared by equality.
    """
    if not isinstance(body, dict):
        return False
    err = body.get("error")
    if not isinstance(err, dict):
        return False
    code = err.get("code")
    return (
        isinstance(code, int)
        and code == _UPSTREAM_STATUS
        and err.get("message") == LLAMA_UNSUPPORTED_CONTENT_TYPE_MESSAGE
    )


def match_unsupported_content(status_code: int, body_text: str) -> bool:
    """True when the HTTP status is 400 and the raw body text matches (silent)."""
    if status_code != _UPSTREAM_STATUS:
        return False
    try:
        parsed = json.loads(body_text)
    except ValueError:
        return False
    return match_unsupported_content_body(parsed)


def unsupported_part_types(payload: dict[str, Any]) -> list[str]:
    """Return up to three distinct non-``text`` part types named in the request.

    Only the ``type`` key of list-valued ``messages[].content`` parts is read,
    and only values that are ``str`` and ``fullmatch`` the safe pattern are kept
    (``re.fullmatch``: a trailing newline must not slip through).
    """
    found: list[str] = []
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return found
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            part_type = part.get("type") if isinstance(part, dict) else None
            if (
                isinstance(part_type, str)
                and part_type != "text"
                and _PART_TYPE_RE.fullmatch(part_type)
                and part_type not in found
            ):
                found.append(part_type)
                if len(found) == _MAX_NAMED_TYPES:
                    return found
    return found


def unsupported_content_error_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the explicit OpenAI-shaped error body through ``_openai_error_body``."""
    # Function-local import: chat.py imports this module at import time and
    # defines _openai_error_body, so a module-level import here is a cycle.
    from audittrace.routes.chat import _openai_error_body

    types = unsupported_part_types(payload)
    named = ", ".join(f"'{t}'" for t in types)
    detail = f"unsupported content part type {named}".rstrip()
    return _openai_error_body(
        message=f"{_MESSAGE_PREFIX} ({detail}). {_MESSAGE_SUFFIX}",
        code=UNSUPPORTED_CONTENT_CODE,
        type_="invalid_request_error",
        param="messages",
        status=_UPSTREAM_STATUS,
    )


def error_detail_from_text(status_code: int, body_text: str) -> str:
    """Audit-row detail keeping the upstream text (same form as the 5xx arm)."""
    return f"status={status_code}: {body_text[:_MAX_BODY_CHARS]}"


def error_detail_from_body(body: dict[str, Any]) -> str:
    """Audit-row detail from a parsed upstream body (compact JSON, bounded)."""
    compact = json.dumps(body, separators=(",", ":"))
    return f"status={_UPSTREAM_STATUS}: {compact[:_MAX_BODY_CHARS]}"


def classify_unsupported_content(
    status_code: int, body_text: str, payload: dict[str, Any]
) -> UnsupportedContentHit | None:
    """Match the raw upstream reply; on a hit log ONE WARNING and return the result."""
    if not match_unsupported_content(status_code, body_text):
        return None
    logger.warning(
        "llama-server rejected a content part: HTTP %d, upstream body: %s",
        status_code,
        body_text[:_MAX_BODY_CHARS],
    )
    return UnsupportedContentHit(
        error_body=unsupported_content_error_body(payload),
        error_detail=error_detail_from_text(status_code, body_text),
    )


def classify_status_error(
    exc: httpx.HTTPStatusError, payload: dict[str, Any]
) -> UnsupportedContentHit | None:
    """``classify_unsupported_content`` for a raised ``HTTPStatusError``.

    A response whose body was never read has no ``.text`` (``ResponseNotRead``):
    that is "no match", never a new failure inside an error handler.
    """
    with contextlib.suppress(httpx.ResponseNotRead):
        return classify_unsupported_content(
            exc.response.status_code, exc.response.text, payload
        )
    return None


def detail_from_hit_or(hit: UnsupportedContentHit | None, fallback: str) -> str:
    """The audit ``error_detail``: the hit's, else the caller's existing one."""
    return hit.error_detail if hit else fallback


def frame_or(hit: UnsupportedContentHit | None, default_frame: bytes) -> bytes:
    """The hit's SSE error frame, else the caller's existing (generic) frame."""
    return emit_sse_error_frame_from_body(hit.error_body) if hit else default_frame


def emit_sse_error_frame_from_body(error_body: dict[str, Any]) -> bytes:
    """SSE frame + ``[DONE]`` for an already-built error body (same bytes as chat.py's)."""
    return ("data: " + json.dumps(error_body) + "\n\n" + "data: [DONE]\n\n").encode()
