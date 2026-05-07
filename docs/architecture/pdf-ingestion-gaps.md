# PDF ingestion robustness — exhaustive gap inventory

**Author:** Luis Filipe de Sousa
**Date:** 2026-05-07
**Status:** Reference material — input to ADR-048 and to future PDF-ingestion ADRs.
**Companion ADR:** [ADR-048 — Ingestion content-control service](../ADR-048-ingestion-content-control.md) covers the trust-boundary / security half of PDF ingestion (untrusted bytes never reach `memory-server`). This document is the robustness / completeness / audit-grade half.

---

## 1. Why this document exists

The current PDF ingestion path (`src/audittrace/routes/memory.py::_index_pdf_objects`) treats a PDF as "bytes that yield text per page." That is sufficient for engineer-curated `.md` corpora. It is insufficient for any deployment that ingests externally produced PDFs, because:

1. PDFs from the wild carry structure, signatures, attachments, and OCR-only pages that the current `pymupdf.get_text()` path silently drops or misrepresents.
2. PDFs are a known malware vector. Any path that calls `pymupdf.open(stream=raw)` on untrusted bytes is an attack surface that the rest of the architecture cannot defend.

Point (2) is addressed by ADR-048's content-control gate. Point (1) is the subject of this document — an exhaustive inventory of robustness, completeness, and audit-grade gaps in the current implementation. Items are listed with the failure mode they create, not as a priority list — prioritisation is a separate exercise once the inventory is agreed.

The four items in **bold** in §2.8 (#8 unflattened redactions, #12 signature validity, #18 PDF bombs, plus the implied #21 audit-grade chunk provenance) are the ones that change the security or reconstructibility posture, not just the feature surface. Those need to be addressed before any external trust claim is made about PDF ingestion. The rest are quality and completeness improvements that can be sequenced over time.

---

## 2. PDF ingestion robustness — exhaustive gap list

### 2.1 Text extraction completeness

**Problem 1 — Scanned / image-only pages produce empty text.**
`pymupdf.get_text()` returns `""` for pages where the text layer is absent (the page is a raster image of text). The current code path is:

```python
text = page.get_text().strip()
if not text:
    continue
```

This is a **silent data-loss bug**. The PDF is recorded as ingested; the actual content is unindexed; nothing in the manifest reflects the gap.

*Fix options:*
- **OCR fallback.** When `text == ""` and the page contains raster content, run Tesseract (or an equivalent) and record the page as `text_source: "ocr"` rather than `"native"`.
- **Explicit gap flag.** If OCR is out of scope, the page must be recorded in the manifest with `status: "no_text_layer"` so downstream consumers can see what was skipped. Silent skipping is the worst option.

**Problem 2 — Mixed text / image pages are partially extracted.**
A page with a native text block plus a raster image containing additional text returns only the native portion. Same fix as above: OCR pass for raster regions, or explicit "partial extraction" flag.

**Problem 3 — Tables lose structure.**
`get_text()` flattens table cells into a column-major or row-major reading order depending on the PDF generator. For documents where tables carry the actual semantic payload (financial schedules, schedules of conditions, KPI tables), this destroys the meaning before embedding.

*Fix options:*
- **Layout-aware extraction.** `pymupdf` exposes `page.get_text("dict")` which returns block-level structure including bounding boxes; a table reconstruction pass (or a library like `pdfplumber`, `camelot`, or `tabula-py`) preserves cell adjacency.
- **Per-table chunking with metadata.** Each detected table is its own chunk with `chunk_type: "table"` so downstream retrieval can prioritise structured matches.

**Problem 4 — Multi-column layouts read out of order.**
Two-column academic papers and many regulatory documents use multi-column layouts. Naive `get_text()` reads left-column-line-1 → right-column-line-1 → left-column-line-2 → ... producing incoherent prose. The same `get_text("dict")` block-level approach with reading-order reconstruction fixes this; without it, embeddings are computed on garbled text and search quality silently degrades.

**Problem 5 — Reading order in RTL / mixed-direction documents.**
Arabic, Hebrew, mixed scripts. PDFs encode visual order, not logical order. The default extraction can reverse strings or interleave them with surrounding LTR text. Anyone ingesting documents in those languages will hit this.

### 2.2 Embedded structures the current path ignores

**Problem 6 — Embedded attachments (PDF/A-3, e-invoicing, court filings).**
PDFs can carry attached files. PDF/A-3 was specifically designed to allow this and is heavily used for embedding XML invoices (ZUGFeRD, Factur-X) inside human-readable PDFs. Court systems use it for evidence bundles. `pymupdf.Document.embfile_count()` and `embfile_get()` expose them. Current code ignores them entirely.

*Decision required:* extract and index, extract and quarantine for separate handling, or refuse-and-flag. All three are defensible — but it must be a documented decision.

**Problem 7 — AcroForm field values.**
Filled-in form fields (medical forms, tax filings, intake questionnaires) are stored separately from the page content stream. `get_text()` does not return them. They are accessed via `page.widgets()`. A "filled form" PDF can ingest as nearly empty under the current path.

**Problem 8 — Annotations, comments, sticky notes, redactions.**
PDF annotations (`page.annots()`) carry reviewer comments, highlights, and — critically — **redaction marks**. A document with applied redactions but unflattened annotations may still contain the redacted text in the underlying content stream. Indexing it would expose data the document author intended to remove. This is a confidentiality bug, not a feature gap.

*Required handling:*
- Detect unflattened redactions before indexing.
- Either reject the document, or extract only the visible (post-redaction) content via `page.get_text(clip=...)` excluding redaction rectangles.

**Problem 9 — Bookmarks / outline / table of contents.**
`doc.get_toc()` returns the document's structural outline. Useful for chunking by logical section rather than by page (a 50-page contract is better chunked by clause than by page boundary). Currently unused.

**Problem 10 — Document metadata.**
`doc.metadata` exposes title, author, creator tool, creation date, modification date, keywords. None of this is captured in the current chunk metadata. For provenance and search relevance both, this is high-value low-cost data.

### 2.3 Signed / certified PDFs

**Problem 11 — Signature presence is not detected.**
A signed PDF (CAdES, PAdES, or any of the variants) has a signature dictionary in `doc.xref_object()`. The signing entity, signing time, and validity status all live there. The current code reads bytes and indexes text; nothing records whether the document was signed.

**Problem 12 — Signature validity is not verified.**
Even if presence were detected, the signature's cryptographic validity (chain to a trusted root, certificate-not-revoked, hash-matches-content) is not checked. A signed PDF that has been tampered with after signing will index identically to one that hasn't. Any audit story that relies on "the indexed content matches the signed content" is currently un-evidenced.

**Problem 13 — Long-Term Validation (LTV) data.**
PAdES-LTV embeds the OCSP/CRL responses needed to validate the signature years after the signing certificate expires. For long-term archival use cases this is the difference between a signature that can be re-verified in 2030 and one that cannot. Worth at least *flagging* whether LTV data is present.

*Recommended approach:*
- Library: `pyhanko` (Python, supports PAdES verification end-to-end) or shell out to `verapdf` for PDF/A + signature analysis.
- Per-document metadata: `signature_present`, `signature_valid`, `signers: [{ name, certificate_subject, signing_time, ltv_present }]`, `tampering_detected`.

### 2.4 Format conformance and integrity

**Problem 14 — PDF/A conformance level not detected.**
PDF/A-1, PDF/A-2, PDF/A-3, PDF/A-4 conformance levels (a / b / u / e) determine archival suitability and what the file is allowed to contain. Detected via XMP metadata + structural validation (`verapdf` is the reference implementation). Worth recording for retention-policy decisions even if not enforced.

**Problem 15 — Encrypted / password-protected PDFs.**
`pymupdf.open()` succeeds on encrypted PDFs but `page.get_text()` returns empty until `doc.authenticate(password)` is called. Current code will silently produce empty extractions for encrypted documents — same silent-data-loss class as Problem 1.

**Problem 16 — Corrupted / truncated files.**
`pymupdf` is generally robust but does throw on severely malformed input. Current code catches all exceptions with a single `logger.warning(...)` and continues. The audit trail records "ingested" without recording the failure. The manifest must distinguish `indexed`, `parse_failed`, `partially_indexed`, and `rejected`.

**Problem 17 — Hybrid / linearised / object-stream-heavy PDFs.**
Some PDFs use cross-reference streams or linearisation that confuse extractors. Failure modes range from missing pages to wrong page count. Worth a sanity check: assert `doc.page_count` matches the page count reported in the document catalog, log discrepancies.

### 2.5 Resource and memory bounds

**Problem 18 — PDF bombs ("billion laughs" for PDFs).**
A small PDF can specify enormous page counts, deeply nested resources, or content streams that decompress to gigabytes. `pymupdf` does not bound these by default. A maliciously crafted 2 KiB PDF can exhaust memory on parse.

*Mitigation:*
- Pre-flight check: file size limit (e.g. ≤200 MiB at the upload route), page count cap (≤2000 pages), content stream decompression cap.
- These bounds are configuration values that the operator can tune; shipping them at zero is the wrong default.

**Problem 19 — Per-page memory growth in long documents.**
Even on benign documents, very long PDFs (1000+ pages) accumulate `pymupdf` page objects in the C-level cache faster than Python's GC releases them. The current `with` block on the Document mitigates this at end-of-file; for individual long files it does not. Consider an explicit `page = None; doc.flush_cache()` pattern between pages once page count exceeds a threshold.

**Problem 20 — Embedding throughput.**
The current per-page chunking calls `_upsert_in_batches` with a fixed `_INDEX_BATCH_SIZE`. For a 500-page document that is 500 separate batched upserts. Worth benchmarking whether batching across pages (collect N chunks, then upsert) improves throughput at the cost of memory residency.

### 2.6 Provenance and chunk metadata

**Problem 21 — Per-chunk provenance is incomplete.**
Current chunk metadata: `source`, `source_key`, `category`, `file_type`, `page`, `chunk`. Missing for legal-grade reconstructibility:

- `bbox` — the bounding box on the page where the chunk's text lives. Without it, "the AI cited page 7" is verifiable; "the AI cited the third paragraph of page 7" is not.
- `text_source` — `native` / `ocr` / `mixed` / `partial`.
- `extraction_confidence` — for OCR results, the engine's confidence score.
- `document_hash` — SHA-256 of the original file. Lets the operator prove the indexed content matches a specific version of the document.
- `signature_status` — if signed, what was the validation result at ingestion time.
- `ingested_by_user_id` and `ingestion_ts` — already present for the manifest, should propagate to every chunk metadata for full per-chunk reconstructibility.

**Problem 22 — Document-level manifest is thin.**
The `MemoryManifestService` records a `MemoryItem` per file. For PDFs the row should additionally carry: page count, signature status, OCR coverage (% of pages that needed OCR), conformance level, scan verdict (ADR-048), and a structured `extraction_warnings` JSON field listing every gap detected during ingestion.

### 2.7 Operator and observability surface

**Problem 23 — No "dry-run" / preview mode.**
An operator preparing to ingest a 5,000-document corpus has no way to surface "of these documents, X are scanned-only, Y are encrypted, Z are signed, W exceed the size limit" without committing to ingestion. A `?dry_run=true` mode that produces the report without writing to ChromaDB or the manifest is worth one weekend of work and saves operators days of remediation.

**Problem 24 — Per-document audit trail granularity.**
A single `/memory/index` call indexing 200 PDFs currently logs per-collection totals. The audit trail needs per-document outcomes: which files succeeded, which failed, which were rejected, and *why*. Otherwise the reconstructibility contract holds at the request level but breaks at the document level — and the document level is what reviewers will ask about.

**Problem 25 — No "re-index this one document" surgical operation.**
The current `?file=<key>` mode handles this for a known key. For a corpus where some documents failed, the operator needs `/memory/reindex?file=<key>&force=true` semantics with a clean delete-then-reinsert pathway, atomic at the chunk-set level. Otherwise partial state from a previous failure poisons the collection.

### 2.8 Summary table

| # | Gap | Severity (silent data loss / security / quality) | Implementation cost |
|---|---|---|---|
| 1 | OCR for scanned pages | Silent data loss | Medium |
| 2 | Mixed text/image extraction | Silent data loss | Medium |
| 3 | Tables lose structure | Quality | Medium |
| 4 | Multi-column reading order | Quality | Low |
| 5 | RTL / mixed-direction handling | Quality | Medium |
| 6 | Embedded attachments | Silent data loss | Low |
| 7 | AcroForm field values | Silent data loss | Low |
| 8 | Annotations / redactions | **Security (confidentiality)** | Medium |
| 9 | Bookmarks / TOC for chunking | Quality | Low |
| 10 | Document metadata | Quality | Trivial |
| 11 | Signature presence | Provenance | Low |
| 12 | Signature validity | **Provenance (audit-grade)** | Medium |
| 13 | LTV data | Provenance | Low |
| 14 | PDF/A conformance | Quality | Low |
| 15 | Encrypted PDFs | Silent data loss | Low |
| 16 | Corrupted / truncated files | Quality | Trivial |
| 17 | Hybrid / linearised PDFs | Quality | Low |
| 18 | PDF bombs | **Security (availability)** | Trivial |
| 19 | Per-page memory growth | Resource management | Low |
| 20 | Embedding throughput | Performance | Low |
| 21 | Per-chunk provenance | Audit-grade | Low |
| 22 | Document-level manifest | Audit-grade | Low |
| 23 | Dry-run mode | Operator UX | Low |
| 24 | Per-document audit granularity | Audit-grade | Low |
| 25 | Surgical re-index | Operator UX | Low |

---

## 3. Sequencing

The robustness workstream (this document) is independent of the trust-boundary workstream (ADR-048), but ADR-048 is a hard prerequisite for any external trust claim involving PDF ingestion. Engineering order:

1. **ADR-048 v1 first.** No external user uploads of PDFs are accepted into the indexable path until the content-control gate is operational. This is a security-posture floor.
2. **Items 8, 12, 18 in parallel.** These three are security/audit-grade gaps that ADR-048 alone does not close.
3. **Items 1, 6, 7, 15, 21, 22 next.** These are the silent-data-loss and reconstructibility gaps. Worth doing before any non-engineer audience evaluates the system.
4. **The remaining items as ongoing engineering work.** None individually justifies a milestone; collectively they harden the path.

This sequencing is not a roadmap commitment — it is the engineering order if and when each item is picked up. Roadmap dates live in `docs/roadmap.md`.
