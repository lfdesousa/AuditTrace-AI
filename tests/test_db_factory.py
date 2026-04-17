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
    assert captured["headers"] == {"Authorization": "Bearer secret-token"}


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
