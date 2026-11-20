"""MEMORY-SERVER-SILENT-TRUNCATION ADDENDUM-1 (gate r1 S3): the BFF relays the
memory server's 413 for a console upload unchanged, status and body."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from bff.app import create_app, get_http_client
from bff.config import get_settings
from tests.bff.conftest import make_token
from tests.bff.test_console_files import (
    _MULTIPART_BODY,
    _MULTIPART_CONTENT_TYPE,
    _ConsoleFilesBackend,
    _settings,
)

DETAIL = {"detail": "memory document exceeds the limit of 4194304 bytes"}


class TestConsoleFilesRefusalRelay:
    def test_a_413_from_the_memory_server_reaches_the_console_unchanged(self) -> None:
        app = create_app()
        backend = _ConsoleFilesBackend()
        backend.orchestrator_response = httpx.Response(
            413, json=DETAIL, headers={"content-type": "application/json"}
        )
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
        app.dependency_overrides[get_settings] = lambda: _settings()
        app.dependency_overrides[get_http_client] = lambda: mock_client
        with TestClient(app) as client:
            answer = client.post(
                "/console/files",
                content=_MULTIPART_BODY,
                headers={
                    "Authorization": f"Bearer {make_token(sub='alice')}",
                    "Content-Type": _MULTIPART_CONTENT_TYPE,
                },
            )

        assert answer.status_code == 413
        assert answer.json() == DETAIL
        assert len(backend.orchestrator_calls) == 1
