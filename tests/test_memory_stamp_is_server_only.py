"""MEMORY-SERVER-SILENT-TRUNCATION ADDENDUM-2 (gate r2 S10): only a fold stamps.

``chunk_total`` and ``fold_sha256`` in the metadata of a direct semantic write
are stripped like ``tier`` and ``user_id``: a writer cannot make a read of
their own text answer ``verified: true``."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from audittrace.routes import memory as m
from tests.memory_truncation_support import memory_client
from tests.memory_truncation_support_a1 import sha256_text

TEXT = "a text its writer would like to see called verified"


def _forged() -> dict[str, Any]:
    return {"source": "forged.md", "chunk_total": 1, "fold_sha256": sha256_text(TEXT)}


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


def _read(client: TestClient, collection: str, doc_id: str) -> dict[str, Any]:
    back = client.get(
        f"/memory/semantic/{collection}/{doc_id}", params={"whole_document": "true"}
    )
    assert back.status_code == 200, back.text
    body: dict[str, Any] = back.json()
    return body


class TestStampIsServerOnly:
    def test_a_stamp_in_a_direct_write_is_stripped_and_the_read_is_unverified(
        self, client: TestClient
    ) -> None:
        doc_id = m._doc_id("decisions", "forged.md", 0)
        written = client.post(
            "/memory/semantic",
            json={
                "collection": "decisions",
                "document_id": doc_id,
                "text": TEXT,
                "metadata": _forged(),
            },
        )
        assert written.status_code == 200, written.text

        body = _read(client, "decisions", doc_id)

        assert body["content"] == TEXT
        assert body.get("verified") is False
        assert "chunk_total" not in body["metadata"]
        assert "fold_sha256" not in body["metadata"]

    def test_a_stamp_in_an_update_is_stripped(self, client: TestClient) -> None:
        doc_id = m._doc_id("decisions", "forged.md", 0)
        client.post(
            "/memory/semantic",
            json={"collection": "decisions", "document_id": doc_id, "text": "seed"},
        )
        updated = client.put(
            f"/memory/semantic/decisions/{doc_id}",
            json={"text": TEXT, "metadata": _forged()},
        )
        assert updated.status_code == 200, updated.text

        body = _read(client, "decisions", doc_id)

        assert body.get("verified") is False
        assert "chunk_total" not in body["metadata"]

    def test_a_stamp_in_an_mcp_write_is_stripped(self, client: TestClient) -> None:
        doc_id = m._doc_id("decisions", "forged.md", 0)
        answer = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "write_decision",
                    "arguments": {
                        "document_id": doc_id,
                        "text": TEXT,
                        "metadata": _forged(),
                    },
                },
            },
        )
        assert answer.status_code == 200
        assert answer.json()["result"]["isError"] is False

        body = _read(client, "decisions", doc_id)

        assert body.get("verified") is False
        assert "fold_sha256" not in body["metadata"]
