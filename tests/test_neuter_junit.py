"""Unit tests for ``scripts/neuter/junit.py`` -- pytest 9.1.1 junit parsing
and the ``mangle_test_address`` forward-derivation (SPEC v3 §4)."""

from __future__ import annotations

from scripts.neuter.junit import mangle_node_id, parse_junit

_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="1" tests="4" time="0.1">
<testcase classname="tests.test_guarded" name="test_a" time="0.01" />
<testcase classname="tests.test_guarded" name="test_b" time="0.01">
<failure message="assert 4 == 5">assert 4 == 5</failure>
</testcase>
<testcase classname="tests.test_guarded.TestC" name="test_c[p1]" time="0.01">
<error message="failed on setup with RuntimeError">RuntimeError: boom</error>
</testcase>
<testcase classname="tests.test_guarded" name="test_d" time="0.01">
<skipped message="skip"/>
</testcase>
</testsuite></testsuites>
"""


def test_mangle_node_id_plain():
    assert mangle_node_id("tests/test_guarded.py::test_a") == (
        "tests.test_guarded",
        "test_a",
    )


def test_mangle_node_id_class_and_params():
    assert mangle_node_id("tests/test_guarded.py::TestC::test_c[p1]") == (
        "tests.test_guarded.TestC",
        "test_c[p1]",
    )


def test_parse_junit_absent_file(tmp_path):
    result = parse_junit(tmp_path / "missing.xml", ["a"])
    assert result.tests_collected is None
    assert result.parse_failed is True


def test_parse_junit_none_path():
    result = parse_junit(None, ["a"])
    assert result.parse_failed is True


def test_parse_junit_unparsable(tmp_path):
    path = tmp_path / "bad.xml"
    path.write_text("not xml <<<")
    result = parse_junit(path, ["a"])
    assert result.parse_failed is True
    assert result.tests_collected is None


def test_parse_junit_passed_failed_error_skipped(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text(_XML)
    ids = [
        "tests/test_guarded.py::test_a",
        "tests/test_guarded.py::test_b",
        "tests/test_guarded.py::TestC::test_c[p1]",
        "tests/test_guarded.py::test_d",
    ]
    result = parse_junit(path, ids)
    assert result.parse_failed is False
    assert result.tests_collected == 4
    assert result.outcomes[ids[0]].outcome == "passed"
    assert result.outcomes[ids[1]].outcome == "failed"
    assert result.outcomes[ids[1]].failure_type == "AssertionError"
    assert result.outcomes[ids[1]].assertion_shaped is True
    assert result.outcomes[ids[2]].outcome == "error"
    assert result.outcomes[ids[3]].outcome == "skipped"


def test_parse_junit_missing_id_not_in_outcomes(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text(_XML)
    result = parse_junit(path, ["tests/test_guarded.py::test_nonexistent"])
    assert "tests/test_guarded.py::test_nonexistent" not in result.outcomes


def test_failure_type_call_exception_shape(tmp_path):
    xml = """<testsuite tests="1"><testcase classname="tests.test_x" name="test_y">
    <failure message="sqlalchemy.exc.IntegrityError: duplicate key">boom</failure>
    </testcase></testsuite>"""
    path = tmp_path / "junit.xml"
    path.write_text(xml)
    result = parse_junit(path, ["tests/test_x.py::test_y"])
    outcome = result.outcomes["tests/test_x.py::test_y"]
    assert outcome.failure_type == "sqlalchemy.exc.IntegrityError"
    assert outcome.assertion_shaped is False


def test_failure_type_pytest_fail_did_not_raise(tmp_path):
    xml = """<testsuite tests="1"><testcase classname="tests.test_x" name="test_y">
    <failure message="Failed: DID NOT RAISE &lt;class 'ValueError'&gt;">boom</failure>
    </testcase></testsuite>"""
    path = tmp_path / "junit.xml"
    path.write_text(xml)
    result = parse_junit(path, ["tests/test_x.py::test_y"])
    outcome = result.outcomes["tests/test_x.py::test_y"]
    assert outcome.failure_type == "Failed"
    assert outcome.assertion_shaped is True


def test_no_testsuite_element(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text("<root/>")
    result = parse_junit(path, ["a"])
    assert result.parse_failed is True


def test_tests_attribute_unparsable(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text('<testsuite tests="not-a-number"></testsuite>')
    result = parse_junit(path, [])
    assert result.tests_collected is None
    assert result.parse_failed is False
