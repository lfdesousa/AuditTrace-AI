"""Tests for services/index_worker.py — SPEC #387 Phase 1 (WU-2) auto-index
outbox worker.

Falsifiability anchor (the SPEC's Acceptance §4, guard #2 — Durability):
a test proving the worker sets ``indexed_at_ms`` ONLY on success and
leaves it NULL on a failure, so ``IndexJanitor`` re-drives it. The
injected ``indexer`` callable (mirrors ``ScanRequestPublisher``'s injected
``amqp_client``) isolates that outbox contract from the real
ChromaDB/MinIO/pymupdf pipeline, which is exercised separately in
``TestDefaultIndexer``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.routes.memory_upload import manifest as manifest_mod
from audittrace.services.index_worker import (
    IndexRequestEnvelope,
    IndexWorker,
    default_indexer,
)


def _envelope(scan_id: str = "scan-1") -> IndexRequestEnvelope:
    return IndexRequestEnvelope(
        scan_id=scan_id,
        key=f"episodic/papers/{scan_id}/paper.pdf",
        collection="ai_research_papers",
        user_id="alice",
        trace_id="trace-abc",
    )


async def _seed_clean_row(
    factory,
    *,
    scan_id: str,
    user_id: str = "alice",
    extraction_warnings: list[dict[str, object]] | None = None,
    index_attempts: int = 0,
) -> None:
    """Insert a scanned-clean manifest row with indexed_at_ms NULL — the
    exact state ScanVerdictConsumer leaves behind on a clean verdict.
    ``extraction_warnings`` / ``index_attempts`` simulate the pipeline
    manifest flush having already run for a prior failed attempt (SPEC
    #450 §"hinge": the flush commits before ``default_indexer`` returns
    ``ok=False``, so ``_record_failure`` always reads a populated row)."""
    async with factory() as session:
        row = await manifest_mod.insert_pending_scan(
            session,
            scan_id=scan_id,
            user_id=user_id,
            object_uri=f"s3://memory-shared/quarantine/{user_id}/{scan_id}/paper.pdf",
            object_sha256="0" * 64,
            size_bytes=42,
            title="paper.pdf",
            trace_id="trace-abc",
        )
        row.scan_status = "scanned_clean"
        row.key = f"s3://memory-shared/episodic/papers/{scan_id}/paper.pdf"
        row.indexed_at_ms = None
        row.extraction_warnings = extraction_warnings
        row.index_attempts = index_attempts
        await session.commit()


def _settings(index_max_attempts: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        aws_bucket="",
        object_storage_backend="minio",
        minio_shared_bucket="memory-shared",
        index_max_attempts=index_max_attempts,
    )


class TestIndexOneOutboxSemantics:
    @pytest.fixture
    async def factory(self):
        f = InMemoryPostgresFactory()
        await f.create_schema()
        return f.get_session_factory()

    async def test_success_marks_indexed_at_ms(self, factory) -> None:
        # SPEC #450 (c) — success still marks indexed_at_ms AND never
        # touches the new terminal columns, even when a prior failed
        # attempt left index_attempts > 0 on the row.
        await _seed_clean_row(factory, scan_id="scan-ok", index_attempts=2)
        indexer = AsyncMock(return_value=True)
        worker = IndexWorker(
            settings=_settings(),
            session_factory=factory,
            queue=asyncio.Queue(),
            indexer=indexer,
        )

        await worker._index_one(_envelope("scan-ok"))

        indexer.assert_awaited_once()
        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-ok")
        assert row is not None
        assert row.indexed_at_ms is not None
        assert row.indexed_at_ms > 0
        assert row.index_attempts == 2  # untouched by the success path
        assert row.index_failed_at_ms is None
        assert row.index_failure_code is None

    async def test_indexer_exception_leaves_indexed_at_ms_null(self, factory) -> None:
        # RED PROOF: an embed-server outage (or any indexer exception)
        # must NOT mark the row — that is precisely what leaves it for
        # IndexJanitor to re-drive. Reverting the try/except in
        # ``_index_one`` to let this propagate would crash the worker's
        # run loop instead of degrading gracefully to a retry.
        await _seed_clean_row(factory, scan_id="scan-fail")
        indexer = AsyncMock(side_effect=OSError("embed server unreachable"))
        worker = IndexWorker(
            settings=_settings(),
            session_factory=factory,
            queue=asyncio.Queue(),
            indexer=indexer,
        )

        await worker._index_one(_envelope("scan-fail"))

        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-fail")
        assert row is not None
        assert row.indexed_at_ms is None

    async def test_indexer_returns_false_leaves_indexed_at_ms_null(
        self, factory
    ) -> None:
        # A rejected object (corrupt PDF, bomb-defense) is a "success" at
        # the Python level (no exception) but the pipeline said "not
        # accepted" — the same outbox contract must apply: stay NULL.
        await _seed_clean_row(factory, scan_id="scan-rejected")
        indexer = AsyncMock(return_value=False)
        worker = IndexWorker(
            settings=_settings(),
            session_factory=factory,
            queue=asyncio.Queue(),
            indexer=indexer,
        )

        await worker._index_one(_envelope("scan-rejected"))

        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-rejected")
        assert row is not None
        assert row.indexed_at_ms is None

    async def test_mark_indexed_is_idempotent_under_double_call(self, factory) -> None:
        # WHERE indexed_at_ms IS NULL means a crash-restart race that
        # re-indexes the same scan_id doesn't clobber the first timestamp.
        await _seed_clean_row(factory, scan_id="scan-idem")
        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-idem")
            assert row is not None
            row.indexed_at_ms = 99999
            await session.commit()

        worker = IndexWorker(
            settings=_settings(),
            session_factory=factory,
            queue=asyncio.Queue(),
            indexer=AsyncMock(return_value=True),
        )
        await worker._index_one(_envelope("scan-idem"))

        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-idem")
        assert row is not None
        assert row.indexed_at_ms == 99999

    async def test_mark_indexed_db_failure_is_logged_not_raised(self) -> None:
        broken_factory = MagicMock(side_effect=RuntimeError("db down"))
        worker = IndexWorker(
            settings=_settings(),
            session_factory=broken_factory,
            queue=asyncio.Queue(),
            indexer=AsyncMock(return_value=True),
        )
        # Must not raise — a DB outage while marking is logged, never
        # crashes the worker's run loop.
        await worker._index_one(_envelope("scan-db-down"))


class TestRecordFailure:
    """SPEC #450 WU-3 — the terminal/dead-letter decision on a failed
    attempt. Falsifiability anchor: neutering the permanent-code branch,
    the attempt-cap branch, or the exclusion in ``_index_janitor.py``
    each has its own dedicated RED proof; these tests pin
    ``_record_failure``'s own column values."""

    @pytest.fixture
    async def factory(self):
        f = InMemoryPostgresFactory()
        await f.create_schema()
        return f.get_session_factory()

    async def test_permanent_code_dead_letters_on_attempt_one(self, factory) -> None:
        # (a) — a failure carrying a permanent structural code
        # dead-letters IMMEDIATELY, on the very first attempt; retrying
        # would be pure waste since the classifier raises the same code
        # every time.
        await _seed_clean_row(
            factory,
            scan_id="scan-perm",
            extraction_warnings=[{"code": "pdf_corrupted_structure", "page": None}],
        )
        indexer = AsyncMock(return_value=False)
        worker = IndexWorker(
            settings=_settings(),
            session_factory=factory,
            queue=asyncio.Queue(),
            indexer=indexer,
        )

        await worker._index_one(_envelope("scan-perm"))

        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-perm")
        assert row is not None
        assert row.index_attempts == 1
        assert row.index_failed_at_ms is not None
        assert row.index_failed_at_ms > 0
        assert row.index_failure_code == "pdf_corrupted_structure"
        # Dead-lettered, never marked "succeeded".
        assert row.indexed_at_ms is None

    async def test_permanent_xref_code_dead_letters_on_attempt_one(
        self, factory
    ) -> None:
        # The OTHER permanent code — proves the decision reads the
        # PERMANENT_INDEX_FAILURE_CODES set, not a hardcoded string.
        await _seed_clean_row(
            factory,
            scan_id="scan-xref",
            extraction_warnings=[{"code": "pdf_corrupted_xref", "page": None}],
        )
        worker = IndexWorker(
            settings=_settings(),
            session_factory=factory,
            queue=asyncio.Queue(),
            indexer=AsyncMock(return_value=False),
        )

        await worker._index_one(_envelope("scan-xref"))

        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-xref")
        assert row is not None
        assert row.index_failure_code == "pdf_corrupted_xref"
        assert row.index_failed_at_ms is not None

    async def test_non_permanent_failure_retries_then_hits_attempt_cap(
        self, factory
    ) -> None:
        # (b) — a failure whose extraction_warnings carry no permanent
        # code (e.g. a transient embed-server outage: no warning at
        # all) must NOT dead-letter until index_attempts reaches the
        # cap (5). Attempts 1-4: terminal columns stay NULL, the row
        # remains eligible for IndexJanitor re-enqueue. Attempt 5:
        # dead-lettered as "max_attempts_exceeded".
        await _seed_clean_row(factory, scan_id="scan-cap")
        worker = IndexWorker(
            settings=_settings(index_max_attempts=5),
            session_factory=factory,
            queue=asyncio.Queue(),
            indexer=AsyncMock(return_value=False),
        )

        for attempt in range(1, 5):
            await worker._index_one(_envelope("scan-cap"))
            async with factory() as session:
                row = await manifest_mod.get_by_scan_id(session, "scan-cap")
            assert row is not None
            assert row.index_attempts == attempt
            assert row.index_failed_at_ms is None, (
                f"attempt {attempt}/5 must still be retryable"
            )
            assert row.index_failure_code is None

        # 5th attempt: cap reached, dead-letter.
        await worker._index_one(_envelope("scan-cap"))
        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-cap")
        assert row is not None
        assert row.index_attempts == 5
        assert row.index_failed_at_ms is not None
        assert row.index_failure_code == "max_attempts_exceeded"

    async def test_non_permanent_warning_code_also_uses_attempt_cap(
        self, factory
    ) -> None:
        # A Tier-B warning code (e.g. "encrypted") is deliberately
        # EXCLUDED from PERMANENT_INDEX_FAILURE_CODES (it warns on a
        # possibly-successful index, not a futile one) — it must fall
        # through to the attempt-cap path, not dead-letter on attempt 1.
        await _seed_clean_row(
            factory,
            scan_id="scan-tier-b",
            extraction_warnings=[{"code": "encrypted", "page": None}],
        )
        worker = IndexWorker(
            settings=_settings(index_max_attempts=5),
            session_factory=factory,
            queue=asyncio.Queue(),
            indexer=AsyncMock(return_value=False),
        )

        await worker._index_one(_envelope("scan-tier-b"))

        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-tier-b")
        assert row is not None
        assert row.index_attempts == 1
        assert row.index_failed_at_ms is None
        assert row.index_failure_code is None

    async def test_record_failure_db_failure_is_logged_not_raised(self) -> None:
        broken_factory = MagicMock(side_effect=RuntimeError("db down"))
        worker = IndexWorker(
            settings=_settings(),
            session_factory=broken_factory,
            queue=asyncio.Queue(),
            indexer=AsyncMock(return_value=False),
        )
        # Must not raise — a DB outage while recording the failure is
        # logged, never crashes the worker's run loop.
        await worker._index_one(_envelope("scan-db-down"))

    async def test_record_failure_row_missing_is_logged_not_raised(
        self, factory
    ) -> None:
        # Envelope names a scan_id with no manifest row at all (e.g. a
        # race with a hard-delete) — must not raise.
        worker = IndexWorker(
            settings=_settings(),
            session_factory=factory,
            queue=asyncio.Queue(),
            indexer=AsyncMock(return_value=False),
        )
        await worker._index_one(_envelope("scan-does-not-exist"))


class TestDefaultIndexerRouting:
    async def test_non_pdf_collection_is_rejected_defensively(self) -> None:
        # The scan pipeline is PDF-only today (GAP-7 is explicitly out of
        # scope) — an envelope somehow routed to any other collection
        # must be rejected loudly rather than mis-indexed.
        env = IndexRequestEnvelope(
            scan_id="scan-x",
            key="episodic/notes.md",
            collection="semantic",
            user_id="alice",
            trace_id="t",
        )
        ok = await default_indexer(env, _settings())
        assert ok is False

    async def test_happy_path_wires_bucket_collection_and_outcomes(self) -> None:
        # Proves the real wiring: bucket selection, physical-collection
        # naming, get_or_create_collection, and that the pipeline's
        # per-object outcome list is what decides success/failure — not
        # just "no exception was raised".
        from unittest.mock import patch

        chroma_client = MagicMock()
        chroma_collection = MagicMock()
        chroma_client.get_or_create_collection = AsyncMock(
            return_value=chroma_collection
        )
        minio_client = MagicMock()
        manifest_service = MagicMock()

        async def _fake_index_pdf_objects(
            collection,
            minio,
            bucket,
            objects,
            col_name,
            *,
            category,
            layer_prefix,
            user_id,
            ingestion_ts_ms,
            manifest_service,
            outcomes=None,
            caller_can_write_shared=False,
        ):
            assert collection is chroma_collection
            assert minio is minio_client
            assert bucket == "memory-shared"
            assert objects == [
                {"key": "episodic/papers/scan-y/paper.pdf", "filename": "paper.pdf"}
            ]
            assert col_name == "ai_research_papers"
            assert category == "episodic"
            assert layer_prefix == "episodic/"
            assert user_id == "alice"
            # SPEC security-memory-manifest-tier-authz (2026-08-30): the
            # auto-index outbox worker is a SYSTEM writer — it always
            # passes ``caller_can_write_shared=True`` so a legitimate
            # re-index of an already-corpus-tier PDF row keeps working
            # (never gains a demote bypass — see ``default_indexer``'s
            # inline comment).
            assert caller_can_write_shared is True
            if outcomes is not None:
                outcomes.append(True)
            return 3

        with (
            patch("audittrace.dependencies.get_chromadb", return_value=chroma_client),
            patch(
                "audittrace.dependencies.get_memory_manifest_service",
                return_value=manifest_service,
            ),
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=minio_client,
            ),
            patch(
                "audittrace.routes.memory_pdf.pipeline._index_pdf_objects",
                _fake_index_pdf_objects,
            ),
        ):
            env = _envelope("scan-y")
            ok = await default_indexer(env, _settings())

        assert ok is True
        chroma_client.get_or_create_collection.assert_awaited_once_with(
            name="ai_research_papers_v2",
            embedding_function=None,
        )

    async def test_pipeline_rejection_surfaces_as_false(self) -> None:
        from unittest.mock import patch

        chroma_client = MagicMock()
        chroma_client.get_or_create_collection = AsyncMock(return_value=MagicMock())

        async def _fake_index_pdf_objects(*_args, outcomes=None, **_kwargs):
            if outcomes is not None:
                outcomes.append(False)
            return 0

        with (
            patch("audittrace.dependencies.get_chromadb", return_value=chroma_client),
            patch(
                "audittrace.dependencies.get_memory_manifest_service",
                return_value=MagicMock(),
            ),
            patch(
                "audittrace.routes.memory._get_minio_client",
                return_value=MagicMock(),
            ),
            patch(
                "audittrace.routes.memory_pdf.pipeline._index_pdf_objects",
                _fake_index_pdf_objects,
            ),
        ):
            ok = await default_indexer(_envelope("scan-z"), _settings())

        assert ok is False


class TestRunLoop:
    async def test_run_drains_queue_and_cancels_cleanly(self) -> None:
        _f = InMemoryPostgresFactory()
        await _f.create_schema()
        factory = _f.get_session_factory()
        await _seed_clean_row(factory, scan_id="scan-run")
        queue: asyncio.Queue[IndexRequestEnvelope] = asyncio.Queue()
        await queue.put(_envelope("scan-run"))

        indexer = AsyncMock(return_value=True)
        worker = IndexWorker(
            settings=_settings(),
            session_factory=factory,
            queue=queue,
            indexer=indexer,
        )
        task = asyncio.create_task(worker.run())
        await queue.join()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        indexer.assert_awaited_once()
        async with factory() as session:
            row = await manifest_mod.get_by_scan_id(session, "scan-run")
        assert row is not None
        assert row.indexed_at_ms is not None
