"""Tests for SemanticService — Layer 4 of the 4-layer memory architecture (ADR-018).

Phase 2 (DESIGN §15): every service method takes ``user_context`` as the
first positional argument. ``ChromaSemanticService.search`` applies a
``where={"user_id": ...}`` filter when the caller is NOT admin — a preview
of the Phase 4 ChromaDB scoped wrapper. Admins see everything, which is
why the sentinel-backed ``user_context`` fixture (admin by construction)
keeps all legacy test data visible.
"""

from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from audittrace.db.factory import MockChromaDBFactory
from audittrace.services.semantic import (
    ChromaSemanticService,
    MockSemanticService,
    SemanticService,
)


@pytest.fixture(autouse=True)
def _mock_nomic_embed(monkeypatch):
    """ADR-047 — ChromaSemanticService always vectorises on the nomic server.
    The mock ChromaDB ignores embeddings (it matches on text/where), so a
    deterministic stub keeps these unit tests offline while exercising the
    real query_embeddings/embeddings code path."""
    monkeypatch.setattr(
        "audittrace.services.semantic.embed_via_nomic",
        AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]),
    )


# ── ChromaSemanticService tests ──────────────────────────────────────────────


class TestChromaSemanticService:
    @pytest.fixture
    async def service(self):
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        # Seed a collection with documents
        col = await client.get_or_create_collection(name="decisions_v2")
        await col.add(
            ids=["doc1", "doc2", "doc3"],
            documents=[
                "KV cache compression reduces memory by 75%",
                "ROCm GPU acceleration for AMD hardware",
                "OAuth2 OIDC token validation patterns",
            ],
            metadatas=[
                {"source": "ADR-009", "project": "AuditTrace"},
                {"source": "ADR-001", "project": "AuditTrace"},
                {"source": "SKILL-IAM", "project": "AuditTrace"},
            ],
        )
        return ChromaSemanticService(client=client, default_collections=["decisions"])

    async def test_search_returns_results(self, service, user_context):
        results = await service.search(user_context, "cache compression", k=2)
        assert len(results) >= 1

    async def test_search_returns_documents_with_metadata(self, service, user_context):
        results = await service.search(user_context, "cache", k=2)
        for doc in results:
            assert doc.page_content
            assert "source" in doc.metadata

    async def test_search_respects_k(self, service, user_context):
        results = await service.search(user_context, "anything", k=1)
        assert len(results) <= 1

    async def test_search_specific_collection(self, user_context):
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        col = await client.get_or_create_collection(name="skills_v2")
        await col.add(
            ids=["s1"],
            documents=["Architecture patterns for cloud"],
            metadatas=[{"source": "SKILL-ARCH"}],
        )
        service = ChromaSemanticService(client=client, default_collections=["skills"])
        results = await service.search(
            user_context, "architecture", k=4, collections=["skills"]
        )
        assert len(results) >= 1

    async def test_search_empty_collection(self, user_context):
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        await client.get_or_create_collection(name="empty_v2")
        service = ChromaSemanticService(client=client, default_collections=["empty"])
        results = await service.search(user_context, "anything", k=4)
        assert results == []

    async def test_available_collections_graceful_when_unsupported(self):
        """MockChromaDBClient doesn't implement list_collections — should return []."""
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        service = ChromaSemanticService(
            client=client, default_collections=["decisions"]
        )
        cols = await service.available_collections()
        assert cols == []  # graceful degradation

    async def test_search_across_multiple_collections(self, user_context):
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        col1 = await client.get_or_create_collection(name="decisions_v2")
        await col1.add(
            ids=["d1"], documents=["ADR content"], metadatas=[{"source": "adr"}]
        )
        col2 = await client.get_or_create_collection(name="skills_v2")
        await col2.add(
            ids=["s1"], documents=["Skill content"], metadatas=[{"source": "skill"}]
        )
        service = ChromaSemanticService(
            client=client, default_collections=["decisions", "skills"]
        )
        results = await service.search(user_context, "content", k=4)
        assert len(results) >= 2

    async def test_search_non_admin_applies_user_id_filter(self, user_context):
        """Phase 4 preview: a non-admin UserContext restricts results to rows
        whose metadata ``user_id`` matches the caller. Rows tagged with a
        different user_id must be invisible."""
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        col = await client.get_or_create_collection(name="decisions_v2")
        await col.add(
            ids=["mine", "theirs", "untagged"],
            documents=[
                "mine: private note about cache",
                "theirs: another user's note about cache",
                "untagged: legacy row without user_id",
            ],
            metadatas=[
                {"source": "n1", "user_id": "user-alice"},
                {"source": "n2", "user_id": "user-bob"},
                {"source": "n3"},  # no user_id — pre-Phase 2 row
            ],
        )
        service = ChromaSemanticService(
            client=client, default_collections=["decisions"]
        )

        alice_ctx = replace(
            user_context, user_id="user-alice", is_admin=False, scopes=()
        )
        results = await service.search(alice_ctx, "cache", k=10)
        contents = [d.page_content for d in results]
        assert any("mine" in c for c in contents)
        assert not any("theirs" in c for c in contents)

    async def test_search_admin_bypasses_user_id_filter(self, user_context):
        """Admin sees every row regardless of ``user_id`` metadata."""
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        col = await client.get_or_create_collection(name="decisions_v2")
        await col.add(
            ids=["mine", "theirs"],
            documents=[
                "mine: note about cache",
                "theirs: another note about cache",
            ],
            metadatas=[
                {"source": "n1", "user_id": "user-alice"},
                {"source": "n2", "user_id": "user-bob"},
            ],
        )
        service = ChromaSemanticService(
            client=client, default_collections=["decisions"]
        )
        # user_context fixture is sentinel → is_admin=True
        results = await service.search(user_context, "cache", k=10)
        assert len(results) == 2


# ── #372 / ADR-060 — record recall by id + score ────────────────────────────


class TestChromaSemanticServiceRecordsIdentity:
    """``search`` must thread the ChromaDB ``chunk_id`` (== the ``document_id``
    supplied at upsert) and a relevance ``score`` (the distance) into each
    returned ``Document.metadata`` so the audit trail can name the exact
    passage that shaped an answer, not just its text (#372)."""

    @pytest.fixture
    async def service(self):
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        col = await client.get_or_create_collection(name="decisions_v2")
        await col.add(
            ids=["chunk-abc", "chunk-def"],
            documents=[
                "KV cache compression reduces memory by 75%",
                "another cache note",
            ],
            metadatas=[
                {"source": "ADR-009.md", "document_sha256": "deadbeef" * 8},
                {"source": "ADR-001.md"},
            ],
        )
        return ChromaSemanticService(client=client, default_collections=["decisions"])

    async def test_search_threads_chunk_id(self, service, user_context):
        results = await service.search(user_context, "cache", k=10)
        assert results, "expected at least one hit"
        ids = {d.metadata.get("chunk_id") for d in results}
        assert "chunk-abc" in ids
        # Every returned doc carries a non-empty chunk_id (the durable id).
        assert all(d.metadata.get("chunk_id") for d in results)

    async def test_search_threads_distance_from_distances(self, service, user_context):
        results = await service.search(user_context, "cache", k=10)
        # MockCollection.query returns distances of 0.1 for every row. The RAW
        # distance is recorded (lower = closer), honestly named ``distance``.
        assert all(d.metadata.get("distance") == pytest.approx(0.1) for d in results)

    async def test_search_preserves_source_and_sha_metadata(
        self, service, user_context
    ):
        results = await service.search(user_context, "cache", k=10)
        by_id = {d.metadata["chunk_id"]: d for d in results}
        assert by_id["chunk-abc"].metadata["source"] == "ADR-009.md"
        assert by_id["chunk-abc"].metadata["document_sha256"] == "deadbeef" * 8

    async def test_search_distance_is_none_when_distances_absent(self, user_context):
        """Guard: a ChromaDB response without ``distances`` must not raise;
        ``distance`` degrades to ``None`` and ``chunk_id`` is still threaded."""
        col = AsyncMock()
        col.count = AsyncMock(return_value=1)
        col.query = AsyncMock(
            return_value={
                "ids": [["only-chunk"]],
                "documents": [["some cache text"]],
                "metadatas": [[{"source": "ADR-009.md"}]],
                # NO "distances" key on purpose.
            }
        )
        # ADR-062 Phase B: search() now also queries the corpus physical
        # collection (`decisions_corpus_v2`) — give it a distinct, empty
        # mock so this test's assertions stay about the PRIVATE tier only.
        corpus_col = AsyncMock()
        corpus_col.count = AsyncMock(return_value=0)
        client = MagicMock()

        async def _select(name: str, **_kw: object) -> AsyncMock:
            return col if name == "decisions_v2" else corpus_col

        client.get_or_create_collection = AsyncMock(side_effect=_select)
        service = ChromaSemanticService(
            client=client, default_collections=["decisions"]
        )
        results = await service.search(user_context, "cache", k=4)
        assert len(results) == 1
        assert results[0].metadata["chunk_id"] == "only-chunk"
        assert results[0].metadata["distance"] is None
        assert results[0].metadata["tier"] == "private"

    async def test_search_still_passes_user_id_where_filter(self, user_context):
        """The per-user isolation filter is UNTOUCHED by the id/score change:
        a non-admin caller still only sees rows tagged with their user_id,
        and those rows now also carry a chunk_id."""
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        col = await client.get_or_create_collection(name="decisions_v2")
        await col.add(
            ids=["mine", "theirs"],
            documents=["mine: cache note", "theirs: cache note"],
            metadatas=[
                {"source": "n1.md", "user_id": "user-alice"},
                {"source": "n2.md", "user_id": "user-bob"},
            ],
        )
        service = ChromaSemanticService(
            client=client, default_collections=["decisions"]
        )
        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())
        results = await service.search(alice, "cache", k=10)
        contents = [d.page_content for d in results]
        assert any("mine" in c for c in contents)
        assert not any("theirs" in c for c in contents)
        assert all(d.metadata.get("chunk_id") for d in results)


# ── MockSemanticService tests ────────────────────────────────────────────────


class TestMockSemanticService:
    async def test_mock_starts_empty(self, user_context):
        service = MockSemanticService()
        assert await service.search(user_context, "anything") == []

    async def test_mock_add_and_search(self, user_context):
        service = MockSemanticService()
        await service.add_document(
            "KV cache content", source="ADR-009", collection="decisions"
        )
        results = await service.search(user_context, "cache")
        assert len(results) == 1
        assert "cache" in results[0].page_content.lower()

    async def test_mock_available_collections(self):
        service = MockSemanticService()
        await service.add_document("test", source="s", collection="decisions")
        await service.add_document("test", source="s", collection="skills")
        cols = await service.available_collections()
        assert "decisions" in cols
        assert "skills" in cols

    async def test_mock_reset(self, user_context):
        service = MockSemanticService()
        await service.add_document("test", source="s", collection="decisions")
        service.reset()
        assert await service.search(user_context, "test") == []
        assert await service.available_collections() == []

    def test_abstract_interface(self):
        assert isinstance(MockSemanticService(), SemanticService)


# ──────────────── Phase 4 — UserScopedSemanticService wrapper ───────────────
# The wrapper binds a UserContext at construction time and ignores any
# user_context passed at call time. Isolation is then true by construction:
# a bug elsewhere in the code base that leaks an admin context into a
# non-admin user's request cannot bypass the filter because the wrapper's
# bound identity is the only one the underlying service ever sees.
#
# This is the ChromaDB half of DESIGN §16 Phase 4. The Postgres half
# lives in Alembic migration 005 (RLS policies).


class TestUserScopedSemanticService:
    """Contract for the Phase 4 request-scoped wrapper."""

    @pytest.fixture
    async def _client_with_two_users(self):
        """Chroma client seeded with two users' docs + one untagged row."""
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        col = await client.get_or_create_collection(name="decisions_v2")
        await col.add(
            ids=["alice1", "bob1", "legacy"],
            documents=[
                "alice private note about cache",
                "bob private note about cache",
                "untagged legacy row about cache",
            ],
            metadatas=[
                {"source": "n1", "user_id": "user-alice"},
                {"source": "n2", "user_id": "user-bob"},
                {"source": "n3"},  # no user_id
            ],
        )
        return client

    async def test_wrapper_binds_user_at_construction(
        self, _client_with_two_users, user_context
    ):
        """A wrapper constructed with alice's UserContext delegates to the
        inner service with that context — regardless of what's passed at
        call time."""
        from dataclasses import replace

        from audittrace.services.semantic import (
            ChromaSemanticService,
            UserScopedSemanticService,
        )

        inner = ChromaSemanticService(
            client=_client_with_two_users, default_collections=["decisions"]
        )
        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())
        wrapper = UserScopedSemanticService(inner=inner, user_context=alice)

        results = await wrapper.search(user_context, "cache", k=10)
        contents = [d.page_content for d in results]
        assert any("alice" in c for c in contents)
        assert not any("bob" in c for c in contents)

    async def test_wrapper_ignores_per_call_user_context(
        self, _client_with_two_users, user_context
    ):
        """Even if the caller passes an ADMIN context at call time, the
        wrapper overrides it with the bound (non-admin alice) context.
        This is the 'isolation by construction' property."""
        from dataclasses import replace

        from audittrace.services.semantic import (
            ChromaSemanticService,
            UserScopedSemanticService,
        )

        inner = ChromaSemanticService(
            client=_client_with_two_users, default_collections=["decisions"]
        )
        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())
        wrapper = UserScopedSemanticService(inner=inner, user_context=alice)

        # user_context fixture is the admin sentinel — wrapper must IGNORE it
        assert user_context.is_admin is True
        results = await wrapper.search(user_context, "cache", k=10)
        # Still only alice's row — admin context at call time is discarded
        assert len(results) == 1
        assert "alice" in results[0].page_content

    async def test_wrapper_admin_binding_bypasses_filter(
        self, _client_with_two_users, user_context
    ):
        """A wrapper BOUND with an admin UserContext bypasses the where
        filter entirely, mirroring Phase 2 admin semantics. The binding
        is the authority — it's just pinned at construction time instead
        of being decided per call."""
        from audittrace.services.semantic import (
            ChromaSemanticService,
            UserScopedSemanticService,
        )

        inner = ChromaSemanticService(
            client=_client_with_two_users, default_collections=["decisions"]
        )
        # user_context fixture is admin-by-construction (sentinel)
        wrapper = UserScopedSemanticService(inner=inner, user_context=user_context)

        results = await wrapper.search(user_context, "cache", k=10)
        # Admin sees everything: alice + bob + untagged legacy
        assert len(results) == 3

    async def test_wrapper_available_collections_delegates(
        self, _client_with_two_users, user_context
    ):
        """available_collections is a pass-through; it doesn't touch the
        where filter."""
        from audittrace.services.semantic import (
            ChromaSemanticService,
            UserScopedSemanticService,
        )

        inner = ChromaSemanticService(
            client=_client_with_two_users, default_collections=["decisions"]
        )
        wrapper = UserScopedSemanticService(inner=inner, user_context=user_context)
        assert (
            await wrapper.available_collections() == await inner.available_collections()
        )

    def test_wrapper_is_semantic_service(self, user_context):
        """The wrapper must implement SemanticService so context_builder
        can inject it transparently as a drop-in replacement."""
        from audittrace.services.semantic import (
            MockSemanticService,
            UserScopedSemanticService,
        )

        wrapper = UserScopedSemanticService(
            inner=MockSemanticService(), user_context=user_context
        )
        assert isinstance(wrapper, SemanticService)


# ── upsert / delete_document / get_document (PR A — CRUD backoffice) ────────


class TestChromaSemanticServiceCrud:
    """Write-side tests via the MockChromaDBFactory."""

    @pytest.fixture
    async def service(self):
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        return ChromaSemanticService(client=client, default_collections=["decisions"])

    async def test_upsert_then_get(self, service, user_context):
        await service.upsert(
            user_context,
            "decisions",
            "doc-7",
            "hello world",
            metadata={"source": "ADR-007"},
        )
        doc = await service.get_document(user_context, "decisions", "doc-7")
        assert doc is not None
        assert doc.page_content == "hello world"
        # User-id stamping happened (sentinel admin user_id).
        assert "user_id" in doc.metadata

    async def test_upsert_replaces_existing(self, service, user_context):
        await service.upsert(user_context, "decisions", "doc-7", "v1")
        await service.upsert(user_context, "decisions", "doc-7", "v2")
        doc = await service.get_document(user_context, "decisions", "doc-7")
        assert doc.page_content == "v2"

    async def test_get_missing_returns_none(self, service, user_context):
        assert await service.get_document(user_context, "decisions", "nope") is None

    async def test_delete_existing_returns_true(self, service, user_context):
        await service.upsert(user_context, "decisions", "doc-d", "bye")
        assert await service.delete_document(user_context, "decisions", "doc-d") is True
        assert await service.get_document(user_context, "decisions", "doc-d") is None

    async def test_delete_missing_returns_false(self, service, user_context):
        assert (
            await service.delete_document(user_context, "decisions", "never") is False
        )


class TestSoftDeleteTombstoneFiltersRecall:
    """ADR-062 §6 (WU-A5) — the manifest soft-delete tombstone is
    authoritative for corpus recall.

    Falsifiable gate: soft-delete a corpus item via the manifest → it no
    longer surfaces via ``search()``/``search_page()``, but is recoverable
    by re-creating (manifest revive + a fresh upsert)."""

    @pytest.fixture
    async def manifest(self):
        from audittrace.services.memory_manifest import MockMemoryManifestService

        return MockMemoryManifestService()

    @pytest.fixture
    async def service(self, manifest):
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        return ChromaSemanticService(
            client=client, default_collections=["decisions"], manifest=manifest
        )

    async def _seed(self, service, manifest, user_context, *, text="body v1") -> None:
        await service.upsert(user_context, "decisions", "doc-tomb", text)
        await manifest.record_create(
            "semantic", "decisions/doc-tomb", "title", len(text), user_context.user_id
        )

    async def test_search_excludes_soft_deleted(
        self, service, manifest, user_context
    ) -> None:
        await self._seed(service, manifest, user_context)
        assert len(await service.search(user_context, "body", k=4)) == 1

        await manifest.record_delete("semantic", "decisions/doc-tomb", "someone")
        assert await service.search(user_context, "body", k=4) == []

    async def test_search_page_excludes_soft_deleted(
        self, service, manifest, user_context
    ) -> None:
        await self._seed(service, manifest, user_context)
        page = await service.search_page(user_context, "body", k=4)
        assert page.total == 1

        await manifest.record_delete("semantic", "decisions/doc-tomb", "someone")
        page = await service.search_page(user_context, "body", k=4)
        assert page.matches == []
        assert page.total == 0

    async def test_recreate_after_soft_delete_is_recoverable(
        self, service, manifest, user_context
    ) -> None:
        await self._seed(service, manifest, user_context)
        await manifest.record_delete("semantic", "decisions/doc-tomb", "someone")
        assert await service.search(user_context, "body", k=4) == []

        # Re-create: manifest revives (record_create clears deleted_at_ms)
        # and the Chroma doc is re-upserted — mirrors what
        # create_semantic()/POST /memory/semantic does.
        await self._seed(service, manifest, user_context, text="body v2 revived")
        results = await service.search(user_context, "body", k=4)
        assert len(results) == 1
        assert results[0].page_content == "body v2 revived"

    async def test_no_manifest_wired_is_a_no_op(self, user_context) -> None:
        """Backward compatibility: a service constructed without
        ``manifest=`` (every pre-existing test double / call site)
        filters nothing — the capability is simply absent, not "nothing
        is ever deleted"."""
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        service = ChromaSemanticService(
            client=client, default_collections=["decisions"]
        )
        await service.upsert(user_context, "decisions", "doc-x", "unfiltered body")
        assert len(await service.search(user_context, "unfiltered", k=4)) == 1

    async def test_manifest_lookup_failure_fails_open(
        self, service, manifest, user_context, monkeypatch
    ) -> None:
        """A manifest outage must not take recall down with it — the
        candidate set is returned UNFILTERED (with a logged warning)
        rather than raising or dropping everything."""
        await self._seed(service, manifest, user_context)

        async def _boom(layer, keys):
            raise RuntimeError("manifest unreachable")

        monkeypatch.setattr(manifest, "get_deleted_keys", _boom)
        results = await service.search(user_context, "body", k=4)
        assert len(results) == 1


class TestMockSemanticServiceCrud:
    """In-memory variant — used by the route tests."""

    async def test_upsert_get_delete(self, user_context):
        s = MockSemanticService()
        await s.upsert(user_context, "col", "id-1", "first", metadata={"k": "v"})
        d = await s.get_document(user_context, "col", "id-1")
        assert d is not None and d.page_content == "first"
        # Replace
        await s.upsert(user_context, "col", "id-1", "second")
        d = await s.get_document(user_context, "col", "id-1")
        assert d.page_content == "second"
        # Delete
        assert await s.delete_document(user_context, "col", "id-1") is True
        assert await s.get_document(user_context, "col", "id-1") is None
        assert await s.delete_document(user_context, "col", "id-1") is False

    async def test_upsert_replaces_only_the_matching_document(self, user_context):
        """The replace scan must skip non-matching documents, not overwrite
        the first row it walks past.

        Every route test that runs against ``MockSemanticService`` trusts it to
        behave like ChromaDB's id-keyed upsert. If the scan replaced on the
        wrong index, re-uploading one document would silently destroy an
        unrelated one and the route tests would still pass — the corruption
        only shows up against real ChromaDB in production.
        """
        s = MockSemanticService()
        await s.upsert(user_context, "col", "id-a", "alpha")
        await s.upsert(user_context, "col", "id-b", "beta")
        # id-a is scanned first and must NOT match, so the loop moves on.
        await s.upsert(user_context, "col", "id-b", "beta-v2")

        doc_a = await s.get_document(user_context, "col", "id-a")
        doc_b = await s.get_document(user_context, "col", "id-b")
        assert doc_a is not None and doc_a.page_content == "alpha"
        assert doc_b is not None and doc_b.page_content == "beta-v2"
        # Replace, not append: the collection still holds exactly two rows.
        assert len(s._docs["col"]) == 2

    async def test_delete_removes_only_the_matching_document(self, user_context):
        """Deleting the second document must leave the first intact.

        ``delete_document`` pops by list index while scanning; an off-by-one
        would delete a bystander document. The admin backoffice DELETE route
        is wired to this method, so the blast radius is operator-visible data
        loss on the wrong record.
        """
        s = MockSemanticService()
        await s.upsert(user_context, "col", "id-a", "alpha")
        await s.upsert(user_context, "col", "id-b", "beta")

        assert await s.delete_document(user_context, "col", "id-b") is True
        survivor = await s.get_document(user_context, "col", "id-a")
        assert survivor is not None and survivor.page_content == "alpha"
        assert await s.get_document(user_context, "col", "id-b") is None


class TestChromaSemanticServiceDegradedPayloads:
    """ChromaDB responses that are well-formed but incomplete."""

    async def test_get_document_returns_none_when_documents_payload_empty(
        self, user_context
    ):
        """A collection row can exist with an id but no stored text (an
        embedding-only record, or a row written by a client that omitted
        ``documents``). ``get_document`` must degrade to ``None``.

        Without the guard the ``documents[0]`` access raises ``IndexError``,
        which surfaces on the admin backoffice GET as a 500 instead of a clean
        404 — an operator investigating an audit trail would read that as
        "the service is broken" rather than "that document has no text".
        """
        col = AsyncMock()
        col.get = AsyncMock(
            return_value={
                "ids": ["doc-1"],
                "documents": [],
                "metadatas": [{"source": "ADR-007"}],
            }
        )
        client = MagicMock()
        client.get_or_create_collection = AsyncMock(return_value=col)
        service = ChromaSemanticService(
            client=client, default_collections=["decisions"]
        )

        assert await service.get_document(user_context, "decisions", "doc-1") is None


class TestUserScopedSemanticServiceCrud:
    """The wrapper must forward upsert/get/delete to the inner service
    using the bound user (not the per-call argument)."""

    async def test_wrapper_forwards_upsert(self, user_context):
        from audittrace.services.semantic import UserScopedSemanticService

        inner = MockSemanticService()
        wrapper = UserScopedSemanticService(inner=inner, user_context=user_context)
        # Use a different user_context as the per-call arg; wrapper should
        # ignore it and use the bound one.
        other = replace(user_context, user_id="some-other-user")
        await wrapper.upsert(other, "col", "id-1", "text")
        assert await inner.get_document(user_context, "col", "id-1") is not None

    async def test_wrapper_forwards_delete_and_get(self, user_context):
        from audittrace.services.semantic import UserScopedSemanticService

        inner = MockSemanticService()
        await inner.upsert(user_context, "col", "id-1", "x")
        wrapper = UserScopedSemanticService(inner=inner, user_context=user_context)
        assert await wrapper.get_document(user_context, "col", "id-1") is not None
        assert await wrapper.delete_document(user_context, "col", "id-1") is True


# ── search_page — backlog #15 residual (#375 / RECALL-PAGINATION-20260803) ──
#
# R1: ``ChromaSemanticService.search_page`` — bounded ranked window, offset
# slicing, deterministic order (distance asc, stable tiebreak on durable id).
# R2/R2b: honest total/has_more, sort={relevance,recency,id} + order.
#
# All seeded ids below are zero-padded (``d000``..``d019`` etc.) so that,
# under MockCollection's uniform 0.1 mock distance, the stable id tiebreak
# alone determines order deterministically — insertion order, lexicographic
# id order, and "true rank" all coincide, which is what makes the
# marker-at-rank-N setup below meaningful without a real vector index.


class TestSearchPageDefaultFallback:
    """The ABC's concrete ``search_page`` default (used by any
    ``SemanticService`` that only implements ``search()`` — mocks, spies).
    """

    async def test_negative_offset_clamps_to_zero(self, user_context):
        service = MockSemanticService()
        await service.add_document("cache note", source="s", collection="decisions")
        page = await service.search_page(user_context, "cache", k=4, offset=-5)
        assert page.offset == 0

    async def test_k_below_one_clamps_to_one(self, user_context):
        service = MockSemanticService()
        await service.add_document("cache note", source="s", collection="decisions")
        page = await service.search_page(user_context, "cache", k=0)
        assert page.limit == 1

    async def test_unknown_sort_falls_back_to_relevance(self, user_context):
        service = MockSemanticService()
        await service.add_document("cache note", source="s", collection="decisions")
        page = await service.search_page(user_context, "cache", k=4, sort="bogus")
        assert page.sort == "relevance"
        assert page.order == "asc"

    async def test_recency_sort_via_default_fallback(self, user_context):
        """Exercises the ABC default's recency/id key-function branches
        (used by e.g. ``MockSemanticService``, which has no bounded-window
        query capability of its own)."""
        service = MockSemanticService()
        await service.add_document(
            "cache note one", source="s1", collection="decisions"
        )
        await service.add_document(
            "cache note two", source="s2", collection="decisions"
        )
        page = await service.search_page(user_context, "cache", k=4, sort="recency")
        assert page.sort == "recency"
        assert page.order == "desc"  # per-sort default (R2b)
        assert len(page.matches) == 2

    async def test_id_sort_via_default_fallback(self, user_context):
        service = MockSemanticService()
        await service.add_document("cache note", source="s1", collection="decisions")
        page = await service.search_page(user_context, "cache", k=4, sort="id")
        assert page.sort == "id"
        assert page.order == "asc"

    async def test_backward_compat_no_offset_matches_search(self, user_context):
        """R2: a call with none of offset/sort/order returns the exact same
        top-k content as plain ``search()`` did before this change."""
        service = MockSemanticService()
        await service.add_document("cache note", source="s1", collection="decisions")
        old = await service.search(user_context, "cache", k=4)
        page = await service.search_page(user_context, "cache", k=4)
        assert [d.page_content for d in page.matches] == [d.page_content for d in old]


class TestChromaSemanticServiceSearchPage:
    """The efficient, ChromaDB-native ``search_page`` override."""

    async def _seed(self, count: int, *, marker_at: int | None = None) -> Any:
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        col = await client.get_or_create_collection(name="decisions_v2")
        ids = [f"d{i:03d}" for i in range(count)]
        documents = [f"filler decision number {i}" for i in range(count)]
        if marker_at is not None:
            documents[marker_at] = "MARKER decision about pagination reachability"
        metadatas = [{"source": f"ADR-{i:03d}.md"} for i in range(count)]
        await col.add(ids=ids, documents=documents, metadatas=metadatas)
        return ChromaSemanticService(client=client, default_collections=["decisions"])

    async def test_backward_compat_default_page_matches_todays_top_k(
        self, user_context
    ):
        """R2/R6: no offset/sort/order → identical top-k content to
        ``search()`` (same k, same distance-ascending order)."""
        service = await self._seed(10)
        old = await service.search(user_context, "anything", k=4)
        page = await service.search_page(user_context, "anything", k=4)
        assert [d.page_content for d in page.matches] == [d.page_content for d in old]
        assert page.limit == 4
        assert page.offset == 0
        assert page.sort == "relevance"
        assert page.order == "asc"

    async def test_falsifiability_marker_beyond_k_unreachable_at_offset_zero(
        self, user_context
    ):
        """FALSIFIABILITY (Gates): a target ranked beyond ``k`` is NOT
        reachable at ``offset=0`` but IS reachable at ``offset=k`` (here
        offset=12, the marker's rank). If ``offset`` is dropped/ignored
        internally (neutered), the ``offset=12`` call below returns the
        SAME page as ``offset=0`` and the second assertion goes red —
        manually verified by temporarily hardcoding ``offset = 0`` at the
        top of ``ChromaSemanticService.search_page`` and re-running this
        test (fails), then reverting (passes)."""
        service = await self._seed(20, marker_at=12)

        page0 = await service.search_page(user_context, "filler", k=5, offset=0)
        contents0 = [d.page_content for d in page0.matches]
        assert not any("MARKER" in c for c in contents0)
        assert page0.has_more is True
        assert page0.total == 6  # probe window: min(0+5+1, 500, 20)

        page12 = await service.search_page(user_context, "filler", k=5, offset=12)
        contents12 = [d.page_content for d in page12.matches]
        assert any("MARKER" in c for c in contents12), (
            "marker must be reachable once offset advances past its rank"
        )
        assert page12.total == 18  # probe window: min(12+5+1, 500, 20)
        assert page12.has_more is True

    async def test_has_more_false_once_collection_exhausted(self, user_context):
        """When the window is NOT saturated (fewer candidates than
        requested), ``total`` is exact and ``has_more`` correctly reads
        False — no ambiguity, no false "keep paging" signal."""
        service = await self._seed(3)
        page = await service.search_page(user_context, "filler", k=10, offset=0)
        assert page.total == 3
        assert page.has_more is False
        assert len(page.matches) == 3

    async def test_offset_beyond_all_candidates_returns_empty_page(self, user_context):
        service = await self._seed(5)
        page = await service.search_page(user_context, "filler", k=5, offset=1000)
        assert page.matches == []
        assert page.total == 5
        assert page.has_more is False

    async def test_max_recall_window_caps_total_and_stops_has_more(self, user_context):
        """DoD scenario: a collection seeded past MAX_RECALL_WINDOW (500).
        ``total`` caps at 500 (never the true 510) and, once
        offset+limit reaches that cap, ``has_more`` correctly flips to
        False — the documented hard-cap boundary, not a bug."""
        from audittrace.services.semantic import MAX_RECALL_WINDOW

        service = await self._seed(MAX_RECALL_WINDOW + 10)
        page = await service.search_page(user_context, "filler", k=10, offset=495)
        assert page.total == MAX_RECALL_WINDOW
        assert page.has_more is False
        # Short page: only 500 candidates were ever collected, so slicing
        # [495:505] yields 5, not the requested 10.
        assert len(page.matches) == 5

    async def test_multi_collection_merge_reaches_second_collection(self, user_context):
        """Proves BOTH target collections are queried and merged before
        pagination — a regression that only opened the first collection
        would make a marker living solely in the second unreachable at any
        offset."""
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        col_a = await client.get_or_create_collection(name="decisions_v2")
        await col_a.add(
            ids=["f000", "f001", "f002"],
            documents=["filler a0", "filler a1", "filler a2"],
            metadatas=[{"source": "a0"}, {"source": "a1"}, {"source": "a2"}],
        )
        col_b = await client.get_or_create_collection(name="skills_v2")
        await col_b.add(
            ids=["m000"],
            documents=["MARKER only in the second collection"],
            metadatas=[{"source": "m0"}],
        )
        service = ChromaSemanticService(
            client=client, default_collections=["decisions", "skills"]
        )
        page = await service.search_page(user_context, "filler", k=1, offset=3)
        assert page.total == 4
        assert len(page.matches) == 1
        assert "MARKER" in page.matches[0].page_content

    async def test_sort_recency_orders_by_ingestion_timestamp_desc_default(
        self, user_context
    ):
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        col = await client.get_or_create_collection(name="decisions_v2")
        await col.add(
            ids=["old", "mid", "new"],
            documents=["oldest chunk", "middle chunk", "newest chunk"],
            metadatas=[
                {"source": "old.md", "ingestion_ts_ms": 100},
                {"source": "mid.md", "ingestion_ts_ms": 200},
                {"source": "new.md", "ingestion_ts_ms": 300},
            ],
        )
        service = ChromaSemanticService(
            client=client, default_collections=["decisions"]
        )
        page = await service.search_page(user_context, "chunk", k=10, sort="recency")
        assert page.order == "desc"
        ids = [d.metadata["chunk_id"] for d in page.matches]
        assert ids == ["new", "mid", "old"]

    async def test_sort_recency_asc_and_missing_timestamp_coalesces_to_zero(
        self, user_context
    ):
        """R2b: total-order safe — a chunk with no ``ingestion_ts_ms``
        (every ``.md``-sourced chunk today) coalesces to 0 and sorts as
        OLDEST rather than raising."""
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        col = await client.get_or_create_collection(name="decisions_v2")
        await col.add(
            ids=["undated", "dated"],
            documents=["undated chunk", "dated chunk"],
            metadatas=[
                {"source": "undated.md"},  # no ingestion_ts_ms
                {"source": "dated.md", "ingestion_ts_ms": 50},
            ],
        )
        service = ChromaSemanticService(
            client=client, default_collections=["decisions"]
        )
        page = await service.search_page(
            user_context, "chunk", k=10, sort="recency", order="asc"
        )
        ids = [d.metadata["chunk_id"] for d in page.matches]
        assert ids == ["undated", "dated"]  # 0 (coalesced) sorts before 50

    async def test_sort_id_orders_lexicographically(self, user_context):
        service = await self._seed(5)
        page = await service.search_page(user_context, "filler", k=10, sort="id")
        ids = [d.metadata["chunk_id"] for d in page.matches]
        assert ids == sorted(ids)

    async def test_relevance_desc_uses_enumeration_path_not_probe_window(
        self, user_context
    ):
        """``relevance`` + ``order="desc"`` cannot use the cheap probe-window
        path (ChromaDB has no "farthest-first" query) — it must fall back to
        the ``get()`` enumeration path, same as recency/id."""
        service = await self._seed(3)
        page = await service.search_page(
            user_context, "filler", k=10, sort="relevance", order="desc"
        )
        assert page.order == "desc"
        assert len(page.matches) == 3

    async def test_non_admin_where_filter_still_applied(self, user_context):
        """The per-user isolation filter from ``search()`` is preserved in
        ``search_page`` for BOTH retrieval strategies."""
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        col = await client.get_or_create_collection(name="decisions_v2")
        await col.add(
            ids=["mine", "theirs"],
            documents=["mine: cache note", "theirs: cache note"],
            metadatas=[
                {"source": "n1.md", "user_id": "user-alice"},
                {"source": "n2.md", "user_id": "user-bob"},
            ],
        )
        service = ChromaSemanticService(
            client=client, default_collections=["decisions"]
        )
        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())

        relevance_page = await service.search_page(alice, "cache", k=10)
        assert len(relevance_page.matches) == 1
        assert "mine" in relevance_page.matches[0].page_content

        recency_page = await service.search_page(alice, "cache", k=10, sort="recency")
        assert len(recency_page.matches) == 1
        assert "mine" in recency_page.matches[0].page_content

    async def test_negative_offset_and_sub_one_k_clamp(self, user_context):
        """``ChromaSemanticService.search_page`` has its own offset/k
        clamps (distinct from the ABC default's — this is the production
        override), so both need direct coverage."""
        service = await self._seed(3)
        page = await service.search_page(user_context, "filler", k=0, offset=-5)
        assert page.offset == 0
        assert page.limit == 1

    async def test_empty_collection_returns_empty_page(self, user_context):
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        await client.get_or_create_collection(name="empty_v2")
        service = ChromaSemanticService(client=client, default_collections=["empty"])
        page = await service.search_page(user_context, "anything", k=4)
        assert page.matches == []
        assert page.total == 0
        assert page.has_more is False

    async def test_relevance_query_exception_is_logged_and_skipped(self, user_context):
        """A collection that explodes mid-query must not take down the
        whole search_page call — mirrors ``search()``'s existing
        graceful-degradation contract."""
        col = AsyncMock()
        col.count = AsyncMock(return_value=1)
        col.query = AsyncMock(side_effect=RuntimeError("boom"))
        client = MagicMock()
        client.get_or_create_collection = AsyncMock(return_value=col)
        service = ChromaSemanticService(
            client=client, default_collections=["decisions"]
        )
        page = await service.search_page(user_context, "anything", k=4)
        assert page.matches == []
        assert page.total == 0

    async def test_enumeration_get_exception_is_logged_and_skipped(self, user_context):
        col = AsyncMock()
        col.get = AsyncMock(side_effect=RuntimeError("boom"))
        client = MagicMock()
        client.get_or_create_collection = AsyncMock(return_value=col)
        service = ChromaSemanticService(
            client=client, default_collections=["decisions"]
        )
        page = await service.search_page(user_context, "anything", k=4, sort="id")
        assert page.matches == []
        assert page.total == 0

    async def test_get_results_degrade_gracefully_when_documents_empty(
        self, user_context
    ):
        """``_shape_get_results`` guard: a row with an id but no
        document/metadata payload must not raise — degrades to empty text
        and empty metadata rather than an IndexError."""
        col = AsyncMock()
        col.get = AsyncMock(
            return_value={"ids": ["doc-1"], "documents": [], "metadatas": []}
        )
        # ADR-062 Phase B: the enumeration path also fetches the corpus
        # physical collection — give it a distinct, empty mock so this
        # test's single-match assertion stays about the PRIVATE tier only.
        corpus_col = AsyncMock()
        corpus_col.get = AsyncMock(
            return_value={"ids": [], "documents": [], "metadatas": []}
        )
        client = MagicMock()

        async def _select(name: str, **_kw: object) -> AsyncMock:
            return col if name == "decisions_v2" else corpus_col

        client.get_or_create_collection = AsyncMock(side_effect=_select)
        service = ChromaSemanticService(
            client=client, default_collections=["decisions"]
        )
        page = await service.search_page(user_context, "anything", k=4, sort="id")
        assert len(page.matches) == 1
        assert page.matches[0].page_content == ""
        assert page.matches[0].metadata["distance"] is None


class TestUserScopedSemanticServiceSearchPage:
    async def test_wrapper_delegates_search_page_to_inner_with_bound_user(
        self, user_context
    ):
        from audittrace.services.semantic import UserScopedSemanticService

        factory = MockChromaDBFactory()
        client = await factory.get_client()
        col = await client.get_or_create_collection(name="decisions_v2")
        await col.add(
            ids=["alice1", "bob1"],
            documents=["alice note about cache", "bob note about cache"],
            metadatas=[
                {"source": "n1", "user_id": "user-alice"},
                {"source": "n2", "user_id": "user-bob"},
            ],
        )
        inner = ChromaSemanticService(client=client, default_collections=["decisions"])
        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())
        wrapper = UserScopedSemanticService(inner=inner, user_context=alice)

        # Per-call context is admin (sentinel) — wrapper must still bind alice.
        page = await wrapper.search_page(user_context, "cache", k=10)
        assert len(page.matches) == 1
        assert "alice" in page.matches[0].page_content

    async def test_wrapper_search_page_forwards_pagination_params(self, user_context):
        from audittrace.services.semantic import UserScopedSemanticService

        service = MockSemanticService()
        await service.add_document("cache note", source="s", collection="decisions")
        wrapper = UserScopedSemanticService(inner=service, user_context=user_context)
        page = await wrapper.search_page(
            user_context, "cache", k=4, offset=0, sort="id", order="desc"
        )
        assert page.sort == "id"
        assert page.order == "desc"


# ── ADR-062 Phase B (WU-B3) — corpus collections + isolation-safety bar ────
#
# D1: every logical collection resolves to TWO physical ChromaDB
# collections — a per-user PRIVATE one (`_physical`, `where`-scoped for
# non-admin, #383) and a shared CORPUS one (`_physical_corpus`, always
# unfiltered). §3's two holes: `get_document`/`delete_document` used to be
# fully unfiltered (`del user_context`); `_merge_semantic_with_chroma` in
# routes/memory.py (tested separately, test_memory_routes.py) used to
# enumerate every row via a raw, unfiltered `col.get()`.
#
# THE FALSIFIABLE BAR (per the spec): user A upserts a PRIVATE vector →
# user B's `search`/`search_page` does NOT return it AND `get_document` by
# id returns None (404 at the route); a CORPUS-tier vector hits for BOTH.


class TestPhysicalCorpusNaming:
    def test_physical_corpus_naming(self):
        assert ChromaSemanticService._physical_corpus("decisions") == (
            "decisions_corpus_v2"
        )
        # Disjoint from the private physical name for the same logical
        # collection — the whole point of D1's physical separation.
        assert ChromaSemanticService._physical_corpus(
            "decisions"
        ) != ChromaSemanticService._physical("decisions")


class TestSearchEmbedFailureDegrades:
    """The hoisted single-embed-call refactor (search()/search_page()
    relevance path now embed ONCE, shared across both tiers) must still
    degrade to an empty result on an embed failure, not raise."""

    @pytest.fixture
    async def service(self):
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        return ChromaSemanticService(client=client, default_collections=["decisions"])

    async def test_search_returns_empty_on_embed_failure(
        self, service, user_context, monkeypatch
    ):
        async def _boom(self, text):
            raise RuntimeError("nomic unreachable")

        monkeypatch.setattr(ChromaSemanticService, "_embed_one", _boom)
        assert await service.search(user_context, "anything", k=4) == []

    async def test_search_page_relevance_returns_empty_page_on_embed_failure(
        self, service, user_context, monkeypatch
    ):
        async def _boom(self, text):
            raise RuntimeError("nomic unreachable")

        monkeypatch.setattr(ChromaSemanticService, "_embed_one", _boom)
        page = await service.search_page(user_context, "anything", k=4)
        assert page.matches == []
        assert page.total == 0


class TestSearchDualTier:
    """search()/search_page() query BOTH physical collections and tag
    `metadata["tier"]`."""

    @pytest.fixture
    async def two_tier_service(self):
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        private_col = await client.get_or_create_collection(name="decisions_v2")
        await private_col.add(
            ids=["alice-private"],
            documents=["alice private cache note"],
            metadatas=[{"user_id": "user-alice", "tier": "private"}],
        )
        corpus_col = await client.get_or_create_collection(name="decisions_corpus_v2")
        await corpus_col.add(
            ids=["shared-adr"],
            documents=["shared corpus cache note"],
            metadatas=[{"user_id": "user-operator", "tier": "corpus"}],
        )
        return ChromaSemanticService(client=client, default_collections=["decisions"])

    async def test_search_tags_private_and_corpus_results(
        self, two_tier_service, user_context
    ):
        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())
        results = await two_tier_service.search(alice, "cache", k=10)
        by_content = {d.page_content: d for d in results}
        assert by_content["alice private cache note"].metadata["tier"] == "private"
        assert by_content["shared corpus cache note"].metadata["tier"] == "corpus"

    async def test_search_non_admin_private_isolation_corpus_shared(
        self, two_tier_service, user_context
    ):
        """FALSIFIABLE BAR: user A's private vector is invisible to user
        B; the corpus vector is visible to both."""
        bob = replace(user_context, user_id="user-bob", is_admin=False, scopes=())
        results = await two_tier_service.search(bob, "cache", k=10)
        contents = [d.page_content for d in results]
        assert "alice private cache note" not in contents
        assert "shared corpus cache note" in contents

        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())
        alice_results = await two_tier_service.search(alice, "cache", k=10)
        alice_contents = [d.page_content for d in alice_results]
        assert "alice private cache note" in alice_contents
        assert "shared corpus cache note" in alice_contents

    async def test_search_page_relevance_tags_and_isolates(
        self, two_tier_service, user_context
    ):
        bob = replace(user_context, user_id="user-bob", is_admin=False, scopes=())
        page = await two_tier_service.search_page(bob, "cache", k=10)
        contents = [d.page_content for d in page.matches]
        assert "alice private cache note" not in contents
        assert "shared corpus cache note" in contents
        tiers = {d.metadata["tier"] for d in page.matches}
        assert tiers == {"corpus"}

    async def test_search_page_enumeration_tags_and_isolates(
        self, two_tier_service, user_context
    ):
        """The recency/id enumeration path (non-``query()``) gets the same
        dual-tier treatment as the relevance path."""
        bob = replace(user_context, user_id="user-bob", is_admin=False, scopes=())
        page = await two_tier_service.search_page(bob, "cache", k=10, sort="id")
        contents = [d.page_content for d in page.matches]
        assert "alice private cache note" not in contents
        assert "shared corpus cache note" in contents

    async def test_search_admin_sees_both_tiers(self, two_tier_service, user_context):
        # user_context fixture is the admin sentinel.
        results = await two_tier_service.search(user_context, "cache", k=10)
        contents = {d.page_content for d in results}
        assert "alice private cache note" in contents
        assert "shared corpus cache note" in contents

    async def test_falsifiability_neuter_private_where_leaks_across_users(
        self, two_tier_service, user_context, monkeypatch
    ):
        """Prove the private-tier `where` filter is load-bearing (not
        vacuous): if `_query_tier` is neutered to ignore `where`, bob's
        search MUST start returning alice's private row. Restoring the
        neuter (this test's teardown is implicit — monkeypatch reverts
        automatically) brings back isolation."""
        real_query_tier = ChromaSemanticService._query_tier

        async def _neutered_query_tier(
            self, physical_name, query_embedding, n_results, where, col_name, tier
        ):
            # Drop the where filter entirely — simulates the #383 guard
            # being removed.
            return await real_query_tier(
                self, physical_name, query_embedding, n_results, None, col_name, tier
            )

        monkeypatch.setattr(ChromaSemanticService, "_query_tier", _neutered_query_tier)
        bob = replace(user_context, user_id="user-bob", is_admin=False, scopes=())
        results = await two_tier_service.search(bob, "cache", k=10)
        contents = [d.page_content for d in results]
        assert "alice private cache note" in contents, (
            "neutering the where filter should leak alice's row to bob — "
            "if this assertion fails, the neuter didn't actually disable "
            "the isolation guard, so the guard's falsifiability is unproven"
        )


class TestUpsertTierWiring:
    """``upsert(..., tier=...)`` writes to the correct physical
    collection and stamps `metadata["tier"]`."""

    @pytest.fixture
    async def service(self):
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        return ChromaSemanticService(client=client, default_collections=["decisions"])

    async def test_default_tier_is_private(self, service, user_context):
        await service.upsert(user_context, "decisions", "doc-1", "hello")
        doc = await service.get_document(user_context, "decisions", "doc-1")
        assert doc is not None
        assert doc.metadata["tier"] == "private"
        assert doc.metadata["user_id"] == user_context.user_id

    async def test_corpus_tier_writes_to_corpus_physical_collection(
        self, service, user_context
    ):
        await service.upsert(
            user_context, "decisions", "doc-c", "shared body", tier="corpus"
        )
        # Visible via get_document (checks both physical collections).
        doc = await service.get_document(user_context, "decisions", "doc-c")
        assert doc is not None
        assert doc.metadata["tier"] == "corpus"
        # Physically landed in the corpus collection, not the private one.
        private_col = await service._client.get_or_create_collection(
            name="decisions_v2"
        )
        assert (await private_col.get(ids=["doc-c"]))["ids"] == []
        corpus_col = await service._client.get_or_create_collection(
            name="decisions_corpus_v2"
        )
        assert (await corpus_col.get(ids=["doc-c"]))["ids"] == ["doc-c"]

    async def test_corpus_write_visible_to_every_non_admin(self, service, user_context):
        await service.upsert(
            user_context, "decisions", "doc-c2", "shared body 2", tier="corpus"
        )
        other = replace(user_context, user_id="someone-else", is_admin=False, scopes=())
        doc = await service.get_document(other, "decisions", "doc-c2")
        assert doc is not None


class TestUpsertStampsFromContextUnconditionally:
    """ADR-062 Phase B (WU-B5 review fix, 2026-08-04) — ``upsert`` must
    stamp ``metadata["tier"]``/``metadata["user_id"]`` from the ``tier``
    parameter / ``user_context`` UNCONDITIONALLY, never honouring a
    caller-supplied value in ``metadata``.

    Reproduces the reviewer's finding directly against the REAL
    ``ChromaSemanticService`` (not ``MockSemanticService``, which does
    not enforce isolation by design): before this fix, ``upsert`` used
    ``dict.setdefault``, which SILENTLY HONOURED caller-supplied
    ``metadata["tier"]``/``metadata["user_id"]`` — a writer holding only
    ``memory:semantic:write`` (private-tier target, gated at the route
    by ``_require_corpus_scope`` only for the TOP-LEVEL ``tier``/
    ``?promote=`` signal) could smuggle ``metadata={"tier": "corpus"}``
    past that gate entirely, and the forged tag was then trusted by
    ``_tier_authorized`` on every subsequent read.

    FALSIFIABLE: revert the unconditional assignment in
    ``ChromaSemanticService.upsert`` back to ``dict.setdefault`` and
    every test below goes RED (the forged value survives)."""

    @pytest.fixture
    async def service(self):
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        return ChromaSemanticService(client=client, default_collections=["decisions"])

    async def test_forged_metadata_tier_corpus_does_not_stick_on_private_write(
        self, service, user_context
    ) -> None:
        """``tier="private"`` (the route's authorized target) wins over
        a caller-supplied ``metadata={"tier": "corpus"}`` — the exact
        top-level-vs-nested-metadata smuggling path the reviewer found."""
        await service.upsert(
            user_context,
            "decisions",
            "forge-tier",
            "attacker body",
            metadata={"tier": "corpus"},
            tier="private",
        )
        doc = await service.get_document(user_context, "decisions", "forge-tier")
        assert doc is not None
        assert doc.metadata["tier"] == "private", (
            f"forged metadata['tier'] survived: {doc.metadata}"
        )
        # Physically landed in the PRIVATE physical collection too — the
        # forgery didn't even smuggle the write TARGET, only the tag
        # (which is the actually-exploitable half via _tier_authorized).
        private_col = await service._client.get_or_create_collection(
            name="decisions_v2"
        )
        assert (await private_col.get(ids=["forge-tier"]))["ids"] == ["forge-tier"]

    async def test_forged_metadata_tier_corpus_is_unreadable_by_a_stranger(
        self, service, user_context
    ) -> None:
        """The end-to-end consequence: because the forged tag doesn't
        stick, a non-owner stranger's read is still denied (None) —
        BEFORE the fix this would have returned the document."""
        alice = replace(user_context, user_id="alice", is_admin=False, scopes=())
        await service.upsert(
            alice,
            "decisions",
            "forge-tier-2",
            "alice's private body",
            metadata={"tier": "corpus"},
            tier="private",
        )
        stranger = replace(user_context, user_id="bob", is_admin=False, scopes=())
        doc = await service.get_document(stranger, "decisions", "forge-tier-2")
        assert doc is None, (
            "forged metadata['tier']='corpus' leaked alice's private doc to bob"
        )

    async def test_forged_metadata_user_id_does_not_stick(
        self, service, user_context
    ) -> None:
        """A caller cannot attribute their write to another user by
        forging ``metadata["user_id"]``."""
        attacker = replace(user_context, user_id="attacker", is_admin=False, scopes=())
        await service.upsert(
            attacker,
            "decisions",
            "forge-user",
            "attacker body",
            metadata={"user_id": "victim"},
        )
        doc = await service.get_document(attacker, "decisions", "forge-user")
        assert doc is not None
        assert doc.metadata["user_id"] == "attacker", (
            f"forged metadata['user_id'] survived: {doc.metadata}"
        )

    async def test_forged_metadata_user_id_does_not_grant_the_forged_owner_access(
        self, service, user_context
    ) -> None:
        attacker = replace(user_context, user_id="attacker", is_admin=False, scopes=())
        await service.upsert(
            attacker,
            "decisions",
            "forge-user-2",
            "attacker body 2",
            metadata={"user_id": "victim"},
        )
        victim = replace(user_context, user_id="victim", is_admin=False, scopes=())
        doc = await service.get_document(victim, "decisions", "forge-user-2")
        assert doc is None, (
            "forged metadata['user_id']='victim' let 'victim' read attacker's "
            "private doc as if they owned it"
        )


class TestGetDocumentClosesHole:
    """§3 hole 1 — ``get_document`` used to be `del user_context`
    (fully unfiltered). Now: owner or corpus-tier or admin."""

    @pytest.fixture
    async def service(self):
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        return ChromaSemanticService(client=client, default_collections=["decisions"])

    async def _seed_private(self, service, user_context, owner: str) -> None:
        alice_like = replace(user_context, user_id=owner, is_admin=False, scopes=())
        await service.upsert(alice_like, "decisions", "priv-doc", "alice's secret")

    async def test_owner_can_read_own_private_doc(self, service, user_context):
        await self._seed_private(service, user_context, "user-alice")
        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())
        doc = await service.get_document(alice, "decisions", "priv-doc")
        assert doc is not None
        assert doc.page_content == "alice's secret"

    async def test_non_owner_gets_none_not_the_document(self, service, user_context):
        """FALSIFIABLE BAR: cross-user read returns None — the same
        signal as a genuine miss (no existence disclosure)."""
        await self._seed_private(service, user_context, "user-alice")
        bob = replace(user_context, user_id="user-bob", is_admin=False, scopes=())
        doc = await service.get_document(bob, "decisions", "priv-doc")
        assert doc is None

    async def test_admin_bypasses_ownership_check(self, service, user_context):
        await self._seed_private(service, user_context, "user-alice")
        # user_context fixture is the admin sentinel.
        doc = await service.get_document(user_context, "decisions", "priv-doc")
        assert doc is not None

    async def test_corpus_doc_readable_by_any_non_admin(self, service, user_context):
        await service.upsert(
            user_context, "decisions", "corpus-doc", "shared", tier="corpus"
        )
        bob = replace(user_context, user_id="user-bob", is_admin=False, scopes=())
        doc = await service.get_document(bob, "decisions", "corpus-doc")
        assert doc is not None

    async def test_falsifiability_neuter_tier_authorized_leaks_cross_user(
        self, service, user_context, monkeypatch
    ):
        """Prove the ownership check is load-bearing: neutering
        `_tier_authorized` to always return True must resurrect the
        cross-user leak this hole-closure fixes."""
        await self._seed_private(service, user_context, "user-alice")
        monkeypatch.setattr(
            ChromaSemanticService,
            "_tier_authorized",
            staticmethod(lambda meta, uc: True),
        )
        bob = replace(user_context, user_id="user-bob", is_admin=False, scopes=())
        doc = await service.get_document(bob, "decisions", "priv-doc")
        assert doc is not None, (
            "neutering _tier_authorized should resurrect the cross-user "
            "leak — if this assertion fails, the check wasn't actually "
            "load-bearing"
        )

    async def test_get_document_checks_corpus_when_absent_from_private(
        self, service, user_context
    ):
        """A doc_id that only exists in the corpus physical collection
        (never written to the private one) must still be found."""
        await service.upsert(
            user_context, "decisions", "corpus-only", "corpus body", tier="corpus"
        )
        other = replace(user_context, user_id="nobody", is_admin=False, scopes=())
        doc = await service.get_document(other, "decisions", "corpus-only")
        assert doc is not None
        assert doc.page_content == "corpus body"


class TestDeleteDocumentClosesHole:
    """§3 hole 1 — ``delete_document`` mirrors get_document's ownership
    check (previously `del user_context`, fully unfiltered)."""

    @pytest.fixture
    async def service(self):
        factory = MockChromaDBFactory()
        client = await factory.get_client()
        return ChromaSemanticService(client=client, default_collections=["decisions"])

    async def test_owner_can_delete_own_private_doc(self, service, user_context):
        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())
        await service.upsert(alice, "decisions", "priv-doc", "secret")
        assert await service.delete_document(alice, "decisions", "priv-doc") is True
        assert await service.get_document(alice, "decisions", "priv-doc") is None

    async def test_non_owner_delete_returns_false_and_does_not_delete(
        self, service, user_context
    ):
        """FALSIFIABLE BAR: a non-owner's delete attempt is a no-op —
        returns False, and the document survives."""
        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())
        await service.upsert(alice, "decisions", "priv-doc", "secret")
        bob = replace(user_context, user_id="user-bob", is_admin=False, scopes=())
        assert await service.delete_document(bob, "decisions", "priv-doc") is False
        # Still there — the delete must not have gone through.
        assert await service.get_document(alice, "decisions", "priv-doc") is not None

    async def test_admin_can_delete_any_users_private_doc(self, service, user_context):
        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())
        await service.upsert(alice, "decisions", "priv-doc", "secret")
        # user_context fixture is the admin sentinel.
        assert (
            await service.delete_document(user_context, "decisions", "priv-doc") is True
        )

    async def test_any_non_admin_can_delete_corpus_doc(self, service, user_context):
        await service.upsert(
            user_context, "decisions", "corpus-doc", "shared", tier="corpus"
        )
        bob = replace(user_context, user_id="user-bob", is_admin=False, scopes=())
        assert await service.delete_document(bob, "decisions", "corpus-doc") is True

    async def test_falsifiability_neuter_tier_authorized_allows_cross_user_delete(
        self, service, user_context, monkeypatch
    ):
        alice = replace(user_context, user_id="user-alice", is_admin=False, scopes=())
        await service.upsert(alice, "decisions", "priv-doc", "secret")
        monkeypatch.setattr(
            ChromaSemanticService,
            "_tier_authorized",
            staticmethod(lambda meta, uc: True),
        )
        bob = replace(user_context, user_id="user-bob", is_admin=False, scopes=())
        deleted = await service.delete_document(bob, "decisions", "priv-doc")
        assert deleted is True, (
            "neutering _tier_authorized should allow the cross-user delete "
            "this hole-closure prevents — if this assertion fails, the "
            "check wasn't actually load-bearing"
        )
