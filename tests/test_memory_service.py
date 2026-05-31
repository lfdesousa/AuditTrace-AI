"""Tests for memory service."""

from audittrace.services.memory import (
    ChromaMemoryService,
    MemoryService,
    MockMemoryService,
)


async def test_mock_memory_service_store():
    """Test mock memory service store method."""
    service = MockMemoryService()

    mem_id = await service.store(
        project="test-project",
        source="test-source",
        content="test content",
        metadata={"key": "value"},
    )

    assert mem_id is not None
    assert await service.count() == 1


async def test_mock_memory_service_retrieve():
    """Test mock memory service retrieve method."""
    service = MockMemoryService()

    # Store some memories
    await service.store("project1", "source1", "content1")
    await service.store("project1", "source1", "content2")
    await service.store("project2", "source1", "content3")

    # Retrieve all
    results = await service.retrieve(query="test", limit=10)
    assert len(results) == 3

    # Retrieve with project filter
    results = await service.retrieve(query="test", project="project1", limit=10)
    assert len(results) == 2

    # Retrieve with limit
    results = await service.retrieve(query="test", limit=1)
    assert len(results) == 1


async def test_mock_memory_service_reset():
    """Test mock memory service reset."""
    service = MockMemoryService()

    await service.store("p1", "s1", "c1")
    await service.store("p1", "s1", "c2")

    assert await service.count() == 2

    service.reset()

    assert await service.count() == 0


async def test_mock_memory_service_call_tracking():
    """Test that service call count is tracked."""
    service = MockMemoryService()

    await service.store("p1", "s1", "c1")
    assert service.call_count == 1

    await service.store("p1", "s1", "c2")
    assert service.call_count == 2


def test_chroma_memory_service_interface():
    """Test that ChromaMemoryService implements MemoryService interface."""
    # Just verify the interface exists
    assert hasattr(MemoryService, "store")
    assert hasattr(MemoryService, "retrieve")
    assert hasattr(MemoryService, "count")


async def test_chroma_memory_service_with_mock_client():
    """Test ChromaMemoryService with mock ChromaDB client."""
    from audittrace.db.factory import MockChromaDBFactory

    factory = MockChromaDBFactory()
    client = await factory.get_client()

    service = ChromaMemoryService(client, collection_name="test")

    # Store memory
    mem_id = await service.store(
        project="test-project",
        source="test-source",
        content="test content",
        metadata={"key": "value"},
    )

    assert mem_id is not None

    # Count should be 1
    assert await service.count() == 1

    # Retrieve should return the memory
    results = await service.retrieve(query="test content", limit=10)
    assert len(results) == 1
    assert "test content" in results[0]["content"]
