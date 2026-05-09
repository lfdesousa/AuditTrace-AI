"""Tests for routes/memory_upload/router.py — GET /memory/upload/status
(ADR-048 PR-B3)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.routes.memory_upload import manifest as manifest_mod
from audittrace.routes.memory_upload.router import _get_session_factory


class TestGetSessionFactory:
    """The default `_get_session_factory()` reaches into the DI
    container — we verify the real call-chain rather than only the
    dependency_overrides path the route tests use."""

    def test_returns_a_sessionmaker_from_di_container(self) -> None:
        # Bypass `register_default_dependencies` (it requires real
        # MinIO settings to bootstrap S3 services) and wire only the
        # postgres_factory we need for this code path.
        from audittrace.dependencies import container

        container._instances["postgres_factory"] = InMemoryPostgresFactory()
        try:
            sf = _get_session_factory()
            assert isinstance(sf, sessionmaker)
        finally:
            container._instances.pop("postgres_factory", None)


def _seed_pending(factory, scan_id: str, *, user_id: str = "alice") -> None:
    with factory() as session:
        manifest_mod.insert_pending_scan(
            session,
            scan_id=scan_id,
            user_id=user_id,
            object_uri=f"s3://memory-shared/quarantine/{user_id}/{scan_id}/x.pdf",
            object_sha256="0" * 64,
            size_bytes=42,
            title="x.pdf",
            trace_id="trace-abc",
        )


def _stub_auth_and_factory(client: TestClient, factory, *, sub: str, scope: str):
    """Common auth + factory setup. Returns the patcher context manager
    that the test uses inside `with`."""
    client.app.dependency_overrides[_get_session_factory] = lambda: factory

    mock_settings = patch("audittrace.auth.get_settings")
    mock_jwks = patch("audittrace.auth._get_jwks_keys")
    mock_decode = patch("audittrace.auth._decode_jwt_with_allowed_issuers")
    return mock_settings, mock_jwks, mock_decode, sub, scope


class TestGetUploadStatus:
    def test_returns_pending_scan_for_owner(self, client: TestClient) -> None:
        factory = InMemoryPostgresFactory().get_session_factory()
        _seed_pending(factory, "scan-1")
        client.app.dependency_overrides[_get_session_factory] = lambda: factory
        try:
            with (
                patch("audittrace.auth.get_settings") as mock_settings,
                patch("audittrace.auth._get_jwks_keys") as mock_jwks,
                patch(
                    "audittrace.auth._decode_jwt_with_allowed_issuers"
                ) as mock_decode,
            ):
                mock_settings.return_value = MagicMock(
                    auth_enabled=True, auth_required=True
                )
                mock_jwks.return_value = ["fake-key"]
                mock_decode.return_value = {
                    "sub": "alice",
                    "scope": "memory:episodic:write",
                }
                response = client.get(
                    "/memory/upload/status",
                    params={"scan_id": "scan-1"},
                    headers={"Authorization": "Bearer fake-token"},
                )
        finally:
            client.app.dependency_overrides.pop(_get_session_factory, None)

        assert response.status_code == 200
        data = response.json()
        assert data["scan_id"] == "scan-1"
        assert data["status"] == "pending_scan"
        assert data["trace_id"] == "trace-abc"
        assert data["size_bytes"] == 42
        assert data["object_sha256"] == "0" * 64

    def test_returns_404_on_unknown_scan_id(self, client: TestClient) -> None:
        factory = InMemoryPostgresFactory().get_session_factory()
        client.app.dependency_overrides[_get_session_factory] = lambda: factory
        try:
            with (
                patch("audittrace.auth.get_settings") as mock_settings,
                patch("audittrace.auth._get_jwks_keys") as mock_jwks,
                patch(
                    "audittrace.auth._decode_jwt_with_allowed_issuers"
                ) as mock_decode,
            ):
                mock_settings.return_value = MagicMock(
                    auth_enabled=True, auth_required=True
                )
                mock_jwks.return_value = ["fake-key"]
                mock_decode.return_value = {
                    "sub": "alice",
                    "scope": "memory:episodic:write",
                }
                response = client.get(
                    "/memory/upload/status",
                    params={"scan_id": "nope"},
                    headers={"Authorization": "Bearer fake-token"},
                )
        finally:
            client.app.dependency_overrides.pop(_get_session_factory, None)
        assert response.status_code == 404

    def test_cross_tenant_lookup_returns_404(self, client: TestClient) -> None:
        # Same 404 shape as "unknown scan_id" — don't leak existence.
        factory = InMemoryPostgresFactory().get_session_factory()
        _seed_pending(factory, "scan-9", user_id="bob")
        client.app.dependency_overrides[_get_session_factory] = lambda: factory
        try:
            with (
                patch("audittrace.auth.get_settings") as mock_settings,
                patch("audittrace.auth._get_jwks_keys") as mock_jwks,
                patch(
                    "audittrace.auth._decode_jwt_with_allowed_issuers"
                ) as mock_decode,
            ):
                mock_settings.return_value = MagicMock(
                    auth_enabled=True, auth_required=True
                )
                mock_jwks.return_value = ["fake-key"]
                mock_decode.return_value = {
                    "sub": "alice",
                    "scope": "memory:episodic:write",
                }
                response = client.get(
                    "/memory/upload/status",
                    params={"scan_id": "scan-9"},
                    headers={"Authorization": "Bearer fake-token"},
                )
        finally:
            client.app.dependency_overrides.pop(_get_session_factory, None)
        assert response.status_code == 404

    def test_admin_can_read_any_scan_id(self, client: TestClient) -> None:
        factory = InMemoryPostgresFactory().get_session_factory()
        _seed_pending(factory, "scan-x", user_id="bob")
        client.app.dependency_overrides[_get_session_factory] = lambda: factory
        try:
            with (
                patch("audittrace.auth.get_settings") as mock_settings,
                patch("audittrace.auth._get_jwks_keys") as mock_jwks,
                patch(
                    "audittrace.auth._decode_jwt_with_allowed_issuers"
                ) as mock_decode,
            ):
                mock_settings.return_value = MagicMock(
                    auth_enabled=True, auth_required=True
                )
                mock_jwks.return_value = ["fake-key"]
                mock_decode.return_value = {
                    "sub": "ops",
                    "scope": "audittrace:admin",
                }
                response = client.get(
                    "/memory/upload/status",
                    params={"scan_id": "scan-x"},
                    headers={"Authorization": "Bearer fake-token"},
                )
        finally:
            client.app.dependency_overrides.pop(_get_session_factory, None)
        assert response.status_code == 200
        assert response.json()["scan_id"] == "scan-x"
