"""MEMORY-SERVER-SILENT-TRUNCATION, WU-2: every memory write path refuses a
document over ``AUDITTRACE_MEMORY_MAX_DOCUMENT_BYTES`` with an explicit error.

At 1f44bf3e no memory write path has any size bound (a 32 MiB body is stored).
The contract: ``413`` with the constant detail naming the limit in BYTES (the
UTF-8 length of the document), nothing stored, nothing indexed, no manifest
row. One byte under the bound is accepted unchanged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from audittrace.config import get_settings
from tests.memory_truncation_support import (
    SCOPES,
    USER,
    chunk_total,
    memory_client,
    upload,
)

LIMIT = 4096
DETAIL = {"detail": f"memory document exceeds the limit of {LIMIT} bytes"}

# (case id, the routes it exercises). ``ROUTES_NOT_CARRYING_A_DOCUMENT`` are the
# POST/PUT memory routes with no document body: they act on stored objects.
FILE_CASES = {
    "upload-episodic": ("POST /memory/upload",),
    "upload-procedural": ("POST /memory/upload",),
    "post-episodic": ("POST /memory/episodic",),
    "put-episodic": ("PUT /memory/episodic/{filename}",),
    "post-procedural": ("POST /memory/procedural",),
    "put-procedural": ("PUT /memory/procedural/{filename}",),
}
SEMANTIC_CASES = {
    "post-semantic": ("POST /memory/semantic",),
    "put-semantic": ("PUT /memory/semantic/{collection}/{document_id}",),
}
ROUTES_NOT_CARRYING_A_DOCUMENT = {"POST /memory/index", "POST /memory/promote"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("AUDITTRACE_MEMORY_MAX_DOCUMENT_BYTES", str(LIMIT))
    get_settings.cache_clear()
    with memory_client() as c:
        yield c
    get_settings.cache_clear()


def _file_write(client: TestClient, case: str, filename: str, text: str) -> Any:
    verb, layer = case.split("-")
    if verb == "upload":
        return upload(client, layer, filename, text)
    if verb == "post":
        return client.post(
            f"/memory/{layer}", json={"filename": filename, "content": text}
        )
    return client.put(f"/memory/{layer}/{filename}", json={"content": text})


def _semantic_write(client: TestClient, case: str, doc_id: str, text: str) -> Any:
    if case == "post-semantic":
        return client.post(
            "/memory/semantic",
            json={"collection": "decisions", "document_id": doc_id, "text": text},
        )
    return client.put(f"/memory/semantic/decisions/{doc_id}", json={"text": text})


def _mcp_write(client: TestClient, tool: str, doc_id: str, text: str) -> Any:
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": {"document_id": doc_id, "text": text},
            },
        },
    )


class TestMemoryWriteLimit:
    @pytest.mark.parametrize("case", sorted(FILE_CASES))
    def test_file_document_one_byte_over_is_refused_and_nothing_is_stored(
        self, client: TestClient, case: str
    ) -> None:
        layer = case.split("-")[1]
        seed = "seed\n"
        if case.startswith("put"):
            created = client.post(
                f"/memory/{layer}", json={"filename": "doc.md", "content": seed}
            )
            assert created.status_code == 200, created.text

        refused = _file_write(client, case, "doc.md", "x" * (LIMIT + 1))

        assert refused.status_code == 413
        assert refused.json() == DETAIL
        after = client.get(f"/memory/{layer}/doc.md")
        if case.startswith("put"):
            assert after.status_code == 200
            assert after.json()["content"] == seed
            assert after.json()["manifest"]["size_bytes"] == len(seed)
        else:
            assert after.status_code == 404
        assert chunk_total(client, "decisions") == 0
        assert chunk_total(client, "skills") == 0

    @pytest.mark.parametrize("case", sorted(FILE_CASES))
    def test_file_document_at_the_limit_is_stored_whole(
        self, client: TestClient, case: str
    ) -> None:
        layer = case.split("-")[1]
        if case.startswith("put"):
            client.post(
                f"/memory/{layer}", json={"filename": "doc.md", "content": "seed\n"}
            )
        text = "x" * LIMIT

        accepted = _file_write(client, case, "doc.md", text)

        assert accepted.status_code == 200
        assert client.get(f"/memory/{layer}/doc.md").json()["content"] == text

    def test_the_limit_counts_utf8_bytes_not_characters(
        self, client: TestClient
    ) -> None:
        text = "é" * (LIMIT // 2 + 1)
        assert len(text) < LIMIT < len(text.encode("utf-8"))

        refused = client.post(
            "/memory/episodic", json={"filename": "accents.md", "content": text}
        )

        assert refused.status_code == 413
        assert refused.json() == DETAIL
        assert client.get("/memory/episodic/accents.md").status_code == 404

    def test_session_upload_over_the_limit_is_refused_and_not_stored(
        self, client: TestClient
    ) -> None:
        from audittrace.dependencies import get_session_memory_service
        from audittrace.identity import UserContext

        refused = upload(client, "session", "attach.md", "x" * (LIMIT + 1))

        assert refused.status_code == 413
        assert refused.json() == DETAIL
        caller = UserContext(
            user_id=USER, username=USER, agent_type="test", scopes=SCOPES
        )
        stored = asyncio.run(get_session_memory_service().list_own(caller, limit=10))
        assert stored == []

    @pytest.mark.parametrize("case", sorted(SEMANTIC_CASES))
    def test_semantic_document_one_byte_over_is_refused_and_not_indexed(
        self, client: TestClient, case: str
    ) -> None:
        seed = "seed"
        if case == "put-semantic":
            created = _semantic_write(client, "post-semantic", "doc", seed)
            assert created.status_code == 200, created.text
        before = chunk_total(client, "decisions")

        refused = _semantic_write(client, case, "doc", "x" * (LIMIT + 1))

        assert refused.status_code == 413
        assert refused.json() == DETAIL
        assert chunk_total(client, "decisions") == before
        after = client.get("/memory/semantic/decisions/doc")
        if case == "put-semantic":
            assert after.json()["content"] == seed
        else:
            assert after.status_code == 404

    @pytest.mark.parametrize(
        ("tool", "collection"),
        [("write_decision", "decisions"), ("write_skill", "skills")],
    )
    def test_mcp_write_tool_over_the_limit_is_a_tool_error_and_not_indexed(
        self, client: TestClient, tool: str, collection: str
    ) -> None:
        answer = _mcp_write(client, tool, "doc", "x" * (LIMIT + 1))

        assert answer.status_code == 200
        result = answer.json()["result"]
        assert result["isError"] is True
        assert DETAIL["detail"] in result["content"][0]["text"]
        assert chunk_total(client, collection) == 0
        assert client.get(f"/memory/semantic/{collection}/doc").status_code == 404

    def test_every_memory_write_route_is_covered_or_named_exempt(
        self, client: TestClient
    ) -> None:
        """An equality pin, not a list to keep in step by hand: a new POST/PUT
        memory route reddens this until it is given a case or an exemption."""
        paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
        declared = {
            f"{method.upper()} {path}"
            for path, operations in paths.items()
            if path.startswith("/memory/")
            for method in operations
            if method in {"post", "put", "patch"}
        }
        assert declared, "the OpenAPI schema lists no memory write route"
        covered = {r for routes in FILE_CASES.values() for r in routes} | {
            r for routes in SEMANTIC_CASES.values() for r in routes
        }
        assert declared == covered | ROUTES_NOT_CARRYING_A_DOCUMENT
