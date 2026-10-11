"""R2 equality guard: `make test` and the ci.yml `test` job must both call
THE canonical pytest-invocation script, ``scripts/ci/run-tests.sh``, and
neither may still hand-type its own main-suite ``pytest tests/ ...
--cov-fail-under=...`` line (SPEC 2026-10-11-SPEC-test-parity-scripted-
enforcement.md, R1/R2).

Origin: PR #380 / CI run 37878027812. ``Makefile:91`` and
``ci.yml:131-145`` had already drifted — the Makefile covered ``bff``,
``scripts/network``, ``scripts/neuter`` and ``scripts.integration_gate``;
CI did not — and "mirror every CI gate locally" was a convention, not a
mechanism. Two unrelated tests also passed locally and failed in CI that
same run (a hermeticity leak, closed separately by the R3 guard in
``tests/conftest.py``); the drift made that divergence harder to notice.

Reads the shipped files; types no constants describing their content
beyond the one canonical script path and the fingerprint of the gate
invocation it replaces.

Neuters (each independently proves the guard fires):
(a) reintroduce an inline ``pytest tests/ ... --cov-fail-under=90`` line
    into a copy of ci.yml's ``test`` job -> RED;
(b) same, into a copy of the Makefile's ``test`` target recipe -> RED;
(c) strip the script-call reference from either file (independently) ->
    RED, naming that file.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"
CI = ROOT / ".github/workflows/ci.yml"

#: the ONE script both `make test` and the ci.yml `test` job must call.
SCRIPT = "scripts/ci/run-tests.sh"

#: fingerprint of "the main-suite gate invocation" — only
#: ``scripts/ci/run-tests.sh`` itself may contain this flag; its presence
#: anywhere in ci.yml or the Makefile means a second, hand-typed pytest
#: gate invocation has crept back in (R1).
_MAIN_SUITE_FINGERPRINT = re.compile(r"--cov-fail-under")


def ci_test_job_run_text(ci_text: str) -> str:
    """Every ``run:`` step's text in ci.yml's ``test`` job, concatenated."""
    data = yaml.safe_load(ci_text)
    steps = data["jobs"]["test"]["steps"]
    return "\n".join(s.get("run") or "" for s in steps)


def make_test_target_recipe(makefile_text: str) -> str:
    """The ``test:`` target's tab-indented recipe lines (never matches
    ``test-cov:``, ``test-unit:`` etc. — the ``(?!-)`` lookahead requires
    the character right after ``test`` to be the colon itself)."""
    lines = makefile_text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if re.match(r"^test:(?!-)", lines[i]):
            i += 1
            while i < len(lines) and (
                lines[i].startswith("\t") or not lines[i].strip()
            ):
                out.append(lines[i])
                i += 1
            break
        i += 1
    return "\n".join(out)


def violations(ci_text: str, makefile_text: str) -> list[str]:
    """Only inspects the `test` job's own ``run:`` steps (ci.yml) and the
    `test:` target's own recipe (Makefile) — never the surrounding prose —
    so a comment that happens to mention ``--cov-fail-under`` (e.g. this
    very module's docstring, or the step's own explanatory comment) can
    never produce a false positive."""
    bad: list[str] = []
    ci_test = ci_test_job_run_text(ci_text)
    mk_test = make_test_target_recipe(makefile_text)

    if SCRIPT not in ci_test:
        bad.append(f"ci.yml `test` job does not call {SCRIPT}")
    if SCRIPT not in mk_test:
        bad.append(f"Makefile `test` target does not call {SCRIPT}")

    for name, text in (("ci.yml", ci_test), ("Makefile", mk_test)):
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _MAIN_SUITE_FINGERPRINT.search(line):
                bad.append(
                    f"{name}:{lineno}: hand-typed main-suite pytest gate "
                    f"invocation ({line.strip()!r}) found outside {SCRIPT}"
                )
    return bad


def test_make_test_and_ci_both_call_the_canonical_script_with_no_drift() -> None:
    assert violations(CI.read_text(), MAKEFILE.read_text()) == []


def test_the_canonical_script_itself_exists_and_is_executable() -> None:
    script = ROOT / SCRIPT
    assert script.is_file(), f"{SCRIPT} is missing"
    assert script.stat().st_mode & 0o111, f"{SCRIPT} is not executable"


# ── neuters: each independently proves the guard is live ────────────────────


def test_neuter_a_reintroduced_inline_pytest_line_in_ci_goes_red() -> None:
    ci_text = CI.read_text()
    mutated = ci_text.replace(
        f"run: bash {SCRIPT}",
        "run: pytest tests/ -v --cov=src --cov-fail-under=90 --junit-xml=junit.xml",
    )
    assert mutated != ci_text, "fixture did not change ci.yml text"
    found = violations(mutated, MAKEFILE.read_text())
    assert any("ci.yml" in v and "--cov-fail-under" in v for v in found), found


def test_neuter_b_reintroduced_inline_pytest_line_in_makefile_goes_red() -> None:
    mk_text = MAKEFILE.read_text()
    mutated = mk_text.replace(
        f"bash {SCRIPT}",
        ".venv/bin/pytest tests/ -v --cov=src --cov-fail-under=90 --junit-xml=junit.xml",
    )
    assert mutated != mk_text, "fixture did not change Makefile text"
    found = violations(CI.read_text(), mutated)
    assert any("Makefile" in v and "--cov-fail-under" in v for v in found), found


def test_neuter_c_dropping_the_script_call_from_ci_goes_red() -> None:
    ci_text = CI.read_text()
    # "echo noop", not "true" -- an unquoted `run: true` parses as YAML
    # boolean True, not the string "true", and breaks yaml.safe_load's
    # `str`-only assumption downstream.
    mutated = ci_text.replace(f"bash {SCRIPT}", "echo noop")
    assert mutated != ci_text, "fixture did not change ci.yml text"
    found = violations(mutated, MAKEFILE.read_text())
    assert any(f"ci.yml `test` job does not call {SCRIPT}" == v for v in found), found
    # Asymmetric: the Makefile side must stay clean when only ci.yml breaks.
    assert not any(v.startswith("Makefile") for v in found), found


def test_neuter_c_dropping_the_script_call_from_makefile_goes_red() -> None:
    mk_text = MAKEFILE.read_text()
    mutated = mk_text.replace(SCRIPT, "/bin/true")
    assert mutated != mk_text, "fixture did not change Makefile text"
    found = violations(CI.read_text(), mutated)
    assert any(f"Makefile `test` target does not call {SCRIPT}" == v for v in found), (
        found
    )
    # Asymmetric: the ci.yml side must stay clean when only the Makefile breaks.
    assert not any(v.startswith("ci.yml") for v in found), found
