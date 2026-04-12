# ADR-024: Chat Proxy Pass-Through + Langfuse Trace Decoupling

**Status:** Accepted
**Date:** 2026-04-11
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-010 (async server), ADR-012 (transparent proxy augmentation),
ADR-014 (full agentic trace capture), ADR-014.4 (logging-as-aspect + OTel),
ADR-018 (four-layer memory port), ADR-021.2 (Langfuse sibling stack)

## Context

The chat completions proxy is the contact surface between agentic clients
(OpenCode, Continue, Roo Code) and the upstream `llama-server`. It is
responsible for two things only:

1. Augmenting the inbound request's system message with 4-layer memory context.
2. Forwarding the request, capturing the response for audit + Langfuse trace.

The Phase 1 port from the predecessor project's LangChain server reimplemented
both halves with a strict Pydantic schema for the request and a context-manager
based span lifetime (via `@log_call`). Five commits later, dog-fooding through
OpenCode surfaced two regressions that this ADR addresses:

### Regression A — tool calling silently broken

`ChatRequest` and `ChatMessage` declared only `model`, `messages`,
`temperature`, `top_p`, `max_tokens`, `stream`, `context_query`, `project`.
Pydantic stripped every other field on parse:

- `tools`, `tool_choice`, `parallel_tool_calls`, `response_format` on the
  request — gone before reaching `llama-server`. The model never received the
  tool definitions, so it could not produce well-formed `tool_calls`.
- `tool_calls`, `tool_call_id`, `name`, `function_call` on individual messages
  — gone on follow-up turns where OpenCode posts back a `role: tool` message
  with the tool result. The model received a tool message with no anchor and
  produced empty or hallucinated output.

User-visible symptom: *"agent executed a tool, but I see nothing in the
OpenCode console"*.

The reference implementation (`langchain_server.py:548`, working) parsed
`await request.json()` as a raw dict and forwarded every key untouched. That is
the proven pattern.

### Regression B — Langfuse renders attributes as `undefined`

Commit `1b40633` routed `telemetry.start_span` through the Langfuse SDK's
`_langfuse_client.start_as_current_observation(...)` so that
`langgraph_node` / `langgraph_step` metadata would land at the top level of
the ClickHouse observation Map (which is what triggers the Langfuse graph
view). This was correct for the metadata at span-create time.

It was wrong for `set_current_span_attributes`, which read
`opentelemetry.trace.get_current_span()` and wrote attributes via OTel only.
The Langfuse SDK does not push its observation onto the OTel current-span
context, so `get_current_span()` returned `INVALID_SPAN` and every
`gen_ai.*`, `input.value`, `output.value`, `langfuse.session.id` write was a
no-op. Langfuse's UI rendered each missing field as `undefined`.

A second issue compounded the first: the streaming generator
`_iter_and_capture` was consumed by FastAPI *after* the request handler had
returned a `StreamingResponse` object. By that point the `@log_call` context
manager had exited and closed the span. Even if the OTel write had been
correct, attributes set inside the generator (response usage, finish reason,
output text) would land on a closed observation.

## Decision

### 1. The chat proxy is a dict pass-through, not a Pydantic-typed endpoint

`POST /v1/chat/completions` accepts the inbound payload as `await
http_request.json()` and forwards it to `llama-server` unchanged except for
its `messages` field, which has memory injected into the system entry. Every
other top-level key (`tools`, `tool_choice`, `response_format`, …) and every
non-`content` field on every message (`tool_call_id`, `name`, `tool_calls`,
`function_call`, …) survives the augmentation by construction.

`ChatRequest` and `ChatMessage` remain in `models.py` as documentation /
type-hints for `tests/test_models.py`, but the route does not import them.

**Trade-off:** less IDE/test ergonomics, no auto-422 on garbage input.
Acceptable: a transparent proxy that strips fields is a footgun by design,
the OpenAI spec drifts faster than our schema can track, and we now validate
explicitly (`messages: list required → 422`, invalid JSON → 400).

### 2. Langfuse trace output is decoupled from span context-manager lifetime

Memory context build is wrapped in an `@observe`-decorated helper
(`_build_memory_context_with_trace`) that:
- runs *synchronously* off-loop via `asyncio.to_thread` so its enclosing
  Langfuse span stays open for the helper's full lifetime
- sets `gen_ai.*` request attributes inside the open span (where they land)
- captures `_lf_get_client().get_current_trace_id()` as a return value

The streaming generator receives the explicit `trace_id` and, after the
stream ends, calls `_record_langfuse_output(trace_id, ...)` — a direct
`httpx.post` to `{settings.langfuse_host}/api/public/ingestion` with
`{"id": trace_id, "output": ..., "metadata": ..., "sessionId": ...}`. This
update path does not require any span context-manager to be open. It is the
exact pattern from `langchain_server.py:248-292`, ported with one fix:
credentials come from `Settings`, not hard-coded.

### 3. `@log_call` no longer decorates the chat handler

The aspect was designed for short sync/async leaf calls. Wrapping a handler
that returns a `StreamingResponse` consumed later is a category error — the
span exits before the work runs. The explicit `@observe` on
`_build_memory_context_with_trace` owns the chat request span instead.

### 4. `set_current_span_attributes` writes to both backends when present

`telemetry.set_current_span_attributes` checks for `_langfuse_client` first
and routes attributes through `_langfuse_client.update_current_span(...)`
(metadata + first-class `input` / `output` fields). It then falls through to
the OTel write so deployments without Langfuse keep working unchanged.

### 5. Streamed `delta.tool_calls` are accumulated and rendered for audit

The streaming generator merges `delta.tool_calls` chunks by index,
concatenating `function.arguments` fragments as they arrive (mirroring
`langchain_server.py:401-417`). After the stream ends, the accumulated tool
calls are rendered as `[tool_call] name(args)` lines and appended to the
persisted answer so the audit trail and the Langfuse output field carry the
real assistant intent even when text content is empty.

## Consequences

### Positive

- Tool calling works end-to-end with OpenCode, Continue, Roo Code without
  per-client schema gymnastics.
- Future OpenAI-spec fields (`web_search`, `prompt_cache_key`, …) reach the
  upstream model with no code change.
- Langfuse traces show populated `input` / `output` / `gen_ai.*` fields
  instead of `undefined`, including for streamed tool-call responses.
- The streaming path is fully async (`httpx.AsyncClient` + `aiter_lines`) so
  it no longer blocks the event loop the way the previous sync `httpx.stream`
  call inside an `async def` did.

### Negative

- Less compile-time type safety on the request shape — relying on runtime
  `isinstance` checks at the entry point.
- `_record_langfuse_output` uses a sync `httpx.post` inside the async
  generator's tail. The blast radius is bounded (5-second timeout, all
  exceptions swallowed), but a future cleanup could move it onto an
  `httpx.AsyncClient` or a fire-and-forget background task.

### Follow-ups

- `tests/test_models.py` still validates `ChatRequest` / `ChatMessage`
  Pydantic constraints. The models are now docs-only — keep or delete in a
  follow-up cleanup ADR if a route ever needs them again.
- The `@log_call` aspect could grow a "skip span on `StreamingResponse`
  return type" guard so other streaming routes can stay decorated. Out of
  scope for this ADR.

## Regression history

| Commit    | Change                                                            |
|-----------|-------------------------------------------------------------------|
| `efd031d` | feat(chat): session_id + interaction persistence + token capture  |
| `0fc54a4` | fix(chat): inject synthetic OpenAI usage chunk in stream          |
| `908015c` | feat(telemetry): enrich spans with input/output + gen_ai conv     |
| `e83dea4` | feat(telemetry): add sovereign.component + dashboard recipes      |
| `1045235` | feat(telemetry): friendly kebab-case span names                   |
| `12396ce` | feat(telemetry): emit langgraph.node attribute                    |
| `1b40633` | feat(telemetry): route spans through Langfuse SDK ← **regression source** |

The Langfuse SDK routing was the right call (the graph view depends on it)
but the telemetry shim was not updated to write through both backends. ADR-024
keeps the SDK path and fixes the shim.

---

## Implementation history (debugging journal)

The fix landed in three iterations because two follow-up bugs surfaced only
under live OpenCode traffic, not the unit tests. Documenting them so future
me doesn't repeat the same mistake of relying on mocked tests for behaviour
that depends on Langfuse SDK contextvar plumbing.

### Iteration 1 — initial port forward (April 2026)

- Replaced strict `ChatRequest`/`ChatMessage` Pydantic with raw-dict pass-through.
- Made `_iter_and_capture` async (`httpx.AsyncClient.stream` + `aiter_lines`).
- Added `delta.tool_calls` index-merge accumulator.
- Added `@observe`-decorated `_build_memory_context_with_trace` returning
  `trace_id` as a value.
- Added `_record_langfuse_output` for post-stream trace updates via the
  ingestion API.
- Stripped `@log_call` from `chat_completions`.
- Added `set_current_span_attributes` Langfuse SDK routing in `telemetry.py`.
- **Result**: tool calling worked end-to-end in OpenCode for the first time.
  Live smoke test confirmed `bash` tool fired and stdout rendered in the panel.

### Iteration 2 — `@log_call` aspect bypassed the new helper

User reported that memory layer spans still showed `Input: undefined` and
`Output: undefined` in Langfuse. Diagnosis:

- `_set_span_input` / `_set_span_output` in `logging_config.py` wrote
  attributes via `span.set_attribute("input.value", ...)` directly on the
  span handle yielded by `start_as_current_observation`.
- `set_attribute` lands as an OTel attribute nested under
  `metadata.attributes` in ClickHouse — Langfuse's UI does not surface those
  in the Input/Output panels, only the SDK first-class fields populated via
  `update_current_span(input=..., output=...)`.

**Fix**: routed `_set_span_input` / `_set_span_output` through
`telemetry.set_current_span_attributes` (which my Iteration 1 fix had already
taught to use the SDK path).

Also fixed the Dockerfile root cause: it hard-coded a pip install list that
omitted `opentelemetry-instrumentation-fastapi` (silently masked because
`langfuse` pulls in OTel core transitively). Switched to `pip install -r
requirements.txt`. While doing so, discovered `psycopg2-binary` was in the
old hard-coded list but missing from both `requirements.txt` and
`pyproject.toml`. Added it.

Made `_record_langfuse_output` async at the same time to stop blocking the
event loop inside the streaming generator's tail.

### Iteration 3 — input clobber via implicit `None` kwargs

User reported the top-level chat span showed input/output, but nested memory
sub-spans still showed `undefined`. Live container introspection (not unit
tests) revealed two stacked bugs:

**Bug 3a — `update_current_span(input=None)` does not no-op, it CLEARS.**
The Langfuse v4 SDK treats explicit `None` as "set this field to None".
`set_current_span_attributes` was passing both `input` and `output` kwargs
unconditionally — so the second call (for `output.value` after the function
ran) passed `input=None`, wiping the first call's input write.

Fix: build the SDK kwargs dict conditionally — only include keys that are
actually present in the incoming attributes.

**Bug 3b — `len(args) > 1` gate skipped self-only methods.**
`_set_span_input` had a guard `if payload:` after building a payload that
only included `args[1:]` (skipping `self`) and non-empty `kwargs`. For
methods like `FileEpisodicService.load(self)` with no args after `self`,
the payload stayed empty `{}`, the guard was falsy, and `input.value` was
never written → `Input: undefined` in the UI.

Found via a live trace inside the running container, monkey-patching
`_langfuse_client.update_current_span` and counting calls during a real
`DefaultContextBuilder.build_system_context` invocation. The trace showed
`memory-episodic-load` had zero `update_current_span` calls — confirming
the path bypass.

Fix: drop the `if payload:` guard. Always emit `input.value`, even as `{}`,
because `{}` is meaningful ("called with no args") while `undefined` is
actively misleading. Same fix for `_set_span_output` when `result is None`
— `null` is a valid display, `undefined` is not.

### Lessons captured

1. **Mocked tests can pass while the live integration is broken.** All my
   Iteration 1 + 2 unit tests passed because they patched
   `telemetry._langfuse_client` directly with a `MagicMock`, bypassing the
   real SDK contextvar plumbing. Verification needed a live trace against
   the running container, not assertions about which methods were called on
   a mock.
2. **`update_current_span(field=None)` is destructive in the Langfuse v4
   SDK.** Build kwargs conditionally for partial updates. Never trust that
   passing `None` is a no-op.
3. **`len(args) > 1` is not a safe "skip self" heuristic.** Self-only and
   no-arg-after-self methods exist. Either always emit the payload (even
   empty) or detect bound methods explicitly.
4. **Per-file coverage gates are not a luxury.** The `--cov-fail-under=90`
   project-wide gate hid telemetry.py dropping to 79% during the refactor
   because other files held the average. Added
   `scripts/check-per-file-coverage.py` and wired it into `make test` so
   each component must stand on its own.

### Test additions

Beyond the tests added in Iteration 1 (tool-calls regression, async streaming
mocks, raw-dict pass-through, `@observe` trace_id capture), Iterations 2-3
added:

- `test_log_call_emits_input_for_self_only_methods` — guards against
  reinstating the `len(args) > 1` gate.
- `test_log_call_emits_output_for_none_returning_function` — guards against
  reinstating the `result is None` early-return.
- `test_set_current_span_attributes_does_not_clobber_input_with_none` —
  asserts the SDK kwargs are built conditionally so the second call's
  `output=...` write does not pass `input=None`.
- `test_set_current_span_attributes_routes_to_langfuse_sdk` — happy-path
  routing assertion.
- `test_init_langfuse_client_*` — three tests for the SDK init paths
  (configured / disabled / missing keys).
- `test_init_telemetry_wires_otlp_exporters_when_endpoint_set` — covers
  the OTLP span + metric exporter wiring.
- Plus targeted tests on `services/episodic.py`, `services/procedural.py`,
  `services/context_builder.py`, and `db/factory.py` to bring each file
  over the new per-file 90% gate.

Final state at end of session: **246 tests passing, 95.36% total coverage,
21 files all ≥ 90% per the per-file gate.**
