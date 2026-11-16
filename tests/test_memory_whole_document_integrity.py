"""MEMORY-SERVER-SILENT-TRUNCATION ADDENDUM-1, WU-1: a whole-document read is
the text ONE fold wrote, verified, or it is a 409.

Closes gate r1 B1 (chunk 0 missing), B2 (chunks of two folds), S2 (bounded
work under a forged stamp) and S4 (the stamp is observable in the response).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from audittrace.routes import memory as m
from tests.memory_truncation_support import (
    USER,
    distinct_body,
    fold,
    memory_client,
    read_whole,
    upload,
)
from tests.memory_truncation_support_a1 import (
    STEP,
    STUB_VECTOR,
    chroma_collection,
    hard_delete_chunk,
    sha256_text,
)

WINDOW = 500


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


def _letter_body(letter: str, chunks: int) -> str:
    """A body of exactly *chunks* chunks whose every line names its fold."""
    lines = chunks * STEP // 9
    return "".join(f"[{letter}{i:05d}]\n" for i in range(lines))


def _fold_ok(client: TestClient, filename: str, text: str) -> int:
    assert upload(client, "episodic", filename, text).status_code == 200
    index = fold(client, "episodic", filename, "decisions")
    assert index.status_code == 200, index.text
    count: int = index.json()["collections"]["decisions"]
    return count


def _refold_failing_on_second_batch(
    client: TestClient, filename: str, text: str
) -> None:
    """Upload *text* and re-fold it with the embedder failing on its 2nd batch."""
    assert upload(client, "episodic", filename, text).status_code == 200
    calls = {"n": 0}

    async def flaky(texts: list[str], **_: Any) -> list[list[float]]:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("embedder down")
        return [STUB_VECTOR for _ in texts]

    with patch.object(m, "embed_via_nomic", AsyncMock(side_effect=flaky)):
        try:
            answer = fold(client, "episodic", filename, "decisions")
        except RuntimeError:
            return
    assert answer.status_code >= 500, answer.text


class TestWholeDocumentIntegrity:
    def test_stamped_document_without_chunk_0_is_refused_from_any_chunk(
        self, client: TestClient
    ) -> None:
        total = _fold_ok(client, "headless.md", distinct_body(10 * STEP))
        hard_delete_chunk(client, "decisions", "headless.md", 0)
        chunk3 = m._doc_id("decisions", "headless.md", 3)

        back = client.get(
            f"/memory/semantic/decisions/{chunk3}", params={"whole_document": "true"}
        )

        assert back.status_code == 409
        assert back.json() == {
            "detail": f"semantic document incomplete: 0 of {total} chunks readable"
        }

    @pytest.mark.parametrize("new_chunks", [110, 120])
    def test_failed_refold_is_refused_never_read_mixed(
        self, client: TestClient, new_chunks: int
    ) -> None:
        """120 -> 110 chunks is the gate's case P2; 120 -> 120 is the case a
        chunk COUNT cannot see."""
        _fold_ok(client, "mixed.md", _letter_body("A", 120))
        _refold_failing_on_second_batch(
            client, "mixed.md", _letter_body("B", new_chunks)
        )

        back = read_whole(client, "decisions", "mixed.md")

        assert back.status_code == 409
        assert back.json() == {
            "detail": (
                f"semantic document incomplete: {m._INDEX_BATCH_SIZE} of "
                f"{new_chunks} chunks readable"
            )
        }

    def test_a_clean_refold_after_a_failed_one_reads_whole_again(
        self, client: TestClient
    ) -> None:
        _fold_ok(client, "healed.md", _letter_body("A", 120))
        second = _letter_body("B", 110)
        _refold_failing_on_second_batch(client, "healed.md", second)
        _fold_ok(client, "healed.md", second)

        back = read_whole(client, "decisions", "healed.md")

        assert back.status_code == 200
        assert back.json()["content"] == second
        assert back.json().get("verified") is True

    def test_a_chunk_whose_text_changed_under_its_stamp_is_refused(
        self, client: TestClient
    ) -> None:
        from audittrace.services.memory_keys import private_physical_id
        from tests.memory_truncation_support_a1 import caller

        _fold_ok(client, "tampered.md", distinct_body(10 * STEP))
        col = chroma_collection("decisions")
        pid = private_physical_id(caller(), m._doc_id("decisions", "tampered.md", 5))
        row = asyncio.run(col.get(ids=[pid], include=["documents", "metadatas"]))
        changed = "X" * len(row["documents"][0])
        asyncio.run(
            col.upsert(
                ids=[pid],
                documents=[changed],
                metadatas=row["metadatas"],
                embeddings=[STUB_VECTOR],
            )
        )

        back = read_whole(client, "decisions", "tampered.md")

        assert back.status_code == 409
        assert back.json() == {
            "detail": "semantic document does not match the text its fold wrote"
        }

    def test_a_forged_stamp_costs_one_window_and_is_refused(
        self, client: TestClient
    ) -> None:
        """A stamp only a fold can write (the routes strip it) is forged here in
        ChromaDB itself: chunk 0 claims 2,000,000 chunks."""
        from audittrace.services.memory_keys import private_physical_id
        from audittrace.services.semantic import ChromaSemanticService
        from tests.memory_truncation_support_a1 import caller

        forged_total = 2_000_000
        _fold_ok(client, "forged.md", distinct_body(10 * STEP))
        col = chroma_collection("decisions")
        pid = private_physical_id(caller(), m._doc_id("decisions", "forged.md", 0))
        row = asyncio.run(col.get(ids=[pid], include=["documents", "metadatas"]))
        asyncio.run(
            col.upsert(
                ids=[pid],
                documents=row["documents"],
                metadatas=[{**row["metadatas"][0], "chunk_total": forged_total}],
                embeddings=[STUB_VECTOR],
            )
        )
        asked: list[int] = []
        real = ChromaSemanticService.get_documents

        async def counting(
            self: Any, user: Any, collection: str, ids: list[str]
        ) -> Any:
            asked.append(len(ids))
            return await real(self, user, collection, ids)

        with patch.object(ChromaSemanticService, "get_documents", counting):
            back = read_whole(client, "decisions", "forged.md")

        assert back.status_code == 409
        assert back.json() == {
            "detail": f"semantic document incomplete: 1 of {forged_total} chunks readable"
        }
        # the requested chunk itself, then ONE window: never the forged total
        assert sum(asked) <= WINDOW + 1

    def test_the_stamp_and_the_verdict_are_in_the_response(
        self, client: TestClient
    ) -> None:
        text = distinct_body(10 * STEP)
        total = _fold_ok(client, "stamped.md", text)

        body = read_whole(client, "decisions", "stamped.md").json()

        assert body["content"] == text
        assert body.get("verified") is True
        assert body["metadata"].get("chunk_total") == total
        assert type(body["metadata"].get("chunk_total")) is int
        assert body["metadata"].get("fold_sha256") == sha256_text(text)
        assert body["chunk_count"] == total

    def test_single_chunk_fallback_is_for_unstamped_rows_only(
        self, client: TestClient
    ) -> None:
        """Restates ``test_whole_document_falls_back_when_chunk_sequence_is_empty``:
        an untitled direct write has no sequence and no stamp; it is returned as
        itself and reported unverified."""
        written = client.post(
            "/memory/semantic",
            json={"collection": "decisions", "document_id": "untitled", "text": "one"},
        )
        assert written.status_code == 200, written.text

        back = client.get(
            "/memory/semantic/decisions/untitled", params={"whole_document": "true"}
        )

        assert back.status_code == 200
        assert back.json()["content"] == "one"
        assert back.json()["chunk_count"] == 1
        assert back.json().get("verified") is False
        assert USER  # the caller reads their own row
