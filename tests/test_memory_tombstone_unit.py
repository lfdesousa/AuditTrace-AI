"""SOFTDELETE-DIRECT-READ: the service layer, on BOTH manifest backends (the Mock
twin and ``MemoryManifestService`` over aiosqlite), plus the pure helpers of
``services/memory_tombstone.py``.

* ``resolve_read_many`` answers every key as ``resolve_read`` does (ADDENDUM-1
  A4 "not covered").
* ``record_delete_many`` is ONE transaction: the set is tombstoned whole or not
  at all, and a corpus row needs the shared-write authorization.
* the helpers: the predicate, the id sets (stamped, unstamped walk, untitled,
  malformed), the strict ``/context`` filters.

Mutation-tested by ``neuters.py`` of the build record (each guard below has a
neuter that reddens it).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from langchain_core.documents import Document
from sqlalchemy import select

from audittrace.db.models import MemoryItem
from audittrace.identity import UserContext
from audittrace.services import memory_tombstone as tomb
from audittrace.services.memory_chunk_ids import (
    STAMP_FOLD,
    STAMP_TOTAL,
    chunk_id,
    document_chunk_ids,
    read_stamp,
)
from audittrace.services.memory_keys import semantic_logical_key
from audittrace.services.memory_manifest import (
    ManifestAuthorizationError,
    MemoryManifestService,
    MockMemoryManifestService,
)
from audittrace.services.semantic_document_incomplete_error import (
    SemanticDocumentIncompleteError,
)

A, B = "user-a", "user-b"


def _user(
    sub: str, *, admin: bool = False, scopes: tuple[str, ...] = ()
) -> UserContext:
    return UserContext(
        user_id=sub, username=sub, agent_type="test", scopes=scopes, is_admin=admin
    )


@dataclass
class Env:
    svc: Any
    kind: str

    async def seed(
        self,
        key: str,
        owner: str,
        *,
        layer: str = "semantic",
        tier: str = "private",
        deleted: bool = False,
    ) -> None:
        """A row AT the exact stored ``key`` (owner key, legacy bare, corpus)."""
        now = int(time.time() * 1000)
        fields: dict[str, Any] = {
            "layer": layer,
            "key": key,
            "title": None,
            "size_bytes": 1,
            "created_at_ms": now,
            "modified_at_ms": now,
            "created_by_user_id": owner,
            "modified_by_user_id": owner,
            "deleted_at_ms": now if deleted else None,
            "deleted_by_user_id": owner if deleted else None,
            "tier": tier,
        }
        if self.kind == "mock":
            self.svc._rows[(layer, key)] = {"id": str(uuid.uuid4()), **fields}
            return
        async with self.svc._session_factory() as session:
            session.add(MemoryItem(**fields))
            await session.commit()

    async def stored_deleted(self, key: str, layer: str = "semantic") -> bool:
        """Is the row stored at exactly ``key`` tombstoned?"""
        if self.kind == "mock":
            return self.svc._rows[(layer, key)].get("deleted_at_ms") is not None
        async with self.svc._session_factory() as session:
            row = (
                await session.scalars(
                    select(MemoryItem).filter_by(layer=layer, key=key)
                )
            ).one()
            return row.deleted_at_ms is not None


@pytest_asyncio.fixture(params=["mock", "sql"])
async def env(request: pytest.FixtureRequest) -> Env:
    if request.param == "mock":
        return Env(MockMemoryManifestService(), "mock")
    from audittrace.db.postgres import InMemoryPostgresFactory

    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    return Env(MemoryManifestService(factory.get_session_factory()), "sql")


class TestResolveReadManyEqualsResolveRead:
    async def test_every_key_resolves_as_the_single_lookup_does(self, env: Env) -> None:
        await env.seed(f"{A}/decisions/own", A)  # the caller's owner key
        await env.seed("decisions/legacy", A)  # the caller's own legacy row
        await env.seed("decisions/corpus", B, tier="corpus")  # a corpus row
        await env.seed("decisions/theirs", B)  # another user's legacy row
        await env.seed(f"{B}/decisions/foreign", B)  # another user's owner key
        await env.seed(f"{A}/decisions/dead", A, deleted=True)  # own tombstone
        keys = [
            "decisions/own",
            "decisions/legacy",
            "decisions/corpus",
            "decisions/theirs",
            "decisions/foreign",
            "decisions/dead",
            "decisions/absent",
        ]
        user = _user(A)

        many = await env.svc.resolve_read_many("semantic", keys, user)

        assert list(many) == keys
        for key in keys:
            single = await env.svc.resolve_read("semantic", key, user)
            assert many[key] == single, key
        assert many["decisions/dead"] is not None
        assert many["decisions/dead"].deleted_at_ms is not None
        assert many["decisions/theirs"] is None and many["decisions/absent"] is None

    async def test_no_keys_is_an_empty_answer(self, env: Env) -> None:
        assert await env.svc.resolve_read_many("semantic", [], _user(A)) == {}

    async def test_an_invalid_layer_is_refused(self, env: Env) -> None:
        with pytest.raises(ValueError, match="Invalid memory layer"):
            await env.svc.resolve_read_many("conversational", ["k"], _user(A))

    async def test_a_malformed_sub_reads_nothing_it_does_not_raise(
        self, env: Env
    ) -> None:
        """A read never raises on a malformed sub (``_read_row``'s contract): it
        just cannot see an owner key."""
        await env.seed("decisions/legacy", "bad/sub")
        user = _user("bad/sub")
        many = await env.svc.resolve_read_many("semantic", ["decisions/legacy"], user)
        assert many["decisions/legacy"] is not None
        assert many["decisions/legacy"] == await env.svc.resolve_read(
            "semantic", "decisions/legacy", user
        )


class TestRecordDeleteManyIsOneTransaction:
    async def test_the_set_is_tombstoned_and_the_rest_is_not(self, env: Env) -> None:
        for n in range(3):
            await env.seed(f"{A}/decisions/c{n}", A)
        await env.seed(f"{A}/decisions/other", A)
        user = _user(A)

        await env.svc.record_delete_many(
            "semantic",
            ["decisions/c0", "decisions/c1", "decisions/c2", "decisions/absent"],
            A,
        )

        assert [await env.stored_deleted(f"{A}/decisions/c{n}") for n in range(3)] == [
            True
        ] * 3
        assert await env.stored_deleted(f"{A}/decisions/other") is False
        listed = await env.svc.resolve_read_many(
            "semantic", ["decisions/c1", "decisions/other"], user
        )
        assert listed["decisions/c1"].deleted_by_user_id == A

    async def test_an_already_deleted_row_keeps_its_first_tombstone(
        self, env: Env
    ) -> None:
        await env.seed(f"{A}/decisions/dead", A, deleted=True)
        before = (
            await env.svc.resolve_read("semantic", "decisions/dead", _user(A))
        ).deleted_at_ms

        await env.svc.record_delete_many("semantic", ["decisions/dead"], A)

        after = (
            await env.svc.resolve_read("semantic", "decisions/dead", _user(A))
        ).deleted_at_ms
        assert after == before

    async def test_a_corpus_row_without_authorization_changes_nothing(
        self, env: Env
    ) -> None:
        """Whole or not at all: the unauthorized row is found BEFORE any row of
        the set is touched."""
        await env.seed(f"{A}/decisions/mine", A)
        await env.seed("decisions/shared", B, tier="corpus")

        with pytest.raises(ManifestAuthorizationError):
            await env.svc.record_delete_many(
                "semantic", ["decisions/mine", "decisions/shared"], A
            )

        assert await env.stored_deleted(f"{A}/decisions/mine") is False
        assert await env.stored_deleted("decisions/shared") is False

    async def test_a_corpus_row_with_authorization_is_tombstoned(
        self, env: Env
    ) -> None:
        await env.seed("decisions/shared", B, tier="corpus")

        await env.svc.record_delete_many(
            "semantic", ["decisions/shared"], A, caller_can_write_shared=True
        )

        assert await env.stored_deleted("decisions/shared") is True

    async def test_no_keys_writes_nothing(self, env: Env) -> None:
        await env.svc.record_delete_many("semantic", [], A)

    async def test_an_invalid_layer_is_refused(self, env: Env) -> None:
        with pytest.raises(ValueError, match="Invalid memory layer"):
            await env.svc.record_delete_many("conversational", ["k"], A)


class TestThePredicate:
    def test_who_reads_a_tombstone(self) -> None:
        assert tomb.reads_tombstones(_user(A, admin=True)) is True
        assert tomb.reads_tombstones(_user(A, scopes=("audittrace:admin",))) is True
        assert (
            tomb.reads_tombstones(_user(A, scopes=("memory:episodic:read",))) is False
        )

    async def test_a_tombstone_is_hidden_from_the_caller_who_may_not_read_it(
        self, env: Env
    ) -> None:
        await env.seed(f"{A}/gone.md", A, layer="episodic", deleted=True)
        await env.seed(f"{A}/live.md", A, layer="episodic")
        gone = await env.svc.resolve_read("episodic", "gone.md", _user(A))
        live = await env.svc.resolve_read("episodic", "live.md", _user(A))

        assert tomb.is_hidden(gone, _user(A)) is True
        assert tomb.is_hidden(gone, _user(A, admin=True)) is False
        assert tomb.is_hidden(live, _user(A)) is False
        assert tomb.is_hidden(None, _user(A)) is False

    async def test_any_hidden_asks_one_question_for_the_whole_set(
        self, env: Env
    ) -> None:
        await env.seed(f"{A}/decisions/c0", A)
        await env.seed(f"{A}/decisions/c1", A, deleted=True)

        assert await tomb.any_hidden(
            env.svc, "semantic", ["decisions/c0", "decisions/c1"], _user(A)
        )
        assert not await tomb.any_hidden(
            env.svc, "semantic", ["decisions/c0", "decisions/absent"], _user(A)
        )
        assert not await tomb.any_hidden(
            env.svc, "semantic", ["decisions/c0", "decisions/c1"], _user(A, admin=True)
        )


def _doc(metadata: dict[str, Any]) -> Document:
    return Document(page_content="x", metadata=metadata)


STAMP = {STAMP_TOTAL: 3, STAMP_FOLD: "f" * 64}


class TestChunkIds:
    def test_a_stamped_chunk_names_the_whole_sequence(self) -> None:
        ids = document_chunk_ids("decisions", "t.md", 0, 3)
        assert (
            tomb.stamped_chunk_ids(
                "decisions", ids[1], _doc({"title": "t.md", **STAMP})
            )
            == ids
        )

    @pytest.mark.parametrize(
        "metadata",
        [
            {"source": "t.md"},  # unstamped
            {STAMP_TOTAL: 3, STAMP_FOLD: "f" * 64},  # stamped, untitled
            {"title": "t.md", STAMP_TOTAL: 0, STAMP_FOLD: ""},  # malformed stamp
            {"title": "t.md", STAMP_TOTAL: 3},  # half a stamp
        ],
        ids=["unstamped", "untitled", "malformed", "half-stamp"],
    )
    def test_a_row_that_names_no_sequence_names_itself(
        self, metadata: dict[str, Any]
    ) -> None:
        assert tomb.stamped_chunk_ids("decisions", "id-1", _doc(metadata)) == ["id-1"]

    def test_a_foreign_id_is_not_widened_into_the_sequence(self) -> None:
        assert tomb.stamped_chunk_ids(
            "decisions", "not-in-it", _doc({"title": "t.md", **STAMP})
        ) == ["not-in-it"]

    def test_the_stamp_reader_refuses_half_a_stamp(self) -> None:
        with pytest.raises(SemanticDocumentIncompleteError):
            read_stamp({STAMP_TOTAL: 3})
        assert read_stamp({}) is None

    def test_chunk_id_is_the_fold_formula(self) -> None:
        assert (
            chunk_id("decisions", "t.md", 2)
            == document_chunk_ids("decisions", "t.md", 2, 3)[0]
        )


class _Store:
    """A semantic service double holding the live chunk ids of one document."""

    def __init__(self, live: Sequence[str]) -> None:
        self.live = set(live)
        self.windows: list[int] = []

    async def get_documents(
        self, _user: UserContext, _collection: str, ids: list[str]
    ) -> list[Document | None]:
        self.windows.append(len(ids))
        return [_doc({}) if i in self.live else None for i in ids]


class TestTheDocumentIdSet:
    async def test_stamped_is_the_stamped_sequence_without_a_read(self) -> None:
        ids = document_chunk_ids("decisions", "t.md", 0, 3)
        store = _Store([])
        got = await tomb.document_chunk_id_set(
            store,  # type: ignore[arg-type]
            _user(A),
            "decisions",
            ids[0],
            _doc({"title": "t.md", **STAMP}),
        )
        assert got == ids and store.windows == []

    async def test_unstamped_walks_to_the_first_missing_chunk_across_windows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tomb, "CHUNK_READ_WINDOW", 2)
        ids = document_chunk_ids("decisions", "t.md", 0, 5)
        store = _Store(ids[:3])  # chunks 0-2 live; the walk reads windows of 2
        got = await tomb.document_chunk_id_set(
            store,  # type: ignore[arg-type]
            _user(A),
            "decisions",
            ids[1],
            _doc({"title": "t.md"}),
        )
        assert got == ids[:3]
        assert store.windows == [2, 2]

    async def test_an_unstamped_row_outside_the_walk_names_itself(self) -> None:
        store = _Store([])
        got = await tomb.document_chunk_id_set(
            store,  # type: ignore[arg-type]
            _user(A),
            "decisions",
            "stray",
            _doc({"title": "t.md"}),
        )
        assert got == ["stray"]

    @pytest.mark.parametrize(
        "doc",
        [None, _doc({"x": 1}), _doc({"title": "t.md", STAMP_TOTAL: 2})],
        ids=["no-row", "untitled", "malformed-stamp"],
    )
    async def test_no_sequence_is_the_row_itself(self, doc: Document | None) -> None:
        got = await tomb.document_chunk_id_set(
            _Store([]),  # type: ignore[arg-type]
            _user(A),
            "decisions",
            "id-1",
            doc,
        )
        assert got == ["id-1"]


class _Semantic(_Store):
    """Adds ``get_document``: the first chunk of ``docs`` by id."""

    def __init__(self, docs: dict[str, Document]) -> None:
        super().__init__(list(docs))
        self.docs = docs

    async def get_document(
        self, _user: UserContext, _collection: str, document_id: str
    ) -> Document | None:
        return self.docs.get(document_id)


class TestTheFoldCascade:
    """B8: only THIS source's fold, at THIS source row's tier and owner."""

    async def _fold(
        self, env: Env, *, category: str, source: str, owner: str = A
    ) -> tuple[_Semantic, str, list[str]]:
        ids = document_chunk_ids("decisions", "doc.md", 0, 3)
        metadata = {
            "category": category,
            "source": source,
            "title": "doc.md",
            **STAMP,
        }
        for cid in ids:
            await env.seed(f"{owner}/{semantic_logical_key('decisions', cid)}", owner)
        return _Semantic({ids[0]: _doc(metadata)}), ids[0], ids

    async def _source(self, env: Env) -> Any:
        await env.seed(f"{A}/doc.md", A, layer="episodic")
        return await env.svc.resolve_read("episodic", "doc.md", _user(A))

    async def test_the_fold_of_the_source_is_tombstoned(self, env: Env) -> None:
        store, chunk0, ids = await self._fold(env, category="episodic", source="doc.md")
        source = await self._source(env)

        touched = await tomb.tombstone_fold_copies(
            store,  # type: ignore[arg-type]
            env.svc,
            _user(A),
            "episodic",
            "doc.md",
            source,
            caller_can_write_shared=False,
        )

        assert touched == [("decisions", chunk0, 3)]
        for cid in ids:
            assert await env.stored_deleted(
                f"{A}/{semantic_logical_key('decisions', cid)}"
            )

    @pytest.mark.parametrize(
        ("category", "source"),
        [("procedural", "doc.md"), ("episodic", "other.md")],
        ids=["other-layer", "other-source"],
    )
    async def test_a_chunk_that_is_not_this_sources_fold_is_left_alone(
        self, env: Env, category: str, source: str
    ) -> None:
        store, _chunk0, ids = await self._fold(env, category=category, source=source)
        src = await self._source(env)

        touched = await tomb.tombstone_fold_copies(
            store,  # type: ignore[arg-type]
            env.svc,
            _user(A),
            "episodic",
            "doc.md",
            src,
            caller_can_write_shared=False,
        )

        assert touched == []
        assert not await env.stored_deleted(
            f"{A}/{semantic_logical_key('decisions', ids[0])}"
        )

    async def test_no_fold_is_nothing_to_tombstone(self, env: Env) -> None:
        src = await self._source(env)
        touched = await tomb.tombstone_fold_copies(
            _Semantic({}),  # type: ignore[arg-type]
            env.svc,
            _user(A),
            "episodic",
            "doc.md",
            src,
            caller_can_write_shared=False,
        )
        assert touched == []

    async def test_a_fold_without_manifest_rows_tombstones_no_row(
        self, env: Env
    ) -> None:
        ids = document_chunk_ids("decisions", "doc.md", 0, 3)
        metadata = {
            "category": "episodic",
            "source": "doc.md",
            "title": "doc.md",
            **STAMP,
        }
        src = await self._source(env)

        touched = await tomb.tombstone_fold_copies(
            _Semantic({ids[0]: _doc(metadata)}),  # type: ignore[arg-type]
            env.svc,
            _user(A),
            "episodic",
            "doc.md",
            src,
            caller_can_write_shared=False,
        )

        assert touched == []


class TestTheSourceGuard:
    async def test_private_reads_the_callers_own_row(self, env: Env) -> None:
        await env.seed(f"{A}/x.md", A, layer="episodic", deleted=True)
        await env.seed(f"{A}/y.md", A, layer="episodic")
        assert await tomb.source_is_tombstoned(
            env.svc, _user(A), "episodic", "x.md", "private"
        )
        assert not await tomb.source_is_tombstoned(
            env.svc, _user(A), "episodic", "y.md", "private"
        )
        assert not await tomb.source_is_tombstoned(
            env.svc, _user(A), "episodic", "z.md", "private"
        )

    async def test_corpus_reads_the_bare_row_at_the_corpus_tier_only(
        self, env: Env
    ) -> None:
        await env.seed("x.md", B, layer="episodic", tier="corpus", deleted=True)
        await env.seed("y.md", B, layer="episodic", tier="private", deleted=True)
        assert await tomb.source_is_tombstoned(
            env.svc, _user(A), "episodic", "x.md", "corpus"
        )
        # a private row at the bare key is not a corpus source
        assert not await tomb.source_is_tombstoned(
            env.svc, _user(A), "episodic", "y.md", "corpus"
        )


class _Manifest:
    """The two calls ``/context`` makes of the manifest, scripted."""

    def __init__(self, deleted_files: set[str], deleted_keys: set[str]) -> None:
        self.deleted_files = deleted_files
        self.deleted_keys = deleted_keys
        self.asked: list[Any] = []

    async def resolve_read_many(
        self, _layer: str, keys: Sequence[str], _user: UserContext
    ) -> dict[str, Any]:
        self.asked.append(list(keys))
        return {
            k: type(
                "E", (), {"deleted_at_ms": 1 if k in self.deleted_files else None}
            )()
            for k in keys
        }

    async def get_deleted_keys(self, _layer: str, keys: Any) -> set[str]:
        self.asked.append(set(keys))
        return self.deleted_keys & set(keys)


class TestTheContextFilters:
    async def test_files_drop_tombstones_for_every_caller_and_keep_unnamed_docs(
        self,
    ) -> None:
        docs = [
            _doc({"file": "gone.md"}),
            _doc({"file": "live.md"}),
            _doc({"title": "no file key"}),
        ]
        manifest = _Manifest({"gone.md"}, set())
        for admin in (False, True):
            kept = await tomb.without_tombstoned_files(
                manifest,  # type: ignore[arg-type]
                "episodic",
                docs,
                _user(A, admin=admin),
            )
            assert [d.metadata.get("file") for d in kept] == ["live.md", None]
        assert manifest.asked[0] == ["gone.md", "live.md"]  # ONE round trip

    async def test_chunks_use_recalls_derivation_and_one_lookup(self) -> None:
        gone = _doc({"collection": "decisions", "chunk_id": "c1", "user_id": A})
        live = _doc({"collection": "decisions", "chunk_id": "c2", "user_id": A})
        candidates = tomb.tombstone_candidates(gone)
        manifest = _Manifest(set(), set(candidates))

        kept = await tomb.without_tombstoned_chunks(manifest, [gone, live])  # type: ignore[arg-type]

        assert kept == [live]
        assert len(manifest.asked) == 1

    async def test_no_chunks_ask_nothing(self) -> None:
        manifest = _Manifest(set(), set())
        assert await tomb.without_tombstoned_chunks(manifest, []) == []  # type: ignore[arg-type]
        assert manifest.asked == []

    async def test_a_failing_lookup_is_not_swallowed(self) -> None:
        class _Down:
            async def get_deleted_keys(self, *_a: Any) -> set[str]:
                raise RuntimeError("manifest down")

        doc = _doc({"collection": "decisions", "chunk_id": "c1", "user_id": A})
        with pytest.raises(RuntimeError, match="manifest down"):
            await tomb.without_tombstoned_chunks(_Down(), [doc])  # type: ignore[arg-type]
