"""L20: a non-2xx response must keep its headers (notably ``X-Trace-Id``).

``_http_request`` (scripts.deploy.verify AND scripts.deploy.memory, the seam the
backfill script uses) returned ``{}`` for every ``HTTPError``, so the backfill
recorded ``trace_id=None`` for every 403 although the server stamps
``X-Trace-Id`` on 403s (src/audittrace/trace_id_header.py).

Declared test doubles: NONE of ``urlopen``. A real loopback ``http.server``
stub (127.0.0.1, ephemeral port, started/stopped per test) answers with a fixed
status, headers and body, so the real ``urllib`` HTTPError path is exercised.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from scripts import backfill_pdf_signature_status as bf
from scripts.deploy import memory, verify

TRACE = "0af7651916cd43dd8448eb211c80319c"
BODY = json.dumps({"detail": "forbidden"}).encode()


@contextmanager
def _stub(status: int, headers: dict[str, str]) -> Iterator[str]:
    class _Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)

        do_GET = do_POST = _answer  # noqa: N815

        def log_message(self, *a: Any) -> None:  # silence stderr
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


SEAMS: list[Callable[..., Any]] = [verify._http_request, memory._http_request]


@pytest.mark.parametrize("seam", SEAMS, ids=["verify", "memory"])
def test_http_error_keeps_lowercased_headers_and_body(seam: Callable[..., Any]) -> None:
    with _stub(403, {"X-Trace-Id": TRACE, "Content-Type": "application/json"}) as url:
        status, headers, body = seam("POST", f"{url}/memory/index")
    assert status == 403
    assert headers["x-trace-id"] == TRACE
    assert headers["content-type"] == "application/json"
    assert body == BODY


@pytest.mark.parametrize("seam", SEAMS, ids=["verify", "memory"])
def test_success_path_headers_unchanged(seam: Callable[..., Any]) -> None:
    with _stub(200, {"X-Trace-Id": TRACE}) as url:
        status, headers, body = seam("GET", f"{url}/x")
    assert (status, headers["x-trace-id"], body) == (200, TRACE, BODY)


@pytest.mark.parametrize("seam", SEAMS, ids=["verify", "memory"])
def test_http_error_with_no_headers_object_yields_empty_dict(
    seam: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Declared mock: ``urllib.request.urlopen`` raises an HTTPError whose
    ``headers`` is ``None`` (the guard case); status and body must survive."""
    import io
    from urllib.error import HTTPError

    def _raise(*a: Any, **k: Any) -> None:
        raise HTTPError("http://x/", 500, "boom", None, io.BytesIO(b"oops"))  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    assert seam("GET", "http://x/") == (500, {}, b"oops")


def test_backfill_post_index_gets_trace_id_on_403() -> None:
    with _stub(403, {"X-Trace-Id": TRACE}) as url:
        client = bf.Client(url, "tok", insecure=False, timeout=5)
        status, new_status, trace_id, error = bf._post_index(
            client, "episodic/x.pdf", dry_run=False
        )
    assert status == 403
    assert new_status is None
    assert trace_id == TRACE
    assert error == "forbidden"
