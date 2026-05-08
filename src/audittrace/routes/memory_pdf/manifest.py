"""Per-document manifest write + per-document outcome accumulation.

Tier-B item #22 (the manifest-write seam) + tier-C item #24 (the
per-document outcome surfaced via ``?details=true``). The function lives
on its own because it has two independent contracts:

1. **Postgres write** — best-effort. A failure here logs but doesn't
   re-raise; the chunks have already landed in ChromaDB and audit-trail
   resiliency takes precedence over strict consistency.
2. **Per-document outcome accumulation** — when ``details_log`` is
   supplied, every call appends one dict for the ``?details=true``
   response shape. Decoupled from the Postgres write so a Postgres
   outage doesn't lose operator visibility.

Both contracts are kept in one function because every call site needs
both, and the data shape is identical between them.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def _flush_pdf_manifest(
    *,
    manifest_service: Any | None,
    layer: str,
    key: str,
    user_id: str,
    size_bytes: int,
    page_count: int | None,
    signature_status: str | None,
    ocr_coverage_pct: float | None,
    attachment_count: int,
    form_field_count: int,
    extraction_warnings: list[dict[str, Any]],
    document_sha256: str | None,
    pdf_title: str | None = None,
    pdf_author: str | None = None,
    pdf_creator: str | None = None,
    pdf_creation_date: datetime | None = None,
    pdfa_part: str | None = None,
    pdfa_conformance: str | None = None,
    ltv_data: dict[str, Any] | None = None,
    chunks_written: int = 0,
    ok: bool = True,
    error: str | None = None,
    details_log: list[dict[str, Any]] | None = None,
) -> None:
    """Best-effort manifest write for one PDF (ADR-050 #22 + ADR-056).

    Skips manifest write silently when ``manifest_service is None``
    (pre-tier-B callers, unit tests that patch out the manifest path).
    Logs but does not re-raise on Postgres failure.

    *details_log* (ADR-056 #24) — when supplied, every call appends
    one per-document outcome dict for the ``?details=true`` response
    shape.
    """
    if details_log is not None:
        details_log.append(
            {
                "file": f"{layer}/{key}" if not key.startswith(f"{layer}/") else key,
                "chunks": chunks_written,
                "signature_status": signature_status,
                "page_count": page_count,
                "extraction_warnings": list(extraction_warnings),
                "document_sha256": document_sha256,
                "pdf_title": pdf_title,
                "pdf_author": pdf_author,
                "pdf_creator": pdf_creator,
                "pdf_creation_date": (
                    pdf_creation_date.isoformat()
                    if pdf_creation_date is not None
                    else None
                ),
                "pdfa_part": pdfa_part,
                "pdfa_conformance": pdfa_conformance,
                "ltv_data": ltv_data,
                "ok": ok,
                "error": error,
            }
        )
    if manifest_service is None:
        return
    try:
        manifest_service.upsert_pdf_metadata(
            layer,
            key,
            user_id=user_id,
            size_bytes=size_bytes,
            page_count=page_count,
            signature_status=signature_status,
            ocr_coverage_pct=ocr_coverage_pct,
            attachment_count=attachment_count,
            form_field_count=form_field_count,
            extraction_warnings=extraction_warnings,
            document_sha256=document_sha256,
            pdf_title=pdf_title,
            pdf_author=pdf_author,
            pdf_creator=pdf_creator,
            pdf_creation_date=pdf_creation_date,
            pdfa_part=pdfa_part,
            pdfa_conformance=pdfa_conformance,
            ltv_data=ltv_data,
        )
    except Exception as exc:
        logger.warning(
            "Failed to write PDF manifest for %s/%s: %s",
            layer,
            key,
            exc,
        )
