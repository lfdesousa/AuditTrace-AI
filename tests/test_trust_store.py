"""Tests for the PAdES trust-store services (ADR-052).

Covers:
  - TrustStoreMetadata + TrustStoreBundle dataclasses
  - S3TrustStoreProvider (round-trip via stub MinIO client)
  - MockTrustStoreProvider
  - StaticTrustStoreBuilder (real fixture directory)
  - EuLotlTrustStoreBuilder (mocked pyhanko[etsi] lotl_to_registry)
  - Service-type closed-set discipline (mirrors ADR-052 §3 + the
    TestExtractionWarningCodes / TestSignatureStatusCodes pattern)
"""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from audittrace.services.trust_store import (
    _QC_SERVICE_TYPES,
    CompositeTrustStoreBuilder,
    EuLotlTrustStoreBuilder,
    MockTrustStoreProvider,
    S3TrustStoreProvider,
    StaticTrustStoreBuilder,
    SwissTslTrustStoreBuilder,
    TrustStoreBuilder,
    TrustStoreBuilderUnavailableError,
    TrustStoreMetadata,
    _bundle_from_pem,
    _count_pem_certs,
    _registry_to_pem_bundle,
)

_FAKE_PEM_ONE = (
    b"-----BEGIN CERTIFICATE-----\n"
    b"MIIBfTCCASOgAwIBAgIBATAFBgMrZXAwIzEhMB8GA1UEAwwYQXVkaXRUcmFjZS1B\n"
    b"-----END CERTIFICATE-----\n"
)

# Repo-rooted path to the vendored Swiss TSLO cert. Computed from
# __file__ so the tests work both on a developer's laptop and on a
# CI runner (different home directories). Caught by CI 2026-05-09:
# hard-coded absolute paths failed because the runner's repo root
# is /home/runner/work/AuditTrace-AI/AuditTrace-AI/, not the
# developer's /home/lfdesousa/work/AuditTrace-AI/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_VENDORED_TSLO_CERT_PATH = (
    _REPO_ROOT / "charts/audittrace/trust-store/swiss-federal-tsl/CH-TL-cert.der"
)
_FAKE_PEM_TWO = (
    b"-----BEGIN CERTIFICATE-----\n"
    b"MIIBfTCCASOgAwIBAgIBAjAFBgMrZXAwIzEhMB8GA1UEAwwYQXVkaXRUcmFjZS1C\n"
    b"-----END CERTIFICATE-----\n"
)


# ──────────────────────────── Helpers ───────────────────────────────────


def _make_metadata(**overrides: Any) -> TrustStoreMetadata:
    defaults: dict[str, Any] = {
        "sha256": "0" * 64,
        "builder_id": "test",
        "built_at": datetime(2026, 5, 9, 9, 0, tzinfo=UTC),
        "cert_count": 1,
        "source_url": "test://fixture",
    }
    defaults.update(overrides)
    return TrustStoreMetadata(**defaults)


# ─────────────────── Bundle / metadata dataclasses ─────────────────────


class TestTrustStoreMetadata:
    def test_to_dict_roundtrips_isoformat(self) -> None:
        meta = _make_metadata(cert_count=42)
        d = meta.to_dict()
        assert d["sha256"] == "0" * 64
        assert d["builder_id"] == "test"
        assert d["cert_count"] == 42
        assert d["source_url"] == "test://fixture"
        # ISO8601 with tz offset.
        assert d["built_at"].startswith("2026-05-09T09:00:00")

    def test_metadata_is_frozen(self) -> None:
        meta = _make_metadata()
        with pytest.raises((AttributeError, TypeError)):
            meta.sha256 = "x" * 64  # type: ignore[misc]


class TestTrustStoreBundle:
    def test_bundle_from_pem_computes_sha256(self) -> None:
        bundle = _bundle_from_pem(
            _FAKE_PEM_ONE,
            builder_id="test",
            source_url="test://x",
            cert_count=1,
        )
        # sha256 of the PEM bytes — deterministic.
        import hashlib

        expected = hashlib.sha256(_FAKE_PEM_ONE).hexdigest()
        assert bundle.metadata.sha256 == expected
        assert bundle.metadata.builder_id == "test"
        assert bundle.metadata.cert_count == 1
        # built_at is recent (within last 60s — flake guard).
        delta = datetime.now(UTC) - bundle.metadata.built_at
        assert delta.total_seconds() < 60

    def test_count_pem_certs_counts_begin_markers(self) -> None:
        assert _count_pem_certs(_FAKE_PEM_ONE) == 1
        assert _count_pem_certs(_FAKE_PEM_ONE + _FAKE_PEM_TWO) == 2
        assert _count_pem_certs(b"") == 0
        assert _count_pem_certs(b"not a cert") == 0


# ─────────────────────── MockTrustStoreProvider ─────────────────────────


class TestMockTrustStoreProvider:
    def test_load_before_store_raises_filenotfound(self) -> None:
        provider = MockTrustStoreProvider()
        with pytest.raises(FileNotFoundError):
            provider.load()

    def test_metadata_before_store_returns_none(self) -> None:
        provider = MockTrustStoreProvider()
        assert provider.metadata() is None

    def test_store_then_load_roundtrips(self) -> None:
        provider = MockTrustStoreProvider()
        bundle = _bundle_from_pem(
            _FAKE_PEM_ONE,
            builder_id="test",
            source_url="test://x",
            cert_count=1,
        )
        provider.store(bundle)
        loaded = provider.load()
        assert loaded.pem_bytes == _FAKE_PEM_ONE
        assert loaded.metadata.sha256 == bundle.metadata.sha256

    def test_metadata_after_store_returns_metadata(self) -> None:
        provider = MockTrustStoreProvider()
        bundle = _bundle_from_pem(
            _FAKE_PEM_ONE,
            builder_id="test",
            source_url="test://x",
            cert_count=1,
        )
        provider.store(bundle)
        meta = provider.metadata()
        assert meta is not None
        assert meta.sha256 == bundle.metadata.sha256


# ───────────────────── S3TrustStoreProvider ────────────────────────────


class _FakeMinioResponse:
    """Mimics minio.Minio's get_object context-manager response."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeMinioResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def read(self) -> bytes:
        return self._body


class _FakeMinioClient:
    """Two-object in-memory MinIO substitute. Matches the methods
    S3TrustStoreProvider actually calls (get_object + put_object)."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def get_object(self, bucket: str, key: str) -> _FakeMinioResponse:
        if (bucket, key) not in self.objects:
            err = Exception(f"NoSuchKey: {bucket}/{key}")
            err.code = "NoSuchKey"  # type: ignore[attr-defined]
            raise err
        return _FakeMinioResponse(self.objects[(bucket, key)])

    def put_object(
        self,
        bucket: str,
        key: str,
        stream: io.BytesIO,
        length: int,
        content_type: str = "",
    ) -> None:
        self.objects[(bucket, key)] = stream.read()


class TestS3TrustStoreProvider:
    def test_load_when_not_provisioned_raises_filenotfound(self) -> None:
        client = _FakeMinioClient()
        provider = S3TrustStoreProvider(minio_client=client, bucket="memory-shared")
        with pytest.raises(FileNotFoundError, match="trust store not provisioned"):
            provider.load()

    def test_metadata_when_not_provisioned_returns_none(self) -> None:
        client = _FakeMinioClient()
        provider = S3TrustStoreProvider(minio_client=client, bucket="memory-shared")
        assert provider.metadata() is None

    def test_store_then_load_roundtrips_pem_and_metadata(self) -> None:
        client = _FakeMinioClient()
        provider = S3TrustStoreProvider(minio_client=client, bucket="memory-shared")
        bundle = _bundle_from_pem(
            _FAKE_PEM_ONE + _FAKE_PEM_TWO,
            builder_id="test",
            source_url="test://x",
            cert_count=2,
        )
        provider.store(bundle)
        # Two objects landed: PEM + metadata sidecar.
        assert ("memory-shared", "trust-store/eu-lotl-bundle.pem") in client.objects
        assert (
            "memory-shared",
            "trust-store/eu-lotl-bundle.metadata.json",
        ) in client.objects
        # Round-trip.
        loaded = provider.load()
        assert loaded.pem_bytes == _FAKE_PEM_ONE + _FAKE_PEM_TWO
        assert loaded.metadata.sha256 == bundle.metadata.sha256
        assert loaded.metadata.cert_count == 2
        assert loaded.metadata.builder_id == "test"

    def test_metadata_returns_only_sidecar_not_pem(self) -> None:
        """metadata() is cheaper than load() — only fetches the JSON
        sidecar. Verifies the optimisation actually fires."""
        client = _FakeMinioClient()
        provider = S3TrustStoreProvider(minio_client=client, bucket="memory-shared")
        bundle = _bundle_from_pem(
            _FAKE_PEM_ONE,
            builder_id="x",
            source_url="test://x",
            cert_count=1,
        )
        provider.store(bundle)
        get_calls: list[tuple[str, str]] = []
        original_get = client.get_object

        def _tracking_get(bucket: str, key: str) -> _FakeMinioResponse:
            get_calls.append((bucket, key))
            return original_get(bucket, key)

        client.get_object = _tracking_get  # type: ignore[assignment]
        meta = provider.metadata()
        assert meta is not None
        # metadata() must NOT pull the PEM key.
        pem_calls = [c for c in get_calls if c[1].endswith(".pem")]
        assert pem_calls == []


# ───────────────────── StaticTrustStoreBuilder ─────────────────────────


class TestStaticTrustStoreBuilder:
    def test_builder_id_is_stable(self) -> None:
        builder = StaticTrustStoreBuilder(directory="/nonexistent")
        assert builder.builder_id == "static"

    def test_build_raises_when_directory_missing(self) -> None:
        builder = StaticTrustStoreBuilder(
            directory="/tmp/audittrace-test-trust-store-NONEXISTENT-xyz"
        )
        with pytest.raises(TrustStoreBuilderUnavailableError, match="not found"):
            asyncio.run(builder.build())

    def test_build_raises_when_directory_empty(self, tmp_path: Any) -> None:
        builder = StaticTrustStoreBuilder(directory=tmp_path)
        with pytest.raises(TrustStoreBuilderUnavailableError, match="no .pem"):
            asyncio.run(builder.build())

    def test_build_concatenates_pem_files_sorted(self, tmp_path: Any) -> None:
        # Two files with deliberate non-alphabetic original names —
        # builder must sort to make output byte-deterministic.
        (tmp_path / "z-second.pem").write_bytes(_FAKE_PEM_TWO)
        (tmp_path / "a-first.pem").write_bytes(_FAKE_PEM_ONE)
        # Non-PEM file: must be ignored.
        (tmp_path / "ignored.txt").write_text("not a cert")

        builder = StaticTrustStoreBuilder(directory=tmp_path)
        bundle = asyncio.run(builder.build())

        # Both PEMs concatenated in sorted order (a-first before z-second).
        assert bundle.metadata.cert_count == 2
        assert bundle.metadata.builder_id == "static"
        assert bundle.metadata.source_url.startswith("file://")
        # First cert appears before second cert in the bundle bytes.
        first_idx = bundle.pem_bytes.index(_FAKE_PEM_ONE.rstrip())
        second_idx = bundle.pem_bytes.index(_FAKE_PEM_TWO.rstrip())
        assert first_idx < second_idx


# ───────────────────── EuLotlTrustStoreBuilder ─────────────────────────


class TestEuLotlTrustStoreBuilder:
    def test_builder_id_is_stable(self) -> None:
        builder = EuLotlTrustStoreBuilder()
        assert builder.builder_id == "eu_lotl"

    def test_build_raises_unavailable_on_import_error(self) -> None:
        """If pyhanko[etsi] is missing, build() raises a typed
        TrustStoreBuilderUnavailableError instead of bubbling
        ImportError. Surfaces as a 502 with the cause from the
        admin endpoint."""
        builder = EuLotlTrustStoreBuilder()
        # The import is inside build(); patch the qualified path.
        with patch.dict(
            "sys.modules",
            {"pyhanko.sign.validation.qualified.eutl_fetch": None},
        ):
            with pytest.raises(TrustStoreBuilderUnavailableError, match="extras not"):
                asyncio.run(builder.build())

    def test_registry_to_pem_bundle_primary_path(self) -> None:
        """Real pyhanko TSPRegistry exposes
        ``known_certificate_authorities`` yielding ``Authority`` objects
        with a ``.certificate`` returning an asn1crypto cert
        (``cert.dump()`` → DER bytes). Verify the primary code path
        walks that shape and emits one PEM block per CA.
        """

        # Two synthetic CAs, each carrying a fake DER blob via
        # an Authority-shaped wrapper. The real pyhanko shape is
        # AuthorityWithCert(cert) where cert.dump() returns DER.
        class _FakeCert:
            def __init__(self, der: bytes) -> None:
                self._der = der

            def dump(self) -> bytes:
                return self._der

        class _FakeAuthority:
            def __init__(self, der: bytes) -> None:
                self.certificate = _FakeCert(der)

        registry = SimpleNamespace(
            known_certificate_authorities=[
                _FakeAuthority(b"\x30\x82der-A"),
                _FakeAuthority(b"\x30\x82der-B"),
                # Duplicate of A — exercise the dedup path.
                _FakeAuthority(b"\x30\x82der-A"),
            ]
        )
        pem_bytes = _registry_to_pem_bundle(registry)
        # Two unique certs (the duplicate was deduped).
        assert _count_pem_certs(pem_bytes) == 2

    def test_build_filters_to_qc_service_types(self) -> None:
        """The walker emits a registry of mixed service types
        (QC, TLS, etc.); the builder must keep only QC-for-ESig
        services and drop the rest."""
        # Synthesise a fake registry: two services, one QC and one TLS.
        qc_service = SimpleNamespace(
            service_type_identifier="http://uri.etsi.org/TrstSvc/Svctype/CA/QC",
            service_digital_identities=[
                SimpleNamespace(x509_certificate=b"\x30\x82\x01\x00qc-cert-der")
            ],
        )
        tls_service = SimpleNamespace(
            service_type_identifier="http://uri.etsi.org/TrstSvc/Svctype/CA/PKC",
            service_digital_identities=[
                SimpleNamespace(x509_certificate=b"\x30\x82\x01\x00tls-cert-der")
            ],
        )
        registry = SimpleNamespace(services=[qc_service, tls_service])

        pem_bytes = _registry_to_pem_bundle(registry)
        # Exactly one cert (the QC one) — TLS service was filtered.
        assert _count_pem_certs(pem_bytes) == 1
        # The QC cert's DER appears base64-encoded somewhere in the PEM.
        import base64

        b64_qc = base64.b64encode(b"\x30\x82\x01\x00qc-cert-der").decode("ascii")
        assert b64_qc[:20] in pem_bytes.decode("ascii", errors="replace")

    def test_build_calls_lotl_to_registry_with_bundled_bootstrap(self) -> None:
        """build() must call lotl_to_registry with lotl_xml=None so
        pyhanko fetches the live LOTL using its bundled bootstrap
        keys (per ADR-052 §6 — no out-of-band cert vendoring)."""
        builder = EuLotlTrustStoreBuilder()

        qc_service = SimpleNamespace(
            service_type_identifier="http://uri.etsi.org/TrstSvc/Svctype/CA/QC",
            service_digital_identities=[
                SimpleNamespace(x509_certificate=b"\x30\x82der-bytes")
            ],
        )
        registry = SimpleNamespace(services=[qc_service])
        mock_lotl = AsyncMock(return_value=(registry, []))

        with patch(
            "pyhanko.sign.validation.qualified.eutl_fetch.lotl_to_registry",
            mock_lotl,
        ):
            bundle = asyncio.run(builder.build())

        # The signature requires both lotl_xml=None (use bundled
        # bootstrap keys) and a fresh aiohttp ClientSession per build.
        mock_lotl.assert_awaited_once()
        call = mock_lotl.await_args
        assert call.kwargs["lotl_xml"] is None
        assert call.kwargs["client"] is not None
        assert bundle.metadata.builder_id == "eu_lotl"
        assert bundle.metadata.source_url == EuLotlTrustStoreBuilder.SOURCE_URL
        assert bundle.metadata.cert_count >= 1

    def test_build_logs_per_tsl_errors_but_succeeds(self) -> None:
        """Per-TSL errors from pyhanko (one member-state TSL down)
        are non-fatal — log + continue with the rest of the registry.

        Test patches ``logger.warning`` directly to assert the call —
        more robust across pytest caplog isolation than relying on
        record-level filters across the full suite (caught me out
        earlier; the records didn't propagate cleanly under the
        full-suite ``conftest._propagate_logs`` autouse fixture)."""
        builder = EuLotlTrustStoreBuilder()

        qc_service = SimpleNamespace(
            service_type_identifier="http://uri.etsi.org/TrstSvc/Svctype/CA/QC",
            service_digital_identities=[
                SimpleNamespace(x509_certificate=b"\x30\x82der")
            ],
        )
        registry = SimpleNamespace(services=[qc_service])
        # Simulate a per-TSL fetch error coming back alongside a partial
        # registry — pyhanko's documented happy-path.
        per_tsl_error = Exception("DE TSL fetch timeout")
        mock_lotl = AsyncMock(return_value=(registry, [per_tsl_error]))

        with patch("audittrace.services.trust_store.logger.warning") as mock_warning:
            with patch(
                "pyhanko.sign.validation.qualified.eutl_fetch.lotl_to_registry",
                mock_lotl,
            ):
                bundle = asyncio.run(builder.build())

        # Bundle still produced — per-TSL error did not abort.
        assert bundle.metadata.cert_count >= 1
        # And the per-TSL error landed in a logger.warning call, not
        # as a raise. Walk the calls to find the one with our error.
        calls = list(mock_warning.call_args_list)
        per_tsl_calls = [
            c for c in calls if "lotl_to_registry per-TSL error" in str(c.args[0])
        ]
        assert len(per_tsl_calls) == 1
        # The error object is passed positional in the second arg.
        assert per_tsl_calls[0].args[1] is per_tsl_error

    def test_build_wraps_lotl_to_registry_exceptions_as_unavailable(self) -> None:
        """Network failure / EU LOTL outage / XAdES-verification
        failure during the walker call surfaces as a typed
        TrustStoreBuilderUnavailableError (admin endpoint → 502)."""
        builder = EuLotlTrustStoreBuilder()
        mock_lotl = AsyncMock(side_effect=ConnectionError("LOTL unreachable"))

        with patch(
            "pyhanko.sign.validation.qualified.eutl_fetch.lotl_to_registry",
            mock_lotl,
        ):
            with pytest.raises(
                TrustStoreBuilderUnavailableError, match="LOTL unreachable"
            ):
                asyncio.run(builder.build())


# ────────────────── SwissTslTrustStoreBuilder (ADR-053) ────────────────


class TestSwissTslTrustStoreBuilder:
    def test_builder_id_is_stable(self, tmp_path: Any) -> None:
        builder = SwissTslTrustStoreBuilder(tslo_cert_path=tmp_path / "ch-tl.der")
        assert builder.builder_id == "swiss_tsl"

    def test_build_raises_unavailable_when_tslo_cert_missing(
        self, tmp_path: Any
    ) -> None:
        """The TSLO cert is the OOB-verified bootstrap — without it
        the builder cannot validate the Swiss TSL's signature.
        Surface as TrustStoreBuilderUnavailableError so the admin
        endpoint returns 502, not 500 (per ADR-053 failure-modes)."""
        builder = SwissTslTrustStoreBuilder(
            tslo_cert_path=tmp_path / "does-not-exist.der"
        )
        with pytest.raises(
            TrustStoreBuilderUnavailableError, match="TSLO cert not found"
        ):
            asyncio.run(builder.build())

    def test_build_raises_unavailable_when_tslo_cert_unparseable(
        self, tmp_path: Any
    ) -> None:
        """Garbage bytes at the TSLO cert path → typed unavailable,
        not a generic crash."""
        bad_path = tmp_path / "bad.der"
        bad_path.write_bytes(b"this is not a certificate")
        builder = SwissTslTrustStoreBuilder(tslo_cert_path=bad_path)
        with pytest.raises(
            TrustStoreBuilderUnavailableError,
            match="failed to parse TSLO cert",
        ):
            asyncio.run(builder.build())

    def test_build_wraps_fetch_failures_as_unavailable(self, tmp_path: Any) -> None:
        """Network outage during the Swiss TSL fetch → typed
        unavailable so the admin endpoint returns 502."""
        # Vendor a real cert (extracted from main_signed.pdf — known-
        # parseable by asn1crypto; we don't actually validate against
        # it because aiohttp.get is mocked to raise before we reach
        # trust_list_to_registry).
        builder = SwissTslTrustStoreBuilder(tslo_cert_path=_VENDORED_TSLO_CERT_PATH)

        # Fake aiohttp.ClientSession that raises on .get().
        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, *args, **kwargs):
                raise ConnectionError("OFCOM down")

        with patch(
            "aiohttp.ClientSession",
            return_value=_FakeSession(),
        ):
            with pytest.raises(
                TrustStoreBuilderUnavailableError, match="failed to fetch Swiss TSL"
            ):
                asyncio.run(builder.build())

    def test_build_wraps_signature_validation_failures_as_unavailable(
        self, tmp_path: Any
    ) -> None:
        """The XAdES validation step raising (TSLO cert mismatch, TSL
        tampered in transit, schema drift, malformed XML) surfaces as
        typed unavailable so the admin endpoint returns 502 with the
        cause string in the response body."""
        builder = SwissTslTrustStoreBuilder(tslo_cert_path=_VENDORED_TSLO_CERT_PATH)

        class _FakeResp:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def raise_for_status(self):
                pass

            async def text(self):
                return "<malformed/>"

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, *args, **kwargs):
                return _FakeResp()

        with (
            patch("aiohttp.ClientSession", return_value=_FakeSession()),
            patch(
                "pyhanko.sign.validation.qualified.eutl_parse._validate_and_extract_tl_data_multiple_certs",
                side_effect=ValueError("XAdES verification failed"),
            ),
        ):
            with pytest.raises(
                TrustStoreBuilderUnavailableError,
                match="TSL signature validation failed",
            ):
                asyncio.run(builder.build())

    def test_build_happy_path_extracts_qc_certs_via_swiss_namespace(
        self, tmp_path: Any
    ) -> None:
        """The Swiss-aware extraction path walks the XML directly
        (URI-suffix matched) and emits PEM blocks for every
        ``X509Certificate`` attached to a ``TSPService`` whose
        ``ServiceTypeIdentifier`` ends in ``/TrstSvc/Svctype/CA/QC``.
        Caught by live evidence 2026-05-09: the unmodified pyhanko
        path returns 0 CAs because Switzerland uses the
        ``https://uri.tsl-switzerland.ch/...`` namespace, not
        ETSI's ``http://uri.etsi.org/...``."""
        import base64

        builder = SwissTslTrustStoreBuilder(tslo_cert_path=_VENDORED_TSLO_CERT_PATH)

        # Synthetic ETSI TS 119 612 fragment with ONE Swiss QC service
        # and ONE non-QC service (must be filtered out). The XML uses
        # the official ETSI v2 namespace.
        swiss_der = b"\x30\x82der-from-swiss-qc-ca"
        non_qc_der = b"\x30\x82der-from-non-qc"
        swiss_b64 = base64.b64encode(swiss_der).decode("ascii")
        non_qc_b64 = base64.b64encode(non_qc_der).decode("ascii")
        synth_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <TrustServiceStatusList xmlns="http://uri.etsi.org/02231/v2#">
          <TSPService>
            <ServiceInformation>
              <ServiceTypeIdentifier>
                https://uri.tsl-switzerland.ch/TrstSvc/Svctype/CA/QC
              </ServiceTypeIdentifier>
              <ServiceDigitalIdentity>
                <DigitalId>
                  <X509Certificate>{swiss_b64}</X509Certificate>
                </DigitalId>
              </ServiceDigitalIdentity>
            </ServiceInformation>
          </TSPService>
          <TSPService>
            <ServiceInformation>
              <ServiceTypeIdentifier>
                https://uri.tsl-switzerland.ch/TrstSvc/Svctype/TSA/QTST
              </ServiceTypeIdentifier>
              <ServiceDigitalIdentity>
                <DigitalId>
                  <X509Certificate>{non_qc_b64}</X509Certificate>
                </DigitalId>
              </ServiceDigitalIdentity>
            </ServiceInformation>
          </TSPService>
        </TrustServiceStatusList>"""

        # Strip leading whitespace per line (lxml handles it but the
        # ServiceTypeIdentifier text gets its surrounding whitespace
        # preserved — strip in production via .text.strip()).
        synth_xml = "\n".join(line.lstrip() for line in synth_xml.splitlines())

        class _FakeResp:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def raise_for_status(self):
                pass

            async def text(self):
                return synth_xml

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, *args, **kwargs):
                return _FakeResp()

        with (
            patch("aiohttp.ClientSession", return_value=_FakeSession()),
            # Skip the XAdES verification — we're testing the
            # Swiss-namespace-aware walker, not the signature path.
            patch(
                "pyhanko.sign.validation.qualified.eutl_parse._validate_and_extract_tl_data_multiple_certs",
                return_value=None,
            ),
        ):
            bundle = asyncio.run(builder.build())

        # Exactly one cert (the QC one); the QTST got filtered out.
        assert bundle.metadata.cert_count == 1
        assert bundle.metadata.builder_id == "swiss_tsl"
        # The QC DER appears base64-encoded in the PEM bundle.
        assert swiss_b64[:30] in bundle.pem_bytes.decode("ascii", errors="replace")


# ───────────────── CompositeTrustStoreBuilder (ADR-053) ────────────────


class _FakeBuilder(TrustStoreBuilder):
    """Tiny test double for CompositeTrustStoreBuilder. Yields a
    pre-constructed bundle on build() success, or raises a typed
    unavailable on failure — mirrors the contract."""

    def __init__(
        self,
        *,
        builder_id: str,
        bundle_pem: bytes | None = None,
        fail: TrustStoreBuilderUnavailableError | None = None,
    ) -> None:
        self._id = builder_id
        self._bundle_pem = bundle_pem
        self._fail = fail

    @property
    def builder_id(self) -> str:
        return self._id

    async def build(self):  # type: ignore[no-untyped-def]
        if self._fail is not None:
            raise self._fail
        assert self._bundle_pem is not None
        return _bundle_from_pem(
            self._bundle_pem,
            builder_id=self._id,
            source_url=f"test://{self._id}",
            cert_count=_count_pem_certs(self._bundle_pem),
        )


class TestCompositeTrustStoreBuilder:
    def test_empty_inner_list_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="empty inner-builder list"):
            CompositeTrustStoreBuilder([])

    def test_builder_id_chains_inner_ids(self) -> None:
        composite = CompositeTrustStoreBuilder(
            [
                _FakeBuilder(builder_id="eu_lotl", bundle_pem=_FAKE_PEM_ONE),
                _FakeBuilder(builder_id="swiss_tsl", bundle_pem=_FAKE_PEM_TWO),
            ]
        )
        assert composite.builder_id == "eu_lotl+swiss_tsl"

    def test_build_concatenates_bundles_from_all_inner_builders(self) -> None:
        composite = CompositeTrustStoreBuilder(
            [
                _FakeBuilder(builder_id="eu_lotl", bundle_pem=_FAKE_PEM_ONE),
                _FakeBuilder(builder_id="swiss_tsl", bundle_pem=_FAKE_PEM_TWO),
            ]
        )
        bundle = asyncio.run(composite.build())
        # Both inner PEMs concatenated; one cert each.
        assert _FAKE_PEM_ONE in bundle.pem_bytes
        assert _FAKE_PEM_TWO in bundle.pem_bytes
        assert bundle.metadata.cert_count == 2
        assert bundle.metadata.builder_id == "eu_lotl+swiss_tsl"

    def test_build_continues_when_some_inner_builders_fail(self) -> None:
        """One inner builder failing should not poison the bundle —
        the others' results survive. Best-effort posture per ADR-053."""
        composite = CompositeTrustStoreBuilder(
            [
                _FakeBuilder(builder_id="eu_lotl", bundle_pem=_FAKE_PEM_ONE),
                _FakeBuilder(
                    builder_id="swiss_tsl",
                    fail=TrustStoreBuilderUnavailableError("OFCOM down"),
                ),
            ]
        )
        bundle = asyncio.run(composite.build())
        # Only the EU LOTL bundle survived.
        assert _FAKE_PEM_ONE in bundle.pem_bytes
        assert bundle.metadata.cert_count == 1
        # builder_id still reports BOTH so auditors can see the
        # composite shape (the per-inner failure lives in the log).
        assert bundle.metadata.builder_id == "eu_lotl+swiss_tsl"

    def test_build_raises_when_all_inner_builders_fail(self) -> None:
        """If EVERY inner builder fails the composite must not return
        an empty bundle — propagate the first failure so the admin
        endpoint surfaces the cause."""
        composite = CompositeTrustStoreBuilder(
            [
                _FakeBuilder(
                    builder_id="eu_lotl",
                    fail=TrustStoreBuilderUnavailableError("EU LOTL outage"),
                ),
                _FakeBuilder(
                    builder_id="swiss_tsl",
                    fail=TrustStoreBuilderUnavailableError("OFCOM outage"),
                ),
            ]
        )
        with pytest.raises(
            TrustStoreBuilderUnavailableError,
            match="every inner builder.*failed",
        ):
            asyncio.run(composite.build())


# ───────────────────────── Closed-set discipline ───────────────────────


class TestQcServiceTypesClosedSet:
    """Closed-set discipline on the qualified-signature service-type
    URIs filter (per ADR-052 §3). Adding a URI without an ADR
    amendment is a quiet documentation drift; this test pins the
    set so the drift surfaces in CI. Mirrors
    :class:`tests.test_memory_routes.TestExtractionWarningCodes`."""

    def test_qc_service_types_match_adr_052_closed_set(self) -> None:
        # ETSI TS 119 612 service type URI for qualified-signature CAs.
        # Adding a URI: bump ADR-052 §3 + add it here. CI fails the
        # diff if these drift.
        expected = {
            "http://uri.etsi.org/TrstSvc/Svctype/CA/QC",
        }
        assert _QC_SERVICE_TYPES == expected
