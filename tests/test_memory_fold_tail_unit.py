"""MEMORY-SERVER-SILENT-TRUNCATION: the stale-tail unit, called directly.

The route-level cases are in ``test_memory_fold_tail_guards.py`` and
``test_memory_blank_refold_clears_the_document.py``. Here: the paths a route
never takes (no manifest service), the audit that cannot be written, and the
rows the tail must NOT take (a legacy row that is somebody else's, or not
private)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from audittrace.routes import memory as m
from audittrace.routes.memory_fold_tail import StaleTail, find_stale_tail
from tests.memory_truncation_support import memory_client
from tests.memory_truncation_support_a1 import STUB_VECTOR, caller, chroma_collection


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


def _tail(count: int = 2) -> StaleTail:
    rows = tuple(
        (m._doc_id("decisions", "doc.md", i), f"owner__{i}") for i in range(count)
    )
    return StaleTail("decisions", "doc.md", rows)


class TestStaleTailUnit:
    def test_without_a_manifest_service_nothing_is_authorized_or_tombstoned(
        self,
    ) -> None:
        collection = AsyncMock()
        audit = AsyncMock()

        async def run() -> int:
            await _tail().authorize(None, caller(), "private")
            with patch(
                "audittrace.routes.memory_fold_tail.emit_memory_audit_event", audit
            ):
                return await _tail().drop(
                    collection, None, caller(), caller_can_write_shared=False
                )

        assert asyncio.run(run()) == 2
        collection.delete.assert_awaited_once_with(ids=["owner__0", "owner__1"])
        assert audit.await_count == 1

    def test_an_empty_tail_removes_nothing_and_audits_nothing(self) -> None:
        collection = AsyncMock()
        audit = AsyncMock()

        async def run() -> int:
            with patch(
                "audittrace.routes.memory_fold_tail.emit_memory_audit_event", audit
            ):
                return await StaleTail("decisions", "doc.md", ()).drop(
                    collection, None, caller(), caller_can_write_shared=False
                )

        assert asyncio.run(run()) == 0
        collection.delete.assert_not_awaited()
        audit.assert_not_awaited()

    def test_an_audit_that_cannot_be_written_is_a_500_after_the_removal(self) -> None:
        collection = AsyncMock()
        audit = AsyncMock(side_effect=RuntimeError("audit store down"))

        async def run() -> None:
            with patch(
                "audittrace.routes.memory_fold_tail.emit_memory_audit_event", audit
            ):
                await _tail().drop(
                    collection, None, caller(), caller_can_write_shared=False
                )

        with pytest.raises(HTTPException) as raised:
            asyncio.run(run())

        assert raised.value.status_code == 500
        assert raised.value.detail == (
            "memory audit-write failed — mutation not confirmed audited: audit store down"
        )

    def test_a_legacy_row_that_is_not_the_callers_own_private_row_is_not_in_the_tail(
        self, client: TestClient
    ) -> None:
        """Three bare-id rows at the chunk positions past the kept ones: one the
        caller's own and private (taken), one another owner's (left), one the
        caller's but corpus-tier (left)."""
        collection = chroma_collection("decisions")
        ids = [m._doc_id("decisions", "doc.md", i) for i in (1, 2, 3)]
        owners = [
            ("00000000-0000-0000-0000-00000000a001", "private"),
            ("someone-else", "private"),
            ("00000000-0000-0000-0000-00000000a001", "corpus"),
        ]
        asyncio.run(
            collection.upsert(
                ids=ids,
                documents=["a", "b", "c"],
                metadatas=[
                    {"source": "doc.md", "user_id": owner, "tier": tier}
                    for owner, tier in owners
                ],
                embeddings=[STUB_VECTOR for _ in ids],
            )
        )

        tail: Any = asyncio.run(
            find_stale_tail(
                collection, caller(), "private", "decisions", "doc.md", kept=1
            )
        )

        assert [logical for logical, _ in tail.rows] == [ids[0]]
