"""LC10A-CHAT-EXCHANGE-SCOPE: the chat-turn token exchange is least privilege.

The token the BFF forwards to ``/v1/chat/completions`` must carry exactly the
scopes the chat path can use (the route's own scope plus every memory tool's
``required_scope``) and no ``:write`` scope.

The expected set is DERIVED from the server code, never typed here. The fake
Keycloak is a model of the legacy (v1) token exchange as measured on a
throwaway Keycloak 24.0.5 (spec section 2, cases K1-K12); its client table is
read from ``keycloak/realm-audittrace.json``.

ADDENDUM-1 (gate r1): a refused grant writes no ``granted=`` line (S1); the
grant is bounded by the caller's own scopes, measured K19-K24 (S2); the chat
audience client pins ``fullScopeAllowed: false`` (S3).

ADDENDUM-2 (gate r2): a caller whose own token lacks a required scope is
refused BEFORE any exchange with an explicit HTTP 403 (RFC 6750 section 3.1)
and its own log event (B1); a genuine Keycloak over-grant still answers 502.
An empty chat audience never falls back to the login audience (A2).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import yaml
from fastapi.params import Security
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jose import jwt
from pydantic import ValidationError

import audittrace.tools.memory_handlers  # noqa: F401  (registers the memory tools)
from audittrace.routes.chat import _openai_error_body
from audittrace.routes.chat import router as chat_router
from audittrace.tools import MEMORY_TOOL_REGISTRY
from bff.app import create_app, get_http_client
from bff.config import Settings, get_settings
from bff.exchange import TokenExchangeError, exchange_token
from tests.bff.conftest import (
    TEST_ISSUER,
    TEST_PRIVATE_PEM,
    TEST_PUBLIC_PEM,
    make_token,
)
from tests.bff.test_app import (
    KEYCLOAK_JWKS_URL,
    KEYCLOAK_TOKEN_URL,
    ORCHESTRATOR_BASE,
    _settings,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CHAT_AUDIENCE = "audittrace-librechat-chat"
LOGIN_CLIENT = "audittrace-librechat"
RESTRICTED_CLIENT = "audittrace-restricted"
CHAT_BODY = b'{"model":"m","messages":[{"role":"user","content":"hi"}],"stream":false}'


def _chat_route_scopes() -> set[str]:
    routes = [
        r
        for r in chat_router.routes
        if isinstance(r, APIRoute)
        and r.path.endswith("/chat/completions")
        and "POST" in r.methods  # type: ignore[operator]
    ]
    assert len(routes) == 1, [r.path for r in routes]
    scopes = {
        scope
        for param in inspect.signature(routes[0].endpoint).parameters.values()
        if isinstance(param.default, Security)
        for scope in param.default.scopes  # type: ignore[union-attr]
    }
    assert scopes, "the chat route declares no Security scope"
    return scopes


def expected_chat_scopes() -> list[str]:
    tool_scopes = {tool.required_scope for tool in MEMORY_TOOL_REGISTRY.values()}
    assert len(MEMORY_TOOL_REGISTRY) >= 7, sorted(MEMORY_TOOL_REGISTRY)
    return sorted(_chat_route_scopes() | tool_scopes)


def _realm_clients() -> dict[str, dict[str, set[str]]]:
    realm = json.loads(
        (REPO_ROOT / "keycloak" / "realm-audittrace.json").read_text(encoding="utf-8")
    )
    return {
        c["clientId"]: {
            "default": set(c.get("defaultClientScopes", [])),
            "optional": set(c.get("optionalClientScopes", [])),
        }
        for c in realm["clients"]
    }


def login_token_scopes() -> set[str]:
    """What a LibreChat login token carries: the login client's defaults plus
    every requested scope it offers (``console.librechat.openidScope``)."""
    values = yaml.safe_load(
        (REPO_ROOT / "charts" / "audittrace" / "values.yaml").read_text("utf-8")
    )
    requested = set(values["console"]["librechat"]["openidScope"].split())
    client = _realm_clients()[LOGIN_CLIENT]
    return client["default"] | (requested & (client["optional"] | {"openid"}))


def restricted_token_scopes() -> set[str]:
    """What an ``audittrace-restricted`` (SC-09) token carries: that client's
    defaults plus ``openid`` (measured: ADDENDUM-1 subject table)."""
    return _realm_clients()[RESTRICTED_CLIENT]["default"] | {"openid"}


def _granted_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("token_exchange.scopes granted=")
    ]


CALLER_EVENT = "chat.caller_scope_insufficient"


def _event_lines(caplog: pytest.LogCaptureFixture, event: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(event)]


def expected_insufficient_scope_body(missing: list[str]) -> dict[str, object]:
    """The explicit refusal, built by the SERVER's own error-body helper (the
    reference the BFF image cannot import): the shape of the #445 explicit error."""
    named = ", ".join(f"'{scope}'" for scope in missing)
    return _openai_error_body(  # type: ignore[no-any-return]
        message=(
            "This sign-in cannot use chat: its access token lacks the required "
            f"scope(s) {named}. Sign out and sign in again. If this message "
            "returns, ask an administrator to grant the scope(s) to this client."
        ),
        code="insufficient_scope",
        type_="invalid_request_error",
        status=403,
        missing_scopes=missing,
    )


def _callers_lacking_a_required_scope() -> dict[str, set[str] | None]:
    """Three caller tokens that lack a required scope. ``None`` = the token
    carries no ``scope`` claim at all."""
    required = expected_chat_scopes()
    return {
        "restricted-client": restricted_token_scopes(),
        "one-scope-short": set(required[:-1]),
        "no-scope-claim": None,
    }


class Keycloak24AndOrchestrator:
    """One MockTransport handler: JWKS, the token endpoint, the orchestrator."""

    def __init__(self) -> None:
        self.clients = _realm_clients()
        self.exchange_forms: list[dict[str, list[str]]] = []
        self.orchestrator_requests: list[httpx.Request] = []
        self.grant_override: set[str] | None = None

    def _grant(self, form: dict[str, list[str]]) -> set[str] | None:
        subject = set(
            jwt.get_unverified_claims(form["subject_token"][0]).get("scope", "").split()
        )
        audience = self.clients.get(form.get("audience", [""])[0])
        if audience is None:
            return None
        effective = set(form["scope"][0].split()) if "scope" in form else subject
        return (
            audience["default"]
            | (effective & subject & audience["optional"])
            | (effective & {"openid"})
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == KEYCLOAK_JWKS_URL:
            return httpx.Response(200, json={"keys": [TEST_PUBLIC_PEM]})
        if url == KEYCLOAK_TOKEN_URL:
            form = parse_qs(request.read().decode())
            self.exchange_forms.append(form)
            granted = self.grant_override or self._grant(form)
            if granted is None:
                return httpx.Response(400, json={"error": "invalid_client"})
            now = int(time.time())
            minted = jwt.encode(
                {
                    "iss": TEST_ISSUER,
                    "sub": jwt.get_unverified_claims(form["subject_token"][0])["sub"],
                    "aud": "audittrace-server",
                    "scope": " ".join(sorted(granted)),
                    "iat": now,
                    "exp": now + 300,
                },
                TEST_PRIVATE_PEM,
                algorithm="RS256",
            )
            return httpx.Response(200, json={"access_token": minted})
        if url == f"{ORCHESTRATOR_BASE}/v1/chat/completions":
            self.orchestrator_requests.append(request)
            return httpx.Response(
                200,
                content=b'{"id":"chatcmpl-1","choices":[]}',
                headers={"content-type": "application/json"},
            )
        raise AssertionError(f"unexpected upstream call: {url}")

    def forwarded_scopes(self) -> list[str]:
        assert len(self.orchestrator_requests) == 1
        bearer = self.orchestrator_requests[0].headers["authorization"]
        assert bearer.startswith("Bearer ")
        claims = jwt.get_unverified_claims(bearer.removeprefix("Bearer "))
        return sorted(claims["scope"].split())


@pytest.fixture
def chat() -> Iterator[tuple[TestClient, Keycloak24AndOrchestrator]]:
    backend = Keycloak24AndOrchestrator()
    app = create_app()
    app.dependency_overrides[get_settings] = _settings
    with contextlib.ExitStack() as stack:
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
        stack.callback(lambda: asyncio.run(upstream.aclose()))
        app.dependency_overrides[get_http_client] = lambda: upstream
        yield stack.enter_context(TestClient(app)), backend


def _chat_turn(
    client: TestClient, subject_scopes: set[str] | None = None
) -> httpx.Response:
    scopes = login_token_scopes() if subject_scopes is None else subject_scopes
    login = make_token(sub="alice", extra_claims={"scope": " ".join(sorted(scopes))})
    return _post_chat(client, login)


def _post_chat(client: TestClient, login: str) -> httpx.Response:
    return client.post(  # type: ignore[return-value]
        "/v1/chat/completions",
        content=CHAT_BODY,
        headers={
            "Authorization": f"Bearer {login}",
            "Content-Type": "application/json",
        },
    )


class TestTheFixtureIsTheMeasuredDefect:
    """Positive controls: the login token really carries write scopes, so a
    green result below cannot come from a subject token that had none."""

    def test_the_login_token_carries_the_twelve_write_scopes(self) -> None:
        writes = sorted(s for s in login_token_scopes() if s.endswith(":write"))
        assert len(writes) == 12, writes
        assert "memory:episodic:write" in writes
        assert "memory:session:write" in writes

    def test_the_expected_set_is_the_route_scope_plus_the_tool_scopes(self) -> None:
        expected = expected_chat_scopes()
        assert "audittrace:query" in expected
        assert not [s for s in expected if s.endswith(":write")]


class TestChatExchangeIsLeastPrivilege:
    def test_the_forwarded_token_carries_exactly_the_chat_scopes(
        self, chat: tuple[TestClient, Keycloak24AndOrchestrator]
    ) -> None:
        client, backend = chat
        assert _chat_turn(client).status_code == 200
        assert backend.forwarded_scopes() == expected_chat_scopes()

    def test_the_forwarded_token_carries_no_write_scope(
        self, chat: tuple[TestClient, Keycloak24AndOrchestrator]
    ) -> None:
        client, backend = chat
        assert _chat_turn(client).status_code == 200
        assert [s for s in backend.forwarded_scopes() if s.endswith(":write")] == []

    def test_the_exchange_requests_the_chat_scopes_by_name(
        self, chat: tuple[TestClient, Keycloak24AndOrchestrator]
    ) -> None:
        client, backend = chat
        _chat_turn(client)
        assert len(backend.exchange_forms) == 1
        assert backend.exchange_forms[0].get("scope") == [
            " ".join(expected_chat_scopes())
        ]

    def test_the_exchange_targets_the_chat_audience(
        self, chat: tuple[TestClient, Keycloak24AndOrchestrator]
    ) -> None:
        client, backend = chat
        _chat_turn(client)
        assert backend.exchange_forms[0]["audience"] == [CHAT_AUDIENCE]

    def test_the_granted_log_line_names_exactly_the_chat_scopes(
        self,
        chat: tuple[TestClient, Keycloak24AndOrchestrator],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, _ = chat
        with caplog.at_level(logging.INFO, logger="bff.exchange"):
            _chat_turn(client)
        lines = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("token_exchange.")
        ]
        assert lines == [
            "token_exchange.scopes granted=" + ",".join(expected_chat_scopes())
        ]

    def test_the_request_body_reaches_v1_byte_identical(
        self, chat: tuple[TestClient, Keycloak24AndOrchestrator]
    ) -> None:
        client, backend = chat
        _chat_turn(client)
        assert len(backend.orchestrator_requests) == 1
        assert backend.orchestrator_requests[0].content == CHAT_BODY


class TestAnOverGrantFailsClosed:
    """Keycloak answers 200 with more than the chat ceiling (the defect, or a
    realm drift): the BFF must not forward that token."""

    @pytest.mark.parametrize(
        "extra", ["memory:episodic:write", "audittrace:admin", "memory:files:read-own"]
    )
    def test_a_scope_beyond_the_ceiling_is_refused_before_v1(
        self,
        chat: tuple[TestClient, Keycloak24AndOrchestrator],
        caplog: pytest.LogCaptureFixture,
        extra: str,
    ) -> None:
        client, backend = chat
        backend.grant_override = set(expected_chat_scopes()) | {extra}
        with caplog.at_level(logging.INFO, logger="bff"):
            response = _chat_turn(client)
        assert response.status_code == 502
        assert response.json() == {"detail": "Upstream authentication service error"}
        assert backend.orchestrator_requests == []
        assert f"unexpected={extra}" in caplog.text
        assert _granted_lines(caplog) == []
        assert len(backend.exchange_forms) == 1
        assert _event_lines(caplog, CALLER_EVENT) == []

    @pytest.mark.parametrize("protocol_scope", ["openid", "offline_access"])
    def test_an_oidc_protocol_scope_is_not_an_over_grant(
        self, chat: tuple[TestClient, Keycloak24AndOrchestrator], protocol_scope: str
    ) -> None:
        client, backend = chat
        backend.grant_override = set(expected_chat_scopes()) | {protocol_scope}
        assert _chat_turn(client).status_code == 200
        assert len(backend.orchestrator_requests) == 1


class TestTheGrantIsBoundedByTheCaller:
    """Keycloak mints the audience client's defaults whatever the subject
    token holds (K19, K23). The BFF must not forward a scope the caller's
    own token does not carry."""

    def test_the_restricted_client_really_lacks_chat_scopes(self) -> None:
        subject = restricted_token_scopes()
        assert "audittrace:query" in subject
        assert len(set(expected_chat_scopes()) - subject) >= 1

    @pytest.mark.parametrize(
        "caller", ["restricted-client", "one-scope-short", "no-scope-claim"]
    )
    def test_a_caller_lacking_a_required_scope_gets_an_explicit_403_and_no_exchange(
        self,
        chat: tuple[TestClient, Keycloak24AndOrchestrator],
        caplog: pytest.LogCaptureFixture,
        caller: str,
    ) -> None:
        client, backend = chat
        held = _callers_lacking_a_required_scope()[caller]
        required = expected_chat_scopes()
        missing = sorted(set(required) - (held or set()))
        assert missing, "the fixture caller lacks nothing"
        claims = None if held is None else {"scope": " ".join(sorted(held))}
        with caplog.at_level(logging.INFO, logger="bff"):
            response = _post_chat(client, make_token(sub="alice", extra_claims=claims))
        assert response.status_code == 403
        assert response.json() == expected_insufficient_scope_body(missing)
        assert response.headers["www-authenticate"] == (
            'Bearer error="insufficient_scope", scope="' + " ".join(required) + '"'
        )
        assert backend.exchange_forms == []
        assert backend.orchestrator_requests == []
        assert _granted_lines(caplog) == []
        assert _event_lines(caplog, CALLER_EVENT) == [
            f"{CALLER_EVENT} sub=alice missing=" + ",".join(missing)
        ]
        assert "scope ceiling exceeded" not in caplog.text

    def test_a_caller_holding_exactly_the_chat_scopes_is_served(
        self, chat: tuple[TestClient, Keycloak24AndOrchestrator]
    ) -> None:
        client, backend = chat
        response = _chat_turn(client, set(expected_chat_scopes()))
        assert response.status_code == 200
        assert backend.forwarded_scopes() == expected_chat_scopes()


class TestTheChatAudienceHasNoFallback:
    """Spec ruling (c), gate r2 A2: an empty chat audience must never turn
    into the login audience (``audience or settings.exchange_audience``)."""

    def test_an_empty_chat_audience_setting_fails_at_startup(self) -> None:
        with pytest.raises(ValidationError):
            Settings(exchange_client_secret="s3cr3t", chat_exchange_audience="")

    def test_an_explicit_empty_audience_is_sent_as_given(self) -> None:
        backend = Keycloak24AndOrchestrator()
        login = make_token(
            sub="alice",
            extra_claims={"scope": " ".join(sorted(login_token_scopes()))},
        )

        async def exchange() -> None:
            transport = httpx.MockTransport(backend.handler)
            async with httpx.AsyncClient(transport=transport) as upstream:
                await exchange_token(login, "alice", _settings(), upstream, audience="")

        with pytest.raises(TokenExchangeError):
            asyncio.run(exchange())
        assert len(backend.exchange_forms) == 1
        assert backend.exchange_forms[0].get("audience", [""]) == [""]


class TestTheBffConstantAndTheRealmMatchTheServer:
    def test_bff_chat_scopes_equal_the_derived_set(self) -> None:
        from bff.chat_scopes import CHAT_SCOPE_STRING, CHAT_SCOPES

        assert sorted(CHAT_SCOPES) == expected_chat_scopes()
        assert len(set(CHAT_SCOPES)) == len(CHAT_SCOPES)
        assert CHAT_SCOPE_STRING == " ".join(CHAT_SCOPES)

    def test_the_realm_chat_audience_defaults_are_exactly_the_chat_scopes(self) -> None:
        client = _realm_clients().get(CHAT_AUDIENCE)
        assert client is not None, f"{CHAT_AUDIENCE} is not in the realm file"
        assert sorted(client["default"]) == expected_chat_scopes()
        assert client["optional"] == set()

    def test_the_realm_chat_audience_mints_no_role_claims(self) -> None:
        realm = json.loads(
            (REPO_ROOT / "keycloak" / "realm-audittrace.json").read_text("utf-8")
        )
        raw = [c for c in realm["clients"] if c["clientId"] == CHAT_AUDIENCE]
        assert len(raw) == 1, f"{CHAT_AUDIENCE} is not in the realm file"
        assert raw[0].get("fullScopeAllowed") is False
