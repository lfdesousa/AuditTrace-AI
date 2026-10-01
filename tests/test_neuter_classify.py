"""Unit tests for ``scripts/neuter/classify.py`` -- THE VERDICT RULE
(SPEC v3 §4; SF-A, SF-B, SF-4), independent of the self-proofs' real
subprocess plumbing."""

from __future__ import annotations

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
    )
    assert result.verdict == "GREEN"
