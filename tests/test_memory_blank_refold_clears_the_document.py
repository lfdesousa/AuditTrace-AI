"""MEMORY-SERVER-SILENT-TRUNCATION gate r3 S14: a re-fold of a blank document
removes every chunk of the file.

A fold that yields zero chunks used to ``continue`` before it looked for a stale
tail, so the previous fold stayed readable as ``verified: true`` while the
stored object was blank. The fold now removes every chunk of the file
(``find_stale_tail`` with ``kept=0``), authorized first and audited once."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from audittrace.routes import memory as m
from tests.memory_truncation_support import (
    USER,
    corpus_fold,
    distinct_body,
    fold,
    memory_client,
    read_whole,
    upload,
)
from tests.memory_truncation_support_a1 import (
    STEP,
    chroma_collection,
    manifest,
    memory_audit_details,
)

OLD = distinct_body(5 * STEP)
BLANKS = {"spaces-and-newline": "   \n", "empty": "", "long-blank": "\n" * 5000}


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


def _old_fold(client: TestClient) -> int:
    assert upload(client, "episodic", "doc.md", OLD).status_code == 200
    index = fold(client, "episodic", "doc.md", "decisions")
    assert index.status_code == 200, index.text
    count: int = index.json()["collections"]["decisions"]
    assert count > 1
    return count


def _stored_chunk_ids(count: int) -> list[str]:
    """The ChromaDB ids still present among the old fold's chunk rows."""
    from audittrace.services.memory_keys import private_physical_id
    from tests.memory_truncation_support_a1 import caller

    owned = [
        private_physical_id(caller(), m._doc_id("decisions", "doc.md", i))
        for i in range(count)
    ]
    present = asyncio.run(chroma_collection("decisions").get(ids=owned, include=[]))
    return sorted(present["ids"])


class TestBlankRefoldClearsTheDocument:
    @pytest.mark.parametrize("case", sorted(BLANKS))
    def test_the_old_text_is_not_readable_after_a_blank_refold(
        self, client: TestClient, case: str
    ) -> None:
        count = _old_fold(client)
        assert upload(client, "episodic", "doc.md", BLANKS[case]).status_code == 200

        index = fold(client, "episodic", "doc.md", "decisions")

        assert index.status_code == 200, index.text
        assert index.json()["collections"]["decisions"] == 0
        assert _stored_chunk_ids(count) == []
        back = read_whole(client, "decisions", "doc.md")
        assert back.status_code == 404
        assert OLD[:40] not in back.text

    def test_every_chunk_manifest_row_is_tombstoned(self, client: TestClient) -> None:
        from audittrace.services.memory_keys import physical_key

        count = _old_fold(client)
        keys = [
            m._semantic_key("decisions", m._doc_id("decisions", "doc.md", i))
            for i in range(count)
        ]
        rows = manifest()._rows
        assert all(
            rows[("semantic", physical_key(USER, k))]["deleted_at_ms"] is None
            for k in keys
        )
        assert upload(client, "episodic", "doc.md", "   \n").status_code == 200

        assert fold(client, "episodic", "doc.md", "decisions").status_code == 200

        assert all(
            rows[("semantic", physical_key(USER, k))]["deleted_at_ms"] is not None
            for k in keys
        )

    def test_the_blank_refold_is_audited_once_with_the_removed_count(
        self, client: TestClient
    ) -> None:
        count = _old_fold(client)
        assert upload(client, "episodic", "doc.md", "   \n").status_code == 200

        assert fold(client, "episodic", "doc.md", "decisions").status_code == 200

        removals = [
            d for d in memory_audit_details() if d.get("reason") == "refold_stale_tail"
        ]
        assert len(removals) == 1
        assert removals[0]["op"] == "delete"
        assert removals[0]["key"] == "doc.md"
        assert removals[0]["removed"] == count
        assert removals[0]["tier"] == "private"

    def test_a_corpus_blank_refold_is_audited_with_the_corpus_tier(
        self, client: TestClient
    ) -> None:
        assert corpus_fold(client, OLD).status_code == 200

        assert corpus_fold(client, "   \n").status_code == 200

        removals = [
            d for d in memory_audit_details() if d.get("reason") == "refold_stale_tail"
        ]
        assert [d["tier"] for d in removals] == ["corpus"]

    def test_a_blank_document_that_was_never_folded_removes_and_audits_nothing(
        self, client: TestClient
    ) -> None:
        assert upload(client, "episodic", "void.md", "\n" * 5000).status_code == 200

        index = fold(client, "episodic", "void.md", "decisions")

        assert index.status_code == 200, index.text
        assert index.json()["collections"]["decisions"] == 0
        assert [
            d for d in memory_audit_details() if d.get("reason") == "refold_stale_tail"
        ] == []

    def test_a_blank_refold_whose_tail_the_caller_may_not_remove_is_refused(
        self, client: TestClient
    ) -> None:
        """One chunk's manifest row now belongs to the shared corpus (somebody
        else's): the blank re-fold would remove it, so it is refused with 403
        BEFORE anything is removed, and the document still reads as the first
        fold. The foreign row is the one the removal reaches LAST, so a removal
        that authorizes chunk by chunk, as it goes, would already have
        tombstoned every other row."""
        from audittrace.services.memory_keys import physical_key

        count = _old_fold(client)
        keys = sorted(
            m._semantic_key("decisions", m._doc_id("decisions", "doc.md", i))
            for i in range(count)
        )
        foreign = keys[-1]
        rows = manifest()._rows
        del rows[("semantic", physical_key(USER, foreign))]
        asyncio.run(
            manifest().record_create(
                layer="semantic",
                key=foreign,
                title="doc.md",
                size_bytes=1,
                user_id="curator",
                tier="corpus",
                caller_can_write_shared=True,
            )
        )
        assert upload(client, "episodic", "doc.md", "   \n").status_code == 200

        refused = fold(client, "episodic", "doc.md", "decisions")

        assert refused.status_code == 403
        # Nothing was removed first: every chunk's row is still live, and every
        # ChromaDB row is still there.
        for key in keys:
            row = rows[
                ("semantic", foreign if key == foreign else physical_key(USER, key))
            ]
            assert row["deleted_at_ms"] is None
        assert len(_stored_chunk_ids(count)) == count
        back = read_whole(client, "decisions", "doc.md")
        assert back.status_code == 200
        assert back.json()["content"] == OLD
