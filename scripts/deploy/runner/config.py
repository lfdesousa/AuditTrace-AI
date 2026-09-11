"""Config, constants, exceptions, and CLI argument parsing for the deploy runner.

Everything here is either a pure value (dataclass, constant, regex-free
exception) or the ``argparse`` surface (:func:`build_parser`,
:func:`config_from_args`) — nothing in this module touches a subprocess, the
cluster, or the clock, so nothing here needs the package-indirection pattern
documented in ``_exec.py``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.deploy import mesh

# Ordered phase identifiers — the determinism contract fixes this sequence.
PHASES = (
    "P0-preflight",
    "P1-resolve-image",
    "P2-chart-apply",
    "P3-bootstrap",
    "P4-settle",
    "P5-report",
)

# Chart facts (see charts/audittrace/templates/memory-server/deployment.yaml).
MEMORY_SERVER_COMPONENT = "memory-server"
MEMORY_SERVER_CONTAINER = "memory-server"
_COMPONENT_SELECTOR = f"app.kubernetes.io/component={MEMORY_SERVER_COMPONENT}"

REPO_ROOT = Path(__file__).resolve().parents[3]
PREFLIGHT_SCRIPT = REPO_ROOT / "scripts" / "deploy-preflight.sh"
CHART_DIR = REPO_ROOT / "charts" / "audittrace"
CHART_VALUES_FILE = CHART_DIR / "values.yaml"

# First-party console images this runner deterministically pins on every
# apply (spec 2026-09-10). Third-party subchart images (postgres/redis/
# vault/rabbitmq) are explicitly OUT of scope — see the spec's "Out of
# scope" section; llm-stub is `enabled: false` and the tests image is a
# Job, not a Deployment, so neither belongs here either.
_CONSOLE_IMAGE_COMPONENTS = ("librechat", "bff")

# component -> (pod label selector, live container name), mirroring
# charts/audittrace/templates/console/deployment-{librechat,bff}.yaml's
# `app.kubernetes.io/component` label + container `name:` exactly. Feeds
# the first-party-image-complete convergence check (spec
# 2026-09-10-SPEC-deploy-runner-convergence-first-party-image-complete):
# `_is_converged()` reads the SAME two components `console_image_set_args`
# pins, never a third-party subchart image.
_CONSOLE_COMPONENT_SELECTORS: dict[str, tuple[str, str]] = {
    "librechat": ("app.kubernetes.io/component=librechat", "librechat"),
    "bff": ("app.kubernetes.io/component=librechat-bff", "bff"),
}

# component -> the `{{ .Release.Name }}-<suffix>` Deployment name suffix,
# mirroring the `metadata.name` in deployment-{librechat,bff}.yaml exactly.
# Feeds the config-drift convergence check (spec 2026-09-10-SPEC-deploy-
# runner-convergence-config-drift): `_is_converged()` reads the SAME
# Deployment objects console_image_digests keys its digest pins on, never a
# third-party subchart.
_CONSOLE_DEPLOYMENT_SUFFIXES: dict[str, str] = {
    "librechat": "librechat",
    "bff": "librechat-bff",
}

# The runner never marks a deploy healthy. This string is stamped verbatim into
# the report so a reader (and the Verify Agent) sees the hand-off unambiguously.
VERIFICATION_DEFERRED = (
    "deferred to the independent verify runner (WS3); "
    "this runner reports what it did, it does not certify health"
)


class PreflightAbortError(RuntimeError):
    """Raised when P0 preflight fails; aborts the run before any mutation."""

    def __init__(self, exit_code: int, meaning: str) -> None:
        super().__init__(f"preflight aborted (exit {exit_code}): {meaning}")
        self.exit_code = exit_code
        self.meaning = meaning


# Exit code for a P0 abort caused by the mesh-health gate (#384 WS1) — distinct
# from the preflight-script codes 1–5 so a mesh abort is legible in the report.
MESH_UNSAFE_EXIT = 6


class MeshGateAbortError(PreflightAbortError):
    """Raised when the P0 mesh gate is UNSAFE; aborts BEFORE any mutation (#384).

    A subclass of :class:`PreflightAbortError` so :meth:`DeployRunner.run` catches
    it through the existing abort path and still emits a report — the only pod is
    never terminated into a cert-dead mesh (invariant I1).
    """

    def __init__(self, result: mesh.MeshGateResult) -> None:
        super().__init__(MESH_UNSAFE_EXIT, f"mesh unsafe — {result.reason}")
        self.result = result


def normalize_version(version: str) -> str:
    """Strip a single leading ``v`` — the git TAG is ``vX.Y.Z``, the IMAGE tag is
    ``X.Y.Z``. ``v1.13.0`` and ``1.13.0`` both normalise to ``1.13.0`` (live:
    hub ``1.13.0`` resolves, ``v1.13.0`` 404s)."""
    return (
        version[1:] if version.startswith("v") and version[1:2].isdigit() else version
    )


@dataclass(frozen=True)
class DeployConfig:
    target_version: str
    namespace: str = "audittrace"
    release: str = "audittrace"
    registry: str = "hub"
    dry_run: bool = False
    out_dir: Path = REPO_ROOT / "scripts" / "deploy" / "runs"
    timeout: int = 300
    settle_samples: int = 6
    settle_interval: float = 5.0
    # Ordered extra chart values files (``--values``/``-f``, repeatable),
    # e.g. ``charts/audittrace/values-laptop.yaml`` — spec
    # 2026-09-10-SPEC-deploy-runner-target-overlay-aware. Empty tuple ==
    # today's behaviour: base ``values.yaml`` only. A plain immutable
    # default (not a ``field(default_factory=tuple)``) is safe here because
    # an empty tuple can never be mutated in place.
    values_files: tuple[Path, ...] = ()

    @property
    def deployment(self) -> str:
        return f"{self.release}-memory-server"

    @property
    def image_tag(self) -> str:
        """The IMAGE tag to resolve + deploy (git ``v`` prefix stripped)."""
        return normalize_version(self.target_version)


@dataclass
class PhaseRecord:
    name: str
    status: str  # ok | skipped | noop | planned | flagged | aborted
    command: str | None = None
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    ended_at: str = ""


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.deploy.runner",
        description="Deterministic, idempotent AuditTrace deploy runner (does NOT self-certify).",
    )
    parser.add_argument(
        "--target-version", required=True, help="image tag / version to deploy"
    )
    parser.add_argument("--namespace", default="audittrace")
    parser.add_argument("--release", default="audittrace")
    parser.add_argument("--registry", choices=("hub", "local"), default="hub")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the ordered plan; mutate nothing"
    )
    parser.add_argument("--out-dir", type=Path, default=DeployConfig.out_dir)
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="bounded helm/rollout timeout (seconds)",
    )
    parser.add_argument(
        "--values",
        "-f",
        dest="values_files",
        action="append",
        type=Path,
        default=None,
        help=(
            "extra chart values file (repeatable, ordered — later files win); "
            "e.g. -f charts/audittrace/values-laptop.yaml. Deep-merged with "
            "base values.yaml and passed to helm as -f (spec 2026-09-10-SPEC-"
            "deploy-runner-target-overlay-aware). Omit for base-only "
            "(unchanged pre-fix behaviour)."
        ),
    )
    return parser


def config_from_args(args: argparse.Namespace) -> DeployConfig:
    return DeployConfig(
        target_version=args.target_version,
        namespace=args.namespace,
        release=args.release,
        registry=args.registry,
        dry_run=args.dry_run,
        out_dir=args.out_dir,
        timeout=args.timeout,
        values_files=tuple(args.values_files or ()),
    )
