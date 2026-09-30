"""Unit tests for ``scripts/neuter/neuter_pathcheck.py`` (SPEC v3 §6 P4).

Calls ``pytest_configure`` directly (a real pytest run already exercises
this plugin behaviourally in ``tests/test_neuter_self_proofs.py`` proof d,
but that runs in a subprocess, invisible to this process's coverage
measurement -- this file gives it direct, in-process coverage too).
"""

from __future__ import annotations

import os

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
