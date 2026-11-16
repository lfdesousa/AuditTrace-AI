"""MEMORY-SERVER-SILENT-TRUNCATION ADDENDUM-1, WU-3 (gate r1 S5): the statuses
``POST /memory/upload`` gains by recording what it stored, each with its effect.

The fleet's ``bru_memory.py log`` writes every record through this route: any
status but 200 is its exit 4 (server/step failure)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from audittrace.routes import memory as m
from tests.memory_truncation_support import memory_client, upload
from tests.memory_truncation_support_a1 import manifest

TEXT = "# note\n\nbody\n"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


class TestMemoryUploadRefusals:
    def test_403_when_the_name_is_a_shared_corpus_document_and_nothing_is_stored(
        self, client: TestClient
    ) -> None:
        asyncio.run(
            manifest().record_create(
                layer="episodic",
                key="note.md",
                title=None,
                size_bytes=1,
                user_id="curator",
                tier="corpus",
                caller_can_write_shared=True,
            )
        )

        refused = upload(client, "episodic", "note.md", TEXT)

        assert refused.status_code == 403
        assert client.get("/memory/episodic/note.md").status_code == 404

    @pytest.mark.parametrize("route", ["upload", "create"])
    def test_409_when_a_concurrent_write_wins_the_object_is_overwritten_and_audited(
        self, client: TestClient, route: str
    ) -> None:
        """The content write happens before the manifest row. When the row is
        lost to a concurrent writer the route answers 409, the object IS the new
        text, and that write has its audit row (marked as such)."""
        from audittrace.services.memory_keys import ConcurrentManifestWriteError
        from tests.memory_truncation_support_a1 import memory_audit_details

        assert upload(client, "episodic", "note.md", TEXT).status_code == 200
        before = len(memory_audit_details())
        newer = TEXT + "newer\n"
        lost = AsyncMock(side_effect=ConcurrentManifestWriteError("lost the race"))
        with patch.object(type(manifest()), "record_create", lost):
            if route == "upload":
                answer = upload(client, "episodic", "note.md", newer)
            else:
                answer = client.post(
                    "/memory/episodic", json={"filename": "note.md", "content": newer}
                )

        assert answer.status_code == 409
        added = memory_audit_details()[before:]
        assert client.get("/memory/episodic/note.md").json()["content"] == newer
        assert [(d["op"], d["key"], d.get("manifest")) for d in added] == [
            ("write", "note.md", "concurrent_write")
        ]

    def test_500_when_the_write_audit_cannot_be_written(
        self, client: TestClient
    ) -> None:
        async def down(**_: Any) -> None:
            raise RuntimeError("audit store down")

        with patch.object(m, "emit_memory_audit_event", down):
            answer = upload(client, "episodic", "note.md", TEXT)

        assert answer.status_code == 500
        assert "memory audit-write failed" in answer.json()["detail"]

    def test_200_twice_for_the_same_name_the_helpers_reseed_path(
        self, client: TestClient
    ) -> None:
        first = upload(client, "episodic", "note.md", TEXT)
        second = upload(client, "episodic", "note.md", TEXT + "more\n")

        assert (first.status_code, second.status_code) == (200, 200)
        read = client.get("/memory/episodic/note.md").json()
        assert read["content"] == TEXT + "more\n"
        assert read["manifest"]["size_bytes"] == len((TEXT + "more\n").encode())

    def test_the_success_body_is_unchanged(self, client: TestClient) -> None:
        answer = upload(client, "episodic", "note.md", TEXT)

        assert answer.status_code == 200
        assert sorted(answer.json()) == [
            "bucket",
            "key",
            "size_bytes",
            "status",
            "tier",
        ]
