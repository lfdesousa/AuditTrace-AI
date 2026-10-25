"""AC-6 as a PR-time gate (DEP-LOCK-AT WU-2): freeze inside each built image == the
lock committed at HEAD, build-not-pull, fail closed.

The REAL ``scripts/ci/check-images-match-locks.sh`` runs in a scratch git repo with a
FAKE ``docker`` that serves canned freeze listings, so every branch of the gate is
exercised without building an image (the real build is the evidence run and the CI
job). Each docstring names the neuter that must turn the test RED.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SHA = "c" * 40
LOCK = "alpha==1.0 \\\n    --hash=sha256:" + "0" * 64 + "\n"
BFF_LOCK = "beta==2.0 \\\n    --hash=sha256:" + "1" * 64 + "\n"
STUB_LOCK = "gamma==3.0 \\\n    --hash=sha256:" + "2" * 64 + "\n"
PYPROJECT = f'deps = ["audittrace-object-storage @ git+https://x/audittrace-object-storage.git@{SHA}"]\n'
MS_FREEZE = (
    f"alpha==1.0\naudittrace-ai @ file:///build\n"
    f"audittrace-object-storage @ git+https://x/audittrace-object-storage.git@{SHA}\n"
)
FREEZES = {"ms": MS_FREEZE, "bff": "beta==2.0\n", "stub": "gamma==3.0\n"}


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t.invalid", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts" / "ci").mkdir(parents=True)
    (root / "bff").mkdir()
    (root / "images" / "llm-stub").mkdir(parents=True)
    for name in (
        "check-images-match-locks.sh",
        "check_freeze_matches_lock.py",
        "check_venv_matches_lock.py",
    ):
        shutil.copy(REPO_ROOT / "scripts" / "ci" / name, root / "scripts" / "ci" / name)
    (root / "scripts" / "__init__.py").touch()
    (root / "requirements.txt").write_text(LOCK)
    (root / "bff" / "requirements.txt").write_text(BFF_LOCK)
    (root / "images" / "llm-stub" / "requirements.txt").write_text(STUB_LOCK)
    (root / "pyproject.toml").write_text(PYPROJECT)
    for f in ("Dockerfile", "bff/Dockerfile", "images/llm-stub/Dockerfile"):
        (root / f).write_text("FROM scratch\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "x")
    return root


def _fake_docker(
    bin_dir: Path, log: Path, freezes: dict[str, str], *, fail_build: bool = False
) -> None:
    """build -> log (rc 1 if fail_build); run ... -c -> a marker env; run ... freeze -> the canned listing."""
    (bin_dir / "freezes.json").write_text(json.dumps(freezes))
    tool = bin_dir / "docker"
    tool.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        'if [ "$1" = build ]; then exit ' + ("1" if fail_build else "0") + " ; fi\n"
        'case "$*" in\n'
        "  *memory-server:*) key=ms ;; *bff:*) key=bff ;; *llm-stub:*) key=stub ;;\n"
        "esac\n"
        'case "$*" in\n'
        "  *'default_environment'*) echo '{\"sys_platform\": \"linux\"}' ;;\n"
        f'  *freeze*) {sys.executable} -c "import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]], end=\'\')" "{bin_dir}/freezes.json" "$key" ;;\n'
        "esac\n"
    )
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)


def _run(root: Path, bin_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
        "AC6_PYTHON": sys.executable,
    }
    return subprocess.run(
        ["bash", str(root / "scripts" / "ci" / "check-images-match-locks.sh"), *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_images_equal_to_their_locks_pass_and_all_three_are_built_from_source(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    log = tmp_path / "docker.log"
    _fake_docker(tmp_path, log, FREEZES)
    result = _run(root, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text()
    assert "build -f Dockerfile -t audittrace-memory-server:ac6 ." in calls
    assert "build -f bff/Dockerfile -t audittrace-bff:ac6 ." in calls
    assert (
        "build -f images/llm-stub/Dockerfile -t audittrace-llm-stub:ac6 images/llm-stub"
        in calls
    )
    assert "pull" not in calls, "build-not-pull: the gate must never pull an image"


def test_one_pin_changed_in_an_image_is_red_and_names_the_package(
    tmp_path: Path,
) -> None:
    """AC-6 neuter: change ONE pin in an image's freeze -> exit 1 naming it.
    Removing the comparison (or the `--freeze` wiring) turns this GREEN-when-it-must-be-RED."""
    root = _repo(tmp_path)
    _fake_docker(tmp_path, tmp_path / "d.log", {**FREEZES, "bff": "beta==2.0.1\n"})
    result = _run(root, tmp_path)
    assert result.returncode != 0
    assert (
        "bff image" in result.stderr
        and "beta: installed 2.0.1, lock pins 2.0" in result.stderr
    )


def test_an_unlocked_extra_package_in_an_image_is_red(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _fake_docker(
        tmp_path, tmp_path / "d.log", {**FREEZES, "stub": "gamma==3.0\nrogue==1\n"}
    )
    result = _run(root, tmp_path)
    assert result.returncode != 0
    assert "rogue==1 is installed but NOT in the lock" in result.stderr


def test_the_vcs_package_at_the_wrong_sha_is_red(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _fake_docker(
        tmp_path,
        tmp_path / "d.log",
        {**FREEZES, "ms": MS_FREEZE.replace(SHA, "d" * 40)},
    )
    result = _run(root, tmp_path)
    assert result.returncode != 0
    assert "pyproject pins commit" in result.stderr


def test_the_gate_compares_the_committed_lock_not_the_working_tree(
    tmp_path: Path,
) -> None:
    """Neuter: read the working-tree lock -> a dirty lock lets a drifted image pass."""
    root = _repo(tmp_path)
    (root / "requirements.txt").write_text(
        LOCK.replace("alpha==1.0", "alpha==9.9")
    )  # uncommitted
    _fake_docker(tmp_path, tmp_path / "d.log", FREEZES)
    assert _run(root, tmp_path).returncode == 0


def test_a_failed_image_build_fails_the_gate(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _fake_docker(tmp_path, tmp_path / "d.log", FREEZES, fail_build=True)
    assert _run(root, tmp_path).returncode != 0


def test_no_build_compares_without_building_and_unknown_arguments_are_refused(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    log = tmp_path / "d.log"
    _fake_docker(tmp_path, log, FREEZES)
    assert _run(root, tmp_path, "--no-build").returncode == 0
    assert "build" not in log.read_text().split()
    refused = _run(root, tmp_path, "--skip")
    assert refused.returncode == 2 and "unknown argument" in refused.stderr


def test_a_missing_docker_fails_closed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "dirname").symlink_to(shutil.which("dirname") or "/usr/bin/dirname")
    env = {"PATH": str(bare), "AC6_PYTHON": sys.executable}
    result = subprocess.run(
        ["/bin/bash", str(root / "scripts" / "ci" / "check-images-match-locks.sh")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0 and "docker not found" in result.stderr


def test_the_arm64_platform_knob_reaches_build_and_run(tmp_path: Path) -> None:
    """AC-7a: AC6_PLATFORM selects `--platform` for both the build and the freeze run."""
    root = _repo(tmp_path)
    log = tmp_path / "d.log"
    _fake_docker(tmp_path, log, FREEZES)
    env = {
        "PATH": f"{tmp_path}{os.pathsep}/usr/bin:/bin",
        "AC6_PYTHON": sys.executable,
        "AC6_PLATFORM": "linux/arm64",
    }
    result = subprocess.run(
        ["bash", str(root / "scripts" / "ci" / "check-images-match-locks.sh")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = log.read_text().splitlines()
    assert all("--platform linux/arm64" in ln for ln in lines)


# ---- the workflow wiring ---------------------------------------------------------------


def _steps() -> list[dict[str, object]]:
    wf = yaml.safe_load((REPO_ROOT / ".github/workflows/e2e-compose.yml").read_text())
    return list(wf["jobs"]["compose-e2e"]["steps"])


def test_the_pr_workflow_runs_the_gate_before_the_stack_comes_up() -> None:
    """Neuter: delete the step (or add `--no-build`/`continue-on-error`) -> RED."""
    steps = _steps()
    idx = [
        i
        for i, s in enumerate(steps)
        if "check-images-match-locks.sh" in str(s.get("run", ""))
    ]
    assert len(idx) == 1, "exactly one AC-6 step"
    step = steps[idx[0]]
    assert str(step["run"]).strip() == "bash scripts/ci/check-images-match-locks.sh"
    assert "continue-on-error" not in step and "if" not in step, (
        "the gate is unconditional and blocking"
    )
    up = next(
        i for i, s in enumerate(steps) if "docker compose up" in str(s.get("run", ""))
    )
    assert idx[0] < up


def test_the_gate_script_itself_never_pulls_and_reads_git_show_head() -> None:
    text = (REPO_ROOT / "scripts/ci/check-images-match-locks.sh").read_text()
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "docker pull" not in code and "compose pull" not in code
    assert '--lock-rev "HEAD:$lock"' in code
