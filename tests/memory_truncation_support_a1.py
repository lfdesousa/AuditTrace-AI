"""ADDENDUM-1 additions to the MEMORY-SERVER-SILENT-TRUNCATION harness (functions only).

State a test cannot reach through a route is arranged here on the repo's own
doubles: a hole in ChromaDB, a document folded before the stamp existed, a
manifest row owned by somebody else. Every ASSERTION still goes through HTTP.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

from audittrace.routes import memory as m
from tests.memory_truncation_support import SCOPES, USER

STEP = m.CHUNK_SIZE - m.CHUNK_OVERLAP
STUB_VECTOR = [0.1, 0.2, 0.3]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def caller(*, admin: bool = False) -> Any:
    from audittrace.identity import UserContext

    scopes = (*SCOPES, "audittrace:admin") if admin else SCOPES
    return UserContext(
        user_id=USER, username=USER, agent_type="test", scopes=scopes, is_admin=admin
    )


@contextmanager
def as_admin(client: TestClient) -> Iterator[None]:
    """The same caller, holding ``audittrace:admin`` for the enclosed requests."""
    from audittrace.auth import require_user

    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    previous = overrides[require_user]
    overrides[require_user] = lambda: caller(admin=True)
    try:
        yield
    finally:
        overrides[require_user] = previous


def chroma_collection(collection: str) -> Any:
    """The ChromaDB double's collection the fold writes *collection* into."""
    from audittrace import dependencies

    chroma = dependencies.container._instances["chromadb"]
    return asyncio.run(
        chroma.get_or_create_collection(
            name=m._physical_collection(collection), embedding_function=None
        )
    )


def manifest() -> Any:
    from audittrace.dependencies import get_memory_manifest_service

    return get_memory_manifest_service()


def hard_delete_chunk(
    client: TestClient, collection: str, filename: str, index: int
) -> None:
    """Remove one chunk's ChromaDB row through the admin hard delete: a real hole."""
    gone = m._doc_id(collection, filename, index)
    with as_admin(client):
        removed = client.delete(
            f"/memory/semantic/{collection}/{gone}", params={"hard": "true"}
        )
    assert removed.status_code == 200, removed.text


def fold_before_the_stamp(
    collection: str, filename: str, text: str, *, bare_ids: bool = False
) -> int:
    """Write *text*'s chunks the way a fold at 1f44bf3e did: no ``chunk_total``,
    no ``fold_sha256``, and nothing removed past the end. ``bare_ids`` stores
    them at the pre-LC-9 id (no owner prefix). Returns the chunk count."""
    from audittrace.services.memory_keys import ID_FORM_LC9, private_physical_id

    chunks = m._chunk_text(text)
    logical = [m._doc_id(collection, filename, i) for i in range(len(chunks))]
    ids = logical if bare_ids else [private_physical_id(caller(), i) for i in logical]
    metadatas: list[dict[str, Any]] = [
        {
            "source": filename,
            "category": "episodic",
            "file_type": "md",
            "chunk": i,
            "user_id": USER,
            "tier": "private",
            **({} if bare_ids else {"id_form": ID_FORM_LC9}),
        }
        for i in range(len(chunks))
    ]
    asyncio.run(
        chroma_collection(collection).upsert(
            ids=ids,
            documents=chunks,
            metadatas=metadatas,
            embeddings=[STUB_VECTOR for _ in chunks],
        )
    )
    return len(chunks)


def memory_audit_details() -> list[dict[str, Any]]:
    """The ``detail`` of every memory-access audit row written so far."""
    from sqlalchemy import select

    from audittrace.db.models import InteractionRecord
    from audittrace.dependencies import get_postgres_factory
    from audittrace.services.memory_audit import MEMORY_AUDIT_EVENT_CLASS

    async def read() -> list[dict[str, Any]]:
        factory = get_postgres_factory().get_session_factory()
        async with factory() as db:
            rows = await db.execute(
                select(InteractionRecord.error_detail).where(
                    InteractionRecord.event_class == MEMORY_AUDIT_EVENT_CLASS
                )
            )
            return [json.loads(detail) for (detail,) in rows.all()]

    return asyncio.run(read())
