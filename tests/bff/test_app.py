"""End-to-end tests for bff/app.py — the wired-together FastAPI service.

No real Keycloak, no real orchestrator: a single ``httpx.MockTransport``
handler plays BOTH the Keycloak realm (JWKS + token-exchange) and the
orchestrator (``/v1/chat/completions``), routed by URL. This is the
level at which the spec's two falsifiable, non-vacuous guards are
proven end-to-end:

* ``TestFailClosed`` — an absent/invalid/expired forwarded token never
  reaches the orchestrator (spy call-counter proves it, not just an
  error code).
* ``TestTwoDistinctCallers`` — two different callers produce two
  different token-derived identities at the orchestrator boundary.

The M3-WU-D2-1 ``/memory/*`` proxy routes get their own backend
(``_MemoryBackend`` below) and their own falsifiable guards:

* ``TestMemoryProxyScopeSelection`` — the memory path's exchange request
  carries the memory scope set, the chat path's carries none (no
  ``scope=`` field at all) — proves the two paths are genuinely
  independent, not one leaking into the other.
* ``TestMemoryProxyFailClosed`` — an absent/invalid token never reaches
  the orchestrator's ``/memory`` API; a 401/403/404 the orchestrator
  itself returns is relayed AS-IS, never manufactured into access.
"""

from __future__ import annotations

import asyncio
import datetime
import time

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

import bff.app as app_module
from bff.app import create_app, get_http_client, lifespan
from bff.config import Settings, get_settings
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


def _make_self_signed_test_ca_pem() -> str:
    """A throwaway self-signed X.509 cert PEM — real enough for
    ``ssl.SSLContext.load_verify_locations`` to accept as a trust
    anchor, not tied to any real cluster CA."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "audittrace-bff-test-ca")]
    )
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


_SELF_SIGNED_TEST_PEM = _make_self_signed_test_ca_pem()


def _settings() -> Settings:
    return Settings(
        exchange_client_secret="s3cr3t",
        keycloak_issuer=TEST_ISSUER,
        keycloak_issuer_extras=[],
        keycloak_jwks_url=KEYCLOAK_JWKS_URL,
        keycloak_token_url=KEYCLOAK_TOKEN_URL,
        exchange_client_id="audittrace-librechat-bff",
        exchange_audience="audittrace-librechat",
        orchestrator_base_url=ORCHESTRATOR_BASE,
        orchestrator_chat_path="/v1/chat/completions",
        proxy_source_label="librechat",
    )


def _minted_token_for(sub: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": TEST_ISSUER,
            "sub": sub,
            "aud": "audittrace-server",
            "scope": "audittrace:query",
            "iat": now,
            "exp": now + 300,
        },
        TEST_PRIVATE_PEM,
        algorithm="RS256",
    )


class _Backend:
    """Routes a single MockTransport handler across the three fake
    upstreams (Keycloak JWKS, Keycloak token endpoint, orchestrator),
    recording every orchestrator call so tests can assert on both the
    call COUNT (never-proxied-unauthenticated) and the exact
    Authorization header each call carried (identity)."""

    def __init__(self) -> None:
        self.orchestrator_calls: list[httpx.Request] = []
        self.keycloak_exchange_calls: int = 0
        self.exchange_request_bodies: list[str] = []
        self.orchestrator_response = httpx.Response(
            200,
            content=b'{"id":"chatcmpl-1","choices":[]}',
            headers={"content-type": "application/json"},
        )
        self.keycloak_down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == KEYCLOAK_JWKS_URL:
            return httpx.Response(200, json={"keys": [TEST_PUBLIC_PEM]})
        if url == KEYCLOAK_TOKEN_URL:
            self.keycloak_exchange_calls += 1
            body = request.read().decode()
            self.exchange_request_bodies.append(body)
            if self.keycloak_down:
                return httpx.Response(503)
            sub = _sub_from_form_body(body)
            minted = _minted_token_for(sub)
            return httpx.Response(
                200, json={"access_token": minted, "token_type": "Bearer"}
            )
        if url == f"{ORCHESTRATOR_BASE}/v1/chat/completions":
            self.orchestrator_calls.append(request)
            return self.orchestrator_response
        raise AssertionError(f"unexpected upstream call: {url}")


def _sub_from_form_body(body: str) -> str:
    """Pull the caller's own sub out of the (unverified, test-only)
    ``subject_token`` in the token-exchange form body, so the fake
    Keycloak mints a token for the SAME subject the real service would
    — exactly what makes the two-distinct-callers test meaningful."""
    from urllib.parse import parse_qs

    parsed = parse_qs(body)
    subject_token = parsed["subject_token"][0]
    claims = jwt.get_unverified_claims(subject_token)
    return claims["sub"]


@pytest.fixture
def app_client():
    app = create_app()
    backend = _Backend()
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_http_client] = lambda: mock_client
    with TestClient(app) as client:
        yield client, backend


class _MemoryBackend:
    """Routes a single MockTransport handler across Keycloak JWKS +
    token-exchange and the orchestrator's ``/memory`` API. Records every
    orchestrator ``/memory`` call AND the raw form body of every
    token-exchange request, so tests can assert on the exact ``scope``
    (or its absence) the BFF requested — the thing that makes
    ``TestMemoryProxyScopeSelection`` meaningful rather than a status-code
    check."""

    def __init__(self) -> None:
        self.orchestrator_calls: list[httpx.Request] = []
        self.exchange_request_bodies: list[str] = []
        self.orchestrator_response = httpx.Response(
            200,
            content=b'{"items":[]}',
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
        if url.startswith(f"{ORCHESTRATOR_BASE}/memory"):
            self.orchestrator_calls.append(request)
            return self.orchestrator_response
        raise AssertionError(f"unexpected upstream call: {url}")


@pytest.fixture
def memory_app_client():
    app = create_app()
    backend = _MemoryBackend()
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_http_client] = lambda: mock_client
    with TestClient(app) as client:
        yield client, backend


class TestHealth:
    def test_health_ok(self, app_client) -> None:
        client, _backend = app_client
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestHappyPath:
    def test_valid_token_streams_orchestrator_response(self, app_client) -> None:
        client, backend = app_client
        token = make_token(sub="alice")
        response = client.post(
            "/v1/chat/completions",
            content=b'{"model":"audittrace-chat","messages":[]}',
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200
        assert response.content == b'{"id":"chatcmpl-1","choices":[]}'
        assert len(backend.orchestrator_calls) == 1

    def test_request_body_forwarded_byte_identical(self, app_client) -> None:
        client, backend = app_client
        token = make_token(sub="alice")
        raw = b'{"messages":  [{"role":"user","content":"caf\xc3\xa9"}],"zeta":1,"alpha":2}'
        client.post(
            "/v1/chat/completions",
            content=raw,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        assert backend.orchestrator_calls[0].read() == raw


class TestFailClosed:
    """The non-negotiable invariant: absent/invalid/expired token ⇒ 401,
    and the orchestrator is NEVER contacted — not "contacted with a
    401-worthy body", literally zero calls."""

    def test_missing_authorization_header_401_never_reaches_orchestrator(
        self, app_client
    ) -> None:
        client, backend = app_client
        response = client.post(
            "/v1/chat/completions",
            content=b"{}",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 401
        assert backend.orchestrator_calls == []
        assert backend.keycloak_exchange_calls == 0

    def test_malformed_scheme_401_never_reaches_orchestrator(self, app_client) -> None:
        client, backend = app_client
        response = client.post(
            "/v1/chat/completions",
            content=b"{}",
            headers={
                "Authorization": "Basic dXNlcjpwYXNz",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 401
        assert backend.orchestrator_calls == []

    def test_expired_token_401_never_reaches_orchestrator(self, app_client) -> None:
        client, backend = app_client
        token = make_token(sub="alice", exp_offset=-3600)
        response = client.post(
            "/v1/chat/completions",
            content=b"{}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 401
        assert backend.orchestrator_calls == []
        assert backend.keycloak_exchange_calls == 0

    def test_garbage_token_401_never_reaches_orchestrator(self, app_client) -> None:
        client, backend = app_client
        response = client.post(
            "/v1/chat/completions",
            content=b"{}",
            headers={
                "Authorization": "Bearer not-a-jwt",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 401
        assert backend.orchestrator_calls == []

    def test_keycloak_exchange_failure_502_never_reaches_orchestrator(
        self, app_client
    ) -> None:
        client, backend = app_client
        backend.keycloak_down = True
        token = make_token(sub="alice")
        response = client.post(
            "/v1/chat/completions",
            content=b"{}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 502
        assert backend.orchestrator_calls == []


class TestTwoDistinctCallers:
    """The identity gate: two different callers must produce two
    different token-derived identities at the orchestrator boundary —
    never a static/shared subject. Neuter ``exchange_token`` to stamp a
    fixed sub and this test goes RED (both minted subs would be equal)."""

    def test_two_callers_produce_two_distinct_minted_subs(self, app_client) -> None:
        client, backend = app_client
        alice_token = make_token(sub="alice")
        bob_token = make_token(sub="bob")

        client.post(
            "/v1/chat/completions",
            content=b"{}",
            headers={
                "Authorization": f"Bearer {alice_token}",
                "Content-Type": "application/json",
            },
        )
        client.post(
            "/v1/chat/completions",
            content=b"{}",
            headers={
                "Authorization": f"Bearer {bob_token}",
                "Content-Type": "application/json",
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
        assert minted_tokens[0] != minted_tokens[1]


class TestOrchestratorUnreachable:
    def test_orchestrator_transport_failure_502(self) -> None:
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
        app.dependency_overrides[get_settings] = _settings
        app.dependency_overrides[get_http_client] = lambda: mock_client
        with TestClient(app) as client:
            token = make_token(sub="alice")
            response = client.post(
                "/v1/chat/completions",
                content=b"{}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        assert response.status_code == 502


class TestExtractBearerToken:
    """Direct unit coverage of the header-parsing helper's edge cases
    not otherwise exercised through the route (empty-after-strip)."""

    def test_bearer_with_only_whitespace_token_is_treated_as_missing(self) -> None:
        from bff.app import _extract_bearer_token

        assert _extract_bearer_token("Bearer    ") is None

    def test_no_header_is_none(self) -> None:
        from bff.app import _extract_bearer_token

        assert _extract_bearer_token(None) is None

    def test_wrong_scheme_is_none(self) -> None:
        from bff.app import _extract_bearer_token

        assert _extract_bearer_token("Token abc123") is None

    def test_valid_bearer_extracted(self) -> None:
        from bff.app import _extract_bearer_token

        assert _extract_bearer_token("Bearer abc123") == "abc123"


class TestHttpClientDependency:
    def test_raises_before_lifespan_starts(self) -> None:
        import bff.app as app_module

        app_module._http_client = None
        with pytest.raises(RuntimeError, match="lifespan has not started"):
            get_http_client()

    def test_returns_the_pooled_client_once_set(self) -> None:
        import bff.app as app_module

        sentinel = httpx.AsyncClient()
        app_module._http_client = sentinel
        try:
            assert get_http_client() is sentinel
        finally:
            app_module._http_client = None


class TestLifespanTlsTrust:
    """The M3-WU-3 CA-mount guard: the lifespan-created pooled client
    must pass ``Settings.ca_bundle_path`` straight through as httpx's
    ``verify=`` argument — a mounted local-CA PEM when set, else
    httpx's normal certifi trust (``True``). Neuter the ``verify=``
    plumbing in ``bff/app.py::lifespan`` (e.g. hardcode ``verify=True``)
    and both assertions below go RED."""

    def _run_lifespan_and_capture_verify(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> object:
        captured: dict[str, object] = {}

        class _CapturingAsyncClient(httpx.AsyncClient):
            def __init__(self, *args: object, **kwargs: object) -> None:
                captured["verify"] = kwargs.get("verify")
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(app_module.httpx, "AsyncClient", _CapturingAsyncClient)

        async def _drive() -> None:
            fastapi_app = FastAPI()
            async with lifespan(fastapi_app):
                pass

        asyncio.run(_drive())
        return captured["verify"]

    def test_empty_ca_bundle_path_verifies_with_default_trust(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET", "s3cr3t")
        monkeypatch.delenv("AUDITTRACE_BFF_CA_BUNDLE_PATH", raising=False)
        get_settings.cache_clear()
        try:
            verify = self._run_lifespan_and_capture_verify(monkeypatch)
            assert verify is True
        finally:
            get_settings.cache_clear()

    def test_set_ca_bundle_path_is_passed_as_verify(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # A real (self-signed, arbitrary) PEM on disk — httpx builds a
        # real ``ssl.SSLContext`` at client construction, so the path
        # must exist and be a loadable cert bundle for the lifespan to
        # succeed; a nonexistent path (e.g. a stray literal) would raise
        # ``FileNotFoundError`` right here, which is itself proof the
        # value flows all the way to httpx's ``verify=``.
        ca_file = tmp_path / "ca.crt"
        ca_file.write_text(_SELF_SIGNED_TEST_PEM)
        monkeypatch.setenv("AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET", "s3cr3t")
        monkeypatch.setenv("AUDITTRACE_BFF_CA_BUNDLE_PATH", str(ca_file))
        get_settings.cache_clear()
        try:
            verify = self._run_lifespan_and_capture_verify(monkeypatch)
            assert verify == str(ca_file)
        finally:
            get_settings.cache_clear()


# ---------------------------------------------------------------------------
# M3-WU-D2-1 — the /memory/* proxy routes
# ---------------------------------------------------------------------------


class TestMemoryProxyHappyPath:
    def test_get_forwards_and_streams_response(self, memory_app_client) -> None:
        client, backend = memory_app_client
        token = make_token(sub="alice")
        response = client.get(
            "/memory/episodic",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json() == {"items": []}
        assert len(backend.orchestrator_calls) == 1
        assert backend.orchestrator_calls[0].method == "GET"
        assert str(backend.orchestrator_calls[0].url).endswith("/memory/episodic")

    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
    def test_write_methods_forwarded_with_correct_verb(
        self, memory_app_client, method: str
    ) -> None:
        client, backend = memory_app_client
        token = make_token(sub="alice")
        response = client.request(
            method,
            "/memory/episodic/some-doc",
            content=b'{"content":"hello"}' if method != "DELETE" else b"",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200
        assert backend.orchestrator_calls[0].method == method

    def test_request_body_forwarded_byte_identical(self, memory_app_client) -> None:
        client, backend = memory_app_client
        token = make_token(sub="alice")
        raw = b'{"content":  "caf\xc3\xa9","zeta":1,"alpha":2}'
        client.post(
            "/memory/semantic",
            content=raw,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        assert backend.orchestrator_calls[0].read() == raw

    def test_query_string_forwarded(self, memory_app_client) -> None:
        client, backend = memory_app_client
        token = make_token(sub="alice")
        client.get(
            "/memory/episodic?limit=500&sort=recency",
            headers={"Authorization": f"Bearer {token}"},
        )
        forwarded_url = str(backend.orchestrator_calls[0].url)
        assert "limit=500" in forwarded_url
        assert "sort=recency" in forwarded_url

    def test_nested_path_forwarded(self, memory_app_client) -> None:
        client, backend = memory_app_client
        token = make_token(sub="alice")
        client.get(
            "/memory/semantic/decisions/some-doc-id",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert str(backend.orchestrator_calls[0].url).endswith(
            "/memory/semantic/decisions/some-doc-id"
        )


class TestMemoryProxyScopeSelection:
    """The falsifiable proof that the memory path and the chat path each
    request their OWN scope set from Keycloak — neuter
    ``bff/app.py::memory_proxy``'s ``requested_scope=MEMORY_SCOPE_STRING``
    argument (e.g. drop it, or pass ``None``) and
    ``test_memory_path_exchange_requests_memory_scope_string`` goes RED.
    """

    def test_memory_path_exchange_requests_memory_scope_string(
        self, memory_app_client
    ) -> None:
        from urllib.parse import parse_qs

        from bff.memory_scopes import MEMORY_SCOPE_STRING

        client, backend = memory_app_client
        token = make_token(sub="alice")
        client.get("/memory/episodic", headers={"Authorization": f"Bearer {token}"})
        assert len(backend.exchange_request_bodies) == 1
        parsed = parse_qs(backend.exchange_request_bodies[0])
        assert parsed["scope"] == [MEMORY_SCOPE_STRING]

    def test_memory_scope_never_includes_admin(self, memory_app_client) -> None:
        from urllib.parse import parse_qs

        client, backend = memory_app_client
        token = make_token(sub="alice")
        client.get("/memory/procedural", headers={"Authorization": f"Bearer {token}"})
        parsed = parse_qs(backend.exchange_request_bodies[0])
        requested_scopes = parsed["scope"][0].split(" ")
        assert "audittrace:admin" not in requested_scopes

    def test_chat_path_exchange_requests_no_scope_field(self, app_client) -> None:
        """Sibling/regression check: the ORIGINAL chat-path exchange
        shape is unregressed by the memory path's addition — no ``scope``
        form field at all (Keycloak falls back to the target client's own
        default scopes, exactly WU-1/WU-2's behaviour). Neuter
        ``bff/app.py::chat_completions`` to also pass
        ``requested_scope=MEMORY_SCOPE_STRING`` and this test goes RED."""
        client, backend = app_client
        token = make_token(sub="alice")
        client.post(
            "/v1/chat/completions",
            content=b"{}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        assert len(backend.exchange_request_bodies) == 1
        assert "scope=" not in backend.exchange_request_bodies[0]


class TestMemoryProxyFailClosed:
    """The non-negotiable invariant for the memory path, mirroring
    ``TestFailClosed`` for chat: absent/invalid token ⇒ 401, orchestrator
    NEVER contacted; a 401/403/404 the orchestrator DOES return is
    relayed as-is, never manufactured into a 200."""

    def test_missing_authorization_header_401_never_reaches_orchestrator(
        self, memory_app_client
    ) -> None:
        client, backend = memory_app_client
        response = client.get("/memory/episodic")
        assert response.status_code == 401
        assert backend.orchestrator_calls == []
        assert backend.exchange_request_bodies == []

    def test_expired_token_401_never_reaches_orchestrator(
        self, memory_app_client
    ) -> None:
        client, backend = memory_app_client
        token = make_token(sub="alice", exp_offset=-3600)
        response = client.get(
            "/memory/episodic", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
        assert backend.orchestrator_calls == []

    def test_keycloak_exchange_failure_502_never_reaches_orchestrator(
        self, memory_app_client
    ) -> None:
        client, backend = memory_app_client
        backend.keycloak_down = True
        token = make_token(sub="alice")
        response = client.get(
            "/memory/episodic", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 502
        assert backend.orchestrator_calls == []

    @pytest.mark.parametrize("upstream_status", [401, 403, 404])
    def test_orchestrator_denial_forwarded_verbatim_not_manufactured(
        self, memory_app_client, upstream_status: int
    ) -> None:
        """The load-bearing fail-closed guard: even with a VALID,
        successfully-exchanged token, a denial from the orchestrator's
        own scope/RLS check must reach the caller unchanged — the BFF
        must not retry, swallow, or upgrade it to a 200."""
        client, backend = memory_app_client
        backend.orchestrator_response = httpx.Response(
            upstream_status,
            json={"detail": "denied"},
            headers={"content-type": "application/json"},
        )
        token = make_token(sub="alice")
        response = client.get(
            "/memory/episodic", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == upstream_status
        assert response.json() == {"detail": "denied"}
        # Proves the orchestrator WAS reached (a real per-request denial,
        # not the "never contacted" 401/502 shortcuts above) — this test
        # is not vacuously satisfied by the earlier fail-closed tests.
        assert len(backend.orchestrator_calls) == 1


class TestMemoryProxyOrchestratorUnreachable:
    def test_transport_failure_502(self) -> None:
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
        app.dependency_overrides[get_settings] = _settings
        app.dependency_overrides[get_http_client] = lambda: mock_client
        with TestClient(app) as client:
            token = make_token(sub="alice")
            response = client.get(
                "/memory/episodic", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 502
