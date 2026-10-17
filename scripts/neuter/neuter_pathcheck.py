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

import importlib
import os
from pathlib import Path

import pytest


def pytest_configure(config) -> None:  # noqa: ANN001 - pytest hook signature
    if not os.environ.get("NEUTER_CHOKEPOINT_SKIP"):
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
