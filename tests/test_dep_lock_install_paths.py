"""Structural guards around the install paths (DEP-LOCK-AT, ADR-065).

The G4-CP token scanner and its rejecting tests live in
``tests/test_dep_lock_g4_chokepoint.py`` (scanner: ``tests/_g4_scan.py``; allow list:
``tests/_g4_sanctioned.py``). This module keeps the structural checks that are not about
installer tokens: the VCS commit SHA exists once and every copy agrees (N7, N7b), the
pre-commit hooks are local system hooks (N6), the chokepoint script builds both source
trees with ``--no-build-isolation``, and ``make security-lint`` resolves ONLY the gate
venv (C7).
"""

from __future__ import annotations

import functools
import re
import subprocess
from pathlib import Path

import yaml

from tests._g4_scan import join_units, scan_files
from tests._hermetic_env import hermetic_env
from tests.test_ci_make_targets_resolve_tools import parse_makefile

ROOT = Path(__file__).resolve().parent.parent

VCS_OCCURRENCE = re.compile(r"audittrace-object-storage\.git@([^\s\"')]+)")


@functools.cache
def tracked_text_files(root: Path = ROOT) -> dict[str, str]:
    """Text of every scanned file (the G4-CP file set); undecodable bytes are replaced,
    never dropped (the scanner itself judges them as bytes)."""
    return {
        rel: data.decode("utf-8", errors="replace")
        for rel, data in scan_files(root).items()
    }


def vcs_violations(files: dict[str, str], pyproject_text: str) -> list[str]:
    pinned = re.search(
        r"audittrace-object-storage\.git@([0-9a-f]{40})\"", pyproject_text
    )
    if not pinned:
        return [
            "pyproject.toml: audittrace-object-storage is not pinned to a 40-hex SHA"
        ]
    sha = pinned.group(1)
    bad: list[str] = []
    for rel, text in sorted(files.items()):
        for m in VCS_OCCURRENCE.finditer(text):
            value = m.group(1)
            if value == sha:
                continue
            reads_pyproject = value in ("${SHA}", "$SHA") and (
                "pyproject.toml" in text and "sed -n" in text
            )
            if not reads_pyproject:
                bad.append(f"{rel}: VCS ref {value!r} is not pyproject's {sha[:12]}…")
    return bad


def precommit_violations(config_text: str) -> list[str]:
    data = yaml.safe_load(config_text)
    bad: list[str] = []
    for repo in data["repos"]:
        url = repo["repo"]
        if "mirrors-mypy" in url or "ruff-pre-commit" in url:
            bad.append(f"pre-commit repo {url} builds its own unlocked environment")
        for hook in repo["hooks"]:
            if "additional_dependencies" in hook:
                bad.append(f"hook {hook['id']}: additional_dependencies is unhashed")
    local = {
        h["id"]: h for r in data["repos"] if r["repo"] == "local" for h in r["hooks"]
    }
    for hook_id, entry in (("ruff", "make lint-ruff"), ("mypy", "make typecheck")):
        h = local.get(hook_id)
        if h is None:
            bad.append(f"no local `{hook_id}` hook")
            continue
        want = {
            "entry": entry,
            "language": "system",
            "pass_filenames": False,
            "require_serial": True,
            "files": r"\.py$",
        }
        for key, value in want.items():
            if h.get(key) != value:
                bad.append(
                    f"hook {hook_id}: {key} is {h.get(key)!r}, want {value!r} (C5)"
                )
    return bad


def _with(files: dict[str, str], rel: str, extra: str) -> dict[str, str]:
    mutated = dict(files)
    mutated[rel] = mutated[rel] + "\n" + extra + "\n"
    return mutated


def _units(rel: str) -> list[str]:
    return [u for u, _ in join_units(tracked_text_files()[rel])]


def test_the_scan_actually_sees_the_install_paths() -> None:
    """Vacuity guard: the files that MUST contain a hashed install do, and the
    scan reached them (an empty scan would make the G4-CP baseline test vacuous)."""
    must = {
        "scripts/install-locked.sh": "--require-hashes",
        "Dockerfile": "-r requirements.txt",
        "Dockerfile.tests": "-r /tmp/requirements-dev.txt",
        "bff/Dockerfile": "-r requirements.txt",
        "images/llm-stub/Dockerfile": "-r /app/requirements.txt",
    }
    for rel, needle in must.items():
        assert any(
            needle in u and "--require-hashes" in u and " install " in u
            for u in _units(rel)
        ), rel
    files = tracked_text_files()
    for rel in (".github/workflows/ci.yml", ".github/workflows/test-count-badge.yml"):
        assert "bash scripts/install-locked.sh pip" in files[rel], rel
    assert (
        "scripts/setup.sh" in files and "install-locked.sh" in files["scripts/setup.sh"]
    )
    makefile = files["Makefile"]
    assert "install-locked.sh .venv/bin/pip" in makefile
    assert "install-locked.sh $(GATE_VENV)/bin/pip gate" in makefile


def test_the_chokepoint_uses_no_build_isolation_for_every_source_build() -> None:
    """Read from the hash-pinned file: both source builds carry the flag."""
    builds = [
        u
        for u in _units("scripts/install-locked.sh")
        if "--no-deps" in u and " install " in u and not u.lstrip().startswith("#")
    ]
    assert len(builds) == 2  # `-e .` and the VCS requirement
    assert all("--no-build-isolation" in u for u in builds)


def test_the_vcs_commit_sha_exists_once_and_every_copy_agrees() -> None:
    files = tracked_text_files()
    assert vcs_violations(files, (ROOT / "pyproject.toml").read_text()) == []
    occurrences = [rel for rel, t in files.items() if VCS_OCCURRENCE.search(t)]
    assert "pyproject.toml" in occurrences


def test_precommit_hooks_are_local_system_and_unlocked_repos_are_gone() -> None:
    assert precommit_violations((ROOT / ".pre-commit-config.yaml").read_text()) == []


def test_the_ruff_hook_target_does_not_pull_in_semgrep() -> None:
    rules = parse_makefile((ROOT / "Makefile").read_text())
    deps, recipe = rules["lint-ruff"]
    assert deps == []
    assert "semgrep" not in recipe and "ruff check" in recipe
    assert "--fix" not in recipe  # check-only: the hook never rewrites staged files


# ───────────────────────── neuters N6 / N7 / N7b ─────────────────────────


def test_n6_additional_dependencies_in_precommit_is_red() -> None:
    text = (ROOT / ".pre-commit-config.yaml").read_text()
    text = text.replace(
        "      - id: mypy\n",
        "      - id: mypy\n        additional_dependencies: [fastapi]\n",
        1,
    )
    assert any("additional_dependencies" in v for v in precommit_violations(text))


def test_n6b_a_mirrors_mypy_repo_is_red() -> None:
    text = (ROOT / ".pre-commit-config.yaml").read_text()
    text += (
        "  - repo: https://github.com/pre-commit/mirrors-mypy\n"
        "    rev: v2.3.1\n    hooks:\n      - id: mypy2\n"
    )
    assert any("mirrors-mypy" in v for v in precommit_violations(text))


def test_n6c_a_precommit_hook_that_passes_filenames_is_red() -> None:
    text = (ROOT / ".pre-commit-config.yaml").read_text()
    head, _, tail = text.partition("      - id: mypy\n")
    tail = tail.replace("        pass_filenames: false\n", "", 1)
    assert any(
        "pass_filenames" in v
        for v in precommit_violations(head + "      - id: mypy\n" + tail)
    )


def test_n7_vcs_pin_back_to_a_tag_is_red() -> None:
    files = tracked_text_files()
    pyproject = (ROOT / "pyproject.toml").read_text()
    sha = re.search(r"\.git@([0-9a-f]{40})\"", pyproject).group(1)  # type: ignore[union-attr]
    files = _with(
        files, "scripts/setup.sh", "git+https://x/audittrace-object-storage.git@v0.1.1"
    )
    assert any("'v0.1.1'" in v for v in vcs_violations(files, pyproject))
    bad_pyproject = pyproject.replace(sha, "v0.1.1")
    assert vcs_violations(files, bad_pyproject)


def test_n7b_a_dockerfile_sha_differing_from_pyproject_is_red() -> None:
    files = tracked_text_files()
    other = "0" * 40
    mutated = _with(
        files,
        "Dockerfile",
        f"RUN pip install git+https://x/audittrace-object-storage.git@{other}",
    )
    assert any(
        "Dockerfile" in v
        for v in vcs_violations(mutated, (ROOT / "pyproject.toml").read_text())
    )


def test_the_sha_variable_is_only_trusted_where_pyproject_is_read() -> None:
    files = {"x.sh": "pip install audittrace-object-storage.git@${SHA}"}
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert vcs_violations(files, pyproject)  # no `sed -n` + pyproject.toml in the file


# ───────────────────── security-lint resolves ONLY the gate venv (C7) ─────────────────────


def test_security_lint_fails_closed_without_the_gate_venv(tmp_path: Path) -> None:
    """A semgrep on PATH (pipx, a stray pip) is unhashed and NOT accepted: with
    no gate venv the target exits non-zero and says how to build it, even when
    a `semgrep` is first on PATH."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sentinel = tmp_path / "path-semgrep-ran"
    (fake_bin / "semgrep").write_text(f'#!/bin/sh\ntouch "{sentinel}"\n')
    (fake_bin / "semgrep").chmod(0o755)
    env = hermetic_env({"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp_path)})
    proc = subprocess.run(
        ["make", "security-lint", f"GATE_VENV={tmp_path / 'no-such-venv'}"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "make gate-venv" in proc.stdout + proc.stderr
    assert not sentinel.exists()


def test_security_lint_uses_the_gate_venv_semgrep(tmp_path: Path) -> None:
    venv_bin = tmp_path / "gate" / "bin"
    venv_bin.mkdir(parents=True)
    log = tmp_path / "argv.log"
    (venv_bin / "semgrep").write_text(f'#!/bin/sh\necho "$@" > "{log}"\n')
    (venv_bin / "semgrep").chmod(0o755)
    proc = subprocess.run(
        ["make", "security-lint", f"GATE_VENV={tmp_path / 'gate'}"],
        cwd=ROOT,
        env=hermetic_env({"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--config ci/semgrep/rules.yml" in log.read_text()


def test_the_gate_venv_is_never_put_on_the_ci_path() -> None:
    """GITHUB_PATH PREPENDS: the gate venv's python/pip would shadow the runner
    python that G5 checks against the dev lock (found in the CI-shaped AC-2 run:
    run-tests.sh resolved `python` to the gate venv and refused the suite)."""
    for rel, text in tracked_text_files().items():
        if rel.startswith(".github/workflows/"):
            assert "gate-venv" not in "".join(
                ln for ln in text.splitlines() if "GITHUB_PATH" in ln
            ), rel
