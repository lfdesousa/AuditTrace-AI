"""Unit tests for the ADR-049 evidence-gate parser
(``scripts/adr049_evidence_check.py``).

This parser is the SINGLE SOURCE OF TRUTH shared by the CI
``evidence-check`` job and the local ``make check-pr-body`` target
(#396). These tests lock the contract so the two can never drift and so
the 2026-07-30 regression — a PR body whose sections carried only
inline-backtick prose (neither a ``- [x]`` box nor a fenced code block)
sailing past a local check while CI rejected it — cannot re-open.

The module lives outside ``src/`` (it is a repo-level gate tool, not part
of the shipped package), so it is loaded by file path rather than
imported as a package.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "adr049_evidence_check.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "audittrace_adr049_evidence_check", _SCRIPT_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


# --- shared fixtures -------------------------------------------------------

# The exact shape of the corrected Batch A body (#365): a fenced block of
# real output in Verification, and ``- [x]`` N/A boxes in Validation +
# Reconstruction. This MUST pass.
_GOOD_BODY = """\
## Summary

Test + documentation only. No routes/services touched.

## Verification

Unit gates, full command output:

```
================= 1950 passed in 116.46s =================
Required test coverage of 90% reached. Total coverage: 98.21%
per-file coverage gate: PASS (63 files checked)
```

## Validation

- [x] N/A — test + documentation change only. No route/service code is
  touched, so there is no deployed behaviour to exercise.

## Reconstruction

- [x] N/A — a test + docs change produces no persisted artefact.
"""


def _make_body(verification: str, validation: str, reconstruction: str) -> str:
    return (
        "## Summary\n\nx\n\n"
        f"## Verification (unit)\n\n{verification}\n\n"
        f"## Validation (end-to-end against deployed image)\n\n{validation}\n\n"
        f"## Reconstruction (audit artefacts)\n\n{reconstruction}\n"
    )


_FENCED_REAL = "```\n1950 passed, 98% coverage\n```"
_CHECKED_BOX = "- [x] N/A — no runtime behaviour change."


# --- PASS cases ------------------------------------------------------------


def test_corrected_batch_a_body_passes(mod) -> None:
    """The real, merged Batch A body (fenced Verification + checked
    Validation/Reconstruction) passes with zero failures."""
    assert mod.check_evidence(_GOOD_BODY) == []


def test_headings_with_parenthetical_suffix_match(mod) -> None:
    """``## Verification (unit)`` etc. still match the ``\\b`` anchored
    regex — the template's real heading shape."""
    body = _make_body(_FENCED_REAL, _CHECKED_BOX, _CHECKED_BOX)
    assert mod.check_evidence(body) == []


def test_checked_box_is_case_insensitive(mod) -> None:
    """A ``- [X]`` (uppercase) box populates a section."""
    body = _make_body(_FENCED_REAL, "- [X] done", "- [x] done")
    assert mod.check_evidence(body) == []


# --- FAIL: missing section -------------------------------------------------


def test_missing_section_fails_with_exact_wording(mod) -> None:
    body = (
        "## Verification\n\n" + _FENCED_REAL + "\n\n"
        "## Validation\n\n" + _CHECKED_BOX + "\n"
        # Reconstruction section entirely absent.
    )
    failures = mod.check_evidence(body)
    assert "Missing required section: ## Reconstruction" in failures
    assert len(failures) == 1


def test_all_three_missing(mod) -> None:
    failures = mod.check_evidence("## Summary\n\nnothing else\n")
    assert failures == [
        "Missing required section: ## Verification",
        "Missing required section: ## Validation",
        "Missing required section: ## Reconstruction",
    ]


# --- FAIL: unpopulated section ---------------------------------------------


def test_only_unchecked_boxes_fails(mod) -> None:
    """A section with only ``- [ ]`` (unchecked) boxes is unfilled."""
    validation = "- [ ] Image tag deployed:\n- [ ] verify-deploy summary:"
    body = _make_body(_FENCED_REAL, validation, _CHECKED_BOX)
    failures = mod.check_evidence(body)
    assert failures == [
        "Section ## Validation (end-to-end against deployed image) has no "
        "checked box and no populated code block — looks like the template "
        "is unfilled"
    ]


def test_placeholder_only_fence_fails(mod) -> None:
    """A fenced block containing only ``<placeholder>`` markers does NOT
    populate a section."""
    verification = "```\n<paste: X passed, Y skipped, Z% coverage>\n```"
    body = _make_body(verification, _CHECKED_BOX, _CHECKED_BOX)
    failures = mod.check_evidence(body)
    assert failures == [
        "Section ## Verification (unit) has no checked box and no populated "
        "code block — looks like the template is unfilled"
    ]


# --- REGRESSION: the 2026-07-30 inline-backtick bug ------------------------


def test_regression_inline_backticks_are_not_a_code_block(mod) -> None:
    """#396 regression guard. A section whose only "evidence" is
    inline-backtick prose (``like `this` ``) — NOT a fenced ``` block and
    NOT a checked box — MUST fail the gate. This is the exact shape that
    passed no local check yet failed CI on 2026-07-30 and motivated
    single-sourcing the parser.
    """
    inline_prose = (
        "Ran `make test`: it printed `1950 passed` and `98% coverage`, and "
        "`check-per-file-coverage.py` said `PASS`."
    )
    body = _make_body(inline_prose, _CHECKED_BOX, _CHECKED_BOX)
    failures = mod.check_evidence(body)
    assert failures == [
        "Section ## Verification (unit) has no checked box and no populated "
        "code block — looks like the template is unfilled"
    ], "inline backticks must NOT be treated as a populated code block"


# --- CLI behaviour ---------------------------------------------------------


def test_cli_passes_on_good_body_file(mod, tmp_path, capsys) -> None:
    f = tmp_path / "good.md"
    f.write_text(_GOOD_BODY, encoding="utf-8")
    rc = mod.main(["adr049_evidence_check.py", str(f)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ADR-049 evidence gate: PASSED" in out


def test_cli_fails_and_prints_ci_identical_errors(mod, tmp_path, capsys) -> None:
    """The CLI on a broken body exits 1 and prints the SAME ``::error::``
    lines CI prints (locking the log contract)."""
    inline_prose = "Ran `make test`, all `1950 passed`."
    body = _make_body(inline_prose, _CHECKED_BOX, _CHECKED_BOX)
    f = tmp_path / "bad.md"
    f.write_text(body, encoding="utf-8")
    rc = mod.main(["adr049_evidence_check.py", str(f)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "::error::ADR-049 evidence gate FAILED" in out
    assert (
        "::error::  - Section ## Verification (unit) has no checked box "
        "and no populated code block — looks like the template is unfilled"
    ) in out
    assert (
        "::error::See ADR-049 + .github/pull_request_template.md. "
        "Check at least one box per section, OR paste real "
        "command output into a code block. No override available."
    ) in out


def test_cli_reads_from_stdin_when_no_arg(mod, monkeypatch, capsys) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(_GOOD_BODY))
    rc = mod.main(["adr049_evidence_check.py"])
    assert rc == 0
    assert "PASSED" in capsys.readouterr().out


def test_cli_empty_arg_falls_back_to_stdin(mod, monkeypatch, capsys) -> None:
    """An empty argv[1] (e.g. ``BODY=`` expansion) reads stdin, not a
    file named ''."""
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("## Summary\n\nx\n"))
    rc = mod.main(["adr049_evidence_check.py", ""])
    assert rc == 1
    out = capsys.readouterr().out
    assert "Missing required section: ## Verification" in out


def test_module_has_scoped_logger(mod) -> None:
    """feedback_module_scoped_loggers — the module uses a
    ``__name__``-scoped logger."""
    assert mod.logger.name == "audittrace_adr049_evidence_check"
