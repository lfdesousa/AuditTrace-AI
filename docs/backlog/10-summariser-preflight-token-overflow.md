---
title: "summariser: pre-flight token count + truncate before calling llama-server"
labels: ["reliability", "tech-debt", "summariser"]
priority: P2
---

## Context

The session summariser (`src/audittrace/services/session_summarizer.py`)
builds a prompt from the full interaction transcript of a session and
`POST`s it to the local llama-server at
`http://audittrace-llm-summarizer:11437/v1/chat/completions`. If the
rendered prompt's token count exceeds llama-server's `--ctx-size`,
llama-server rejects the request with `400 Bad Request` in ~40 ms
(fail-fast on context overflow).

The summariser catches the `HTTPStatusError`, leaves `sessions.
summarized_at` NULL (per the code comment at `session_summarizer.py:269`),
and the row stays in the eligibility set. On the next cycle (default
5 min) the summariser picks the same row, builds the same
over-ctx-window prompt, and gets the same 400 again.

**Result**: one stuck session generates one error span every 5 minutes
indefinitely. Langfuse fills with retry noise and the operator can't
tell real failures from the stuck loop.

### Observed incident

- **2026-04-22 to 2026-04-24.** Session
  `opencode-2026-04-22-b72148945f88d1703bbb09e17921a3e0` (18 interactions,
  ~32 minutes of OpenCode activity on the AuditTrace-AI repo itself)
  got stuck on a llama-summarizer running with `--ctx-size 8192`.
  ~550 failed retries over 2 days cluttered Langfuse traces.
- **Immediate fix applied 2026-04-24:** (1) inserted a sentinel
  `sessions` row for the stuck session with
  `model='sentinel-skip-ctx-overflow'` so the eligibility query drops
  it; (2) bumped `scripts/start-summarizer-llama.sh` default
  `CTX_SIZE` from 16384 → 32768 (matches Mistral-7B-Instruct-v0.3's
  trained ctx window), restarted llama-summarizer. KV cache cost at
  32768 with Q4 cache ≈ 1 GB.

### Why the immediate fix is not enough

- **Ceiling still exists.** Any session whose rendered transcript
  exceeds 32768 tokens will hit the same 400-loop. Long-running
  OpenCode sessions on large codebases routinely cross that
  threshold.
- **Sentinel rows are manual.** Each stuck session needs a hand-
  crafted `INSERT` to unblock. Not scalable.
- **Langfuse still shows the failure class the same way** as other
  400s, which backlog #09 identifies as a UX problem.

## Fix sketch

### Primary — pre-flight in `_call_llm`

Before sending the prompt, count tokens (use llama-server's
`/tokenize` endpoint; cost is ~20 ms and already on the hot path to
Mistral). Compare to `ctx_size`. Branch:

- **Fits** → send as today.
- **Over ctx** → three strategies, in order of preference:
  1. **Truncate transcript** by dropping oldest turns until the
     prompt fits below, say, `0.9 * ctx_size` (reserve headroom for
     the response). Record the drop in the resulting
     `sessions.summary` as `"[truncated: first N of M turns omitted,
     summariser ctx=CTX]"`.
  2. **Chunk + reduce.** Summarise long sessions in two passes:
     first-pass summaries per chunk, second-pass summary of
     summaries. Only when truncation would lose material the
     auditor needs.
  3. **Hard-skip with sentinel row.** If even one chunk exceeds ctx
     (pathological), insert a sentinel `sessions` row like the
     manual 2026-04-24 fix, with
     `model='sentinel-skip-ctx-overflow-auto'` and a summary
     explaining what was dropped and why. This fails the retry loop
     without clobbering the transcript data.

All three branches set `sessions.summarized_at` so the row leaves
the eligibility set. No retry loop.

### Secondary — emit a distinct error class

When we skip a session for ctx overflow, emit a dedicated metric
counter (`audittrace_summariser_skipped_ctx_overflow_total`) and a
span attribute on the trace. Do not reuse the generic
`summariser_failed_total` counter — a sentinel skip is a
known-and-handled outcome, not a failure. This dovetails with
backlog #09's "distinguish error-path from empty-result" work.

### Tertiary — config discoverability

Surface the llama-summarizer ctx size on the `/health` endpoint
(`summariser_ctx_tokens`) and in `audittrace-login`'s status output
so an operator can see in one command whether a ctx bump is needed.

## Acceptance

- Unit test: prompt with known > `ctx_size` token count triggers
  the truncate branch and `summarized_at` is set.
- Unit test: prompt at chunk-threshold triggers the chunk+reduce
  branch and produces a summary.
- Integration test: a simulated 400-ctx-overflow response from
  llama-server causes exactly **one** retry, not infinite.
- Live smoke: with llama-summarizer at `--ctx-size 4096`, submit a
  long OpenCode session and verify the row is summarised (possibly
  with a truncation note) rather than stuck.

## Cross-references

- `project_summarizer_400.md` — original detection memory, updated
  2026-04-24 with the immediate-fix outcome.
- Backlog #09 — distinguish error-path from empty-result in span
  output; related rendering-side concern.
- `scripts/start-summarizer-llama.sh` — ctx-size default lives
  here; 32768 after 2026-04-24.
- ADR-030 — summariser design (current behaviour assumes
  transcripts fit; this issue is the bound-case handling the ADR
  did not spell out).
