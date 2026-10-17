"""Tests for bff/console_conversations_scopes.py — the console-
conversations scope pair (WU-1, MongoDB-elimination EPIC).

Mirrors ``tests/bff/test_console_files_scopes.py``'s structure: the
load-bearing class is ``TestNeverForbiddenScope`` — the falsifiable proof
that this exchange can never widen beyond the two conversations scopes.
"""

from __future__ import annotations

from bff.console_conversations_scopes import (
    CONSOLE_CONVERSATIONS_SCOPE_STRING,
    CONSOLE_CONVERSATIONS_SCOPES,
)


class TestConsoleConversationsScopesContent:
    def test_exact_expected_scope_set(self) -> None:
        assert set(CONSOLE_CONVERSATIONS_SCOPES) == {
            "memory:conversations:read-own",
            "memory:conversations:write",
        }

    def test_no_duplicate_scopes(self) -> None:
        assert len(CONSOLE_CONVERSATIONS_SCOPES) == len(
            set(CONSOLE_CONVERSATIONS_SCOPES)
        )

    def test_exactly_two_scopes(self) -> None:
        assert len(CONSOLE_CONVERSATIONS_SCOPES) == 2


class TestNeverForbiddenScope:
    """Falsifiable: add ``audittrace:admin``, any ``memory:corpus:*``, or
    any other layer's read/write scope to
    :data:`CONSOLE_CONVERSATIONS_SCOPES` and these tests go RED."""

    def test_admin_scope_absent(self) -> None:
        assert "audittrace:admin" not in CONSOLE_CONVERSATIONS_SCOPES

    def test_admin_scope_absent_from_scope_string(self) -> None:
        assert "audittrace:admin" not in CONSOLE_CONVERSATIONS_SCOPE_STRING.split(" ")

    def test_no_corpus_scope_present(self) -> None:
        assert not any(
            s.startswith("memory:corpus:") for s in CONSOLE_CONVERSATIONS_SCOPES
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
            "memory:upload:write",
        }
        assert other_layer_scopes.isdisjoint(CONSOLE_CONVERSATIONS_SCOPES)

    def test_scope_string_never_shares_other_proxy_scope_strings(self) -> None:
        """Distinct exchange request from every other BFF proxy path —
        proves independence even if a future route imports two scope
        modules by mistake."""
        from bff.console_files_scopes import INGEST_SCOPE_STRING
        from bff.memory_scopes import MEMORY_SCOPE_STRING

        assert CONSOLE_CONVERSATIONS_SCOPE_STRING != MEMORY_SCOPE_STRING
        assert CONSOLE_CONVERSATIONS_SCOPE_STRING != INGEST_SCOPE_STRING
        assert set(CONSOLE_CONVERSATIONS_SCOPES).isdisjoint(
            set(MEMORY_SCOPE_STRING.split(" "))
        )
        assert set(CONSOLE_CONVERSATIONS_SCOPES).isdisjoint(
            set(INGEST_SCOPE_STRING.split(" "))
        )


class TestConsoleConversationsScopeString:
    def test_exact_value(self) -> None:
        assert (
            CONSOLE_CONVERSATIONS_SCOPE_STRING
            == "memory:conversations:read-own memory:conversations:write"
        )

    def test_space_separated_matches_rfc8693_scope_shape(self) -> None:
        assert CONSOLE_CONVERSATIONS_SCOPE_STRING == " ".join(
            CONSOLE_CONVERSATIONS_SCOPES
        )
        assert CONSOLE_CONVERSATIONS_SCOPE_STRING.split(" ") == list(
            CONSOLE_CONVERSATIONS_SCOPES
        )
