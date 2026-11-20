"""Unit tests for ``scripts/ci/check_collected_vs_junit.py`` (D6 gate)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.check_collected_vs_junit import (
    count_collected,
    count_junit_testcases,
    main,
)


def _junit(path: Path, n: int) -> Path:
    cases = "".join(f'<testcase classname="c" name="t{i}"/>' for i in range(n))
    path.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
    return path


def _ids(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n")
    return path


def test_count_collected_counts_only_node_id_lines():
    listing = (
        "tests/a.py::test_one\n"
        "tests/a.py::TestK::test_two[a b-1]\n"
        "bff/tests/b.py::test_three\n"
        "\n"
        "11128 tests collected in 27.22s\n"
        "warnings summary: tests/x.py:12: UserWarning\n"
    )
    assert count_collected(listing) == 3


def test_count_junit_testcases(tmp_path: Path):
    assert count_junit_testcases(_junit(tmp_path / "j.xml", 4)) == 4


def test_equal_counts_pass_and_print_the_numbers(tmp_path: Path, capsys):
    ids = _ids(tmp_path / "ids", ["tests/a.py::t0", "tests/a.py::t1"])
    junit = _junit(tmp_path / "j.xml", 2)

    assert main([str(ids), str(junit), "--workers", "8"]) == 0
    assert "collected=2 junit_testcases=2 workers=8" in capsys.readouterr().out


def test_fewer_run_than_collected_fails(tmp_path: Path, capsys):
    """Neuter: compare with `<=` the wrong way round / drop the check -> RED."""
    ids = _ids(tmp_path / "ids", ["tests/a.py::t0", "tests/a.py::t1"])
    junit = _junit(tmp_path / "j.xml", 1)

    assert main([str(ids), str(junit)]) == 1
    assert "collected 2 ids but junit holds 1 testcases" in capsys.readouterr().err


def test_more_run_than_collected_also_fails(tmp_path: Path):
    ids = _ids(tmp_path / "ids", ["tests/a.py::t0"])
    junit = _junit(tmp_path / "j.xml", 2)

    assert main([str(ids), str(junit)]) == 1


def test_zero_collected_fails_even_when_junit_is_also_empty(tmp_path: Path, capsys):
    """Zero == zero is not a pass: a gate that covered nothing is a failure."""
    ids = _ids(tmp_path / "ids", ["no tests collected"])
    junit = _junit(tmp_path / "j.xml", 0)

    assert main([str(ids), str(junit)]) == 1
    assert "no test ids were collected" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["ids", "junit"])
def test_a_missing_input_fails_closed(tmp_path: Path, missing: str, capsys):
    ids = _ids(tmp_path / "ids", ["tests/a.py::t0"])
    junit = _junit(tmp_path / "j.xml", 1)
    (ids if missing == "ids" else junit).unlink()

    assert main([str(ids), str(junit)]) == 1
    assert "not found" in capsys.readouterr().err
