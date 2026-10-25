"""Committed tests for ``scripts/ci/run-tests.sh``'s OWN process-layer
enforcement — ADDENDUM A1-4 (kubectl), A2-1/A3-8 (the helm shim), A4-5/
A5-3 (the shared case table, driven here through the bash shim on 'list'
rows only), A4-6 (the pipefail pin), and A4-10/A5-4/A6-2 (the CI probes).

This file drives the REAL ``scripts/ci/run-tests.sh`` (copied verbatim
into a scratch root, never reimplemented) with FAKE ``pytest``/``python``/
``helm`` standing in for the real ones — isolating the script's OWN
shim/reject-log logic from the Python-level conftest.py guard entirely
(the fake pytest calls `kubectl`/`helm` directly, as raw shell commands,
with no pytest/conftest.py machinery involved at all).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from tests._hermetic_env import override_sentinel

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_SCRIPT = REPO_ROOT / "scripts" / "ci" / "run-tests.sh"
REAL_SKIP_CHECK = REPO_ROOT / "scripts" / "check-no-skipped-tests.py"
REAL_COVERAGE_CHECK = REPO_ROOT / "scripts" / "check-per-file-coverage.py"
REAL_VENV_LOCK_CHECK = REPO_ROOT / "scripts" / "ci" / "check_venv_matches_lock.py"
REAL_FREEZE_CHECK = REPO_ROOT / "scripts" / "ci" / "check_freeze_matches_lock.py"
HELM_RULE_CASES = json.loads(
    (REPO_ROOT / "tests" / "data" / "helm_rule_cases.json").read_text()
)["cases"]


def _scratch_script_root(tmp_path: Path) -> Path:
    """A throwaway root containing a verbatim copy of run-tests.sh and its
    skip-check dependency, at the same relative layout, so `cd
    "$REPO_ROOT"` + the script's own relative references resolve."""
    root = tmp_path / "scratch-repo"
    (root / "scripts" / "ci").mkdir(parents=True)
    shutil.copy(REAL_SCRIPT, root / "scripts" / "ci" / "run-tests.sh")
    shutil.copy(REAL_SKIP_CHECK, root / "scripts" / "check-no-skipped-tests.py")
    shutil.copy(REAL_COVERAGE_CHECK, root / "scripts" / "check-per-file-coverage.py")
    # DEP-LOCK-AT G5 (spec gate C2): run-tests.sh now refuses an interpreter
    # that is not the hashed lock BEFORE pytest. The scratch root therefore
    # carries the REAL checker, the REAL lock and the REAL pyproject — never a
    # bypass flag or env var. That is legitimate: the suite's own interpreter IS
    # the locked one (and if it is not, G5 refuses the whole suite first).
    shutil.copy(
        REAL_VENV_LOCK_CHECK, root / "scripts" / "ci" / "check_venv_matches_lock.py"
    )
    shutil.copy(REPO_ROOT / "requirements-dev.txt", root / "requirements-dev.txt")
    shutil.copy(REPO_ROOT / "pyproject.toml", root / "pyproject.toml")
    # WU-2: G5 also covers the gate venv (second lock environment) through the
    # freeze comparator. The scratch root gets the REAL comparator, the REAL gate
    # lock and the REAL gate venv (symlinked; `make gate-venv` makes it, CI runs
    # that before the suite) — a bypass would defeat the point of the test.
    shutil.copy(
        REAL_FREEZE_CHECK, root / "scripts" / "ci" / "check_freeze_matches_lock.py"
    )
    shutil.copy(REPO_ROOT / "requirements-gate.txt", root / "requirements-gate.txt")
    (root / ".gate-venv").symlink_to(REPO_ROOT / ".gate-venv")
    return root


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _fake_pytest(
    bin_dir: Path,
    *,
    body: str = "",
    junit_testcase: str = '<testcase classname="fake" name="test_fake" time="0.0"/>',
    junit_skipped_attr: str = "0",
    record_argv_to: Path | None = None,
    write_coverage_xml: str | None = None,
) -> Path:
    """Stands in for the real `pytest` binary resolve_tool() finds on
    PATH. `body` is extra shell to run before writing the junit.xml
    (`junit_testcase`/`junit_skipped_attr` control its content, so a
    caller can simulate a skipped test) and optionally a coverage.xml
    (`write_coverage_xml`), then exits 0 — a test suite that "passed"
    while doing whatever `body` says (e.g. leaking to a real cluster
    tool, or reporting a skip / a low-coverage file)."""
    tool = bin_dir / "pytest"
    record_line = f'echo "$@" > "{record_argv_to}"\n' if record_argv_to else ""
    coverage_line = (
        f"cat > coverage.xml <<COVXML\n{write_coverage_xml}\nCOVXML\n"
        if write_coverage_xml
        else ""
    )
    _write_exec(
        tool,
        "#!/usr/bin/env bash\n"
        'junit_path=""\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        '    --junit-xml=*) junit_path="${arg#--junit-xml=}" ;;\n'
        "  esac\n"
        "done\n"
        f"{record_line}"
        f"{coverage_line}"
        f"{body}\n"
        'cat > "$junit_path" <<XML\n'
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuites><testsuite name="fake" tests="1" failures="0" errors="0" skipped="{junit_skipped_attr}">\n'
        f"{junit_testcase}\n"
        "</testsuite></testsuites>\n"
        "XML\n"
        "exit 0\n",
    )
    return tool


def _fake_python_passthrough(bin_dir: Path) -> Path:
    tool = bin_dir / "python"
    _write_exec(tool, f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
    return tool


def _fake_real_helm(bin_dir: Path) -> Path:
    """Stands in for the system helm the script resolves BEFORE changing
    PATH — logs its own invocation and exits 0. Never the real helm; this
    test suite does not need to actually render a chart to prove the
    shim's rule."""
    sentinel = bin_dir / "helm.ran.log"
    tool = bin_dir / "helm"
    _write_exec(tool, f'#!/usr/bin/env bash\necho "$@" >> "{sentinel}"\nexit 0\n')
    return sentinel


def _run_script(
    root: Path,
    bin_dir: Path,
    *,
    subset: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    if extra_env:
        env.update(extra_env)
    argv = ["bash", str(root / "scripts" / "ci" / "run-tests.sh")]
    if subset:
        argv += ["--subset", "test_fake"]
    # This call is itself a spawn the OUTER (pytest-level) guard's env
    # check sees — it deliberately hands the scratch script a from-
    # scratch, minimal env with no ambient HERMETIC_* sentinel, so
    # temporarily make the ambient sentinel match this test's own
    # PATH/absent-KUBECONFIG shape for the duration of the call (the same
    # technique tests/test_hermetic_cluster_tool_guard.py's own
    # `fake_tools` fixture uses).
    with override_sentinel(shim_dir=str(bin_dir), kubeconfig=None, required=False):
        return subprocess.run(
            argv, cwd=root, env=env, capture_output=True, text=True, timeout=60
        )


def test_a_leaking_fake_pytest_makes_run_tests_sh_exit_nonzero(tmp_path: Path):
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _fake_pytest(bin_dir, body="kubectl get nodes || true")
    _fake_python_passthrough(bin_dir)
    _fake_real_helm(bin_dir)

    result = _run_script(root, bin_dir)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "sentinel kubectl/helm shim rejected" in result.stderr
    assert "kubectl get nodes" in result.stderr


def test_kubectl_shim_itself_exits_17(tmp_path: Path):
    """Non-blocking item carried from round 3: the overall script's exit
    code and the reject log are what A1-4 checks, and those stay the same
    regardless of the shim's OWN per-call exit status to its caller (the
    reject-log check never reads it) — so this needs its own assertion on
    the shim's actual exit code, not merely "the script exits non-zero
    somewhere"."""
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    rc_file = tmp_path / "kubectl-rc.txt"
    _fake_pytest(
        bin_dir,
        body=f'kubectl get nodes; echo "$?" > "{rc_file}"',
    )
    _fake_python_passthrough(bin_dir)
    _fake_real_helm(bin_dir)

    _run_script(root, bin_dir)

    assert rc_file.read_text().strip() == "17"


def test_a_clean_fake_pytest_leaves_run_tests_sh_exit_zero(tmp_path: Path):
    """Negative control: without the leak, the same harness must pass —
    proves the failure above is caused by the leak, not by the scratch
    scaffolding itself."""
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _fake_pytest(bin_dir, body=": # no leak")
    _fake_python_passthrough(bin_dir)
    _fake_real_helm(bin_dir)

    result = _run_script(root, bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Tests passed" in result.stdout
    assert "sentinel kubectl/helm shim rejected" not in result.stderr


def test_a_leaking_helm_call_makes_run_tests_sh_exit_nonzero(tmp_path: Path):
    """A2-1: the reject log is shared — a rejected HELM call also fails
    the script, not only a kubectl one."""
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _fake_pytest(bin_dir, body="helm upgrade release chart || true")
    _fake_python_passthrough(bin_dir)
    _fake_real_helm(bin_dir)

    result = _run_script(root, bin_dir)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "sentinel kubectl/helm shim rejected" in result.stderr
    assert "helm upgrade release chart" in result.stderr


def test_an_allowed_helm_call_execs_the_real_helm_and_run_tests_sh_still_passes(
    tmp_path: Path,
):
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _fake_pytest(bin_dir, body="helm template release charts/audittrace")
    _fake_python_passthrough(bin_dir)
    ran_log = _fake_real_helm(bin_dir)

    result = _run_script(root, bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert ran_log.exists() and ran_log.stat().st_size > 0, (
        "the allowed call must reach the REAL (here: fake-but-standing-in-for-real) helm"
    )
    assert "template release charts/audittrace" in ran_log.read_text()


def test_helm_missing_on_path_fails_run_tests_sh_at_start(tmp_path: Path):
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _fake_pytest(bin_dir, body=": # no leak")
    _fake_python_passthrough(bin_dir)
    # deliberately no fake helm, and /usr/bin:/bin (needed so `bash`
    # itself resolves) has none either on this host:
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    with override_sentinel(shim_dir=str(bin_dir), kubeconfig=None, required=False):
        result = subprocess.run(
            [
                "bash",
                str(root / "scripts" / "ci" / "run-tests.sh"),
                "--subset",
                "test_fake",
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert result.returncode != 0
    assert "helm not found" in result.stderr


def test_pytest_addopts_set_fails_closed_before_anything_runs(tmp_path: Path):
    """A2-9: the script fails if PYTEST_ADDOPTS is non-empty."""
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _fake_pytest(bin_dir, body=": # should never run")
    _fake_python_passthrough(bin_dir)
    _fake_real_helm(bin_dir)

    result = _run_script(root, bin_dir, extra_env={"PYTEST_ADDOPTS": "-x"})

    assert result.returncode != 0
    assert "PYTEST_ADDOPTS is set" in result.stderr


def test_a4_6_pipefail_is_pinned_as_the_first_non_comment_line():
    """A4-6: the exact line `set -euo pipefail` is the first non-comment
    line of the REAL script (shebang + comment block excluded), and no
    line anywhere turns errexit/pipefail back off."""
    lines = REAL_SCRIPT.read_text().splitlines()
    first_non_comment = next(
        line for line in lines if line.strip() and not line.lstrip().startswith("#")
    )
    assert first_non_comment == "set -euo pipefail"
    text = REAL_SCRIPT.read_text()
    import re

    assert not re.search(r"^\s*set\s+\+", text, re.MULTILINE)
    assert not re.search(r"^\s*set\s+-o\s+(no)?errexit", text, re.MULTILINE)
    assert not re.search(r"^\s*shopt\b", text, re.MULTILINE)


def test_a4_6_neuter_a_set_plus_e_line_would_be_caught(tmp_path: Path):
    """Neuter proof for the pin above: a scratch copy with `set +e` added
    anywhere must fail this same assertion style."""
    scratch = tmp_path / "run-tests-neutered.sh"
    scratch.write_text(REAL_SCRIPT.read_text() + "\nset +e\n")
    import re

    assert re.search(r"^\s*set\s+\+", scratch.read_text(), re.MULTILINE)


def test_a4_10_ci_probes_use_only_the_or_echo_form():
    """A4-10/A5-4/A6-2: the ONLY allowed probe form is
    `... || echo "probe ... rc=$?"` — a bare `; echo rc=$?` would abort
    under `set -euo pipefail` when the probed command fails (measured:
    curl's connection-refused exit code is non-zero)."""
    text = REAL_SCRIPT.read_text()
    assert (
        'curl -sS --max-time 2 http://localhost:8080 >/dev/null 2>&1 || echo "probe localhost:8080 rc=$?"'
        in text
    )
    assert "http://localhost:8080 >/dev/null 2>&1; echo" not in text


def test_a4_10_probes_do_not_abort_the_script_under_pipefail(tmp_path: Path):
    """Behavioural proof that the `|| echo` form is non-fatal: drives the
    real script end-to-end (localhost:8080 is refused on this host) and
    confirms the probe line's exit does not abort the run."""
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _fake_pytest(bin_dir, body=": # no leak")
    _fake_python_passthrough(bin_dir)
    _fake_real_helm(bin_dir)

    result = _run_script(root, bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "probe localhost:8080 rc=" in result.stdout


def test_a4_10_command_dash_v_helm_probe_prints(tmp_path: Path):
    """Non-blocking item carried from round 3: removing the `command -v
    helm` probe line leaves the script green (it is diagnostic-only, per
    the digest), so it needs its own behavioural assertion that the probe
    line actually prints — otherwise a silent deletion goes unnoticed by
    every OTHER test in this file."""
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _fake_pytest(bin_dir, body=": # no leak")
    _fake_python_passthrough(bin_dir)
    _fake_real_helm(bin_dir)

    result = _run_script(root, bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "command -v helm ->" in result.stdout


# ──────────────── A4-5/A5-3: the shared helm case table, bash side ──────────


def _drive_case_through_real_script(tmp_path_factory, case: dict) -> tuple[bool, str]:
    """Runs ONE helm case ('list' rows only, per A5-3) through the REAL
    run-tests.sh, by having the fake pytest invoke exactly that argv.
    Returns (rejected, reject_log_text)."""
    tmp_path = tmp_path_factory.mktemp("helm-case")
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    argv = case["argv"]
    quoted = " ".join(f'"{a}"' for a in argv)
    _fake_pytest(bin_dir, body=f"{quoted} || true")
    _fake_python_passthrough(bin_dir)
    _fake_real_helm(bin_dir)

    result = _run_script(root, bin_dir)
    rejected = "sentinel kubectl/helm shim rejected" in result.stderr
    return rejected, result.stderr


def test_helm_rule_case_table_through_bash_shim_list_rows_only(tmp_path_factory):
    """A5-3: the bash shim is driven over 'list' rows ONLY, and must
    agree with the Python rule (tests/test_hermetic_cluster_tool_guard.py
    drives the SAME table through the Python rule) on every one."""
    list_cases = [
        c for c in HELM_RULE_CASES if c["form"] == "list" and c["argv"][:1] == ["helm"]
    ]
    assert list_cases, "the table must contain at least one helm list-form case"
    mismatches = []
    for case in list_cases:
        rejected, _ = _drive_case_through_real_script(tmp_path_factory, case)
        expect_rejected = case["expect"] == "raise"
        if rejected != expect_rejected:
            mismatches.append(
                (case["id"], case["expect"], "raise" if rejected else "allow")
            )
    assert mismatches == []


# ──────────────── A2-9: each script gate fires on its own ───────────────────


def test_a2_9_zero_skip_gate_fires_on_full_run(tmp_path: Path):
    """S20 (round-2 Required #4, re-REJECTed in round 3): the fake pytest
    must ALSO write a PASSING coverage.xml, so that ONLY the skip gate can
    fail here — otherwise `check-per-file-coverage.py`'s own "coverage.xml
    not found" abort (which also happens to contain the substring "skip"
    in the script's unconditional banner echo) makes this pass for the
    wrong reason. The assertion matches the skip-check script's OWN
    specific failure text, never the banner echo lines every run prints
    regardless of outcome."""
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    passing_coverage_xml = (
        '<?xml version="1.0" ?>\n'
        '<coverage line-rate="1.0" branch-rate="1.0"><packages/></coverage>'
    )
    _fake_pytest(
        bin_dir,
        junit_testcase='<testcase classname="fake" name="test_fake" time="0.0"><skipped/></testcase>',
        junit_skipped_attr="1",
        write_coverage_xml=passing_coverage_xml,
    )
    _fake_python_passthrough(bin_dir)
    _fake_real_helm(bin_dir)

    result = _run_script(root, bin_dir, subset=False)

    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "coverage.xml not found" not in combined, (
        "the per-file gate must not be what failed here"
    )
    assert "skipped testcase(s)" in combined
    assert "skips are forbidden" in combined


def test_a2_9_zero_skip_gate_fires_on_subset_run(tmp_path: Path):
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _fake_pytest(
        bin_dir,
        junit_testcase='<testcase classname="fake" name="test_fake" time="0.0"><skipped/></testcase>',
        junit_skipped_attr="1",
    )
    _fake_python_passthrough(bin_dir)
    _fake_real_helm(bin_dir)

    result = _run_script(root, bin_dir, subset=True)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "skip" in (result.stdout + result.stderr).lower()


def test_a2_9_per_file_coverage_gate_fires(tmp_path: Path):
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    low_coverage_xml = (
        '<?xml version="1.0" ?>\n'
        '<coverage line-rate="0.5" branch-rate="0.5">\n'
        '<packages><package name="src">\n'
        '<classes><class filename="src/audittrace/example.py" line-rate="0.5" branch-rate="0.5">\n'
        '<lines><line number="1" hits="1"/><line number="2" hits="0"/></lines>\n'
        "</class></classes>\n"
        "</package></packages>\n"
        "</coverage>"
    )
    _fake_pytest(bin_dir, write_coverage_xml=low_coverage_xml)
    _fake_python_passthrough(bin_dir)
    _fake_real_helm(bin_dir)

    result = _run_script(root, bin_dir, subset=False)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "coverage" in (result.stdout + result.stderr).lower()


def test_a2_9_pytest_argument_set_matches_golden(tmp_path: Path):
    """The `--cov` set and `--cov-fail-under=90` handed to pytest, as
    CAPTURED by a fake pytest that records its own argv, compared against
    a golden list."""
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    argv_dump = tmp_path / "argv.txt"
    passing_coverage_xml = (
        '<?xml version="1.0" ?>\n'
        '<coverage line-rate="1.0" branch-rate="1.0"><packages/></coverage>'
    )
    _fake_pytest(
        bin_dir, record_argv_to=argv_dump, write_coverage_xml=passing_coverage_xml
    )
    _fake_python_passthrough(bin_dir)
    _fake_real_helm(bin_dir)

    result = _run_script(root, bin_dir, subset=False)

    assert result.returncode == 0, result.stdout + result.stderr
    recorded = argv_dump.read_text().split()
    golden = [
        "tests/",
        "-v",
        "--cov=src",
        "--cov=bff",
        "--cov=scripts/deploy",
        "--cov=scripts/hooks",
        "--cov=scripts/release",
        "--cov=scripts/migrate",
        "--cov=scripts/curator",
        "--cov=scripts/network",
        "--cov=scripts/neuter",
        "--cov=scripts/ci",
        "--cov=scripts.replay_dead_lettered_index",
        "--cov=scripts.backfill_pdf_signature_status",
        "--cov=scripts.integration_gate",
        "--cov-report=term-missing",
        "--cov-report=xml",
        "--cov-report=html:htmlcov",
        "--cov-fail-under=90",
        "--junit-xml=junit.xml",
    ]
    assert recorded == golden


def test_a_scratch_run_with_an_altered_lock_copy_is_refused_before_pytest(
    tmp_path: Path,
):
    """C2 / N8: the lock copy names fastapi at a version this interpreter does
    not have -> run-tests.sh exits non-zero BEFORE pytest runs, with the
    recovery path in the error. The fake pytest would have exited 0, so the
    refusal can only come from the G5 preamble."""
    root = _scratch_script_root(tmp_path)
    lock = root / "requirements-dev.txt"
    lock.write_text(
        lock.read_text().replace("\nfastapi==0.146.0 ", "\nfastapi==0.141.1 ", 1)
    )
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    ran = tmp_path / "pytest.ran"
    _fake_pytest(bin_dir, body=f'touch "{ran}"')
    _fake_python_passthrough(bin_dir)
    _fake_real_helm(bin_dir)

    result = _run_script(root, bin_dir)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "check-venv-matches-lock" in result.stderr
    assert "rm -rf .venv && make venv" in result.stderr
    assert not ran.exists(), "pytest ran although the venv does not match the lock"


# --------------------------------------------------------------------------
# DEP-LOCK-AT WU-2: G5 on every lock environment, after pytest, on $PYTEST's
# interpreter (BL-1), and the SIGTERM trap (BL-4).
# --------------------------------------------------------------------------


def _fakes(tmp_path: Path, **pytest_kwargs: object) -> tuple[Path, Path]:
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _fake_pytest(bin_dir, **pytest_kwargs)  # type: ignore[arg-type]
    _fake_python_passthrough(bin_dir)
    _fake_real_helm(bin_dir)
    return root, bin_dir


def test_g5_runs_again_after_pytest_and_refuses_a_run_that_broke_the_lock(
    tmp_path: Path,
):
    """Residual 4: the pre-pytest check passes, then the (fake) suite changes
    the environment's contract; the run must NOT pass. Neuter: delete the
    `check_environments post` line -> this goes RED (exit 0)."""
    root, bin_dir = _fakes(
        tmp_path,
        body=("sed -i 's/^fastapi==0.146.0 /fastapi==0.141.1 /' requirements-dev.txt"),
    )
    result = _run_script(root, bin_dir)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "G5 (post-pytest)" in result.stdout
    assert "check-venv-matches-lock" in result.stderr
    assert "fastapi" in result.stderr


def test_g5_checks_the_interpreter_pytest_runs_under_not_only_python(tmp_path: Path):
    """BL-1: $PYTHON is green, but the pytest console script's shebang names a
    different interpreter that is NOT the lock. Neuter: check only $PYTHON
    (delete the PYTEST_PYTHON branch) -> this goes RED."""
    root, bin_dir = _fakes(tmp_path)
    other = bin_dir / "other" / "python3"
    other.parent.mkdir()
    _write_exec(
        other,
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  *check_venv_matches_lock.py)\n"
        '    echo "STALE-INTERPRETER: this is not the lock" >&2; exit 1 ;;\n'
        "esac\n"
        f'exec "{sys.executable}" "$@"\n',
    )
    ran = tmp_path / "pytest.ran"
    pytest_tool = bin_dir / "pytest"
    body = pytest_tool.read_text().split("\n", 1)[1]
    _write_exec(
        pytest_tool,
        f"#!{other}\n" + body.replace("exit 0\n", f'touch "{ran}"\nexit 0\n'),
    )

    result = _run_script(root, bin_dir)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "STALE-INTERPRETER" in result.stderr
    assert not ran.exists(), "pytest ran under an interpreter that is not the lock"


def test_a_pytest_whose_interpreter_cannot_be_identified_is_refused(tmp_path: Path):
    """BL-1 fail closed: a pytest that is neither a python-shebang script nor has
    a sibling python is refused, never assumed to be $PYTHON."""
    root = _scratch_script_root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _fake_pytest(bin_dir)
    # `python` lives elsewhere on PATH, not next to pytest.
    py_dir = tmp_path / "pydir"
    py_dir.mkdir()
    _fake_python_passthrough(py_dir)
    _fake_real_helm(bin_dir)

    result = _run_script(
        root,
        bin_dir,
        extra_env={"PATH": f"{bin_dir}{os.pathsep}{py_dir}{os.pathsep}/usr/bin:/bin"},
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "cannot identify the interpreter" in result.stderr


def test_g5_refuses_a_gate_venv_that_is_not_the_gate_lock(tmp_path: Path):
    """Second lock environment. A gate venv whose `pip freeze --all` is wrong is
    refused before pytest. Neuter: drop the gate-venv freeze call -> RED."""
    root, bin_dir = _fakes(tmp_path)
    (root / ".gate-venv").unlink()
    gate_bin = root / ".gate-venv" / "bin"
    gate_bin.mkdir(parents=True)
    _write_exec(gate_bin / "python", "#!/usr/bin/env bash\necho 'semgrep==0.0.1'\n")
    ran = tmp_path / "pytest.ran"
    _fake_pytest(bin_dir, body=f'touch "{ran}"')

    result = _run_script(root, bin_dir)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "gate venv is NOT requirements-gate.txt" in result.stderr
    assert not ran.exists()


def test_a_missing_gate_venv_is_refused_not_skipped(tmp_path: Path):
    root, bin_dir = _fakes(tmp_path)
    (root / ".gate-venv").unlink()

    result = _run_script(root, bin_dir)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "make gate-venv" in result.stderr


def _fake_docker(bin_dir: Path, log: Path) -> None:
    """A docker that lists two containers for the run label and logs `rm -f`."""
    _write_exec(
        bin_dir / "docker",
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        'if [ "$1" = ps ]; then echo cid-one; echo cid-two; fi\n'
        "exit 0\n",
    )


def _default_sigint() -> None:
    """A parent started as `cmd &` from a non-interactive shell (CI, nohup chains)
    has SIGINT ignored, and an ignored-on-entry signal cannot be trapped by bash.
    The child under test is given the default disposition a terminal user has."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_sigterm_to_the_wrapper_stops_pytest_its_children_and_the_containers(
    tmp_path: Path,
):
    """BL-4. Send SIGTERM to run-tests.sh itself while a (fake) pytest and a child
    of it are running: both must be gone, the run's labelled containers must be
    force-removed, and the exit status is 143. Neuter: delete the TERM trap ->
    the fake pytest is orphaned and this goes RED."""
    root, bin_dir = _fakes(tmp_path)
    pids = tmp_path / "pids"
    docker_log = tmp_path / "docker.log"
    _fake_docker(bin_dir, docker_log)
    tool = bin_dir / "pytest"
    _write_exec(
        tool,
        f'#!/usr/bin/env bash\nsleep 300 &\necho "$$ $!" > "{pids}"\nwait\n',
    )
    argv = ["bash", str(root / "scripts" / "ci" / "run-tests.sh"), "--subset", "x"]
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    with override_sentinel(shim_dir=str(bin_dir), kubeconfig=None, required=False):
        proc = subprocess.Popen(
            argv,
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=_default_sigint,
        )
        deadline = time.time() + 30
        while not pids.exists() and time.time() < deadline:
            time.sleep(0.1)
        assert pids.exists(), "the fake pytest never started"
        time.sleep(0.2)
        pytest_pid, child_pid = (int(x) for x in pids.read_text().split())
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)

    assert proc.returncode == 143, out + err
    assert "caught SIGTERM" in err
    deadline = time.time() + 5
    while (_alive(pytest_pid) or _alive(child_pid)) and time.time() < deadline:
        time.sleep(0.1)
    assert not _alive(pytest_pid), "the pytest child was orphaned"
    assert not _alive(child_pid), "a grandchild of pytest was orphaned"
    log = docker_log.read_text()
    assert "label=audittrace.run-tests-id=run-tests-" in log
    assert "rm -f cid-one cid-two" in log


def test_sigint_is_handled_the_same_way(tmp_path: Path):
    root, bin_dir = _fakes(tmp_path)
    pids = tmp_path / "pids"
    docker_log = tmp_path / "docker.log"
    _fake_docker(bin_dir, docker_log)
    _write_exec(
        bin_dir / "pytest",
        f'#!/usr/bin/env bash\necho "$$" > "{pids}"\nsleep 300 &\nwait\n',
    )
    argv = ["bash", str(root / "scripts" / "ci" / "run-tests.sh"), "--subset", "x"]
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    with override_sentinel(shim_dir=str(bin_dir), kubeconfig=None, required=False):
        proc = subprocess.Popen(
            argv,
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=_default_sigint,
        )
        deadline = time.time() + 30
        while not pids.exists() and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(0.2)
        pytest_pid = int(pids.read_text().split()[0])
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=30)

    assert proc.returncode == 130, out + err
    assert not _alive(pytest_pid)
    assert "rm -f cid-one cid-two" in docker_log.read_text()


def test_a_normal_run_removes_its_labelled_containers_too(tmp_path: Path):
    """The label cleanup is not signal-only: a run that ends normally with a
    container left behind (atexit skipped) is reaped as well."""
    root, bin_dir = _fakes(tmp_path)
    docker_log = tmp_path / "docker.log"
    _fake_docker(bin_dir, docker_log)

    result = _run_script(root, bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "rm -f cid-one cid-two" in docker_log.read_text()


def test_ephemeral_postgres_is_labelled_with_the_run_id_only_when_one_is_set():
    """BL-4 half two: the label is what lets the wrapper find the containers.
    Neuter: return [] unconditionally -> the SIGTERM cleanup finds nothing."""
    from tests._pg_ephemeral import run_label_args

    assert run_label_args({"RUN_TESTS_ID": "run-tests-1-2"}) == [
        "--label",
        "audittrace.run-tests-id=run-tests-1-2",
    ]
    assert run_label_args({}) == []
    assert run_label_args() == run_label_args(dict(os.environ))


def test_a_pytest_that_ignores_sigterm_is_killed_after_the_grace_period(tmp_path: Path):
    """BL-4 escalation: TERM is not enough for a hung pytest. Neuter: delete the
    `kill -KILL` after the grace loop -> the ignoring child survives and this goes
    RED. (The scratch copy shortens the 10 s grace to 1 s.)"""
    root, bin_dir = _fakes(tmp_path)
    script = root / "scripts" / "ci" / "run-tests.sh"
    text = script.read_text()
    assert "TERM_GRACE_POLLS=50" in text
    script.write_text(text.replace("TERM_GRACE_POLLS=50", "TERM_GRACE_POLLS=5"))
    pids = tmp_path / "pids"
    _write_exec(
        bin_dir / "pytest",
        "#!/usr/bin/env bash\n"
        "trap '' TERM\n"
        f'echo "$$" > "{pids}"\n'
        "while true; do sleep 0.1; done\n",
    )
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    with override_sentinel(shim_dir=str(bin_dir), kubeconfig=None, required=False):
        proc = subprocess.Popen(
            ["bash", str(script), "--subset", "x"],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + 30
        while not pids.exists() and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(0.3)
        hung_pid = int(pids.read_text())
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
    assert proc.returncode == 143, out + err
    deadline = time.time() + 5
    while _alive(hung_pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not _alive(hung_pid), "a pytest that ignores SIGTERM was left running"


def test_g5_runs_after_pytest_on_the_full_path_too(tmp_path: Path):
    """B1: `make test` and CI use full mode, not --subset. The fake suite leaves the
    lock violated; the full-mode post-pytest G5 must refuse BEFORE the zero-skip and
    coverage checks (a passing coverage.xml is supplied so only G5 can fail the run).
    Neuter: delete ONLY the full-mode `check_environments post` -> exit 0, RED."""
    passing_xml = (
        '<?xml version="1.0" ?>\n'
        '<coverage line-rate="1.0" branch-rate="1.0"><packages/></coverage>'
    )
    root, bin_dir = _fakes(
        tmp_path,
        body="sed -i 's/^fastapi==0.146.0 /fastapi==0.141.1 /' requirements-dev.txt",
        write_coverage_xml=passing_xml,
    )
    result = _run_script(root, bin_dir, subset=False)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "G5 (post-pytest)" in result.stdout
    assert "fastapi" in result.stderr
    assert "Enforcing zero-skip" not in result.stdout, "G5 must refuse first"


def test_the_container_label_variable_survives_conftests_environment_wipe():
    """B2: conftest deletes every AUDITTRACE_* variable at import. Import the REAL
    conftest in a fresh interpreter with the variable set and read the label args
    through the real helper. Neuter: rename the variable back to an AUDITTRACE_
    name -> the label list is empty -> RED."""
    from tests._pg_ephemeral import RUN_ID_ENV

    code = (
        "import tests.conftest\n"
        "from tests._pg_ephemeral import run_label_args\n"
        "print(run_label_args())\n"
    )
    env = {**os.environ, RUN_ID_ENV: "probe-123", "PYTHONDONTWRITEBYTECODE": "1"}
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "audittrace.run-tests-id=probe-123" in out


def test_inside_a_real_wrapped_run_the_label_variable_reaches_the_suite():
    """B2, observed from INSIDE the suite process: under scripts/ci/run-tests.sh
    (which exports HERMETIC_REQUIRED=1) the run id must have survived conftest's
    wipe and produce a label. Outside the wrapper there is nothing to observe."""
    from tests._pg_ephemeral import run_label_args

    if os.environ.get("HERMETIC_REQUIRED") == "1":
        args = run_label_args()
        assert args and args[1].startswith("audittrace.run-tests-id=run-tests-")
