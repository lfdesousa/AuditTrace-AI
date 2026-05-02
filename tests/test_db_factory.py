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


def test_memory_chromadb_factory():
    """Test memory ChromaDB factory creates in-memory client."""
    factory = MemoryChromaDBFactory()
    client = factory.get_client()
    assert client is not None


def test_http_chromadb_factory_stores_token():
    """Test HTTPChromaDBFactory accepts and stores auth token."""
    factory = HTTPChromaDBFactory(url="http://localhost:8000", token="secret-token")
    assert factory.token == "secret-token"


def test_http_chromadb_factory_token_defaults_none():
    """Test HTTPChromaDBFactory token defaults to None."""
    factory = HTTPChromaDBFactory(url="http://localhost:8000")
    assert factory.token is None


def test_mock_chromadb_factory():
    """Test mock ChromaDB factory."""
    factory = MockChromaDBFactory()
    client = factory.get_client()

    # Test collection operations
    collection = client.get_or_create_collection("test")
    assert collection.name == "test"

    # Test add
    ids = collection.add(ids=["id1"], documents=["doc1"], metadatas=[{"key": "value"}])
    assert ids == ["id1"]

    # Test query
    results = collection.query(query_texts=["test"])
    assert "ids" in results
    assert "documents" in results

    # Test count
    assert collection.count() == 1

    # Test reset
    factory.reset()
    assert len(factory.collections) == 0


def test_mock_collection():
    """Test MockCollection class."""
    collection = MockCollection("test_collection")

    # Test add
    collection.add(ids=["id1", "id2"], documents=["doc1", "doc2"])
    assert collection.count() == 2

    # Test query
    results = collection.query(query_texts=["test"], n_results=2)
    assert len(results["ids"][0]) == 2
    assert results["documents"][0][0] == "doc1"

    # Test get
    results = collection.get()
    assert len(results["ids"]) == 2


def test_factory_call_tracking():
    """Test that factory call count is tracked."""
    factory = MockChromaDBFactory()

    factory.get_client()
    assert factory.call_count == 1

    factory.get_client()
    assert factory.call_count == 2


# ── HTTPChromaDBFactory URL parsing branches ─────────────────────────────────


def test_http_factory_parses_url_with_scheme(monkeypatch):
    """HTTPChromaDBFactory.get_client must strip the scheme and split host:port."""
    from audittrace.db import factory as factory_mod

    captured: dict = {}

    def fake_http_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(factory_mod.chromadb, "HttpClient", fake_http_client)
    f = HTTPChromaDBFactory(url="http://chroma.example.com:9000")
    f.get_client()
    assert captured["host"] == "chroma.example.com"
    assert captured["port"] == 9000
    assert "headers" not in captured


def test_http_factory_parses_url_without_scheme(monkeypatch):
    """Bare host:port form is also accepted."""
    from audittrace.db import factory as factory_mod

    captured: dict = {}

    def fake_http_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(factory_mod.chromadb, "HttpClient", fake_http_client)
    f = HTTPChromaDBFactory(url="chroma:8000")
    f.get_client()
    assert captured["host"] == "chroma"
    assert captured["port"] == 8000


def test_http_factory_attaches_token_when_set(monkeypatch):
    """Bearer token must land in the Authorization header."""
    from audittrace.db import factory as factory_mod

    captured: dict = {}

    def fake_http_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(factory_mod.chromadb, "HttpClient", fake_http_client)
    f = HTTPChromaDBFactory(url="http://chroma:8000", token="secret-token")
    f.get_client()
    assert captured["headers"] == {"X-Chroma-Token": "secret-token"}


# ── MockCollection where-filter branches ─────────────────────────────────────


def test_mock_collection_query_with_where_filter():
    """MockCollection.query with a `where` clause filters by metadata."""
    collection = MockCollection("test")
    collection.add(
        ids=["a", "b", "c"],
        documents=["doc-a", "doc-b", "doc-c"],
        metadatas=[
            {"category": "x"},
            {"category": "y"},
            {"category": "x"},
        ],
    )
    result = collection.query(query_texts=["q"], where={"category": "x"})
    ids = result["ids"][0]
    assert set(ids) == {"a", "c"}
    assert "b" not in ids


def test_mock_collection_count_with_where_filter():
    """MockCollection.count with a `where` clause counts only matching rows."""
    collection = MockCollection("test")
    collection.add(
        ids=["a", "b", "c"],
        documents=["x", "y", "z"],
        metadatas=[{"k": "1"}, {"k": "2"}, {"k": "1"}],
    )
    assert collection.count() == 3
    assert collection.count(where={"k": "1"}) == 2
    assert collection.count(where={"k": "no-match"}) == 0


# ── HTTPChromaDBFactory startup-race retry ───────────────────────────────────
#
# Mirrors the Istio-sidecar startup race: chromadb's SDK wraps Envoy 503
# responses as ValueError("no healthy upstream") inside HttpClient.__init__.
# Pattern follows TestJWKSFetchRetry in tests/test_auth.py — same shape of
# transient-failure-with-exponential-backoff lives in audittrace.auth.


def test_http_factory_succeeds_on_second_attempt(monkeypatch):
    """First call fails, second succeeds — must return the client without raising."""
    import pytest  # noqa: F401  (kept for parity with the auth tests)

    from audittrace.db import factory as factory_mod

    sentinel = object()
    calls: list[int] = []

    def fake_http_client(**_kwargs):
        calls.append(len(calls) + 1)
        if len(calls) < 2:
            raise ValueError("no healthy upstream")
        return sentinel

    monkeypatch.setattr(factory_mod.chromadb, "HttpClient", fake_http_client)
    sleep_calls: list[float] = []
    monkeypatch.setattr(factory_mod.time, "sleep", sleep_calls.append)

    f = HTTPChromaDBFactory(url="http://chroma:8000")
    client = f.get_client()

    assert client is sentinel
    assert len(calls) == 2
    assert sleep_calls == [2.0]  # one backoff before the successful retry


def test_http_factory_exhausts_retries_and_reraises(monkeypatch):
    """All attempts fail — last ValueError must propagate after 4 total calls."""
    import pytest

    from audittrace.db import factory as factory_mod

    calls: list[int] = []

    def fake_http_client(**_kwargs):
        calls.append(len(calls) + 1)
        raise ValueError("no healthy upstream")

    monkeypatch.setattr(factory_mod.chromadb, "HttpClient", fake_http_client)
    monkeypatch.setattr(factory_mod.time, "sleep", lambda _delay: None)

    f = HTTPChromaDBFactory(url="http://chroma:8000")
    with pytest.raises(ValueError, match="no healthy upstream"):
        f.get_client()

    # 1 initial + _CHROMADB_CONNECT_RETRIES retries = 4 total calls
    assert len(calls) == factory_mod._CHROMADB_CONNECT_RETRIES + 1


def test_http_factory_backoff_delays_are_exponential(monkeypatch):
    """Sleep delays must follow 2^(attempt+1): 2, 4, 8."""
    import pytest

    from audittrace.db import factory as factory_mod

    def fake_http_client(**_kwargs):
        raise ValueError("no healthy upstream")

    monkeypatch.setattr(factory_mod.chromadb, "HttpClient", fake_http_client)
    sleep_calls: list[float] = []
    monkeypatch.setattr(factory_mod.time, "sleep", sleep_calls.append)

    f = HTTPChromaDBFactory(url="http://chroma:8000")
    with pytest.raises(ValueError):
        f.get_client()

    assert sleep_calls == [2.0, 4.0, 8.0]


def test_http_factory_succeeds_immediately_without_sleep(monkeypatch):
    """Happy path — single call returns client, no backoff."""
    from audittrace.db import factory as factory_mod

    sentinel = object()

    def fake_http_client(**_kwargs):
        return sentinel

    monkeypatch.setattr(factory_mod.chromadb, "HttpClient", fake_http_client)
    sleep_calls: list[float] = []
    monkeypatch.setattr(factory_mod.time, "sleep", sleep_calls.append)

    f = HTTPChromaDBFactory(url="http://chroma:8000")
    client = f.get_client()

    assert client is sentinel
    assert sleep_calls == []


def test_http_factory_retries_on_connection_error(monkeypatch):
    """ConnectionError (defensive catch alongside ValueError) must also retry."""
    import pytest  # noqa: F401

    from audittrace.db import factory as factory_mod

    sentinel = object()
    calls: list[int] = []

    def fake_http_client(**_kwargs):
        calls.append(len(calls) + 1)
        if len(calls) < 2:
            raise ConnectionError("connection refused")
        return sentinel

    monkeypatch.setattr(factory_mod.chromadb, "HttpClient", fake_http_client)
    monkeypatch.setattr(factory_mod.time, "sleep", lambda _delay: None)

    f = HTTPChromaDBFactory(url="http://chroma:8000")
    client = f.get_client()

    assert client is sentinel
    assert len(calls) == 2
