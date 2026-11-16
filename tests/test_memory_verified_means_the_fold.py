"""MEMORY-SERVER-SILENT-TRUNCATION ADDENDUM-2 (gate r2 S9): what ``verified``
says. ``verified: true`` means the text equals what the fold that wrote chunk 0
chunked. It says nothing about the stored object: after a re-upload that was
not re-folded, the read is still the OLD fold, verified. The comparison of
``fold_sha256`` with the stored object is the only statement about the object."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from tests.memory_truncation_support import (
    distinct_body,
    fold,
    memory_client,
    read_whole,
    upload,
)
from tests.memory_truncation_support_a1 import STEP, sha256_text

FIRST = distinct_body(4 * STEP)
SECOND = "".join(f"<{i:06d}>\n" for i in range(4 * STEP // 9))


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


class TestVerifiedMeansTheFold:
    def test_a_reupload_without_a_refold_still_reads_the_old_fold_verified(
        self, client: TestClient
    ) -> None:
        assert upload(client, "episodic", "doc.md", FIRST).status_code == 200
        assert fold(client, "episodic", "doc.md", "decisions").status_code == 200
        assert upload(client, "episodic", "doc.md", SECOND).status_code == 200

        body = read_whole(client, "decisions", "doc.md").json()
        stored = client.get("/memory/episodic/doc.md").json()["content"]

        assert stored == SECOND
        assert body.get("verified") is True
        assert body["content"] == FIRST
        assert body["metadata"]["fold_sha256"] == sha256_text(FIRST)
        assert body["metadata"]["fold_sha256"] != sha256_text(stored)

    def test_after_the_refold_the_stamp_equals_the_stored_object(
        self, client: TestClient
    ) -> None:
        assert upload(client, "episodic", "doc.md", FIRST).status_code == 200
        assert fold(client, "episodic", "doc.md", "decisions").status_code == 200
        assert upload(client, "episodic", "doc.md", SECOND).status_code == 200
        assert fold(client, "episodic", "doc.md", "decisions").status_code == 200

        body = read_whole(client, "decisions", "doc.md").json()
        stored = client.get("/memory/episodic/doc.md").json()["content"]

        assert body.get("verified") is True
        assert body["content"] == stored == SECOND
        assert body["metadata"]["fold_sha256"] == sha256_text(stored)
