"""Deterministic, idempotent deploy runner — Component 2 of the CD program.

The instrument the CD Agent executes. Given a target version + a reachable
cluster it always performs the SAME ordered phases and converges to the SAME
end state; re-running on an already-converged cluster is a no-op. There is NO
wall-clock branching in the control flow — timestamps are stamped into evidence
only, never read back to decide anything.

Ordered phases (each logged; each declares the evidence it captures):

* **P0 Preflight**  — ``scripts/deploy-preflight.sh``; ABORTS the run on any
  non-zero exit, surfacing exit 3 (vault injector) / 4 (istiod) explicitly. No
  mutation happens before P0 passes.
* **P1 Resolve**    — resolve ``--target-version`` to a published digest via the
  registry API (:mod:`scripts.deploy.registry`).
* **P2 Chart apply**— ``helm upgrade --install ... --reset-then-reuse-values
  --wait`` (bounded timeout, NO ``--atomic``). The surge-safe strategy lives in
  the chart (WS1); the runner does not re-specify it.
* **P3 Bootstrap**  — ``make k8s-bootstrap-secrets``, GUARDED: SKIPPED when
  ``VAULT_TOKEN`` is unset (already-provisioned assumption). Safe to re-run.
* **P4 Settle**     — wait for the memory-server rollout, sample the pods, and
  ASSERT the peak concurrent Running count never exceeds ``spec.replicas`` — the
  runtime form of the WS1 surge-safe guarantee.
* **P5 Report**     — emit the deploy report (JSON + human summary) + evidence
  bundle.

**The runner NEVER self-certifies success.** It reports what it DID; the
health verdict belongs to the independent Verify Agent (WS3). The report carries
``certified: null`` to make that explicit.

## Package layout (spec 2026-09-10-SPEC-deploy-runner-decomposition)

This was a single ~1600-line module; it is now a package split by concern,
each file <500 LOC:

* ``_exec.py``          — the ``_run``/``_sleep``/``_now_iso`` seams.
* ``config.py``         — ``DeployConfig``, constants, exceptions, the CLI.
* ``values.py``         — chart-values read/merge + first-party image `--set`.
* ``convergence.py``    — ``_is_converged`` + the live-cluster digest/config reads.
* ``helm.py``           — the ``helm upgrade`` argv + the #456 adopt/retry flow.
* ``orchestrator.py``   — :class:`DeployRunner` (P0-P5) + ``main``/``print_plan``.

This module (``__init__.py``) re-exports the full public + test-facing
surface below so ``from scripts.deploy.runner import X`` and ``python -m
scripts.deploy.runner`` (via ``__main__.py``) are BYTE-IDENTICAL to the
pre-decomposition single-module behaviour. ``Path`` is re-exported too:
the test suite patches ``runner.Path.home`` (a ``pathlib.Path`` classmethod,
shared globally by every module that imports the class — patching it here
patches it everywhere).
"""

from __future__ import annotations

from pathlib import Path

from scripts.deploy.runner._exec import _now_iso, _run, _sleep, logger
from scripts.deploy.runner.config import (
    _COMPONENT_SELECTOR,
    _CONSOLE_COMPONENT_SELECTORS,
    _CONSOLE_DEPLOYMENT_SUFFIXES,
    _CONSOLE_IMAGE_COMPONENTS,
    CHART_DIR,
    CHART_VALUES_FILE,
    MEMORY_SERVER_COMPONENT,
    MEMORY_SERVER_CONTAINER,
    MESH_UNSAFE_EXIT,
    PHASES,
    PREFLIGHT_SCRIPT,
    REPO_ROOT,
    VERIFICATION_DEFERRED,
    DeployConfig,
    MeshGateAbortError,
    PhaseRecord,
    PreflightAbortError,
    build_parser,
    config_from_args,
    normalize_version,
)
from scripts.deploy.runner.convergence import (
    ConvergenceCheck,
    _container_spec_from_deployment_doc,
    _find_container,
    _find_deployment_doc,
    _normalize_container_spec,
    _normalize_env,
    _normalize_resources,
    extract_digest,
    max_concurrent_running,
    within_surge_bound,
)
from scripts.deploy.runner.helm import (
    _helm_apply_cmd,
    parse_ownership_conflicts,
)
from scripts.deploy.runner.orchestrator import DeployRunner, main, print_plan
from scripts.deploy.runner.values import (
    _deep_merge,
    _parse_values_file,
    _read_chart_values,
    _values_file_args,
    apply_image_tag,
    console_image_digests,
    console_image_set_args,
)

__all__ = [
    "CHART_DIR",
    "CHART_VALUES_FILE",
    "MEMORY_SERVER_COMPONENT",
    "MEMORY_SERVER_CONTAINER",
    "MESH_UNSAFE_EXIT",
    "PHASES",
    "PREFLIGHT_SCRIPT",
    "REPO_ROOT",
    "VERIFICATION_DEFERRED",
    "ConvergenceCheck",
    "DeployConfig",
    "DeployRunner",
    "MeshGateAbortError",
    "Path",
    "PhaseRecord",
    "PreflightAbortError",
    "_COMPONENT_SELECTOR",
    "_CONSOLE_COMPONENT_SELECTORS",
    "_CONSOLE_DEPLOYMENT_SUFFIXES",
    "_CONSOLE_IMAGE_COMPONENTS",
    "_container_spec_from_deployment_doc",
    "_deep_merge",
    "_find_container",
    "_find_deployment_doc",
    "_helm_apply_cmd",
    "_normalize_container_spec",
    "_normalize_env",
    "_normalize_resources",
    "_now_iso",
    "_parse_values_file",
    "_read_chart_values",
    "_run",
    "_sleep",
    "_values_file_args",
    "apply_image_tag",
    "build_parser",
    "config_from_args",
    "console_image_digests",
    "console_image_set_args",
    "extract_digest",
    "logger",
    "main",
    "max_concurrent_running",
    "normalize_version",
    "parse_ownership_conflicts",
    "print_plan",
    "within_surge_bound",
]
