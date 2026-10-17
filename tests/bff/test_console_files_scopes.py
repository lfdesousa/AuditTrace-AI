"""Tests for bff/console_files_scopes.py — the narrow ingest scope
constant (M3 Sovereign-Attach WU-2).

The load-bearing test class is ``TestNeverDurableOrAdminScope`` — the
falsifiable proof of the spec's non-negotiable "the console-files
exchange must NEVER send a durable/corpus/admin scope, only
``memory:session:write``" invariant. Add any other scope to
``INGEST_SCOPES`` and these tests go RED.
"""

from __future__ import annotations

from bff.console_files_scopes import INGEST_SCOPE_STRING, INGEST_SCOPES


class TestIngestScopesContent:
    def test_exact_expected_scope_set(self) -> None:
        """The scope set is exactly one scope — ``memory:session:write``
        — never the memory-proxy path's broad set and never a scope this
        module doesn't need."""
        assert set(INGEST_SCOPES) == {"memory:session:write"}

    def test_no_duplicate_scopes(self) -> None:
        assert len(INGEST_SCOPES) == len(set(INGEST_SCOPES))

    def test_single_scope_tuple(self) -> None:
        assert len(INGEST_SCOPES) == 1


class TestNeverDurableOrAdminScope:
    """The falsifiable non-negotiable invariant: the console-files
    exchange must never be able to request a durable memory write, any
    corpus scope, or ``audittrace:admin``. Mirrors
    ``tests/bff/test_memory_scopes.py::TestNeverAdminScope``."""

    def test_admin_scope_absent(self) -> None:
        assert "audittrace:admin" not in INGEST_SCOPES

    def test_admin_scope_absent_from_scope_string(self) -> None:
        assert "audittrace:admin" not in INGEST_SCOPE_STRING.split(" ")

    def test_no_corpus_scope_present(self) -> None:
        assert not any(s.startswith("memory:corpus:") for s in INGEST_SCOPES)

    def test_no_durable_layer_write_scope_present(self) -> None:
        """The console-files route is ephemeral-only (WU-1's
        least-privilege wall) — none of the durable layers'
        ``:write`` scopes may appear here."""
        durable_write_scopes = {
            "memory:episodic:write",
            "memory:procedural:write",
            "memory:semantic:write",
        }
        assert durable_write_scopes.isdisjoint(INGEST_SCOPES)

    def test_no_read_scope_present(self) -> None:
        """Ingest is write-only from this seam — no read scope of any
        kind belongs in the narrow ingest set."""
        assert not any(
            s.endswith(":read") or s.endswith(":read-own") for s in INGEST_SCOPES
        )

    def test_scope_string_never_shares_memory_scope_string(self) -> None:
        """The narrow ingest scope string must be a genuinely distinct
        request from the memory-proxy path's broad one — proves the two
        modules stay independent even if someone later imports both into
        the same route by mistake."""
        from bff.memory_scopes import MEMORY_SCOPE_STRING

        assert INGEST_SCOPE_STRING != MEMORY_SCOPE_STRING
        assert set(INGEST_SCOPES).isdisjoint(set(MEMORY_SCOPE_STRING.split(" ")))


class TestIngestScopeString:
    def test_exact_value(self) -> None:
        assert INGEST_SCOPE_STRING == "memory:session:write"

    def test_space_separated_matches_rfc8693_scope_shape(self) -> None:
        assert INGEST_SCOPE_STRING == " ".join(INGEST_SCOPES)
        assert INGEST_SCOPE_STRING.split(" ") == list(INGEST_SCOPES)
