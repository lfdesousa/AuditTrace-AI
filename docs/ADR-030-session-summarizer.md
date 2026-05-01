# ADR-030: Session Summariser & Three-Model Inference Topology

**Status:** Accepted
**Date:** 2026-04-15
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-024 (proxy passthrough), ADR-025 (memory-as-tools),
ADR-026 (multi-user identity), ADR-029 (audit trail completeness)

## Context

`recall_recent_sessions` is one of the four memory tools exposed under
ADR-025. Its value is entirely gated on the `sessions` table being
populated. Today that table is only written by seed scripts and unit
tests — no component in the live chat flow ever calls
`PostgresConversationalService.save_session`. The tool is therefore
permanently cold: every call from the model returns zero rows.

Two behavioural consequences follow:

1. **The tool is never useful on day one.** A new user has zero
   summaries even after weeks of chatting, because the population path
   does not exist. `recall_recent_sessions(project='AuditTrace-AI')`
   returns `[]` and the model's ambient context loses one of its four
   advertised tools.
2. **Tools-mode evaluation is noisier than it should be.** The
   2026-04-14 eval baseline (`docs/eval-memory-modes-20260414.md`)
   showed tools winning on mean latency, p95, and error rate, but the
   iteration distribution was strongly bimodal (`[1,1,1,2,4,8,8,10,10,10]`).
   The long tail is dominated by analytical probes for which
   `recall_recent_sessions` would be the ideal answer but returns
   nothing, forcing the model to keep searching.

A second, adjacent discovery drove the topology decision: the single
Qwen3.5-35B-A3B chat model cannot play both roles (user-facing
generation and background summarisation) without latency interference.
Summarisation is cheap, deterministic JSON output; running it on the
same llama-server slot the tool-loop uses would steal budget from
interactive traffic. A dedicated endpoint on a smaller, dense,
fast model is the natural split.

## Decision

Two parts under a single ADR because they share the same end state (a
populated `sessions` table) and must be designed together:

1. **Hybrid `recall_recent_sessions`** — read real summaries first,
   pad with synthetic rows derived from `interactions` for session
   identifiers not yet summarised. Ship before Part 2 so the tool
   becomes useful immediately.
2. **Session summariser loop** — a background asyncio task that
   consumes idle sessions and emits `SessionRecord` rows via a
   dedicated Mistral 7B Instruct v0.3 llama-server endpoint. Ship
   after Part 1 so the transition from synthetic to real summaries is
   invisible to callers.

### §1. Three-model inference topology

Three llama-server processes, each pinned to a port, each running a
model chosen for the job. The topology is now part of the
architecture and must appear in `docs/architecture/workspace.dsl` and
the deployment view so it is legible at L2.

| Port | Model | Role | Why this model |
|---|---|---|---|
| `:11435` | `Qwen3.5-35B-A3B-Q4_K_M` | Chat / tool-loop reasoning | MoE (3B active of 35B). Strong multi-turn reasoning, long context, the model the memory-as-tools loop was measured against in ADR-025. GPU-offloaded (`--n-gpu-layers 99`), Q4 KV cache, 64k ctx. |
| `:11436` | `nomic-embed-text-v1.5.Q8_0` | Embeddings for ChromaDB semantic recall | 768-dim embeddings, 8k ctx, CPU-only (`--n-gpu-layers 0`). Q8 quant because embedding quality is more sensitive to quantisation than chat generation; CPU-only because ChromaDB queries are not on the user-facing critical path. |
| `:11437` | `Mistral-7B-Instruct-v0.3-Q4_K_M` | Session summariser | Dense 7B, fast greedy decoding, strong instruction-following for strict-JSON output. Proven reliable (same vendor and tokenizer family as Mistral Small — minimal vendor surface). 🇫🇷 EU-origin, consistent with the AuditTrace-AI sovereignty framing. **Partial GPU offload `--n-gpu-layers 10`** (~1 GB GPU at rest, ~24 s/summary, no measurable contention with Qwen — see [eval-memory-modes-20260415-contention.md](eval-memory-modes-20260415-contention.md)). Q4 KV cache (`--cache-type-k q4_0 --cache-type-v q4_0`), 16k ctx — plenty for a 15-min idle session transcript. Kept on a separate port so summarisation never contends with the interactive tool-loop for the Qwen slot. |

Launch command for the new summariser endpoint (mirrors the Qwen
process in `~/opt/llamacpp/`):

```bash
/home/lfdesousa/opt/llamacpp/llama-server \
  --model /home/lfdesousa/models/Mistral-7B-Instruct-v0.3-Q4_K_M.gguf \
  --port 11437 --host 0.0.0.0 \
  --ctx-size 16384 \
  --batch-size 1024 --ubatch-size 256 \
  --cache-type-k q4_0 --cache-type-v q4_0 \
  --n-gpu-layers 10 --flash-attn on \
  --threads 8 --metrics \
  --alias mistral-7b-summarizer \
  --reasoning-format none
```

Resident memory budget at rest: Qwen ~20 GB + Mistral 7B at
`--n-gpu-layers 10` ~1 GB GPU + ~4 GB CPU. Nomic sits on CPU and does
not compete. Mistral residency at this offload level was measured on
2026-04-15 to add no statistically meaningful cost to Qwen's
tools-mode latency (mean +4.6 % at N=10, well inside the per-probe
variance of -52 s to +58 s — see `eval-memory-modes-20260415-contention.md`).
Comfortable headroom on the Aigle workstation for observability
containers and additional agents.

> **Operator note** — `--n-gpu-layers 10` is the recommended default
> for single-iGPU boxes (Ryzen AI Max+ 395 / Strix Halo and similar
> unified-memory architectures). On a discrete-GPU host where a
> second GPU can be dedicated to summarisation, raise to `99` for
> 3-14 s/summary throughput. On a constrained box, drop to `0` for
> pure-CPU summarisation (~60 s/summary). Tunable via the
> `GPU_LAYERS` env var on `scripts/start-summarizer-llama.sh`.

### §2. Settings

New entries on `Settings` in `src/audittrace/config.py`:

| Setting | Default | Purpose |
|---|---|---|
| `AUDITTRACE_SUMMARIZER_URL` | `${AUDITTRACE_LLAMA_URL}` | OpenAI-compat base URL for the summariser model. Falls back to the Qwen endpoint when the dedicated Mistral server is not running, so the feature degrades rather than crashes. |
| `AUDITTRACE_SUMMARIZER_MODEL` | `mistral-7b-summarizer` | Model alias sent in the `model` field of the chat-completions request. Must match the `--alias` on the llama-server process. |
| `AUDITTRACE_SUMMARIZER_ENABLED` | `True` | Kill switch. Set to `False` to disable the background loop without removing config. |
| `AUDITTRACE_SUMMARIZER_IDLE_MINUTES` | `15` | Minimum idle window before a session is eligible for summarisation. Chosen as a trade-off: short enough that today's sessions get summarised before tomorrow's work starts, long enough that we do not summarise mid-conversation. |
| `AUDITTRACE_SUMMARIZER_INTERVAL_MINUTES` | `5` | Wake cadence for the background loop. |
| `AUDITTRACE_SUMMARIZER_MAX_PER_CYCLE` | `10` | Upper bound on sessions processed per wake. Protects against a first-run spike when there are thousands of unsummarised sessions. |

### §3. Part 1 — hybrid `recall_recent_sessions`

`PostgresConversationalService.load_sessions` is extended. It must
return up to `n` rows ordered by recency, where each row is either
a real summary or a synthetic one:

1. Query `SessionRecord` for this `(user_id, project)` ordered by
   `date DESC`, limit `n`. These are real summaries.
2. Query `interactions` for distinct `session_id` values where
   `user_id`/`project` match, `session_id IS NOT NULL`, and the
   session_id is **not** in the real-summary id set. For each, pull
   the first question and the last answer, plus the max timestamp.
   These are synthetic rows.
3. Merge, order by timestamp `DESC`, truncate to `n`.
4. Synthetic rows carry a `synthetic: True` flag in the returned
   dict so the tool response can label them for the model: *"draft
   summary, not yet finalised"*. Real rows carry `synthetic: False`.

**Contract change on `SessionRecord.id`.** Today the row ID is a
server-generated timestamp string. Part 2 will generate IDs equal to
the chat-level `session_id` so the two tables can be joined without
a side table. Safe because nothing currently writes `SessionRecord`
from the chat flow; only seed scripts do, and they will be updated
in lockstep with Part 2.

### §4. Part 2 — summariser loop

New module `src/audittrace/services/session_summarizer.py`.
Started from `server.py::lifespan` as an `asyncio.create_task(...)`
alongside the existing tracing setup, guarded by
`settings.sovereign_summarizer_enabled`.

**Data model.** A new column `summarized_at: Mapped[datetime | None]`
on `SessionRecord`, added via Alembic migration. NULL means never
summarised; a value older than `MAX(interactions.timestamp)` means the
session has new content since the last summary and should be
re-summarised.

**Eligibility query** (per wake cycle, one SQL statement):

```sql
SELECT i.session_id,
       i.user_id,
       i.project,
       MAX(i.timestamp) AS last_ts
  FROM interactions i
  LEFT JOIN sessions s ON s.id = i.session_id
 WHERE i.session_id IS NOT NULL
   AND (s.summarized_at IS NULL OR s.summarized_at < i.timestamp)
 GROUP BY i.session_id, i.user_id, i.project
HAVING MAX(i.timestamp) < NOW() - INTERVAL '15 minutes'
 ORDER BY last_ts ASC
 LIMIT :max_per_cycle
 FOR UPDATE OF s SKIP LOCKED;
```

The `FOR UPDATE ... SKIP LOCKED` clause is the concurrency contract —
if we later run multiple summariser workers (or one summariser plus a
manual backfill), no two workers will pick the same row.

**User attribution for RLS.** Each eligible session has a `user_id`.
The summary row must pass RLS on insert (the session_factory uses an
`after_begin` listener that reads `app.current_user_id`). The
summariser sets that GUC per-session inside a transaction:

```python
async with session_factory() as db:
    await db.execute(text("SET LOCAL app.current_user_id = :uid"),
                     {"uid": row.user_id})
    # ... fetch interactions, call LLM, insert SessionRecord, commit
```

`SET LOCAL` scopes the GUC to the transaction, so concurrent
summarisations for different users cannot leak identity.

**Summarisation prompt.** Strict JSON, no prose wrapper:

```
System: You are summarising a chat session for an audit archive. Output ONLY valid JSON matching the schema. No markdown fences. No commentary.

Schema: {"summary": string (<= 400 chars), "key_points": array<string> (<= 8 items, each <= 120 chars)}

User: <numbered transcript of Q/A pairs from this session>
```

The response is parsed with `json.loads`. A parse failure is logged,
the transaction is rolled back, and `summarized_at` is left NULL so
the row is retried next cycle. No partial writes.

**Request parameters to the Mistral endpoint.** `temperature=0.2`,
`max_tokens=600`, `response_format={"type": "json_object"}` — llama.cpp
honours the last one via grammar-constrained decoding, which makes
malformed JSON essentially impossible. `stream=False`.

### §5. Rollout order

Part 1 ships first and alone (`feat(memory): hybrid recall_recent_sessions
with synthetic rows`). It is a pure read-side change, no new
infrastructure, no new model dependency. The tool becomes useful on
day one for every existing user.

Part 2 ships second (`feat(summarizer): background session summariser
on Mistral 7B Instruct v0.3`). It requires the new llama-server process to
be running; when absent, the background task logs a warning once per
cycle and retries. The transition from synthetic to real summaries is
invisible to callers — the tool response shape is identical, only the
`synthetic` flag flips.

Architecture diagrams (`docs/architecture/workspace.dsl`) are updated
in between to reflect the three-model topology (new `summarizerServer`
software system + matching deployment node + `sessionSummarizer`
component wired to the `conversationalSvc`).

## Consequences

### Positive

- `recall_recent_sessions` stops being permanently cold. From the day
  Part 1 merges, it returns *something* for any session that
  accumulated at least one Q/A pair — degraded (first+last turn) but
  useful.
- The tool-loop tail should shorten. Yesterday's bimodal distribution
  had analytical probes running to the 10-iter cap partly because
  `recall_recent_sessions` returned empty. Re-running the eval after
  Part 1 is a prerequisite for judging whether the LangGraph
  exit-condition cherry-pick (separate task) is still needed.
- Summarisation latency is isolated from interactive latency. The
  Mistral endpoint runs on a separate port; the interactive tool-loop
  on Qwen is untouched. Budget interference is structurally impossible.
- Model choice is config-driven. Swapping Mistral for any other
  OpenAI-compatible endpoint (a smaller local model, a cloud model
  for a benchmark run) is one env-var change.
- The three-model topology makes each model's job legible. Reviewers
  asking "why three LLMs?" get a one-line answer per model in both
  the ADR and the L2 deployment diagram.

### Negative / caveats

- **GPU budget now carries two models.** Qwen (~20GB) + Mistral 7B
  (~5GB) = ~25GB resident — comfortable today but not infinite. Every
  future GPU-resident workload (a larger embedder, a second agent
  model, a vision model) eats into the same pool. The fallback
  options in the follow-ups below are the release valve if we
  accumulate containers faster than expected.
- **Synthetic summaries can mislead the model.** A first-turn question
  plus the last-turn answer is a poor stand-in for a real summary. The
  `synthetic: True` flag mitigates this by signalling draft quality,
  but the model may still over-trust it. Worth watching in the next
  eval.
- **`SessionRecord.id` contract change is one-way.** Once Part 2
  starts writing rows with `id == session_id`, the old timestamp-based
  IDs from seed scripts become inconsistent. A backfill script for
  existing seed data ships alongside Part 2.
- **`FOR UPDATE SKIP LOCKED` requires Postgres 9.5+.** Already our
  baseline (we run Postgres 16), so no runtime risk — flagged because
  a SQLite test fallback would need a different eligibility query.
- **Grammar-constrained decoding is llama.cpp specific.** If the
  summariser endpoint is ever pointed at a non-llama.cpp backend, the
  `response_format` hint becomes advisory rather than enforced, and
  the JSON-parse-failure-retries path becomes load-bearing.
- **Re-summarisation cost is unbounded in principle.** A very active
  session that receives a new Q/A pair every 14 minutes will be
  re-summarised on every cycle. Mitigated in practice by the idle
  threshold; worth revisiting if we see a billing spike.

### Follow-ups

- **Eval re-run** after Part 1: `scripts/eval-memory-modes.py
  --n-per-mode 10`, new `docs/eval-memory-modes-YYYYMMDD.md`.
- **Eval re-run** after Part 2: full `N=100` across 6 categories,
  once real summaries have accumulated for a few days of use.
- **Summariser metrics** via the `/metrics` Prometheus endpoint:
  sessions_summarised_total, summarisation_duration_seconds,
  summarisation_failures_total. Wire after Part 2 lands.
- **Backfill script** for historic seed-script `SessionRecord` rows
  with timestamp-style IDs, so the JOIN with `interactions` finds them.
- **Smaller summariser alternatives** — documented here so we can
  swap without re-opening the ADR when memory pressure rises or
  sovereignty requirements tighten. Every option below is a single
  env-var change (`AUDITTRACE_SUMMARIZER_URL` / `AUDITTRACE_SUMMARIZER_MODEL`)
  plus a new llama-server process on `:11437`. Grammar-constrained
  decoding guarantees valid JSON regardless of model size, so the
  trade-off is purely about summary *content* quality.

  | Model | Size (Q4) | Origin | Angle |
  |---|---|---|---|
  | Phi-4-mini Instruct | 3.8B · ~2.4GB | 🇺🇸 Microsoft Research | Strong JSON/instruction-following for its size; default "small" benchmark. First stop if memory pressure forces a downgrade. |
  | Llama 3.2 3B Instruct | 3B · ~2GB | 🇺🇸 Meta | Smallest of the serious options; weakest on abstractive technical content; best raw tokens/sec. |
  | Gemma 3 4B-it | 4B · ~2.5GB | 🇺🇸 Google DeepMind | Good instruction tuning, strong multilingual coverage — useful if sessions drift into French/German. |
  | EuroLLM-9B-Instruct | 9B · ~5.5GB | 🇪🇺 EU-funded research consortium | Explicitly trained on all 24 official EU languages. Slightly larger than the Mistral 7B default but strongest EU-sovereignty narrative. Pick this if sovereignty framing outweighs footprint. |
  | Teuken-7B-Instruct | 7B · ~4.2GB | 🇪🇺 OpenGPT-X (Germany-led EU project) | Alternative EU option at the same size as the default; less production-tested than EuroLLM or Mistral. |

  **Trigger to revisit:** sustained GPU utilisation > 80%, a new
  GPU-resident container scheduled to deploy, or a regulatory change
  that tightens data-residency requirements on model weights.
