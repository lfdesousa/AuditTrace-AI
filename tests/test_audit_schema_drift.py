"""Schema-drift audit for `/interactions` and `/sessions` serialisers.

Guards against the failure mode we encountered 2026-04-18 morning:
migration 007 (ADR-033) added `status`, `failure_class`, `error_detail`,
`duration_ms` columns to `interactions` — but the `/interactions` route
handler's serialiser still only returned the pre-007 fields. Net effect:
every failed chat call was silently rendered as "success" in the audit
browser. The audit-trail promise was literally being obscured by the
audit browser itself.

These tests assert, structurally, that every SQLAlchemy column on each
audited table is surfaced in the corresponding route's serialiser dict.
A new migration that adds a column will cause this test to fail unless
the serialiser is updated alongside — which is exactly the invariant we
want.

Scope: this file covers the two audit-oriented routes (`/interactions`
and `/sessions`). It does NOT attempt to cover `/context`, `/memory/*`,
`/session/save` — those return shaped contexts rather than row
serialisations and are not subject to the same drift failure mode.
"""

from __future__ import annotations

from audittrace.db.models import InteractionRecord as InteractionRow
from audittrace.db.models import SessionRecord as SessionRow
from audittrace.routes.audit import _row_to_dict, _session_row_to_dict


def _model_column_names(model: type) -> set[str]:
    """Return the set of SQLAlchemy column names defined on `model`."""
    return {col.name for col in model.__table__.columns}


def _fake_row(model: type) -> object:
    """Build a duck-typed stand-in with an attribute for every column on
    the model. The serialisers do plain ``row.col_name`` lookups; a
    SimpleNamespace works without dragging the full SQLAlchemy ORM
    initialisation into a unit test."""
    from types import SimpleNamespace

    attrs: dict[str, object] = {}
    for col in model.__table__.columns:
        try:
            py_type = col.type.python_type
        except NotImplementedError:
            py_type = str  # fall back for exotic types
        if py_type is int:
            attrs[col.name] = 0
        elif py_type is bool:
            attrs[col.name] = False
        elif py_type is str:
            attrs[col.name] = ""
        else:
            attrs[col.name] = None
    return SimpleNamespace(**attrs)


def test_interactions_serialiser_covers_every_model_column() -> None:
    """Every column of the `interactions` table must appear as a key in
    the REST response. If a migration adds a column without updating the
    serialiser, this assertion fails and the drift is caught at CI time
    instead of silently at read time."""
    model_columns = _model_column_names(InteractionRow)
    serialised_keys = set(_row_to_dict(_fake_row(InteractionRow)).keys())
    missing = model_columns - serialised_keys
    assert not missing, (
        f"Columns present in InteractionRecord model but NOT in the "
        f"/interactions REST response: {sorted(missing)}. "
        "Add them to _row_to_dict in routes/audit.py. If a column is "
        "deliberately NOT exposed (e.g. sensitive internal field), "
        "document the exclusion alongside this test and add it here "
        "as an explicit allowlist."
    )


def test_sessions_serialiser_covers_every_model_column() -> None:
    """Same invariant for `/sessions` / `SessionRecord`."""
    model_columns = _model_column_names(SessionRow)
    serialised_keys = set(_session_row_to_dict(_fake_row(SessionRow)).keys())
    missing = model_columns - serialised_keys
    assert not missing, (
        f"Columns present in SessionRecord model but NOT in the "
        f"/sessions REST response: {sorted(missing)}. "
        "Add them to _session_row_to_dict in routes/audit.py."
    )


def test_interactions_serialiser_never_invents_keys() -> None:
    """Inverse direction: the serialiser must not produce keys that
    don't map back to a column on the model. Catches typos and
    deprecated-column remnants."""
    model_columns = _model_column_names(InteractionRow)
    serialised_keys = set(_row_to_dict(_fake_row(InteractionRow)).keys())
    extra = serialised_keys - model_columns
    assert not extra, (
        f"REST response keys that don't map to InteractionRecord columns: "
        f"{sorted(extra)}. Either add the column to the model or remove "
        "it from _row_to_dict."
    )


def test_sessions_serialiser_never_invents_keys() -> None:
    model_columns = _model_column_names(SessionRow)
    serialised_keys = set(_session_row_to_dict(_fake_row(SessionRow)).keys())
    extra = serialised_keys - model_columns
    assert not extra, (
        f"REST response keys that don't map to SessionRecord columns: {sorted(extra)}."
    )
