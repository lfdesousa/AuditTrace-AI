"""SOFTDELETE-DIRECT-READ (operator ruling (a), 2026-11-20): a soft-deleted
(tombstoned) document answers 404 on every direct read except an admin's.

Every assertion goes through the real HTTP routes of ``create_app()`` (the
``memory_truncation_support`` harness). Statuses are asserted EXACTLY. A
tombstoned key and a key that never existed must be the same reply, so a
non-admin caller cannot tell them apart.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from audittrace.auth import require_user
from audittrace.identity import UserContext
from audittrace.routes import memory as m
from audittrace.tools import reset_registry_for_tests
from tests.memory_truncation_support import (
    SCOPES,
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
    as_admin,
    chroma_collection,
    memory_audit_details,
)

CANARY = "TOMBSTONE-CANARY-7f3a"
READER = "00000000-0000-0000-0000-00000000b002"
FILE_LAYERS = {
    "episodic": ("ADR-900-softdelete.md", "read_decision", "ADR not found"),
    "procedural": ("SKILL-SOFTDELETE.md", "read_skill", "SKILL not found"),
}
SEMANTIC_404 = {"detail": "semantic doc not found"}


@pytest.fixture(autouse=True)
def _real_read_tools() -> Iterator[None]:
    reset_registry_for_tests()
    import audittrace.tools.memory_handlers as handlers

    importlib.reload(handlers)
    yield
    reset_registry_for_tests()


@pytest.fixture(autouse=True)
def _pinned_tool_cache() -> Iterator[None]:
    """ADDENDUM-1 (gate r1 condition 2): a known tool-result cache for every
    test, never the Redis the host happens to run."""
    import fakeredis

    from audittrace.tools.cache import (
        ToolResultCache,
        reset_tool_result_cache,
        set_tool_result_cache,
    )

    redis = fakeredis.FakeRedis(decode_responses=True)
    set_tool_result_cache(ToolResultCache(redis, default_ttl_seconds=900))
    yield
    reset_tool_result_cache()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


def _tool(client: TestClient, name: str, file: str) -> dict[str, Any]:
    """The JSON payload of one MCP ``tools/call`` result, plus ``isError``."""
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": {"file": file}},
        },
    )
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    payload: dict[str, Any] = json.loads(result["content"][0]["text"])
    return {"isError": result["isError"], "payload": payload, "raw": r.text}


def _create_and_delete(client: TestClient, layer: str, name: str) -> None:
    body = f"# {name}\n\n{CANARY} body\n"
    created = client.post(f"/memory/{layer}", json={"filename": name, "content": body})
    assert created.status_code == 200, created.text
    deleted = client.delete(f"/memory/{layer}/{name}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_at_ms"] is not None


def _folded(client: TestClient, chunks: int = 5) -> list[str]:
    text = CANARY + "\n" + distinct_body(chunks * STEP)
    assert upload(client, "episodic", "doc.md", text).status_code == 200
    index = fold(client, "episodic", "doc.md", "decisions")
    assert index.status_code == 200, index.text
    count: int = index.json()["collections"]["decisions"]
    assert count >= 4
    return [m._doc_id("decisions", "doc.md", i) for i in range(count)]


def _soft_delete_chunk(client: TestClient, chunk_id: str) -> None:
    deleted = client.delete(f"/memory/semantic/decisions/{chunk_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_at_ms"] is not None


def _read_audit_rows() -> list[dict[str, Any]]:
    return [d for d in memory_audit_details() if d.get("op") == "read"]


class TestOwnerReadBackAfterSoftDeleteIs404:
    """AC-1 .. AC-4: the owner (non-admin) reads the key back after the delete."""

    @pytest.mark.parametrize("layer", sorted(FILE_LAYERS))
    def test_file_layer_read_is_404_and_equals_never_existed(
        self, client: TestClient, layer: str
    ) -> None:
        name, _tool_name, detail = FILE_LAYERS[layer]
        _create_and_delete(client, layer, name)

        back = client.get(f"/memory/{layer}/{name}")
        never = client.get(f"/memory/{layer}/never-{name}")

        assert never.status_code == 404
        assert back.status_code == 404
        assert back.json() == never.json() == {"detail": detail}
        assert CANARY not in back.text

    @pytest.mark.parametrize("layer", sorted(FILE_LAYERS))
    def test_file_layer_read_by_upload_shaped_key_is_404(
        self, client: TestClient, layer: str
    ) -> None:
        name, _tool_name, detail = FILE_LAYERS[layer]
        _create_and_delete(client, layer, name)

        back = client.get(f"/memory/{layer}/{USER}/{layer}/{name}")

        assert back.status_code == 404
        assert back.json() == {"detail": detail}
        assert CANARY not in back.text

    @pytest.mark.parametrize("layer", sorted(FILE_LAYERS))
    def test_read_tool_answers_not_found_and_equals_never_existed(
        self, client: TestClient, layer: str
    ) -> None:
        name, tool_name, _detail = FILE_LAYERS[layer]
        _create_and_delete(client, layer, name)

        back = _tool(client, tool_name, name)
        never = _tool(client, tool_name, f"never-{name}")

        assert back["isError"] is True
        assert back["payload"]["error"] == "not_found"
        assert CANARY not in back["raw"]
        swapped = json.loads(
            json.dumps(never["payload"]).replace(f"never-{name}", name)
        )
        assert back["payload"] == swapped

    @pytest.mark.parametrize("whole", [False, True], ids=["single-chunk", "whole"])
    def test_semantic_read_is_404_and_equals_never_existed(
        self, client: TestClient, whole: bool
    ) -> None:
        created = client.post(
            "/memory/semantic",
            json={
                "collection": "semantic",
                "document_id": "doc-1",
                "text": f"{CANARY} single",
                "title": "doc-1",
            },
        )
        assert created.status_code == 200, created.text
        assert client.delete("/memory/semantic/semantic/doc-1").status_code == 200
        params = {"whole_document": "true"} if whole else {}

        back = client.get("/memory/semantic/semantic/doc-1", params=params)
        never = client.get("/memory/semantic/semantic/never", params=params)

        assert never.status_code == 404
        assert back.status_code == 404
        assert back.json() == never.json() == SEMANTIC_404
        assert CANARY not in back.text

    @pytest.mark.parametrize("tombstoned", [0, 2], ids=["chunk0", "chunk2"])
    @pytest.mark.parametrize("entry", [0, 1, 2], ids=["via0", "via1", "via2"])
    def test_whole_document_with_a_tombstoned_chunk_is_404(
        self, client: TestClient, tombstoned: int, entry: int
    ) -> None:
        ids = _folded(client)
        _soft_delete_chunk(client, ids[tombstoned])

        back = client.get(
            f"/memory/semantic/decisions/{ids[entry]}",
            params={"whole_document": "true"},
        )

        assert back.status_code == 404
        assert back.json() == SEMANTIC_404
        assert CANARY not in back.text
        assert "[0000" not in back.text

    def test_single_read_of_the_tombstoned_chunk_is_404(
        self, client: TestClient
    ) -> None:
        ids = _folded(client)
        _soft_delete_chunk(client, ids[0])

        back = client.get(f"/memory/semantic/decisions/{ids[0]}")

        assert back.status_code == 404
        assert back.json() == SEMANTIC_404
        assert CANARY not in back.text

    def test_corpus_tombstone_is_404_for_a_non_admin_corpus_reader(
        self, client: TestClient
    ) -> None:
        text = CANARY + "\n" + distinct_body(2 * STEP)
        assert corpus_fold(client, text).status_code == 200
        chunk0 = m._doc_id("decisions", "doc.md", 0)
        assert client.delete(f"/memory/semantic/decisions/{chunk0}").status_code == 200
        client.app.dependency_overrides[require_user] = lambda: UserContext(  # type: ignore[attr-defined]
            user_id=READER,
            username=READER,
            agent_type="test",
            scopes=(*SCOPES, "memory:corpus:decisions:read"),
            is_admin=False,
        )

        single = client.get(f"/memory/semantic/decisions/{chunk0}")
        whole = read_whole(client, "decisions", "doc.md")

        assert (single.status_code, whole.status_code) == (404, 404)
        assert single.json() == whole.json() == SEMANTIC_404
        assert CANARY not in single.text + whole.text


class TestRefusedReadLeavesNoReadAuditRow:
    """AC-5: a refused read records what the route's existing 404 records: nothing."""

    def test_no_read_audit_row_for_a_tombstoned_key(self, client: TestClient) -> None:
        name, _tool_name, _detail = FILE_LAYERS["episodic"]
        _create_and_delete(client, "episodic", name)
        before = len(_read_audit_rows())

        assert client.get(f"/memory/episodic/{name}").status_code == 404

        assert len(_read_audit_rows()) == before


class TestAdminReadsTheTombstone:
    """AC-6 / AC-7: GREEN on main, and must stay GREEN (over-blocking guard)."""

    @pytest.mark.parametrize("layer", sorted(FILE_LAYERS))
    def test_admin_file_layer_read_is_200_with_content_and_tombstone(
        self, client: TestClient, layer: str
    ) -> None:
        name, _tool_name, _detail = FILE_LAYERS[layer]
        _create_and_delete(client, layer, name)

        with as_admin(client):
            back = client.get(f"/memory/{layer}/{name}")

        assert back.status_code == 200
        assert CANARY in back.json()["content"]
        assert back.json()["manifest"]["deleted_at_ms"] is not None
        assert back.json()["manifest"]["deleted_by_user_id"] == USER

    def test_admin_semantic_reads_are_200_with_content_and_tombstone(
        self, client: TestClient
    ) -> None:
        ids = _folded(client)
        _soft_delete_chunk(client, ids[0])

        with as_admin(client):
            single = client.get(f"/memory/semantic/decisions/{ids[0]}")
            whole = read_whole(client, "decisions", "doc.md")

        assert (single.status_code, whole.status_code) == (200, 200)
        assert CANARY in single.json()["content"]
        assert CANARY in whole.json()["content"]
        assert single.json()["manifest"]["deleted_at_ms"] is not None
        assert whole.json()["manifest"]["deleted_at_ms"] is not None
        assert whole.json()["chunk_count"] == len(ids)

    def test_admin_read_audit_row_keeps_its_shape(self, client: TestClient) -> None:
        name, _tool_name, _detail = FILE_LAYERS["episodic"]
        created = client.post(
            "/memory/episodic", json={"filename": name, "content": f"{CANARY}\n"}
        )
        assert created.status_code == 200
        with as_admin(client):
            assert client.get(f"/memory/episodic/{name}").status_code == 200
        live_row = _read_audit_rows()[-1]
        assert client.delete(f"/memory/episodic/{name}").status_code == 200

        with as_admin(client):
            assert client.get(f"/memory/episodic/{name}").status_code == 200

        tombstone_row = _read_audit_rows()[-1]
        assert tombstone_row is not live_row
        assert sorted(tombstone_row) == sorted(live_row)
        assert tombstone_row == live_row


class TestWhatStaysReadable:
    """AC-9 / AC-10: GREEN on main, and must stay GREEN (over-blocking guards).
    r1 AC-8 (a live sibling chunk stays 200) is SUPERSEDED by ADDENDUM-1 AC-13."""

    @pytest.mark.parametrize("layer", sorted(FILE_LAYERS))
    def test_recreate_after_soft_delete_is_readable_again(
        self, client: TestClient, layer: str
    ) -> None:
        name, _tool_name, _detail = FILE_LAYERS[layer]
        _create_and_delete(client, layer, name)
        revived = client.post(
            f"/memory/{layer}", json={"filename": name, "content": "revived\n"}
        )
        assert revived.status_code == 200, revived.text

        back = client.get(f"/memory/{layer}/{name}")

        assert back.status_code == 200
        assert back.json()["content"] == "revived\n"
        assert back.json()["manifest"]["deleted_at_ms"] is None

    def test_soft_delete_leaves_every_chunk_row_in_the_vector_store(
        self, client: TestClient
    ) -> None:
        """Pins today's answer to operator question Q1 (keep the rows)."""
        import asyncio

        ids = _folded(client)
        before = asyncio.run(chroma_collection("decisions").get(include=[]))["ids"]

        _soft_delete_chunk(client, ids[0])

        after = asyncio.run(chroma_collection("decisions").get(include=[]))["ids"]
        assert sorted(after) == sorted(before)
        assert len(after) == len(ids)
