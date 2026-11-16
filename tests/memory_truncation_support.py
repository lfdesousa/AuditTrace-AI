"""Shared harness for the MEMORY-SERVER-SILENT-TRUNCATION tests (functions only).

Every request goes through the REAL HTTP routes of ``create_app()``; the doubles
are the repo's own (``create_test_container``) with the real
``ChromaSemanticService`` over ``MockChromaDBFactory``, as
``tests/test_wu1_memory_retrieval_correctness.py`` wires it.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from audittrace.routes import memory as m

USER = "00000000-0000-0000-0000-00000000a001"
SCOPES = (
    "memory:episodic:read",
    "memory:episodic:write",
    "memory:procedural:read",
    "memory:procedural:write",
    "memory:semantic:read",
    "memory:semantic:write",
    "memory:decisions:write",
    "memory:skills:write",
    "memory:session:write",
)


def reload_mcp_write_tools() -> None:
    """Re-register the real ``write_decision`` / ``write_skill`` tools.

    Other test files reset the MCP write registry on teardown (and reload the
    handlers on setup, for themselves). Run after one of them, a test that
    depends on the tools finds none, so every test here that needs them starts
    from a freshly registered pair, whatever ran before it."""
    from audittrace.tools import mcp_write_handlers
    from audittrace.tools.mcp_write_registry import reset_mcp_write_registry_for_tests

    reset_mcp_write_registry_for_tests()
    importlib.reload(mcp_write_handlers)


def distinct_body(n_chars: int) -> str:
    """Exactly *n_chars* characters, every 9-character line carrying its own
    index, so an omitted, repeated or reordered span is visible."""
    lines = n_chars // 9 + 1
    return "".join(f"[{i:06d}]\n" for i in range(lines))[:n_chars]


@contextmanager
def memory_client() -> Iterator[TestClient]:
    from audittrace import dependencies
    from audittrace.auth import require_user
    from audittrace.db.factory import MockChromaDBFactory
    from audittrace.dependencies import create_test_container, reset_container
    from audittrace.identity import UserContext
    from audittrace.server import create_app
    from audittrace.services.semantic import ChromaSemanticService

    reload_mcp_write_tools()
    stub = AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts])
    chroma = asyncio.run(MockChromaDBFactory().get_client())
    container = create_test_container()
    container._instances["chromadb"] = chroma
    container._instances["semantic"] = ChromaSemanticService(
        client=chroma,
        default_collections=["semantic", "decisions", "skills"],
        manifest=container._instances["memory_manifest"],
    )
    dependencies.container = container
    app = create_app()
    identity = UserContext(
        user_id=USER, username=USER, agent_type="test", scopes=SCOPES, is_admin=False
    )
    app.dependency_overrides[require_user] = lambda: identity
    with (
        patch("audittrace.services.semantic.embed_via_nomic", stub),
        patch("audittrace.routes.memory.embed_via_nomic", stub),
        TestClient(app) as client,
    ):
        yield client
    reset_container()


def _object_store(contents: dict[str, bytes]) -> MagicMock:
    def get_object(_bucket: str, key: str) -> MagicMock:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = contents[key]
        return response

    fake = MagicMock()
    fake.get_object.side_effect = get_object
    return fake


def upload(client: TestClient, layer: str, filename: str, text: str) -> Any:
    with patch.object(m, "_get_minio_client", return_value=MagicMock()):
        return client.post(
            "/memory/upload",
            params={"layer": layer, "filename": filename},
            files={"file": (filename, text.encode("utf-8"), "text/markdown")},
        )


def fold(client: TestClient, layer: str, filename: str, collection: str) -> Any:
    """``POST /memory/index?file=`` over the bytes the layer's read route
    returns for *filename* (what the write route actually stored)."""
    stored = client.get(f"/memory/{layer}/{filename}")
    assert stored.status_code == 200, stored.text
    key = f"{USER}/{layer}/{filename}"
    store = _object_store({key: stored.json()["content"].encode("utf-8")})
    with patch.object(m, "_get_minio_client", return_value=store):
        return client.post(
            "/memory/index", params={"file": key, "collections": collection}
        )


def read_whole(client: TestClient, collection: str, filename: str) -> Any:
    chunk0 = m._doc_id(collection, filename, 0)
    return client.get(
        f"/memory/semantic/{collection}/{chunk0}", params={"whole_document": "true"}
    )


def chunk_total(client: TestClient, collection: str) -> int:
    listing = client.get(
        "/memory/semantic", params={"collection": collection, "granularity": "chunk"}
    )
    assert listing.status_code == 200, listing.text
    total: int = listing.json()["total_chunks"]
    return total
