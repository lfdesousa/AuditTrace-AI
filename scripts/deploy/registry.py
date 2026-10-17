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

import json
import logging
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Public Docker Hub endpoints. The registry proper lives behind a per-scope
# bearer token minted anonymously by the auth service.
HUB_REGISTRY = "https://registry-1.docker.io"
HUB_AUTH = "https://auth.docker.io/token"
HUB_SERVICE = "registry.docker.io"

LOCAL_REGISTRY = "http://localhost:5000"

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
# registry-API path, base URL)} (D5: the verify runner's BFF probe). Only a
# ``hub`` entry exists for ``"bff"`` — the BFF is published to Docker Hub
# only (``publish.yml``, spec F8); there is no local-registry counterpart,
# so the verify probe reports ``skipped`` under ``--registry local`` rather
# than ever calling :func:`resolve` with ``repo_path="bff"`` there.
_EXTRA_REPO_PATHS: dict[str, dict[str, tuple[str, str, str]]] = {
    "bff": {
        "hub": (
            "docker.io/lfds/audittrace-librechat-bff",
            "lfds/audittrace-librechat-bff",
            HUB_REGISTRY,
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
    backends = (
        _BACKENDS
        if repo_path == "memory-server"
        else _EXTRA_REPO_PATHS.get(repo_path, {})
    )
    if registry not in backends:
        raise ValueError(f"unknown registry backend: {registry!r}")
    repository, api_path, base = backends[registry]

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
