# ADR-047 implementation plan — move embedding to the nomic server

> **Status: PLAN ONLY — not implemented.** Written 2026-06-18 alongside ADR-047's
> acceptance. This is the sequenced, test-and-evidence-gated plan for the cutover;
> no `src/` change has been made. Source of decision: `docs/ADR-047-server-side-embedding.md`
> (Accepted 2026-06-18). Rationale for non-engineers: `docs/architecture/model-topology.md`.

## Goal

Move ChromaDB embedding off the memory-server request path and onto the dedicated
`nomic-embed-server` (`:11436`, 768-dim), retiring the in-process ONNX
`all-MiniLM-L6-v2` (384-dim) embedder. Accept the one-way 384→768 vector-space
migration via `_v2` coexistence collections.

## Non-goals

- No change to the OpenAI-compatible `/v1/chat/completions` contract (ADR-024
  invariant — byte-compatible default POST shape).
- No re-platform to pgvector (ADR-047 alternative A3, explicitly deferred).
- No deletion of `_v1` collections in this work — that is a later follow-up release
  after one full operator cycle on `_v2`.

## Preconditions (do before any code)

1. **Baseline the embed round-trip.** ADR-047 open question: nomic-embed-server p99
   under realistic per-document load is unmeasured. Measure `POST /v1/embeddings`
   p50/p99 for a single short query and for a batched per-file input, on the laptop
   rig, before committing the design. Capture the numbers as evidence.
2. **Confirm batching.** Verify the embed server accepts `input: [str, …]`
   (OpenAI-compatible batch) so the round-trip count is per-file, not per-chunk.
3. **Confirm nomic is reachable** from memory-server over the in-cluster path
   (Istio mTLS) at `:11436`, not just on the host.

## Workstreams

### WS1 — `embed_via_nomic()` helper + config (`services/embedder.py`, `config.py`)
- Add `async def embed_via_nomic(texts: list[str]) -> list[list[float]]` POSTing to
  `settings.embed_url` (`/v1/embeddings`, OpenAI-shaped, batched). Use an async httpx
  client per PYTHON-ENGINEERING skill (`async with`), with a retry/circuit-breaker per
  ADR-034.
- Keep `SINGLETON_EMBEDDER` available during coexistence (the `_v1` collections still
  use it for reads); mark it for removal in the retire step.
- **Fix the dimension drift:** correct `memory_embedding_dim` to 768 with an accurate
  comment, or delete it if it stays unused. Update the llm-stub's `_EMBEDDING_DIM`
  (currently 1024) to 768 so the stub emulates the real vector width.

### WS2 — collection wiring (`routes/memory.py`, `services/semantic.py`)
- Open `_v2` collections with explicit `embedding_function=None` (prevents ChromaDB's
  default-default from re-introducing client-side embedding).
- In `_index_md_objects` / `_index_pdf_objects` (and the semantic query path), compute
  vectors via `embed_via_nomic(...)` and pass `embeddings=` to `upsert()` / `query()`.
- Tests assert **no** `embedding_function=` arg is passed at the call sites.

### WS3 — deploy / chart (`charts/audittrace/values.yaml`, preflight, stub)
- **Placement (DECIDED 2026-06-18): a dedicated CPU Deployment.** At scale nomic runs as
  its own Deployment (its own pods + HPA on embed load) on the EKS **CPU node group** —
  NOT inside memory-server's pod (ADR-047 principle) and NOT on the GPU EC2's CPU (don't
  burn GPU-instance vCPU on a CPU job, don't couple embedding to GPU-serving load). This
  is the same "first-class, independently-scaled concern" move as the ClickHouse node in
  #313. memory-server → nomic is an in-cluster hop (Istio mTLS); embedding is off the
  user-facing critical path so the hop is free. (Small/POC deployments may co-locate nomic
  on the model node's CPU, but the at-scale target is the dedicated Deployment.)
- **CPU type for the nomic pods — DECIDED 2026-06-18: AMX-class x86, AWS `c7i` baseline.**
  nomic v1.5 is a ~137M int8-GGUF transformer — matmul-bound. The cloud has no consumer
  NPU; the in-CPU equivalent is **Intel AMX** (Sapphire Rapids) + AVX-512-VNNI, exactly
  what int8 embedding inference exploits via llama.cpp, with zero new toolchain. **Baseline
  target: AWS `c7i`** (compute-optimised, AMX). Cost alternative held in reserve: **Graviton
  `c7g`/`c8g`** (ARM, strong throughput/$, no AMX). True-accelerator future option:
  **Inferentia2 (`inf2`)** — the real cloud "NPU", best throughput/$ at high volume, but
  needs the Neuron toolchain (nomic compiled via optimum-neuron; llama.cpp/GGUF does not
  target it) — revisit only if embed volume justifies it. Right-size: tiny model + off the
  critical path → a few vCPU per pod, scale via HPA, not big boxes. Confirm per-replica
  throughput at the #296 baseline (the decision stands; the number tunes the HPA target).
- Promote `nomic-embed-server` to a **hard dependency**: gate it in deploy-preflight,
  add a probe in `scripts/post-deploy-verify.sh`.
- Add an **HPA** for the nomic Deployment keyed on embed load (CPU / queue depth), plus
  monitoring + alerting for the new write-path dependency.
- Stub parity: ensure the Tier-0 llm-stub serves a 768-dim embeddings endpoint so cloud
  stub runs stay representative.

### WS4 — migration (`scripts/migrate-embedder.sh`)
- Per-file index loop builds `decisions_v2`, `skills_v2`, `semantic_v2`,
  `ai_research_papers_v2` from the **live MinIO** source set (chunks whose source file
  is gone are correctly dropped — document as expected in the runbook).
- Flag in `values.yaml` switches `recall_*` tools to read from `_v2`.

### WS5 — tests (zero-skip, ≥90% per-file)
- Unit: `embed_via_nomic` happy path + retry/breaker + batch shape.
- Migrate any sync embedding fixtures to async (`pytest-asyncio`), mirroring the #263
  async test pattern.
- Assertion test: collections opened with `embedding_function=None`.
- `make test` green, per-file gate PASS.

### WS6 — live evidence (the HARD gate — ADR-049)
- Build the new image, **restart the running memory-server**, hit `/memory/index` and a
  recall through the **public API with a scoped JWT** (no kubectl bypass).
- Verify: `_v2` chunk counts ≥ `_v1` counts (ADR-046 smoke pattern); a recall returns
  results; the Langfuse trace shows a `peer.service=nomic-embed-server` span on the
  index/recall path (proof the call actually left memory-server).
- Capture: trace ID, the ChromaDB `_v2` count query, the API request/response, image tag.

## Rollout sequence (matches ADR-047 §"Sequence after acceptance")

1. Preconditions (baseline + batching + reachability).
2. WS1 + WS2 behind coexistence (`_v1` untouched, `_v2` built with nomic).
3. WS3 chart/preflight/stub.
4. WS4 re-index into `_v2`.
5. WS5 tests green.
6. WS6 live evidence on the laptop rig.
7. Cutover flag → recall reads `_v2`.
8. **Later release:** retire `SINGLETON_EMBEDDER`, drop onnxruntime from the image,
   delete `_v1`.

## Rollback

Cutover is a single flag (`values.yaml`) flip back to `_v1`; `_v1` collections stay
intact and queryable through the whole migration, so rollback is instant and lossless
until the retire step.

## Risks / open questions

- **Latency** — embed round-trip per file adds a hop; baseline (precondition 1) decides
  whether batching-per-file is sufficient or per-page batching is needed.
- **Hard dependency** — nomic now sits in the `/memory/index` + recall SLA; needs the
  circuit-breaker (ADR-034) and alerting so its failure degrades gracefully.
- **Quality delta** — nomic v1.5 should improve recall vs MiniLM; worth a before/after
  retrieval spot-check on a known query set so the migration is justified by evidence,
  not just architecture.

## Acceptance criteria

- Embedding runs on nomic-embed-server; memory-server holds no ML model (onnxruntime
  out of the image in the retire step).
- `_v2` collections built and recall reads them; chunk counts ≥ `_v1`.
- Live trace proves the embed call leaves memory-server (`peer.service` span).
- `make test` green, per-file ≥90%, zero-skip.
- Dimension drift (config + stub) corrected to 768.

## Files in scope

`services/embedder.py` · `config.py` · `routes/memory.py` · `services/semantic.py` ·
`images/llm-stub/server.py` · `charts/audittrace/values.yaml` ·
`scripts/post-deploy-verify.sh` · new `scripts/migrate-embedder.sh` · tests under
`tests/` · `docs/architecture/workspace.dsl` (flip the FUTURE embed edge to live).
