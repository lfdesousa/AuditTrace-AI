"""HTTP-level tests for ``POST /mcp`` (ADR-063 Phase 1).

Exercises the whole stack — the FastAPI route, ``require_user`` auth +
RLS ContextVar binding, the ``mcp_bridge``, the real registered memory
tools, and the SAME tamper-evident audit path the chat tool loop uses
(``InteractionRecord`` + ``ToolCall``) — through the public JSON-RPC
transport a real MCP client would speak.

Falsifiable acceptance criteria covered (spec Rule 1):

  1. ``tools/list`` returns exactly the Phase-1 read/recall tools —
     ``TestToolsListManifest.test_manifest_excludes_injected_write_tool``.
  2. A valid-scope call executes, returns a result, AND writes exactly
     one audit row (spy on the DB) —
     ``TestToolsCallAuthzAndAudit.test_valid_scope_executes_and_writes_exactly_one_audit_row``.
  3. A missing-scope call is denied: no execution, no data, no silent
     success — ``TestToolsCallAuthzAndAudit.test_missing_scope_denied_no_execution_no_data``.
  4. Tenant isolation — ``TestIdentityBinding.test_cross_tenant_isolation_via_recall_recent_sessions``
     (request layer, two distinct REAL callers) +
     ``TestIdentityBinding.test_rls_contextvar_rebinds_per_real_caller_no_stale_leak``
     (binding correctness, genuine neuter-and-confirm against production
     ``auth.py``) + ``tests/test_mcp_rls_isolation.py`` (policy
     sufficiency: Postgres RLS alone, no app-level filter, on the exact
     ``sessions`` table MCP recall reads). See the reviewer-finding note
     on ``test_cross_tenant_isolation_via_recall_recent_sessions`` for
     why this is three tests, not one — two independent, correct guards
     (the Phase-2 app-level filter and Phase-4 RLS) can't be told apart
     by breaking just one of them while the other still holds.
  5. Caller-supplied identity is ignored, token ``sub`` wins —
     ``TestIdentityBinding.test_caller_supplied_user_id_argument_is_ignored``.
"""

from __future__ import annotations

import importlib
import time
from dataclasses import replace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization as _crypto_serialization
from cryptography.hazmat.primitives.asymmetric import rsa as _crypto_rsa
from fastapi.testclient import TestClient
from jose import jwt as _jose_jwt
from sqlalchemy import select

# Side-effect import — running the module is what runs the
# @register_memory_tool decorators for the six real Phase 1 read tools.
import audittrace.tools.memory_handlers  # noqa: F401
from audittrace.auth import require_user
from audittrace.db import rls as rls_module
from audittrace.db.models import InteractionRecord, ToolCall
from audittrace.dependencies import get_postgres_factory
from audittrace.identity import UserContext, sentinel_user_context
from audittrace.tools import MEMORY_TOOL_REGISTRY, reset_registry_for_tests

_READ_TOOL_NAMES = {
    "recall_decisions",
    "recall_skills",
    "recall_recent_sessions",
    "recall_semantic",
    "read_decision",
    "read_skill",
}


@pytest.fixture(autouse=True)
def _fresh_registry_with_real_handlers():
    """Every test in this file depends on the REAL registered handlers
    (unlike ``test_mcp_bridge.py``, which builds its own stub registry).
    Reset + reload so a prior test file's ``reset_registry_for_tests()``
    teardown can't leave this file starting from an empty registry."""
    reset_registry_for_tests()
    import audittrace.tools.memory_handlers as handlers_mod

    importlib.reload(handlers_mod)
    yield
    reset_registry_for_tests()


def _override_identity(
    client: TestClient, user_id: str, scopes: tuple[str, ...], *, is_admin: bool = False
) -> UserContext:
    """Swap ``require_user`` for a specific non-sentinel identity.
    Caller MUST ``client.app.dependency_overrides.clear()`` afterwards."""
    identity = UserContext(
        user_id=user_id,
        username=user_id,
        agent_type="test",
        scopes=scopes,
        is_admin=is_admin,
    )
    client.app.dependency_overrides[require_user] = lambda: identity
    return identity


async def _tool_call_rows() -> list[ToolCall]:
    pg = get_postgres_factory()
    async with pg.get_session_factory()() as db:
        return list((await db.execute(select(ToolCall))).scalars().all())


async def _interaction_rows() -> list[InteractionRecord]:
    pg = get_postgres_factory()
    async with pg.get_session_factory()() as db:
        return list((await db.execute(select(InteractionRecord))).scalars().all())


def _rpc(
    method: str, params: dict[str, Any] | None = None, *, id_: Any = 1
) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if id_ is not None:
        body["id"] = id_
    return body


# ───────────────── Real-JWT machinery (reviewer finding fix) ────────────────
# Self-contained (not imported from test_auth.py — pytest fixtures imported
# by name only "work" via a shadowing parameter, which ruff correctly flags
# as an unused import; the codebase's convention is per-file self-contained
# auth helpers, e.g. test_admin_routes.py / test_memory_routes.py).
#
# Why this exists: the independent reviewer found that
# ``test_cross_tenant_isolation_via_recall_recent_sessions`` used
# ``client.app.dependency_overrides[require_user] = lambda: identity`` —
# which REPLACES ``require_user``'s entire body, so ``set_current_user_id``
# (the RLS ContextVar binding) never runs. The test was non-vacuous for
# "does the handler thread user_context correctly" but VACUOUS for the
# specific claim ADR-063 + the spec make ("isolated per tenant by row-level
# security") — neutering the ContextVar binding left it green. This section
# drives ``require_user`` for REAL (JWT decode → JWKS verify → UserContext
# → cache → set_current_user_id) with two DISTINCT signed tokens, so a test
# built on it is falsifiable for the ContextVar-binding guard specifically.

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
    ``_decode_jwt_with_allowed_issuers`` inside ``require_user``, not a
    stand-in. Mirrors ``tests/test_auth.py::_make_user_token``."""
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
    """Flip ``AUDITTRACE_AUTH_REQUIRED=true``, point the issuer/audience at
    the throwaway test keypair, mock JWKS fetch to return the matching
    public key, and give ``require_user`` a fakeredis-backed token cache
    (so its cold path runs fully — JWT verify + ``set_current_user_id`` —
    without needing a real Redis). Everything reverts on teardown."""
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


# ────────────────────────────── initialize ──────────────────────────────────


class TestInitialize:
    def test_initialize_returns_server_info(self, client: TestClient) -> None:
        r = client.post("/mcp", json=_rpc("initialize", {}))
        assert r.status_code == 200
        body = r.json()
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 1
        result = body["result"]
        assert result["serverInfo"]["name"] == "audittrace-mcp"
        assert result["capabilities"]["tools"] == {"listChanged": False}


class TestNotification:
    def test_notification_without_id_returns_202_empty_body(
        self, client: TestClient
    ) -> None:
        r = client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        assert r.status_code == 202
        assert r.content == b""


class TestUnknownMethod:
    def test_unknown_method_returns_jsonrpc_error(self, client: TestClient) -> None:
        r = client.post("/mcp", json=_rpc("bogus/method", {}))
        assert r.status_code == 200
        body = r.json()
        assert body["error"]["code"] == -32601

    def test_ping_returns_empty_result(self, client: TestClient) -> None:
        r = client.post("/mcp", json=_rpc("ping", {}))
        assert r.status_code == 200
        assert r.json()["result"] == {}

    def test_malformed_body_is_rejected(self, client: TestClient) -> None:
        r = client.post("/mcp", json=["not", "an", "object"])
        assert r.status_code == 400
        assert r.json()["error"]["code"] == -32600

    def test_unparseable_json_returns_parse_error(self, client: TestClient) -> None:
        r = client.post(
            "/mcp",
            content=b"{not valid json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == -32700

    def test_missing_method_is_invalid_request(self, client: TestClient) -> None:
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "params": {}})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == -32600


# ───────────────────────────── tools/list manifest ──────────────────────────


class TestToolsListManifest:
    def test_manifest_lists_only_the_real_read_tools(self, client: TestClient) -> None:
        r = client.post("/mcp", json=_rpc("tools/list", {}))
        assert r.status_code == 200
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert names == _READ_TOOL_NAMES

    def test_manifest_excludes_injected_write_tool(self, client: TestClient) -> None:
        """Falsifiable acceptance #1, at the HTTP boundary: a write tool
        registered into the SAME registry the real handlers live in must
        never appear in ``tools/list`` — even though the request runs
        through the default bypass-mode ADMIN identity, which bypasses
        every OTHER scope check in this codebase."""
        from audittrace.tools import register_memory_tool

        @register_memory_tool(
            name="delete_everything",
            description="Write/curation tool — must never leak into MCP Phase 1.",
            parameters_schema={"type": "object", "properties": {}},
            required_scope="memory:episodic:write",
        )
        async def _write_stub(
            user_context: UserContext, args: dict[str, Any]
        ) -> dict[str, Any]:  # pragma: no cover - must never run
            raise AssertionError("write tool handler must never execute")

        r = client.post("/mcp", json=_rpc("tools/list", {}))
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert "delete_everything" not in names
        assert names == _READ_TOOL_NAMES


# ───────────────────────── tools/call authz + audit ─────────────────────────


class TestToolsCallAuthzAndAudit:
    @pytest.mark.asyncio
    async def test_valid_scope_executes_and_writes_exactly_one_audit_row(
        self, client: TestClient
    ) -> None:
        """Falsifiable acceptance #2: a valid-scope call executes,
        RETURNS a result, AND writes exactly one audit row carrying the
        token-derived user_id + trace correlation. Neuter (drop the
        ``_flush_pending_tool_calls`` call in routes/mcp.py) → this test
        goes RED because zero ToolCall rows would exist. ``test_container``
        gives every test a fresh, isolated in-memory DB, so the row
        count observed here is exactly what this one call produced."""
        r = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {"name": "recall_decisions", "arguments": {"query": "kv cache"}},
            ),
        )
        assert r.status_code == 200
        body = r.json()["result"]
        assert body["isError"] is False
        assert "matches" in body["structuredContent"]

        after_tool_calls = await _tool_call_rows()
        assert len(after_tool_calls) == 1
        row = after_tool_calls[0]
        assert row.tool_name == "recall_decisions"
        assert row.user_id == sentinel_user_context().user_id
        assert row.error is None
        assert row.granted_scope == "memory:episodic:read"

        interactions = await _interaction_rows()
        mine = [i for i in interactions if i.id == row.interaction_id]
        assert len(mine) == 1
        assert mine[0].source == "mcp"
        assert mine[0].status == "success"
        assert mine[0].user_id == sentinel_user_context().user_id

    @pytest.mark.asyncio
    async def test_missing_scope_denied_no_execution_no_data(
        self, client: TestClient
    ) -> None:
        """Falsifiable acceptance #3. Spies on the REAL registered
        handler so the assertion is non-vacuous: neuter (skip the scope
        check inside call_read_tool) → the handler WOULD run and the
        call-counter would flip to 1, turning this RED."""
        original_tool = MEMORY_TOOL_REGISTRY["recall_decisions"]
        calls: list[dict[str, Any]] = []

        async def _spy(
            user_context: UserContext, args: dict[str, Any]
        ) -> dict[str, Any]:
            calls.append(args)
            return await original_tool.handler(user_context, args)

        MEMORY_TOOL_REGISTRY["recall_decisions"] = replace(original_tool, handler=_spy)
        try:
            _override_identity(client, "u-no-scope", scopes=())
            try:
                r = client.post(
                    "/mcp",
                    json=_rpc(
                        "tools/call",
                        {"name": "recall_decisions", "arguments": {"query": "x"}},
                    ),
                )
            finally:
                client.app.dependency_overrides.clear()

            assert r.status_code == 200
            body = r.json()["result"]
            assert body["isError"] is True
            assert body.get("structuredContent") is None
            assert calls == [], "handler must never execute on scope denial"
        finally:
            MEMORY_TOOL_REGISTRY["recall_decisions"] = original_tool

    @pytest.mark.asyncio
    async def test_unknown_tool_denied_with_jsonrpc_result_error(
        self, client: TestClient
    ) -> None:
        r = client.post(
            "/mcp",
            json=_rpc("tools/call", {"name": "does_not_exist", "arguments": {}}),
        )
        assert r.status_code == 200
        body = r.json()["result"]
        assert body["isError"] is True

    def test_tools_call_missing_name_is_invalid_params(
        self, client: TestClient
    ) -> None:
        r = client.post("/mcp", json=_rpc("tools/call", {"arguments": {}}))
        assert r.status_code == 200
        assert r.json()["error"]["code"] == -32602


# ─────────────────────────── identity / isolation ───────────────────────────


class TestIdentityBinding:
    @pytest.mark.asyncio
    async def test_caller_supplied_user_id_argument_is_ignored(
        self, client: TestClient, test_container
    ) -> None:
        """Falsifiable acceptance #5: token sub wins, the arg is ignored.
        Seed a session under the TOKEN identity 'alice'; call with an
        ``arguments.user_id`` claiming to be 'mallory'. The result must
        reflect alice's own data — proving the handler never consulted
        the spoofed argument."""
        alice = _override_identity(
            client, "alice", scopes=("memory:conversational:read-own",)
        )
        try:
            await test_container._instances["conversational"].save_session(
                alice,
                "AuditTrace",
                "alice's session",
                ["alice decided X"],
                session_id="sess-alice-1",
            )
            r = client.post(
                "/mcp",
                json=_rpc(
                    "tools/call",
                    {
                        "name": "recall_recent_sessions",
                        "arguments": {
                            "project": "AuditTrace",
                            "n": 5,
                            "user_id": "mallory",
                        },
                    },
                ),
            )
        finally:
            client.app.dependency_overrides.clear()

        body = r.json()["result"]
        assert body["isError"] is False
        assert body["structuredContent"]["total"] == 1
        assert "alice" in body["structuredContent"]["matches"][0]["snippet"]

    @pytest.mark.asyncio
    async def test_cross_tenant_isolation_via_recall_recent_sessions(
        self, client: TestClient, test_container, _real_auth_stack: None
    ) -> None:
        """Falsifiable acceptance #4. Two identities, two seeded
        sessions; caller B must see ZERO of caller A's rows.

        **Reviewer finding fix (2026-08-23, fixture-masked vacuous
        guard):** this test used to drive identity via
        ``client.app.dependency_overrides[require_user] = lambda: identity``,
        which REPLACES ``require_user``'s entire body — ``set_current_user_id``
        (the RLS ContextVar binding the spec + ADR-063 claim isolation
        rests on) never ran, so the test was vacuous for that specific
        guard even though it correctly proved the Phase-2 app-level
        filter. Rewritten to use TWO DISTINCT, genuinely SIGNED JWTs
        (``_real_auth_stack`` + ``_make_real_jwt``) with NO
        ``dependency_overrides`` — ``require_user``'s real cold path
        (JWT verify → JWKS → UserContext → cache → ``set_current_user_id``)
        executes for both alice and bob. Neuter (e.g. the handler
        ignoring ``user_context`` and querying globally) → this test
        goes RED because bob would see alice's session.

        RLS's OWN independent sufficiency — i.e. that Postgres RLS
        would still block this leak even if the Phase-2 app-level
        ``.filter()`` were ever missing or wrong, per the reviewer's
        "a missing/wrong .filter still cannot leak" framing — is
        proven separately (live Postgres, no app filter in the query
        path) in ``tests/test_mcp_rls_isolation.py``, which extends the
        SAME ``sessions``-table policy proof used generically by
        ``tests/test_rls_isolation.py``. This test proves the request
        layer: two distinct real callers, each correctly isolated."""
        alice_sub = "alice-jwt-user"
        bob_sub = "bob-jwt-user"
        alice_seed_ctx = replace(
            sentinel_user_context(), user_id=alice_sub, is_admin=False
        )
        await test_container._instances["conversational"].save_session(
            alice_seed_ctx,
            "AuditTrace",
            "alice's private session",
            [],
            session_id="sess-alice-2",
        )

        bob_token = _make_real_jwt(
            sub=bob_sub, scope="memory:conversational:read-own", jti="jti-bob-1"
        )
        r = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {
                    "name": "recall_recent_sessions",
                    "arguments": {"project": "AuditTrace", "n": 5},
                },
            ),
            headers={"Authorization": f"Bearer {bob_token}"},
        )

        body = r.json()["result"]
        assert body["structuredContent"]["total"] == 0
        assert body["structuredContent"]["matches"] == []

        # Sanity: bob is not simply denied outright (which would make the
        # zero-rows assertion vacuous too) — he genuinely authenticated
        # and authorized, just sees none of alice's data.
        assert body["isError"] is False

        # And alice, using her OWN real token, sees her own row — proving
        # the zero-for-bob result is isolation, not a broken query.
        alice_token = _make_real_jwt(
            sub=alice_sub, scope="memory:conversational:read-own", jti="jti-alice-1"
        )
        r2 = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {
                    "name": "recall_recent_sessions",
                    "arguments": {"project": "AuditTrace", "n": 5},
                },
            ),
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        body2 = r2.json()["result"]
        assert body2["structuredContent"]["total"] == 1

    def test_rls_contextvar_bound_at_dispatch_time(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The MCP route depends on the SAME ``require_user`` dependency
        the chat proxy uses, which binds ``app.current_user_id`` — the
        ContextVar that drives real Postgres RLS in production — before
        any handler runs (``feedback_unit_tests_miss_rls``). No
        ``require_user`` override here: the real dependency executes and
        must have already bound the ContextVar by the time
        ``call_read_tool`` is dispatched. Captured INSIDE the
        still-running request task, because a ContextVar mutation made
        inside an asyncio task does not propagate back out to the
        caller once the task completes — reading it after
        ``client.post`` returns would prove nothing. Neuter (swap
        ``require_user`` for a dependency that never calls
        ``set_current_user_id``) → this test goes RED because the
        captured value would be ``None``, not the sentinel's user_id."""
        import audittrace.routes.mcp as mcp_route

        seen_user_ids: list[str | None] = []
        real_call_read_tool = mcp_route.call_read_tool

        async def _spy(**kwargs: Any) -> Any:
            seen_user_ids.append(rls_module.current_user_id())
            return await real_call_read_tool(**kwargs)

        monkeypatch.setattr(mcp_route, "call_read_tool", _spy)

        client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {"name": "recall_decisions", "arguments": {"query": "x"}},
            ),
        )

        assert seen_user_ids == [sentinel_user_context().user_id]

    def test_rls_contextvar_rebinds_per_real_caller_no_stale_leak(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        _real_auth_stack: None,
    ) -> None:
        """Generalises ``test_rls_contextvar_bound_at_dispatch_time`` (one
        sentinel identity, bypass mode) to TWO DISTINCT REAL callers via
        genuinely signed JWTs, driven through ``require_user``'s real
        body with NO override — the reviewer's fix instruction #1.

        Proves the ContextVar REBINDS per request rather than leaking a
        stale value from the prior caller: alice's request must bind
        alice's sub, and bob's very next request (same TestClient, same
        underlying worker) must bind bob's sub, not alice's stale one.

        Non-vacuous per fix instruction #3: monkeypatching
        ``audittrace.auth.set_current_user_id`` to a no-op — the exact
        three call sites the reviewer named inside ``require_user`` all
        route through this one module-level name — must make bob's
        captured ContextVar value WRONG (not his own sub). Restore →
        correct again."""
        import audittrace.routes.mcp as mcp_route

        seen_user_ids: list[str | None] = []
        real_call_read_tool = mcp_route.call_read_tool

        async def _spy(**kwargs: Any) -> Any:
            seen_user_ids.append(rls_module.current_user_id())
            return await real_call_read_tool(**kwargs)

        monkeypatch.setattr(mcp_route, "call_read_tool", _spy)

        alice_token = _make_real_jwt(
            sub="alice-rebind", scope="memory:episodic:read", jti="jti-rebind-alice"
        )
        bob_token = _make_real_jwt(
            sub="bob-rebind", scope="memory:episodic:read", jti="jti-rebind-bob"
        )

        client.post(
            "/mcp",
            json=_rpc(
                "tools/call", {"name": "recall_decisions", "arguments": {"query": "x"}}
            ),
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        client.post(
            "/mcp",
            json=_rpc(
                "tools/call", {"name": "recall_decisions", "arguments": {"query": "x"}}
            ),
            headers={"Authorization": f"Bearer {bob_token}"},
        )

        assert seen_user_ids == ["alice-rebind", "bob-rebind"]

        # ── Neuter-and-confirm (reviewer fix instruction #3) ──
        seen_user_ids.clear()
        rls_module.set_current_user_id(None)  # clear any leftover binding

        def _neutered_set_current_user_id(_user_id: str | None) -> None:
            pass  # the exact bug class the reviewer's finding is about

        monkeypatch.setattr(
            "audittrace.auth.set_current_user_id", _neutered_set_current_user_id
        )

        client.post(
            "/mcp",
            json=_rpc(
                "tools/call", {"name": "recall_decisions", "arguments": {"query": "x"}}
            ),
            headers={"Authorization": f"Bearer {bob_token}"},
        )

        assert seen_user_ids == [None], (
            "neutering set_current_user_id must leave the ContextVar unbound "
            "(RED) — got a value, meaning something else is still binding it"
        )
