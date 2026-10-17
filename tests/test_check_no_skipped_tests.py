"""ADDENDUM A1-5 (2026-10-11, review REJECT of f72cdd7, N4):
scripts/check-no-skipped-tests.py must read BOTH the per-testcase
``<skipped>`` child AND the ``<testsuite skipped="N">`` roll-up
attribute, and fail if either is non-zero — the old CI inline heredoc
summed the roll-up; this script only ever read the per-testcase shape.

Calls the REAL ``scripts.check_no_skipped_tests`` module's ``main()``
against real, hand-crafted junit XML fixtures on disk — never a
reimplementation of the parsing logic.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "check-no-skipped-tests.py"


def _load_module():
    """``scripts/check-no-skipped-tests.py`` has a hyphen in its filename,
    so it cannot be `import`ed by dotted path — load it directly."""
    spec = importlib.util.spec_from_file_location(
        "check_no_skipped_tests_under_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


_CLEAN_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="s" tests="1" failures="0" errors="0" skipped="0">
<testcase classname="c" name="test_ok" time="0.0"/>
</testsuite></testsuites>
"""

_PER_TESTCASE_SKIP_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="s" tests="1" failures="0" errors="0" skipped="1">
<testcase classname="c" name="test_skipped" time="0.0">
<skipped message="no docker"/>
</testcase>
</testsuite></testsuites>
"""

#: A1-5's own case: the SUITE-level roll-up is non-zero but there is no
#: per-testcase <skipped> child at all (e.g. a non-pytest junit writer, or
#: a plugin that only maintains the roll-up) — this is exactly what the
#: OLD per-testcase-only scan would miss.
_SUITE_LEVEL_ONLY_SKIP_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="s" tests="2" failures="0" errors="0" skipped="1">
<testcase classname="c" name="test_ok" time="0.0"/>
<testcase classname="c" name="test_quietly_skipped" time="0.0"/>
</testsuite></testsuites>
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "junit.xml"
    p.write_text(text)
    return p


def test_clean_junit_passes(tmp_path: Path) -> None:
    path = _write(tmp_path, _CLEAN_JUNIT)
    assert MODULE.main([str(path)]) == 0


def test_per_testcase_skip_fails(tmp_path: Path) -> None:
    path = _write(tmp_path, _PER_TESTCASE_SKIP_JUNIT)
    assert MODULE.main([str(path)]) == 1


def test_suite_level_only_skip_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A1-5: the roll-up alone, with no matching per-testcase <skipped>
    child, must still fail closed."""
    path = _write(tmp_path, _SUITE_LEVEL_ONLY_SKIP_JUNIT)
    assert MODULE.main([str(path)]) == 1
    captured = capsys.readouterr()
    assert "roll-up" in captured.err


def test_missing_file_fails_closed(tmp_path: Path) -> None:
    assert MODULE.main([str(tmp_path / "does-not-exist.xml")]) == 1


def test_no_argv_defaults_to_the_same_path_an_explicit_call_would_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No argv -> defaults to <repo-root>/junit.xml, resolved from the
    SCRIPT's own location, not cwd (same behaviour the Makefile/CI relied
    on before the A1-3/A1-5 path parameter was added). Proven without
    depending on whether a real junit.xml happens to exist at the repo
    root right now: both calls must report the IDENTICAL resolved path
    in their output."""
    monkeypatch.chdir(tmp_path)  # must NOT matter -- the default is NOT cwd-relative
    expected_default = SCRIPT_PATH.resolve().parent.parent / "junit.xml"

    MODULE.main([str(expected_default)])
    explicit_output = capsys.readouterr()
    MODULE.main([])
    no_argv_output = capsys.readouterr()

    assert str(expected_default) in explicit_output.out + explicit_output.err
    assert str(expected_default) in no_argv_output.out + no_argv_output.err
