"""Unit tests for ``scripts/neuter/spec.py`` -- fail-closed neuter spec
loading (SPEC v3 §3, §5)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.neuter.spec import SpecLoadError, load_neuter_spec

PYTHON = sys.executable


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "mod.py").write_text("def f(x):\n    return x + 1\n")
    (r / "test_mod.py").write_text(
        "from mod import f\n\n\ndef test_f():\n    assert f(1) == 2\n"
    )
    _run_git(r, "init", "-q")
    _run_git(r, "config", "user.email", "a@b.c")
    _run_git(r, "config", "user.name", "a")
    _run_git(r, "add", "-A")
    _run_git(r, "commit", "-q", "-m", "init")
    return r


def _sha(repo: Path) -> str:
    return _run_git(repo, "rev-parse", "HEAD").stdout.strip()


def _valid_spec(repo: Path) -> dict:
    return {
        "schema": 3,
        "sha": _sha(repo),
        "scope_files": ["test_mod.py"],
        "guard_tests": [{"id": "test_mod.py::test_f", "row": "R1"}],
        "neuters": [
            {
                "id": "n1",
                "file": "mod.py",
                "edits": [{"old": "    return x + 1", "new": "    return x + 2"}],
                "tests": ["test_mod.py::test_f"],
                "engines": ["mock"],
                "guard": "G",
                "clause": "C",
            }
        ],
    }


def _write(path: Path, spec: dict) -> Path:
    p = path / "neuters.json"
    p.write_text(json.dumps(spec))
    return p


def test_load_valid_spec(tmp_path, repo):
    spec_path = _write(tmp_path, _valid_spec(repo))
    spec = load_neuter_spec(spec_path, repo_dir=repo, python=PYTHON)
    assert spec.sha == _sha(repo)
    assert len(spec.neuters) == 1
    assert spec.neuters[0].id == "n1"


def test_missing_file_exit_3(tmp_path, repo):
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(tmp_path / "missing.json", repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "unreadable"


def test_bad_json_exit_3(tmp_path, repo):
    p = tmp_path / "neuters.json"
    p.write_text("not json {{{")
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(p, repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "unreadable"


def test_wrong_schema(tmp_path, repo):
    d = _valid_spec(repo)
    d["schema"] = 2
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "schema"


def test_missing_sha(tmp_path, repo):
    d = _valid_spec(repo)
    del d["sha"]
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "sha"


def test_empty_scope_files(tmp_path, repo):
    d = _valid_spec(repo)
    d["scope_files"] = []
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "scope_files"


def test_empty_guard_tests(tmp_path, repo):
    d = _valid_spec(repo)
    d["guard_tests"] = []
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "guard_tests"


def test_empty_neuters(tmp_path, repo):
    d = _valid_spec(repo)
    d["neuters"] = []
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "neuters"


def test_missing_neuter_id(tmp_path, repo):
    d = _valid_spec(repo)
    del d["neuters"][0]["id"]
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "neuter_id"


def test_duplicate_neuter_id(tmp_path, repo):
    d = _valid_spec(repo)
    d["neuters"].append(dict(d["neuters"][0]))
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "duplicate_id"


def test_expect_not_red(tmp_path, repo):
    d = _valid_spec(repo)
    d["neuters"][0]["expect"] = "GREEN"
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "expect"


def test_missing_neuter_file(tmp_path, repo):
    d = _valid_spec(repo)
    del d["neuters"][0]["file"]
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "neuter_file"


def test_empty_edits(tmp_path, repo):
    d = _valid_spec(repo)
    d["neuters"][0]["edits"] = []
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "edits"


def test_old_equals_new(tmp_path, repo):
    d = _valid_spec(repo)
    d["neuters"][0]["edits"][0]["new"] = d["neuters"][0]["edits"][0]["old"]
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "old_eq_new"


def test_empty_tests(tmp_path, repo):
    d = _valid_spec(repo)
    d["neuters"][0]["tests"] = []
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "tests"


def test_unknown_engine(tmp_path, repo):
    d = _valid_spec(repo)
    d["neuters"][0]["engines"] = ["oracle"]
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "engines"


def test_untracked_file(tmp_path, repo):
    d = _valid_spec(repo)
    d["neuters"][0]["file"] = "does_not_exist.py"
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "untracked_file"


def test_zero_match(tmp_path, repo):
    d = _valid_spec(repo)
    d["neuters"][0]["edits"][0]["old"] = "nonexistent"
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "match_count"


def test_two_match(tmp_path, repo):
    d = _valid_spec(repo)
    d["neuters"][0]["edits"][0]["old"] = (
        "return"  # matches "return" once in mod.py... force 2 by editing file
    )
    (repo / "mod.py").write_text("def f(x):\n    return x + 1\n    return x + 1\n")
    subprocess.run(["git", "commit", "-am", "dup"], cwd=repo, capture_output=True)
    d["sha"] = _sha(repo)
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "match_count"


def test_guard_tests_entry_without_row(tmp_path, repo):
    d = _valid_spec(repo)
    del d["guard_tests"][0]["row"]
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "guard_tests_row"


def test_closure_mismatch(tmp_path, repo):
    d = _valid_spec(repo)
    d["guard_tests"].append({"id": "test_mod.py::test_unrelated", "row": "R2"})
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "closure"


def test_uncollected_test_id(tmp_path, repo):
    d = _valid_spec(repo)
    d["neuters"][0]["tests"] = ["test_mod.py::test_nonexistent"]
    d["guard_tests"] = [{"id": "test_mod.py::test_nonexistent", "row": "R1"}]
    with pytest.raises(SpecLoadError) as excinfo:
        load_neuter_spec(_write(tmp_path, d), repo_dir=repo, python=PYTHON)
    assert excinfo.value.reason == "uncollected"


def test_collected_ids_can_be_supplied_directly(tmp_path, repo):
    spec_path = _write(tmp_path, _valid_spec(repo))
    spec = load_neuter_spec(
        spec_path, repo_dir=repo, python=PYTHON, collected_ids={"test_mod.py::test_f"}
    )
    assert spec.neuters[0].id == "n1"
