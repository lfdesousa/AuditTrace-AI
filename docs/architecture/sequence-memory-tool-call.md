# Sequence Diagram: /v1/chat/completions with `memory_mode=tools`

> **Created 2026-04-11** for ADR-025 (memory-as-tools). This document
> covers the `SOVEREIGN_MEMORY_MODE=tools` code path. For the default
> inject-mode path see `sequence-chat-completions.md`.
>
> Status: ADR-025 is **Accepted** (2026-04-12). The flow below matches
> the deployed code. Live-verified: Qwen3.5-35B-A3B selectively calls
> individual memory tools based on the question intent — the ambient
> context includes selection rules that guide the LLM to pick the most
> relevant tool instead of blast-calling all four.

## What makes tools-mode different

The default (`memory_mode=inject`) path builds a full 4-layer memory
context on every request and injects it into the system message.
Trivial prompts pay the same cost as memory-hungry ones — 4 memory
searches, 4 Langfuse spans, hundreds of prompt tokens — regardless of
whether the model actually needs the context.

Tools-mode inverts the relationship:

- **The LLM decides** which memory layer it needs by calling the
  most relevant tool — `recall_decisions`, `recall_skills`,
  `recall_recent_sessions`, or `recall_semantic`.
- The proxy injects a minimal **ambient context** (~280 words:
  identity + project + date + **selection rules** + tool hints).
  The selection rules guide the LLM to pick ONE tool per question:
  architectural decisions → `recall_decisions`, methodology →
  `recall_skills`, session continuity → `recall_recent_sessions`,
  everything else → `recall_semantic`.
- Memory tool invocations are **handled inside the proxy** (Pattern A
  from the brainstorm) via the tool-call loop — OpenCode never sees
  memory tool calls, only the final answer.
- Every memory invocation writes one row to `tool_calls` for audit,
  with a cache-hit optimisation (Redis-backed) that skips the audit
  row when the result is already known.

## Component map

```
Coding Agent
      │ POST /v1/chat/completions
      ▼
chat_completions (routes/chat.py)
      │ settings.memory_mode == "tools" ?
      ▼
_handle_tools_mode (routes/chat.py)
      │  tools_visible_to(user) → filter by scope
      │  build_ambient_context(user, project, tools)
      │  augment_messages + augment_tools
      ▼
run_memory_tool_loop (routes/_memory_tool_loop.py)
      │  non-streaming POST → llama-server
      │  inspect tool_calls → dispatch memory tools
      │  loop until finish or cap
      ▼
invoke_tool (tools/__init__.py)
      │  cache.get(key) → HIT? return + skip audit
      │  handler(user_context, args)
      │  cache.put(key, result)
      ▼
memory handlers (tools/memory_handlers.py)
      │ recall_decisions → EpisodicService.search
      │ recall_skills    → ProceduralService.search
      │ recall_recent_sessions → ConversationalService.load_sessions
      │ recall_semantic  → ChromaSemanticService.search
```

Two persistence boundaries are involved: `InteractionRecord` lands
first so `tool_calls.interaction_id` FK can be satisfied when
`_flush_pending_tool_calls` writes the audit rows.

## Happy path — memory-hungry prompt

The model decides it needs `recall_decisions`, the proxy dispatches,
the second llama.cpp call returns the final answer. Two POSTs to
llama-server, one `ToolCall` audit row, interaction row written at the
end.

```mermaid
sequenceDiagram
    participant Agent as Coding Agent
    participant Auth as require_user
    participant Handler as _handle_tools_mode
    participant Loop as run_memory_tool_loop
    participant Registry as tools_visible_to\\n+ get_tool_by_name
    participant Cache as ToolResultCache\\n(sovereign-redis)
    participant Invoke as invoke_tool
    participant Episodic as EpisodicService
    participant LLM as llama-server :11435
    participant PG as PostgreSQL

    Agent->>Handler: POST /v1/chat/completions\\n{messages: [{user: "KV cache?"}], project: "AuditTrace"}
    Handler->>Auth: Depends(require_user)
    Auth-->>Handler: UserContext (admin sentinel or Keycloak)

    Note over Handler: Build ambient context\\nprofile + project + date + tool hints
    Handler->>Registry: tools_visible_to(user)
    Registry-->>Handler: 4 memory tool defs (scope-filtered)

    Note over Handler: Merge ambient into system message\\naugmented_tools = client_tools + memory_tools

    Handler->>Loop: run_memory_tool_loop(\\n  llama_url, loop_payload,\\n  user, session_id, max_iter=5)

    rect rgb(220, 230, 250)
        Note over Loop,LLM: Iteration 1 — non-streaming

        Loop->>LLM: POST /chat/completions (stream=false)\\n{messages: [...ambient, user],\\ntools: [bash?, recall_decisions, ...]}
        LLM-->>Loop: {tool_calls: [{id: c1, name: recall_decisions,\\narguments: '{"query":"KV cache"}'}]}

        Note over Loop: All tool_calls are memory tools\\n→ dispatch, don't exit

        Loop->>Invoke: invoke_tool(user, recall_decisions_tool,\\n{"query":"KV cache"}, session_id)
        Invoke->>Cache: get(sha256(session|tool|args))
        Cache-->>Invoke: None (miss)
        Invoke->>Episodic: search(user_context, "KV cache")
        Episodic-->>Invoke: [Document(ADR-009, ...)]
        Invoke->>Cache: put(cache_id, {matches, total, truncated})
        Invoke-->>Loop: (result, was_cache_hit=False)

        Note over Loop: Append assistant tool_calls message\\n+ tool_result message to conversation\\nRecord PendingToolCall (audit row)
    end

    rect rgb(230, 250, 220)
        Note over Loop,LLM: Iteration 2 — final answer

        Loop->>LLM: POST /chat/completions (stream=false)\\n{messages: [...ambient, user,\\nassistant tool_calls, tool_result]}
        LLM-->>Loop: {content: "Based on ADR-009: 75% reduction."}\\nfinish_reason: "stop"

        Note over Loop: No more tool_calls → done
    end

    Loop-->>Handler: (final_body, [pending_tool_call])

    Handler->>PG: INSERT InteractionRecord\\n(user_id, question, answer, session_id, ...)
    PG-->>Handler: interaction_id

    Handler->>PG: INSERT ToolCall\\n(interaction_id FK, user_id, tool_name,\\ngranted_scope, duration_ms, result_summary)
    PG-->>Handler: ok

    Handler-->>Agent: 200 OK\\n{choices: [{message: {content: "Based on ADR-009..."}}]}
```

## Trivial prompt — no tool calls fired

A prompt like "ls /tmp" (or "hello") produces zero memory tool calls.
Exactly ONE POST to llama-server, zero `ToolCall` audit rows, one
interaction row. This is the cost profile we're optimising for.

```mermaid
sequenceDiagram
    participant Agent as Coding Agent
    participant Handler as _handle_tools_mode
    participant Loop as run_memory_tool_loop
    participant LLM as llama-server
    participant PG as PostgreSQL

    Agent->>Handler: POST /v1/chat/completions\\n{messages: [{user: "hello"}]}

    Note over Handler: Build ambient context (~50 words)\\nno memory search fires

    Handler->>Loop: run_memory_tool_loop(...)
    Loop->>LLM: POST (stream=false)
    LLM-->>Loop: {content: "Hi!", finish_reason: "stop"}

    Note over Loop: no tool_calls → exit immediately\\n(1 iteration, 0 pending audit rows)

    Loop-->>Handler: (final_body, [])
    Handler->>PG: INSERT InteractionRecord (user_id, ...)
    Note over PG: ZERO ToolCall rows

    Handler-->>Agent: 200 OK {content: "Hi!"}
```

## Cache hit — second identical invocation in the same session

When the model (or the user, via a second similar prompt) retrieves
the same memory tool with the same arguments inside the same session,
the Redis-backed `ToolResultCache` short-circuits the handler. **No
audit row is written for cache hits** per ADR-025 §Decision.8 — the
same execution was already audited when the cache was populated.

```mermaid
sequenceDiagram
    participant Loop as run_memory_tool_loop
    participant Invoke as invoke_tool
    participant Cache as ToolResultCache
    participant Episodic as EpisodicService

    Note over Loop: 2nd turn, same session, same args

    Loop->>Invoke: invoke_tool(user, recall_decisions,\\n{"query":"KV cache"}, session_id)
    Invoke->>Cache: get(sha256(session|tool|args))
    Cache-->>Invoke: {matches, total, truncated}

    Note over Invoke: HIT — skip handler\\nskip audit row\\nreturn (cached, was_cache_hit=True)

    Invoke-->>Loop: (cached_result, True)

    Note over Loop: was_cache_hit=True\\n→ PendingToolCall NOT appended\\n→ tool_result still sent to LLM
```

## External tool call — loop exits, body passes through

If the LLM calls a non-memory tool like `bash`, the proxy cannot
execute it. The tool-call loop exits immediately and returns the body
unchanged so the agentic client handles the external tool call.
Memory tool calls in the *same response* are NOT executed — the
model will re-emit them on the next turn if still needed.

```mermaid
sequenceDiagram
    participant Agent as Coding Agent
    participant Loop as run_memory_tool_loop
    participant LLM as llama-server

    Loop->>LLM: POST (iteration 1)
    LLM-->>Loop: {tool_calls: [{name: "bash", ...}]}

    Note over Loop: tool_calls contains "bash"\\n→ external → exit loop\\nreturn body unchanged, pending=[]

    Loop-->>Agent: passthrough: bash tool_call
    Note over Agent: Agent runs bash locally,\\nre-submits with tool_result\\nnext turn
```

## Iteration cap — defensive bound on chained tool calls

A misbehaving model that keeps emitting memory tool calls every turn
is stopped at `SOVEREIGN_MEMORY_TOOL_LOOP_MAX_ITERATIONS` (default 5).
The loop returns whatever the last body was and logs a WARNING so
operators can correlate cap-hits with bad prompts in production.

```mermaid
sequenceDiagram
    participant Loop as run_memory_tool_loop
    participant LLM as llama-server
    participant Logger as logger.warning

    loop N iterations (N = max_iter)
        Loop->>LLM: POST
        LLM-->>Loop: {tool_calls: [memory tool]}
        Note over Loop: dispatch, continue
    end

    Note over Loop: cap hit — last body still has tool_calls
    Loop->>Logger: "memory tool-call loop reached\\nmax iterations (5)"
    Loop-->>Loop: return (last_body, pending)

    Note over Loop: Caller renders whatever text/tool_calls\\nare in the body; cap-hit is not an error
```

## Streaming — stream=true via SSE synthesis

The loop is always non-streaming internally because the proxy must
inspect `tool_calls` between iterations. When the client asked for
`stream=true`, the handler synthesises an SSE wire-format response
from the final body: one content delta chunk, one finish chunk, a
synthetic usage chunk, then `[DONE]`.

```mermaid
sequenceDiagram
    participant Handler as _handle_tools_mode
    participant Loop as run_memory_tool_loop
    participant Synth as _synthesize_sse_from_body

    Handler->>Loop: run loop (non-streaming internally)
    Loop-->>Handler: final_body

    alt stream=false
        Handler-->>Handler: return final_body as JSON
    else stream=true
        Handler->>Synth: _synthesize_sse_from_body(\\n  final_body, requested_model)

        Synth-->>Handler: data: {delta: {content}}
        Synth-->>Handler: data: {finish_reason: "stop"}
        Synth-->>Handler: data: {usage: {...}}
        Synth-->>Handler: data: [DONE]

        Handler-->>Handler: return StreamingResponse
    end
```

**Known trade-off:** synthesised SSE emits the full content in ONE
chunk rather than being streamed word-by-word as llama-server would
natively do. The wire format is correct but the UX is slightly worse
than inject-mode for long answers. Phase 5's performance measurement
may motivate a native-streaming optimisation on the final iteration
only.

## Authorisation — scope filtering is enforced twice

1. **At advertisement time:** `tools_visible_to(user)` returns only
   the tools whose `required_scope` is in `user.scopes` (or bypasses
   the filter entirely for admins). The LLM only sees tools it can
   actually call.
2. **At dispatch time:** inside `_execute_memory_tool` the loop
   re-checks `tool.required_scope` against `user.scopes`. This is
   defensive — it catches stale `tool_calls` messages from earlier
   conversation turns issued before a scope revocation.

Scope-denied dispatch produces a `tool_result` with
`{"error": "scope denied: ..."}` and a `PendingToolCall` audit row
with the error populated. The loop continues.

## What this doc does NOT cover

- **Full Langfuse trace shape.** Per-tool nested spans are deferred
  to Phase 5 alongside the live-trace verification work. The current
  implementation produces a single parent chat observation without
  nested per-invocation children.
- **Async persistence.** `_persist_interaction` and
  `_flush_pending_tool_calls` are synchronous and inline in this
  first cut. Async persistence is deferred to a separate ADR — see
  brainstorm §12.
- **Memory writes via tools.** `save_decision`, `record_session` and
  similar write-side tools are out of scope for ADR-025.

## Related documents

- **ADR-025** (`docs/ADR-025-memory-as-tools.md`) — the authoritative
  design record with all decisions and success metrics.
- **Seed** (`docs/architecture/BRAINSTORM-memory-as-tools.md`) — the
  exploration that preceded the ADR.
- **Inject-mode sequence** (`sequence-chat-completions.md`) — the
  legacy path, retained as a feature flag (`SOVEREIGN_MEMORY_MODE=inject`).
- **Multi-user identity** (`ADR-026` §15) —
  how `UserContext` reaches the loop; Phase 2 of that design shipped
  in the preceding commits.
