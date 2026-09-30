"""The heavy cap, both directions (SPEC v3 §10; FNH2-BL-1, SF-2, SF-3, SF-E).

One full ``make test``/``make test-cov`` or image build runs at a time,
never beside the neuter pool. The lock file is opened through exactly one
function that may create it, :func:`open_lock_file` -- ``hold-shared`` and
the pool call it; :func:`open_existing_lock_file` (used by ``assert-idle``)
never creates the file.

The lock path defaults to ``$XDG_RUNTIME_DIR`` (or ``tempfile.gettempdir()``)
``/audittrace-neuter-pool.lock``, and is overridable per SF-2 via
``--lock-path`` or the ``AUDITTRACE_NEUTER_LOCK`` environment variable (both
resolved here so every caller shares one seam) -- this is what lets every
harness test run against a ``tmp_path`` lock file instead of the real one.
"""

from __future__ import annotations

import fcntl
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_FILENAME = "audittrace-neuter-pool.lock"
ENV_LOCK_PATH = "AUDITTRACE_NEUTER_LOCK"

EXIT_LOCK_HELD = 8


class LockHeldError(Exception):
    """A non-blocking ``flock`` attempt failed -- the lock is held elsewhere."""


def resolve_lock_path(override: str | None = None) -> Path:
    """Resolve the lock file path: ``--lock-path`` > env var > XDG > tempdir.

    SF-2: the override chain lets every test point at a ``tmp_path`` file
    without ever touching the real, shared lock.
    """
    if override:
        return Path(override)
    env_override = os.environ.get(ENV_LOCK_PATH)
    if env_override:
        return Path(env_override)
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / LOCK_FILENAME


def open_lock_file(path: Path) -> int:
    """Open the lock file, creating it if absent. The only creator (§10).

    ``O_CLOEXEC`` (SF-3) so the fd never survives an ``exec`` in a child --
    which is also why ``hold-shared`` must fork+exec (``subprocess.run``)
    and never ``os.exec*``: an exec would drop this fd (and the lock with
    it) before the child even started.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(str(path), os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o644)


def open_existing_lock_file(path: Path) -> int | None:
    """Open the lock file read-only WITHOUT creating it (``assert-idle``).

    Returns ``None`` -- never raises -- when the file is absent or cannot be
    opened (permission error on a mode-000 file, etc.): SF-E requires
    ``assert-idle`` to exit 0 with a stderr warning on an unreadable lock
    file, never crash.
    """
    if not path.exists():
        return None
    try:
        return os.open(str(path), os.O_RDONLY | os.O_CLOEXEC)
    except OSError as exc:
        logger.warning(
            "assert-idle: lock file %s unreadable (%s); treating as idle", path, exc
        )
        return None


def try_flock(fd: int, flags: int) -> None:
    """Non-blocking ``flock(fd, flags | LOCK_NB)``; raises :class:`LockHeldError`."""
    try:
        fcntl.flock(fd, flags | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise LockHeldError from exc


def foreign_docker_build_running() -> bool:
    """``/proc/*/cmdline`` scan for a ``docker build``/``buildx`` process (§10 H4).

    Own PID is excluded; no ``pgrep`` (the self-matching pgrep lesson).
    """
    own_pid = os.getpid()
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        if pid == own_pid:
            continue
        cmdline_path = entry / "cmdline"
        try:
            cmdline = (
                cmdline_path.read_bytes().replace(b"\0", b" ").decode(errors="replace")
            )
        except OSError:
            continue
        if "docker" in cmdline and ("build" in cmdline or "buildx" in cmdline):
            return True
    return False
