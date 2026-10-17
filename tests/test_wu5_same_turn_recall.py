"""D5 — same-turn E2E test through the real surfaces (WU-5, Sovereign-
Attach EPIC, 2026-09-06-SPEC-wu5-same-turn-session-recall.md).

Write to the session layer via the REAL ``POST /memory/upload?layer=
session`` route (WU-1), then in the same flow:

  (a) ``tools_visible_to`` includes ``recall_attachments`` for a
      ``memory:session:read-own`` holder and excludes it otherwise;
  (b) ``recall_attachments`` (via ``invoke_tool`` — the exact dispatch
      the ``/v1`` tool loop uses) returns the just-written doc with
      content; cross-user returns empty;
  (c) a ``ToolCall`` audit row carries token-derived ``user_id`` and,
      via its ``interaction_id`` FK, links to an ``InteractionRecord``
      carrying ``session_id``/``trace_id`` (feedback_traceability_requirement)
      — proven through the REAL ``/v1/chat/completions`` tool-call loop,
      mirroring ``tests/test_chat_proxy.py::TestToolsModeIntegration::
      test_tools_mode_memory_prompt_fires_loop_and_audits`` exactly, for
      ``recall_attachments`` instead of ``recall_decisions``.
"""

from __future__ import annotations

import importlib
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

# Side-effect import — running the module is what runs the
# @register_memory_tool decorators, including recall_attachments.
import audittrace.tools.memory_handlers as handlers_mod
from audittrace.db.models import InteractionRecord, ToolCall
from audittrace.dependencies import get_postgres_factory
from audittrace.identity import SENTINEL_SUBJECT, sentinel_user_context
from audittrace.tools import (
    get_tool_by_name,
    invoke_tool,
    reset_registry_for_tests,
    tools_visible_to,
)
from tests.test_chat_proxy import (
    _patch_async_client,
    _patch_tool_loop_client,
    _SequencedClient,
    _tools_mode_response_text,
    _tools_mode_tool_call_response,
)


@pytest.fixture(autouse=True)
def _fresh_registry_with_handlers():
    """Reset the registry and re-run the decorator pass for each test
    (mirrors tests/test_memory_tool_handlers.py's identical fixture) —
    the MEMORY_TOOL_REGISTRY is a process-global mutable singleton other
    test files reset in their OWN teardown, so a test file that only
    relies on the module-level side-effect import at collection time can
    see an EMPTY registry depending on run order (``create_app()``'s own
    ``import audittrace.tools.memory_handlers`` is a no-op after the
    first process-wide import — it does not re-run the decorators).
    Self-sufficient here regardless of file run order."""
    reset_registry_for_tests()
    importlib.reload(handlers_mod)
    yield
    reset_registry_for_tests()


class _Auth:
    """Mirrors ``tests/test_memory_promote_route.py::_Auth`` — patches the
    JWT-decode chain so the real HTTP upload route sees a token for *sub*
    with *scope*, without needing a live Keycloak."""

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
    """Write via the REAL (WU-1) ``POST /memory/upload?layer=session``
    route — the actual same-turn chat-attachment write path, not a
    direct service call (feedback_test_through_real_http_route)."""
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


def _user_for(sub: str, scope: str) -> Any:
    """Build the ``UserContext`` ``invoke_tool``/``tools_visible_to`` need
    — same shape ``require_user`` would build from a real JWT carrying
    this ``sub``/``scope``."""
    return replace(
        sentinel_user_context(),
        user_id=sub,
        username=sub,
        is_admin=False,
        scopes=(scope,),
    )


# ─────────────────── (a) tool visibility, scope-gated ───────────────────────


class TestSameTurnToolVisibility:
    def test_scope_holder_sees_recall_attachments(self) -> None:
        holder = _user_for("wu5-e2e-visibility", "memory:session:read-own")
        names = {t["function"]["name"] for t in tools_visible_to(holder)}
        assert "recall_attachments" in names

    def test_non_holder_does_not_see_recall_attachments(self) -> None:
        non_holder = _user_for("wu5-e2e-visibility-2", "memory:conversational:read-own")
        names = {t["function"]["name"] for t in tools_visible_to(non_holder)}
        assert "recall_attachments" not in names


# ──────────── (b) write via the real route, recall via invoke_tool ──────────


class TestSameTurnWriteThenRecall:
    """Write via the REAL upload route; recall via ``invoke_tool`` — the
    exact dispatch mechanism the ``/v1`` tool loop uses
    (``_memory_tool_loop.py::_execute_memory_tool``)."""

    def test_owner_recalls_just_written_attachment_same_turn(self, client) -> None:
        _upload_session_doc(
            client,
            sub="wu5-e2e-alice",
            filename="just-attached.txt",
            content=b"the thing I just attached to this chat turn",
        )

        alice = _user_for("wu5-e2e-alice", "memory:session:read-own")
        tool = get_tool_by_name("recall_attachments")
        assert tool is not None
        import asyncio

        result, _ = asyncio.run(invoke_tool(alice, tool, {}, session_id="wu5-e2e-sess"))

        assert result["total"] == 1
        assert result["matches"][0]["title"] == "just-attached.txt"
        assert "just attached to this chat turn" in result["matches"][0]["snippet"]

    def test_cross_user_recall_attachments_returns_empty(self, client) -> None:
        _upload_session_doc(
            client,
            sub="wu5-e2e-alice-2",
            filename="alice-only.txt",
            content=b"alice's private turn attachment",
        )

        bob = _user_for("wu5-e2e-bob-2", "memory:session:read-own")
        tool = get_tool_by_name("recall_attachments")
        assert tool is not None
        import asyncio

        result, _ = asyncio.run(
            invoke_tool(bob, tool, {}, session_id="wu5-e2e-sess-bob")
        )

        assert result["total"] == 0
        assert result["matches"] == [], (
            "cross-user recall_attachments leaked another user's same-turn attachment"
        )


# ───────────── (c) ToolCall audit row + traceability, real loop ─────────────


class TestSameTurnAuditRowTraceability:
    """Mirrors ``TestToolsModeIntegration::
    test_tools_mode_memory_prompt_fires_loop_and_audits`` exactly, for
    ``recall_attachments`` — through the REAL ``/v1/chat/completions``
    tool-call loop (sentinel/admin caller, which bypasses the scope gate
    per ``tools_visible_to``'s documented admin-bypass semantics — the
    scope gate itself is proven separately by
    ``TestSameTurnToolVisibility`` above and
    ``tests/test_recall_attachments_tool.py::TestRecallAttachmentsScopeGate``)."""

    @pytest.fixture
    def _tools_mode(self, monkeypatch):
        from audittrace import config as config_mod

        config_mod.get_settings.cache_clear()
        monkeypatch.setenv("AUDITTRACE_MEMORY_MODE", "tools")
        yield
        config_mod.get_settings.cache_clear()

    async def test_tool_call_produces_audited_and_traceable_row(
        self, client, _tools_mode, monkeypatch
    ) -> None:
        # Seed a session doc for the sentinel/admin caller so the tool
        # call has real content to surface (not required for the audit
        # proof itself, but keeps the scenario realistic).
        from audittrace.dependencies import get_session_memory_service

        session_memory = get_session_memory_service()
        await session_memory.write(
            sentinel_user_context(), "sentinel-attachment.txt", "sentinel's upload"
        )

        # The TestClient's request doesn't reliably carry an ambient OTel
        # span the way a real deployed request does — pin the auto-capture
        # to a known value (same technique as
        # tests/test_chat_failure_audit.py::TestPersistTraceIdBinding) so
        # this test proves the interaction row CAPTURES trace_id, without
        # depending on TestClient tracing plumbing that is orthogonal to
        # this WU.
        from audittrace.routes import chat as chat_mod

        pinned_trace_id = "d" * 32
        monkeypatch.setattr(chat_mod, "_current_trace_id_hex", lambda: pinned_trace_id)

        fake = _SequencedClient(
            [
                _tools_mode_tool_call_response("recall_attachments", "{}"),
                _tools_mode_response_text("Here is what you attached."),
            ]
        )
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [
                        {"role": "user", "content": "summarise what I attached"}
                    ],
                    "project": "AuditTrace",
                },
            )

        assert response.status_code == 200
        assert len(fake.post_calls) == 2
        body = response.json()
        assert "attached" in body["choices"][0]["message"]["content"]

        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            interactions = (await db.execute(select(InteractionRecord))).scalars().all()
            tool_calls_rows = (await db.execute(select(ToolCall))).scalars().all()

        assert len(interactions) >= 1
        latest_interaction = interactions[-1]
        assert latest_interaction.user_id == SENTINEL_SUBJECT
        assert latest_interaction.session_id, (
            "interaction row missing session_id — traceability invariant broken"
        )
        assert latest_interaction.trace_id == pinned_trace_id, (
            "interaction row missing/wrong trace_id — traceability invariant "
            "broken (EU AI Act Art 12 reconstruction anchor)"
        )

        assert len(tool_calls_rows) == 1
        tc_row = tool_calls_rows[0]
        assert tc_row.tool_name == "recall_attachments"
        # Token-derived user_id — never a caller-supplied field.
        assert tc_row.user_id == SENTINEL_SUBJECT
        assert tc_row.granted_scope == "memory:session:read-own"
        assert tc_row.interaction_id == latest_interaction.id, (
            "ToolCall row not linked to the interaction that carries "
            "session_id/trace_id — the audit chain is broken"
        )
