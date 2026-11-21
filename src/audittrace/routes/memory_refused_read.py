"""The server-side record of a refused read of a soft-deleted document (F-a).

The reply to the caller is the never-existed 404, byte for byte; that is the
security property of SOFTDELETE-DIRECT-READ and it does not change here. What
this adds is the ONE structured line an operator needs to follow the refusal by
trace: ``trace_id``, ``span_id`` (stamped by ``logging_config.TraceContextFilter``
from the active span) and ``user_id`` (explicit, so it does not depend on a
bound ContextVar).

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

from fastapi import HTTPException

from audittrace.identity import UserContext

logger = logging.getLogger(__name__)

REFUSED_READ_CODE = "not_found"


def refused_read(*, user: UserContext, layer: str, detail: str) -> HTTPException:
    """Log the refusal once and return the 404 to ``raise`` (its body is *detail*,
    the same text the route answers for a key that never existed)."""
    logger.info(
        "memory.read refused | code=%s layer=%s",
        REFUSED_READ_CODE,
        layer,
        extra={"user_id": user.user_id, "code": REFUSED_READ_CODE, "layer": layer},
    )
    return HTTPException(status_code=404, detail=detail)
