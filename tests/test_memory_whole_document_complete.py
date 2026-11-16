"""MEMORY-SERVER-SILENT-TRUNCATION, WU-1: a whole-document read returns the
WHOLE document or an explicit error, never a silent prefix.

At 1f44bf3e ``GET /memory/semantic/{collection}/{id}?whole_document=true`` reads
at most ``_MAX_DOCUMENT_CHUNKS`` chunks and returns their reassembly with
``200`` and no flag: a document one character past the cap comes back as a
strict prefix. The stored object and the index are complete; the READ is not.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from audittrace.routes import memory as m
from tests.memory_truncation_support import (
    SCOPES,
    USER,
    chunk_total,
    distinct_body,
    fold,
    memory_client,
    read_whole,
    upload,
)

STEP = m.CHUNK_SIZE - m.CHUNK_OVERLAP
# The chunk cap in force at 1f44bf3e (``_MAX_DOCUMENT_CHUNKS``, removed by the
# fix, so this file owns the number) and the longest text that cap still covered.
FORMER_CAP = 500
LAST_COMPLETE = m.CHUNK_SIZE + (FORMER_CAP - 1) * STEP

WRITE_PATHS = ("upload", "post", "put")


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


@contextmanager
def _as_admin(client: TestClient) -> Iterator[None]:
    """The same caller, holding ``audittrace:admin`` for the enclosed request."""
    from audittrace.auth import require_user
    from audittrace.identity import UserContext

    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    previous = overrides[require_user]
    admin = UserContext(
        user_id=USER,
        username=USER,
        agent_type="test",
        scopes=(*SCOPES, "audittrace:admin"),
        is_admin=True,
    )
    overrides[require_user] = lambda: admin
    try:
        yield
    finally:
        overrides[require_user] = previous


def _write(client: TestClient, path: str, layer: str, filename: str, text: str) -> None:
    if path == "upload":
        response = upload(client, layer, filename, text)
    elif path == "post":
        response = client.post(
            f"/memory/{layer}", json={"filename": filename, "content": text}
        )
    else:
        seed = client.post(
            f"/memory/{layer}", json={"filename": filename, "content": "seed\n"}
        )
        assert seed.status_code == 200, seed.text
        response = client.put(f"/memory/{layer}/{filename}", json={"content": text})
    assert response.status_code == 200, response.text


class TestWholeDocumentReadIsComplete:
    def test_control_last_length_under_the_cap_reads_byte_exact(
        self, client: TestClient
    ) -> None:
        text = distinct_body(LAST_COMPLETE)
        _write(client, "upload", "episodic", "control.md", text)
        assert fold(client, "episodic", "control.md", "decisions").status_code == 200
        back = read_whole(client, "decisions", "control.md")
        assert back.status_code == 200
        assert back.json()["content"] == text

    @pytest.mark.parametrize("path", WRITE_PATHS)
    @pytest.mark.parametrize(
        ("layer", "collection"), [("episodic", "decisions"), ("procedural", "skills")]
    )
    @pytest.mark.parametrize("extra", [1, 2 * LAST_COMPLETE])
    def test_document_past_the_cap_reads_byte_exact(
        self, client: TestClient, path: str, layer: str, collection: str, extra: int
    ) -> None:
        text = distinct_body(LAST_COMPLETE + extra)
        _write(client, path, layer, "big.md", text)
        index = fold(client, layer, "big.md", collection)
        assert index.status_code == 200, index.text
        indexed = index.json()["collections"][collection]

        back = read_whole(client, collection, "big.md")

        assert back.status_code == 200
        body = back.json()
        assert len(body["content"]) == len(text)
        assert body["content"] == text
        assert body["chunk_count"] == indexed

    def test_two_byte_characters_past_the_cap_read_byte_exact(
        self, client: TestClient
    ) -> None:
        text = "é" * (LAST_COMPLETE + 1)
        _write(client, "upload", "episodic", "accents.md", text)
        assert fold(client, "episodic", "accents.md", "decisions").status_code == 200
        back = read_whole(client, "decisions", "accents.md")
        assert back.status_code == 200
        assert back.json()["content"].encode() == text.encode()

    def test_a_missing_chunk_is_an_explicit_error_never_a_prefix(
        self, client: TestClient
    ) -> None:
        text = distinct_body(10 * STEP)
        _write(client, "upload", "episodic", "holed.md", text)
        index = fold(client, "episodic", "holed.md", "decisions")
        indexed = index.json()["collections"]["decisions"]
        gone = m._doc_id("decisions", "holed.md", 4)
        # A hard delete (admin only) removes the ChromaDB row itself: a real hole.
        with _as_admin(client):
            removed = client.delete(
                f"/memory/semantic/decisions/{gone}", params={"hard": "true"}
            )
        assert removed.status_code == 200, removed.text

        back = read_whole(client, "decisions", "holed.md")

        assert back.status_code == 409
        assert back.json() == {
            "detail": f"semantic document incomplete: 4 of {indexed} chunks readable"
        }

    def test_a_shorter_reindex_leaves_no_stale_tail_in_the_read(
        self, client: TestClient
    ) -> None:
        _write(client, "upload", "episodic", "shrunk.md", distinct_body(10 * STEP))
        assert fold(client, "episodic", "shrunk.md", "decisions").status_code == 200
        shorter = distinct_body(3 * STEP)
        _write(client, "upload", "episodic", "shrunk.md", shorter)
        assert fold(client, "episodic", "shrunk.md", "decisions").status_code == 200

        back = read_whole(client, "decisions", "shrunk.md")

        assert back.status_code == 200
        assert back.json()["content"] == shorter

    def test_a_shorter_reindex_removes_the_stale_tail_chunks(
        self, client: TestClient
    ) -> None:
        _write(client, "upload", "episodic", "shrunk.md", distinct_body(10 * STEP))
        first = fold(client, "episodic", "shrunk.md", "decisions")
        was = first.json()["collections"]["decisions"]
        _write(client, "upload", "episodic", "shrunk.md", distinct_body(3 * STEP))
        second = fold(client, "episodic", "shrunk.md", "decisions")
        now = second.json()["collections"]["decisions"]
        assert now < was

        assert chunk_total(client, "decisions") == now
        for index in range(now, was):
            stale = m._doc_id("decisions", "shrunk.md", index)
            assert client.get(f"/memory/semantic/decisions/{stale}").status_code == 404
