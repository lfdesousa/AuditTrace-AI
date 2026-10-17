"""HTTP-route tests for the console-files API (Files-metadata domain,
MongoDB-elimination EPIC).

Mirrors ``test_console_chat_projects_routes.py``'s structure EXACTLY
(the spec's instruction), scaled to the files field set
(filename/type/bytes/object_key/... instead of name/description), plus
one extra guard class for the batch-get-by-ids route this domain adds.
Two identity strategies, per ``feedback_test_through_real_http_route``:

* Most tests use the default ``client`` fixture (``AUDITTRACE_AUTH_REQUIRED
  =false`` bypass — the sentinel user) to exercise route wiring, scope
  declarations, and response shapes without JWT ceremony.
* Cross-user isolation tests drive the REAL ``require_user`` cold path
  (mirrors ``tests/test_console_chat_projects_routes.py``'s
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

READ_SCOPE = "memory:files:read-own"
WRITE_SCOPE = "memory:files:write"


@contextmanager
def _identity(*, sub: str, scope: str):
    """Drive the REAL ``require_user`` cold path for ``sub`` with
    ``scope``, mirroring ``tests/test_console_chat_projects_routes.py``'s
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


class TestConsoleFilesCrud:
    def test_upsert_creates_file(self, client: TestClient) -> None:
        r = client.post(
            "/console/files",
            json={
                "file_id": "file-1",
                "filename": "report.pdf",
                "type": "application/pdf",
                "bytes": 1024,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["file_id"] == "file-1"
        assert body["filename"] == "report.pdf"
        assert body["type"] == "application/pdf"
        assert body["bytes"] == 1024
        assert body["embedded"] is False
        assert "user_sub" not in body, (
            "response must never leak the internal user_sub field"
        )

    def test_upsert_defaults(self, client: TestClient) -> None:
        r = client.post(
            "/console/files",
            json={"file_id": "file-1", "filename": "bare.txt", "type": "text/plain"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["bytes"] == 0
        assert body["object_key"] is None
        assert body["width"] is None
        assert body["height"] is None
        assert body["context"] is None
        assert body["usage"] == {}
        assert body["embedded"] is False
        assert body["temp_file_id"] is None
        assert body["metadata"] == {}

    def test_upsert_full_field_set(self, client: TestClient) -> None:
        r = client.post(
            "/console/files",
            json={
                "file_id": "file-1",
                "filename": "photo.png",
                "type": "image/png",
                "bytes": 2048,
                "object_key": "uploads/u1/photo.png",
                "width": 800,
                "height": 600,
                "context": "message_attachment",
                "usage": {"embed_tokens": 42},
                "embedded": True,
                "temp_file_id": "temp-abc",
                "metadata": {"k": "v"},
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["object_key"] == "uploads/u1/photo.png"
        assert body["width"] == 800
        assert body["height"] == 600
        assert body["context"] == "message_attachment"
        assert body["usage"] == {"embed_tokens": 42}
        assert body["embedded"] is True
        assert body["temp_file_id"] == "temp-abc"
        assert body["metadata"] == {"k": "v"}

    def test_upsert_is_idempotent(self, client: TestClient) -> None:
        client.post(
            "/console/files",
            json={"file_id": "file-1", "filename": "a.txt", "type": "text/plain"},
        )
        r = client.post(
            "/console/files",
            json={"file_id": "file-1", "filename": "b.txt", "type": "text/plain"},
        )
        assert r.status_code == 200
        listed = client.get("/console/files").json()
        matches = [i for i in listed["items"] if i["file_id"] == "file-1"]
        assert len(matches) == 1
        assert matches[0]["filename"] == "b.txt"

    def test_upsert_rejects_missing_file_id(self, client: TestClient) -> None:
        r = client.post(
            "/console/files", json={"filename": "a.txt", "type": "text/plain"}
        )
        assert r.status_code == 422

    def test_upsert_rejects_missing_filename(self, client: TestClient) -> None:
        r = client.post(
            "/console/files", json={"file_id": "file-1", "type": "text/plain"}
        )
        assert r.status_code == 422

    def test_upsert_rejects_missing_type(self, client: TestClient) -> None:
        r = client.post(
            "/console/files", json={"file_id": "file-1", "filename": "a.txt"}
        )
        assert r.status_code == 422

    def test_upsert_rejects_negative_bytes(self, client: TestClient) -> None:
        r = client.post(
            "/console/files",
            json={
                "file_id": "file-1",
                "filename": "a.txt",
                "type": "text/plain",
                "bytes": -1,
            },
        )
        assert r.status_code == 422

    def test_hostile_body_extra_fields_are_dropped_from_response(
        self, client: TestClient
    ) -> None:
        """Shape-only smoke check: Pydantic's ``extra="ignore"`` drops a
        ``user_sub``/``user_id`` field the request model doesn't declare,
        so it never round-trips into the response. This is NOT the
        security proof — a response shape says nothing about which row
        the write landed in; see
        ``TestCrossUserIsolation::test_hostile_body_user_sub_cannot_hijack_another_users_row``
        for the actual non-vacuous side-effect proof (two distinct real
        subs through the real ``require_user`` cold path)."""
        r = client.post(
            "/console/files",
            json={
                "file_id": "file-hostile",
                "filename": "x.txt",
                "type": "text/plain",
                "user_sub": "attacker-sub",
                "user_id": "attacker-sub",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["file_id"] == "file-hostile"
        assert "user_sub" not in body
        assert "user_id" not in body

    def test_get_file(self, client: TestClient) -> None:
        client.post(
            "/console/files",
            json={"file_id": "file-1", "filename": "x.txt", "type": "text/plain"},
        )
        r = client.get("/console/files/file-1")
        assert r.status_code == 200
        assert r.json()["file_id"] == "file-1"

    def test_get_unknown_file_returns_404(self, client: TestClient) -> None:
        r = client.get("/console/files/does-not-exist")
        assert r.status_code == 404

    def test_list_files_shape(self, client: TestClient) -> None:
        client.post(
            "/console/files",
            json={"file_id": "file-1", "filename": "x.txt", "type": "text/plain"},
        )
        r = client.get("/console/files")
        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert "next_cursor" in body

    def test_list_pagination(self, client: TestClient) -> None:
        for i in range(5):
            client.post(
                "/console/files",
                json={
                    "file_id": f"file-{i}",
                    "filename": "p",
                    "type": "text/plain",
                },
            )
        r = client.get("/console/files", params={"limit": 2})
        body = r.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"] is not None

        r2 = client.get(
            "/console/files",
            params={"limit": 2, "cursor": body["next_cursor"]},
        )
        assert r2.status_code == 200
        assert len(r2.json()["items"]) == 2

    def test_list_invalid_cursor_returns_400(self, client: TestClient) -> None:
        r = client.get("/console/files", params={"cursor": "not-valid!!"})
        assert r.status_code == 400

    def test_delete_file(self, client: TestClient) -> None:
        client.post(
            "/console/files",
            json={"file_id": "file-1", "filename": "x.txt", "type": "text/plain"},
        )
        r = client.delete("/console/files/file-1")
        assert r.status_code == 204
        assert client.get("/console/files/file-1").status_code == 404

    def test_delete_unknown_file_returns_404(self, client: TestClient) -> None:
        r = client.delete("/console/files/does-not-exist")
        assert r.status_code == 404


# ── Batch-get-by-ids (the extra read shape this domain adds) ──────────────


class TestConsoleFilesBatchGet:
    def test_batch_get_returns_owned_files_in_request_order(
        self, client: TestClient
    ) -> None:
        for i in range(3):
            client.post(
                "/console/files",
                json={
                    "file_id": f"file-{i}",
                    "filename": f"f{i}.txt",
                    "type": "text/plain",
                },
            )
        r = client.post(
            "/console/files/batch-get",
            json={"file_ids": ["file-2", "file-0", "file-1"]},
        )
        assert r.status_code == 200
        ids = [i["file_id"] for i in r.json()["items"]]
        assert ids == ["file-2", "file-0", "file-1"]

    def test_batch_get_omits_unknown_ids_without_error(
        self, client: TestClient
    ) -> None:
        client.post(
            "/console/files",
            json={"file_id": "file-1", "filename": "x.txt", "type": "text/plain"},
        )
        r = client.post(
            "/console/files/batch-get",
            json={"file_ids": ["file-1", "does-not-exist"]},
        )
        assert r.status_code == 200
        ids = [i["file_id"] for i in r.json()["items"]]
        assert ids == ["file-1"]

    def test_batch_get_rejects_empty_list(self, client: TestClient) -> None:
        r = client.post("/console/files/batch-get", json={"file_ids": []})
        assert r.status_code == 422

    def test_batch_get_rejects_oversized_list(self, client: TestClient) -> None:
        r = client.post(
            "/console/files/batch-get",
            json={"file_ids": [f"file-{i}" for i in range(201)]},
        )
        assert r.status_code == 422

    def test_batch_get_excludes_deleted_files(self, client: TestClient) -> None:
        client.post(
            "/console/files",
            json={"file_id": "file-1", "filename": "x.txt", "type": "text/plain"},
        )
        client.delete("/console/files/file-1")
        r = client.post("/console/files/batch-get", json={"file_ids": ["file-1"]})
        assert r.status_code == 200
        assert r.json()["items"] == []


# ── Scope enforcement (real require_user/validate_jwt cold path) ──────────


class TestScopeEnforcement:
    def test_write_requires_files_write_scope(self, client: TestClient) -> None:
        with _identity(sub="file-user", scope=READ_SCOPE):
            r = client.post(
                "/console/files",
                json={"file_id": "file-1", "filename": "x.txt", "type": "text/plain"},
                headers=_auth_headers("file-user"),
            )
        assert r.status_code == 403
        assert WRITE_SCOPE in r.json()["detail"]

    def test_read_requires_files_read_scope(self, client: TestClient) -> None:
        with _identity(sub="file-user", scope=WRITE_SCOPE):
            r = client.get("/console/files", headers=_auth_headers("file-user"))
        assert r.status_code == 403
        assert READ_SCOPE in r.json()["detail"]

    def test_write_scope_alone_cannot_read(self, client: TestClient) -> None:
        """The least-privilege wall: a write-only token cannot list/read
        — read and write are two DISTINCT scopes, never implied by each
        other."""
        with _identity(sub="file-user", scope=WRITE_SCOPE):
            r = client.get("/console/files/file-1", headers=_auth_headers("file-user"))
        assert r.status_code == 403

    def test_read_scope_alone_cannot_write(self, client: TestClient) -> None:
        with _identity(sub="file-user", scope=READ_SCOPE):
            r = client.delete(
                "/console/files/file-1", headers=_auth_headers("file-user")
            )
        assert r.status_code == 403

    def test_no_scope_token_cannot_write(self, client: TestClient) -> None:
        with _identity(sub="file-user", scope="audittrace:query"):
            r = client.post(
                "/console/files",
                json={"file_id": "file-1", "filename": "x.txt", "type": "text/plain"},
                headers=_auth_headers("file-user"),
            )
        assert r.status_code == 403

    def test_write_scope_alone_cannot_batch_get(self, client: TestClient) -> None:
        """Batch-get is gated on the READ scope despite the POST verb —
        a write-only token cannot use it."""
        with _identity(sub="file-user", scope=WRITE_SCOPE):
            r = client.post(
                "/console/files/batch-get",
                json={"file_ids": ["file-1"]},
                headers=_auth_headers("file-user"),
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

    def test_user_b_cannot_read_user_as_file(self, client: TestClient) -> None:
        create = self._act_as(
            client,
            "user-alice",
            "POST",
            "/console/files",
            json={
                "file_id": "alice-file",
                "filename": "alice-secret.pdf",
                "type": "application/pdf",
            },
        )
        assert create.status_code == 200

        bob_read = self._act_as(client, "user-bob", "GET", "/console/files/alice-file")
        assert bob_read.status_code == 404, (
            "user B read user A's file via the real HTTP route — the "
            "RLS/isolation wall is broken"
        )

        bob_list = self._act_as(client, "user-bob", "GET", "/console/files")
        ids = [i["file_id"] for i in bob_list.json()["items"]]
        assert "alice-file" not in ids, "user B's file list included user A's file"

        bob_batch = self._act_as(
            client,
            "user-bob",
            "POST",
            "/console/files/batch-get",
            json={"file_ids": ["alice-file"]},
        )
        assert bob_batch.json()["items"] == [], (
            "user B's batch-get returned user A's file — the isolation wall is broken"
        )

        alice_read = self._act_as(
            client, "user-alice", "GET", "/console/files/alice-file"
        )
        assert alice_read.status_code == 200
        assert alice_read.json()["filename"] == "alice-secret.pdf"

    def test_user_b_cannot_write_user_as_file(self, client: TestClient) -> None:
        self._act_as(
            client,
            "user-alice-w",
            "POST",
            "/console/files",
            json={
                "file_id": "alice-file-w",
                "filename": "original.txt",
                "type": "text/plain",
            },
        )

        # Bob upserting the SAME file_id must create/update HIS OWN row,
        # never alice's — see the service-level non-vacuity guard for
        # the underlying assertion; here we confirm alice's row is
        # untouched through the real route.
        bob_upsert = self._act_as(
            client,
            "user-bob-w",
            "POST",
            "/console/files",
            json={
                "file_id": "alice-file-w",
                "filename": "hijacked-by-bob.txt",
                "type": "text/plain",
            },
        )
        assert bob_upsert.status_code == 200
        assert bob_upsert.json()["filename"] == "hijacked-by-bob.txt"

        bob_delete = self._act_as(
            client, "user-bob-w", "DELETE", "/console/files/alice-file-w"
        )
        assert bob_delete.status_code == 204

        alice_read = self._act_as(
            client, "user-alice-w", "GET", "/console/files/alice-file-w"
        )
        assert alice_read.status_code == 200
        assert alice_read.json()["filename"] == "original.txt", (
            "user A's file name/existence was altered by user B — isolation wall broken"
        )

    def test_hostile_body_user_sub_cannot_hijack_another_users_row(
        self, client: TestClient
    ) -> None:
        """The real non-vacuous proof that a hostile ``user_sub``/
        ``user_id`` body field is IGNORED, not merely absent from the
        response shape — driven through TWO DISTINCT real subs via the
        real ``require_user`` cold path (the single-identity sentinel
        ``client`` fixture used by
        ``TestConsoleFilesCrud::test_hostile_body_extra_fields_are_dropped_from_response``
        cannot observe cross-user ownership at all, since every request
        in that fixture resolves to the SAME sentinel sub).

        Falsifiable: if a future change added a ``user_sub`` field to
        ``ConsoleFileUpsertRequest`` AND the route honored it (e.g. via
        ``dataclasses.replace(user, user_id=body.user_sub)``), the
        attacker's writes below would land in/leak into the victim's
        rows and this test would go RED — proven by a builder-side
        neuter pass (temporarily reintroducing exactly that injection)
        reported in the evidence file.
        """
        # The victim already owns a row under her OWN real identity.
        victim_create = self._act_as(
            client,
            "victim-hostile",
            "POST",
            "/console/files",
            json={
                "file_id": "shared-hostile-id",
                "filename": "victims-real-file.txt",
                "type": "text/plain",
            },
        )
        assert victim_create.status_code == 200

        # The attacker upserts the SAME file_id, with a hostile body
        # claiming (via user_sub AND user_id) to BE the victim — an
        # attempted overwrite/hijack of the victim's existing row.
        attacker_upsert = self._act_as(
            client,
            "attacker-hostile",
            "POST",
            "/console/files",
            json={
                "file_id": "shared-hostile-id",
                "filename": "hijacked-by-attacker.txt",
                "type": "text/plain",
                "user_sub": "victim-hostile",
                "user_id": "victim-hostile",
            },
        )
        assert attacker_upsert.status_code == 200

        # The victim's row must be COMPLETELY UNTOUCHED — the hostile
        # user_sub/user_id body fields must never redirect the write
        # into the victim's row.
        victim_read = self._act_as(
            client, "victim-hostile", "GET", "/console/files/shared-hostile-id"
        )
        assert victim_read.status_code == 200
        assert victim_read.json()["filename"] == "victims-real-file.txt", (
            "attacker's hostile user_sub/user_id body fields hijacked/"
            "overwrote the victim's file — the fields must be silently "
            "ignored, never honored"
        )

        # The attacker's write must have landed under the ATTACKER's OWN
        # real identity (RLS-isolated from the victim's row with the
        # same file_id), not the victim's.
        attacker_read = self._act_as(
            client, "attacker-hostile", "GET", "/console/files/shared-hostile-id"
        )
        assert attacker_read.status_code == 200
        assert attacker_read.json()["filename"] == "hijacked-by-attacker.txt"

        # A second hostile upsert, this time under a FRESH file_id, must
        # never be visible to the victim via get/list/batch-get —
        # proving the hostile field cannot plant a row the victim can
        # see either.
        planted = self._act_as(
            client,
            "attacker-hostile",
            "POST",
            "/console/files",
            json={
                "file_id": "planted-hostile-id",
                "filename": "planted.txt",
                "type": "text/plain",
                "user_sub": "victim-hostile",
                "user_id": "victim-hostile",
            },
        )
        assert planted.status_code == 200

        victim_get_planted = self._act_as(
            client, "victim-hostile", "GET", "/console/files/planted-hostile-id"
        )
        assert victim_get_planted.status_code == 404, (
            "the victim can read a file planted via a hostile user_sub "
            "body field — the field must never be honored"
        )

        victim_list = self._act_as(client, "victim-hostile", "GET", "/console/files")
        victim_ids = [i["file_id"] for i in victim_list.json()["items"]]
        assert "planted-hostile-id" not in victim_ids, (
            "the victim's list included a file planted via a hostile "
            "user_sub body field"
        )

        victim_batch = self._act_as(
            client,
            "victim-hostile",
            "POST",
            "/console/files/batch-get",
            json={"file_ids": ["planted-hostile-id"]},
        )
        assert victim_batch.json()["items"] == [], (
            "the victim's batch-get returned a file planted via a "
            "hostile user_sub body field"
        )
