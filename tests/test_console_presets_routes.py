"""HTTP-route tests for the console-presets API (Mongo-repl WU-presets,
MongoDB-elimination EPIC).

Mirrors ``test_console_conversations_routes.py``'s structure EXACTLY
(the spec's instruction), scaled down to a single resource (no message
tree). Two identity strategies, per
``feedback_test_through_real_http_route``:

* Most tests use the default ``client`` fixture (``AUDITTRACE_AUTH_REQUIRED
  =false`` bypass — the sentinel user) to exercise route wiring, scope
  declarations, and response shapes without JWT ceremony.
* Cross-user isolation tests drive the REAL ``require_user`` cold path
  (mirrors ``tests/test_console_conversations_routes.py``'s
  ``TestCrossUserIsolation``): ``auth_required=True`` + a patched
  ``_decode_jwt_with_allowed_issuers`` returning distinct ``sub``s and
  the required scopes, so ``require_user`` genuinely binds the RLS
  ContextVar per caller (not just a ``dependency_overrides[require_user]``
  swap, which would replace ``require_user``'s body and never exercise
  ``set_current_user_id``).
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

READ_SCOPE = "memory:presets:read-own"
WRITE_SCOPE = "memory:presets:write"


@contextmanager
def _identity(*, sub: str, scope: str):
    """Drive the REAL ``require_user`` cold path for ``sub`` with
    ``scope``, mirroring ``tests/test_console_conversations_routes.py``'s
    identity helper — NOT a ``dependency_overrides`` swap (see module
    docstring for why that distinction matters for RLS ContextVar
    binding)."""
    with (
        patch("audittrace.auth.get_settings") as mock_settings,
        patch("audittrace.auth._get_jwks_keys") as mock_jwks,
        patch("audittrace.auth._decode_jwt_with_allowed_issuers") as mock_decode,
    ):
        mock_settings.return_value = MagicMock(auth_enabled=True, auth_required=True)
        mock_jwks.return_value = ["fake-key"]
        mock_decode.return_value = {"sub": sub, "scope": scope}
        yield


def _auth_headers(sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer token-for-{sub}"}


# ── Bypass-mode wiring (sentinel identity, no JWT ceremony) ───────────────


class TestConsolePresetsCrud:
    def test_upsert_creates_preset(self, client: TestClient) -> None:
        r = client.post(
            "/console/presets",
            json={"preset_id": "preset-1", "title": "Hello", "data": {"model": "x"}},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["preset_id"] == "preset-1"
        assert body["title"] == "Hello"
        assert body["data"] == {"model": "x"}
        assert "user_sub" not in body, (
            "response must never leak the internal user_sub field"
        )

    def test_upsert_defaults_title(self, client: TestClient) -> None:
        r = client.post("/console/presets", json={"preset_id": "preset-1"})
        assert r.status_code == 200
        assert r.json()["title"] == "New Chat"

    def test_upsert_is_idempotent(self, client: TestClient) -> None:
        client.post("/console/presets", json={"preset_id": "preset-1", "title": "A"})
        r = client.post(
            "/console/presets", json={"preset_id": "preset-1", "title": "B"}
        )
        assert r.status_code == 200
        listed = client.get("/console/presets").json()
        matches = [i for i in listed["items"] if i["preset_id"] == "preset-1"]
        assert len(matches) == 1
        assert matches[0]["title"] == "B"

    def test_upsert_rejects_missing_preset_id(self, client: TestClient) -> None:
        r = client.post("/console/presets", json={"title": "no id"})
        assert r.status_code == 422

    def test_hostile_body_user_sub_is_ignored(self, client: TestClient) -> None:
        """A hostile caller cannot stamp an arbitrary ``user_sub``/
        ``user_id`` via the request body — the field doesn't exist on the
        request model, so Pydantic drops it, and the route always stamps
        the RESOLVED identity."""
        r = client.post(
            "/console/presets",
            json={
                "preset_id": "preset-hostile",
                "title": "hi",
                "user_sub": "attacker-sub",
                "user_id": "attacker-sub",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["preset_id"] == "preset-hostile"
        assert "user_sub" not in body
        assert "user_id" not in body

    def test_get_preset(self, client: TestClient) -> None:
        client.post("/console/presets", json={"preset_id": "preset-1"})
        r = client.get("/console/presets/preset-1")
        assert r.status_code == 200
        assert r.json()["preset_id"] == "preset-1"

    def test_get_unknown_preset_returns_404(self, client: TestClient) -> None:
        r = client.get("/console/presets/does-not-exist")
        assert r.status_code == 404

    def test_list_presets_shape(self, client: TestClient) -> None:
        client.post("/console/presets", json={"preset_id": "preset-1"})
        r = client.get("/console/presets")
        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert "next_cursor" in body

    def test_list_pagination(self, client: TestClient) -> None:
        for i in range(5):
            client.post("/console/presets", json={"preset_id": f"preset-{i}"})
        r = client.get("/console/presets", params={"limit": 2})
        body = r.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"] is not None

        r2 = client.get(
            "/console/presets",
            params={"limit": 2, "cursor": body["next_cursor"]},
        )
        assert r2.status_code == 200
        assert len(r2.json()["items"]) == 2

    def test_list_invalid_cursor_returns_400(self, client: TestClient) -> None:
        r = client.get("/console/presets", params={"cursor": "not-valid!!"})
        assert r.status_code == 400

    def test_delete_preset(self, client: TestClient) -> None:
        client.post("/console/presets", json={"preset_id": "preset-1"})
        r = client.delete("/console/presets/preset-1")
        assert r.status_code == 204
        assert client.get("/console/presets/preset-1").status_code == 404

    def test_delete_unknown_preset_returns_404(self, client: TestClient) -> None:
        r = client.delete("/console/presets/does-not-exist")
        assert r.status_code == 404


# ── Scope enforcement (real require_user/validate_jwt cold path) ──────────


class TestScopeEnforcement:
    def test_write_requires_presets_write_scope(self, client: TestClient) -> None:
        with _identity(sub="chat-user", scope=READ_SCOPE):
            r = client.post(
                "/console/presets",
                json={"preset_id": "preset-1"},
                headers=_auth_headers("chat-user"),
            )
        assert r.status_code == 403
        assert WRITE_SCOPE in r.json()["detail"]

    def test_read_requires_presets_read_scope(self, client: TestClient) -> None:
        with _identity(sub="chat-user", scope=WRITE_SCOPE):
            r = client.get("/console/presets", headers=_auth_headers("chat-user"))
        assert r.status_code == 403
        assert READ_SCOPE in r.json()["detail"]

    def test_write_scope_alone_cannot_read(self, client: TestClient) -> None:
        """The least-privilege wall: a write-only token cannot list/read
        — read and write are two DISTINCT scopes, never implied by each
        other."""
        with _identity(sub="chat-user", scope=WRITE_SCOPE):
            r = client.get(
                "/console/presets/preset-1", headers=_auth_headers("chat-user")
            )
        assert r.status_code == 403

    def test_read_scope_alone_cannot_write(self, client: TestClient) -> None:
        with _identity(sub="chat-user", scope=READ_SCOPE):
            r = client.delete(
                "/console/presets/preset-1", headers=_auth_headers("chat-user")
            )
        assert r.status_code == 403

    def test_no_scope_token_cannot_write(self, client: TestClient) -> None:
        with _identity(sub="chat-user", scope="audittrace:query"):
            r = client.post(
                "/console/presets",
                json={"preset_id": "preset-1"},
                headers=_auth_headers("chat-user"),
            )
        assert r.status_code == 403


# ── Cross-user isolation (real require_user, distinct real subs) ──────────


class TestCrossUserIsolation:
    """The WU-4 cross-user hijack lesson
    (feedback_per_user_namespace_shared_store_ids), proven through the
    REAL HTTP route with the REAL ``require_user`` cold path — NOT a
    ``dependency_overrides`` swap (see module docstring)."""

    _BOTH_SCOPES = f"{READ_SCOPE} {WRITE_SCOPE}"

    def _act_as(self, client: TestClient, sub: str, method: str, path: str, **kwargs):
        with _identity(sub=sub, scope=self._BOTH_SCOPES):
            return client.request(method, path, headers=_auth_headers(sub), **kwargs)

    def test_user_b_cannot_read_user_as_preset(self, client: TestClient) -> None:
        create = self._act_as(
            client,
            "user-alice",
            "POST",
            "/console/presets",
            json={"preset_id": "alice-preset", "title": "alice's private preset"},
        )
        assert create.status_code == 200

        bob_read = self._act_as(
            client, "user-bob", "GET", "/console/presets/alice-preset"
        )
        assert bob_read.status_code == 404, (
            "user B read user A's preset via the real HTTP route — the "
            "RLS/isolation wall is broken"
        )

        bob_list = self._act_as(client, "user-bob", "GET", "/console/presets")
        ids = [i["preset_id"] for i in bob_list.json()["items"]]
        assert "alice-preset" not in ids, (
            "user B's preset list included user A's preset"
        )

        alice_read = self._act_as(
            client, "user-alice", "GET", "/console/presets/alice-preset"
        )
        assert alice_read.status_code == 200
        assert alice_read.json()["title"] == "alice's private preset"

    def test_user_b_cannot_write_user_as_preset(self, client: TestClient) -> None:
        self._act_as(
            client,
            "user-alice-w",
            "POST",
            "/console/presets",
            json={"preset_id": "alice-preset-w", "title": "original"},
        )

        # Bob upserting the SAME preset_id must create/update HIS OWN
        # row, never alice's — see the service-level non-vacuity guard
        # for the underlying assertion; here we confirm alice's row is
        # untouched through the real route.
        bob_upsert = self._act_as(
            client,
            "user-bob-w",
            "POST",
            "/console/presets",
            json={"preset_id": "alice-preset-w", "title": "hijacked by bob"},
        )
        assert bob_upsert.status_code == 200
        assert bob_upsert.json()["title"] == "hijacked by bob"

        bob_delete = self._act_as(
            client, "user-bob-w", "DELETE", "/console/presets/alice-preset-w"
        )
        assert bob_delete.status_code == 204

        alice_read = self._act_as(
            client, "user-alice-w", "GET", "/console/presets/alice-preset-w"
        )
        assert alice_read.status_code == 200
        assert alice_read.json()["title"] == "original", (
            "user A's preset title/existence was altered by user B — "
            "isolation wall broken"
        )
