"""HTTP-level tests for the write/curation ``POST /mcp`` surface (ADR-063
Phase 2 Track A).

Exercises the whole stack — the FastAPI route, ``require_user`` auth +
identity binding, ``services.mcp_write_bridge``, the real registered
``write_decision``/``write_skill`` handlers (writing into the test
container's ``MockSemanticService`` + ``MockMemoryManifestService``), and
the SAME tamper-evident audit path Phase 1/Track B use (``InteractionRecord``
+ ``ToolCall``) — through the public JSON-RPC transport a real MCP client
would speak.

Self-contained auth helpers (not imported from ``test_mcp_routes.py``) —
this file follows the "per-file self-contained auth helpers" convention
(``test_admin_routes.py`` / ``test_mcp_broker_routes.py``). Per the
spec's own instruction, the denied-write proof drives identity via a
REAL signed JWT through the REAL ``require_user`` — no
``client.app.dependency_overrides[require_user]`` bypass anywhere in
this file.

Falsifiable acceptance criteria covered (spec Rule 1, Track A):

  1. A write tool appears in ``tools/list`` ONLY for a caller holding its
     own per-tool operator scope — ``TestToolsListManifest``.
  2. A caller WITHOUT the scope is denied on ``tools/call``: no mutation
     (the mock semantic service stays empty), no data returned, and an
     audit-DENIED ``ToolCall`` row is written —
     ``TestScopeDeniedNoMutation.test_missing_scope_denied_no_mutation_no_data_audit_row``.
  3. Per-TOOL scope, not per-tier: holding ``memory:decisions:write``
     does NOT authorize ``write_skill`` (Track B review's design input,
     applied) — ``TestScopeDeniedNoMutation.test_decisions_scope_does_not_authorize_write_skill``.
  4. A valid-scope call executes: the document lands in the semantic
     service (private tier), a manifest row + a memory-audit event are
     recorded, AND exactly one ``ToolCall``/``InteractionRecord`` audit
     row is written — ``TestValidScopeWritesDocumentAndAudit``.
  5. Caller-supplied identity/tier is ignored — token sub + fixed
     ``tier="private"`` always win — ``TestCallerSuppliedFieldsIgnored``.
  6. Handler-level failures (validation, upsert exception, audit-write
     exception) are reported as errors, never silent success —
     ``TestHandlerFailurePaths``.
"""

from __future__ import annotations

import importlib
import time
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization as _crypto_serialization
from cryptography.hazmat.primitives.asymmetric import rsa as _crypto_rsa
from fastapi.testclient import TestClient
from jose import jwt as _jose_jwt
from sqlalchemy import select

import audittrace.tools.memory_handlers as memory_handlers_mod
from audittrace.db.models import InteractionRecord, ToolCall
from audittrace.dependencies import get_postgres_factory
from audittrace.identity import UserContext, sentinel_user_context
from audittrace.tools import mcp_write_handlers as write_handlers_mod
from audittrace.tools import reset_registry_for_tests
from audittrace.tools.mcp_write_registry import reset_mcp_write_registry_for_tests

_READ_TOOL_NAMES = {
    "recall_decisions",
    "recall_skills",
    "recall_recent_sessions",
    "recall_semantic",
    "read_decision",
    "read_skill",
}


@pytest.fixture(autouse=True)
def _fresh_registries_with_real_handlers():
    """Every test in this file depends on BOTH the real Phase 1 read
    tools AND the real ``write_decision``/``write_skill`` handlers,
    freshly reloaded — a prior test FILE's ``reset_registry_for_tests()``
    teardown (e.g. ``test_mcp_bridge.py``, which leaves
    ``MEMORY_TOOL_REGISTRY`` empty on purpose) can otherwise leak an
    empty registry into this file when the whole suite runs together.
    Mirrors ``test_mcp_routes.py::_fresh_registry_with_real_handlers``."""
    reset_registry_for_tests()
    importlib.reload(memory_handlers_mod)
    reset_mcp_write_registry_for_tests()
    importlib.reload(write_handlers_mod)
    yield
    reset_registry_for_tests()
    reset_mcp_write_registry_for_tests()


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


# ───────────────────── Real-JWT machinery (no dependency_overrides) ─────────

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
    """A genuinely signed RS256 JWT, decoded by the REAL
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
    real cold path (JWT verify → JWKS → UserContext → cache) against a
    throwaway signed keypair, with a fakeredis-backed token cache."""
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


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ───────────────────────────── tools/list manifest ──────────────────────────


class TestToolsListManifest:
    def test_manifest_omits_write_tools_for_default_sentinel_caller(
        self, client: TestClient
    ) -> None:
        """The default (AUDITTRACE_AUTH_REQUIRED=false) bypass identity is
        ``is_admin=True`` but holds NEITHER write scope literally — write
        tools must stay off the manifest for it (no admin bypass, see
        ``services.mcp_write_bridge``'s module docstring). This also pins
        Phase 1's own manifest test (``test_mcp_routes.py::
        test_manifest_lists_only_the_real_read_tools``) staying green
        unmodified."""
        r = client.post("/mcp", json=_rpc("tools/list", {}))
        assert r.status_code == 200
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert "write_decision" not in names
        assert "write_skill" not in names
        assert names == _READ_TOOL_NAMES

    def test_manifest_includes_only_the_scoped_write_tool(
        self, client: TestClient, _real_auth_stack: None
    ) -> None:
        token = _make_real_jwt(
            sub="curator-1", scope="memory:decisions:write", jti="jti-list-1"
        )
        r = client.post("/mcp", json=_rpc("tools/list", {}), headers=_bearer(token))
        assert r.status_code == 200
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert "write_decision" in names
        assert "write_skill" not in names


# ───────────────────── scope-deny before mutation (falsifiable #2/#3) ──────


class TestScopeDeniedNoMutation:
    @pytest.mark.asyncio
    async def test_missing_scope_denied_no_mutation_no_data_audit_row(
        self, client: TestClient, test_container, _real_auth_stack: None
    ) -> None:
        """Falsifiable acceptance #2. Neuter the scope check inside
        ``mcp_write_bridge.call_write_tool`` (e.g. always allow) → this
        test goes RED because the mock semantic service would then hold
        the 'decisions' document that must never exist here."""
        token = _make_real_jwt(sub="no-scope-user", scope="", jti="jti-noscope")
        r = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {
                    "name": "write_decision",
                    "arguments": {
                        "document_id": "adr-999",
                        "text": "should never land",
                    },
                },
            ),
            headers=_bearer(token),
        )
        assert r.status_code == 200
        body = r.json()["result"]
        assert body["isError"] is True
        assert body.get("structuredContent") is None

        semantic = test_container._instances["semantic"]
        assert semantic._docs.get("decisions", []) == [], (
            "handler must never execute on scope denial — no mutation"
        )

        manifest = test_container._instances["memory_manifest"]
        assert ("semantic", "decisions/adr-999") not in manifest._rows

        rows = await _tool_call_rows()
        assert len(rows) == 1
        assert rows[0].tool_name == "write_decision"
        assert rows[0].error is not None
        assert "scope denied" in rows[0].error
        assert rows[0].granted_scope == "memory:decisions:write"

        interactions = await _interaction_rows()
        mine = [i for i in interactions if i.id == rows[0].interaction_id]
        assert len(mine) == 1
        assert mine[0].status == "failed"
        assert mine[0].failure_class == "scope_denied"

    @pytest.mark.asyncio
    async def test_decisions_scope_does_not_authorize_write_skill(
        self, client: TestClient, test_container, _real_auth_stack: None
    ) -> None:
        """Falsifiable acceptance #3 — per-TOOL scope, not per-tier
        (Track B review's design input applied here). Neuter
        ``call_write_tool`` to check membership against ANY
        ``memory:*:write`` scope instead of the tool's OWN
        ``required_scope`` → this test goes RED."""
        token = _make_real_jwt(
            sub="decisions-only-user",
            scope="memory:decisions:write",
            jti="jti-cross-scope",
        )
        r = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {
                    "name": "write_skill",
                    "arguments": {"document_id": "skill-999", "text": "must not land"},
                },
            ),
            headers=_bearer(token),
        )
        assert r.status_code == 200
        assert r.json()["result"]["isError"] is True

        semantic = test_container._instances["semantic"]
        assert semantic._docs.get("skills", []) == []


# ───────────────────── valid scope: mutation + audit (falsifiable #4) ──────


class TestValidScopeWritesDocumentAndAudit:
    @pytest.mark.asyncio
    async def test_valid_scope_writes_document_manifest_and_audit_rows(
        self, client: TestClient, test_container, _real_auth_stack: None
    ) -> None:
        token = _make_real_jwt(
            sub="curator-2", scope="memory:decisions:write", jti="jti-valid-1"
        )
        r = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {
                    "name": "write_decision",
                    "arguments": {
                        "document_id": "adr-100",
                        "text": "We decided to ship Track A.",
                        "title": "ADR-100",
                    },
                },
            ),
            headers=_bearer(token),
        )
        assert r.status_code == 200
        body = r.json()["result"]
        assert body["isError"] is False
        assert body["structuredContent"]["document_id"] == "adr-100"
        assert body["structuredContent"]["collection"] == "decisions"
        assert body["structuredContent"]["tier"] == "private"

        semantic = test_container._instances["semantic"]
        docs = semantic._docs.get("decisions", [])
        assert len(docs) == 1
        assert docs[0].page_content == "We decided to ship Track A."
        assert docs[0].metadata.get("tier") == "private"

        manifest = test_container._instances["memory_manifest"]
        assert ("semantic", "decisions/adr-100") in manifest._rows
        row = manifest._rows[("semantic", "decisions/adr-100")]
        assert row["created_by_user_id"] == "curator-2"
        assert row["tier"] == "private"

        tool_rows = await _tool_call_rows()
        assert len(tool_rows) == 1
        assert tool_rows[0].tool_name == "write_decision"
        assert tool_rows[0].error is None
        assert tool_rows[0].user_id == "curator-2"
        assert tool_rows[0].granted_scope == "memory:decisions:write"

        interactions = await _interaction_rows()
        mcp_rows = [i for i in interactions if i.source == "mcp"]
        assert len(mcp_rows) == 1
        assert mcp_rows[0].status == "success"

        # The SAME memory-audit trail routes/memory.py's create_semantic
        # produces (services.memory_audit.emit_memory_audit_event).
        audit_rows = [i for i in interactions if i.source == "memory-audit"]
        assert len(audit_rows) == 1
        assert "op=write" in audit_rows[0].question
        assert "layer=semantic" in audit_rows[0].question

    @pytest.mark.asyncio
    async def test_write_skill_valid_scope_writes_document(
        self, client: TestClient, test_container, _real_auth_stack: None
    ) -> None:
        """Same shape as write_decision, exercised for write_skill so its
        own registered handler (not just its scope-deny path) is
        covered."""
        token = _make_real_jwt(
            sub="curator-6", scope="memory:skills:write", jti="jti-valid-skill"
        )
        r = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {
                    "name": "write_skill",
                    "arguments": {
                        "document_id": "skill-1",
                        "text": "Always run make test before committing.",
                    },
                },
            ),
            headers=_bearer(token),
        )
        assert r.status_code == 200
        body = r.json()["result"]
        assert body["isError"] is False
        assert body["structuredContent"]["collection"] == "skills"

        semantic = test_container._instances["semantic"]
        docs = semantic._docs.get("skills", [])
        assert len(docs) == 1
        assert docs[0].metadata.get("tier") == "private"

        manifest = test_container._instances["memory_manifest"]
        assert ("semantic", "skills/skill-1") in manifest._rows


class TestMcpWriteNoBypassOfCorpusGuard:
    """SPEC security-memory-write-authorization-choke (2026-08-30) —
    SUPERSEDES the manifest-only fix. ``mcp_write`` is documented as
    PRIVATE-tier only (``write_decision``/``write_skill`` never accept a
    caller-supplied tier). This proves the bridge gains NO bypass of the
    pre-write choke: a caller who happens to pick a ``document_id`` that
    collides with an EXISTING corpus item must not be able to overwrite
    its title/ownership OR its underlying ChromaDB content via MCP, same
    as the REST ``POST /memory/semantic`` path — and, per the third-review
    finding, the ChromaDB write must never land AT ALL (not "land then
    get reported as an error").

    FALSIFIABLE: neuter ``authorize_write`` in
    ``services/memory_manifest.py`` (e.g. make it return immediately
    without ever raising) and
    ``test_write_decision_denied_over_existing_corpus_row`` goes RED (the
    corpus row's title/owner AND its ChromaDB content silently change to
    the attacker's); restore it and it goes GREEN.
    """

    @pytest.mark.asyncio
    async def test_write_decision_denied_over_existing_corpus_row(
        self, client: TestClient, test_container, _real_auth_stack: None
    ) -> None:
        manifest = test_container._instances["memory_manifest"]
        await manifest.record_create(
            "semantic",
            "decisions/adr-999",
            "Shared ADR",
            10,
            "curator-orig",
            tier="corpus",
        )
        semantic = test_container._instances["semantic"]
        await semantic.upsert(
            None,
            "decisions",
            "adr-999",
            "shared corpus content",
            {"tier": "corpus", "user_id": "curator-orig"},
            tier="corpus",
        )

        token = _make_real_jwt(
            sub="attacker-mcp", scope="memory:decisions:write", jti="jti-mcp-hijack"
        )
        r = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {
                    "name": "write_decision",
                    "arguments": {
                        "document_id": "adr-999",
                        "text": "attacker content",
                        "title": "pwned",
                    },
                },
            ),
            headers=_bearer(token),
        )
        assert r.status_code == 200
        body = r.json()["result"]
        assert body["isError"] is True

        row = manifest._rows[("semantic", "decisions/adr-999")]
        assert row["tier"] == "corpus"
        assert row["title"] == "Shared ADR"
        assert row["created_by_user_id"] == "curator-orig"
        assert row["modified_by_user_id"] == "curator-orig"

        # SPEC security-memory-write-authorization-choke: the ChromaDB
        # content is byte-unchanged — the write never landed at all, not
        # "landed then got reported as an error" (the third-review gap).
        docs = [
            d
            for d in semantic._docs.get("decisions", [])
            if d.metadata.get("document_id") == "adr-999"
        ]
        assert len(docs) == 1
        assert docs[0].page_content == "shared corpus content"
        assert docs[0].metadata.get("user_id") == "curator-orig"

    @pytest.mark.asyncio
    async def test_manifest_layer_guard_still_protects_if_choke_is_bypassed(
        self, client: TestClient, test_container
    ) -> None:
        """Defense-in-depth, proven independently: even if the PRIMARY
        ``authorize_write`` choke were bypassed (patched to a no-op here,
        simulating a bug/gap in it), ``record_create``'s OWN guard still
        denies the write — the manifest-layer guards this branch stays
        kept for are not decorative. ``client`` is depended on (unused
        directly) so the ``app`` fixture's DI-container swap runs before
        ``write_decision`` resolves its services — mirrors every other
        test in this class."""
        from unittest.mock import AsyncMock, patch

        from audittrace.tools.mcp_write_handlers import write_decision

        manifest = test_container._instances["memory_manifest"]
        await manifest.record_create(
            "semantic",
            "decisions/adr-defense-in-depth",
            "Shared ADR",
            10,
            "curator-orig",
            tier="corpus",
        )
        attacker = UserContext(
            user_id="attacker-defense-in-depth",
            username="attacker-defense-in-depth",
            agent_type="test",
            scopes=("memory:decisions:write",),
            is_admin=False,
        )

        with patch(
            "audittrace.tools.mcp_write_handlers.authorize_write",
            AsyncMock(return_value=None),
        ):
            result = await write_decision(
                attacker,
                {
                    "document_id": "adr-defense-in-depth",
                    "text": "attacker content",
                    "title": "pwned",
                },
            )
        assert "error" in result

        row = manifest._rows[("semantic", "decisions/adr-defense-in-depth")]
        assert row["tier"] == "corpus"
        assert row["title"] == "Shared ADR"
        assert row["created_by_user_id"] == "curator-orig"
        assert row["modified_by_user_id"] == "curator-orig"


# ───────────────────── caller-supplied identity/tier ignored ───────────────


class TestCallerSuppliedFieldsIgnored:
    @pytest.mark.asyncio
    async def test_caller_cannot_forge_tier_or_user_id(
        self, client: TestClient, test_container, _real_auth_stack: None
    ) -> None:
        """Falsifiable acceptance #5. A caller holding only the private-
        tier ``memory:decisions:write`` scope tries to smuggle
        ``metadata={"tier": "corpus"}`` and ``arguments.user_id="mallory"``
        — both must be ignored; the write always lands private, attributed
        to the token sub."""
        token = _make_real_jwt(
            sub="real-writer", scope="memory:decisions:write", jti="jti-forge-1"
        )
        r = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {
                    "name": "write_decision",
                    "arguments": {
                        "document_id": "adr-200",
                        "text": "attempted forge",
                        "user_id": "mallory",
                        "metadata": {"tier": "corpus", "user_id": "mallory"},
                    },
                },
            ),
            headers=_bearer(token),
        )
        assert r.status_code == 200
        assert r.json()["result"]["isError"] is False

        semantic = test_container._instances["semantic"]
        docs = semantic._docs.get("decisions", [])
        assert len(docs) == 1
        assert docs[0].metadata.get("tier") == "private"
        assert docs[0].metadata.get("user_id") != "mallory"

        manifest = test_container._instances["memory_manifest"]
        row = manifest._rows[("semantic", "decisions/adr-200")]
        assert row["created_by_user_id"] == "real-writer"
        assert row["tier"] == "private"


# ───────────────────────── handler-level failure paths ─────────────────────


class TestHandlerFailurePaths:
    @pytest.mark.asyncio
    async def test_missing_required_args_is_error_no_mutation(
        self, client: TestClient, test_container, _real_auth_stack: None
    ) -> None:
        token = _make_real_jwt(
            sub="curator-3", scope="memory:decisions:write", jti="jti-badargs"
        )
        r = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {"name": "write_decision", "arguments": {"document_id": "adr-300"}},
            ),
            headers=_bearer(token),
        )
        assert r.status_code == 200
        assert r.json()["result"]["isError"] is True
        semantic = test_container._instances["semantic"]
        assert semantic._docs.get("decisions", []) == []

    @pytest.mark.asyncio
    async def test_missing_document_id_is_error_no_mutation(
        self, client: TestClient, test_container, _real_auth_stack: None
    ) -> None:
        token = _make_real_jwt(
            sub="curator-3b", scope="memory:decisions:write", jti="jti-badargs-2"
        )
        r = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {"name": "write_decision", "arguments": {"text": "no id here"}},
            ),
            headers=_bearer(token),
        )
        assert r.status_code == 200
        body = r.json()["result"]
        assert body["isError"] is True
        assert "document_id" in body["content"][0]["text"]
        semantic = test_container._instances["semantic"]
        assert semantic._docs.get("decisions", []) == []

    @pytest.mark.asyncio
    async def test_non_string_title_is_error_no_mutation(
        self, client: TestClient, test_container, _real_auth_stack: None
    ) -> None:
        token = _make_real_jwt(
            sub="curator-3c", scope="memory:decisions:write", jti="jti-badtitle"
        )
        r = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {
                    "name": "write_decision",
                    "arguments": {
                        "document_id": "adr-350",
                        "text": "x",
                        "title": 12345,
                    },
                },
            ),
            headers=_bearer(token),
        )
        assert r.status_code == 200
        body = r.json()["result"]
        assert body["isError"] is True
        assert "title" in body["content"][0]["text"]
        semantic = test_container._instances["semantic"]
        assert semantic._docs.get("decisions", []) == []

    @pytest.mark.asyncio
    async def test_upsert_exception_reported_as_error_no_manifest_row(
        self,
        client: TestClient,
        test_container,
        _real_auth_stack: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("chroma unavailable")

        monkeypatch.setattr(test_container._instances["semantic"], "upsert", _boom)
        token = _make_real_jwt(
            sub="curator-4", scope="memory:decisions:write", jti="jti-boom"
        )
        r = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {
                    "name": "write_decision",
                    "arguments": {"document_id": "adr-400", "text": "x"},
                },
            ),
            headers=_bearer(token),
        )
        assert r.status_code == 200
        assert r.json()["result"]["isError"] is True

        manifest = test_container._instances["memory_manifest"]
        assert ("semantic", "decisions/adr-400") not in manifest._rows

    @pytest.mark.asyncio
    async def test_memory_audit_write_failure_reported_as_error(
        self,
        client: TestClient,
        test_container,
        _real_auth_stack: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The mutation + manifest row already landed by the time the
        memory-audit event write is attempted (matches
        ``routes/memory.py::_emit_write_audit``'s existing fail-closed
        shape) — the caller sees an explicit error, never a false
        'success'."""

        async def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("audit db unavailable")

        monkeypatch.setattr(write_handlers_mod, "emit_memory_audit_event", _boom)

        token = _make_real_jwt(
            sub="curator-5", scope="memory:decisions:write", jti="jti-auditboom"
        )
        r = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {
                    "name": "write_decision",
                    "arguments": {"document_id": "adr-500", "text": "x"},
                },
            ),
            headers=_bearer(token),
        )
        assert r.status_code == 200
        body = r.json()["result"]
        assert body["isError"] is True
        assert "mutation not confirmed audited" in body["content"][0]["text"]

        semantic = test_container._instances["semantic"]
        assert len(semantic._docs.get("decisions", [])) == 1


# ─────────────────── sentinel identity keeps existing behaviour ────────────


class TestSentinelUnaffected:
    def test_default_sentinel_call_of_write_tool_is_denied(
        self, client: TestClient, test_container
    ) -> None:
        """The default bypass identity (``is_admin=True``, sentinel
        scopes) must be denied on ``tools/call`` too — the defense-in-
        depth edge, not just ``tools/list`` filtering."""
        r = client.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {
                    "name": "write_decision",
                    "arguments": {"document_id": "adr-600", "text": "x"},
                },
            ),
        )
        assert r.status_code == 200
        assert r.json()["result"]["isError"] is True
        semantic = test_container._instances["semantic"]
        assert semantic._docs.get("decisions", []) == []
        assert sentinel_user_context().is_admin is True
