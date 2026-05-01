---
title: "refactor(chat): split _iter_and_capture into composable helpers"
labels: ["tech-debt", "refactor", "chat-proxy"]
priority: P3
---

## Context

`src/audittrace/routes/chat.py:_iter_and_capture` (the streaming
generator inside `chat_completions`) is ~130 lines and bundles five distinct
concerns:

1. SSE line parsing + byte-equal forwarding
2. Synthetic OpenAI usage chunk injection (ADR-024 requirement for OpenCode)
3. Text content accumulation
4. Streamed `delta.tool_calls` index-merge accumulation
5. Post-stream persistence + Langfuse output recording

It also handles two error paths (`httpx.ConnectError`, `httpx.HTTPStatusError`)
inline. The total cyclomatic complexity is well above what's comfortable to
test or modify safely.

## Suggested decomposition

- `_parse_sse_chunk(payload_str) -> dict | None` — JSON-decodes one data line
- `_accumulate_delta(state, chunk)` — folds one parsed chunk into a small state
  dataclass (`StreamState`) that tracks text chunks, tool_calls_acc, model,
  finish_reason, etc.
- `_emit_synthetic_usage_chunk(state) -> bytes`
- `_finalize_stream(state) -> tuple[answer, tool_calls_list]`
- `_iter_and_capture` becomes a thin orchestrator: open AsyncClient, loop over
  lines, fold into state, yield bytes, finally call `_finalize_stream` +
  persist + record.

Side-benefit: each helper is unit-testable in isolation without httpx mocks.

## Acceptance criteria

- `_iter_and_capture` ≤ 30 lines
- All ADR-024 regression tests still pass without modification
- Added unit tests for `_accumulate_delta` covering: text-only chunk, tool_call
  initial chunk, tool_call argument fragment append, finish_reason capture,
  llama.cpp `timings` block fallback for tokens.
- No behavioural change observable from outside (byte-equal forward, same
  synthetic usage chunk, same persisted answer format).

## Out of scope

Refactoring `chat_completions` itself. Just the streaming generator.
