"""#459 WU-459-2 closed vocabularies: BREAKER_CODES (AC-P4'/part) and STATE_SOURCES (S8).

Every assertion here is by LITERAL value (equality, never subset), so a code
added to or dropped from a set in ``errors.py`` turns a test RED: N1 (adding
``invalid_input`` to the breaker set) fails :func:`test_breaker_codes_exact_set`
and the ``invalid_input`` row of :func:`test_trip_table_over_all_fourteen_codes`.
"""

from __future__ import annotations

import pytest

from audittrace.services.decision.errors import (
    BREAKER_CODES,
    ERROR_CODES,
    RESULTS,
    STATE_SOURCES,
    result_for,
)

# The literal trip table over ALL 14 codes (Addendum 1 S5): only transport and
# endpoint faults open the breaker. Written out, not derived from the module.
TRIP_TABLE: dict[str, bool] = {
    "disabled": False,
    "connect_error": True,
    "timeout": True,
    "http_status": True,
    "malformed_response": False,
    "missing_probabilities": False,
    "option_not_single_token": False,
    "template_token_unresolved": False,
    "no_allowed_token_in_top": False,
    "model_identity_mismatch": False,
    "invalid_input": False,
    "internal_error": False,
    "skipped": False,
    "cancelled": False,
}


def test_breaker_codes_exact_set() -> None:
    assert BREAKER_CODES == {"connect_error", "timeout", "http_status"}
    assert len(BREAKER_CODES) == 3
    assert isinstance(BREAKER_CODES, frozenset)


def test_breaker_codes_are_a_subset_of_the_error_vocabulary() -> None:
    assert BREAKER_CODES <= ERROR_CODES
    assert len(ERROR_CODES) == 14
    assert set(TRIP_TABLE) == ERROR_CODES  # the table covers every code, once


@pytest.mark.parametrize(("code", "trips"), sorted(TRIP_TABLE.items()))
def test_trip_table_over_all_fourteen_codes(code: str, trips: bool) -> None:
    assert (code in BREAKER_CODES) is trips
    assert result_for(code) in RESULTS


def test_invalid_input_never_trips_the_breaker() -> None:
    assert "invalid_input" not in BREAKER_CODES
    assert "disabled" not in BREAKER_CODES
    assert "skipped" not in BREAKER_CODES
    assert "cancelled" not in BREAKER_CODES


def test_exactly_three_codes_trip() -> None:
    assert sum(TRIP_TABLE.values()) == 3
    assert {c for c, t in TRIP_TABLE.items() if t} == BREAKER_CODES


def test_skipped_and_cancelled_are_unavailable_not_breaker_input() -> None:
    assert result_for("skipped") == "unavailable"
    assert result_for("cancelled") == "unavailable"
    assert {result_for(c) for c in BREAKER_CODES} == {"unavailable", "timeout", "error"}


def test_state_sources_exact_set() -> None:
    assert STATE_SOURCES == {"context_query", "last_user_message", "empty"}
    assert len(STATE_SOURCES) == 3
    assert isinstance(STATE_SOURCES, frozenset)
