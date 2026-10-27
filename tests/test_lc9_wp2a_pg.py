"""LC-9 WP2a on REAL Postgres 16 (migration 034 applied, asyncpg driver).

Reuses the throwaway-container scaffolding of ``test_lc9_migration_034_pg``
(one container per session; skipped only where Docker is unavailable, exactly
like that module).

* S2 copy-forward order: the owner-keyed row and the legacy tombstone commit in
  ONE transaction (a fault before the commit leaves BOTH untouched and the
  legacy row still answering; the retry converges).
* A2: two simultaneous copy-forwards of one owner's legacy row -> exactly one
  winner, the loser gets ``ConcurrentManifestWriteError`` (SQLSTATE 23505 read
  off the real asyncpg exception), never a raw IntegrityError; a CHECK (23514)
  is NOT softened.
* A1: ``upsert_pdf_metadata`` over a foreign legacy private row is refused by
  the SERVICE (``CrossOwnerWriteError``), not by the CHECK.

Neuters: ``_commit_or_conflict`` returning a bare ``session.commit()`` ->
``test_simultaneous_copy_forward_one_winner_one_conflict`` RED;
``_is_unique_violation`` returning ``True`` -> ``test_check_violation_is_not_a_conflict``
RED; ``_pdf_private_row_foreign`` removed -> ``test_foreign_legacy_pdf_row_refused_by_service`` RED;
copy-forward split into two commits -> ``test_copy_forward_is_one_transaction`` RED.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace.services.memory_keys import (
    ConcurrentManifestWriteError,
    CrossOwnerWriteError,
)
from audittrace.services.memory_manifest import (
    MemoryManifestService,
    _is_unique_violation,
)
from tests.test_lc9_migration_034_pg import _ADMIN_URL, Schema, pg

A, B = "user-a", "user-b"


@pytest.fixture
def pgs() -> Iterator[Schema]:
    """A fresh schema on the shared throwaway postgres:16 (see the 034 module)."""
    assert _ADMIN_URL is not None
    s = Schema(_ADMIN_URL)
    try:
        yield s
    finally:
        s.drop()


def _svc(pgs: Schema) -> MemoryManifestService:
    return MemoryManifestService(async_sessionmaker(pgs.async_engine))


@pg
class TestCopyForwardOnPostgres:
    @pytest.mark.parametrize("verb", ["update", "create"])
    async def test_copy_forward_is_one_transaction(
        self, pgs: Schema, verb: str
    ) -> None:
        """Crash AFTER the legacy tombstone is staged and BEFORE the owner-keyed
        row is added (the insert raises): the single transaction rolls back, so
        the legacy row is still live and still answers. A copy-forward done in
        two commits would leave the tombstone behind with no new row."""
        pgs.insert(
            key="note.md", created_by_user_id=A, created_at_ms=11, modified_at_ms=11
        )
        pgs.migrate("upgrade")
        svc = _svc(pgs)
        real_add = AsyncSession.add
        calls = {"n": 0}

        def _crash_on_first_owner_row(self: AsyncSession, obj: Any, **kw: Any) -> None:
            if getattr(obj, "key", "").startswith(f"{A}/"):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("crash before the owner-keyed row")
            real_add(self, obj, **kw)

        async def _write() -> Any:
            if verb == "update":
                return await svc.record_update("episodic", "note.md", 5, A)
            return await svc.record_create("episodic", "note.md", "t", 5, A)

        with patch.object(AsyncSession, "add", _crash_on_first_owner_row):
            with pytest.raises(RuntimeError):
                await _write()
            # BOTH halves absent: the legacy row is live and still answers.
            assert pgs.row("note.md").deleted_at_ms is None
            assert pgs.row(f"{A}/note.md") is None
            entry = await svc.resolve_read("episodic", "note.md", _ctx(A))
            assert entry is not None and entry.storage_key == "note.md"
            out = await _write()  # the retry converges, both halves together
        assert out.storage_key == f"{A}/note.md" and out.created_at_ms == 11
        tomb, new = pgs.row("note.md"), pgs.row(f"{A}/note.md")
        assert tomb.deleted_by_user_id == A
        assert tomb.deleted_at_ms == new.modified_at_ms  # same transaction instant

    async def test_create_path_copy_forward_passes_the_check(self, pgs: Schema) -> None:
        pgs.insert(key="note.md", created_by_user_id=A, created_at_ms=11)
        pgs.migrate("upgrade")
        out = await _svc(pgs).record_create("episodic", "note.md", "t", 3, A)
        assert out.storage_key == f"{A}/note.md" and out.created_at_ms == 11
        assert pgs.row("note.md").deleted_by_user_id == A


def _ctx(sub: str) -> Any:
    from audittrace.identity import UserContext

    return UserContext(
        user_id=sub, username=sub, agent_type="test", scopes=(), is_admin=False
    )


@pg
class TestConcurrentCopyForwardOnPostgres:
    async def test_simultaneous_copy_forward_one_winner_one_conflict(
        self, pgs: Schema
    ) -> None:
        pgs.insert(key="note.md", created_by_user_id=A)
        pgs.migrate("upgrade")
        svc = _svc(pgs)
        barrier = asyncio.Barrier(2)
        real = svc._read_row

        async def _both_read_first(*a: Any, **k: Any) -> Any:
            row = await real(*a, **k)
            await barrier.wait()  # neither commits before both resolved
            return row

        with patch.object(svc, "_read_row", _both_read_first):
            results = await asyncio.gather(
                svc.record_update("episodic", "note.md", 1, A),
                svc.record_update("episodic", "note.md", 2, A),
                return_exceptions=True,
            )
        ok = [r for r in results if not isinstance(r, BaseException)]
        bad = [r for r in results if isinstance(r, BaseException)]
        assert len(ok) == 1 and len(bad) == 1, results
        assert isinstance(bad[0], ConcurrentManifestWriteError)
        assert not isinstance(bad[0], IntegrityError)
        # exactly one owner-keyed row, the legacy tombstoned once
        assert pgs.row(f"{A}/note.md") is not None
        assert pgs.row("note.md").deleted_by_user_id == A
        # the loser's retry converges on the winner's row
        retry = await svc.record_update("episodic", "note.md", 9, A)
        assert retry.storage_key == f"{A}/note.md" and retry.size_bytes == 9

    async def test_unique_violation_has_sqlstate_23505(self, pgs: Schema) -> None:
        pgs.migrate("upgrade")
        pgs.insert(key=f"{A}/dup.md", created_by_user_id=A)
        async with async_sessionmaker(pgs.async_engine)() as s:
            from audittrace.db.models import MemoryItem

            s.add(
                MemoryItem(
                    layer="episodic",
                    key=f"{A}/dup.md",
                    size_bytes=1,
                    created_at_ms=1,
                    modified_at_ms=1,
                    created_by_user_id=A,
                    modified_by_user_id=A,
                    tier="private",
                )
            )
            with pytest.raises(IntegrityError) as ei:
                await s.commit()
        assert _is_unique_violation(ei.value)

    async def test_check_violation_is_not_a_conflict(self, pgs: Schema) -> None:
        """The 034 CHECK (23514) stays a hard IntegrityError: never softened."""
        pgs.migrate("upgrade")
        from audittrace.db.models import MemoryItem

        async with async_sessionmaker(pgs.async_engine)() as s:
            s.add(
                MemoryItem(
                    layer="episodic",
                    key="bare.md",  # a bare PRIVATE key violates 034
                    size_bytes=1,
                    created_at_ms=1,
                    modified_at_ms=1,
                    created_by_user_id=A,
                    modified_by_user_id=A,
                    tier="private",
                )
            )
            with pytest.raises(IntegrityError) as ei:
                await s.commit()
        assert not _is_unique_violation(ei.value)


@pg
class TestPdfMetadataOnPostgres:
    async def test_foreign_legacy_pdf_row_refused_by_service(self, pgs: Schema) -> None:
        pgs.insert(key="scan.pdf", created_by_user_id=A)  # legacy private
        pgs.migrate("upgrade")
        svc = _svc(pgs)
        with pytest.raises(CrossOwnerWriteError):
            await svc.upsert_pdf_metadata(
                "episodic",
                "scan.pdf",
                user_id=B,
                size_bytes=1,
                page_count=1,
                signature_status="none",
                ocr_coverage_pct=None,
                attachment_count=0,
                form_field_count=0,
                extraction_warnings=[],
                document_sha256="0" * 64,
                caller_can_write_shared=True,
            )
        assert pgs.row("scan.pdf").modified_by_user_id == A
