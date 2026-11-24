"""The first-party workloads the verifier's check 11 compares to the registry,
and the containers of each that it must look at.

Split out of ``verify.py`` (PYTHON-ENGINEERING section 11: ``verify.py`` is past
the 2000-line stop-adding threshold). Pure data and pure functions: every
cluster read stays in ``verify.py`` (tests patch ``verify._run``), and nothing
here imports the deploy runner package (independence contract, verify.py header).
"""

from __future__ import annotations

import os
from typing import Any, NamedTuple

from scripts.deploy import registry, verify_alias

# The verifier's OWN first-party image prefixes (duplicated, not imported from
# ``scripts/deploy/runner/images.py``): an image under one of these is ours, so a
# container on it must be classified and compared, never ignored. Extended
# additively by the same env var the runner honours; ``tests/
# test_verify_probe11_conditions.py`` pins the defaults equal to the runner's.
FIRST_PARTY_PREFIXES = ("docker.io/lfds/", "localhost:5000/audittrace/")
FIRST_PARTY_PREFIXES_ENV_VAR = "AUDITTRACE_FIRST_PARTY_IMAGE_PREFIXES"


class FirstPartyWorkload(NamedTuple):
    """One first-party workload check 11 compares to the registry.

    ``deployment`` is the chart's fullname under the DEFAULT release (the
    probe derives the live name from ``--release``), ``component`` the pod
    selector value, ``container`` the main container whose ``imageID`` is read,
    ``repo_path`` the registry repository (``registry.resolve(repo_path=...)``)
    and ``console_gated`` marks a workload the chart renders only with the
    console (so its expectation follows the console's other Deployment).

    Mirrors the chart's own naming (``templates/reaper``,
    ``templates/console/deployment-bff.yaml``) and the deploy runner's
    selector/container pairs. Pinned by EQUALITY to the rendered chart, per
    (kind, name, container, image repository), in
    ``tests/test_verify_probe11_conditions.py`` (G7').

    Excluded by definition, each named in that test: ``audittrace-memory-server``
    (check 2's item), ``audittrace-librechat`` (the fork's own pin, not versioned
    by ``--target-version``), ``audittrace-test-rls`` (a test Pod, render-level
    only) and ``audittrace-llm-stub`` (kind-only posture).
    """

    deployment: str
    component: str
    container: str
    repo_path: str
    console_gated: bool = False

    @property
    def label(self) -> str:
        return self.deployment.removeprefix("audittrace-")


FIRST_PARTY_WORKLOADS = (
    FirstPartyWorkload(
        "audittrace-pod-reaper", "pod-reaper", "pod-reaper", "memory-server"
    ),
    FirstPartyWorkload(
        "audittrace-librechat-bff", "librechat-bff", "bff", "bff", console_gated=True
    ),
)

# The console's OTHER Deployment: it renders together with the BFF (same
# ``console.enabled`` gate), so its presence is the signal that the BFF is
# EXPECTED (an absent BFF beside a present console is a finding, not "absent").
CONSOLE_ANCHOR_SUFFIX = "librechat"


class ContainerRef(NamedTuple):
    """A container of a workload that check 11 must compare. ``repo_path`` is
    ``None`` for a first-party image the registry module cannot resolve (it is
    then a FAIL, never a skip)."""

    name: str
    is_init: bool
    repo_path: str | None


def first_party_prefixes() -> tuple[str, ...]:
    extra = tuple(
        p.strip()
        for p in os.environ.get(FIRST_PARTY_PREFIXES_ENV_VAR, "").split(",")
        if p.strip()
    )
    return FIRST_PARTY_PREFIXES + extra


def _repository(image: str) -> str:
    """``repo`` of ``repo[:tag][@digest]`` (a registry port is not a tag)."""
    repo_tag = image.partition("@")[0]
    head, _, tail = repo_tag.rpartition("/")
    return f"{head}/{tail.partition(':')[0]}" if head else tail.partition(":")[0]


def containers_to_check(
    workload: FirstPartyWorkload, deployment: dict[str, Any]
) -> list[ContainerRef]:
    """The workload's main container, then EVERY other container (init included)
    of its Deployment whose image is first-party (A1): an extra first-party
    container on an existing workload is compared, never invisible. A
    first-party image the registry module cannot resolve gets ``repo_path=None``
    (fail closed)."""
    refs = [ContainerRef(workload.container, False, workload.repo_path)]
    spec = ((deployment.get("spec") or {}).get("template") or {}).get("spec") or {}
    prefixes = first_party_prefixes()
    for key, is_init in (("containers", False), ("initContainers", True)):
        for container in spec.get(key) or []:
            name = container.get("name")
            repository = _repository(str(container.get("image", "")))
            if name == workload.container and not is_init:
                continue
            if not name or not repository.startswith(prefixes):
                continue
            located = registry.locate_repository(repository)
            refs.append(ContainerRef(name, is_init, located[1] if located else None))
    return refs


def any_alias(items: dict[str, dict[str, Any]]) -> bool:
    """The probe-level ``alias`` fact: True when ANY compared container, extras
    included (``item["containers"]``), is a content-equal alias (R2-2)."""
    return any(
        entry.get("match") == verify_alias.ALIAS
        for item in items.values()
        for entry in (item, *(item.get("containers") or {}).values())
    )
