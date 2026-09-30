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

**Chokepoint totality (review round 3).** ``scripts/neuter/pytest_run.py``
makes ``NEUTER_CHOKEPOINT_REQUIRED=1`` and ``PYTEST_PLUGINS=neuter_pathcheck``
STICKY in ``os.environ`` the moment it is imported, so THIS plugin
auto-loads for every pytest session that starts afterwards in the SAME
process -- no ``-p neuter_pathcheck`` flag required, and therefore no flag
for a bypass to simply omit. Whenever ``NEUTER_CHOKEPOINT_REQUIRED`` is
set, ``pytest_configure`` fails the session outright (``pytest.exit``,
never a soft warning) unless BOTH ``NEUTER_CHOKEPOINT_TOKEN`` and
``NEUTER_CHOKEPOINT_TOKEN_FILE`` are present AND the token matches what
``run_pytest`` recorded to that file before spawning this session -- a
bypass that never calls ``run_pytest`` at all (``pytest.main()``, a
module-level constant handed to ``subprocess.run``, ``os.system``,
``subprocess.call``, a bare ``py.test`` launcher) inherits the REQUIRED
flag (sticky in ``os.environ``) but never the per-invocation token (which
only ever exists in ``run_pytest``'s own local env dict for its OWN
subprocess), so it fails by construction the instant pytest starts --
never reaching a single test.

If ``NEUTER_CHOKEPOINT_REQUIRED`` is unset (the plugin loaded in some
completely unrelated pytest invocation that never touched
``scripts.neuter.pytest_run`` at all) this check is skipped entirely, and
if ``NEUTER_PATHCHECK_EXPECT`` is also unset the plugin does nothing further
-- safe to leave on ``sys.path``/``PYTEST_PLUGINS`` for such invocations.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


def pytest_configure(config) -> None:  # noqa: ANN001 - pytest hook signature
    required = os.environ.get("NEUTER_CHOKEPOINT_REQUIRED")
    if required:
        token = os.environ.get("NEUTER_CHOKEPOINT_TOKEN")
        token_file = os.environ.get("NEUTER_CHOKEPOINT_TOKEN_FILE")
        if not token or not token_file:
            pytest.exit(
                "neuter chokepoint: NEUTER_CHOKEPOINT_REQUIRED is set but "
                "NEUTER_CHOKEPOINT_TOKEN/_FILE is missing -- this pytest "
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
