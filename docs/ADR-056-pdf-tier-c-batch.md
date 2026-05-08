# ADR-056 — PDF tier-C batch (#9 TOC, #10 metadata, #11 close, #13 LTV, #14 PDF/A, #16 corrupted-file taxonomy, #23 dry-run, #24 per-document audit granularity, #25 file-level surgical reindex)

**Status:** Accepted
**Date:** 2026-05-09
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-050 (PDF tier-B), ADR-052 (PAdES taxonomy + trust store), ADR-054 (as-of-signing-time), [`pdf-ingestion-gaps.md`](architecture/pdf-ingestion-gaps.md), [`pdf-ingestion-status.md`](architecture/pdf-ingestion-status.md).

## Context

Tier-A (audit-grade signature + bombs + redactions) and tier-B (silent-data-loss + reconstructibility) are complete. The remaining gap-inventory items in tier-C are the "operator UX + data-quality" cluster — none individually justifies its own ADR; collectively they harden the operator-facing path that PR3/PR4/Bruno+WebUI surfaced today.

This ADR batches the three highest-leverage tier-C items into one PR:

- **#10** — Document-level metadata extraction (title, author, dates) into manifest columns. Trivial; pymupdf already exposes `doc.metadata`.
- **#16** — Corrupted-file extraction-warning taxonomy. Currently malformed PDFs fall through to the generic `check_failed` signature status with the underlying exception logged but not classified. Auditors can't query "show me all docs that failed for THIS class of reason."
- **#24** — Per-document granularity in the `/memory/index` response. Today operators get `{collections: {col: count}, total_chunks, duration_s}` — a per-collection summary. To answer "did MY upload work? what's its signature_status?" they have to grep server logs or follow up with `GET /memory/episodic`. Highest operator-UX leverage of the three.

## Decision

Three changes, one PR, one Alembic migration (011), one ADR. Targets v1.0.17 via `make release`.

### #1 — Document metadata extraction (gap-inventory #10)

**Decision.** Migration 011 adds four nullable columns to `memory_items`:

```
pdf_title             VARCHAR(255)  NULL
pdf_author            VARCHAR(255)  NULL
pdf_creator           VARCHAR(255)  NULL
pdf_creation_date     TIMESTAMPTZ   NULL
```

Populated during `_index_pdf_objects` by reading `doc.metadata` (a `dict`) from pymupdf. Empty / missing fields stay `NULL`. Surfaced via the existing per-layer GET endpoints (the `ManifestEntry` dataclass gets the four fields).

**Why these four, not the full pymupdf set:**

pymupdf's `doc.metadata` exposes 9 keys (`title, author, subject, keywords, creator, producer, creationDate, modDate, format`). Four of those are load-bearing for audit:

| Field | Why included |
|---|---|
| `title` | Human-readable identifier; auditors use it in queries + dashboards. |
| `author` | Provenance — who wrote / signed the document. Pairs with `signature_status`. |
| `creator` | The application that produced the PDF (e.g. "Microsoft Word", "SwissSign Web"). Useful for fraud signal — unexpected creator on a Swiss-signed legal doc raises flags. |
| `creation_date` | Anchors the document in time. Pairs with `signature_status="signed_expired"` (created when?) and the signature's `self_reported_timestamp`. |

The other five (`subject`, `keywords`, `producer`, `modDate`, `format`) are low-leverage:
- `subject` / `keywords` are author-supplied free text; rarely populated reliably.
- `producer` typically duplicates `creator`.
- `modDate` is unreliable (any tool modifying the PDF can rewrite it).
- `format` is always the PDF version; not audit-relevant.

A future ADR can extend the column set if a customer asks; the migration shape (nullable VARCHARs + a TIMESTAMPTZ) is forward-compatible.

**Date parsing:** PDF dates use the `D:YYYYMMDDHHMMSS+HHMM` format (PDF 1.7 §7.9.4). pymupdf returns them as strings; we parse to `datetime` via a small helper. Unparseable dates fall through to NULL with a `pdf_metadata_parse_error` extraction warning.

### #2 — Corrupted-file extraction-warning taxonomy (gap-inventory #16)

**Decision.** Add three new closed-set codes to `_PDF_WARNING_CODES`:

```
pdf_corrupted_xref        # pymupdf raised on xref walk (truncated, malformed)
pdf_corrupted_structure   # generic structural parse error (unrecognised dict, etc.)
pdf_metadata_parse_error  # doc.metadata yielded malformed values (date, encoding)
```

Updated `_index_pdf_objects` exception handler classifies pymupdf raises into the new codes via type-matching on the exception class name + message substring. Falls through to a generic `pdf_extraction_error` for unclassified raises (not a closed-set code — escalates to an info-level log so we can refine the taxonomy if a real-world failure mode lands a new shape).

Each classification appends to `extraction_warnings` (the existing JSONB column) and continues processing the next file. Doesn't change the signature_status logic; signature validation runs *after* extraction succeeds.

**Why three codes not one:**

`pdf_corrupted_xref` is the most common malformation (truncated downloads, mid-write copies). `pdf_corrupted_structure` covers everything else pymupdf can't parse. `pdf_metadata_parse_error` is the metadata-specific subclass introduced in #1 — kept distinct so an audit query for "which uploads have unreliable provenance" surfaces them without dragging in the broader corruption set.

The closed-set discipline is preserved: `tests/test_memory_routes.py::TestExtractionWarningCodes` extends to assert the new codes; future additions still need an ADR amendment.

### #3 — Per-document audit granularity (gap-inventory #24)

**Decision.** `POST /memory/index` gains an optional `?details=true` query parameter. Default behaviour (omitted or `false`) returns the existing shape unchanged:

```json
{
  "status": "indexed",
  "collections": {"col_name": <chunk_count>},
  "total_chunks": <int>,
  "duration_s": <float>
}
```

When `details=true`, the response gains a `documents` array:

```json
{
  "status": "indexed",
  "collections": {"col_name": 46},
  "total_chunks": 46,
  "duration_s": 12.55,
  "documents": [
    {
      "file": "episodic/main_signed.pdf",
      "chunks": 46,
      "signature_status": "signed_expired",
      "page_count": 23,
      "extraction_warnings": [],
      "document_sha256": "49aadc5e7cc103cb…",
      "ok": true,
      "error": null
    },
    {
      "file": "episodic/corrupt.pdf",
      "chunks": 0,
      "signature_status": null,
      "page_count": null,
      "extraction_warnings": [{"code":"pdf_corrupted_xref","page":null}],
      "document_sha256": null,
      "ok": false,
      "error": "pymupdf raised on xref walk"
    }
  ]
}
```

**Why query parameter, not a new endpoint:**

1. **Backwards compatibility.** All existing callers (Bruno, scripts, the WebUI) keep working unchanged. Opt-in detail level matches the OpenAI / Stripe / many enterprise APIs' pattern (e.g. `?expand=`, `?include=`).
2. **Same auth + scope contract.** Per-document detail is the same surface from a permissions standpoint — the caller already has `audittrace:admin` (the indexing route is admin-scoped). No new scope to bolt on.
3. **Response-size discipline.** The `documents` array can be large for bulk indexes; opt-in keeps the small clients lightweight.

**Why include both `ok: bool` and `error: str | null`:**

Auditors filter for `ok=false` (a bool) faster than they parse error strings. `error` carries the human-readable cause for debug. Both fields paired is a minor redundancy with strong UX payoff.

### #4 — Update protocol

When this PR lands:

- Update `docs/architecture/pdf-ingestion-status.md` tier-C rows for #10, #16, #24 → ✅ Shipped.
- Update the Bruno collection's `index-single-file.bru` to add a sibling `index-single-file-detailed.bru` exercising `?details=true`.
- WebUI's index-trigger button (if/when added) will set `details=true` so per-document outcomes render in the UI.

## Consequences

### Positive

- **Operator UX.** `/memory/index?details=true` answers "did my upload work?" in one request instead of a re-list cycle. Highest-leverage of the three.
- **Audit-trail richness.** `pdf_title` / `pdf_author` / `pdf_creator` / `pdf_creation_date` give auditors human-readable identifiers without dragging the PDF bytes back through their pipeline.
- **Closed-set discipline preserved.** New corruption codes pin to the existing test class; future drifts surface in CI.
- **No breaking change.** Default response shape unchanged; existing callers (Bruno, WebUI, scripts) keep working.

### Negative

- **Migration adds 4 nullable columns.** Backward-compatible; rows from before tier-C land with NULLs. No backfill required.
- **`details=true` response can be large** for bulk indexes (the bulk path has been deprecated in operator workflow, but still supported). Mitigated by the opt-in design — small clients pay nothing.
- **Date parsing surface area.** PDF dates are a known mess (§7.9.4 + de-facto vendor dialects). Defensive — parse failure is a warning, not a fatal.

### Risks

- **`creator` field as fraud signal** is a soft signal. Someone forging a SwissSign-style PDF can also lie about the creator. The audit value is "anomaly detection across a corpus" not "individual-document authenticity." Documented in the column comment.
- **Per-document response shape stability.** Adding fields to `documents[]` entries in future iterations is fine (additive); removing fields would be a breaking change that needs an ADR amendment.

## Validation per the gate (ADR-049 §Decision)

| Verification | Validation | Reconstruction |
|---|---|---|
| `make test` green; per-file gate ≥90% on `routes/memory.py` + `services/memory_manifest.py`; `TestExtractionWarningCodes` extended to 16 codes (was 13); new test for `details=true` shape; new test for metadata extraction. | E2E: deploy v1.0.17; re-index `main_signed.pdf` with `?details=true` — expect `documents[0]` carrying the new metadata fields + `signature_status="signed_expired"` + `ok=true`. Plus `GET /memory/episodic` shows the new metadata columns on the manifest row. | Postgres `memory_items` row carries `pdf_title`, `pdf_author`, `pdf_creator`, `pdf_creation_date`. ChromaDB chunks unchanged (per-chunk metadata already had what they need). |

## Additional items shipped under this ADR

This ADR expanded mid-iteration to bundle six more low-friction tier-C items rather than queue them as separate ADRs. The shape is consistent with the original three: small migration footprint, additive API surface, no breaking change.

### #11 — Signature presence detection

**Decision.** Closed as a documentation cross-reference. The 9-class taxonomy from ADR-052 + ADR-054 already answers "is this signed?" via `none` vs `signed_*`. Item #11 from the gap inventory was a duplicate of #12; we keep the row in `pdf-ingestion-status.md` but mark it as subsumed.

### #14 — PDF/A conformance level

**Decision.** Migration 011 adds two short string columns:

```
pdfa_part         VARCHAR(4)  NULL
pdfa_conformance  VARCHAR(4)  NULL
```

`pdfa_part` is `1` / `2` / `3` / `4` (per ISO 19005-1..-4); `pdfa_conformance` is `A` / `B` / `U`. Populated from the XMP packet's `pdfaid:` namespace via `_extract_pdfa_conformance`. Both NULL means "not a PDF/A document" or "XMP missing". Two columns, not one combined value, so audit queries can filter on either independently (e.g. `WHERE pdfa_part = '3'` for ZUGFeRD invoices).

**Why two columns not one combined `pdfa_3b`-style string:** auditors want to filter on conformance level (`B` = visual reproducibility) and part (`3` = ZUGFeRD-capable) independently. Combining them forces every query to parse the suffix.

### #13 — LTV (Long-Term Validation) data

**Decision.** Migration 011 adds one JSONB column:

```
ltv_data  JSONB  NULL
```

Populated by `_summarize_ltv()` once per file. Contains a flat audit-pivot summary of the DSS dictionary:

```json
{
  "has_dss": true,
  "ocsp_responses": 2,
  "crls": 1,
  "certs": 5,
  "timestamps": 1,
  "vri_keys": 2
}
```

NULL on unsigned PDFs and on signed PDFs without a DSS dictionary. The full ASN.1 (certs, OCSP responses, CRLs) stays in the source PDF — we don't duplicate the certificate store; this column is the index, not the data.

**Why summary not full DSS:** a DSS dictionary on a PAdES-LTV-A document can carry hundreds of OCSP responses + CRLs across signatures. Persisting the full ASN.1 in Postgres adds ~50-200 KB per document with low query value (you re-validate from the source PDF, not from the manifest copy). The summary is enough for the audit pivot question "does this signature have long-term-validation evidence we could re-validate years from now?" — yes/no plus rough counts.

### #9 — TOC-aware chunk metadata

**Decision.** Per-chunk ChromaDB metadata gains an additive `toc_section` field carrying the title of the most-recent TOC entry that started at or before this page. Forward-fill semantics: page 7 of a doc whose TOC has entries `Introduction (page 1)` and `Methods (page 5)` carries `toc_section="Methods"`; pages before the first TOC entry leave `toc_section` absent (key omitted, not set to None — ChromaDB rejects None metadata).

`_build_toc_index()` returns `{page_1based: title}` from `doc.get_toc(simple=True)`. Multi-level TOCs collapse to the leaf title for now (a future iteration can synthesise breadcrumbs).

**Why metadata not chunk boundaries:** changing chunk boundaries to follow TOC sections would mean every existing PDF in the corpus needs re-chunking + re-embedding (chunk IDs are deterministic from page+chunk-index). That's a bigger rework than this iteration justifies. Adding the title as metadata gives 80% of the retrieval-quality win (the embedder doesn't care, but downstream filters do — "show me chunks from the Methods section") with zero migration cost.

### #23 — Dry-run mode

**Decision.** `POST /memory/index?dry_run=true` walks the full pipeline (read MinIO, extract text, run signature + metadata + LTV + TOC checks, classify warnings) but does NOT:

- Delete the ChromaDB collection (in bulk mode).
- Upsert chunks into ChromaDB.
- Write the manifest row to Postgres.

The response shape gains a `dry_run: true` field and `status` flips to `"dry_run"`. Pairs naturally with `?details=true` to surface the per-document outcome the writer would have produced — operators can preview every signature_status / extraction_warnings / metadata field before committing to the write.

**Why opt-in not a separate endpoint:** same auth contract, same code path, same response shape; only the side-effect set differs. A separate `/memory/index/dry-run` endpoint would force callers to remember which one runs the upsert — confusing and error-prone.

### #25 — File-level surgical re-index

**Decision.** Closed as already-supported. The existing `?file=<key>` mode (introduced in the per-file client loop pattern, 2026-05-06) IS the file-level surgical re-index: idempotent upsert of one file into one collection, leaves all other files untouched. Pair `?file=…` with `?dry_run=true` for the preview workflow.

**Chunk-level surgical reindex is deferred.** True per-chunk re-index would require: parse the source PDF, run extraction for the page that contains chunk_id X, re-embed just that chunk, upsert. Substantial rework (the chunker is page-deterministic but the chunk text is content-derived). Operators who need this today can `?file=<key>` to re-process the whole document; the per-page memory cost is bounded by the existing tier-A bombs gates.

## Update protocol

When this PR lands:

- Update `docs/architecture/pdf-ingestion-status.md` tier-C rows for #9 / #10 / #11 / #13 / #14 / #16 / #23 / #24 / #25 → ✅ Shipped.
- Update the Bruno collection's `index-single-file.bru` to add a sibling `index-single-file-detailed.bru` exercising `?details=true`. (Done.)
- A future iteration adds `index-single-file-dry-run.bru` exercising `?dry_run=true` so operators can spot the contract from the collection alone.

## Out of scope (deferred to future ADRs)

The following tier-C items remain pending. None blocks the M5 customer demo (2026-05-15); each will land as its own small ADR if a customer asks.

- **#2** — Mixed text/image extraction (partial-text pages where the figure is raster); current OCR only fires when the page has zero text layer.
- **#3** — Tables lose structure (single text run; no row/column relationships).
- **#4** — Multi-column reading order (two-column papers interleave when pymupdf's default reading order misfires).
- **#5** — RTL / mixed-direction handling (Hebrew / Arabic / mixed text).
- **#17** — Hybrid / linearised PDFs with combined XRefStream/XRefTable.
- **#19** — Per-page memory growth (bulk path is bounded by the per-file client loop; a single 200-page PDF still spikes mid-iteration).
- **#20** — Embedding throughput (page-by-page; batching would improve 3-5×, not a correctness gap).
- **Chunk-level surgical reindex** (#25 covers file-level only).
- **Full pymupdf metadata** (`subject`, `keywords`, `producer`, `modDate`, `format`); current four columns cover the audit-relevant set.
- **Bulk-mode `details=true` response truncation**; current shape includes every document. A future iteration may add `?limit=N` if bulk indexes start producing 10k+ document arrays.
- **TOC breadcrumbs** (multi-level Chapter / Section / Subsection composition); current implementation collapses to leaf title.
