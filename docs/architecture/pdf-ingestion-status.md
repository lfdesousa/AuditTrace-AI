# PDF ingestion robustness — work-in-progress status

**Companion to:** [`pdf-ingestion-gaps.md`](pdf-ingestion-gaps.md) (the *what*).
**Last updated:** 2026-05-08 (tier-B kickoff)
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
| **#12** | Signature validity (audit-grade provenance) | 🟨 **Code shipped, data gap** | PR #42 (`fdeb24e`) | `_pdf_signature_status` returns 7-class taxonomy. **Trust store empty in deployed image** → all SwissSign-signed docs flag `signed_invalid`. Backlog #13 deferred 2026-05-08 pending OOB SwissSign root verification. |
| **#18** | PDF bombs (availability) | ✅ **Shipped** | PR #42 (`fdeb24e`) | 4 layers: byte cap (`pdf_max_size_mb`), page cap (`pdf_max_pages`), xref cap (`pdf_max_xref_count`), per-page text cap + wall-clock budget (`pdf_parse_timeout_seconds`). |

**Tier-A summary:** code-complete; `#12`'s data gap is the only residual, deferred to backlog #13.

---

## Tier-B — silent-data-loss + reconstructibility

| # | Item | Status | Shipped where | Notes |
|---|---|---|---|---|
| **#1** | OCR for scanned pages | ⏳ **Pending** | (tier-B) | Tesseract `eng+deu+fra+ita`. `text_source: native\|ocr\|mixed` chunk metadata. Adds ~200 MB to image. |
| **#6** | Embedded attachments (PDF/A-3, e-invoicing) | ⏳ **Pending** | (tier-B) | Quarantine to MinIO under `episodic/<doc>/attachments/<name>`; manifest field. Recursion bound = 1 level. |
| **#7** | AcroForm field values | ⏳ **Pending** | (tier-B) | `page.widgets()` extraction; per-section chunking with `chunk_type: "form_field"`. Populates `form_field_count` manifest column. |
| **#15** | Encrypted / password-protected PDFs | ⏳ **Pending** | (tier-B) | `doc.is_encrypted` / `doc.needs_pass` detection → HTTP 422 reject. **No password-bearing endpoint** (sanitisation surface). |
| **#21** | Per-chunk provenance | 🟨 **8/9 fields shipped** | PR #42 (`fdeb24e`) | Shipped: `bbox_{x0,y0,x1,y1}`, `text_source` (default `native`), `extraction_confidence` (default `1.0`), `document_hash` (SHA-256), `signature_status`, `redaction_status`, `ingested_by_user_id`, `ingestion_ts_ms`. Residual: `text_source` flip to `ocr`/`mixed` lands with #1. |
| **#22** | Document-level manifest columns | ⏳ **Pending** | (tier-B) | Alembic migration 010 adds: `page_count`, `signature_status`, `ocr_coverage_pct`, `pdfa_conformance`, `scan_verdict`, `attachment_count`, `form_field_count`, `extraction_warnings (jsonb)`, `document_sha256` to `memory_items`. Surfaced via `GET /memory/episodic/{filename}`. |

**Tier-B target:** all 5 fully pending items + the OCR-driven #21 residual, one ADR-050 covering decisions, single PR `feat/tier-b-pdf-robustness`.

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
- **Tier-B:** 1/6 partial (#21 8/9 fields), 5/6 pending — **today's target: ship all 6 to ✅ or 🟨**
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
