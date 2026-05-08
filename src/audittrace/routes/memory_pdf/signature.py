"""PAdES signature validation + closed-set 9-class taxonomy.

The heaviest pure-PDF concern in this sub-package. Encapsulates:

- ``_SIGNATURE_STATUS_CODES`` — closed-set vocabulary, pinned by tests.
- Process-resident cache of the pyhanko ``ValidationContext`` keyed by
  ``(trust_store_path, provider_metadata_sha256)``. The two-key cache
  invalidation supports both the legacy operator-mounted PEM file path
  AND the ADR-052 dynamic Provider refresh — either source changing
  rebuilds the context without a pod restart.
- Cached parsed ``asn1crypto.x509.Certificate`` list re-used by the
  ADR-054 as-of-signing-time retry path.
- ``_pdf_signature_status`` — the function ChromaDB chunk metadata and
  Postgres manifest rows reflect on every PDF call.

Module-level shared state (``_VALIDATION_CONTEXT``, ``_VC_LOCK``,
``_VC_TRUST_STORE_PATH``, ``_VC_TRUST_ROOTS``) is the singleton
mechanism for the validation context; see PYTHON-ENGINEERING §2 for
the singleton rationale (loading pyhanko + walking the trust store
costs ~50ms; doing it per PDF would dominate index latency).
"""

from __future__ import annotations

import io
import logging
import re
import threading
from typing import Any

logger = logging.getLogger(__name__)


_SIGNATURE_STATUS_CODES: frozenset[str] = frozenset(
    {
        "check_skipped",
        "check_unavailable",
        "check_failed",
        "none",
        "signed_valid",
        "signed_invalid",
        "signed_untrusted",
        "signed_expired",
        "signed_tampered",
    }
)


_VALIDATION_CONTEXT: Any = None
_VC_LOCK = threading.Lock()
_VC_TRUST_STORE_PATH: str = ""  # tracks which trust store the cached VC was built with
# Cached parsed cert list — re-used by the as-of-signing-time retry
# path in ``_pdf_signature_status`` (ADR-054 §3) so we don't re-parse
# the PEM bundle for the second ValidationContext.
_VC_TRUST_ROOTS: list[Any] = []


_PEM_CERT_RE = re.compile(
    rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    re.DOTALL,
)


def _pem_bundle_to_cert_list(pem_bytes: bytes) -> list[Any]:
    """Parse a multi-cert PEM bundle into a list of
    ``asn1crypto.x509.Certificate`` objects suitable for
    ``pyhanko_certvalidator.ValidationContext(trust_roots=...)``.

    pyhanko_certvalidator's ``TrustRootList`` is type-aliased as
    ``Iterable[Union[x509.Certificate, TrustAnchor]]`` — the
    docstring at ``context.py:96`` claims "byte strings containing
    DER or PEM-encoded X.509 certificates" are also accepted, but
    that's misleading: passing raw PEM bytes raises
    ``'bytes' object has no attribute 'subject'`` deep inside the
    trust-root walk. Caught by live evidence 2026-05-09 on
    main_signed.pdf re-index. Returning parsed Certificate
    objects matches the actual contract.

    Returns an empty list if no cert markers are found — callers
    should treat that as "no trust roots configured" and fall back
    to system roots.
    """
    from asn1crypto import pem as asn1_pem
    from asn1crypto import x509 as asn1_x509

    blocks = _PEM_CERT_RE.findall(pem_bytes)
    certs: list[Any] = []
    for block in blocks:
        try:
            if asn1_pem.detect(block):
                _, _, der_bytes = asn1_pem.unarmor(block)
            else:
                der_bytes = block
            certs.append(asn1_x509.Certificate.load(der_bytes))
        except (ValueError, TypeError) as exc:
            # Skip malformed entries — log + continue. A single bad
            # cert in the bundle should not poison the whole trust
            # context. Same posture as pyhanko's own per-TSL error
            # handling in EuLotlTrustStoreBuilder.build().
            logger.warning("skipping malformed cert in trust bundle: %s", exc)
    return certs


def _get_validation_context(trust_store_path: str) -> Any:
    """Return a process-cached pyhanko ValidationContext.

    First call builds it from the configured trust source; subsequent
    calls return the cached instance. The cache key is
    ``(trust_store_path, provider_metadata_sha256)`` — either the
    operator-set file path changing OR the Provider's stored bundle
    being refreshed invalidates the cache. Same lazy double-checked
    locking pattern as the existing singleton.

    Trust source resolution (ADR-052 §2):

    * If ``trust_store_path`` is non-empty, read the PEM bundle from
      that filesystem path. This is the pre-ADR-052 backwards-compat
      path: an operator who mounts a PEM via Vault Agent at
      ``/etc/audittrace/trust-store.pem`` continues to work unchanged.
    * Otherwise, attempt ``provider.load()`` to fetch the bundle from
      the configured Provider (default ``S3TrustStoreProvider`` →
      MinIO). ``FileNotFoundError`` falls through to certifi + OS
      trust roots — the honest "no trust store provisioned yet"
      state. The next ``POST /system/trust-store/refresh`` populates
      the Provider; the cache invalidates on metadata sha256 change.
    * If neither source is configured, certifi + OS trust roots only.
    """
    global _VALIDATION_CONTEXT, _VC_TRUST_STORE_PATH, _VC_TRUST_ROOTS
    # Resolve the Provider-side cache key BEFORE the fast-path check
    # so a refresh (which changes the metadata sha256) invalidates
    # the in-process cache without a pod restart.
    provider_sha: str = ""
    if not trust_store_path:
        try:
            from audittrace.dependencies import get_trust_store_provider

            provider = get_trust_store_provider()
            metadata = provider.metadata()
            if metadata is not None:
                provider_sha = metadata.sha256
        except (KeyError, RuntimeError, ImportError):
            # DI container not initialised (test paths) or import
            # cycle — no Provider available, fall through to system
            # trust roots.
            pass
    cache_key = f"{trust_store_path}|{provider_sha}"

    # Fast path — lock-free read.
    if _VALIDATION_CONTEXT is not None and _VC_TRUST_STORE_PATH == cache_key:
        return _VALIDATION_CONTEXT
    # Slow path — race-safe init.
    with _VC_LOCK:
        if _VALIDATION_CONTEXT is not None and _VC_TRUST_STORE_PATH == cache_key:
            return _VALIDATION_CONTEXT
        from pyhanko_certvalidator import ValidationContext

        kwargs: dict[str, Any] = {}
        if trust_store_path:
            # Operator-set explicit PEM file path takes precedence —
            # pre-ADR-052 backwards-compat path.
            try:
                with open(trust_store_path, "rb") as fh:
                    pem_bytes = fh.read()
                kwargs["trust_roots"] = _pem_bundle_to_cert_list(pem_bytes)
            except OSError as exc:
                logger.warning(
                    "Could not read pdf_signature_trust_store=%r: %s; "
                    "falling back to system trust roots",
                    trust_store_path,
                    exc,
                )
        elif provider_sha:
            # Provider has a bundle stored — load it. We already know
            # metadata exists (provider_sha was set above) so load()
            # succeeds; the try/except guards against a race where the
            # bundle is deleted between metadata() and load().
            try:
                from audittrace.dependencies import get_trust_store_provider

                provider = get_trust_store_provider()
                bundle = provider.load()
                kwargs["trust_roots"] = _pem_bundle_to_cert_list(bundle.pem_bytes)
            except (FileNotFoundError, KeyError, RuntimeError, ImportError) as exc:
                logger.warning(
                    "TrustStoreProvider.load() failed: %s; "
                    "falling back to system trust roots",
                    exc,
                )
        # Defaults: certifi + system trust + pyhanko's built-in
        # algorithm_usage_policy (rejects MD5, SHA-1, RSA<2048 with
        # warnings or hard-fail per pyhanko's current defaults).
        # IAM §"Algorithm Security Rules" — never accept weak algs.
        _VALIDATION_CONTEXT = ValidationContext(**kwargs)
        _VC_TRUST_STORE_PATH = cache_key
        # ADR-054 §3 — cache the parsed trust-roots list so the
        # as-of-signing-time retry path in _pdf_signature_status can
        # build a second ValidationContext(moment=signing_time) with
        # the SAME roots, no re-parsing.
        _VC_TRUST_ROOTS = list(kwargs.get("trust_roots") or [])
        return _VALIDATION_CONTEXT


def _invalidate_validation_context() -> None:
    """Drop the cached ValidationContext singleton.

    Called by ``POST /system/trust-store/refresh`` after the new
    bundle is persisted, so the next signature check rebuilds
    against the freshly-stored PEM. The existing cache-key check
    (Provider metadata sha256) would catch the refresh on its own,
    but explicit invalidation is cheaper than letting the next
    request walk the full slow path.
    """
    global _VALIDATION_CONTEXT, _VC_TRUST_STORE_PATH, _VC_TRUST_ROOTS
    with _VC_LOCK:
        _VALIDATION_CONTEXT = None
        _VC_TRUST_STORE_PATH = ""
        _VC_TRUST_ROOTS = []


def _pdf_signature_status(
    raw: bytes,
    *,
    enabled: bool,
    trust_store_path: str,
) -> tuple[str, int]:
    """Return ``(status, signers_count)`` for *raw* PDF bytes.

    Status taxonomy — 9 closed-set values pinned by
    ``_SIGNATURE_STATUS_CODES`` (per ADR-052 §1 + ADR-054 §1).
    Every chunk's metadata carries one via ``signature_status``:

    * ``"check_skipped"`` — operator disabled the check via
      ``AUDITTRACE_PDF_SIGNATURE_CHECK_ENABLED=false``.
    * ``"check_unavailable"`` — pyhanko not importable (graceful
      degradation per PYTHON-ENGINEERING §4).
    * ``"check_failed"`` — pyhanko raised on this file (malformed
      PDF, network failure during OCSP, unexpected exception).
      Distinct from ``"signed_invalid"`` so auditors can separate
      "we tried and the document broke" from "the document said it
      was signed and the signature was bad."
    * ``"none"`` — file parsed successfully and contains zero
      embedded signatures.
    * ``"signed_valid"`` — every signature is intact, valid, AND
      trusted by the configured trust store **as of now**.
    * ``"signed_invalid"`` — at least one signature exists with
      ``valid=False`` (signature math broken — wrong key, corrupted
      bytes, weak-algorithm policy reject). Real audit signal: the
      claim itself is unverifiable.
    * ``"signed_untrusted"`` — at least one signature is intact and
      ``valid=True`` but its chain does not terminate at our
      configured trust roots, even when re-validated at the
      self-reported signing time. Split from ``signed_invalid``
      per ADR-052 §1 so auditors can distinguish "broken" from
      "scope gap."
    * ``"signed_expired"`` — at least one signature is intact +
      valid + trusted **as of the self-reported signing time** but
      no longer trusted at present (typically because the
      end-entity cert expired since signing). ADR-054 §1 — distinct
      from ``signed_untrusted`` because we DO trust the issuing CA;
      the chain has just aged out.
    * ``"signed_tampered"`` — at least one signature shows the
      content was modified after signing (``intact=False``). The
      strongest negative signal: the file is provably altered
      from what was signed.

    Aggregate precedence when a document carries multiple signatures
    (per ADR-054 §4):

        ``signed_tampered > signed_invalid > signed_untrusted
         > signed_expired > signed_valid``

    The worst signal across all signatures wins. ``signed_untrusted``
    outranks ``signed_expired`` because untrusted = no confidence in
    the signing identity at any time, while expired = confidence in
    the identity, just past the validity window.

    Detect-and-record only in v1 — never reject. The chunk metadata
    field lets auditors query for any non-clean state without changing
    the ingestion contract.
    """
    if not enabled:
        return ("check_skipped", 0)
    try:
        from pyhanko.pdf_utils.reader import PdfFileReader
        from pyhanko.sign.validation import validate_pdf_signature
        from pyhanko_certvalidator import ValidationContext
    except ImportError:
        return ("check_unavailable", 0)
    try:
        reader = PdfFileReader(io.BytesIO(raw))
        signatures = list(reader.embedded_signatures)
        if not signatures:
            return ("none", 0)
        vc = _get_validation_context(trust_store_path)
        any_tampered = False
        any_invalid = False
        any_untrusted = False
        any_expired = False
        for emb in signatures:
            status = validate_pdf_signature(emb, vc)
            if not getattr(status, "intact", True):
                any_tampered = True
                continue
            if not getattr(status, "valid", True):
                any_invalid = True
                continue
            if getattr(status, "trusted", True):
                continue  # signed_valid for this sig
            # trusted=False — try as-of-signing-time validation
            # (ADR-054 §2). If pyhanko's chain walk failed because
            # the end-entity cert is expired, retry with a
            # ValidationContext anchored at the signer-asserted
            # signing time. If THAT validates, the signature was
            # legitimate when signed (issuing CA in our trust roots);
            # the chain has just aged out — distinct audit signal
            # from "we don't know this CA at all".
            signing_time = getattr(emb, "self_reported_timestamp", None)
            if signing_time is None or not _VC_TRUST_ROOTS:
                any_untrusted = True
                continue
            try:
                # ADR-054 §2 + 2026-05-09 hotfix:
                # ``time_tolerance`` absorbs clock skew between the
                # signing machine and the CA that issued the leaf
                # cert. main_signed.pdf had a self-reported signing
                # time 28 seconds BEFORE the leaf cert's NotBefore —
                # signing happened mid-issuance, but pyhanko's
                # default 1-second tolerance rejected it as
                # NotYetValidError. 5 minutes is a realistic
                # cap on signer-vs-CA clock skew (roughly Kerberos's
                # default skew tolerance), generous enough to
                # absorb workflow timing without being so wide it
                # masks genuinely-out-of-window signatures.
                from datetime import timedelta

                retry_vc = ValidationContext(
                    trust_roots=list(_VC_TRUST_ROOTS),
                    moment=signing_time,
                    best_signature_time=signing_time,
                    time_tolerance=timedelta(minutes=5),
                )
                retry_status = validate_pdf_signature(emb, retry_vc)
            except Exception as retry_exc:
                # Retry path crashed — fall back to untrusted rather
                # than masking the original outcome with a worse one.
                logger.warning(
                    "as-of-signing-time retry crashed: %s; "
                    "classifying as signed_untrusted",
                    retry_exc,
                )
                any_untrusted = True
                continue
            if getattr(retry_status, "trusted", False):
                any_expired = True
            else:
                any_untrusted = True
        # Precedence (ADR-054 §4): tampered > invalid > untrusted
        # > expired > valid. signed_untrusted (no confidence at any
        # time) outranks signed_expired (confidence at signing time,
        # past validity now).
        if any_tampered:
            return ("signed_tampered", len(signatures))
        if any_invalid:
            return ("signed_invalid", len(signatures))
        if any_untrusted:
            return ("signed_untrusted", len(signatures))
        if any_expired:
            return ("signed_expired", len(signatures))
        return ("signed_valid", len(signatures))
    except Exception as exc:
        logger.warning(
            "Signature validation raised on document: %s",
            exc,
            extra={"reason": "signature_check_exception", "error": repr(exc)},
        )
        return ("check_failed", 0)
