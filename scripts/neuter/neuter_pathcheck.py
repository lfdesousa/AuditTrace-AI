"""pytest plugin (SPEC v3 §6 P4): prove a worker imported the tested package
from ITS OWN worktree, not a stale sibling copy.

Configured entirely by environment variable so the same plugin serves both
the product harness (module ``audittrace``) and the self-proof fixture
(module ``guarded``): ``NEUTER_PATHCHECK_MODULE`` (default
``audittrace``), ``NEUTER_PATHCHECK_EXPECT`` (the worktree's
``src`` root the module must resolve under), ``NEUTER_PATHCHECK_LOG``
(where to append the proof line).

If ``NEUTER_PATHCHECK_EXPECT`` is unset the plugin does nothing
(so it is safe to leave on `sys.path` for unrelated pytest invocations).
The assertion firing (wrong ``PYTHONPATH``) means the log line below is
never reached -- the pool observes "no log line" and classifies the run
``ERROR pathcheck`` (§4), never ``RED`` (proof d).
"""

from __future__ import annotations

import importlib
import os


def pytest_configure(config) -> None:  # noqa: ANN001 - pytest hook signature
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
