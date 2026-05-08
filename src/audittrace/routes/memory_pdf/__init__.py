"""PDF concerns extracted from ``routes/memory.py`` (2026-05-09 refactor).

The pre-refactor ``routes/memory.py`` had grown to 3263 lines, with
the PDF pipeline accounting for ~1100 of them across closely-coupled
helpers. The §11 (PYTHON-ENGINEERING SKILL) module-decomposition
discipline says: at >2000 LOC, no new code goes in; new work goes in
a sibling module. This package is the sibling.

Each module decomposes by **concern**, not by call frequency. The
orchestrator (``_index_pdf_objects``) and page-level helpers stay in
``routes/memory.py`` for now — extracting those is a future PR once
the helpers below have settled.

Public API: callers import ``from audittrace.routes.memory_pdf import …``.
``routes/memory.py`` re-exports the same names at module scope for
backwards compatibility with existing test imports
(``from audittrace.routes.memory import _PDF_WARNING_CODES``).
"""

from __future__ import annotations

from audittrace.routes.memory_pdf.classification import (
    _PDF_WARNING_CODES,
    _classify_pdf_extraction_error,
)
from audittrace.routes.memory_pdf.extraction import (
    _acroform_text_for_page,
    _ocr_render_page,
    _pdf_is_encrypted,
    _quarantine_pdf_attachments,
)
from audittrace.routes.memory_pdf.ltv import _summarize_ltv
from audittrace.routes.memory_pdf.manifest import _flush_pdf_manifest
from audittrace.routes.memory_pdf.metadata import (
    _extract_pdf_metadata,
    _parse_pdf_date,
    _trim_pdf_metadata_string,
)
from audittrace.routes.memory_pdf.pdfa import (
    _PDFA_CONFORMANCE_RE,
    _PDFA_PART_RE,
    _extract_pdfa_conformance,
)
from audittrace.routes.memory_pdf.pipeline import _index_pdf_objects
from audittrace.routes.memory_pdf.redactions import (
    _PDF_ANNOT_REDACT,
    _page_bbox,
    _rects_intersect,
    _redaction_rects,
    _text_clipped_around_redactions,
)
from audittrace.routes.memory_pdf.signature import (
    _PEM_CERT_RE,
    _SIGNATURE_STATUS_CODES,
    _get_validation_context,
    _invalidate_validation_context,
    _pdf_signature_status,
    _pem_bundle_to_cert_list,
)
from audittrace.routes.memory_pdf.toc import _build_toc_index

__all__ = [
    # classification
    "_PDF_WARNING_CODES",
    "_classify_pdf_extraction_error",
    # extraction (page-level helpers)
    "_acroform_text_for_page",
    "_ocr_render_page",
    "_pdf_is_encrypted",
    "_quarantine_pdf_attachments",
    # ltv
    "_summarize_ltv",
    # manifest
    "_flush_pdf_manifest",
    # metadata
    "_extract_pdf_metadata",
    "_parse_pdf_date",
    "_trim_pdf_metadata_string",
    # pdfa
    "_PDFA_CONFORMANCE_RE",
    "_PDFA_PART_RE",
    "_extract_pdfa_conformance",
    # pipeline (orchestrator)
    "_index_pdf_objects",
    # redactions
    "_PDF_ANNOT_REDACT",
    "_page_bbox",
    "_rects_intersect",
    "_redaction_rects",
    "_text_clipped_around_redactions",
    # signature
    "_PEM_CERT_RE",
    "_SIGNATURE_STATUS_CODES",
    "_get_validation_context",
    "_invalidate_validation_context",
    "_pdf_signature_status",
    "_pem_bundle_to_cert_list",
    # toc
    "_build_toc_index",
]
