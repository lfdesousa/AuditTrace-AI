"""AUDIT-ROW-TIER-DEFAULT-CORPUS: RED tests (spec 2026-11-20).

Every memory_access audit row names the tier the operation ran under. Each test
drives the REAL HTTP route and reads the row's ``detail`` back; it asserts the
EXACT tier string. The private-tier cells are RED on main a1945364 (the emitter
default ``corpus``); the corpus-tier cells are the controls that stop a fix
from hard-coding ``private``. Builder: copy to
``tests/test_memory_audit_tier.py`` unchanged.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from audittrace.routes.memory_promote import _emit_promote_audit
from audittrace.routes.memory_write_audit import emit_write_audit, emit_write_audits
from audittrace.services.memory_audit import (
    emit_memory_audit_event,
    emit_memory_audit_event_background,
    schedule_read_audit,
)
from tests.memory_truncation_support import (
    corpus_fold,
    fold,
    memory_client,
    upload,
)
from tests.memory_truncation_support_a1 import as_admin, caller, memory_audit_details

BOTH_TIERS = "private+corpus"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


def _tiers(**match: Any) -> list[str]:
    """The ``tier`` of every memory-access row whose detail matches *match*."""
    return [
        d["tier"]
        for d in memory_audit_details()
        if all(d.get(k) == v for k, v in match.items())
    ]


def _seed_corpus_file(layer: str, filename: str) -> None:
    """A corpus-tier document of a file layer, on the repo's own double."""
    from audittrace import dependencies

    dependencies.container._instances[layer].add_document(
        "corpus body", file=filename, tier="corpus"
    )


def _create_file(client: TestClient, layer: str, filename: str) -> None:
    made = client.post(f"/memory/{layer}", json={"filename": filename, "content": "v1"})
    assert made.status_code == 200, made.text


def _create_semantic(client: TestClient, doc: str, *, corpus: bool = False) -> None:
    made = client.post(
        "/memory/semantic",
        params={"promote": "corpus"} if corpus else {},
        json={"collection": "decisions", "document_id": doc, "text": "body"},
    )
    assert made.status_code == 200, made.text


@pytest.mark.parametrize("layer", ["episodic", "procedural"])
class TestFileLayerPrivate:
    def test_create_records_private(self, client: TestClient, layer: str) -> None:
        _create_file(client, layer, "ADR-p.md")
        assert _tiers(op="write", layer=layer, key="ADR-p.md") == ["private"]

    def test_upload_records_private(self, client: TestClient, layer: str) -> None:
        assert upload(client, layer, "ADR-u.md", "text").status_code == 200
        assert _tiers(op="write", layer=layer, key="ADR-u.md") == ["private"]

    def test_update_records_private(self, client: TestClient, layer: str) -> None:
        _create_file(client, layer, "ADR-p.md")
        put = client.put(f"/memory/{layer}/ADR-p.md", json={"content": "v2"})
        assert put.status_code == 200, put.text
        assert _tiers(op="write", layer=layer, key="ADR-p.md") == [
            "private",
            "private",
        ]

    def test_delete_records_private(self, client: TestClient, layer: str) -> None:
        _create_file(client, layer, "ADR-p.md")
        assert client.delete(f"/memory/{layer}/ADR-p.md").status_code == 200
        assert _tiers(op="delete", layer=layer, key="ADR-p.md") == ["private"]

    def test_read_records_private(self, client: TestClient, layer: str) -> None:
        _create_file(client, layer, "ADR-p.md")
        assert client.get(f"/memory/{layer}/ADR-p.md").status_code == 200
        assert _tiers(op="read", layer=layer, key="ADR-p.md") == ["private"]

    def test_read_of_a_corpus_document_records_corpus(
        self, client: TestClient, layer: str
    ) -> None:
        _seed_corpus_file(layer, "ADR-c.md")
        assert client.get(f"/memory/{layer}/ADR-c.md").status_code == 200
        assert _tiers(op="read", layer=layer, key="ADR-c.md") == ["corpus"]

    def test_list_records_both_tiers(self, client: TestClient, layer: str) -> None:
        assert client.get(f"/memory/{layer}").status_code == 200
        assert _tiers(op="list", layer=layer) == [BOTH_TIERS]


class TestSemanticPrivate:
    def test_create_update_delete_record_private(self, client: TestClient) -> None:
        _create_semantic(client, "doc-p")
        put = client.put("/memory/semantic/decisions/doc-p", json={"text": "v2"})
        assert put.status_code == 200, put.text
        assert client.delete("/memory/semantic/decisions/doc-p").status_code == 200
        assert _tiers(layer="semantic", key="doc-p") == ["private"] * 3

    def test_read_records_private(self, client: TestClient) -> None:
        _create_semantic(client, "doc-p")
        assert client.get("/memory/semantic/decisions/doc-p").status_code == 200
        assert _tiers(op="read", layer="semantic", key="doc-p") == ["private"]

    def test_whole_document_read_records_private(self, client: TestClient) -> None:
        _create_semantic(client, "doc-p")
        whole = client.get(
            "/memory/semantic/decisions/doc-p", params={"whole_document": "true"}
        )
        assert whole.status_code == 200, whole.text
        assert _tiers(op="read", layer="semantic", key="doc-p") == ["private"]

    def test_list_without_a_corpus_scope_records_private(
        self, client: TestClient
    ) -> None:
        assert client.get("/memory/semantic").status_code == 200
        assert _tiers(op="list", layer="semantic") == ["private"]


class TestSemanticCorpus:
    def test_create_read_delete_record_corpus(self, client: TestClient) -> None:
        with as_admin(client):
            _create_semantic(client, "doc-c", corpus=True)
            assert client.get("/memory/semantic/decisions/doc-c").status_code == 200
            gone = client.delete("/memory/semantic/decisions/doc-c")
            assert gone.status_code == 200, gone.text
        assert _tiers(layer="semantic", key="doc-c") == ["corpus"] * 3


class TestOtherWritePaths:
    """Paths that already name their tier: they must keep naming it."""

    def test_session_upload_records_private(self, client: TestClient) -> None:
        assert upload(client, "session", "note.txt", "scratch").status_code == 200
        assert _tiers(op="write", layer="session") == ["private"]

    def test_private_fold_records_private(self, client: TestClient) -> None:
        assert upload(client, "episodic", "doc.md", "text").status_code == 200
        assert fold(client, "episodic", "doc.md", "decisions").status_code == 200
        assert _tiers(op="write", layer="semantic", mode="single_file") == ["private"]

    def test_corpus_fold_records_corpus(self, client: TestClient) -> None:
        assert corpus_fold(client, "text").status_code == 200
        assert _tiers(op="write", layer="semantic", mode="single_file") == ["corpus"]


class TestRowShape:
    """Audit-schema freeze: the same keys, in the same order, as on main."""

    def test_detail_keys_keep_their_order(self, client: TestClient) -> None:
        assert upload(client, "episodic", "doc.md", "text").status_code == 200
        assert fold(client, "episodic", "doc.md", "decisions").status_code == 200
        assert client.delete("/memory/episodic/doc.md").status_code == 200
        row = ["op", "layer", "collection", "key", "tier"]
        writes = [list(d) for d in memory_audit_details() if d["op"] != "read"]
        assert writes == [
            row,
            [*row, "mode", "dry_run", "total_chunks"],
            [*row, "hard"],
            # #472 (B16): the fold tombstoned by the source delete is audited
            # too; its tier is the row-level field, never a detail_extra key.
            [*row, "hard", "cascade_from", "chunks"],
        ]


class TestTheEmitterContract:
    @pytest.mark.parametrize(
        "emitter",
        [
            emit_memory_audit_event,
            emit_memory_audit_event_background,
            schedule_read_audit,
            emit_write_audit,
            emit_write_audits,
            _emit_promote_audit,
        ],
    )
    def test_no_emitter_defaults_the_tier(self, emitter: Callable[..., Any]) -> None:
        params = inspect.signature(emitter).parameters
        assert "tier" in params
        assert params["tier"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["tier"].default is inspect.Parameter.empty

    async def test_a_call_without_tier_is_refused(self, client: TestClient) -> None:
        with pytest.raises(TypeError, match="tier"):
            await emit_memory_audit_event(  # type: ignore[call-arg]
                user=caller(), op="read", layer="episodic", key="x.md"
            )

    @pytest.mark.parametrize("reserved", ["tier", "op", "layer", "collection", "key"])
    async def test_detail_extra_cannot_restate_a_row_field(
        self, client: TestClient, reserved: str
    ) -> None:
        with pytest.raises(ValueError, match=reserved):
            await emit_memory_audit_event(
                user=caller(),
                op="read",
                layer="episodic",
                key="x.md",
                tier="private",
                detail_extra={reserved: "corpus"},
            )

    async def test_an_unknown_tier_is_refused(self, client: TestClient) -> None:
        with pytest.raises(ValueError, match="tier"):
            await emit_memory_audit_event(
                user=caller(), op="read", layer="episodic", key="x.md", tier="shared"
            )
