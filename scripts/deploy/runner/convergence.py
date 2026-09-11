"""Convergence / idempotency: is the cluster already at the target state?

Holds the pure comparison helpers (config-drift normalisation, surge
counting) plus :class:`ConvergenceMixin`, mixed into
:class:`scripts.deploy.runner.orchestrator.DeployRunner`, which supplies
:meth:`_is_converged` and its supporting live-cluster reads.

``ConvergenceMixin`` methods call the ``_run``/``_read_chart_values`` seams
via the PACKAGE namespace (``_runner_pkg._run(...)``), never via a direct
``from ._exec import _run``-style binding — see ``_exec.py``'s module
docstring for why: a direct import would desync from
``monkeypatch.setattr(runner, "_run", ...)``, which only ever patches the
package's own attribute.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml

from scripts.deploy import runner as _runner_pkg
from scripts.deploy.runner.config import (
    _COMPONENT_SELECTOR,
    _CONSOLE_COMPONENT_SELECTORS,
    _CONSOLE_DEPLOYMENT_SUFFIXES,
    _CONSOLE_IMAGE_COMPONENTS,
    CHART_DIR,
    MEMORY_SERVER_COMPONENT,
    MEMORY_SERVER_CONTAINER,
)
from scripts.deploy.runner.values import _values_file_args, console_image_digests

if TYPE_CHECKING:
    from scripts.deploy.runner.orchestrator import DeployRunner

# NOTE ``_runner_pkg`` above is the PACKAGE (``scripts.deploy.runner``, i.e.
# this file's own ``__init__.py``), imported back into one of its own
# submodules — safe because Python registers a partially-initialised package
# in ``sys.modules`` before executing its ``__init__.py`` body (verified: a
# submodule importing its own in-progress parent package sees live attribute
# updates made to that parent afterwards). Every call below goes through
# ``_runner_pkg._run(...)`` / ``_runner_pkg._read_chart_values(...)`` — never
# a bare ``_run(...)`` — so ``monkeypatch.setattr(runner, "_run", stub)`` in
# the test suite (which only ever patches the package's own attribute)
# reaches these methods. See ``_exec.py``'s module docstring.

# The ONLY Helm release status that counts as "converged" (#451). Anything
# else — failed / pending-install / pending-upgrade / pending-rollback / an
# unreadable-or-unknown status — must NOT be recorded as noop, or a release
# stuck mid-way stays stuck across every re-run (the #451 root cause).
_HELM_DEPLOYED_STATUS = "deployed"


@dataclass(frozen=True)
class ConvergenceCheck:
    """Result of :meth:`DeployRunner._is_converged` (#451).

    ``digest_matched`` is carried separately from ``converged`` so the caller
    can tell "digest matches but the Helm release itself is not `deployed`"
    (re-converge — gets a DISTINCT record detail explaining why an upgrade
    ran on a digest-matched release) apart from an ordinary digest mismatch
    (unchanged re-deploy path, no such note needed).
    """

    converged: bool
    basis: str
    helm_status: str | None
    digest_matched: bool


def max_concurrent_running(samples: list[list[str]]) -> int:
    """Peak count of pods in phase ``Running`` across all samples."""
    return max(
        (sum(1 for phase in s if phase == "Running") for s in samples), default=0
    )


def within_surge_bound(samples: list[list[str]], replicas: int) -> tuple[bool, int]:
    """Return ``(ok, peak)`` — the WS1 surge-safe assertion.

    ``ok`` is False when the peak concurrent Running count exceeds
    ``spec.replicas`` (i.e. a surge was observed — two memory-server pods where
    the chart guarantees at most one on a single-node cluster).
    """
    peak = max_concurrent_running(samples)
    return peak <= replicas, peak


def extract_digest(image_id: str | None) -> str | None:
    """Pull the ``sha256:...`` component out of a k8s container ``imageID``.

    ``imageID`` is ``repo@sha256:hex`` (or a bare ``sha256:hex``) and reflects
    what is ACTUALLY running, regardless of how the image was specified — the
    right thing to key digest-convergence on.
    """
    if not image_id:
        return None
    _, sep, tail = image_id.partition("@")
    candidate = tail if sep else image_id
    return candidate if candidate.startswith("sha256:") else None


def _find_container(containers: list[Any] | None, name: str) -> dict[str, Any] | None:
    """The container named ``name`` out of a Deployment's container list, or
    ``None`` if absent/malformed. Only ever looks at the top-level
    ``containers`` list, never ``initContainers`` — so an init container that
    happens to share a name is never mistaken for the workload's own
    container (spec 2026-09-10-SPEC-deploy-runner-convergence-config-drift)."""
    for container in containers or []:
        if isinstance(container, dict) and container.get("name") == name:
            return container
    return None


def _normalize_env(env: list[Any] | None) -> dict[str, Any]:
    """``env`` as a name -> value dict, ignoring declaration order (spec
    2026-09-10). A plain ``value`` entry is kept verbatim; a ``valueFrom``
    reference (``secretKeyRef`` / ``configMapKeyRef`` / ``fieldRef``) is kept
    as its own sub-dict — the comparison is over the REFERENCE (secret/key
    name), never a secret's plaintext content, which this runner never reads.
    """
    result: dict[str, Any] = {}
    for item in env or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        if "value" in item:
            result[name] = item["value"]
        elif "valueFrom" in item:
            result[name] = {"valueFrom": item["valueFrom"]}
    return result


def _normalize_resources(resources: Any) -> dict[str, Any]:
    """``resources`` as ``{"requests": {...}, "limits": {...}}`` — the maps a
    CPU/memory config change would move. Anything malformed (not a mapping,
    or a non-mapping ``requests``/``limits``) degrades to an empty map rather
    than raising or comparing unequal by accident of shape."""
    if not isinstance(resources, dict):
        return {"requests": {}, "limits": {}}
    requests = resources.get("requests")
    limits = resources.get("limits")
    return {
        "requests": dict(requests) if isinstance(requests, dict) else {},
        "limits": dict(limits) if isinstance(limits, dict) else {},
    }


def _normalize_container_spec(container: dict[str, Any]) -> dict[str, Any]:
    """The config-drift-relevant subset of a container spec: env (as a
    name-keyed dict), args, command, resources. Deliberately never the
    ``image`` field — digest convergence already covers that, earlier in
    :meth:`DeployRunner._is_converged` (spec 2026-09-10-SPEC-deploy-runner-
    convergence-config-drift)."""
    return {
        "env": _normalize_env(container.get("env")),
        "args": list(container.get("args") or []),
        "command": list(container.get("command") or []),
        "resources": _normalize_resources(container.get("resources")),
    }


def _container_spec_from_deployment_doc(
    doc: dict[str, Any] | None, container_name: str
) -> dict[str, Any] | None:
    """Pull + normalise one named container's config-relevant spec out of a
    Deployment manifest doc (either a rendered `helm template` doc or a live
    `kubectl get deployment -o json` doc — both share the same
    ``spec.template.spec.containers`` shape). Returns ``None`` when the doc,
    its pod spec, or the named container is missing/malformed — the caller
    treats that as an unknown state (fail-safe drift), never a silent match.
    """
    if not isinstance(doc, dict):
        return None
    spec = doc.get("spec")
    template = spec.get("template") if isinstance(spec, dict) else None
    pod_spec = template.get("spec") if isinstance(template, dict) else None
    containers = pod_spec.get("containers") if isinstance(pod_spec, dict) else None
    container = _find_container(containers, container_name)
    return _normalize_container_spec(container) if container is not None else None


def _find_deployment_doc(
    docs: list[dict[str, Any]] | None, name: str
) -> dict[str, Any] | None:
    """The rendered ``Deployment`` doc named ``name`` out of a multi-doc
    `helm template` render, or ``None`` if absent/unreadable. ``docs=None``
    (an unreadable/unparsable render) degrades to "not found" here, same as
    an empty list — the caller (``_config_drifted_workloads``) is what turns
    that into fail-safe drift, not this lookup."""
    for doc in docs or []:
        if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
            continue
        metadata = doc.get("metadata")
        if isinstance(metadata, dict) and metadata.get("name") == name:
            return doc
    return None


class ConvergenceMixin:
    """:class:`DeployRunner` methods answering "is the cluster already at the
    target state?" — mixed into the orchestrator so ``self.cfg``/``self.image_ref``/
    ``self.helm_revision`` are shared with the P0-P5 phase methods.
    """

    # -- convergence (idempotency) --

    def _running_image(self: DeployRunner) -> str | None:
        """The deployment's configured image string (tag-form convergence, local)."""
        jsonpath = (
            '{.spec.template.spec.containers[?(@.name=="'
            + MEMORY_SERVER_CONTAINER
            + '")].image}'
        )
        proc = _runner_pkg._run(
            [
                "kubectl",
                "get",
                "deployment",
                self.cfg.deployment,
                "-n",
                self.cfg.namespace,
                "-o",
                f"jsonpath={jsonpath}",
            ]
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def _running_component_digest(
        self: DeployRunner, selector: str, container: str
    ) -> str | None:
        """The ``sha256:...`` actually running for an arbitrary
        component/container pair, from the live pod ``imageID``.

        Generalises the memory-server-only jsonpath read so the
        first-party-image-complete convergence check (spec 2026-09-10) can
        read a console component's live digest through the identical
        kubectl call shape, without duplicating it. :meth:`_running_image_digest`
        is this method specialised to the memory-server component.
        """
        jsonpath = (
            '{.items[*].status.containerStatuses[?(@.name=="'
            + container
            + '")].imageID}'
        )
        proc = _runner_pkg._run(
            [
                "kubectl",
                "get",
                "pods",
                "-l",
                selector,
                "-n",
                self.cfg.namespace,
                "-o",
                f"jsonpath={jsonpath}",
            ]
        )
        if proc.returncode != 0:
            return None
        # Multiple pods -> space-separated imageIDs; take the first with a digest.
        for token in proc.stdout.split():
            digest = extract_digest(token)
            if digest:
                return digest
        return None

    def _running_image_digest(self: DeployRunner) -> str | None:
        """The ``sha256:...`` actually running, from the live pod ``imageID``."""
        return self._running_component_digest(
            _COMPONENT_SELECTOR, MEMORY_SERVER_CONTAINER
        )

    def _mismatched_console_images(
        self: DeployRunner, pinned: dict[str, str]
    ) -> list[str]:
        """Component names (of ``pinned``) whose live pod ``imageID`` differs
        from the chart-pinned digest (spec 2026-09-10-SPEC-deploy-runner-
        convergence-first-party-image-complete).

        ``pinned`` is normally :func:`console_image_digests` applied to the
        committed chart ``values.yaml`` — the exact digests
        :func:`console_image_set_args` would ``--set``. An unreadable live
        ``imageID`` (kubectl error, no matching pod, ...) counts as a
        mismatch — fail-safe: unknown state must never be read as converged.
        """
        mismatched: list[str] = []
        for component, digest in pinned.items():
            selector, container = _CONSOLE_COMPONENT_SELECTORS[component]
            running = self._running_component_digest(selector, container)
            if running != digest:
                mismatched.append(component)
        return mismatched

    # -- config-drift (env / args / command / resources) convergence ----------
    # spec 2026-09-10-SPEC-deploy-runner-convergence-config-drift: a
    # config-only chart change (e.g. `memoryServer.env.AUDITTRACE_RESPONSE_
    # SOURCES: off->trailer`) moves no first-party image digest, so the
    # digest + console-image checks above see nothing to reconcile and P2
    # no-ops — the config change never reaches the cluster (observed live
    # 2026-09-10: the WU-5 trailer redeploy no-op'd, rev stayed 266). This
    # closes that gap by ALSO comparing the intended (rendered) vs live
    # container spec of every enabled first-party workload.

    def _first_party_config_workloads(
        self: DeployRunner, chart_values: dict[str, Any]
    ) -> list[tuple[str, str, str]]:
        """``(component, deployment_name, container_name)`` triples to
        config-drift-check: memory-server ALWAYS, plus the console
        `librechat`/`bff` deployments when ``console.enabled`` (mirroring
        :func:`console_image_digests`'s own enabled-gate exactly). Third-party
        subcharts (postgres/redis/rabbitmq/vault) are out of scope, matching
        :func:`console_image_set_args`."""
        workloads: list[tuple[str, str, str]] = [
            (MEMORY_SERVER_COMPONENT, self.cfg.deployment, MEMORY_SERVER_CONTAINER)
        ]
        console = chart_values.get("console")
        if isinstance(console, dict) and console.get("enabled"):
            for component in _CONSOLE_IMAGE_COMPONENTS:
                suffix = _CONSOLE_DEPLOYMENT_SUFFIXES[component]
                workloads.append((component, f"{self.cfg.release}-{suffix}", component))
        return workloads

    def _render_chart_manifest(self: DeployRunner) -> list[dict[str, Any]] | None:
        """The INTENDED manifest, via `helm template` on the identical
        effective values source :func:`_read_chart_values` /
        :func:`_helm_apply_cmd` already read — base ``values.yaml`` deep-merged
        with ``cfg.values_files`` overlays, in order, via the same ``-f``
        argv `_helm_apply_cmd` passes. Never talks to the live cluster:
        Helm's ``lookup`` function always resolves empty under `helm
        template` (no ``--dry-run=server``), so this render is deterministic
        and reproducible offline regardless of what is actually running —
        unlike the digest checks above, which read live cluster state.
        Returns ``None`` (fail-safe unknown) on any helm failure or
        unparsable output; the caller treats that as drift, never a silent
        match."""
        cmd = [
            "helm",
            "template",
            self.cfg.release,
            str(CHART_DIR),
            "-n",
            self.cfg.namespace,
            *_values_file_args(self.cfg.values_files),
        ]
        proc = _runner_pkg._run(cmd)
        if proc.returncode != 0:
            return None
        try:
            docs = list(yaml.safe_load_all(proc.stdout))
        except yaml.YAMLError:
            return None
        return [doc for doc in docs if isinstance(doc, dict)]

    def _live_deployment_container_spec(
        self: DeployRunner, deployment_name: str, container_name: str
    ) -> dict[str, Any] | None:
        """The LIVE, normalised container spec for one Deployment, read via
        `kubectl get deployment ... -o json` — a Deployment object, never a
        Pod, so an Istio-injected sidecar container can never be mistaken for
        the workload's own container."""
        proc = _runner_pkg._run(
            [
                "kubectl",
                "get",
                "deployment",
                deployment_name,
                "-n",
                self.cfg.namespace,
                "-o",
                "json",
            ]
        )
        if proc.returncode != 0:
            return None
        try:
            parsed = json.loads(proc.stdout)
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
        return _container_spec_from_deployment_doc(parsed, container_name)

    def _config_drifted_workloads(
        self: DeployRunner, chart_values: dict[str, Any]
    ) -> list[str]:
        """Component names whose INTENDED (rendered) container spec differs
        from the LIVE deployment's container spec (spec 2026-09-10-SPEC-
        deploy-runner-convergence-config-drift). Compares env / args /
        command / resources — the fields a config-only change moves — never
        the image field itself (digest convergence already covers that,
        earlier in :meth:`_is_converged`). Fail-safe: an unreadable/
        unparsable render or live read counts as drifted, exactly like
        :meth:`_mismatched_console_images` — unknown state must never read
        as converged."""
        workloads = self._first_party_config_workloads(chart_values)
        docs = self._render_chart_manifest()
        drifted: list[str] = []
        for component, deployment_name, container_name in workloads:
            intended = _container_spec_from_deployment_doc(
                _find_deployment_doc(docs, deployment_name), container_name
            )
            live = self._live_deployment_container_spec(deployment_name, container_name)
            if intended is None or live is None or intended != live:
                drifted.append(component)
        return drifted

    def _is_converged(
        self: DeployRunner, chart_values: dict[str, Any] | None = None
    ) -> ConvergenceCheck:
        """Converged when the LIVE digest equals the resolved digest, EVERY
        enabled first-party console image also matches its chart pin, AND
        the Helm release itself is ``deployed`` (#451 + spec 2026-09-10).

        **First-party-image-complete (2026-09-10).** Digest-keying the
        memory-server image alone is not enough: at the v1.26.0 WU-6 Part
        C.4 redeploy, memory-server was already converged while the
        `librechat` console pod had silently drifted to a stale digest —
        ``_is_converged()`` reported converged, P2 skipped ``helm upgrade``
        entirely, and the already-merged console ``--set`` fix
        (:func:`console_image_set_args`) never ran to correct the drift.
        So after the memory-server digest matches, every ENABLED console
        component's live pod ``imageID`` is compared against the digest
        pinned in the committed chart ``values.yaml`` (:func:`console_image_digests`
        — the SAME source :func:`console_image_set_args` reads; never
        re-resolved from the registry). ANY first-party mismatch means NOT
        converged, so ``helm upgrade`` runs and reasserts the correct
        ``--set``. ``console.enabled=false`` skips the console checks
        entirely (an absent, un-templated component is not a mismatch).
        Third-party subchart images (postgres/redis/rabbitmq/vault) are
        out of scope, matching :func:`console_image_set_args`.

        Digest match alone is NOT sufficient: a release stuck in ``failed`` /
        ``pending-install`` / ``pending-upgrade`` / ``pending-rollback`` — e.g.
        a prior ``helm upgrade`` timed out, and with no ``--atomic`` there is
        no rollback — can still have rolled its pods to the target image.
        Recording that as ``noop`` would skip the very ``helm upgrade`` that
        reconciles the release back to ``deployed``, leaving it stuck across
        every re-run (the #451 root cause). An unreadable/unknown Helm status
        is likewise treated as NOT converged — fail-safe: ``helm upgrade
        --install`` is idempotent, so a redundant upgrade is cheap and a false
        noop is not.

        Keys the digest comparison on the digest (or live pod ``imageID``),
        never the mutable ``repository:tag`` — a re-pushed tag is therefore
        never falsely seen as converged. Falls back to tag-string comparison
        only when the digest is unresolved (local registry, soft-fail); the
        Helm-status requirement still applies on that path.

        Only reads Helm status when the digest AND every console image
        already match — a mismatch on any first-party image runs
        ``helm upgrade`` unconditionally, so there is nothing to gain from an
        extra ``helm status`` call in that case. ``basis`` records which
        image(s) drove the verdict (memory-server digest always; the console
        components too, whenever console is enabled).

        **Config-drift-complete (2026-09-10).** Image-digest keying alone
        cannot see a config-only chart change (env vars, args/command,
        resource limits — no image moves): observed live 2026-09-10, the
        WU-5 sources-trailer redeploy (``AUDITTRACE_RESPONSE_SOURCES:
        off->trailer``, no image change) no-op'd because every first-party
        digest already matched. So once Helm reports the release
        ``deployed`` (i.e. there is otherwise nothing to reconcile), this
        method ALSO compares the INTENDED container spec — env, args/
        command, resources — of every enabled first-party workload
        (memory-server + console `librechat`/`bff`) against its LIVE
        Deployment container spec (:meth:`_config_drifted_workloads`). ANY
        diff on ANY first-party workload means NOT converged, so ``helm
        upgrade`` runs and reconciles it. This check runs LAST (after the
        Helm-status read, not before) so the existing status-based re-
        convergence paths above are unaffected — it only fires on the
        otherwise-would-be-noop case, keeping the true no-op (nothing
        changed at all) cheap and unchanged.
        """
        assert self.image_ref is not None
        if self.image_ref.digest:
            running = self._running_image_digest()
            digest_matched = running == self.image_ref.digest
            basis = f"digest {self.image_ref.digest}"
        else:
            intended = f"{self.image_ref.repository}:{self.cfg.image_tag}"
            digest_matched = self._running_image() == intended
            basis = f"tag {intended}"

        if not digest_matched:
            return ConvergenceCheck(False, basis, None, False)

        if chart_values is None:
            chart_values = _runner_pkg._read_chart_values(
                values_files=self.cfg.values_files
            )
        pinned = console_image_digests(chart_values)
        if pinned:
            mismatched = self._mismatched_console_images(pinned)
            if mismatched:
                basis = f"{basis}; console mismatch: {', '.join(sorted(mismatched))}"
                return ConvergenceCheck(False, basis, None, False)
            basis = f"{basis}; console matched: {', '.join(sorted(pinned))}"

        revision, status = self._helm_status_info()
        self.helm_revision = revision
        if status != _HELM_DEPLOYED_STATUS:
            return ConvergenceCheck(False, basis, status, True)

        drifted = self._config_drifted_workloads(chart_values)
        if drifted:
            basis = f"{basis}; config drift: {', '.join(sorted(drifted))}"
            return ConvergenceCheck(False, basis, status, False)

        return ConvergenceCheck(True, basis, status, True)
