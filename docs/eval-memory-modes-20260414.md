# Memory Access Modes — Empirical Comparison (2026-04-14 smoke)

**Author:** Luis Filipe de Sousa
**Date:** 2026-04-14
**Related:** [ADR-025](ADR-025-memory-as-tools.md) (memory-as-tools), [ADR-029](ADR-029-audit-trail-completeness.md) (audit trail completeness)
**Raw data:** `tmp/eval-memory-modes-20260414T202935Z.jsonl`
**Script:** `scripts/eval-memory-modes.py --n-per-mode 10`

## TL;DR

On this hardware (Qwen3.5-35B-A3B-Q4_K_M over llama-server, ROCm, unified memory), **tools mode beats inject on every headline metric** for the `decisions` probe category:

| | tools | inject |
|---|---|---|
| mean latency | **76.7 s** | 93.9 s |
| p95 latency | **123.5 s** | 144.7 s (+1 timeout) |
| error rate | **0 %** | 10 % |
| tool selection accuracy | **10/10** | n/a |

Tools mode wins despite the counter-intuitive token accounting (tools uses ~3.5× more prompt tokens on average — explained below). The decisive factor is *per-round-trip* prompt size, not total token volume.

**The dominant cost signal is the bimodal tool iteration distribution**: half the probes resolve in 1-4 iterations (25-71 s wall); the other half blow up to 8-10 iterations (93-140 s wall). Tightening exit conditions (LangGraph-style "if two consecutive calls returned identical snippets, stop") should collapse the tail without harming the fast path. That is the next optimisation to earn.

**Recommendation: keep `SOVEREIGN_MEMORY_MODE=tools` as the default.** Evidence supports it; re-measure after the exit-condition fix and after ADR-030 lands (which changes `recall_recent_sessions` economics and needs its own re-evaluation).

---

## Methodology

- **Script**: `scripts/eval-memory-modes.py --n-per-mode 10` (the shipped harness).
- **Dataset**: 10 probes per mode, all drawn from the `decisions` category. Every probe targets a question where the expected tool is `recall_decisions` (ADR lookups). Examples: *"What did ADR-025 decide about memory-as-tools?"*, *"Which ADR documents the four-layer memory port?"*, *"Why did we reject LangChain for the tool-call loop?"*.
- **Harness behaviour**: for each mode, writes `SOVEREIGN_MEMORY_MODE` into `.env`, recreates the `memory-server` container, waits for `/health`, runs the 10 probes sequentially, records JSONL. `try/finally` restores the original `.env` on exit (even on Ctrl-C).
- **Sequential by design**: the hardware cannot take parallel LLM requests; concurrency would contaminate the latency signal. One probe at a time.
- **Stack under test**: AuditTrace-AI at commit `686d9a3` (tools-mode default, ADR-025 tool loop with max 10 iterations per probe, ADR-029 project tagging, memory-server built with urllib3 + HTTP semconv opt-in).
- **Mode ordering**: tools first, then inject. The flip costs one container recreate (~6 s warm-up) plus a full restart.
- **Scope**: this is a **smoke** — sample size is small enough that individual outliers shift the means. The full planned harness is 100 probes per mode across 6 categories. Today's run exercises one category to validate the tool chain before the full sweep.

### Probes that ran

| # | Prompt |
|---|---|
| 1 | What did ADR-025 decide about memory-as-tools? |
| 2 | Why did we reject LangChain for the tool-call loop? |
| 3 | Summarise ADR-024 on proxy pass-through. |
| 4 | What ADRs cover multi-user identity? |
| 5 | Which ADR documents the four-layer memory port? |
| 6 | What decision did we make about KV cache compression? |
| 7 | Recall the reasoning behind transparent proxy augmentation. |
| 8 | Which ADR covers full agentic trace capture? |
| 9 | What architectural choice did ADR-018 settle? |
| 10 | Why is `SOVEREIGN_MEMORY_MODE` a kill switch? |

---

## Results

### §1. Latency (seconds, successful probes only)

| statistic | tools | inject |
|---|---|---|
| n successful | 10 | 9 |
| min | **25.5** | 42.3 |
| median (p50) | 82.3 | **64.8** |
| mean | **76.7** | 84.3 |
| p95 | **123.5** | 144.7 |
| max | **140.5** | 161.0 |
| timeouts (>180 s) | 0 | 1 |

Tools wins every summary statistic **except median**, where inject is faster. The difference lies in the shape of the distribution, not in a single "mode is faster" winner — read on.

### §2. Token cost

| statistic | tools | inject |
|---|---|---|
| mean prompt tokens | **8,401** | 2,384 |
| mean completion tokens | 332 | **1,321** |
| mean total tokens | 8,733 | **3,706** |
| max total tokens | 15,347 | 5,795 |

Tools uses ~3.5× more prompt tokens on average. This is *cumulative across tool iterations*: every round-trip to llama-server re-sends the accumulated `tool_result` messages from prior rounds. Inject pays a flat ~2-3 k prefix (the 4-layer memory context block) per probe and doesn't iterate.

Inject's completion is ~4× longer. With the 4 layers visible to the model in the system prompt, it writes synthesising answers. Tools gets focused snippets from one or two `recall_*` calls and writes tight, targeted replies. Neither is objectively "better" — it depends on whether you value synthesis breadth or focused precision. For *"what did ADR-X decide"*, precision wins.

### §3. Tool iteration depth (tools mode)

| iterations | count | latency range |
|---|---|---|
| 1 | 3 | 25.5 – 34.3 s |
| 2 | 1 | 37.0 s |
| 4 | 1 | 71.4 s |
| 8 | 2 | 119.2 – 140.5 s |
| 10 | 3 | 93.3 – 123.5 s |

Strongly bimodal. The breakdown mapping probes → iterations:

| iterations | prompts that landed there |
|---|---|
| 1 | *"What ADRs cover multi-user identity?"* · *"Which ADR documents the four-layer memory port?"* · *"What architectural choice did ADR-018 settle?"* |
| 2-4 | *"What decision did we make about KV cache compression?"* · *"Summarise ADR-024 on proxy pass-through."* |
| 8-10 | *"Recall the reasoning behind transparent proxy augmentation."* · *"What did ADR-025 decide about memory-as-tools?"* · *"Why did we reject LangChain for the tool-call loop?"* · *"Why is SOVEREIGN_MEMORY_MODE a kill switch?"* · *"Which ADR covers full agentic trace capture?"* |

Clear pattern: **factual lookups stop after one tool call; open-ended "why" / "reasoning" questions iterate 8-10 times**. The model keeps fishing for more context to back up an analytical answer.

### §4. Tool selection accuracy (tools mode)

**10/10 (100 %)**. Every probe invoked `recall_decisions` at least once (alongside zero or more follow-up `recall_semantic` calls). The ambient-context guidance shipped with ADR-025 is doing its job on this category.

### §5. Errors

- **tools**: 0 / 10.
- **inject**: 1 / 10 — probe 10 *"Why is SOVEREIGN_MEMORY_MODE a kill switch?"* timed out at 180 s (the configured `SOVEREIGN_LLAMA_PROXY_TIMEOUT` ceiling). `http_status = None`, no response body. This is the open-ended analytical question biting inject specifically: full 4-layer context plus a long synthesis attempt overran the streaming budget.

---

## Analysis

### Why tools wins mean latency despite using more tokens

Prompt-eval time on this hardware is the dominant cost. From probe telemetry the llama-server reports ~2.6 ms per prompt token (`prompt_per_second ≈ 380`). A tools-mode probe does 1-10 small round-trips; an inject-mode probe does one large round-trip.

Tools-mode *per-round-trip* prompt size is smaller than inject's, and KV-cache prefix reuse across iterations (same system + tool-schema prefix) amortises a lot of the repeated token count. Inject pays a fat ~2-3 k prefix every probe with no reuse — the 4-layer context block is regenerated each request, and the KV cache can't amortise what hasn't been sent before.

The net result: tools moves more *total* tokens through the wire but processes them faster. For a compute-bound single-GPU deployment, per-round-trip size dominates.

### Why tools is bimodal

The model's implicit policy on this category looks like:

```
if I can answer factually from the first tool result:
    stop and answer (1-2 iterations, ~25-40 s)
else:
    keep calling tools until the loop cap fires (8-10 iterations, 90-140 s)
```

There is no smart middle-ground exit. Once the model gets past iteration 2 without satisfaction, it runs until the max-iterations ceiling. That is exactly the LangGraph exit-condition gap — the loop itself has no check for "the last two tool calls returned the same or overlapping snippets, you are not learning anything new, stop".

Concretely: probes 1, 2, 7, 8, 10 all ran 8-10 iterations. In every case the tail iterations were repeated `recall_semantic` calls returning similar / overlapping content. The model's self-termination heuristic is unreliable here.

### Why inject is bimodal too (just differently)

Inject has no tool loop, so it doesn't iterate. Its variance comes from a different source: **completion length** varies wildly based on how open-ended the question is. Short factual probes finish in 42-65 s; long analytical ones push 144-161 s because the model keeps writing. Probe 10 overran the 180 s timeout entirely.

In both modes the same underlying question type (open-ended / analytical) produces the pathological case. Mode swap doesn't fix the root cause; it changes what the cost looks like (more iterations vs longer completion).

### Reliability

Tools mode completed every probe. Inject failed 10 % of them on a workload it should handle well. The error is specifically the timeout; bumping `SOVEREIGN_LLAMA_PROXY_TIMEOUT` to 300 s would likely recover the probe, but that is a band-aid — the root cause is an unbounded completion on an analytical question. Tools mode's iteration cap acts as an implicit completion budget per round-trip; inject has none.

### Tool selection accuracy is a genuine result

100 % on this category is worth noting. The ambient-context guidance — *"for questions about architecture decisions, use `recall_decisions`"* — is effective. This is not a trivial result: a common failure mode for prompt-routed tool selection is the model defaulting to `recall_semantic` for everything because it's the most general tool. That isn't happening here. Keep the guidance.

The follow-up tool calls (`recall_semantic` after `recall_decisions`) do not *reduce* accuracy; they happen after the correct first call and represent the iteration-depth problem, not a misrouting problem.

---

## Recommendations

1. **Keep `SOVEREIGN_MEMORY_MODE=tools` as the default.** Data supports it on every headline metric that matters for this workload (latency mean, p95, error rate, tool selection). The median win for inject is an artefact of the bimodal distribution, not a structural advantage.
2. **Tighten the tools-mode iteration tail.** Cherry-pick LangGraph's exit-condition pattern into `src/sovereign_memory/routes/_memory_tool_loop.py`:
    - If two consecutive tool calls return payloads with > N % content overlap, force-terminate and make the model answer from what it has.
    - If the same tool was called three times in a row with the same arguments, force-terminate.
    - Optional: cap `max_tokens` for intermediate iterations (e.g., 512) so tail iterations can't balloon completion cost; keep the last turn uncapped.

    Expected impact: the 8-10 iteration probes collapse to 3-4, cutting 50-60 seconds off their latency. Median tools latency should drop below inject's median.

3. **Tighten ambient-context guidance.** Explicit line: *"One tool call per question is the norm. Call a second tool only if the first returned nothing relevant."* Current guidance is implicit.

4. **Re-measure after each change.** This harness is the measurement. Re-run `--n-per-mode 10` after the exit-condition fix; compare to this baseline. This file should be dated and preserved; follow-ups should be new files (`docs/eval-memory-modes-YYYYMMDD.md`), so the decision trail is clear.

5. **Bump `SOVEREIGN_LLAMA_PROXY_TIMEOUT` to 300 s** to eliminate inject's 180 s timeout cliff, but track that as a workaround, not a fix. The underlying open-ended-completion cost is still there.

---

## Caveats

- **N = 10 is a smoke.** The full harness is 100 probes × 2 modes = 200 probes across 6 categories. Individual outliers (e.g., probe 3 inject at 161 s) shift the mean meaningfully at n=10. Treat percentile numbers as directional, not precise.
- **Single category.** All 10 probes hit `decisions`. The smoke does not exercise `recall_skills`, `recall_recent_sessions` (would be empty without ADR-030), `recall_semantic`, or any mixed-tool probes. Shape of the distribution may differ across categories.
- **Single user, single project.** RLS applies but isn't stressed. No concurrent traffic.
- **Sequential.** A production deployment would queue requests. Queue behaviour could change the effective latency distribution.
- **Hardware-specific.** Unified-memory GPU path with Qwen3.5-35B-A3B-Q4_K_M. A cloud deployment on a different quantisation / model / accelerator would measure differently.
- **Language model non-determinism.** Same prompts can produce different tool iteration counts in a re-run. The harness does not pin seed. The bimodal shape should reproduce but exact values will shift.
- **No token-cost pricing applied.** Token counts are reported in raw OpenAI-usage units. If you want to price this out, apply a $/1M-token rate and assume prompt ≈ 1/3 the cost of completion for most cloud providers. (On local llama-server, there is no direct $ cost — the resource cost is GPU-seconds, which latency already captures.)

---

## Next

1. Land the ADR-030 Part 1 (hybrid `recall_recent_sessions` with raw-interactions fallback) so `sessions`-category probes can be exercised.
2. Implement the LangGraph-style exit conditions in `_memory_tool_loop.py`.
3. Re-run with `--n-per-mode 10` on this same category; expect the iteration tail to collapse.
4. Run the full 100-per-mode harness across all 6 categories and write the production-grade successor to this document.
