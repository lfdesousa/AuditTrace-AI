"""The SHARED first-party-image predicate (spec 2026-10-07-SPEC-bff-bump-
1.29.1-and-stale-override-guard.md, Rule B1 of Addendum A).

Before this module, the deploy runner's no-op decision
(:meth:`~scripts.deploy.runner.convergence.ConvergenceMixin._is_converged`)
and the intended post-apply equality check both keyed console-image
presence on the FILE-side ``console.enabled`` flag. On the laptop target
that flag is FALSE in base ``values.yaml`` and only ever flips TRUE via a
``-f values-laptop.yaml`` overlay neither the deployer definition nor any
CD agent actually passes — so both checks were permanently blind to the
console on every deploy that ran (BFF-BUMP-1.29.1 gate B1, re-pinning the
chart alone would have no-op'd straight past a stale stored BFF override).

:class:`FirstPartyImagesMixin` (mixed into :class:`DeployRunner` — its
:meth:`~FirstPartyImagesMixin._first_party_image_rows` is the entry point)
replaces BOTH checks with ONE predicate, derived from the LIVE
``helm get manifest`` — never the file flag:

* a row exists for a container/init-container IFF it is actually present in
  the live manifest AND its image repository is first-party (a repository
  already in the committed map, OR matching
  :func:`_configured_first_party_prefixes` — portability fix-round
  should-fix: a committed mirror repository is recognised without a code
  change);
* its ``chart`` side is the value :func:`committed_first_party_digests`
  derives from the committed chart values (or the resolved memory-server
  digest) — never re-resolved from the registry a second time;
* its ``live`` side is the pod ``imageID`` actually running, read through
  THIS WORKLOAD'S OWN ``spec.selector.matchLabels``
  (:func:`_selector_from_manifest_doc`) — never a component-keyed lookup
  table (fix-round B-1: the pod-reaper runs the memory-server IMAGE under
  its own, DISTINCT selector; a table keyed by the image's component read
  memory-server's pods for it and reported a false mismatch on every real
  deploy). Selected per the S1 pod-selection rule
  (:meth:`FirstPartyImagesMixin._live_row_digest`): excluding any
  Terminating pod, restricted to the Deployment's CURRENT ReplicaSet
  (matched by the ``deployment.kubernetes.io/revision`` annotation, never
  creation-timestamp order — S-F2), requiring every remaining pod to agree;
* a Deployment/StatefulSet scaled to zero replicas marks its row
  ``scaled_to_zero`` (live not compared, S-F3) rather than ``unreadable``;
  a workload whose manifest doc carries no selector at all (a Job/CronJob,
  e.g. the ``tests`` Helm-test-hook) marks its row ``render_only``;
* a first-party repository present in the manifest but absent from the
  committed map is ``unknown_first_party`` — ALWAYS counted as a mismatch
  (S-F1's completeness pin: a newly-enabled first-party image the 4-key
  enumeration forgot about is flagged, never silently skipped);
* an UNREADABLE manifest (``helm get manifest`` itself failed) is its OWN
  unequal sentinel row (``status="manifest_unreadable"``) — never ``[]``
  (fix-round B-2: ``[]`` read as "nothing to compare", which let a stale
  BFF survive a transient `helm get manifest` failure with exit 0).

Convergence and the post-apply settle check both call this SAME function on
the SAME manifest read, so "should I apply" and "did it converge" can never
disagree (``feedback_a_chokepoint_is_per_domain_never_inherited``). The two
CAN still disagree with the pre-existing, independent memory-server-only
jsonpath check inside ``_is_converged`` — that disagreement is SAFE (it can
only ever turn a would-be noop into an apply, never the reverse) and is
measured per-case in this module's and ``convergence.py``'s own tests,
never asserted as a blanket "they agree on a real cluster" (fix-round
finding: that prose was unmeasured and, on the real chart before B-1's
fix, actually false).
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import yaml

from scripts.deploy import registry
from scripts.deploy import runner as _runner_pkg
from scripts.deploy.runner.config import MEMORY_SERVER_COMPONENT

if TYPE_CHECKING:
    from scripts.deploy.runner.orchestrator import DeployRunner

# NOTE: like ``convergence.py`` and ``helm.py``, every ``_run``/
# ``_read_chart_values`` call below goes through ``_runner_pkg.<name>(...)``
# (the PACKAGE namespace) rather than a direct ``from ._exec import _run``-
# style binding, so ``monkeypatch.setattr(runner, "_run", stub)`` in the
# test suite reaches this module too. ``extract_digest`` is imported LAZILY
# inside :meth:`FirstPartyImagesMixin._live_row_digest` (not at module
# level) because it lives in ``convergence.py``, which itself needs
# :func:`mismatched_components` from THIS module for its own
# ``_is_converged`` — a module-level import either way would be circular;
# deferring one side to call-time (after both modules have finished
# executing) breaks the cycle. See ``_exec.py``'s module docstring.

# Repository prefixes this runner treats as FIRST-PARTY — ours to pin and
# compare. docker.io/lfds/* is the Hub namespace every published image
# lives in (publish.yml); localhost:5000/audittrace/* is the k3s dev
# mirror (--registry local). Any other prefix (postgres/redis/rabbitmq/
# vault subcharts, istio/proxyv2, ...) is third-party and out of scope by
# construction — it can never match either prefix. This is the LAPTOP
# default; a target with a committed mirror repository (e.g. an ECR
# namespace) is covered WITHOUT a code change two ways (portability
# invariant, fix-round should-fix): (1) any repository already present in
# ``committed_first_party_digests``'s map is first-party regardless of
# prefix (see ``_first_party_image_rows``'s gating check) — a mirror
# committed in a target's own overlay is never silently dropped; (2) the
# prefix LIST itself is configurable via
# :data:`FIRST_PARTY_PREFIXES_ENV_VAR` for a repository that is first-party
# but not YET in the committed map (e.g. during a migration).
FIRST_PARTY_PREFIXES = ("docker.io/lfds/", "localhost:5000/audittrace/")

FIRST_PARTY_PREFIXES_ENV_VAR = "AUDITTRACE_FIRST_PARTY_IMAGE_PREFIXES"


def _configured_first_party_prefixes() -> tuple[str, ...]:
    """:data:`FIRST_PARTY_PREFIXES`, extended by a comma-separated
    ``AUDITTRACE_FIRST_PARTY_IMAGE_PREFIXES`` override (additive — the
    laptop defaults are never dropped, only ever extended) so a second
    target's prefix is a config value, never a code change."""
    raw = os.environ.get(FIRST_PARTY_PREFIXES_ENV_VAR, "")
    extra = tuple(p.strip() for p in raw.split(",") if p.strip())
    return FIRST_PARTY_PREFIXES + extra


# The manifest kinds this module ever looks at. Secret docs are NEVER
# parsed into a row — not filtered out of evidence after the fact, simply
# never visited at all (AC3e).
_WORKLOAD_KINDS = frozenset(
    {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
)


def _parse_image_ref(image: str) -> tuple[str, str | None, str | None]:
    """Split a rendered ``repo[:tag][@sha256:hex]`` image string.

    The repository itself may carry a port (``localhost:5000/...``), so the
    tag separator is only ever looked for AFTER the final ``/`` — never a
    bare ``rpartition(":")`` over the whole string, which would otherwise
    misparse ``localhost:5000/audittrace/memory-server`` (no tag) as
    repository ``localhost`` / tag ``5000/audittrace/memory-server``.
    """
    repo_tag, _, digest_part = image.partition("@")
    digest = digest_part or None
    tail = repo_tag.rsplit("/", 1)[-1]
    if ":" in tail:
        repo, _, tag = repo_tag.rpartition(":")
        return repo, tag or None, digest
    return repo_tag, None, digest


def _pod_spec_of(doc: dict[str, Any]) -> dict[str, Any] | None:
    """The ``spec.template.spec`` pod spec out of a workload manifest doc.

    CronJob nests one level deeper (``spec.jobTemplate.spec.template.spec``);
    every other workload kind this module looks at (Deployment/StatefulSet/
    DaemonSet/Job) shares the flat ``spec.template.spec`` shape. Returns
    ``None`` on anything malformed — the caller treats a missing pod spec as
    "no containers to examine", never a crash.
    """
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return None
    if doc.get("kind") == "CronJob":
        job_template = spec.get("jobTemplate")
        spec = job_template.get("spec") if isinstance(job_template, dict) else None
        if not isinstance(spec, dict):
            return None
    template = spec.get("template")
    pod_spec = template.get("spec") if isinstance(template, dict) else None
    return pod_spec if isinstance(pod_spec, dict) else None


def _manifest_workload_docs(
    docs: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Workload-kind docs only (never ``Secret``, never anything unknown)."""
    return [
        doc
        for doc in docs or []
        if isinstance(doc, dict) and doc.get("kind") in _WORKLOAD_KINDS
    ]


def committed_first_party_digests(
    chart_values: dict[str, Any], image_ref: registry.ImageRef | None
) -> dict[str, dict[str, Any]]:
    """Repository -> ``{"key", "digest", "tag"}`` for every first-party image
    this runner commits to, derived from the chart values themselves (the
    console/tests images) and the RESOLVED memory-server reference (never
    re-read from the chart's own ``memoryServer.image.repository`` key — the
    runner's apply already keys memory-server on ``image_ref``, including
    under ``--registry local`` where the repository differs from hub's).

    ``digest`` is ``None`` only for an unpinned local-registry memory-server
    reference (soft-failed digest resolution) — the caller falls back to
    tag-form equality and skips the live read for that row (S3a).
    """
    committed: dict[str, dict[str, Any]] = {}
    if image_ref is not None and image_ref.repository:
        committed[image_ref.repository] = {
            "key": MEMORY_SERVER_COMPONENT,
            "digest": image_ref.digest,
            "tag": image_ref.tag,
        }
    console = chart_values.get("console") if isinstance(chart_values, dict) else None
    if isinstance(console, dict):
        for component in ("librechat", "bff"):
            block = console.get(component)
            image = block.get("image") if isinstance(block, dict) else None
            repository = image.get("repository") if isinstance(image, dict) else None
            if repository:
                committed[repository] = {
                    "key": component,
                    "digest": image.get("digest") or None,
                    "tag": image.get("tag"),
                }
    tests_block = chart_values.get("tests") if isinstance(chart_values, dict) else None
    tests_image = tests_block.get("image") if isinstance(tests_block, dict) else None
    tests_repository = (
        tests_image.get("repository") if isinstance(tests_image, dict) else None
    )
    if tests_repository:
        committed[tests_repository] = {
            "key": "tests",
            "digest": tests_image.get("digest") or None,
            "tag": tests_image.get("tag"),
        }
    return committed


def _selector_from_manifest_doc(doc: dict[str, Any]) -> str | None:
    """The pod label selector for THIS workload, read straight off ITS OWN
    ``spec.selector.matchLabels`` in the manifest — never a static
    component -> selector table (fix-round B-1: the pod-reaper runs the
    memory-server IMAGE in a container named ``pod-reaper``, but it is its
    OWN Deployment with its OWN selector
    (``app.kubernetes.io/component=pod-reaper``, distinct from
    memory-server's ``...=memory-server``); a table keyed by the IMAGE's
    component name reads the wrong pods for it.

    Uses ONLY the ``app.kubernetes.io/component`` key out of
    ``matchLabels`` — this chart's own
    ``_helpers.tpl``/``audittrace.selectorLabels`` convention makes that
    one key unique per workload, and a single-label selector is already a
    fully valid ``kubectl -l`` filter (the extra ``name``/``instance``
    labels every workload also sets add no further discrimination within
    one release/namespace). Every Deployment/StatefulSet/DaemonSet this
    chart renders sets ``spec.selector.matchLabels`` explicitly; a
    Job/CronJob never carries one in a `helm template`/`helm get manifest`
    render (Kubernetes defaults it server-side) — this returns ``None``
    for those, and for any doc missing the component key, so the row
    stays ``render_only``.

    Falsifiable: key the selector off ``component`` (the image's identity)
    instead of the workload's own manifest selector, and the pod-reaper
    row goes back to reading memory-server's pods — RED on
    ``test_neuter_component_selector_table_breaks_the_pod_reaper_row``.
    """
    spec = doc.get("spec")
    selector = spec.get("selector") if isinstance(spec, dict) else None
    match_labels = selector.get("matchLabels") if isinstance(selector, dict) else None
    if not isinstance(match_labels, dict):
        return None
    component = match_labels.get("app.kubernetes.io/component")
    if not component:
        return None
    return f"app.kubernetes.io/component={component}"


def mismatched_components(rows: list[dict[str, Any]]) -> list[str]:
    """Sorted, de-duplicated component names of every unequal row — the
    basis text and the apply-vs-noop decision both read this, never a raw
    row count (so two unequal rows on the same component name once, not
    twice)."""
    return sorted({row["component"] for row in rows if not row["equal"]})


class FirstPartyImagesMixin:
    """:class:`DeployRunner` methods computing the shared first-party image
    rows (Rule B1) — mixed in alongside
    :class:`~scripts.deploy.runner.convergence.ConvergenceMixin` and
    :class:`~scripts.deploy.runner.helm.HelmMixin` so ``self.cfg`` /
    ``self.image_ref`` are shared."""

    def _live_manifest_docs(self: DeployRunner) -> list[dict[str, Any]] | None:
        """The CURRENTLY-DEPLOYED manifest (``helm get manifest``, no
        ``--revision`` — the latest release), never the rendered-but-not-yet-
        applied ``helm template`` output. ``None`` (fail-safe unknown) on any
        helm failure or unparsable output; the caller never reads that as "no
        first-party rows", it reads it as "nothing could be checked"."""
        proc = _runner_pkg._run(
            ["helm", "get", "manifest", self.cfg.release, "-n", self.cfg.namespace]
        )
        if proc.returncode != 0:
            return None
        try:
            docs = list(yaml.safe_load_all(proc.stdout))
        except yaml.YAMLError:
            return None
        return [doc for doc in docs if isinstance(doc, dict)]

    def _deployment_revision(self: DeployRunner, name: str) -> str | None:
        proc = _runner_pkg._run(
            [
                "kubectl",
                "get",
                "deployment",
                name,
                "-n",
                self.cfg.namespace,
                "-o",
                "jsonpath={.metadata.annotations['deployment.kubernetes.io/revision']}",
            ]
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def _current_pod_template_hash(
        self: DeployRunner, deployment_name: str, selector: str
    ) -> str | None:
        """The ``pod-template-hash`` of the ReplicaSet whose own
        ``deployment.kubernetes.io/revision`` annotation matches the
        Deployment's CURRENT revision (S-F2) — never
        ``--sort-by=.metadata.creationTimestamp``, which picks the WRONG
        ReplicaSet after a ``rollout undo`` (the old RS is RE-SCALED, not
        re-created, so it is not the newest by creation time either).
        ``None`` (unreadable/no match) makes the caller fall back to "every
        non-Terminating pod on the selector", never a crash."""
        revision = self._deployment_revision(deployment_name)
        if revision is None:
            return None
        proc = _runner_pkg._run(
            [
                "kubectl",
                "get",
                "rs",
                "-l",
                selector,
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
        except (ValueError, TypeError):
            return None
        items = parsed.get("items") if isinstance(parsed, dict) else None
        for item in items or []:
            if not isinstance(item, dict):
                continue
            annotations = (item.get("metadata") or {}).get("annotations") or {}
            if annotations.get("deployment.kubernetes.io/revision") == revision:
                labels = (item.get("metadata") or {}).get("labels") or {}
                pod_hash = labels.get("pod-template-hash")
                if pod_hash:
                    return pod_hash
        return None

    def _selector_pods(self: DeployRunner, selector: str) -> list[dict[str, Any]]:
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
                "json",
            ]
        )
        if proc.returncode != 0:
            return []
        try:
            parsed = json.loads(proc.stdout)
        except (ValueError, TypeError):
            return []
        items = parsed.get("items") if isinstance(parsed, dict) else None
        return [item for item in items or [] if isinstance(item, dict)]

    def _live_row_digest(
        self: DeployRunner,
        *,
        selector: str | None,
        kind: str,
        name: str,
        container: str,
        is_init: bool,
    ) -> tuple[str | None, str]:
        """``(digest, status)`` for one row's LIVE side (S1).

        ``selector`` is THIS workload's own ``spec.selector.matchLabels``
        (:func:`_selector_from_manifest_doc`) — never a component-keyed
        lookup table (B-1: the pod-reaper runs the memory-server image
        under its OWN, distinct selector; a table keyed by the image's
        component would read memory-server's pods instead). ``None`` means
        the manifest doc carries no selector at all (a Job/CronJob) — the
        row stays ``render_only``.

        Excludes any pod with ``metadata.deletionTimestamp`` set (a
        Terminating old pod must never mask an already-Running new one);
        for a Deployment, restricts to the pods of the CURRENT ReplicaSet
        (:meth:`_current_pod_template_hash`, S-F2); requires every
        remaining pod to report the SAME digest. ``status`` is ``"ok"``
        (single agreeing digest), ``"render_only"`` (no selector on this
        workload — comparison is render-level only), or ``"unreadable"``
        (zero matching pods, an unreadable kubectl call, or disagreement
        among the matched pods — fail-safe: unknown state is never read as
        equal).
        """
        from scripts.deploy.runner.convergence import extract_digest

        if selector is None:
            return None, "render_only"
        current_hash = (
            self._current_pod_template_hash(name, selector)
            if kind == "Deployment"
            else None
        )
        candidates: list[dict[str, Any]] = []
        for pod in self._selector_pods(selector):
            metadata = pod.get("metadata") or {}
            if metadata.get("deletionTimestamp"):
                continue
            if current_hash is not None:
                labels = metadata.get("labels") or {}
                if labels.get("pod-template-hash") != current_hash:
                    continue
            candidates.append(pod)
        if not candidates:
            return None, "unreadable"
        status_key = "initContainerStatuses" if is_init else "containerStatuses"
        digests: set[str] = set()
        for pod in candidates:
            statuses = (pod.get("status") or {}).get(status_key) or []
            match = next(
                (
                    s
                    for s in statuses
                    if isinstance(s, dict) and s.get("name") == container
                ),
                None,
            )
            digest = extract_digest(match.get("imageID")) if match else None
            if digest is None:
                return None, "unreadable"
            digests.add(digest)
        if len(digests) != 1:
            return None, "unreadable"
        return next(iter(digests)), "ok"

    def _first_party_image_rows(
        self: DeployRunner,
        chart_values: dict[str, Any] | None = None,
        manifest_docs: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """The shared row set (Rule B1). See the module docstring.

        ``chart_values``/``manifest_docs`` default to a fresh read (the
        production path); tests inject both so the derivation is exercised
        hermetically.

        B-2 (fix round 1): an UNREADABLE manifest is its own sentinel row
        (``status="manifest_unreadable"``, ``equal=False``) — never ``[]``.
        ``[]`` meant "nothing to compare, trivially converged", which is
        indistinguishable from "I could not even look" and let a stale
        BFF ride through on an `helm get manifest` hiccup (the rev-272
        outcome reached through a new door). Falsifiable: revert to
        returning ``[]`` here and
        ``test_rows_unreadable_manifest_is_an_unequal_sentinel`` goes RED.
        """
        if chart_values is None:
            chart_values = _runner_pkg._read_chart_values(
                values_files=self.cfg.values_files
            )
        if manifest_docs is None:
            manifest_docs = self._live_manifest_docs()
        if manifest_docs is None:
            return [
                {
                    "component": "manifest",
                    "kind": None,
                    "workload": None,
                    "container": None,
                    "is_init": False,
                    "repository": None,
                    "rendered": None,
                    "chart": None,
                    "live": None,
                    "unpinned": False,
                    "status": "manifest_unreadable",
                    "equal": False,
                }
            ]
        committed = committed_first_party_digests(chart_values, self.image_ref)
        prefixes = _configured_first_party_prefixes()
        rows: list[dict[str, Any]] = []
        for doc in _manifest_workload_docs(manifest_docs):
            kind = doc.get("kind")
            name = (doc.get("metadata") or {}).get("name")
            pod_spec = _pod_spec_of(doc)
            if not name or pod_spec is None:
                continue
            selector = _selector_from_manifest_doc(doc)
            replicas = (
                doc.get("spec", {}).get("replicas")
                if isinstance(doc.get("spec"), dict)
                else None
            )
            scaled_to_zero = kind in ("Deployment", "StatefulSet") and replicas == 0
            for is_init, key in ((False, "containers"), (True, "initContainers")):
                for container in pod_spec.get(key) or []:
                    if not isinstance(container, dict):
                        continue
                    image = container.get("image")
                    container_name = container.get("name")
                    if not image or not container_name:
                        continue
                    repository, tag, digest = _parse_image_ref(image)
                    is_first_party = repository in committed or any(
                        repository.startswith(prefix) for prefix in prefixes
                    )
                    if not is_first_party:
                        continue
                    rendered = digest or (f"{repository}:{tag}" if tag else repository)
                    info = committed.get(repository)
                    if info is None:
                        rows.append(
                            {
                                "component": "unknown",
                                "kind": kind,
                                "workload": name,
                                "container": container_name,
                                "is_init": is_init,
                                "repository": repository,
                                "rendered": rendered,
                                "chart": None,
                                "live": None,
                                "unpinned": False,
                                "status": "unknown_first_party",
                                "equal": False,
                            }
                        )
                        continue
                    component = info["key"]
                    chart_digest = info.get("digest")
                    unpinned = chart_digest is None
                    chart_ref = chart_digest or f"{repository}:{info.get('tag')}"
                    render_match = rendered == chart_ref
                    if scaled_to_zero:
                        rows.append(
                            {
                                "component": component,
                                "kind": kind,
                                "workload": name,
                                "container": container_name,
                                "is_init": is_init,
                                "repository": repository,
                                "rendered": rendered,
                                "chart": chart_ref,
                                "live": None,
                                "unpinned": unpinned,
                                "status": "scaled_to_zero",
                                "equal": render_match,
                            }
                        )
                        continue
                    if unpinned:
                        rows.append(
                            {
                                "component": component,
                                "kind": kind,
                                "workload": name,
                                "container": container_name,
                                "is_init": is_init,
                                "repository": repository,
                                "rendered": rendered,
                                "chart": chart_ref,
                                "live": None,
                                "unpinned": True,
                                "status": "unpinned_not_compared",
                                "equal": render_match,
                            }
                        )
                        continue
                    live_digest, live_status = self._live_row_digest(
                        selector=selector,
                        kind=kind,
                        name=name,
                        container=container_name,
                        is_init=is_init,
                    )
                    if live_status == "render_only":
                        equal = render_match
                    elif live_digest is None:
                        equal = False
                    else:
                        equal = render_match and live_digest == digest
                    rows.append(
                        {
                            "component": component,
                            "kind": kind,
                            "workload": name,
                            "container": container_name,
                            "is_init": is_init,
                            "repository": repository,
                            "rendered": rendered,
                            "chart": chart_ref,
                            "live": live_digest,
                            "unpinned": False,
                            "status": live_status,
                            "equal": equal,
                        }
                    )
        return rows
