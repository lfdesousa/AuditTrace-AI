"""AUDIT-ROW-TIER-DEFAULT-CORPUS ADDENDUM-1, WU-2: RED tests (probe P-C1).

An authorized ``PUT`` over a corpus-tier document used to answer 200, store a
PRIVATE copy that shadowed the corpus document for the caller, and mark the
CORPUS manifest row modified: three records, two tiers. Now the update writes
the tier of the row it resolves to. On semantic that is the corpus collection;
on episodic and procedural, which have no corpus write, the ``PUT`` is refused
(409) before any write. Either way the content store, the manifest row and the
audit rows name the same tier. Run on both manifest backends. Builder: copy
unchanged to ``tests/test_memory_update_corpus_document.py``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from audittrace.routes import memory as m
from tests.memory_truncation_support import SCOPES, USER, memory_client
from tests.memory_truncation_support_a1 import manifest

CURATOR = "00000000-0000-0000-0000-00000000c003"
READER = "00000000-0000-0000-0000-00000000d004"
LAYERS = ("semantic", "episodic", "procedural")
FILE_LAYERS = ("episodic", "procedural")
URL = {
    "semantic": "/memory/semantic/decisions/doc-c",
    "episodic": "/memory/episodic/ADR-c.md",
    "procedural": "/memory/procedural/SKILL-c.md",
}
KEY = {"semantic": "doc-c", "episodic": "ADR-c.md", "procedural": "SKILL-c.md"}
BODY = {"semantic": "text", "episodic": "content", "procedural": "content"}


def use_sql_manifest() -> None:
    """Swap the manifest double for ``MemoryManifestService`` over aiosqlite."""
    from audittrace import dependencies
    from audittrace.db.postgres import InMemoryPostgresFactory
    from audittrace.services.memory_manifest import MemoryManifestService
    from audittrace.services.semantic import ChromaSemanticService

    instances = dependencies.container._instances
    sql = MemoryManifestService(InMemoryPostgresFactory().get_session_factory())
    instances["memory_manifest"] = sql
    instances["semantic"] = ChromaSemanticService(
        client=instances["chromadb"],
        default_collections=["semantic", "decisions", "skills"],
        manifest=sql,
    )


@pytest.fixture(params=["mock", "sql"])
def client(request: pytest.FixtureRequest) -> Iterator[TestClient]:
    """Both manifest backends: the refusal is decided on the manifest row."""
    with memory_client() as c:
        if request.param == "sql":
            use_sql_manifest()
        yield c


@contextmanager
def as_caller(
    client: TestClient, user_id: str, *extra_scopes: str, admin: bool = False
) -> Iterator[None]:
    from audittrace.auth import require_user
    from audittrace.identity import UserContext

    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    previous = overrides[require_user]
    scopes = (*SCOPES, *extra_scopes, *(("audittrace:admin",) if admin else ()))
    overrides[require_user] = lambda: UserContext(
        user_id=user_id,
        username=user_id,
        agent_type="test",
        scopes=scopes,
        is_admin=admin,
    )
    try:
        yield
    finally:
        overrides[require_user] = previous


def audit_rows(key: str) -> list[tuple[str, str]]:
    """``(op, tier)`` of every memory-access row of *key*, in written order."""
    from sqlalchemy import select

    from audittrace.db.models import InteractionRecord
    from audittrace.dependencies import get_postgres_factory
    from audittrace.services.memory_audit import MEMORY_AUDIT_EVENT_CLASS

    async def read() -> list[dict[str, Any]]:
        factory = get_postgres_factory().get_session_factory()
        async with factory() as db:
            found = await db.execute(
                select(InteractionRecord.error_detail)
                .where(InteractionRecord.event_class == MEMORY_AUDIT_EVENT_CLASS)
                .order_by(InteractionRecord.id)
            )
            return [json.loads(detail) for (detail,) in found.all()]

    return [(d["op"], d["tier"]) for d in asyncio.run(read()) if d.get("key") == key]


def private_chunks(collection: str) -> dict[str, str]:
    """Every chunk of the PRIVATE physical collection: id -> text."""
    from audittrace import dependencies

    chroma = dependencies.container._instances["chromadb"]
    col = asyncio.run(
        chroma.get_or_create_collection(
            name=m._physical_collection(collection), embedding_function=None
        )
    )
    got = asyncio.run(col.get(include=["documents"]))
    return dict(zip(got["ids"], got["documents"], strict=True))


def seed_corpus(client: TestClient, layer: str) -> None:
    """A corpus-tier document of *layer*, ``corpus-v1``, written by the curator."""
    if layer == "semantic":
        with as_caller(client, CURATOR, admin=True):
            made = client.post(
                "/memory/semantic",
                params={"promote": "corpus"},
                json={
                    "collection": "decisions",
                    "document_id": "doc-c",
                    "text": "corpus-v1",
                },
            )
        assert made.status_code == 200, made.text
        return
    from audittrace import dependencies

    dependencies.container._instances[layer].add_document(
        "corpus-v1", file=KEY[layer], tier="corpus"
    )
    asyncio.run(
        manifest().record_create(
            layer=layer,
            key=KEY[layer],
            title="t",
            size_bytes=9,
            user_id=CURATOR,
            tier="corpus",
            caller_can_write_shared=True,
        )
    )


def state(client: TestClient, layer: str, reader: str = USER) -> dict[str, Any]:
    """What the three records say about the document, read by *reader* (admin)."""
    with as_caller(client, reader, admin=True):
        read = client.get(URL[layer])
    assert read.status_code == 200, read.text
    body = read.json()
    return {
        "content": body["content"],
        "content_tier": body["metadata"]["tier"],
        "manifest_tier": body["manifest"]["tier"],
        "manifest_modified_at_ms": body["manifest"]["modified_at_ms"],
        "manifest_modified_by": body["manifest"]["modified_by_user_id"],
        "private_chunks": private_chunks("decisions") if layer == "semantic" else {},
    }


class TestSemanticUpdateOfACorpusDocument:
    @pytest.mark.parametrize("caller", ["admin", "scope-holder"])
    def test_an_authorized_put_updates_the_corpus_document(
        self, client: TestClient, caller: str
    ) -> None:
        seed_corpus(client, "semantic")
        extra = () if caller == "admin" else ("memory:corpus:decisions:write",)

        with as_caller(client, USER, *extra, admin=caller == "admin"):
            put = client.put(URL["semantic"], json={"text": "v2"})

        assert put.status_code == 200, put.text
        assert put.json()["tier"] == "corpus"
        # No private copy: the caller and another reader see the same document.
        assert private_chunks("decisions") == {}
        for reader in (USER, READER):
            after = state(client, "semantic", reader)
            assert after["content"] == "v2"
            assert (after["content_tier"], after["manifest_tier"]) == (
                "corpus",
                "corpus",
            )
            assert after["manifest_modified_by"] == USER
        assert audit_rows("doc-c") == [
            ("write", "corpus"),
            ("write", "corpus"),
            ("read", "corpus"),
            ("read", "corpus"),
        ]

    def test_a_private_document_of_the_same_name_is_what_put_updates(
        self, client: TestClient
    ) -> None:
        """The caller's own row resolves first: the corpus document is untouched."""
        seed_corpus(client, "semantic")
        own = {"collection": "decisions", "document_id": "doc-c", "text": "mine-v1"}
        with as_caller(client, USER, admin=True):
            assert client.post("/memory/semantic", json=own).status_code == 200
            put = client.put(URL["semantic"], json={"text": "mine-v2"})
        assert put.status_code == 200, put.text
        assert put.json()["tier"] == "private"
        assert sorted(private_chunks("decisions").values()) == ["mine-v2"]
        with as_caller(client, READER, "memory:corpus:decisions:read"):
            assert client.get(URL["semantic"]).json()["content"] == "corpus-v1"
        assert audit_rows("doc-c") == [
            ("write", "corpus"),
            ("write", "private"),
            ("write", "private"),
            ("read", "corpus"),
        ]

    def test_control_put_over_an_own_private_document_still_works(
        self, client: TestClient
    ) -> None:
        made = client.post(
            "/memory/semantic",
            json={"collection": "decisions", "document_id": "doc-p", "text": "v1"},
        )
        assert made.status_code == 200, made.text
        put = client.put("/memory/semantic/decisions/doc-p", json={"text": "v2"})
        assert put.status_code == 200, put.text
        read = client.get("/memory/semantic/decisions/doc-p")
        assert read.json()["content"] == "v2"
        assert read.json()["metadata"]["tier"] == "private"


@pytest.mark.parametrize("layer", FILE_LAYERS)
class TestFileLayerUpdateOfACorpusDocument:
    def test_an_admin_put_is_refused_and_nothing_changes(
        self, client: TestClient, layer: str
    ) -> None:
        seed_corpus(client, layer)
        before = state(client, layer)
        rows_before = audit_rows(KEY[layer])

        with as_caller(client, USER, admin=True):
            put = client.put(URL[layer], json={BODY[layer]: "admin-v2"})

        assert put.status_code == 409
        assert put.json()["detail"] == (
            f"{KEY[layer]} is a corpus-tier {layer} document. Not supported: "
            f"updating a corpus document of the {layer} layer."
        )
        after = state(client, layer)
        assert after == before
        assert after["content"] == "corpus-v1"
        assert (after["content_tier"], after["manifest_tier"]) == ("corpus", "corpus")
        assert after["manifest_modified_by"] == CURATOR
        # No write row for the refused PUT: only the second state read was added.
        assert audit_rows(KEY[layer]) == [*rows_before, ("read", "corpus")]

    def test_control_put_over_an_own_private_file_still_works(
        self, client: TestClient, layer: str
    ) -> None:
        made = client.post(
            f"/memory/{layer}", json={"filename": KEY[layer], "content": "v1"}
        )
        assert made.status_code == 200, made.text
        put = client.put(URL[layer], json={"content": "v2"})
        assert put.status_code == 200, put.text
        assert client.get(URL[layer]).json()["content"] == "v2"
        assert audit_rows(KEY[layer]) == [
            ("write", "private"),
            ("write", "private"),
            ("read", "private"),
        ]


@pytest.mark.parametrize("layer", LAYERS)
class TestUnauthorizedUpdateOfACorpusDocument:
    def test_a_caller_without_the_shared_write_authority_is_still_403(
        self, client: TestClient, layer: str
    ) -> None:
        seed_corpus(client, layer)
        rows_before = audit_rows(KEY[layer])
        put = client.put(URL[layer], json={BODY[layer]: "user-v2"})
        assert put.status_code == 403
        assert state(client, layer)["content"] == "corpus-v1"
        assert audit_rows(KEY[layer]) == [*rows_before, ("read", "corpus")]
