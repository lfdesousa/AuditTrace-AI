"""PDF ingestion orchestrator (tier-A bombs / tier-B robustness / tier-C metadata).

The orchestrator is the seam where every PDF concern in this
sub-package gets composed into a per-file → per-page pipeline:

  1. Bomb defenses (size, page count, xref count, parse timeout)
  2. Encryption gate (refuse password-protected PDFs)
  3. Document metadata extraction (title / author / creator / dates)
  4. PDF/A conformance (XMP namespace)
  5. TOC index (page → section)
  6. Attachment quarantine (PDF/A-3, ZUGFeRD bundles)
  7. Per-page redaction handling (reject / clip-extract policies)
  8. OCR fallback for raster-only pages
  9. AcroForm widget extraction
  10. Chunking + embedding upsert
  11. Manifest write + per-document outcome accumulator

Each step delegates to a small helper from the sibling modules in
this sub-package, so the orchestrator stays a sequence of clearly-
named steps rather than a 600-line wall of nested logic.

**Multi-pod posture (project_k8s_zta_trajectory rule):** every helper
this orchestrator calls is per-pod-idempotent. The ValidationContext
cache (in ``signature.py``) keys on the shared MinIO bundle's sha256;
all replicas converge to the same context after a refresh without
coordination. The chunker and ChromaDB upserts are stateless from
the pod's perspective — state lives in the external stores.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime
from typing import Any

from audittrace.config import get_settings
from audittrace.routes.memory_pdf.classification import (
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
from audittrace.routes.memory_pdf.metadata import _extract_pdf_metadata
from audittrace.routes.memory_pdf.pdfa import _extract_pdfa_conformance
from audittrace.routes.memory_pdf.redactions import (
    _page_bbox,
    _redaction_rects,
    _text_clipped_around_redactions,
)
from audittrace.routes.memory_pdf.signature import _pdf_signature_status
from audittrace.routes.memory_pdf.toc import _build_toc_index

logger = logging.getLogger(__name__)


async def _index_pdf_objects(
    collection: Any,
    minio_client: Any,
    bucket: str,
    objects: list[dict[str, str]],
    col_name: str,
    category: str,
    layer_prefix: str,
    user_id: str,
    ingestion_ts_ms: int,
    manifest_service: Any | None = None,
    details_log: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> int:
    """Stream-index ``.pdf`` files into *collection*, per-page.

    Each page yields one or more text chunks; embedding happens per
    page in _INDEX_BATCH_SIZE slices. The pymupdf ``Document`` is
    opened in a ``with`` block so its internal C-level page cache
    is released deterministically when the block exits — important
    for the per-file client loop where each request must come down
    cleanly before the next one starts.

    *layer_prefix* (``"episodic/"`` / ``"procedural/"``) is stripped
    from the MinIO key to produce a stable ``source_key`` metadata
    field — useful for disambiguating same-name files across folders
    (e.g. two ``main.pdf`` in different paper subdirs).

    *user_id* + *ingestion_ts_ms* propagate into every chunk's
    metadata as ``ingested_by_user_id`` and ``ingestion_ts_ms`` —
    per-chunk reconstructibility per gap-inventory item #21.

    *manifest_service* (tier-B item #22) — when supplied, every PDF
    processed yields one ``upsert_pdf_metadata`` call carrying
    page_count, signature_status, ocr_coverage_pct, attachment_count,
    form_field_count, the structured ``extraction_warnings`` list,
    and document_sha256 (plus the tier-C ADR-056 fields).
    """
    import pymupdf  # heavy import; only load when ai_research_papers is requested

    # Lazy imports of the shared utilities that live in
    # ``audittrace.routes.memory`` (chunking, doc IDs, MinIO read,
    # ChromaDB batched upsert). Keeping them lazy avoids a circular
    # import: routes.memory imports from memory_pdf for re-exports;
    # memory_pdf.pipeline needs the chunker. Function-level imports
    # cost essentially nothing after the first call (Python module
    # cache).
    from audittrace.routes.memory import (
        _chunk_text,
        _doc_id,
        _read_minio_object,
        _upsert_in_batches,
    )

    settings = get_settings()
    max_size_bytes = settings.pdf_max_size_mb * 1024 * 1024

    # Layer for manifest writes. The function takes ``layer_prefix``
    # as ``"episodic/"`` / ``"procedural/"``; strip the trailing slash
    # for the manifest's ``layer`` column.
    manifest_layer = layer_prefix.rstrip("/")

    total = 0
    for obj in objects:
        if not obj["filename"].lower().endswith(".pdf"):
            continue
        raw = await asyncio.to_thread(
            _read_minio_object, minio_client, bucket, obj["key"]
        )
        if raw is None:
            continue

        # Per-document state — accumulated through the per-page loop
        # and flushed to the manifest at end-of-file. Reset per file
        # so a previous bomb-rejected document never bleeds into the
        # next one.
        warnings: list[dict[str, Any]] = []
        attachment_count_doc = 0
        form_field_count_doc = 0
        ocr_pages_doc = 0
        page_count_doc: int | None = None
        # Tier-C metadata (ADR-056 #10) — populated after pymupdf.open
        # succeeds. Bomb-rejected docs leave them None.
        pdf_title_doc: str | None = None
        pdf_author_doc: str | None = None
        pdf_creator_doc: str | None = None
        pdf_creation_date_doc: datetime | None = None
        # Tier-C PDF/A (ADR-056 #14) — extracted from XMP if present.
        pdfa_part_doc: str | None = None
        pdfa_conformance_doc: str | None = None
        # Tier-C LTV (ADR-056 #13) — DSS dictionary summary; only
        # meaningful on signed PDFs.
        ltv_data_doc: dict[str, Any] | None = None
        # Per-document chunk count for the ?details=true response.
        chunks_written_doc = 0

        # Item #18 — bomb defense layer 1: raw byte-size cap.
        if len(raw) > max_size_bytes:
            logger.warning(
                "PDF %s rejected: %d bytes exceeds pdf_max_size_mb=%d",
                obj["key"],
                len(raw),
                settings.pdf_max_size_mb,
                extra={
                    "file": obj["key"],
                    "reason": "max_size",
                    "size_bytes": len(raw),
                    "cap_bytes": max_size_bytes,
                },
            )
            warnings.append(
                {
                    "code": "max_size",
                    "size_bytes": len(raw),
                    "cap_bytes": max_size_bytes,
                }
            )
            await _flush_pdf_manifest(
                manifest_service=manifest_service,
                layer=manifest_layer,
                key=obj["key"],
                user_id=user_id,
                size_bytes=len(raw),
                page_count=None,
                signature_status=None,
                ocr_coverage_pct=None,
                attachment_count=0,
                form_field_count=0,
                extraction_warnings=warnings,
                document_sha256=None,
                ok=False,
                error="max_size",
                details_log=details_log,
            )
            continue
        source_key = (
            obj["key"][len(layer_prefix) :]
            if obj["key"].startswith(layer_prefix)
            else obj["key"]
        )
        # SHA-256 of the raw bytes is the canonical document identity
        # for the entire downstream lifecycle.
        document_hash = hashlib.sha256(raw).hexdigest()
        # Item #12 — signature validation. Computed once per file.
        signature_status, _signers_count = _pdf_signature_status(
            raw,
            enabled=settings.pdf_signature_check_enabled,
            trust_store_path=settings.pdf_signature_trust_store,
        )
        # Tier-C #13 (ADR-056) — LTV summary. Computed once per file
        # alongside signature_status. Returns ``None`` for
        # unsigned / no-DSS PDFs.
        ltv_data_doc = _summarize_ltv(raw)
        try:
            # Assign through Any so mypy doesn't flag pymupdf.open's
            # untyped Document return.
            doc_factory: Any = pymupdf.open
            with doc_factory(stream=raw, filetype="pdf") as doc:
                # Tier-B item #15 — encrypted PDFs refused.
                if _pdf_is_encrypted(doc):
                    logger.warning(
                        "PDF %s rejected: encrypted (refusing — no "
                        "password endpoint per ADR-050)",
                        obj["key"],
                        extra={"file": obj["key"], "reason": "encrypted"},
                    )
                    warnings.append({"code": "encrypted", "page": None})
                    await _flush_pdf_manifest(
                        manifest_service=manifest_service,
                        layer=manifest_layer,
                        key=obj["key"],
                        user_id=user_id,
                        size_bytes=len(raw),
                        page_count=None,
                        signature_status=signature_status,
                        ocr_coverage_pct=None,
                        attachment_count=0,
                        form_field_count=0,
                        extraction_warnings=warnings,
                        document_sha256=document_hash,
                        ok=False,
                        error="encrypted",
                        details_log=details_log,
                    )
                    continue
                # Tier-C item #10 — extract document metadata.
                (
                    pdf_title_doc,
                    pdf_author_doc,
                    pdf_creator_doc,
                    pdf_creation_date_doc,
                    metadata_warning_codes,
                ) = _extract_pdf_metadata(doc)
                for code in metadata_warning_codes:
                    warnings.append({"code": code, "page": None})
                # Tier-C item #14 — PDF/A conformance from XMP.
                pdfa_part_doc, pdfa_conformance_doc = _extract_pdfa_conformance(doc)
                # Tier-C item #9 — page → TOC-section map.
                toc_index = _build_toc_index(doc)
                # Item #18 — bomb defense layer 2: declared-shape caps.
                page_count_doc = doc.page_count
                if doc.page_count > settings.pdf_max_pages:
                    logger.warning(
                        "PDF %s rejected: page_count=%d exceeds pdf_max_pages=%d",
                        obj["key"],
                        doc.page_count,
                        settings.pdf_max_pages,
                        extra={
                            "file": obj["key"],
                            "reason": "max_pages",
                            "page_count": doc.page_count,
                            "cap": settings.pdf_max_pages,
                        },
                    )
                    warnings.append(
                        {
                            "code": "max_pages",
                            "page_count": doc.page_count,
                            "cap": settings.pdf_max_pages,
                        }
                    )
                    await _flush_pdf_manifest(
                        manifest_service=manifest_service,
                        layer=manifest_layer,
                        key=obj["key"],
                        user_id=user_id,
                        size_bytes=len(raw),
                        page_count=page_count_doc,
                        signature_status=signature_status,
                        ocr_coverage_pct=None,
                        attachment_count=0,
                        form_field_count=0,
                        extraction_warnings=warnings,
                        document_sha256=document_hash,
                        pdf_title=pdf_title_doc,
                        pdf_author=pdf_author_doc,
                        pdf_creator=pdf_creator_doc,
                        pdf_creation_date=pdf_creation_date_doc,
                        ok=False,
                        error="max_pages",
                        details_log=details_log,
                    )
                    continue
                xref_count = doc.xref_length()
                if xref_count > settings.pdf_max_xref_count:
                    logger.warning(
                        "PDF %s rejected: xref_count=%d exceeds pdf_max_xref_count=%d",
                        obj["key"],
                        xref_count,
                        settings.pdf_max_xref_count,
                        extra={
                            "file": obj["key"],
                            "reason": "max_xref",
                            "xref_count": xref_count,
                            "cap": settings.pdf_max_xref_count,
                        },
                    )
                    warnings.append(
                        {
                            "code": "max_xref",
                            "xref_count": xref_count,
                            "cap": settings.pdf_max_xref_count,
                        }
                    )
                    await _flush_pdf_manifest(
                        manifest_service=manifest_service,
                        layer=manifest_layer,
                        key=obj["key"],
                        user_id=user_id,
                        size_bytes=len(raw),
                        page_count=page_count_doc,
                        signature_status=signature_status,
                        ocr_coverage_pct=None,
                        attachment_count=0,
                        form_field_count=0,
                        extraction_warnings=warnings,
                        document_sha256=document_hash,
                        pdf_title=pdf_title_doc,
                        pdf_author=pdf_author_doc,
                        pdf_creator=pdf_creator_doc,
                        pdf_creation_date=pdf_creation_date_doc,
                        ok=False,
                        error="max_xref",
                        details_log=details_log,
                    )
                    continue
                # Tier-B item #6 — quarantine embedded attachments.
                attachment_count_doc, attachment_warnings = _quarantine_pdf_attachments(
                    doc,
                    parent_filename=source_key,
                    layer_prefix=layer_prefix,
                    minio_client=minio_client,
                    bucket=bucket,
                )
                warnings.extend(attachment_warnings)
                # Item #18 — bomb defense layer 3: wall-clock budget.
                parse_start = time.monotonic()
                for page_num, page in enumerate(doc, start=1):
                    if (
                        time.monotonic() - parse_start
                        > settings.pdf_parse_timeout_seconds
                    ):
                        logger.warning(
                            "PDF %s parse aborted: exceeded pdf_parse_timeout_seconds=%d",
                            obj["key"],
                            settings.pdf_parse_timeout_seconds,
                            extra={
                                "file": obj["key"],
                                "reason": "parse_timeout",
                                "pages_processed": page_num - 1,
                            },
                        )
                        warnings.append(
                            {
                                "code": "parse_timeout",
                                "pages_processed": page_num - 1,
                            }
                        )
                        break
                    # Item #8 — unflattened redaction handling.
                    redaction_rects = _redaction_rects(page)
                    redaction_status = "none"
                    if redaction_rects:
                        if settings.pdf_redaction_policy == "reject":
                            logger.warning(
                                "PDF %s rejected at page %d: %d unflattened "
                                "redaction(s); pdf_redaction_policy=reject",
                                obj["key"],
                                page_num,
                                len(redaction_rects),
                                extra={
                                    "file": obj["key"],
                                    "page": page_num,
                                    "reason": "unflattened_redactions",
                                    "redaction_count": len(redaction_rects),
                                    "policy": "reject",
                                },
                            )
                            warnings.append(
                                {
                                    "code": "redaction_rejected",
                                    "page": page_num,
                                    "redaction_count": len(redaction_rects),
                                }
                            )
                            break
                        if settings.pdf_redaction_policy == "clip-extract":
                            text = _text_clipped_around_redactions(
                                page, redaction_rects
                            )
                            redaction_status = "clipped"
                            logger.info(
                                "PDF %s page %d: %d redaction(s) clipped",
                                obj["key"],
                                page_num,
                                len(redaction_rects),
                                extra={
                                    "file": obj["key"],
                                    "page": page_num,
                                    "redaction_count": len(redaction_rects),
                                    "policy": "clip-extract",
                                },
                            )
                            warnings.append(
                                {
                                    "code": "redaction_clipped",
                                    "page": page_num,
                                    "redaction_count": len(redaction_rects),
                                }
                            )
                        else:
                            # Unknown policy value: log + reject for safety.
                            logger.warning(
                                "PDF %s rejected at page %d: unknown "
                                "pdf_redaction_policy=%r (expected "
                                "'reject' | 'clip-extract')",
                                obj["key"],
                                page_num,
                                settings.pdf_redaction_policy,
                            )
                            break
                    else:
                        text = page.get_text()
                    # Item #18 — bomb defense layer 4: per-page text cap.
                    if len(text) > settings.pdf_max_page_text_bytes:
                        logger.warning(
                            "PDF %s page %d skipped: extracted_bytes=%d exceeds pdf_max_page_text_bytes=%d",
                            obj["key"],
                            page_num,
                            len(text),
                            settings.pdf_max_page_text_bytes,
                            extra={
                                "file": obj["key"],
                                "page": page_num,
                                "reason": "max_page_text",
                                "extracted_bytes": len(text),
                                "cap": settings.pdf_max_page_text_bytes,
                            },
                        )
                        warnings.append(
                            {
                                "code": "max_page_text",
                                "page": page_num,
                                "extracted_bytes": len(text),
                                "cap": settings.pdf_max_page_text_bytes,
                            }
                        )
                        continue
                    text = text.strip()
                    # Tier-B item #1 — OCR fallback for raster-only pages.
                    text_source = "native"
                    extraction_confidence = 1.0
                    if not text:  # pragma: no cover — exercised by live OCR PDFs
                        try:
                            has_images = bool(page.get_images(full=False))
                        except Exception:
                            has_images = False
                        if has_images:
                            ocr_text, ocr_source, ocr_conf = _ocr_render_page(
                                page,
                                enabled=settings.pdf_ocr_enabled,
                                languages=settings.pdf_ocr_languages,
                                dpi=settings.pdf_ocr_dpi,
                            )
                            if ocr_source == "ocr" and ocr_text:
                                text = ocr_text
                                text_source = "ocr"
                                extraction_confidence = ocr_conf
                                ocr_pages_doc += 1
                                if ocr_conf < 0.6:
                                    warnings.append(
                                        {
                                            "code": "ocr_low_confidence",
                                            "page": page_num,
                                            "confidence": ocr_conf,
                                        }
                                    )
                            else:
                                # Raster-only page, OCR unavailable.
                                warnings.append(
                                    {
                                        "code": "no_text_layer",
                                        "page": page_num,
                                    }
                                )
                                continue
                        else:
                            # Truly empty page — benign, no warning.
                            continue
                    # Tier-B item #7 — AcroForm widget extraction.
                    form_text, form_count = _acroform_text_for_page(page)
                    if (
                        form_text and form_count > 0
                    ):  # pragma: no cover — exercised by live form-bearing PDFs
                        form_field_count_doc += form_count
                        warnings.append(
                            {
                                "code": "form_fields",
                                "page": page_num,
                                "field_count": form_count,
                            }
                        )
                    if (
                        not text and not form_text
                    ):  # pragma: no cover — defensive; both empty is rare
                        continue
                    chunks = _chunk_text(text) if text else []
                    if (
                        form_text
                    ):  # pragma: no cover — exercised by live form-bearing PDFs
                        chunks.append(form_text)
                    if not chunks:  # pragma: no cover — defensive
                        continue
                    bbox_x0, bbox_y0, bbox_x1, bbox_y1 = _page_bbox(page)
                    ids = [
                        _doc_id(col_name, f"{source_key}:p{page_num}", i)
                        for i in range(len(chunks))
                    ]
                    form_idx = len(chunks) - 1 if form_text else -1
                    toc_section = toc_index.get(page_num)
                    metadatas: list[dict[str, Any]] = [
                        {
                            "source": obj["filename"],
                            "source_key": source_key,
                            "category": category,
                            "file_type": "pdf",
                            "page": page_num,
                            "chunk": i,
                            # Tier-C #9 — TOC section title for this page.
                            "toc_section": toc_section,
                            "bbox_x0": bbox_x0,
                            "bbox_y0": bbox_y0,
                            "bbox_x1": bbox_x1,
                            "bbox_y1": bbox_y1,
                            "text_source": (
                                "form_field" if i == form_idx else text_source
                            ),
                            "extraction_confidence": extraction_confidence,
                            "document_hash": document_hash,
                            "signature_status": signature_status,
                            "redaction_status": redaction_status,
                            "ingested_by_user_id": user_id,
                            "ingestion_ts_ms": ingestion_ts_ms,
                            "chunk_type": ("form_field" if i == form_idx else "text"),
                        }
                        for i in range(len(chunks))
                    ]
                    # ChromaDB metadata can't carry None values — drop
                    # ``toc_section`` when this page sits before any
                    # TOC entry or the document has no TOC at all.
                    for md in metadatas:
                        if md.get("toc_section") is None:
                            md.pop("toc_section", None)
                    if not dry_run:
                        await _upsert_in_batches(collection, ids, chunks, metadatas)
                        total += len(chunks)
                    # Tier-C #23 — chunks_written_doc reflects what
                    # *would* have been written, so dry-run response
                    # shape matches real-run shape exactly.
                    chunks_written_doc += len(chunks)
            # Successful (or partial) processing — flush manifest.
            ocr_coverage_pct: float | None = None
            if page_count_doc and page_count_doc > 0:
                ocr_coverage_pct = round((ocr_pages_doc / page_count_doc) * 100.0, 2)
            await _flush_pdf_manifest(
                manifest_service=None if dry_run else manifest_service,
                layer=manifest_layer,
                key=obj["key"],
                user_id=user_id,
                size_bytes=len(raw),
                page_count=page_count_doc,
                signature_status=signature_status,
                ocr_coverage_pct=ocr_coverage_pct,
                attachment_count=attachment_count_doc,
                form_field_count=form_field_count_doc,
                extraction_warnings=warnings,
                document_sha256=document_hash,
                pdf_title=pdf_title_doc,
                pdf_author=pdf_author_doc,
                pdf_creator=pdf_creator_doc,
                pdf_creation_date=pdf_creation_date_doc,
                pdfa_part=pdfa_part_doc,
                pdfa_conformance=pdfa_conformance_doc,
                ltv_data=ltv_data_doc,
                chunks_written=chunks_written_doc,
                ok=True,
                error=None,
                details_log=details_log,
            )
        except Exception as exc:
            # Tier-C item #16 — classify pymupdf raises into closed-set
            # codes. Falls through to ``pdf_corrupted_structure`` for
            # unmatched raises.
            code = _classify_pdf_extraction_error(exc)
            logger.warning(
                "Failed to process PDF %s: %s (classified=%s)",
                obj["key"],
                exc,
                code,
                extra={"file": obj["key"], "reason": code},
            )
            warnings.append({"code": code, "page": None})
            await _flush_pdf_manifest(
                manifest_service=manifest_service,
                layer=manifest_layer,
                key=obj["key"],
                user_id=user_id,
                size_bytes=len(raw),
                page_count=page_count_doc,
                signature_status=signature_status,
                ocr_coverage_pct=None,
                attachment_count=attachment_count_doc,
                form_field_count=form_field_count_doc,
                extraction_warnings=warnings,
                document_sha256=document_hash,
                pdf_title=pdf_title_doc,
                pdf_author=pdf_author_doc,
                pdf_creator=pdf_creator_doc,
                pdf_creation_date=pdf_creation_date_doc,
                pdfa_part=pdfa_part_doc,
                pdfa_conformance=pdfa_conformance_doc,
                ltv_data=ltv_data_doc,
                chunks_written=chunks_written_doc,
                ok=False,
                error=str(exc) or code,
                details_log=details_log,
            )
            continue
    return total
