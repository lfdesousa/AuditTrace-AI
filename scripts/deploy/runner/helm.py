"""The ``helm upgrade --install`` argv, revision/status reads, and the #456
reactive-adopt-and-retry-once flow.

:class:`HelmMixin` methods and :func:`_helm_apply_cmd` reach the
``_run``/``_read_chart_values`` seams via the PACKAGE namespace
(``_runner_pkg._run(...)``) rather than a direct ``from ._exec import
_run``-style binding — see ``_exec.py``'s module docstring for why: a direct
import would desync from ``monkeypatch.setattr(runner, "_run", ...)``, which
only ever patches the package's own attribute. ``_now_iso`` is never
monkeypatched, so it is imported directly.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from scripts.deploy import registry
from scripts.deploy import runner as _runner_pkg
from scripts.deploy.runner._exec import _now_iso
from scripts.deploy.runner.config import CHART_DIR, PHASES, DeployConfig
from scripts.deploy.runner.values import (
    _values_file_args,
    apply_image_tag,
    console_image_set_args,
)

if TYPE_CHECKING:
    from scripts.deploy.runner.orchestrator import DeployRunner

logger = logging.getLogger("audittrace.deploy.runner")

# ── Option A1: reactive adopt of out-of-band Helm objects (#456) ─────────────
#
# When ``helm upgrade`` meets an object that already exists in the cluster but
# was never created by THIS release (e.g. applied out-of-band, or left behind
# by a prior non-Helm bootstrap), it refuses to import it and fails with an
# "invalid ownership metadata" error. Option A1 is REACTIVE: on that specific
# failure, stamp the three Helm ownership fields onto each named object and
# retry the upgrade EXACTLY ONCE (bounded — never loop). Option A2 (a
# proactive pre-scan of the rendered manifest) was REJECTED as over-scoped;
# do not build it here.

# The tight marker that must be present before ANY adopt is attempted — an
# unrelated helm failure (timeout, chart error, ...) must never trigger one.
_OWNERSHIP_MARKER_RE = re.compile(r"invalid ownership metadata")

# Each conflicting object is named on stderr as:
#     <Kind> "<name>" in namespace "<ns>" ...
# Cluster-scoped objects omit the ` in namespace "<ns>"` segment.
_OWNERSHIP_OBJECT_RE = re.compile(
    r'([A-Z][A-Za-z0-9]*) "([^"]+)"(?: in namespace "([^"]+)")?'
)


def parse_ownership_conflicts(stderr: str) -> list[tuple[str, str, str | None]]:
    """Extract ``(kind, name, namespace | None)`` triples from a helm
    ownership-metadata conflict error (Option A1, #456).

    Returns an empty list unless the tight ``invalid ownership metadata``
    marker is present, so an unrelated helm failure can never trigger an
    adopt. Falsifiable: drop the marker check and a stderr that merely
    contains a quoted ``Kind "name"``-shaped substring (with no ownership
    conflict at all) would wrongly be treated as one.
    """
    if not _OWNERSHIP_MARKER_RE.search(stderr):
        return []
    return [
        (kind, name, ns or None)
        for kind, name, ns in _OWNERSHIP_OBJECT_RE.findall(stderr)
    ]


def _helm_apply_cmd(
    cfg: DeployConfig,
    image_ref: registry.ImageRef,
    chart_values: dict[str, Any] | None = None,
) -> list[str]:
    """The exact ``helm upgrade --install`` argv for P2 (surge-safe via chart).

    ``chart_values`` defaults to a fresh read of the EFFECTIVE chart values
    (:func:`scripts.deploy.runner.values._read_chart_values`, base
    ``values.yaml`` deep-merged with ``cfg.values_files`` in order); tests
    inject an explicit dict so the console-image assertion
    (:func:`console_image_set_args`) is exercised hermetically without
    depending on the chart's current on-disk state.

    Each of ``cfg.values_files`` is ALSO passed to helm itself as an
    explicit ``-f <path>`` — after the chart directory / namespace, before
    ``--set`` — so the apply doesn't rely on ``--reset-then-reuse-values``
    to carry a previously-``-f``'d overlay forward (spec 2026-09-10-SPEC-
    deploy-runner-target-overlay-aware). With no ``values_files`` this argv
    is byte-identical to before the fix.
    """
    if chart_values is None:
        chart_values = _runner_pkg._read_chart_values(values_files=cfg.values_files)
    return [
        "helm",
        "upgrade",
        "--install",
        cfg.release,
        str(CHART_DIR),
        "-n",
        cfg.namespace,
        *_values_file_args(cfg.values_files),
        "--reset-then-reuse-values",
        "--set",
        f"memoryServer.image.repository={image_ref.repository}",
        "--set",
        f"memoryServer.image.tag={apply_image_tag(cfg, image_ref)}",
        *console_image_set_args(chart_values),
        "--wait",
        "--timeout",
        f"{cfg.timeout}s",
    ]


class HelmMixin:
    """:class:`DeployRunner` methods that talk to Helm directly: the P2
    chart-apply phase, the #456 adopt/retry-once flow, and the single
    ``helm status`` read shared with convergence."""

    def _adopt_object(
        self: DeployRunner, kind: str, name: str, namespace: str | None
    ) -> None:
        """Stamp Helm's ownership fields onto an out-of-band object so the next
        ``helm upgrade`` imports it into THIS release (Option A1, #456).

        ``--overwrite`` keeps the operation idempotent. A failed kubectl call
        here is not raised — it simply means the retried helm upgrade still
        fails, which is recorded as "flagged" exactly as an unrelated helm
        failure would be (no special-casing of the adopt-step failure mode).
        """
        target: list[str] = [kind.lower(), name]
        if namespace is not None:
            target += ["-n", namespace]
        _runner_pkg._run(
            [
                "kubectl",
                "label",
                *target,
                "app.kubernetes.io/managed-by=Helm",
                "--overwrite",
            ]
        )
        _runner_pkg._run(
            [
                "kubectl",
                "annotate",
                *target,
                f"meta.helm.sh/release-name={self.cfg.release}",
                "--overwrite",
            ]
        )
        _runner_pkg._run(
            [
                "kubectl",
                "annotate",
                *target,
                f"meta.helm.sh/release-namespace={self.cfg.namespace}",
                "--overwrite",
            ]
        )

    def phase_chart_apply(self: DeployRunner) -> None:
        assert self.image_ref is not None
        cmd = _helm_apply_cmd(self.cfg, self.image_ref)
        if self.cfg.dry_run:
            self._record(
                PHASES[2],
                "planned",
                command=" ".join(cmd),
                detail="would helm upgrade --install (surge-safe via chart; no --atomic)",
            )
            return

        check = self._is_converged()
        if check.converged:
            self.converged = True
            self._record(
                PHASES[2],
                "noop",
                command=" ".join(cmd),
                detail=(
                    f"already converged on {check.basis} + helm status={check.helm_status}; "
                    "skipping helm upgrade (idempotent no-op)"
                ),
                evidence={
                    "converged_on": check.basis,
                    "helm_revision": self.helm_revision,
                    "helm_status": check.helm_status,
                },
            )
            return

        # #451 — the digest already matches but the release itself is not
        # `deployed`; explain WHY an upgrade runs despite the digest match so
        # the report and the independent verifier can see the reconcile
        # reason. A plain digest mismatch gets no such note (unchanged path).
        reconcile_note = (
            f"digest matches but helm release status={check.helm_status!r} "
            "→ re-running helm upgrade to reconcile release state; "
            if check.digest_matched
            else ""
        )

        started = _now_iso()
        proc = _runner_pkg._run(cmd)
        # Option A1 (#456): react to an ownership-metadata conflict by adopting
        # the named out-of-band objects and retrying EXACTLY ONCE — bounded, no
        # loop, no second adopt attempt even if the retry also fails.
        adopted: list[dict[str, Any]] = []
        if proc.returncode != 0:
            conflicts = parse_ownership_conflicts(proc.stderr)
            if conflicts:
                for kind, name, ns in conflicts:
                    self._adopt_object(kind, name, ns)
                    adopted.append({"kind": kind, "name": name, "namespace": ns})
                logger.info(
                    "adopted %d out-of-band object(s) into release %r; retrying helm upgrade once",
                    len(adopted),
                    self.cfg.release,
                )
                proc = _runner_pkg._run(cmd)
        if proc.returncode != 0:
            self._record(
                PHASES[2],
                "flagged",
                command=" ".join(cmd),
                detail=reconcile_note
                + "helm upgrade returned non-zero (no --atomic → no rollback; Verify Agent decides)"
                + (
                    f"; adopted {len(adopted)} out-of-band object(s) before retry"
                    if adopted
                    else ""
                ),
                evidence={
                    "exit_code": proc.returncode,
                    "stderr_tail": proc.stderr[-2000:],
                    **({"adopted": adopted} if adopted else {}),
                },
                started_at=started,
                ended_at=_now_iso(),
            )
            return
        self.helm_revision = self._helm_revision()
        self._record(
            PHASES[2],
            "ok",
            command=" ".join(cmd),
            detail=reconcile_note
            + f"chart applied; helm revision {self.helm_revision}"
            + (
                f" (after adopting {len(adopted)} out-of-band object(s))"
                if adopted
                else ""
            ),
            evidence={
                "helm_revision": self.helm_revision,
                "stdout_tail": proc.stdout[-2000:],
                **({"adopted": adopted} if adopted else {}),
            },
            started_at=started,
            ended_at=_now_iso(),
        )

    def _helm_status_info(self: DeployRunner) -> tuple[int | None, str | None]:
        """Single ``helm status -o json`` read -> ``(revision, release status)``.

        Serves both P2's revision bookkeeping AND the #451 convergence check
        from the SAME subprocess call — the runner never issues a second
        ``helm status`` call to read a snapshot of cluster state it already
        has.
        """
        proc = _runner_pkg._run(
            ["helm", "status", self.cfg.release, "-n", self.cfg.namespace, "-o", "json"]
        )
        if proc.returncode != 0:
            return None, None
        try:
            parsed = json.loads(proc.stdout)
        except (ValueError, TypeError, json.JSONDecodeError):
            return None, None
        if not isinstance(parsed, dict):
            return None, None
        try:
            revision = int(parsed.get("version"))
        except (ValueError, TypeError):
            revision = None
        info = parsed.get("info")
        status = info.get("status") if isinstance(info, dict) else None
        return revision, status if isinstance(status, str) else None

    def _helm_revision(self: DeployRunner) -> int | None:
        """Thin, independently-testable accessor: revision only."""
        revision, _status = self._helm_status_info()
        return revision
