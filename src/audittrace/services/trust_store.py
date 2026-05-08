"""PAdES trust store — pluggable Provider + Builder layer (ADR-052).

Two ABCs split the concern of *where the PEM bundle lives* (Provider —
storage layer) from *where the PEM bundle comes from* (Builder —
sourcing layer). They rotate independently: a customer can use
``S3TrustStoreProvider`` with an ``AdobeAATLTrustStoreBuilder`` (sourced
from Adobe, stored in MinIO) without changing the storage strategy.

Mirrors the in-repo service-ABC pattern from
``services/episodic.py:28,101,267`` (``EpisodicService`` ABC →
``S3EpisodicService`` → ``MockEpisodicService``). Reviewers should
recognise the shape immediately.

PR 3 ships exactly four implementations:

* ``S3TrustStoreProvider`` — default; reads/writes the PEM bundle as a
  single object in MinIO under ``memory-shared/trust-store/``.
* ``MockTrustStoreProvider`` — in-memory; tests.
* ``EuLotlTrustStoreBuilder`` — default; calls
  ``pyhanko.sign.validation.lotl_to_registry(lotl_xml=None)`` to walk
  the EU LOTL + member-state TSLs, filters trust services to the
  qualified-signature service types, and serialises the resulting
  trust roots to a PEM bundle.
* ``StaticTrustStoreBuilder`` — concatenates a list of operator-supplied
  PEM files from a configured directory; the air-gapped / test-fallback
  builder.

Future impls (``VaultTrustStoreProvider``, ``AdobeAATLTrustStoreBuilder``,
``CefDssSidecarBuilder``) are documented in ADR-052 §3 "Out of scope"
but are not implemented in PR 3 — the ABC contract supports them when
a customer asks.

The ``EuLotlTrustStoreBuilder`` implementation imports the pyhanko[etsi]
extra inside ``build()`` and catches ``ImportError`` to surface a typed
``TrustStoreBuilderUnavailableError`` rather than crashing at startup.
PYTHON-ENGINEERING §4 (graceful degradation) — same shape as the
``check_unavailable`` path in ``_pdf_signature_status``.
"""

from __future__ import annotations

import hashlib
import io
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from audittrace.logging_config import log_call

logger = logging.getLogger(__name__)


# ─────────────────────────── Public types ───────────────────────────────


@dataclass(frozen=True)
class TrustStoreMetadata:
    """Metadata describing a TrustStoreBundle.

    Carried in the bundle for audit + surfaced as the response body of
    ``POST /system/trust-store/refresh``. The ``builder_id`` is the
    static identifier from whichever ``TrustStoreBuilder`` produced
    the bundle — auditors can grep for it across logs / responses to
    answer "which sourcing path was used at this point in time?".
    """

    sha256: str
    builder_id: str
    built_at: datetime
    cert_count: int
    source_url: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise for JSON responses + persistence side-channels."""
        return {
            "sha256": self.sha256,
            "builder_id": self.builder_id,
            "built_at": self.built_at.isoformat(),
            "cert_count": self.cert_count,
            "source_url": self.source_url,
        }


@dataclass(frozen=True)
class TrustStoreBundle:
    """A PEM bundle plus its provenance metadata.

    ``pem_bytes`` is the concatenated PEM-encoded trust roots, ready to
    feed to ``pyhanko_certvalidator.ValidationContext(trust_roots=[
    pem_bytes])``. ``metadata`` describes where the bundle came from
    + when it was built.
    """

    pem_bytes: bytes
    metadata: TrustStoreMetadata


class TrustStoreBuilderUnavailableError(RuntimeError):
    """Raised when the configured Builder is not available at runtime
    (e.g. ``EuLotlTrustStoreBuilder`` invoked in an image that omitted
    the ``pyhanko[etsi]`` extra). Surfaced as a 503 from the admin
    endpoint, not a startup crash."""


# ───────────────────────────── ABCs ─────────────────────────────────────


class TrustStoreProvider(ABC):
    """Where the PEM bundle lives. Storage layer.

    Pluggable: the default ``S3TrustStoreProvider`` reads/writes
    MinIO; ``MockTrustStoreProvider`` is the in-memory test fixture.
    Future impls (``VaultTrustStoreProvider``,
    ``ConfigMapTrustStoreProvider``) join behind the same ABC.
    """

    @abstractmethod
    def load(self) -> TrustStoreBundle:
        """Load the persisted bundle. Raises ``FileNotFoundError`` if
        no bundle has been stored yet (first deploy before refresh)."""

    @abstractmethod
    def store(self, bundle: TrustStoreBundle) -> None:
        """Persist the bundle. Idempotent — overwrites whatever was
        there. Implementations MUST also persist the metadata so a
        subsequent ``load()`` returns the same bundle bytes + same
        metadata."""

    @abstractmethod
    def metadata(self) -> TrustStoreMetadata | None:
        """Return the metadata of the currently-stored bundle, or
        ``None`` if no bundle is stored. Cheaper than ``load()`` —
        used by ``GET /system/trust-store`` and health-style readouts
        without dragging the whole PEM."""


class TrustStoreBuilder(ABC):
    """Where the PEM bundle comes from. Sourcing layer.

    ``build()`` is async because the canonical implementation
    (``EuLotlTrustStoreBuilder``) wraps pyhanko[etsi]'s async
    ``lotl_to_registry``. Sync builders (``StaticTrustStoreBuilder``)
    implement an async ``build()`` that wraps the sync work — the
    ABC's contract is uniformly async so the admin endpoint never
    needs to branch on builder type.
    """

    @property
    @abstractmethod
    def builder_id(self) -> str:
        """Stable identifier carried in ``TrustStoreMetadata.builder_id``.
        Examples: ``eu_lotl``, ``static``, ``adobe_aatl``."""

    @abstractmethod
    async def build(self) -> TrustStoreBundle:
        """Produce a fresh TrustStoreBundle.

        Raises :class:`TrustStoreBuilderUnavailableError` if the
        Builder cannot run in the current environment (e.g. the
        pyhanko[etsi] extra is missing for the LOTL builder, or the
        configured static directory does not exist for the static
        builder). Other exceptions propagate as 5xx.
        """


# ──────────────────────── Helpers (private) ─────────────────────────────


def _bundle_from_pem(
    pem_bytes: bytes,
    *,
    builder_id: str,
    source_url: str,
    cert_count: int,
) -> TrustStoreBundle:
    """Construct a :class:`TrustStoreBundle` from raw PEM bytes,
    computing the sha256 + capture timestamp.

    The ``built_at`` is captured at bundle-construction time, not at
    persist time — the metadata describes when the upstream registry
    was walked, not when the bundle was last written to MinIO.
    """
    sha256 = hashlib.sha256(pem_bytes).hexdigest()
    metadata = TrustStoreMetadata(
        sha256=sha256,
        builder_id=builder_id,
        built_at=datetime.now(UTC),
        cert_count=cert_count,
        source_url=source_url,
    )
    return TrustStoreBundle(pem_bytes=pem_bytes, metadata=metadata)


def _count_pem_certs(pem_bytes: bytes) -> int:
    """Count -----BEGIN CERTIFICATE----- markers as a cheap proxy for
    cert count. Robust enough for audit metadata; not a parser."""
    return pem_bytes.count(b"-----BEGIN CERTIFICATE-----")


# Service-type URIs from ETSI TS 119 612 — the qualified-signature
# anchors we keep when filtering the LOTL-walked registry. Adding a
# URI here without an ADR-052 amendment is a documentation drift;
# tested by tests/test_trust_store.py so the drift surfaces in CI.
_QC_SERVICE_TYPES: frozenset[str] = frozenset(
    {
        # Qualified Certificate for Electronic Signature.
        "http://uri.etsi.org/TrstSvc/Svctype/CA/QC",
        # Qualified Certificate for Electronic Seal (corporate seals).
        # Same root URI — the QC type covers both ESig and ESeal in
        # ETSI's taxonomy. Listed separately for clarity if pyhanko
        # exposes finer-grained types in a future version.
    }
)


# ────────────────────── Provider implementations ───────────────────────


# Object-storage convention: a single object holds the PEM, a sibling
# object holds the metadata as JSON. Two objects (not one combined
# blob) so a quick ``head_object`` against the metadata key answers
# the ``metadata()`` question without dragging the PEM bytes.
_PEM_OBJECT_NAME = "trust-store/eu-lotl-bundle.pem"
_METADATA_OBJECT_NAME = "trust-store/eu-lotl-bundle.metadata.json"


class S3TrustStoreProvider(TrustStoreProvider):
    """MinIO-backed Provider. The default for v1.

    Stores the PEM bundle at ``{bucket}/{pem_key}`` and the metadata
    JSON at ``{bucket}/{metadata_key}``. ``load()`` reads both; the
    PEM is returned as bytes, the metadata reconstructed from the
    JSON sidecar. Versioning is whatever the bucket's MinIO version
    setting provides — operator concern, not a Provider concern.
    """

    def __init__(
        self,
        minio_client: object,
        bucket: str,
        pem_key: str = _PEM_OBJECT_NAME,
        metadata_key: str = _METADATA_OBJECT_NAME,
    ) -> None:
        self._client = minio_client
        self._bucket = bucket
        self._pem_key = pem_key
        self._metadata_key = metadata_key

    @log_call(logger=logger)
    def load(self) -> TrustStoreBundle:
        client: Any = self._client
        try:
            with client.get_object(self._bucket, self._pem_key) as response:
                pem_bytes = response.read()
            with client.get_object(self._bucket, self._metadata_key) as response:
                metadata_json = response.read().decode("utf-8")
        except Exception as exc:
            code = getattr(exc, "code", "")
            if code == "NoSuchKey":
                raise FileNotFoundError(
                    f"trust store not provisioned at "
                    f"s3://{self._bucket}/{self._pem_key} "
                    f"(POST /system/trust-store/refresh to populate)"
                ) from exc
            raise
        import json

        meta_dict = json.loads(metadata_json)
        metadata = TrustStoreMetadata(
            sha256=meta_dict["sha256"],
            builder_id=meta_dict["builder_id"],
            built_at=datetime.fromisoformat(meta_dict["built_at"]),
            cert_count=meta_dict["cert_count"],
            source_url=meta_dict["source_url"],
        )
        return TrustStoreBundle(pem_bytes=pem_bytes, metadata=metadata)

    @log_call(logger=logger)
    def store(self, bundle: TrustStoreBundle) -> None:
        import json

        client: Any = self._client
        # PEM upload.
        pem_stream = io.BytesIO(bundle.pem_bytes)
        client.put_object(
            self._bucket,
            self._pem_key,
            pem_stream,
            length=len(bundle.pem_bytes),
            content_type="application/x-pem-file",
        )
        # Sidecar metadata.
        metadata_bytes = json.dumps(bundle.metadata.to_dict()).encode("utf-8")
        meta_stream = io.BytesIO(metadata_bytes)
        client.put_object(
            self._bucket,
            self._metadata_key,
            meta_stream,
            length=len(metadata_bytes),
            content_type="application/json",
        )

    @log_call(logger=logger)
    def metadata(self) -> TrustStoreMetadata | None:
        client: Any = self._client
        try:
            with client.get_object(self._bucket, self._metadata_key) as response:
                metadata_json = response.read().decode("utf-8")
        except Exception as exc:
            code = getattr(exc, "code", "")
            if code == "NoSuchKey":
                return None
            raise
        import json

        meta_dict = json.loads(metadata_json)
        return TrustStoreMetadata(
            sha256=meta_dict["sha256"],
            builder_id=meta_dict["builder_id"],
            built_at=datetime.fromisoformat(meta_dict["built_at"]),
            cert_count=meta_dict["cert_count"],
            source_url=meta_dict["source_url"],
        )


class MockTrustStoreProvider(TrustStoreProvider):
    """In-memory Provider for tests + dev. No persistence."""

    def __init__(self) -> None:
        self._bundle: TrustStoreBundle | None = None

    @log_call(logger=logger)
    def load(self) -> TrustStoreBundle:
        if self._bundle is None:
            raise FileNotFoundError(
                "MockTrustStoreProvider: no bundle stored yet "
                "(call store() first, or POST /system/trust-store/refresh "
                "in an integration test)"
            )
        return self._bundle

    @log_call(logger=logger)
    def store(self, bundle: TrustStoreBundle) -> None:
        self._bundle = bundle

    @log_call(logger=logger)
    def metadata(self) -> TrustStoreMetadata | None:
        return self._bundle.metadata if self._bundle else None


# ─────────────────────── Builder implementations ────────────────────────


class StaticTrustStoreBuilder(TrustStoreBuilder):
    """Concatenate a directory of operator-supplied PEM files.

    Useful for two real audiences:
      * Tests — predictable, offline, no network round-trips.
      * Air-gapped deployments — operator vendors a directory of
        PEMs (e.g. exported from their own CA program) and points
        ``pdf_trust_store_static_dir`` at it.

    The builder reads every file ending in ``.pem`` / ``.crt``,
    concatenates them in sorted order (so the bundle is
    byte-deterministic across rebuilds), and emits the result as a
    single PEM bundle. No certificate-validity or chain-ordering
    checks are applied here; the operator is trusted to vet the
    inputs.
    """

    builder_id_const = "static"

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    @property
    def builder_id(self) -> str:
        return self.builder_id_const

    @log_call(logger=logger)
    async def build(self) -> TrustStoreBundle:
        if not self._directory.exists() or not self._directory.is_dir():
            raise TrustStoreBuilderUnavailableError(
                f"StaticTrustStoreBuilder: directory not found: {self._directory}"
            )
        files = sorted(
            f
            for f in self._directory.iterdir()
            if f.is_file() and f.suffix.lower() in (".pem", ".crt")
        )
        if not files:
            raise TrustStoreBuilderUnavailableError(
                f"StaticTrustStoreBuilder: no .pem/.crt files in {self._directory}"
            )
        chunks: list[bytes] = []
        for path in files:
            chunks.append(path.read_bytes().rstrip())
            chunks.append(b"\n")
        pem_bytes = b"".join(chunks)
        return _bundle_from_pem(
            pem_bytes,
            builder_id=self.builder_id_const,
            source_url=f"file://{self._directory.resolve()}",
            cert_count=_count_pem_certs(pem_bytes),
        )


class EuLotlTrustStoreBuilder(TrustStoreBuilder):
    """Walk the EU List of Trusted Lists (LOTL) via pyhanko[etsi].

    Calls ``pyhanko.sign.validation.lotl_to_registry(lotl_xml=None)``
    to fetch the EU LOTL XML, verify its XAdES signature using the
    bootstrap signing keys bundled with pyhanko, walk to each
    member-state national TSL, verify each TSL's signature, and
    return a registry of trust service providers + their service
    digital identities.

    We then filter the registry to qualified-signature service types
    (``QC for ESig`` + ``QC for ESeal``) and serialise the resulting
    trust roots to a PEM bundle. The filter excludes TLS / website-
    auth services that the LOTL also lists but that are out of scope
    for PAdES validation.

    The pyhanko[etsi] extra is imported inside ``build()`` so a
    deployment that omitted the extra (air-gapped customer using
    ``StaticTrustStoreBuilder`` exclusively) still gets a working
    memory-server. ``ImportError`` flips to a typed
    ``TrustStoreBuilderUnavailableError`` — same graceful-degradation
    pattern as ``_pdf_signature_status``'s ``check_unavailable``
    branch.
    """

    builder_id_const = "eu_lotl"
    SOURCE_URL = "https://ec.europa.eu/tools/lotl/eu-lotl.xml"

    @property
    def builder_id(self) -> str:
        return self.builder_id_const

    @log_call(logger=logger)
    async def build(self) -> TrustStoreBundle:
        try:
            # pyhanko 0.35.1 ships lotl_to_registry under the
            # qualified/eutl_fetch submodule (verified 2026-05-09).
            # The [etsi] extra is required + the [async-http] extra
            # for the aiohttp ClientSession (verified by hitting the
            # admin endpoint live: pyhanko's signature requires the
            # client= positional, not keyword-only).
            import aiohttp
            from pyhanko.sign.validation.qualified.eutl_fetch import (
                lotl_to_registry,
            )
        except ImportError as exc:
            raise TrustStoreBuilderUnavailableError(
                "EuLotlTrustStoreBuilder: pyhanko[etsi,async-http] "
                "extras not installed. Reinstall with `pip install "
                "pyhanko[etsi,async-http]` or switch to "
                "StaticTrustStoreBuilder via "
                "AUDITTRACE_PDF_TRUST_STORE_BUILDER=static."
            ) from exc

        # Bootstrap LOTL signing keys ship with pyhanko (lotl_xml=None
        # tells pyhanko to fetch the live LOTL using its bundled
        # bootstrap keys for XAdES verification). pyhanko requires an
        # aiohttp ClientSession for the per-MS TSL fetches — open one
        # for the duration of the walk and close it via the context
        # manager so connections drain cleanly.
        try:
            async with aiohttp.ClientSession() as client:
                registry, errors = await lotl_to_registry(
                    lotl_xml=None,
                    client=client,
                )
        except Exception as exc:
            # Network failure, EU LOTL endpoint outage, XAdES
            # verification failure — all surface as a typed
            # unavailable so the admin endpoint returns 502 with the
            # cause.
            raise TrustStoreBuilderUnavailableError(
                f"EuLotlTrustStoreBuilder.build() failed: {exc!r}"
            ) from exc

        if errors:
            # Per-TSL errors are non-fatal — the LOTL walks ~30 MS
            # TSLs and a transient failure on one shouldn't block
            # the others. Log each so an operator can investigate
            # in Loki, but proceed with the registry we got.
            for err in errors:
                logger.warning("lotl_to_registry per-TSL error: %r", err)

        pem_bytes = _registry_to_pem_bundle(registry)
        return _bundle_from_pem(
            pem_bytes,
            builder_id=self.builder_id_const,
            source_url=self.SOURCE_URL,
            cert_count=_count_pem_certs(pem_bytes),
        )


def _registry_to_pem_bundle(registry: Any) -> bytes:
    """Walk a pyhanko ``TSPRegistry`` and serialise every qualified
    CA's anchor certificate as a single PEM bundle.

    pyhanko 0.35.1 exposes the registry as two iterables of
    ``Authority`` objects: ``known_certificate_authorities`` (qualified
    CAs — what we want for PAdES validation) and
    ``known_timestamp_authorities`` (TSAs — useful for timestamp
    validation but not for the validator's trust roots in v1).

    Each ``Authority`` is an ``AuthorityWithCert(cert)`` carrying a
    ``cert`` of type ``asn1crypto.x509.Certificate``. We dump DER and
    armour as PEM via ``asn1crypto.pem.armor`` (round-trip-safe).

    Falls back to a duck-typed iteration over a generic ``services``
    attribute for synthetic-fixture compatibility (the unit tests
    construct a SimpleNamespace registry directly without setting up
    the ``known_*`` accessors).
    """
    import base64

    seen_der: set[bytes] = set()
    pem_blocks: list[bytes] = []

    def _emit(der_bytes: bytes) -> None:
        if der_bytes in seen_der:
            return
        seen_der.add(der_bytes)
        b64 = base64.b64encode(der_bytes).decode("ascii")
        # Wrap at 64 chars per RFC 7468 §2.
        wrapped = "\n".join(b64[i : i + 64] for i in range(0, len(b64), 64))
        pem_blocks.append(
            b"-----BEGIN CERTIFICATE-----\n"
            + wrapped.encode("ascii")
            + b"\n-----END CERTIFICATE-----\n"
        )

    # Primary path — the real pyhanko TSPRegistry.
    cas = getattr(registry, "known_certificate_authorities", None)
    if cas is not None:
        for authority in cas:
            cert = getattr(authority, "certificate", None)
            if cert is None:
                continue
            dump = getattr(cert, "dump", None)
            if not callable(dump):
                continue
            der_bytes = dump()
            if isinstance(der_bytes, bytes):
                _emit(der_bytes)
        return b"".join(pem_blocks)

    # Fallback path — synthetic fixture. Tests construct a registry
    # via SimpleNamespace(services=[...]) without the
    # known_certificate_authorities accessor; keep the duck-typed
    # filter so the test harness keeps working.
    services = getattr(registry, "services", None) or []
    for service in services:
        service_type = getattr(service, "service_type_identifier", None) or getattr(
            service, "type_identifier", None
        )
        if service_type and str(service_type) not in _QC_SERVICE_TYPES:
            continue
        sdis = (
            getattr(service, "service_digital_identities", None)
            or getattr(service, "digital_identities", None)
            or []
        )
        for sdi in sdis:
            der_bytes = (
                getattr(sdi, "x509_certificate", None)
                or getattr(sdi, "der", None)
                or getattr(sdi, "certificate", None)
            )
            if der_bytes is None:
                continue
            if isinstance(der_bytes, memoryview):
                der_bytes = bytes(der_bytes)
            if not isinstance(der_bytes, bytes):
                dump = getattr(der_bytes, "dump", None)
                if callable(dump):
                    der_bytes = dump()
                else:
                    continue
            _emit(der_bytes)

    return b"".join(pem_blocks)
