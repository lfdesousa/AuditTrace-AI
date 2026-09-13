"""HTTP-route tests for the console-tool-favorites API (Tool-Favorites
domain, MongoDB-elimination EPIC).

Mirrors ``test_console_conversation_tags_routes.py``'s structure (the
spec's instruction), scaled to the tool-favorites field set (item_type/
item_id/tenant_id, no cursor pagination, only three operations — list/
add/remove — per the ratified spec). Two identity strategies, per
``feedback_test_through_real_http_route``:

* Most tests use the default ``client`` fixture (``AUDITTRACE_AUTH_REQUIRED
  =false`` bypass — the sentinel user) to exercise route wiring, scope
  declarations, and response shapes without JWT ceremony.
* Cross-user isolation tests drive the REAL ``require_user`` cold path
  (mirrors ``tests/test_console_conversation_tags_routes.py``'s
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

from audittrace.services.console_tool_favorites import MAX_TOOL_FAVORITES

READ_SCOPE = "memory:tool_favorites:read-own"
WRITE_SCOPE = "memory:tool_favorites:write"


@contextmanager
def _identity(*, sub: str, scope: str):
    """Drive the REAL ``require_user`` cold path for ``sub`` with
    ``scope``, mirroring ``tests/test_console_conversation_tags_routes.py``'s
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


class TestConsoleToolFavoritesCrud:
    def test_add_creates_favorite(self, client: TestClient) -> None:
        r = client.post(
            "/console/tool-favorites",
            json={"item_type": "tool", "item_id": "web-search", "tenant_id": "acme"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["item_type"] == "tool"
        assert body["item_id"] == "web-search"
        assert body["tenant_id"] == "acme"
        assert "user_sub" not in body, (
            "response must never leak the internal user_sub field"
        )

    def test_add_defaults_tenant_id_and_metadata(self, client: TestClient) -> None:
        r = client.post(
            "/console/tool-favorites", json={"item_type": "tool", "item_id": "bare"}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["tenant_id"] is None
        assert body["metadata"] == {}

    def test_add_is_idempotent(self, client: TestClient) -> None:
        client.post(
            "/console/tool-favorites",
            json={"item_type": "tool", "item_id": "web-search", "tenant_id": "A"},
        )
        r = client.post(
            "/console/tool-favorites",
            json={"item_type": "tool", "item_id": "web-search", "tenant_id": "B"},
        )
        assert r.status_code == 200
        listed = client.get("/console/tool-favorites").json()
        matches = [i for i in listed["items"] if i["item_id"] == "web-search"]
        assert len(matches) == 1
        assert matches[0]["tenant_id"] == "B"

    def test_add_rejects_missing_item_id(self, client: TestClient) -> None:
        r = client.post("/console/tool-favorites", json={"item_type": "tool"})
        assert r.status_code == 422

    def test_add_rejects_invalid_item_type(self, client: TestClient) -> None:
        r = client.post(
            "/console/tool-favorites",
            json={"item_type": "not-a-real-type", "item_id": "x"},
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
            "/console/tool-favorites",
            json={
                "item_type": "tool",
                "item_id": "hostile",
                "user_sub": "attacker-sub",
                "user_id": "attacker-sub",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["item_id"] == "hostile"
        assert "user_sub" not in body
        assert "user_id" not in body

    def test_list_tool_favorites_shape(self, client: TestClient) -> None:
        client.post(
            "/console/tool-favorites", json={"item_type": "tool", "item_id": "x"}
        )
        r = client.get("/console/tool-favorites")
        assert r.status_code == 200
        assert "items" in r.json()

    def test_list_excludes_removed(self, client: TestClient) -> None:
        client.post(
            "/console/tool-favorites", json={"item_type": "tool", "item_id": "x"}
        )
        client.delete("/console/tool-favorites/tool/x")
        items = client.get("/console/tool-favorites").json()["items"]
        assert [i for i in items if i["item_id"] == "x"] == []

    def test_remove_tool_favorite(self, client: TestClient) -> None:
        client.post(
            "/console/tool-favorites", json={"item_type": "tool", "item_id": "x"}
        )
        r = client.delete("/console/tool-favorites/tool/x")
        assert r.status_code == 204
        items = client.get("/console/tool-favorites").json()["items"]
        assert [i for i in items if i["item_id"] == "x"] == []

    def test_remove_unknown_tool_favorite_returns_404(self, client: TestClient) -> None:
        r = client.delete("/console/tool-favorites/tool/does-not-exist")
        assert r.status_code == 404

    def test_remove_item_id_containing_slash(self, client: TestClient) -> None:
        """The ``:path`` route converter accepts an item_id containing a
        ``/`` — a client-supplied, possibly server-qualified, tool
        name."""
        client.post(
            "/console/tool-favorites",
            json={"item_type": "mcp", "item_id": "server/tool-name"},
        )
        d = client.delete("/console/tool-favorites/mcp/server/tool-name")
        assert d.status_code == 204

    def test_add_101st_favorite_returns_409(self, client: TestClient) -> None:
        for i in range(MAX_TOOL_FAVORITES):
            r = client.post(
                "/console/tool-favorites",
                json={"item_type": "tool", "item_id": f"item-{i}"},
            )
            assert r.status_code == 200
        r = client.post(
            "/console/tool-favorites",
            json={"item_type": "tool", "item_id": "one-too-many"},
        )
        assert r.status_code == 409

    def test_soft_delete_then_re_add_via_http(self, client: TestClient) -> None:
        """The D13 avoidance guard, end-to-end through the real HTTP
        route: remove then re-add the SAME (item_type, item_id) and the
        favorite must be visible again, not left tombstoned."""
        client.post(
            "/console/tool-favorites",
            json={"item_type": "tool", "item_id": "reviving", "tenant_id": "first"},
        )
        client.delete("/console/tool-favorites/tool/reviving")
        items = client.get("/console/tool-favorites").json()["items"]
        assert [i for i in items if i["item_id"] == "reviving"] == []

        r = client.post(
            "/console/tool-favorites",
            json={"item_type": "tool", "item_id": "reviving", "tenant_id": "second"},
        )
        assert r.status_code == 200
        assert r.json()["deleted_at_ms"] is None

        items = client.get("/console/tool-favorites").json()["items"]
        revived = [i for i in items if i["item_id"] == "reviving"]
        assert len(revived) == 1
        assert revived[0]["tenant_id"] == "second"


# ── Scope enforcement (real require_user/validate_jwt cold path) ──────────


class TestScopeEnforcement:
    def test_write_requires_tool_favorites_write_scope(
        self, client: TestClient
    ) -> None:
        with _identity(sub="fav-user", scope=READ_SCOPE):
            r = client.post(
                "/console/tool-favorites",
                json={"item_type": "tool", "item_id": "x"},
                headers=_auth_headers("fav-user"),
            )
        assert r.status_code == 403
        assert WRITE_SCOPE in r.json()["detail"]

    def test_read_requires_tool_favorites_read_scope(self, client: TestClient) -> None:
        with _identity(sub="fav-user", scope=WRITE_SCOPE):
            r = client.get("/console/tool-favorites", headers=_auth_headers("fav-user"))
        assert r.status_code == 403
        assert READ_SCOPE in r.json()["detail"]

    def test_write_scope_alone_cannot_read(self, client: TestClient) -> None:
        """The least-privilege wall: a write-only token cannot list —
        read and write are two DISTINCT scopes, never implied by each
        other."""
        with _identity(sub="fav-user", scope=WRITE_SCOPE):
            r = client.get("/console/tool-favorites", headers=_auth_headers("fav-user"))
        assert r.status_code == 403

    def test_read_scope_alone_cannot_write(self, client: TestClient) -> None:
        with _identity(sub="fav-user", scope=READ_SCOPE):
            r = client.delete(
                "/console/tool-favorites/tool/x", headers=_auth_headers("fav-user")
            )
        assert r.status_code == 403

    def test_no_scope_token_cannot_write(self, client: TestClient) -> None:
        with _identity(sub="fav-user", scope="audittrace:query"):
            r = client.post(
                "/console/tool-favorites",
                json={"item_type": "tool", "item_id": "x"},
                headers=_auth_headers("fav-user"),
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

    def test_user_b_cannot_list_user_as_favorite(self, client: TestClient) -> None:
        create = self._act_as(
            client,
            "user-alice",
            "POST",
            "/console/tool-favorites",
            json={"item_type": "tool", "item_id": "alice-tool", "tenant_id": "alice"},
        )
        assert create.status_code == 200

        bob_list = self._act_as(client, "user-bob", "GET", "/console/tool-favorites")
        item_ids = [i["item_id"] for i in bob_list.json()["items"]]
        assert "alice-tool" not in item_ids, (
            "user B's tool-favorites list included user A's favorite"
        )

        alice_list = self._act_as(
            client, "user-alice", "GET", "/console/tool-favorites"
        )
        alice_ids = [i["item_id"] for i in alice_list.json()["items"]]
        assert "alice-tool" in alice_ids

    def test_user_b_cannot_write_user_as_favorite(self, client: TestClient) -> None:
        self._act_as(
            client,
            "user-alice-w",
            "POST",
            "/console/tool-favorites",
            json={"item_type": "tool", "item_id": "shared-w", "tenant_id": "original"},
        )

        # Bob adding the SAME (item_type, item_id) must create/update HIS
        # OWN row, never alice's.
        bob_add = self._act_as(
            client,
            "user-bob-w",
            "POST",
            "/console/tool-favorites",
            json={"item_type": "tool", "item_id": "shared-w", "tenant_id": "hijacked"},
        )
        assert bob_add.status_code == 200
        assert bob_add.json()["tenant_id"] == "hijacked"

        bob_remove = self._act_as(
            client, "user-bob-w", "DELETE", "/console/tool-favorites/tool/shared-w"
        )
        assert bob_remove.status_code == 204

        alice_list = self._act_as(
            client, "user-alice-w", "GET", "/console/tool-favorites"
        )
        alice_items = {i["item_id"]: i for i in alice_list.json()["items"]}
        assert "shared-w" in alice_items, (
            "user A's tool-favorite was removed/altered by user B — "
            "isolation wall broken"
        )
        assert alice_items["shared-w"]["tenant_id"] == "original"

    def test_cap_is_per_user_through_real_route(self, client: TestClient) -> None:
        """The cap-COUNT aggregate's ``user_sub`` filter, proven through
        the REAL HTTP route with TWO DISTINCT real subs via the real
        ``require_user`` cold path (the 2026-09-13 REJECT — aggregate
        queries must be user-scoped). Alice fills ``MAX_TOOL_FAVORITES``;
        her next add is 409; bob's FIRST add must still be 200.

        Falsifiable: neuter the ``user_sub`` clause on the
        ``select(func.count())`` in
        ``PostgresConsoleToolFavoritesService.add_tool_favorite`` (the
        implementation the ``client`` fixture wires) and bob's first add
        returns 409 — one user filling the cap would deny the feature to
        every other user (cross-user DoS)."""
        for i in range(MAX_TOOL_FAVORITES):
            r = self._act_as(
                client,
                "user-alice-cap",
                "POST",
                "/console/tool-favorites",
                json={"item_type": "tool", "item_id": f"alice-item-{i}"},
            )
            assert r.status_code == 200
        alice_over = self._act_as(
            client,
            "user-alice-cap",
            "POST",
            "/console/tool-favorites",
            json={"item_type": "tool", "item_id": "alice-one-too-many"},
        )
        assert alice_over.status_code == 409

        bob_first = self._act_as(
            client,
            "user-bob-cap",
            "POST",
            "/console/tool-favorites",
            json={"item_type": "tool", "item_id": "bob-first"},
        )
        assert bob_first.status_code == 200, (
            "bob's FIRST add was rejected because ALICE is at the cap — the "
            "cap-count aggregate is missing/neutered its user_sub filter "
            f"(per-user cap degraded to GLOBAL): {bob_first.status_code} "
            f"{bob_first.text}"
        )
        assert bob_first.json()["item_id"] == "bob-first"

        bob_list = self._act_as(
            client, "user-bob-cap", "GET", "/console/tool-favorites"
        )
        assert [i["item_id"] for i in bob_list.json()["items"]] == ["bob-first"]
        alice_list = self._act_as(
            client, "user-alice-cap", "GET", "/console/tool-favorites"
        )
        assert len(alice_list.json()["items"]) == MAX_TOOL_FAVORITES

    def test_user_b_add_cannot_resurrect_user_as_removed_favorite(
        self, client: TestClient
    ) -> None:
        """The tombstone lookup's ``user_sub`` filter, proven through
        the real route with two distinct real subs: alice adds then
        removes a key; bob adds the SAME key. Bob gets his own row and
        alice's tombstone stays deleted. Neuter the ``user_sub`` clause
        on the ``tombstoned`` select and alice's removed favorite
        reappears in her list carrying bob's ``tenant_id``."""
        self._act_as(
            client,
            "user-alice-tomb",
            "POST",
            "/console/tool-favorites",
            json={"item_type": "tool", "item_id": "shared-tomb", "tenant_id": "alice"},
        )
        removed = self._act_as(
            client,
            "user-alice-tomb",
            "DELETE",
            "/console/tool-favorites/tool/shared-tomb",
        )
        assert removed.status_code == 204

        bob_add = self._act_as(
            client,
            "user-bob-tomb",
            "POST",
            "/console/tool-favorites",
            json={"item_type": "tool", "item_id": "shared-tomb", "tenant_id": "bob"},
        )
        assert bob_add.status_code == 200

        alice_list = self._act_as(
            client, "user-alice-tomb", "GET", "/console/tool-favorites"
        )
        alice_ids = [i["item_id"] for i in alice_list.json()["items"]]
        assert "shared-tomb" not in alice_ids, (
            "bob's add resurrected alice's soft-deleted tool-favorite — the "
            "user_sub filter in the add's tombstone lookup is missing/neutered"
        )
        bob_list = self._act_as(
            client, "user-bob-tomb", "GET", "/console/tool-favorites"
        )
        bob_items = {i["item_id"]: i for i in bob_list.json()["items"]}
        assert bob_items["shared-tomb"]["tenant_id"] == "bob"

    def test_hostile_body_user_sub_cannot_hijack_another_users_row(
        self, client: TestClient
    ) -> None:
        """The real non-vacuous proof that a hostile ``user_sub``/
        ``user_id`` body field is IGNORED, not merely absent from the
        response shape — driven through TWO DISTINCT real subs via the
        real ``require_user`` cold path (the single-identity sentinel
        ``client`` fixture used by
        ``TestConsoleToolFavoritesCrud::test_hostile_body_extra_fields_are_dropped_from_response``
        cannot observe cross-user ownership at all, since every request
        in that fixture resolves to the SAME sentinel sub).

        Falsifiable: if a future change added a ``user_sub`` field to
        ``ConsoleToolFavoriteAddRequest`` AND the route honored it (e.g.
        via ``dataclasses.replace(user, user_id=body.user_sub)``), the
        attacker's writes below would land in/leak into the victim's
        rows and this test would go RED — proven by a builder-side
        neuter pass (temporarily reintroducing exactly that injection)
        reported in the evidence file.
        """
        # The victim already owns a row under her OWN real identity.
        victim_add = self._act_as(
            client,
            "victim-hostile",
            "POST",
            "/console/tool-favorites",
            json={
                "item_type": "tool",
                "item_id": "shared-hostile-item",
                "tenant_id": "victim-real",
            },
        )
        assert victim_add.status_code == 200

        # The attacker re-adds the SAME item, with a hostile body
        # claiming (via user_sub AND user_id) to BE the victim — an
        # attempted overwrite/hijack of the victim's existing row.
        attacker_add = self._act_as(
            client,
            "attacker-hostile",
            "POST",
            "/console/tool-favorites",
            json={
                "item_type": "tool",
                "item_id": "shared-hostile-item",
                "tenant_id": "hijacked-by-attacker",
                "user_sub": "victim-hostile",
                "user_id": "victim-hostile",
            },
        )
        assert attacker_add.status_code == 200

        # The victim's row must be COMPLETELY UNTOUCHED — the hostile
        # user_sub/user_id body fields must never redirect the write
        # into the victim's row.
        victim_list = self._act_as(
            client, "victim-hostile", "GET", "/console/tool-favorites"
        )
        victim_items = {i["item_id"]: i for i in victim_list.json()["items"]}
        assert "shared-hostile-item" in victim_items
        assert victim_items["shared-hostile-item"]["tenant_id"] == "victim-real", (
            "attacker's hostile user_sub/user_id body fields hijacked/"
            "overwrote the victim's tool-favorite — the fields must be "
            "silently ignored, never honored"
        )

        # The attacker's write must have landed under the ATTACKER's OWN
        # real identity (RLS-isolated from the victim's row with the
        # same item), not the victim's.
        attacker_list = self._act_as(
            client, "attacker-hostile", "GET", "/console/tool-favorites"
        )
        attacker_items = {i["item_id"]: i for i in attacker_list.json()["items"]}
        assert "shared-hostile-item" in attacker_items
        assert (
            attacker_items["shared-hostile-item"]["tenant_id"] == "hijacked-by-attacker"
        )

        # A second hostile add, this time under a FRESH item, must
        # never be visible to the victim via list — proving the hostile
        # field cannot plant a row the victim can see either.
        planted = self._act_as(
            client,
            "attacker-hostile",
            "POST",
            "/console/tool-favorites",
            json={
                "item_type": "tool",
                "item_id": "planted-hostile-item",
                "user_sub": "victim-hostile",
                "user_id": "victim-hostile",
            },
        )
        assert planted.status_code == 200

        victim_list_2 = self._act_as(
            client, "victim-hostile", "GET", "/console/tool-favorites"
        )
        victim_ids_2 = [i["item_id"] for i in victim_list_2.json()["items"]]
        assert "planted-hostile-item" not in victim_ids_2, (
            "the victim's list included a tool-favorite planted via a "
            "hostile user_sub body field"
        )
