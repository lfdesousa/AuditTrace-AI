"""Tests for services/index_routing.py — SPEC #387 Phase 1 (WU-1/WU-4)
deterministic collection routing for the auto-index pipeline."""

from __future__ import annotations

from audittrace.services.index_routing import (
    AI_RESEARCH_PAPERS_COLLECTION,
    DEFAULT_MD_COLLECTION,
    bare_key_from_uri,
    collection_for_key,
)


class TestCollectionForKey:
    def test_pdf_key_routes_to_ai_research_papers(self) -> None:
        assert (
            collection_for_key("episodic/papers/scan-1/paper.pdf")
            == AI_RESEARCH_PAPERS_COLLECTION
        )

    def test_pdf_suffix_is_case_insensitive(self) -> None:
        assert (
            collection_for_key("episodic/papers/scan-1/PAPER.PDF")
            == AI_RESEARCH_PAPERS_COLLECTION
        )

    def test_md_key_routes_to_default_collection(self) -> None:
        assert collection_for_key("episodic/ADR-007.md") == DEFAULT_MD_COLLECTION

    def test_unknown_extension_routes_to_default_collection(self) -> None:
        # Falsifiability: only .pdf is special-cased — everything else,
        # including no extension at all, falls to the .md-fed default.
        assert collection_for_key("episodic/mystery-file") == DEFAULT_MD_COLLECTION

    def test_works_on_full_s3_uri_not_just_bare_key(self) -> None:
        # The routing decision only looks at the suffix, so it must give
        # the same answer whether called with a bare key or the
        # manifest's full s3:// URI form.
        assert (
            collection_for_key("s3://memory-shared/episodic/papers/s/f.pdf")
            == AI_RESEARCH_PAPERS_COLLECTION
        )


class TestBareKeyFromUri:
    def test_strips_scheme_and_bucket(self) -> None:
        assert (
            bare_key_from_uri("s3://memory-shared/episodic/papers/scan-1/x.pdf")
            == "episodic/papers/scan-1/x.pdf"
        )

    def test_non_s3_uri_returned_unchanged(self) -> None:
        # Defensive fallback — every manifest row this pipeline promotes
        # carries the s3:// scheme, but a caller passing a bare key
        # already must not be corrupted by a naive strip.
        assert bare_key_from_uri("episodic/papers/scan-1/x.pdf") == (
            "episodic/papers/scan-1/x.pdf"
        )

    def test_round_trips_with_collection_for_key(self) -> None:
        uri = "s3://memory-shared/episodic/papers/scan-9/report.pdf"
        bare = bare_key_from_uri(uri)
        assert collection_for_key(bare) == AI_RESEARCH_PAPERS_COLLECTION
