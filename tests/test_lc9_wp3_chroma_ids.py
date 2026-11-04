"""LC-9 WP3 -- the ChromaDB side of cross-user private-row isolation.

Everything goes through the REAL HTTP routes (``TestClient(create_app())``, two
subs through the repo's JWT-patch pattern, the REAL ``ChromaSemanticService`` on
the in-repo fake ChromaDB client) and runs the manifest half on BOTH backends
(the Mock twin and ``MemoryManifestService`` over aiosqlite).

RT -> class map (spec ``2026-10-26-SPEC-lc9-cross-user-private-row-isolation``
+ ADDENDUM-1 C3/C4/S2/S5 + brief ``2026-11-04-BRIEF-lc9-wp3-chroma-dispatch``):

* RT-1 content half   ``TestRt1ContentHalf``      (A reads OWN text after B wrote the same id)
* RT-7                ``TestRt7FoldSameFilename``  (two subs fold the same ``x.md``)
* RT-8                ``TestRt8McpSameDocumentId`` (MCP ``write_decision``, two subs)
* RT-11               ``TestRt11OneItemPerDocument`` (new, legacy, promoted: one item each)
* RT-19               ``TestRt19SoftDeleteExcludesRecall`` (C3 reader #5)
* RT-23               ``TestRt23AlwaysPrepend``    (``foo`` and ``user-a__foo`` are two docs)
* batched read        ``TestBatchedWholeDocumentRead`` (ONE ``get`` for N chunks)
* dual lookup         ``TestLegacyChunkDualLookup`` (owner id, then the caller's OWN legacy id)
* S2 step 3           ``TestCopyForwardDropsLegacyChunk``

Neuters (each ALONE, restored with ``git checkout``) are listed in the build
record's RT -> test -> neuter map.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from audittrace import dependencies
from audittrace.db.models import MemoryItem
from audittrace.identity import UserContext
from audittrace.routes.memory import _chunk_text, _doc_id
from audittrace.services.memory_manifest import MemoryManifestService

A, B = "user-a", "user-b"
BACKENDS = ("mock", "sql")
BASE = "memory:semantic:write memory:semantic:read"
FOLD = (
    "memory:episodic:write memory:episodic:read memory:semantic:read "
    "memory:semantic:write"
)


class _Auth:
    """Three patches bundled: a valid JWT for *sub* holding *scope*."""

    def __init__(self, sub: str, scope: str) -> None:
        self.sub, self.scope = sub, scope

    def __enter__(self) -> _Auth:
        self._patches = [
            patch("audittrace.auth.get_settings"),
            patch("audittrace.auth._get_jwks_keys"),
            patch("audittrace.auth._decode_jwt_with_allowed_issuers"),
        ]
        settings, jwks, decode = (p.__enter__() for p in self._patches)
        settings.return_value = MagicMock(auth_enabled=True, auth_required=True)
        jwks.return_value = ["fake-key"]
        decode.return_value = {"sub": self.sub, "scope": self.scope}
        return self

    def __exit__(self, *exc: Any) -> None:
        for p in reversed(self._patches):
            p.__exit__(*exc)


def _ctx(sub: str) -> UserContext:
    return UserContext(
        user_id=sub, username=sub, agent_type="test", scopes=(), is_admin=False
    )


class Rig:
    """A TestClient plus the manifest, the real semantic service and the raw
    fake ChromaDB client behind it."""

    def __init__(self, client: TestClient, container: Any, kind: str, chroma: Any):
        self.client, self.kind, self.chroma = client, kind, chroma
        self.manifest = container._instances["memory_manifest"]
        self.semantic = container._instances["semantic"]

    def call(
        self, method: str, url: str, sub: str, scope: str = BASE, **kw: Any
    ) -> Any:
        with _Auth(sub, scope):
            return self.client.request(
                method, url, headers={"Authorization": "Bearer t"}, **kw
            )

    def create(
        self, sub: str, doc: str, text: str = "body", *, collection: str = "decisions"
    ) -> Any:
        body = {"collection": collection, "document_id": doc, "text": text}
        return self.call("POST", "/memory/semantic", sub, json=body)

    def update(
        self, sub: str, doc: str, text: str = "upd", *, collection: str = "decisions"
    ) -> Any:
        return self.call(
            "PUT", f"/memory/semantic/{collection}/{doc}", sub, json={"text": text}
        )

    def delete(self, sub: str, doc: str, *, collection: str = "decisions") -> Any:
        return self.call("DELETE", f"/memory/semantic/{collection}/{doc}", sub)

    def get(
        self, sub: str, doc: str, *, collection: str = "decisions", whole: bool = False
    ) -> Any:
        url = f"/memory/semantic/{collection}/{doc}"
        params = {"whole_document": "true"} if whole else None
        return self.call("GET", url, sub, params=params)

    def chunk_items(self, sub: str, collection: str = "decisions") -> list[Any]:
        r = self.call(
            "GET",
            "/memory/semantic",
            sub,
            params={"collection": collection, "granularity": "chunk"},
        )
        assert r.status_code == 200, r.text
        return list(r.json()["items"])

    def raw(self, collection: str = "decisions") -> dict[str, dict[str, Any]]:
        """Every raw ChromaDB row of the private physical collection by id."""
        col = asyncio.run(
            self.chroma.get_or_create_collection(
                name=f"{collection}_v2", embedding_function=None
            )
        )
        return {r["id"]: r for r in col.data}

    def raw_col(self, collection: str = "decisions") -> Any:
        return asyncio.run(
            self.chroma.get_or_create_collection(
                name=f"{collection}_v2", embedding_function=None
            )
        )

    def seed_raw_chunk(
        self, bare_id: str, owner: str, text: str, collection: str = "decisions"
    ) -> None:
        """A pre-LC-9 ChromaDB chunk: a BARE id, owner in the metadata."""
        col = self.raw_col(collection)
        asyncio.run(
            col.upsert(
                ids=[bare_id],
                documents=[text],
                metadatas=[{"user_id": owner, "tier": "private"}],
            )
        )

    def seed_legacy_row(
        self, doc: str, owner: str, *, collection: str = "decisions"
    ) -> None:
        """A pre-LC-9 bare-key private MANIFEST row (no Chroma chunk)."""
        key = f"{collection}/{doc}"
        now = int(time.time() * 1000) - 5000
        cols = dict(
            layer="semantic",
            key=key,
            title="legacy",
            size_bytes=3,
            created_at_ms=now,
            modified_at_ms=now,
            created_by_user_id=owner,
            modified_by_user_id=owner,
            tier="private",
        )
        if self.kind == "mock":
            self.manifest._rows[("semantic", key)] = {
                "id": str(uuid.uuid4()),
                **cols,
                "deleted_at_ms": None,
                "deleted_by_user_id": None,
            }
            return

        async def _add() -> None:
            async with self.manifest._session_factory() as s:
                s.add(MemoryItem(**cols))
                await s.commit()

        asyncio.run(_add())

    def seed_legacy(self, doc: str, owner: str, text: str = "legacy body") -> None:
        """Legacy manifest row AND legacy Chroma chunk, both at the bare key."""
        self.seed_legacy_row(doc, owner)
        self.seed_raw_chunk(doc, owner, text)


@pytest.fixture(params=BACKENDS)
def rig(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Rig]:
    from audittrace.db.factory import MockChromaDBFactory
    from audittrace.services.semantic import ChromaSemanticService

    embed = AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts])
    monkeypatch.setattr("audittrace.services.semantic.embed_via_nomic", embed)
    monkeypatch.setattr("audittrace.routes.memory.embed_via_nomic", embed)
    previous = dependencies.container
    container = dependencies.create_test_container()
    if request.param == "sql":
        from audittrace.db.postgres import InMemoryPostgresFactory

        container._instances["memory_manifest"] = MemoryManifestService(
            InMemoryPostgresFactory().get_session_factory()
        )
    client = asyncio.run(MockChromaDBFactory().get_client())
    container._instances["chromadb"] = client
    container._instances["semantic"] = ChromaSemanticService(
        client=client,
        default_collections=["decisions", "skills", "semantic"],
        manifest=container._instances["memory_manifest"],
    )
    dependencies.container = container
    from audittrace.server import create_app

    with TestClient(create_app()) as c:
        yield Rig(c, container, request.param, client)
    dependencies.container = previous


def _fake_minio(contents: dict[str, bytes]) -> MagicMock:
    def get_object(_bucket: str, key: str) -> MagicMock:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = contents[key]
        return response

    fake = MagicMock()
    fake.get_object.side_effect = get_object
    return fake


def fold(rig: Rig, sub: str, filename: str, content: bytes) -> None:
    """Real ``POST /memory/upload`` -> ``POST /memory/index`` for *sub*."""
    with patch("audittrace.routes.memory._get_minio_client", return_value=MagicMock()):
        up = rig.call(
            "POST",
            "/memory/upload",
            sub,
            FOLD,
            params={"layer": "episodic", "filename": filename},
            files={"file": (filename, content, "text/markdown")},
        )
    assert up.status_code == 200, up.text
    key = up.json()["key"]
    with patch(
        "audittrace.routes.memory._get_minio_client",
        return_value=_fake_minio({key: content}),
    ):
        ix = rig.call(
            "POST",
            "/memory/index",
            sub,
            FOLD,
            params={"file": key, "collections": "decisions"},
        )
    assert ix.status_code == 200, ix.text


def _body(tag: str, entries: int = 220) -> str:
    """A multi-chunk document whose every line names *tag* and its index."""
    text = "".join(f"[{tag}-{i:05d}]\n" for i in range(entries))
    assert len(_chunk_text(text)) > 1
    return text


@pytest.fixture
def registries() -> Iterator[None]:
    """Re-register the memory tools and the MCP write tools for this test."""
    import importlib

    import audittrace.tools.memory_handlers as mh
    from audittrace.tools import mcp_write_handlers as wh
    from audittrace.tools import reset_registry_for_tests
    from audittrace.tools.mcp_write_registry import (
        reset_mcp_write_registry_for_tests,
    )

    reset_registry_for_tests()
    importlib.reload(mh)
    reset_mcp_write_registry_for_tests()
    importlib.reload(wh)
    yield
    reset_registry_for_tests()
    reset_mcp_write_registry_for_tests()


# ── RT-1 content half ───────────────────────────────────────────────────────


class TestRt1ContentHalf:
    """A reads its OWN text after B wrote the same ``document_id``. Neuter:
    ``private_physical_id`` returns the bare id -> one shared Chroma row -> RED."""

    def test_a_reads_own_text_after_b_writes_the_same_id(self, rig: Rig) -> None:
        assert rig.create(A, "x", "A-body").status_code == 200
        assert rig.create(B, "x", "B-body").status_code == 200
        ga, gb = rig.get(A, "x"), rig.get(B, "x")
        assert ga.status_code == gb.status_code == 200
        assert ga.json()["content"] == "A-body"
        assert gb.json()["content"] == "B-body"
        assert ga.json()["metadata"]["user_id"] == A

    def test_two_chroma_rows_each_owner_prefixed(self, rig: Rig) -> None:
        rig.create(A, "x", "A-body")
        rig.create(B, "x", "B-body")
        raw = rig.raw()
        assert set(raw) == {f"{A}__x", f"{B}__x"}
        for cid, row in raw.items():
            assert cid.startswith(row["metadata"]["user_id"] + "__")  # the R4 pin

    def test_an_update_by_a_never_touches_b(self, rig: Rig) -> None:
        rig.create(A, "x", "A-body")
        rig.create(B, "x", "B-body")
        assert rig.update(A, "x", "A-v2").status_code == 200
        assert rig.get(B, "x").json()["content"] == "B-body"
        assert rig.get(A, "x").json()["content"] == "A-v2"

    def test_b_cannot_read_a_by_a_known_physical_id(self, rig: Rig) -> None:
        rig.create(A, "x", "A-body")
        assert rig.get(B, f"{A}__x").status_code == 404


# ── RT-23 ───────────────────────────────────────────────────────────────────


class TestRt23AlwaysPrepend:
    """``foo`` and ``user-a__foo`` stay TWO documents (ADDENDUM-1 S5). Neuter:
    an idempotent own-prefix rule -> one Chroma row -> RED."""

    def test_foo_and_prefixed_foo_are_two_docs(self, rig: Rig) -> None:
        assert rig.create(A, "foo", "plain").status_code == 200
        assert rig.create(A, f"{A}__foo", "prefixed").status_code == 200
        assert set(rig.raw()) == {f"{A}__foo", f"{A}__{A}__foo"}
        assert rig.get(A, "foo").json()["content"] == "plain"
        assert rig.get(A, f"{A}__foo").json()["content"] == "prefixed"


# ── RT-8 ────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("registries")
class TestRt8McpSameDocumentId:
    """The MCP ``write_decision`` tool, two subs, one ``document_id``: disjoint.
    Neuter: ``private_physical_id`` returns the bare id -> RED."""

    def _write(self, rig: Rig, sub: str, doc: str, text: str) -> Any:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "write_decision",
                "arguments": {"document_id": doc, "text": text},
            },
        }
        return rig.call("POST", "/mcp", sub, "memory:decisions:write", json=body)

    def test_two_subs_same_document_id_are_disjoint(self, rig: Rig) -> None:
        for sub, text in ((A, "A-mcp"), (B, "B-mcp")):
            r = self._write(rig, sub, "dec-1", text)
            assert r.status_code == 200 and r.json()["result"]["isError"] is False
        assert set(rig.raw()) == {f"{A}__dec-1", f"{B}__dec-1"}
        assert rig.get(A, "dec-1").json()["content"] == "A-mcp"
        assert rig.get(B, "dec-1").json()["content"] == "B-mcp"


# ── RT-7 + the batched whole-document read ─────────────────────────────────


class TestRt7FoldSameFilename:
    """Two subs upload+index the SAME ``x.md`` to ``decisions``; each reads its
    OWN chunks whole-document. Neuter: the fold writes bare ids -> the second
    fold overwrites the first (and A's chunks vanish) -> RED."""

    def test_each_sub_reads_its_own_whole_document(self, rig: Rig) -> None:
        a_text, b_text = _body("A"), _body("B")
        fold(rig, A, "x.md", a_text.encode())
        fold(rig, B, "x.md", b_text.encode())
        first = _doc_id("decisions", "x.md", 0)
        ra, rb = rig.get(A, first, whole=True), rig.get(B, first, whole=True)
        assert ra.status_code == rb.status_code == 200, (ra.text, rb.text)
        assert ra.json()["content"] == a_text
        assert rb.json()["content"] == b_text
        assert ra.json()["chunk_count"] == rb.json()["chunk_count"] > 1

    def test_chroma_holds_two_owner_prefixed_copies(self, rig: Rig) -> None:
        fold(rig, A, "x.md", _body("A").encode())
        fold(rig, B, "x.md", _body("B").encode())
        raw = rig.raw()
        assert raw
        for cid, row in raw.items():
            assert cid.startswith(row["metadata"]["user_id"] + "__")
        assert {r["metadata"]["user_id"] for r in raw.values()} == {A, B}


class TestBatchedWholeDocumentRead:
    """ONE ``get`` for the whole chunk sequence, not one per chunk (ADDENDUM-1
    S2: ``get(ids=[owner..., legacy...])``, missing ids omitted). Neuter: loop
    ``get_document`` per chunk -> hundreds of calls -> RED."""

    def test_whole_document_read_is_one_get_per_physical_collection(
        self, rig: Rig
    ) -> None:
        text = _body("A")
        fold(rig, A, "x.md", text.encode())
        first = _doc_id("decisions", "x.md", 0)
        private, corpus = rig.raw_col(), rig.raw_col("decisions_corpus")
        counts = {"private": 0, "corpus": 0}
        real_p, real_c = private.get, corpus.get

        async def count_p(*a: Any, **kw: Any) -> Any:
            counts["private"] += 1 if kw.get("ids") is not None else 0
            return await real_p(*a, **kw)

        async def count_c(*a: Any, **kw: Any) -> Any:
            counts["corpus"] += 1 if kw.get("ids") is not None else 0
            return await real_c(*a, **kw)

        with (
            patch.object(private, "get", count_p),
            patch.object(corpus, "get", count_c),
        ):
            r = rig.get(A, first, whole=True)
        assert r.status_code == 200 and r.json()["content"] == text
        # 1 id-probe to resolve the requested chunk + 1 batched sequence read
        assert counts["private"] <= 2, counts
        assert counts["corpus"] <= 2, counts

    def test_a_legacy_whole_document_is_read_for_its_owner_only(self, rig: Rig) -> None:
        """Pre-LC-9 chunks (bare ids, owner in metadata) still read whole for
        their OWNER; B reading the same ids gets nothing."""
        text = _body("L")
        chunks = _chunk_text(text)
        col = rig.raw_col()
        ids = [_doc_id("decisions", "old.md", i) for i in range(len(chunks))]
        asyncio.run(
            col.upsert(
                ids=ids,
                documents=chunks,
                metadatas=[
                    {"user_id": A, "tier": "private", "source": "old.md"} for _ in ids
                ],
            )
        )
        r = rig.get(A, ids[0], whole=True)
        assert r.status_code == 200 and r.json()["content"] == text
        assert rig.get(B, ids[0], whole=True).status_code == 404


# ── RT-11 ───────────────────────────────────────────────────────────────────


class TestRt11OneItemPerDocument:
    """A's chunk list shows exactly ONE item per document: a new one (owner-keyed
    row + prefixed chunk), a legacy one (bare row + bare chunk) and a promoted
    one (``{sub}__file`` id). Neuter: a discovered row keeps the PHYSICAL id in
    its key -> the manifest row and its own chunk list twice -> RED."""

    def test_new_legacy_and_promoted_each_listed_once(self, rig: Rig) -> None:
        assert rig.create(A, "new-1", "new body").status_code == 200
        rig.seed_legacy("legacy-1", A)
        assert rig.create(A, f"{A}__promoted.md", "promoted body").status_code == 200
        keys = sorted(i["key"].rsplit("/", 1)[-1] for i in rig.chunk_items(A))
        # the dedup identity is the trailing id: one per document, never twice
        assert keys == sorted(["new-1", "legacy-1", f"{A}__promoted.md"])

    def test_a_discovered_row_carries_the_logical_id(self, rig: Rig) -> None:
        """A chunk with no manifest row is discovered from Chroma; its key
        shows the LOGICAL id, not ``user-a__...``."""
        asyncio.run(
            rig.semantic.upsert(
                _ctx(A), "decisions", "orphan-1", "t", {}, tier="private"
            )
        )
        items = [i for i in rig.chunk_items(A) if i["discovered"]]
        assert [i["key"].rsplit("/", 1)[-1] for i in items] == ["orphan-1"]

    def test_other_users_chunk_is_not_listed(self, rig: Rig) -> None:
        rig.create(B, "b-only", "b")
        assert rig.chunk_items(A) == []


# ── RT-19 ───────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("registries")
class TestRt19SoftDeleteExcludesRecall:
    """A soft-deleted NEW-form private chunk never surfaces in recall (MCP
    ``recall_semantic``). Neuter: ``_tombstone_candidates`` tries only the
    legacy bare key -> the owner-keyed tombstone is missed -> RED."""

    def _recall(self, rig: Rig, sub: str) -> list[str]:
        from audittrace.tools import get_tool_by_name

        tool = get_tool_by_name("recall_semantic")
        assert tool is not None
        out = asyncio.run(tool.handler(_ctx(sub), {"query": "body", "k": 20}))
        return [m["snippet"] for m in out["matches"]]

    def test_soft_deleted_chunk_is_excluded_from_its_owners_recall(
        self, rig: Rig
    ) -> None:
        rig.create(A, "x", "A-body")
        assert "A-body" in self._recall(rig, A)
        assert rig.delete(A, "x").status_code == 200
        assert "A-body" not in self._recall(rig, A)

    def test_a_delete_does_not_hide_b_same_named_chunk(self, rig: Rig) -> None:
        rig.create(A, "x", "A-body")
        rig.create(B, "x", "B-body")
        assert rig.delete(A, "x").status_code == 200
        assert self._recall(rig, B) == ["B-body"]
        assert self._recall(rig, A) == []

    def test_legacy_tombstone_still_excludes_a_legacy_chunk(self, rig: Rig) -> None:
        rig.seed_legacy("old", A, "legacy-body")
        assert "legacy-body" in self._recall(rig, A)
        assert rig.delete(A, "old").status_code == 200
        assert "legacy-body" not in self._recall(rig, A)


# ── dual lookup (reads + hard delete) ──────────────────────────────────────


class TestLegacyChunkDualLookup:
    """Owner id first, then the caller's OWN legacy bare id (R3). Neuter:
    accept any legacy owner -> B reads A's legacy chunk -> RED."""

    def test_owner_reads_own_legacy_chunk(self, rig: Rig) -> None:
        rig.seed_legacy("x", A, "A-legacy")
        r = rig.get(A, "x")
        assert r.status_code == 200 and r.json()["content"] == "A-legacy"

    def test_other_user_gets_404_for_a_legacy_chunk(self, rig: Rig) -> None:
        rig.seed_legacy("x", A, "A-legacy")
        assert rig.get(B, "x").status_code == 404

    def test_owner_id_wins_over_the_legacy_id(self, rig: Rig) -> None:
        rig.seed_legacy("x", A, "A-legacy")
        asyncio.run(
            rig.semantic.upsert(_ctx(A), "decisions", "x", "A-new", {}, tier="private")
        )
        assert rig.get(A, "x").json()["content"] == "A-new"

    def test_hard_delete_removes_only_the_callers_chunk(self, rig: Rig) -> None:
        rig.create(A, "x", "A-body")
        rig.create(B, "x", "B-body")
        admin = "memory:semantic:write audittrace:admin"
        r = rig.call("DELETE", "/memory/semantic/decisions/x?hard=true", A, admin)
        assert r.status_code == 200, r.text
        assert set(rig.raw()) == {f"{B}__x"}

    def test_hard_delete_never_removes_another_users_legacy_chunk(
        self, rig: Rig
    ) -> None:
        rig.seed_raw_chunk("x", B, "B-legacy")
        deleted = asyncio.run(rig.semantic.delete_document(_ctx(A), "decisions", "x"))
        assert deleted is False
        assert "x" in rig.raw()


# ── S2 step 3 ───────────────────────────────────────────────────────────────


class TestCopyForwardDropsLegacyChunk:
    """After the manifest copy-forward the caller's OWN legacy chunk is deleted
    (one batched delete; ADDENDUM-1 S2 step 3). Neuter: skip the owner check ->
    another user's chunk at the same bare id is deleted -> RED."""

    def test_put_over_own_legacy_leaves_only_the_prefixed_chunk(self, rig: Rig) -> None:
        rig.seed_legacy("x", A, "A-legacy")
        assert rig.update(A, "x", "A-new").status_code == 200
        assert set(rig.raw()) == {f"{A}__x"}
        assert rig.get(A, "x").json()["content"] == "A-new"

    def test_create_over_own_legacy_leaves_only_the_prefixed_chunk(
        self, rig: Rig
    ) -> None:
        rig.seed_legacy("x", A, "A-legacy")
        assert rig.create(A, "x", "A-new").status_code == 200
        assert set(rig.raw()) == {f"{A}__x"}

    def test_another_users_chunk_at_the_bare_id_survives(self, rig: Rig) -> None:
        rig.seed_legacy_row("x", A)
        rig.seed_raw_chunk("x", B, "B-legacy")
        assert rig.update(A, "x", "A-new").status_code == 200
        raw = rig.raw()
        assert set(raw) == {"x", f"{A}__x"}
        assert raw["x"]["metadata"]["user_id"] == B

    def test_a_plain_new_write_drops_nothing(self, rig: Rig) -> None:
        rig.seed_raw_chunk("x", B, "B-legacy")
        assert rig.create(A, "x", "A-body").status_code == 200
        assert set(rig.raw()) == {"x", f"{A}__x"}


# ── invalid subs fail closed ───────────────────────────────────────────────


class TestInvalidSubIsRefused:
    def test_a_sub_that_breaks_the_prefix_rule_is_403_not_500(self, rig: Rig) -> None:
        r = rig.create("bad__sub", "x", "t")
        assert r.status_code == 403
        assert rig.raw() == {}
