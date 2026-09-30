"""Unit tests for ``scripts/neuter/neuter_pathcheck.py`` (SPEC v3 §6 P4).

Calls ``pytest_configure`` directly (a real pytest run already exercises
this plugin behaviourally in ``tests/test_neuter_self_proofs.py`` proof d,
but that runs in a subprocess, invisible to this process's coverage
measurement -- this file gives it direct, in-process coverage too).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.neuter import neuter_pathcheck


def test_noop_when_expect_unset(monkeypatch):
    monkeypatch.delenv("AUDITTRACE_NEUTER_PATHCHECK_EXPECT", raising=False)
    neuter_pathcheck.pytest_configure(
        config=None
    )  # must not raise, must not require LOG


def test_appends_log_line_when_module_resolves_under_expect(tmp_path, monkeypatch):
    log_path = tmp_path / "pathcheck.log"
    monkeypatch.setenv("AUDITTRACE_NEUTER_PATHCHECK_MODULE", "os")
    monkeypatch.setenv(
        "AUDITTRACE_NEUTER_PATHCHECK_EXPECT", os.path.dirname(os.__file__)
    )
    monkeypatch.setenv("AUDITTRACE_NEUTER_PATHCHECK_LOG", str(log_path))

    neuter_pathcheck.pytest_configure(config=None)

    assert log_path.exists()
    line = log_path.read_text().strip()
    pid_str, path_str = line.split(" ", 1)
    assert int(pid_str) == os.getpid()
    assert path_str == os.path.realpath(os.__file__)


def test_assertion_fires_and_no_log_line_on_wrong_path(tmp_path, monkeypatch):
    log_path = tmp_path / "pathcheck.log"
    monkeypatch.setenv("AUDITTRACE_NEUTER_PATHCHECK_MODULE", "os")
    monkeypatch.setenv("AUDITTRACE_NEUTER_PATHCHECK_EXPECT", str(tmp_path / "nowhere"))
    monkeypatch.setenv("AUDITTRACE_NEUTER_PATHCHECK_LOG", str(log_path))

    with pytest.raises(AssertionError):
        neuter_pathcheck.pytest_configure(config=None)

    assert not log_path.exists()


def test_env_vars_survive_this_repos_own_env_wipe():
    """When THIS repo is itself the neuter target (dogfooding, A11.1), the
    mapped test's own ``tests/conftest.py`` import runs in the same
    subprocess as this plugin's ``pytest_configure`` and wipes every
    ``AUDITTRACE_*`` var not explicitly allow-listed -- found by dogfooding
    the harness against this repo. All four of this plugin's env vars (plus
    the fake-pg one it hands off to) must be allow-listed there, or the
    plugin silently no-ops in exactly the run it's meant to prove itself in."""
    conftest_text = (Path(__file__).parent / "conftest.py").read_text()
    for name in (
        "AUDITTRACE_NEUTER_PATHCHECK_MODULE",
        "AUDITTRACE_NEUTER_PATHCHECK_EXPECT",
        "AUDITTRACE_NEUTER_PATHCHECK_LOG",
        "AUDITTRACE_NEUTER_FAKE_PG_STATE",
    ):
        assert f'"{name}"' in conftest_text, (
            f"{name} missing from tests/conftest.py's allowlist"
        )
