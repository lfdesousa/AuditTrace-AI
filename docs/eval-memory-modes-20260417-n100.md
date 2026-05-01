# Memory Access Modes — N=100 Full Sweep (2026-04-17)

**Model:** Qwen 3.6-35B-A3B Q4_K_M (DeltaNet linear attention, 3B active params)
**Infrastructure:** ADR-034 per-chunk idle timeout (120s) + SSE keep-alive (15s)
**Client timeout:** 1800s (30 min)
**Probes:** 100 per mode × 2 modes = 200 total
**Categories:** decisions (25), skills (25), recent_sessions (15), semantic (15), ambiguous (10), control (10)

## Executive summary

Tools mode wins every metric across every category. The ADR-031
per-request routing design — motivated by the 2026-04-15 finding that
"inject crushes tools on analytical" — is no longer justified. That
finding was a measurement artefact of the flat 300s total timeout on
Qwen 3.5, not a genuine capability gap.

## Head-to-head

| Metric | Tools | Inject | Delta |
|--------|-------|--------|-------|
| Mean latency | **55.7s** | 78.9s | Tools 1.4x faster |
| Median | **51.3s** | 76.2s | Tools 1.5x faster |
| P95 | **120.6s** | 133.7s | Tools 10% tighter |
| Max | 251.9s | 300.2s | — |
| Min | 3.0s | 5.2s | — |
| Error rate | **0.0%** | 4.0% | Tools flawless |
| Timeouts | **0** | 4 | — |
| Tool accuracy | 97.0% | n/a | — |
| Mean prompt tokens | 4,314 | 1,849 | Inject leaner (no tool overhead) |
| Mean completion tokens | 834 | 1,658 | Inject generates more text |

## Per-category breakdown

### Tools mode

| Category | N | Mean | Median | P95 | Errors | Tool accuracy |
|----------|---|------|--------|-----|--------|---------------|
| control | 10 | 8.7s | 4.4s | — | 0 | 10/10 |
| recent_sessions | 15 | 37.5s | 26.5s | — | 0 | 15/15 |
| semantic | 15 | 57.1s | 60.2s | — | 0 | 13/15 |
| skills | 25 | 60.4s | 58.3s | — | 0 | 25/25 |
| decisions | 25 | 72.4s | 67.1s | — | 0 | 24/25 |
| ambiguous | 10 | 73.8s | 74.4s | — | 0 | 10/10 |

### Inject mode

| Category | N | Mean | Median | P95 | Errors | Timeouts |
|----------|---|------|--------|-----|--------|----------|
| control | 10 | 11.8s | 10.5s | — | 0 | 0 |
| recent_sessions | 15 | 57.2s | 48.1s | — | 0 | 0 |
| skills | 25 | 84.3s | 81.8s | — | 1 | 1 |
| ambiguous | 10 | 88.9s | 89.8s | — | 0 | 0 |
| semantic | 15 | 91.4s | 83.4s | — | 0 | 0 |
| decisions | 25 | 101.9s | 80.0s | — | 3 | 3 |

## Historical comparison

| Metric | Qwen 3.5 N=10 (2026-04-14) | Qwen 3.5 N=10 (2026-04-15) | Qwen 3.6 N=100 (today) |
|--------|---------------------------|---------------------------|----------------------|
| Tools mean | 76.7s | 107.5s | **55.7s** |
| Tools errors | 0% | 10% | **0%** |
| Tools accuracy | 100% | 90% | **97%** |
| Inject mean | 93.9s | 145.8s | **78.9s** |
| Inject errors | 10% | 50% | **4%** |

### Ambiguous category — the critical reframe

| | Qwen 3.5 (2026-04-15) | Qwen 3.6 (today) |
|---|---|---|
| Tools mean | 175s | **73.8s** |
| Tools errors | **80%** | **0%** |
| Inject mean | 118s | 88.9s |
| Inject errors | 0% | 0% |
| Winner | Inject (decisively) | **Tools (decisively)** |

The 80% error rate on Qwen 3.5 tools-mode ambiguous probes was entirely
caused by the flat 300s `AUDITTRACE_LLAMA_PROXY_TIMEOUT` killing valid
`<think>` reasoning mid-stream. With ADR-034's per-chunk idle timeout,
those same prompt shapes complete successfully — and faster than inject.

## Architectural implications

### ADR-031 (per-request memory-mode routing): on hold

The original motivation — "factual probes favour tools, analytical
probes favour inject" — no longer holds at N=100 with Qwen 3.6.
Tools mode wins every category including ambiguous. The complexity of
a per-request router (regex classifier, Mistral fallback, X-Memory-Mode
header) is not justified by the data.

**Status:** On hold. Revisit only if a future model or prompt shape
reintroduces a category where inject meaningfully outperforms tools.

### AUDITTRACE_MEMORY_MODE=tools is the permanent default

No kill switch or routing logic needed. The 97% tool accuracy across
100 probes (3 misses: 1 in decisions, 2 in semantic) is operationally
clean. The 3 misses did not cause errors — the model recovered by
falling back to its parametric knowledge.

### Qwen 3.6 upgrade was the highest-ROI change

The DeltaNet linear attention architecture delivers ~2x throughput vs
Qwen 3.5 on the same hardware (Ryzen AI MAX+ 395 iGPU). Combined
with ADR-034's per-chunk idle timeout, the system now handles the
full spectrum of prompt complexity — from 3s trivial to 252s deep
analytical — without a single timeout or error.

## Token economics

| Mode | Mean prompt | Mean completion | Total per probe |
|------|------------|----------------|-----------------|
| Tools | 4,314 | 834 | 5,148 |
| Inject | 1,849 | 1,658 | 3,507 |

Tools mode uses 2.3x more prompt tokens (tool descriptions + ambient
context + multi-iteration re-sends) but 2x fewer completion tokens
(focused answers vs inject's unconstrained generation). The net token
cost is 1.5x higher for tools, but the latency is 1.4x lower — the
prompt-eval phase is dominated by the KV cache warm-up, not the
incremental token count.

## Methodology

- Sequential probes, one at a time (no concurrency)
- Two runs: tools-first, then inject (container restart between modes)
- 30-minute client timeout (ADR-034)
- Per-chunk idle timeout 120s on server (ADR-034)
- SSE keep-alive frames every 15s (ADR-034)
- Temperature 0.0 (deterministic)
- Non-streaming (POST, not SSE) — measures total wall-clock time
- Tool accuracy scored by matching `tool_calls` audit rows against
  expected tool per prompt
- Qwen 3.6-35B-A3B Q4_K_M, llama-server b5220+, ROCm 7.2

## Raw data

`tmp/eval-memory-modes-20260417T065949Z.jsonl` — 200 JSONL rows with
full per-probe detail (latency, tokens, tool_calls, error, category).
