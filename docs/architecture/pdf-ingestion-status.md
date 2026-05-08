# PDF ingestion robustness — work-in-progress status

**Companion to:** [`pdf-ingestion-gaps.md`](pdf-ingestion-gaps.md) (the *what*).
**Last updated:** 2026-05-09 (ADR-052 + ADR-053 — full EU + CH trust-store coverage shipped)
**Maintainer note:** update on every commit that ships or partially ships a gap-inventory item. The gap inventory describes the failure mode; this file records what's been done about it.

---

## Status legend

- ✅ **Shipped** — code on `main`, live evidence captured, item closed
- 🟨 **Partial** — meaningful work shipped, residual scope on a follow-up
- 🔄 **In progress** — open PR or active branch
- ⏸️ **Deferred** — work paused with an explicit resume trigger
- ⏳ **Pending** — not started

---

## Sequencing batches

The gap inventory's §3 "Sequencing" defines four batches in dependency order. This table is the source of truth for which batch each item belongs to.

| Batch | Items | Dependency posture |
|---|---|---|
| **ADR-048 prereq** | (no inventory items — gate + scanner pod) | Hard prereq for any external user upload of PDFs |
| **Tier-A** (security / audit-grade) | #8, #12, #18 | Cluster of three ADR-048 doesn't close |
| **Tier-B** (silent-data-loss + reconstructibility) | #1, #6, #7, #15, #21, #22 | After tier-A; before non-engineer audience |
| **Tier-C** (rest) | #2, #3, #4, #5, #9, #10, #11, #13, #14, #16, #17, #19, #20, #23, #24, #25 | Ongoing engineering; none individually justifies a milestone |

---

## ADR-048 prereq

| Status | What | Where |
|---|---|---|
| 🔄 **Proposed** | Content-control gate + scanner pod (untrusted bytes never reach memory-server) | `docs/ADR-048-…md` (Proposed) |

Until v1 ships, no external user uploads of PDFs are accepted into the indexable path. This is a security-posture floor.

---

## Tier-A — security / audit-grade

| # | Item | Status | Shipped where | Notes |
|---|---|---|---|---|
| **#8** | Annotations / unflattened redactions (confidentiality) | ✅ **Shipped** | PR #42 (`fdeb24e`) | `pdf_redaction_policy = "reject" \| "clip-extract"`. Default reject. |
| **#12** | Signature validity (audit-grade provenance) | ✅ **Shipped** | PR #42 (`fdeb24e`) + ADR-052 PR (combined: taxonomy split + trust-store provisioning) + ADR-053 PR (Swiss federal TSL + composite builder) | `_pdf_signature_status` returns **8-class taxonomy** (per ADR-052 §1) — `signed_invalid` (math broken) split from new `signed_untrusted` (chain doesn't terminate at our trust roots) so configuration gaps stop poisoning the audit signal. Trust store provisioned via a `CompositeTrustStoreBuilder` chaining `EuLotlTrustStoreBuilder` (~887 EU eIDAS qualified TSPs) + `SwissTslTrustStoreBuilder` (Swiss federal TSL via OFCOM/BAKOM — incl. SwissSign + Swisscom Trust Services). Pluggable Provider/Builder ABCs in `services/trust_store.py` per ADR-052 §2-3 + ADR-053 §1-2. Live evidence 2026-05-09: EU-recognised `Luis_Research_Proposal_signed.pdf` + Swiss-recognised `main_signed.pdf` both flip to `signed_valid` end-to-end through `POST /system/trust-store/refresh` (scope `audittrace:admin`). TSLO signing cert vendored OOB-verified (SHA-1 `e8638362…261b137f`). Backlog #13 fully closed. |
| **#18** | PDF bombs (availability) | ✅ **Shipped** | PR #42 (`fdeb24e`) | 4 layers: byte cap (`pdf_max_size_mb`), page cap (`pdf_max_pages`), xref cap (`pdf_max_xref_count`), per-page text cap + wall-clock budget (`pdf_parse_timeout_seconds`). |

**Tier-A summary:** code-complete; `#12`'s data gap is being closed by ADR-052 PR 3 (EU LOTL trust-store provisioning, Layer 1+2 — SwissSign + EU eIDAS). Taxonomy split (ADR-052 PR 2) lands first so `signed_untrusted` is available as the honest intermediate-state signal.

---

## Tier-B — silent-data-loss + reconstructibility

| # | Item | Status | Shipped where | Notes |
|---|---|---|---|---|
| **#1** | OCR for scanned pages | ✅ **Shipped** | tier-B PR (`feat/tier-b-pdf-robustness`) | Tesseract `eng+deu+fra+ita` (~65 MB image delta), 300 DPI per-page render, `text_source` ∈ {`native`,`ocr`,`form_field`}, per-page `extraction_confidence` from Tesseract's mean-per-word. `ocr_coverage_pct` populated on the manifest. Graceful degradation when Tesseract binary missing → `no_text_layer` warning. `pdf_ocr_enabled` / `pdf_ocr_languages` / `pdf_ocr_dpi` settings. |
| **#6** | Embedded attachments (PDF/A-3, e-invoicing) | ✅ **Shipped** | tier-B PR | `embfile_count` / `embfile_get` extraction; quarantine to MinIO at `{layer}/{parent}/attachments/{name}`. Recursion bound = 1. Sanity cap = 256 attachments per doc. `attachment` + `attachment_quarantine_failed` warnings; `attachment_count` manifest column. |
| **#7** | AcroForm field values | ✅ **Shipped** | tier-B PR | `page.widgets()` extraction; `Label: Value` lines emitted as one form-field chunk per page (chunk_type=`form_field`, text_source=`form_field`). Empty fields skipped. `form_fields` warnings; `form_field_count` manifest column. |
| **#15** | Encrypted / password-protected PDFs | ✅ **Shipped** | tier-B PR | `is_encrypted` ∧ `needs_pass` strict-bool detection → 0 chunks emitted, manifest row written with `extraction_warnings += [{"code":"encrypted"}]`. **No password-bearing endpoint** (per ADR-050 §#15 — operator decrypts out-of-band). |
| **#21** | Per-chunk provenance | ✅ **Shipped (9/9)** | PR #42 (`fdeb24e`) + tier-B PR | tier-A shipped 8/9 fields; tier-B closes the residual: `text_source` now flips to `"ocr"` / `"form_field"` and `extraction_confidence` carries Tesseract's mean-per-word on OCR pages (was always `1.0` in tier-A). New `chunk_type` field disambiguates form-field chunks from text chunks. |
| **#22** | Document-level manifest columns | ✅ **Shipped** | tier-B PR — Alembic migration 010 | Added to `memory_items`: `page_count`, `signature_status`, `ocr_coverage_pct`, `attachment_count` (default 0), `form_field_count` (default 0), `extraction_warnings` (JSONB on Postgres / JSON on SQLite, default `[]`), `document_sha256`. Surfaced via the existing per-layer endpoints (the `ManifestEntry` dataclass carries the new fields). `pdfa_conformance` + `scan_verdict` deferred per ADR-050. GIN index on `extraction_warnings` for the audit-pivot query `WHERE extraction_warnings @> '[{"code":"…"}]'`. |

**Tier-B summary:** 6/6 ✅ shipped. ADR-050 records the design decisions. `extraction_warnings` JSONB closed-set enum (13 codes) is the single audit pivot; `tests/test_memory_routes.py::TestExtractionWarningCodes` pins the set so a code added without ADR amendment fails CI.

---

## Tier-C — rest

| # | Item | Status |
|---|---|---|
| #2 | Mixed text/image extraction | ⏳ Pending |
| #3 | Tables lose structure | ⏳ Pending |
| #4 | Multi-column reading order | ⏳ Pending |
| #5 | RTL / mixed-direction handling | ⏳ Pending |
| #9 | Bookmarks / TOC for chunking | ⏳ Pending |
| #10 | Document metadata (title, author, dates) | ⏳ Pending |
| #11 | Signature presence detection | ⏳ Pending (subsumed by #12 already) |
| #13 | LTV (Long-Term Validation) data | ⏳ Pending |
| #14 | PDF/A conformance level | ⏳ Pending |
| #16 | Corrupted / truncated files | ⏳ Pending |
| #17 | Hybrid / linearised PDFs | ⏳ Pending |
| #19 | Per-page memory growth | ⏳ Pending |
| #20 | Embedding throughput | ⏳ Pending |
| #23 | Dry-run / preview mode | ⏳ Pending |
| #24 | Per-document audit-trail granularity | ⏳ Pending |
| #25 | Surgical re-index | ⏳ Pending |

---

## Snapshot — quick numbers

- **ADR-048 prereq:** 0 % (Proposed)
- **Tier-A:** 100 % code shipped, 1 data-side gap (backlog #13)
- **Tier-B:** **6/6 ✅ shipped** (ADR-050 + tier-B PR, 2026-05-08)
- **Tier-C:** 0/16 shipped

---

## Update protocol

When a tier-B / tier-C item ships:

1. Edit the relevant row's **Status** + **Shipped where** columns.
2. Update the "Last updated" date at the top.
3. Update the snapshot count.
4. The commit that ships the code MUST also touch this file. The
   pre-commit gate doesn't enforce that, but the test-and-evidence
   discipline does — the PR body's `Reconstruction` section should
   reference this file's diff alongside the ChromaDB / manifest
   evidence.
