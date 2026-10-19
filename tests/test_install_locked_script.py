"""``scripts/install-locked.sh`` — the one install chokepoint (DEP-LOCK-AT C1/C4).

Runs the REAL script in a scratch root with a FAKE ``pip`` that logs argv and
the content of every ``-r`` file at call time, so the ORDER and FLAGS of the
steps are asserted offline: pip first from its own hashed block (never
``--upgrade pip``), then the whole lock, then ``-e .`` and the VCS package at
the SHA READ from pyproject.toml, all ``--no-deps --no-build-isolation``.
The real hashed install is live evidence (AC-1), not a unit test.

Neuters: drop the pip bootstrap -> test_pip_is_bootstrapped_first_from_its_own_
hashed_block RED; hard-code the SHA in the script -> test_the_vcs_sha_is_read_
from_pyproject RED; drop --no-build-isolation -> test_every_source_build_...RED.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests._hermetic_env import hermetic_env

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "install-locked.sh"
SHA = "a" * 40
LOCK = """\
# header
fastapi==0.146.0 \\
    --hash=sha256:ff
    # via x
pip==26.2.1 \\
    --hash=sha256:p1 \\
    --hash=sha256:p2
setuptools==84.0.0 \\
    --hash=sha256:ss
uv==0.13.1 \\
    --hash=sha256:uu
"""
PYPROJECT = (
    "dependencies = [\n"
    f'  "audittrace-object-storage @ git+https://github.com/o/audittrace-object-storage.git@{SHA}",\n'
    "]\n"
)
FAKE_PIP = """#!/usr/bin/env bash
echo "CALL $*" >> "$FAKE_LOG"
prev=""
for a in "$@"; do
  if [ "$prev" = "-r" ]; then
    echo "FILE-BEGIN $a" >> "$FAKE_LOG"; cat "$a" >> "$FAKE_LOG"; echo "FILE-END" >> "$FAKE_LOG"
  fi
  prev="$a"
done
"""


def _scratch(tmp_path: Path, lock: str = LOCK, pyproject: str = PYPROJECT) -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "install-locked.sh")
    (root / "requirements-dev.txt").write_text(lock)
    (root / "requirements-gate.txt").write_text(lock)
    (root / "pyproject.toml").write_text(pyproject)
    return root


def _run(
    tmp_path: Path, root: Path, *args: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    pip = tmp_path / "fakepip"
    pip.write_text(FAKE_PIP)
    pip.chmod(pip.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "pip.log"
    log.write_text("")
    env = hermetic_env(
        {
            "PATH": "/usr/bin:/bin",
            "HOME": os.environ.get("HOME", "/tmp"),
            "FAKE_LOG": str(log),
        }
    )
    result = subprocess.run(
        ["bash", str(root / "scripts" / "install-locked.sh"), str(pip), *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, log.read_text().splitlines()


def _calls(lines: list[str]) -> list[str]:
    return [ln.removeprefix("CALL ") for ln in lines if ln.startswith("CALL ")]


def test_pip_is_bootstrapped_first_from_its_own_hashed_block(tmp_path: Path) -> None:
    root = _scratch(tmp_path)
    result, lines = _run(tmp_path, root)
    assert result.returncode == 0, result.stderr
    first = _calls(lines)[0]
    assert (
        "--require-hashes" in first
        and "--only-binary=:all:" in first
        and "-r " in first
    )
    assert "--upgrade" not in first
    begin = lines.index(next(ln for ln in lines if ln.startswith("FILE-BEGIN")))
    block = lines[begin + 1 : lines.index("FILE-END", begin)]
    assert block[0].startswith("pip==26.2.1") and len(block) == 3  # pin + 2 hashes only
    assert not any("fastapi" in ln or "setuptools" in ln for ln in block)


def test_the_whole_lock_is_installed_hashed_and_wheels_only_second(
    tmp_path: Path,
) -> None:
    result, lines = _run(tmp_path, _scratch(tmp_path))
    assert result.returncode == 0
    second = _calls(lines)[1]
    assert second.startswith(
        "install --require-hashes --only-binary=:all: -r requirements-dev.txt"
    )


def test_every_source_build_is_no_deps_and_no_build_isolation(tmp_path: Path) -> None:
    result, lines = _run(tmp_path, _scratch(tmp_path))
    assert result.returncode == 0
    calls = _calls(lines)
    assert len(calls) == 4
    editable, vcs = calls[2], calls[3]
    assert editable == "install --no-deps --no-build-isolation -e ."
    assert vcs.startswith("install --no-deps --no-build-isolation ")


def test_the_vcs_sha_is_read_from_pyproject(tmp_path: Path) -> None:
    root = _scratch(tmp_path)
    result, lines = _run(tmp_path, root)
    assert result.returncode == 0
    assert _calls(lines)[3].endswith(f"audittrace-object-storage.git@{SHA}")
    other = "b" * 40
    root2 = _scratch(tmp_path / "second", pyproject=PYPROJECT.replace(SHA, other))
    result2, lines2 = _run(tmp_path / "second", root2)
    assert _calls(lines2)[3].endswith(f"@{other}")


def test_a_pyproject_without_a_40_hex_sha_fails_closed(tmp_path: Path) -> None:
    root = _scratch(tmp_path, pyproject=PYPROJECT.replace(SHA, "v0.1.1"))
    result, lines = _run(tmp_path, root)
    assert result.returncode == 1
    assert "no 40-hex" in result.stderr
    assert len(_calls(lines)) == 3  # nothing for the VCS package ran


def test_a_lock_without_a_hashed_pip_line_fails_closed_before_any_install(
    tmp_path: Path,
) -> None:
    lock = "\n".join(
        ln for ln in LOCK.splitlines() if "pip" not in ln and "p1" not in ln
    )
    result, lines = _run(tmp_path, _scratch(tmp_path, lock=lock))
    assert result.returncode == 1
    assert "pip==" in result.stderr
    assert _calls(lines) == []


def test_gate_mode_installs_only_the_gate_lock(tmp_path: Path) -> None:
    result, lines = _run(tmp_path, _scratch(tmp_path), "gate")
    assert result.returncode == 0
    calls = _calls(lines)
    assert len(calls) == 2 and "requirements-gate.txt" in calls[1]
    assert not any("-e ." in c or "git+" in c for c in calls)


def test_uv_only_mode_installs_just_the_hashed_uv_block(tmp_path: Path) -> None:
    result, lines = _run(tmp_path, _scratch(tmp_path), "uv-only")
    assert result.returncode == 0
    calls = _calls(lines)
    assert len(calls) == 2 and all("--require-hashes" in c for c in calls)
    uv_begin = [i for i, ln in enumerate(lines) if ln.startswith("FILE-BEGIN")][1]
    assert lines[uv_begin + 1].startswith("uv==0.13.1")


def test_uv_only_mode_without_a_hashed_uv_line_fails_closed(tmp_path: Path) -> None:
    lock = "\n".join(
        ln for ln in LOCK.splitlines() if "uv" not in ln and "sha256:uu" not in ln
    )
    result, _ = _run(tmp_path, _scratch(tmp_path, lock=lock), "uv-only")
    assert result.returncode == 1 and "uv==" in result.stderr


def test_an_unknown_mode_is_a_usage_error(tmp_path: Path) -> None:
    result, lines = _run(tmp_path, _scratch(tmp_path), "everything")
    assert result.returncode == 2 and _calls(lines) == []


@pytest.mark.parametrize("flag", ["--upgrade", "-U"])
def test_the_script_never_upgrades_pip(flag: str) -> None:
    text = SCRIPT.read_text()
    assert f"pip install {flag}" not in text and "install --upgrade" not in text
