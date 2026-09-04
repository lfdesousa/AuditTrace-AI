"""End-to-end tests for ``POST /console/files`` — the narrow-scope
ephemeral file-ingest route (M3 Sovereign-Attach WU-2).

No real Keycloak, no real orchestrator: a single ``httpx.MockTransport``
handler plays BOTH the Keycloak realm (JWKS + token-exchange) and the
orchestrator's ``/memory/upload`` endpoint, routed by URL — same
technique ``tests/bff/test_app.py`` uses for the chat and memory-proxy
routes.

The spec's non-vacuous guards, each with its own test class:

* ``TestExactNarrowScope`` — the exchange sends ``scope`` EXACTLY
  ``memory:session:write``, never ``MEMORY_SCOPE_STRING`` or anything
  else. Neuter ``bff/app.py::console_files``'s
  ``requested_scope=INGEST_SCOPE_STRING`` (e.g. swap it for
  ``MEMORY_SCOPE_STRING``) and ``test_exchange_requests_exactly_ingest_scope_string``
  goes RED.
* ``TestForcedLayer`` — the upstream ``/memory/upload`` request always
  carries ``layer=session`` regardless of what the caller's own query
  string carries. Neuter the forced-layer assignment in
  ``bff/app.py::console_files`` (e.g. read ``request.url.query``
  instead) and ``test_caller_supplied_layer_is_overridden`` goes RED.
* ``TestIdentityPropagation`` — two distinct callers produce two
  distinct minted subs at the orchestrator boundary.
* ``TestFailClosed`` — absent/invalid token never reaches Keycloak or
  the orchestrator; a Keycloak failure is 502; a 4xx the orchestrator
  itself returns (PDF→session refusal, RLS denial, etc.) is relayed
  byte-for-byte, never manufactured into a 200.
* ``TestByteFaithfulForward`` — the multipart body and its
  ``Content-Type`` header reach the orchestrator unchanged.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs

import httpx
import pytest
from jose import jwt

from bff.app import create_app, get_http_client
from bff.config import Settings, get_settings
from bff.console_files_scopes import INGEST_SCOPE_STRING
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
        "orchestrator_memory_path_prefix": "/memory",
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
            "scope": INGEST_SCOPE_STRING,
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


class _ConsoleFilesBackend:
    """Routes a single MockTransport handler across Keycloak JWKS +
    token-exchange and the orchestrator's ``/memory/upload`` endpoint.
    Records every orchestrator call (method, URL, headers, body) AND the
    raw form body of every token-exchange request, so tests can assert
    on the exact ``scope`` requested and the exact upstream URL/body
    forwarded."""

    def __init__(self) -> None:
        self.orchestrator_calls: list[httpx.Request] = []
        self.exchange_request_bodies: list[str] = []
        self.orchestrator_response = httpx.Response(
            200,
            json={"key": "session/upload-abc123"},
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
        if url.startswith(f"{ORCHESTRATOR_BASE}/memory/upload"):
            self.orchestrator_calls.append(request)
            return self.orchestrator_response
        raise AssertionError(f"unexpected upstream call: {url}")


@pytest.fixture
def console_files_client():
    from fastapi.testclient import TestClient

    app = create_app()
    backend = _ConsoleFilesBackend()
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
    app.dependency_overrides[get_settings] = lambda: _settings()
    app.dependency_overrides[get_http_client] = lambda: mock_client
    with TestClient(app) as client:
        yield client, backend


_MULTIPART_BODY = (
    b"--boundary123\r\n"
    b'Content-Disposition: form-data; name="file"; filename="notes.txt"\r\n'
    b"Content-Type: text/plain\r\n\r\n"
    b"hello sovereign world\r\n"
    b"--boundary123--\r\n"
)
_MULTIPART_CONTENT_TYPE = "multipart/form-data; boundary=boundary123"


class TestExactNarrowScope:
    """Deliverable (a): the exchange sends ``scope`` EXACTLY
    ``memory:session:write`` — never the broad memory-proxy scope set."""

    def test_exchange_requests_exactly_ingest_scope_string(
        self, console_files_client
    ) -> None:
        client, backend = console_files_client
        token = make_token(sub="alice")
        client.post(
            "/console/files",
            content=_MULTIPART_BODY,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": _MULTIPART_CONTENT_TYPE,
            },
        )
        assert len(backend.exchange_request_bodies) == 1
        parsed = parse_qs(backend.exchange_request_bodies[0])
        assert parsed["scope"] == [INGEST_SCOPE_STRING]
        assert parsed["scope"] != [MEMORY_SCOPE_STRING]

    def test_ingest_scope_is_a_single_scope_no_admin_no_durable(
        self, console_files_client
    ) -> None:
        client, backend = console_files_client
        token = make_token(sub="alice")
        client.post(
            "/console/files",
            content=_MULTIPART_BODY,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": _MULTIPART_CONTENT_TYPE,
            },
        )
        parsed = parse_qs(backend.exchange_request_bodies[0])
        requested_scopes = parsed["scope"][0].split(" ")
        assert requested_scopes == ["memory:session:write"]
        assert "audittrace:admin" not in requested_scopes
        assert not any(s.startswith("memory:corpus:") for s in requested_scopes)


class TestForcedLayer:
    """Deliverable (b)/(3): the upstream request always carries
    ``layer=session`` — a caller-supplied ``?layer=episodic`` (or
    anything else) is ignored, not honoured."""

    def test_caller_supplied_layer_is_overridden(self, console_files_client) -> None:
        client, backend = console_files_client
        token = make_token(sub="alice")
        client.post(
            "/console/files?layer=episodic",
            content=_MULTIPART_BODY,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": _MULTIPART_CONTENT_TYPE,
            },
        )
        assert len(backend.orchestrator_calls) == 1
        forwarded_url = str(backend.orchestrator_calls[0].url)
        assert forwarded_url == f"{ORCHESTRATOR_BASE}/memory/upload?layer=session"
        assert "episodic" not in forwarded_url

    def test_no_query_string_still_forces_layer_session(
        self, console_files_client
    ) -> None:
        client, backend = console_files_client
        token = make_token(sub="alice")
        client.post(
            "/console/files",
            content=_MULTIPART_BODY,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": _MULTIPART_CONTENT_TYPE,
            },
        )
        forwarded_url = str(backend.orchestrator_calls[0].url)
        assert forwarded_url == f"{ORCHESTRATOR_BASE}/memory/upload?layer=session"

    def test_configured_forced_layer_value_is_used(self) -> None:
        """The forced layer is read from ``settings.console_files_forced_layer``,
        not a hardcoded literal — override it and the upstream URL
        follows."""
        from fastapi.testclient import TestClient

        app = create_app()
        backend = _ConsoleFilesBackend()
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
        app.dependency_overrides[get_settings] = lambda: _settings(
            console_files_forced_layer="quarantine"
        )
        app.dependency_overrides[get_http_client] = lambda: mock_client
        with TestClient(app) as client:
            token = make_token(sub="alice")
            client.post(
                "/console/files",
                content=_MULTIPART_BODY,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": _MULTIPART_CONTENT_TYPE,
                },
            )
        forwarded_url = str(backend.orchestrator_calls[0].url)
        assert forwarded_url == f"{ORCHESTRATOR_BASE}/memory/upload?layer=quarantine"


class TestIdentityPropagation:
    """Deliverable (c): the minted token's ``sub`` matches the caller's
    own — two distinct callers produce two distinct identities at the
    orchestrator boundary, never a static/shared subject."""

    def test_two_callers_produce_two_distinct_minted_subs(
        self, console_files_client
    ) -> None:
        client, backend = console_files_client
        alice_token = make_token(sub="alice")
        bob_token = make_token(sub="bob")

        client.post(
            "/console/files",
            content=_MULTIPART_BODY,
            headers={
                "Authorization": f"Bearer {alice_token}",
                "Content-Type": _MULTIPART_CONTENT_TYPE,
            },
        )
        client.post(
            "/console/files",
            content=_MULTIPART_BODY,
            headers={
                "Authorization": f"Bearer {bob_token}",
                "Content-Type": _MULTIPART_CONTENT_TYPE,
            },
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
    """Deliverable (d): the non-negotiable invariant — absent/invalid
    token never reaches Keycloak or the orchestrator; Keycloak failure
    is 502; a 4xx the orchestrator itself returns is relayed as-is."""

    def test_missing_authorization_header_401_never_reaches_orchestrator(
        self, console_files_client
    ) -> None:
        client, backend = console_files_client
        response = client.post(
            "/console/files",
            content=_MULTIPART_BODY,
            headers={"Content-Type": _MULTIPART_CONTENT_TYPE},
        )
        assert response.status_code == 401
        assert backend.orchestrator_calls == []
        assert backend.exchange_request_bodies == []

    def test_expired_token_401_never_reaches_orchestrator(
        self, console_files_client
    ) -> None:
        client, backend = console_files_client
        token = make_token(sub="alice", exp_offset=-3600)
        response = client.post(
            "/console/files",
            content=_MULTIPART_BODY,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": _MULTIPART_CONTENT_TYPE,
            },
        )
        assert response.status_code == 401
        assert backend.orchestrator_calls == []
        assert backend.exchange_request_bodies == []

    def test_garbage_token_401_never_reaches_orchestrator(
        self, console_files_client
    ) -> None:
        client, backend = console_files_client
        response = client.post(
            "/console/files",
            content=_MULTIPART_BODY,
            headers={
                "Authorization": "Bearer not-a-jwt",
                "Content-Type": _MULTIPART_CONTENT_TYPE,
            },
        )
        assert response.status_code == 401
        assert backend.orchestrator_calls == []

    def test_keycloak_exchange_failure_502_never_reaches_orchestrator(
        self, console_files_client
    ) -> None:
        client, backend = console_files_client
        backend.keycloak_down = True
        token = make_token(sub="alice")
        response = client.post(
            "/console/files",
            content=_MULTIPART_BODY,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": _MULTIPART_CONTENT_TYPE,
            },
        )
        assert response.status_code == 502
        assert backend.orchestrator_calls == []

    @pytest.mark.parametrize("upstream_status", [400, 403, 413])
    def test_orchestrator_denial_forwarded_verbatim_not_manufactured(
        self, console_files_client, upstream_status: int
    ) -> None:
        """The load-bearing fail-closed guard: even with a VALID,
        successfully-exchanged token, a denial from the orchestrator's
        own layer-write check (e.g. PDF→session refusal = 400, RLS
        denial = 403, size-limit = 413) must reach the caller unchanged
        — the BFF must not retry, swallow, or upgrade it to a 200."""
        client, backend = console_files_client
        backend.orchestrator_response = httpx.Response(
            upstream_status,
            json={"detail": "denied"},
            headers={"content-type": "application/json"},
        )
        token = make_token(sub="alice")
        response = client.post(
            "/console/files",
            content=_MULTIPART_BODY,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": _MULTIPART_CONTENT_TYPE,
            },
        )
        assert response.status_code == upstream_status
        assert response.json() == {"detail": "denied"}
        # Proves the orchestrator WAS reached (a real per-request
        # denial), not one of the never-contacted shortcuts above.
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
            response = client.post(
                "/console/files",
                content=_MULTIPART_BODY,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": _MULTIPART_CONTENT_TYPE,
                },
            )
        assert response.status_code == 502


class TestByteFaithfulForward:
    """Deliverable (f)/(3): the multipart body and its ``Content-Type``
    are forwarded to the orchestrator UNCHANGED — no re-encoding, no
    boundary rewrite, no parsing."""

    def test_multipart_body_forwarded_byte_identical(
        self, console_files_client
    ) -> None:
        client, backend = console_files_client
        token = make_token(sub="alice")
        client.post(
            "/console/files",
            content=_MULTIPART_BODY,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": _MULTIPART_CONTENT_TYPE,
            },
        )
        assert backend.orchestrator_calls[0].read() == _MULTIPART_BODY

    def test_content_type_header_forwarded_unchanged(
        self, console_files_client
    ) -> None:
        client, backend = console_files_client
        token = make_token(sub="alice")
        client.post(
            "/console/files",
            content=_MULTIPART_BODY,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": _MULTIPART_CONTENT_TYPE,
            },
        )
        assert (
            backend.orchestrator_calls[0].headers["content-type"]
            == _MULTIPART_CONTENT_TYPE
        )

    def test_orchestrator_response_body_returned_unchanged(
        self, console_files_client
    ) -> None:
        """The API's response body (the memory key) is returned to the
        caller unchanged, so WU-5 recall can reference it."""
        client, backend = console_files_client
        token = make_token(sub="alice")
        response = client.post(
            "/console/files",
            content=_MULTIPART_BODY,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": _MULTIPART_CONTENT_TYPE,
            },
        )
        assert response.status_code == 200
        assert response.json() == {"key": "session/upload-abc123"}


class TestConsoleFilesMethodIsPostOnly:
    """POST only — the route must not accept GET/PUT/DELETE, since the
    console has no reason to read/update/delete via this seam."""

    @pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
    def test_other_methods_not_allowed(self, console_files_client, method: str) -> None:
        client, _backend = console_files_client
        response = client.request(method, "/console/files")
        assert response.status_code == 405
