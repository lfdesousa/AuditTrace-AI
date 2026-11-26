"""Image digest resolution for the deterministic deploy runner (P1).

Resolves a target version (a tag) to a concrete, pinned image reference so the
deploy can honour the tracks-published-digest invariant. Two backends:

* ``hub``   — Docker Hub public registry for
  ``docker.io/lfds/audittrace-memory-server`` (anonymous pull token per scope).
* ``local`` — the k3s local-registry mirror at ``localhost:5000`` (no auth).
  Digest resolution is best-effort there: a local registry may be unreachable
  from where the runner is invoked, in which case the reference pins the tag
  only.

Everything here is READ-ONLY: these are registry GETs, never mutations. All
network egress funnels through the module-level ``_http_get`` indirection so
tests can substitute it without real sockets.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.request
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

# Public Docker Hub endpoints. The registry proper lives behind a per-scope
# bearer token minted anonymously by the auth service.
HUB_REGISTRY = "https://registry-1.docker.io"
HUB_AUTH = "https://auth.docker.io/token"
HUB_SERVICE = "registry.docker.io"

LOCAL_REGISTRY = "http://localhost:5000"

# THE alias policy (operator ruling Q1, 2026-11-24, decisions-in/2026-11-24-
# probe11-q1-alias-policy-fail.md). A *content-equal alias* is a live image
# whose index digest differs from the published one while every platform
# manifest (the bytes that run) is identical: containerd reports the first
# index digest it recorded for a content it already had. ``"fail"`` (the
# ruling): the recorded provenance must be the published build, so an alias is
# a FAIL carrying an ``ALIAS:`` diagnosis. ``"pass"``: PASS, same diagnosis.
#
# This is ONE decision, read by the verifier (probes 2 and 11) and by the
# deployer (runner P4). The JUDGMENT is NOT shared: the verifier classifies in
# ``scripts/deploy/verify_alias.py`` and the runner in
# ``scripts/deploy/runner/alias.py`` (independence contract, verify.py header);
# only the registry READS below are shared.
ALIAS_POLICY: Literal["fail", "pass"] = "fail"

# The registry may store a fat manifest list (multi-arch) or a single image
# manifest; we accept both so the ``Docker-Content-Digest`` header comes back
# either way.
_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

# Backend → (full repository reference, registry-API path, registry base URL).
# Kept as the memory-server-only shape it always was — ``orchestrator.py``'s
# P1 dry-run fallback reads ``_BACKENDS[registry][0]`` directly, so this name
# and shape stay byte-identical. :data:`_EXTRA_REPO_PATHS` below is the
# EXTENSION (D5, spec 2026-10-07-SPEC-bff-bump-1.29.1-and-stale-override-
# guard.md) that lets :func:`resolve` resolve a SECOND first-party image.
_BACKENDS = {
    "hub": (
        "docker.io/lfds/audittrace-memory-server",
        "lfds/audittrace-memory-server",
        HUB_REGISTRY,
    ),
    "local": (
        "localhost:5000/audittrace/memory-server",
        "audittrace/memory-server",
        LOCAL_REGISTRY,
    ),
}

# Additional first-party images this module can resolve besides
# memory-server, keyed by ``repo_path`` -> {backend -> (repository,
# registry-API path, base URL)} (D5: the verify runner's BFF probe). The BFF
# is published to Docker Hub (``publish.yml``) AND pushed to the local
# registry by every ``--registry local`` rc deploy (measured 2026-11-24:
# ``localhost:5000`` holds ``audittrace/librechat-bff`` rc tags). A tag that
# was never pushed locally resolves to an UNPINNED ref (``digest=None``) and the
# verifier FAILs on it, exactly as probe 2 does for memory-server.
_EXTRA_REPO_PATHS: dict[str, dict[str, tuple[str, str, str]]] = {
    "bff": {
        "hub": (
            "docker.io/lfds/audittrace-librechat-bff",
            "lfds/audittrace-librechat-bff",
            HUB_REGISTRY,
        ),
        "local": (
            "localhost:5000/audittrace/librechat-bff",
            "audittrace/librechat-bff",
            LOCAL_REGISTRY,
        ),
    },
}


class DigestResolutionError(RuntimeError):
    """Raised when a published digest cannot be resolved for a target tag."""


@dataclass(frozen=True)
class ImageRef:
    """A resolved, deploy-ready image reference.

    ``digest`` is ``None`` only for a local-registry tag whose registry could
    not be reached — the runner records the tag and flags the missing pin.
    """

    repository: str
    tag: str
    digest: str | None
    registry: str

    @property
    def pinned(self) -> bool:
        return self.digest is not None

    def as_dict(self) -> dict[str, str | None | bool]:
        return {
            "repository": self.repository,
            "tag": self.tag,
            "digest": self.digest,
            "registry": self.registry,
            "pinned": self.pinned,
        }


def _http_get(  # pragma: no cover - thin urllib egress boundary; monkeypatched in tests
    url: str, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    """Perform a GET and return ``(status, response_headers, body)``.

    Sole network egress point of this module; monkeypatched in tests. Header
    keys are normalised to lower-case so callers read them case-insensitively.
    """
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - https/http registry only
        status = response.status
        resp_headers = {k.lower(): v for k, v in response.headers.items()}
        body = response.read()
    return status, resp_headers, body


def _get_hub_token(repo_path: str) -> str:
    """Mint an anonymous pull token scoped to ``repo_path`` on Docker Hub."""
    scope = f"repository:{repo_path}:pull"
    url = f"{HUB_AUTH}?service={HUB_SERVICE}&scope={scope}"
    # Real ``urlopen`` RAISES HTTPError on a non-2xx (401/429/5xx); the mocked
    # egress in tests returns a status instead. Normalise BOTH shapes to a clean
    # DigestResolutionError so a resolution failure never escapes as a raw
    # urllib error (the runner must always still emit a report). ``OSError`` is
    # the superclass of HTTPError/URLError AND of a bare ``TimeoutError`` (==
    # ``socket.timeout``) raised on a READ timeout — which is NOT a ``URLError``,
    # so catching only ``(HTTPError, URLError)`` would let a timeout escape and
    # crash the deploy runner with no report (the WS5 finding). Catch the whole
    # transport class.
    try:
        status, _headers, body = _http_get(url)
    except OSError as exc:
        raise DigestResolutionError(f"Docker Hub auth failed: {exc}") from exc
    if status != 200:
        raise DigestResolutionError(f"Docker Hub auth returned HTTP {status}")
    token = json.loads(body).get("token")
    if not token:
        raise DigestResolutionError("Docker Hub auth response carried no token")
    return token


def _manifest_digest(base: str, repo_path: str, tag: str, token: str | None) -> str:
    """Return the ``Docker-Content-Digest`` for ``base/repo_path:tag``.

    Any transport failure (real ``urlopen`` raises HTTPError on 404 for a bad
    tag, or a bare ``TimeoutError``/``socket.timeout`` — an ``OSError``, NOT a
    ``URLError`` — on a READ timeout) or a non-200 status becomes a
    :class:`DigestResolutionError` so callers have a single, catchable failure
    type and the runner always still emits a report. ``OSError`` covers the whole
    class (HTTPError/URLError/TimeoutError/ConnectionError).
    """
    url = f"{base}/v2/{repo_path}/manifests/{tag}"
    headers = {"Accept": _MANIFEST_ACCEPT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        status, resp_headers, _body = _http_get(url, headers)
    except OSError as exc:
        raise DigestResolutionError(
            f"manifest fetch for {repo_path}:{tag} failed: {exc}"
        ) from exc
    if status != 200:
        raise DigestResolutionError(
            f"manifest fetch for {repo_path}:{tag} returned HTTP {status}"
        )
    digest = resp_headers.get("docker-content-digest")
    if not digest:
        raise DigestResolutionError(
            f"registry did not return a content digest for {repo_path}:{tag}"
        )
    return digest


def _backend(registry: str, repo_path: str) -> tuple[str, str, str]:
    """``(repository, registry-API path, base URL)`` for a backend and
    ``repo_path``; an unknown pair is a :class:`ValueError` (fail loud)."""
    backends = (
        _BACKENDS
        if repo_path == "memory-server"
        else _EXTRA_REPO_PATHS.get(repo_path, {})
    )
    if registry not in backends:
        raise ValueError(f"unknown registry backend: {registry!r}")
    return backends[registry]


def locate_repository(repository: str) -> tuple[str, str] | None:
    """``(registry, repo_path)`` for a rendered first-party repository string
    (``docker.io/lfds/audittrace-librechat-bff`` -> ``("hub", "bff")``), or
    ``None`` when this module cannot resolve that repository (the fork image,
    the tests image, anything unknown). A pure table lookup, no I/O."""
    for repo_path, backends in (
        ("memory-server", _BACKENDS),
        *_EXTRA_REPO_PATHS.items(),
    ):
        for name, (known, _api_path, _base) in backends.items():
            if known == repository:
                return name, repo_path
    return None


def resolve(
    version: str, registry: str = "hub", *, repo_path: str = "memory-server"
) -> ImageRef:
    """Resolve ``version`` to an :class:`ImageRef` for the given backend.

    ``hub`` failures are hard (a published deploy must pin its digest).
    ``local`` failures are soft: an unreachable local registry yields an
    unpinned ref rather than aborting the whole run, since the k3s mirror may
    not be reachable from the runner host.

    ``repo_path`` (D5, spec 2026-10-07-SPEC-bff-bump-1.29.1-and-stale-
    override-guard.md) selects WHICH first-party image to resolve —
    ``"memory-server"`` (the default, :data:`_BACKENDS`) for every existing
    caller, byte-identical; any other value looks up
    :data:`_EXTRA_REPO_PATHS` instead (today: ``"bff"``, the verify runner's
    BFF probe). An unknown ``(repo_path, registry)`` pair raises the SAME
    :class:`ValueError` shape as an unknown backend — fail loud either way.
    """
    repository, api_path, base = _backend(registry, repo_path)

    if registry == "hub":
        token = _get_hub_token(api_path)
        digest = _manifest_digest(base, api_path, version, token)
        return ImageRef(repository, version, digest, registry)

    # local
    try:
        digest = _manifest_digest(base, api_path, version, token=None)
    except (DigestResolutionError, OSError) as exc:
        # OSError subsumes HTTPError/URLError/TimeoutError/ConnectionError; a local
        # registry that is unreachable OR times out is a SOFT failure here (the
        # k3s mirror may not be reachable from the runner host) — unpinned ref.
        logger.warning("local registry digest unresolved for %s: %s", version, exc)
        digest = None
    return ImageRef(repository, version, digest, registry)


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def fetch_by_digest(
    kind: Literal["manifests", "blobs"],
    digest: str,
    registry: str,
    *,
    repo_path: str,
) -> bytes:
    """GET ``/v2/<repo>/<kind>/<digest>`` and return the body ONLY when
    ``sha256(body) == digest`` (B2.6). A registry that serves other bytes for a
    digest, a non-200, a malformed digest or any transport failure is a
    :class:`DigestResolutionError` — the caller never reasons over unverified
    bytes. A READ only: no policy, no judgment (the verifier and the deployer
    each judge the bytes with their own classifier)."""
    if not _DIGEST_RE.fullmatch(digest):
        raise DigestResolutionError(f"not a sha256 digest: {digest!r}")
    _repository, api_path, base = _backend(registry, repo_path)
    token = _get_hub_token(api_path) if registry == "hub" else None
    url = f"{base}/v2/{api_path}/{kind}/{digest}"
    headers = {"Accept": _MANIFEST_ACCEPT if kind == "manifests" else "*/*"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        status, _resp_headers, body = _http_get(url, headers)
    except OSError as exc:
        raise DigestResolutionError(
            f"{kind} fetch for {repo_path}@{digest} failed: {exc}"
        ) from exc
    if status != 200:
        raise DigestResolutionError(
            f"{kind} fetch for {repo_path}@{digest} returned HTTP {status}"
        )
    actual = "sha256:" + hashlib.sha256(body).hexdigest()
    if actual != digest:
        raise DigestResolutionError(
            f"{kind} body for {repo_path}@{digest} hashes to {actual}"
        )
    return body


_IN_TOTO_REVISION_RE = re.compile(r'"vcs:revision"\s*:\s*"([0-9a-f]{40})"')
NO_BUILD_REVISION = "n/a"


def slsa_build_revision(index_body: bytes, registry: str, *, repo_path: str) -> str:
    """The ``vcs:revision`` of an image index's SLSA provenance, for the ALIAS
    diagnosis TEXT only (never a verdict input): index -> attestation child
    (``unknown/unknown``) -> its manifest -> in-toto blob, every by-digest fetch
    hash-checked (:func:`fetch_by_digest`). ``"n/a"`` when the attestation is
    absent or anything fails to read — never a guess."""
    try:
        index = json.loads(index_body)
        children = index.get("manifests") if isinstance(index, dict) else None
        for child in children or []:
            platform = child.get("platform") or {}
            if platform.get("os") != "unknown" or not child.get("digest"):
                continue
            manifest = json.loads(
                fetch_by_digest(
                    "manifests", child["digest"], registry, repo_path=repo_path
                )
            )
            for layer in manifest.get("layers") or []:
                blob = fetch_by_digest(
                    "blobs", layer["digest"], registry, repo_path=repo_path
                )
                found = _IN_TOTO_REVISION_RE.search(blob.decode("utf-8", "replace"))
                if found:
                    return found.group(1)
    except (DigestResolutionError, ValueError, KeyError, AttributeError, TypeError):
        logger.debug("no SLSA build revision readable for %s", repo_path)
    return NO_BUILD_REVISION


def alias_diagnosis(
    item: str,
    live: str,
    published: str,
    platforms: dict[str, str],
    live_revision: str,
    published_revision: str,
) -> str:
    """The ``ALIAS:`` line, identical for probe 2, probe 11 and the runner
    detail (AC4'). Text only."""
    shared = ", ".join(
        f"{key}: {digest.split(':')[-1][:12]}"
        for key, digest in sorted(platforms.items())
    )
    return (
        f"ALIAS: {item} live index {live.split(':')[-1][:12]} != published index "
        f"{published.split(':')[-1][:12]}; shared platform manifests {{{shared}}}; "
        f"build revisions live={live_revision} published={published_revision}; "
        "remedy: operator accepts the alias, or clean the node image cache "
        "(crictl rmi <config>) and re-verify"
    )
