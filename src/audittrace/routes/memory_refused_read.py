"""The server-side record of a refused read of a soft-deleted document (F-a).

The reply to the caller is the never-existed 404, byte for byte; that is the
security property of SOFTDELETE-DIRECT-READ and it does not change here. What
this adds is the ONE structured line an operator needs to follow the refusal by
trace: ``trace_id``, ``span_id`` (stamped by ``logging_config.TraceContextFilter``
from the active span) and ``user_id`` (explicit, so it does not depend on a
bound ContextVar), plus ``route`` and ``status``. ``route`` is the matched
route TEMPLATE (``/memory/semantic/{collection}/{document_id}``), never the raw
URL: the template carries no requested key. ``status`` is the int the caller
got (404).

The ``code`` is neutral: it is the code of the 404 the caller got
(``not_found``). The line itself is operator-side and DOES reveal that the key
existed and was deleted: it is written only for a deleted key, a key that never
existed logs nothing. That is within precedent: Tempo already tells the two
apart (``MemoryManifestService.resolve_read``), logs and traces are
operator-only, and admins can already read tombstones. What stays
byte-equal to a never-existed key is the caller-visible 404 body.
No ``read`` audit row is written: a refused read returns nothing, and the
existing read rows (``schedule_read_audit``) record reads that returned data.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from audittrace.identity import UserContext

logger = logging.getLogger(__name__)

REFUSED_READ_CODE = "not_found"
REFUSED_READ_STATUS = 404


def _route_template(request: Request) -> str:
    """The matched route's path template, include prefix and all.

    Starlette's ``scope["route"].path`` omits the ``include_router`` prefix
    (``/episodic/{filename:path}``). The prefix is the part of the request path
    in front of the first slash from which the route's own regex matches the
    rest; it is a configured constant, never the requested key (the key sits
    in the matched tail, which the template replaces). Falls back to the route
    path alone, and to the empty string, never to the raw URL."""
    route = request.scope.get("route")
    template = str(getattr(route, "path", ""))
    regex = getattr(route, "path_regex", None)
    path = str(request.scope.get("path", ""))
    if regex is None:
        return template
    for i, ch in enumerate(path):
        if ch == "/" and regex.match(path[i:]):
            return path[:i] + template
    return template


def refused_read(
    *, request: Request, user: UserContext, layer: str, detail: str
) -> HTTPException:
    """Log the refusal once and return the 404 to ``raise`` (its body is *detail*,
    the same text the route answers for a key that never existed)."""
    logger.info(
        "memory.read refused | code=%s layer=%s",
        REFUSED_READ_CODE,
        layer,
        extra={
            "user_id": user.user_id,
            "code": REFUSED_READ_CODE,
            "layer": layer,
            "route": _route_template(request),
            "status": REFUSED_READ_STATUS,
        },
    )
    return HTTPException(status_code=REFUSED_READ_STATUS, detail=detail)
