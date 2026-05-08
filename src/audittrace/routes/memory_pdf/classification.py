"""Closed-set extraction-warning vocabulary + corruption classifier.

The ``_PDF_WARNING_CODES`` frozenset pins every value that may appear
under ``extraction_warnings[*].code`` in ``memory_items``. The set is
audit-load-bearing — adding a code without amending the relevant ADR
is a quiet documentation drift. ``tests/test_memory_routes.py::
TestExtractionWarningCodes`` enforces set-equality so the drift
surfaces in CI.

``_classify_pdf_extraction_error`` is the public face of tier-C item
#16 (ADR-056) — routes pymupdf raises into the closed set so an
auditor can query "show me docs that failed for THIS class of reason"
without parsing exception strings out of logs.
"""

from __future__ import annotations

_PDF_WARNING_CODES: frozenset[str] = frozenset(
    {
        # Bomb defenses (tier-A item #18)
        "max_size",
        "max_pages",
        "max_xref",
        "max_page_text",
        "parse_timeout",
        # Redaction handling (tier-A item #8)
        "redaction_clipped",
        "redaction_rejected",
        # Tier-B items
        "encrypted",  # #15 — encrypted PDF refused
        "no_text_layer",  # #1 — page had raster only, OCR unavailable
        "ocr_low_confidence",  # #1 — OCR ran but confidence < threshold
        "attachment",  # #6 — quarantined embedded file
        "attachment_quarantine_failed",  # #6 — could not write to MinIO
        "form_fields",  # #7 — page yielded form-field data
        # Tier-C items (ADR-056)
        "pdf_corrupted_xref",  # #16 — pymupdf raised on xref walk
        "pdf_corrupted_structure",  # #16 — generic structural parse error
        "pdf_metadata_parse_error",  # #10 — doc.metadata yielded malformed values
    }
)


def _classify_pdf_extraction_error(exc: Exception) -> str:
    """Map a pymupdf raise to a closed-set ``extraction_warnings`` code.

    Tier-C item #16 (ADR-056). pymupdf surfaces parse failures via a
    handful of exception classes (``FileDataError``, ``EmptyFileError``,
    plain ``RuntimeError``) plus message strings naming the specific
    failure mode. We route on a combination of class name + message
    substring — both are stable across recent pymupdf releases (verified
    against the corpus of issues on the upstream tracker).

    Returns one of:
      * ``"pdf_corrupted_xref"`` — xref walk failed (truncated, malformed)
      * ``"pdf_corrupted_structure"`` — other structural parse error
    """
    cls = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "xref" in msg or "trailer" in msg:
        return "pdf_corrupted_xref"
    if "empty" in cls or "filedata" in cls or "format" in msg:
        return "pdf_corrupted_structure"
    return "pdf_corrupted_structure"
