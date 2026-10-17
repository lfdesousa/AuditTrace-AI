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


async def test_mock_memory_service_count_scoped_to_project():
    """A project-scoped count must exclude other projects' memories.

    The mock backs every unit test that asserts on memory volume. If its
    project filter were ignored it would return the global total, so a
    test asserting "project A holds 2 memories" would pass even against a
    service that leaked project B's rows — hiding exactly the cross-project
    bleed the real filter exists to prevent.
    """
    service = MockMemoryService()

    await service.store("project1", "s1", "c1")
    await service.store("project1", "s1", "c2")
    await service.store("project2", "s1", "c3")

    assert await service.count(project="project1") == 2
    assert await service.count(project="project2") == 1
    # Unknown project is 0, not the global total — proves the filter runs.
    assert await service.count(project="project3") == 0
    # And the unfiltered count still sees everything.
    assert await service.count() == 3


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


async def test_chroma_count_passes_project_filter_to_collection():
    """``count(project=...)`` must push a ``where`` clause down to ChromaDB.

    The collection is shared across every project, so counting without the
    ``where={"project": ...}`` predicate returns the whole collection. That
    number surfaces on the memory-stats path — an operator reading it would
    see another tenant's document volume, and any capacity or retention
    decision keyed off it would be wrong.
    """
    from audittrace.db.factory import MockChromaDBFactory

    factory = MockChromaDBFactory()
    client = await factory.get_client()
    service = ChromaMemoryService(client, collection_name="test")

    await service.store(project="alpha", source="s", content="a1")
    await service.store(project="alpha", source="s", content="a2")
    await service.store(project="beta", source="s", content="b1")

    assert await service.count(project="alpha") == 2
    assert await service.count(project="beta") == 1
    # Distinct from the global total — the filter is not being dropped.
    assert await service.count() == 3
