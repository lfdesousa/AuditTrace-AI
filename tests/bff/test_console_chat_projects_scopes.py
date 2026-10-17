"""Tests for bff/console_chat_projects_scopes.py — the
console-chat-projects scope pair (Chat-Projects domain,
MongoDB-elimination EPIC).

Mirrors ``tests/bff/test_console_presets_scopes.py``'s structure: the
load-bearing class is ``TestNeverForbiddenScope`` — the falsifiable
proof that this exchange can never widen beyond the two chat-projects
scopes.
"""

from __future__ import annotations

from bff.console_chat_projects_scopes import (
    CONSOLE_CHAT_PROJECTS_SCOPE_STRING,
    CONSOLE_CHAT_PROJECTS_SCOPES,
)


class TestConsoleChatProjectsScopesContent:
    def test_exact_expected_scope_set(self) -> None:
        assert set(CONSOLE_CHAT_PROJECTS_SCOPES) == {
            "memory:chat_projects:read-own",
            "memory:chat_projects:write",
        }

    def test_no_duplicate_scopes(self) -> None:
        assert len(CONSOLE_CHAT_PROJECTS_SCOPES) == len(
            set(CONSOLE_CHAT_PROJECTS_SCOPES)
        )

    def test_exactly_two_scopes(self) -> None:
        assert len(CONSOLE_CHAT_PROJECTS_SCOPES) == 2


class TestNeverForbiddenScope:
    """Falsifiable: add ``audittrace:admin``, any ``memory:corpus:*``, or
    any other layer's read/write scope to
    :data:`CONSOLE_CHAT_PROJECTS_SCOPES` and these tests go RED."""

    def test_admin_scope_absent(self) -> None:
        assert "audittrace:admin" not in CONSOLE_CHAT_PROJECTS_SCOPES

    def test_admin_scope_absent_from_scope_string(self) -> None:
        assert "audittrace:admin" not in CONSOLE_CHAT_PROJECTS_SCOPE_STRING.split(" ")

    def test_no_corpus_scope_present(self) -> None:
        assert not any(
            s.startswith("memory:corpus:") for s in CONSOLE_CHAT_PROJECTS_SCOPES
        )

    def test_no_other_layer_scope_present(self) -> None:
        other_layer_scopes = {
            "memory:episodic:read",
            "memory:episodic:write",
            "memory:procedural:read",
            "memory:procedural:write",
            "memory:semantic:read",
            "memory:semantic:write",
            "memory:session:read-own",
            "memory:session:write",
            "memory:conversational:read-own",
            "memory:conversations:read-own",
            "memory:conversations:write",
            "memory:presets:read-own",
            "memory:presets:write",
            "memory:prompts:read-own",
            "memory:prompts:write",
            "memory:upload:write",
        }
        assert other_layer_scopes.isdisjoint(CONSOLE_CHAT_PROJECTS_SCOPES)

    def test_scope_string_never_shares_other_proxy_scope_strings(self) -> None:
        """Distinct exchange request from every other BFF proxy path —
        proves independence even if a future route imports two scope
        modules by mistake."""
        from bff.console_conversations_scopes import CONSOLE_CONVERSATIONS_SCOPE_STRING
        from bff.console_files_scopes import INGEST_SCOPE_STRING
        from bff.console_presets_scopes import CONSOLE_PRESETS_SCOPE_STRING
        from bff.console_prompts_scopes import CONSOLE_PROMPTS_SCOPE_STRING
        from bff.memory_scopes import MEMORY_SCOPE_STRING

        assert CONSOLE_CHAT_PROJECTS_SCOPE_STRING != MEMORY_SCOPE_STRING
        assert CONSOLE_CHAT_PROJECTS_SCOPE_STRING != INGEST_SCOPE_STRING
        assert CONSOLE_CHAT_PROJECTS_SCOPE_STRING != CONSOLE_CONVERSATIONS_SCOPE_STRING
        assert CONSOLE_CHAT_PROJECTS_SCOPE_STRING != CONSOLE_PRESETS_SCOPE_STRING
        assert CONSOLE_CHAT_PROJECTS_SCOPE_STRING != CONSOLE_PROMPTS_SCOPE_STRING
        assert set(CONSOLE_CHAT_PROJECTS_SCOPES).isdisjoint(
            set(MEMORY_SCOPE_STRING.split(" "))
        )
        assert set(CONSOLE_CHAT_PROJECTS_SCOPES).isdisjoint(
            set(INGEST_SCOPE_STRING.split(" "))
        )
        assert set(CONSOLE_CHAT_PROJECTS_SCOPES).isdisjoint(
            set(CONSOLE_CONVERSATIONS_SCOPE_STRING.split(" "))
        )
        assert set(CONSOLE_CHAT_PROJECTS_SCOPES).isdisjoint(
            set(CONSOLE_PRESETS_SCOPE_STRING.split(" "))
        )
        assert set(CONSOLE_CHAT_PROJECTS_SCOPES).isdisjoint(
            set(CONSOLE_PROMPTS_SCOPE_STRING.split(" "))
        )


class TestConsoleChatProjectsScopeString:
    def test_exact_value(self) -> None:
        assert (
            CONSOLE_CHAT_PROJECTS_SCOPE_STRING
            == "memory:chat_projects:read-own memory:chat_projects:write"
        )

    def test_space_separated_matches_rfc8693_scope_shape(self) -> None:
        assert CONSOLE_CHAT_PROJECTS_SCOPE_STRING == " ".join(
            CONSOLE_CHAT_PROJECTS_SCOPES
        )
        assert CONSOLE_CHAT_PROJECTS_SCOPE_STRING.split(" ") == list(
            CONSOLE_CHAT_PROJECTS_SCOPES
        )
