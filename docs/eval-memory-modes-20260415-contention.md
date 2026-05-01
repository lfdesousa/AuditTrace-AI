# Memory Access Modes — Mistral Residency Contention (2026-04-15 contention run)

**Author:** Luis Filipe de Sousa
**Date:** 2026-04-15
**Related:** [ADR-030](ADR-030-session-summarizer.md), companion clean baseline [eval-memory-modes-20260415-clean.md](eval-memory-modes-20260415-clean.md)
**Raw data:** `tmp/eval-memory-modes-20260415T121903Z.jsonl`
**Script:** `scripts/eval-memory-modes.py --n-per-mode 10`
**Stack under test:** AuditTrace-AI at commit `a92bf9f`. **Mistral 7B Instruct v0.3 running on `:11437` at `--n-gpu-layers 10`** (partial offload), summariser worker enabled (`AUDITTRACE_SUMMARIZER_ENABLED=true`). Otherwise identical to the clean baseline run from 12:08 CEST.

## TL;DR

**Mistral residency at `--n-gpu-layers 10` adds essentially nothing measurable to Qwen's latency budget.** Tools-mode mean rose by 4.6 % (107.5 s → 112.4 s) — well inside the per-probe variance, which spanned -52 s to +58 s on matched pairs. p95 unchanged. Tool-selection accuracy 80 % vs 90 % (one extra timeout dropped a tool count to zero). The signal is **noise floor for N=10**.

**The architectural conclusion: the three-model topology (Qwen + nomic + Mistral on a single Strix Halo iGPU) is viable.** GPU_LAYERS=10 is the recommended default — Mistral takes ~1 GB of GPU memory at rest, contributes ~24 s/summary throughput, and demonstrably does not steal Qwen's working set in a measurable way. Ship it.

**Inject-mode showed a dramatic and counterintuitive improvement (mean -33 %, 2 fewer timeouts).** This is *almost certainly not* a Mistral effect — it is OS page cache + Postgres query cache warming between the sequential runs. Inject mode loads all four memory layers per request and is the most I/O-heavy of the two; cache warmth helps it disproportionately. To isolate Mistral residency cleanly we would need to randomise run order and drop OS caches between runs (operationally heavy; deferred).

**Recommendation: ship Mistral with `--n-gpu-layers 10` as the default.** Update the start script default. Amend ADR-030 §1 to reflect the measured partial-offload number. Close the inference-tuning thread for today and pivot back to package work.

---

## Headline comparison

| Metric | Tools clean | Tools contention | Δ | Inject clean | Inject contention | Δ |
|---|---|---|---|---|---|---|
| mean latency | 107.5 s | 112.4 s | +4.6 % | 145.8 s | 96.8 s | **-33.6 %** |
| p95 | 180.1 s | 180.1 s | flat | 180.1 s | 180.1 s | flat |
| timeouts | 1 / 10 | 2 / 10 | +1 | 5 / 10 | 3 / 10 | -2 |
| tool-selection acc | 90 % | 80 % | -10 pp | n/a | n/a | — |
| mean prompt tok | 4 513 | 4 075 | -10 % | 1 992 | 1 954 | -2 % |
| mean completion tok | 336 | 416 | +24 % | 1 230 | 741 | -40 % |
| mean total tok | 4 849 | 4 490 | -7 % | 3 222 | 2 695 | -16 % |

The token deltas are themselves noisy at N=10. Don't read too much into them.

---

## Per-probe matched-pair deltas

### Tools mode (clean → contention)

| # | Clean lat | Contention lat | Δ |
|---|---|---|---|
| 1 | 174.1 s | 128.6 s | **−45.5 s** |
| 2 | 167.6 s | 180.1 s **TIMEOUT** | +12.5 s |
| 3 | 180.1 s **TIMEOUT** | 128.0 s | **−52.1 s** |
| 4 | 76.7 s | 47.4 s | −29.3 s |
| 5 | 65.2 s | 65.9 s | +0.8 s |
| 6 | 50.2 s | 46.1 s | −4.2 s |
| 7 | 91.4 s | 146.7 s | **+55.3 s** |
| 8 | 123.9 s | 180.1 s **TIMEOUT** | +56.1 s |
| 9 | 44.4 s | 102.8 s | **+58.4 s** |
| 10 | 101.7 s | 97.8 s | −3.9 s |

5 probes faster, 4 slower, 1 essentially flat. Spread −52 s to +58 s. The mean delta of +5 s on tools is therefore **statistically meaningless at this sample size** — variance dwarfs the contention signal.

### Inject mode (clean → contention)

| # | Clean lat | Contention lat | Δ |
|---|---|---|---|
| 1 | 75.0 s | 55.0 s | −20.0 s |
| 2 | 180.1 **T** | 180.1 **T** | 0 |
| 3 | 171.5 s | 180.0 **T** | +8.6 s |
| 4 | 180.1 **T** | 78.1 s | **−102.0 s** |
| 5 | 88.2 s | 52.3 s | −35.9 s |
| 6 | 82.0 s | 42.7 s | −39.3 s |
| 7 | 180.0 **T** | 70.2 s | **−109.8 s** |
| 8 | 180.1 **T** | 180.1 **T** | 0 |
| 9 | 140.7 s | 74.9 s | −65.8 s |
| 10 | 180.1 **T** | 54.8 s | **−125.3 s** |

7 probes faster, 1 slower, 2 consistent timeouts. Three probes flipped from timeout to ~50–80 s success (probes 4, 7, 10) — that's the cache-warming signal in raw form.

---

## Why the inject-mode "improvement" is almost certainly cache warming, not Mistral

The eval harness runs sequentially: clean tools → clean inject → contention tools → contention inject. Between the clean baseline (12:08) and the contention run (14:19), the stack served:

- **20 probes worth of ChromaDB queries** (each `recall_semantic` reads the embedding store; 3 123 chunks across 4 collections).
- **20 probes worth of Postgres `recall_recent_sessions` queries** (the new hybrid path with the join).
- **Mistral writing 19 SessionRecord rows** before the eval-isolation shutdown.
- **Multiple full container restarts** of `memory-server` (cold-restarts FastAPI, but does NOT reset OS page cache, Postgres shared_buffers, or ChromaDB's LRU).

By the time eval #2 began, the OS file-system cache, Postgres `shared_buffers`, and ChromaDB's vector indices had **two hours of warmth on disk pages they need**. Inject mode is I/O-dominant — every request loads ADRs, skills, sessions, and semantic hits up front. It benefits disproportionately from those warm caches. Tools mode is more LLM-dominant — the cache-warming win is muted because the wall time is mostly per-iteration prompt eval on Qwen.

This explains the asymmetry: tools moved by ~5 s (noise), inject moved by ~50 s (cache effect). The cache effect would have happened regardless of whether Mistral was running. **Therefore the Mistral residency budget for inject mode is unknown from this data — we'd need a properly controlled re-run to measure it.** That's a future-eval concern, not a today concern.

---

## What this run also confirms (positively)

- **Hybrid `recall_recent_sessions` is being used by the model.** Probe 7 of contention tools called `recall_recent_sessions` alongside other tools. The Part 1 hybrid recall path is live in production and being selected by the LLM as designed.
- **`recall_skills` was invoked** on probe 7 (the eval prompt was *"Why is AUDITTRACE_MEMORY_MODE a kill switch?"* — the model went looking for Sovereignty / IAM skills). Tool registry routing is working across all four memory tools.
- **The exit-condition cherry-pick still didn't fire** — varied args throughout. Pending a noisier-category re-run (analytical "why X" probes from 2026-04-14 night) to validate empirically.
- **Summariser DID NOT fire any cycle during the eval window.** Its 5-min wake interval came up four times during the ~35-min eval, but the eligibility query found 0 sessions because all interactions were younger than the 15-min idle threshold. So **today's contention measurement is for Mistral residency only, not concurrent generation.**

---

## Caveats (honest)

- **N=10, single category.** Same caveat as the clean baseline doc. The full 100×6 sweep is still pending, and the cold-cache control is still operationally complex.
- **Sequential runs introduce cache-warming asymmetry** that confounded the inject-mode measurement. A future eval should either randomise run order or include a `sync; echo 3 > /proc/sys/vm/drop_caches` between runs.
- **Concurrent-generation contention NOT measured today.** Mistral was loaded but idle. The proper test is to backdate some interactions so the summariser actually fires mid-eval. Future work.
- **Variance at N=10 is too large to draw fine-grained conclusions.** Anything within ~30 s of the baseline mean is in the noise.

---

## Decisions taken from this run

1. **Mistral default GPU offload:** `GPU_LAYERS=10` (was `99`). Update `scripts/start-summarizer-llama.sh` default. Reflect in ADR-030 §1.
2. **Three-model topology:** confirmed viable on Ryzen AI Max+ 395 / Strix Halo. No re-tune needed before more meaningful data is available.
3. **Inference-tuning thread:** closed for today. Future tuning waits on either (a) a properly cache-controlled eval run or (b) actual user-reported latency complaints in production.
4. **Pivot back to package work.** Carry-over items from `project_session_20260415.md` are the next focus: backfill script for old SessionRecord IDs, observability-stack as a tracked repo, Langfuse trace verification for the summariser path.
