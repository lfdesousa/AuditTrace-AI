"""AUDIT-ROW-TIER WU-1, gate r2 condition C5 (OQ-1): the ownership exemption.

A semantic list row names the tiers the list was CLEARED to enumerate, not the
tiers of the items it returned. A caller with ``memory:corpus:decisions:write``
and no ``:read`` creates a corpus document, sees it listed under the corpus
gate's ownership exemption (item tier ``corpus``), cannot read it back (403),
and the list row says ``private``: right about the clearance, silent about the
one corpus row the caller itself wrote. The release note carries the caveat.

The helpers are the ratified RED file's own (``test_memory_audit_tier_a1``,
copied unchanged); this module only adds the case the gate asked to pin.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from tests.memory_truncation_support import USER, memory_client
from tests.test_memory_audit_tier_a1 import as_caller, audit_rows


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


class TestSemanticListTierOwnership:
    def test_an_own_corpus_row_listed_under_the_exemption_records_private(
        self, client: TestClient
    ) -> None:
        with as_caller(client, USER, "memory:corpus:decisions:write"):
            made = client.post(
                "/memory/semantic",
                params={"promote": "corpus"},
                json={"collection": "decisions", "document_id": "doc-o", "text": "c"},
            )
            assert made.status_code == 200, made.text
            listing = client.get("/memory/semantic", params={"collection": "decisions"})
            assert listing.status_code == 200, listing.text
            read = client.get("/memory/semantic/decisions/doc-o")
        assert [i["tier"] for i in listing.json()["items"]] == ["corpus"]
        assert read.status_code == 403
        assert audit_rows(op="list") == [("list", "private", None)]
