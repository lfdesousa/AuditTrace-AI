"""The VERIFIER's own digest-pair classifier (probes 2 and 11).

A live image digest that differs from the published one is not automatically a
different image: containerd reports the first index digest it recorded for a
content it already had, so a pod can run byte-identical platform manifests under
another build's index digest (a *content-equal alias*, three times in nine
days). This module classifies a ``(live, published)`` digest pair into one of

* ``exact``               — the digests are equal;
* ``content-equal-alias`` — different index digests, the SAME non-empty set of
  platform manifests;
* ``mismatch``            — readable, different (or an empty platform set);
* ``unresolved``          — a body could not be read, failed its hash, or is
  malformed (a duplicate platform, a child without a platform, ...).

**Independence (ADDENDUM-2 C2; verify.py header).** ONE policy constant
(:data:`scripts.deploy.registry.ALIAS_POLICY`) is shared; the JUDGMENT is not:
the deployer classifies in ``scripts/deploy/runner/alias.py`` with its own
parser. This module never imports the runner package. What both share is only
the READ (:func:`scripts.deploy.registry.fetch_by_digest`, which hashes every
body), so a classifier defect cannot reach both verdicts.

Fail-closed by construction: anything this module cannot positively prove equal
is ``mismatch`` or ``unresolved``, never an alias.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from scripts.deploy import registry

EXACT = "exact"
ALIAS = "content-equal-alias"
MISMATCH = "mismatch"
UNRESOLVED = "unresolved"

# The single-image-manifest key (no index): never equal across two digests.
_SINGLE_MANIFEST_KEY = "manifest"


class PlatformError(ValueError):
    """An image index this classifier refuses to reason over (fail closed)."""


@dataclass(frozen=True)
class Match:
    """The verdict on one ``(live, published)`` pair."""

    kind: str
    platforms: dict[str, str] = field(default_factory=dict)
    live_body: bytes | None = None
    published_body: bytes | None = None
    reason: str = ""

    @property
    def alias(self) -> bool:
        return self.kind == ALIAS


def platform_map(body: bytes, digest: str) -> dict[str, str]:
    """``{"os/arch[/variant][@osversion]": child digest}`` of an image index.

    * An index is detected by its ``manifests`` key (never by media type: a
      Docker manifest list and an OCI index both qualify).
    * Only a child whose platform is ``unknown/unknown`` (a buildkit attestation)
      is excluded; an annotated child with a real platform is compared.
    * Variant and ``os.version`` are part of the key.
    * A repeated key, a child without a ``platform`` object, or a malformed child
      raises :class:`PlatformError` (containerd may run either duplicate; a
      platform-less child is unspecified).
    * A single image manifest (no ``manifests`` key) is ``{"manifest": digest}``.
    """
    try:
        doc = json.loads(body)
    except ValueError as exc:
        raise PlatformError(f"body is not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise PlatformError("body is not a JSON object")
    if "manifests" not in doc:
        return {_SINGLE_MANIFEST_KEY: digest}
    children = doc["manifests"]
    if not isinstance(children, list):
        raise PlatformError("'manifests' is not a list")
    result: dict[str, str] = {}
    for child in children:
        if not isinstance(child, dict):
            raise PlatformError("a child entry is not an object")
        platform = child.get("platform")
        if not isinstance(platform, dict):
            raise PlatformError("a child has no platform")
        child_digest = child.get("digest")
        if not isinstance(child_digest, str):
            raise PlatformError("a child has no digest")
        os_ = str(platform.get("os") or "")
        arch = str(platform.get("architecture") or "")
        if os_ == "unknown" and arch == "unknown":
            continue
        key = f"{os_}/{arch}"
        if platform.get("variant"):
            key += f"/{platform['variant']}"
        if platform.get("os.version"):
            key += f"@{platform['os.version']}"
        if key in result:
            raise PlatformError(f"duplicate platform {key}")
        result[key] = child_digest
    return result


def classify(live: str, published: str, registry_name: str, *, repo_path: str) -> Match:
    """Classify a live/published digest pair (see the module docstring)."""
    if live == published:
        return Match(EXACT)
    try:
        live_body = registry.fetch_by_digest(
            "manifests", live, registry_name, repo_path=repo_path
        )
        published_body = registry.fetch_by_digest(
            "manifests", published, registry_name, repo_path=repo_path
        )
        live_platforms = platform_map(live_body, live)
        published_platforms = platform_map(published_body, published)
    except (registry.DigestResolutionError, PlatformError) as exc:
        return Match(UNRESOLVED, reason=str(exc))
    if live_platforms and live_platforms == published_platforms:
        return Match(ALIAS, live_platforms, live_body, published_body)
    return Match(MISMATCH, reason="platform manifests differ")


def diagnosis(
    item: str,
    live: str,
    published: str,
    match: Match,
    registry_name: str,
    *,
    repo_path: str,
) -> str:
    """The ``ALIAS:`` line for an alias match (build revisions from SLSA)."""
    revisions = [
        registry.slsa_build_revision(body, registry_name, repo_path=repo_path)
        if body is not None
        else registry.NO_BUILD_REVISION
        for body in (match.live_body, match.published_body)
    ]
    live_revision, published_revision = revisions
    return registry.alias_diagnosis(
        item, live, published, match.platforms, live_revision, published_revision
    )
