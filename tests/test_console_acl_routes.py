"""HTTP-route tests for the console-ACL API (Sovereign Authorization
Layer EPIC, WU-1 — READ PATH ONLY).

Mirrors ``test_console_agents_routes.py``'s structure — two identity
strategies per ``feedback_test_through_real_http_route``:

* Most tests use the default ``client`` fixture (bypass-mode sentinel)
  to exercise route wiring, scope declarations, and response shapes.
* Cross-user isolation (the neuter-list's guard #5 —
  ``feedback_per_user_namespace_shared_store_ids``/RLS) drives the REAL
  ``require_user`` cold path with distinct real ``sub``s, exactly like
  ``test_console_agents_routes.py``'s ``TestCrossUserIsolation``.

WU-1 has NO write route — every test SEEDS rows directly against the
container's Postgres-backed service (bypassing HTTP), mirroring how
``tests/test_console_acl_service.py`` seeds rows, then reads through
the real route.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from audittrace import dependencies
from audittrace.db.models import ConsoleAclEntry
from audittrace.services.console_acl import (
    OWNER_PERMISSION_BITS,
    PERMISSION_BIT_EDIT,
    PERMISSION_BIT_VIEW,
)

READ_SCOPE = "memory:acl:read-own"


async def _seed_row(**kwargs) -> None:
    """Insert a ``ConsoleAclEntry`` row directly against the wired
    test container's session factory — WU-1 has no write route."""
    service = dependencies.get_console_acl_service()
    session_factory = service._session_factory  # type: ignore[attr-defined]
    kwargs.setdefault("id", str(uuid.uuid4()))
    kwargs.setdefault("granted_at_ms", 0)
    kwargs.setdefault("created_at_ms", 0)
    kwargs.setdefault("updated_at_ms", 0)
    kwargs.setdefault("expired_at_ms", None)
    kwargs.setdefault("tenant_id", None)
    async with session_factory() as session:
        session.add(ConsoleAclEntry(**kwargs))
        await session.commit()


@contextmanager
def _identity(*, sub: str, scope: str = READ_SCOPE):
    """Drive the REAL ``require_user`` cold path for ``sub`` — NOT a
    ``dependency_overrides`` swap (see
    ``test_console_agents_routes.py``'s module docstring for why that
    distinction matters for RLS ContextVar binding)."""
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


class TestRouteWiringAndShapes:
    async def test_effective_permissions_defaults_to_zero(
        self, client: TestClient
    ) -> None:
        r = client.get("/console/acl/agent/no-such-resource/permissions")
        assert r.status_code == 200
        assert r.json() == {"perm_bits": 0}

    async def test_has_permission_false_with_no_grant(self, client: TestClient) -> None:
        r = client.get(
            "/console/acl/agent/no-such-resource/has-permission",
            params={"bit": PERMISSION_BIT_VIEW},
        )
        assert r.status_code == 200
        assert r.json() == {"has_permission": False}

    async def test_unknown_resource_type_is_400(self, client: TestClient) -> None:
        r = client.get("/console/acl/not-a-real-type/some-id/permissions")
        assert r.status_code == 400

    async def test_accessible_resources_empty_shape(self, client: TestClient) -> None:
        r = client.get(
            "/console/acl/agent/accessible", params={"bit": PERMISSION_BIT_VIEW}
        )
        assert r.status_code == 200
        assert r.json() == {"resource_ids": []}

    async def test_public_resource_ids_empty_shape(self, client: TestClient) -> None:
        r = client.get("/console/acl/agent/public", params={"bit": PERMISSION_BIT_VIEW})
        assert r.status_code == 200
        assert r.json() == {"resource_ids": []}

    async def test_sole_owned_empty_shape(self, client: TestClient) -> None:
        r = client.get("/console/acl/agent/sole-owned")
        assert r.status_code == 200
        assert r.json() == {"resource_ids": []}

    async def test_batch_permissions_empty_map(self, client: TestClient) -> None:
        r = client.post(
            "/console/acl/agent/permissions/batch",
            json={"resource_ids": ["a", "b"]},
        )
        assert r.status_code == 200
        assert r.json() == {"permissions": {}}

    async def test_batch_permissions_rejects_empty_list(
        self, client: TestClient
    ) -> None:
        r = client.post(
            "/console/acl/agent/permissions/batch", json={"resource_ids": []}
        )
        assert r.status_code == 422

    async def test_has_permission_rejects_bit_out_of_range(
        self, client: TestClient
    ) -> None:
        r = client.get("/console/acl/agent/res-1/has-permission", params={"bit": 16})
        assert r.status_code == 422

    async def test_seeded_public_row_is_visible_through_route(
        self, client: TestClient
    ) -> None:
        await _seed_row(
            user_sub="owner-route-1",
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-route-pub",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        r = client.get(
            "/console/acl/agent/res-route-pub/has-permission",
            params={"bit": PERMISSION_BIT_VIEW},
        )
        assert r.status_code == 200
        assert r.json() == {"has_permission": True}

        r2 = client.get(
            "/console/acl/agent/public", params={"bit": PERMISSION_BIT_VIEW}
        )
        assert r2.json() == {"resource_ids": ["res-route-pub"]}


# ── Scope enforcement ──────────────────────────────────────────────────────


class TestScopeEnforcement:
    def test_read_route_requires_acl_read_scope(self, client: TestClient) -> None:
        with patch("audittrace.auth.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                auth_enabled=True, auth_required=True
            )
            with patch("audittrace.auth._get_jwks_keys", return_value=["k"]):
                with patch(
                    "audittrace.auth._decode_jwt_with_allowed_issuers",
                    return_value={"sub": "no-scope-user", "scope": ""},
                ):
                    r = client.get(
                        "/console/acl/agent/res-1/permissions",
                        headers=_auth_headers("no-scope-user"),
                    )
        assert r.status_code == 403


# ── Cross-user isolation (real require_user, distinct real subs) ─────────


class TestCrossUserIsolation:
    """The RLS/isolation guard through the REAL HTTP route — neuter
    list guard #5. Neuter the service's ``_rls_mirror_clause`` (or the
    live Postgres RLS policy) and ``test_user_b_cannot_see_user_as_
    grant`` below goes RED."""

    def _act_as(self, client: TestClient, sub: str, method: str, path: str, **kwargs):
        with _identity(sub=sub):
            return client.request(method, path, headers=_auth_headers(sub), **kwargs)

    async def test_user_b_cannot_see_user_as_private_grant(
        self, client: TestClient
    ) -> None:
        await _seed_row(
            user_sub="alice-route",
            principal_type="user",
            principal_id="alice-route",
            principal_model="User",
            resource_type="agent",
            resource_id="res-alice-private",
            perm_bits=PERMISSION_BIT_VIEW,
        )

        bob_perms = self._act_as(
            client,
            "bob-route",
            "GET",
            "/console/acl/agent/res-alice-private/permissions",
        )
        assert bob_perms.json() == {"perm_bits": 0}, (
            "user B saw user A's private grant via the real HTTP route — "
            "the RLS/isolation wall is broken"
        )

        bob_has = self._act_as(
            client,
            "bob-route",
            "GET",
            "/console/acl/agent/res-alice-private/has-permission",
            params={"bit": PERMISSION_BIT_VIEW},
        )
        assert bob_has.json() == {"has_permission": False}

        alice_perms = self._act_as(
            client,
            "alice-route",
            "GET",
            "/console/acl/agent/res-alice-private/permissions",
        )
        assert alice_perms.json() == {"perm_bits": PERMISSION_BIT_VIEW}

    async def test_user_b_cannot_see_user_as_owned_resources(
        self, client: TestClient
    ) -> None:
        await _seed_row(
            user_sub="alice-owner-route",
            principal_type="user",
            principal_id="alice-owner-route",
            principal_model="User",
            resource_type="agent",
            resource_id="res-alice-owned",
            perm_bits=OWNER_PERMISSION_BITS,
        )

        bob_sole_owned = self._act_as(
            client, "bob-owner-route", "GET", "/console/acl/agent/sole-owned"
        )
        assert bob_sole_owned.json() == {"resource_ids": []}, (
            "user B's sole-owned list included user A's resource — "
            "isolation wall broken"
        )

        alice_sole_owned = self._act_as(
            client, "alice-owner-route", "GET", "/console/acl/agent/sole-owned"
        )
        assert alice_sole_owned.json() == {"resource_ids": ["res-alice-owned"]}

    async def test_user_b_cannot_see_user_as_accessible_resources(
        self, client: TestClient
    ) -> None:
        await _seed_row(
            user_sub="alice-acc-route",
            principal_type="user",
            principal_id="alice-acc-route",
            principal_model="User",
            resource_type="agent",
            resource_id="res-alice-acc",
            perm_bits=PERMISSION_BIT_EDIT,
        )

        bob_accessible = self._act_as(
            client,
            "bob-acc-route",
            "GET",
            "/console/acl/agent/accessible",
            params={"bit": PERMISSION_BIT_EDIT},
        )
        assert bob_accessible.json() == {"resource_ids": []}

        alice_accessible = self._act_as(
            client,
            "alice-acc-route",
            "GET",
            "/console/acl/agent/accessible",
            params={"bit": PERMISSION_BIT_EDIT},
        )
        assert alice_accessible.json() == {"resource_ids": ["res-alice-acc"]}

    async def test_public_row_is_visible_to_every_caller(
        self, client: TestClient
    ) -> None:
        """Non-vacuity companion: the isolation wall must not
        over-block PUBLIC rows either."""
        await _seed_row(
            user_sub="owner-of-public-route",
            principal_type="public",
            principal_id=None,
            principal_model=None,
            resource_type="agent",
            resource_id="res-truly-public",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        for sub in ("random-user-1", "random-user-2"):
            resp = self._act_as(
                client,
                sub,
                "GET",
                "/console/acl/agent/res-truly-public/has-permission",
                params={"bit": PERMISSION_BIT_VIEW},
            )
            assert resp.json() == {"has_permission": True}, (
                f"{sub} could not see the PUBLIC grant"
            )

    async def test_hostile_query_cannot_impersonate_another_principal(
        self, client: TestClient
    ) -> None:
        """No route accepts a principal_id/principal_type parameter at
        all — proven by confirming the has-permission/accessible routes
        ignore any such query param a hostile caller might add."""
        await _seed_row(
            user_sub="alice-hostile-route",
            principal_type="user",
            principal_id="alice-hostile-route",
            principal_model="User",
            resource_type="agent",
            resource_id="res-hostile",
            perm_bits=PERMISSION_BIT_VIEW,
        )
        bob_attempt = self._act_as(
            client,
            "bob-hostile-route",
            "GET",
            "/console/acl/agent/res-hostile/has-permission",
            params={
                "bit": PERMISSION_BIT_VIEW,
                "principal_id": "alice-hostile-route",
                "principal_type": "user",
            },
        )
        assert bob_attempt.json() == {"has_permission": False}, (
            "a hostile principal_id/principal_type query param let bob "
            "impersonate alice"
        )
