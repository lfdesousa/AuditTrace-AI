"""Mongo-audit verdict core — the pure function + response models (WU-A1).

Part of **WU-A1** of the Mongo-audit route
(``~/work/audittrace-private/specs/2026-09-25-SPEC-mongo-audit-sanctioned-read-route-CONSOLIDATED-v5.md``,
``sha256:b47de202681f36114e5568531e8ed11d320dbc63e3c4a976292d34663035ff72``). Pure; imports
nothing from ``pymongo``, FastAPI or ``audittrace.config`` (spec §7.1) — only the sibling A1
classification module, ``pydantic`` and the standard library.

``compute_audit()`` is a pure function over ``(server_collections, counts, classification)`` —
exactly why this WU needs no HTTP, no ``pymongo``, no live database to build or prove (spec §11,
"why three [work units] is right"). ``build_response()`` is the **one** constructor of
``MongoAuditResponse`` that both the handler (WU-A2) and the tests call, so there is exactly one
place the 14 response fields are assembled (spec §7.1, v3-A-3).

**Why the aggregate guard is layered, and why this module only carries two of the layers.** The
spec's N-3 rule ("per-collection counts, never an aggregate") was defeated four times across the
spec's review rounds — by a declared field, a computed field, an alias, and a data-conditional
serialiser (spec §5.2). The full guard is five layers; **this WU builds layers 1 and 3-5's
model side** (the ``extra="forbid"`` contract, the serialised-key-set pin, the value pin and the
declaration pin — all proven in ``tests/test_mongo_audit_verdict.py``'s T7). **Layer 2 (the
wire-level key pin) and the rest of layer 3-5's wire-side half belong to WU-A2**, which has an
HTTP route to pin against; nothing here can see a wire, only a model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from audittrace.services import mongo_audit_classification
from audittrace.services.mongo_audit_classification import Classification

Verdict = Literal["PASS", "FAIL", "INCOMPLETE"]

ROW_FIELDS: frozenset[str] = frozenset(
    {"name", "count", "classification", "expected_zero", "error", "row_verdict"}
)
POSITIVE_CONTROL_FIELDS: frozenset[str] = frozenset({"collection", "count", "passed"})
RESPONSE_FIELDS: frozenset[str] = frozenset(
    {
        "database",
        "server_host",
        "classification_version",
        "server_collections",
        "positive_control",
        "collections",
        "classified_but_absent",
        "unclassified",
        "user_nonzero",
        "verdict",
        "queried_at",
        "duration_ms",
        "trace_id",
        "audit_row_id",
    }
)


class CollectionRow(BaseModel):
    """One row of the per-collection count table (spec §5.2, N-3: never an aggregate).

    ``extra="forbid"`` is deliberate but is **not** the aggregate guard by itself (spec §5.2,
    "Why neither ``extra=forbid`` nor ``model_fields`` is the guard"): it only rejects
    undeclared keys at *parse* time and says nothing about what a ``@computed_field`` or a
    serialiser can inject at *dump* time. The guard is this WU's T7 — the serialised-key-set
    pin, the ``model_computed_fields == {}`` pin, and the declaration pin (no
    ``field_serializers``/``model_serializers``/``field_validators``/``model_validators``/
    ``computed_fields``, no field ``alias``/``serialization_alias``) — plus WU-A2's wire-level
    pins on the real route, which this module cannot see.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    count: int | None
    classification: Classification
    expected_zero: bool | None
    error: str | None
    row_verdict: Verdict


class PositiveControl(BaseModel):
    """N-2's structural positive control (spec §5.2, §5.3): ``users`` non-zero, reported
    before any zero counts as evidence."""

    model_config = ConfigDict(extra="forbid")

    collection: str
    count: int | None
    passed: bool


class MongoAuditResponse(BaseModel):
    """The full ``GET /system/mongo-audit`` 200 body (spec §5.2, 14 fields).

    WU-A1 owns this model and ``build_response()``; WU-A2 declares the route with
    ``response_model=MongoAuditResponse`` and returns the constructed instance, never a
    ``Response``/``JSONResponse``/dict (spec §5.1's return contract — a route-level guard this
    module cannot enforce on its own).
    """

    model_config = ConfigDict(extra="forbid")

    database: str
    server_host: str
    classification_version: str
    server_collections: list[str]
    positive_control: PositiveControl
    collections: list[CollectionRow]
    classified_but_absent: list[str]
    unclassified: list[str]
    user_nonzero: list[str]
    verdict: Verdict
    queried_at: str
    duration_ms: int
    trace_id: str
    audit_row_id: int


@dataclass(frozen=True)
class AuditComputation:
    """The pure verdict, before the eight handler-supplied identity/timing fields
    (``database``, ``server_host``, ``classification_version``, ``server_collections``,
    ``queried_at``, ``duration_ms``, ``trace_id``, ``audit_row_id``) are added by
    ``build_response()`` (spec §7.1, v3-A-3)."""

    collections: list[CollectionRow]
    classified_but_absent: list[str]
    unclassified: list[str]
    user_nonzero: list[str]
    positive_control: PositiveControl
    verdict: Verdict


def _resolve(
    name: str, counts: Mapping[str, int | None], errors: Mapping[str, str]
) -> tuple[int | None, str | None]:
    """Resolve one collection's ``(count, error)`` pair — **errors win** (spec §5.3, v2-A-3).

    A name present in BOTH maps reports the error, never the count: a stale count sitting next
    to a live error would misrepresent what was actually observed. A name absent from BOTH means
    the reader never reported it at all — ``count=None, error="missing"``, **never** coerced to
    0 ([[feedback_a_number_in_a_durable_artefact_must_reproduce]]: an absence is not a zero).
    """
    if name in errors:
        return None, errors[name]
    if name in counts:
        return counts[name], None
    return None, "missing"


def _row_verdict(
    classification: Classification, count: int | None, error: str | None
) -> Verdict:
    """One row's verdict (spec §5.3 step 2, §5.4, §9 T5/T6/T8).

    An error always wins (INCOMPLETE, never coerced to a pass or a fail). Otherwise an
    unclassified name is INCOMPLETE (N-4: a mis-typed or missing classification key never
    silently passes). Otherwise a nonzero USER row is FAIL. Everything else — OPERATIONAL rows
    (which may legitimately be nonzero) and zero USER rows — is PASS.
    """
    if error is not None:
        return "INCOMPLETE"
    if classification == "UNCLASSIFIED":
        return "INCOMPLETE"
    if classification == "USER" and count is not None and count > 0:
        return "FAIL"
    return "PASS"


_EXPECTED_ZERO: dict[Classification, bool | None] = {
    "USER": True,
    "OPERATIONAL": False,
    "UNCLASSIFIED": None,
}


def compute_audit(
    server_collections: Sequence[str],
    counts: Mapping[str, int | None],
    errors: Mapping[str, str],
    *,
    positive_control_collection: str,
) -> AuditComputation:
    """The pure verdict function — ``(server_collections, counts, classification)`` in, an
    ``AuditComputation`` out (spec §11, §5.3). No HTTP, no ``pymongo``; that is exactly why
    WU-A1 can be built and proven without either.

    ``server_collections`` is the population (N-1): one row is built for every name in it,
    sorted for determinism, names never renamed or normalised. A ratified classification key
    absent from that population lands in ``classified_but_absent``, not an error — Mongoose
    creates collections lazily, so an unused feature legitimately never materialises (spec §6
    "Denominator statement": absence is stronger evidence than a zero count).
    """
    server_set = set(server_collections)
    sorted_names = sorted(server_set)

    rows: list[CollectionRow] = []
    unclassified: list[str] = []
    user_nonzero: list[str] = []
    any_row_error = False

    for name in sorted_names:
        classification = mongo_audit_classification.classify(name)
        count, error = _resolve(name, counts, errors)
        rows.append(
            CollectionRow(
                name=name,
                count=count,
                classification=classification,
                expected_zero=_EXPECTED_ZERO[classification],
                error=error,
                row_verdict=_row_verdict(classification, count, error),
            )
        )
        if classification == "UNCLASSIFIED":
            unclassified.append(name)
        if error is not None:
            any_row_error = True
        if classification == "USER" and count is not None and count > 0:
            user_nonzero.append(name)

    classified_but_absent = sorted(
        name
        for name in mongo_audit_classification.CLASSIFICATION
        if name not in server_set
    )

    control_count: int | None = None
    if positive_control_collection in server_set:
        control_count, _control_error = _resolve(
            positive_control_collection, counts, errors
        )
    control_passed = control_count is not None and control_count >= 1
    positive_control = PositiveControl(
        collection=positive_control_collection,
        count=control_count,
        passed=control_passed,
    )

    verdict: Verdict
    if user_nonzero:
        # §5.3 step 3 — FAIL takes precedence over everything else.
        verdict = "FAIL"
    elif not control_passed or unclassified or any_row_error:
        # §5.3 step 4.
        verdict = "INCOMPLETE"
    else:
        # §5.3 step 5.
        verdict = "PASS"

    return AuditComputation(
        collections=rows,
        classified_but_absent=classified_but_absent,
        unclassified=unclassified,
        user_nonzero=user_nonzero,
        positive_control=positive_control,
        verdict=verdict,
    )


def build_response(
    computation: AuditComputation,
    *,
    database: str,
    server_host: str,
    classification_version: str,
    server_collections: Sequence[str],
    queried_at: str,
    duration_ms: int,
    trace_id: str,
    audit_row_id: int,
) -> MongoAuditResponse:
    """The ONE constructor of the response (spec §7.1, v3-A-3).

    The handler (WU-A2) and the tests (T7) both call this — there is exactly one place the 14
    response fields are assembled, so a field cannot drift between what is tested and what is
    served. ``server_collections`` is copied sorted here; nothing beyond field assembly is
    computed in this function.
    """
    return MongoAuditResponse(
        database=database,
        server_host=server_host,
        classification_version=classification_version,
        server_collections=sorted(server_collections),
        positive_control=computation.positive_control,
        collections=computation.collections,
        classified_but_absent=computation.classified_but_absent,
        unclassified=computation.unclassified,
        user_nonzero=computation.user_nonzero,
        verdict=computation.verdict,
        queried_at=queried_at,
        duration_ms=duration_ms,
        trace_id=trace_id,
        audit_row_id=audit_row_id,
    )
