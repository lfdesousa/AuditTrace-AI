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


def _clear_chokepoint_env(monkeypatch):
    """These tests exercise the PATH-CHECK half of ``pytest_configure``, not
    the chokepoint-totality guard added in review round 3 -- which is
    STICKY process-wide the moment ``scripts.neuter.pytest_run`` is
    imported (by other files in this same collection), so by the time this
    module's tests EXECUTE, ``NEUTER_CHOKEPOINT_REQUIRED`` is already set in
    ``os.environ`` even though this direct, in-process call was never
    routed through ``run_pytest()``. Without clearing it here, every test
    below raises ``pytest.exit()`` (``_pytest.outcomes.Exit``), which
    pytest treats as "abort the whole session", not "fail this test" --
    found live: it silently truncated this file's own suite (and would have
    truncated ``make test``) after exactly 42 unrelated tests, with no
    per-test failure ever recorded."""
    monkeypatch.delenv("NEUTER_CHOKEPOINT_REQUIRED", raising=False)
    monkeypatch.delenv("NEUTER_CHOKEPOINT_TOKEN", raising=False)
    monkeypatch.delenv("NEUTER_CHOKEPOINT_TOKEN_FILE", raising=False)


def test_noop_when_expect_unset(monkeypatch):
    _clear_chokepoint_env(monkeypatch)
    monkeypatch.delenv("NEUTER_PATHCHECK_EXPECT", raising=False)
    neuter_pathcheck.pytest_configure(
        config=None
    )  # must not raise, must not require LOG


def test_appends_log_line_when_module_resolves_under_expect(tmp_path, monkeypatch):
    _clear_chokepoint_env(monkeypatch)
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
    _clear_chokepoint_env(monkeypatch)
    log_path = tmp_path / "pathcheck.log"
    monkeypatch.setenv("NEUTER_PATHCHECK_MODULE", "os")
    monkeypatch.setenv("NEUTER_PATHCHECK_EXPECT", str(tmp_path / "nowhere"))
    monkeypatch.setenv("NEUTER_PATHCHECK_LOG", str(log_path))

    with pytest.raises(AssertionError):
        neuter_pathcheck.pytest_configure(config=None)

    assert not log_path.exists()


def test_chokepoint_required_but_token_missing_exits(monkeypatch):
    monkeypatch.setenv("NEUTER_CHOKEPOINT_REQUIRED", "1")
    monkeypatch.delenv("NEUTER_CHOKEPOINT_TOKEN", raising=False)
    monkeypatch.delenv("NEUTER_CHOKEPOINT_TOKEN_FILE", raising=False)
    monkeypatch.delenv("NEUTER_PATHCHECK_EXPECT", raising=False)

    with pytest.raises(pytest.exit.Exception) as exc_info:
        neuter_pathcheck.pytest_configure(config=None)
    assert "NEUTER_CHOKEPOINT_TOKEN/_FILE is missing" in str(exc_info.value)


def test_chokepoint_required_but_token_file_unreadable_exits(tmp_path, monkeypatch):
    monkeypatch.setenv("NEUTER_CHOKEPOINT_REQUIRED", "1")
    monkeypatch.setenv("NEUTER_CHOKEPOINT_TOKEN", "sometoken")
    monkeypatch.setenv("NEUTER_CHOKEPOINT_TOKEN_FILE", str(tmp_path / "does-not-exist"))
    monkeypatch.delenv("NEUTER_PATHCHECK_EXPECT", raising=False)

    with pytest.raises(pytest.exit.Exception) as exc_info:
        neuter_pathcheck.pytest_configure(config=None)
    assert "unreadable" in str(exc_info.value)


def test_chokepoint_required_but_token_mismatch_exits(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("expected-token\n")
    monkeypatch.setenv("NEUTER_CHOKEPOINT_REQUIRED", "1")
    monkeypatch.setenv("NEUTER_CHOKEPOINT_TOKEN", "wrong-token")
    monkeypatch.setenv("NEUTER_CHOKEPOINT_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("NEUTER_PATHCHECK_EXPECT", raising=False)

    with pytest.raises(pytest.exit.Exception) as exc_info:
        neuter_pathcheck.pytest_configure(config=None)
    assert "does not match" in str(exc_info.value)


def test_chokepoint_required_and_token_matches_falls_through(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("good-token\n")
    monkeypatch.setenv("NEUTER_CHOKEPOINT_REQUIRED", "1")
    monkeypatch.setenv("NEUTER_CHOKEPOINT_TOKEN", "good-token")
    monkeypatch.setenv("NEUTER_CHOKEPOINT_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("NEUTER_PATHCHECK_EXPECT", raising=False)

    neuter_pathcheck.pytest_configure(config=None)  # must not raise


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
