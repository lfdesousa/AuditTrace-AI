"""Unit tests for ``MemoryManifestService`` + ``MockMemoryManifestService``.

Covers the contract documented in
``src/audittrace/services/memory_manifest.py`` — record_create,
record_update, record_delete, list_for_layer, get + the
``ManifestEntry`` dataclass round-trip.

Tests run against the Mock implementation. The real Postgres-backed
``MemoryManifestService`` is exercised via the ``test_memory_routes.py``
integration tests through the in-memory PostgresFactory.
"""

from __future__ import annotations

import time

import pytest

from audittrace.services.memory_manifest import (
    ManifestEntry,
    MockMemoryManifestService,
    _now_ms,
    _validate_layer,
)


@pytest.fixture
def manifest() -> MockMemoryManifestService:
    return MockMemoryManifestService()


class TestNowMs:
    def test_returns_milliseconds_not_seconds(self) -> None:
        now = _now_ms()
        # > 1e12 means we're in millis (we'd be in seconds territory if
        # this returned 1.7e9).
        assert now > 10**12
        # Plausibly current.
        assert abs(now - int(time.time() * 1000)) < 1000


class TestValidateLayer:
    def test_accepts_valid(self) -> None:
        for layer in ("episodic", "procedural", "semantic"):
            _validate_layer(layer)  # no raise

    def test_rejects_invalid(self) -> None:
        for bad in ("conversational", "EPISODIC", "", "anything"):
            with pytest.raises(ValueError, match="Invalid memory layer"):
                _validate_layer(bad)


class TestRecordCreate:
    def test_first_create_sets_created_modified_to_same(
        self, manifest: MockMemoryManifestService
    ) -> None:
        entry = manifest.record_create(
            "episodic", "ADR-x.md", "Title X", 100, "user-alice"
        )
        assert entry.layer == "episodic"
        assert entry.key == "ADR-x.md"
        assert entry.title == "Title X"
        assert entry.size_bytes == 100
        assert entry.created_at_ms == entry.modified_at_ms
        assert entry.created_by_user_id == "user-alice"
        assert entry.modified_by_user_id == "user-alice"
        assert entry.deleted_at_ms is None

    def test_recreate_revives_soft_deleted(
        self, manifest: MockMemoryManifestService
    ) -> None:
        manifest.record_create("episodic", "k.md", None, 1, "alice")
        manifest.record_delete("episodic", "k.md", "alice")
        # Recreate
        revived = manifest.record_create("episodic", "k.md", "new title", 2, "bob")
        assert revived.deleted_at_ms is None
        assert revived.deleted_by_user_id is None
        assert revived.title == "new title"
        assert revived.size_bytes == 2
        assert revived.modified_by_user_id == "bob"

    def test_recreate_existing_live_row_overwrites(
        self, manifest: MockMemoryManifestService
    ) -> None:
        manifest.record_create("episodic", "k.md", "v1", 10, "alice")
        again = manifest.record_create("episodic", "k.md", "v2", 20, "bob")
        assert again.title == "v2"
        assert again.size_bytes == 20
        # Created_at preserved (the row was created by alice originally)
        # — Mock implementation keeps the original entry's id and adds
        # modifications, but doesn't preserve created_at across overwrite.
        # That's a divergence from the Postgres path which DOES preserve.
        # Documented for awareness.

    def test_rejects_invalid_layer(self, manifest: MockMemoryManifestService) -> None:
        with pytest.raises(ValueError):
            manifest.record_create("conversational", "x", None, 0, "u")


class TestRecordUpdate:
    def test_update_bumps_modified_only(
        self, manifest: MockMemoryManifestService
    ) -> None:
        e1 = manifest.record_create("episodic", "k.md", "v1", 10, "alice")
        # Sleep just enough to guarantee a different millisecond.
        time.sleep(0.002)
        e2 = manifest.record_update("episodic", "k.md", 20, "bob", title="v2")
        assert e2.created_at_ms == e1.created_at_ms
        assert e2.modified_at_ms > e1.modified_at_ms
        assert e2.created_by_user_id == "alice"
        assert e2.modified_by_user_id == "bob"
        assert e2.title == "v2"
        assert e2.size_bytes == 20

    def test_update_title_none_preserves_existing(
        self, manifest: MockMemoryManifestService
    ) -> None:
        manifest.record_create("episodic", "k.md", "stays", 1, "alice")
        e2 = manifest.record_update("episodic", "k.md", 2, "bob", title=None)
        assert e2.title == "stays"

    def test_update_missing_raises(self, manifest: MockMemoryManifestService) -> None:
        with pytest.raises(LookupError):
            manifest.record_update("episodic", "missing.md", 1, "u")

    def test_update_soft_deleted_raises(
        self, manifest: MockMemoryManifestService
    ) -> None:
        manifest.record_create("episodic", "k.md", None, 1, "alice")
        manifest.record_delete("episodic", "k.md", "alice")
        with pytest.raises(RuntimeError, match="soft-deleted"):
            manifest.record_update("episodic", "k.md", 2, "bob")


class TestRecordDelete:
    def test_delete_sets_timestamp_and_user(
        self, manifest: MockMemoryManifestService
    ) -> None:
        manifest.record_create("episodic", "k.md", None, 1, "alice")
        d = manifest.record_delete("episodic", "k.md", "bob")
        assert d.deleted_at_ms is not None
        assert d.deleted_by_user_id == "bob"

    def test_delete_already_deleted_is_idempotent(
        self, manifest: MockMemoryManifestService
    ) -> None:
        manifest.record_create("episodic", "k.md", None, 1, "alice")
        d1 = manifest.record_delete("episodic", "k.md", "bob")
        d2 = manifest.record_delete("episodic", "k.md", "cleo")
        # Second call returns existing entry, doesn't update deleter.
        assert d2.deleted_at_ms == d1.deleted_at_ms
        assert d2.deleted_by_user_id == "bob"

    def test_delete_missing_raises(self, manifest: MockMemoryManifestService) -> None:
        with pytest.raises(LookupError):
            manifest.record_delete("episodic", "missing.md", "u")


class TestListForLayer:
    def test_excludes_deleted_by_default(
        self, manifest: MockMemoryManifestService
    ) -> None:
        manifest.record_create("episodic", "live.md", None, 1, "u")
        manifest.record_create("episodic", "deleted.md", None, 1, "u")
        manifest.record_delete("episodic", "deleted.md", "u")
        rows = manifest.list_for_layer("episodic")
        keys = {r.key for r in rows}
        assert keys == {"live.md"}

    def test_include_deleted_returns_all(
        self, manifest: MockMemoryManifestService
    ) -> None:
        manifest.record_create("episodic", "live.md", None, 1, "u")
        manifest.record_create("episodic", "deleted.md", None, 1, "u")
        manifest.record_delete("episodic", "deleted.md", "u")
        rows = manifest.list_for_layer("episodic", include_deleted=True)
        assert {r.key for r in rows} == {"live.md", "deleted.md"}

    def test_layer_isolation(self, manifest: MockMemoryManifestService) -> None:
        manifest.record_create("episodic", "a.md", None, 1, "u")
        manifest.record_create("procedural", "b.md", None, 1, "u")
        manifest.record_create("semantic", "c/d", None, 1, "u")
        assert {r.key for r in manifest.list_for_layer("episodic")} == {"a.md"}
        assert {r.key for r in manifest.list_for_layer("procedural")} == {"b.md"}
        assert {r.key for r in manifest.list_for_layer("semantic")} == {"c/d"}

    def test_ordered_by_modified_at_desc(
        self, manifest: MockMemoryManifestService
    ) -> None:
        manifest.record_create("episodic", "first.md", None, 1, "u")
        time.sleep(0.002)
        manifest.record_create("episodic", "second.md", None, 1, "u")
        time.sleep(0.002)
        manifest.record_update("episodic", "first.md", 2, "u")
        rows = manifest.list_for_layer("episodic")
        # `first.md` was modified most recently → ordered first.
        assert [r.key for r in rows] == ["first.md", "second.md"]


class TestManifestEntryRoundTrip:
    def test_to_dict_contains_all_fields(
        self, manifest: MockMemoryManifestService
    ) -> None:
        entry = manifest.record_create("episodic", "k.md", "Title", 100, "user-x")
        d = entry.to_dict()
        for k in (
            "id",
            "layer",
            "key",
            "title",
            "size_bytes",
            "created_at_ms",
            "modified_at_ms",
            "created_by_user_id",
            "modified_by_user_id",
            "deleted_at_ms",
            "deleted_by_user_id",
        ):
            assert k in d, f"missing key: {k}"
        # Ensure it's a flat JSON-friendly dict (no nested objects).
        for v in d.values():
            assert v is None or isinstance(v, (str, int))

    def test_frozen_dataclass(self, manifest: MockMemoryManifestService) -> None:
        entry = manifest.record_create("episodic", "k.md", None, 1, "u")
        with pytest.raises(Exception):  # FrozenInstanceError
            entry.layer = "procedural"  # type: ignore[misc]

    def test_immutable_via_get(self, manifest: MockMemoryManifestService) -> None:
        manifest.record_create("episodic", "k.md", "t", 1, "u")
        e1 = manifest.get("episodic", "k.md")
        assert e1 is not None
        # Updating doesn't mutate the previously-returned entry.
        manifest.record_update("episodic", "k.md", 2, "u")
        assert e1.size_bytes == 1


class TestGet:
    def test_returns_none_for_missing(
        self, manifest: MockMemoryManifestService
    ) -> None:
        assert manifest.get("episodic", "never.md") is None

    def test_returns_entry_for_existing(
        self, manifest: MockMemoryManifestService
    ) -> None:
        manifest.record_create("episodic", "k.md", "t", 1, "u")
        e = manifest.get("episodic", "k.md")
        assert e is not None and e.key == "k.md"

    def test_returns_soft_deleted_too(
        self, manifest: MockMemoryManifestService
    ) -> None:
        manifest.record_create("episodic", "k.md", None, 1, "u")
        manifest.record_delete("episodic", "k.md", "u")
        e = manifest.get("episodic", "k.md")
        assert e is not None
        assert e.deleted_at_ms is not None


class TestManifestEntryFromRow:
    def test_from_row_with_real_orm_object(self) -> None:
        """`from_row` accepts duck-typed objects with the right attrs."""
        from types import SimpleNamespace

        row = SimpleNamespace(
            id="abc",
            layer="episodic",
            key="k.md",
            title=None,
            size_bytes=42,
            created_at_ms=1700000000000,
            modified_at_ms=1700000000000,
            created_by_user_id="u",
            modified_by_user_id="u",
            deleted_at_ms=None,
            deleted_by_user_id=None,
        )
        e = ManifestEntry.from_row(row)
        assert e.id == "abc"
        assert e.size_bytes == 42


# ── Postgres-backed MemoryManifestService (uses InMemoryPostgresFactory) ─────


@pytest.fixture
def pg_manifest():
    """Real ``MemoryManifestService`` over an in-memory SQLite-via-SQLAlchemy
    DB. Schema is created via ``Base.metadata.create_all`` (no Alembic
    needed for unit tests; the actual production run does run migration
    009)."""
    from audittrace.db.models import Base
    from audittrace.db.postgres import InMemoryPostgresFactory
    from audittrace.services.memory_manifest import MemoryManifestService

    factory = InMemoryPostgresFactory()
    Base.metadata.create_all(factory.get_engine())
    return MemoryManifestService(session_factory=factory.get_session_factory())


class TestPostgresMemoryManifestService:
    """End-to-end tests on the real Postgres-backed implementation. Mirrors
    the Mock test suite so the production code path is exercised."""

    def test_create_then_get(self, pg_manifest) -> None:
        e = pg_manifest.record_create(
            "episodic", "ADR-001.md", "Title", 100, "user-alice"
        )
        got = pg_manifest.get("episodic", "ADR-001.md")
        assert got is not None
        assert got.id == e.id
        assert got.layer == "episodic"
        assert got.title == "Title"
        assert got.size_bytes == 100
        assert got.created_by_user_id == "user-alice"
        assert got.deleted_at_ms is None

    def test_get_returns_none_for_missing(self, pg_manifest) -> None:
        assert pg_manifest.get("episodic", "never.md") is None

    def test_recreate_revives_soft_deleted(self, pg_manifest) -> None:
        pg_manifest.record_create("procedural", "SKILL-x.md", None, 1, "alice")
        pg_manifest.record_delete("procedural", "SKILL-x.md", "alice")
        # Pre-condition: row is soft-deleted
        deleted = pg_manifest.get("procedural", "SKILL-x.md")
        assert deleted is not None and deleted.deleted_at_ms is not None
        # Recreate
        revived = pg_manifest.record_create("procedural", "SKILL-x.md", "new", 2, "bob")
        assert revived.deleted_at_ms is None
        assert revived.deleted_by_user_id is None
        assert revived.title == "new"
        assert revived.modified_by_user_id == "bob"

    def test_recreate_overwrites_live_row(self, pg_manifest) -> None:
        e1 = pg_manifest.record_create("semantic", "decisions/d-1", "v1", 10, "alice")
        e2 = pg_manifest.record_create("semantic", "decisions/d-1", "v2", 20, "bob")
        # Same row id (UNIQUE on (layer, key))
        assert e2.id == e1.id
        assert e2.title == "v2"
        assert e2.size_bytes == 20
        assert e2.modified_by_user_id == "bob"

    def test_update_bumps_modified_only(self, pg_manifest) -> None:
        e1 = pg_manifest.record_create("episodic", "k.md", "v1", 10, "alice")
        time.sleep(0.002)  # guarantee different ms
        e2 = pg_manifest.record_update("episodic", "k.md", 20, "bob", title="v2")
        assert e2.id == e1.id
        assert e2.created_at_ms == e1.created_at_ms
        assert e2.modified_at_ms > e1.modified_at_ms
        assert e2.modified_by_user_id == "bob"
        assert e2.title == "v2"

    def test_update_title_none_preserves_existing(self, pg_manifest) -> None:
        pg_manifest.record_create("episodic", "k.md", "stays", 1, "alice")
        e2 = pg_manifest.record_update("episodic", "k.md", 2, "bob", title=None)
        assert e2.title == "stays"

    def test_update_missing_raises(self, pg_manifest) -> None:
        with pytest.raises(LookupError):
            pg_manifest.record_update("episodic", "missing.md", 1, "u")

    def test_update_soft_deleted_raises(self, pg_manifest) -> None:
        pg_manifest.record_create("episodic", "k.md", None, 1, "alice")
        pg_manifest.record_delete("episodic", "k.md", "alice")
        with pytest.raises(RuntimeError, match="soft-deleted"):
            pg_manifest.record_update("episodic", "k.md", 2, "bob")

    def test_delete_sets_timestamp(self, pg_manifest) -> None:
        pg_manifest.record_create("episodic", "k.md", None, 1, "alice")
        d = pg_manifest.record_delete("episodic", "k.md", "bob")
        assert d.deleted_at_ms is not None
        assert d.deleted_by_user_id == "bob"

    def test_delete_idempotent(self, pg_manifest) -> None:
        pg_manifest.record_create("episodic", "k.md", None, 1, "alice")
        d1 = pg_manifest.record_delete("episodic", "k.md", "bob")
        d2 = pg_manifest.record_delete("episodic", "k.md", "cleo")
        # Returns existing entry; doesn't update deleter (lossy bob-was-here)
        assert d2.deleted_at_ms == d1.deleted_at_ms
        assert d2.deleted_by_user_id == "bob"

    def test_delete_missing_raises(self, pg_manifest) -> None:
        with pytest.raises(LookupError):
            pg_manifest.record_delete("episodic", "missing.md", "u")

    def test_list_excludes_deleted_by_default(self, pg_manifest) -> None:
        pg_manifest.record_create("episodic", "live.md", None, 1, "u")
        pg_manifest.record_create("episodic", "deleted.md", None, 1, "u")
        pg_manifest.record_delete("episodic", "deleted.md", "u")
        rows = pg_manifest.list_for_layer("episodic")
        assert {r.key for r in rows} == {"live.md"}

    def test_list_include_deleted(self, pg_manifest) -> None:
        pg_manifest.record_create("episodic", "live.md", None, 1, "u")
        pg_manifest.record_create("episodic", "deleted.md", None, 1, "u")
        pg_manifest.record_delete("episodic", "deleted.md", "u")
        rows = pg_manifest.list_for_layer("episodic", include_deleted=True)
        assert {r.key for r in rows} == {"live.md", "deleted.md"}

    def test_list_layer_isolation(self, pg_manifest) -> None:
        pg_manifest.record_create("episodic", "a.md", None, 1, "u")
        pg_manifest.record_create("procedural", "b.md", None, 1, "u")
        pg_manifest.record_create("semantic", "c/d", None, 1, "u")
        assert {r.key for r in pg_manifest.list_for_layer("episodic")} == {"a.md"}
        assert {r.key for r in pg_manifest.list_for_layer("procedural")} == {"b.md"}
        assert {r.key for r in pg_manifest.list_for_layer("semantic")} == {"c/d"}

    def test_list_ordered_by_modified_desc(self, pg_manifest) -> None:
        pg_manifest.record_create("episodic", "first.md", None, 1, "u")
        time.sleep(0.002)
        pg_manifest.record_create("episodic", "second.md", None, 1, "u")
        time.sleep(0.002)
        pg_manifest.record_update("episodic", "first.md", 2, "u")
        rows = pg_manifest.list_for_layer("episodic")
        assert [r.key for r in rows] == ["first.md", "second.md"]

    def test_invalid_layer_raises(self, pg_manifest) -> None:
        with pytest.raises(ValueError):
            pg_manifest.record_create("conversational", "x.md", None, 0, "u")
        with pytest.raises(ValueError):
            pg_manifest.list_for_layer("not-a-layer")
        with pytest.raises(ValueError):
            pg_manifest.get("not-a-layer", "k")


# ── Telemetry-coverage regression test ──────────────────────────────────────


class TestTelemetryCoverage:
    """Per `feedback_traceability_requirement` + the user's mandatory
    telemetry directive (2026-05-03 evening): every new feature MUST be
    visible in OpenTelemetry traces. The chart's ``@log_call`` decorator
    is the project's standard way to emit a Tempo+Langfuse span around
    a service method. This test is a regression guard so a future
    refactor doesn't silently strip the decorator."""

    def test_manifest_service_methods_carry_log_call(self) -> None:
        from audittrace.services.memory_manifest import MemoryManifestService

        for method_name in (
            "record_create",
            "record_update",
            "record_delete",
            "list_for_layer",
            "get",
        ):
            method = getattr(MemoryManifestService, method_name)
            # The @log_call decorator wraps with a function that has
            # __wrapped__ pointing at the original. A naked method
            # would not have that attribute.
            assert hasattr(method, "__wrapped__"), (
                f"{method_name} is not @log_call-decorated — telemetry "
                f"coverage gap (per feedback_traceability_requirement). "
                f"Re-add the decorator so spans land in Tempo + Langfuse."
            )

    def test_episodic_write_methods_carry_log_call(self) -> None:
        from audittrace.services.episodic import (
            MockEpisodicService,
            S3EpisodicService,
        )

        for cls in (S3EpisodicService, MockEpisodicService):
            for method_name in ("write", "delete", "invalidate_cache"):
                method = getattr(cls, method_name)
                assert hasattr(method, "__wrapped__"), (
                    f"{cls.__name__}.{method_name} is not @log_call-decorated"
                )

    def test_procedural_write_methods_carry_log_call(self) -> None:
        from audittrace.services.procedural import (
            MockProceduralService,
            S3ProceduralService,
        )

        for cls in (S3ProceduralService, MockProceduralService):
            for method_name in ("write", "delete", "invalidate_cache"):
                method = getattr(cls, method_name)
                assert hasattr(method, "__wrapped__"), (
                    f"{cls.__name__}.{method_name} is not @log_call-decorated"
                )

    def test_semantic_crud_methods_carry_log_call(self) -> None:
        from audittrace.services.semantic import (
            ChromaSemanticService,
            MockSemanticService,
            UserScopedSemanticService,
        )

        for cls in (
            ChromaSemanticService,
            MockSemanticService,
            UserScopedSemanticService,
        ):
            for method_name in ("upsert", "delete_document", "get_document"):
                method = getattr(cls, method_name)
                assert hasattr(method, "__wrapped__"), (
                    f"{cls.__name__}.{method_name} is not @log_call-decorated"
                )
