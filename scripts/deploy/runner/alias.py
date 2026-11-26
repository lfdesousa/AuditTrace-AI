"""The DEPLOYER's own digest-pair classifier (runner P4, first-party rows).

The deploy runner compares each first-party row's live digest with the digest
the chart pins. When they differ, the pod may still run byte-identical platform
manifests under another build's index digest (a *content-equal alias*:
containerd reports the first index digest it recorded for a content it already
had). This module decides that for the DEPLOYER.

**Independence (ADDENDUM-2 C2).** The verifier judges in
``scripts/deploy/verify_alias.py`` with its own parser; this module shares with it
only the ONE policy constant (:data:`scripts.deploy.registry.ALIAS_POLICY`) and
the hash-checked READ (:func:`scripts.deploy.registry.fetch_by_digest`). A defect
here cannot move the verifier's verdict, and the other way round;
``tests/test_verify_probe11_conditions.py`` runs both over one case set and
requires equal kinds (the parity test).

Rows whose repository the registry module cannot resolve (the fork image, the
tests image, anything unknown) are NOT classified: :func:`classify` returns
``None`` and the caller keeps raw digest equality (S1: such a row exits 8, it
never crashes).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

from scripts.deploy import registry

EXACT = "exact"
ALIAS = "content-equal-alias"
MISMATCH = "mismatch"
UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class Verdict:
    """The deployer's reading of one ``(live, pinned)`` pair."""

    kind: str
    platforms: dict[str, str] = field(default_factory=dict)
    line: str = ""


def _platforms(body: bytes, digest: str) -> dict[str, str]:
    """The platform -> manifest-digest map of an index body; ``ValueError`` on
    anything this classifier will not reason over (not JSON, a child without a
    platform or digest, a repeated platform). An index is recognised by its
    ``manifests`` key; a bare image manifest is ``{"manifest": digest}``; only
    ``unknown/unknown`` children (attestations) are left out."""
    doc = json.loads(body)
    if not isinstance(doc, dict):
        raise ValueError("not a JSON object")
    if "manifests" not in doc:
        return {"manifest": digest}
    entries: list[tuple[str, str]] = []
    for child in doc["manifests"]:
        platform = child.get("platform")
        child_digest = child.get("digest")
        if not isinstance(platform, dict) or not isinstance(child_digest, str):
            raise ValueError("a child lacks a platform or a digest")
        parts = (
            str(platform.get("os") or ""),
            str(platform.get("architecture") or ""),
        )
        if parts == ("unknown", "unknown"):
            continue
        key = "/".join(parts)
        if platform.get("variant"):
            key += "/" + str(platform["variant"])
        if platform.get("os.version"):
            key += "@" + str(platform["os.version"])
        entries.append((key, child_digest))
    repeated = [key for key, n in Counter(k for k, _ in entries).items() if n > 1]
    if repeated:
        raise ValueError(f"repeated platform {repeated[0]}")
    return dict(entries)


def classify(live: str, pinned: str, repository: str, item: str) -> Verdict | None:
    """Classify ``live`` against the chart's ``pinned`` digest for ``repository``;
    ``None`` when ``repository`` has no registry mapping (raw equality stays)."""
    located = registry.locate_repository(repository)
    if located is None:
        return None
    backend, repo_path = located
    if live == pinned:
        return Verdict(EXACT)
    try:
        bodies = [
            registry.fetch_by_digest("manifests", d, backend, repo_path=repo_path)
            for d in (live, pinned)
        ]
        live_map, pinned_map = (
            _platforms(body, d) for body, d in zip(bodies, (live, pinned), strict=True)
        )
    except (registry.DigestResolutionError, ValueError, AttributeError, TypeError):
        return Verdict(UNRESOLVED)
    if not live_map or live_map != pinned_map:
        return Verdict(MISMATCH)
    revisions = [
        registry.slsa_build_revision(body, backend, repo_path=repo_path)
        for body in bodies
    ]
    return Verdict(
        ALIAS,
        live_map,
        registry.alias_diagnosis(item, live, pinned, live_map, *revisions),
    )
