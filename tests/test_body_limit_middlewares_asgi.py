"""MEMORY-SERVER-SILENT-TRUNCATION: the two body-limit middlewares at the ASGI
level, where the framework's own handling of an aborted body is simulated.

``TestClient`` always reaches these through a full framework app. What happens
when the application swallows the abort, answers anyway, or has already begun
its answer is decided by the middleware, so it is driven here with small apps:

* an app that catches the abort and answers 400 on its own: the caller still
  gets exactly one 413, never the 400;
* an app that has already started its answer when the body overruns: the abort
  propagates, the 413 is not stacked on a started response;
* an app that lets the abort escape: the middleware answers 413;
* a message that is not a body chunk passes through unchanged.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest

from audittrace.config import get_settings as get_server_settings
from audittrace.memory_body_limit_middleware import MemoryBodyLimitMiddleware
from audittrace.memory_request_too_large_error import MemoryRequestTooLargeError
from bff.body_limit_middleware import BodyLimitMiddleware
from bff.body_limit_refusal import request_too_large_body
from bff.config import get_settings as get_bff_settings
from bff.request_too_large_error import RequestTooLargeError

Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
App = Callable[[dict[str, Any], Receive, Send], Awaitable[None]]

BFF_CEILING = 100
MEMORY_LIMIT = 10
MEMORY_CEILING = 6 * MEMORY_LIMIT + 1_048_576
CHUNK = b"x" * 600_000  # two of them pass the memory ceiling, one does not


@pytest.fixture(autouse=True)
def _small_ceilings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AUDITTRACE_BFF_MAX_REQUEST_BODY_BYTES", str(BFF_CEILING))
    monkeypatch.setenv(
        "AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET", "test-secret-never-real"
    )
    monkeypatch.setenv("AUDITTRACE_MEMORY_MAX_DOCUMENT_BYTES", str(MEMORY_LIMIT))
    get_bff_settings.cache_clear()
    get_server_settings.cache_clear()
    yield
    get_bff_settings.cache_clear()
    get_server_settings.cache_clear()


def _bff(app: App) -> Any:
    return BodyLimitMiddleware(app)


def _memory(app: App) -> Any:
    return MemoryBodyLimitMiddleware(app)


CASES: dict[str, tuple[Callable[[App], Any], str, bytes, type[Exception], Any]] = {
    "bff": (
        _bff,
        "/v1/chat/completions",
        b"y" * (BFF_CEILING + 1),
        RequestTooLargeError,
        request_too_large_body(BFF_CEILING),
    ),
    "memory": (
        _memory,
        "/memory/episodic",
        CHUNK * 2,
        MemoryRequestTooLargeError,
        {"detail": f"memory request body exceeds the limit of {MEMORY_CEILING} bytes"},
    ),
}


async def _drive(
    middleware: Any, path: str, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    queue = list(messages)

    async def receive() -> dict[str, Any]:
        return queue.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": path, "headers": []}
    await middleware(scope, receive, send)
    return sent


def _chunks(body: bytes) -> list[dict[str, Any]]:
    """The body as request messages of at most 600,000 bytes each."""
    parts = [body[i : i + 600_000] for i in range(0, len(body), 600_000)] or [b""]
    return [
        {"type": "http.request", "body": part, "more_body": index < len(parts) - 1}
        for index, part in enumerate(parts)
    ]


def _status_and_body(sent: list[dict[str, Any]]) -> tuple[int, Any]:
    starts = [m for m in sent if m["type"] == "http.response.start"]
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert len(starts) == 1, sent
    assert len(bodies) == 1, sent
    return starts[0]["status"], json.loads(bodies[0]["body"])


@pytest.mark.parametrize("case", sorted(CASES))
class TestBodyLimitMiddlewaresAsgi:
    def test_an_app_that_swallows_the_abort_and_answers_400_still_gives_one_413(
        self, case: str
    ) -> None:
        build, path, body, abort, refusal = CASES[case]

        async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
            try:
                while (await receive()).get("more_body"):
                    pass
            except abort:
                pass
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b'{"detail": "400"}'})

        sent = asyncio.run(_drive(build(app), path, _chunks(body)))

        assert _status_and_body(sent) == (413, refusal)

    def test_an_app_that_lets_the_abort_escape_gets_the_413_from_the_middleware(
        self, case: str
    ) -> None:
        build, path, body, _abort, refusal = CASES[case]

        async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
            while (await receive()).get("more_body"):
                pass

        sent = asyncio.run(_drive(build(app), path, _chunks(body)))

        assert _status_and_body(sent) == (413, refusal)

    def test_an_app_that_has_begun_its_answer_is_not_given_a_second_one(
        self, case: str
    ) -> None:
        build, path, body, abort, _refusal = CASES[case]

        async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            while (await receive()).get("more_body"):
                pass

        sent: list[dict[str, Any]] = []

        async def run() -> None:
            nonlocal sent
            try:
                sent = await _drive(build(app), path, _chunks(body))
            except abort:
                sent = []

        asyncio.run(run())

        assert sent == []  # the abort propagated; no 413 was stacked on the 200

    def test_a_message_that_is_not_a_body_chunk_passes_through_unchanged(
        self, case: str
    ) -> None:
        build, path, _body, _abort, _refusal = CASES[case]
        seen: list[dict[str, Any]] = []

        async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
            seen.append(await receive())
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        sent = asyncio.run(_drive(build(app), path, [{"type": "http.disconnect"}]))

        assert seen == [{"type": "http.disconnect"}]
        assert [
            m.get("status") for m in sent if m["type"] == "http.response.start"
        ] == [204]
