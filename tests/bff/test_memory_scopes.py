"""Tests for bff/memory_scopes.py — the memory-proxy scope constant.

The load-bearing test is ``TestNeverAdminScope`` — the falsifiable proof
of the spec's non-negotiable "the console must never obtain
``audittrace:admin``" invariant. Add ``"audittrace:admin"`` to
``MEMORY_SCOPES`` and this test goes RED.
"""

from __future__ import annotations

from bff.memory_scopes import MEMORY_SCOPE_STRING, MEMORY_SCOPES


class TestMemoryScopesContent:
    def test_exact_expected_scope_set(self) -> None:
        """One assertion per drift class: adding, removing, or renaming
        any scope in this tuple fails here — the exact set the spec
        (WU-D2-1) names, using the REAL ``memory:conversational:read-own``
        scope name (not the spec text's informal
        ``memory:conversational:read``)."""
        assert set(MEMORY_SCOPES) == {
            "memory:episodic:read",
            "memory:procedural:read",
            "memory:semantic:read",
            "memory:conversational:read-own",
            "memory:episodic:write",
            "memory:procedural:write",
            "memory:semantic:write",
        }

    def test_no_duplicate_scopes(self) -> None:
        assert len(MEMORY_SCOPES) == len(set(MEMORY_SCOPES))


class TestNeverAdminScope:
    """The falsifiable non-negotiable invariant: the memory-proxy exchange
    must never be able to request ``audittrace:admin`` (no hard-delete of
    the audit trail)."""

    def test_admin_scope_absent(self) -> None:
        assert "audittrace:admin" not in MEMORY_SCOPES

    def test_admin_scope_absent_from_scope_string(self) -> None:
        assert "audittrace:admin" not in MEMORY_SCOPE_STRING.split(" ")

    def test_no_corpus_scope_present(self) -> None:
        """The console's memory proxy is private-tier only (ADR-062);
        corpus scopes are operator/curator-tier and must never appear
        here either."""
        assert not any(s.startswith("memory:corpus:") for s in MEMORY_SCOPES)


class TestMemoryScopeString:
    def test_space_separated_matches_rfc8693_scope_shape(self) -> None:
        assert MEMORY_SCOPE_STRING == " ".join(MEMORY_SCOPES)
        assert MEMORY_SCOPE_STRING.split(" ") == list(MEMORY_SCOPES)

    def test_every_scope_present_exactly_once_in_string(self) -> None:
        parts = MEMORY_SCOPE_STRING.split(" ")
        for scope in MEMORY_SCOPES:
            assert parts.count(scope) == 1
