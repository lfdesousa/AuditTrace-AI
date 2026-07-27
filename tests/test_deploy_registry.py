"""Unit tests for the deploy runner's image-digest resolution (P1).

The single network egress point (``registry._http_get``) is monkeypatched; no
real sockets. Covers the hub happy path, the local best-effort path, and every
error branch (auth failure, missing token, non-200 manifest, missing digest,
unknown backend).
"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError

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
        if "auth.docker.io" in url:
            return 200, {}, json.dumps({"token": "tok123"}).encode()
        assert headers["Authorization"] == "Bearer tok123"
        return 200, _digest_headers("sha256:cafe"), b""

    monkeypatch.setattr(registry, "_http_get", fake_get)
    ref = registry.resolve("v1.2.3", "hub")
    assert ref.repository == "docker.io/lfds/audittrace-memory-server"
    assert ref.digest == "sha256:cafe"
    assert ref.pinned is True
    assert any("auth.docker.io" in u for u in calls)


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
        if "auth.docker.io" in url:
            return 200, {}, json.dumps({"token": "t"}).encode()
        return 404, {}, b""

    monkeypatch.setattr(registry, "_http_get", fake_get)
    with pytest.raises(DigestResolutionError, match="HTTP 404"):
        registry.resolve("v1", "hub")


def test_resolve_hub_manifest_missing_digest(monkeypatch):
    def fake_get(url, headers=None):
        if "auth.docker.io" in url:
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
        if "auth.docker.io" in url:
            return 200, {}, json.dumps({"token": "t"}).encode()
        raise HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(registry, "_http_get", fake_get)
    with pytest.raises(DigestResolutionError, match="manifest fetch"):
        registry.resolve("v9.does.not.exist", "hub")


def test_resolve_hub_manifest_urlerror_becomes_clean(monkeypatch):
    def fake_get(url, headers=None):
        if "auth.docker.io" in url:
            return 200, {}, json.dumps({"token": "t"}).encode()
        raise URLError("connection reset")

    monkeypatch.setattr(registry, "_http_get", fake_get)
    with pytest.raises(DigestResolutionError, match="manifest fetch"):
        registry.resolve("1.13.0", "hub")
