"""``tests/_neuter_test_evidence.py``: the evidence dir must be unique per
pytest-xdist worker, because ``tmp_path.name`` repeats across workers."""

from __future__ import annotations

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
