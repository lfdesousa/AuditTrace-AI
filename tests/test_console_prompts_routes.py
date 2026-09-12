"""HTTP-route tests for the console-prompts API (Mongo-repl WU-prompts,
MongoDB-elimination EPIC).

Mirrors ``test_console_conversations_routes.py``'s structure (the
spec's instruction), scaled to the group+versions shape. Two identity
strategies, per ``feedback_test_through_real_http_route``:

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

COMPLETENESS coverage (the WU-2 lesson): a hostile cross-user attempt
to attach a version to, or promote a version within, another user's
group must 404 through the real HTTP route.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

READ_SCOPE = "memory:prompts:read-own"
WRITE_SCOPE = "memory:prompts:write"


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


class TestConsolePromptsGroupCrud:
    def test_upsert_creates_group(self, client: TestClient) -> None:
        r = client.post(
            "/console/prompts",
            json={"group_id": "group-1", "name": "Hello", "category": "writing"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["group_id"] == "group-1"
        assert body["name"] == "Hello"
        assert body["category"] == "writing"
        assert body["production_prompt_id"] is None
        assert "user_sub" not in body, (
            "response must never leak the internal user_sub field"
        )

    def test_upsert_rejects_missing_name(self, client: TestClient) -> None:
        r = client.post("/console/prompts", json={"group_id": "group-1"})
        assert r.status_code == 422

    def test_upsert_rejects_missing_group_id(self, client: TestClient) -> None:
        r = client.post("/console/prompts", json={"name": "no id"})
        assert r.status_code == 422

    def test_upsert_is_idempotent(self, client: TestClient) -> None:
        client.post("/console/prompts", json={"group_id": "group-1", "name": "A"})
        r = client.post("/console/prompts", json={"group_id": "group-1", "name": "B"})
        assert r.status_code == 200
        listed = client.get("/console/prompts").json()
        matches = [i for i in listed["items"] if i["group_id"] == "group-1"]
        assert len(matches) == 1
        assert matches[0]["name"] == "B"

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
            "/console/prompts",
            json={
                "group_id": "group-hostile",
                "name": "hi",
                "user_sub": "attacker-sub",
                "user_id": "attacker-sub",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["group_id"] == "group-hostile"
        assert "user_sub" not in body
        assert "user_id" not in body

    def test_get_group_includes_versions(self, client: TestClient) -> None:
        client.post("/console/prompts", json={"group_id": "group-1", "name": "G"})
        r = client.get("/console/prompts/group-1")
        assert r.status_code == 200
        body = r.json()
        assert body["group_id"] == "group-1"
        assert body["versions"] == []

    def test_get_unknown_group_returns_404(self, client: TestClient) -> None:
        r = client.get("/console/prompts/does-not-exist")
        assert r.status_code == 404

    def test_list_groups_shape(self, client: TestClient) -> None:
        client.post("/console/prompts", json={"group_id": "group-1", "name": "G"})
        r = client.get("/console/prompts")
        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert "next_cursor" in body
        assert "versions" not in body["items"][0], (
            "list view must stay summary-only, no versions per row"
        )

    def test_list_pagination(self, client: TestClient) -> None:
        for i in range(5):
            client.post(
                "/console/prompts", json={"group_id": f"group-{i}", "name": f"g{i}"}
            )
        r = client.get("/console/prompts", params={"limit": 2})
        body = r.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"] is not None

        r2 = client.get(
            "/console/prompts",
            params={"limit": 2, "cursor": body["next_cursor"]},
        )
        assert r2.status_code == 200
        assert len(r2.json()["items"]) == 2

    def test_list_invalid_cursor_returns_400(self, client: TestClient) -> None:
        r = client.get("/console/prompts", params={"cursor": "not-valid!!"})
        assert r.status_code == 400

    def test_delete_group(self, client: TestClient) -> None:
        client.post("/console/prompts", json={"group_id": "group-1", "name": "G"})
        r = client.delete("/console/prompts/group-1")
        assert r.status_code == 204
        assert client.get("/console/prompts/group-1").status_code == 404

    def test_delete_unknown_group_returns_404(self, client: TestClient) -> None:
        r = client.delete("/console/prompts/does-not-exist")
        assert r.status_code == 404


class TestConsolePromptsVersions:
    def test_upsert_version_creates_v1(self, client: TestClient) -> None:
        client.post("/console/prompts", json={"group_id": "group-1", "name": "G"})
        r = client.post(
            "/console/prompts/group-1/versions",
            json={"prompt_id": "prompt-1", "text": "hello world"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["prompt_id"] == "prompt-1"
        assert body["group_id"] == "group-1"
        assert body["text"] == "hello world"
        assert body["type"] == "text"
        assert body["version"] == 1

    def test_upsert_version_rejects_unknown_type(self, client: TestClient) -> None:
        client.post("/console/prompts", json={"group_id": "group-1", "name": "G"})
        r = client.post(
            "/console/prompts/group-1/versions",
            json={"prompt_id": "prompt-1", "text": "hi", "type": "bogus"},
        )
        assert r.status_code == 422

    def test_upsert_version_missing_group_returns_404(self, client: TestClient) -> None:
        r = client.post(
            "/console/prompts/no-such-group/versions",
            json={"prompt_id": "prompt-1", "text": "hi"},
        )
        assert r.status_code == 404

    def test_upsert_version_monotonic_numbering(self, client: TestClient) -> None:
        client.post("/console/prompts", json={"group_id": "group-1", "name": "G"})
        client.post(
            "/console/prompts/group-1/versions",
            json={"prompt_id": "p1", "text": "v1"},
        )
        r2 = client.post(
            "/console/prompts/group-1/versions",
            json={"prompt_id": "p2", "text": "v2"},
        )
        assert r2.json()["version"] == 2

        fetched = client.get("/console/prompts/group-1").json()
        assert [v["prompt_id"] for v in fetched["versions"]] == ["p1", "p2"]

    def test_set_production(self, client: TestClient) -> None:
        client.post("/console/prompts", json={"group_id": "group-1", "name": "G"})
        client.post(
            "/console/prompts/group-1/versions",
            json={"prompt_id": "p1", "text": "v1"},
        )
        r = client.patch(
            "/console/prompts/group-1/production", json={"prompt_id": "p1"}
        )
        assert r.status_code == 200
        assert r.json()["production_prompt_id"] == "p1"

    def test_set_production_missing_group_returns_404(self, client: TestClient) -> None:
        r = client.patch(
            "/console/prompts/no-such-group/production", json={"prompt_id": "p1"}
        )
        assert r.status_code == 404

    def test_set_production_missing_version_returns_404(
        self, client: TestClient
    ) -> None:
        client.post("/console/prompts", json={"group_id": "group-1", "name": "G"})
        r = client.patch(
            "/console/prompts/group-1/production",
            json={"prompt_id": "no-such-prompt"},
        )
        assert r.status_code == 404

    def test_delete_group_removes_versions(self, client: TestClient) -> None:
        client.post("/console/prompts", json={"group_id": "group-1", "name": "G"})
        client.post(
            "/console/prompts/group-1/versions",
            json={"prompt_id": "p1", "text": "v1"},
        )
        client.delete("/console/prompts/group-1")
        assert client.get("/console/prompts/group-1").status_code == 404


# ── Scope enforcement (real require_user/validate_jwt cold path) ──────────


class TestScopeEnforcement:
    def test_write_requires_prompts_write_scope(self, client: TestClient) -> None:
        with _identity(sub="chat-user", scope=READ_SCOPE):
            r = client.post(
                "/console/prompts",
                json={"group_id": "group-1", "name": "G"},
                headers=_auth_headers("chat-user"),
            )
        assert r.status_code == 403
        assert WRITE_SCOPE in r.json()["detail"]

    def test_read_requires_prompts_read_scope(self, client: TestClient) -> None:
        with _identity(sub="chat-user", scope=WRITE_SCOPE):
            r = client.get("/console/prompts", headers=_auth_headers("chat-user"))
        assert r.status_code == 403
        assert READ_SCOPE in r.json()["detail"]

    def test_write_scope_alone_cannot_read(self, client: TestClient) -> None:
        """The least-privilege wall: a write-only token cannot list/read
        — read and write are two DISTINCT scopes, never implied by each
        other."""
        with _identity(sub="chat-user", scope=WRITE_SCOPE):
            r = client.get(
                "/console/prompts/group-1", headers=_auth_headers("chat-user")
            )
        assert r.status_code == 403

    def test_read_scope_alone_cannot_write(self, client: TestClient) -> None:
        with _identity(sub="chat-user", scope=READ_SCOPE):
            r = client.delete(
                "/console/prompts/group-1", headers=_auth_headers("chat-user")
            )
        assert r.status_code == 403

    def test_no_scope_token_cannot_write(self, client: TestClient) -> None:
        with _identity(sub="chat-user", scope="audittrace:query"):
            r = client.post(
                "/console/prompts",
                json={"group_id": "group-1", "name": "G"},
                headers=_auth_headers("chat-user"),
            )
        assert r.status_code == 403

    def test_read_scope_alone_cannot_upsert_version(self, client: TestClient) -> None:
        with _identity(sub="chat-user", scope=READ_SCOPE):
            r = client.post(
                "/console/prompts/group-1/versions",
                json={"prompt_id": "p1", "text": "v1"},
                headers=_auth_headers("chat-user"),
            )
        assert r.status_code == 403

    def test_read_scope_alone_cannot_set_production(self, client: TestClient) -> None:
        with _identity(sub="chat-user", scope=READ_SCOPE):
            r = client.patch(
                "/console/prompts/group-1/production",
                json={"prompt_id": "p1"},
                headers=_auth_headers("chat-user"),
            )
        assert r.status_code == 403


# ── Cross-user isolation (real require_user, distinct real subs) ──────────


class TestCrossUserIsolation:
    """The WU-4 cross-user hijack lesson
    (feedback_per_user_namespace_shared_store_ids), proven through the
    REAL HTTP route with the REAL ``require_user`` cold path — NOT a
    ``dependency_overrides`` swap (see module docstring). Includes the
    WU-2 completeness lesson: a version can never be attached to, nor
    promoted within, another user's group."""

    _BOTH_SCOPES = f"{READ_SCOPE} {WRITE_SCOPE}"

    def _act_as(self, client: TestClient, sub: str, method: str, path: str, **kwargs):
        with _identity(sub=sub, scope=self._BOTH_SCOPES):
            return client.request(method, path, headers=_auth_headers(sub), **kwargs)

    def test_user_b_cannot_read_user_as_group(self, client: TestClient) -> None:
        create = self._act_as(
            client,
            "user-alice",
            "POST",
            "/console/prompts",
            json={"group_id": "alice-group", "name": "alice's private group"},
        )
        assert create.status_code == 200

        bob_read = self._act_as(
            client, "user-bob", "GET", "/console/prompts/alice-group"
        )
        assert bob_read.status_code == 404, (
            "user B read user A's prompt group via the real HTTP route "
            "— the RLS/isolation wall is broken"
        )

        bob_list = self._act_as(client, "user-bob", "GET", "/console/prompts")
        ids = [i["group_id"] for i in bob_list.json()["items"]]
        assert "alice-group" not in ids, "user B's group list included user A's group"

        alice_read = self._act_as(
            client, "user-alice", "GET", "/console/prompts/alice-group"
        )
        assert alice_read.status_code == 200
        assert alice_read.json()["name"] == "alice's private group"

    def test_user_b_cannot_write_user_as_group(self, client: TestClient) -> None:
        self._act_as(
            client,
            "user-alice-w",
            "POST",
            "/console/prompts",
            json={"group_id": "alice-group-w", "name": "original"},
        )

        # Bob upserting the SAME group_id must create/update HIS OWN
        # row, never alice's — see the service-level non-vacuity guard
        # for the underlying assertion; here we confirm alice's row is
        # untouched through the real route.
        bob_upsert = self._act_as(
            client,
            "user-bob-w",
            "POST",
            "/console/prompts",
            json={"group_id": "alice-group-w", "name": "hijacked by bob"},
        )
        assert bob_upsert.status_code == 200
        assert bob_upsert.json()["name"] == "hijacked by bob"

        bob_delete = self._act_as(
            client, "user-bob-w", "DELETE", "/console/prompts/alice-group-w"
        )
        assert bob_delete.status_code == 204

        alice_read = self._act_as(
            client, "user-alice-w", "GET", "/console/prompts/alice-group-w"
        )
        assert alice_read.status_code == 200
        assert alice_read.json()["name"] == "original", (
            "user A's group title/existence was altered by user B — "
            "isolation wall broken"
        )

    def test_user_b_cannot_attach_version_to_user_as_group(
        self, client: TestClient
    ) -> None:
        """COMPLETENESS (WU-2 lesson): a hostile caller can never
        attach a version to a group owned by someone else, even
        knowing its exact group_id."""
        self._act_as(
            client,
            "user-alice-v",
            "POST",
            "/console/prompts",
            json={"group_id": "alice-group-v", "name": "alice's group"},
        )

        bob_upsert_version = self._act_as(
            client,
            "user-bob-v",
            "POST",
            "/console/prompts/alice-group-v/versions",
            json={"prompt_id": "hijack-prompt", "text": "malicious"},
        )
        assert bob_upsert_version.status_code == 404, (
            "user B attached a version to user A's group via the real "
            "HTTP route — the completeness/isolation wall is broken"
        )

        alice_group = self._act_as(
            client, "user-alice-v", "GET", "/console/prompts/alice-group-v"
        )
        assert alice_group.json()["versions"] == [], (
            "user A's group gained a version it never created"
        )

    def test_user_b_cannot_promote_user_as_version(self, client: TestClient) -> None:
        """COMPLETENESS (WU-2 lesson): a hostile caller can never
        promote a version belonging to someone else's group, even when
        the caller has their OWN group with the same group_id."""
        self._act_as(
            client,
            "user-alice-p",
            "POST",
            "/console/prompts",
            json={"group_id": "shared-id", "name": "alice's group"},
        )
        self._act_as(
            client,
            "user-alice-p",
            "POST",
            "/console/prompts/shared-id/versions",
            json={"prompt_id": "alice-p1", "text": "alice's prompt"},
        )
        self._act_as(
            client,
            "user-bob-p",
            "POST",
            "/console/prompts",
            json={"group_id": "shared-id", "name": "bob's group"},
        )

        bob_promote = self._act_as(
            client,
            "user-bob-p",
            "PATCH",
            "/console/prompts/shared-id/production",
            json={"prompt_id": "alice-p1"},
        )
        assert bob_promote.status_code == 404, (
            "user B promoted user A's version to production via the "
            "real HTTP route — the completeness/isolation wall is broken"
        )

        alice_group = self._act_as(
            client, "user-alice-p", "GET", "/console/prompts/shared-id"
        )
        assert alice_group.json()["production_prompt_id"] is None

    def test_hostile_body_user_sub_cannot_hijack_another_users_row(
        self, client: TestClient
    ) -> None:
        """The real non-vacuous proof that a hostile ``user_sub``/
        ``user_id`` body field is IGNORED, not merely absent from the
        response shape — driven through TWO DISTINCT real subs via the
        real ``require_user`` cold path (the single-identity sentinel
        ``client`` fixture used by
        ``TestConsolePromptsGroupCrud::test_hostile_body_extra_fields_are_dropped_from_response``
        cannot observe cross-user ownership at all, since every request
        in that fixture resolves to the SAME sentinel sub).

        Falsifiable: if a future change added a ``user_sub`` field to
        ``ConsolePromptGroupUpsertRequest`` AND the route honored it
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
            "/console/prompts",
            json={"group_id": "shared-hostile-id", "name": "victim's real group"},
        )
        assert victim_create.status_code == 200

        # The attacker upserts the SAME group_id, with a hostile body
        # claiming (via user_sub AND user_id) to BE the victim — an
        # attempted overwrite/hijack of the victim's existing row.
        attacker_upsert = self._act_as(
            client,
            "attacker-hostile",
            "POST",
            "/console/prompts",
            json={
                "group_id": "shared-hostile-id",
                "name": "hijacked by attacker",
                "user_sub": "victim-hostile",
                "user_id": "victim-hostile",
            },
        )
        assert attacker_upsert.status_code == 200

        # The victim's row must be COMPLETELY UNTOUCHED — the hostile
        # user_sub/user_id body fields must never redirect the write
        # into the victim's row.
        victim_read = self._act_as(
            client, "victim-hostile", "GET", "/console/prompts/shared-hostile-id"
        )
        assert victim_read.status_code == 200
        assert victim_read.json()["name"] == "victim's real group", (
            "attacker's hostile user_sub/user_id body fields hijacked/"
            "overwrote the victim's group — the fields must be silently "
            "ignored, never honored"
        )

        # The attacker's write must have landed under the ATTACKER's OWN
        # real identity (RLS-isolated from the victim's row with the
        # same group_id), not the victim's.
        attacker_read = self._act_as(
            client, "attacker-hostile", "GET", "/console/prompts/shared-hostile-id"
        )
        assert attacker_read.status_code == 200
        assert attacker_read.json()["name"] == "hijacked by attacker"

        # A second hostile upsert, this time under a FRESH group_id,
        # must never be visible to the victim via get/list — proving the
        # hostile field cannot plant a row the victim can see either.
        planted = self._act_as(
            client,
            "attacker-hostile",
            "POST",
            "/console/prompts",
            json={
                "group_id": "planted-hostile-id",
                "name": "planted",
                "user_sub": "victim-hostile",
                "user_id": "victim-hostile",
            },
        )
        assert planted.status_code == 200

        victim_get_planted = self._act_as(
            client, "victim-hostile", "GET", "/console/prompts/planted-hostile-id"
        )
        assert victim_get_planted.status_code == 404, (
            "the victim can read a group planted via a hostile user_sub "
            "body field — the field must never be honored"
        )

        victim_list = self._act_as(client, "victim-hostile", "GET", "/console/prompts")
        victim_ids = [i["group_id"] for i in victim_list.json()["items"]]
        assert "planted-hostile-id" not in victim_ids, (
            "the victim's list included a group planted via a hostile "
            "user_sub body field"
        )
