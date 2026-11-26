"""CONSOLE-AUDIT-EVIDENCE: the console message id of a chat turn, read once and kept
bound while the turn is persisted.

``X-AuditTrace-Message-Id`` carries the LibreChat message id the BFF forwards. This module
is the sibling of :mod:`audittrace.routes._session_bind` (one concept per file):

* :func:`header_message_id` is the ONE function that reads the header;
* :func:`message_id_rejection` is the shared predicate, also used for the evidence path
  parameter and the refusal ingest field, so every entry point accepts the same set;
* :func:`bound_message_id` / :func:`stream_with_bound_message_id` keep the id in a
  request-scoped ContextVar so ``_persist_interaction`` (21 call sites) inherits it without
  each site passing it.

The id is correlation metadata, never a credential and never hashed: a forged value can
only find the caller's own rows (the lookup is row-level-security scoped).
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import AsyncGenerator, Iterator
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

#: The accepted shape: 1-255 characters of ``[A-Za-z0-9._:-]``, and not ``.`` / ``..`` (a
#: dot segment would collapse in a URL built from the id). ALWAYS applied with
#: ``re.fullmatch``: ``$`` also matches before a trailing newline, so ``re.match`` would
#: accept ``"abc\n"``. The lookahead is why a ``field_validator`` is used where a pydantic
#: ``Field(pattern=...)`` cannot be (its default Rust regex engine rejects lookaheads).
MESSAGE_ID_RE = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9._:-]{1,255}$")

#: An unresolved template placeholder (the console's ``{{LIBRECHAT_BODY_MESSAGEID}}`` left
#: as is) names no message. Checked FIRST so the reason code is ``template``.
_TEMPLATE_MARKERS = ("{{", "}}")
_MAX_LENGTH = 255

_current_message_id: ContextVar[str | None] = ContextVar(
    "audittrace_client_message_id", default=None
)


def message_id_rejection(value: str) -> str | None:
    """``None`` when ``value`` has the accepted shape, else the reason code.

    Order (A6 A2): ``template``, then the shape; a shape failure is ``dot_only``,
    ``too_long`` or ``invalid_charset``.
    """
    if any(marker in value for marker in _TEMPLATE_MARKERS):
        return "template"
    if MESSAGE_ID_RE.fullmatch(value) is not None:
        return None
    if value in (".", ".."):
        return "dot_only"
    if len(value) > _MAX_LENGTH:
        return "too_long"
    return "invalid_charset"


def header_message_id(request: Any) -> str | None:
    """Return the request's ``X-AuditTrace-Message-Id`` if it has the accepted shape.

    * absent or blank -> ``None``, silently;
    * present but malformed -> ``None`` plus ONE WARNING carrying the LENGTH and a reason
      code, never the value (it is caller-chosen text); never a 4xx.

    Falsifiable: log the value, or replace ``fullmatch`` with ``match``, and
    ``tests/test_message_id_bind.py`` goes RED.
    """
    raw = request.headers.get("x-audittrace-message-id")
    if not isinstance(raw, str):
        return None
    value = raw.strip(" ")
    if not value:
        return None
    reason = message_id_rejection(value)
    if reason is not None:
        logger.warning(
            "message_id.header_rejected",
            extra={"header_length": len(value), "reason": reason},
        )
        return None
    return value


def current_message_id() -> str | None:
    """The id bound for the running request, ``None`` outside one."""
    return _current_message_id.get()


@contextlib.contextmanager
def bound_message_id(message_id: str | None) -> Iterator[None]:
    """Bind ``message_id`` for the block and restore the previous value on every exit
    path (nothing bleeds into the next request served on the same task)."""
    token = _current_message_id.set(message_id)
    try:
        yield
    finally:
        _current_message_id.reset(token)


async def stream_with_bound_message_id(
    message_id: str | None, body: AsyncGenerator[bytes, None]
) -> AsyncGenerator[bytes, None]:
    """Yield ``body`` unchanged with ``message_id`` bound while it runs.

    A ``StreamingResponse`` body is consumed AFTER the handler returned, so the
    handler's own block has already exited; the id is re-bound inside the generator,
    on the task that iterates it (the ``stream_with_bound_session`` pattern).
    """
    with bound_message_id(message_id):
        async with contextlib.aclosing(body) as inner:
            async for chunk in inner:
                yield chunk
