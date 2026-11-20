"""MEMORY-SERVER-SILENT-TRUNCATION r2 (review r1 findings 4 and 5): the edges of
the fold stamp a whole-document read trusts.

* a stamp of the wrong type, or half of one, is refused as malformed (C3');
* rows past the stamped total belong to no fold of this document and are
  trimmed: the read is the stamped total, verified and exact.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from audittrace.routes import memory as m
from audittrace.services.memory_keys import private_physical_id
from tests.memory_truncation_support import distinct_body, fold, memory_client, upload
from tests.memory_truncation_support_a1 import (
    STEP,
    STUB_VECTOR,
    caller,
    chroma_collection,
)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


def _fold_ok(client: TestClient, filename: str, text: str) -> int:
    assert upload(client, "episodic", filename, text).status_code == 200
    index = fold(client, "episodic", filename, "decisions")
    assert index.status_code == 200, index.text
    count: int = index.json()["collections"]["decisions"]
    return count


def _physical(filename: str, index: int) -> str:
    return private_physical_id(caller(), m._doc_id("decisions", filename, index))


def _row(filename: str, index: int) -> Any:
    col = chroma_collection("decisions")
    return asyncio.run(
        col.get(ids=[_physical(filename, index)], include=["documents", "metadatas"])
    )


def _rewrite_metadata(filename: str, index: int, metadata: dict[str, Any]) -> None:
    row = _row(filename, index)
    col = chroma_collection("decisions")
    asyncio.run(col.delete(ids=[_physical(filename, index)]))
    asyncio.run(
        col.upsert(
            ids=[_physical(filename, index)],
            documents=row["documents"],
            metadatas=[metadata],
            embeddings=[STUB_VECTOR],
        )
    )


def _read(client: TestClient, filename: str) -> Any:
    return client.get(
        f"/memory/semantic/decisions/{m._doc_id('decisions', filename, 0)}",
        params={"whole_document": "true"},
    )


MALFORMED = {"detail": "semantic document carries a malformed stamp"}


class TestMalformedStamp:
    @pytest.mark.parametrize(
        "bad_total",
        [True, 0, -3, "5", 5.5],
        ids=["bool", "zero", "neg", "str", "float"],
    )
    def test_a_total_of_the_wrong_type_or_range_is_refused(
        self, client: TestClient, bad_total: Any
    ) -> None:
        _fold_ok(client, "bad.md", distinct_body(4 * STEP))
        meta = dict(_row("bad.md", 0)["metadatas"][0])
        meta["chunk_total"] = bad_total
        _rewrite_metadata("bad.md", 0, meta)

        back = _read(client, "bad.md")

        assert back.status_code == 409
        assert back.json() == MALFORMED

    @pytest.mark.parametrize("dropped", ["chunk_total", "fold_sha256"])
    def test_half_a_stamp_is_refused(self, client: TestClient, dropped: str) -> None:
        _fold_ok(client, "half.md", distinct_body(4 * STEP))
        meta = dict(_row("half.md", 0)["metadatas"][0])
        del meta[dropped]
        _rewrite_metadata("half.md", 0, meta)

        back = _read(client, "half.md")

        assert back.status_code == 409
        assert back.json() == MALFORMED

    @pytest.mark.parametrize("bad_fold", ["", 7], ids=["empty", "int"])
    def test_a_fold_hash_that_is_not_text_is_refused(
        self, client: TestClient, bad_fold: Any
    ) -> None:
        _fold_ok(client, "badfold.md", distinct_body(4 * STEP))
        meta = dict(_row("badfold.md", 0)["metadatas"][0])
        meta["fold_sha256"] = bad_fold
        _rewrite_metadata("badfold.md", 0, meta)

        back = _read(client, "badfold.md")

        assert back.status_code == 409
        assert back.json() == MALFORMED


class TestTrimToTheStampedTotal:
    def test_rows_past_the_stamped_total_are_not_part_of_the_document(
        self, client: TestClient
    ) -> None:
        text = distinct_body(4 * STEP)
        total = _fold_ok(client, "tail.md", text)
        last = _row("tail.md", total - 1)
        col = chroma_collection("decisions")
        for extra in (total, total + 1):
            asyncio.run(
                col.upsert(
                    ids=[_physical("tail.md", extra)],
                    documents=last["documents"],
                    metadatas=[{**last["metadatas"][0], "chunk": extra}],
                    embeddings=[STUB_VECTOR],
                )
            )

        back = _read(client, "tail.md")

        assert back.status_code == 200, back.text
        assert back.json()["content"] == text
        assert back.json()["verified"] is True
        assert back.json()["chunk_count"] == total
