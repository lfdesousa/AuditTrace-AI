"""End-to-end tests for ``POST /console/files/{filename}/promote`` — the
"keep this" durable-promote route (M3 Sovereign-Attach WU-4).

No real Keycloak, no real orchestrator: a single ``httpx.MockTransport``
handler plays BOTH the Keycloak realm (JWKS + token-exchange) and the
orchestrator's ``/memory/promote`` endpoint, routed by URL — same
technique ``tests/bff/test_console_files.py`` uses.

The spec's non-vacuous guards, each with its own test class:

* ``TestExactDurableScope`` — the exchange sends ``scope`` EXACTLY the
  configured target layer's durable write scope — never
  ``memory:session:write``, never the broad Souvenirs set. Neuter
  ``bff/app.py::console_files_promote``'s ``requested_scope=`` (e.g.
  swap it for ``INGEST_SCOPE_STRING``) and
  ``test_exchange_requests_exactly_the_durable_scope`` goes RED.
* ``TestForcedTargetLayer`` — the upstream ``/memory/promote`` request
  body always carries the CONFIGURED ``target_layer`` — a caller cannot
  override it from this seam (there is no field in the request for one).
* ``TestByteExactBody`` — the JSON body sent upstream is exactly
  ``{"filename": <path param>, "target_layer": <configured>}``, nothing
  from the caller's own request body leaks through.
* ``TestIdentityPropagation`` — two distinct callers produce two
  distinct minted subs at the orchestrator boundary.
* ``TestFailClosed`` — absent/invalid token never reaches Keycloak or
  the orchestrator; a Keycloak failure is 502; a 4xx the orchestrator
  itself returns (403 scope gate, 404 not-owned, 422 bad target) is
  relayed byte-for-byte, never manufactured into a 200.
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
from bff.console_files_scopes import INGEST_SCOPE_STRING
from bff.console_promote_scopes import promote_scope_string_for_layer
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
        "orchestrator_memory_path_prefix": "/memory",
        "proxy_source_label": "librechat",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _minted_token_for(sub: str, scope: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": TEST_ISSUER,
            "sub": sub,
            "aud": "audittrace-server",
            "scope": scope,
            "iat": now,
            "exp": now + 300,
        },
        TEST_PRIVATE_PEM,
        algorithm="RS256",
    )


def _sub_and_scope_from_form_body(body: str) -> tuple[str, str | None]:
    parsed = parse_qs(body)
    subject_token = parsed["subject_token"][0]
    claims = jwt.get_unverified_claims(subject_token)
    return claims["sub"], (parsed.get("scope") or [None])[0]


class _ConsolePromoteBackend:
    """Routes a single MockTransport handler across Keycloak JWKS +
    token-exchange and the orchestrator's ``/memory/promote`` endpoint.
    Records every orchestrator call AND the raw form body of every
    token-exchange request."""

    def __init__(self) -> None:
        self.orchestrator_calls: list[httpx.Request] = []
        self.exchange_request_bodies: list[str] = []
        self.orchestrator_response = httpx.Response(
            200,
            json={
                "status": "promoted",
                "target_layer": "episodic",
                "key": "notes.txt.md",
            },
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
            sub, scope = _sub_and_scope_from_form_body(body)
            minted = _minted_token_for(sub, scope or "")
            return httpx.Response(
                200, json={"access_token": minted, "token_type": "Bearer"}
            )
        if url.startswith(f"{ORCHESTRATOR_BASE}/memory/promote"):
            self.orchestrator_calls.append(request)
            return self.orchestrator_response
        raise AssertionError(f"unexpected upstream call: {url}")


@pytest.fixture
def console_promote_client():
    from fastapi.testclient import TestClient

    app = create_app()
    backend = _ConsolePromoteBackend()
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
    app.dependency_overrides[get_settings] = lambda: _settings()
    app.dependency_overrides[get_http_client] = lambda: mock_client
    with TestClient(app) as client:
        yield client, backend


class TestExactDurableScope:
    """Deliverable: the exchange sends ``scope`` EXACTLY the configured
    target layer's durable write scope — never the session scope, never
    the broad Souvenirs set."""

    def test_exchange_requests_exactly_the_durable_scope(
        self, console_promote_client
    ) -> None:
        client, backend = console_promote_client
        token = make_token(sub="alice")
        client.post(
            "/console/files/notes.txt/promote",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert len(backend.exchange_request_bodies) == 1
        parsed = parse_qs(backend.exchange_request_bodies[0])
        assert parsed["scope"] == [promote_scope_string_for_layer("episodic")]
        assert parsed["scope"] == ["memory:episodic:write"]

    def test_promote_scope_is_never_session_or_broad(
        self, console_promote_client
    ) -> None:
        client, backend = console_promote_client
        token = make_token(sub="alice")
        client.post(
            "/console/files/notes.txt/promote",
            headers={"Authorization": f"Bearer {token}"},
        )
        parsed = parse_qs(backend.exchange_request_bodies[0])
        requested_scopes = parsed["scope"][0].split(" ")
        assert requested_scopes == ["memory:episodic:write"]
        assert "memory:session:write" not in requested_scopes
        assert "audittrace:admin" not in requested_scopes
        assert requested_scopes != [INGEST_SCOPE_STRING]

    def test_configured_target_layer_changes_the_requested_scope(self) -> None:
        """The scope is a FUNCTION of config, not a hardcoded literal —
        override ``console_promote_default_layer`` and the requested
        scope follows."""
        from fastapi.testclient import TestClient

        app = create_app()
        backend = _ConsolePromoteBackend()
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
        app.dependency_overrides[get_settings] = lambda: _settings(
            console_promote_default_layer="semantic"
        )
        app.dependency_overrides[get_http_client] = lambda: mock_client
        with TestClient(app) as client:
            token = make_token(sub="alice")
            client.post(
                "/console/files/notes.txt/promote",
                headers={"Authorization": f"Bearer {token}"},
            )
        parsed = parse_qs(backend.exchange_request_bodies[0])
        assert parsed["scope"] == ["memory:semantic:write"]


class TestForcedTargetLayer:
    """Deliverable: the upstream request body always carries the
    CONFIGURED ``target_layer`` — there is no request field for the
    caller to override it with."""

    def test_default_target_layer_is_episodic(self, console_promote_client) -> None:
        client, backend = console_promote_client
        token = make_token(sub="alice")
        client.post(
            "/console/files/notes.txt/promote",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert len(backend.orchestrator_calls) == 1
        sent_body = json.loads(backend.orchestrator_calls[0].read())
        assert sent_body["target_layer"] == "episodic"

    def test_configured_target_layer_is_forwarded(self) -> None:
        from fastapi.testclient import TestClient

        app = create_app()
        backend = _ConsolePromoteBackend()
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
        app.dependency_overrides[get_settings] = lambda: _settings(
            console_promote_default_layer="semantic"
        )
        app.dependency_overrides[get_http_client] = lambda: mock_client
        with TestClient(app) as client:
            token = make_token(sub="alice")
            client.post(
                "/console/files/notes.txt/promote",
                headers={"Authorization": f"Bearer {token}"},
            )
        sent_body = json.loads(backend.orchestrator_calls[0].read())
        assert sent_body["target_layer"] == "semantic"


class TestByteExactBody:
    """Deliverable: the upstream JSON body is exactly ``{"filename":
    <path param>, "target_layer": <configured>}`` — nothing from the
    caller's own request body (if any) leaks through."""

    def test_upstream_body_is_exactly_filename_and_target_layer(
        self, console_promote_client
    ) -> None:
        client, backend = console_promote_client
        token = make_token(sub="alice")
        # A caller-supplied body — must be IGNORED entirely; the route
        # takes no request body at all.
        client.post(
            "/console/files/my-note.txt/promote",
            json={"target_layer": "semantic", "promoted_by": "mallory"},
            headers={"Authorization": f"Bearer {token}"},
        )
        sent_body = json.loads(backend.orchestrator_calls[0].read())
        assert sent_body == {"filename": "my-note.txt", "target_layer": "episodic"}

    def test_upstream_content_type_is_json(self, console_promote_client) -> None:
        client, backend = console_promote_client
        token = make_token(sub="alice")
        client.post(
            "/console/files/notes.txt/promote",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert (
            backend.orchestrator_calls[0].headers["content-type"] == "application/json"
        )

    def test_orchestrator_response_body_returned_unchanged(
        self, console_promote_client
    ) -> None:
        client, backend = console_promote_client
        token = make_token(sub="alice")
        response = client.post(
            "/console/files/notes.txt/promote",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "status": "promoted",
            "target_layer": "episodic",
            "key": "notes.txt.md",
        }


class TestIdentityPropagation:
    def test_two_callers_produce_two_distinct_minted_subs(
        self, console_promote_client
    ) -> None:
        client, backend = console_promote_client
        alice_token = make_token(sub="alice")
        bob_token = make_token(sub="bob")

        client.post(
            "/console/files/notes.txt/promote",
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        client.post(
            "/console/files/notes.txt/promote",
            headers={"Authorization": f"Bearer {bob_token}"},
        )

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
        self, console_promote_client
    ) -> None:
        client, backend = console_promote_client
        response = client.post("/console/files/notes.txt/promote")
        assert response.status_code == 401
        assert backend.orchestrator_calls == []
        assert backend.exchange_request_bodies == []

    def test_expired_token_401_never_reaches_orchestrator(
        self, console_promote_client
    ) -> None:
        client, backend = console_promote_client
        token = make_token(sub="alice", exp_offset=-3600)
        response = client.post(
            "/console/files/notes.txt/promote",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401
        assert backend.orchestrator_calls == []
        assert backend.exchange_request_bodies == []

    def test_garbage_token_401_never_reaches_orchestrator(
        self, console_promote_client
    ) -> None:
        client, backend = console_promote_client
        response = client.post(
            "/console/files/notes.txt/promote",
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        assert response.status_code == 401
        assert backend.orchestrator_calls == []

    def test_keycloak_exchange_failure_502_never_reaches_orchestrator(
        self, console_promote_client
    ) -> None:
        client, backend = console_promote_client
        backend.keycloak_down = True
        token = make_token(sub="alice")
        response = client.post(
            "/console/files/notes.txt/promote",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 502
        assert backend.orchestrator_calls == []

    @pytest.mark.parametrize("upstream_status", [403, 404, 422])
    def test_orchestrator_denial_forwarded_verbatim_not_manufactured(
        self, console_promote_client, upstream_status: int
    ) -> None:
        """The load-bearing fail-closed guard: even with a VALID,
        successfully-exchanged token, a denial from the orchestrator's
        own gate (403 scope, 404 not-owned, 422 bad target_layer) must
        reach the caller unchanged."""
        client, backend = console_promote_client
        backend.orchestrator_response = httpx.Response(
            upstream_status,
            json={"detail": "denied"},
            headers={"content-type": "application/json"},
        )
        token = make_token(sub="alice")
        response = client.post(
            "/console/files/notes.txt/promote",
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
                minted = _minted_token_for("alice", "memory:episodic:write")
                return httpx.Response(200, json={"access_token": minted})
            raise httpx.ConnectError("connection refused", request=request)

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.dependency_overrides[get_settings] = lambda: _settings()
        app.dependency_overrides[get_http_client] = lambda: mock_client
        with TestClient(app) as client:
            token = make_token(sub="alice")
            response = client.post(
                "/console/files/notes.txt/promote",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 502


class TestConsoleFilesPromoteMethodIsPostOnly:
    @pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
    def test_other_methods_not_allowed(
        self, console_promote_client, method: str
    ) -> None:
        client, _backend = console_promote_client
        response = client.request(method, "/console/files/notes.txt/promote")
        assert response.status_code == 405
