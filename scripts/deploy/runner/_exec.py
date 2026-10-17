"""Leaf module: the runner's external-effect indirections.

``_run``, ``_sleep``, and ``_now_iso`` are the SOLE seams the test suite
monkeypatches (via ``scripts.deploy.runner._run`` / ``_sleep`` — see
``tests/test_deploy_runner.py``) to avoid a real subprocess/cluster/clock in
unit tests. Kept in a leaf module with ZERO imports from sibling
``scripts.deploy.runner.*`` modules so every other module in this package can
import from here without a circular-import hazard.

Every other module in this package that calls ``_run``/``_sleep`` reaches
them through the PACKAGE namespace (``from scripts.deploy import runner as
_runner_pkg`` then ``_runner_pkg._run(...)``) rather than importing the names
directly from this module — a direct ``from ._exec import _run`` would bind
a private copy in the importing module's namespace, and a test's
``monkeypatch.setattr(runner, "_run", stub)`` (which only ever patches the
package's own attribute) would silently fail to reach it. ``_now_iso`` is
never monkeypatched by the test suite, so it IS imported directly elsewhere
— it is evidence-only (see its docstring) and never read back into control
flow.
"""

from __future__ import annotations

import logging
import os
import subprocess  # noqa: S404 - the runner is the operator; it shells out to helm/kubectl/make
from datetime import UTC, datetime

logger = logging.getLogger("audittrace.deploy.runner")


def _run(
    cmd: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run a command, capturing output. Sole subprocess entry point."""
    logger.info("exec: %s", " ".join(cmd))
    return subprocess.run(  # noqa: S603 - fixed argv lists, no shell
        cmd,
        env={**os.environ, **(env or {})} if env else None,
        capture_output=True,
        text=True,
        check=False,
    )


def _sleep(seconds: float) -> None:
    """Sleep between pod samples. Not a control-flow branch; mocked to 0 in tests."""
    import time

    time.sleep(seconds)


def _now_iso() -> str:
    """UTC ISO-8601 timestamp — EVIDENCE ONLY, never read back into logic."""
    return datetime.now(UTC).isoformat()
