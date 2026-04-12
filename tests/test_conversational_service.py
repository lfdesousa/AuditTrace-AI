"""Tests for MockConversationalService — Layer 3 of the 4-layer memory architecture.

ADR-020: SQLite tests removed. PostgreSQL tests are in test_postgres_conversational.py.
This file retains mock service tests and ABC interface verification.

Phase 2 (DESIGN §15): every service method takes ``user_context`` as the
first positional argument. ``load_sessions`` applies a per-user filter
unconditionally — conversations are inherently personal, there is no
admin bypass at this layer.
"""

from dataclasses import replace

from sovereign_memory.services.conversational import (
    ConversationalService,
    MockConversationalService,
)

# ── MockConversationalService tests ──────────────────────────────────────────


class TestMockConversationalService:
    def test_mock_starts_empty(self, user_context):
        service = MockConversationalService()
        assert service.load_sessions(user_context, "any") == []

    def test_mock_save_and_load(self, user_context):
        service = MockConversationalService()
        service.save_session(user_context, "AuditTrace", "Test summary", ["point1"])
        sessions = service.load_sessions(user_context, "AuditTrace")
        assert len(sessions) == 1
        assert sessions[0]["summary"] == "Test summary"

    def test_mock_filters_by_project(self, user_context):
        service = MockConversationalService()
        service.save_session(user_context, "ProjectA", "Summary A", [])
        service.save_session(user_context, "ProjectB", "Summary B", [])
        assert len(service.load_sessions(user_context, "ProjectA")) == 1
        assert len(service.load_sessions(user_context, "ProjectB")) == 1

    def test_mock_isolates_by_user(self, user_context):
        """Phase 2 contract: user B cannot see user A's sessions, even for
        the same project. No admin bypass — conversations are per-user."""
        service = MockConversationalService()
        alice = replace(user_context, user_id="user-alice", is_admin=False)
        bob = replace(user_context, user_id="user-bob", is_admin=False)
        service.save_session(alice, "SharedProject", "Alice summary", [])
        service.save_session(bob, "SharedProject", "Bob summary", [])
        alice_sessions = service.load_sessions(alice, "SharedProject")
        bob_sessions = service.load_sessions(bob, "SharedProject")
        assert len(alice_sessions) == 1
        assert alice_sessions[0]["summary"] == "Alice summary"
        assert len(bob_sessions) == 1
        assert bob_sessions[0]["summary"] == "Bob summary"

    def test_mock_reset(self, user_context):
        service = MockConversationalService()
        service.save_session(user_context, "P", "S", [])
        service.reset()
        assert service.load_sessions(user_context, "P") == []

    def test_abstract_interface(self):
        assert isinstance(MockConversationalService(), ConversationalService)

    def test_mock_as_context_passes_through(self, user_context):
        service = MockConversationalService()
        service.save_session(user_context, "P", "A summary", ["k1"])
        ctx = service.as_context(user_context, "P")
        assert "Recent Sessions" in ctx
        assert "A summary" in ctx
