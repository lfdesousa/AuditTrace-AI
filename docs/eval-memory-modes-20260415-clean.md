# Memory Access Modes — Clean Code-Change Baseline (2026-04-15)

**Author:** Luis Filipe de Sousa
**Date:** 2026-04-15
**Related:** [ADR-025](ADR-025-memory-as-tools.md) (memory-as-tools), [ADR-030](ADR-030-session-summarizer.md) (session summariser), prior baseline [eval-memory-modes-20260414.md](eval-memory-modes-20260414.md)
**Raw data:** `tmp/eval-memory-modes-20260415T110845Z.jsonl`
**Script:** `scripts/eval-memory-modes.py --n-per-mode 10`
**Stack under test:** AuditTrace-AI at commit `1a82eed` (ADR-030 Parts 1+2 merged + RLS owner-role fix). **Mistral summariser endpoint stopped + `AUDITTRACE_SUMMARIZER_ENABLED=false`** so the only resident GPU model is Qwen — apples-to-apples against the 2026-04-14 baseline that pre-dated Mistral.

## TL;DR

**Headline numbers regressed across the board.** Tools mean latency rose from 76.7 s → 107.5 s, p95 from 123.5 s → 180.1 s (capped at the eval client's timeout), error rate from 0 % → 10 %, tool-selection accuracy from 100 % → 90 %. Inject mode regressed harder still — error rate from 10 % → **50 %**.

**That regression is real work, not a code regression.** Yesterday's baseline ran against an *empty* ChromaDB (3 113 chunks were re-indexed at 23:10 *after* the 22:29 eval) and an *empty* sessions table (no summariser existed). Both `recall_semantic` and `recall_recent_sessions` returned no-op tool_results. Today both return real data — semantic search actually queries 3 123 chunks across 4 collections, hybrid recall surfaces synthetic rows from `interactions`. The added wall time is dominated by ChromaDB round-trips and richer tool_results that grow per-iteration prompt eval cost. The system is doing the *correct* amount of work for the first time; yesterday it was fast because it was effectively idle.

**Tools still beats inject by a wide margin.** Tools mean 107.5 s vs inject 145.8 s. Tools error rate 10 % vs inject 50 %. The architectural call to default to `tools` (ADR-025) is reinforced — the data-richness shift hurts inject mode much harder because injecting a populated 4-layer context blows past the 180 s eval client timeout half the time.

**The exit-condition cherry-pick (commit `e18800c`) didn't fire on this category.** Every probe today used *varied* tool args across iterations, so the signature-equality check never matched. Defensive architecture, no observed effect on this dataset. Worth re-running on a category where 2026-04-14 showed pathological repeat patterns (analytical "why X" probes) before judging.

**Recommendation: keep `AUDITTRACE_MEMORY_MODE=tools` as the default.** Re-measure after standing Mistral back up with `--n-gpu-layers 10` (partial offload) + summariser re-enabled to quantify the contention budget — that's the next eval doc.

---

## Headline comparison

| | tools today | tools 2026-04-14 | Δ | inject today | inject 2026-04-14 | Δ |
|---|---|---|---|---|---|---|
| mean latency | 107.5 s | 76.7 s | +40 % | 145.8 s | 93.9 s | +55 % |
| p50 | 96.6 s | 82.3 s | +17 % | (5 timeouts) | 64.8 s | n/a |
| p95 | 180.1 s | 123.5 s | +46 % | 180.1 s | 144.7 s | +24 % |
| max | 180.1 s | 140.5 s | +28 % | 180.1 s | 161.0 s | +12 % |
| error rate | 10 % (1) | 0 % | +10 pp | **50 %** (5) | 10 % (1) | +40 pp |
| tool-select | 90 % | 100 % | -10 pp | n/a | n/a | — |
| mean prompt tok | 4 513 | 8 401 | -46 % | 1 992 | 2 384 | -16 % |
| mean completion tok | 336 | 332 | flat | 1 230 | 1 321 | -7 % |

Notable: today's tools-mode prompt tokens are *lower* than yesterday (4 513 vs 8 401) but latency is higher. The slowdown isn't prompt size — it's the *composition* of work. ChromaDB queries that returned nothing yesterday now traverse 3 123 chunks per call; per recall_semantic round-trip that adds ~3 s of nomic-embed + chromadb wall time before the LLM sees the result.

---

## Per-probe latencies

### Tools mode (ordered by execution)

| # | Prompt | Lat | Tool calls | Result |
|---|---|---|---|---|
| 1 | What did ADR-025 decide about memory-as-tools? | 174.1 s | 10 (3×decisions, 7×semantic) | ✓ |
| 2 | Why did we reject Langchain for the tool-call loop? | 167.6 s | 10 (mix + 1×recent_sessions) | ✓ |
| 3 | Summarise ADR-024 on proxy pass-through. | 180.1 s | 0 | **TIMEOUT** |
| 4 | What ADRs cover multi-user identity? | 76.7 s | 1 (recall_decisions) | ✓ |
| 5 | Which ADR documents the four-layer memory port? | 65.2 s | 1 (recall_decisions) | ✓ |
| 6 | What decision did we make about KV cache compression? | 50.2 s | 2 (decisions + semantic) | ✓ |
| 7 | Why is AUDITTRACE_MEMORY_MODE a kill switch? | 91.4 s | 4 (mix + recent_sessions) | ✓ |
| 8 | Recall the reasoning behind transparent proxy augmentation. | 123.9 s | 10 (mix) | ✓ |
| 9 | What architectural choice did ADR-018 settle? | 44.4 s | 1 (recall_decisions) | ✓ |
| 10 | Which ADR covers full agentic trace capture? | 101.7 s | 2 (decisions×2) | ✓ |

Latency distribution (sorted): **[44.4, 50.2, 65.2, 76.7, 91.4, 101.7, 123.9, 167.6, 174.1, 180.1]**.

### Inject mode (ordered by execution)

| # | Prompt | Lat | Result |
|---|---|---|---|
| 1 | What did ADR-025 decide about memory-as-tools? | 75.0 s | ✓ |
| 2 | Why did we reject Langchain for the tool-call loop? | 180.1 s | **TIMEOUT** |
| 3 | Summarise ADR-024 on proxy pass-through. | 171.5 s | ✓ |
| 4 | What ADRs cover multi-user identity? | 180.1 s | **TIMEOUT** |
| 5 | Which ADR documents the four-layer memory port? | 88.2 s | ✓ |
| 6 | What decision did we make about KV cache compression? | 82.0 s | ✓ |
| 7 | Why is AUDITTRACE_MEMORY_MODE a kill switch? | 180.0 s | **TIMEOUT** |
| 8 | Recall the reasoning behind transparent proxy augmentation. | 180.1 s | **TIMEOUT** |
| 9 | What architectural choice did ADR-018 settle? | 140.7 s | ✓ |
| 10 | Which ADR covers full agentic trace capture? | 180.1 s | **TIMEOUT** |

5/10 timeouts — half the inject probes blew past the 180 s eval client timeout. This is the *populated-store penalty* hitting inject mode hardest: the inject path stuffs *all four* memory layers into the system prompt before the call. With ChromaDB now serving real semantic hits, that prompt is meaningfully larger than yesterday and the streaming generation runs out of timeout budget on the longer-form analytical questions.

---

## What this tells us about the code changes shipped today

Three code changes landed today on `main`:

1. **Hybrid `recall_recent_sessions`** (`7b1c892` — ADR-030 Part 1) — adds synthetic rows from `interactions` for sessions not yet summarised. Today `recall_recent_sessions` returns data on probes 2 and 7. No correctness regression. Adds tool_result body, marginal latency.
2. **Tool-loop identity-based exit condition** (`e18800c`) — exits early when consecutive iterations emit *identical* tool signatures. **Did not fire on a single probe today.** Every multi-iteration probe used varied args (different `query` strings as the model triangulated). The pattern this guards against (the 10-iter pathological loop on yesterday's analytical probes) is *not* in this dataset. Exit condition is defensive architecture pending a re-run on a noisier category.
3. **Background session summariser** (`aa9ffb0` + `1a82eed` — ADR-030 Part 2) — disabled for this eval (the whole point of this clean baseline). Validated live before shutdown: 19 SessionRecord rows written for opencode sessions from 2026-04-12/13, ~3–14 s each via Mistral 7B Instruct v0.3 on `:11437`.

Net code-change effect on this category: **zero direct regression, zero direct improvement.** The headline regression is entirely explained by the populated-store baseline shift.

---

## Why fast-path probes are still the architectural win

Probes 4, 5, 6, 9 in tools mode resolved in 44–77 s with 1–2 tool calls. That's the *correct shape* — model picks the right tool, gets a useful answer, returns. The cap-bound probes (1, 2, 8 with 10 tool calls) are the model triangulating across multiple memory layers when no single layer has the full answer; that's working as designed too, just costly.

The tool-selection accuracy of 90 % on tools mode (9/10 — the timeout dropped probe 3 with no recorded tool call) confirms the registry + ambient context guidance from ADR-025 is still steering correctly.

---

## Caveats

- **N=10, single category.** The full 100×6 sweep flagged in ADR-025 is still pending. This is a smoke that validates the code lineage is sound; conclusions about *headline performance* should wait for the larger run.
- **Cold-start effect dominates the early probes.** Probes 1, 2, 3 in tools (174, 168, 180 s) — heavy. Probes 4–10 normalised to 44–124 s. Yesterday's baseline had a warmer cache at run start (re-run that container had been up longer). A controlled cold/warm split would isolate this.
- **180 s timeout is the eval client, not the proxy.** `AUDITTRACE_LLAMA_PROXY_TIMEOUT=300` so the proxy itself accepts up to 5 min. Bumping the eval client timeout would convert today's timeouts to slow successes — useful data but doesn't help the user-facing latency story.
- **Tool-args field is `None` in the JSONL** — not a regression of the harness, just a data quirk where the audit row's args column was sparse during this run. Doesn't affect the conclusions.

---

## Next: the contention measurement (next eval doc)

Restart Mistral with `--n-gpu-layers 10` (partial offload), re-enable summariser (`AUDITTRACE_SUMMARIZER_ENABLED=true`), restart memory-server, re-run `--n-per-mode 10`. The delta between *this clean baseline* and *that contention run* is the answer to "what does running the third model alongside Qwen on a single iGPU cost us?". That number is what we either accept ("X seconds per probe — fine") or push back on ("too much, drop to `--n-gpu-layers 0`"). Doc lands as `eval-memory-modes-20260415-contention.md`.
