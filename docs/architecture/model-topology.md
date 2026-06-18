# Model topology — why three specialised models, not one

> Canonical reference for "why do you run three models?" Keep this current; it is
> the single source for that answer in pitches, reviews, and design-partner Q&A.
> Authoritative for the *rationale*; the live model names/ports track ADR-030 and
> the C4 `workspace.dsl`. Last verified against code 2026-06-18.

## Bottom line

AuditTrace runs **three small, self-hosted, specialised models** — each sized to
exactly one job — instead of one large general model or any external API. The shape
is a deliberate consequence of three product constraints: **latency, sovereignty, and
auditability.** Every model runs on our own infrastructure; no inference call leaves
the boundary.

| Job | Model | Port | Placement | Why this model |
|---|---|---|---|---|
| **Reasoning (chat)** | Qwen 3.6-35B-A3B-Q4_K_M (MoE, ~3B active/token) | `:11435` | GPU (ROCm) | MoE gives near-35B quality at ~3B inference cost; MoE prompt-eval beats a dense 27B on a consumer GPU at the 5–15K-token prompts the client produces |
| **Summarisation** | Mistral 7B Instruct v0.3 Q4_K_M | `:11437` | GPU (~1 GB, background) | Small, fast, reliable strict-JSON; EU-origin; runs off the user-facing path |
| **Embeddings** | nomic-embed-text v1.5 Q8_0 (768-dim) | `:11436` | CPU only | Quality is quant-sensitive (Q8); embedding is off the critical path, so it leaves the scarce GPU for chat |

## The three jobs, and why each gets its own model

### 1 — Reasoning (chat): Qwen 3.6-35B-A3B, on the GPU

The interactive, latency-critical path. It reads the augmented prompt — the four
memory layers plus, in tools mode, the memory-tool loop — and produces the answer.
We use a Mixture-of-Experts model: ~3B active parameters per token deliver
near-35B-class quality at roughly 3B inference cost. The choice is measured, not
fashionable: MoE prompt-eval throughput is materially faster than a dense 27B on a
consumer GPU at the 5–15K-token prompts the coding client emits, which is why the
chat model was swapped back to the MoE on 2026-05-01.

### 2 — Summarisation: Mistral 7B Instruct v0.3, on the GPU (background)

A separate, smaller model for one narrow job: compressing idle conversations into
**strict-JSON** session summaries that become Layer-3 (conversational) memory. The
requirements here are the opposite of chat — not raw reasoning power, but small size,
speed, and dependable structured output. It runs in the background (never on the
user-facing path), takes about 1 GB of GPU at rest, and — proven in the contention
evaluations — does not steal the chat model's working set. It is EU-origin (Mistral,
French), a deliberate sovereignty choice. Governed by ADR-030.

### 3 — Embeddings: nomic-embed-text v1.5, on the CPU

Turns text into vectors for the semantic memory layer (Layer 4, ChromaDB). Embedding
is **off the user-facing critical path** — a recall query embeds one short string;
indexing happens at write time, not in the chat hot loop — so it does not need the
GPU, and putting it on the CPU keeps the scarce GPU reserved for the two
latency/quality-sensitive jobs. Q8 quantisation because embedding quality is more
sensitive to quantisation than chat generation. Governed by ADR-030 (placement) and
ADR-047 (the embedding path itself — see the honest note below).

## The deeper why — for "why not one big model, why not just call OpenAI?"

- **Specialisation beats a monolith.** A big reasoner for reasoning, a small
  structured-output model for summaries, a tiny embedder for vectors. Each is
  right-sized for its job, cheaper and faster than forcing one large model to do all
  three, and each is independently swappable without touching the others.
- **Resource economics on one box.** The three models coexist on a single
  GPU-plus-CPU machine. The GPU is the scarce resource, so it goes to the two
  latency- and quality-sensitive jobs (chat and summarisation); embeddings go to the
  CPU. This placement is a measured decision (ADR-030; the contention evaluations
  proved the summariser does not degrade chat).
- **Sovereignty is the thesis, not a feature.** Every model is self-hosted,
  EU-origin where it matters, with **zero** calls to OpenAI or Anthropic. The product
  is auditable, sovereign AI under the EU AI Act; routing embeddings through a US API
  would contradict the entire pitch. Three local models mean full data residency and
  no third-party inference dependency.
- **Auditability.** Each model is a distinct, traced dependency. The telemetry labels
  every outbound model call by name (`qwen-chat-llm`, `nomic-embed-server`,
  `mistral-summariser-llm`), so *which model did what* is part of the reconstructable
  trace for any decision. The model topology is itself inside the audit trail.

## What we gain — the value, by audience

The three-model split is not an engineering indulgence; it buys concrete value, and the
value is different for each audience who will ask.

**For the architect / CTO:**
- **Independent failure domains.** Chat, summarisation, and embedding fail and scale
  separately. One model being slow or down does not take the others with it.
- **Right-sized cost.** No paying 35B-class compute to embed a sentence. Each job runs
  the smallest model that does it well, on the cheapest resource that fits (GPU only
  where latency or quality demands it).
- **Swap-ability.** Any model can be replaced — a better embedder, a cheaper summariser,
  a larger reasoner — without touching the other two or the request gateway.

**For the buyer / business:**
- **No third-party inference bill, no third-party data exposure.** Everything runs on
  owned infrastructure; there is no per-token API cost and no prompt or document leaving
  the boundary. Predictable cost, and a clean data-handling story for procurement.
- **A defensible "sovereign by construction" claim.** Self-hosted, EU-origin models are
  a differentiator in regulated and public-sector deals where US-API dependence is a
  blocker, not a detail.

**For the regulator / auditor (the product thesis):**
- **Every model call is on the audit trail.** Each model is a named, traced dependency
  (`qwen-chat-llm`, `nomic-embed-server`, `mistral-summariser-llm`); a reconstruction of
  any decision shows which model contributed what, in what order. The model topology is
  itself auditable — the same standard the product applies to everything else.
- **Data residency by construction.** Because no inference leaves the boundary, "where
  did this decision's compute happen?" has a fixed, demonstrable answer.

The one-line version: **specialisation buys cost and resilience; self-hosting buys
sovereignty; tracing every model buys auditability — and auditability is the product.**

## Honest current state — the embedding path (read before answering questions)

The chat (Qwen) and summariser (Mistral) paths are wired in code and verified. The
**embedding path is mid-migration**, and the precise current state matters because it
is the most likely sharp question:

- nomic-embed-text v1.5 is **deployed and is the intended embedder** (768-dim).
- As of 2026-06-18 the shipped application code still embeds **in-process**, using
  ChromaDB's stock ONNX `all-MiniLM-L6-v2` model (384-dim), via a module-level
  singleton (`services/embedder.py`, `SINGLETON_EMBEDDER`).
- **ADR-047 (Accepted 2026-06-18)** is the decision to cut embedding over to the
  nomic server — moving the model out of the request handler and onto its dedicated
  box. It is motivated by a real out-of-memory incident (ChromaDB's stock embedder
  re-instantiated the model on every call) and the architectural smell of a request
  gateway hosting a 1–1.5 GiB ML model. The cutover carries a one-way vector-space
  migration (384-dim → 768-dim), so existing collections are re-indexed under a `_v2`
  suffix before recall switches over.

**The safe phrasing for Q&A:** *"nomic is deployed as the intended embedder; the
current code path still embeds in-process with a smaller MiniLM model; moving
embedding onto the dedicated nomic server is an accepted, in-progress change
(ADR-047), driven by a real OOM and by keeping the request gateway out of the
inference business."*

## Where the embedder runs at scale (placement, decided 2026-06-18)

On the dev box all three models co-reside (nomic on the host CPU). At scale (cloud
Tier-2+), nomic runs as its **own dedicated CPU Deployment** — its own pods, horizontally
auto-scaled (HPA) on embed load, on the EKS CPU node group. Deliberately **not** inside
memory-server's pod (the ADR-047 principle: the request gateway hosts no model) and
**not** on the GPU instance's CPU (that would burn expensive GPU-instance vCPU on a
CPU-only job and couple embedding availability to GPU-serving load). Embedding is off the
user-facing critical path, so the in-cluster hop from memory-server to nomic is free. This
mirrors how the trace store (ClickHouse) became its own first-class, separately-scaled
node in the observability work — embedding gets the same treatment.

**Performance-test consequence.** This adds an embedding dimension to the load posture
that the stub hides today. Post-cutover the suite must: baseline nomic p50/p99 (single
recall string + batched per-file index); load-test the index + recall paths with nomic in
the loop (not stub); prove nomic scales horizontally on CPU (per-replica throughput →
HPA target); and prove chat p95 is unaffected (embedding stays off the critical path).
This folds into the real-LLM run (#296), where all three models run for real.

## Known drift to clean up (so it stops being a question-magnet)

- `config.py: memory_embedding_dim = 1024` is dead config (unused in `src/`) **and**
  wrong (nomic v1.5 is 768-dim, MiniLM is 384). The llm-stub copied the 1024. Fold
  the correction into the ADR-047 implementation.
- `docs/architecture/product-and-dependencies.md` still lists the chat model as the
  dense 27B (a 2026-04-24 note); the live chat model is the 35B-A3B MoE (swapped back
  2026-05-01). Update on next pass.

## Likely questions, with answers

**"Why not one model that does everything?"** Cost and quality. A single large model
forced to embed and summarise is slower and worse at each than a specialist, and pins
the whole stack to one model's memory and failure profile. Specialists are smaller,
faster, independently swappable, and individually traceable.

**"Why self-host instead of calling an API?"** Sovereignty and auditability are the
product. The pitch is that AI decisions stay reconstructable and the data stays
resident under the EU AI Act; sending prompts or documents to a US inference API
would break exactly the guarantee we sell.

**"Three models on one box — don't they fight for resources?"** No, by design and by
measurement. The GPU serves the two latency/quality-sensitive jobs (chat,
summarisation); embeddings run on the CPU. Contention evaluations confirmed the
background summariser does not steal the chat model's GPU working set.

**"Do you use nomic for embeddings?"** It is the intended embedder and it is
deployed; the current code path still embeds in-process with a smaller MiniLM model;
the cutover to the nomic server is accepted and in progress (ADR-047). (See the
honest-current-state section.)

**"What happens if a model is down?"** Chat degrades to an error the client sees;
summarisation is background and simply retries on the next cycle; embedding (post
ADR-047) gets a retry/circuit-breaker per ADR-034. Each dependency fails
independently — a benefit of the split.

## References

- ADR-030 — session summariser + the three-model placement decision.
- ADR-047 — move ChromaDB embedding off the request path (Accepted 2026-06-18).
- ADR-016 — specialised-model routing (tool-call adapter).
- ADR-034 — resilience / retry patterns (the embedding round-trip failure mode).
- C4 model — `docs/architecture/workspace.dsl` (the live container + deployment views).
