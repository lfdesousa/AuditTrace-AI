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
from urllib.error import HTTPError, URLError

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
    # urllib error (the runner must always still emit a report).
    try:
        status, _headers, body = _http_get(url)
    except (HTTPError, URLError) as exc:
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
    tag) or a non-200 status becomes a :class:`DigestResolutionError` so callers
    have a single, catchable failure type.
    """
    url = f"{base}/v2/{repo_path}/manifests/{tag}"
    headers = {"Accept": _MANIFEST_ACCEPT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        status, resp_headers, _body = _http_get(url, headers)
    except (HTTPError, URLError) as exc:
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


def resolve(version: str, registry: str = "hub") -> ImageRef:
    """Resolve ``version`` to an :class:`ImageRef` for the given backend.

    ``hub`` failures are hard (a published deploy must pin its digest).
    ``local`` failures are soft: an unreachable local registry yields an
    unpinned ref rather than aborting the whole run, since the k3s mirror may
    not be reachable from the runner host.
    """
    if registry not in _BACKENDS:
        raise ValueError(f"unknown registry backend: {registry!r}")
    repository, repo_path, base = _BACKENDS[registry]

    if registry == "hub":
        token = _get_hub_token(repo_path)
        digest = _manifest_digest(base, repo_path, version, token)
        return ImageRef(repository, version, digest, registry)

    # local
    try:
        digest = _manifest_digest(base, repo_path, version, token=None)
    except (DigestResolutionError, HTTPError, URLError, OSError) as exc:
        logger.warning("local registry digest unresolved for %s: %s", version, exc)
        digest = None
    return ImageRef(repository, version, digest, registry)
