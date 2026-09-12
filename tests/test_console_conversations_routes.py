"""HTTP-route tests for the console-conversations API (WU-1, MongoDB-
elimination EPIC).

Two identity strategies, per ``feedback_test_through_real_http_route``:

* Most tests use the default ``client`` fixture (``AUDITTRACE_AUTH_REQUIRED
  =false`` bypass — the sentinel user) to exercise route wiring, scope
  declarations, and response shapes without JWT ceremony.
* Cross-user isolation tests drive the REAL ``require_user`` cold path
  (mirrors ``tests/test_memory_routes.py``'s
  ``TestSessionWriteScope*``/session-layer tests): ``auth_required=True``
  + a patched ``_decode_jwt_with_allowed_issuers`` returning distinct
  ``sub``s and the required scopes, so ``require_user`` genuinely binds
  the RLS ContextVar per caller (not just a
  ``dependency_overrides[require_user]`` swap, which would replace
  ``require_user``'s body and never exercise
  ``set_current_user_id`` — see ``tests/test_mcp_rls_isolation.py``'s
  module docstring for why that distinction matters).
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

READ_SCOPE = "memory:conversations:read-own"
WRITE_SCOPE = "memory:conversations:write"


@contextmanager
def _identity(*, sub: str, scope: str):
    """Drive the REAL ``require_user`` cold path for ``sub`` with
    ``scope``, mirroring ``tests/test_memory_routes.py``'s session-layer
    auth patches — NOT a ``dependency_overrides`` swap (see module
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


class TestConsoleConversationsCrud:
    def test_upsert_creates_conversation(self, client: TestClient) -> None:
        r = client.post(
            "/console/conversations",
            json={"conversation_id": "conv-1", "title": "Hello"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["conversation_id"] == "conv-1"
        assert body["title"] == "Hello"
        assert body["is_temporary"] is False
        assert "user_sub" not in body, (
            "response must never leak the internal user_sub field"
        )

    def test_upsert_defaults_title(self, client: TestClient) -> None:
        r = client.post("/console/conversations", json={"conversation_id": "conv-1"})
        assert r.status_code == 200
        assert r.json()["title"] == "New Chat"

    def test_upsert_is_idempotent(self, client: TestClient) -> None:
        client.post(
            "/console/conversations", json={"conversation_id": "conv-1", "title": "A"}
        )
        r = client.post(
            "/console/conversations", json={"conversation_id": "conv-1", "title": "B"}
        )
        assert r.status_code == 200
        listed = client.get("/console/conversations").json()
        matches = [i for i in listed["items"] if i["conversation_id"] == "conv-1"]
        assert len(matches) == 1
        assert matches[0]["title"] == "B"

    def test_upsert_rejects_missing_conversation_id(self, client: TestClient) -> None:
        r = client.post("/console/conversations", json={"title": "no id"})
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
        (and the pre-existing
        ``test_hostile_body_user_sub_never_lands_as_another_real_user``)
        for the actual non-vacuous side-effect proofs (two distinct real
        subs through the real ``require_user`` cold path)."""
        r = client.post(
            "/console/conversations",
            json={
                "conversation_id": "conv-hostile",
                "title": "hi",
                "user_sub": "attacker-sub",
                "user_id": "attacker-sub",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["conversation_id"] == "conv-hostile"
        assert "user_sub" not in body
        assert "user_id" not in body

    def test_get_conversation(self, client: TestClient) -> None:
        client.post("/console/conversations", json={"conversation_id": "conv-1"})
        r = client.get("/console/conversations/conv-1")
        assert r.status_code == 200
        assert r.json()["conversation_id"] == "conv-1"

    def test_get_unknown_conversation_returns_404(self, client: TestClient) -> None:
        r = client.get("/console/conversations/does-not-exist")
        assert r.status_code == 404

    def test_list_conversations_shape(self, client: TestClient) -> None:
        client.post("/console/conversations", json={"conversation_id": "conv-1"})
        r = client.get("/console/conversations")
        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert "next_cursor" in body

    def test_list_pagination(self, client: TestClient) -> None:
        for i in range(5):
            client.post("/console/conversations", json={"conversation_id": f"conv-{i}"})
        r = client.get("/console/conversations", params={"limit": 2})
        body = r.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"] is not None

        r2 = client.get(
            "/console/conversations",
            params={"limit": 2, "cursor": body["next_cursor"]},
        )
        assert r2.status_code == 200
        assert len(r2.json()["items"]) == 2

    def test_list_invalid_cursor_returns_400(self, client: TestClient) -> None:
        r = client.get("/console/conversations", params={"cursor": "not-valid!!"})
        assert r.status_code == 400

    def test_update_title(self, client: TestClient) -> None:
        client.post("/console/conversations", json={"conversation_id": "conv-1"})
        r = client.patch("/console/conversations/conv-1", json={"title": "Renamed"})
        assert r.status_code == 200
        assert r.json()["title"] == "Renamed"

    def test_update_title_unknown_conversation_returns_404(
        self, client: TestClient
    ) -> None:
        r = client.patch("/console/conversations/does-not-exist", json={"title": "x"})
        assert r.status_code == 404

    def test_update_title_rejects_empty_string(self, client: TestClient) -> None:
        client.post("/console/conversations", json={"conversation_id": "conv-1"})
        r = client.patch("/console/conversations/conv-1", json={"title": ""})
        assert r.status_code == 422

    def test_delete_conversation(self, client: TestClient) -> None:
        client.post("/console/conversations", json={"conversation_id": "conv-1"})
        r = client.delete("/console/conversations/conv-1")
        assert r.status_code == 204
        assert client.get("/console/conversations/conv-1").status_code == 404

    def test_delete_unknown_conversation_returns_404(self, client: TestClient) -> None:
        r = client.delete("/console/conversations/does-not-exist")
        assert r.status_code == 404


class TestConsoleMessagesCrud:
    def test_get_messages_requires_existing_conversation(
        self, client: TestClient
    ) -> None:
        r = client.get("/console/conversations/does-not-exist/messages")
        assert r.status_code == 404

    def test_upsert_and_list_messages(self, client: TestClient) -> None:
        client.post("/console/conversations", json={"conversation_id": "conv-1"})
        r = client.post(
            "/console/conversations/conv-1/messages",
            json={
                "message_id": "msg-1",
                "sender": "user",
                "text": "hello",
                "is_created_by_user": True,
            },
        )
        assert r.status_code == 200
        assert r.json()["text"] == "hello"

        listed = client.get("/console/conversations/conv-1/messages")
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert len(items) == 1
        assert items[0]["message_id"] == "msg-1"
        assert "user_sub" not in items[0]

    def test_message_tree_parent_link(self, client: TestClient) -> None:
        client.post("/console/conversations", json={"conversation_id": "conv-1"})
        client.post(
            "/console/conversations/conv-1/messages",
            json={
                "message_id": "msg-root",
                "sender": "user",
                "text": "root",
                "is_created_by_user": True,
            },
        )
        client.post(
            "/console/conversations/conv-1/messages",
            json={
                "message_id": "msg-child",
                "parent_message_id": "msg-root",
                "sender": "assistant",
                "text": "child",
                "is_created_by_user": False,
            },
        )
        items = client.get("/console/conversations/conv-1/messages").json()["items"]
        assert [i["message_id"] for i in items] == ["msg-root", "msg-child"]
        assert items[1]["parent_message_id"] == "msg-root"

    def test_edit_message(self, client: TestClient) -> None:
        client.post("/console/conversations", json={"conversation_id": "conv-1"})
        client.post(
            "/console/conversations/conv-1/messages",
            json={
                "message_id": "msg-1",
                "sender": "user",
                "text": "original",
                "is_created_by_user": True,
            },
        )
        r = client.patch(
            "/console/conversations/conv-1/messages/msg-1",
            json={"text": "edited"},
        )
        assert r.status_code == 200
        assert r.json()["text"] == "edited"

    def test_edit_unknown_message_returns_404(self, client: TestClient) -> None:
        client.post("/console/conversations", json={"conversation_id": "conv-1"})
        r = client.patch(
            "/console/conversations/conv-1/messages/nope",
            json={"text": "x"},
        )
        assert r.status_code == 404

    def test_delete_message(self, client: TestClient) -> None:
        client.post("/console/conversations", json={"conversation_id": "conv-1"})
        client.post(
            "/console/conversations/conv-1/messages",
            json={
                "message_id": "msg-1",
                "sender": "user",
                "text": "hi",
                "is_created_by_user": True,
            },
        )
        r = client.delete("/console/conversations/conv-1/messages/msg-1")
        assert r.status_code == 204
        items = client.get("/console/conversations/conv-1/messages").json()["items"]
        assert items == []

    def test_delete_unknown_message_returns_404(self, client: TestClient) -> None:
        client.post("/console/conversations", json={"conversation_id": "conv-1"})
        r = client.delete("/console/conversations/conv-1/messages/nope")
        assert r.status_code == 404


# ── Scope enforcement (real require_user/validate_jwt cold path) ──────────


class TestScopeEnforcement:
    def test_write_requires_conversations_write_scope(self, client: TestClient) -> None:
        with _identity(sub="chat-user", scope=READ_SCOPE):
            r = client.post(
                "/console/conversations",
                json={"conversation_id": "conv-1"},
                headers=_auth_headers("chat-user"),
            )
        assert r.status_code == 403
        assert WRITE_SCOPE in r.json()["detail"]

    def test_read_requires_conversations_read_scope(self, client: TestClient) -> None:
        with _identity(sub="chat-user", scope=WRITE_SCOPE):
            r = client.get("/console/conversations", headers=_auth_headers("chat-user"))
        assert r.status_code == 403
        assert READ_SCOPE in r.json()["detail"]

    def test_write_scope_alone_cannot_read(self, client: TestClient) -> None:
        """The least-privilege wall: a write-only token cannot list/read
        — read and write are two DISTINCT scopes, never implied by each
        other."""
        with _identity(sub="chat-user", scope=WRITE_SCOPE):
            r = client.get(
                "/console/conversations/conv-1", headers=_auth_headers("chat-user")
            )
        assert r.status_code == 403

    def test_read_scope_alone_cannot_write(self, client: TestClient) -> None:
        with _identity(sub="chat-user", scope=READ_SCOPE):
            r = client.delete(
                "/console/conversations/conv-1", headers=_auth_headers("chat-user")
            )
        assert r.status_code == 403

    def test_no_scope_token_cannot_write(self, client: TestClient) -> None:
        with _identity(sub="chat-user", scope="audittrace:query"):
            r = client.post(
                "/console/conversations",
                json={"conversation_id": "conv-1"},
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

    def test_user_b_cannot_read_user_as_conversation(self, client: TestClient) -> None:
        create = self._act_as(
            client,
            "user-alice",
            "POST",
            "/console/conversations",
            json={"conversation_id": "alice-conv", "title": "alice's private chat"},
        )
        assert create.status_code == 200

        bob_read = self._act_as(
            client, "user-bob", "GET", "/console/conversations/alice-conv"
        )
        assert bob_read.status_code == 404, (
            "user B read user A's conversation via the real HTTP route — "
            "the RLS/isolation wall is broken"
        )

        bob_list = self._act_as(client, "user-bob", "GET", "/console/conversations")
        ids = [i["conversation_id"] for i in bob_list.json()["items"]]
        assert "alice-conv" not in ids, (
            "user B's conversation list included user A's conversation"
        )

        alice_read = self._act_as(
            client, "user-alice", "GET", "/console/conversations/alice-conv"
        )
        assert alice_read.status_code == 200
        assert alice_read.json()["title"] == "alice's private chat"

    def test_user_b_cannot_write_user_as_conversation(self, client: TestClient) -> None:
        self._act_as(
            client,
            "user-alice-w",
            "POST",
            "/console/conversations",
            json={"conversation_id": "alice-conv-w", "title": "original"},
        )

        bob_patch = self._act_as(
            client,
            "user-bob-w",
            "PATCH",
            "/console/conversations/alice-conv-w",
            json={"title": "hijacked by bob"},
        )
        assert bob_patch.status_code == 404

        bob_delete = self._act_as(
            client, "user-bob-w", "DELETE", "/console/conversations/alice-conv-w"
        )
        assert bob_delete.status_code == 404

        alice_read = self._act_as(
            client, "user-alice-w", "GET", "/console/conversations/alice-conv-w"
        )
        assert alice_read.status_code == 200
        assert alice_read.json()["title"] == "original", (
            "user A's conversation title/existence was altered by user B — "
            "isolation wall broken"
        )

    def test_user_b_cannot_read_user_as_messages(self, client: TestClient) -> None:
        self._act_as(
            client,
            "user-alice-m",
            "POST",
            "/console/conversations",
            json={"conversation_id": "alice-conv-m"},
        )
        self._act_as(
            client,
            "user-alice-m",
            "POST",
            "/console/conversations/alice-conv-m/messages",
            json={
                "message_id": "alice-msg",
                "sender": "user",
                "text": "alice's secret message",
                "is_created_by_user": True,
            },
        )

        bob_messages = self._act_as(
            client, "user-bob-m", "GET", "/console/conversations/alice-conv-m/messages"
        )
        # Bob doesn't own the conversation at all -> 404, never a 200
        # with alice's (or an empty) message list that would confirm the
        # conversation_id exists.
        assert bob_messages.status_code == 404

    def test_user_b_cannot_write_user_as_message(self, client: TestClient) -> None:
        self._act_as(
            client,
            "user-alice-mw",
            "POST",
            "/console/conversations",
            json={"conversation_id": "alice-conv-mw"},
        )
        self._act_as(
            client,
            "user-alice-mw",
            "POST",
            "/console/conversations/alice-conv-mw/messages",
            json={
                "message_id": "alice-msg-mw",
                "sender": "user",
                "text": "original",
                "is_created_by_user": True,
            },
        )

        bob_edit = self._act_as(
            client,
            "user-bob-mw",
            "PATCH",
            "/console/conversations/alice-conv-mw/messages/alice-msg-mw",
            json={"text": "hijacked"},
        )
        assert bob_edit.status_code == 404

        bob_delete = self._act_as(
            client,
            "user-bob-mw",
            "DELETE",
            "/console/conversations/alice-conv-mw/messages/alice-msg-mw",
        )
        assert bob_delete.status_code == 404

        alice_messages = self._act_as(
            client,
            "user-alice-mw",
            "GET",
            "/console/conversations/alice-conv-mw/messages",
        )
        items = alice_messages.json()["items"]
        assert len(items) == 1
        assert items[0]["text"] == "original", (
            "user A's message was altered/removed by user B — isolation wall broken"
        )

    def test_hostile_body_user_sub_never_lands_as_another_real_user(
        self, client: TestClient
    ) -> None:
        """Bob POSTs a conversation with a hostile ``user_sub`` claiming
        to be Alice. The row must land under BOB's real (token-derived)
        identity, never Alice's — proven by Alice never seeing it."""
        self._act_as(
            client,
            "user-bob-hostile",
            "POST",
            "/console/conversations",
            json={
                "conversation_id": "hostile-conv",
                "title": "bob pretending to be alice",
                "user_sub": "user-alice-hostile",
                "user_id": "user-alice-hostile",
            },
        )

        alice_read = self._act_as(
            client,
            "user-alice-hostile",
            "GET",
            "/console/conversations/hostile-conv",
        )
        assert alice_read.status_code == 404, (
            "a hostile user_sub in the request body landed the "
            "conversation under another user's identity"
        )

        bob_read = self._act_as(
            client,
            "user-bob-hostile",
            "GET",
            "/console/conversations/hostile-conv",
        )
        assert bob_read.status_code == 200

    def test_hostile_body_user_sub_cannot_hijack_another_users_row(
        self, client: TestClient
    ) -> None:
        """The real non-vacuous proof that a hostile ``user_sub``/
        ``user_id`` body field is IGNORED, not merely absent from the
        response shape, AND cannot overwrite a victim's EXISTING row —
        driven through TWO DISTINCT real subs via the real
        ``require_user`` cold path (the single-identity sentinel
        ``client`` fixture used by
        ``TestConsoleConversationsCrud::test_hostile_body_extra_fields_are_dropped_from_response``
        cannot observe cross-user ownership at all, since every request
        in that fixture resolves to the SAME sentinel sub). Complements
        ``test_hostile_body_user_sub_never_lands_as_another_real_user``
        above, which only covers a FRESH ``conversation_id`` — this test
        covers the attacker targeting the victim's PRE-EXISTING row.

        Falsifiable: if a future change added a ``user_sub`` field to
        ``ConsoleConversationUpsertRequest`` AND the route honored it
        (e.g. via ``dataclasses.replace(user, user_id=body.user_sub)``),
        the attacker's writes below would land in/leak into the
        victim's rows and this test would go RED — proven by a
        builder-side neuter pass (temporarily reintroducing exactly
        that injection) reported in the evidence file.
        """
        # The victim already owns a row under her OWN real identity.
        victim_create = self._act_as(
            client,
            "victim-hostile",
            "POST",
            "/console/conversations",
            json={
                "conversation_id": "shared-hostile-id",
                "title": "victim's real conversation",
            },
        )
        assert victim_create.status_code == 200

        # The attacker upserts the SAME conversation_id, with a hostile
        # body claiming (via user_sub AND user_id) to BE the victim — an
        # attempted overwrite/hijack of the victim's existing row.
        attacker_upsert = self._act_as(
            client,
            "attacker-hostile",
            "POST",
            "/console/conversations",
            json={
                "conversation_id": "shared-hostile-id",
                "title": "hijacked by attacker",
                "user_sub": "victim-hostile",
                "user_id": "victim-hostile",
            },
        )
        assert attacker_upsert.status_code == 200

        # The victim's row must be COMPLETELY UNTOUCHED — the hostile
        # user_sub/user_id body fields must never redirect the write
        # into the victim's row.
        victim_read = self._act_as(
            client, "victim-hostile", "GET", "/console/conversations/shared-hostile-id"
        )
        assert victim_read.status_code == 200
        assert victim_read.json()["title"] == "victim's real conversation", (
            "attacker's hostile user_sub/user_id body fields hijacked/"
            "overwrote the victim's conversation — the fields must be "
            "silently ignored, never honored"
        )

        # The attacker's write must have landed under the ATTACKER's OWN
        # real identity (RLS-isolated from the victim's row with the
        # same conversation_id), not the victim's.
        attacker_read = self._act_as(
            client,
            "attacker-hostile",
            "GET",
            "/console/conversations/shared-hostile-id",
        )
        assert attacker_read.status_code == 200
        assert attacker_read.json()["title"] == "hijacked by attacker"

        # A second hostile upsert, this time under a FRESH
        # conversation_id, must never be visible to the victim via
        # get/list — proving the hostile field cannot plant a row the
        # victim can see either.
        planted = self._act_as(
            client,
            "attacker-hostile",
            "POST",
            "/console/conversations",
            json={
                "conversation_id": "planted-hostile-id",
                "title": "planted",
                "user_sub": "victim-hostile",
                "user_id": "victim-hostile",
            },
        )
        assert planted.status_code == 200

        victim_get_planted = self._act_as(
            client,
            "victim-hostile",
            "GET",
            "/console/conversations/planted-hostile-id",
        )
        assert victim_get_planted.status_code == 404, (
            "the victim can read a conversation planted via a hostile "
            "user_sub body field — the field must never be honored"
        )

        victim_list = self._act_as(
            client, "victim-hostile", "GET", "/console/conversations"
        )
        victim_ids = [i["conversation_id"] for i in victim_list.json()["items"]]
        assert "planted-hostile-id" not in victim_ids, (
            "the victim's list included a conversation planted via a "
            "hostile user_sub body field"
        )
