"""Long-Term-Validation (LTV) data summary from the DSS dictionary.

Tier-C item #13 (ADR-056). PAdES-LT / PAdES-LTA documents carry a DSS
(Document Security Store) dictionary with cached OCSP responses, CRLs,
intermediate certs, and document timestamps so the signature can be
re-validated years from now without re-walking external infrastructure.

We do **not** persist the full ASN.1 — those bytes stay in the source
PDF on MinIO and are re-readable for re-validation. The audit-pivot
question this module answers is: *"do we have long-term-validation
evidence on this signature?"*. The summary is a flat JSON-safe object
serialised into the ``ltv_data`` JSONB column on ``memory_items``.

Schema (always the same six keys; counts are zero when ``has_dss`` is
false):

    {
      "has_dss": bool,
      "ocsp_responses": int,
      "crls": int,
      "certs": int,
      "timestamps": int,    # /DocTimeStamp signatures, independent of DSS
      "vri_keys": int       # entries in the VRI sub-dictionary
    }
"""

from __future__ import annotations

import io
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _summarize_ltv(raw: bytes) -> dict[str, Any] | None:
    """Return a flat JSON-safe summary of the DSS dictionary, or ``None``.

    Returns ``None`` for unsigned PDFs (no embedded signatures at all).
    Returns the six-key summary with ``has_dss=False`` for signed PDFs
    that don't carry a DSS dictionary (e.g. PAdES B-B basic).
    """
    try:
        from pyhanko.pdf_utils.reader import PdfFileReader
    except Exception:  # pragma: no cover — pyhanko always installed in prod
        return None
    try:
        reader = PdfFileReader(io.BytesIO(raw))
    except Exception:
        return None
    try:
        signatures = reader.embedded_signatures
    except Exception:  # pragma: no cover — defensive
        signatures = []
    if not signatures:
        return None

    has_dss = False
    ocsp_count = 0
    crl_count = 0
    cert_count = 0
    vri_keys = 0
    # Document timestamps land alongside the regular signature list —
    # counted independently of DSS presence (a doc-timestamp without a
    # DSS dictionary is a valid PAdES-LT shape).
    try:
        timestamp_count = sum(
            1
            for s in signatures
            if "DocTimeStamp" in str(getattr(s, "sig_object", b""))
        )
    except Exception:  # pragma: no cover — defensive
        timestamp_count = 0
    try:
        # We walk the trailer dict directly to stay independent of
        # pyhanko's higher-level helpers (which evolve across releases).
        # pyhanko 0.30+ exposes the trailer as either ``trailer_view``
        # or ``trailer`` depending on access mode.
        trailer = getattr(reader, "trailer_view", None) or getattr(
            reader, "trailer", None
        )
        if trailer is None:  # pragma: no cover — defensive
            dss = None
        else:
            root = trailer.get("/Root") if hasattr(trailer, "get") else None
            dss = (
                root.get("/DSS") if root is not None and hasattr(root, "get") else None
            )
        if dss is None:
            return {
                "has_dss": False,
                "ocsp_responses": 0,
                "crls": 0,
                "certs": 0,
                "timestamps": timestamp_count,
                "vri_keys": 0,
            }
        has_dss = True
        for key, counter in (  # pragma: no cover — exercised by live signed PDFs
            ("/OCSPs", "ocsp"),
            ("/CRLs", "crl"),
            ("/Certs", "cert"),
        ):
            arr = dss.get(key) if hasattr(dss, "get") else None
            try:
                count = len(list(arr)) if arr is not None else 0
            except Exception:
                count = 0
            if counter == "ocsp":
                ocsp_count = count
            elif counter == "crl":
                crl_count = count
            elif counter == "cert":
                cert_count = count
        vri = dss.get("/VRI") if hasattr(dss, "get") else None
        if vri is not None:  # pragma: no cover — exercised by live signed PDFs
            try:
                vri_keys = len(list(vri.keys()))
            except Exception:
                vri_keys = 0
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("LTV summary partial: %s", exc)
    return {
        "has_dss": has_dss,
        "ocsp_responses": ocsp_count,
        "crls": crl_count,
        "certs": cert_count,
        "timestamps": timestamp_count,
        "vri_keys": vri_keys,
    }
