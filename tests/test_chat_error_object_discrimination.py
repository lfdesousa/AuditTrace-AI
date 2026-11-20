"""AT-NONSTREAM-UPSTREAM-STATUS r2 (F1) -- an ``error`` OBJECT is not an ``error`` key.

The contract speaks of an ``error`` OBJECT (ADDENDUM-2 4(d), ADDENDUM-3 2, Q3). Three
sites decide on it; each cell below fails when its site reads "an ``error`` key" instead:

* ``_upstream_errors.non_stream_error_reply``: a 4xx whose ``error`` is not an object
  is no relayable error, so the proxy answers its own 502 (one failed row).
* ``_upstream_errors.completion_body``: a 2xx completion that also carries
  ``"error": null`` is a success, not a failed turn.
* ``_stream_reply.StreamReplyCheck.feed``: a complete stream whose chunks carry
  ``"error": null`` is a success with no error frame.

Through the real HTTP route, both memory modes; every status assertion is exact.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_chat_nonstream_upstream_status import (
    CHUNK,
    DONE,
    MODES,
    SSE,
    TRACE_ID,
    _assert_failed_row,
    _call,
    _data,
    _only_row,
    _sid,
    _Upstream,
    _upstream_502,
)
from tests.test_chat_unsupported_content_error import (  # noqa: F401 (fixtures)
    inject_mode,
    tools_mode,
)

#: Bodies a 4xx may carry under ``error`` that are NOT an object.
NOT_AN_OBJECT: dict[str, Any] = {
    "string": "model not found",
    "null": None,
    "number": 404,
    "list": [{"message": "model not found"}],
}


def _completion(extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        **extra,
    }


def _error_object_frames(sse: str) -> list[Any]:
    """The ``data:`` frames whose ``error`` is an object (``"error": null`` is not one)."""
    frames = []
    for line in sse.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            frame = json.loads(line[6:])
            if isinstance(frame.get("error"), dict):
                frames.append(frame)
    return frames


def _chunk_with_error(error: Any, delta: dict[str, Any], finish: str | None) -> bytes:
    return _data(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            "error": error,
        }
    )


class TestA4xxWhoseErrorIsNotAnObjectIsTheProxys502:
    """(a) not relayed: no error object to relay, so 502 + one failed row."""

    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("kind", list(NOT_AN_OBJECT))
    async def test_502_gateway_object_and_one_failed_row(
        self,
        client: TestClient,
        request: pytest.FixtureRequest,
        mode: str,
        kind: str,
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        raw = json.dumps({"error": NOT_AN_OBJECT[kind]}).encode()
        fake, sid = _Upstream(404, raw), _sid(request)
        response = _call(client, fake, sid)
        assert response.status_code == 502
        assert response.json() == _upstream_502(404)
        await _assert_failed_row(sid, 404, raw.decode(), response)


class TestA2xxCompletionCarryingANonObjectErrorIsASuccess:
    """(b) an ``error`` that is not an object (null, string, ...) is no failure: served."""

    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("kind", list(NOT_AN_OBJECT))
    async def test_success_row_and_completion_relayed(
        self,
        client: TestClient,
        request: pytest.FixtureRequest,
        mode: str,
        kind: str,
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        body = _completion({"error": NOT_AN_OBJECT[kind]})
        fake, sid = _Upstream(200, json.dumps(body).encode()), _sid(request)
        response = _call(client, fake, sid)
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "Hello"
        row = await _only_row(sid)
        assert (row.status, row.failure_class, row.error_detail) == (
            "success",
            None,
            None,
        )
        assert row.answer == "Hello"
        assert row.trace_id is not None and TRACE_ID.fullmatch(row.trace_id)
        assert len(fake.post_calls) == 1


class TestACompleteStreamWhoseChunksCarryANonObjectErrorIsASuccess:
    """(c) a chunk ``error`` that is not an object: no frame, a success row."""

    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("kind", list(NOT_AN_OBJECT))
    async def test_success_row_no_error_frame(
        self,
        client: TestClient,
        request: pytest.FixtureRequest,
        mode: str,
        kind: str,
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        error = NOT_AN_OBJECT[kind]
        raw = (
            _chunk_with_error(error, {"content": "Hel"}, None)
            + _chunk_with_error(error, {}, "stop")
            + DONE
        )
        fake, sid = _Upstream(200, raw, headers=SSE), _sid(request)
        response = _call(client, fake, sid, stream=True)
        body = response.content.decode()
        assert response.status_code == 200
        assert _error_object_frames(body) == [], body
        assert body.endswith("data: [DONE]\n\n")
        row = await _only_row(sid)
        assert (row.status, row.failure_class, row.answer) == ("success", None, "Hel")


def test_the_fixtures_are_the_shapes_they_claim() -> None:
    """Guards the guards: the chunk helper differs from the plain one only by the key."""
    plain = json.loads(CHUNK.decode()[len("data: ") :])
    nulled = json.loads(
        _chunk_with_error(None, {"content": "Hel"}, None).decode()[len("data: ") :]
    )
    assert nulled == {**plain, "error": None}


class TestEmptyBodyDetail:
    """F5 + r3: ``raise_status_error`` keeps ``raise_for_status()`` (spec rule 8).

    A non-2xx with an EMPTY body has no text to audit, so the row reads
    ``status=<code>: (empty reply)`` (the streamed wording, ADDENDUM-3 2) and never
    httpx's sentence, which names the internal upstream URL. A 2xx that is no
    completion has no status to raise for, so the row carries the proxy's
    ``Unusable reply`` sentence. Dropping the ``raise_for_status()`` call turns the
    first cell into the second; restoring the ``str(exc)`` fallback names the URL.
    """

    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("status", [404, 503])
    async def test_non_2xx_empty_body_detail_is_empty_reply_without_url(
        self, client: TestClient, request: pytest.FixtureRequest, mode: str, status: int
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        fake, sid = _Upstream(status, b""), _sid(request)
        response = _call(client, fake, sid)
        assert response.status_code == 502
        row = await _only_row(sid)
        assert (row.status, row.failure_class) == ("failed", "upstream_error")
        assert row.error_detail == f"status={status}: (empty reply)"
        assert "http" not in row.error_detail and "url" not in row.error_detail
        assert "Unusable reply" not in row.error_detail
        assert "http" not in response.text

    @pytest.mark.parametrize("mode", MODES)
    async def test_2xx_empty_body_detail_is_the_proxys_sentence(
        self, client: TestClient, request: pytest.FixtureRequest, mode: str
    ) -> None:
        request.getfixturevalue(f"{mode}_mode")
        fake, sid = _Upstream(200, b""), _sid(request)
        response = _call(client, fake, sid)
        assert response.status_code == 502
        row = await _only_row(sid)
        assert row.error_detail == (
            "status=200: Unusable reply '200' from the model server"
        )
