"""Unit tests for the deploy runner's image-digest resolution (P1).

The single network egress point (``registry._http_get``) is monkeypatched; no
real sockets. Covers the hub happy path, the local best-effort path, and every
error branch (auth failure, missing token, non-200 manifest, missing digest,
unknown backend).
"""

from __future__ import annotations

import hashlib
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

import pytest

from scripts.deploy import registry
from scripts.deploy.registry import DigestResolutionError, ImageRef


def _digest_headers(digest="sha256:deadbeef"):
    return {"docker-content-digest": digest}


def test_imageref_pinned_and_as_dict():
    ref = ImageRef("repo", "v1", "sha256:abc", "hub")
    assert ref.pinned is True
    d = ref.as_dict()
    assert d["digest"] == "sha256:abc" and d["pinned"] is True

    unpinned = ImageRef("repo", "v1", None, "local")
    assert unpinned.pinned is False
    assert unpinned.as_dict()["pinned"] is False


def test_resolve_hub_happy_path(monkeypatch):
    calls = []

    def fake_get(url, headers=None):
        calls.append(url)
        if urlparse(url).hostname == "auth.docker.io":
            return 200, {}, json.dumps({"token": "tok123"}).encode()
        assert headers["Authorization"] == "Bearer tok123"
        return 200, _digest_headers("sha256:cafe"), b""

    monkeypatch.setattr(registry, "_http_get", fake_get)
    ref = registry.resolve("v1.2.3", "hub")
    assert ref.repository == "docker.io/lfds/audittrace-memory-server"
    assert ref.digest == "sha256:cafe"
    assert ref.pinned is True
    assert any(urlparse(u).hostname == "auth.docker.io" for u in calls)


def test_resolve_bff_repo_path_hits_the_bff_repository(monkeypatch):
    """V-3 (fix round 1): `resolve(..., repo_path="bff")` resolves
    `lfds/audittrace-librechat-bff`, NEVER the default memory-server
    repository — a non-vacuous test calling `resolve` with `repo_path`
    explicitly (the review's own finding: no pre-existing test did this at
    all, making the prior "100% lines+branches" on `registry.py` a floor,
    not proof, on this one conditional).

    Falsifiable: hardcode `resolve` to always use `_BACKENDS` (ignore
    `repo_path`) and this goes RED — the resolved repository/API path
    revert to the memory-server ones.
    """
    calls = []

    def fake_get(url, headers=None):
        calls.append(url)
        if urlparse(url).hostname == "auth.docker.io":
            assert (
                "lfds%2Faudittrace-librechat-bff" in url
                or "lfds/audittrace-librechat-bff" in url
            )
            return 200, {}, json.dumps({"token": "tok123"}).encode()
        assert "lfds/audittrace-librechat-bff" in url
        return 200, _digest_headers("sha256:bffcafe"), b""

    monkeypatch.setattr(registry, "_http_get", fake_get)
    ref = registry.resolve("1.29.1", "hub", repo_path="bff")
    assert ref.repository == "docker.io/lfds/audittrace-librechat-bff"
    assert ref.digest == "sha256:bffcafe"
    assert any("librechat-bff" in u for u in calls)


def test_resolve_bff_repo_path_unknown_registry_backend_raises(monkeypatch):
    with pytest.raises(ValueError, match="unknown registry backend"):
        registry.resolve("1.29.1", "nowhere", repo_path="bff")


def test_resolve_local_bff_repo_path_uses_the_local_registry(monkeypatch):
    """AC5 (VERIFY-PROBE11): the BFF is pushed to ``localhost:5000`` by every
    ``--registry local`` rc deploy; ``resolve(repo_path="bff")`` reads it there.
    Neuter: remove ``_EXTRA_REPO_PATHS["bff"]["local"]`` -> ValueError -> RED."""
    urls: list[str] = []

    def _get(url, headers=None):
        urls.append(url)
        return 200, {"docker-content-digest": "sha256:" + "a" * 64}, b"{}"

    monkeypatch.setattr(registry, "_http_get", _get)
    ref = registry.resolve("1.34.0-rc3-cc47cc7", "local", repo_path="bff")
    assert ref.repository == "localhost:5000/audittrace/librechat-bff"
    assert urls == [
        "http://localhost:5000/v2/audittrace/librechat-bff/manifests/1.34.0-rc3-cc47cc7"
    ]


@pytest.mark.parametrize(
    "repository,expected",
    [
        ("docker.io/lfds/audittrace-memory-server", ("hub", "memory-server")),
        ("localhost:5000/audittrace/memory-server", ("local", "memory-server")),
        ("docker.io/lfds/audittrace-librechat-bff", ("hub", "bff")),
        ("localhost:5000/audittrace/librechat-bff", ("local", "bff")),
        ("docker.io/lfds/audittrace-librechat", None),
        ("docker.io/lfds/audittrace-tests", None),
    ],
)
def test_locate_repository_maps_known_repositories_only(repository, expected):
    assert registry.locate_repository(repository) == expected


def test_resolve_hub_auth_non_200(monkeypatch):
    monkeypatch.setattr(registry, "_http_get", lambda url, headers=None: (503, {}, b""))
    with pytest.raises(DigestResolutionError, match="HTTP 503"):
        registry.resolve("v1", "hub")


def test_resolve_hub_auth_no_token(monkeypatch):
    monkeypatch.setattr(
        registry, "_http_get", lambda url, headers=None: (200, {}, b"{}")
    )
    with pytest.raises(DigestResolutionError, match="no token"):
        registry.resolve("v1", "hub")


def test_resolve_hub_manifest_non_200(monkeypatch):
    def fake_get(url, headers=None):
        if urlparse(url).hostname == "auth.docker.io":
            return 200, {}, json.dumps({"token": "t"}).encode()
        return 404, {}, b""

    monkeypatch.setattr(registry, "_http_get", fake_get)
    with pytest.raises(DigestResolutionError, match="HTTP 404"):
        registry.resolve("v1", "hub")


def test_resolve_hub_manifest_missing_digest(monkeypatch):
    def fake_get(url, headers=None):
        if urlparse(url).hostname == "auth.docker.io":
            return 200, {}, json.dumps({"token": "t"}).encode()
        return 200, {}, b""  # no docker-content-digest header

    monkeypatch.setattr(registry, "_http_get", fake_get)
    with pytest.raises(DigestResolutionError, match="content digest"):
        registry.resolve("v1", "hub")


def test_resolve_local_happy_path(monkeypatch):
    monkeypatch.setattr(
        registry,
        "_http_get",
        lambda url, headers=None: (200, _digest_headers("sha256:local"), b""),
    )
    ref = registry.resolve("v2", "local")
    assert ref.repository == "localhost:5000/audittrace/memory-server"
    assert ref.digest == "sha256:local"


def test_resolve_local_unreachable_is_soft(monkeypatch):
    def boom(url, headers=None):
        raise OSError("connection refused")

    monkeypatch.setattr(registry, "_http_get", boom)
    ref = registry.resolve("v2", "local")
    # local failure is soft: unpinned ref, no exception
    assert ref.digest is None
    assert ref.pinned is False


def test_resolve_local_digest_error_is_soft(monkeypatch):
    def missing(url, headers=None):
        return 200, {}, b""  # -> DigestResolutionError, caught + downgraded

    monkeypatch.setattr(registry, "_http_get", missing)
    ref = registry.resolve("v2", "local")
    assert ref.digest is None


def test_resolve_unknown_backend():
    with pytest.raises(ValueError, match="unknown registry backend"):
        registry.resolve("v1", "gcr")


def test_resolve_hub_auth_httperror_becomes_clean(monkeypatch):
    # Real urlopen RAISES on 401/429/5xx; must surface as DigestResolutionError.
    def raiser(url, headers=None):
        raise HTTPError(url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(registry, "_http_get", raiser)
    with pytest.raises(DigestResolutionError, match="auth failed"):
        registry.resolve("1.13.0", "hub")


def test_resolve_hub_manifest_httperror_becomes_clean(monkeypatch):
    # token OK, then the manifest fetch raises HTTPError (e.g. 404 bad tag).
    def fake_get(url, headers=None):
        if urlparse(url).hostname == "auth.docker.io":
            return 200, {}, json.dumps({"token": "t"}).encode()
        raise HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(registry, "_http_get", fake_get)
    with pytest.raises(DigestResolutionError, match="manifest fetch"):
        registry.resolve("v9.does.not.exist", "hub")


def test_resolve_hub_manifest_urlerror_becomes_clean(monkeypatch):
    def fake_get(url, headers=None):
        if urlparse(url).hostname == "auth.docker.io":
            return 200, {}, json.dumps({"token": "t"}).encode()
        raise URLError("connection reset")

    monkeypatch.setattr(registry, "_http_get", fake_get)
    with pytest.raises(DigestResolutionError, match="manifest fetch"):
        registry.resolve("1.13.0", "hub")


# ── WS5: transport-timeout hardening ─────────────────────────────────────────
#
# A READ timeout raises a bare ``TimeoutError`` (== ``socket.timeout``, an
# ``OSError`` that is NOT a ``URLError``). The callers must map the WHOLE
# transport class to a CLEAN ``DigestResolutionError`` (hub) or a soft unpinned
# ref (local) so the runner always still emits a report. Falsifiable: revert the
# caller catches to ``(HTTPError, URLError)`` and these raise a bare TimeoutError.


def test_resolve_hub_auth_timeout_becomes_clean(monkeypatch):
    def _timeout(url, headers=None):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(registry, "_http_get", _timeout)
    with pytest.raises(DigestResolutionError, match="auth failed"):
        registry.resolve("1.13.0", "hub")


def test_resolve_hub_manifest_timeout_becomes_clean(monkeypatch):
    def fake_get(url, headers=None):
        if urlparse(url).hostname == "auth.docker.io":
            return 200, {}, json.dumps({"token": "t"}).encode()
        raise TimeoutError("read timed out")

    monkeypatch.setattr(registry, "_http_get", fake_get)
    with pytest.raises(DigestResolutionError, match="manifest fetch"):
        registry.resolve("1.13.0", "hub")


def test_resolve_local_timeout_is_soft(monkeypatch):
    def _timeout(url, headers=None):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(registry, "_http_get", _timeout)
    ref = registry.resolve("v2", "local")
    # a local-registry timeout is soft: unpinned ref, no exception
    assert ref.digest is None
    assert ref.pinned is False


# ── fetch_by_digest / slsa_build_revision / alias_diagnosis (VERIFY-PROBE11) ──

_BODY = b'{"schemaVersion": 2}'
_BODY_DIGEST = "sha256:" + hashlib.sha256(_BODY).hexdigest()


def test_fetch_by_digest_returns_a_body_that_hashes_to_the_digest(monkeypatch):
    seen: list[tuple[str, dict]] = []

    def _get(url, headers=None):
        seen.append((url, headers))
        return 200, {}, _BODY

    monkeypatch.setattr(registry, "_http_get", _get)
    out = registry.fetch_by_digest(
        "manifests", _BODY_DIGEST, "local", repo_path="memory-server"
    )
    assert out == _BODY
    assert (
        seen[0][0]
        == f"http://localhost:5000/v2/audittrace/memory-server/manifests/{_BODY_DIGEST}"
    )
    assert "image.index" in seen[0][1]["Accept"]


def test_fetch_by_digest_blobs_accept_anything(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(
        registry,
        "_http_get",
        lambda url, headers=None: (seen.append(headers), (200, {}, _BODY))[1],
    )
    registry.fetch_by_digest("blobs", _BODY_DIGEST, "local", repo_path="bff")
    assert seen[0]["Accept"] == "*/*"


def test_fetch_by_digest_hub_sends_the_anonymous_bearer_token(monkeypatch):
    seen: list[dict | None] = []

    def _get(url, headers=None):
        if url.startswith(registry.HUB_AUTH):
            return 200, {}, b'{"token": "t0k"}'
        seen.append(headers)
        return 200, {}, _BODY

    monkeypatch.setattr(registry, "_http_get", _get)
    registry.fetch_by_digest("manifests", _BODY_DIGEST, "hub", repo_path="bff")
    assert seen[0]["Authorization"] == "Bearer t0k"


@pytest.mark.parametrize("digest", ["sha256:LIVE", "md5:abc", "", "sha256:" + "A" * 64])
def test_fetch_by_digest_refuses_a_malformed_digest_without_any_read(
    monkeypatch, digest
):
    monkeypatch.setattr(
        registry, "_http_get", lambda *a, **k: pytest.fail("no read for a bad digest")
    )
    with pytest.raises(DigestResolutionError, match="not a sha256 digest"):
        registry.fetch_by_digest("manifests", digest, "local", repo_path="bff")


def test_fetch_by_digest_refuses_a_body_that_does_not_hash_to_the_digest(monkeypatch):
    """B2.6. Neuter: skip the hash comparison -> this returns the wrong bytes -> RED."""
    monkeypatch.setattr(
        registry, "_http_get", lambda url, headers=None: (200, {}, _BODY + b"\n")
    )
    with pytest.raises(DigestResolutionError, match="hashes to"):
        registry.fetch_by_digest("manifests", _BODY_DIGEST, "local", repo_path="bff")


def test_fetch_by_digest_non_200_and_transport_errors_are_resolution_errors(
    monkeypatch,
):
    monkeypatch.setattr(registry, "_http_get", lambda url, headers=None: (404, {}, b""))
    with pytest.raises(DigestResolutionError, match="HTTP 404"):
        registry.fetch_by_digest("manifests", _BODY_DIGEST, "local", repo_path="bff")

    def _boom(url, headers=None):
        raise TimeoutError("slow")

    monkeypatch.setattr(registry, "_http_get", _boom)
    with pytest.raises(DigestResolutionError, match="failed"):
        registry.fetch_by_digest("manifests", _BODY_DIGEST, "local", repo_path="bff")


def _att_registry(revision_blob: bytes | None):
    """A local registry stub serving an index -> attestation manifest -> blob."""
    blob_digest = "sha256:" + hashlib.sha256(revision_blob or b"").hexdigest()
    att_manifest = json.dumps({"layers": [{"digest": blob_digest}]}).encode()
    att_digest = "sha256:" + hashlib.sha256(att_manifest).hexdigest()
    index = json.dumps(
        {
            "manifests": [
                {
                    "digest": "sha256:" + "b" * 64,
                    "platform": {"os": "linux", "architecture": "amd64"},
                },
                {
                    "digest": att_digest,
                    "platform": {"os": "unknown", "architecture": "unknown"},
                },
            ]
        }
    ).encode()
    served = {att_digest: att_manifest}
    if revision_blob is not None:
        served[blob_digest] = revision_blob

    def _get(url, headers=None):
        ref = url.rsplit("/", 1)[-1]
        return (200, {}, served[ref]) if ref in served else (404, {}, b"")

    return index, _get


def test_slsa_build_revision_reads_vcs_revision_from_the_attestation(monkeypatch):
    rev = "f04c5c491b55cc03daaf73157a5d7a5cb40ad64b"
    index, get = _att_registry(
        json.dumps({"predicate": {"vcs:revision": rev}}).encode()
    )
    monkeypatch.setattr(registry, "_http_get", get)
    assert registry.slsa_build_revision(index, "local", repo_path="bff") == rev


def test_slsa_build_revision_is_na_when_the_attestation_is_unreadable(monkeypatch):
    """Never a guess: no blob (404), no attestation child, or a non-index body -> n/a."""
    index, get = _att_registry(None)
    monkeypatch.setattr(registry, "_http_get", get)
    assert registry.slsa_build_revision(index, "local", repo_path="bff") == "n/a"
    assert registry.slsa_build_revision(b"not json", "local", repo_path="bff") == "n/a"
    assert registry.slsa_build_revision(b"[]", "local", repo_path="bff") == "n/a"
    no_att = json.dumps(
        {"manifests": [{"digest": "sha256:" + "b" * 64, "platform": {"os": "linux"}}]}
    ).encode()
    assert registry.slsa_build_revision(no_att, "local", repo_path="bff") == "n/a"


def test_alias_diagnosis_line_format():
    line = registry.alias_diagnosis(
        "librechat-bff",
        "sha256:" + "1" * 64,
        "sha256:" + "2" * 64,
        {"linux/amd64": "sha256:" + "3" * 64},
        "aaa",
        "n/a",
    )
    assert line == (
        "ALIAS: librechat-bff live index 111111111111 != published index 222222222222; "
        "shared platform manifests {linux/amd64: 333333333333}; "
        "build revisions live=aaa published=n/a; "
        "remedy: operator accepts the alias, or clean the node image cache "
        "(crictl rmi <config>) and re-verify"
    )
