"""pytest plugin (SPEC v3 §6 P4): prove a worker imported the tested package
from ITS OWN worktree, not a stale sibling copy -- AND (review round 3,
requirement B2) prove this pytest SESSION was started through the
chokepoint at all.

Configured entirely by environment variable so the same plugin serves both
the product harness (module ``audittrace``) and the self-proof fixture
(module ``guarded``): ``NEUTER_PATHCHECK_MODULE`` (default
``audittrace``), ``NEUTER_PATHCHECK_EXPECT`` (the worktree's
``src`` root the module must resolve under), ``NEUTER_PATHCHECK_LOG``
(where to append the proof line).

**Chokepoint totality (review round 3; FAIL-CLOSED BY DEFAULT, review round
4 requirement E1).** ``scripts/neuter/pytest_run.py`` makes
``PYTEST_PLUGINS=neuter_pathcheck`` STICKY in ``os.environ`` for the
DURATION of a ``chokepoint_scope()`` block, so THIS plugin auto-loads for
every pytest session that starts in that window -- no ``-p
neuter_pathcheck`` flag required, and therefore no flag for a bypass to
simply omit.

Round 3 made the check CONDITIONAL on ``NEUTER_CHOKEPOINT_REQUIRED`` being
present -- round 4's review found a bypass that constructs a HERMETIC
subprocess env (e.g. ``env={"PATH": ..., "HOME": ...}``), discarding the
ambient ``os.environ`` instead of inheriting it, which strips
``NEUTER_CHOKEPOINT_REQUIRED`` along with everything else and sails
through with ``rc=0``. **The check is now UNCONDITIONAL**: whenever this
plugin loads at all, it fails the session (``pytest.exit``, never a soft
warning) unless a valid token is present -- the ONLY way to skip it is an
EXPLICIT, affirmative opt-out, ``NEUTER_CHOKEPOINT_SKIP=1``, which no
legitimate code path in this harness ever sets (not ``run_pytest()``, not
``hold-shared``, not any test). Absence of a flag used to mean
"permissive"; now it means nothing -- the token is required unless the
opt-out is explicitly present. A bypass that never calls ``run_pytest()``
at all (``pytest.main()``, a module-level constant handed to
``subprocess.run``, ``os.system``, ``subprocess.call``, a bare ``py.test``
launcher, OR a hermetic env that discards the ambient environ) never gets
a token to omit; it fails by construction the instant pytest starts --
never reaching a single test.

**Entry-point auto-load and the activity signal (#368).** The plugin is also
registered as a ``pytest11`` entry point (``src/neuter_pathcheck_entry.py``),
so it loads in EVERY pytest run of the project venv whatever the caller's
environment -- including a from-scratch env with no ``PYTEST_PLUGINS``. Since
that includes ordinary ``make test`` / IDE runs, the unconditional refusal
became conditional on :func:`enforcement_active`: a claim of the chokepoint
in the env (token must then verify), or a neuter pool holding its heavy-cap
lock exclusively (read from the filesystem, not from the caller's env). With
neither, the plugin is inert; ``NEUTER_CHOKEPOINT_SKIP=1`` still opts out.
Residual: a pool started with a custom ``--lock-path`` outside the default
locations AND a stripped env is not seen (documented, tests-only usage).

**Marker-based self-check (review round 4 requirement E2).** A SEPARATE
bypass is a stray ``-p no:neuter_pathcheck`` reaching ``pytest_args``
(disabling the plugin outright, so it never loads and this file's own
check code never runs at all). Since the plugin can't detect its OWN
absence, ``run_pytest()`` -- the chokepoint ITSELF, not this plugin --
checks for this: before validating the token, if this plugin's
``pytest_configure`` runs, it writes the token to
``NEUTER_CHOKEPOINT_MARKER_FILE`` (a path ``run_pytest()`` also hands the
child). After the child process exits, ``run_pytest()`` checks that this
marker file now contains the expected token; if it is missing or wrong,
the whole call is ERROR ``chokepoint_marker_missing`` -- regardless of the
child's own exit code, catching any case where this plugin failed to load
or run at all, not just a token mismatch within it.

If ``NEUTER_PATHCHECK_EXPECT`` is also unset the plugin does nothing
further beyond the two checks above -- safe to leave on
``sys.path``/``PYTEST_PLUGINS`` for invocations that don't ask for
path-checking.
"""

from __future__ import annotations

import fcntl
import importlib
import os
import tempfile
from pathlib import Path

import pytest

#: Mirrors ``scripts.neuter.lock`` (``LOCK_FILENAME`` / ``ENV_LOCK_PATH``).
#: Duplicated on purpose: this module is loaded as a TOP-LEVEL plugin
#: (``PYTEST_PLUGINS=neuter_pathcheck`` or the ``pytest11`` entry point) in
#: processes where ``scripts`` is not importable, so it cannot import it.
#: ``tests/test_neuter_pathcheck.py`` pins the two copies together.
_LOCK_FILENAME = "audittrace-neuter-pool.lock"
_ENV_LOCK_PATH = "AUDITTRACE_NEUTER_LOCK"

#: Env vars that mean "the caller claims to be inside the chokepoint". Any one
#: of them present makes the token check mandatory (a half-set claim is a
#: bypass attempt, never a reason to relax).
_CLAIM_VARS = (
    "NEUTER_CHOKEPOINT_REQUIRED",
    "NEUTER_CHOKEPOINT_TOKEN",
    "NEUTER_CHOKEPOINT_TOKEN_FILE",
    "NEUTER_CHOKEPOINT_MARKER_FILE",
)


def _candidate_lock_paths() -> list[Path]:
    """Every place a neuter pool's heavy-cap lock may live.

    The caller's environment is NOT trusted to point at the lock (a stripped
    env discards ``XDG_RUNTIME_DIR`` / ``AUDITTRACE_NEUTER_LOCK``), so besides
    the env-derived paths this also tries the per-user runtime dir and the
    tempdir default that ``scripts.neuter.lock.resolve_lock_path`` can pick.
    """
    candidates: list[Path] = []
    override = os.environ.get(_ENV_LOCK_PATH)
    if override:
        candidates.append(Path(override))
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        candidates.append(Path(xdg) / _LOCK_FILENAME)
    candidates.append(Path(f"/run/user/{os.getuid()}") / _LOCK_FILENAME)
    candidates.append(Path(tempfile.gettempdir()) / _LOCK_FILENAME)
    unique: list[Path] = []
    for path in candidates:
        if path not in unique:
            unique.append(path)
    return unique


def _pool_lock_held_exclusive(path: Path) -> bool:
    """True iff some process holds ``LOCK_EX`` on ``path`` right now.

    Never creates the file and never keeps a lock: a non-blocking ``LOCK_SH``
    probe on a fresh descriptor fails exactly when an exclusive holder exists
    (the neuter pool). ``make test``'s ``hold-shared`` holds ``LOCK_SH``, which
    does not conflict, so an ordinary run reads as "no pool active".
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def enforcement_active() -> bool:
    """Must THIS pytest session prove it came through the chokepoint?

    The plugin auto-loads in every run of the project venv (``pytest11``
    entry point), so ``make test`` and IDE runs load it too and must stay
    untouched. The signal is deliberately NOT a single caller-supplied flag
    (a hermetic env strips any flag, review round 4): enforcement is active
    when EITHER the caller claims the chokepoint (any ``_CLAIM_VARS`` set --
    then the token must verify) OR a neuter pool is running, observed
    OUTSIDE the caller's environment as an exclusive hold on the pool's
    heavy-cap lock (:func:`_pool_lock_held_exclusive`).
    """
    if any(os.environ.get(var) for var in _CLAIM_VARS):
        return True
    return any(_pool_lock_held_exclusive(p) for p in _candidate_lock_paths())


def pytest_configure(config) -> None:  # noqa: ANN001 - pytest hook signature
    if not os.environ.get("NEUTER_CHOKEPOINT_SKIP") and enforcement_active():
        token = os.environ.get("NEUTER_CHOKEPOINT_TOKEN")
        token_file = os.environ.get("NEUTER_CHOKEPOINT_TOKEN_FILE")
        if not token or not token_file:
            pytest.exit(
                "neuter chokepoint: no valid NEUTER_CHOKEPOINT_TOKEN/_FILE "
                "(and no NEUTER_CHOKEPOINT_SKIP opt-out) -- this pytest "
                "session was not started through "
                "scripts.neuter.pytest_run.run_pytest()",
                returncode=1,
            )
        try:
            recorded = Path(token_file).read_text().strip()
        except OSError as exc:
            pytest.exit(
                f"neuter chokepoint: token file {token_file!r} unreadable ({exc}) "
                "-- this pytest session was not started through "
                "scripts.neuter.pytest_run.run_pytest()",
                returncode=1,
            )
        if recorded != token:
            pytest.exit(
                "neuter chokepoint: NEUTER_CHOKEPOINT_TOKEN does not match "
                "the value scripts.neuter.pytest_run.run_pytest() recorded "
                "for this invocation",
                returncode=1,
            )
        marker_file = os.environ.get("NEUTER_CHOKEPOINT_MARKER_FILE")
        if marker_file:
            Path(marker_file).write_text(token)

    expect = os.environ.get("NEUTER_PATHCHECK_EXPECT")
    if not expect:
        return
    module_name = os.environ.get("NEUTER_PATHCHECK_MODULE", "audittrace")
    log_path = os.environ["NEUTER_PATHCHECK_LOG"]
    module = importlib.import_module(module_name)
    got = os.path.realpath(module.__file__)
    want = os.path.realpath(expect)
    assert got.startswith(want + os.sep), (
        f"{module_name} imported from {got}, want under {want}"
    )
    with open(log_path, "a") as fh:
        fh.write(f"{os.getpid()} {got}\n")
