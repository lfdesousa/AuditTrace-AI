"""WU-6 Part A — promoted-durable independence PROOF (Sovereign-Attach
EPIC, 2026-09-06-SPEC-wu6-session-gc-live-e2e-release.md §2.5, non-vacuity
guard 3): a WU-4-**promoted** durable row is NEVER deleted by session GC.

Real HTTP routes for both the write side and the durable read-back
(feedback_test_through_real_http_route) — seed via ``POST
/memory/upload?layer=session`` (WU-1), promote via the real ``POST
/memory/promote`` (WU-4), durable read-back via the real ``GET
/memory/episodic/{filename}``. Only the GC sweep itself is exercised at
the service layer, because ``gc_expired`` has NO route by design (spec
§2.3 A-D1: a system sweep, never reachable from a user route) — this test
also asserts that absence directly (guard: no route in ``routes/`` ever
calls it).

Duplicates (rather than imports) the ``_Auth``/upload/promote helpers from
``tests/test_memory_promote_route.py`` — same independence precedent
``tests/test_wu5_promoted_durable_recall.py`` already established for
this exact helper shape.
"""

from __future__ import annotations

import ast
import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from audittrace.dependencies import get_session_memory_service
from audittrace.identity import sentinel_user_context


class _Auth:
    """Patches the JWT-decode chain so the real HTTP route sees a token
    for *sub* with *scope*, without needing a live Keycloak."""

    def __init__(self, sub: str, scope: str) -> None:
        self.sub = sub
        self.scope = scope

    def __enter__(self):
        self._patches = [
            patch("audittrace.auth.get_settings"),
            patch("audittrace.auth._get_jwks_keys"),
            patch("audittrace.auth._decode_jwt_with_allowed_issuers"),
        ]
        mocks = [p.__enter__() for p in self._patches]
        mock_settings, mock_jwks, mock_decode = mocks
        mock_settings.return_value = MagicMock(auth_enabled=True, auth_required=True)
        mock_jwks.return_value = ["fake-key"]
        mock_decode.return_value = {"sub": self.sub, "scope": self.scope}
        return self

    def __exit__(self, *exc: Any) -> None:
        for p in reversed(self._patches):
            p.__exit__(*exc)


def _upload_session_doc(
    client: TestClient, *, sub: str, filename: str, content: bytes = b"scratch note"
) -> None:
    """Seed a session-layer document via the real (WU-1) upload route."""
    with (
        _Auth(sub, "memory:session:write"),
        patch(
            "audittrace.routes.memory._get_minio_client",
            return_value=MagicMock(),
        ),
    ):
        response = client.post(
            "/memory/upload",
            params={"layer": "session"},
            files={"file": (filename, content, "text/plain")},
            headers={"Authorization": "Bearer session-token"},
        )
    assert response.status_code == 200, response.text


def _promote(
    client: TestClient, *, sub: str, scope: str, payload: dict[str, Any]
) -> Any:
    """Hit the real WU-4 ``POST /memory/promote`` route."""
    with _Auth(sub, scope):
        return client.post(
            "/memory/promote",
            json=payload,
            headers={"Authorization": "Bearer promote-token"},
        )


def _read_episodic(client: TestClient, *, sub: str, filename: str) -> Any:
    """Hit the real ``GET /memory/episodic/{filename}`` route
    (feedback_test_through_real_http_route)."""
    with _Auth(sub, "memory:episodic:read"):
        return client.get(
            f"/memory/episodic/{filename}",
            headers={"Authorization": "Bearer read-token"},
        )


class TestPromotedDurableSurvivesSessionGC:
    """The proof: promote, force-GC the session original, confirm the
    durable copy is untouched and the ephemeral original is gone."""

    def test_promoted_episodic_copy_survives_gc_of_expired_session_original(
        self, client: TestClient
    ) -> None:
        """Non-vacuity guard 3 (spec §2.5): confuse the layer under GC
        (e.g. point ``gc_expired`` at the wrong table, or have promote
        delete/move instead of copy) and the durable read-back below goes
        404 — proving this test is load-bearing, not vacuous."""
        _upload_session_doc(
            client, sub="wu6-alice", filename="keepalive.md", content=b"do not lose me"
        )
        promote_resp = _promote(
            client,
            sub="wu6-alice",
            scope="memory:episodic:write",
            payload={"filename": "keepalive.md", "target_layer": "episodic"},
        )
        assert promote_resp.status_code == 200, promote_resp.text
        durable_key = promote_resp.json()["key"]

        # Sanity: the durable copy is readable BEFORE any GC runs.
        pre_gc = _read_episodic(client, sub="wu6-alice", filename=durable_key)
        assert pre_gc.status_code == 200, pre_gc.text

        # Force-collect the session ORIGINAL: a cutoff far in the future
        # makes every session row (including the one just promoted from)
        # eligible, exactly like a real janitor tick past the retention
        # window would. Calling the service directly (not a route) is
        # correct here — gc_expired has no route by design (see the
        # guard test below).
        alice_ctx = replace(sentinel_user_context(), user_id="wu6-alice")
        far_future_ms = int(time.time() * 1000) + 100_000_000
        deleted = asyncio.run(
            get_session_memory_service().gc_expired(
                older_than_ms=far_future_ms, limit=100
            )
        )
        assert deleted >= 1, "the session original was not collected by GC"

        # The ephemeral session ORIGINAL is now gone …
        gone = asyncio.run(
            get_session_memory_service().read_own(alice_ctx, "keepalive.md")
        )
        assert gone is None, (
            "the session original survived GC — the retention sweep isn't "
            "actually deleting expired rows"
        )

        # … but the PROMOTED DURABLE COPY survives GC untouched — the
        # actual guard this test exists to prove. Through the REAL HTTP
        # route, not the service, so a regression that broke the read
        # path would also be caught here.
        post_gc = _read_episodic(client, sub="wu6-alice", filename=durable_key)
        assert post_gc.status_code == 200, (
            f"the PROMOTED durable copy was deleted by session GC "
            f"(independence guard broken): {post_gc.text}"
        )
        assert "do not lose me" in post_gc.json()["content"]


class TestGcExpiredNotWiredToAnyRoute:
    """Structural guard (spec §2.3 A-D1): ``gc_expired`` is a SYSTEM
    sweep, never reachable from a user-facing route. Greps the routes
    package's source for any call site — the only caller anywhere in
    ``src/`` should be ``services/session_gc_janitor.py``."""

    def test_no_route_module_calls_gc_expired(self) -> None:
        routes_dir = (
            Path(__file__).resolve().parents[1] / "src" / "audittrace" / "routes"
        )
        offending: list[str] = []
        for py_file in routes_dir.rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "gc_expired":
                    offending.append(str(py_file))
        assert offending == [], (
            f"gc_expired is referenced from a route module — the janitor's "
            f"system sweep must never be reachable from a user route: "
            f"{offending}"
        )
