"""``tests/_neuter_test_evidence.py``: the evidence dir must be unique per
pytest-xdist worker, because ``tmp_path.name`` repeats across workers."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests._neuter_test_evidence import ROOT, evidence_dir_for


def test_same_tmp_path_name_on_two_workers_gets_two_dirs(tmp_path, monkeypatch):
    """Neuter: drop the worker component from the path -> the dirs are equal and
    this goes RED (the cross-worker collision that corrupted report tests)."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    first = evidence_dir_for(tmp_path)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    second = evidence_dir_for(tmp_path)

    assert first != second
    assert first.is_dir() and second.is_dir()
    assert first.parts[-3] == "gw0" and second.parts[-3] == "gw1"


def test_without_xdist_the_worker_component_is_main(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)

    assert evidence_dir_for(tmp_path) == ROOT / "main" / tmp_path.name / "ev"


def test_create_false_does_not_create_anything(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw9")
    path = evidence_dir_for(tmp_path, "never", create=False)

    assert not path.exists()
    assert isinstance(path, Path)


def test_importing_conftest_outside_a_pytest_session_does_not_wipe_the_evidence_root(
    tmp_path,
):
    """Several tests start a fresh interpreter that imports ``tests.conftest``.
    The stale-evidence cleanup used to run at import, so each such child wiped
    the evidence directories of every other test running at that moment (a
    FileNotFoundError under pytest-xdist). It now runs only when a pytest
    session starts, on the controller / serial process.
    Neuter: call the cleanup at module level again -> the marker vanishes, RED."""
    marker_dir = ROOT / f"conftest-import-marker-{os.getpid()}"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / "marker"
    marker.write_text("x")
    try:
        subprocess.run(
            [sys.executable, "-c", "import tests.conftest"],
            cwd=Path(__file__).resolve().parent.parent,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=True,
            capture_output=True,
            timeout=120,
        )
        assert marker.exists()
    finally:
        shutil.rmtree(marker_dir, ignore_errors=True)
