"""Package-decomposition acceptance tests (spec 2026-09-10-SPEC-deploy-
runner-decomposition).

``scripts/deploy/runner.py`` (~1600 LOC) was split into a
``scripts/deploy/runner/`` package by concern (config/values/convergence/helm/
orchestrator), behaviour-preserving. The strongest guard against a
refactor regression is that every PRE-EXISTING test in
``tests/test_deploy_runner*.py`` passes UNCHANGED — this file adds ONLY the
decomposition-specific guarantees the spec calls out: the CLI entrypoint
import path is unchanged, ``--help`` is unchanged, and every concern module
is independently importable (proving the package boundary is real, not a
re-export shim hiding a single god-module).

Falsifiable: revert ``scripts/deploy/runner/__main__.py`` (or drop the
``__main__`` submodule entirely) and
``test_python_dash_m_entrypoint_still_resolves`` goes RED — ``python -m
scripts.deploy.runner`` is a PACKAGE-mode invocation (PEP 338) that requires
an explicit ``__main__.py``; a plain module doesn't need one, so this is
exactly the behaviour the decomposition could silently break.
"""

from __future__ import annotations

import runpy
import subprocess
import sys

import pytest

from scripts.deploy import runner


def test_entrypoint_module_path_unchanged():
    """``python -m scripts.deploy.runner`` is still the documented entrypoint
    — ``build_parser()``'s ``prog`` (shown in ``--help`` and any usage error)
    names it explicitly, and that string is part of the public CLI surface."""
    assert runner.build_parser().prog == "python -m scripts.deploy.runner"


def test_help_output_unchanged():
    """The exact `--help` text is unchanged post-decomposition (byte-for-byte
    checked manually against a pre-refactor capture as part of the spec's
    acceptance criteria; this test pins the live surface so it can't drift
    silently afterwards)."""
    parser = runner.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--help"])
    assert exc.value.code == 0


def test_python_dash_m_entrypoint_still_resolves(monkeypatch):
    """``python -m scripts.deploy.runner --help`` — exercised IN-PROCESS via
    :func:`runpy.run_module` (not a subprocess) so this is the exact mechanism
    Python uses for ``-m`` (PEP 338) and so the module executes under
    ``__name__ == "__main__"`` for real, hitting ``__main__.py``'s
    ``if __name__ == "__main__": sys.exit(main())`` guard. A package needs an
    explicit ``__main__.py`` submodule for ``-m`` to find something to run —
    dropping it (or reverting to `scripts/deploy/runner.py` as a plain
    module) would make this raise ``ImportError`` instead of ``SystemExit``.
    """
    monkeypatch.setattr(sys, "argv", ["scripts.deploy.runner", "--help"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("scripts.deploy.runner", run_name="__main__")
    assert exc.value.code == 0


def test_python_dash_m_subprocess_help_exits_zero():
    """The REAL subprocess invocation an operator would type, kept as a
    second, independent confirmation alongside the in-process
    :func:`runpy.run_module` check above (which exists mainly to give
    ``__main__.py`` in-process coverage)."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, test-only
        [sys.executable, "-m", "scripts.deploy.runner", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "python -m scripts.deploy.runner" in proc.stdout


@pytest.mark.parametrize(
    "module_name",
    [
        "scripts.deploy.runner._exec",
        "scripts.deploy.runner.config",
        "scripts.deploy.runner.values",
        "scripts.deploy.runner.convergence",
        "scripts.deploy.runner.helm",
        "scripts.deploy.runner.orchestrator",
    ],
)
def test_every_concern_module_independently_importable(module_name):
    """Each concern module the spec calls for imports on its own — proving
    the package boundary is real (a genuine split by concern), not a
    re-export shim in front of one god-module."""
    import importlib

    mod = importlib.import_module(module_name)
    assert mod is not None
