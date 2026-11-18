"""BFF-CHAT-ROUTE-ERRORS ADDENDUM-2, item 1: every BFF route answers its own
failures in the one shape, with the fixed codes of the chat route.

Lands as ``tests/bff/test_all_routes_errors.py``. The routes are read from the
app itself, so a new proxy route is covered the day it is added. Each cell
goes through the real route; ``httpx.MockTransport`` plays Keycloak and the
memory server. Status, challenge and body are asserted by equality. The body
is the error object plus the additive ``detail`` (the same message): the
LibreChat fork's clients print ``data.detail`` and would print
``[object Object]`` for the error object alone.

Neuters (each alone): R1 drop ``detail`` from the route answer -> every cell of
``test_route_refusal``; R2 answer a route's JWKS failure 401 as before -> the
``jwks_unreachable`` cells; R3 keep a refused subject token at 502 on the routes
-> the ``subject_refused`` cells; R4 remove one ``@route_last_resort`` -> that
route's ``unforeseen`` cell; R5 give one route twin another status than its
chat twin -> ``test_route_set_is_the_chat_set``; R6 remove the framework error
handler -> ``test_framework_answers_are_in_the_one_shape``; R7 add ``detail`` to
a relayed answer -> ``test_relayed_answers_are_untouched_on_a_proxy_route``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jose import jwt

from bff import chat_refusals
from bff.app import create_app, get_http_client
from bff.config import get_settings
from tests.bff.conftest import TEST_ISSUER, TEST_JWK, TEST_PRIVATE_PEM, make_token
from tests.bff.test_app import (
    KEYCLOAK_JWKS_URL,
    KEYCLOAK_TOKEN_URL,
    ORCHESTRATOR_BASE,
    _settings,
    _sub_from_form_body,
)

SESSION_ID = "librechat-conversation-7"
INBOUND_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
HEADERS = {
    "traceparent": f"00-{INBOUND_TRACE_ID}-b7ad6b7169203331-01",
    "X-Session-Id": SESSION_ID,
}
CHALLENGE = (
    'Bearer error="invalid_token", '
    'error_description="The access token is invalid or expired"'
)
# code -> (status, error.type, WWW-Authenticate | None, message)
ROUTE_CONTRACT: dict[str, tuple[int, str, str | None, str]] = {
    "missing_token": (
        401,
        "invalid_request_error",
        "Bearer",
        "Sign-in required: this request carries no bearer access token. Sign in "
        "and try again.",
    ),
    "invalid_token": (
        401,
        "invalid_request_error",
        CHALLENGE,
        "Your sign-in is no longer valid: the access token is invalid or "
        "expired. Sign out and sign in again.",
    ),
    "auth_service_unavailable": (
        502,
        "api_error",
        None,
        "AuditTrace is temporarily unavailable: the sign-in service cannot be "
        "reached or its answer cannot be used. Try again in a moment. If this "
        "message returns, contact an administrator.",
    ),
    "token_exchange_refused": (
        502,
        "api_error",
        None,
        "AuditTrace is unavailable: the sign-in service refused to issue the "
        "access token for this request. Contact an administrator: the cause is "
        "the server configuration, not your request.",
    ),
    "token_exchange_invalid": (
        502,
        "api_error",
        None,
        "AuditTrace is unavailable: the sign-in service issued an access token "
        "that the gateway refused. Contact an administrator: the cause is the "
        "server configuration, not your request.",
    ),
    "upstream_unavailable": (
        502,
        "api_error",
        None,
        "AuditTrace is temporarily unavailable: the memory server cannot be "
        "reached. Try again in a moment. If this message returns, contact an "
        "administrator.",
    ),
    "internal_error": (
        500,
        "api_error",
        None,
        "AuditTrace is unavailable: the gateway failed unexpectedly. Try again "
        "in a moment. If this message returns, contact an administrator.",
    ),
}
# path prefix -> the route label of the log event
LABELS: dict[str, str] = {
    "/memory": "memory",
    "/console/files/{filename}/promote": "console_files_promote",
    "/console/files": "console_files",
    "/console/conversations": "console_conversations",
    "/console/presets": "console_presets",
    "/console/prompts": "console_prompts",
    "/console/chat-projects": "console_chat_projects",
    "/console/file-records": "console_file_records",
    "/console/agents": "console_agents",
    "/console/conversation-tags": "console_conversation_tags",
    "/console/tool-favorites": "console_tool_favorites",
}
NOT_PROXIES = {"/health", "/v1/chat/completions"}

Respond = Callable[[httpx.Request], httpx.Response]


def _proxy_routes() -> dict[str, tuple[str, str, str]]:
    """id -> (method, concrete path, label), for every route of the app that is
    neither ``/health`` nor the chat route."""
    found: dict[str, tuple[str, str, str]] = {}
    for route in create_app().routes:
        if not isinstance(route, APIRoute) or route.path in NOT_PROXIES:
            continue
        label = next(
            label
            for prefix, label in LABELS.items()
            if route.path == prefix or route.path.startswith(prefix + "/")
        )
        method = "POST" if "POST" in route.methods else sorted(route.methods)[0]
        path = route.path.replace("{path:path}", "x").replace("{filename}", "n.txt")
        found[f"{method} {route.path}"] = (method, path, label)
    return found


ROUTES = _proxy_routes()


def test_the_app_has_the_nineteen_proxy_routes() -> None:
    """The cells below would be vacuous over a wrong route list."""
    assert len(ROUTES) == 19
    assert {label for _m, _p, label in ROUTES.values()} == set(LABELS.values())


class _Upstreams:
    """Keycloak and the memory server behind one ``MockTransport``."""

    def __init__(self) -> None:
        self.jwks: Respond | None = None
        self.exchange: Respond | None = None
        self.memory_server: Respond | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == KEYCLOAK_JWKS_URL:
            if self.jwks is not None:
                return self.jwks(request)
            return httpx.Response(200, json={"keys": [TEST_JWK]})
        if url == KEYCLOAK_TOKEN_URL:
            if self.exchange is not None:
                return self.exchange(request)
            now = int(time.time())
            form = request.read().decode()
            claims = {
                "iss": TEST_ISSUER,
                "sub": _sub_from_form_body(form),
                # Keycloak grants what the route asked for (no missing-scope warning).
                "scope": parse_qs(form)["scope"][0],
                "iat": now,
                "exp": now + 300,
            }
            token = jwt.encode(claims, TEST_PRIVATE_PEM, algorithm="RS256")
            return httpx.Response(200, json={"access_token": token})
        if url.startswith(ORCHESTRATOR_BASE):
            if self.memory_server is not None:
                return self.memory_server(request)
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected upstream call: {url}")


def _raises(exc: Exception) -> Respond:
    def respond(_request: httpx.Request) -> httpx.Response:
        raise exc

    return respond


def _kc(status: int, error: str) -> Respond:
    return lambda _request: httpx.Response(status, json={"error": error})


def _good() -> str:
    return "Bearer " + make_token(sub="alice")


# failure id -> (Authorization | None, upstream attribute, respond, code, sub)
FAILURES: dict[str, tuple[Callable[[], str] | None, str, Respond | None, str, str]] = {
    "missing": (None, "", None, "missing_token", "-"),
    "invalid": (lambda: "Bearer not-a-jwt", "", None, "invalid_token", "-"),
    "jwks_unreachable": (
        _good,
        "jwks",
        _raises(httpx.ConnectError("refused")),
        "auth_service_unavailable",
        "-",
    ),
    "exchange_refused": (
        _good,
        "exchange",
        _kc(403, "access_denied"),
        "token_exchange_refused",
        "alice",
    ),
    "subject_refused": (
        _good,
        "exchange",
        _kc(400, "invalid_token"),
        "invalid_token",
        "alice",
    ),
    "exchange_unavailable": (
        _good,
        "exchange",
        _raises(httpx.ConnectError("refused")),
        "auth_service_unavailable",
        "alice",
    ),
    "exchange_invalid": (
        _good,
        "exchange",
        lambda _request: httpx.Response(200, json=[]),
        "token_exchange_invalid",
        "alice",
    ),
    "upstream_unreachable": (
        _good,
        "memory_server",
        _raises(httpx.ConnectError("refused")),
        "upstream_unavailable",
        "alice",
    ),
}

_EVENT = re.compile(
    r"^route\.refused route=(?P<route>\S+) code=(?P<code>\S+) "
    r"status=(?P<status>\d{3}) sub=(?P<sub>\S+) session_id=(?P<session_id>\S+) "
    r"inbound_trace_id=(?P<inbound_trace_id>\S+) cause=(?P<cause>.*)$",
    re.DOTALL,
)


@pytest.fixture
def bff() -> Iterator[tuple[TestClient, _Upstreams]]:
    upstreams = _Upstreams()
    with contextlib.ExitStack() as stack:
        mock_client = httpx.AsyncClient(
            transport=httpx.MockTransport(upstreams.handler)
        )
        stack.callback(lambda: asyncio.run(mock_client.aclose()))
        app = create_app()
        app.dependency_overrides[get_settings] = _settings
        app.dependency_overrides[get_http_client] = lambda: mock_client
        client = stack.enter_context(TestClient(app, raise_server_exceptions=False))
        yield client, upstreams


def _expected(code: str) -> tuple[int, str | None, dict[str, Any]]:
    status, error_type, challenge, message = ROUTE_CONTRACT[code]
    error = {
        "message": message,
        "type": error_type,
        "param": None,
        "code": code,
        "status": status,
    }
    return status, challenge, {"error": error, "detail": message}


def _assert_refusal(
    response: httpx.Response,
    caplog: pytest.LogCaptureFixture,
    *,
    label: str,
    code: str,
    sub: str,
) -> None:
    status, challenge, body = _expected(code)
    assert response.status_code == status
    assert response.headers.get("www-authenticate") == challenge
    assert response.headers["content-type"] == "application/json"
    assert response.json() == body
    loud = [record for record in caplog.records if record.levelno >= logging.WARNING]
    events = [_EVENT.match(record.getMessage()) for record in loud]
    assert len(events) == 1 and events[0] is not None, [r.getMessage() for r in loud]
    assert events[0].groupdict() | {"cause": ""} == {
        "route": label,
        "code": code,
        "status": str(status),
        "sub": sub,
        "session_id": SESSION_ID,
        "inbound_trace_id": INBOUND_TRACE_ID,
        "cause": "",
    }
    assert loud[0].levelno == (logging.WARNING if status < 500 else logging.ERROR)
    assert len(events[0].string.splitlines()) == 1


@pytest.mark.parametrize("failure", sorted(FAILURES))
@pytest.mark.parametrize("route", sorted(ROUTES))
def test_route_refusal(
    bff: tuple[TestClient, _Upstreams],
    route: str,
    failure: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The exact status, challenge, body and the one log event, per route."""
    client, upstreams = bff
    method, path, label = ROUTES[route]
    authorization, attribute, respond, code, sub = FAILURES[failure]
    if attribute:
        setattr(upstreams, attribute, respond)
    headers = dict(HEADERS)
    if authorization is not None:
        headers["Authorization"] = authorization()
    with caplog.at_level(logging.INFO, logger="bff"):
        response = client.request(method, path, headers=headers, content=b"{}")

    _assert_refusal(response, caplog, label=label, code=code, sub=sub)


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_route_unforeseen_exception(
    bff: tuple[TestClient, _Upstreams],
    route: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The last resort covers every proxy route, not only chat."""
    client, _upstreams = bff
    method, path, label = ROUTES[route]

    async def boom(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("unforeseen")

    monkeypatch.setattr("bff.app.exchange_token", boom)
    with caplog.at_level(logging.INFO, logger="bff"):
        response = client.request(
            method, path, headers={**HEADERS, "Authorization": _good()}, content=b"{}"
        )

    _assert_refusal(response, caplog, label=label, code="internal_error", sub="alice")


def test_route_set_is_the_chat_set() -> None:
    """Same codes, statuses, types and challenges as the chat route: the two
    sets cannot drift apart. Only the wording differs."""
    from bff.route_refusals import ROUTE_REFUSALS

    def shape(refusals: Any) -> dict[str, tuple[int, str, str | None]]:
        return {r.code: (r.status, r.error_type, r.challenge) for r in refusals}

    chat = shape(
        refusal
        for refusal in vars(chat_refusals).values()
        if isinstance(refusal, chat_refusals.ChatRefusal)
    )
    assert shape(ROUTE_REFUSALS.values()) == chat
    assert chat == {
        code: (status, error_type, challenge)
        for code, (status, error_type, challenge, _m) in ROUTE_CONTRACT.items()
    }
    assert {c: r.message for c, r in ROUTE_REFUSALS.items()} == {
        code: message for code, (_s, _t, _c, message) in ROUTE_CONTRACT.items()
    }


@pytest.mark.parametrize(
    ("method", "path", "status", "code", "message"),
    [
        ("GET", "/no-such-route", 404, "not_found", "The BFF has no such route."),
        (
            "DELETE",
            "/health",
            405,
            "method_not_allowed",
            "The BFF does not serve this method on this route.",
        ),
    ],
)
def test_framework_answers_are_in_the_one_shape(
    bff: tuple[TestClient, _Upstreams],
    method: str,
    path: str,
    status: int,
    code: str,
    message: str,
) -> None:
    client, _upstreams = bff
    response = client.request(method, path)

    assert response.status_code == status
    assert response.json() == {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "param": None,
            "code": code,
            "status": status,
        },
        "detail": message,
    }


def test_relayed_answers_are_untouched_on_a_proxy_route(
    bff: tuple[TestClient, _Upstreams], caplog: pytest.LogCaptureFixture
) -> None:
    """What the memory server answers is relayed as it is: the fork branches
    on these statuses (404, 409) and reads their ``detail``."""
    client, upstreams = bff
    upstreams.memory_server = lambda _request: httpx.Response(
        404,
        content=b'{"detail":"Not found"}',
        headers={"content-type": "application/json"},
    )
    with caplog.at_level(logging.INFO, logger="bff"):
        response = client.get("/memory/episodic/x", headers={"Authorization": _good()})

    assert response.status_code == 404
    assert response.content == b'{"detail":"Not found"}'
    assert "www-authenticate" not in response.headers
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
