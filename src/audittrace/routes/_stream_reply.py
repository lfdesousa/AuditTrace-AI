"""Whether a 2xx reply to a streamed chat turn was a complete stream.

A model server can answer ``stream: true`` with HTTP 200 and a body that is no
stream at all (a JSON object, an HTML page, nothing), with data lines that do not
parse, with a stream that stops without ``data: [DONE]``, or with an ``error``
object inside the stream (llama-server's shape for a failure after generation
started). None of these is a completed turn. ``StreamReplyCheck`` watches one
upstream reply line by line and raises ``httpx.HTTPStatusError`` for each, so both
stream arms audit it on the path they already have for an upstream status error:
one SSE error frame, one ``failed`` / ``upstream_error`` row, one WARNING.

A reply is a COMPLETE STREAM iff all hold: every ``data: `` payload that parses is a
JSON object, at least one did, none of them carried an ``error`` object, and
``data: [DONE]`` was seen.

Wire format: a data line is ``data: `` WITH the single space after the colon, as
llama-server sends it and as both stream loops in ``chat.py`` require. An upstream
that sends ``data:{...}`` (no space) is therefore a reply with no completion chunk.

Disclosure: the WARNING (and so the log pipeline) carries up to ``_HEAD_CHARS``
(400) characters of the start of the raw reply. For a stream that is cut after
content, that head includes the first tokens of the model's answer. This is the same
exposure class as the non-stream arms' WARNING with the upstream text.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import NoReturn

import httpx

logger = logging.getLogger(__name__)

_DATA = "data: "
_DONE = "[DONE]"
#: How much of the reply's start is kept in memory and written to the WARNING. This is a
#: memory bound only: the audit row's own cut is ``_upstream_errors._MAX_BODY_CHARS``,
#: applied when the detail is built, so this constant is not asserted by any test.
_HEAD_CHARS = 400
EMPTY_REPLY = "(empty reply)"
_UNPARSEABLE = object()


class StreamReplyCheck:
    """Watch one upstream reply to a streamed turn; raise unless it is a stream."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self._head = ""
        self._chunks = 0
        self._done = False

    def feed(self, line: str) -> None:
        """Record one upstream line.

        Raises at once on a ``data:`` payload that is JSON but not an object, or
        that carries an ``error`` object. A payload that does not parse is skipped,
        as both stream loops skip it.
        """
        if len(self._head) < _HEAD_CHARS:
            self._head = (self._head + line + "\n")[:_HEAD_CHARS]
        stripped = line.strip()
        if not stripped.startswith(_DATA):
            return
        payload = stripped[len(_DATA) :]
        if payload == _DONE:
            self._done = True
            return
        chunk: object = _UNPARSEABLE
        with contextlib.suppress(ValueError):
            chunk = json.loads(payload)
        if chunk is _UNPARSEABLE:
            return
        if not isinstance(chunk, dict):
            self._fail("a data payload that is not a JSON object")
        if isinstance(chunk.get("error"), dict):
            self._fail("an error object in the stream")
        self._chunks += 1

    def raise_unless_complete(self) -> None:
        """Call when the upstream body ended: raise unless it was a complete stream."""
        if not self._chunks:
            self._fail("no completion chunk")
        if not self._done:
            self._fail("no [DONE]")

    def _fail(self, reason: str) -> NoReturn:
        head = self._head.strip() or EMPTY_REPLY
        logger.warning(
            "llama-server stream reply HTTP %d is not a complete stream (%s) — head: %s",
            self._response.status_code,
            reason,
            head,
        )
        raise httpx.HTTPStatusError(
            head, request=self._response.request, response=self._response
        )
