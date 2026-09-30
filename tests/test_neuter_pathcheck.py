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
    monkeypatch.delenv("NEUTER_PATHCHECK_EXPECT", raising=False)
    neuter_pathcheck.pytest_configure(
        config=None
    )  # must not raise, must not require LOG


def test_appends_log_line_when_module_resolves_under_expect(tmp_path, monkeypatch):
    log_path = tmp_path / "pathcheck.log"
    monkeypatch.setenv("NEUTER_PATHCHECK_MODULE", "os")
    monkeypatch.setenv("NEUTER_PATHCHECK_EXPECT", os.path.dirname(os.__file__))
    monkeypatch.setenv("NEUTER_PATHCHECK_LOG", str(log_path))

    neuter_pathcheck.pytest_configure(config=None)

    assert log_path.exists()
    line = log_path.read_text().strip()
    pid_str, path_str = line.split(" ", 1)
    assert int(pid_str) == os.getpid()
    assert path_str == os.path.realpath(os.__file__)


def test_assertion_fires_and_no_log_line_on_wrong_path(tmp_path, monkeypatch):
    log_path = tmp_path / "pathcheck.log"
    monkeypatch.setenv("NEUTER_PATHCHECK_MODULE", "os")
    monkeypatch.setenv("NEUTER_PATHCHECK_EXPECT", str(tmp_path / "nowhere"))
    monkeypatch.setenv("NEUTER_PATHCHECK_LOG", str(log_path))

    with pytest.raises(AssertionError):
        neuter_pathcheck.pytest_configure(config=None)

    assert not log_path.exists()


def test_env_var_names_never_start_with_the_products_own_prefix():
    """Any neuter TARGET repo may wipe ``AUDITTRACE_*`` env vars in its own
    ``tests/conftest.py`` (this repo does; so, historically, did an A2-era
    commit this harness's T1 oracle run replays) -- when that repo is
    itself the mapped test's own collection root, that wipe runs in the
    SAME subprocess as this plugin's ``pytest_configure``, before it ever
    reads its env vars. A prior revision named them
    ``AUDITTRACE_NEUTER_PATHCHECK_*`` and worked around it with an
    allowlist entry in THIS repo's own conftest.py -- which cannot help
    when the target is a DIFFERENT (or frozen, historical) repo with the
    identical wipe convention but no matching entry. Found by dogfooding
    against this repo first, then confirmed against the T1 oracle target
    (A11.1). The fix is structural, not an allowlist: these names must
    never start with the product's own env prefix, in any product."""
    module = neuter_pathcheck.__file__
    text = Path(module).read_text()
    for name in (
        "NEUTER_PATHCHECK_MODULE",
        "NEUTER_PATHCHECK_EXPECT",
        "NEUTER_PATHCHECK_LOG",
    ):
        assert f'"{name}"' in text
        assert not name.startswith("AUDITTRACE_")
