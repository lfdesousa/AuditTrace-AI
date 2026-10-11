"""R2 equality guard: the ONLY pytest invocation over the whole `tests/`
directory, across EVERY workflow job and EVERY Makefile target, is in
`scripts/ci/run-tests.sh` (SPEC 2026-10-11-SPEC-test-parity-scripted-
enforcement.md R1/R2, redesigned by ADDENDUM A2/A3/A4/A7 after an
independent review REJECTed the first two rebuilds for being argv/wrapper
PATTERN-MATCHING heuristics with new bypasses each round).

This round changes the instrument for the WIRING checks too: instead of
pattern-matching every job/target to decide "did this genuinely call the
script, masked how", the things that matter are pinned by EXACT GOLDENS
— the whole ci.yml `test` job mapping (so adding `continue-on-error`,
`if:`, or any other key is a diff against a reviewed golden, not
something a scanner has to notice), the workflow-level `on`/`env`/
`defaults`/`permissions` keys, the Makefile `test`/`test-unit` recipe
lines, and the `[tool.pytest.ini_options]` table. A SEPARATE, explicitly
heuristic scan then looks for any OTHER job/target anywhere that
mentions `pytest`/`py.test` as a word — it can miss calls (accepted),
and anything it does find must be in an exact exemption list.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

from tests.test_ci_make_targets_resolve_tools import parse_makefile

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"
CI = ROOT / ".github/workflows/ci.yml"
WORKFLOWS_DIR = ROOT / ".github/workflows"
PYPROJECT = ROOT / "pyproject.toml"

#: the ONE script every pytest invocation over `tests/` must go through.
SCRIPT = "scripts/ci/run-tests.sh"

# ─────────────────────────── exact goldens (R2, A2-6/A2-7/A3-4/A3-10) ───────

#: the ENTIRE ci.yml `test` job mapping — every job-level key, every key
#: of every step. Any drift (a new step, a reordered one, a masking key
#: like `continue-on-error`/`if` added to the script-calling step, a
#: changed `run:`) is a reviewed golden diff, not something a scanner has
#: to infer.
CI_TEST_JOB_GOLDEN: dict = {
    "runs-on": "ubuntu-latest",
    "services": {
        "postgres": {
            "image": "postgres:16",
            "env": {
                "POSTGRES_USER": "postgres",
                "POSTGRES_PASSWORD": "postgres",
                "POSTGRES_DB": "audittrace",
            },
            "ports": ["5432:5432"],
            "options": (
                '--health-cmd "pg_isready -U postgres" --health-interval 10s '
                "--health-timeout 5s --health-retries 5"
            ),
        }
    },
    "env": {
        "AUDITTRACE_TEST_POSTGRES_URL": (
            "postgresql+psycopg2://postgres:postgres@localhost:5432/audittrace"
        )
    },
    "steps": [
        {"uses": "actions/checkout@v6", "with": {"fetch-depth": 0}},
        {
            "name": "Set up Python 3.12",
            "uses": "actions/setup-python@v6",
            "with": {"python-version": "3.12"},
        },
        {
            "name": "Cache pip dependencies",
            "uses": "actions/cache@v5",
            "with": {
                "path": "~/.cache/pip",
                "key": "${{ runner.os }}-pip-${{ hashFiles('pyproject.toml') }}",
                "restore-keys": "${{ runner.os }}-pip-\n",
            },
        },
        {
            "name": "Install dependencies",
            "run": 'python -m pip install --upgrade pip\npip install -e ".[dev]"\n',
        },
        {
            "name": (
                "Install CI-Agent gate tooling (semgrep — ISOLATED gatekeeper, "
                "never in the app/test venv)"
            ),
            "run": (
                'pipx install "semgrep==1.171.0"\n'
                'echo "$HOME/.local/bin" >> "$GITHUB_PATH"\n'
            ),
        },
        {"name": "Lint", "run": "ruff check src/ tests/"},
        {"name": "Format check", "run": "ruff format --check src/ tests/"},
        {
            "name": "Type check (make typecheck — same target as local;",
            "run": "make typecheck",
        },
        {
            "name": (
                "Security-lint (offline semgrep gate — same make target as "
                "local + pre-commit;"
            ),
            "run": "make security-lint",
        },
        {
            "name": "Assert RLS integration tests are actually runnable",
            "run": (
                "python - <<'PY'\n"
                "import socket\n"
                'with socket.create_connection(("localhost", 5432), timeout=10):\n'
                '    print("postgres reachable on :5432 — RLS tests will run")\n'
                "PY\n"
            ),
        },
        {
            "name": (
                "Tests + coverage + skip + per-file gate (scripts/ci/run-tests.sh "
                "— same script `make test` calls; SPEC "
                "2026-10-11-test-parity-scripted-enforcement + ADDENDUM A1)"
            ),
            "run": f"bash {SCRIPT}",
        },
        {
            "name": "Upload coverage XML",
            "if": "always()",
            "uses": "actions/upload-artifact@v7",
            "with": {
                "name": "coverage-xml",
                "path": "coverage.xml",
                "retention-days": 30,
            },
        },
        {
            "name": "Upload coverage HTML report",
            "if": "always()",
            "uses": "actions/upload-artifact@v7",
            "with": {
                "name": "coverage-html",
                "path": "htmlcov/",
                "retention-days": 30,
            },
        },
    ],
}

#: workflow-level keys. A4-9: PyYAML loads the bare `on:` key as the
#: BOOLEAN `True` — read it under that key (never the string `"on"`,
#: which is absent from the parsed mapping) and assert it's non-empty.
CI_WORKFLOW_ON_GOLDEN: dict = {
    "push": {"branches": ["main", "feat/**", "fix/**"], "tags": ["v*"]},
    "pull_request": {"branches": ["main", "feat/**"]},
}
CI_WORKFLOW_ENV_GOLDEN: dict | None = None
CI_WORKFLOW_DEFAULTS_GOLDEN: dict | None = None
CI_WORKFLOW_PERMISSIONS_GOLDEN: dict = {"contents": "read"}

#: the Makefile `test`/`test-unit` recipes, WITH their line prefixes
#: (`@`/`-@`/tabs) — exact text, not reconstructed from parts.
MAKEFILE_TEST_RECIPE_GOLDEN = (
    f"\t@.venv/bin/python -m scripts.neuter.runner hold-shared -- bash {SCRIPT}\n"
)
MAKEFILE_TEST_UNIT_RECIPE_GOLDEN = f'\t@bash {SCRIPT} --subset "not integration"\n'

#: pyproject.toml's `[tool.pytest.ini_options]` table, verbatim.
PYPROJECT_PYTEST_INI_OPTIONS_GOLDEN: dict = {
    "testpaths": ["tests"],
    "python_files": ["test_*.py"],
    "python_functions": ["test_*"],
    "asyncio_mode": "auto",
    "addopts": "-v --cov=src --cov-report=term --cov-report=xml --cov-fail-under=90",
    "filterwarnings": ["ignore::DeprecationWarning"],
    "markers": [
        "allow_cluster_tool(tool, reason): opt-in half of the hermetic "
        "cluster-tool guard (tests/conftest.py, SPEC "
        "2026-10-11-SPEC-test-parity-scripted-enforcement.md R3) — must be "
        "paired with a matching entry in tests/hermetic_allowlist.py or the "
        "guard still fails the test."
    ],
}


def _load_ci() -> dict:
    return yaml.safe_load(CI.read_text())


def test_ci_test_job_matches_exact_golden():
    data = _load_ci()
    assert data["jobs"]["test"] == CI_TEST_JOB_GOLDEN


def test_ci_workflow_level_keys_match_exact_golden():
    data = _load_ci()
    assert data.get(True) == CI_WORKFLOW_ON_GOLDEN, "the `on:` block (PyYAML key True)"
    assert data.get(True), "the `on:` block must be non-empty"
    assert data.get("env") == CI_WORKFLOW_ENV_GOLDEN
    assert data.get("defaults") == CI_WORKFLOW_DEFAULTS_GOLDEN
    assert data.get("permissions") == CI_WORKFLOW_PERMISSIONS_GOLDEN


def test_makefile_test_and_test_unit_recipes_match_exact_golden():
    targets = parse_makefile(MAKEFILE.read_text())
    assert targets["test"][1] == MAKEFILE_TEST_RECIPE_GOLDEN
    assert targets["test-unit"][1] == MAKEFILE_TEST_UNIT_RECIPE_GOLDEN


def test_pyproject_pytest_ini_options_match_exact_golden():
    data = tomllib.loads(PYPROJECT.read_text())
    assert data["tool"]["pytest"]["ini_options"] == PYPROJECT_PYTEST_INI_OPTIONS_GOLDEN


def test_the_canonical_script_itself_exists_and_is_executable():
    script = ROOT / SCRIPT
    assert script.is_file(), f"{SCRIPT} is missing"
    assert script.stat().st_mode & 0o111, f"{SCRIPT} is not executable"


# ── neuters: each independently proves a golden fires ───────────────────────


def test_neuter_masking_the_script_step_breaks_the_ci_golden():
    data = _load_ci()
    for step in data["jobs"]["test"]["steps"]:
        if step.get("run") == f"bash {SCRIPT}":
            step["continue-on-error"] = True
            break
    else:
        raise AssertionError("could not find the script-calling step")
    assert data["jobs"]["test"] != CI_TEST_JOB_GOLDEN


def test_neuter_if_condition_on_script_step_breaks_the_ci_golden():
    data = _load_ci()
    for step in data["jobs"]["test"]["steps"]:
        if step.get("run") == f"bash {SCRIPT}":
            step["if"] = "false"
            break
    else:
        raise AssertionError("could not find the script-calling step")
    assert data["jobs"]["test"] != CI_TEST_JOB_GOLDEN


def test_neuter_or_true_on_script_step_breaks_the_ci_golden():
    data = _load_ci()
    for step in data["jobs"]["test"]["steps"]:
        if step.get("run") == f"bash {SCRIPT}":
            step["run"] = f"bash {SCRIPT} || true"
            break
    else:
        raise AssertionError("could not find the script-calling step")
    assert data["jobs"]["test"] != CI_TEST_JOB_GOLDEN


def test_neuter_dropped_on_trigger_breaks_the_workflow_golden():
    data = _load_ci()
    data[True] = {}
    assert not (data.get(True) == CI_WORKFLOW_ON_GOLDEN and data.get(True))


def test_neuter_makefile_test_target_reverted_to_bare_pytest_breaks_golden():
    mk_text = MAKEFILE.read_text()
    mutated = re.sub(
        r"test:[^\n]*\n(?:\t[^\n]*\n)*",
        "test: ## reverted\n\t@.venv/bin/pytest tests/ -v --cov=src --cov-fail-under=90\n",
        mk_text,
        count=1,
    )
    assert mutated != mk_text
    targets = parse_makefile(mutated)
    assert targets["test"][1] != MAKEFILE_TEST_RECIPE_GOLDEN


def test_neuter_pyproject_addopts_drift_breaks_golden():
    data = dict(PYPROJECT_PYTEST_INI_OPTIONS_GOLDEN)
    data["addopts"] = data["addopts"] + " -x"
    assert data != PYPROJECT_PYTEST_INI_OPTIONS_GOLDEN


# ─────────────────── heuristic cross-job/cross-workflow/all-target scan ─────
#
# A2-6/A3-4/A3-11: ANY `run:` step in ANY workflow under .github/workflows/,
# and ANY Makefile recipe (continuation-joined first), whose words contain
# `pytest` or `py.test` (case-insensitive — so an indirection like
# `"$$PYTEST"` is also caught) must be in an EXACT exemption list keyed by
# (workflow file, job, step index, exact run string) or (target, exact
# recipe text). This is explicitly a HEURISTIC: it can miss calls that
# never spell the word at all (e.g. a doubly-indirected variable built
# from characters), and that is ACCEPTED — the golden checks above, plus
# the process-layer shim in scripts/ci/run-tests.sh, are the controls.

_PYTEST_WORD_RE = re.compile(r"\bpytest\b|\bpy\.test\b", re.IGNORECASE)


def _join_backslash_continuations(text: str) -> str:
    return re.sub(r"\\\n", " ", text)


#: (workflow filename, job name, step index) -> the EXACT `run:` string of
#: that one, already-vetted step.
WORKFLOW_PYTEST_EXEMPTIONS: frozenset[tuple[str, str, int, str]] = frozenset(
    {
        (
            "test-count-badge.yml",
            "badge",
            3,
            "set -euo pipefail\n"
            'output="$(pytest tests --collect-only -q -p no:cacheprovider -o addopts= 2>&1)"\n'
            'echo "${output}"\n'
            'count="$(grep -oP \'\\d+(?= tests? collected)\' <<<"${output}" | tail -n1)"\n'
            'if [ -z "${count}" ]; then\n'
            "  echo \"::error::could not parse '(\\\\d+) tests? collected' from --collect-only output\"\n"
            "  exit 1\n"
            "fi\n"
            'echo "Parsed live test count: ${count}"\n'
            'echo "count=${count}" >> "$GITHUB_OUTPUT"\n',
        ),
    }
)

#: target name -> the EXACT (pre-join) recipe text of that one, already-
#: vetted target.
MAKEFILE_PYTEST_EXEMPTIONS: dict[str, str] = {
    "test-rls-local": (
        "\t# Runs tests/test_rls_isolation.py against a throwaway postgres:16\n"
        "\t# container on localhost:15432. CRITICALLY: does NOT port-forward the\n"
        "\t# k3s production Postgres — the test file includes a positive test\n"
        "\t# (test_alice_cannot_insert_as_bob) that deliberately provokes an RLS\n"
        "\t# violation to prove WITH CHECK works. That violation gets logged as\n"
        "\t# an ERROR by Postgres unconditionally, which would pollute production\n"
        "\t# logs. An ephemeral container keeps the noise scoped.\n"
        '\t@echo "🐳 Spinning up ephemeral test Postgres on :15432 ..."\n'
        "\t@docker rm -f audittrace-test-pg >/dev/null 2>&1 || true\n"
        "\t@docker run -d --rm --name audittrace-test-pg \\\n"
        "\t  -e POSTGRES_USER=postgres \\\n"
        "\t  -e POSTGRES_PASSWORD=test \\\n"
        "\t  -e POSTGRES_DB=audittrace \\\n"
        "\t  -p 15432:5432 \\\n"
        "\t  postgres:16 -c fsync=off -c synchronous_commit=off -c full_page_writes=off >/dev/null\n"
        '\t@echo "⏳ Waiting for Postgres to accept connections ..."\n'
        "\t@for i in 1 2 3 4 5 6 7 8; do \\\n"
        "\t  docker exec audittrace-test-pg pg_isready -U postgres >/dev/null 2>&1 && break ; \\\n"
        "\t  sleep 1 ; \\\n"
        "\tdone\n"
        '\t@AUDITTRACE_TEST_POSTGRES_URL="postgresql+psycopg2://postgres:test@localhost:15432/audittrace" \\\n'
        "\t  .venv/bin/pytest tests/test_rls_isolation.py -v --no-cov ; \\\n"
        "\t  status=$$? ; \\\n"
        '\t  echo "🧹 Tearing down ephemeral test Postgres ..." ; \\\n'
        "\t  docker rm -f audittrace-test-pg >/dev/null 2>&1 ; \\\n"
        "\t  [ $$status -eq 0 ] || exit $$status\n"
        '\t@echo "✅ RLS integration tests passed (ephemeral Postgres, no production pollution)"\n'
    ),
    "openapi-export": (
        "\t@OPENAPI_SNAPSHOT_UPDATE=1 .venv/bin/pytest tests/test_openapi_drift.py -q --no-cov\n"
        '\t@echo "✅ Regenerated:"\n'
        '\t@echo "   tests/fixtures/openapi.snapshot.yaml"\n'
        '\t@echo "   docs/reference/audittrace/openapi.yaml"\n'
        '\t@echo "Commit both alongside the API change so reviewers see the diff."\n'
    ),
    "release": (
        '\t@if [ -z "$(VERSION)" ]; then \\\n'
        '\t\techo "❌ usage: make release VERSION=1.0.14"; \\\n'
        '\t\techo "Bumps the two single-source-of-truth pin sites + regenerates"; \\\n'
        '\t\techo "the OpenAPI snapshot. Stops short of committing or tagging —"; \\\n'
        '\t\techo "review the diff, then commit + tag manually."; \\\n'
        "\t\texit 1; \\\n"
        "\tfi\n"
        '\t@echo "🔖 bumping pyproject.toml::version → $(VERSION)"\n'
        '\t@sed -i \'s/^version = ".*"/version = "$(VERSION)"/\' pyproject.toml\n'
        '\t@echo "🔖 bumping charts/audittrace/Chart.yaml::version + appVersion → $(VERSION)"\n'
        "\t@sed -i 's/^version: .*/version: $(VERSION)/' charts/audittrace/Chart.yaml\n"
        '\t@sed -i \'s/^appVersion: ".*"/appVersion: "$(VERSION)"/\' charts/audittrace/Chart.yaml\n'
        '\t@echo "🔖 bumping docker-compose.yml demo default → $(VERSION)"\n'
        "\t@sed -i 's/$${AUDITTRACE_IMAGE_TAG:-[^}]*}/$${AUDITTRACE_IMAGE_TAG:-$(VERSION)}/g' docker-compose.yml\n"
        '\t@echo "🔖 bumping .env.ci + .env.dev-real-llm.example AUDITTRACE_IMAGE_TAG → $(VERSION)"\n'
        "\t@sed -i 's/^AUDITTRACE_IMAGE_TAG=.*/AUDITTRACE_IMAGE_TAG=$(VERSION)/' .env.ci .env.dev-real-llm.example\n"
        '\t@PYTEST="$$( [ -x .venv/bin/pytest ] && echo .venv/bin/pytest || command -v pytest )"; \\\n'
        '\techo "📝 regenerating OpenAPI snapshot (defensive — no version string embedded today, so this is normally a no-op diff) ..."; \\\n'
        '\tOPENAPI_SNAPSHOT_UPDATE=1 "$$PYTEST" tests/test_openapi_drift.py -q --no-cov >/dev/null; \\\n'
        '\techo "🚦 running drift gate ..."; \\\n'
        '\t"$$PYTEST" tests/test_version_drift.py -q --no-cov\n'
        "\t@echo\n"
        '\t@echo "✅ release-prep done for v$(VERSION). Diff:"\n'
        "\t@git diff --stat $$($(MAKE) --no-print-directory release-bump-files)\n"
        "\t@echo\n"
        '\t@echo "Next steps:"\n'
        '\t@echo "  1. git add $$($(MAKE) --no-print-directory release-bump-files)"\n'
        "\t@echo \"  2. git commit -m 'chore(release): v$(VERSION)'\"\n"
        '\t@echo "  3. open release PR; after merge, tag v$(VERSION) on main"\n'
        '\t@echo "  4. docker build/push localhost:5000/audittrace/memory-server:v$(VERSION)"\n'
        '\t@echo "  5. helm upgrade --reset-then-reuse-values --set memoryServer.image.tag=v$(VERSION)"\n'
    ),
}


def workflow_pytest_violations(workflows_dir: Path) -> list[str]:
    """HEURISTIC (see module docstring). Returns one entry per unexempted
    `run:` step, across every workflow file, whose text mentions
    `pytest`/`py.test` as a word."""
    bad: list[str] = []
    for wf_path in sorted(workflows_dir.glob("*.yml")):
        data = yaml.safe_load(wf_path.read_text())
        for job_name, job in (data.get("jobs") or {}).items():
            for idx, step in enumerate(job.get("steps") or []):
                run_text = step.get("run")
                if not run_text:
                    continue
                joined = _join_backslash_continuations(run_text)
                if not _PYTEST_WORD_RE.search(joined):
                    continue
                key = (wf_path.name, job_name, idx, run_text)
                if key not in WORKFLOW_PYTEST_EXEMPTIONS:
                    bad.append(
                        f"{wf_path.name} job {job_name!r} step {idx} mentions "
                        f"pytest/py.test and is not exempted: {run_text!r}"
                    )
    return bad


def makefile_pytest_violations(makefile_text: str) -> list[str]:
    """HEURISTIC (see module docstring). Returns one entry per unexempted
    Makefile target whose continuation-joined recipe mentions
    `pytest`/`py.test` as a word."""
    bad: list[str] = []
    for target_name, (_deps, recipe) in parse_makefile(makefile_text).items():
        joined = _join_backslash_continuations(recipe)
        if not _PYTEST_WORD_RE.search(joined):
            continue
        if MAKEFILE_PYTEST_EXEMPTIONS.get(target_name) != recipe:
            bad.append(
                f"Makefile target {target_name!r} mentions pytest/py.test and "
                f"is not exempted (or the exemption text is stale): {recipe!r}"
            )
    return bad


def test_no_unexempted_pytest_mentions_across_all_workflows():
    assert workflow_pytest_violations(WORKFLOWS_DIR) == []


def test_no_unexempted_pytest_mentions_across_all_makefile_targets():
    assert makefile_pytest_violations(MAKEFILE.read_text()) == []


def test_neuter_new_workflow_job_bare_pytest_is_caught(tmp_path: Path):
    """A new workflow file running bare pytest, with no exemption, must be
    flagged by the heuristic."""
    scratch_dir = tmp_path / "workflows"
    scratch_dir.mkdir()
    (scratch_dir / "evasion.yml").write_text(
        "name: evasion\njobs:\n  e:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: pytest tests/ --cov-fail-under=90\n"
    )
    found = workflow_pytest_violations(scratch_dir)
    assert any("evasion.yml" in v for v in found), found


def test_neuter_new_makefile_target_bare_pytest_is_caught():
    mutated = (
        MAKEFILE.read_text()
        + "\ntest-fast:\n\t@.venv/bin/pytest tests/ --cov-fail-under=90\n"
    )
    found = makefile_pytest_violations(mutated)
    assert any("test-fast" in v for v in found), found


def test_exemption_entries_are_all_genuinely_present_and_exact():
    """Every exemption must describe REALITY — a stale entry (text that no
    longer matches the shipped file) is itself a finding, not a free
    pass."""
    data = _load_ci()
    for wf_name, job_name, idx, run_text in WORKFLOW_PYTEST_EXEMPTIONS:
        wf_path = WORKFLOWS_DIR / wf_name
        assert wf_path.is_file(), f"{wf_name} no longer exists"
        wf_data = yaml.safe_load(wf_path.read_text()) if wf_name != CI.name else data
        actual = (wf_data["jobs"][job_name]["steps"][idx]).get("run")
        assert actual == run_text, f"{wf_name}/{job_name}[{idx}] run text drifted"

    targets = parse_makefile(MAKEFILE.read_text())
    for target_name, exempted_text in MAKEFILE_PYTEST_EXEMPTIONS.items():
        assert target_name in targets, (
            f"Makefile target {target_name!r} no longer exists"
        )
        assert targets[target_name][1] == exempted_text, (
            f"Makefile target {target_name!r} recipe drifted from its exemption text"
        )
