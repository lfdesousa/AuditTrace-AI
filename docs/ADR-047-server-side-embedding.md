# ADR-047 — Move ChromaDB embedding off the request path

**Status:** Proposed
**Date:** 2026-05-06 (proposed)
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-027 (MinIO object storage), ADR-029 (`/interactions`
route + audit-record schema), ADR-046 (async chat-completion
persistence — same pattern, different consumer), and the same-day
PYTHON-ENGINEERING skill at
`~/work/claude-config/skills/PYTHON-ENGINEERING/SKILL.md` (lessons
that landed alongside this proposal — `feedback_use_context_managers`
and the singleton-embedder fix in `src/audittrace/services/embedder.py`).

## Context

The `/memory/index` route in `src/audittrace/routes/memory.py` builds
ChromaDB collections by calling `collection.upsert(documents=[...])`
with raw text chunks. ChromaDB's HttpClient mode then computes
embeddings **client-side** in the memory-server process, using its
default `ONNXMiniLM_L6_V2` model (sentence-transformers via ONNX
runtime; ~80 MiB model weights plus PyTorch / onnxruntime context).

This was discovered to be a leak in 2026-05-06 live evidence:
ChromaDB's stock `DefaultEmbeddingFunction.__call__` instantiates
`ONNXMiniLM_L6_V2()` on every call. After three hops on the
ai_research_papers per-file index loop, the pod OOMKilled at the 8
GiB chart-bounded limit. The same-day fix
(`src/audittrace/services/embedder.py`) introduces a module-level
singleton that loads the model once. **That contains the leak; it
does not address the architectural smell.**

The architectural smell:

1. **memory-server is a request-handling gateway, not an inference
   host.** It already runs FastAPI, sqlalchemy, MinIO/Redis/ChromaDB
   clients, OpenTelemetry, Vault Agent. Adding a 1.5–2 GiB
   resident-set ML model puts a request handler into the same
   memory budget as a model server. Cold-start is dominated by
   model load.
2. **Per-replica memory cost.** Horizontal scaling of memory-server
   means N copies of the model. The cluster already runs a
   dedicated `nomic-embed-server` at port 11436 (visible in the
   chart's `peer.service` map and OpenTelemetry instrumentation).
   Embedding belongs there.
3. **Coupling.** memory-server's image carries onnxruntime as a
   transitive dep, raising the build surface and CVE attack
   surface. ChromaDB's pod ALSO carries the same model code — so
   we have two redundant model copies in two pods, and neither
   pod knows about the other.

The cluster's existing layout already separates inference (chat,
summariser, embed) from request handling (memory-server). This ADR
extends that separation to the embedding step that currently leaks
across the boundary.

## Decision

Migrate embedding off memory-server. Two parts:

**Part 1 — pre-compute in memory-server, send `embeddings=` to ChromaDB.**

Replace `collection.upsert(documents=[...])` with:

```python
vectors = await embed_via_nomic(documents)        # external HTTP call
collection.upsert(
    ids=[...],
    embeddings=vectors,                            # pre-computed
    documents=[...],
    metadatas=[...],
)
```

The `embed_via_nomic` helper POSTs to `nomic-embed-server`
(`POST /v1/embeddings`, OpenAI-compatible) and returns vectors.
ChromaDB stores; doesn't compute.

ChromaDB's collection is opened with an explicit
`embedding_function=None` so the lib doesn't fall back to its
default-default (which would re-introduce client-side embedding).

**Part 2 — accept the vector-space migration.**

`nomic-embed-text-v1.5` produces 768-dim vectors; ChromaDB's stock
`ONNXMiniLM_L6_V2` produces 384-dim vectors. **Embeddings from one
are NOT comparable with embeddings from the other.** A vector-space
change is a one-way migration: every existing chunk in
`decisions`, `skills`, `semantic`, and `ai_research_papers` must
be re-embedded.

The migration plan:

1. **Coexistence phase.** New collections under suffix `_v2`
   (e.g. `decisions_v2`, `ai_research_papers_v2`) built with the
   nomic-served embedder. Old collections remain queryable
   under their existing names with the singleton ONNX embedder
   (no behaviour change for existing reads).
2. **Re-index.** Operator runs `/memory/index?file=…` against
   each `_v2` collection — same per-file loop pattern that
   ai_research_papers uses today.
3. **Cutover.** Application code switches `recall_*` tools to
   point at `_v2` collections. The `_v1` collections become
   read-only and eventually deleted in a follow-up release.
4. **Retire stock embedder.** Remove
   `src/audittrace/services/embedder.py` once no live collection
   references it. Drop onnxruntime from the memory-server image.

## Consequences

**Wins:**

- memory-server resident set drops by ~1–1.5 GiB; the 4 GiB chart
  ceiling becomes generous instead of tight, and the previous OOM
  class is structurally impossible.
- Horizontal scaling of memory-server stops paying N×model-size in
  RAM.
- nomic-embed-server replaces a redundant copy of model code that
  was running silently inside memory-server.
- New embedder is higher-quality (nomic-embed-text-v1.5 ≫
  all-MiniLM-L6 on standard retrieval benchmarks) — better recall
  for the same query budget.
- onnxruntime exits memory-server's dependency tree.

**Costs:**

- One-shot vector-space re-index of every collection. For the
  3,181-chunk `ai_research_papers` snapshot from 2026-05-06, this
  is ~10 minutes via the per-file loop. Other collections are
  smaller.
- nomic-embed-server becomes a hard dependency for `/memory/index`
  and recall tools. Currently used in some chat-side flows, so
  it's already in the SLA, but its monitoring + alerting need to
  account for the new write path.
- Network hop per upsert. memory-server → nomic-embed-server runs
  over Istio mTLS within the cluster — sub-millisecond p50 — but
  it adds a failure mode that didn't exist before. Need a circuit
  breaker / retry policy per ADR-034 patterns.
- API contract evolution. `/memory/index` continues to accept
  document-only input from clients, but its internal flow now
  has a hard nomic-embed dependency.

**Compatibility:**

- ChromaDB's persisted collection metadata records the embedding
  function name; mismatched names print a noisy warning on every
  open. The `_v2` suffix on the new collections is in part to
  avoid that warning, in part to make the migration auditable
  (operators see both old and new collections side-by-side).
- The OpenAI-compatible chat endpoint (`/v1/chat/completions`) is
  not affected; its persistence path is independent of embedding.

## Alternatives considered

**A1. Server-side embedding inside ChromaDB.**

ChromaDB 1.5.x does not expose a server-side embedding configuration
that survives `chromadb.HttpClient` queries — the embedding function
on a collection is honoured by the *client*, not the *server*. Some
4.x betas hinted at this; current GA does not. Rejected because the
upstream pattern is "compute embeddings on the producer side and
store vectors only", which is exactly what Part 1 of this ADR does.

**A2. Keep the singleton fix as the durable answer.**

The 2026-05-06 singleton + context-manager + sync-handler patch
demonstrably closes the leak and unblocks the ai_research_papers
ingest. It does NOT address the architectural cost (memory budget,
horizontal scaling, dependency surface), and it leaves the
two-redundant-models problem in place. Adequate as a stopgap; not
a closing ADR.

**A3. Abandon ChromaDB.**

`pgvector` on the existing Postgres would consolidate vector store
+ relational data. Rejected (for now): the memory CRUD backoffice
(`/memory/<layer>`) and tool-call audit are deeply wired to
ChromaDB's collection model and metadata-filter semantics. The
re-platforming cost is large; benefits don't dominate. Worth
revisiting in a later ADR if pgvector's filter performance matures
and Postgres's RLS story extends to vector indexes.

## Sequence after acceptance

1. Land the 2026-05-06 singleton/context-manager/sync-handler PR
   first (already done as of this ADR's proposal — that is the
   immediate fix that this ADR plans to retire).
2. Stand up `nomic-embed-server` as a hard dependency in
   `charts/audittrace/values.yaml` (it currently exists but isn't
   gated by deploy-preflight). Add a probe for it in
   `scripts/post-deploy-verify.sh`.
3. Add `embed_via_nomic()` helper to
   `src/audittrace/services/embedder.py` (replacing the
   `SINGLETON_EMBEDDER` exposed surface — same module, new
   responsibility). Implement with `with httpx.Client(...)` per
   PYTHON-ENGINEERING skill §1.
4. Refactor `routes/memory.py` `_index_md_objects` /
   `_index_pdf_objects` / `services/semantic.py` to take a
   pre-computed `embeddings` argument and route through
   `embed_via_nomic` at the call site. Tests assert no
   `embedding_function=` arg is passed (collection-level
   embedding intentionally disabled).
5. Cutover script: `scripts/migrate-embedder.sh` runs the per-file
   index loop against `_v2` collections, then flips a flag in
   `values.yaml` that switches recall tools to read from `_v2`.
6. Smoke + verify-deploy assertion: chunk counts in `_v2` ≥
   chunk counts in `_v1`. ADR-046 pattern.
7. Delete `_v1` collections in a follow-up release once production
   has run on `_v2` for one full operator cycle.

## Open questions

- **Latency budget for embedding round-trip.** nomic-embed-server's
  p99 under realistic per-document load is unmeasured. If a single
  PDF page produces 5–10 chunks and we batch per-page, the embed
  step adds one round-trip per page. Need a measurement before
  committing to the design — if p99 is e.g. 200ms, the
  Agentic_Design_Patterns 19MB PDF ballparks 100s in embedding
  alone, vs the 240s the singleton path took end-to-end. Probably
  net-positive but worth baselining.
- **Batching at memory-server vs at nomic-embed-server.** If the
  embed server accepts batched inputs (it should — OpenAI-
  compatible API supports `input: [str, str, …]`), the round-trip
  count is per-file not per-page. Cleaner.
- **Re-indexing strategy for chunks where the source file is
  gone.** Some chunks in `decisions` / `skills` may be from
  Markdown files no longer in MinIO. Re-index from the live MinIO
  set drops them; that's correct (MinIO is canonical) but worth
  documenting as expected behaviour in the migration runbook.

## References

- 2026-05-06 leak fix PR (this same branch):
  `src/audittrace/services/embedder.py`, `src/audittrace/routes/memory.py`,
  `src/audittrace/services/semantic.py`, `feedback_use_context_managers`.
- ChromaDB 1.5 client-side embedding behaviour:
  `chromadb/api/types.py:DefaultEmbeddingFunction.__call__`.
- nomic-embed-server: existing chart dependency at port 11436
  (`charts/audittrace/values.yaml` `peer.service` map).
- PYTHON-ENGINEERING skill (Julien Danjou — Hacker's Guide to
  Python; Scaling Python).
