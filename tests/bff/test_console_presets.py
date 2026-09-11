"""End-to-end tests for ``/console/presets[/*]`` — the console-presets
proxy (Mongo-repl WU-presets, MongoDB-elimination EPIC).

Mirrors ``tests/bff/test_console_conversations.py``'s structure EXACTLY
(the spec's instruction), scaled down to a single resource (no message
tree). Same MockTransport technique: one handler plays Keycloak (JWKS +
token-exchange) AND the orchestrator's ``/console/presets`` mount,
routed by URL.

Guard classes:

* ``TestExactScopePair`` — the exchange sends ``scope`` EXACTLY the two
  presets scopes, never the broad memory-proxy set or the ingest scope.
* ``TestPathForwarding`` — the base path (no suffix) and nested path
  (``{preset_id}``) reach the orchestrator at the correct URL, with the
  query string preserved for the list route.
* ``TestIdentityPropagation`` — two distinct callers mint two distinct
  subs at the orchestrator boundary.
* ``TestFailClosed`` — absent/invalid token never reaches Keycloak or the
  orchestrator; Keycloak failure is 502; the orchestrator's own 4xx is
  relayed byte-for-byte.
* ``TestByteFaithfulForward`` — the JSON body reaches the orchestrator
  unchanged, and the orchestrator's response is returned unchanged.
"""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs

import httpx
import pytest
from jose import jwt

from bff.app import create_app, get_http_client
from bff.config import Settings, get_settings
from bff.console_presets_scopes import CONSOLE_PRESETS_SCOPE_STRING
from bff.memory_scopes import MEMORY_SCOPE_STRING
from tests.bff.conftest import (
    TEST_ISSUER,
    TEST_PRIVATE_PEM,
    TEST_PUBLIC_PEM,
    make_token,
)

KEYCLOAK_JWKS_URL = (
    "http://keycloak:8080/realms/audittrace/protocol/openid-connect/certs"
)
KEYCLOAK_TOKEN_URL = (
    "http://keycloak:8080/realms/audittrace/protocol/openid-connect/token"
)
ORCHESTRATOR_BASE = "http://orchestrator:8765"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "exchange_client_secret": "s3cr3t",
        "keycloak_issuer": TEST_ISSUER,
        "keycloak_issuer_extras": [],
        "keycloak_jwks_url": KEYCLOAK_JWKS_URL,
        "keycloak_token_url": KEYCLOAK_TOKEN_URL,
        "exchange_client_id": "audittrace-librechat-bff",
        "exchange_audience": "audittrace-librechat",
        "orchestrator_base_url": ORCHESTRATOR_BASE,
        "orchestrator_console_presets_path_prefix": "/console/presets",
        "proxy_source_label": "librechat",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _minted_token_for(sub: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": TEST_ISSUER,
            "sub": sub,
            "aud": "audittrace-server",
            "scope": CONSOLE_PRESETS_SCOPE_STRING,
            "iat": now,
            "exp": now + 300,
        },
        TEST_PRIVATE_PEM,
        algorithm="RS256",
    )


def _sub_from_form_body(body: str) -> str:
    parsed = parse_qs(body)
    subject_token = parsed["subject_token"][0]
    claims = jwt.get_unverified_claims(subject_token)
    return claims["sub"]


class _ConsolePresetsBackend:
    def __init__(self) -> None:
        self.orchestrator_calls: list[httpx.Request] = []
        self.exchange_request_bodies: list[str] = []
        self.orchestrator_response = httpx.Response(
            200,
            json={"preset_id": "preset-1"},
            headers={"content-type": "application/json"},
        )
        self.keycloak_down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == KEYCLOAK_JWKS_URL:
            return httpx.Response(200, json={"keys": [TEST_PUBLIC_PEM]})
        if url == KEYCLOAK_TOKEN_URL:
            body = request.read().decode()
            self.exchange_request_bodies.append(body)
            if self.keycloak_down:
                return httpx.Response(503)
            sub = _sub_from_form_body(body)
            minted = _minted_token_for(sub)
            return httpx.Response(
                200, json={"access_token": minted, "token_type": "Bearer"}
            )
        if url.startswith(f"{ORCHESTRATOR_BASE}/console/presets"):
            self.orchestrator_calls.append(request)
            return self.orchestrator_response
        raise AssertionError(f"unexpected upstream call: {url}")


@pytest.fixture
def console_presets_client():
    from fastapi.testclient import TestClient

    app = create_app()
    backend = _ConsolePresetsBackend()
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
    app.dependency_overrides[get_settings] = lambda: _settings()
    app.dependency_overrides[get_http_client] = lambda: mock_client
    with TestClient(app) as client:
        yield client, backend


class TestExactScopePair:
    def test_exchange_requests_exactly_presets_scope_pair(
        self, console_presets_client
    ) -> None:
        client, backend = console_presets_client
        token = make_token(sub="alice")
        client.get("/console/presets", headers={"Authorization": f"Bearer {token}"})
        assert len(backend.exchange_request_bodies) == 1
        parsed = parse_qs(backend.exchange_request_bodies[0])
        assert parsed["scope"] == [CONSOLE_PRESETS_SCOPE_STRING]
        assert parsed["scope"] != [MEMORY_SCOPE_STRING]

    def test_scope_pair_has_no_admin_no_corpus(self, console_presets_client) -> None:
        client, backend = console_presets_client
        token = make_token(sub="alice")
        client.get("/console/presets", headers={"Authorization": f"Bearer {token}"})
        parsed = parse_qs(backend.exchange_request_bodies[0])
        requested_scopes = parsed["scope"][0].split(" ")
        assert set(requested_scopes) == {
            "memory:presets:read-own",
            "memory:presets:write",
        }
        assert "audittrace:admin" not in requested_scopes
        assert not any(s.startswith("memory:corpus:") for s in requested_scopes)


class TestPathForwarding:
    def test_base_path_no_suffix(self, console_presets_client) -> None:
        client, backend = console_presets_client
        token = make_token(sub="alice")
        client.get("/console/presets", headers={"Authorization": f"Bearer {token}"})
        assert len(backend.orchestrator_calls) == 1
        assert (
            str(backend.orchestrator_calls[0].url)
            == f"{ORCHESTRATOR_BASE}/console/presets"
        )

    def test_base_path_preserves_query_string(self, console_presets_client) -> None:
        client, backend = console_presets_client
        token = make_token(sub="alice")
        client.get(
            "/console/presets?limit=5&cursor=abc",
            headers={"Authorization": f"Bearer {token}"},
        )
        forwarded = str(backend.orchestrator_calls[0].url)
        assert forwarded == (f"{ORCHESTRATOR_BASE}/console/presets?limit=5&cursor=abc")

    def test_preset_id_path(self, console_presets_client) -> None:
        client, backend = console_presets_client
        token = make_token(sub="alice")
        client.get(
            "/console/presets/preset-1",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert (
            str(backend.orchestrator_calls[0].url)
            == f"{ORCHESTRATOR_BASE}/console/presets/preset-1"
        )

    def test_delete_method_forwarded(self, console_presets_client) -> None:
        client, backend = console_presets_client
        token = make_token(sub="alice")
        client.delete(
            "/console/presets/preset-1",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert backend.orchestrator_calls[0].method == "DELETE"


class TestIdentityPropagation:
    def test_two_callers_produce_two_distinct_minted_subs(
        self, console_presets_client
    ) -> None:
        client, backend = console_presets_client
        alice_token = make_token(sub="alice")
        bob_token = make_token(sub="bob")

        client.get(
            "/console/presets", headers={"Authorization": f"Bearer {alice_token}"}
        )
        client.get("/console/presets", headers={"Authorization": f"Bearer {bob_token}"})

        assert len(backend.orchestrator_calls) == 2
        auth_headers = [
            req.headers["authorization"] for req in backend.orchestrator_calls
        ]
        minted_tokens = [h.removeprefix("Bearer ") for h in auth_headers]
        subs = [jwt.get_unverified_claims(t)["sub"] for t in minted_tokens]
        assert subs == ["alice", "bob"]
        assert subs[0] != subs[1]


class TestFailClosed:
    def test_missing_authorization_header_401_never_reaches_orchestrator(
        self, console_presets_client
    ) -> None:
        client, backend = console_presets_client
        response = client.get("/console/presets")
        assert response.status_code == 401
        assert backend.orchestrator_calls == []
        assert backend.exchange_request_bodies == []

    def test_expired_token_401_never_reaches_orchestrator(
        self, console_presets_client
    ) -> None:
        client, backend = console_presets_client
        token = make_token(sub="alice", exp_offset=-3600)
        response = client.get(
            "/console/presets", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
        assert backend.orchestrator_calls == []

    def test_keycloak_exchange_failure_502_never_reaches_orchestrator(
        self, console_presets_client
    ) -> None:
        client, backend = console_presets_client
        backend.keycloak_down = True
        token = make_token(sub="alice")
        response = client.get(
            "/console/presets", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 502
        assert backend.orchestrator_calls == []

    @pytest.mark.parametrize("upstream_status", [400, 403, 404])
    def test_orchestrator_denial_forwarded_verbatim_not_manufactured(
        self, console_presets_client, upstream_status: int
    ) -> None:
        client, backend = console_presets_client
        backend.orchestrator_response = httpx.Response(
            upstream_status,
            json={"detail": "denied"},
            headers={"content-type": "application/json"},
        )
        token = make_token(sub="alice")
        response = client.get(
            "/console/presets/preset-1",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == upstream_status
        assert response.json() == {"detail": "denied"}
        assert len(backend.orchestrator_calls) == 1

    def test_orchestrator_transport_failure_502(self) -> None:
        from fastapi.testclient import TestClient

        app = create_app()

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == KEYCLOAK_JWKS_URL:
                return httpx.Response(200, json={"keys": [TEST_PUBLIC_PEM]})
            if url == KEYCLOAK_TOKEN_URL:
                minted = _minted_token_for("alice")
                return httpx.Response(200, json={"access_token": minted})
            raise httpx.ConnectError("connection refused", request=request)

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.dependency_overrides[get_settings] = lambda: _settings()
        app.dependency_overrides[get_http_client] = lambda: mock_client
        with TestClient(app) as client:
            token = make_token(sub="alice")
            response = client.get(
                "/console/presets", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 502


class TestByteFaithfulForward:
    def test_json_body_forwarded_byte_identical(self, console_presets_client) -> None:
        client, backend = console_presets_client
        token = make_token(sub="alice")
        body = {"preset_id": "preset-1", "title": "Hello"}
        client.post(
            "/console/presets",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert json.loads(backend.orchestrator_calls[0].read()) == body

    def test_orchestrator_response_body_returned_unchanged(
        self, console_presets_client
    ) -> None:
        client, backend = console_presets_client
        token = make_token(sub="alice")
        response = client.get(
            "/console/presets/preset-1",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json() == {"preset_id": "preset-1"}
