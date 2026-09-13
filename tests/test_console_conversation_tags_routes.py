"""HTTP-route tests for the console-conversation-tags API
(Conversation-Tags domain, MongoDB-elimination EPIC).

Mirrors ``test_console_chat_projects_routes.py``'s structure EXACTLY
(the spec's instruction), scaled to the conversation-tags field set
(tag/description/count/position instead of chat_project_id/name/
description). Two identity strategies, per
``feedback_test_through_real_http_route``:

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

READ_SCOPE = "memory:conversation_tags:read-own"
WRITE_SCOPE = "memory:conversation_tags:write"


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


class TestConsoleConversationTagsCrud:
    def test_upsert_creates_conversation_tag(self, client: TestClient) -> None:
        r = client.post(
            "/console/conversation-tags",
            json={"tag": "work", "description": "work chats", "count": 3},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["tag"] == "work"
        assert body["description"] == "work chats"
        assert body["count"] == 3
        assert "user_sub" not in body, (
            "response must never leak the internal user_sub field"
        )

    def test_upsert_defaults_description_count_position(
        self, client: TestClient
    ) -> None:
        r = client.post("/console/conversation-tags", json={"tag": "bare"})
        assert r.status_code == 200
        body = r.json()
        assert body["description"] == ""
        assert body["count"] == 0
        assert body["position"] == 0

    def test_upsert_is_idempotent(self, client: TestClient) -> None:
        client.post(
            "/console/conversation-tags", json={"tag": "work", "description": "A"}
        )
        r = client.post(
            "/console/conversation-tags", json={"tag": "work", "description": "B"}
        )
        assert r.status_code == 200
        listed = client.get("/console/conversation-tags").json()
        matches = [i for i in listed["items"] if i["tag"] == "work"]
        assert len(matches) == 1
        assert matches[0]["description"] == "B"

    def test_upsert_rejects_missing_tag(self, client: TestClient) -> None:
        r = client.post("/console/conversation-tags", json={"description": "no tag"})
        assert r.status_code == 422

    def test_upsert_rejects_negative_count(self, client: TestClient) -> None:
        r = client.post("/console/conversation-tags", json={"tag": "work", "count": -1})
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
            "/console/conversation-tags",
            json={
                "tag": "hostile",
                "user_sub": "attacker-sub",
                "user_id": "attacker-sub",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["tag"] == "hostile"
        assert "user_sub" not in body
        assert "user_id" not in body

    def test_get_conversation_tag(self, client: TestClient) -> None:
        client.post("/console/conversation-tags", json={"tag": "work"})
        r = client.get("/console/conversation-tags/work")
        assert r.status_code == 200
        assert r.json()["tag"] == "work"

    def test_get_unknown_conversation_tag_returns_404(self, client: TestClient) -> None:
        r = client.get("/console/conversation-tags/does-not-exist")
        assert r.status_code == 404

    def test_list_conversation_tags_shape(self, client: TestClient) -> None:
        client.post("/console/conversation-tags", json={"tag": "work"})
        r = client.get("/console/conversation-tags")
        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert "next_cursor" in body

    def test_list_pagination(self, client: TestClient) -> None:
        for i in range(5):
            client.post("/console/conversation-tags", json={"tag": f"tag-{i}"})
        r = client.get("/console/conversation-tags", params={"limit": 2})
        body = r.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"] is not None

        r2 = client.get(
            "/console/conversation-tags",
            params={"limit": 2, "cursor": body["next_cursor"]},
        )
        assert r2.status_code == 200
        assert len(r2.json()["items"]) == 2

    def test_list_invalid_cursor_returns_400(self, client: TestClient) -> None:
        r = client.get("/console/conversation-tags", params={"cursor": "not-valid!!"})
        assert r.status_code == 400

    def test_delete_conversation_tag(self, client: TestClient) -> None:
        client.post("/console/conversation-tags", json={"tag": "work"})
        r = client.delete("/console/conversation-tags/work")
        assert r.status_code == 204
        assert client.get("/console/conversation-tags/work").status_code == 404

    def test_delete_unknown_conversation_tag_returns_404(
        self, client: TestClient
    ) -> None:
        r = client.delete("/console/conversation-tags/does-not-exist")
        assert r.status_code == 404

    def test_get_and_delete_tag_containing_slash(self, client: TestClient) -> None:
        """The ``:path`` route converter accepts a tag containing a
        ``/`` — a client-supplied free-form string, unlike the plain
        chat-project-id shape this route otherwise mirrors."""
        client.post("/console/conversation-tags", json={"tag": "work/urgent"})
        r = client.get("/console/conversation-tags/work/urgent")
        assert r.status_code == 200
        assert r.json()["tag"] == "work/urgent"

        d = client.delete("/console/conversation-tags/work/urgent")
        assert d.status_code == 204


# ── Scope enforcement (real require_user/validate_jwt cold path) ──────────


class TestScopeEnforcement:
    def test_write_requires_conversation_tags_write_scope(
        self, client: TestClient
    ) -> None:
        with _identity(sub="tag-user", scope=READ_SCOPE):
            r = client.post(
                "/console/conversation-tags",
                json={"tag": "work"},
                headers=_auth_headers("tag-user"),
            )
        assert r.status_code == 403
        assert WRITE_SCOPE in r.json()["detail"]

    def test_read_requires_conversation_tags_read_scope(
        self, client: TestClient
    ) -> None:
        with _identity(sub="tag-user", scope=WRITE_SCOPE):
            r = client.get(
                "/console/conversation-tags", headers=_auth_headers("tag-user")
            )
        assert r.status_code == 403
        assert READ_SCOPE in r.json()["detail"]

    def test_write_scope_alone_cannot_read(self, client: TestClient) -> None:
        """The least-privilege wall: a write-only token cannot list/read
        — read and write are two DISTINCT scopes, never implied by each
        other."""
        with _identity(sub="tag-user", scope=WRITE_SCOPE):
            r = client.get(
                "/console/conversation-tags/work", headers=_auth_headers("tag-user")
            )
        assert r.status_code == 403

    def test_read_scope_alone_cannot_write(self, client: TestClient) -> None:
        with _identity(sub="tag-user", scope=READ_SCOPE):
            r = client.delete(
                "/console/conversation-tags/work", headers=_auth_headers("tag-user")
            )
        assert r.status_code == 403

    def test_no_scope_token_cannot_write(self, client: TestClient) -> None:
        with _identity(sub="tag-user", scope="audittrace:query"):
            r = client.post(
                "/console/conversation-tags",
                json={"tag": "work"},
                headers=_auth_headers("tag-user"),
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

    def test_user_b_cannot_read_user_as_conversation_tag(
        self, client: TestClient
    ) -> None:
        create = self._act_as(
            client,
            "user-alice",
            "POST",
            "/console/conversation-tags",
            json={"tag": "alice-tag", "description": "alice's private tag"},
        )
        assert create.status_code == 200

        bob_read = self._act_as(
            client, "user-bob", "GET", "/console/conversation-tags/alice-tag"
        )
        assert bob_read.status_code == 404, (
            "user B read user A's conversation-tag via the real HTTP "
            "route — the RLS/isolation wall is broken"
        )

        bob_list = self._act_as(client, "user-bob", "GET", "/console/conversation-tags")
        tags = [i["tag"] for i in bob_list.json()["items"]]
        assert "alice-tag" not in tags, (
            "user B's conversation-tag list included user A's tag"
        )

        alice_read = self._act_as(
            client, "user-alice", "GET", "/console/conversation-tags/alice-tag"
        )
        assert alice_read.status_code == 200
        assert alice_read.json()["description"] == "alice's private tag"

    def test_user_b_cannot_write_user_as_conversation_tag(
        self, client: TestClient
    ) -> None:
        self._act_as(
            client,
            "user-alice-w",
            "POST",
            "/console/conversation-tags",
            json={"tag": "alice-tag-w", "description": "original"},
        )

        # Bob upserting the SAME tag must create/update HIS OWN row,
        # never alice's — see the service-level non-vacuity guard for
        # the underlying assertion; here we confirm alice's row is
        # untouched through the real route.
        bob_upsert = self._act_as(
            client,
            "user-bob-w",
            "POST",
            "/console/conversation-tags",
            json={"tag": "alice-tag-w", "description": "hijacked by bob"},
        )
        assert bob_upsert.status_code == 200
        assert bob_upsert.json()["description"] == "hijacked by bob"

        bob_delete = self._act_as(
            client, "user-bob-w", "DELETE", "/console/conversation-tags/alice-tag-w"
        )
        assert bob_delete.status_code == 204

        alice_read = self._act_as(
            client, "user-alice-w", "GET", "/console/conversation-tags/alice-tag-w"
        )
        assert alice_read.status_code == 200
        assert alice_read.json()["description"] == "original", (
            "user A's conversation-tag description/existence was altered "
            "by user B — isolation wall broken"
        )

    def test_hostile_body_user_sub_cannot_hijack_another_users_row(
        self, client: TestClient
    ) -> None:
        """The real non-vacuous proof that a hostile ``user_sub``/
        ``user_id`` body field is IGNORED, not merely absent from the
        response shape — driven through TWO DISTINCT real subs via the
        real ``require_user`` cold path (the single-identity sentinel
        ``client`` fixture used by
        ``TestConsoleConversationTagsCrud::test_hostile_body_extra_fields_are_dropped_from_response``
        cannot observe cross-user ownership at all, since every request
        in that fixture resolves to the SAME sentinel sub).

        Falsifiable: if a future change added a ``user_sub`` field to
        ``ConsoleConversationTagUpsertRequest`` AND the route honored it
        (e.g. via ``dataclasses.replace(user, user_id=body.user_sub)``),
        the attacker's writes below would land in/leak into the victim's
        rows and this test would go RED — proven by a builder-side
        neuter pass (temporarily reintroducing exactly that injection)
        reported in the evidence file.
        """
        # The victim already owns a row under her OWN real identity.
        victim_create = self._act_as(
            client,
            "victim-hostile",
            "POST",
            "/console/conversation-tags",
            json={"tag": "shared-hostile-tag", "description": "victim's real tag"},
        )
        assert victim_create.status_code == 200

        # The attacker upserts the SAME tag, with a hostile body
        # claiming (via user_sub AND user_id) to BE the victim — an
        # attempted overwrite/hijack of the victim's existing row.
        attacker_upsert = self._act_as(
            client,
            "attacker-hostile",
            "POST",
            "/console/conversation-tags",
            json={
                "tag": "shared-hostile-tag",
                "description": "hijacked by attacker",
                "user_sub": "victim-hostile",
                "user_id": "victim-hostile",
            },
        )
        assert attacker_upsert.status_code == 200

        # The victim's row must be COMPLETELY UNTOUCHED — the hostile
        # user_sub/user_id body fields must never redirect the write
        # into the victim's row.
        victim_read = self._act_as(
            client,
            "victim-hostile",
            "GET",
            "/console/conversation-tags/shared-hostile-tag",
        )
        assert victim_read.status_code == 200
        assert victim_read.json()["description"] == "victim's real tag", (
            "attacker's hostile user_sub/user_id body fields hijacked/"
            "overwrote the victim's conversation-tag — the fields must "
            "be silently ignored, never honored"
        )

        # The attacker's write must have landed under the ATTACKER's OWN
        # real identity (RLS-isolated from the victim's row with the
        # same tag), not the victim's.
        attacker_read = self._act_as(
            client,
            "attacker-hostile",
            "GET",
            "/console/conversation-tags/shared-hostile-tag",
        )
        assert attacker_read.status_code == 200
        assert attacker_read.json()["description"] == "hijacked by attacker"

        # A second hostile upsert, this time under a FRESH tag, must
        # never be visible to the victim via get/list — proving the
        # hostile field cannot plant a row the victim can see either.
        planted = self._act_as(
            client,
            "attacker-hostile",
            "POST",
            "/console/conversation-tags",
            json={
                "tag": "planted-hostile-tag",
                "description": "planted",
                "user_sub": "victim-hostile",
                "user_id": "victim-hostile",
            },
        )
        assert planted.status_code == 200

        victim_get_planted = self._act_as(
            client,
            "victim-hostile",
            "GET",
            "/console/conversation-tags/planted-hostile-tag",
        )
        assert victim_get_planted.status_code == 404, (
            "the victim can read a conversation-tag planted via a "
            "hostile user_sub body field — the field must never be "
            "honored"
        )

        victim_list = self._act_as(
            client, "victim-hostile", "GET", "/console/conversation-tags"
        )
        victim_tags = [i["tag"] for i in victim_list.json()["items"]]
        assert "planted-hostile-tag" not in victim_tags, (
            "the victim's list included a conversation-tag planted via "
            "a hostile user_sub body field"
        )
