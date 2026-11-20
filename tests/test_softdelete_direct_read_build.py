"""SOFTDELETE-DIRECT-READ: the cases the ratified RED files name as "not covered;
the builder adds them" (ADDENDUM-1 A4, ADDENDUM-2 A2.3), at the route level.

Same harness and doubles as ``tests/test_softdelete_direct_read_a1.py``. The
service-level cases (``resolve_read_many`` against ``resolve_read``, the one
transaction of ``record_delete_many``, the helpers of ``memory_tombstone``) are
in ``tests/test_memory_tombstone_unit.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from audittrace.auth import require_user
from audittrace.identity import UserContext
from audittrace.routes import memory as m
from tests import test_softdelete_direct_read_a1 as a1
from tests.memory_truncation_support import (
    SCOPES,
    USER,
    _object_store,
    distinct_body,
    read_whole,
)
from tests.memory_truncation_support_a1 import (
    STEP,
    as_admin,
    manifest,
    memory_audit_details,
)
from tests.test_softdelete_direct_read_a1 import (
    CANARY,
    SOURCES,
    _delete_source,
    _fold_source,
    _same_reply,
    _tool,
)
from tests.test_softdelete_direct_read_a2 import ADMIN_REFOLD_409, _index_writes

client = a1.client
tool_cache = a1.tool_cache
_real_read_tools = a1._real_read_tools

CORPUS_WRITE = ("memory:corpus:decisions:write",)


class TestTheCascadeOrder:
    """B8: the folds are tombstoned BEFORE the source row, and a failure
    anywhere leaves nothing reported deleted."""

    @pytest.mark.parametrize("layer", sorted(SOURCES))
    def test_a_failing_cascade_leaves_the_source_and_its_fold_live(
        self, client: TestClient, layer: str
    ) -> None:
        name, collection, _tool_name, _detail = SOURCES[layer]
        ids = _fold_source(client, layer, collection)

        with (
            patch(
                "audittrace.routes.memory_delete_cascade.tombstone_fold_copies",
                side_effect=RuntimeError("manifest down"),
            ),
            pytest.raises(RuntimeError, match="manifest down"),
        ):
            client.delete(f"/memory/{layer}/{name}")

        # nothing is tombstoned: not the source, not a chunk of its fold
        assert client.get(f"/memory/{layer}/{name}").status_code == 200
        assert client.get(f"/memory/semantic/{collection}/{ids[0]}").status_code == 200
        assert read_whole(client, collection, name).status_code == 200
        assert not [d for d in memory_audit_details() if d.get("op") == "delete"]


class TestDeletingASourceThatNeverExisted:
    """No manifest row, so nothing was folded under it: the route's own 404 and
    no cascade row."""

    @pytest.mark.parametrize("layer", sorted(SOURCES))
    def test_the_delete_is_the_routes_404_and_writes_no_row(
        self, client: TestClient, layer: str
    ) -> None:
        gone = client.delete(f"/memory/{layer}/never-written.md")

        assert gone.status_code == 404
        assert not [d for d in memory_audit_details() if d.get("op") == "delete"]


class TestTheAdminReadsTheToolDocument:
    """AC-6 for the tools (A4 "not covered"): the admin's ``read_decision`` /
    ``read_skill`` of a tombstone is the content; the owner's is not_found."""

    @pytest.mark.parametrize("layer", sorted(SOURCES))
    def test_admin_tool_read_of_a_tombstone_returns_the_content(
        self, client: TestClient, layer: str
    ) -> None:
        name, collection, tool_name, _detail = SOURCES[layer]
        _fold_source(client, layer, collection)
        _delete_source(client, layer)

        owner = _tool(client, tool_name, {"file": name}, sid="tool-owner")
        with as_admin(client):
            admin = _tool(client, tool_name, {"file": name}, sid="tool-admin")

        assert owner["payload"]["error"] == "not_found"
        assert CANARY not in owner["raw"]
        assert admin["isError"] is False
        assert CANARY in admin["payload"]["content"]


class TestCorpusSource:
    """B21 and B20 on a corpus-tier source (A2.3 "not covered")."""

    def _corpus_source(self, client: TestClient) -> bytes:
        """A corpus-tier ``episodic/doc.md`` row (as the admin), folded into
        ``decisions`` through the legacy corpus key; the object's bytes."""
        text = CANARY + "\n" + distinct_body(5 * STEP)
        asyncio.run(
            manifest().record_create(
                "episodic",
                "doc.md",
                "doc",
                len(text),
                USER,
                "corpus",
                caller_can_write_shared=True,
            )
        )
        with as_admin(client):
            assert self._fold(client, text.encode()).status_code == 200
        return text.encode()

    @staticmethod
    def _fold(client: TestClient, stored: bytes | None) -> Any:
        store = _object_store({} if stored is None else {"episodic/doc.md": stored})
        with patch.object(m, "_get_minio_client", return_value=store):
            return client.post(
                "/memory/index",
                params={"file": "episodic/doc.md", "collections": "decisions"},
            )

    def test_a_deleted_corpus_source_is_not_folded_again(
        self, client: TestClient
    ) -> None:
        stored = self._corpus_source(client)
        with as_admin(client):
            assert client.delete("/memory/episodic/doc.md").status_code == 200
        writes_before = len(_index_writes())

        with as_admin(client):
            admin = self._fold(client, stored)
        corpus_writer = UserContext(
            user_id=USER,
            username=USER,
            agent_type="test",
            scopes=(*SCOPES, *CORPUS_WRITE),
            is_admin=False,
        )
        app: Any = client.app
        app.dependency_overrides[require_user] = lambda: corpus_writer
        never = self._fold(client, None)
        plain = self._fold(client, stored)

        assert admin.status_code == 409 and admin.json() == ADMIN_REFOLD_409
        assert plain.status_code == never.status_code == 422
        assert plain.content == never.content
        # every attempt is audited, none wrote a chunk
        attempts = _index_writes()[writes_before:]
        assert len(attempts) == 3
        assert {a["total_chunks"] for a in attempts} == {0}

    def test_the_cascade_row_of_a_corpus_source_says_corpus(
        self, client: TestClient
    ) -> None:
        self._corpus_source(client)

        with as_admin(client):
            deleted = client.delete("/memory/episodic/doc.md")

        assert deleted.status_code == 200, deleted.text
        cascade = [
            d
            for d in memory_audit_details()
            if d.get("cascade_from") == "episodic/doc.md"
        ]
        assert len(cascade) == 1
        assert cascade[0]["tier"] == "corpus"
        assert cascade[0]["layer"] == "semantic"
        assert cascade[0]["collection"] == "decisions"
        assert cascade[0]["chunks"] >= 4 and cascade[0]["hard"] is False


class TestTheChunkDeleteRowCountsTheDocument:
    """B17 with B16's count: a SOFT delete of one chunk reports the document."""

    def test_the_soft_delete_row_counts_every_chunk_and_hides_the_siblings(
        self, client: TestClient
    ) -> None:
        ids = _fold_source(client, "episodic", "decisions", chunks=6)
        deleted = client.delete(f"/memory/semantic/decisions/{ids[2]}")
        assert deleted.status_code == 200

        row = [d for d in memory_audit_details() if d.get("op") == "delete"][-1]
        assert (row["key"], row["hard"], row["chunks"]) == (ids[2], False, len(ids))
        never = client.get("/memory/semantic/decisions/0000000000000000")
        _same_reply(client.get(f"/memory/semantic/decisions/{ids[4]}"), never)
