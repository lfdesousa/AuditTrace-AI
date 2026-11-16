"""MEMORY-SERVER-SILENT-TRUNCATION ADDENDUM-1, WU-1 (gate r1 S1): documents
folded before the stamp existed.

Such a document carries no total and no fold identity, and a shorter re-fold
at 1f44bf3e left the old tail behind. Its read is reported ``verified: false``;
one re-fold through the route makes it exact and verified.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from tests.memory_truncation_support import (
    chunk_total,
    distinct_body,
    fold,
    memory_client,
    read_whole,
    upload,
)
from tests.memory_truncation_support_a1 import STEP, fold_before_the_stamp

LONG = distinct_body(10 * STEP)
SHORT = "".join(f"<{i:06d}>\n" for i in range(3 * STEP // 9))


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


class TestDocumentsFoldedBeforeTheStamp:
    def test_an_unstamped_document_is_reported_unverified(
        self, client: TestClient
    ) -> None:
        fold_before_the_stamp("decisions", "old.md", LONG)

        back = read_whole(client, "decisions", "old.md")

        assert back.status_code == 200
        assert back.json()["content"] == LONG
        assert back.json().get("verified") is False

    @pytest.mark.parametrize("bare_ids", [False, True])
    def test_one_refold_makes_an_old_document_exact_and_verified(
        self, client: TestClient, bare_ids: bool
    ) -> None:
        """Two folds at 1f44bf3e, the second shorter (gate case P3), then ONE
        re-fold after the fix. ``bare_ids``: the old chunks sit at the
        pre-LC-9 id."""
        was = fold_before_the_stamp("decisions", "old.md", LONG, bare_ids=bare_ids)
        fold_before_the_stamp("decisions", "old.md", SHORT, bare_ids=bare_ids)
        assert upload(client, "episodic", "old.md", SHORT).status_code == 200

        index = fold(client, "episodic", "old.md", "decisions")

        assert index.status_code == 200, index.text
        now = index.json()["collections"]["decisions"]
        assert now < was
        back = read_whole(client, "decisions", "old.md")
        assert back.status_code == 200
        assert back.json()["content"] == SHORT
        assert back.json().get("verified") is True
        assert chunk_total(client, "decisions") == now
