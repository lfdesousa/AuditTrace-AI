"""Shared hermetic-environment helpers for the test-parity gate (the
"change the instrument" redesign after two independent-review REJECTs on
the previous argv-parsing approach — ADDENDUM A2 through A7 of the
2026-10-11 test-parity-scripted-enforcement spec chain).

This module is one of the two files
(``tests/conftest.py``, where the frozen sentinel holder
:data:`tests.conftest.HERMETIC_SENTINELS` is defined, and this file)
exempted from the static-reference test that bans any other file under
``tests/`` from naming that holder directly, and one of the two files
(the other being ``tests/test_hermetic_cluster_tool_guard.py``) exempted
from the static-usage test on :func:`expect_hermetic_violation`.

Public comments in this module state only what the committed tests in
``tests/test_hermetic_cluster_tool_guard.py`` and
``tests/test_ci_make_test_parity.py`` measure.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import textwrap
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def hermetic_env(
    base: dict[str, str] | None = None, **overrides: str
) -> dict[str, str]:
    """Builds a child ``env=`` mapping that still satisfies the guard's
    A3-3 env-equality check: the frozen shim dir first on ``PATH``, the
    frozen ``KUBECONFIG``, and no ``KUBERNETES_SERVICE_*`` / ``HELM_KUBE*``
    variable. ``base`` defaults to a copy of ``os.environ``; ``overrides``
    are applied last, so a caller can still deliberately set a bad value
    to prove the guard raises.

    A test that builds its own ``env=`` for a spawn (a custom ``PATH``
    probe, a ``monkeypatch.setenv("PATH", ...)`` fixture, etc.) must route
    it through this helper instead, or the spawn trips the guard's env
    check — tests/test_hermetic_cluster_tool_guard.py and the suite's own
    migrated call sites are what is actually measured.
    """
    from tests import conftest

    env = dict(os.environ if base is None else base)
    sentinels = conftest.HERMETIC_SENTINELS
    if sentinels.shim_dir:
        existing = [
            p
            for p in env.get("PATH", "").split(os.pathsep)
            if p and p != sentinels.shim_dir
        ]
        env["PATH"] = os.pathsep.join([sentinels.shim_dir, *existing])
    if sentinels.kubeconfig:
        env["KUBECONFIG"] = sentinels.kubeconfig
    for name in list(env):
        if name in (
            "KUBERNETES_SERVICE_HOST",
            "KUBERNETES_SERVICE_PORT",
        ) or name.startswith("HELM_KUBE"):
            del env[name]
    env.update(overrides)
    return env


_UNSET = object()


@contextlib.contextmanager
def override_sentinel(
    *,
    shim_dir: str | None = _UNSET,  # type: ignore[assignment]
    kubeconfig: str | None = _UNSET,  # type: ignore[assignment]
    required: bool | None = _UNSET,  # type: ignore[assignment]
):
    """The ONLY sanctioned way for a test to change the frozen sentinel
    holder (A5-6/A6-6) — a static test in
    ``tests/test_hermetic_cluster_tool_guard.py`` asserts no other file
    under ``tests/`` references the holder by name. Restores the prior
    values on exit, including on exception.

    A field left at its default (unpassed) is left untouched. Passing
    ``None`` explicitly for ``shim_dir``/``kubeconfig`` DOES clear that
    field (distinct from "not specified") — a sentinel default, not
    ``None``, marks "unspecified", so a test can deliberately simulate the
    sentinel being absent.
    """
    from tests import conftest

    holder = conftest.HERMETIC_SENTINELS
    prev = (holder.shim_dir, holder.kubeconfig, holder.required)
    try:
        if shim_dir is not _UNSET:
            holder.shim_dir = shim_dir
        if kubeconfig is not _UNSET:
            holder.kubeconfig = kubeconfig
        if required is not _UNSET:
            holder.required = required
        yield holder
    finally:
        holder.shim_dir, holder.kubeconfig, holder.required = prev


@contextlib.contextmanager
def expect_hermetic_violation():
    """The ONLY sanctioned way to assert a deliberate hermetic-guard
    violation (ADDENDUM A7-1) — a bare ``pytest.raises`` on the guard's
    error type is banned in ``tests/`` (a static test enforces the ban).

    On entry, snapshots the per-test violation record's length. On exit it
    asserts that :class:`tests.conftest.HermeticClusterToolError` was
    raised inside the block AND that at least one violation was recorded
    during the block, then removes ONLY the entries added during the
    block (entries recorded before or after it are left alone). Safe to
    nest.
    """
    from tests import conftest

    record = conftest.PER_TEST_VIOLATIONS
    start = len(record)
    raised = False
    try:
        yield
    except conftest.HermeticClusterToolError:
        raised = True
    end = len(record)
    recorded_inside = end - start
    if not raised:
        raise AssertionError(
            "expect_hermetic_violation(): the block did not raise "
            "HermeticClusterToolError"
        )
    if recorded_inside <= 0:
        raise AssertionError(
            "expect_hermetic_violation(): HermeticClusterToolError was "
            "raised inside the block, but no violation was recorded "
            "during it"
        )
    del record[start : start + recorded_inside]


def build_fresh_sentinel_env(tmp_path: Path) -> dict[str, str]:
    """Builds a brand-new, self-contained hermetic env — a scratch shim
    dir (with a sentinel-logging ``kubectl``), a nonexistent kubeconfig
    path, and the three ``HERMETIC_*`` exports — the same shape
    ``scripts/ci/run-tests.sh`` produces, for tests that need a REAL child
    pytest session to see them (A5-2), independent of whatever env this
    outer test process happens to be running under.
    """
    shim_dir = tmp_path / "hermetic-shim"
    shim_dir.mkdir()
    kubectl = shim_dir / "kubectl"
    kubectl.write_text("#!/usr/bin/env bash\nexit 17\n")
    kubectl.chmod(0o755)
    kubeconfig = tmp_path / "nonexistent-kubeconfig"

    env = dict(os.environ)
    env["HERMETIC_SHIM_DIR"] = str(shim_dir)
    env["HERMETIC_KUBECONFIG"] = str(kubeconfig)
    env["HERMETIC_REQUIRED"] = "1"
    env["KUBECONFIG"] = str(kubeconfig)
    existing = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    env["PATH"] = os.pathsep.join([str(shim_dir), *existing])
    for name in list(env):
        if name in (
            "KUBERNETES_SERVICE_HOST",
            "KUBERNETES_SERVICE_PORT",
        ) or name.startswith("HELM_KUBE"):
            del env[name]
    return env


def run_child_pytest_session(
    body: str,
    *,
    env: dict[str, str],
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Runs a REAL child pytest session, through the SAME interpreter, on
    a throwaway probe test file dropped under ``tests/`` so
    ``tests/conftest.py`` (and its autouse hermetic guard fixture) applies
    to it exactly as it does to every other test in this suite — the
    mechanism ``tests/test_hermetic_cluster_tool_guard.py`` (A5-2, A6-7 /
    A7-3) measures. ``body`` is the probe file's full source; the probe
    file is removed again once the child session exits.
    """
    token = uuid.uuid4().hex[:12]
    probe_path = REPO_ROOT / "tests" / f"test_hermetic_child_probe_{token}.py"
    probe_path.write_text(textwrap.dedent(body))
    try:
        argv = [
            sys.executable,
            "-m",
            "pytest",
            str(probe_path),
            "-p",
            "no:cacheprovider",
            "--no-cov",
            "-o",
            "addopts=",
            "-q",
        ]
        if extra_args:
            argv.extend(extra_args)
        # This call is itself a spawn the OUTER guard's env check sees.
        # `env` is deliberately a FRESH, self-contained sentinel set (not
        # the outer process's ambient one) — temporarily make it the
        # ambient sentinel too, for the duration of this one call, so the
        # outer check's expectation matches what we are actually handing
        # the child (the same technique the guard test module's own
        # `fake_tools` fixture uses).
        with override_sentinel(
            shim_dir=env.get("HERMETIC_SHIM_DIR"),
            kubeconfig=env.get("HERMETIC_KUBECONFIG"),
        ):
            return subprocess.run(
                argv,
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
    finally:
        probe_path.unlink(missing_ok=True)


def scratch_tmpdir_path(prefix: str) -> Path:
    """A tiny indirection so callers that need a throwaway directory
    OUTSIDE pytest's own ``tmp_path`` (for example a static-reference-ban
    neuter that must land outside ``tests/``) have one obvious helper
    rather than each re-deriving ``tempfile.mkdtemp``."""
    return Path(tempfile.mkdtemp(prefix=prefix))
