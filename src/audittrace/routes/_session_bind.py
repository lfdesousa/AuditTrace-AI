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
from collections.abc import AsyncGenerator, AsyncIterator

from audittrace.services.console_store._context import bound_session_id


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
