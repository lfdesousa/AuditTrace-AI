"""ADR-047 — embedding on the dedicated nomic-embed-server (#318).

Post-WS7 end state: the in-process ONNX path is gone. Embedding ALWAYS runs
on the nomic server, collections are opened with ``embedding_function=None``,
and vectors are supplied explicitly (``query_embeddings`` on read,
``embeddings`` on write). There is no flag and no ``SINGLETON_EMBEDDER``.

Covers:
* ``embed_via_nomic`` — the batched OpenAI-shaped embed call.
* ``ChromaSemanticService`` — query_embeddings on read, explicit embeddings on
  write, _v2 collection names, ``embedding_function=None``.
* ``routes/memory.py`` ``_upsert_in_batches`` — the index choke-point both the
  markdown and PDF paths flow through; always embeds on nomic.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from audittrace.services import embedder
from audittrace.services.embedder import EmbeddingServerError, embed_via_nomic
from audittrace.services.semantic import ChromaSemanticService

EMBED_URL = "http://embed.test/v1"


# ── fake async httpx client (dependency-free) ────────────────────────────


class _FakeResp:
    def __init__(self, payload: Any, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=httpx.Request("POST", EMBED_URL), response=None
            )

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    """Returns queued responses (or raises queued exceptions) in order."""

    def __init__(self, responses: list[Any]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, json: dict[str, Any]) -> _FakeResp:
        self.calls.append({"url": url, "json": json})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, _FakeResp) else _FakeResp(item)


def _embedding_payload(vectors: list[list[float]]) -> dict[str, Any]:
    return {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}


# ── embed_via_nomic ──────────────────────────────────────────────────────


class TestEmbedViaNomic:
    async def test_happy_path_returns_vectors_in_order(self):
        client = _FakeClient([_embedding_payload([[0.1, 0.2], [0.3, 0.4]])])
        vectors = await embed_via_nomic(["a", "b"], embed_url=EMBED_URL, client=client)
        assert vectors == [[0.1, 0.2], [0.3, 0.4]]
        assert client.calls[0]["url"] == "http://embed.test/v1/embeddings"
        assert client.calls[0]["json"]["input"] == ["a", "b"]

    async def test_reorders_by_response_index(self):
        payload = {
            "data": [
                {"index": 1, "embedding": [9.0]},
                {"index": 0, "embedding": [1.0]},
            ]
        }
        client = _FakeClient([payload])
        vectors = await embed_via_nomic(["x", "y"], embed_url=EMBED_URL, client=client)
        assert vectors == [[1.0], [9.0]]

    async def test_empty_input_short_circuits(self):
        client = _FakeClient([])
        assert await embed_via_nomic([], embed_url=EMBED_URL, client=client) == []
        assert client.calls == []

    async def test_vector_count_mismatch_raises(self):
        client = _FakeClient([_embedding_payload([[0.1]])])
        with pytest.raises(EmbeddingServerError):
            await embed_via_nomic(["a", "b"], embed_url=EMBED_URL, client=client)

    async def test_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(embedder.asyncio, "sleep", AsyncMock())
        client = _FakeClient([httpx.ConnectError("boom"), _embedding_payload([[0.5]])])
        vectors = await embed_via_nomic(["a"], embed_url=EMBED_URL, client=client)
        assert vectors == [[0.5]]
        assert len(client.calls) == 2

    async def test_retries_exhausted_raises(self, monkeypatch):
        monkeypatch.setattr(embedder.asyncio, "sleep", AsyncMock())
        client = _FakeClient([httpx.ConnectError("boom")] * 3)
        with pytest.raises(EmbeddingServerError):
            await embed_via_nomic(
                ["a"], embed_url=EMBED_URL, client=client, max_attempts=3
            )
        assert len(client.calls) == 3

    def test_client_singleton_is_reused(self):
        embedder._embed_client = None
        c1 = embedder._embed_client_singleton()
        c2 = embedder._embed_client_singleton()
        assert c1 is c2


# ── ChromaSemanticService — always embeds on nomic ───────────────────────


def _service(client: Any) -> ChromaSemanticService:
    return ChromaSemanticService(
        client=client, default_collections=["decisions"], embed_url=EMBED_URL
    )


class TestSemanticNomic:
    async def test_search_uses_query_embeddings_and_physical_v2(
        self, monkeypatch, user_context
    ):
        monkeypatch.setattr(
            "audittrace.services.semantic.embed_via_nomic",
            AsyncMock(return_value=[[0.1, 0.2, 0.3]]),
        )
        collection = AsyncMock()
        collection.count = AsyncMock(return_value=2)
        collection.query = AsyncMock(
            return_value={
                "ids": [["d1"]],
                "documents": [["hello"]],
                "metadatas": [[{"source": "ADR-1"}]],
            }
        )
        client = AsyncMock()
        client.get_or_create_collection = AsyncMock(return_value=collection)

        docs = await _service(client).search(user_context, "a query", k=1)

        assert len(docs) == 1
        open_kwargs = client.get_or_create_collection.call_args.kwargs
        assert open_kwargs["name"] == "decisions_v2"
        assert open_kwargs["embedding_function"] is None
        query_kwargs = collection.query.call_args.kwargs
        assert "query_embeddings" in query_kwargs
        assert "query_texts" not in query_kwargs

    async def test_upsert_supplies_explicit_embeddings(self, monkeypatch, user_context):
        monkeypatch.setattr(
            "audittrace.services.semantic.embed_via_nomic",
            AsyncMock(return_value=[[0.9, 0.8]]),
        )
        collection = AsyncMock()
        client = AsyncMock()
        client.get_or_create_collection = AsyncMock(return_value=collection)

        await _service(client).upsert(user_context, "decisions", "doc-1", "text", {})

        assert (
            client.get_or_create_collection.call_args.kwargs["name"] == "decisions_v2"
        )
        assert (
            client.get_or_create_collection.call_args.kwargs["embedding_function"]
            is None
        )
        assert collection.upsert.call_args.kwargs["embeddings"] == [[0.9, 0.8]]

    async def test_get_and_delete_open_with_no_ef(self, user_context):
        collection = AsyncMock()
        collection.get = AsyncMock(return_value={"ids": []})
        client = AsyncMock()
        client.get_or_create_collection = AsyncMock(return_value=collection)
        svc = _service(client)

        await svc.get_document(user_context, "skills", "x")
        assert client.get_or_create_collection.call_args.kwargs["name"] == "skills_v2"
        assert (
            client.get_or_create_collection.call_args.kwargs["embedding_function"]
            is None
        )
        assert await svc.delete_document(user_context, "skills", "x") is False


# ── routes/memory.py — _upsert_in_batches choke-point ────────────────────


class TestUpsertInBatches:
    async def test_embeds_on_nomic_and_supplies_vectors(self, monkeypatch):
        from audittrace.routes import memory

        monkeypatch.setattr(
            memory, "embed_via_nomic", AsyncMock(return_value=[[0.1], [0.2]])
        )
        collection = AsyncMock()
        await memory._upsert_in_batches(
            collection, ids=["a", "b"], documents=["t1", "t2"], metadatas=[{}, {}]
        )
        upsert_kwargs = collection.upsert.call_args.kwargs
        assert upsert_kwargs["embeddings"] == [[0.1], [0.2]]
        assert "embedding_function" not in upsert_kwargs
