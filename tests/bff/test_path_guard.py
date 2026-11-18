"""BFF-CHAT-ROUTE-ERRORS ADDENDUM-3 (gate S5): the BFF does not relay a path
that means something else than it spells.

The proxy routes forward the path suffix they were given, under the BFF's own
mesh identity, and an HTTP client resolves ``.`` and ``..`` before it sends:
``/memory/../interactions/refusals`` left the BFF as ``POST
/interactions/refusals`` (measured). A path with a dot segment, or with an
encoded slash or backslash, is answered 404 in the one shape before any route,
token check or upstream call.

The cells build the ASGI scope by hand: an HTTP client library normalises the
path before it leaves, so it cannot carry these spellings.

Neuters (each alone): G1 remove the middleware -> every ``refused`` cell; G2
check the raw path only (not the decoded one) -> ``encoded_dots``; G3 drop the
encoded-separator check -> the ``encoded_slash`` and ``encoded_backslash``
cells; G4 refuse any segment CONTAINING a dot -> ``test_names_with_dots_are_
relayed``; G5 (round 2) drop the per-segment second decode / parameter strip
-> the ``double_encoded_*`` dot cells and the ``parameter_*`` cells; G6 drop
the residual-encoding check -> ``double_encoded_slash``, ``..._backslash``,
``..._dot_inside_a_name``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest

from bff.app import create_app, get_http_client
from bff.config import get_settings
from tests.bff.test_all_routes_errors import _good, _Upstreams
from tests.bff.test_app import _settings

# id -> (method, decoded path, raw path as the request line spelled it, reason)
REFUSED: dict[str, tuple[str, str, bytes, str]] = {
    "parent_segment": (
        "POST",
        "/memory/../interactions/refusals",
        b"/memory/../interactions/refusals",
        "dot_segment",
    ),
    "two_parent_segments": (
        "POST",
        "/memory/episodic/../../interactions/refusals",
        b"/memory/episodic/../../interactions/refusals",
        "dot_segment",
    ),
    "current_segment": (
        "GET",
        "/memory/./episodic",
        b"/memory/./episodic",
        "dot_segment",
    ),
    "encoded_dots": (
        "GET",
        "/memory/../episodic",
        b"/memory/%2e%2e/episodic",
        "dot_segment",
    ),
    "encoded_slash": (
        "GET",
        "/memory/a/../b",
        b"/memory/a%2F..%2Fb",
        "encoded_separator",
    ),
    "encoded_slash_lower_case": (
        "GET",
        "/memory/semantic/a/b",
        b"/memory/semantic/a%2fb",
        "encoded_separator",
    ),
    "encoded_backslash": (
        "GET",
        "/memory/a\\..\\b",
        b"/memory/a%5C..%5Cb",
        "encoded_separator",
    ),
    "double_encoded_dots": (
        "GET",
        "/memory/%2e%2e/interactions/refusals",
        b"/memory/%252e%252e/interactions/refusals",
        "dot_segment",
    ),
    "half_double_encoded_dots": (
        "GET",
        "/memory/.%2e/episodic",
        b"/memory/.%252e/episodic",
        "dot_segment",
    ),
    "upper_case_double_encoded_dot": (
        "GET",
        "/memory/%2E./episodic",
        b"/memory/%252E./episodic",
        "dot_segment",
    ),
    "parameter_after_two_dots": (
        "GET",
        "/memory/..;x/episodic",
        b"/memory/..;x/episodic",
        "dot_segment",
    ),
    "parameter_after_one_dot": (
        "GET",
        "/memory/.;x/episodic",
        b"/memory/.;x/episodic",
        "dot_segment",
    ),
    "double_encoded_slash": (
        "GET",
        "/memory/a%2fb",
        b"/memory/a%252fb",
        "encoded_separator",
    ),
    "double_encoded_backslash": (
        "GET",
        "/memory/a%5cb",
        b"/memory/a%255cb",
        "encoded_separator",
    ),
    "double_encoded_dot_inside_a_name": (
        "GET",
        "/memory/episodic/a%2eb",
        b"/memory/episodic/a%252eb",
        "encoded_dot",
    ),
    "console_route": (
        "DELETE",
        "/console/conversation-tags/../files/x",
        b"/console/conversation-tags/../files/x",
        "dot_segment",
    ),
    "chat_route": (
        "POST",
        "/v1/chat/completions/..",
        b"/v1/chat/completions/..",
        "dot_segment",
    ),
}
RELAYED: dict[str, tuple[str, bytes]] = {
    "plain": ("/memory/episodic/note.md", b"/memory/episodic/note.md"),
    "two_dots_inside_a_name": ("/memory/episodic/a..b.md", b"/memory/episodic/a..b.md"),
    "leading_dot_name": ("/memory/episodic/.hidden", b"/memory/episodic/.hidden"),
    "three_dots": ("/memory/episodic/...", b"/memory/episodic/..."),
    "semicolon_inside_a_name": (
        "/memory/episodic/a;b.md",
        b"/memory/episodic/a;b.md",
    ),
    "encoded_space": ("/memory/episodic/a b.md", b"/memory/episodic/a%20b.md"),
}


def _call(
    method: str, path: str, raw_path: bytes
) -> tuple[int, dict[str, Any], list[str]]:
    """One request through the real app with a hand-built scope. Returns the
    status, the JSON body and the paths the memory server was asked for."""
    upstreams = _Upstreams()
    asked: list[str] = []

    def memory_server(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"ok": True})

    upstreams.memory_server = memory_server
    sent: list[dict[str, Any]] = []

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(upstreams.handler)
        ) as client:
            app = create_app()
            app.dependency_overrides[get_settings] = _settings
            app.dependency_overrides[get_http_client] = lambda: client
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": path,
                "raw_path": raw_path,
                "query_string": b"",
                "root_path": "",
                "headers": [(b"authorization", _good().encode()), (b"host", b"bff")],
                "client": ("127.0.0.1", 1),
                "server": ("bff", 80),
            }

            body_sent = False

            async def receive() -> dict[str, Any]:
                nonlocal body_sent
                if body_sent:  # the client stays connected and sends no more
                    await asyncio.Event().wait()
                body_sent = True
                return {"type": "http.request", "body": b"{}", "more_body": False}

            async def send(message: dict[str, Any]) -> None:
                sent.append(message)

            await app(scope, receive, send)

    asyncio.run(run())
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return status, json.loads(body), asked


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_a_path_that_means_something_else_is_refused(
    case: str, caplog: pytest.LogCaptureFixture
) -> None:
    method, path, raw_path, reason = REFUSED[case]
    with caplog.at_level(logging.INFO, logger="bff"):
        status, body, asked = _call(method, path, raw_path)

    message = "The BFF has no such route."
    assert status == 404
    assert body == {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "param": None,
            "code": "not_found",
            "status": 404,
        },
        "detail": message,
    }
    assert asked == []
    loud = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert loud == [f"path_guard.refused reason={reason}"]


@pytest.mark.parametrize("case", sorted(RELAYED))
def test_names_with_dots_are_relayed(case: str) -> None:
    """Control, green before and after: only a segment that IS ``.`` or ``..``
    is refused; a file name may hold dots."""
    path, raw_path = RELAYED[case]
    status, body, asked = _call("GET", path, raw_path)

    assert status == 200
    assert body == {"ok": True}
    assert len(asked) == 1
    assert asked[0].startswith("/memory/episodic/")
