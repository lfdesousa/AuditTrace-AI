"""SOFTDELETE-DIRECT-READ ADDENDUM-2: the gate r2 conditions C1-C6, as tests.

Same harness as ``tests/test_softdelete_direct_read_a1.py``: every assertion
goes through the real HTTP routes of ``create_app()`` (or the real ``/mcp``),
statuses asserted EXACTLY, a refused reply compared raw with a never-existed
one, the tool-result cache pinned to fakeredis.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pymupdf
import pytest
from fastapi.testclient import TestClient

from audittrace.routes import memory as m
from audittrace.tools import MEMORY_TOOL_REGISTRY, get_tool_by_name
from tests import test_softdelete_direct_read_a1 as a1
from tests.memory_truncation_support import (
    USER,
    _object_store,
    distinct_body,
    fold,
    read_whole,
    upload,
)
from tests.memory_truncation_support_a1 import (
    STEP,
    as_admin,
    hard_delete_chunk,
    manifest,
    memory_audit_details,
)
from tests.test_softdelete_direct_read_a1 import (
    CANARY,
    SOURCES,
    _chroma_ids,
    _delete_source,
    _fold_source,
    _same_reply,
    _take_manifest_down,
    _tool,
    as_user,
)

# The ADDENDUM-1 fixtures, shared (same doubles, same pinned fakeredis cache).
client = a1.client
tool_cache = a1.tool_cache
_real_read_tools = a1._real_read_tools

CONTEXT = ("audittrace:context",)
QUERY = {"query": CANARY, "project": "p"}
# tool -> (layer of the source, fold target the tool recalls from)
RECALL_TOOLS = {
    "recall_decisions": ("episodic", "decisions"),
    "recall_skills": ("procedural", "skills"),
    "recall_semantic": ("episodic", "semantic"),
}
ADMIN_REFOLD_409 = {
    "detail": "source is soft-deleted: re-create it before indexing it again"
}


def _refold(
    client: TestClient, layer: str, name: str, collection: str, stored: bytes | None
) -> Any:
    """``POST /memory/index?file=`` for the caller's private key of *name*;
    ``stored`` is what object storage holds there (``None``: no such object)."""
    key = f"{USER}/{layer}/{name}"
    store = _object_store({} if stored is None else {key: stored})
    with patch.object(m, "_get_minio_client", return_value=store):
        return client.post(
            "/memory/index", params={"file": key, "collections": collection}
        )


def _stored(client: TestClient, layer: str) -> bytes:
    """The bytes the layer holds for the source (a soft delete keeps them)."""
    back = client.get(f"/memory/{layer}/{SOURCES[layer][0]}")
    assert back.status_code == 200, back.text
    content: str = back.json()["content"]
    return content.encode("utf-8")


def _index_writes() -> list[dict[str, Any]]:
    return [
        d
        for d in memory_audit_details()
        if d.get("op") == "write" and d.get("mode") == "single_file"
    ]


class TestHardDeleteOfOneChunkTombstonesTheDocument:
    """AC-24 (C1): ruling 2 has no ``?hard=true`` exemption: the delete row
    counts the whole document. The #466 contract
    (an incomplete document is an explicit 409) survives for the reader who
    may still see the document: the admin."""

    @pytest.mark.parametrize("gone", [0, 4], ids=["chunk0", "chunk4"])
    def test_owner_gets_the_never_existed_404_admin_keeps_the_409(
        self, client: TestClient, gone: int
    ) -> None:
        assert (
            upload(client, "episodic", "holed.md", distinct_body(10 * STEP)).status_code
            == 200
        )
        index = fold(client, "episodic", "holed.md", "decisions")
        assert index.status_code == 200, index.text
        total: int = index.json()["collections"]["decisions"]
        ids = [m._doc_id("decisions", "holed.md", i) for i in range(total)]
        entry = ids[3]

        hard_delete_chunk(client, "decisions", "holed.md", gone)

        # The owner's 404 below also follows from B10 with ONE tombstoned row,
        # so the document-wide tombstone is pinned by the delete row's count.
        row = [d for d in memory_audit_details() if d.get("op") == "delete"][-1]
        assert (row["layer"], row["key"], row["hard"]) == ("semantic", ids[gone], True)
        assert row.get("chunks") == total
        with as_admin(client):
            admin = client.get(
                f"/memory/semantic/decisions/{entry}", params={"whole_document": "true"}
            )
        assert admin.status_code == 409
        assert admin.json() == {
            "detail": f"semantic document incomplete: {gone} of {total} chunks readable"
        }
        never = client.get("/memory/semantic/decisions/0000000000000000")
        owner_whole = client.get(
            f"/memory/semantic/decisions/{entry}", params={"whole_document": "true"}
        )
        _same_reply(owner_whole, never)
        for chunk in ids:
            if chunk != ids[gone]:
                _same_reply(client.get(f"/memory/semantic/decisions/{chunk}"), never)


class TestContextSemanticLayerFailsClosed:
    """AC-25 (C2): ``/context``'s semantic layer carries nothing when the
    tombstone lookup cannot be made. Recall itself stays fail-open (B12)."""

    def test_folded_then_deleted_source_is_not_served_during_an_outage(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fold_source(client, "episodic", "decisions")
        with as_user(client, USER, extra=CONTEXT):
            live = client.post("/context", json=QUERY)
        assert CANARY in live.json()["context_string"]
        assert live.json()["layer_stats"]["semantic"] >= 1
        _delete_source(client, "episodic")
        _take_manifest_down(monkeypatch)

        with as_user(client, USER, extra=CONTEXT):
            down = client.post("/context", json=QUERY)

        assert down.status_code == 200
        assert CANARY not in down.json()["context_string"]
        assert "doc.md" not in down.json()["context_string"]
        assert down.json()["layer_stats"]["semantic"] == 0
        assert down.json()["layer_stats"]["episodic"] == 0

    def test_a_live_fold_is_dropped_too_during_an_outage(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed means the layer goes, whatever the rows would have said."""
        _fold_source(client, "episodic", "decisions")
        _take_manifest_down(monkeypatch)

        with as_user(client, USER, extra=CONTEXT):
            down = client.post("/context", json=QUERY)

        assert down.status_code == 200
        assert CANARY not in down.json()["context_string"]
        assert down.json()["layer_stats"]["semantic"] == 0

    def test_a_live_fold_stays_in_the_context_when_the_manifest_answers(
        self, client: TestClient
    ) -> None:
        """Over-blocking guard (GREEN on main by design)."""
        ids = _fold_source(client, "episodic", "decisions")

        with as_user(client, USER, extra=CONTEXT):
            live = client.post("/context", json=QUERY)

        assert live.status_code == 200
        assert CANARY in live.json()["context_string"]
        assert live.json()["layer_stats"]["semantic"] == min(4, len(ids))


class TestPdfDocumentsCannotBeTombstoned:
    """AC-26 (C3): the PREMISE of leaving the ``_index_pdf_objects`` folds out
    of B8. No route can soft-delete a PDF source or a PDF chunk, so no PDF
    document can be tombstoned and still readable. GREEN on main by design: if
    either becomes deletable, this reddens and B8 must cover the PDF folds."""

    PAPERS = "ai_research_papers"

    @staticmethod
    def _pdf() -> bytes:
        with pymupdf.open() as doc:
            doc.new_page().insert_text((72, 72), f"{CANARY} pdf body")
            raw: bytes = doc.tobytes()
        return raw

    def _index(self, client: TestClient, key: str) -> Any:
        store = _object_store({key: self._pdf()})
        with patch.object(m, "_get_minio_client", return_value=store):
            return client.post(
                "/memory/index", params={"file": key, "collections": self.PAPERS}
            )

    @pytest.mark.parametrize("admin", [False, True], ids=["owner", "admin"])
    def test_a_pdf_source_and_its_chunks_have_no_soft_delete(
        self, client: TestClient, admin: bool
    ) -> None:
        key = "episodic/paper.pdf" if admin else f"{USER}/episodic/paper.pdf"
        source_key = "paper.pdf" if admin else key
        chunk = m._doc_id(self.PAPERS, f"{source_key}:p1", 0)

        def requests() -> tuple[Any, Any, Any, Any]:
            return (
                self._index(client, key),
                client.delete("/memory/episodic/paper.pdf"),
                client.delete(f"/memory/semantic/{self.PAPERS}/{chunk}"),
                client.get(f"/memory/semantic/{self.PAPERS}/{chunk}"),
            )

        if admin:
            with as_admin(client):
                index, source, soft, read = requests()
        else:
            index, source, soft, read = requests()

        assert index.status_code == 200, index.text
        assert index.json()["collections"] == {self.PAPERS: 1}
        assert source.status_code == 400
        assert source.json() == {
            "detail": "filename must end with .md (got 'paper.pdf')"
        }
        assert soft.status_code == 404
        assert soft.json() == {
            "detail": (
                f"no manifest row for layer='semantic' key='{self.PAPERS}/{chunk}'"
            )
        }
        assert read.status_code == 200 and CANARY in read.text
        rows = asyncio.run(manifest().list_for_layer("semantic", include_deleted=True))
        assert rows == []


class TestCascadeRowsCarryTheSourceTier:
    """AC-27 (C4): a cascade ``delete`` row says the tier of the rows it
    explains, which B8 makes the source row's tier."""

    def test_private_source_cascade_row_is_private(self, client: TestClient) -> None:
        _fold_source(client, "episodic", "decisions")
        before = len(memory_audit_details())

        deleted = client.delete("/memory/episodic/doc.md")

        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["tier"] == "private"
        cascade = [d for d in memory_audit_details()[before:] if d.get("cascade_from")]
        assert len(cascade) == 1
        assert cascade[0]["op"] == "delete"
        assert cascade[0]["tier"] == deleted.json()["tier"]


class TestRefoldOfATombstonedSourceIsRefused:
    """AC-28 (C5, OQ-1 + the admin ruling): nobody re-folds a soft-deleted
    source. The undelete path is re-creating the source (A1 AC-12 guard)."""

    @pytest.mark.parametrize("layer", sorted(SOURCES))
    def test_owner_refold_equals_the_refold_of_a_key_that_never_existed(
        self, client: TestClient, layer: str
    ) -> None:
        name, collection, _tool_name, _detail = SOURCES[layer]
        ghost = "z" * (len(name) - 3) + ".md"  # same length: equal content-length
        ids = _fold_source(client, layer, collection)
        stored = _stored(client, layer)
        _delete_source(client, layer)
        chroma_before = _chroma_ids(collection)
        writes_before = len(_index_writes())

        back = _refold(client, layer, name, collection, stored)
        never = _refold(client, layer, ghost, collection, None)

        assert back.status_code == never.status_code == 422
        assert back.content == never.content.replace(ghost.encode(), name.encode())
        assert sorted(back.headers) == sorted(never.headers)
        for header in ("content-type", "content-length"):
            assert back.headers[header] == never.headers[header]
        assert CANARY.encode() not in back.content
        # the attempt is audited exactly like the never-existed one
        mine, theirs = _index_writes()[writes_before:]
        assert {**theirs, "key": mine["key"]} == mine and mine["total_chunks"] == 0
        # nothing was written or revived
        assert _chroma_ids(collection) == chroma_before
        gone = client.get(f"/memory/semantic/{collection}/0000000000000000")
        for chunk in ids:
            _same_reply(client.get(f"/memory/semantic/{collection}/{chunk}"), gone)
        _same_reply(read_whole(client, collection, name), gone)

    @pytest.mark.parametrize("layer", sorted(SOURCES))
    def test_admin_refold_is_an_explicit_409_and_resurrects_nothing(
        self, client: TestClient, layer: str
    ) -> None:
        """Gate r2 probe G2: on main the admin re-fold answers 200 and the
        OWNER reads the deleted text again."""
        name, collection, _tool_name, _detail = SOURCES[layer]
        ids = _fold_source(client, layer, collection)
        stored = _stored(client, layer)
        _delete_source(client, layer)
        chroma_before = _chroma_ids(collection)

        with as_admin(client):
            back = _refold(client, layer, name, collection, stored)

        assert back.status_code == 409
        assert back.json() == ADMIN_REFOLD_409
        assert _chroma_ids(collection) == chroma_before
        gone = client.get(f"/memory/semantic/{collection}/0000000000000000")
        for chunk in ids:
            _same_reply(client.get(f"/memory/semantic/{collection}/{chunk}"), gone)
        _same_reply(read_whole(client, collection, name), gone)

    def test_refold_of_a_live_source_still_indexes(self, client: TestClient) -> None:
        """Over-blocking guard (GREEN on main by design)."""
        ids = _fold_source(client, "episodic", "decisions")
        stored = _stored(client, "episodic")

        again = _refold(client, "episodic", "doc.md", "decisions", stored)

        assert again.status_code == 200, again.text
        assert again.json()["collections"]["decisions"] == len(ids)
        assert read_whole(client, "decisions", "doc.md").status_code == 200


class TestRecallToolsAreNotCached:
    """AC-29 (C6, OQ-2 ruled (i)): the recall tools that serve manifest-tracked
    content are ``cacheable=False``, like ``read_decision`` / ``read_skill``."""

    def test_the_only_cacheable_memory_tool_is_recall_recent_sessions(self) -> None:
        """Equality pin: a new recall tool is uncached unless this set says so."""
        cacheable = {t.name for t in MEMORY_TOOL_REGISTRY.values() if t.cacheable}
        assert cacheable == {"recall_recent_sessions"}

    @pytest.mark.parametrize("tool", sorted(RECALL_TOOLS))
    def test_a_recall_leaves_nothing_in_the_cache(
        self, client: TestClient, tool_cache: Any, tool: str
    ) -> None:
        layer, collection = RECALL_TOOLS[tool]
        _fold_source(client, layer, collection)

        found = _tool(client, tool, {"query": CANARY})

        assert found["isError"] is False and found["payload"]["total"] >= 1
        registered = get_tool_by_name(tool)
        assert registered is not None and registered.cacheable is False
        assert tool_cache.keys("*") == []

    @pytest.mark.parametrize("tool", sorted(RECALL_TOOLS))
    def test_no_session_recalls_the_source_after_its_delete(
        self, client: TestClient, tool: str
    ) -> None:
        """The same session, and a second session that recalled BEFORE the
        delete (the cache id carries the session: gate r2 S6)."""
        layer, collection = RECALL_TOOLS[tool]
        _fold_source(client, layer, collection)
        for sid in ("recall-session-1", "recall-session-2"):
            before = _tool(client, tool, {"query": CANARY}, sid=sid)
            assert before["payload"]["total"] >= 1

        _delete_source(client, layer)

        for sid in ("recall-session-1", "recall-session-2"):
            after = _tool(client, tool, {"query": CANARY}, sid=sid)
            assert after["isError"] is False
            assert after["payload"]["total"] == 0
            assert after["payload"]["matches"] == []
            assert CANARY not in after["raw"]
