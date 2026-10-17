"""AC-5 — every PUBLISHED ``/console/acl`` request body forbids unknown
fields and declares no server-stamped field (ACL WU-2c-A, Addenda D + D1).

**Mechanism (measured on fastapi 0.142.2 / pydantic 2.13.5 / starlette
1.7.0).** The instrument is ``app.openapi()`` of the real application —
public API, and the artefact the service actually publishes. On the
resolved FastAPI the route table is not a flat list of ``APIRoute``
(``include_router`` appends a lazy ``_IncludedRouter``) and
``route.body_field`` has no ``.type_``, so any route-table walk would
depend on private/underscored API. The OpenAPI document does not.

Derivation: every operation under ``/console/acl`` that has a
``requestBody``; the JSON body schema is resolved through ``$ref`` into
``components.schemas`` and the WHOLE reference closure is walked
(``properties``, ``items``, ``additionalProperties``, ``anyOf``/``oneOf``/
``allOf``/``prefixItems``) to every reachable object schema. Each must
carry ``additionalProperties is False`` — on these versions a model
WITHOUT ``extra="forbid"`` OMITS the key, so a missing key FAILS.

Non-vacuity: the derived operation set must equal the literal list below
(derived from the document, then compared with a literal) — an empty or
partial derivation fails. ``/v1`` is never asserted (the derivation
filters strictly to ``/console/acl``).

The eight forbidden field names are written LITERALLY (not by reference).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from audittrace.server import create_app

ACL_PREFIX = "/console/acl"

# Written literally: the server-stamped / identity fields no ACL request
# body may declare (A §S5 + D1 advisory).
FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "user_sub",
        "granted_by",
        "trace_id",
        "session_id",
        "granted_at_ms",
        "created_at_ms",
        "updated_at_ms",
        "user_id",
    }
)

# W1, W3, W4, W5 and the WU-1 batch-read body (gains extra="forbid" in
# 2c-A — disclosure 19). W2 is DELETE + query parameters: no body.
EXPECTED_BODY_OPERATIONS = [
    ("patch", "/console/acl/{resource_type}/{resource_id}/grants"),
    ("post", "/console/acl/expire"),
    ("post", "/console/acl/{resource_type}/permissions/batch"),
    ("post", "/console/acl/{resource_type}/{resource_id}/grants"),
    ("post", "/console/acl/{resource_type}/{resource_id}/grants/bulk"),
]

_HTTP_METHODS = {"get", "put", "post", "delete", "patch", "options", "head"}


def derive_body_operations(openapi: dict[str, Any]) -> dict[tuple[str, str], str]:
    """``{(method, path): top-level schema name}`` for every
    ``/console/acl`` operation with a JSON ``requestBody``."""
    found: dict[tuple[str, str], str] = {}
    for path, item in openapi.get("paths", {}).items():
        if not path.startswith(ACL_PREFIX):
            continue
        for method, op in item.items():
            if method not in _HTTP_METHODS or "requestBody" not in op:
                continue
            schema = op["requestBody"]["content"]["application/json"]["schema"]
            found[(method, path)] = schema["$ref"].rsplit("/", 1)[-1]
    return found


def _closure(schemas: dict[str, Any], root: str) -> dict[str, dict[str, Any]]:
    """Every reachable named schema from ``root`` (reference closure)."""
    seen: dict[str, dict[str, Any]] = {}
    stack = [root]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen[name] = schemas[name]
        stack.extend(_refs_in(schemas[name]))
    return seen


def _refs_in(node: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            refs.append(ref.rsplit("/", 1)[-1])
        for key in ("properties",):
            for sub in (node.get(key) or {}).values():
                refs += _refs_in(sub)
        for key in ("items", "additionalProperties", "not"):
            sub = node.get(key)
            if isinstance(sub, dict):
                refs += _refs_in(sub)
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for sub in node.get(key) or []:
                refs += _refs_in(sub)
    return refs


def acl_body_problems(openapi: dict[str, Any]) -> list[str]:
    """Every violation found; ``[]`` only when the derived set equals the
    literal expected list AND every reachable object schema forbids
    extras AND no schema declares a forbidden field name."""
    problems: list[str] = []
    ops = derive_body_operations(openapi)
    if not ops:
        return ["derived an EMPTY set of /console/acl body operations"]
    if sorted(ops) != sorted(EXPECTED_BODY_OPERATIONS):
        problems.append(
            f"derived operations {sorted(ops)} != expected {EXPECTED_BODY_OPERATIONS}"
        )
    schemas = openapi.get("components", {}).get("schemas", {})
    for (method, path), root in sorted(ops.items()):
        for name, schema in _closure(schemas, root).items():
            is_object = schema.get("type") == "object" or "properties" in schema
            if is_object and schema.get("additionalProperties") is not False:
                problems.append(
                    f"{method.upper()} {path}: schema {name} does not forbid "
                    f"extra fields (additionalProperties="
                    f"{schema.get('additionalProperties', '<absent>')!r})"
                )
            banned = FORBIDDEN_FIELD_NAMES & set(schema.get("properties", {}))
            if banned:
                problems.append(
                    f"{method.upper()} {path}: schema {name} declares "
                    f"server-stamped field(s) {sorted(banned)}"
                )
    return problems


# ── the live pin ───────────────────────────────────────────────────────────


@pytest.fixture
def fresh_app(app: FastAPI) -> FastAPI:
    app.openapi_schema = None
    return app


class TestPublishedAclBodiesForbidExtras:
    def test_published_document_is_clean(self, fresh_app: FastAPI) -> None:
        assert acl_body_problems(fresh_app.openapi()) == []

    def test_derived_set_equals_the_literal_list(self, fresh_app: FastAPI) -> None:
        derived = sorted(derive_body_operations(fresh_app.openapi()))
        assert derived == sorted(EXPECTED_BODY_OPERATIONS)
        assert len(derived) == 5, "non-empty and complete"

    def test_nested_predicate_model_is_reached_by_the_closure(
        self, fresh_app: FastAPI
    ) -> None:
        """The $ref closure walk must reach ``ConsoleAclPredicate`` (nested
        inside ``ConsoleAclExpireRequest``) — otherwise Neuter 4 (drop
        forbid on the nested model only) could never redden."""
        doc = fresh_app.openapi()
        reached = _closure(doc["components"]["schemas"], "ConsoleAclExpireRequest")
        assert "ConsoleAclPredicate" in reached
        bulk = _closure(doc["components"]["schemas"], "ConsoleAclBulkRequest")
        assert "ConsoleAclGrantRequest" in bulk

    def test_v1_is_never_in_the_derivation(self, fresh_app: FastAPI) -> None:
        paths = {p for _m, p in derive_body_operations(fresh_app.openapi())}
        assert all(p.startswith(ACL_PREFIX) for p in paths)

    def test_forbid_emits_additional_properties_false_on_these_versions(self) -> None:
        """The mechanism claim, measured: pydantic's ``extra="forbid"``
        emits ``additionalProperties: false`` and a default model OMITS
        the key (so ``is False`` — not ``.get(...)`` truthiness — is the
        right assertion)."""
        from pydantic import ConfigDict

        class Forbids(BaseModel):
            model_config = ConfigDict(extra="forbid")
            a: int

        class Allows(BaseModel):
            a: int

        assert Forbids.model_json_schema()["additionalProperties"] is False
        assert "additionalProperties" not in Allows.model_json_schema()


# ── self-attacks: the instrument must redden on each defect ───────────────


class Scratch(BaseModel):
    """A body model that does NOT forbid extras (module level: a local
    class would be an unresolved forward ref under ``__future__``)."""

    a: int


class Lax(BaseModel):
    a: int


def _doc_with(mutate: Any, app: FastAPI) -> dict[str, Any]:
    import copy

    doc = copy.deepcopy(app.openapi())
    mutate(doc["components"]["schemas"])
    return doc


class TestInstrumentSelfAttacks:
    def test_neuter_1_one_top_level_model_loses_forbid(
        self, fresh_app: FastAPI
    ) -> None:
        doc = _doc_with(
            lambda s: s["ConsoleAclGrantRequest"].pop("additionalProperties"),
            fresh_app,
        )
        assert any("ConsoleAclGrantRequest" in p for p in acl_body_problems(doc))

    def test_neuter_4_only_the_nested_predicate_loses_forbid(
        self, fresh_app: FastAPI
    ) -> None:
        doc = _doc_with(
            lambda s: s["ConsoleAclPredicate"].pop("additionalProperties"), fresh_app
        )
        problems = acl_body_problems(doc)
        assert any("ConsoleAclPredicate" in p for p in problems)
        assert not any("ConsoleAclExpireRequest does not" in p for p in problems)

    def test_missing_key_fails_even_when_other_value_is_truthy(
        self, fresh_app: FastAPI
    ) -> None:
        doc = _doc_with(
            lambda s: s["ConsoleAclModifyRequest"].update(additionalProperties=True),
            fresh_app,
        )
        assert any("ConsoleAclModifyRequest" in p for p in acl_body_problems(doc))

    def test_neuter_5_a_forbidden_field_name_is_caught(
        self, fresh_app: FastAPI
    ) -> None:
        for name in sorted(FORBIDDEN_FIELD_NAMES):
            doc = _doc_with(
                lambda s, n=name: s["ConsoleAclGrantRequest"]["properties"].update(
                    {n: {"type": "string"}}
                ),
                fresh_app,
            )
            assert any(name in p for p in acl_body_problems(doc)), name

    def test_neuter_3_empty_derivation_is_a_failure_not_a_pass(self) -> None:
        assert acl_body_problems({"paths": {}, "components": {"schemas": {}}}) == [
            "derived an EMPTY set of /console/acl body operations"
        ]

    def test_partial_derivation_is_a_failure(self, fresh_app: FastAPI) -> None:
        import copy

        doc = copy.deepcopy(fresh_app.openapi())
        del doc["paths"]["/console/acl/expire"]
        assert any("derived operations" in p for p in acl_body_problems(doc))

    def test_neuter_2_scratch_prefixed_route_on_a_test_app_copy(
        self, fresh_app: FastAPI
    ) -> None:
        """A POST under ``/console/acl`` with a body model that does NOT
        forbid extras, mounted on a copy of the app, appears in
        ``openapi()`` and reddens the instrument — proof the derivation
        reads the published document, not a hand-kept list."""

        @fresh_app.post("/console/acl/scratch")
        async def scratch(body: Scratch) -> dict[str, int]:  # pragma: no cover
            return {"a": body.a}

        fresh_app.openapi_schema = None
        problems = acl_body_problems(fresh_app.openapi())
        assert any("/console/acl/scratch" in p for p in problems)
        assert any("derived operations" in p for p in problems)

    def test_unrelated_v1_body_models_are_not_asserted(
        self, fresh_app: FastAPI
    ) -> None:
        """Planting a lax body under a NON-ACL prefix changes nothing."""

        @fresh_app.post("/notacl/lax")
        async def lax(body: Lax) -> dict[str, int]:  # pragma: no cover
            return {"a": body.a}

        fresh_app.openapi_schema = None
        assert acl_body_problems(fresh_app.openapi()) == []


def test_real_factory_app_matches_fixture_app() -> None:
    """``create_app()`` (what the fixture wraps) publishes the same ACL
    body set — the instrument is not an artefact of the fixture."""
    assert sorted(derive_body_operations(create_app().openapi())) == sorted(
        EXPECTED_BODY_OPERATIONS
    )
