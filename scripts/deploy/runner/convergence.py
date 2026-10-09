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
    _CONSOLE_IMAGE_COMPONENTS,
    CHART_DIR,
    MEMORY_SERVER_COMPONENT,
    MEMORY_SERVER_CONTAINER,
)
from scripts.deploy.runner.images import mismatched_components
from scripts.deploy.runner.values import _values_file_args

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

    # Console-image mismatch detection USED TO live here as
    # ``_mismatched_console_images``, gated on the file-side
    # ``console.enabled`` (via ``console_image_digests``) — removed per
    # BFF-BUMP-1.29.1 Rule B1: that gate is exactly the mechanism that let a
    # stale stored BFF override survive every deploy that never passed
    # ``-f values-laptop.yaml``. Superseded by
    # :meth:`~scripts.deploy.runner.images.FirstPartyImagesMixin.
    # _first_party_image_rows`, gated on LIVE manifest presence instead —
    # see :meth:`_is_converged` below.

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
        self: DeployRunner, rows: list[dict[str, Any]]
    ) -> list[tuple[str, str, str]]:
        """``(component, deployment_name, container_name)`` triples to
        config-drift-check: memory-server ALWAYS, plus any console
        `librechat`/`bff` Deployment actually PRESENT in ``rows`` — the
        SAME manifest-derived first-party image rows
        (:meth:`~scripts.deploy.runner.images.FirstPartyImagesMixin.
        _first_party_image_rows`) the mismatch check above just computed.

        BFF-BUMP-1.29.1 Rule B1 supersedes the prior file-side
        ``console.enabled`` gate here too: presence is derived from the
        LIVE manifest, never the chart flag. Init-container rows (the
        librechat ``wait-for-oidc-discovery`` init container) are excluded
        — config-drift compares a workload's OWN main container spec, not
        an init container's. Falsifiable: derive this list from
        ``chart_values``'s ``console.enabled`` instead of ``rows`` and the
        neuter-proof test goes RED (a console Deployment absent from the
        manifest but ``console.enabled: true`` on disk would wrongly be
        config-drift-checked)."""
        workloads: list[tuple[str, str, str]] = [
            (MEMORY_SERVER_COMPONENT, self.cfg.deployment, MEMORY_SERVER_CONTAINER)
        ]
        seen = {MEMORY_SERVER_COMPONENT}
        for row in rows:
            component = row.get("component")
            if (
                row.get("is_init")
                or row.get("kind") != "Deployment"
                or component not in _CONSOLE_IMAGE_COMPONENTS
                or component in seen
            ):
                continue
            seen.add(component)
            workloads.append((component, row["workload"], row["container"]))
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
        self: DeployRunner, rows: list[dict[str, Any]]
    ) -> list[str]:
        """Component names whose INTENDED (rendered) container spec differs
        from the LIVE deployment's container spec (spec 2026-09-10-SPEC-
        deploy-runner-convergence-config-drift). Compares env / args /
        command / resources — the fields a config-only change moves — never
        the image field itself (digest convergence already covers that,
        earlier in :meth:`_is_converged`). Fail-safe: an unreadable/
        unparsable render or live read counts as drifted — unknown state
        must never read as converged. ``rows`` is the SAME manifest-derived
        first-party row set :meth:`_is_converged` computed for the image
        check (BFF-BUMP-1.29.1 Rule B1): "should I apply" and "did it
        converge" share one chokepoint for workload PRESENCE too, not just
        image equality."""
        workloads = self._first_party_config_workloads(rows)
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
        first-party image actually present in the LIVE manifest also
        matches its chart pin, AND the Helm release itself is ``deployed``
        (#451 + spec 2026-09-10 + BFF-BUMP-1.29.1 Rule B1).

        **First-party-image-complete, gated on LIVE presence (BFF-BUMP-
        1.29.1 Rule B1, superseding the 2026-09-10 ``console.enabled``
        gate).** Digest-keying the memory-server image alone is not
        enough: at the v1.26.0 WU-6 Part C.4 redeploy, memory-server was
        already converged while the `librechat` console pod had silently
        drifted to a stale digest, and P2 skipped ``helm upgrade``
        entirely. The FIRST fix (2026-09-10) compared every console image
        the FILE-side ``console.enabled`` flag said was enabled — but on
        every deploy that actually ran (no ``-f values-laptop.yaml``, base
        ``console.enabled: false``), that gate made the check a no-op too:
        a stale STORED BFF override (``--reset-then-reuse-values``) then
        survived every re-pin of this chart (gate B1, 2026-10-07). This
        method now calls
        :meth:`~scripts.deploy.runner.images.FirstPartyImagesMixin.
        _first_party_image_rows`, which derives presence from the LIVE
        ``helm get manifest`` — never the file flag — so a stale console
        image is caught whether or not any overlay was passed. ANY
        unequal row (:func:`~scripts.deploy.runner.images.
        mismatched_components`) means NOT converged, so ``helm upgrade``
        runs and reasserts the correct ``--set`` (:func:`console_image_set_args`
        is now unconditional too — D2). Third-party subchart images
        (postgres/redis/rabbitmq/vault) are out of scope by construction
        (:data:`~scripts.deploy.runner.images.FIRST_PARTY_PREFIXES`).

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

        Keys the top-level digest comparison on the digest (or live pod
        ``imageID``), never the mutable ``repository:tag`` — a re-pushed tag
        is therefore never falsely seen as converged. Falls back to
        tag-string comparison only when the digest is unresolved (local
        registry, soft-fail); the Helm-status requirement still applies on
        that path. Kept separate from (never folded into) the first-party
        row set below SOLELY so the pre-existing ``digest_matched``/
        ``reconcile_note`` bookkeeping (:mod:`scripts.deploy.runner.helm`)
        is unaffected — NOT because the two are claimed to agree: this
        top-level check and the rows below can disagree on a real cluster
        (the rows read the memory-server row too, via its own manifest
        selector), and the disagreement is SAFE only in one direction —
        this check says "match" while the rows say "mismatch" still yields
        NOT converged (the rows win, per D3/exit-8); the reverse can never
        happen because the rows are a strict superset check. No instrument
        here asserts the two "agree on a real cluster" — fix-round finding:
        that prose was unmeasured and, before B-1's selector fix, was
        actually false (the old jsonpath check matched while a mis-selected
        pod-reaper row mismatched).

        Only reads Helm status when the digest AND every first-party row
        already match — a mismatch runs ``helm upgrade`` unconditionally,
        so there is nothing to gain from an extra ``helm status`` call in
        that case.

        **Config-drift-complete (2026-09-10), workload presence now ALSO
        gated on the manifest-derived rows (Rule B1).** Image-digest keying
        alone cannot see a config-only chart change (env vars, args/
        command, resource limits — no image moves): observed live
        2026-09-10, the WU-5 sources-trailer redeploy (``AUDITTRACE_
        RESPONSE_SOURCES: off->trailer``, no image change) no-op'd because
        every first-party digest already matched. So once Helm reports the
        release ``deployed`` (i.e. there is otherwise nothing to
        reconcile), this method ALSO compares the INTENDED container spec
        — env, args/command, resources — of every first-party workload
        ACTUALLY PRESENT in ``rows`` (memory-server always; console
        `librechat`/`bff` whenever present in the live manifest) against
        its LIVE Deployment container spec (:meth:`_config_drifted_workloads`).
        ANY diff means NOT converged, so ``helm upgrade`` runs and
        reconciles it. This check runs LAST (after the Helm-status read,
        not before) so the existing status-based re-convergence paths
        above are unaffected — it only fires on the otherwise-would-be-noop
        case, keeping the true no-op (nothing changed at all) cheap and
        unchanged.
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
        rows = self._first_party_image_rows(chart_values=chart_values)
        mismatched = mismatched_components(rows)
        if mismatched:
            basis = f"{basis}; first-party mismatch: {', '.join(mismatched)}"
            return ConvergenceCheck(False, basis, None, False)
        if rows:
            matched = sorted({row["component"] for row in rows})
            basis = f"{basis}; first-party matched: {', '.join(matched)}"

        revision, status = self._helm_status_info()
        self.helm_revision = revision
        if status != _HELM_DEPLOYED_STATUS:
            return ConvergenceCheck(False, basis, status, True)

        drifted = self._config_drifted_workloads(rows)
        if drifted:
            basis = f"{basis}; config drift: {', '.join(sorted(drifted))}"
            return ConvergenceCheck(False, basis, status, False)

        return ConvergenceCheck(True, basis, status, True)
