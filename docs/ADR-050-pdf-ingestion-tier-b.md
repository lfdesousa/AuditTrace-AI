# ADR-050 — PDF ingestion robustness, tier-B

**Status:** Accepted
**Date:** 2026-05-08
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-048 (Proposed — content-control gate; security half), ADR-049 (Test, Evidence, and Reconstructibility Gate), [pdf-ingestion-gaps.md](architecture/pdf-ingestion-gaps.md), [pdf-ingestion-status.md](architecture/pdf-ingestion-status.md), tier-A PR #42 (signature_status + redactions + bombs), backlog #13 (deferred trust-store provisioning).

## Context

Tier-A (PR #42, 2026-05-07) closed the **security and audit-grade**
half of PDF ingestion: unflattened redactions (#8), signature presence
+ validity taxonomy (#12), PDF bombs (#18), and 8 of the 9 per-chunk
provenance fields (#21).

Tier-B is the **silent-data-loss + reconstructibility** half. Five
gap-inventory items, plus the residual #21 OCR-driven `text_source`
flip:

| # | Gap | Failure today |
|---|---|---|
| #1 | OCR for scanned pages | Pages without a text layer return `""` and are skipped silently — a 200-page scanned PDF ingests as 0 chunks with no manifest signal |
| #6 | Embedded attachments | PDF/A-3 attachments (ZUGFeRD, court evidence bundles) ignored entirely — silent data loss |
| #7 | AcroForm field values | Filled forms (tax / medical / regulatory) lose their data — `get_text()` doesn't read widgets |
| #15 | Encrypted PDFs | `pymupdf.open` succeeds but `get_text()` returns empty until `authenticate(password)` — silently produces 0 chunks |
| #22 | Document-level manifest | One `MemoryItem` per file with no provenance fields — auditors cannot ask "what was indexed" without scanning every chunk |

The gap inventory documents the *what*. This ADR records the *decisions*.

## Decision

Adopt the following decisions for tier-B, shipped as one PR (`feat/tier-b-pdf-robustness`) with one Alembic migration (010), one ADR (this file), one status-doc update per item.

### #15 — Encrypted PDFs: refuse, don't decrypt

**Decision:** `doc.is_encrypted` + `doc.needs_pass` checked **before** any text extraction. Encrypted documents are refused with `extraction_warnings += [{"code": "encrypted", "page": null}]`, the manifest's `extraction_warnings` JSONB carries the warning, and the document produces zero chunks. **No password-bearing endpoint** is exposed.

**Why refuse over unlock:**
1. A `password` query / form parameter ends up in access logs, request traces, and (worst case) Langfuse spans. Sanitising that surface forever is more work than refusing.
2. The operator who has the password can produce a decrypted copy themselves; supporting password-bearing upload optimises for an unlikely workflow at lasting cost.
3. Audit-grade systems should not normalise "we decrypt your documents on the way in." Refusing is the more defensible default; an explicit `?password=…` mode could be added later if a real customer needs it.

### #22 — Document-level manifest: extend `memory_items`, don't create a sibling

**Decision:** Migration 010 adds **nullable** columns to the existing `memory_items` table:

```
page_count            INTEGER             NULL
signature_status      VARCHAR(32)         NULL
ocr_coverage_pct      REAL                NULL
attachment_count      INTEGER             NULL  DEFAULT 0
form_field_count      INTEGER             NULL  DEFAULT 0
extraction_warnings   JSONB               NULL  DEFAULT '[]'::jsonb
document_sha256       CHAR(64)            NULL
```

**Why extend not split:**

1. The "memory item" abstraction is the right one — a PDF *is* a memory item, not a separate entity. Splitting would require joins on every list endpoint.
2. Nullable columns are backward-compatible with existing rows. No backfill migration required.
3. Callers that don't care about PDF-specific fields ignore them; the existing `ManifestEntry` dataclass gets the new fields via `Optional[...]` and old code paths are unchanged.
4. The fields named in the gap inventory (`pdfa_conformance`, `scan_verdict`) are deferred to tier-C / ADR-048-v1 respectively — adding columns we won't populate is dead weight.

**Surfaced via** `GET /memory/episodic/{filename}` — the existing list/get endpoints carry the new fields when populated.

### #7 — AcroForm field values: per-page chunk, not per-field

**Decision:** When a page has widgets, accumulate `(field_name, field_label, field_value, field_type)` tuples in reading order, render to text as `Label: Value` lines, emit **one chunk per page** carrying the form data with `chunk_type: "form_field"` metadata. The page's normal `get_text()` content is also chunked separately if non-empty.

**Why per-page over per-field:**
1. A field's `field_value` is meaningless without surrounding context. "Yes" / "No" / a date alone cannot be embedded usefully — the field label provides the semantic anchor.
2. Single-field chunks would explode the chunk count on dense forms (regulatory submissions can have 200+ fields).
3. Per-page matches the existing PDF chunking shape (already per-page); the new chunks land alongside text chunks naturally.

**`form_field_count` manifest column** is the document-level count.

### #6 — Embedded attachments: quarantine, don't index inline

**Decision:** Iterate `doc.embfile_count()` / `embfile_get(i)`; for each attachment:

1. Compute `sha256(bytes)`.
2. Write to MinIO at `{layer}/{parent_filename}/attachments/{attachment_filename}` (parent prefix preserved for traceability).
3. Append `{name, mime, size, sha256, minio_key}` to the parent document's `extraction_warnings` (attachment metadata is *audit signal*, not error — the warnings list is mis-named here, will rename to `extraction_notes` if it becomes confusing).
4. Increment `attachment_count`.

**Recursion bound: 1 level.** A PDF embedded inside a PDF gets its `extraction_warnings` recorded but is not itself parsed.

**Why quarantine over index inline:**
1. Most attachments are XML / structured payloads (ZUGFeRD invoices, evidence bundles). Naive text-indexing them produces garbled embeddings.
2. The operator can later run `/memory/index?file=<attachment-key>` against the quarantined attachment if it's a PDF / Markdown / something the indexer understands.
3. Quarantine preserves the data without polluting the parent document's chunk space.

**Why MinIO not blob in Postgres:**
1. Attachments can be large (10s of MB). The manifest table should not become a blob store.
2. MinIO's existing per-layer prefix structure handles this naturally.
3. The `attachments/` sub-prefix is already per-document; no additional naming scheme to design.

### #1 — OCR for scanned pages: Tesseract, page-by-page rendering, eng+deu+fra+ita

**Decision:** When `page.get_text().strip() == ""` AND `page.get_images()` is non-empty:

1. Render the page to a 300-DPI PNG via `page.get_pixmap(dpi=300)`.
2. Feed the PNG to `pytesseract.image_to_data(...)` with languages `eng+deu+fra+ita` (the four CH languages).
3. Concatenate the recognised text in reading order; record per-chunk `text_source: "ocr"` and `extraction_confidence: <Tesseract's mean per-word confidence / 100>`.
4. Increment a per-document `ocr_pages` counter; emit `ocr_coverage_pct = ocr_pages / page_count * 100` to the manifest.
5. If OCR also returns empty text on a raster-bearing page, append `{"code": "no_text_layer", "page": page_num}` to `extraction_warnings`. Never silently skip.

**Why Tesseract over a hosted model:**
1. Sovereign / on-prem posture — see ADR-041. Hosted OCR contradicts the data-residency claim.
2. Tesseract is mature, free, supports 100+ languages.
3. Quality is sufficient for the legal / regulatory document classes we target. Marketing copy / glossy magazines would benefit from a hosted alternative; we do not target them in v1.

**Why eng+deu+fra+ita specifically:**
1. Three of the four are Switzerland's national languages; English is the universal lingua franca of business / regulatory documents.
2. Each language pack is ~10 MB; the four together add ~40 MB to the image (the larger ~200 MB image-bloat figure cited in the gap inventory was conservative — measured during this PR's image build, the actual delta is ~65 MB including `tesseract-ocr` + the 4 language packs + dependencies).
3. Adding more languages later is one Dockerfile line; keeping the default minimal preserves cold-start time.

**Why 300 DPI:**
1. Tesseract's recommendation for scanned-document accuracy. Below 200 DPI quality drops noticeably; above 400 DPI the speedup-vs-quality curve flattens.
2. Memory cost is bounded — a single 300-DPI A4 page is ~3 MB raster. With the existing `pdf_max_pages` cap (default 2000), worst-case OCR memory is ~6 GB across the lifetime of one document but only ~3 MB resident at any time (we render and free per page).

### Cross-cutting: extraction_warnings JSONB schema

`extraction_warnings` is a JSONB array. Each entry is one of:

```json
{"code": "encrypted",          "page": null}
{"code": "no_text_layer",      "page": 42}
{"code": "ocr_low_confidence", "page": 42, "confidence": 0.31}
{"code": "attachment",         "name": "invoice.xml",
                                "mime": "application/xml",
                                "size": 14336,
                                "sha256": "…",
                                "minio_key": "episodic/main.pdf/attachments/invoice.xml"}
{"code": "max_size",           "size_bytes": 234567890,
                                "cap_bytes": 209715200}
{"code": "max_pages",          "page_count": 99999, "cap": 2000}
{"code": "parse_timeout",      "pages_processed": 137}
{"code": "redaction_clipped",  "page": 11, "redaction_count": 2}
{"code": "redaction_rejected", "page": 11, "redaction_count": 2}
```

**Why JSONB over a normalised side-table:**
1. Most documents have ≤3 warnings; a side-table joins for nothing.
2. Postgres JSONB is queryable (`extraction_warnings @> '[{"code": "ocr_low_confidence"}]'`). An auditor's "show me everything that needed OCR" query is one operator.
3. The shape evolves; a side-table commits to columns prematurely.

The shape is **closed-set** for `code`: only the values listed above are allowed. The route layer asserts this; new codes need an ADR amendment.

### Cross-cutting: PR shape

Single PR (`feat/tier-b-pdf-robustness`) with one commit per item plus a final commit for the live-evidence INDEX. Tier-A's mega-PR (#42) review burden was acceptable; tier-B is comparable scope.

### Out of scope (deferred)

- **#13 LTV data + #14 PDF/A conformance + #11 explicit signature presence** — defer to tier-C or a later signature-focused ADR. Today's #15 / #22 / #7 / #6 / #1 cluster is already large.
- **`pdfa_conformance` manifest column** — deferred until tier-C ships #14.
- **`scan_verdict` manifest column** — deferred until ADR-048 v1 ships the scanner pod. Adding a column we can't populate is noise.

## Consequences

### Positive

- Five tier-B gaps closed in one PR. Test-and-evidence gate satisfied per item.
- The `extraction_warnings` JSONB becomes the single audit-pivot for "what happened to this document."
- `attachment_count` + `form_field_count` give operators a one-row answer to "did we lose anything in this filing." Without them, today's only signal is "no chunks were indexed; was that because the file is empty or because everything got dropped silently?" — a real anti-feature.
- OCR closes the most-cited silent-data-loss case. After tier-B, any reviewer scanning a 200-page legal PDF and getting 0 chunks knows it's because the file is genuinely blank, not because we dropped it.

### Negative

- Image size grows ~65 MB (Tesseract + 4 language packs). Cold-start unchanged; pull time on first-run cluster slightly longer.
- `extraction_warnings` is loosely typed at the column level (JSONB). The closed-set discipline lives in code; a sloppy commit could introduce a new code value.
- Tier-A's `redaction_status` chunk metadata field is now duplicated by `extraction_warnings` (a clipped page emits both `redaction_status: "clipped"` chunk-meta AND `extraction_warnings += [{"code": "redaction_clipped", ...}]`). Decided to keep both: the chunk-meta is queryable per chunk in ChromaDB; the doc-level warnings are queryable per document in Postgres. They serve different audit pivots.

### Risks

- **OCR quality on adversarial inputs.** Tesseract is good; not perfect. A document with 30 % low-confidence OCR text will still produce embeddings, just lower-quality ones. The `extraction_confidence` chunk-meta + `ocr_low_confidence` warning let auditors filter; nothing prevents the embedding from being computed in the first place. Acceptable trade-off in v1.
- **Encrypted-document refuse-rate.** Some regulatory PDFs ship encrypted by default with a public password (literally "0000" or the document's own ID). Our refuse posture means those need decryption out-of-band before upload. Operator-friction we accept.
- **JSONB column closed-set drift.** The pre-commit gate doesn't enforce the warning code enum. A test (`tests/test_extraction_warnings_codes.py`) asserts the set against a frozen list — fail-fast for new codes added without ADR amendment.

## Validation per the gate (ADR-049 §Decision)

1. **Verification:** unit tests for each of #1, #6, #7, #15, #22, plus the new `extraction_warnings` schema test. Per-file coverage gate ≥ 90 %.
2. **Validation:** image rebuilt with Tesseract; helm rolled out; live `/memory/index?file=episodic/main_signed.pdf` (already-known fixture) returns the new manifest fields populated. Plus targeted fixtures for each item — encrypted PDF rejected, AcroForm yields chunks, attachment quarantined, scanned PDF OCR'd.
3. **Reconstruction:** `~/work/audittrace-evidence/2026-05-08-tier-b/INDEX.md` — chunk + manifest queries per item, image tag, helm revision.
4. **No override:** every item carries its own evidence row; no item ships without it.

## Update protocol for the status doc

`docs/architecture/pdf-ingestion-status.md` MUST be edited in the same commit that ships an item. The PR body's Reconstruction section references the status-doc diff alongside the cluster evidence. This is the audit pivot: a reviewer can read the status doc and see exactly what's done.
