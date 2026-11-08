"""SESSION-ID-BIND (v1.31.0 WP-6): keep the request's ``session_id`` bound for
as long as the response is being produced.

``POST /v1/chat/completions`` and ``POST /mcp`` resolve the request's
``session_id`` (``chat._resolve_session_id``) and bind it into the
console-store ContextVar (``bound_session_id``), which the
``TraceContextFilter`` stamps on every log line.

* A non-streaming handler binds with the plain context manager and the id is
  restored when the handler returns.
* A streaming handler returns a ``StreamingResponse`` whose body iterator is
  consumed AFTER the handler returned (ADDENDUM-2 S8), so the handler's own
  block has already exited: :func:`stream_with_bound_session` re-binds inside
  the generator, on the task that iterates it, and restores on every exit
  path (exhaustion, error, client disconnect). Log lines emitted from inside
  the stream therefore still carry ``session_id``.

Kept out of ``routes/chat.py`` on purpose (PYTHON-ENGINEERING section 11: that
module is past 2000 lines; new work goes in a sibling).
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from typing import Any

from audittrace.services.console_store._context import bound_session_id

logger = logging.getLogger(__name__)

#: The accepted shape of a client-supplied ``X-Session-Id`` (LC-10a R-Q5):
#: printable ASCII without space, 1 to 255 characters. The reject set is the
#: LibreChat fork's, pinned verbatim by its AC-23. ALWAYS applied with
#: ``re.fullmatch``: ``$`` also matches before a trailing newline, so
#: ``re.match`` with this pattern would accept ``"abc\n"``.
SESSION_ID_RE = re.compile(r"^[\x21-\x7e]{1,255}$")


#: An unresolved template placeholder (for example the console's
#: ``{{LIBRECHAT_BODY_CONVERSATIONID}}`` when the client could not fill it)
#: matches the printable-ASCII shape but names no chat: every such request
#: would pool into ONE compartment. Rejected like any other malformed value
#: (operator ruling 2026-11-08, option 1).
_TEMPLATE_MARKERS = ("{{", "}}")


def _looks_like_template(value: str) -> bool:
    return any(marker in value for marker in _TEMPLATE_MARKERS)


def header_session_id(request: Any) -> str | None:
    """Return the request's ``X-Session-Id`` if it has the accepted shape.

    The ONE function that reads the header (LC-10a H-A4). Surrounding
    spaces are trimmed (an HTTP parser has already dropped optional
    whitespace, so ``" s1 "`` is ``"s1"``); the trimmed value must then
    ``fullmatch`` :data:`SESSION_ID_RE`.

    * absent or blank -> ``None``, silently (it is simply not there);
    * present but malformed (wrong shape, or an unresolved ``{{...}}``
      template placeholder) -> ``None`` plus a WARNING that carries the
      LENGTH only, never the value (the id is caller-chosen text).

    The caller decides what ``None`` means: chat and MCP fall back to the
    computed id, ``/memory/upload`` binds nothing (a ``NULL`` compartment
    that no bound recall returns).

    Falsifiable: drop the ``fullmatch`` and a 256-character or control-
    character id reaches ``interactions.session_id`` and the compartment
    (``tests/test_session_id_shape.py``).
    """
    raw = request.headers.get("x-session-id")
    if not isinstance(raw, str):
        return None
    value = raw.strip(" ")
    if not value:
        return None
    if SESSION_ID_RE.fullmatch(value) is None or _looks_like_template(value):
        logger.warning(
            "session_id.header_rejected", extra={"header_length": len(value)}
        )
        return None
    return value


async def stream_with_bound_session(
    session_id: str | None, body: AsyncGenerator[bytes, None]
) -> AsyncIterator[bytes]:
    """Yield ``body`` unchanged with ``session_id`` bound while it runs.

    ``aclosing`` closes ``body`` deterministically on early exit so the inner
    generator's own ``finally`` blocks run inside the bound block too.

    Falsifiable: return ``body`` without the wrapper at a ``StreamingResponse``
    site and the ``stream: true`` request lines lose ``session_id``
    (``tests/test_session_id_bind_route.py``).
    """
    with bound_session_id(session_id):
        async with contextlib.aclosing(body) as inner:
            async for chunk in inner:
                yield chunk


@contextlib.contextmanager
def bound_upload_session(request: Any) -> Iterator[None]:
    """Bind the ``X-Session-Id`` of a ``/memory/upload`` request for the write.

    Absent or malformed -> binds ``None`` (a ``NULL`` compartment that no bound
    recall returns, R-Q6 strict). Kept here so ``routes/memory.py`` (3400+
    lines) gains one import and one ``with`` only.

    Falsifiable: drop the bind and a header-bearing upload is stored with
    ``session_id NULL`` (``tests/integration/test_lc10a_session_recall.py``).
    """
    with bound_session_id(header_session_id(request)):
        yield
