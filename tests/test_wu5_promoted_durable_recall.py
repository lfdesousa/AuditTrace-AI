"""D4 — promoted-durable recall PROOF (WU-5, Sovereign-Attach EPIC,
2026-09-06-SPEC-wu5-same-turn-session-recall.md). NO new production
code — this file proves an EXISTING mechanism end-to-end, through the
REAL routes, per the spec's explicit framing:

    "Promoted durable recall — PROOF, not code. WU-4 semantic-promote
    upserts into ChromaSemanticService stamping meta.user_id (the
    existing private-tier pattern), so a promoted semantic doc is
    ALREADY reachable by recall_semantic, owner-scoped. WU-5 must
    PROVE this end-to-end, add no new code for it."

Flow: promote a session doc to ``semantic`` via the REAL
``POST /memory/promote`` route (WU-4), then ``recall_semantic``
(through the tool-dispatch path, ``invoke_tool`` — the exact mechanism
the ``/v1`` tool loop uses) returns it for its owner and NOT for
another user.

Reuses the ``real_semantic_client`` fixture pattern from
``tests/test_memory_promote_route.py::TestPromoteSemanticRealHttpRoundTrip``
— a REAL ``ChromaSemanticService`` (backed by the in-repo fake ChromaDB
client, ``MockChromaDBFactory``), NOT ``MockSemanticService``, because
the mock's ``get_document``/``search_page`` do not enforce
``_tier_authorized``'s per-user ``where`` filter and would pass this
proof vacuously (feedback_test_through_real_http_route: a fix must not
regress the read path — proving it needs the REAL scoping logic under
test, not a mock that never denies anything).
"""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Side-effect import — running the module is what runs the
# @register_memory_tool decorators, including recall_semantic.
import audittrace.tools.memory_handlers as handlers_mod
from audittrace.identity import sentinel_user_context
from audittrace.tools import get_tool_by_name, invoke_tool, reset_registry_for_tests


@pytest.fixture(autouse=True)
def _fresh_registry_with_handlers():
    """Reset the registry and re-run the decorator pass for each test
    (mirrors tests/test_memory_tool_handlers.py's identical fixture) —
    the MEMORY_TOOL_REGISTRY is a process-global mutable singleton other
    test files reset in their OWN teardown, so a test file that only
    relies on the module-level side-effect import at collection time can
    see an EMPTY registry depending on run order. Self-sufficient here."""
    reset_registry_for_tests()
    importlib.reload(handlers_mod)
    yield
    reset_registry_for_tests()


class _Auth:
    """Mirrors ``tests/test_memory_promote_route.py::_Auth`` — patches the
    JWT-decode chain so the real HTTP route sees a token for *sub* with
    *scope*, without needing a live Keycloak."""

    def __init__(self, sub: str, scope: str) -> None:
        self.sub = sub
        self.scope = scope

    def __enter__(self):
        self._patches = [
            patch("audittrace.auth.get_settings"),
            patch("audittrace.auth._get_jwks_keys"),
            patch("audittrace.auth._decode_jwt_with_allowed_issuers"),
        ]
        mocks = [p.__enter__() for p in self._patches]
        mock_settings, mock_jwks, mock_decode = mocks
        mock_settings.return_value = MagicMock(auth_enabled=True, auth_required=True)
        mock_jwks.return_value = ["fake-key"]
        mock_decode.return_value = {"sub": self.sub, "scope": self.scope}
        return self

    def __exit__(self, *exc: Any) -> None:
        for p in reversed(self._patches):
            p.__exit__(*exc)


def _upload_session_doc(
    client: TestClient, *, sub: str, filename: str, content: bytes
) -> None:
    """Seed a session-layer document via the real (WU-1) upload route."""
    with (
        _Auth(sub, "memory:session:write"),
        patch(
            "audittrace.routes.memory._get_minio_client",
            return_value=MagicMock(),
        ),
    ):
        response = client.post(
            "/memory/upload",
            params={"layer": "session"},
            files={"file": (filename, content, "text/plain")},
            headers={"Authorization": "Bearer session-token"},
        )
    assert response.status_code == 200, response.text


def _promote(
    client: TestClient, *, sub: str, scope: str, payload: dict[str, Any]
) -> Any:
    """Hit the real WU-4 ``POST /memory/promote`` route."""
    with _Auth(sub, scope):
        return client.post(
            "/memory/promote",
            json=payload,
            headers={"Authorization": "Bearer promote-token"},
        )


def _user_for(sub: str, scope: str) -> Any:
    """Build the ``UserContext`` ``invoke_tool`` needs for the recall
    half — same shape ``require_user`` would build from a real JWT
    carrying this ``sub``/``scope``."""
    return replace(
        sentinel_user_context(),
        user_id=sub,
        username=sub,
        is_admin=False,
        scopes=(scope,),
    )


@pytest.fixture
def real_semantic_client(monkeypatch: pytest.MonkeyPatch):
    """A TestClient wired to the REAL ``ChromaSemanticService`` (see
    module docstring for why the mock would pass this proof vacuously)."""
    from audittrace import dependencies
    from audittrace.db.factory import MockChromaDBFactory
    from audittrace.dependencies import create_test_container, reset_container
    from audittrace.server import create_app
    from audittrace.services.semantic import ChromaSemanticService

    monkeypatch.setattr(
        "audittrace.services.semantic.embed_via_nomic",
        AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]),
    )

    factory = MockChromaDBFactory()
    real_client = asyncio.run(factory.get_client())
    real_semantic = ChromaSemanticService(
        client=real_client, default_collections=["semantic", "decisions"]
    )

    test_container = create_test_container()
    test_container._instances["semantic"] = real_semantic
    dependencies.container = test_container
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_container()


class TestPromotedDurableRecallProof:
    """D4 — the proof, both halves of acceptance (d)."""

    def test_owner_recalls_promoted_doc_via_recall_semantic(
        self, real_semantic_client: TestClient
    ) -> None:
        _upload_session_doc(
            real_semantic_client,
            sub="wu5-alice",
            filename="attached-notes.md",
            content=b"the quarterly roadmap review notes",
        )
        promote_resp = _promote(
            real_semantic_client,
            sub="wu5-alice",
            scope="memory:semantic:write",
            payload={"filename": "attached-notes.md", "target_layer": "semantic"},
        )
        assert promote_resp.status_code == 200, promote_resp.text

        alice = _user_for("wu5-alice", "memory:semantic:read")
        tool = get_tool_by_name("recall_semantic")
        assert tool is not None
        result, _ = asyncio.run(
            invoke_tool(
                alice,
                tool,
                {"query": "quarterly roadmap review"},
                session_id="wu5-sess",
            )
        )
        assert result["total"] >= 1
        assert any(
            "quarterly roadmap review" in m["snippet"] for m in result["matches"]
        ), f"owner's recall_semantic did not surface the promoted doc: {result}"

    def test_cross_user_recall_semantic_never_sees_others_promoted_doc(
        self, real_semantic_client: TestClient
    ) -> None:
        """Non-vacuity guard 5 (spec §6.5): another user's recall_semantic
        must never surface Alice's promoted document — proves the
        EXISTING ``search_page`` per-user ``where={"user_id": ...}``
        filter (services/semantic.py) still holds for a doc that
        ARRIVED via the promote path, not the direct /memory/semantic
        write path recall_semantic's other tests already cover."""
        _upload_session_doc(
            real_semantic_client,
            sub="wu5-alice-2",
            filename="private-attachment.md",
            content=b"a uniquely identifiable phrase xyzzy123",
        )
        promote_resp = _promote(
            real_semantic_client,
            sub="wu5-alice-2",
            scope="memory:semantic:write",
            payload={"filename": "private-attachment.md", "target_layer": "semantic"},
        )
        assert promote_resp.status_code == 200, promote_resp.text

        bob = _user_for("wu5-bob-2", "memory:semantic:read")
        tool = get_tool_by_name("recall_semantic")
        assert tool is not None
        result, _ = asyncio.run(
            invoke_tool(
                bob,
                tool,
                {"query": "uniquely identifiable phrase xyzzy123"},
                session_id="wu5-sess-bob",
            )
        )
        assert not any("xyzzy123" in m["snippet"] for m in result["matches"]), (
            f"user B's recall_semantic surfaced user A's promoted doc: {result}"
        )

        # Sanity: the owner CAN find it (proves the empty result above is
        # isolation, not a broken promote/recall path).
        alice = _user_for("wu5-alice-2", "memory:semantic:read")
        alice_result, _ = asyncio.run(
            invoke_tool(
                alice,
                tool,
                {"query": "uniquely identifiable phrase xyzzy123"},
                session_id="wu5-sess-alice",
            )
        )
        assert any("xyzzy123" in m["snippet"] for m in alice_result["matches"])
