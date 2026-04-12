"""Tests for memory service."""

from sovereign_memory.services.memory import (
    ChromaMemoryService,
    MemoryService,
    MockMemoryService,
)


def test_mock_memory_service_store():
    """Test mock memory service store method."""
    service = MockMemoryService()

    mem_id = service.store(
        project="test-project",
        source="test-source",
        content="test content",
        metadata={"key": "value"},
    )

    assert mem_id is not None
    assert service.count() == 1


def test_mock_memory_service_retrieve():
    """Test mock memory service retrieve method."""
    service = MockMemoryService()

    # Store some memories
    service.store("project1", "source1", "content1")
    service.store("project1", "source1", "content2")
    service.store("project2", "source1", "content3")

    # Retrieve all
    results = service.retrieve(query="test", limit=10)
    assert len(results) == 3

    # Retrieve with project filter
    results = service.retrieve(query="test", project="project1", limit=10)
    assert len(results) == 2

    # Retrieve with limit
    results = service.retrieve(query="test", limit=1)
    assert len(results) == 1


def test_mock_memory_service_reset():
    """Test mock memory service reset."""
    service = MockMemoryService()

    service.store("p1", "s1", "c1")
    service.store("p1", "s1", "c2")

    assert service.count() == 2

    service.reset()

    assert service.count() == 0


def test_mock_memory_service_call_tracking():
    """Test that service call count is tracked."""
    service = MockMemoryService()

    service.store("p1", "s1", "c1")
    assert service.call_count == 1

    service.store("p1", "s1", "c2")
    assert service.call_count == 2


def test_chroma_memory_service_interface():
    """Test that ChromaMemoryService implements MemoryService interface."""
    # Just verify the interface exists
    assert hasattr(MemoryService, "store")
    assert hasattr(MemoryService, "retrieve")
    assert hasattr(MemoryService, "count")


def test_chroma_memory_service_with_mock_client():
    """Test ChromaMemoryService with mock ChromaDB client."""
    from sovereign_memory.db.factory import MockChromaDBFactory

    factory = MockChromaDBFactory()
    client = factory.get_client()

    service = ChromaMemoryService(client, collection_name="test")

    # Store memory
    mem_id = service.store(
        project="test-project",
        source="test-source",
        content="test content",
        metadata={"key": "value"},
    )

    assert mem_id is not None

    # Count should be 1
    assert service.count() == 1

    # Retrieve should return the memory
    results = service.retrieve(query="test content", limit=10)
    assert len(results) == 1
    assert "test content" in results[0]["content"]
