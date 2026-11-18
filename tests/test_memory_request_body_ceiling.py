"""MEMORY-SERVER-SILENT-TRUNCATION ADDENDUM-1, WU-2 (gate r1 S3): a memory
write request over its path's ceiling is refused before its body is spooled or
parsed. The ceiling is the largest body an acceptable document could need."""

from __future__ import annotations

import json
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from audittrace.config import get_settings
from audittrace.routes import memory as m
from tests.memory_truncation_support import memory_client

TEXT_LIMIT = 4096
PDF_LIMIT = 1024 * 1024
FRAMING = 1_048_576
JSON_CEILING = 6 * TEXT_LIMIT + FRAMING
UPLOAD_CEILING = max(PDF_LIMIT, TEXT_LIMIT) + FRAMING


def _detail(ceiling: int) -> dict[str, str]:
    return {"detail": f"memory request body exceeds the limit of {ceiling} bytes"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("AUDITTRACE_MEMORY_MAX_DOCUMENT_BYTES", str(TEXT_LIMIT))
    monkeypatch.setenv("AUDITTRACE_PDF_MAX_SIZE_MB", "1")
    get_settings.cache_clear()
    with memory_client() as c:
        yield c
    get_settings.cache_clear()


def _json_body(content_bytes: int) -> bytes:
    return json.dumps({"filename": "big.md", "content": "x" * content_bytes}).encode()


class TestMemoryRequestBodyCeiling:
    def test_a_declared_body_over_the_ceiling_is_refused_unparsed(
        self, client: TestClient
    ) -> None:
        body = _json_body(JSON_CEILING + 1)

        refused = client.post(
            "/memory/episodic",
            content=body,
            headers={"content-type": "application/json"},
        )

        assert refused.status_code == 413
        assert refused.json() == _detail(JSON_CEILING)
        assert client.get("/memory/episodic/big.md").status_code == 404

    def test_an_undeclared_streamed_body_is_cut_at_the_ceiling(
        self, client: TestClient
    ) -> None:
        body = _json_body(JSON_CEILING + 1)

        def stream() -> Iterator[bytes]:
            for start in range(0, len(body), 65536):
                yield body[start : start + 65536]

        refused = client.post(
            "/memory/episodic",
            content=stream(),
            headers={"content-type": "application/json"},
        )

        assert refused.status_code == 413
        assert refused.json() == _detail(JSON_CEILING)
        assert client.get("/memory/episodic/big.md").status_code == 404

    @pytest.mark.parametrize(
        "path", ["/memory/episodic/big.md", "/memory/procedural/big.md"]
    )
    def test_a_put_over_the_ceiling_is_refused_with_the_ceiling_sentence(
        self, client: TestClient, path: str
    ) -> None:
        refused = client.put(
            path,
            content=_json_body(JSON_CEILING + 1),
            headers={"content-type": "application/json"},
        )

        assert refused.status_code == 413
        assert refused.json() == _detail(JSON_CEILING)  # not the document sentence
        assert client.get(path).status_code == 404

    def test_a_put_to_a_semantic_document_over_the_ceiling_is_refused(
        self, client: TestClient
    ) -> None:
        refused = client.put(
            "/memory/semantic/decisions/big",
            content=json.dumps({"text": "x" * (JSON_CEILING + 1)}).encode(),
            headers={"content-type": "application/json"},
        )

        assert refused.status_code == 413
        assert refused.json() == _detail(JSON_CEILING)
        assert client.get("/memory/semantic/decisions/big").status_code == 404

    def test_an_upload_over_its_ceiling_is_refused_unspooled(
        self, client: TestClient
    ) -> None:
        store = MagicMock()
        with patch.object(m, "_get_minio_client", return_value=store):
            refused = client.post(
                "/memory/upload",
                params={"layer": "episodic", "filename": "big.md"},
                files={
                    "file": ("big.md", b"x" * (UPLOAD_CEILING + 1), "text/markdown")
                },
            )

        assert refused.status_code == 413
        assert refused.json() == _detail(UPLOAD_CEILING)
        assert store.mock_calls == []
        assert client.get("/memory/episodic/big.md").status_code == 404

    def test_an_mcp_request_over_the_ceiling_is_refused(
        self, client: TestClient
    ) -> None:
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "write_decision",
                    "arguments": {
                        "document_id": "big",
                        "text": "x" * (JSON_CEILING + 1),
                    },
                },
            }
        ).encode()

        refused = client.post(
            "/mcp", content=body, headers={"content-type": "application/json"}
        )

        assert refused.status_code == 413
        assert refused.json() == _detail(JSON_CEILING)
        assert client.get("/memory/semantic/decisions/big").status_code == 404

    def test_control_under_the_ceiling_the_document_bound_answers(
        self, client: TestClient
    ) -> None:
        refused = client.post(
            "/memory/episodic",
            json={"filename": "mid.md", "content": "x" * (TEXT_LIMIT + 1)},
        )

        assert refused.status_code == 413
        assert refused.json() == {
            "detail": f"memory document exceeds the limit of {TEXT_LIMIT} bytes"
        }

    def test_control_a_read_is_never_bounded(self, client: TestClient) -> None:
        assert client.get("/memory/episodic").status_code == 200
