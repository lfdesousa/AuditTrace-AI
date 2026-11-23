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

ADDENDUM-3 (gate S2): the malformed-JWKS shapes are pinned on EVERY proxy
route again (ADDENDUM-2's harness had left them on the chat route only):
``jwks_not_an_object``, ``jwks_no_signing_key``, ``jwks_key_malformed`` and the
three shapes of the SECOND fetch (the minted-token re-validation). Neuters,
each alone, in the shared path: R8 drop the JWKS object guard of
``bff/auth.py`` -> the ``jwks_not_an_object`` and ``jwks_second_not_an_object``
cells of all 19 routes; R9 drop ``ValueError`` from the inbound decode's arm ->
``jwks_key_malformed`` x 19; R10 drop the re-validation's JWKS arm ->
``jwks_second_fetch`` x 19; R11 treat a JWKS without a signing key as "no key
for this kid" -> ``jwks_no_signing_key`` x 19.
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
import httpx2
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jose import jwt

from bff import chat_refusals
from bff.app import create_app, get_http_client
from bff.chat_refusal import ChatRefusal
from bff.config import get_settings
from tests.bff.conftest import (
    TEST_ISSUER,
    TEST_JWK,
    TEST_KID,
    TEST_PRIVATE_PEM,
    make_token,
)
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
    "/console/evidence": "console_evidence",
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
        methods = route.methods or set()
        method = "POST" if "POST" in methods else sorted(methods)[0]
        path = (
            route.path.replace("{path:path}", "x")
            .replace("{filename}", "n.txt")
            .replace("{client_message_id}", "m-1")
            .replace("{interaction_id:int}", "1")
        )
        found[f"{method} {route.path}"] = (method, path, label)
    return found


ROUTES = _proxy_routes()


def test_the_app_has_the_twentyone_proxy_routes() -> None:
    """The cells below would be vacuous over a wrong route list. (19 + the two
    GET-only evidence routes of CONSOLE-AUDIT-EVIDENCE WU-C.)"""
    assert len(ROUTES) == 21
    assert {label for _m, _p, label in ROUTES.values()} == set(LABELS.values())


class _Upstreams:
    """Keycloak and the memory server behind one ``MockTransport``."""

    def __init__(self) -> None:
        self.jwks: Respond | None = None
        self.exchange: Respond | None = None
        self.memory_server: Respond | None = None
        self.jwks_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == KEYCLOAK_JWKS_URL:
            self.jwks_calls += 1
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
    # ``audittrace:audit``: the evidence routes' caller-scope pre-check (WU-C)
    # refuses a token without it before any exchange; no other route reads it.
    return "Bearer " + make_token(
        sub="alice", extra_claims={"scope": "audittrace:audit"}
    )


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

# ── ADDENDUM-3 (gate S2): malformed JWKS, per route ─────────────────────────
# A realm key python-jose cannot build for RS256 (an Ed25519 key), and an RSA
# entry that names the token's kid but carries no key material.
_OKP_ONLY = {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": "okp-1", "x": "AA"}]}
_MALFORMED = {"keys": [{"kty": "RSA", "kid": TEST_KID}]}


def _jwks_answers(body: Any) -> Respond:
    return lambda _request: httpx.Response(200, json=body)


class _FromSecondFetch:
    """Answers the first JWKS fetch well and the later ones with ``respond``."""

    def __init__(self, respond: Respond) -> None:
        self._respond = respond
        self._calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self._calls += 1
        if self._calls >= 2:
            return self._respond(request)
        return httpx.Response(200, json={"keys": [TEST_JWK]})


FAILURES.update(
    {
        "jwks_not_an_object": (
            _good,
            "jwks",
            _jwks_answers([]),
            "auth_service_unavailable",
            "-",
        ),
        "jwks_no_signing_key": (
            _good,
            "jwks",
            _jwks_answers(_OKP_ONLY),
            "auth_service_unavailable",
            "-",
        ),
        "jwks_key_malformed": (
            _good,
            "jwks",
            _jwks_answers(_MALFORMED),
            "auth_service_unavailable",
            "-",
        ),
    }
)
# The second fetch happens in the minted-token re-validation: the caller is
# verified by then, so the event carries the user.
SECOND_FETCH: dict[str, Respond] = {
    "jwks_second_fetch": _raises(httpx.ConnectError("refused")),
    "jwks_second_not_an_object": _jwks_answers([]),
    "jwks_second_no_signing_key": _jwks_answers(_OKP_ONLY),
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
        # TTL 0: every key lookup fetches, so the second fetch (the
        # minted-token re-validation) is reachable.
        app.dependency_overrides[get_settings] = lambda: _settings().model_copy(
            update={"jwks_cache_ttl_seconds": 0}
        )
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
    response: httpx2.Response,
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


@pytest.mark.parametrize("shape", sorted(SECOND_FETCH))
@pytest.mark.parametrize("route", sorted(ROUTES))
def test_route_second_jwks_fetch(
    bff: tuple[TestClient, _Upstreams],
    route: str,
    shape: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The key set goes bad between the inbound check and the minted-token
    re-validation: 502 in the one shape on every route, never an escape."""
    client, upstreams = bff
    method, path, label = ROUTES[route]
    upstreams.jwks = _FromSecondFetch(SECOND_FETCH[shape])
    with caplog.at_level(logging.INFO, logger="bff"):
        response = client.request(
            method, path, headers={**HEADERS, "Authorization": _good()}, content=b"{}"
        )

    _assert_refusal(
        response, caplog, label=label, code="auth_service_unavailable", sub="alice"
    )
    assert upstreams.jwks_calls >= 2


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
        if isinstance(refusal, ChatRefusal)
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
