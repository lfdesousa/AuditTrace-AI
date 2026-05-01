# Memory Access Modes — Ambiguous / Analytical Category (2026-04-15)

**Author:** Luis Filipe de Sousa
**Date:** 2026-04-15
**Related:** [ADR-025](ADR-025-memory-as-tools.md), [ADR-030](ADR-030-session-summarizer.md), prior 2026-04-15 runs: [clean](eval-memory-modes-20260415-clean.md) · [contention](eval-memory-modes-20260415-contention.md)
**Raw data:** `tmp/eval-memory-modes-20260415T162858Z.jsonl`
**Script:** `scripts/eval-memory-modes.py --category ambiguous --n-per-mode 10`
**Stack under test:** AuditTrace-AI at commit `fa1e522`. Full three-model topology (Qwen :11435 + nomic :11436 + Mistral :11437 at `--n-gpu-layers 10`), summariser enabled. Same stack as the contention run, different prompt category.

## TL;DR

**Inject mode DECISIVELY beats tools mode on analytical / "why X" prompts.**

| | tools | inject |
|---|---|---|
| mean latency | 175.4 s | **118.1 s** (−33 %) |
| p95 | 180.3 s | **168.1 s** (−7 %) |
| error rate | **80 %** (8/10 timeouts) | **0 %** (10/10 success) |
| tool-selection accuracy | **20 %** (only 2 probes completed) | n/a |
| mean prompt tokens | 3 730 | 2 141 |
| mean completion tokens | 708 | 1 277 |

The result is architecturally clean and statistically unambiguous at N=10.

**The LangGraph-inspired exit condition (commit `e18800c`) did NOT fire on any probe in this run.** Memory-server logs show `reached max iterations (10)` — the hard cap, not the signature-based early exit. Every iteration used distinct tool args, so the "repeated signatures" heuristic had nothing to match.

**Architectural finding worth an ADR:** memory mode should not be a single global default. Factual probes (decisions category) favour `tools`; analytical probes (ambiguous category) favour `inject`. A **per-request routing hint** (client-supplied `X-Memory-Mode` header + server-side classifier default) would let each prompt get the right path — proposed as ADR-031 follow-up.

---

## Per-probe latencies

### Tools mode — 8 timeouts / 10

| # | Lat | Result | Prompt head |
|---|---|---|---|
| 1 | 180.3 s | **TIMEOUT** | How did we decide to structure our Structurizr workspace? |
| 2 | 180.1 s | **TIMEOUT** | What recent decisions affect how I should write a new tool handler? |
| 3 | 180.1 s | **TIMEOUT** | Recap our architectural choices from the last week of sessions. |
| 4 | 180.1 s | **TIMEOUT** | What's the background on the current Traefik routing setup? |
| 5 | 180.0 s | **TIMEOUT** | How do the ADRs inform our Terraform naming practice? |
| 6 | 133.3 s | ✓ 2 tools | What recent work touched the memory tool registry? |
| 7 | 180.1 s | **TIMEOUT** | Explain the design thinking behind our ambient context builder. |
| 8 | 179.6 s | ✓ 5 tools | How has the memory-as-tools pattern evolved across sessions? |
| 9 | 180.1 s | **TIMEOUT** | What should I know before editing the chat proxy route? |
| 10 | 180.1 s | **TIMEOUT** | How do prior decisions constrain how I add a new Keycloak scope? |

The two successes (probes 6 and 8) correspond to prompts where the model converged on 2–5 tool calls quickly. The 8 failures are where the model kept exploring across *different* memory layers — `recall_decisions` then `recall_semantic` then `recall_recent_sessions` then `recall_skills`, each with a distinct query — accumulating tool_results in the prompt every round until the eval client timed out at 180 s. The server-side hard cap of 10 iterations is visible in the memory-server log as `memory tool-call loop reached max iterations (10)` — confirming the model exhausts the cap, not the client timeout, on these prompts; the client just gives up first because each iteration takes ~20–25 s.

### Inject mode — 10 / 10 success

| # | Lat | Tokens (prompt / completion) |
|---|---|---|
| 1 | 87.1 s | 1 897 / 1 054 |
| 2 | 148.2 s | 2 362 / 1 563 |
| 3 | 130.9 s | 2 305 / 1 486 |
| 4 | 98.1 s | 1 918 / 1 167 |
| 5 | 74.1 s | 1 877 / 835 |
| 6 | 100.7 s | 2 186 / 1 066 |
| 7 | 165.3 s | 2 446 / 1 890 |
| 8 | 111.5 s | 2 219 / 1 255 |
| 9 | 97.3 s | 1 972 / 1 164 |
| 10 | 168.1 s | 2 228 / 1 295 |

Every probe completed. Latencies cluster in the 75–170 s range with no outliers and no timeouts. Mean inject answer length is 4 921 chars — substantial prose responses that evidently *used* the injected 4-layer context effectively.

---

## Why the inversion from the decisions category

| | Decisions (clean run) | Ambiguous (this run) |
|---|---|---|
| tools error rate | 10 % | **80 %** |
| inject error rate | 50 % | **0 %** |
| which mode wins | tools (clear) | inject (decisive) |

The two categories have fundamentally different shapes:

**Decisions ("what did ADR-X decide")** have a clear expected tool (`recall_decisions`). The model picks it, fires one or two targeted queries, gets a concrete document snippet, composes a short answer. Tool-loop is efficient; inject is wasteful because 3 of 4 memory layers are irrelevant to the question.

**Ambiguous ("why/how/recap/explain the design of X")** don't map to one layer. The model reasonably wants to *triangulate* across ADRs + sessions + skills + semantic hits. In tools mode that means 10 iterations accumulating tool_results, each round making the prompt bigger. In inject mode it means one pre-built 4-layer context, one generation pass. The "triangulating" work happens inside the model's attention, not across round-trips.

**Generation-side token accounting confirms this:**
- Tools mode on ambiguous: mean 708 completion tokens when it succeeds — curtailed because most probes died mid-iteration.
- Inject mode on ambiguous: mean 1 277 completion tokens — the model actually *produced* the analytical synthesis it was asked for.

The inject mode is not just faster; it's *doing the work the user asked for*. Tools mode was spending its iterations fetching data, not producing synthesis.

---

## What the exit condition told us (by not firing)

The ADR-030 exit-condition cherry-pick (`e18800c`) guards against the pathology "model calls the *same* tool with the *same* args twice in a row". Across all 10 tools-mode ambiguous probes, that pathology **never occurred** — every iteration carried distinct args. The heuristic is therefore defensive code against a failure mode that does not exist in practice at this prompt shape.

The real failure mode is "model makes 10 *distinct* calls exploring different angles, prompt grows each round, per-iteration prompt-eval time climbs, client times out". A useful exit condition here would be token-budget-based ("stop when accumulated tool_result bytes exceed N") or per-tool-name-count ("stop when we've called `recall_semantic` 4 times regardless of args"). That's separate architectural work — the identity check stays in place as cheap insurance for the scenario it guards against; nothing to remove.

---

## Architectural implication — ADR-031 (future)

This eval motivates a proper design doc on **per-request memory-mode routing**. Sketch:

- Retain `AUDITTRACE_MEMORY_MODE` as the global default.
- Accept `X-Memory-Mode: tools|inject|auto` header on `/v1/chat/completions`, symmetric with `X-Project` (ADR-029).
- When `auto` (or unset), run a **classifier**:
  - Fast path: regex heuristic on the last user message (`^(why|how has|explain|recap|what should|describe)\b` → `inject`; else `tools`).
  - Slow path: if the regex is unsure, Mistral on `:11437` runs a ~200-token classifier prompt (`FACTUAL or ANALYTICAL?`). Adds ~1 s but saves 10+ s on wrong-mode timeouts.
- Client-supplied hints win over the classifier.

Not in scope for today. Documented here so the next architect (or future-me) has the motivation in one place.

---

## Caveats

- N=10 on a single category. The finding is strong (80 % vs 0 % error is not subtle), but the full sweep across 6 categories × N=100 is still the right validation before encoding this into ADR-031.
- 180 s timeout is the eval client. The memory-server proxy (300 s) would have let some of the tools-mode probes complete. Raising the eval client to 300 s would turn timeouts into slow successes, which would *still* put tools behind inject on this category but with less dramatic headlines.
- Mistral residency budget: the contention run confirmed it's in-the-noise for tools mode on decisions; on ambiguous the signal is so strong that contention is a second-order concern.
- Cold-cache vs warm-cache between sequential runs is still a confound. Not meaningful here because inject's 0 % error rate cannot be explained by cache warmth — it's a structural advantage for the prompt shape.

## Next

- Capture ADR-031 scope in `project_session_20260415.md` carry-over (already implicit from the three-eval arc).
- Task #13 (cache-controlled N=100 sweep across 6 categories) remains the right validation before committing to per-request routing in code.
