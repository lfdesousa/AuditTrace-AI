"""Unit tests for ``scripts/neuter/classify.py`` -- THE VERDICT RULE
(SPEC v3 §4; SF-A, SF-B, SF-4), independent of the self-proofs' real
subprocess plumbing."""

from __future__ import annotations

import pytest

from scripts.neuter.classify import classify
from scripts.neuter.junit import JunitResult, JunitTestcase


def test_nocompile_short_circuits_everything():
    result = classify(
        ["a"],
        JunitResult(
            tests_collected=5,
            parse_failed=False,
            outcomes={"a": JunitTestcase("passed")},
        ),
        tests_expected=1,
        nocompile=True,
        pathcheck_ok=False,
        db_leak=True,
        timed_out=True,
        exit_code=99,
        watch_attached=True,
    )
    assert result.verdict == "ERROR"
    assert result.error_reason == "nocompile"
    assert result.error_reasons == ["nocompile"]
    assert result.tests_collected is None


def test_green_when_all_passed():
    jr = JunitResult(
        tests_collected=2,
        parse_failed=False,
        outcomes={"a": JunitTestcase("passed"), "b": JunitTestcase("passed")},
    )
    result = classify(
        ["a", "b"],
        jr,
        tests_expected=2,
        nocompile=False,
        pathcheck_ok=True,
        db_leak=False,
        timed_out=False,
        exit_code=0,
        watch_attached=True,
    )
    assert result.verdict == "GREEN"
    assert result.vacuous is True


def test_red_when_one_failed_assertion_shaped():
    jr = JunitResult(
        tests_collected=1,
        parse_failed=False,
        outcomes={"a": JunitTestcase("failed", "assert 1 == 2", "AssertionError")},
    )
    result = classify(
        ["a"],
        jr,
        tests_expected=1,
        nocompile=False,
        pathcheck_ok=True,
        db_leak=False,
        timed_out=False,
        exit_code=1,
        watch_attached=True,
    )
    assert result.verdict == "RED"
    assert result.error_reason is None
    assert result.failure_types == ["AssertionError"]


def test_call_exception_is_error_not_red():
    jr = JunitResult(
        tests_collected=1,
        parse_failed=False,
        outcomes={"a": JunitTestcase("failed", "RuntimeError: boom", "RuntimeError")},
    )
    result = classify(
        ["a"],
        jr,
        tests_expected=1,
        nocompile=False,
        pathcheck_ok=True,
        db_leak=False,
        timed_out=False,
        exit_code=1,
        watch_attached=True,
    )
    assert result.verdict == "ERROR"
    assert result.error_reason == "call_exception"
    assert result.vacuous is False


def test_pathcheck_error():
    jr = JunitResult(
        tests_collected=1, parse_failed=False, outcomes={"a": JunitTestcase("passed")}
    )
    result = classify(
        ["a"],
        jr,
        tests_expected=1,
        nocompile=False,
        pathcheck_ok=False,
        db_leak=False,
        timed_out=False,
        exit_code=0,
        watch_attached=True,
    )
    assert result.error_reason == "pathcheck"


def test_db_leak_error():
    jr = JunitResult(
        tests_collected=1, parse_failed=False, outcomes={"a": JunitTestcase("passed")}
    )
    result = classify(
        ["a"],
        jr,
        tests_expected=1,
        nocompile=False,
        pathcheck_ok=True,
        db_leak=True,
        timed_out=False,
        exit_code=0,
        watch_attached=True,
    )
    assert result.error_reason == "db_leak"


def test_timeout_error_junit_absent():
    result = classify(
        ["a"],
        None,
        tests_expected=1,
        nocompile=False,
        pathcheck_ok=True,
        db_leak=False,
        timed_out=True,
        exit_code=None,
        watch_attached=True,
    )
    assert result.error_reason == "timeout"
    assert "junit" in result.error_reasons  # junit was never produced either


def test_setup_or_teardown_error():
    jr = JunitResult(
        tests_collected=1, parse_failed=False, outcomes={"a": JunitTestcase("error")}
    )
    result = classify(
        ["a"],
        jr,
        tests_expected=1,
        nocompile=False,
        pathcheck_ok=True,
        db_leak=False,
        timed_out=False,
        exit_code=0,
        watch_attached=True,
    )
    assert result.error_reason == "setup_or_teardown"


def test_skipped_error():
    jr = JunitResult(
        tests_collected=1, parse_failed=False, outcomes={"a": JunitTestcase("skipped")}
    )
    result = classify(
        ["a"],
        jr,
        tests_expected=1,
        nocompile=False,
        pathcheck_ok=True,
        db_leak=False,
        timed_out=False,
        exit_code=0,
        watch_attached=True,
    )
    assert result.error_reason == "skipped"


def test_missing_error():
    jr = JunitResult(tests_collected=0, parse_failed=False, outcomes={})
    result = classify(
        ["a"],
        jr,
        tests_expected=1,
        nocompile=False,
        pathcheck_ok=True,
        db_leak=False,
        timed_out=False,
        exit_code=0,
        watch_attached=True,
    )
    assert "missing" in result.error_reasons


def test_collected_mismatch_error():
    jr = JunitResult(
        tests_collected=3, parse_failed=False, outcomes={"a": JunitTestcase("passed")}
    )
    result = classify(
        ["a"],
        jr,
        tests_expected=1,
        nocompile=False,
        pathcheck_ok=True,
        db_leak=False,
        timed_out=False,
        exit_code=0,
        watch_attached=True,
    )
    assert result.error_reason == "collected"


def test_junit_none_result_treated_as_parse_failed():
    result = classify(
        ["a"],
        None,
        tests_expected=1,
        nocompile=False,
        pathcheck_ok=True,
        db_leak=False,
        timed_out=False,
        exit_code=0,
        watch_attached=True,
    )
    assert "junit" in result.error_reasons


def test_bad_exit_code_error():
    jr = JunitResult(
        tests_collected=1, parse_failed=False, outcomes={"a": JunitTestcase("passed")}
    )
    result = classify(
        ["a"],
        jr,
        tests_expected=1,
        nocompile=False,
        pathcheck_ok=True,
        db_leak=False,
        timed_out=False,
        exit_code=5,
        watch_attached=True,
    )
    assert "exit_code" in result.error_reasons


def test_error_reasons_order_and_first_wins():
    jr = JunitResult(tests_collected=9, parse_failed=False, outcomes={})
    result = classify(
        ["a"],
        jr,
        tests_expected=1,
        nocompile=False,
        pathcheck_ok=False,
        db_leak=True,
        timed_out=True,
        exit_code=5,
        watch_attached=True,
    )
    assert result.error_reasons == [
        "collected",
        "pathcheck",
        "db_leak",
        "timeout",
        "missing",
        "exit_code",
    ]
    assert result.error_reason == "collected"


def test_outcomes_and_errors_lists_populated():
    jr = JunitResult(
        tests_collected=2,
        parse_failed=False,
        outcomes={
            "a": JunitTestcase("passed"),
            "b": JunitTestcase("error", "boom", None),
        },
    )
    result = classify(
        ["a", "b"],
        jr,
        tests_expected=2,
        nocompile=False,
        pathcheck_ok=True,
        db_leak=False,
        timed_out=False,
        exit_code=0,
        watch_attached=True,
    )
    assert result.outcomes == {"a": "passed", "b": "error"}
    assert result.errors == ["b"]


def test_chokepoint_marker_missing_forces_error():
    """Review round 4, requirement E2: `run_pytest()` detects, from the
    OUTSIDE, that `neuter_pathcheck` never ran during this invocation
    (e.g. a stray `-p no:neuter_pathcheck`) -- classify() must turn that
    into a hard ERROR, never a silent pass, regardless of exit code or
    otherwise-passing tests."""
    jr = JunitResult(
        tests_collected=1,
        parse_failed=False,
        outcomes={"t": JunitTestcase("passed")},
    )
    result = classify(
        ["t"],
        jr,
        tests_expected=1,
        nocompile=False,
        pathcheck_ok=True,
        db_leak=False,
        timed_out=False,
        exit_code=0,
        chokepoint_marker_ok=False,
        watch_attached=True,
    )
    assert result.verdict == "ERROR"
    assert result.error_reason == "chokepoint_marker_missing"


def test_chokepoint_marker_ok_true_is_the_default_and_never_errors():
    jr = JunitResult(
        tests_collected=1,
        parse_failed=False,
        outcomes={"t": JunitTestcase("passed")},
    )
    result = classify(
        ["t"],
        jr,
        tests_expected=1,
        nocompile=False,
        pathcheck_ok=True,
        db_leak=False,
        timed_out=False,
        exit_code=0,
        watch_attached=True,
    )
    assert result.verdict == "GREEN"


def test_watch_unproven_forces_error():
    """Review round 6 blocker fix: the continuous foreign-container watch's
    own readiness probe could never prove the `docker events` stream was
    live for this invocation (e.g. a slow/contended docker daemon) --
    `foreign_pg_container=False` from an unproven watch can never be
    trusted as "clean", so classify() must turn that into a hard ERROR,
    never a silent pass, regardless of exit code or otherwise-passing
    tests."""
    jr = JunitResult(
        tests_collected=1,
        parse_failed=False,
        outcomes={"t": JunitTestcase("passed")},
    )
    result = classify(
        ["t"],
        jr,
        tests_expected=1,
        nocompile=False,
        pathcheck_ok=True,
        db_leak=False,
        timed_out=False,
        exit_code=0,
        watch_attached=False,
    )
    assert result.verdict == "ERROR"
    assert result.error_reason == "watch_unproven"


def test_watch_attached_true_passed_explicitly_never_errors():
    """Review round 7 (O-1a): ``watch_attached`` is now a REQUIRED kwarg
    (no default) -- this test passes it explicitly, unlike its
    pre-round-7 namesake which relied on the (now-removed) default."""
    jr = JunitResult(
        tests_collected=1,
        parse_failed=False,
        outcomes={"t": JunitTestcase("passed")},
    )
    result = classify(
        ["t"],
        jr,
        tests_expected=1,
        nocompile=False,
        pathcheck_ok=True,
        db_leak=False,
        timed_out=False,
        exit_code=0,
        watch_attached=True,
    )
    assert result.verdict == "GREEN"


def test_classify_requires_watch_attached_explicitly():
    """Review round 7 (O-1a, orchestrator-confirmed blocker): dropping the
    explicit ``watch_attached=...`` pass at a ``classify()`` call site must
    be an immediate ``TypeError``, never a silent, fail-open default of
    ``True``."""
    jr = JunitResult(
        tests_collected=1,
        parse_failed=False,
        outcomes={"t": JunitTestcase("passed")},
    )
    with pytest.raises(TypeError, match="watch_attached"):
        classify(  # type: ignore[call-arg]
            ["t"],
            jr,
            tests_expected=1,
            nocompile=False,
            pathcheck_ok=True,
            db_leak=False,
            timed_out=False,
            exit_code=0,
        )
