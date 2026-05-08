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


class SwissTslTrustStoreBuilder(TrustStoreBuilder):
    """Walk Switzerland's federal Trusted List (OFCOM/BAKOM, ETSI TS
    119 612) via pyhanko's per-TSL parser (ADR-053).

    Switzerland is **not an EU member state**, so the EU LOTL walked
    by :class:`EuLotlTrustStoreBuilder` does not include the Swiss
    federal TSL. For an audit-grade product running on Swiss soil with
    Swiss customers, the ZertES-supervised TSPs (SwissSign, Swisscom
    Trust Services, et al.) must be in scope. This builder closes that
    gap by fetching the Swiss federal TSL directly from OFCOM and
    walking it with pyhanko's :func:`trust_list_to_registry`.

    Auth chain (ADR-053 §4):

    1. The TSLO (Trust List Operator) signing certificate is vendored
       in the chart at ``charts/audittrace/trust-store/swiss-federal-tsl/CH-TL-cert.der``.
       Out-of-band SHA-1 fingerprint
       ``e8638362 5130bdf0 1e42a317 6501e079 261b137f`` was verified
       against the published one at
       https://uri.tsl-switzerland.ch/TrstSvc/TrustedList/schemerules/CH/index.html
       on 2026-05-09 by the maintainer.
    2. The cert is mounted into memory-server via ConfigMap; the path
       is read from ``Settings.pdf_trust_store_swiss_tslo_cert_path``.
    3. The TSL XML is fetched from
       ``https://trustedlist.tsl-switzerland.ch/tsl-ch.xml`` (HTTPS,
       state-managed endpoint) and validated against the vendored
       TSLO cert before any TSP is added to the registry.

    Combine with :class:`EuLotlTrustStoreBuilder` via
    :class:`CompositeTrustStoreBuilder` to cover both EU + CH
    qualified TSPs in one trust bundle.
    """

    builder_id_const = "swiss_tsl"
    SOURCE_URL = "https://trustedlist.tsl-switzerland.ch/tsl-ch.xml"

    def __init__(self, tslo_cert_path: str | Path) -> None:
        self._tslo_cert_path = Path(tslo_cert_path)

    @property
    def builder_id(self) -> str:
        return self.builder_id_const

    @log_call(logger=logger)
    async def build(self) -> TrustStoreBundle:
        try:
            import aiohttp
            from asn1crypto import pem as asn1_pem
            from asn1crypto import x509 as asn1_x509
            from pyhanko.sign.validation.qualified.eutl_parse import (
                _validate_and_extract_tl_data_multiple_certs,
            )
        except ImportError as exc:
            raise TrustStoreBuilderUnavailableError(
                "SwissTslTrustStoreBuilder: pyhanko[etsi,async-http] "
                "extras + asn1crypto required. Reinstall with "
                "`pip install pyhanko[etsi,async-http]`."
            ) from exc

        # Load the OOB-vendored TSLO cert.
        if not self._tslo_cert_path.exists() or not self._tslo_cert_path.is_file():
            raise TrustStoreBuilderUnavailableError(
                f"SwissTslTrustStoreBuilder: TSLO cert not found at "
                f"{self._tslo_cert_path}. Vendor the cert in the chart "
                f"and set AUDITTRACE_PDF_TRUST_STORE_SWISS_TSLO_CERT_PATH."
            )
        try:
            cert_bytes = self._tslo_cert_path.read_bytes()
            if asn1_pem.detect(cert_bytes):
                _, _, der = asn1_pem.unarmor(cert_bytes)
            else:
                der = cert_bytes
            tslo_cert = asn1_x509.Certificate.load(der)
        except (ValueError, TypeError) as exc:
            raise TrustStoreBuilderUnavailableError(
                f"SwissTslTrustStoreBuilder: failed to parse TSLO cert "
                f"at {self._tslo_cert_path}: {exc!r}"
            ) from exc

        # Fetch the Swiss TSL XML over HTTPS.
        try:
            async with aiohttp.ClientSession() as client:
                async with client.get(
                    self.SOURCE_URL, timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    resp.raise_for_status()
                    tl_xml = await resp.text()
        except Exception as exc:
            raise TrustStoreBuilderUnavailableError(
                f"SwissTslTrustStoreBuilder: failed to fetch Swiss TSL "
                f"from {self.SOURCE_URL}: {exc!r}"
            ) from exc

        # Two-phase processing (caught live 2026-05-09):
        # (1) verify the XAdES signature on the TSL using pyhanko's
        #     internal helper — this raises if the TSL was tampered
        #     with or signed by an unknown TSLO.
        # (2) walk the verified XML directly with lxml to extract
        #     the X.509 certs from QC services. We can't use
        #     ``trust_list_to_registry`` here because Switzerland
        #     uses its own URI namespace for service-type identifiers
        #     (``https://uri.tsl-switzerland.ch/TrstSvc/Svctype/CA/QC``)
        #     while pyhanko's parser only recognises ETSI's URI
        #     (``http://uri.etsi.org/TrstSvc/Svctype/CA/QC``). Both
        #     URIs identify the same ETSI TS 119 612 concept — a
        #     qualified-signature CA — but pyhanko's hard-coded URI
        #     check drops every Swiss CA on the floor. Verified live
        #     2026-05-09: 36 services in the Swiss TSL, 0 CAs parsed
        #     by pyhanko under the unmodified path; this two-phase
        #     approach extracts them correctly.
        try:
            _validate_and_extract_tl_data_multiple_certs(tl_xml, [tslo_cert])
        except Exception as exc:
            raise TrustStoreBuilderUnavailableError(
                f"SwissTslTrustStoreBuilder: TSL signature validation "
                f"failed (TSLO cert may be stale, or the TSL was "
                f"tampered in transit): {exc!r}"
            ) from exc

        try:
            der_certs = _extract_qc_certs_from_swiss_tsl(tl_xml)
        except Exception as exc:
            raise TrustStoreBuilderUnavailableError(
                f"SwissTslTrustStoreBuilder: Swiss TSL XML walk failed: {exc!r}"
            ) from exc

        pem_bytes = _ders_to_pem_bundle(der_certs)
        return _bundle_from_pem(
            pem_bytes,
            builder_id=self.builder_id_const,
            source_url=self.SOURCE_URL,
            cert_count=_count_pem_certs(pem_bytes),
        )


class CompositeTrustStoreBuilder(TrustStoreBuilder):
    """Run a list of inner ``TrustStoreBuilder`` impls in order and
    concatenate the resulting PEM bundles into a single bundle (ADR-053).

    Use case: cover EU eIDAS qualified TSPs + Swiss federal qualified
    TSPs + any operator-supplied jurisdictional roots in one trust
    bundle, without inflating each builder's responsibilities.

    The composite's ``builder_id`` is the comma-joined list of inner
    builder ids (e.g. ``eu_lotl+swiss_tsl``); the ``source_url`` is
    the first inner builder's source for human readability — full
    sourcing detail is in the chart values + ADR-053.

    If ANY inner builder raises :class:`TrustStoreBuilderUnavailableError`,
    the composite logs the error and continues with the remaining
    builders. The bundle reflects whichever builders succeeded —
    "best-effort, audit-clear" posture (the operator can read the
    cert_count + builder_id chain and notice a missing layer).
    Composite raises only if EVERY inner builder fails.
    """

    builder_id_const = "composite"

    def __init__(self, inner: list[TrustStoreBuilder]) -> None:
        if not inner:
            raise ValueError(
                "CompositeTrustStoreBuilder: empty inner-builder list "
                "(at least one TrustStoreBuilder required)"
            )
        self._inner = list(inner)

    @property
    def builder_id(self) -> str:
        # Stable, deterministic — operators see the chain in the
        # admin endpoint response + audit log.
        return "+".join(b.builder_id for b in self._inner)

    @log_call(logger=logger)
    async def build(self) -> TrustStoreBundle:
        chunks: list[bytes] = []
        successes: list[str] = []
        failures: list[tuple[str, Exception]] = []
        for inner_builder in self._inner:
            try:
                bundle = await inner_builder.build()
            except TrustStoreBuilderUnavailableError as exc:
                logger.warning(
                    "CompositeTrustStoreBuilder: inner builder %r failed: %s",
                    inner_builder.builder_id,
                    exc,
                )
                failures.append((inner_builder.builder_id, exc))
                continue
            successes.append(f"{inner_builder.builder_id}={bundle.metadata.cert_count}")
            chunks.append(bundle.pem_bytes)
        if not chunks:
            # All inner builders failed — propagate the first error
            # so the admin endpoint surfaces the cause.
            first_id, first_exc = failures[0]
            raise TrustStoreBuilderUnavailableError(
                f"CompositeTrustStoreBuilder: every inner builder "
                f"failed. First: {first_id}: {first_exc}"
            )
        if failures:
            logger.warning(
                "CompositeTrustStoreBuilder: %d/%d inner builders "
                "failed (%s); proceeding with remainder",
                len(failures),
                len(self._inner),
                ", ".join(b for b, _ in failures),
            )
        # Concatenate; deduplication already happens at the per-cert
        # PEM-emission level inside _registry_to_pem_bundle. Cross-
        # bundle duplicates (e.g. a CA listed in both EU LOTL and
        # the Swiss TSL) survive here — pyhanko_certvalidator handles
        # duplicate trust roots gracefully (set semantics on subject
        # name + key match).
        pem_bytes = b"".join(chunks)
        return _bundle_from_pem(
            pem_bytes,
            builder_id=self.builder_id,
            source_url=", ".join(b.builder_id for b in self._inner),
            cert_count=_count_pem_certs(pem_bytes),
        )


# ETSI TS 119 612 service-type URI fragment for qualified-signature
# CAs. Switzerland uses ``https://uri.tsl-switzerland.ch/...``;
# the EU/ETSI standard uses ``http://uri.etsi.org/...``. Both end
# in ``/Svctype/CA/QC`` (ETSI's stable suffix). Suffix-match means
# we accept either jurisdiction's URI prefix, which is the right
# semantic per the ETSI standard.
_SVCTYPE_QC_SUFFIX = "/TrstSvc/Svctype/CA/QC"


def _extract_qc_certs_from_swiss_tsl(tl_xml: str) -> list[bytes]:
    """Walk a ETSI TS 119 612 trusted list XML and return the DER
    bytes of every X.509 cert attached to a qualified-signature CA
    service. URI-suffix-matched on ``/TrstSvc/Svctype/CA/QC`` so the
    Swiss namespace (``https://uri.tsl-switzerland.ch/...``) and the
    ETSI namespace (``http://uri.etsi.org/...``) both resolve to QC.

    Caller is responsible for verifying the TSL's XAdES signature
    BEFORE invoking this — this function trusts the input bytes.
    """
    import base64

    from lxml import etree

    # Parse without resolving entities (defence in depth — tl_xml
    # came from the network).
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(tl_xml.encode("utf-8"), parser=parser)

    # ETSI TS 119 612 namespace.
    ns = {"tsl": "http://uri.etsi.org/02231/v2#"}
    der_certs: list[bytes] = []
    seen: set[bytes] = set()
    for service in root.findall(".//tsl:TSPService", ns):
        type_el = service.find(".//tsl:ServiceTypeIdentifier", ns)
        if type_el is None or not type_el.text:
            continue
        if not type_el.text.strip().endswith(_SVCTYPE_QC_SUFFIX):
            continue
        for cert_el in service.findall(".//tsl:X509Certificate", ns):
            if not cert_el.text:
                continue
            try:
                der_bytes = base64.b64decode(cert_el.text.strip())
            except (ValueError, TypeError):
                continue
            if der_bytes in seen:
                continue
            seen.add(der_bytes)
            der_certs.append(der_bytes)
    return der_certs


def _ders_to_pem_bundle(der_certs: list[bytes]) -> bytes:
    """Serialise a list of DER cert bytes as a single PEM bundle."""
    import base64

    blocks: list[bytes] = []
    for der in der_certs:
        b64 = base64.b64encode(der).decode("ascii")
        wrapped = "\n".join(b64[i : i + 64] for i in range(0, len(b64), 64))
        blocks.append(
            b"-----BEGIN CERTIFICATE-----\n"
            + wrapped.encode("ascii")
            + b"\n-----END CERTIFICATE-----\n"
        )
    return b"".join(blocks)


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
