"""Issue #368: the ``neuter_pathcheck`` plugin loads through a ``pytest11``
entry point, so a from-scratch environment (no ``PYTEST_PLUGINS``) cannot
skip the chokepoint, while an ordinary run stays untouched.

Each guard below names the neuter that must turn it RED (see the PR body).
The subprocess tests build the CHILD environment from scratch (PATH, HOME and
a private TMPDIR only -- no ``PYTEST_PLUGINS``, no ``NEUTER_*``, no
``AUDITTRACE_NEUTER_LOCK``) and start a real ``python -m pytest``.
"""

from __future__ import annotations

import fcntl
import importlib.metadata
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import neuter_pathcheck_entry
from scripts.neuter import lock as lockmod
from scripts.neuter import neuter_pathcheck

REPO_ROOT = Path(__file__).resolve().parent.parent


def _scratch_project(tmp_path: Path) -> Path:
    """An empty dir with one passing test and NO pytest ini (no addopts)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    return proj


def _stripped_env(tmp_path: Path) -> dict[str, str]:
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "TMPDIR": str(tmpdir),
    }
    # The suite's hermetic spawn guard (tests/conftest.py) rejects any child
    # whose KUBECONFIG differs from its sentinel; carry it through. It is
    # irrelevant to the plugin under test.
    if "KUBECONFIG" in os.environ:
        env["KUBECONFIG"] = os.environ["KUBECONFIG"]
    return env


def _run_stripped(tmp_path: Path, extra_env: dict[str, str] | None = None):
    env = _stripped_env(tmp_path)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "test_ok.py"],
        cwd=_scratch_project(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _lock_path(tmp_path: Path) -> Path:
    return Path(_stripped_env(tmp_path)["TMPDIR"]) / lockmod.LOCK_FILENAME


def _hold(path: Path, mode: int) -> int:
    fd = lockmod.open_lock_file(path)
    fcntl.flock(fd, mode | fcntl.LOCK_NB)
    return fd


# --- packaging: the declaration and the installed metadata ----------------


def test_pyproject_declares_the_pytest11_entry_point():
    """Neuter N1: delete the ``[project.entry-points.pytest11]`` table."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert data["project"]["entry-points"]["pytest11"] == {
        "neuter_pathcheck": "neuter_pathcheck_entry"
    }
    assert "neuter_pathcheck_entry" in data["tool"]["setuptools"]["py-modules"]


def test_installed_metadata_registers_the_entry_point():
    """Neuter N2: remove the line from the venv's ``entry_points.txt``."""
    eps = importlib.metadata.entry_points(group="pytest11")
    ours = [ep for ep in eps if ep.name == "neuter_pathcheck"]
    assert [ep.value for ep in ours] == ["neuter_pathcheck_entry"]
    assert ours[0].load() is neuter_pathcheck_entry


# --- behaviour in a from-scratch environment ------------------------------


def test_stripped_env_run_is_refused_while_a_pool_holds_the_lock(tmp_path):
    """The #368 acceptance test. The pool holds LOCK_EX; the child has no
    PYTEST_PLUGINS and no token, yet the plugin loads (entry point) and
    refuses. Neuters: N2 (entry point removed -> child passes, rc 0);
    N3 (``enforcement_active`` ignores the lock -> rc 0)."""
    fd = _hold(_lock_path(tmp_path), fcntl.LOCK_EX)
    try:
        result = _run_stripped(tmp_path)
    finally:
        os.close(fd)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "neuter chokepoint" in result.stdout + result.stderr


def test_stripped_env_run_is_untouched_when_no_pool_is_active(tmp_path):
    """Ordinary ``make test`` / IDE runs load the plugin and must pass.
    Neuter N4: make ``enforcement_active`` return True unconditionally."""
    result = _run_stripped(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_shared_lock_is_not_a_pool(tmp_path):
    """``make test`` holds LOCK_SH via hold-shared; that is not a pool.
    Neuter N5: probe with LOCK_EX instead of LOCK_SH (shared reads active)."""
    fd = _hold(_lock_path(tmp_path), fcntl.LOCK_SH)
    try:
        result = _run_stripped(tmp_path)
    finally:
        os.close(fd)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_chokepoint_claim_without_a_valid_token_is_refused_without_a_pool(tmp_path):
    """A half-set claim is never relaxed. Neuter N6: empty ``_CLAIM_VARS``."""
    result = _run_stripped(tmp_path, {"NEUTER_CHOKEPOINT_TOKEN": "forged"})
    assert result.returncode != 0, result.stdout + result.stderr
    assert "neuter chokepoint" in result.stdout + result.stderr


def test_explicit_skip_opt_out_still_works_during_a_pool(tmp_path):
    """The one affirmative opt-out is unchanged."""
    fd = _hold(_lock_path(tmp_path), fcntl.LOCK_EX)
    try:
        result = _run_stripped(tmp_path, {"NEUTER_CHOKEPOINT_SKIP": "1"})
    finally:
        os.close(fd)
    assert result.returncode == 0, result.stdout + result.stderr


# --- in-process units ------------------------------------------------------


def test_lock_constants_match_the_harness_lock_module():
    """The plugin cannot import ``scripts``; the copies must not drift.
    Neuter N7: change ``_LOCK_FILENAME`` in the plugin."""
    assert neuter_pathcheck._LOCK_FILENAME == lockmod.LOCK_FILENAME
    assert neuter_pathcheck._ENV_LOCK_PATH == lockmod.ENV_LOCK_PATH


def test_candidate_paths_cover_the_stripped_env_locations(monkeypatch, tmp_path):
    """A stripped env loses XDG/override, so the per-user runtime dir and the
    tempdir default must be probed regardless. Neuter N8: drop the
    ``/run/user/<uid>`` candidate."""
    monkeypatch.delenv("AUDITTRACE_NEUTER_LOCK", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(neuter_pathcheck.tempfile, "tempdir", str(tmp_path))
    paths = neuter_pathcheck._candidate_lock_paths()
    assert Path(f"/run/user/{os.getuid()}") / lockmod.LOCK_FILENAME in paths
    assert tmp_path / lockmod.LOCK_FILENAME in paths


def test_candidate_paths_include_env_derived_and_are_deduplicated(
    monkeypatch, tmp_path
):
    override = tmp_path / "custom.lock"
    monkeypatch.setenv("AUDITTRACE_NEUTER_LOCK", str(override))
    monkeypatch.setenv("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    paths = neuter_pathcheck._candidate_lock_paths()
    assert paths[0] == override
    assert len(paths) == len(set(paths))


def test_enforcement_active_sees_an_exclusive_holder_via_the_env_override(
    monkeypatch, tmp_path
):
    """Neuter N3 as well: ignoring the lock leaves only the claim vars."""
    lock_file = tmp_path / "pool.lock"
    monkeypatch.setenv("AUDITTRACE_NEUTER_LOCK", str(lock_file))
    for var in neuter_pathcheck._CLAIM_VARS:
        monkeypatch.delenv(var, raising=False)
    assert neuter_pathcheck.enforcement_active() is False
    fd = _hold(lock_file, fcntl.LOCK_EX)
    try:
        assert neuter_pathcheck.enforcement_active() is True
    finally:
        os.close(fd)
    assert neuter_pathcheck.enforcement_active() is False


def test_probe_is_false_for_a_missing_file_and_never_creates_it(tmp_path):
    missing = tmp_path / "nope.lock"
    assert neuter_pathcheck._pool_lock_held_exclusive(missing) is False
    assert not missing.exists()


def test_probe_is_false_when_flock_errors(monkeypatch, tmp_path):
    lock_file = tmp_path / "x.lock"
    lock_file.write_text("")

    def boom(fd, op):
        raise OSError("flock unsupported")

    monkeypatch.setattr(neuter_pathcheck.fcntl, "flock", boom)
    assert neuter_pathcheck._pool_lock_held_exclusive(lock_file) is False


def test_pytest_configure_enforces_when_a_pool_is_active_in_process(
    monkeypatch, tmp_path
):
    lock_file = tmp_path / "pool.lock"
    monkeypatch.setenv("AUDITTRACE_NEUTER_LOCK", str(lock_file))
    monkeypatch.delenv("NEUTER_CHOKEPOINT_SKIP", raising=False)
    for var in neuter_pathcheck._CLAIM_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("NEUTER_PATHCHECK_EXPECT", raising=False)
    fd = _hold(lock_file, fcntl.LOCK_EX)
    try:
        with pytest.raises(pytest.exit.Exception) as exc_info:
            neuter_pathcheck.pytest_configure(config=None)
    finally:
        os.close(fd)
    assert "no valid NEUTER_CHOKEPOINT_TOKEN/_FILE" in str(exc_info.value)


def test_pytest_configure_is_inert_with_no_claim_and_no_pool(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDITTRACE_NEUTER_LOCK", str(tmp_path / "absent.lock"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(neuter_pathcheck.tempfile, "tempdir", str(tmp_path))
    monkeypatch.delenv("NEUTER_CHOKEPOINT_SKIP", raising=False)
    for var in neuter_pathcheck._CLAIM_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("NEUTER_PATHCHECK_EXPECT", raising=False)
    neuter_pathcheck.pytest_configure(config=None)  # must not raise


# --- the entry-point shim --------------------------------------------------


def test_shim_delegates_to_the_real_plugin(monkeypatch):
    calls = []

    class Fake:
        @staticmethod
        def pytest_configure(config):
            calls.append(config)

    monkeypatch.setitem(sys.modules, "neuter_pathcheck", Fake)
    neuter_pathcheck_entry.pytest_configure("cfg")
    assert calls == ["cfg"]


def test_shim_loads_the_real_plugin_by_path_when_not_yet_imported(monkeypatch):
    monkeypatch.delitem(sys.modules, "neuter_pathcheck", raising=False)
    module = neuter_pathcheck_entry._load_real_plugin()
    assert module is not None
    assert Path(module.__file__).resolve() == (
        REPO_ROOT / "scripts" / "neuter" / "neuter_pathcheck.py"
    )
    assert sys.modules["neuter_pathcheck"] is module


def test_shim_is_inert_when_the_real_plugin_is_not_shipped(monkeypatch, tmp_path):
    monkeypatch.delitem(sys.modules, "neuter_pathcheck", raising=False)
    monkeypatch.setattr(neuter_pathcheck_entry, "_REAL_PLUGIN", tmp_path / "absent.py")
    assert neuter_pathcheck_entry._load_real_plugin() is None
    neuter_pathcheck_entry.pytest_configure("cfg")  # must not raise


def test_shim_module_level_code_is_measured_by_reloading_it():
    """The entry point imports the shim at pytest startup, before coverage
    starts, so its module-level statements would never be measured. Reloading
    re-executes them under coverage (and proves the import is side-effect
    free)."""
    import importlib

    reloaded = importlib.reload(neuter_pathcheck_entry)
    assert reloaded is neuter_pathcheck_entry
    assert callable(reloaded.pytest_configure)


def test_token_ok_without_a_marker_file_falls_through(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("good-token\n")
    monkeypatch.delenv("NEUTER_CHOKEPOINT_SKIP", raising=False)
    monkeypatch.setenv("NEUTER_CHOKEPOINT_TOKEN", "good-token")
    monkeypatch.setenv("NEUTER_CHOKEPOINT_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("NEUTER_CHOKEPOINT_MARKER_FILE", raising=False)
    monkeypatch.delenv("NEUTER_PATHCHECK_EXPECT", raising=False)
    neuter_pathcheck.pytest_configure(config=None)  # must not raise
