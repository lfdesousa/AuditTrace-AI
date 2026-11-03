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


def _fake_proc(root, pid, argv):
    """Create ``<root>/<pid>/cmdline`` with NUL-separated ``argv`` (bytes)."""
    d = root / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(b"\x00".join(argv) + b"\x00" if argv else b"")
    return d


def test_foreign_docker_build_running_empty_proc_is_false(tmp_path):
    assert lockmod.foreign_docker_build_running(tmp_path) is False


def test_foreign_docker_build_running_default_root_is_real_proc():
    # Production callers pass no argument: the seam default must stay /proc.
    import inspect

    default = (
        inspect.signature(lockmod.foreign_docker_build_running)
        .parameters["proc_root"]
        .default
    )
    assert default == __import__("pathlib").Path("/proc")


def test_foreign_docker_build_running_detects_docker_build(tmp_path):
    _fake_proc(tmp_path, 999991, [b"docker", b"build", b"."])
    assert lockmod.foreign_docker_build_running(tmp_path) is True


def test_foreign_docker_build_running_detects_buildx_exe_with_buildx_arg(tmp_path):
    _fake_proc(tmp_path, 999992, [b"/usr/bin/docker-buildx", b"buildx", b"bake"])
    assert lockmod.foreign_docker_build_running(tmp_path) is True


def test_foreign_docker_build_running_ignores_non_numeric_entry(tmp_path):
    # "12abc" matches the "[0-9]*" glob but int() raises ValueError -> skipped,
    # even though its cmdline is a genuine docker build.
    _fake_proc(tmp_path, "12abc", [b"docker", b"build", b"."])
    assert lockmod.foreign_docker_build_running(tmp_path) is False


def test_foreign_docker_build_running_excludes_own_pid(tmp_path):
    _fake_proc(tmp_path, os.getpid(), [b"docker", b"build", b"."])
    assert lockmod.foreign_docker_build_running(tmp_path) is False


def test_foreign_docker_build_running_unreadable_cmdline_is_skipped(tmp_path):
    # A directory named "cmdline" makes read_bytes() raise IsADirectoryError
    # (an OSError) for ANY uid, root included -- no permission games.
    (tmp_path / "999993").mkdir()
    (tmp_path / "999993" / "cmdline").mkdir()
    assert lockmod.foreign_docker_build_running(tmp_path) is False


def test_foreign_docker_build_running_missing_cmdline_is_skipped(tmp_path):
    (tmp_path / "999994").mkdir()  # process vanished: no cmdline file
    assert lockmod.foreign_docker_build_running(tmp_path) is False


def test_foreign_docker_build_running_empty_argv_is_skipped(tmp_path):
    _fake_proc(tmp_path, 999995, [])  # zombie / kernel thread: empty cmdline
    assert lockmod.foreign_docker_build_running(tmp_path) is False


def test_foreign_docker_build_running_non_docker_exe_is_ignored(tmp_path):
    _fake_proc(tmp_path, 999996, [b"podman", b"build", b"."])
    assert lockmod.foreign_docker_build_running(tmp_path) is False


def test_foreign_docker_build_running_docker_without_build_is_ignored(tmp_path):
    _fake_proc(tmp_path, 999997, [b"docker", b"ps", b"-a"])
    assert lockmod.foreign_docker_build_running(tmp_path) is False


def test_foreign_docker_build_running_substring_false_positive_is_ignored(tmp_path):
    # A sibling worker's pytest carries "docker" and "build" as SUBSTRINGS of
    # its test path; argv[0] is not docker, so this must NOT count.
    _fake_proc(
        tmp_path,
        999998,
        [b"python", b"-m", b"pytest", b"tests/test_docker_build_chart.py::test_x"],
    )
    assert lockmod.foreign_docker_build_running(tmp_path) is False


def test_foreign_docker_build_running_scans_past_skipped_entries(tmp_path):
    # Skips (`continue`) must not abort the scan: a real build AFTER several
    # skippable entries is still found.
    _fake_proc(tmp_path, "12abc", [b"x"])
    _fake_proc(tmp_path, os.getpid(), [b"docker", b"build"])
    _fake_proc(tmp_path, 999990, [])
    (tmp_path / "999989").mkdir()
    _fake_proc(tmp_path, 999999, [b"docker", b"build", b"-t", b"x", b"."])
    assert lockmod.foreign_docker_build_running(tmp_path) is True


def test_assert_lock_held_returns_when_another_fd_holds_the_lock(tmp_path):
    """The ``LockHeldError`` -> ``return`` path (lines 109-110).

    WHY this was host-only: on the dev host the heavy-cap lock is routinely
    held by a real concurrent run, so whatever other test touched it hit this
    path by accident; in a clean container nobody holds it, only the
    "not held" branch ran. Here a SECOND open file description holds
    LOCK_EX in-test -- flock is per open file description, so the probe's
    fresh fd must fail non-blockingly and the function returns None.
    """
    path = tmp_path / "pool.lock"
    holder = lockmod.open_lock_file(path)
    try:
        lockmod.try_flock(holder, fcntl.LOCK_EX)
        assert lockmod.assert_lock_held(path) is None
        # the probe must not have released / stolen the holder's lock
        probe = lockmod.open_lock_file(path)
        try:
            with pytest.raises(lockmod.LockHeldError):
                lockmod.try_flock(probe, fcntl.LOCK_EX)
        finally:
            os.close(probe)
    finally:
        os.close(holder)


def test_assert_lock_held_raises_when_nobody_holds_the_lock(tmp_path):
    path = tmp_path / "pool.lock"
    with pytest.raises(RuntimeError, match="is not held"):
        lockmod.assert_lock_held(path)
    # and the probe's accidental lock was released again
    fd = lockmod.open_lock_file(path)
    try:
        lockmod.try_flock(fd, fcntl.LOCK_EX)
    finally:
        os.close(fd)
