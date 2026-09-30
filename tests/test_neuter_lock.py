"""Unit tests for ``scripts/neuter/lock.py`` (SPEC v3 §10; SF-2, SF-3, SF-E)."""

from __future__ import annotations

import fcntl
import os

import pytest

from scripts.neuter import lock as lockmod


def test_resolve_lock_path_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDITTRACE_NEUTER_LOCK", str(tmp_path / "env.lock"))
    assert (
        lockmod.resolve_lock_path(str(tmp_path / "cli.lock")) == tmp_path / "cli.lock"
    )


def test_resolve_lock_path_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDITTRACE_NEUTER_LOCK", str(tmp_path / "env.lock"))
    assert lockmod.resolve_lock_path() == tmp_path / "env.lock"


def test_resolve_lock_path_xdg_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("AUDITTRACE_NEUTER_LOCK", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert lockmod.resolve_lock_path() == tmp_path / lockmod.LOCK_FILENAME


def test_resolve_lock_path_tempdir_fallback(monkeypatch):
    monkeypatch.delenv("AUDITTRACE_NEUTER_LOCK", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    import tempfile

    assert (
        lockmod.resolve_lock_path()
        == __import__("pathlib").Path(tempfile.gettempdir()) / lockmod.LOCK_FILENAME
    )


def test_open_lock_file_is_cloexec(tmp_path):
    path = tmp_path / "sub" / "pool.lock"  # exercises parent.mkdir
    fd = lockmod.open_lock_file(path)
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        assert flags & fcntl.FD_CLOEXEC
        assert path.exists()
    finally:
        os.close(fd)


def test_open_lock_file_is_the_only_creator(tmp_path):
    path = tmp_path / "pool.lock"
    assert not path.exists()
    fd = lockmod.open_lock_file(path)
    os.close(fd)
    assert path.exists()


def test_open_existing_lock_file_absent_returns_none(tmp_path):
    assert lockmod.open_existing_lock_file(tmp_path / "missing.lock") is None


def test_open_existing_lock_file_never_creates(tmp_path):
    path = tmp_path / "missing.lock"
    lockmod.open_existing_lock_file(path)
    assert not path.exists()


def test_open_existing_lock_file_unreadable_returns_none_not_raise(tmp_path):
    path = tmp_path / "unreadable.lock"
    path.touch()
    path.chmod(0o000)
    try:
        if os.geteuid() == 0:
            pytest.skip("root ignores file permission bits")
        assert lockmod.open_existing_lock_file(path) is None
    finally:
        path.chmod(0o644)


def test_try_flock_exclusive_then_shared_blocks(tmp_path):
    path = tmp_path / "pool.lock"
    fd1 = lockmod.open_lock_file(path)
    lockmod.try_flock(fd1, fcntl.LOCK_EX)
    fd2 = lockmod.open_lock_file(path)
    try:
        with pytest.raises(lockmod.LockHeldError):
            lockmod.try_flock(fd2, fcntl.LOCK_SH)
    finally:
        os.close(fd1)
        os.close(fd2)


def test_try_flock_shared_then_exclusive_blocks(tmp_path):
    path = tmp_path / "pool.lock"
    fd1 = lockmod.open_lock_file(path)
    lockmod.try_flock(fd1, fcntl.LOCK_SH)
    fd2 = lockmod.open_lock_file(path)
    try:
        with pytest.raises(lockmod.LockHeldError):
            lockmod.try_flock(fd2, fcntl.LOCK_EX)
    finally:
        os.close(fd1)
        os.close(fd2)


def test_try_flock_succeeds_after_release(tmp_path):
    path = tmp_path / "pool.lock"
    fd1 = lockmod.open_lock_file(path)
    lockmod.try_flock(fd1, fcntl.LOCK_EX)
    os.close(fd1)  # kernel releases on close
    fd2 = lockmod.open_lock_file(path)
    try:
        lockmod.try_flock(fd2, fcntl.LOCK_EX)  # does not raise
    finally:
        os.close(fd2)


def test_foreign_docker_build_running_false_when_none(monkeypatch):
    assert lockmod.foreign_docker_build_running() is False


def test_foreign_docker_build_running_detects_cmdline(tmp_path, monkeypatch):
    fake_proc = tmp_path / "999999"
    fake_proc.mkdir()
    (fake_proc / "cmdline").write_bytes(b"docker\x00build\x00.\x00")

    real_glob = __import__("pathlib").Path.glob

    def fake_glob(self, pattern):
        if str(self) == "/proc" and pattern == "[0-9]*":
            return iter([fake_proc])
        return real_glob(self, pattern)

    monkeypatch.setattr(__import__("pathlib").Path, "glob", fake_glob)
    assert lockmod.foreign_docker_build_running() is True
