"""R2 equality guard: the ONLY pytest invocation over the whole `tests/`
directory, across EVERY ci.yml job and EVERY Makefile target, is in
`scripts/ci/run-tests.sh` (SPEC 2026-10-11-SPEC-test-parity-scripted-
enforcement.md R1/R2 + ADDENDUM A1-3).

ADDENDUM A1-3 (2026-10-11, review REJECT of f72cdd7, B3): the first cut
only scanned the `test` job's steps and the `test:` target's recipe, and
its fingerprint was the `--cov-fail-under` flag rather than "a pytest
invocation over `tests/`". Both were evadable exactly as B3 demonstrated:
a second CI step/job, or a second Makefile target, running bare
`pytest tests/ ...` with no `--cov-fail-under` flag (E1/E6/E7); masking the
real step with `|| true` / `continue-on-error` / `if:` (E4/E5/E8); or
hiding the real call behind `; echo` so grep-for-the-string-anywhere
checks pass without the script ever actually running.

This version:
  * scans every `run:` step in every ci.yml job, and every recipe line of
    every Makefile target (not just `test`/`test:`);
  * treats a bare `pytest tests/` (or `tests`) invocation ANYWHERE outside
    scripts/ci/run-tests.sh as a violation, unconditionally;
  * treats a SCOPED run (a specific file or node id) as a violation too,
    UNLESS it is listed in SCOPED_PYTEST_EXEMPTIONS below;
  * splits each run-text/recipe into shell STATEMENTS (on `;`, `&&`,
    `||`, `|`, newlines) before classifying, so `pytest tests/ -q; echo
    bash scripts/ci/run-tests.sh` is seen as two statements — the first a
    real, unexempted whole-directory invocation (violation), the second a
    mere `echo` that does NOT count as "the script was called";
  * requires a GENUINE invocation of the script (as the executed command,
    not merely a string appearing in an echoed/commented line) before a
    job/target is considered to "call the script";
  * rejects `continue-on-error`, an `if:` key, `|| true`, `|| :` and
    `; true` on the step/line that genuinely calls the script.

Reads the shipped files; types no constants describing their content
beyond the canonical script path, the exemption list (each entry is a
real, pre-existing, legitimate single-file test run), and the masking
tokens named above.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import yaml

from tests.test_ci_make_targets_resolve_tools import parse_makefile

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"
CI = ROOT / ".github/workflows/ci.yml"

#: the ONE script every pytest invocation over `tests/` must go through.
SCRIPT = "scripts/ci/run-tests.sh"

#: SCOPED (single-file or node-id) pytest invocations outside the script,
#: each pre-existing and legitimate — keyed by "<location>:<target name>"
#: -> the exact `tests/...` argument it runs. Anything NOT listed here,
#: and any BARE `tests/`/`tests` (whole-directory) invocation regardless
#: of whether it's listed, is a violation.
SCOPED_PYTEST_EXEMPTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("Makefile:test-rls-local", "tests/test_rls_isolation.py"),
        ("Makefile:openapi-export", "tests/test_openapi_drift.py"),
        ("Makefile:release", "tests/test_openapi_drift.py"),
        ("Makefile:release", "tests/test_version_drift.py"),
    }
)

#: statement separators (A1-3's own "; echo" evasion needs each statement
#: checked independently, not the whole line as one blob).
_STATEMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n")

#: exit-code/condition maskers forbidden on the step/line that calls the
#: script (B3 E4/E5/E8).
_MASKING_TOKENS = ("|| true", "|| :", "; true")


def _statements(text: str) -> list[str]:
    return [s.strip() for s in _STATEMENT_SPLIT.split(text) if s.strip()]


def _tokenize(statement: str) -> list[str]:
    try:
        return shlex.split(statement)
    except ValueError:
        return statement.split()


def _pytest_tests_targets(statement: str) -> list[str]:
    """`tests` / `tests/...` tokens in `statement`, but ONLY if the
    statement actually invokes something named/containing "pytest"
    (case-insensitively — catches both a literal `pytest` binary/module
    invocation AND an indirect one through a resolved shell variable like
    `"$$PYTEST"`)."""
    if not re.search(r"pytest", statement, re.IGNORECASE):
        return []
    tokens = _tokenize(statement)
    return [t for t in tokens if t == "tests" or t.startswith("tests/")]


def _is_whole_directory(target: str) -> bool:
    return target in ("tests", "tests/")


def _genuinely_calls_script(statement: str) -> bool:
    """True only if `statement`'s EXECUTED command is the script — never
    merely because the script's path string appears somewhere in the
    statement (an `echo` of it, a comment, part of another word)."""
    tokens = _tokenize(statement)
    if not tokens:
        return False
    tokens[0] = tokens[0].lstrip(
        "@-"
    )  # Makefile's @ (quiet) / - (ignore-error) prefixes
    if tokens[0] == "":
        tokens = tokens[1:]
    if not tokens:
        return False
    # Local wrappers (e.g. `make test`'s `scripts.neuter.runner hold-shared
    # -- bash scripts/ci/run-tests.sh`) may stay around the script call
    # (R1's own text says so) — the REAL command is whatever follows the
    # LAST bare `--` token, if there is one.
    if "--" in tokens:
        idx = len(tokens) - 1 - tokens[::-1].index("--")
        tokens = tokens[idx + 1 :]
        if not tokens:
            return False
    if tokens[0] in ("bash", "sh", ".", "source") and len(tokens) > 1:
        return tokens[1] == SCRIPT
    return tokens[0] == SCRIPT


def _masking_violations_in_text(text: str, *, where: str) -> list[str]:
    """Checked on the WHOLE, unsplit run-text/recipe (never on an already
    `;`/`&&`/`||`-split statement — splitting on `||` itself would cut a
    `|| true` suffix away from the statement that calls the script before
    this ever saw it). Flags any LINE that both calls the script and
    carries a masking token."""
    bad = []
    for line in text.splitlines():
        if SCRIPT not in line:
            continue
        for token in _MASKING_TOKENS:
            if token in line:
                bad.append(
                    f"{where}: the line calling {SCRIPT} masks its exit "
                    f"code with {token!r}: {line.strip()!r}"
                )
    return bad


def _scan_statements(
    statements: list[str], *, where: str, exemption_prefix: str
) -> tuple[list[str], bool]:
    bad: list[str] = []
    script_called = False
    for statement in statements:
        if _genuinely_calls_script(statement):
            script_called = True
        for target in _pytest_tests_targets(statement):
            if _is_whole_directory(target):
                bad.append(
                    f"{where}: whole-tests/-directory pytest invocation "
                    f"outside {SCRIPT}: {statement!r}"
                )
            elif (exemption_prefix, target) not in SCOPED_PYTEST_EXEMPTIONS:
                bad.append(
                    f"{where}: unexempted scoped pytest invocation "
                    f"{target!r} outside {SCRIPT}: {statement!r}"
                )
    return bad, script_called


def violations(ci_text: str, makefile_text: str) -> list[str]:
    bad: list[str] = []

    # ── every ci.yml job's every run: step ──
    data = yaml.safe_load(ci_text)
    for job_name, job in (data.get("jobs") or {}).items():
        for step_index, step in enumerate(job.get("steps") or []):
            run_text = step.get("run")
            if not run_text:
                continue
            statements = _statements(run_text)
            step_bad, step_called = _scan_statements(
                statements,
                where=f"ci.yml job {job_name!r} step {step_index}",
                exemption_prefix=f"ci.yml:{job_name}",
            )
            bad.extend(step_bad)
            if step_called:
                bad.extend(
                    _masking_violations_in_text(
                        run_text, where=f"ci.yml job {job_name!r} step {step_index}"
                    )
                )
                if step.get("continue-on-error"):
                    bad.append(
                        f"ci.yml job {job_name!r} step {step_index}: the "
                        f"step calling {SCRIPT} carries continue-on-error"
                    )
                if "if" in step:
                    bad.append(
                        f"ci.yml job {job_name!r} step {step_index}: the "
                        f"step calling {SCRIPT} carries an `if:` condition"
                    )

    # ── every Makefile target's every recipe line ──
    all_targets = parse_makefile(makefile_text)
    for target_name, (_deps, recipe) in all_targets.items():
        statements = _statements(recipe)
        target_bad, target_called = _scan_statements(
            statements,
            where=f"Makefile target {target_name!r}",
            exemption_prefix=f"Makefile:{target_name}",
        )
        if target_called:
            target_bad = target_bad + _masking_violations_in_text(
                recipe, where=f"Makefile target {target_name!r}"
            )
        bad.extend(target_bad)

    # ── R1: `make test` and the ci.yml `test` job must EACH genuinely
    # call the script at least once (the positive half R2 always had). ──
    ci_test_job = (data.get("jobs") or {}).get("test") or {}
    ci_test_calls = any(
        _genuinely_calls_script(s)
        for step in (ci_test_job.get("steps") or [])
        for s in _statements(step.get("run") or "")
    )
    if not ci_test_calls:
        bad.append(f"ci.yml `test` job never genuinely calls {SCRIPT}")
    mk_test_recipe = (all_targets.get("test") or ((), ""))[1]
    mk_test_calls = any(_genuinely_calls_script(s) for s in _statements(mk_test_recipe))
    if not mk_test_calls:
        bad.append(f"Makefile `test` target never genuinely calls {SCRIPT}")

    return bad


def test_make_test_and_ci_both_call_the_canonical_script_with_no_drift() -> None:
    assert violations(CI.read_text(), MAKEFILE.read_text()) == []


def test_the_canonical_script_itself_exists_and_is_executable() -> None:
    script = ROOT / SCRIPT
    assert script.is_file(), f"{SCRIPT} is missing"
    assert script.stat().st_mode & 0o111, f"{SCRIPT} is not executable"


def test_exemption_list_entries_are_all_genuinely_present() -> None:
    """The exemption list must describe REALITY, never aspiration — every
    entry must correspond to an actual, still-present scoped pytest
    invocation in the shipped Makefile (a stale entry nobody needs
    anymore should be deleted, which this also catches)."""
    makefile_text = MAKEFILE.read_text()
    all_targets = parse_makefile(makefile_text)
    seen: set[tuple[str, str]] = set()
    for target_name, (_deps, recipe) in all_targets.items():
        for statement in _statements(recipe):
            for target in _pytest_tests_targets(statement):
                if not _is_whole_directory(target):
                    seen.add((f"Makefile:{target_name}", target))
    assert SCOPED_PYTEST_EXEMPTIONS <= seen, SCOPED_PYTEST_EXEMPTIONS - seen


# ── neuters: each independently proves the guard fires ──────────────────────


def test_neuter_e1_second_ci_step_bare_pytest_goes_red() -> None:
    """B3 E1: a second ci.yml step in the `test` job, `run: pytest tests/
    -q`, alongside the real script call."""
    ci_text = CI.read_text()
    mutated = ci_text.replace(
        f"run: bash {SCRIPT}",
        f"run: bash {SCRIPT}\n      - name: evasion E1\n        run: pytest tests/ -q",
    )
    assert mutated != ci_text
    found = violations(mutated, MAKEFILE.read_text())
    assert any("whole-tests/-directory" in v and "job 'test'" in v for v in found), (
        found
    )


def test_neuter_e4_masked_with_or_true_goes_red() -> None:
    ci_text = CI.read_text()
    mutated = ci_text.replace(f"run: bash {SCRIPT}", f"run: bash {SCRIPT} || true")
    assert mutated != ci_text
    found = violations(mutated, MAKEFILE.read_text())
    assert any("masks its exit code with '|| true'" in v for v in found), found


def test_neuter_e4b_masked_with_or_colon_goes_red() -> None:
    # Structural mutation (not a raw string .replace()): appending a bare
    # trailing `:` to a plain YAML scalar via text-splicing produces
    # invalid YAML ("mapping values are not allowed here") — mutate the
    # PARSED structure instead, exactly like the e5/e8 neuters below.
    ci_text = CI.read_text()
    data = yaml.safe_load(ci_text)
    for step in data["jobs"]["test"]["steps"]:
        if step.get("run") == f"bash {SCRIPT}":
            step["run"] = f"bash {SCRIPT} || :"
            break
    else:
        raise AssertionError("could not find the script-calling step")
    mutated = yaml.safe_dump(data, sort_keys=False)
    found = violations(mutated, MAKEFILE.read_text())
    assert any("masks its exit code with '|| :'" in v for v in found), found


def test_neuter_e5_continue_on_error_goes_red() -> None:
    ci_text = CI.read_text()
    data = yaml.safe_load(ci_text)
    for step in data["jobs"]["test"]["steps"]:
        if step.get("run") == f"bash {SCRIPT}":
            step["continue-on-error"] = True
            break
    else:
        raise AssertionError("could not find the script-calling step")
    mutated = yaml.safe_dump(data, sort_keys=False)
    found = violations(mutated, MAKEFILE.read_text())
    assert any("continue-on-error" in v for v in found), found


def test_neuter_e8_if_condition_goes_red() -> None:
    ci_text = CI.read_text()
    data = yaml.safe_load(ci_text)
    for step in data["jobs"]["test"]["steps"]:
        if step.get("run") == f"bash {SCRIPT}":
            step["if"] = "false"
            break
    else:
        raise AssertionError("could not find the script-calling step")
    mutated = yaml.safe_dump(data, sort_keys=False)
    found = violations(mutated, MAKEFILE.read_text())
    assert any("`if:` condition" in v for v in found), found


def test_neuter_e6_new_ci_job_bare_pytest_goes_red() -> None:
    """B3 E6: a brand-new ci.yml job running `pytest tests/
    --cov-fail-under=90` — must be caught even though it is not the
    `test` job and carries no fingerprint this guard relies on beyond
    "runs pytest over tests/"."""
    ci_text = CI.read_text()
    data = yaml.safe_load(ci_text)
    data["jobs"]["evasion-e6"] = {
        "runs-on": "ubuntu-latest",
        "steps": [{"run": "pytest tests/ --cov-fail-under=90"}],
    }
    mutated = yaml.safe_dump(data, sort_keys=False)
    found = violations(mutated, MAKEFILE.read_text())
    assert any(
        "job 'evasion-e6'" in v and "whole-tests/-directory" in v for v in found
    ), found


def test_neuter_e7_new_makefile_target_bare_pytest_goes_red() -> None:
    """B3 E7: a brand-new Makefile target `test-fast` running `pytest
    tests/ --cov-fail-under=90`."""
    mk_text = MAKEFILE.read_text()
    mutated = mk_text + "\ntest-fast:\n\t@.venv/bin/pytest tests/ --cov-fail-under=90\n"
    found = violations(CI.read_text(), mutated)
    assert any(
        "target 'test-fast'" in v and "whole-tests/-directory" in v for v in found
    ), found


def test_neuter_e_semicolon_echo_decoy_goes_red() -> None:
    """The reviewer's own ``pytest tests/ -q; echo bash
    scripts/ci/run-tests.sh`` decoy: the ECHO must not count as "the
    script was called", and the real `pytest tests/` statement must still
    be flagged as a whole-directory violation."""
    ci_text = CI.read_text()
    mutated = ci_text.replace(
        f"run: bash {SCRIPT}",
        f"run: pytest tests/ -q; echo bash {SCRIPT}",
    )
    assert mutated != ci_text
    found = violations(mutated, MAKEFILE.read_text())
    assert any("whole-tests/-directory" in v and "job 'test'" in v for v in found), (
        found
    )
    # AND the positive "test job calls the script" check must now fail,
    # since echoing the path is not calling it.
    assert any("never genuinely calls" in v and "ci.yml" in v for v in found), found


def test_neuter_dropping_the_script_call_from_ci_goes_red() -> None:
    ci_text = CI.read_text()
    mutated = ci_text.replace(f"run: bash {SCRIPT}", "run: echo noop")
    assert mutated != ci_text
    found = violations(mutated, MAKEFILE.read_text())
    assert any("ci.yml `test` job never genuinely calls" in v for v in found), found
    assert not any(v.startswith("Makefile") for v in found), found


def test_neuter_dropping_the_script_call_from_makefile_goes_red() -> None:
    mk_text = MAKEFILE.read_text()
    mutated = mk_text.replace(f"bash {SCRIPT}", "/bin/true", 1)
    assert mutated != mk_text
    found = violations(CI.read_text(), mutated)
    assert any("Makefile `test` target never genuinely calls" in v for v in found), (
        found
    )


def test_neuter_test_unit_as_raw_bare_pytest_goes_red() -> None:
    """A1-3: `test-unit` must never be a raw, unexempted bare pytest
    invocation — if it ever reverts to one (exactly what it was before
    ADDENDUM A1), this must catch it."""
    mk_text = MAKEFILE.read_text()
    assert "test-unit:" in mk_text
    mutated = re.sub(
        r"test-unit:[^\n]*\n(?:\t[^\n]*\n)*",
        'test-unit: ## reverted\n\t@.venv/bin/pytest tests/ -v -k "not integration"\n',
        mk_text,
        count=1,
    )
    assert mutated != mk_text
    found = violations(CI.read_text(), mutated)
    assert any(
        "target 'test-unit'" in v and "whole-tests/-directory" in v for v in found
    ), found
