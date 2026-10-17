"""Unit tests for the pre-push ADR-049 PR-body gate decision logic
(``scripts/hooks/prepush_pr_body_gate.py``, #397, marker
ADR049-PRBODY-AUTOENFORCE-20260803).

These tests exercise the extracted decision logic DIRECTLY — no real
``git push`` is ever invoked (per the spec's R4). They cover the full
decision table:

  - ``PR_BODY`` set + body PASSES the ADR-049 gate  -> allow (exit 0).
  - ``PR_BODY`` set + body FAILS the ADR-049 gate    -> BLOCK (exit != 0),
    printing the CI-identical ``::error::`` lines (single source of
    truth — the SAME ``scripts/adr049_evidence_check.py`` CI and
    ``make check-pr-body`` use, #396 invariant).
  - ``PR_BODY`` unset                                 -> allow, with a
    non-blocking reminder on feature branches (silent on main/master).
  - ``PR_BODY=none`` (case-insensitive)                -> allow, silent
    (explicit, acknowledged skip).

``scripts/hooks`` is a real package (``scripts/hooks/__init__.py``) so
pytest-cov measures it the same way it already measures
``scripts/deploy`` — imported normally, unlike
``scripts/adr049_evidence_check.py`` (a deliberately loose, unmeasured
script loaded by file path in ``tests/test_adr049_evidence_check.py``).
"""

from __future__ import annotations

import pytest

from scripts.hooks import prepush_pr_body_gate as _prepush_pr_body_gate


@pytest.fixture(scope="module")
def mod():
    return _prepush_pr_body_gate


# --- shared fixtures ---------------------------------------------------

_GOOD_BODY = """\
## Summary

x

## Verification

```
1950 passed, 98% coverage
```

## Validation

- [x] N/A — no runtime behaviour change.

## Reconstruction

- [x] N/A — no persisted artefact.
"""

_BAD_BODY = """\
## Summary

x

## Verification

Ran `make test`, all `1950 passed`.

## Validation

- [x] N/A — no runtime behaviour change.

## Reconstruction

- [x] N/A — no persisted artefact.
"""

_FEATURE_REF = "refs/heads/ci/397-adr049-prepush-autoenforce"
_MAIN_REF = "refs/heads/main"
_MASTER_REF = "refs/heads/master"


# --- classify_pr_body_env ----------------------------------------------


def test_classify_none_value_is_unset(mod) -> None:
    assert mod.classify_pr_body_env(None) == "unset"


def test_classify_empty_string_is_unset(mod) -> None:
    assert mod.classify_pr_body_env("") == "unset"


@pytest.mark.parametrize("raw", ["none", "None", "NONE", " none ", "NoNe"])
def test_classify_none_literal_is_skip_case_insensitive(mod, raw: str) -> None:
    assert mod.classify_pr_body_env(raw) == "skip"


def test_classify_real_path_is_path(mod) -> None:
    assert mod.classify_pr_body_env("/home/user/work/pr-bodies/x.md") == "path"


def test_classify_none_word_inside_longer_string_is_path(mod) -> None:
    """A path that merely CONTAINS "none" (e.g. a filename) must not be
    misclassified as the skip literal — only an exact (trimmed,
    case-insensitive) match to "none" is the skip mode."""
    assert mod.classify_pr_body_env("/tmp/none-of-the-above.md") == "path"


# --- is_protected_ref ----------------------------------------------------


def test_main_ref_is_protected(mod) -> None:
    assert mod.is_protected_ref(_MAIN_REF) is True


def test_master_ref_is_protected(mod) -> None:
    assert mod.is_protected_ref(_MASTER_REF) is True


def test_feature_ref_is_not_protected(mod) -> None:
    assert mod.is_protected_ref(_FEATURE_REF) is False


def test_empty_ref_is_not_protected(mod) -> None:
    assert mod.is_protected_ref("") is False


# --- run_pr_body_gate: PR_BODY=none -> silent skip -----------------------


def test_skip_mode_allows_silently(mod, capsys) -> None:
    rc = mod.run_pr_body_gate("none", _FEATURE_REF)
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_skip_mode_case_insensitive_on_main(mod, capsys) -> None:
    rc = mod.run_pr_body_gate("NONE", _MAIN_REF)
    assert rc == 0
    assert capsys.readouterr().out == ""


# --- run_pr_body_gate: PR_BODY unset -------------------------------------


def test_unset_on_feature_branch_allows_with_reminder(mod, capsys) -> None:
    rc = mod.run_pr_body_gate(None, _FEATURE_REF)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Reminder: set PR_BODY=" in out
    assert "PR_BODY=none" in out


def test_unset_on_main_allows_silently(mod, capsys) -> None:
    rc = mod.run_pr_body_gate(None, _MAIN_REF)
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_unset_on_master_allows_silently(mod, capsys) -> None:
    rc = mod.run_pr_body_gate("", _MASTER_REF)
    assert rc == 0
    assert capsys.readouterr().out == ""


# --- run_pr_body_gate: PR_BODY set to a real path ------------------------


def test_set_and_passing_body_allows(mod, tmp_path, capsys) -> None:
    """R1: PR_BODY set + passing -> allow (exit 0)."""
    body_file = tmp_path / "good-pr-body.md"
    body_file.write_text(_GOOD_BODY, encoding="utf-8")

    rc = mod.run_pr_body_gate(str(body_file), _FEATURE_REF)

    assert rc == 0
    out = capsys.readouterr().out
    assert "ADR-049 evidence gate: PASSED" in out
    # No block tail on a passing body.
    assert "BLOCKED" not in out


def test_set_and_failing_body_blocks_with_ci_identical_output(
    mod, tmp_path, capsys
) -> None:
    """R1: PR_BODY set + failing -> BLOCK (exit != 0), CI-identical
    ::error:: output PLUS the one-line 'fix the body' tail."""
    body_file = tmp_path / "bad-pr-body.md"
    body_file.write_text(_BAD_BODY, encoding="utf-8")

    rc = mod.run_pr_body_gate(str(body_file), _FEATURE_REF)

    assert rc != 0
    out = capsys.readouterr().out
    # CI-identical lines (produced by adr049_evidence_check.main itself —
    # single source of truth, #396 invariant).
    assert "::error::ADR-049 evidence gate FAILED" in out
    assert (
        "::error::  - Section ## Verification has no checked box "
        "and no populated code block — looks like the template is unfilled"
    ) in out
    assert (
        "::error::See ADR-049 + .github/pull_request_template.md. "
        "Check at least one box per section, OR paste real "
        "command output into a code block. No override available."
    ) in out
    # The pre-push-hook-specific tail line (R1: "a one-line 'fix the
    # body, see ADR-049'").
    assert "::error::Push BLOCKED by the pre-push ADR-049 gate (#397)" in out
    assert "fix the body, see ADR-049" in out


def test_set_and_failing_body_on_main_still_blocks(mod, tmp_path, capsys) -> None:
    """Blocking behaviour does not depend on which branch is targeted —
    only whether PR_BODY was set and failed."""
    body_file = tmp_path / "bad-pr-body.md"
    body_file.write_text(_BAD_BODY, encoding="utf-8")

    rc = mod.run_pr_body_gate(str(body_file), _MAIN_REF)

    assert rc != 0


def test_set_mode_uses_injected_adr049_main(mod) -> None:
    """Dependency-injection seam: ``adr049_main`` can be swapped for a
    fake, proving the wiring without touching the filesystem at all."""
    calls: list[list[str]] = []

    def fake_adr049_main(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    rc = mod.run_pr_body_gate(
        "/some/path.md", _FEATURE_REF, adr049_main=fake_adr049_main
    )

    assert rc == 0
    assert calls == [["adr049_evidence_check.py", "/some/path.md"]]


def test_set_mode_injected_failure_prints_block_tail(mod, capsys) -> None:
    def fake_adr049_main(argv: list[str]) -> int:
        return 1

    rc = mod.run_pr_body_gate(
        "/some/path.md", _FEATURE_REF, adr049_main=fake_adr049_main
    )

    assert rc == 1
    assert "::error::Push BLOCKED" in capsys.readouterr().out


# --- _parse_remote_ref ---------------------------------------------------


def test_parse_remote_ref_present(mod) -> None:
    assert mod._parse_remote_ref(["prog", _FEATURE_REF]) == _FEATURE_REF


def test_parse_remote_ref_absent(mod) -> None:
    assert mod._parse_remote_ref(["prog"]) == ""


def test_parse_remote_ref_empty_string_arg(mod) -> None:
    assert mod._parse_remote_ref(["prog", ""]) == ""


# --- main() CLI -----------------------------------------------------------


def test_main_reads_pr_body_from_environment(mod, monkeypatch, tmp_path, capsys):
    body_file = tmp_path / "good.md"
    body_file.write_text(_GOOD_BODY, encoding="utf-8")
    monkeypatch.setenv("PR_BODY", str(body_file))

    rc = mod.main(["prepush_pr_body_gate.py", _FEATURE_REF])

    assert rc == 0
    assert "PASSED" in capsys.readouterr().out


def test_main_with_no_pr_body_env_and_feature_ref_reminds(
    mod, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("PR_BODY", raising=False)

    rc = mod.main(["prepush_pr_body_gate.py", _FEATURE_REF])

    assert rc == 0
    assert "Reminder" in capsys.readouterr().out


def test_main_with_pr_body_none_is_silent(mod, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PR_BODY", "none")

    rc = mod.main(["prepush_pr_body_gate.py", _FEATURE_REF])

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_main_defaults_argv_to_sys_argv(mod, monkeypatch, capsys) -> None:
    """``main(None)`` falls back to ``sys.argv`` — covers the branch the
    real ``if __name__ == '__main__'`` entry point exercises."""
    monkeypatch.delenv("PR_BODY", raising=False)
    monkeypatch.setattr("sys.argv", ["prepush_pr_body_gate.py", _FEATURE_REF])

    rc = mod.main()

    assert rc == 0
    assert "Reminder" in capsys.readouterr().out


# --- module hygiene -------------------------------------------------------


def test_module_has_scoped_logger(mod) -> None:
    """feedback_module_scoped_loggers — the module uses a
    ``__name__``-scoped logger."""
    assert mod.logger.name == "scripts.hooks.prepush_pr_body_gate"
