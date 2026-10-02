"""HTTP-route tests for the console-ACL WRITE routes (ACL WU-2c-A).

**What this file proves — and what it does NOT.** It runs on the
container's aiosqlite factory, where **no Row-Level Security exists**
(2b-core-A2 §8.14). It therefore proves route WIRING, scope gates, shape
bounds, the closed refusal->status mapping, trace/audit linkage and exact
audit-row deltas. It proves NOTHING about cross-user isolation: every
isolation claim lives in ``test_console_acl_routes_rls_postgres.py``
(real Postgres, real ``require_user`` cold path), because an isolation
assertion through this file's ``client`` would be green while production
refuses.

Acceptance criteria covered here: AC-1 (per-route scope sets), AC-3
(shape bounds, no row on a 422), AC-4 (closed mapping), AC-6 (trace
equality, non-vacuous), AC-7 (exact audit-row deltas), AC-9 (the routes
module carries no authority model), AC-10 (R7 ``trace_id`` filter).
"""

from __future__ import annotations

import ast
import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from audittrace import dependencies, telemetry
from audittrace.db.models import ConsoleAclEntry
from audittrace.routes import console_acl as routes_mod
from audittrace.routes.console_acl import _REFUSAL_STATUS, refusal_to_http
from audittrace.services.console_acl import _audit
from audittrace.services.console_acl._errors import (
    AclBulkRolledBackError,
    AclPastExpiryError,
    AclPrincipalTypeRefused,
    AclWriteRefused,
)

READ_SCOPE = "memory:acl:read-own"
WRITE_SCOPE = "memory:acl:write"
OWNER = "00000000-0000-0000-0000-000000000001"  # the bypass sentinel's sub
HEX32 = re.compile(r"[0-9a-f]{32}")


# ── helpers ────────────────────────────────────────────────────────────────


@contextmanager
def _identity(*, sub: str, scope: str):
    """The REAL ``require_user`` cold path for ``sub`` (never a
    ``dependency_overrides`` swap)."""
    with (
        patch("audittrace.auth.get_settings") as mock_settings,
        patch("audittrace.auth._get_jwks_keys") as mock_jwks,
        patch("audittrace.auth._decode_jwt_with_allowed_issuers") as mock_decode,
    ):
        mock_settings.return_value = MagicMock(auth_enabled=True, auth_required=True)
        mock_jwks.return_value = ["fake-key"]
        mock_decode.return_value = {"sub": sub, "scope": scope}
        yield


def _headers(sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer token-for-{sub}"}


def _grants(rid: str, rtype: str = "agent") -> str:
    return f"/console/acl/{rtype}/{rid}/grants"


def _grant_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "principal_type": "user",
        "principal_id": "principal-1",
        "perm_bits": 1,
    }
    body.update(over)
    return body


def _acl_rows(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    resp = client.get(
        "/interactions",
        params={"event_class": "acl_authz", "limit": 1000, **params},
    )
    assert resp.status_code == 200
    return resp.json()["interactions"]


def _answer(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row["answer"])


async def _entry_rows(resource_id: str) -> list[ConsoleAclEntry]:
    """The stored ``console_acl_entries`` rows for a resource, read
    directly (the service's read shape omits ``trace_id``)."""
    pg = dependencies.get_postgres_factory()
    async with pg.get_session_factory()() as db:
        result = await db.execute(
            sa.select(ConsoleAclEntry)
            .where(ConsoleAclEntry.resource_id == resource_id)
            .order_by(ConsoleAclEntry.created_at_ms, ConsoleAclEntry.id)
        )
        return list(result.scalars())


@pytest.fixture
def recording_tracer(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """A REAL recording provider behind ``@log_call``'s tracer, so
    ``current_trace_id_hex`` returns a real 32-hex id (S6: under no
    recording provider it is ``None`` and ``None == None`` proves
    nothing)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("acl-write-test"))
    return exporter


@pytest.fixture
def global_recording_provider() -> TracerProvider:
    """Install a real GLOBAL provider (the ADR-033 500 handler reads the
    ambient span via ``trace.get_current_span()``, which only yields a
    valid id when the server span the instrumentor opens is recording).
    The OTel API allows setting it once per process; this mirrors the
    precedent in ``test_chat_failure_audit.py``."""
    from opentelemetry import trace as otel_trace

    provider = TracerProvider()
    otel_trace.set_tracer_provider(provider)
    return provider


# Each entry: (id, method, path, kwargs) — a VALID request on that route.
def _valid_requests(rid: str) -> list[tuple[str, str, str, dict[str, Any]]]:
    return [
        ("W1", "POST", _grants(rid), {"json": _grant_body()}),
        (
            "W2",
            "DELETE",
            _grants(rid),
            {"params": {"principal_type": "user", "principal_id": "principal-1"}},
        ),
        (
            "W3",
            "PATCH",
            _grants(rid),
            {
                "json": {
                    "principal_type": "user",
                    "principal_id": "principal-1",
                    "add_bits": 2,
                }
            },
        ),
        (
            "W4",
            "POST",
            _grants(rid) + "/bulk",
            {"json": {"ops": [_grant_body()]}},
        ),
        (
            "W5",
            "POST",
            "/console/acl/expire",
            {"json": {"predicates": [{"resource_id": rid}]}},
        ),
    ]


# ── AC-1: per-route scope sets ─────────────────────────────────────────────


class TestScopeGates:
    @pytest.mark.parametrize("scope", ["", READ_SCOPE, "audittrace:audit"])
    @pytest.mark.parametrize("idx", range(5))
    def test_write_route_403_without_write_scope(
        self, client: TestClient, idx: int, scope: str
    ) -> None:
        rid = "scope-gate"
        _id, method, path, kwargs = _valid_requests(rid)[idx]
        with _identity(sub="sub-gate", scope=scope):
            r = client.request(method, path, headers=_headers("sub-gate"), **kwargs)
        assert r.status_code == 403, _id

    @pytest.mark.parametrize("idx", range(5))
    def test_write_route_opens_with_write_scope(
        self, client: TestClient, idx: int
    ) -> None:
        """The neuter's GREEN half: add the missing scope and the SAME
        request is no longer a 403 (so the 403 above is the scope gate,
        not a broken route)."""
        _id, method, path, kwargs = _valid_requests("scope-open")[idx]
        with _identity(sub="sub-open", scope=WRITE_SCOPE):
            r = client.request(method, path, headers=_headers("sub-open"), **kwargs)
        assert r.status_code in (200, 201, 404), (_id, r.text)

    def test_write_scope_alone_does_not_open_a_read_route(
        self, client: TestClient
    ) -> None:
        with _identity(sub="sub-w", scope=WRITE_SCOPE):
            r = client.get(
                "/console/acl/agent/x/permissions", headers=_headers("sub-w")
            )
        assert r.status_code == 403

    def test_read_scope_still_opens_a_read_route(self, client: TestClient) -> None:
        with _identity(sub="sub-r", scope=READ_SCOPE):
            r = client.get(
                "/console/acl/agent/x/permissions", headers=_headers("sub-r")
            )
        assert r.status_code == 200

    def test_declared_scope_strings(self) -> None:
        assert routes_mod._WRITE_SCOPE == "memory:acl:write"
        assert routes_mod._READ_SCOPE == "memory:acl:read-own"


# ── AC-3: shape bounds — 422 and NO acl_authz row ──────────────────────────


def _over(n: int) -> str:
    return "x" * n


_BAD: list[tuple[str, str, str, dict[str, Any]]] = [
    ("perm_bits=16", "POST", _grants("r"), {"json": _grant_body(perm_bits=16)}),
    ("perm_bits=-1", "POST", _grants("r"), {"json": _grant_body(perm_bits=-1)}),
    (
        "unknown principal_type",
        "POST",
        _grants("r"),
        {"json": _grant_body(principal_type="group")},
    ),
    (
        "negative expiry",
        "POST",
        _grants("r"),
        {"json": _grant_body(expired_at_ms=-5)},
    ),
    (
        "principal_id 65",
        "POST",
        _grants("r"),
        {"json": _grant_body(principal_id=_over(65))},
    ),
    (
        "tenant_id 65",
        "POST",
        _grants("r"),
        {"json": _grant_body(tenant_id=_over(65))},
    ),
    ("role_id 37", "POST", _grants("r"), {"json": _grant_body(role_id=_over(37))}),
    ("resource_id 37", "POST", _grants(_over(37)), {"json": _grant_body()}),
    (
        "hostile user_sub",
        "POST",
        _grants("r"),
        {"json": _grant_body(user_sub="someone-else")},
    ),
    (
        "hostile granted_by",
        "POST",
        _grants("r"),
        {"json": _grant_body(granted_by="someone-else")},
    ),
    (
        "hostile trace_id",
        "POST",
        _grants("r"),
        {"json": _grant_body(trace_id="0" * 32)},
    ),
    (
        "modify both None",
        "PATCH",
        _grants("r"),
        {"json": {"principal_type": "user", "principal_id": "p"}},
    ),
    (
        "modify add_bits=16",
        "PATCH",
        _grants("r"),
        {"json": {"principal_type": "user", "principal_id": "p", "add_bits": 16}},
    ),
    (
        "modify remove_bits=-1",
        "PATCH",
        _grants("r"),
        {"json": {"principal_type": "user", "principal_id": "p", "remove_bits": -1}},
    ),
    (
        "bulk 201 ops",
        "POST",
        _grants("r") + "/bulk",
        {"json": {"ops": [_grant_body()] * 201}},
    ),
    ("bulk 0 ops", "POST", _grants("r") + "/bulk", {"json": {"ops": []}}),
    (
        "bulk op bad bits",
        "POST",
        _grants("r") + "/bulk",
        {"json": {"ops": [_grant_body(perm_bits=99)]}},
    ),
    (
        "bulk hostile resource in op",
        "POST",
        _grants("r") + "/bulk",
        {"json": {"ops": [_grant_body(resource_id="other")]}},
    ),
    (
        "revoke bad principal_type",
        "DELETE",
        _grants("r"),
        {"params": {"principal_type": "group", "principal_id": "p"}},
    ),
    ("revoke no principal_type", "DELETE", _grants("r"), {"params": {}}),
    (
        "revoke principal_id 65",
        "DELETE",
        _grants("r"),
        {"params": {"principal_type": "user", "principal_id": _over(65)}},
    ),
    (
        "expire 201 predicates",
        "POST",
        "/console/acl/expire",
        {"json": {"predicates": [{"resource_id": "r"}] * 201}},
    ),
    (
        "expire 0 predicates",
        "POST",
        "/console/acl/expire",
        {"json": {"predicates": []}},
    ),
    (
        "expire unknown key",
        "POST",
        "/console/acl/expire",
        {"json": {"predicates": [{"resource_id": "r", "granted_by": "x"}]}},
    ),
    (
        "expire bad resource_type",
        "POST",
        "/console/acl/expire",
        {"json": {"predicates": [{"resource_id": "r", "resource_type": "nope"}]}},
    ),
    (
        "expire no anchor (service ValueError -> 422)",
        "POST",
        "/console/acl/expire",
        {"json": {"predicates": [{"resource_type": "agent"}]}},
    ),
    (
        "expire empty predicate",
        "POST",
        "/console/acl/expire",
        {"json": {"predicates": [{}]}},
    ),
]


class TestShapeBounds:
    @pytest.mark.parametrize(
        ("label", "method", "path", "kwargs"), _BAD, ids=[b[0] for b in _BAD]
    )
    def test_bad_shape_is_422_and_writes_no_row(
        self, client: TestClient, label: str, method: str, path: str, kwargs: dict
    ) -> None:
        before = len(_acl_rows(client))
        r = client.request(method, path, **kwargs)
        assert r.status_code == 422, (label, r.text)
        assert len(_acl_rows(client)) == before, f"{label}: a 422 wrote a row"

    def test_unknown_resource_type_is_400_and_writes_no_row(
        self, client: TestClient
    ) -> None:
        before = len(_acl_rows(client))
        r = client.post(_grants("r", "nope"), json=_grant_body())
        assert r.status_code == 400
        assert len(_acl_rows(client)) == before

    def test_bounds_equal_the_031_widths(self) -> None:
        """Pin the numbers the spec derived from migration 031 so a drift
        in either direction is loud."""
        from audittrace.models import (
            ConsoleAclGrantRequest,
            ConsoleAclPredicate,
        )
        from audittrace.services.console_acl import MAX_PERM_BITS

        assert MAX_PERM_BITS == 15
        grant = ConsoleAclGrantRequest.model_json_schema()["properties"]
        assert grant["perm_bits"]["maximum"] == MAX_PERM_BITS
        assert grant["perm_bits"]["minimum"] == 0
        assert _max_len(grant["principal_id"]) == 64
        assert _max_len(grant["tenant_id"]) == 64
        assert _max_len(grant["role_id"]) == 36
        pred = ConsoleAclPredicate.model_json_schema()["properties"]
        assert _max_len(pred["resource_id"]) == 36
        assert _max_len(pred["principal_id"]) == 64
        from audittrace.models import ConsoleAclModifyRequest

        modify = ConsoleAclModifyRequest.model_json_schema()["properties"]
        for operand in ("add_bits", "remove_bits"):
            # redundant with the route's _validate_bit_operands (A4) by
            # design, so the MODEL bound is pinned on the schema itself.
            assert _bound(modify[operand], "maximum") == MAX_PERM_BITS
            assert _bound(modify[operand], "minimum") == 0


def _bound(prop: dict[str, Any], key: str) -> int:
    if key in prop:
        return int(prop[key])
    for alt in prop.get("anyOf", []):
        if key in alt:
            return int(alt[key])
    raise AssertionError(f"no {key} in {prop}")


def _max_len(prop: dict[str, Any]) -> int:
    if "maxLength" in prop:
        return int(prop["maxLength"])
    for alt in prop.get("anyOf", []):
        if "maxLength" in alt:
            return int(alt["maxLength"])
    raise AssertionError(f"no maxLength in {prop}")


# ── AC-4: the closed refusal -> status mapping ─────────────────────────────


class TestRefusalMapping:
    def test_closed_table_is_exactly_the_spec(self) -> None:
        assert _REFUSAL_STATUS == {
            "acl_denied_policy": 403,
            "acl_denied_bulk_rollback": 403,
            "acl_denied_principal_type": 400,
            "acl_denied_past_expiry": 400,
        }

    @pytest.mark.parametrize(
        ("exc", "status"),
        [
            (AclWriteRefused("x", failure_class="acl_denied_policy"), 403),
            (
                AclBulkRolledBackError("x", failure_class="acl_denied_bulk_rollback"),
                403,
            ),
            (
                AclPrincipalTypeRefused("x", failure_class="acl_denied_principal_type"),
                400,
            ),
            (AclPastExpiryError("x", failure_class="acl_denied_past_expiry"), 400),
        ],
    )
    def test_each_class_maps_to_its_status(
        self, exc: AclWriteRefused, status: int
    ) -> None:
        """``acl_denied_principal_type`` is UNREACHABLE through the route
        by construction (the ``principal_type`` Literal answers 422
        first; the 031 CHECK is proven at the service by A1), so this is
        a unit test of the mapping function — disclosed as such. The
        other three are driven through the route below."""
        http = refusal_to_http(exc)
        assert http.status_code == status
        assert set(http.detail) == {"failure_class", "db_error_class", "trace_id"}
        assert http.detail["failure_class"] == exc.failure_class

    def test_unknown_failure_class_fails_closed_500(self) -> None:
        exc = AclWriteRefused("x", failure_class="something_new")
        assert refusal_to_http(exc).status_code == 500

    def test_past_expiry_through_the_route_is_400_with_one_denial_row(
        self, client: TestClient
    ) -> None:
        before = len(_acl_rows(client))
        r = client.post(_grants("map-past"), json=_grant_body(expired_at_ms=1))
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["failure_class"] == "acl_denied_past_expiry"
        assert detail["db_error_class"] == "app:past_expiry"
        assert "trace_id" in detail
        rows = _acl_rows(client)
        assert len(rows) == before + 1
        denial = next(x for x in rows if ":map-past " in x["question"] + " ")
        assert denial["status"] == "failed"
        assert denial["failure_class"] == "acl_denied_past_expiry"

    async def test_bulk_op_two_past_expiry_is_403_rollback_and_no_entries(
        self, client: TestClient, user_context: Any
    ) -> None:
        before = len(_acl_rows(client))
        body = {"ops": [_grant_body(principal_id=OWNER), _grant_body(expired_at_ms=1)]}
        r = client.post(_grants("map-bulk") + "/bulk", json=body)
        assert r.status_code == 403
        assert r.json()["detail"]["failure_class"] == "acl_denied_bulk_rollback"
        service = dependencies.get_console_acl_service()
        assert (
            await service.find_entries_by_resource(user_context, "agent", "map-bulk")
            == []
        )
        rows = _acl_rows(client)
        assert len(rows) == before + 1, (
            "exactly ONE denial row, no surviving success row"
        )
        assert rows[0]["status"] == "failed"

    def test_policy_denial_maps_to_403_through_the_route(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The REAL ``acl_denied_policy`` cell is N-1 on Postgres (RLS).
        Here the route is shown to MAP a service-raised policy refusal;
        the refusal itself is a patched raise and is labelled so."""
        service = dependencies.get_console_acl_service()

        async def refuse(*_a: Any, **_k: Any) -> Any:
            raise AclWriteRefused(
                "denied", failure_class="acl_denied_policy", db_error_class="42501"
            )

        monkeypatch.setattr(service, "grant_permission", refuse)
        r = client.post(_grants("map-policy"), json=_grant_body())
        assert r.status_code == 403
        detail = r.json()["detail"]
        # ``trace_id`` is whatever span the process has (None without a
        # recording provider, 32-hex with one); the mapping must not
        # depend on test order, so it is checked for SHAPE only here —
        # equality is AC-6's job under a recording provider.
        trace = detail.pop("trace_id")
        assert trace is None or HEX32.fullmatch(trace)
        assert detail == {
            "failure_class": "acl_denied_policy",
            "db_error_class": "42501",
        }

    @pytest.mark.parametrize(
        ("method", "suffix", "kwargs", "svc_method"),
        [
            ("DELETE", "", {"params": {"principal_type": "user"}}, "revoke_permission"),
            (
                "PATCH",
                "",
                {"json": {"principal_type": "user", "add_bits": 1}},
                "modify_permission_bits",
            ),
            (
                "POST",
                "/bulk",
                {"json": {"ops": [_grant_body()]}},
                "bulk_write_acl_entries",
            ),
        ],
    )
    def test_every_write_route_maps_a_refusal(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        method: str,
        suffix: str,
        kwargs: dict,
        svc_method: str,
    ) -> None:
        service = dependencies.get_console_acl_service()

        async def refuse(*_a: Any, **_k: Any) -> Any:
            raise AclWriteRefused("denied", failure_class="acl_denied_policy")

        monkeypatch.setattr(service, svc_method, refuse)
        r = client.request(method, _grants("map-all") + suffix, **kwargs)
        assert r.status_code == 403

    def test_expire_route_maps_a_refusal(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = dependencies.get_console_acl_service()

        async def refuse(*_a: Any, **_k: Any) -> Any:
            raise AclWriteRefused("denied", failure_class="acl_denied_policy")

        monkeypatch.setattr(service, "delete_acl_entries", refuse)
        r = client.post(
            "/console/acl/expire", json={"predicates": [{"resource_id": "x"}]}
        )
        assert r.status_code == 403

    def test_audit_write_failure_is_500_with_one_failed_row_and_no_entry(
        self,
        app: Any,
        client: TestClient,
        recording_tracer: InMemorySpanExporter,
        global_recording_provider: TracerProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """B1/SA-3: the direct ``record_denial`` path re-raises the
        ORIGINAL exception; the existing ADR-033 handler answers 500.
        The raiser fails on the FIRST ``_content_hash`` call only (record's
        own) so the writer's own ``acl_audit_write_failed`` row lands."""
        original = _audit._content_hash
        calls = {"n": 0}

        def flaky(*args: object, **kwargs: object) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("content_hash broken for this test")
            return original(*args, **kwargs)

        monkeypatch.setattr(_audit, "_content_hash", flaky)
        with TestClient(app, raise_server_exceptions=False) as quiet:
            r = quiet.post(_grants("map-500"), json=_grant_body(principal_id=OWNER))
        assert r.status_code == 500
        err = r.json()["error"]
        assert err["code"] == "internal_error"
        rows = [
            x
            for x in _acl_rows(client)
            if x["failure_class"] == "acl_audit_write_failed"
            and "map-500" in x["question"]
        ]
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert json.loads(rows[0]["error_detail"])["db_error_class"] == "RuntimeError"
        # zero entries for the key: the domain write rolled back
        eff = client.get("/console/acl/agent/map-500/permissions")
        assert eff.json() == {"perm_bits": 0}
        # AC-6 extended to the 500: the envelope's trace id is the
        # denial row's trace id (32 hex), not None == None.
        env_trace = err["trace_id"]
        assert env_trace is not None and HEX32.fullmatch(env_trace), (
            "vacuous without a real trace id (None == None proves nothing)"
        )
        assert rows[0]["trace_id"] == env_trace

    def test_audit_failure_neuter_no_op_raiser_lands_201(
        self,
        app: Any,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The GREEN half of the 500 case: with a no-op raiser the same
        request is a 201, so the 500 above is caused by the audit failure
        and nothing else."""
        original = _audit._content_hash
        monkeypatch.setattr(_audit, "_content_hash", lambda *a, **k: original(*a, **k))
        r = client.post(_grants("map-500-ok"), json=_grant_body(principal_id=OWNER))
        assert r.status_code == 201


# ── happy shapes + AC-6 trace linkage + AC-7 exact row deltas ─────────────


class TestHappyShapesAndAudit:
    def test_grant_201_shape_and_no_user_sub_echo(
        self, client: TestClient, recording_tracer: InMemorySpanExporter
    ) -> None:
        r = client.post(_grants("shape-1"), json=_grant_body(perm_bits=5))
        assert r.status_code == 201
        item = r.json()
        assert item["perm_bits"] == 5
        assert item["bits"] == {
            "VIEW": True,
            "EDIT": False,
            "DELETE": True,
            "SHARE": False,
        }
        assert item["granted_by"] == OWNER
        assert "user_sub" not in item
        assert set(item) == {
            "id",
            "principal_type",
            "principal_id",
            "principal_model",
            "resource_type",
            "resource_id",
            "perm_bits",
            "bits",
            "role_id",
            "granted_by",
            "granted_at_ms",
            "expired_at_ms",
            "inherited_from",
            "tenant_id",
            "trace_id",
            "created_at_ms",
            "updated_at_ms",
        }

    def test_resource_id_with_slash_routes_through_path(
        self, client: TestClient
    ) -> None:
        r = client.post(_grants("a/b"), json=_grant_body())
        assert r.status_code == 201
        assert r.json()["resource_id"] == "a/b"

    def test_w3_404_body_and_row(
        self, client: TestClient, recording_tracer: InMemorySpanExporter
    ) -> None:
        before = len(_acl_rows(client))
        r = client.patch(
            _grants("w3-miss"),
            json={"principal_type": "user", "principal_id": "nobody", "add_bits": 1},
        )
        assert r.status_code == 404
        body = r.json()
        assert body["detail"] == "no active grant"
        assert HEX32.fullmatch(body["trace_id"])
        rows = _acl_rows(client, trace_id=body["trace_id"])
        assert len(rows) == 1 and len(_acl_rows(client)) == before + 1
        assert rows[0]["status"] == "success"
        ans = _answer(rows[0])
        assert ans["acl_entry_ids"] == [] and ans["expired_ids"] == []

    def test_w2_idempotent_200_empty(
        self, client: TestClient, recording_tracer: InMemorySpanExporter
    ) -> None:
        r = client.delete(
            _grants("w2-none"),
            params={"principal_type": "user", "principal_id": "nobody"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["expired_ids"] == [] and body["visible_matched_count"] == 0
        assert HEX32.fullmatch(body["trace_id"])


class TestTraceEquality:
    """AC-6 (S6 non-vacuity): the response's ``trace_id`` is 32-hex BEFORE
    the equality, and equals the audit row's and the entry row's. Neuters:
    return ``None`` / a fresh uuid / the expired row's trace -> RED."""

    async def test_w1_three_way(
        self,
        client: TestClient,
        user_context: Any,
        recording_tracer: InMemorySpanExporter,
    ) -> None:
        r = client.post(_grants("tr-w1"), json=_grant_body())
        assert r.status_code == 201
        item = r.json()
        assert HEX32.fullmatch(item["trace_id"]), "vacuous without a real trace id"
        audit = _acl_rows(client, trace_id=item["trace_id"])
        assert len(audit) == 1
        assert audit[0]["trace_id"] == item["trace_id"]
        entries = await _entry_rows("tr-w1")
        assert [e.id for e in entries] == [item["id"]]
        assert entries[0].trace_id == item["trace_id"]

    async def test_w2_expired_row_keeps_the_grants_trace(
        self,
        client: TestClient,
        user_context: Any,
        recording_tracer: InMemorySpanExporter,
    ) -> None:
        g = client.post(_grants("tr-w2"), json=_grant_body()).json()
        r = client.delete(
            _grants("tr-w2"),
            params={"principal_type": "user", "principal_id": "principal-1"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["expired_ids"] == [g["id"]]
        assert HEX32.fullmatch(body["trace_id"])
        assert body["trace_id"] != g["trace_id"]
        audit = _acl_rows(client, trace_id=body["trace_id"])
        assert len(audit) == 1 and _answer(audit[0])["expired_ids"] == [g["id"]]
        entries = await _entry_rows("tr-w2")
        assert entries[0].expired_at_ms is not None
        assert entries[0].trace_id == g["trace_id"], (
            "expired row keeps the GRANT's trace"
        )

    async def test_w3_new_row_and_w4_w5_trace(
        self,
        client: TestClient,
        user_context: Any,
        recording_tracer: InMemorySpanExporter,
    ) -> None:
        client.post(_grants("tr-w3"), json=_grant_body())
        m = client.patch(
            _grants("tr-w3"),
            json={
                "principal_type": "user",
                "principal_id": "principal-1",
                "add_bits": 2,
            },
        )
        assert m.status_code == 200
        item = m.json()
        assert item["perm_bits"] == 3 and HEX32.fullmatch(item["trace_id"])
        assert [
            r["trace_id"] for r in _acl_rows(client, trace_id=item["trace_id"])
        ] == [item["trace_id"]]
        b = client.post(
            _grants("tr-w4") + "/bulk",
            json={"ops": [_grant_body(), _grant_body(principal_id="p2")]},
        )
        bb = b.json()
        assert HEX32.fullmatch(bb["trace_id"]) and len(bb["acl_entry_ids"]) == 2
        for e in await _entry_rows("tr-w4"):
            assert e.trace_id == bb["trace_id"]
        x = client.post(
            "/console/acl/expire", json={"predicates": [{"resource_id": "tr-w4"}]}
        )
        assert HEX32.fullmatch(x.json()["trace_id"])


class TestExactRowDeltas:
    """AC-7: per request the ``interactions`` delta is EXACTLY the
    service's — asserted as an integer, never ``>= 1``. Neuter: have the
    route write a second row, or swallow a denial -> the integer moves."""

    def _delta(self, client: TestClient, fn: Any) -> int:
        before = len(_acl_rows(client))
        fn()
        return len(_acl_rows(client)) - before

    def test_w1_one(self, client: TestClient) -> None:
        assert (
            self._delta(
                client, lambda: client.post(_grants("d-w1"), json=_grant_body())
            )
            == 1
        )

    def test_w2_one_even_when_nothing_matched(self, client: TestClient) -> None:
        d = self._delta(
            client,
            lambda: client.delete(
                _grants("d-w2"), params={"principal_type": "user", "principal_id": "z"}
            ),
        )
        assert d == 1

    def test_w3_one_match_and_one_miss(self, client: TestClient) -> None:
        client.post(_grants("d-w3"), json=_grant_body())
        body = {"principal_type": "user", "principal_id": "principal-1", "add_bits": 2}
        assert (
            self._delta(client, lambda: client.patch(_grants("d-w3"), json=body)) == 1
        )
        miss = {"principal_type": "user", "principal_id": "ghost", "add_bits": 2}
        assert (
            self._delta(client, lambda: client.patch(_grants("d-w3"), json=miss)) == 1
        )

    @pytest.mark.parametrize("k", [1, 3, 7])
    def test_w4_one_per_op(self, client: TestClient, k: int) -> None:
        ops = [_grant_body(principal_id=f"p{i}") for i in range(k)]
        assert (
            self._delta(
                client,
                lambda: client.post(_grants(f"d-w4-{k}") + "/bulk", json={"ops": ops}),
            )
            == k
        )

    @pytest.mark.parametrize("n", [1, 2, 5])
    def test_w5_one_per_predicate(self, client: TestClient, n: int) -> None:
        preds = [{"resource_id": f"d-w5-{n}-{i}"} for i in range(n)]
        assert (
            self._delta(
                client,
                lambda: client.post("/console/acl/expire", json={"predicates": preds}),
            )
            == n
        )

    def test_refusal_is_one_denial_row(self, client: TestClient) -> None:
        d = self._delta(
            client,
            lambda: client.post(_grants("d-deny"), json=_grant_body(expired_at_ms=1)),
        )
        assert d == 1

    def test_422_and_400_resource_type_are_zero(self, client: TestClient) -> None:
        assert (
            self._delta(
                client,
                lambda: client.post(_grants("d-422"), json=_grant_body(perm_bits=99)),
            )
            == 0
        )
        assert (
            self._delta(
                client,
                lambda: client.post(_grants("d-400", "nope"), json=_grant_body()),
            )
            == 0
        )

    def test_session_id_is_null_on_every_row_under_default_context(
        self, client: TestClient
    ) -> None:
        """AC-8's observable half (the 'no test binds a session' half is a
        reviewer grep): under the default context the rows carry no
        session — O-2/Q-7, M5 not landed."""
        client.post(_grants("d-sess"), json=_grant_body())
        assert all(r["session_id"] is None for r in _acl_rows(client))


# ── AC-9: no authority model in the routes module ──────────────────────────

_FORBIDDEN_NAME = re.compile(r"PERMISSION_BIT_")


def authority_violations(source: str) -> list[str]:
    """Static drift detector (NOT a correctness guard — N-1..N-5 are):
    the routes module imports nothing from ``_ownership``, names no
    ``PERMISSION_BIT_*`` and calls no ``owns(``."""
    problems: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and "_ownership" in (node.module or ""):
            problems.append(f"imports {node.module}")
        if isinstance(node, ast.Import):
            problems += [
                f"imports {a.name}" for a in node.names if "_ownership" in a.name
            ]
        if isinstance(node, ast.ImportFrom):
            problems += [
                f"imports {a.name}"
                for a in node.names
                if _FORBIDDEN_NAME.search(a.name) or a.name == "owns"
            ]
        if isinstance(node, ast.Name) and _FORBIDDEN_NAME.search(node.id):
            problems.append(f"names {node.id}")
        if isinstance(node, ast.Attribute) and _FORBIDDEN_NAME.search(node.attr):
            problems.append(f"names {node.attr}")
        if isinstance(node, ast.Call):
            fn = node.func
            callee = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if callee == "owns":
                problems.append("calls owns(")
    return problems


class TestNoAuthorityModelInRoutes:
    def test_routes_module_is_clean(self) -> None:
        source = Path(routes_mod.__file__).read_text(encoding="utf-8")
        assert authority_violations(source) == []

    @pytest.mark.parametrize(
        "planted",
        [
            "from audittrace.services.console_acl._ownership import owns",
            "import audittrace.services.console_acl._ownership",
            "from audittrace.services.console_acl import PERMISSION_BIT_SHARE",
            "x = PERMISSION_BIT_SHARE",
            "x = acl.PERMISSION_BIT_VIEW",
            "owns(user, 'agent', 'a')",
            "svc.owns(user, 'agent', 'a')",
        ],
    )
    def test_detector_catches_each_planted_violation(self, planted: str) -> None:
        assert authority_violations(planted), planted


# ── AC-10: R7 — GET /interactions?trace_id= ────────────────────────────────


class TestInteractionsTraceIdFilter:
    def test_returns_exactly_the_rows_with_that_trace(
        self, client: TestClient, recording_tracer: InMemorySpanExporter
    ) -> None:
        a = client.post(_grants("r7-a"), json=_grant_body()).json()
        b = client.post(_grants("r7-b"), json=_grant_body()).json()
        assert a["trace_id"] != b["trace_id"]
        rows_a = client.get("/interactions", params={"trace_id": a["trace_id"]}).json()
        assert [r["trace_id"] for r in rows_a["interactions"]] == [a["trace_id"]]
        assert rows_a["total"] == 1
        assert ":r7-a " in rows_a["interactions"][0]["question"] + " "

    def test_composes_with_event_class(
        self, client: TestClient, recording_tracer: InMemorySpanExporter
    ) -> None:
        a = client.post(_grants("r7-c"), json=_grant_body()).json()
        hit = client.get(
            "/interactions",
            params={"trace_id": a["trace_id"], "event_class": "acl_authz"},
        ).json()
        miss = client.get(
            "/interactions",
            params={"trace_id": a["trace_id"], "event_class": "interaction"},
        ).json()
        assert hit["total"] == 1 and miss["total"] == 0

    def test_unknown_trace_is_empty_not_everything(self, client: TestClient) -> None:
        client.post(_grants("r7-d"), json=_grant_body())
        body = client.get("/interactions", params={"trace_id": "0" * 32}).json()
        assert body["total"] == 0 and body["interactions"] == []

    def test_published_parameter_declares_length_and_pattern(
        self, client: TestClient
    ) -> None:
        """The length bound is redundant with the pattern at runtime, so
        the PUBLISHED parameter schema pins all three (a dropped length
        would otherwise be invisible)."""
        doc = client.get("/openapi.json").json()
        params = doc["paths"]["/interactions"]["get"]["parameters"]
        schema = next(p for p in params if p["name"] == "trace_id")["schema"]
        arms = schema.get("anyOf", [schema])
        string_arm = next(a for a in arms if a.get("type") == "string")
        assert string_arm["minLength"] == 32 and string_arm["maxLength"] == 32
        assert string_arm["pattern"] == "^[0-9a-f]{32}$"

    @pytest.mark.parametrize("bad", ["abc", "G" * 32, "A" * 32, "0" * 31, "0" * 33])
    def test_malformed_trace_is_422(self, client: TestClient, bad: str) -> None:
        assert client.get("/interactions", params={"trace_id": bad}).status_code == 422
