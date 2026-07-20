"""Tests for database factory pattern.

ADR-020: SQLiteChromaDBFactory removed. ChromaDB is server-mode only.
HTTPChromaDBFactory now supports token-based authentication.
"""

from audittrace.db.factory import (
    HTTPChromaDBFactory,
    MemoryChromaDBFactory,
    MockChromaDBFactory,
    MockCollection,
)


async def test_memory_chromadb_factory():
    """Test memory ChromaDB factory creates in-memory client."""
    factory = MemoryChromaDBFactory()
    client = await factory.get_client()
    assert client is not None


def test_http_chromadb_factory_stores_token():
    """Test HTTPChromaDBFactory accepts and stores auth token."""
    factory = HTTPChromaDBFactory(url="http://localhost:8000", token="secret-token")
    assert factory.token == "secret-token"


def test_http_chromadb_factory_token_defaults_none():
    """Test HTTPChromaDBFactory token defaults to None."""
    factory = HTTPChromaDBFactory(url="http://localhost:8000")
    assert factory.token is None


async def test_mock_chromadb_factory():
    """Test mock ChromaDB factory."""
    factory = MockChromaDBFactory()
    client = await factory.get_client()

    # Test collection operations
    collection = await client.get_or_create_collection("test")
    assert collection.name == "test"

    # Test add
    ids = await collection.add(
        ids=["id1"], documents=["doc1"], metadatas=[{"key": "value"}]
    )
    assert ids == ["id1"]

    # Test query
    results = await collection.query(query_texts=["test"])
    assert "ids" in results
    assert "documents" in results

    # Test count
    assert await collection.count() == 1

    # Test reset
    factory.reset()
    assert len(factory.collections) == 0


async def test_mock_collection():
    """Test MockCollection class."""
    collection = MockCollection("test_collection")

    # Test add
    await collection.add(ids=["id1", "id2"], documents=["doc1", "doc2"])
    assert await collection.count() == 2

    # Test query
    results = await collection.query(query_texts=["test"], n_results=2)
    assert len(results["ids"][0]) == 2
    assert results["documents"][0][0] == "doc1"

    # Test get
    results = await collection.get()
    assert len(results["ids"]) == 2


async def test_factory_call_tracking():
    """Test that factory call count is tracked."""
    factory = MockChromaDBFactory()

    await factory.get_client()
    assert factory.call_count == 1

    await factory.get_client()
    assert factory.call_count == 2


# ── HTTPChromaDBFactory URL parsing branches ─────────────────────────────────


async def test_http_factory_parses_url_with_scheme(monkeypatch):
    """HTTPChromaDBFactory.get_client must strip the scheme and split host:port."""
    from audittrace.db import factory as factory_mod

    captured: dict = {}

    async def fake_http_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(factory_mod.chromadb, "AsyncHttpClient", fake_http_client)
    f = HTTPChromaDBFactory(url="http://chroma.example.com:9000")
    await f.get_client()
    assert captured["host"] == "chroma.example.com"
    assert captured["port"] == 9000
    assert "headers" not in captured


async def test_http_factory_parses_url_without_scheme(monkeypatch):
    """Bare host:port form is also accepted."""
    from audittrace.db import factory as factory_mod

    captured: dict = {}

    async def fake_http_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(factory_mod.chromadb, "AsyncHttpClient", fake_http_client)
    f = HTTPChromaDBFactory(url="chroma:8000")
    await f.get_client()
    assert captured["host"] == "chroma"
    assert captured["port"] == 8000


async def test_http_factory_attaches_token_when_set(monkeypatch):
    """Bearer token must land in the Authorization header."""
    from audittrace.db import factory as factory_mod

    captured: dict = {}

    async def fake_http_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(factory_mod.chromadb, "AsyncHttpClient", fake_http_client)
    f = HTTPChromaDBFactory(url="http://chroma:8000", token="secret-token")
    await f.get_client()
    assert captured["headers"] == {"X-Chroma-Token": "secret-token"}


# ── MockCollection where-filter branches ─────────────────────────────────────


async def test_mock_collection_query_with_where_filter():
    """MockCollection.query with a `where` clause filters by metadata."""
    collection = MockCollection("test")
    await collection.add(
        ids=["a", "b", "c"],
        documents=["doc-a", "doc-b", "doc-c"],
        metadatas=[
            {"category": "x"},
            {"category": "y"},
            {"category": "x"},
        ],
    )
    result = await collection.query(query_texts=["q"], where={"category": "x"})
    ids = result["ids"][0]
    assert set(ids) == {"a", "c"}
    assert "b" not in ids


async def test_mock_collection_count_with_where_filter():
    """MockCollection.count with a `where` clause counts only matching rows."""
    collection = MockCollection("test")
    await collection.add(
        ids=["a", "b", "c"],
        documents=["x", "y", "z"],
        metadatas=[{"k": "1"}, {"k": "2"}, {"k": "1"}],
    )
    assert await collection.count() == 3
    assert await collection.count(where={"k": "1"}) == 2
    assert await collection.count(where={"k": "no-match"}) == 0


# ── MockCollection upsert / delete semantics ─────────────────────────────────
#
# MockCollection is the ChromaDB stand-in for the whole memory-service suite.
# If its write semantics drift from real ChromaDB, every test that exercises
# a memory layer through it validates against the wrong contract, and the
# divergence only shows up against a live Chroma.


async def test_mock_collection_upsert_replaces_only_the_matching_id():
    """Upsert must replace the row whose id matches and leave its neighbours
    untouched — including rows scanned *before* the match is found.

    Real ChromaDB upsert is per-id. A stub that replaced the first row it
    looked at (or appended a duplicate instead of replacing) would let a
    memory-layer regression that overwrites a different user's document pass
    the unit suite, since the collection state the assertions read back is
    entirely the stub's.
    """
    collection = MockCollection("test")
    await collection.add(
        ids=["a", "b", "c"],
        documents=["doc-a", "doc-b", "doc-c"],
        metadatas=[{"owner": "u1"}, {"owner": "u2"}, {"owner": "u3"}],
    )

    # "b" is not the first row, so the scan walks past a non-matching row.
    await collection.upsert(
        ids=["b"], documents=["doc-b-v2"], metadatas=[{"owner": "u2"}]
    )

    # Replaced in place: still three rows, not four.
    assert await collection.count() == 3
    rows = await collection.get()
    by_id = dict(zip(rows["ids"], rows["documents"], strict=True))
    assert by_id["b"] == "doc-b-v2"
    # Neighbours on both sides of the match survive unchanged.
    assert by_id["a"] == "doc-a"
    assert by_id["c"] == "doc-c"
    # Order is preserved — the replacement is positional, not a delete+append.
    assert rows["ids"] == ["a", "b", "c"]


async def test_mock_collection_upsert_appends_when_id_is_new():
    """An id not already present must be appended, not silently dropped."""
    collection = MockCollection("test")
    await collection.add(ids=["a"], documents=["doc-a"])

    await collection.upsert(ids=["b"], documents=["doc-b"])

    rows = await collection.get()
    assert rows["ids"] == ["a", "b"]
    assert rows["documents"] == ["doc-a", "doc-b"]


async def test_mock_collection_delete_without_ids_is_a_noop():
    """``delete()`` with no ids must delete nothing.

    Real ChromaDB treats an empty id list as "nothing to do". The dangerous
    misreading is "no filter → delete everything": a service that computes an
    empty deletion set (nothing expired, nothing to evict) would then wipe the
    whole collection, and against this stub the test suite would happily
    confirm it.
    """
    collection = MockCollection("test")
    await collection.add(ids=["a", "b"], documents=["doc-a", "doc-b"])

    await collection.delete()
    assert await collection.count() == 2

    await collection.delete(ids=[])
    assert await collection.count() == 2

    # Contrast: a populated id list does delete, and only the named row.
    await collection.delete(ids=["a"])
    assert (await collection.get())["ids"] == ["b"]


# ── HTTPChromaDBFactory startup-race retry ───────────────────────────────────
#
# Mirrors the Istio-sidecar startup race: chromadb's SDK wraps Envoy 503
# responses as ValueError("no healthy upstream") inside HttpClient.__init__.
# Pattern follows TestJWKSFetchRetry in tests/test_auth.py — same shape of
# transient-failure-with-exponential-backoff lives in audittrace.auth.


async def test_http_factory_succeeds_on_second_attempt(monkeypatch):
    """First call fails, second succeeds — must return the client without raising."""
    import pytest  # noqa: F401  (kept for parity with the auth tests)

    from audittrace.db import factory as factory_mod

    sentinel = object()
    calls: list[int] = []

    async def fake_http_client(**_kwargs):
        calls.append(len(calls) + 1)
        if len(calls) < 2:
            raise ValueError("no healthy upstream")
        return sentinel

    monkeypatch.setattr(factory_mod.chromadb, "AsyncHttpClient", fake_http_client)
    sleep_calls: list[float] = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(factory_mod.asyncio, "sleep", fake_sleep)

    f = HTTPChromaDBFactory(url="http://chroma:8000")
    client = await f.get_client()

    assert client is sentinel
    assert len(calls) == 2
    assert sleep_calls == [2.0]  # one backoff before the successful retry


async def test_http_factory_exhausts_retries_and_reraises(monkeypatch):
    """All attempts fail — last ValueError must propagate after 4 total calls."""
    import pytest

    from audittrace.db import factory as factory_mod

    calls: list[int] = []

    async def fake_http_client(**_kwargs):
        calls.append(len(calls) + 1)
        raise ValueError("no healthy upstream")

    monkeypatch.setattr(factory_mod.chromadb, "AsyncHttpClient", fake_http_client)

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(factory_mod.asyncio, "sleep", fake_sleep)

    f = HTTPChromaDBFactory(url="http://chroma:8000")
    with pytest.raises(ValueError, match="no healthy upstream"):
        await f.get_client()

    # 1 initial + _CHROMADB_CONNECT_RETRIES retries = 4 total calls
    assert len(calls) == factory_mod._CHROMADB_CONNECT_RETRIES + 1


async def test_http_factory_backoff_delays_are_exponential(monkeypatch):
    """Sleep delays must follow 2^(attempt+1): 2, 4, 8."""
    import pytest

    from audittrace.db import factory as factory_mod

    async def fake_http_client(**_kwargs):
        raise ValueError("no healthy upstream")

    monkeypatch.setattr(factory_mod.chromadb, "AsyncHttpClient", fake_http_client)
    sleep_calls: list[float] = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(factory_mod.asyncio, "sleep", fake_sleep)

    f = HTTPChromaDBFactory(url="http://chroma:8000")
    with pytest.raises(ValueError):
        await f.get_client()

    assert sleep_calls == [2.0, 4.0, 8.0]


async def test_http_factory_succeeds_immediately_without_sleep(monkeypatch):
    """Happy path — single call returns client, no backoff."""
    from audittrace.db import factory as factory_mod

    sentinel = object()

    async def fake_http_client(**_kwargs):
        return sentinel

    monkeypatch.setattr(factory_mod.chromadb, "AsyncHttpClient", fake_http_client)
    sleep_calls: list[float] = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(factory_mod.asyncio, "sleep", fake_sleep)

    f = HTTPChromaDBFactory(url="http://chroma:8000")
    client = await f.get_client()

    assert client is sentinel
    assert sleep_calls == []


async def test_http_factory_retries_on_connection_error(monkeypatch):
    """ConnectionError (defensive catch alongside ValueError) must also retry."""
    import pytest  # noqa: F401

    from audittrace.db import factory as factory_mod

    sentinel = object()
    calls: list[int] = []

    async def fake_http_client(**_kwargs):
        calls.append(len(calls) + 1)
        if len(calls) < 2:
            raise ConnectionError("connection refused")
        return sentinel

    monkeypatch.setattr(factory_mod.chromadb, "AsyncHttpClient", fake_http_client)

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(factory_mod.asyncio, "sleep", fake_sleep)

    f = HTTPChromaDBFactory(url="http://chroma:8000")
    client = await f.get_client()

    assert client is sentinel
    assert len(calls) == 2
