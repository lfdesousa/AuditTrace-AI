"""Tests for ``routes/memory_semantic_documents.py`` (WU-1, closes D15).

Pure-function tests for the document-level grouping ``list_semantic`` uses.
End-to-end proof through the REAL ``GET /memory/semantic`` route (upload ->
index -> list, honest pagination, whole-document read, the key-shape trap
fix) lives in ``tests/test_wu1_memory_retrieval_correctness.py`` — this
file locks in the grouping primitive itself, same split as
``test_pagination_service.py`` vs the route tests that exercise it
indirectly.
"""

from __future__ import annotations

from audittrace.routes.memory_semantic_documents import (
    dedupe_semantic_chunks,
    group_semantic_chunks_by_document,
)


def _chunk(
    key: str,
    title: str | None,
    *,
    created_at_ms: int | None = 100,
    modified_at_ms: int | None = 100,
    size_bytes: int | None = 10,
    discovered: bool = False,
) -> dict:
    return {
        "key": key,
        "title": title,
        "created_at_ms": created_at_ms,
        "modified_at_ms": modified_at_ms,
        "size_bytes": size_bytes,
        "discovered": discovered,
    }


class TestDedupeSemanticChunks:
    """The manifest-vs-discovered cross-path duplicate collapse."""

    def test_distinct_chunks_all_survive(self) -> None:
        items = [_chunk("decisions/aaa", "a.md"), _chunk("decisions/bbb", "b.md")]
        out = dedupe_semantic_chunks(items)
        assert len(out) == 2

    def test_same_hash_two_prefixes_collapses_to_one(self) -> None:
        """FALSIFIABLE: a no-op dedupe (``return items``) would keep both
        rows below (same trailing hash ``deadbeef``, different discovery
        prefixes) — this test goes RED without the collapse."""
        items = [
            _chunk("decisions/deadbeef", "same.md", discovered=True),
            _chunk("decisions_v2/deadbeef", "same.md", discovered=False),
        ]
        out = dedupe_semantic_chunks(items)
        assert len(out) == 1

    def test_manifest_tracked_wins_over_discovered_duplicate(self) -> None:
        """The SURVIVING row must be the manifest-tracked one (real
        authorship/timestamps), not whichever happened to sort first."""
        tracked = _chunk(
            "decisions/deadbeef", "same.md", discovered=False, created_at_ms=555
        )
        discovered = _chunk(
            "decisions_v2/deadbeef", "same.md", discovered=True, created_at_ms=999
        )
        out = dedupe_semantic_chunks([discovered, tracked])
        assert len(out) == 1
        assert out[0]["created_at_ms"] == 555

    def test_first_manifest_tracked_kept_when_duplicate_discovered_follows(
        self,
    ) -> None:
        """The OTHER branch of the manifest-wins rule: when the
        manifest-tracked row is seen FIRST, a later discovered duplicate
        must NOT overwrite it. FALSIFIABLE: an unconditional
        'last-one-wins' replace would flip ``created_at_ms`` to the
        discovered row's value here."""
        tracked = _chunk(
            "decisions/deadbeef", "same.md", discovered=False, created_at_ms=111
        )
        discovered = _chunk(
            "decisions_v2/deadbeef", "same.md", discovered=True, created_at_ms=999
        )
        out = dedupe_semantic_chunks([tracked, discovered])
        assert len(out) == 1
        assert out[0]["created_at_ms"] == 111

    def test_missing_key_never_collapses_distinct_rows(self) -> None:
        a = _chunk("", "a.md")
        b = _chunk("", "b.md")
        out = dedupe_semantic_chunks([a, b])
        assert len(out) == 2

    def test_empty_input(self) -> None:
        assert dedupe_semantic_chunks([]) == []


class TestGroupSemanticChunksByDocument:
    """Chunk -> document grouping, the core of the D15 fix."""

    def test_single_chunk_documents_pass_through_one_to_one(self) -> None:
        items = [_chunk("decisions/aaa", "a.md"), _chunk("decisions/bbb", "b.md")]
        docs = group_semantic_chunks_by_document(items)
        assert len(docs) == 2
        assert {d["chunk_count"] for d in docs} == {1}

    def test_multi_chunk_document_collapses_to_one_row(self) -> None:
        """FALSIFIABLE: an identity ('no grouping') implementation would
        return 3 rows here, not 1 — this is the exact D15 defect
        ('limit=25 -> 4 documents of ~600') reproduced at unit scale."""
        items = [
            _chunk("decisions/c0", "big-record.md"),
            _chunk("decisions/c1", "big-record.md"),
            _chunk("decisions/c2", "big-record.md"),
        ]
        docs = group_semantic_chunks_by_document(items)
        assert len(docs) == 1
        assert docs[0]["chunk_count"] == 3

    def test_untitled_rows_never_merge_with_each_other(self) -> None:
        """A caller-created row with no title is its OWN document — merging
        two unrelated untitled rows would UNDER-count (the wrong failure
        direction for a recall fix): a real distinct document could vanish
        behind another one's title-less sibling."""
        items = [
            _chunk("decisions/x1", None),
            _chunk("decisions/x2", None),
        ]
        docs = group_semantic_chunks_by_document(items)
        assert len(docs) == 2

    def test_size_bytes_summed_across_chunks(self) -> None:
        items = [
            _chunk("decisions/c0", "big.md", size_bytes=100),
            _chunk("decisions/c1", "big.md", size_bytes=250),
        ]
        docs = group_semantic_chunks_by_document(items)
        assert docs[0]["size_bytes"] == 350

    def test_created_at_is_the_earliest_chunk(self) -> None:
        items = [
            _chunk("decisions/c0", "big.md", created_at_ms=500),
            _chunk("decisions/c1", "big.md", created_at_ms=100),
            _chunk("decisions/c2", "big.md", created_at_ms=300),
        ]
        docs = group_semantic_chunks_by_document(items)
        assert docs[0]["created_at_ms"] == 100

    def test_modified_at_is_the_latest_chunk(self) -> None:
        items = [
            _chunk("decisions/c0", "big.md", modified_at_ms=500),
            _chunk("decisions/c1", "big.md", modified_at_ms=100),
            _chunk("decisions/c2", "big.md", modified_at_ms=300),
        ]
        docs = group_semantic_chunks_by_document(items)
        assert docs[0]["modified_at_ms"] == 500

    def test_representative_key_is_deterministic_earliest_created(self) -> None:
        """Tie-broken by key ascending when ``created_at_ms`` is equal —
        deterministic across repeated calls (no accidental dict-order
        dependence)."""
        items = [
            _chunk("decisions/zzz", "big.md", created_at_ms=100),
            _chunk("decisions/aaa", "big.md", created_at_ms=100),
        ]
        docs = group_semantic_chunks_by_document(items)
        assert docs[0]["key"] == "decisions/aaa"

    def test_null_timestamps_do_not_corrupt_real_ones(self) -> None:
        """A chunk with no timestamp (``None``) must not win the min/max
        aggregation over a chunk that DOES carry a real one."""
        items = [
            _chunk("decisions/c0", "big.md", created_at_ms=None, modified_at_ms=None),
            _chunk("decisions/c1", "big.md", created_at_ms=200, modified_at_ms=400),
        ]
        docs = group_semantic_chunks_by_document(items)
        assert docs[0]["created_at_ms"] == 200
        assert docs[0]["modified_at_ms"] == 400

    def test_all_null_timestamps_leave_representative_value_untouched(self) -> None:
        """When EVERY chunk lacks a timestamp, the aggregation must not
        fabricate one — the representative's own (``None``) value stands,
        exercising the 'no real values to aggregate' branch for both
        ``created_at_ms`` and ``modified_at_ms``."""
        items = [
            _chunk("decisions/c0", "big.md", created_at_ms=None, modified_at_ms=None),
            _chunk("decisions/c1", "big.md", created_at_ms=None, modified_at_ms=None),
        ]
        docs = group_semantic_chunks_by_document(items)
        assert docs[0]["created_at_ms"] is None
        assert docs[0]["modified_at_ms"] is None

    def test_empty_input(self) -> None:
        assert group_semantic_chunks_by_document([]) == []

    def test_document_order_preserves_first_seen_group(self) -> None:
        items = [
            _chunk("decisions/a0", "a.md"),
            _chunk("decisions/b0", "b.md"),
            _chunk("decisions/a1", "a.md"),
        ]
        docs = group_semantic_chunks_by_document(items)
        titles = [d["title"] for d in docs]
        assert titles == ["a.md", "b.md"]
