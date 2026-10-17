"""Tests for bff/console_tool_favorites_scopes.py — the
console-tool-favorites scope pair (Tool-Favorites domain, MongoDB-
elimination EPIC).

Mirrors ``tests/bff/test_console_conversation_tags_scopes.py``'s
structure: the load-bearing class is ``TestNeverForbiddenScope`` — the
falsifiable proof that this exchange can never widen beyond the two
tool-favorites scopes.
"""

from __future__ import annotations

from bff.console_tool_favorites_scopes import (
    CONSOLE_TOOL_FAVORITES_SCOPE_STRING,
    CONSOLE_TOOL_FAVORITES_SCOPES,
)


class TestConsoleToolFavoritesScopesContent:
    def test_exact_expected_scope_set(self) -> None:
        assert set(CONSOLE_TOOL_FAVORITES_SCOPES) == {
            "memory:tool_favorites:read-own",
            "memory:tool_favorites:write",
        }

    def test_no_duplicate_scopes(self) -> None:
        assert len(CONSOLE_TOOL_FAVORITES_SCOPES) == len(
            set(CONSOLE_TOOL_FAVORITES_SCOPES)
        )

    def test_exactly_two_scopes(self) -> None:
        assert len(CONSOLE_TOOL_FAVORITES_SCOPES) == 2


class TestNeverForbiddenScope:
    """Falsifiable: add ``audittrace:admin``, any ``memory:corpus:*``, or
    any other layer's read/write scope to
    :data:`CONSOLE_TOOL_FAVORITES_SCOPES` and these tests go RED."""

    def test_admin_scope_absent(self) -> None:
        assert "audittrace:admin" not in CONSOLE_TOOL_FAVORITES_SCOPES

    def test_admin_scope_absent_from_scope_string(self) -> None:
        assert "audittrace:admin" not in CONSOLE_TOOL_FAVORITES_SCOPE_STRING.split(" ")

    def test_no_corpus_scope_present(self) -> None:
        assert not any(
            s.startswith("memory:corpus:") for s in CONSOLE_TOOL_FAVORITES_SCOPES
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
            "memory:chat_projects:read-own",
            "memory:chat_projects:write",
            "memory:files:read-own",
            "memory:files:write",
            "memory:agents:read-own",
            "memory:agents:write",
            "memory:conversation_tags:read-own",
            "memory:conversation_tags:write",
            "memory:upload:write",
        }
        assert other_layer_scopes.isdisjoint(CONSOLE_TOOL_FAVORITES_SCOPES)

    def test_scope_string_never_shares_other_proxy_scope_strings(self) -> None:
        """Distinct exchange request from every other BFF proxy path —
        proves independence even if a future route imports two scope
        modules by mistake."""
        from bff.console_agents_scopes import CONSOLE_AGENTS_SCOPE_STRING
        from bff.console_chat_projects_scopes import (
            CONSOLE_CHAT_PROJECTS_SCOPE_STRING,
        )
        from bff.console_conversation_tags_scopes import (
            CONSOLE_CONVERSATION_TAGS_SCOPE_STRING,
        )
        from bff.console_conversations_scopes import CONSOLE_CONVERSATIONS_SCOPE_STRING
        from bff.console_file_records_scopes import CONSOLE_FILE_RECORDS_SCOPE_STRING
        from bff.console_files_scopes import INGEST_SCOPE_STRING
        from bff.console_presets_scopes import CONSOLE_PRESETS_SCOPE_STRING
        from bff.console_prompts_scopes import CONSOLE_PROMPTS_SCOPE_STRING
        from bff.memory_scopes import MEMORY_SCOPE_STRING

        others = {
            "memory": MEMORY_SCOPE_STRING,
            "ingest": INGEST_SCOPE_STRING,
            "conversations": CONSOLE_CONVERSATIONS_SCOPE_STRING,
            "presets": CONSOLE_PRESETS_SCOPE_STRING,
            "prompts": CONSOLE_PROMPTS_SCOPE_STRING,
            "chat_projects": CONSOLE_CHAT_PROJECTS_SCOPE_STRING,
            "file_records": CONSOLE_FILE_RECORDS_SCOPE_STRING,
            "agents": CONSOLE_AGENTS_SCOPE_STRING,
            "conversation_tags": CONSOLE_CONVERSATION_TAGS_SCOPE_STRING,
        }
        for label, other_scope_string in others.items():
            assert CONSOLE_TOOL_FAVORITES_SCOPE_STRING != other_scope_string, (
                f"tool-favorites scope string collides with {label}'s"
            )
            assert set(CONSOLE_TOOL_FAVORITES_SCOPES).isdisjoint(
                set(other_scope_string.split(" "))
            ), f"tool-favorites scopes overlap with {label}'s scopes"


class TestConsoleToolFavoritesScopeString:
    def test_exact_value(self) -> None:
        assert (
            CONSOLE_TOOL_FAVORITES_SCOPE_STRING
            == "memory:tool_favorites:read-own memory:tool_favorites:write"
        )

    def test_space_separated_matches_rfc8693_scope_shape(self) -> None:
        assert CONSOLE_TOOL_FAVORITES_SCOPE_STRING == " ".join(
            CONSOLE_TOOL_FAVORITES_SCOPES
        )
        assert CONSOLE_TOOL_FAVORITES_SCOPE_STRING.split(" ") == list(
            CONSOLE_TOOL_FAVORITES_SCOPES
        )
