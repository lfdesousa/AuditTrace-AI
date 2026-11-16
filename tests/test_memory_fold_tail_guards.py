"""MEMORY-SERVER-SILENT-TRUNCATION ADDENDUM-1, WU-1 (gate r1 S2): the guards of
the stale-tail removal. The removal is authorized BEFORE the fold writes
anything, and it is audited once per fold."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from audittrace.routes import memory as m
from tests.memory_truncation_support import (
    USER,
    distinct_body,
    fold,
    memory_client,
    read_whole,
    upload,
)
from tests.memory_truncation_support_a1 import STEP, manifest, memory_audit_details

LONG = distinct_body(10 * STEP)
SHORT = "".join(f"<{i:06d}>\n" for i in range(3 * STEP // 9))


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


def _fold(client: TestClient, text: str) -> int:
    assert upload(client, "episodic", "doc.md", text).status_code == 200
    index = fold(client, "episodic", "doc.md", "decisions")
    assert index.status_code == 200, index.text
    count: int = index.json()["collections"]["decisions"]
    return count


class TestFoldTailGuards:
    def test_a_tail_the_caller_may_not_remove_refuses_the_fold_before_any_write(
        self, client: TestClient
    ) -> None:
        """Chunk 7's manifest row now belongs to the shared corpus (somebody
        else's). The shorter re-fold would remove chunk 7: it must be refused,
        and the document must still read as the FIRST fold, untouched."""
        from audittrace.services.memory_keys import physical_key

        _fold(client, LONG)
        key = m._semantic_key("decisions", m._doc_id("decisions", "doc.md", 7))
        rows = manifest()._rows
        del rows[("semantic", physical_key(USER, key))]
        asyncio.run(
            manifest().record_create(
                layer="semantic",
                key=key,
                title="doc.md",
                size_bytes=1,
                user_id="curator",
                tier="corpus",
                caller_can_write_shared=True,
            )
        )
        assert upload(client, "episodic", "doc.md", SHORT).status_code == 200

        refused = fold(client, "episodic", "doc.md", "decisions")

        assert refused.status_code == 403
        assert rows[("semantic", key)]["deleted_at_ms"] is None
        back = read_whole(client, "decisions", "doc.md")
        assert back.status_code == 200
        assert back.json()["content"] == LONG

    def test_a_fold_that_removes_a_tail_writes_one_audit_event(
        self, client: TestClient
    ) -> None:
        was = _fold(client, LONG)
        now = _fold(client, SHORT)

        removals = [
            d for d in memory_audit_details() if d.get("reason") == "refold_stale_tail"
        ]

        assert removals == [
            {
                "op": "delete",
                "layer": "semantic",
                "collection": "decisions",
                "key": "doc.md",
                "tier": removals[0]["tier"] if removals else None,
                "reason": "refold_stale_tail",
                "removed": was - now,
            }
        ]

    def test_a_fold_with_no_tail_writes_no_removal_event(
        self, client: TestClient
    ) -> None:
        _fold(client, SHORT)
        _fold(client, LONG)

        assert [
            d for d in memory_audit_details() if d.get("reason") == "refold_stale_tail"
        ] == []
