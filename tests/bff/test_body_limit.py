"""MEMORY-SERVER-SILENT-TRUNCATION ADDENDUM-2 (gate r2 S11): the BFF refuses an
oversized body before it buffers it; nothing upstream is called. Gate r3 S15: the
refusal is in the BFF error shape, whatever route it answers."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
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

FILES_CEILING = 2048
OTHER_CEILING = 4096


def _detail(ceiling: int) -> dict[str, object]:
    """The BFF error shape (gate r3 S15), pinned EXACTLY: the error object of the
    chat route's contract plus the same text under ``detail`` (which the fork
    reads). ``param`` is JSON ``null``; ``status`` repeats the HTTP status."""
    message = f"request body exceeds the limit of {ceiling} bytes"
    return {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "param": None,
            "code": "request_too_large",
            "status": 413,
        },
        "detail": message,
    }


@pytest.fixture
def bff(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, _ConsoleFilesBackend]]:
    monkeypatch.setenv(
        "AUDITTRACE_BFF_CONSOLE_FILES_MAX_BODY_BYTES", str(FILES_CEILING)
    )
    monkeypatch.setenv("AUDITTRACE_BFF_MAX_REQUEST_BODY_BYTES", str(OTHER_CEILING))
    get_settings.cache_clear()
    app = create_app()
    backend = _ConsoleFilesBackend()
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
    app.dependency_overrides[get_settings] = lambda: _settings()
    app.dependency_overrides[get_http_client] = lambda: mock_client
    with TestClient(app) as client:
        yield client, backend
    get_settings.cache_clear()


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {make_token(sub='alice')}",
        "Content-Type": _MULTIPART_CONTENT_TYPE,
    }


def _untouched(backend: _ConsoleFilesBackend) -> bool:
    return backend.orchestrator_calls == [] and backend.exchange_request_bodies == []


class TestBffBodyLimit:
    def test_a_declared_console_upload_over_the_ceiling_is_refused_unread(
        self, bff: tuple[TestClient, _ConsoleFilesBackend]
    ) -> None:
        client, backend = bff

        refused = client.post(
            "/console/files", content=b"x" * (FILES_CEILING + 1), headers=_headers()
        )

        assert refused.status_code == 413
        assert refused.json() == _detail(FILES_CEILING)
        assert _untouched(backend)

    def test_an_undeclared_streamed_upload_is_cut_at_the_ceiling(
        self, bff: tuple[TestClient, _ConsoleFilesBackend]
    ) -> None:
        client, backend = bff

        def stream() -> Iterator[bytes]:
            for _ in range(FILES_CEILING // 512 + 1):
                yield b"x" * 512

        refused = client.post("/console/files", content=stream(), headers=_headers())

        # The route authenticates before it reads the body, so the token
        # exchange may have run; the body is never forwarded.
        assert refused.status_code == 413
        assert refused.json() == _detail(FILES_CEILING)
        assert backend.orchestrator_calls == []

    def test_any_other_route_has_the_general_ceiling(
        self, bff: tuple[TestClient, _ConsoleFilesBackend]
    ) -> None:
        client, backend = bff

        refused = client.post(
            "/v1/chat/completions",
            content=b"x" * (OTHER_CEILING + 1),
            headers={"Authorization": f"Bearer {make_token(sub='alice')}"},
        )

        assert refused.status_code == 413
        assert refused.json() == _detail(OTHER_CEILING)
        assert _untouched(backend)

    def test_control_an_upload_under_the_ceiling_is_forwarded_unchanged(
        self, bff: tuple[TestClient, _ConsoleFilesBackend]
    ) -> None:
        client, backend = bff
        assert len(_MULTIPART_BODY) < FILES_CEILING

        answer = client.post(
            "/console/files", content=_MULTIPART_BODY, headers=_headers()
        )

        assert answer.status_code == 200
        assert [call.content for call in backend.orchestrator_calls] == [
            _MULTIPART_BODY
        ]
