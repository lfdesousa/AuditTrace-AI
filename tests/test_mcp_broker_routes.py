"""HTTP-level tests for the brokered ``POST /mcp`` surface (ADR-063 Phase
2 Track B).

Exercises the whole stack — the FastAPI route, ``require_user`` auth +
RLS ContextVar binding, ``services.mcp_broker``, a STUB downstream MCP
server (``httpx.MockTransport`` — no real network process), and the SAME
tamper-evident audit path Phase 1 and the chat tool loop use
(``InteractionRecord`` + ``ToolCall``) — through the public JSON-RPC
transport a real MCP client would speak.

Self-contained auth helpers (not imported from ``test_mcp_routes.py``) —
this file follows the same "per-file self-contained auth helpers"
convention that module documents (``test_admin_routes.py`` /
``test_memory_routes.py``), and per the spec's own instruction:
"Identity is token-derived via the REAL require_user (NO
``dependency_overrides`` bypass — that was the Phase 1 RLS reject; drive
with a real scoped token so neutering ``set_current_user_id`` actually
leaks → RED)". No test in this file uses ``client.app.dependency_overrides
[require_user]``.

Falsifiable acceptance criteria covered (spec Rule 1, Track B):

  1. ``tools/list`` merges the brokered manifest —
     ``TestToolsListMergesBrokerManifest``.
  2. A valid-scope brokered call executes, forwards under the caller's
     identity, returns the downstream result, AND writes TWO tamper-
     evident audit rows tagged ``brokered`` (request + result) sharing
     one interaction/trace —
     ``TestBrokeredCallWritesTwoAuditRows.test_valid_scope_writes_request_and_result_rows``.
  3. Scope denial blocks BEFORE forward (spy on the downstream transport)
     — ``TestScopeDeniedBeforeForward``.
  4. A downstream failure/timeout still writes a recorded (result) event
     — ``TestDownstreamFailureRecorded``.
  5. Identity/RLS: real, un-overridden ``require_user`` binds each
     distinct real caller's own sub before ``broker_tool_call`` runs, and
     genuinely neutering ``set_current_user_id`` (the exact Phase 1
     reviewer finding) turns the assertion RED —
     ``TestIdentityBinding``.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization as _crypto_serialization
from cryptography.hazmat.primitives.asymmetric import rsa as _crypto_rsa
from fastapi.testclient import TestClient
from jose import jwt as _jose_jwt
from sqlalchemy import select

from audittrace.db import rls as rls_module
from audittrace.db.models import InteractionRecord, ToolCall
from audittrace.dependencies import get_postgres_factory
from audittrace.identity import sentinel_user_context
from audittrace.services import mcp_broker as broker_mod

_DOWNSTREAM_URL = "http://weather-mcp.test/mcp"
_REQUIRED_SCOPE = "audittrace:broker:weather"


def _rpc(
    method: str, params: dict[str, Any] | None = None, *, id_: Any = 1
) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if id_ is not None:
        body["id"] = id_
    return body


async def _tool_call_rows() -> list[ToolCall]:
    pg = get_postgres_factory()
    async with pg.get_session_factory()() as db:
        return list((await db.execute(select(ToolCall))).scalars().all())


async def _interaction_rows() -> list[InteractionRecord]:
    pg = get_postgres_factory()
    async with pg.get_session_factory()() as db:
        return list((await db.execute(select(InteractionRecord))).scalars().all())


@pytest.fixture
def stub_broker(monkeypatch: pytest.MonkeyPatch):
    """Configure ONE downstream server ("weather") and point the broker's
    outbound client at an ``httpx.MockTransport`` running ``handler`` —
    the "fake/stub downstream MCP server" the spec requires. Returns a
    configurator so each test supplies its own handler."""
    from audittrace import config as config_mod

    def _configure(
        handler,
        *,
        required_scope: str = _REQUIRED_SCOPE,
        url: str = _DOWNSTREAM_URL,
    ) -> None:
        monkeypatch.setenv(
            "AUDITTRACE_MCP_BROKER_SERVERS",
            json.dumps({"weather": {"url": url, "required_scope": required_scope}}),
        )
        config_mod.get_settings.cache_clear()
        monkeypatch.setattr(
            broker_mod,
            "_downstream_client",
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    yield _configure
    config_mod.get_settings.cache_clear()


def _forecast_ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "sunny"}],
                "structuredContent": {"forecast": "sunny"},
                "isError": False,
            },
        },
    )


# ───────────────── Real-JWT machinery (Phase 1 reviewer-finding fix,
# mirrored here per the spec's explicit instruction for Track B) ───────────

_JWT_PRIVATE_KEY = _crypto_rsa.generate_private_key(
    public_exponent=65537, key_size=2048
)
_JWT_PRIVATE_PEM = _JWT_PRIVATE_KEY.private_bytes(
    encoding=_crypto_serialization.Encoding.PEM,
    format=_crypto_serialization.PrivateFormat.PKCS8,
    encryption_algorithm=_crypto_serialization.NoEncryption(),
).decode()
_JWT_PUBLIC_PEM = (
    _JWT_PRIVATE_KEY.public_key()
    .public_bytes(
        encoding=_crypto_serialization.Encoding.PEM,
        format=_crypto_serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode()
)
_JWT_ISSUER = "http://keycloak:8080/realms/audittrace"
_JWT_AUDIENCE = "audittrace-server"


def _make_real_jwt(*, sub: str, scope: str, jti: str) -> str:
    """A genuinely signed RS256 JWT — decoded by the REAL
    ``_decode_jwt_with_allowed_issuers`` inside ``require_user``."""
    now = int(time.time())
    claims = {
        "iss": _JWT_ISSUER,
        "aud": _JWT_AUDIENCE,
        "sub": sub,
        "scope": scope,
        "iat": now,
        "exp": now + 3600,
        "jti": jti,
        "preferred_username": sub,
    }
    return _jose_jwt.encode(claims, _JWT_PRIVATE_PEM, algorithm="RS256")


@pytest.fixture
def _real_auth_stack(monkeypatch: pytest.MonkeyPatch):
    """Flip ``AUDITTRACE_AUTH_REQUIRED=true`` and drive ``require_user``'s
    real cold path (JWT verify → JWKS → UserContext → cache →
    ``set_current_user_id``) against a throwaway signed keypair, with a
    fakeredis-backed token cache so no real Redis is needed."""
    import fakeredis

    from audittrace import config as config_mod
    from audittrace import identity as identity_mod
    from audittrace.auth import _jwks_cache
    from audittrace.identity import TokenCache

    config_mod.get_settings.cache_clear()
    monkeypatch.setenv("AUDITTRACE_AUTH_REQUIRED", "true")
    monkeypatch.setenv("AUDITTRACE_KEYCLOAK_ISSUER", _JWT_ISSUER)
    monkeypatch.setenv("AUDITTRACE_KEYCLOAK_JWKS_URL", "http://test/jwks")
    monkeypatch.setenv("AUDITTRACE_JWT_AUDIENCE", _JWT_AUDIENCE)

    _jwks_cache.clear()

    async def _fake_fetch_jwks_keys(_url: str) -> list[str]:
        return [_JWT_PUBLIC_PEM]

    monkeypatch.setattr("audittrace.auth._fetch_jwks_keys", _fake_fetch_jwks_keys)
    monkeypatch.setattr("audittrace.auth._jwks_client", None)
    monkeypatch.setattr("audittrace.auth._jwks_fetch_lock", None)

    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(
        identity_mod, "_token_cache", TokenCache(fake_redis, default_ttl_seconds=300)
    )
    monkeypatch.setattr(identity_mod, "_redis_client", fake_redis)

    yield

    config_mod.get_settings.cache_clear()
    _jwks_cache.clear()


# ────────────────────────── tools/list manifest merge ─────────────────────


class TestToolsListMergesBrokerManifest:
    def test_manifest_includes_namespaced_brokered_tool(
        self, client: TestClient, stub_broker
    ) -> None:
        def _list_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"result": {"tools": [{"name": "forecast"}]}}
            )

        stub_broker(_list_handler)
        r = client.post("/mcp", json=_rpc("tools/list", {}))
        assert r.status_code == 200
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert "broker:weather:forecast" in names

    def test_manifest_omits_broker_tools_when_no_server_configured(
        self, client: TestClient
    ) -> None:
        r = client.post("/mcp", json=_rpc("tools/list", {}))
        assert r.status_code == 200
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert not any(n.startswith("broker:") for n in names)


# ───────────────────── brokered tools/call: audit rows ────────────────────


class TestBrokeredCallWritesTwoAuditRows:
    @pytest.mark.asyncio
    async def test_valid_scope_writes_request_and_result_rows(
        self, client: TestClient, stub_broker
    ) -> None:
        """Falsifiable acceptance #2: a brokered call executes, returns
        the downstream result, AND writes TWO ``ToolCall`` rows tagged
        ``provenance="brokered"`` (request + result), sharing one
        interaction/trace. Neuter (drop either
        ``PendingToolCall`` in ``mcp.py``'s pending list) → this test
        goes RED because ``len(rows) != 2``."""
        stub_broker(_forecast_ok_handler)

        r = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {
                    "name": "broker:weather:forecast",
                    "arguments": {"city": "Zurich"},
                },
            ),
        )
        assert r.status_code == 200
        body = r.json()["result"]
        assert body["isError"] is False
        assert body["structuredContent"] == {"forecast": "sunny"}

        rows = await _tool_call_rows()
        assert len(rows) == 2
        by_phase = {row.phase: row for row in rows}
        assert set(by_phase) == {"request", "result"}
        req, res = by_phase["request"], by_phase["result"]
        assert req.provenance == "brokered"
        assert res.provenance == "brokered"
        assert req.downstream_server == "weather"
        assert res.downstream_server == "weather"
        assert req.downstream_tool == "forecast"
        assert req.interaction_id == res.interaction_id
        assert req.user_id == sentinel_user_context().user_id
        assert res.user_id == sentinel_user_context().user_id
        assert res.error is None
        assert res.result_digest is not None

        interactions = await _interaction_rows()
        mine = [i for i in interactions if i.id == req.interaction_id]
        assert len(mine) == 1
        assert mine[0].source == "mcp"
        assert mine[0].status == "success"

    @pytest.mark.asyncio
    async def test_unconfigured_broker_tool_denied_writes_one_row_no_forward(
        self, client: TestClient
    ) -> None:
        r = client.post(
            "/mcp",
            json=_rpc("tools/call", {"name": "broker:ghost:forecast", "arguments": {}}),
        )
        assert r.status_code == 200
        assert r.json()["result"]["isError"] is True

        rows = await _tool_call_rows()
        assert len(rows) == 1
        assert rows[0].phase == "request"
        assert rows[0].provenance == "brokered"


# ───────────────────────── scope-deny before forward ──────────────────────


class TestScopeDeniedBeforeForward:
    @pytest.mark.asyncio
    async def test_missing_scope_denied_no_forward_no_data(
        self, client: TestClient, stub_broker, _real_auth_stack: None
    ) -> None:
        """Falsifiable acceptance #3: 'an unauthorized brokered call
        reaches downstream' must stay RED, driven by a REAL signed JWT
        (no dependency_overrides). Spies on the transport so the
        assertion is non-vacuous."""
        called = {"count": 0}

        def _spy_handler(request: httpx.Request) -> httpx.Response:
            called["count"] += 1
            return _forecast_ok_handler(request)

        stub_broker(_spy_handler)

        token = _make_real_jwt(sub="no-scope-user", scope="", jti="jti-noscope")
        r = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {"name": "broker:weather:forecast", "arguments": {"city": "Zurich"}},
            ),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        body = r.json()["result"]
        assert body["isError"] is True
        assert body.get("structuredContent") is None
        assert called["count"] == 0, "downstream must never be reached on scope denial"

        rows = await _tool_call_rows()
        assert len(rows) == 1
        assert rows[0].error is not None


# ─────────────────────── downstream failure is recorded ───────────────────


class TestDownstreamFailureRecorded:
    @pytest.mark.asyncio
    async def test_downstream_timeout_still_writes_result_row(
        self, client: TestClient, stub_broker
    ) -> None:
        """Falsifiable acceptance: a downstream failure/timeout still
        writes a recorded event. Neuter the except-branch's
        ``result_pending`` in ``mcp_broker.broker_tool_call`` → RED
        (only 1 row, not 2)."""

        def _timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=request)

        stub_broker(_timeout_handler)

        r = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {"name": "broker:weather:forecast", "arguments": {"city": "Zurich"}},
            ),
        )
        assert r.status_code == 200
        assert r.json()["result"]["isError"] is True

        rows = await _tool_call_rows()
        assert len(rows) == 2
        by_phase = {row.phase: row for row in rows}
        assert by_phase["result"].error is not None
        assert by_phase["result"].provenance == "brokered"

        interactions = await _interaction_rows()
        mine = [i for i in interactions if i.id == rows[0].interaction_id]
        assert mine[0].status == "failed"
        assert mine[0].failure_class == "downstream_error"


# ─────────────────────────── identity / RLS binding ────────────────────────


class TestIdentityBinding:
    def test_rls_contextvar_bound_at_dispatch_time_for_broker_calls(
        self,
        client: TestClient,
        stub_broker,
        monkeypatch: pytest.MonkeyPatch,
        _real_auth_stack: None,
    ) -> None:
        """Falsifiable acceptance #5. TWO distinct REAL signed callers,
        NO ``dependency_overrides`` — ``require_user``'s real body
        (JWT verify → JWKS → UserContext → cache →
        ``set_current_user_id``) executes for both, and the ContextVar
        it binds is captured from INSIDE ``broker_tool_call`` at
        dispatch time. Neuter (monkeypatch
        ``audittrace.auth.set_current_user_id`` to a no-op — the exact
        Phase 1 reviewer finding) → the second assertion block goes RED
        because the captured value stops matching the real caller."""
        import audittrace.routes.mcp as mcp_route

        stub_broker(_forecast_ok_handler)

        seen_user_ids: list[str | None] = []
        real_broker_tool_call = mcp_route.broker_tool_call

        async def _spy(**kwargs: Any) -> Any:
            seen_user_ids.append(rls_module.current_user_id())
            return await real_broker_tool_call(**kwargs)

        monkeypatch.setattr(mcp_route, "broker_tool_call", _spy)

        alice_token = _make_real_jwt(
            sub="alice-broker", scope=_REQUIRED_SCOPE, jti="jti-alice-broker"
        )
        bob_token = _make_real_jwt(
            sub="bob-broker", scope=_REQUIRED_SCOPE, jti="jti-bob-broker"
        )

        client.post(
            "/mcp",
            json=_rpc(
                "tools/call", {"name": "broker:weather:forecast", "arguments": {}}
            ),
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        client.post(
            "/mcp",
            json=_rpc(
                "tools/call", {"name": "broker:weather:forecast", "arguments": {}}
            ),
            headers={"Authorization": f"Bearer {bob_token}"},
        )

        assert seen_user_ids == ["alice-broker", "bob-broker"]

        # ── Neuter-and-confirm (the exact Phase 1 reviewer-finding bug) ──
        seen_user_ids.clear()
        rls_module.set_current_user_id(None)

        def _neutered_set_current_user_id(_user_id: str | None) -> None:
            pass  # the exact bug class the reviewer's finding is about

        monkeypatch.setattr(
            "audittrace.auth.set_current_user_id", _neutered_set_current_user_id
        )

        client.post(
            "/mcp",
            json=_rpc(
                "tools/call", {"name": "broker:weather:forecast", "arguments": {}}
            ),
            headers={"Authorization": f"Bearer {bob_token}"},
        )

        assert seen_user_ids == [None], (
            "neutering set_current_user_id must leave the ContextVar unbound "
            "(RED) — got a value, meaning something else is still binding it"
        )

    @pytest.mark.asyncio
    async def test_two_real_callers_each_own_their_audit_rows(
        self, client: TestClient, stub_broker, _real_auth_stack: None
    ) -> None:
        """Complements the ContextVar-binding proof above with the
        end-to-end row-ownership property: alice's and bob's brokered
        calls each write rows carrying THEIR OWN token-derived
        ``user_id`` — not a proxy/dependency-override identity."""
        stub_broker(_forecast_ok_handler)

        alice_token = _make_real_jwt(
            sub="alice-rows", scope=_REQUIRED_SCOPE, jti="jti-alice-rows"
        )
        bob_token = _make_real_jwt(
            sub="bob-rows", scope=_REQUIRED_SCOPE, jti="jti-bob-rows"
        )

        client.post(
            "/mcp",
            json=_rpc(
                "tools/call", {"name": "broker:weather:forecast", "arguments": {}}
            ),
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        client.post(
            "/mcp",
            json=_rpc(
                "tools/call", {"name": "broker:weather:forecast", "arguments": {}}
            ),
            headers={"Authorization": f"Bearer {bob_token}"},
        )

        rows = await _tool_call_rows()
        user_ids = {row.user_id for row in rows}
        assert user_ids == {"alice-rows", "bob-rows"}
        assert len(rows) == 4  # 2 rows (request+result) per caller
