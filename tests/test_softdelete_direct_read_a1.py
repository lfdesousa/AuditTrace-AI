"""SOFTDELETE-DIRECT-READ ADDENDUM-1 (spec round r2): the operator rulings of
2026-11-20 and the gate r1 conditions, as tests.

Every assertion goes through the real HTTP routes of ``create_app()`` (or the
real ``/mcp``), statuses asserted EXACTLY, a refused read compared with a
never-existed read. The tool-result cache is pinned to fakeredis for every
test, so no result depends on the Redis the host happens to run.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from audittrace.auth import require_user
from audittrace.identity import UserContext
from audittrace.routes import memory as m
from audittrace.tools import get_tool_by_name, reset_registry_for_tests
from audittrace.tools.cache import (
    ToolResultCache,
    reset_tool_result_cache,
    set_tool_result_cache,
)
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
    fold_before_the_stamp,
    manifest,
    memory_audit_details,
)

CANARY = "tombcanary7f3a"
OTHER = "00000000-0000-0000-0000-00000000b002"
SEMANTIC_404 = {"detail": "semantic doc not found"}
SESSION = "softdelete-a1-session-0001"
# layer -> (filename, fold target, read tool, not-found detail of the route)
SOURCES = {
    "episodic": ("doc.md", "decisions", "read_decision", "ADR not found"),
    "procedural": ("SKILL-FOLD.md", "skills", "read_skill", "SKILL not found"),
}
FOLDS = [
    ("episodic", "decisions"),
    ("procedural", "skills"),
    ("episodic", "semantic"),
    ("procedural", "semantic"),
]
FOLD_IDS = [f"{layer}-to-{col}" for layer, col in FOLDS]


@pytest.fixture(autouse=True)
def _real_read_tools() -> Iterator[None]:
    reset_registry_for_tests()
    import audittrace.tools.memory_handlers as handlers

    importlib.reload(handlers)
    yield
    reset_registry_for_tests()


@pytest.fixture(autouse=True)
def tool_cache() -> Iterator[Any]:
    """Gate r1 condition 2: a KNOWN cache for every test (the shipped
    ``fakeredis_cache`` fixture of ``tests/test_lc9_cache_key.py``)."""
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    set_tool_result_cache(ToolResultCache(redis, default_ttl_seconds=900))
    yield redis
    reset_tool_result_cache()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


@contextmanager
def as_user(
    client: TestClient, user_id: str, *, extra: tuple[str, ...] = ()
) -> Iterator[None]:
    """Another non-admin caller for the enclosed requests."""
    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    previous = overrides[require_user]
    overrides[require_user] = lambda: UserContext(
        user_id=user_id,
        username=user_id,
        agent_type="test",
        scopes=(*SCOPES, *extra),
        is_admin=False,
    )
    try:
        yield
    finally:
        overrides[require_user] = previous


def _tool(
    client: TestClient, name: str, arguments: dict[str, Any], sid: str = SESSION
) -> dict[str, Any]:
    r = client.post(
        "/mcp",
        headers={"X-Session-Id": sid},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    payload: dict[str, Any] = json.loads(result["content"][0]["text"])
    return {"isError": result["isError"], "payload": payload, "raw": r.text}


def _fold_source(
    client: TestClient, layer: str, collection: str, chunks: int = 5
) -> list[str]:
    """Upload ``layer``'s source and fold it; the fold's chunk ids, in order."""
    name = SOURCES[layer][0]
    text = CANARY + "\n" + distinct_body(chunks * STEP)
    assert upload(client, layer, name, text).status_code == 200
    index = fold(client, layer, name, collection)
    assert index.status_code == 200, index.text
    count: int = index.json()["collections"][collection]
    assert count >= 4
    return [m._doc_id(collection, name, i) for i in range(count)]


def _delete_source(client: TestClient, layer: str) -> None:
    deleted = client.delete(f"/memory/{layer}/{SOURCES[layer][0]}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_at_ms"] is not None


def _delete_chunk(client: TestClient, collection: str, chunk_id: str) -> None:
    deleted = client.delete(f"/memory/semantic/{collection}/{chunk_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_at_ms"] is not None


def _chroma_ids(collection: str) -> list[str]:
    ids: list[str] = asyncio.run(chroma_collection(collection).get(include=[]))["ids"]
    return sorted(ids)


def _same_reply(back: Any, never: Any) -> None:
    """Gate r1 S4: raw bytes and headers, not parsed JSON."""
    assert back.status_code == never.status_code == 404
    assert back.content == never.content
    assert sorted(back.headers) == sorted(never.headers)
    for header in ("content-type", "content-length"):
        assert back.headers[header] == never.headers[header]
    assert CANARY.encode() not in back.content


def _read_rows() -> list[dict[str, Any]]:
    return [d for d in memory_audit_details() if d.get("op") == "read"]


def _mcp_rows() -> list[tuple[str, str | None, str]]:
    """``(status, failure_class, answer)`` of every persisted /mcp interaction."""
    from sqlalchemy import select

    from audittrace.db.models import InteractionRecord
    from audittrace.dependencies import get_postgres_factory

    async def read() -> list[tuple[str, str | None, str]]:
        factory = get_postgres_factory().get_session_factory()
        async with factory() as db:
            rows = await db.execute(
                select(
                    InteractionRecord.status,
                    InteractionRecord.failure_class,
                    InteractionRecord.answer,
                )
                .where(InteractionRecord.source == "mcp")
                .order_by(InteractionRecord.id)
            )
            return [(s, f, a) for (s, f, a) in rows.all()]

    return asyncio.run(read())


class TestSourceDeleteTombstonesItsFold:
    """AC-11 (operator ruling 1, gate B1): deleting the episodic / procedural
    source soft-deletes every chunk its fold wrote."""

    @pytest.mark.parametrize(("layer", "collection"), FOLDS, ids=FOLD_IDS)
    def test_every_fold_chunk_is_404_after_the_source_delete(
        self, client: TestClient, layer: str, collection: str
    ) -> None:
        ids = _fold_source(client, layer, collection)
        before = _chroma_ids(collection)

        _delete_source(client, layer)

        never = client.get(f"/memory/semantic/{collection}/0000000000000000")
        for chunk in ids:
            _same_reply(client.get(f"/memory/semantic/{collection}/{chunk}"), never)
        for entry in (ids[0], ids[2]):
            whole = client.get(
                f"/memory/semantic/{collection}/{entry}",
                params={"whole_document": "true"},
            )
            _same_reply(whole, never)
        assert _chroma_ids(collection) == before  # ruling 5: the rows stay

    @pytest.mark.parametrize(("layer", "collection"), FOLDS[:2], ids=FOLD_IDS[:2])
    def test_admin_still_reads_the_fold_with_every_chunk_tombstoned(
        self, client: TestClient, layer: str, collection: str
    ) -> None:
        ids = _fold_source(client, layer, collection)
        _delete_source(client, layer)

        with as_admin(client):
            whole = read_whole(client, collection, SOURCES[layer][0])
            singles = [
                client.get(f"/memory/semantic/{collection}/{chunk}") for chunk in ids
            ]

        assert whole.status_code == 200
        assert CANARY in whole.json()["content"]
        assert whole.json()["chunk_count"] == len(ids)
        assert [s.status_code for s in singles] == [200] * len(ids)
        assert all(s.json()["manifest"]["deleted_at_ms"] is not None for s in singles)

    def test_recall_no_longer_returns_the_deleted_source(
        self, client: TestClient
    ) -> None:
        _fold_source(client, "episodic", "decisions")
        before = _tool(client, "recall_decisions", {"query": CANARY}, sid="recall-1")
        assert before["payload"]["total"] >= 1

        _delete_source(client, "episodic")

        after = _tool(client, "recall_decisions", {"query": CANARY}, sid="recall-2")
        assert after["payload"]["total"] == 0
        assert after["payload"]["matches"] == []


class TestRefusedFileLayerReplyIsByteEqual:
    """AC-19 (gate S4) for R1 / R2: raw bytes and headers, not parsed JSON
    (R3 / R4 use the same comparison in every test of this file)."""

    @pytest.mark.parametrize("layer", sorted(SOURCES))
    def test_refused_read_equals_the_never_existed_read(
        self, client: TestClient, layer: str
    ) -> None:
        name = SOURCES[layer][0]
        created = client.post(
            f"/memory/{layer}", json={"filename": name, "content": f"{CANARY}\n"}
        )
        assert created.status_code == 200, created.text
        _delete_source(client, layer)

        back = client.get(f"/memory/{layer}/{name}")
        never = client.get(f"/memory/{layer}/never-{name}")

        _same_reply(back, never)
        assert back.json() == {"detail": SOURCES[layer][3]}


class TestTheCascadeIsAudited:
    """AC-23: every tombstone the delete wrote is explained by a ``delete``
    audit row (what explains the later 404s, gate ruling on B5)."""

    @staticmethod
    def _deletes() -> list[dict[str, Any]]:
        return [d for d in memory_audit_details() if d.get("op") == "delete"]

    def test_source_delete_writes_one_row_per_fold_document(
        self, client: TestClient
    ) -> None:
        ids = _fold_source(client, "episodic", "decisions")
        before = len(self._deletes())

        _delete_source(client, "episodic")

        rows = self._deletes()[before:]
        source = [r for r in rows if r["layer"] == "episodic"]
        fold = [r for r in rows if r["layer"] == "semantic"]
        assert len(source) == 1 and source[0]["key"] == "doc.md"
        assert len(fold) == 1 and len(rows) == 2
        assert fold[0]["collection"] == "decisions"
        assert fold[0]["key"] == ids[0]
        assert fold[0]["cascade_from"] == "episodic/doc.md"
        assert fold[0]["chunks"] == len(ids)
        assert fold[0]["hard"] is False

    def test_source_delete_without_a_fold_writes_only_its_own_row(
        self, client: TestClient
    ) -> None:
        created = client.post(
            "/memory/episodic", json={"filename": "doc.md", "content": "x\n"}
        )
        assert created.status_code == 200, created.text
        before = len(self._deletes())

        _delete_source(client, "episodic")

        rows = self._deletes()[before:]
        assert [(r["layer"], r["key"]) for r in rows] == [("episodic", "doc.md")]

    def test_chunk_delete_row_counts_the_document(self, client: TestClient) -> None:
        ids = _fold_source(client, "episodic", "decisions")
        before = len(self._deletes())

        _delete_chunk(client, "decisions", ids[1])

        rows = self._deletes()[before:]
        assert len(rows) == 1
        assert (rows[0]["layer"], rows[0]["collection"]) == ("semantic", "decisions")
        assert rows[0]["key"] == ids[1]
        assert rows[0]["chunks"] == len(ids)


class TestSourceDeleteLeavesOtherDocumentsAlone:
    """AC-12: over-blocking guards of ruling 1 (GREEN on main by design)."""

    def test_a_corpus_fold_of_the_same_name_is_not_tombstoned(
        self, client: TestClient
    ) -> None:
        text = CANARY + "\n" + distinct_body(2 * STEP)
        assert corpus_fold(client, text).status_code == 200
        with as_user(client, USER):
            created = client.post(
                "/memory/episodic", json={"filename": "doc.md", "content": "mine\n"}
            )
            assert created.status_code == 200, created.text
            assert client.delete("/memory/episodic/doc.md").status_code == 200

        with as_user(client, OTHER, extra=("memory:corpus:decisions:read",)):
            whole = read_whole(client, "decisions", "doc.md")

        assert whole.status_code == 200
        assert CANARY in whole.json()["content"]
        assert whole.json()["manifest"]["deleted_at_ms"] is None

    def test_the_other_layers_fold_of_the_same_name_is_not_tombstoned(
        self, client: TestClient
    ) -> None:
        """``semantic`` takes folds of both layers under one id per filename;
        the chunk's ``category`` says whose fold it is."""
        name = SOURCES["procedural"][0]
        _fold_source(client, "procedural", "semantic")
        created = client.post(
            "/memory/episodic", json={"filename": name, "content": "an ADR\n"}
        )
        assert created.status_code == 200, created.text

        assert client.delete(f"/memory/episodic/{name}").status_code == 200

        whole = read_whole(client, "semantic", name)
        assert whole.status_code == 200
        assert CANARY in whole.json()["content"]

    def test_another_document_in_the_same_collection_stays_readable(
        self, client: TestClient
    ) -> None:
        _fold_source(client, "episodic", "decisions")
        assert upload(client, "episodic", "kept.md", "kept body\n").status_code == 200
        assert fold(client, "episodic", "kept.md", "decisions").status_code == 200

        _delete_source(client, "episodic")

        kept = read_whole(client, "decisions", "kept.md")
        assert kept.status_code == 200
        assert kept.json()["content"] == "kept body\n"

    def test_refolding_a_recreated_source_is_readable_again(
        self, client: TestClient
    ) -> None:
        _fold_source(client, "episodic", "decisions")
        _delete_source(client, "episodic")
        assert upload(client, "episodic", "doc.md", "revived\n").status_code == 200
        assert fold(client, "episodic", "doc.md", "decisions").status_code == 200

        back = read_whole(client, "decisions", "doc.md")

        assert back.status_code == 200
        assert back.json()["content"] == "revived\n"
        assert back.json()["manifest"]["deleted_at_ms"] is None


class TestOneChunkDeleteTombstonesTheDocument:
    """AC-13 (operator ruling 2, Q1-bis). SUPERSEDES r1 AC-8."""

    @pytest.mark.parametrize("deleted", [0, 2], ids=["chunk0", "chunk2"])
    def test_every_chunk_is_404_after_one_chunk_delete(
        self, client: TestClient, deleted: int
    ) -> None:
        ids = _fold_source(client, "episodic", "decisions")
        before = _chroma_ids("decisions")

        _delete_chunk(client, "decisions", ids[deleted])

        never = client.get("/memory/semantic/decisions/0000000000000000")
        for chunk in ids:
            _same_reply(client.get(f"/memory/semantic/decisions/{chunk}"), never)
        _same_reply(read_whole(client, "decisions", "doc.md"), never)
        assert _chroma_ids("decisions") == before

    def test_the_tombstoned_set_equals_the_documents_chunk_keys(
        self, client: TestClient
    ) -> None:
        """Equality pin: what the delete tombstoned is exactly what the admin's
        whole-document read names as the document."""
        ids = _fold_source(client, "episodic", "decisions")
        assert upload(client, "episodic", "kept.md", "kept body\n").status_code == 200
        assert fold(client, "episodic", "kept.md", "decisions").status_code == 200

        _delete_chunk(client, "decisions", ids[1])

        with as_admin(client):
            whole = read_whole(client, "decisions", "doc.md")
            listing = client.get(
                "/memory/semantic",
                params={
                    "collection": "decisions",
                    "granularity": "chunk",
                    "include_deleted": "true",
                    "limit": 200,
                },
            )
        assert whole.status_code == 200 and listing.status_code == 200
        tombstoned = {
            i["key"] for i in listing.json()["items"] if i["deleted_at_ms"] is not None
        }
        assert tombstoned == set(whole.json()["chunk_keys"])
        assert len(tombstoned) == len(ids)

    def test_another_document_and_a_direct_write_stay_readable(
        self, client: TestClient
    ) -> None:
        ids = _fold_source(client, "episodic", "decisions")
        assert upload(client, "episodic", "kept.md", "kept body\n").status_code == 200
        assert fold(client, "episodic", "kept.md", "decisions").status_code == 200
        for doc_id in ("a-1", "a-2"):
            created = client.post(
                "/memory/semantic",
                json={
                    "collection": "semantic",
                    "document_id": doc_id,
                    "text": f"direct {doc_id}",
                    "title": "same-title",
                },
            )
            assert created.status_code == 200, created.text

        _delete_chunk(client, "decisions", ids[0])
        _delete_chunk(client, "semantic", "a-1")

        assert read_whole(client, "decisions", "kept.md").status_code == 200
        assert client.get("/memory/semantic/semantic/a-2").status_code == 200


class TestPartialTombstoneFromBeforeTheBuild:
    """AC-14: a document with ONE tombstoned chunk row (what a delete wrote
    before this build; arranged on the manifest double, asserted over HTTP) is
    refused on every direct read, single or whole."""

    @pytest.mark.parametrize("tombstoned", [0, 2], ids=["chunk0", "chunk2"])
    def test_single_and_whole_reads_are_404(
        self, client: TestClient, tombstoned: int
    ) -> None:
        ids = _fold_source(client, "episodic", "decisions")
        key = m._semantic_key("decisions", ids[tombstoned])
        asyncio.run(manifest().record_delete("semantic", key, USER))

        never = client.get("/memory/semantic/decisions/0000000000000000")
        for chunk in ids:
            _same_reply(client.get(f"/memory/semantic/decisions/{chunk}"), never)
        for entry in ids[:3]:
            whole = client.get(
                f"/memory/semantic/decisions/{entry}",
                params={"whole_document": "true"},
            )
            _same_reply(whole, never)

    def test_unstamped_document_whole_read_is_404(self, client: TestClient) -> None:
        """A fold from before the stamp: nothing in a chunk says where the
        document ends, so the WHOLE read finds the tombstone after its walk."""
        text = CANARY + "\n" + distinct_body(4 * STEP)
        count = fold_before_the_stamp("decisions", "old.md", text)
        ids = [m._doc_id("decisions", "old.md", i) for i in range(count)]
        key = m._semantic_key("decisions", ids[2])
        asyncio.run(
            manifest().record_create(
                layer="semantic", key=key, title="old.md", size_bytes=1, user_id=USER
            )
        )
        live = read_whole(client, "decisions", "old.md")
        assert live.status_code == 200 and CANARY in live.json()["content"]
        asyncio.run(manifest().record_delete("semantic", key, USER))

        never = client.get("/memory/semantic/decisions/0000000000000000")
        for entry in (ids[0], ids[2]):
            whole = client.get(
                f"/memory/semantic/decisions/{entry}",
                params={"whole_document": "true"},
            )
            _same_reply(whole, never)

    def test_admin_reads_it(self, client: TestClient) -> None:
        ids = _fold_source(client, "episodic", "decisions")
        key = m._semantic_key("decisions", ids[2])
        asyncio.run(manifest().record_delete("semantic", key, USER))

        with as_admin(client):
            whole = read_whole(client, "decisions", "doc.md")
            single = client.get(f"/memory/semantic/decisions/{ids[1]}")

        assert (whole.status_code, single.status_code) == (200, 200)
        assert CANARY in whole.json()["content"]


class TestReadToolsAreNotCached:
    """AC-15 (operator ruling 6, gate B2): ``read_decision`` / ``read_skill``
    are ``cacheable=False``; a read after the delete, same session, is not_found."""

    @pytest.mark.parametrize("layer", sorted(SOURCES))
    def test_same_session_read_after_delete_is_not_found(
        self, client: TestClient, layer: str
    ) -> None:
        name, _col, tool, _detail = SOURCES[layer]
        created = client.post(
            f"/memory/{layer}", json={"filename": name, "content": f"{CANARY}\n"}
        )
        assert created.status_code == 200, created.text
        before = _tool(client, tool, {"file": name})
        assert CANARY in before["raw"] and before["isError"] is False

        _delete_source(client, layer)

        after = _tool(client, tool, {"file": name})
        never = _tool(client, tool, {"file": f"never-{name}"})
        assert after["isError"] is True
        assert after["payload"]["error"] == "not_found"
        assert CANARY not in after["raw"]
        swapped = json.loads(
            json.dumps(never["payload"]).replace(f"never-{name}", name)
        )
        assert after["payload"] == swapped

    @pytest.mark.parametrize("layer", sorted(SOURCES))
    def test_a_read_leaves_nothing_in_the_cache(
        self, client: TestClient, tool_cache: Any, layer: str
    ) -> None:
        name, _col, tool, _detail = SOURCES[layer]
        created = client.post(
            f"/memory/{layer}", json={"filename": name, "content": f"{CANARY}\n"}
        )
        assert created.status_code == 200, created.text

        assert _tool(client, tool, {"file": name})["isError"] is False

        registered = get_tool_by_name(tool)
        assert registered is not None and registered.cacheable is False
        assert tool_cache.keys("*") == []

    def test_refused_and_never_existed_reads_persist_the_same_interaction(
        self, client: TestClient
    ) -> None:
        """Gate r1 S4 for R5/R6: status, failure_class and the stored answer."""
        for layer in sorted(SOURCES):
            name, _col, tool, _detail = SOURCES[layer]
            created = client.post(
                f"/memory/{layer}", json={"filename": name, "content": f"{CANARY}\n"}
            )
            assert created.status_code == 200, created.text
            _delete_source(client, layer)
            start = len(_mcp_rows())

            _tool(client, tool, {"file": name})
            _tool(client, tool, {"file": f"never-{name}"})

            refused, never = _mcp_rows()[start:]
            assert refused[:2] == never[:2]
            assert refused[2] == never[2].replace(f"never-{name}", name)
            assert CANARY not in refused[2]


class TestTombstoneAnswersBeforeTheCorpusScope:
    """AC-16 (operator ruling 4, gate S1)."""

    def test_tombstoned_corpus_document_is_404_without_the_corpus_scope(
        self, client: TestClient
    ) -> None:
        text = CANARY + "\n" + distinct_body(2 * STEP)
        assert corpus_fold(client, text).status_code == 200
        chunk0 = m._doc_id("decisions", "doc.md", 0)
        assert client.delete(f"/memory/semantic/decisions/{chunk0}").status_code == 200

        with as_user(client, OTHER):
            never = client.get("/memory/semantic/decisions/0000000000000000")
            _same_reply(client.get(f"/memory/semantic/decisions/{chunk0}"), never)
            _same_reply(read_whole(client, "decisions", "doc.md"), never)

    def test_live_corpus_document_is_still_403_without_the_corpus_scope(
        self, client: TestClient
    ) -> None:
        text = CANARY + "\n" + distinct_body(2 * STEP)
        assert corpus_fold(client, text).status_code == 200
        chunk0 = m._doc_id("decisions", "doc.md", 0)

        with as_user(client, OTHER):
            single = client.get(f"/memory/semantic/decisions/{chunk0}")
            whole = read_whole(client, "decisions", "doc.md")

        assert (single.status_code, whole.status_code) == (403, 403)
        assert CANARY not in single.text + whole.text


class ManifestDownError(RuntimeError):
    pass


async def _down(*_args: Any, **_kwargs: Any) -> Any:
    raise ManifestDownError("manifest unavailable")


def _take_manifest_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every READ-side manifest lookup raises (whatever name the build uses)."""
    target = manifest()
    for name in ("resolve_read", "resolve_read_many", "get_deleted_keys", "get"):
        monkeypatch.setattr(target, name, _down, raising=False)


class TestManifestFailureFailsClosed:
    """AC-17 (gate S2): when the tombstone lookup raises, no content goes out."""

    @pytest.mark.parametrize("layer", sorted(SOURCES))
    def test_file_layer_read_is_500_without_content(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, layer: str
    ) -> None:
        name = SOURCES[layer][0]
        created = client.post(
            f"/memory/{layer}", json={"filename": name, "content": f"{CANARY}\n"}
        )
        assert created.status_code == 200, created.text
        _take_manifest_down(monkeypatch)
        quiet = TestClient(client.app, raise_server_exceptions=False)

        back = quiet.get(f"/memory/{layer}/{name}")

        assert back.status_code == 500
        assert CANARY not in back.text

    def test_semantic_reads_are_500_without_content(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ids = _fold_source(client, "episodic", "decisions")
        _take_manifest_down(monkeypatch)
        quiet = TestClient(client.app, raise_server_exceptions=False)

        single = quiet.get(f"/memory/semantic/decisions/{ids[0]}")
        whole = quiet.get(
            f"/memory/semantic/decisions/{ids[0]}", params={"whole_document": "true"}
        )

        assert (single.status_code, whole.status_code) == (500, 500)
        assert CANARY not in single.text + whole.text

    @pytest.mark.parametrize("layer", sorted(SOURCES))
    def test_read_tool_is_an_error_result_without_content(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, layer: str
    ) -> None:
        name, _col, tool, _detail = SOURCES[layer]
        created = client.post(
            f"/memory/{layer}", json={"filename": name, "content": f"{CANARY}\n"}
        )
        assert created.status_code == 200, created.text
        _take_manifest_down(monkeypatch)

        back = _tool(client, tool, {"file": name})

        assert back["isError"] is True
        assert back["payload"] == {"error": "ManifestDownError"}
        assert CANARY not in back["raw"]

    def test_context_drops_the_layer_without_content(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created = client.post(
            "/memory/episodic",
            json={"filename": "doc.md", "content": f"{CANARY} body\n"},
        )
        assert created.status_code == 200, created.text
        _take_manifest_down(monkeypatch)

        with as_user(client, USER, extra=("audittrace:context",)):
            ctx = client.post("/context", json={"query": CANARY, "project": "p"})

        assert ctx.status_code == 200
        assert CANARY not in ctx.json()["context_string"]
        assert ctx.json()["layer_stats"]["episodic"] == 0


class TestAnotherOwnersTombstoneDoesNotHideMine:
    """AC-18 (gate S3): the direct-read mirror of ``TestD1jOneTombstoneCandidate``
    (one tombstone per row, decided by the caller's own resolution)."""

    def test_semantic_single_and_whole_reads_stay_200(self, client: TestClient) -> None:
        body = {"collection": "semantic", "document_id": "x", "title": "x"}
        with as_user(client, OTHER):
            theirs = client.post("/memory/semantic", json={**body, "text": "theirs"})
            assert theirs.status_code == 200, theirs.text
            assert client.delete("/memory/semantic/semantic/x").status_code == 200
        mine = client.post("/memory/semantic", json={**body, "text": f"{CANARY} mine"})
        assert mine.status_code == 200, mine.text

        single = client.get("/memory/semantic/semantic/x")
        whole = client.get(
            "/memory/semantic/semantic/x", params={"whole_document": "true"}
        )

        assert (single.status_code, whole.status_code) == (200, 200)
        assert CANARY in single.json()["content"]
        assert single.json()["manifest"]["deleted_at_ms"] is None

    def test_file_layer_read_and_tool_stay_live(self, client: TestClient) -> None:
        for layer in sorted(SOURCES):
            name, _col, tool, _detail = SOURCES[layer]
            body = {"filename": name, "content": "theirs\n"}
            with as_user(client, OTHER):
                assert client.post(f"/memory/{layer}", json=body).status_code == 200
                assert client.delete(f"/memory/{layer}/{name}").status_code == 200
            mine = client.post(
                f"/memory/{layer}", json={"filename": name, "content": f"{CANARY}\n"}
            )
            assert mine.status_code == 200, mine.text

            assert client.get(f"/memory/{layer}/{name}").status_code == 200
            assert _tool(client, tool, {"file": name})["isError"] is False


class TestRefusedReadsLeaveNoReadAuditRow:
    """AC-20 (gate S5): r1 AC-5 extended to R2, R3 and R4."""

    def test_procedural_read(self, client: TestClient) -> None:
        name = SOURCES["procedural"][0]
        created = client.post(
            "/memory/procedural", json={"filename": name, "content": f"{CANARY}\n"}
        )
        assert created.status_code == 200, created.text
        _delete_source(client, "procedural")
        before = len(_read_rows())

        assert client.get(f"/memory/procedural/{name}").status_code == 404

        assert len(_read_rows()) == before

    @pytest.mark.parametrize("whole", [False, True], ids=["single", "whole"])
    def test_semantic_read(self, client: TestClient, whole: bool) -> None:
        ids = _fold_source(client, "episodic", "decisions")
        _delete_chunk(client, "decisions", ids[0])
        params = {"whole_document": "true"} if whole else {}
        before = len(_read_rows())

        back = client.get(f"/memory/semantic/decisions/{ids[1]}", params=params)

        assert back.status_code == 404
        assert len(_read_rows()) == before


class TestContextExcludesSoftDeleted:
    """AC-21 (operator ruling 3, Q3): ``POST /context`` carries nothing of a
    soft-deleted document, for any caller (recall semantics: admins included)."""

    @pytest.mark.parametrize("admin", [False, True], ids=["owner", "admin"])
    def test_deleted_episodic_document_is_not_in_the_context(
        self, client: TestClient, admin: bool
    ) -> None:
        _fold_source(client, "episodic", "decisions")
        query = {"query": CANARY, "project": "p"}
        with as_user(client, USER, extra=("audittrace:context",)):
            live = client.post("/context", json=query)
        assert live.status_code == 200
        assert CANARY in live.json()["context_string"]
        assert live.json()["layer_stats"]["episodic"] == 1

        _delete_source(client, "episodic")

        extra = (
            ("audittrace:context", "audittrace:admin")
            if admin
            else ("audittrace:context",)
        )
        with as_user(client, USER, extra=extra):
            gone = client.post("/context", json=query)
        assert gone.status_code == 200
        assert CANARY not in gone.json()["context_string"]
        assert "doc.md" not in gone.json()["context_string"]
        assert gone.json()["layer_stats"]["episodic"] == 0
        assert gone.json()["layer_stats"]["semantic"] == 0

    def test_deleted_skill_is_not_in_the_context(self, client: TestClient) -> None:
        name = SOURCES["procedural"][0]
        created = client.post(
            "/memory/procedural",
            json={"filename": name, "content": f"{CANARY} skill body\n"},
        )
        assert created.status_code == 200, created.text
        query = {"query": CANARY, "project": "p"}
        with as_user(client, USER, extra=("audittrace:context",)):
            live = client.post("/context", json=query)
            assert live.json()["layer_stats"]["procedural"] == 1
            _delete_source(client, "procedural")
            gone = client.post("/context", json=query)

        assert gone.status_code == 200
        assert name not in gone.json()["context_string"]
        assert gone.json()["layer_stats"]["procedural"] == 0

    def test_a_live_document_stays_in_the_context(self, client: TestClient) -> None:
        for name in ("doc.md", "kept.md"):
            created = client.post(
                "/memory/episodic",
                json={"filename": name, "content": f"{CANARY} {name} body\n"},
            )
            assert created.status_code == 200, created.text
        assert client.delete("/memory/episodic/doc.md").status_code == 200

        with as_user(client, USER, extra=("audittrace:context",)):
            ctx = client.post("/context", json={"query": CANARY, "project": "p"})

        assert ctx.json()["layer_stats"]["episodic"] == 1
        assert "kept.md body" in ctx.json()["context_string"]


class TestListingHidesTheTombstonedDocument:
    """AC-22 (operator ruling 6, Q2): a soft-deleted chunk does not come back
    in the listing under its physical collection key."""

    def _list(self, client: TestClient, **params: Any) -> dict[str, Any]:
        listing = client.get(
            "/memory/semantic",
            params={
                "collection": "decisions",
                "granularity": "chunk",
                "limit": 200,
                **params,
            },
        )
        assert listing.status_code == 200, listing.text
        body: dict[str, Any] = listing.json()
        return body

    def test_no_row_of_the_deleted_document_is_listed(self, client: TestClient) -> None:
        ids = _fold_source(client, "episodic", "decisions")
        live = self._list(client)
        assert live["total_chunks"] == len(ids)
        assert all(i["key"].startswith("decisions/") for i in live["items"])

        _delete_chunk(client, "decisions", ids[0])

        after = self._list(client)
        assert after["total_chunks"] == 0
        assert after["items"] == []

    def test_include_deleted_lists_each_row_once_under_its_logical_key(
        self, client: TestClient
    ) -> None:
        ids = _fold_source(client, "episodic", "decisions")
        _delete_chunk(client, "decisions", ids[0])

        with as_admin(client):
            everything = self._list(client, include_deleted="true")

        keys = [i["key"] for i in everything["items"]]
        assert sorted(keys) == sorted(m._semantic_key("decisions", i) for i in ids)
        assert all(i["deleted_at_ms"] is not None for i in everything["items"])
