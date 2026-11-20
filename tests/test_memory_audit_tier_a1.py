"""AUDIT-ROW-TIER-DEFAULT-CORPUS ADDENDUM-1, WU-1: RED tests (spec gate r1, S1 to S3).

Each test drives the REAL route and reads back, in insertion order, EVERY
memory-access row of its key as ``(op, tier, marker)``: a missing tombstone, an
extra row or a wrong tier all fail. Arranged state (a legacy bare-key row, a
lost race, a scan row, a chunk with no tier stamp, a second caller) is put on
the repo's own doubles. Builder: copy unchanged to
``tests/test_memory_audit_tier_a1.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from audittrace.services import memory_audit
from tests.memory_truncation_support import SCOPES, USER, memory_client, upload
from tests.memory_truncation_support_a1 import as_admin, chroma_collection, manifest

OTHER = "00000000-0000-0000-0000-00000000b002"
BOTH_TIERS = "private+corpus"
_MARKERS = ("reason", "copied_from", "manifest", "endpoint")


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


@contextmanager
def as_caller(
    client: TestClient, user_id: str, *extra_scopes: str, admin: bool = False
) -> Iterator[None]:
    """Another identity (its own sub and scopes) for the enclosed requests."""
    from audittrace.auth import require_user
    from audittrace.identity import UserContext

    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    previous = overrides[require_user]
    overrides[require_user] = lambda: UserContext(
        user_id=user_id,
        username=user_id,
        agent_type="test",
        scopes=(*SCOPES, *extra_scopes),
        is_admin=admin,
    )
    try:
        yield
    finally:
        overrides[require_user] = previous


def audit_rows(**match: Any) -> list[tuple[str, str, str | None]]:
    """``(op, tier, marker)`` of every memory-access row matching *match*, in the
    order the rows were written. ``marker`` is the first of ``reason``,
    ``copied_from``, ``manifest``, ``endpoint`` the row carries."""
    from sqlalchemy import select

    from audittrace.db.models import InteractionRecord
    from audittrace.dependencies import get_postgres_factory

    async def read() -> list[dict[str, Any]]:
        factory = get_postgres_factory().get_session_factory()
        async with factory() as db:
            found = await db.execute(
                select(InteractionRecord.error_detail)
                .where(
                    InteractionRecord.event_class
                    == memory_audit.MEMORY_AUDIT_EVENT_CLASS
                )
                .order_by(InteractionRecord.id)
            )
            return [json.loads(detail) for (detail,) in found.all()]

    out: list[tuple[str, str, str | None]] = []
    for detail in asyncio.run(read()):
        if all(detail.get(k) == v for k, v in match.items()):
            marker = next((str(detail[m]) for m in _MARKERS if m in detail), None)
            out.append((detail["op"], detail["tier"], marker))
    return out


def seed_legacy_row(layer: str, key: str, *, with_file: bool = True) -> None:
    """The caller's OWN pre-LC-9 bare-key private row (and its stored file)."""
    from audittrace import dependencies

    now = int(time.time() * 1000) - 5000
    manifest()._rows[(layer, key)] = {
        "id": str(uuid.uuid4()),
        "layer": layer,
        "key": key,
        "title": "legacy",
        "size_bytes": 3,
        "created_at_ms": now,
        "modified_at_ms": now,
        "created_by_user_id": USER,
        "modified_by_user_id": USER,
        "deleted_at_ms": None,
        "deleted_by_user_id": None,
        "tier": "private",
    }
    if with_file:
        dependencies.container._instances[layer].add_document(
            "legacy body", file=key, tier="private", user_id=USER
        )


def mcp_write(client: TestClient, tool: str, document_id: str) -> dict[str, Any]:
    answer = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": {"document_id": document_id, "text": "body"},
            },
        },
    )
    assert answer.status_code == 200, answer.text
    result: dict[str, Any] = answer.json()["result"]
    assert result["isError"] is False, result
    return result


def corpus_document(client: TestClient, document_id: str, owner: str = OTHER) -> None:
    """A corpus-tier semantic document written by *owner* (not the caller)."""
    with as_caller(client, owner, admin=True):
        made = client.post(
            "/memory/semantic",
            params={"promote": "corpus"},
            json={"collection": "decisions", "document_id": document_id, "text": "c"},
        )
    assert made.status_code == 200, made.text


class TestPromote:
    """AC-10 (gate neuter G2)."""

    def test_promote_over_a_legacy_row_records_private_on_both_rows(
        self, client: TestClient
    ) -> None:
        seed_legacy_row("episodic", "note.md")
        assert upload(client, "session", "note.md", "scratch").status_code == 200
        promoted = client.post("/memory/promote", json={"filename": "note.md"})
        assert promoted.status_code == 200, promoted.text
        assert audit_rows(layer="episodic", key="note.md") == [
            ("write", "private", "note.md"),
            ("delete", "private", "copy_forward"),
        ]


class TestMcpWrite:
    """AC-11 (gate neuters G8, G3)."""

    @pytest.mark.parametrize(
        ("tool", "collection"),
        [("write_decision", "decisions"), ("write_skill", "skills")],
    )
    def test_write_tools_record_private(
        self, client: TestClient, tool: str, collection: str
    ) -> None:
        mcp_write(client, tool, "doc-m")
        assert audit_rows(collection=collection, key="doc-m") == [
            ("write", "private", None)
        ]

    def test_copy_forward_records_private_on_both_rows(
        self, client: TestClient
    ) -> None:
        seed_legacy_row("semantic", "decisions/doc-m", with_file=False)
        mcp_write(client, "write_decision", "doc-m")
        assert audit_rows(collection="decisions", key="doc-m") == [
            ("write", "private", "decisions/doc-m"),
            ("delete", "private", "copy_forward"),
        ]


class TestConcurrentWrite:
    """AC-12 (gate neuter G5)."""

    @pytest.mark.parametrize("route", ["create", "upload"])
    def test_a_lost_race_records_the_requested_tier(
        self, client: TestClient, route: str
    ) -> None:
        from audittrace.services.memory_keys import ConcurrentManifestWriteError

        lost = AsyncMock(side_effect=ConcurrentManifestWriteError("lost the race"))
        with patch.object(type(manifest()), "record_create", lost):
            if route == "upload":
                answer = upload(client, "episodic", "race.md", "text")
            else:
                answer = client.post(
                    "/memory/episodic", json={"filename": "race.md", "content": "text"}
                )
        assert answer.status_code == 409
        assert audit_rows(key="race.md") == [("write", "private", "concurrent_write")]


class TestUploadStatus:
    """AC-13 (gate neuter G4, note N2)."""

    @pytest.mark.parametrize("tier", ["private", "corpus"])
    def test_status_read_records_the_scan_rows_tier(
        self, client: TestClient, tier: str
    ) -> None:
        from audittrace.dependencies import get_postgres_factory
        from audittrace.routes.memory_upload.manifest import (
            get_by_scan_id,
            insert_pending_scan,
        )

        async def seed() -> str:
            factory = get_postgres_factory().get_session_factory()
            async with factory() as session:
                await insert_pending_scan(
                    session,
                    scan_id="scan-1",
                    user_id=USER,
                    object_uri=f"s3://memory-shared/quarantine/{USER}/scan-1/x.pdf",
                    object_sha256="0" * 64,
                    size_bytes=42,
                    title="x.pdf",
                    trace_id="trace-abc",
                )
                row = await get_by_scan_id(session, "scan-1")
                assert row is not None
                default = row.tier
                row.tier = tier
                await session.commit()
                return default

        assert asyncio.run(seed()) == "corpus"  # the server default of a scan row
        status = client.get("/memory/upload/status", params={"scan_id": "scan-1"})
        assert status.status_code == 200, status.text
        assert "tier" not in status.json()  # the response contract is unchanged
        assert audit_rows(key="scan-1") == [("read", tier, "upload_status")]


@pytest.mark.parametrize("layer", ["episodic", "procedural"])
class TestCopyForward:
    """AC-14 (gate neuter G7): both rows ``emit_write_audits`` writes."""

    def test_update_of_a_legacy_row_records_private_on_both_rows(
        self, client: TestClient, layer: str
    ) -> None:
        seed_legacy_row(layer, "note.md")
        put = client.put(f"/memory/{layer}/note.md", json={"content": "v2"})
        assert put.status_code == 200, put.text
        assert audit_rows(layer=layer, key="note.md") == [
            ("write", "private", "note.md"),
            ("delete", "private", "copy_forward"),
        ]

    def test_create_over_a_legacy_row_records_private_on_both_rows(
        self, client: TestClient, layer: str
    ) -> None:
        seed_legacy_row(layer, "note.md")
        made = client.post(
            f"/memory/{layer}", json={"filename": "note.md", "content": "v2"}
        )
        assert made.status_code == 200, made.text
        assert audit_rows(layer=layer, key="note.md") == [
            ("write", "private", "note.md"),
            ("delete", "private", "copy_forward"),
        ]


class TestSemanticListTier:
    """S1: a list names the tiers it was CLEARED to enumerate."""

    def _list(self, client: TestClient, **params: str) -> list[dict[str, Any]]:
        listing = client.get("/memory/semantic", params=params)
        assert listing.status_code == 200, listing.text
        items: list[dict[str, Any]] = listing.json()["items"]
        return items

    def test_without_the_corpus_scope_the_list_records_private(
        self, client: TestClient
    ) -> None:
        corpus_document(client, "doc-c")
        items = self._list(client, collection="decisions")
        assert [i["tier"] for i in items] == []  # the gate dropped the corpus row
        assert audit_rows(op="list") == [("list", "private", None)]

    def test_with_the_corpus_scope_the_list_records_both(
        self, client: TestClient
    ) -> None:
        corpus_document(client, "doc-c")
        with as_caller(client, USER, "memory:corpus:decisions:read"):
            items = self._list(client, collection="decisions")
        assert [i["tier"] for i in items] == ["corpus"]
        assert audit_rows(op="list") == [("list", BOTH_TIERS, None)]

    def test_the_scope_of_another_collection_does_not_count(
        self, client: TestClient
    ) -> None:
        corpus_document(client, "doc-c")
        with as_caller(client, USER, "memory:corpus:skills:read"):
            items = self._list(client, collection="decisions")
        assert items == []
        assert audit_rows(op="list") == [("list", "private", None)]

    def test_a_list_over_every_collection_needs_one_corpus_scope(
        self, client: TestClient
    ) -> None:
        self._list(client)
        with as_caller(client, USER, "memory:corpus:skills:read"):
            self._list(client)
        assert audit_rows(op="list") == [
            ("list", "private", None),
            ("list", BOTH_TIERS, None),
        ]

    def test_an_admin_list_records_both(self, client: TestClient) -> None:
        with as_admin(client):
            self._list(client, collection="decisions")
        assert audit_rows(op="list") == [("list", BOTH_TIERS, None)]


class TestReadTier:
    """S3: the tier a read was authorized under is the stored stamp; a
    document with NO stamp is reachable by its owner only, so it is private."""

    @pytest.mark.parametrize(
        ("metadata", "tier"),
        [
            ({}, "private"),
            ({"tier": "private"}, "private"),
            ({"tier": "corpus"}, "corpus"),
        ],
    )
    def test_read_tier(self, metadata: dict[str, Any], tier: str) -> None:
        assert memory_audit.read_tier(metadata) == tier

    def test_a_document_with_no_stamp_is_owner_only_and_records_private(
        self, client: TestClient
    ) -> None:
        asyncio.run(
            chroma_collection("decisions").upsert(
                ids=["doc-s"],
                documents=["body"],
                metadatas=[{"user_id": USER, "source": "legacy"}],
                embeddings=[[0.1, 0.2, 0.3]],
            )
        )
        with as_caller(client, OTHER, "memory:corpus:decisions:read"):
            foreign = client.get("/memory/semantic/decisions/doc-s")
        assert foreign.status_code == 404  # not admitted as corpus
        own = client.get("/memory/semantic/decisions/doc-s")
        assert own.status_code == 200, own.text
        assert "tier" not in own.json()["metadata"]
        assert audit_rows(key="doc-s") == [("read", "private", None)]
