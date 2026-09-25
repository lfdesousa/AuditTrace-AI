"""Tests for the Mongo-audit verdict core (WU-A1, spec §9: T3, T5, T6, T6b, T7, T8).

Spec: ``2026-09-25-SPEC-mongo-audit-sanctioned-read-route-CONSOLIDATED-v5.md``
(``sha256:b47de202681f36114e5568531e8ed11d320dbc63e3c4a976292d34663035ff72``).

``compute_audit()`` and ``build_response()`` are pure functions over plain dict/list inputs — no
HTTP, no ``pymongo``, no app, no fixtures beyond the ones built here (spec §11).

**T7 is the load-bearing test in this file.** N-3 ("per-collection counts, never an aggregate")
was defeated four times across the spec's review rounds. This WU carries the **model-side** half
of the guard — four independent assertion groups, every one of which must hold on its own:
(i) the *serialised* key set of a constructed instance (never ``model_fields``, which a
``@computed_field`` bypasses); (ii) ``model_computed_fields == {}``; (iii) the dumped *value*, in
FastAPI's own mode and alias, against a literal; (iv) every ``__pydantic_decorators__`` bucket
empty and no field carries an alias. (i)-(iii) see a *fixture*; (iv) sees the *class declaration*
— a data-conditional serialiser the fixture never triggers is invisible to (i)-(iii) but not to
(iv) (see ``TestT7Asymmetry`` below). The wire-level half of the guard (T11, T2's real HTTP
route) is WU-A2's, not this module's.
"""

from __future__ import annotations

import re
from types import MappingProxyType

import pytest
from pydantic import field_serializer

from audittrace.services import mongo_audit_classification
from audittrace.services.mongo_audit_classification import (
    CLASSIFICATION,
    CLASSIFICATION_VERSION,
)
from audittrace.services.mongo_audit_verdict import (
    POSITIVE_CONTROL_FIELDS,
    RESPONSE_FIELDS,
    ROW_FIELDS,
    CollectionRow,
    MongoAuditResponse,
    PositiveControl,
    build_response,
    compute_audit,
)

# ---------------------------------------------------------------------------
# T3 — verdict algebra: the positive control gates PASS; USER-nonzero gates FAIL.
# ---------------------------------------------------------------------------


class TestVerdictAlgebra:
    def test_users_zero_is_incomplete_never_pass(self) -> None:
        comp = compute_audit(
            ["users"], {"users": 0}, {}, positive_control_collection="users"
        )
        assert comp.verdict == "INCOMPLETE"
        assert comp.positive_control.passed is False

    def test_users_nonzero_and_all_zero_user_rows_is_pass(self) -> None:
        comp = compute_audit(
            ["users", "conversations"],
            {"users": 2, "conversations": 0},
            {},
            positive_control_collection="users",
        )
        assert comp.verdict == "PASS"
        assert comp.positive_control.passed is True

    def test_fail_takes_precedence_over_a_failed_positive_control(self) -> None:
        # T8: a USER row with count>0 is FAIL even when the positive control itself failed —
        # FAIL outranks INCOMPLETE (spec §5.3 step 3 before step 4).
        comp = compute_audit(
            ["users", "agents"],
            {"users": 0, "agents": 3},
            {},
            positive_control_collection="users",
        )
        assert comp.verdict == "FAIL"
        assert comp.user_nonzero == ["agents"]
        assert comp.positive_control.passed is False


# ---------------------------------------------------------------------------
# T5 — errors are never coerced to 0; errors win over counts (precedence).
# ---------------------------------------------------------------------------


class TestErrorPrecedence:
    def test_error_row_count_is_none_not_zero(self) -> None:
        comp = compute_audit(
            ["x"], {}, {"x": "OperationFailure"}, positive_control_collection="users"
        )
        row = comp.collections[0]
        assert row.count is None
        assert row.error == "OperationFailure"
        assert row.row_verdict == "INCOMPLETE"

    def test_a_name_in_both_maps_has_the_error_win(self) -> None:
        comp = compute_audit(
            ["x"],
            {"x": 7},
            {"x": "OperationFailure"},
            positive_control_collection="users",
        )
        row = comp.collections[0]
        assert row.count is None
        assert row.error == "OperationFailure"
        assert row.row_verdict == "INCOMPLETE"

    def test_a_name_reported_by_neither_map_is_missing_not_zero(self) -> None:
        comp = compute_audit(["x"], {}, {}, positive_control_collection="users")
        row = comp.collections[0]
        assert row.count is None
        assert row.error == "missing"
        assert row.row_verdict == "INCOMPLETE"


# ---------------------------------------------------------------------------
# T6 — a mis-typed classification key never silently passes.
# ---------------------------------------------------------------------------


class TestClassificationTypoNeverFalsePasses:
    def test_case_mismatch_yields_unclassified_and_classified_but_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A deliberately wrong mapping key ("conversationTags" vs the server's
        # "conversationtags") must not exact-match — the row is UNCLASSIFIED and the mapping
        # key is reported as classified-but-absent (spec §9 T6). Patching the canonical mapping
        # here (rather than injecting a parameter compute_audit's frozen signature does not
        # accept, spec §11.1) affects both `classify()` and `compute_audit`'s
        # `classified_but_absent` derivation, which both read the module attribute dynamically.
        monkeypatch.setattr(
            mongo_audit_classification,
            "CLASSIFICATION",
            MappingProxyType({"conversationTags": "USER"}),
        )
        comp = compute_audit(
            ["conversationtags"],
            {"conversationtags": 0},
            {},
            positive_control_collection="users",
        )
        assert comp.unclassified == ["conversationtags"]
        assert comp.classified_but_absent == ["conversationTags"]
        assert comp.verdict == "INCOMPLETE"


# ---------------------------------------------------------------------------
# T7 — the model-side aggregate guard: keys, computed fields, value, declarations.
# ---------------------------------------------------------------------------

_FIXTURE_SERVER = ["users", "conversations", "agents", "somethingnew"]
_FIXTURE_COUNTS = {"users": 2, "conversations": 0, "agents": 3, "somethingnew": 1}


def _build_fixture_response() -> MongoAuditResponse:
    comp = compute_audit(
        _FIXTURE_SERVER, _FIXTURE_COUNTS, {}, positive_control_collection="users"
    )
    return build_response(
        comp,
        database="LibreChat",
        server_host="mongo-test:27017",
        classification_version=CLASSIFICATION_VERSION,
        server_collections=_FIXTURE_SERVER,
        queried_at="2026-09-25T12:00:00.000Z",
        duration_ms=312,
        trace_id="0" * 32,
        audit_row_id=1,
    )


# `classified_but_absent` is derived from the ratified CLASSIFICATION table (independently
# proven correct in test_mongo_audit_classification.py), never hand-typed — a 43-name literal
# would be pure transcription risk without adding any protection T12 does not already provide
# ([[feedback_a_number_in_a_durable_artefact_must_reproduce]]: derive, do not type).
_EXPECTED_CLASSIFIED_BUT_ABSENT = sorted(set(CLASSIFICATION) - set(_FIXTURE_SERVER))

EXPECTED_RESPONSE = {
    "database": "LibreChat",
    "server_host": "mongo-test:27017",
    "classification_version": CLASSIFICATION_VERSION,
    "server_collections": ["agents", "conversations", "somethingnew", "users"],
    "positive_control": {"collection": "users", "count": 2, "passed": True},
    "collections": [
        {
            "name": "agents",
            "count": 3,
            "classification": "USER",
            "expected_zero": True,
            "error": None,
            "row_verdict": "FAIL",
        },
        {
            "name": "conversations",
            "count": 0,
            "classification": "USER",
            "expected_zero": True,
            "error": None,
            "row_verdict": "PASS",
        },
        {
            "name": "somethingnew",
            "count": 1,
            "classification": "UNCLASSIFIED",
            "expected_zero": None,
            "error": None,
            "row_verdict": "INCOMPLETE",
        },
        {
            "name": "users",
            "count": 2,
            "classification": "OPERATIONAL",
            "expected_zero": False,
            "error": None,
            "row_verdict": "PASS",
        },
    ],
    "classified_but_absent": _EXPECTED_CLASSIFIED_BUT_ABSENT,
    "unclassified": ["somethingnew"],
    "user_nonzero": ["agents"],
    "verdict": "FAIL",
    "queried_at": "2026-09-25T12:00:00.000Z",
    "duration_ms": 312,
    "trace_id": "0" * 32,
    "audit_row_id": 1,
}


class TestT7Keys:
    """(i) the SERIALISED key set of a constructed instance — never ``model_fields``."""

    def test_response_keys(self) -> None:
        resp = _build_fixture_response()
        assert set(resp.model_dump()) == RESPONSE_FIELDS
        assert len(RESPONSE_FIELDS) == 14

    def test_row_keys(self) -> None:
        resp = _build_fixture_response()
        for row in resp.collections:
            assert set(row.model_dump()) == ROW_FIELDS
        assert len(ROW_FIELDS) == 6

    def test_positive_control_keys(self) -> None:
        resp = _build_fixture_response()
        assert set(resp.positive_control.model_dump()) == POSITIVE_CONTROL_FIELDS
        assert len(POSITIVE_CONTROL_FIELDS) == 3

    def test_one_row_per_server_collection(self) -> None:
        resp = _build_fixture_response()
        assert len(resp.collections) == len(resp.server_collections)
        assert {row.name for row in resp.collections} == set(resp.server_collections)


class TestT7ComputedFields:
    """(ii) no ``@computed_field`` on any of the three models."""

    def test_no_computed_fields(self) -> None:
        assert MongoAuditResponse.model_computed_fields == {}
        assert CollectionRow.model_computed_fields == {}
        assert PositiveControl.model_computed_fields == {}


class TestT7Value:
    """(iii) the dumped VALUE, in FastAPI's own mode (``json``) and alias (``by_alias=True``),
    against a literal — closes an alias route and a value-rewriting serialiser route that (i)
    and (ii) cannot see."""

    def test_value_matches_literal_exactly(self) -> None:
        resp = _build_fixture_response()
        assert resp.model_dump(mode="json", by_alias=True) == EXPECTED_RESPONSE


class TestT7Declarations:
    """(iv) every ``__pydantic_decorators__`` bucket empty, no field alias — closes a
    data-conditional serialiser that a fixture's rows never trigger (v4-B-1 fix 4)."""

    @pytest.mark.parametrize(
        "model", [MongoAuditResponse, CollectionRow, PositiveControl]
    )
    def test_no_decorators(self, model: type) -> None:
        decorators = model.__pydantic_decorators__
        assert dict(decorators.field_serializers) == {}
        assert dict(decorators.model_serializers) == {}
        assert dict(decorators.field_validators) == {}
        assert dict(decorators.model_validators) == {}
        assert dict(decorators.computed_fields) == {}

    @pytest.mark.parametrize(
        "model", [MongoAuditResponse, CollectionRow, PositiveControl]
    )
    def test_no_field_carries_an_alias(self, model: type) -> None:
        for field in model.model_fields.values():
            assert field.alias is None
            assert field.serialization_alias is None


class TestT7Asymmetry:
    """The asymmetry IS the proof (iii) and (iv) are independent instruments (spec §9 T7,
    v4-B-1 fix 4, §1 #68).

    A data-conditional ``@field_serializer`` that no fixture row triggers passes every
    fixture-driven pin — (i) keys, (ii) computed fields, (iii) value — because the fixture never
    exercises the poisoned branch. Only (iv), which inspects the *class declaration* rather than
    a dumped instance, sees it. If (iii) and (iv) ever agreed on this case, one of them would be
    redundant with the other.
    """

    def test_data_conditional_serializer_evades_iii_but_not_iv(self) -> None:
        class PoisonedRow(CollectionRow):
            """Same shape as ``CollectionRow``, plus a serialiser that only fires on a prefix
            none of the fixture's rows carry."""

            @field_serializer("count")
            def _poison_count(self, value: int | None, _info: object) -> int | None:
                if value is not None and self.name.startswith("zz"):
                    return value * 1000
                return value

        fixture_row = PoisonedRow(
            name="agents",
            count=3,
            classification="USER",
            expected_zero=True,
            error=None,
            row_verdict="FAIL",
        )
        clean_row = CollectionRow(
            name="agents",
            count=3,
            classification="USER",
            expected_zero=True,
            error=None,
            row_verdict="FAIL",
        )

        # (i) keys — GREEN: the serialiser rewrites a value, not the key set.
        assert set(fixture_row.model_dump()) == ROW_FIELDS
        # (ii) computed — GREEN: a field_serializer is not a computed_field.
        assert PoisonedRow.model_computed_fields == {}
        # (iii) value — GREEN: "agents" does not start with "zz", so the poison never fires;
        # the dumped value is identical to the clean model's.
        assert fixture_row.model_dump(mode="json") == clean_row.model_dump(mode="json")
        # (iv) declarations — RED: the poisoned class carries a field_serializer the clean
        # model does not.
        assert dict(PoisonedRow.__pydantic_decorators__.field_serializers) != {}
        assert dict(CollectionRow.__pydantic_decorators__.field_serializers) == {}

    def test_a_prefix_row_would_actually_be_smuggled(self) -> None:
        """Confirms the poison is not vacuous: a row whose name DOES start with "zz" really
        does carry an aggregated value on the wire, which is exactly what (iv) exists to catch
        before such a row ever reaches a fixture."""

        class PoisonedRow(CollectionRow):
            @field_serializer("count")
            def _poison_count(self, value: int | None, _info: object) -> int | None:
                if value is not None and self.name.startswith("zz"):
                    return value * 1000
                return value

        poisoned = PoisonedRow(
            name="zzagents",
            count=3,
            classification="USER",
            expected_zero=True,
            error=None,
            row_verdict="FAIL",
        )
        assert poisoned.model_dump(mode="json")["count"] == 3000


class TestBuildResponseIsTheOneConstructor:
    """v3-A-3: ``build_response`` is the ONE constructor — the handler (WU-A2) and the tests
    both call it, so a response field cannot drift between what is tested and what is served."""

    def test_server_collections_is_sorted_verbatim_copy(self) -> None:
        comp = compute_audit(
            ["b", "a"], {"a": 0, "b": 0}, {}, positive_control_collection="a"
        )
        resp = build_response(
            comp,
            database="LibreChat",
            server_host="h:27017",
            classification_version="v",
            server_collections=["b", "a"],
            queried_at="2026-09-25T12:00:00.000Z",
            duration_ms=1,
            trace_id="0" * 32,
            audit_row_id=1,
        )
        assert resp.server_collections == ["a", "b"]

    def test_run_variant_fields_pass_through(self) -> None:
        comp = compute_audit(["a"], {"a": 0}, {}, positive_control_collection="a")
        resp = build_response(
            comp,
            database="LibreChat",
            server_host="h:27017",
            classification_version="v",
            server_collections=["a"],
            queried_at="2026-09-25T12:00:00.000Z",
            duration_ms=42,
            trace_id="f" * 32,
            audit_row_id=999,
        )
        assert resp.queried_at == "2026-09-25T12:00:00.000Z"
        assert resp.duration_ms == 42
        assert re.fullmatch(r"[0-9a-f]{32}", resp.trace_id)
        assert resp.audit_row_id == 999
