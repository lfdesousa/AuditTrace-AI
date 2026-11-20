"""MEMORY-SERVER-SILENT-TRUNCATION ADDENDUM-2 (gate r2 S12): a document with a
whitespace-only window reads back whole.

``_chunk_text`` drops a window that is only whitespace. At the END of a
document that takes as little as 201 whitespace characters. At 0ea4e1ed such a
document reads ``200`` without that text; the fold now keeps it."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from audittrace.routes import memory as m
from tests.memory_truncation_support import (
    distinct_body,
    fold,
    memory_client,
    read_whole,
    upload,
)
from tests.memory_truncation_support_a1 import STEP

BODY = distinct_body(10 * STEP)
CASES = {
    "201-trailing-newlines": BODY + "\n" * 201,
    "250-trailing-newlines": BODY + "\n" * 250,
    "1299-trailing-newlines": BODY + "\n" * 1299,
    "600-trailing-spaces": BODY + " " * 600,
    "interior-run-of-3-chunks": BODY + " \t\n" * m.CHUNK_SIZE + BODY,
    "leading-run-of-2-chunks": "\n" * (2 * m.CHUNK_SIZE) + BODY,
}


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


class TestWhitespaceDocumentsReadWhole:
    @pytest.mark.parametrize("case", sorted(CASES))
    def test_the_document_reads_back_exactly_and_verified(
        self, client: TestClient, case: str
    ) -> None:
        text = CASES[case]
        assert len(m._chunk_text(text)) < -(-len(text) // STEP), (
            "the case must make _chunk_text drop a window"
        )
        assert upload(client, "episodic", "blank.md", text).status_code == 200
        index = fold(client, "episodic", "blank.md", "decisions")
        assert index.status_code == 200, index.text

        back = read_whole(client, "decisions", "blank.md")

        assert back.status_code == 200
        assert back.json()["content"] == text
        assert back.json().get("verified") is True

    def test_a_document_that_is_only_whitespace_folds_to_nothing_and_is_not_a_500(
        self, client: TestClient
    ) -> None:
        assert upload(client, "episodic", "void.md", "\n" * 5000).status_code == 200

        index = fold(client, "episodic", "void.md", "decisions")

        assert index.status_code == 200, index.text
        assert index.json()["collections"]["decisions"] == 0
