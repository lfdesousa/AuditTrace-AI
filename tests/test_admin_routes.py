"""Tests for /admin/* routes (ADR-052).

Covers POST /admin/trust-store/refresh and GET /admin/trust-store:
  - Happy-path refresh: builder.build() → provider.store() → 200
  - Builder-unavailable: TrustStoreBuilderUnavailableError → 502
  - Provider write failure → 500
  - Metadata read: 200 when bundle exists, 404 when not provisioned
  - Auth gate: scope `audittrace:admin` required (defence-in-depth
    on top of validate_jwt's scope check; mirrors the per-route
    is_admin guard from routes/memory.py:200-206)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from audittrace.identity import UserContext, sentinel_user_context
from audittrace.services.trust_store import (
    TrustStoreBuilderUnavailableError,
    TrustStoreBundle,
    TrustStoreMetadata,
)


def _make_bundle(*, builder_id: str = "test", cert_count: int = 7) -> TrustStoreBundle:
    metadata = TrustStoreMetadata(
        sha256="ab" * 32,
        builder_id=builder_id,
        built_at=datetime(2026, 5, 9, 10, 0, tzinfo=UTC),
        cert_count=cert_count,
        source_url="https://example.test/lotl",
    )
    return TrustStoreBundle(
        pem_bytes=b"-----BEGIN CERTIFICATE-----\nfoo\n", metadata=metadata
    )


def _override_builder(client: Any, builder: Any) -> None:
    """Override the trust_store_builder DI dependency on the client app."""
    from audittrace.dependencies import get_trust_store_builder

    client.app.dependency_overrides[get_trust_store_builder] = lambda: builder


def _override_provider(client: Any, provider: Any) -> None:
    """Override the trust_store_provider DI dependency on the client app."""
    from audittrace.dependencies import get_trust_store_provider

    client.app.dependency_overrides[get_trust_store_provider] = lambda: provider


def _override_user(client: Any, user: UserContext) -> None:
    """Override the require_user dependency to a specific UserContext."""
    from audittrace.auth import require_user

    client.app.dependency_overrides[require_user] = lambda: user


# ───────────────────── POST /admin/trust-store/refresh ─────────────────


class TestRefreshTrustStoreEndpoint:
    def test_refresh_happy_path_returns_metadata(self, client: Any) -> None:
        bundle = _make_bundle(builder_id="eu_lotl", cert_count=42)
        builder = MagicMock()
        builder.builder_id = "eu_lotl"
        builder.build = AsyncMock(return_value=bundle)
        provider = MagicMock()
        provider.store = MagicMock()

        _override_builder(client, builder)
        _override_provider(client, provider)
        try:
            r = client.post("/system/trust-store/refresh")
        finally:
            client.app.dependency_overrides.clear()

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["sha256"] == "ab" * 32
        assert body["builder_id"] == "eu_lotl"
        assert body["cert_count"] == 42
        assert body["source_url"] == "https://example.test/lotl"
        # built_at round-trips as ISO8601.
        assert body["built_at"].startswith("2026-05-09T10:00:00")

        # Verify the provider was actually called with the built bundle.
        builder.build.assert_awaited_once()
        provider.store.assert_called_once_with(bundle)

    def test_refresh_returns_502_when_builder_unavailable(self, client: Any) -> None:
        builder = MagicMock()
        builder.builder_id = "eu_lotl"
        builder.build = AsyncMock(
            side_effect=TrustStoreBuilderUnavailableError(
                "EuLotlTrustStoreBuilder: pyhanko[etsi] extra not installed"
            )
        )
        provider = MagicMock()
        provider.store = MagicMock()

        _override_builder(client, builder)
        _override_provider(client, provider)
        try:
            r = client.post("/system/trust-store/refresh")
        finally:
            client.app.dependency_overrides.clear()

        assert r.status_code == 502, r.text
        body = r.json()
        # FastAPI wraps HTTPException.detail under "detail".
        assert body["detail"]["error"] == "trust_store_build_failed"
        assert body["detail"]["builder_id"] == "eu_lotl"
        assert "pyhanko[etsi]" in body["detail"]["cause"]
        # Critically: provider.store MUST NOT have been called when
        # the builder failed — the cached bundle stays put.
        provider.store.assert_not_called()

    def test_refresh_returns_500_when_provider_store_fails(self, client: Any) -> None:
        bundle = _make_bundle()
        builder = MagicMock()
        builder.builder_id = "eu_lotl"
        builder.build = AsyncMock(return_value=bundle)
        provider = MagicMock()
        provider.store = MagicMock(side_effect=RuntimeError("MinIO write timeout"))

        _override_builder(client, builder)
        _override_provider(client, provider)
        try:
            r = client.post("/system/trust-store/refresh")
        finally:
            client.app.dependency_overrides.clear()

        assert r.status_code == 500, r.text
        body = r.json()
        assert body["detail"]["error"] == "trust_store_persist_failed"
        assert "MinIO write timeout" in body["detail"]["cause"]

    def test_refresh_invalidates_validation_context(self, client: Any) -> None:
        """After a successful refresh, the in-process ValidationContext
        singleton is invalidated so the next signature check rebuilds
        against the freshly-stored PEM."""
        from audittrace.routes import memory as memory_route

        # Prime the singleton.
        memory_route._VALIDATION_CONTEXT = MagicMock()
        memory_route._VC_TRUST_STORE_PATH = "primed-cache-key"

        bundle = _make_bundle()
        builder = MagicMock()
        builder.builder_id = "eu_lotl"
        builder.build = AsyncMock(return_value=bundle)
        provider = MagicMock()

        _override_builder(client, builder)
        _override_provider(client, provider)
        try:
            r = client.post("/system/trust-store/refresh")
        finally:
            client.app.dependency_overrides.clear()

        assert r.status_code == 200
        # Singleton was reset.
        assert memory_route._VALIDATION_CONTEXT is None
        assert memory_route._VC_TRUST_STORE_PATH == ""

    def test_refresh_403_when_user_not_admin(self, client: Any) -> None:
        """Non-admin user (no audittrace:admin scope) gets 403 from
        the per-route guard. Mirrors the in-route is_admin check
        from routes/memory.py:200-206."""
        from dataclasses import replace

        non_admin = replace(sentinel_user_context(), is_admin=False, scopes=())
        bundle = _make_bundle()
        builder = MagicMock()
        builder.builder_id = "eu_lotl"
        builder.build = AsyncMock(return_value=bundle)
        provider = MagicMock()

        _override_user(client, non_admin)
        _override_builder(client, builder)
        _override_provider(client, provider)
        try:
            r = client.post("/system/trust-store/refresh")
        finally:
            client.app.dependency_overrides.clear()

        assert r.status_code == 403, r.text
        assert "audittrace:admin" in r.json()["detail"]
        # Provider must NOT have been called.
        provider.store.assert_not_called()


# ───────────────────── GET /admin/trust-store ──────────────────────────


class TestGetTrustStoreMetadataEndpoint:
    def test_get_metadata_returns_200_when_bundle_exists(self, client: Any) -> None:
        provider = MagicMock()
        bundle = _make_bundle(builder_id="static", cert_count=3)
        provider.metadata = MagicMock(return_value=bundle.metadata)

        _override_provider(client, provider)
        try:
            r = client.get("/system/trust-store")
        finally:
            client.app.dependency_overrides.clear()

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["builder_id"] == "static"
        assert body["cert_count"] == 3

    def test_get_metadata_returns_404_when_not_provisioned(self, client: Any) -> None:
        provider = MagicMock()
        provider.metadata = MagicMock(return_value=None)

        _override_provider(client, provider)
        try:
            r = client.get("/system/trust-store")
        finally:
            client.app.dependency_overrides.clear()

        assert r.status_code == 404, r.text
        body = r.json()
        assert body["detail"]["error"] == "trust_store_not_provisioned"
        assert "POST /system/trust-store/refresh" in body["detail"]["hint"]

    def test_get_metadata_403_when_user_not_admin(self, client: Any) -> None:
        from dataclasses import replace

        non_admin = replace(sentinel_user_context(), is_admin=False, scopes=())
        provider = MagicMock()
        provider.metadata = MagicMock(return_value=None)

        _override_user(client, non_admin)
        _override_provider(client, provider)
        try:
            r = client.get("/system/trust-store")
        finally:
            client.app.dependency_overrides.clear()

        assert r.status_code == 403, r.text
        assert "audittrace:admin" in r.json()["detail"]
        # Provider's metadata() must NOT have been called when the
        # auth gate trips.
        provider.metadata.assert_not_called()
