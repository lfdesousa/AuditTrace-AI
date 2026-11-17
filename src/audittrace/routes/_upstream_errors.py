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

A SECOND rejection is recognised the same way (MODEL-MMPROJ-PERMANENT): a
llama-server started WITHOUT a vision projector answers an ``image_url`` part
with HTTP 500 and ``image input is not supported - hint: ...``
(``match_image_input_unsupported``). It is matched by equality on the status,
the int ``code`` 500 and the message, and answered with an explicit HTTP 400
``image_input_unsupported`` (a routed model, e.g. ``AUDITTRACE_MODEL_ROUTES``,
can reach such a server). Every other 500 keeps today's behaviour; the file
matcher is NOT widened. Falsifiability:
``tests/test_chat_image_input_unsupported_error.py`` and
``tests/test_chat_image_error_tools_nonstream_logging.py``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, NoReturn

import httpx
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

#: The exact message llama-server emits (``server-common.cpp``); matched by equality.
LLAMA_UNSUPPORTED_CONTENT_TYPE_MESSAGE = "unsupported content[].type"
#: Stable, machine-readable ``error.code`` returned to clients.
UNSUPPORTED_CONTENT_CODE = "unsupported_content_type"

#: The exact message a llama-server WITHOUT a vision projector emits for an image
#: part (HTTP 500); matched by equality.
LLAMA_IMAGE_INPUT_UNSUPPORTED_MESSAGE = (
    "image input is not supported - hint: if this is unexpected, "
    "you may need to provide the mmproj"
)
#: Stable ``error.code`` for that rejection.
IMAGE_INPUT_UNSUPPORTED_CODE = "image_input_unsupported"

_UPSTREAM_STATUS = 400
_IMAGE_UPSTREAM_STATUS = 500
_IMAGE_MESSAGE = (
    "The model server selected for this request does not accept image input. "
    "Choose a model that accepts images, or remove the image from the message."
)
_MAX_NAMED_TYPES = 3
#: Upstream statuses about the proxy's OWN credentials towards the model server or the
#: network in between (RFC 9110 15.5.2, 15.5.4, 15.5.8; RFC 6585 6): never relayed.
_HOP_CREDENTIAL_STATUSES: frozenset[int] = frozenset({401, 403, 407, 511})
#: Whether an upstream 5xx with a relayable body is relayed with its own status.
_RELAY_SERVER_ERRORS = False
#: Whether only a body carrying an ``error`` object is relayed (else any JSON object).
_RELAY_REQUIRES_ERROR_OBJECT = True
#: RFC 9110 10.2.3: delay-seconds or an IMF-fixdate; anything else is not relayed.
_RETRY_AFTER_RE = re.compile(
    r"\d{1,9}|[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT"
)
_MAX_BODY_CHARS = 400
_PART_TYPE_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}")

_MESSAGE_PREFIX = (
    "The model server does not accept one of the content part types in this message"
)
_MESSAGE_SUFFIX = "To attach a document, use 'Upload as Text' instead."


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


def match_image_input_unsupported(status_code: int, body_text: str) -> bool:
    """True when the reply is exactly the no-projector image rejection (silent)."""
    if status_code != _IMAGE_UPSTREAM_STATUS:
        return False
    try:
        parsed = json.loads(body_text)
    except ValueError:
        return False
    err = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(err, dict):
        return False
    code = err.get("code")
    return (
        type(code) is int
        and code == _IMAGE_UPSTREAM_STATUS
        and err.get("message") == LLAMA_IMAGE_INPUT_UNSUPPORTED_MESSAGE
    )


def image_input_unsupported_error_body() -> dict[str, Any]:
    """The explicit client error for the no-projector image rejection.

    Names no model, route or flag: the proxy only knows the selected server
    said it does not take image input.
    """
    # Function-local import: same chat.py cycle as unsupported_content_error_body.
    from audittrace.routes.chat import _openai_error_body

    return _openai_error_body(
        message=_IMAGE_MESSAGE,
        code=IMAGE_INPUT_UNSUPPORTED_CODE,
        type_="invalid_request_error",
        param="messages",
        status=_UPSTREAM_STATUS,
    )


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
    detail = f" (non-text part types sent: {named})" if types else ""
    return _openai_error_body(
        message=f"{_MESSAGE_PREFIX}{detail}. {_MESSAGE_SUFFIX}",
        code=UNSUPPORTED_CONTENT_CODE,
        type_="invalid_request_error",
        param="messages",
        status=_UPSTREAM_STATUS,
    )


def error_detail_from_text(status_code: int, body_text: str) -> str:
    """Audit-row detail keeping the upstream text (same form as the 5xx arm)."""
    return f"status={status_code}: {body_text[:_MAX_BODY_CHARS]}"


def status_error_detail(exc: httpx.HTTPStatusError) -> str:
    """Audit detail of a raised upstream status error: body text, else ``str(exc)``.

    A body that was never read has no ``.text`` (``ResponseNotRead``) and an
    empty body carries no reason: both fall back to httpx's own sentence.
    """
    text = ""
    with contextlib.suppress(httpx.ResponseNotRead):
        text = exc.response.text
    return error_detail_from_text(exc.response.status_code, text or str(exc))


def classify_unsupported_content(
    status_code: int, body_text: str, payload: dict[str, Any]
) -> UnsupportedContentHit | None:
    """Match the raw upstream reply (file part or no-projector image).

    On a hit, log ONE WARNING and return the result.
    """
    if match_image_input_unsupported(status_code, body_text):
        error_body = image_input_unsupported_error_body()
    elif match_unsupported_content(status_code, body_text):
        error_body = unsupported_content_error_body(payload)
    else:
        return None
    logger.warning(
        "llama-server rejected a content part: HTTP %d, upstream body: %s",
        status_code,
        body_text[:_MAX_BODY_CHARS],
    )
    return UnsupportedContentHit(
        error_body=error_body,
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


def stream_detail(hit: UnsupportedContentHit | None, exc: httpx.HTTPStatusError) -> str:
    """Audit detail for a streamed upstream status error (hit's, else the body text)."""
    return detail_from_hit_or(hit, status_error_detail(exc))


async def read_error_body(resp: httpx.Response) -> None:
    """Read a streamed >= 400 body so ``.text`` exists to classify (ADR-024 untouched)."""
    if resp.status_code >= _UPSTREAM_STATUS:
        await resp.aread()


def completion_body(response: httpx.Response) -> dict[str, Any] | None:
    """The parsed completion of a usable reply, else ``None`` (silent).

    Usable = a 2xx whose body is a JSON object that carries no ``error`` object.
    """
    if not response.is_success:
        return None
    body: object = None
    with contextlib.suppress(ValueError):
        body = response.json()
    if isinstance(body, dict) and not isinstance(body.get("error"), dict):
        return body
    return None


def raise_status_error(response: httpx.Response) -> NoReturn:
    """Raise ``httpx.HTTPStatusError`` for a reply that is not a completion.

    A 2xx that is not one (non-JSON, not an object, or carrying an ``error``
    object) has no status to raise for, so the same exception is built here:
    every caller audits one class of failure.
    """
    response.raise_for_status()
    raise httpx.HTTPStatusError(
        f"Unusable reply '{response.status_code}' from the model server",
        request=response.request,
        response=response,
    )


def completion_or_raise(
    response: httpx.Response, payload: dict[str, Any]
) -> dict[str, Any]:
    """Inject non-stream arm: the completion, or ONE WARNING with the text and a raise.

    A recognised rejection keeps its own line (``classify_unsupported_content``);
    every other reply gets the generic one.
    """
    body = completion_body(response)
    if body is not None:
        return body
    status, text = response.status_code, response.text
    if classify_unsupported_content(status, text, payload) is None:
        logger.warning(
            "llama-server upstream error HTTP %d — body: %s",
            status,
            text[:_MAX_BODY_CHARS],
        )
    raise_status_error(response)


def gateway_error_response(
    status: int, failure_class: str, message: str
) -> JSONResponse:
    """A 502/504 of the proxy itself: the OpenAI error object plus ``detail``.

    ``error`` has the keys of the SSE error frame; ``detail`` (the same text) is
    kept for clients that read FastAPI's shape. Names no upstream URL.
    """
    # Function-local import: same chat.py cycle as unsupported_content_error_body.
    from audittrace.routes.chat import _openai_error_body

    body = _openai_error_body(message=message, code=failure_class, status=status)
    return JSONResponse(status_code=status, content={**body, "detail": message})


def non_stream_error_reply(
    status: int, text: str, payload: dict[str, Any], retry_after: str | None
) -> JSONResponse | None:
    """The relayable client reply for an upstream reply that is not a completion.

    A recognised rejection -> the explicit 400. A relayable status with a relayable
    body -> relayed with the upstream status, and with the upstream's well-formed
    ``Retry-After``. ``None`` -> the caller answers 502.
    """
    if match_image_input_unsupported(status, text):
        return JSONResponse(
            status_code=_UPSTREAM_STATUS, content=image_input_unsupported_error_body()
        )
    if match_unsupported_content(status, text):
        return JSONResponse(
            status_code=_UPSTREAM_STATUS,
            content=unsupported_content_error_body(payload),
        )
    body: object = None
    with contextlib.suppress(ValueError):
        body = json.loads(text)
    relayable = (
        httpx.codes.is_client_error(status)
        or (_RELAY_SERVER_ERRORS and httpx.codes.is_server_error(status))
    ) and status not in _HOP_CREDENTIAL_STATUSES
    shaped = isinstance(body, dict) and (
        isinstance(body.get("error"), dict) or not _RELAY_REQUIRES_ERROR_OBJECT
    )
    if not (relayable and shaped):
        return None
    headers: dict[str, str] = {}
    if retry_after is not None and _RETRY_AFTER_RE.fullmatch(retry_after):
        headers["retry-after"] = retry_after
    return JSONResponse(status_code=status, content=body, headers=headers)


async def non_stream_upstream_error(
    exc: httpx.HTTPStatusError,
    loop_payload: dict[str, Any],
    pending: list[Any],
    *,
    project: str,
    source: str,
    question: str,
    session_id: str,
    model: str,
    user_id: str,
    perf_start: float,
) -> JSONResponse:
    """Both non-stream arms: audit any reply that is not a completion, then reply.

    The failed row carries ``status=<code>: <upstream text[:400]>`` and the
    turn's already-recorded memory ``tool_calls`` (``pending``) are flushed
    onto it (none on the inject arm). Reply: ``non_stream_error_reply``, else the
    502 gateway error. Logs nothing: the caller's single WARNING is the one line.
    """
    # Function-local import: chat.py imports this module at import time (cycle).
    from audittrace.routes.chat import (
        FAILURE_UPSTREAM_ERROR,
        _flush_pending_tool_calls,
        _persist_interaction,
    )

    status = exc.response.status_code
    text = exc.response.text
    interaction_id = await _persist_interaction(
        project=project,
        source=source,
        question=question,
        answer="",
        prompt_tokens=0,
        completion_tokens=0,
        session_id=session_id,
        model=model,
        user_id=user_id,
        status="failed",
        failure_class=FAILURE_UPSTREAM_ERROR,
        error_detail=status_error_detail(exc),
        duration_ms=int((time.perf_counter() - perf_start) * 1000),
    )
    await _flush_pending_tool_calls(pending, interaction_id)
    reply = non_stream_error_reply(
        status, text, loop_payload, exc.response.headers.get("retry-after")
    )
    if reply is None:
        return gateway_error_response(
            502, FAILURE_UPSTREAM_ERROR, f"llama-server returned {status}"
        )
    return reply
