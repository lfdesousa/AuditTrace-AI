"""LC-9 WP3 -- the batched, dual-lookup read against a REAL ChromaDB engine.

The Mock Chroma client in ``test_lc9_wp3_chroma_ids.py`` omits missing ids and
filters by id like the real one, but the spec's batched read (ADDENDUM-1 S2:
"chromadb 1.5.9 returned 2 of 4 requested ids in 1 call, missing ids omitted")
is a property of the REAL engine, so it is proven here against ``chromadb``'s
in-process ``EphemeralClient`` (no mock engine). This module is an integration
module: it holds no mocked-Chroma case.

Doubles declared: ``audittrace.services.semantic.embed_via_nomic`` is replaced
by an ``AsyncMock`` returning fixed 3-d vectors (no embedding server runs); a
thin async shim exposes the sync ``chromadb`` collection through the awaitable
surface ``ChromaSemanticService`` expects. The engine, the ids and the
metadata filtering are real.

Neuter: loop ``get`` per id in ``_fetch_by_ids`` / ``get_documents`` -> the
call-count assertion goes RED; drop the ``dict.fromkeys`` dedupe -> ChromaDB
raises ``DuplicateIDError`` -> RED.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import chromadb
import pytest
from chromadb.config import Settings

from audittrace.identity import UserContext
from audittrace.services.semantic import ChromaSemanticService

A, B = "user-a", "user-b"


def _ctx(sub: str) -> UserContext:
    return UserContext(
        user_id=sub, username=sub, agent_type="test", scopes=(), is_admin=False
    )


class _AsyncCollection:
    """Awaitable view of one real sync ``chromadb`` collection; counts ``get``."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.get_calls: list[int] = []

    async def get(self, **kw: Any) -> Any:
        self.get_calls.append(len(kw.get("ids") or []))
        return self._inner.get(**kw)

    async def upsert(self, **kw: Any) -> None:
        self._inner.upsert(**kw)

    async def delete(self, **kw: Any) -> None:
        self._inner.delete(**kw)


class _AsyncClient:
    def __init__(self) -> None:
        self._real = chromadb.EphemeralClient(
            settings=Settings(anonymized_telemetry=False)
        )
        self.cols: dict[str, _AsyncCollection] = {}

    async def get_or_create_collection(self, name: str, **_: Any) -> _AsyncCollection:
        if name not in self.cols:
            self.cols[name] = _AsyncCollection(
                self._real.get_or_create_collection(name=name, embedding_function=None)
            )
        return self.cols[name]


@pytest.fixture
def env(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ChromaSemanticService, _AsyncClient, str]:
    monkeypatch.setattr(
        "audittrace.services.semantic.embed_via_nomic",
        AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]),
    )
    client = _AsyncClient()
    # a unique logical collection so the process-wide engine never mixes tests
    collection = f"lc9wp3{uuid.uuid4().hex[:12]}"
    return ChromaSemanticService(client=client), client, collection  # type: ignore[arg-type]


async def test_missing_ids_are_omitted_and_the_read_is_one_call(env: Any) -> None:
    svc, client, coll = env
    for doc in ("c0", "c1"):
        await svc.upsert(_ctx(A), coll, doc, f"text-{doc}", {})
    ids = ["c0", "c1", "c2", "c3"]
    docs = await svc.get_documents(_ctx(A), coll, ids)
    assert [d.page_content if d else None for d in docs] == [
        "text-c0",
        "text-c1",
        None,
        None,
    ]
    private = client.cols[f"{coll}_v2"]
    # 4 logical ids -> 8 physical ids (owner + legacy) in ONE get
    assert private.get_calls == [8]


async def test_five_hundred_chunk_sequence_is_one_private_get(env: Any) -> None:
    svc, client, coll = env
    await svc.upsert(_ctx(A), coll, "c0", "first", {})
    ids = [f"c{i}" for i in range(500)]
    docs = await svc.get_documents(_ctx(A), coll, ids)
    assert docs[0] is not None and docs[1] is None
    assert client.cols[f"{coll}_v2"].get_calls == [1000]


async def test_other_users_legacy_id_is_not_returned(env: Any) -> None:
    svc, client, coll = env
    private = await client.get_or_create_collection(f"{coll}_v2")
    await private.upsert(
        ids=["x"],
        documents=["B-legacy"],
        embeddings=[[0.1, 0.2, 0.3]],
        metadatas=[{"user_id": B, "tier": "private"}],
    )
    assert await svc.get_documents(_ctx(A), coll, ["x"]) == [None]
    own = await svc.get_documents(_ctx(B), coll, ["x"])
    assert own[0] is not None and own[0].page_content == "B-legacy"


async def test_owner_id_wins_and_duplicates_are_not_sent(env: Any) -> None:
    svc, client, coll = env
    await svc.upsert(_ctx(A), coll, "x", "A-new", {})
    private = await client.get_or_create_collection(f"{coll}_v2")
    await private.upsert(
        ids=["x"],
        documents=["A-old"],
        embeddings=[[0.1, 0.2, 0.3]],
        metadatas=[{"user_id": A, "tier": "private"}],
    )
    got = await svc.get_documents(_ctx(A), coll, ["x", "x"])
    assert [g.page_content if g else None for g in got] == ["A-new", "A-new"]


async def test_drop_legacy_only_removes_the_callers_bare_chunk(env: Any) -> None:
    svc, client, coll = env
    private = await client.get_or_create_collection(f"{coll}_v2")
    await private.upsert(
        ids=["mine", "theirs"],
        documents=["m", "t"],
        embeddings=[[0.1, 0.2, 0.3]] * 2,
        metadatas=[
            {"user_id": A, "tier": "private"},
            {"user_id": B, "tier": "private"},
        ],
    )
    assert await svc.drop_legacy_documents(_ctx(A), coll, ["mine", "theirs"]) == 1
    left = await svc.get_documents(_ctx(B), coll, ["theirs"])
    assert left[0] is not None
    assert await svc.get_documents(_ctx(A), coll, ["mine"]) == [None]
    assert await svc.drop_legacy_documents(_ctx(A), coll, ["mine"]) == 0
