# Brainstorm: Memory Layers as LLM-Callable Tools

> **Status:** **SUPERSEDED** — historical exploration record. Kept for
> archival reasons; do not use as the current design.
>
> - **Memory-as-tools** decisions: see `docs/ADR-025-memory-as-tools.md`
>   (Proposed, Phase 7a live-verified on 2026-04-11). Phases 0-6
>   shipped; Phase 7b dogfood canary + Phase 8 status flip pending.
> - **Multi-user / liability** (§13 below): see
>   `docs/ADR-026-multi-user-identity.md` §15 (Keycloak-
>   delegated identity) and §16 (end-of-2026-04-11 status snapshot).
>   Phases 0-3 shipped; Phases 4/5/6/7 pending (Option B).
> - **Async persistence** (§12 below): deferred to a separate ADR. Not
>   yet written.
>
> **Date:** 2026-04-11 (original brainstorm) — revised morning/afternoon
> of the same day.
> **Author:** Luis Filipe de Sousa (with Claude as scribe).
> **Trigger:** Live observation that every chat request fires all 4 memory
> layers regardless of whether the prompt needs them. "Do an `ls`" pays the
> same memory cost as "explain ADR-014".

This document was the original exploration of the design space — three
patterns (proxy intercepts, MCP, hybrid), eleven orthogonal decisions,
eight risks, twelve open questions. It **deliberately did not pick a
winning option** at the time. The decisions were made in
`ADR-025-memory-as-tools.md` later the same day:

- **Pattern:** C (minimal ambient context) + A (proxy-internal tool-call
  loop) — ADR-025 §Decision.1 and §Decision.2
- **Pure Python** tool registry, **no Langchain** — ADR-025 §Context
  "Why not Langchain"
- **Dynamic decorator-based registration** with optional TOML overlay
  — ADR-025 §Decision.3
- **Configurable iteration cap** via
  `AUDITTRACE_MEMORY_TOOL_LOOP_MAX_ITERATIONS` — ADR-025 §Decision.2
- **Redis-backed tool result cache** (audittrace-redis, disjoint
  namespace from TokenCache) — ADR-025 §Decision.8
- **Audit via `tool_calls` rows** with interaction_id FK; cache hits
  skip audit — ADR-025 §Decision.5
- **Kill switch** `AUDITTRACE_MEMORY_MODE={inject|tools}`, default
  `inject` until the dogfood canary completes — ADR-025 §Decision.4

Read ADR-025 first. Come back here only for the design-space archaeology
(what options were considered, what trade-offs were rejected and why).

---

## 1. The problem

Today's flow (post-ADR-024, see `sequence-context-build.md`):

```
OpenCode  →  POST /v1/chat/completions
              │
              ▼
          chat_completions(http_request)
              │
              ▼
          _build_memory_context_with_trace
              │  (always — every request, every prompt)
              ▼
          DefaultContextBuilder.build_system_context_with_stats
              │
              ├──► EpisodicService.search        (always)
              ├──► ProceduralService.search      (always)
              ├──► ConversationalService.as_context + load_sessions  (always)
              └──► ChromaSemanticService.search  (always)
              │
              ▼
          system_message = profile + ADRs + skills + sessions + RAG
              │
              ▼
          forward to llama-server
```

**Observed cost on a trivial prompt** ("do an `ls /tmp`"):

- 4 memory searches fired
- 4 ChromaDB queries (semantic layer hits the embedding server twice — once
  to embed the query, once to retrieve)
- 4 Langfuse spans (`memory-episodic-search`, `memory-procedural-search`,
  `memory-conversational-load`, `memory-semantic-search`) plus their parents
- System prompt inflated with hundreds to thousands of tokens of ADR/skill
  content the model will never reference
- Latency penalty ~200-500ms before llama.cpp even starts decoding
- Token waste in both directions (prompt tokens for the augmented context,
  KV cache footprint for the rest of the conversation)

**The waste compounds.** Every follow-up turn on the same conversation
re-fires all four layers, even when the model already has the context from
the previous turn. A 10-turn debugging session pays the memory tax 10 times.

## 2. The intuition

**The LLM knows what it needs to know better than we do.**

For "do an `ls /tmp`", the LLM needs *zero* memory context — it just needs
the bash tool. For "what did we decide about KV cache compression?", the
LLM needs `episodic` (ADR-009) and probably `semantic`. For "remind me of
what we worked on yesterday", the LLM needs `conversational`. For "how do
I structure a Structurizr DSL?", the LLM needs `procedural`.

In the current architecture **we** make all four guesses on every prompt.
The proposal: **let the LLM choose**. Each memory layer becomes a tool
the LLM can call when it decides the prompt warrants it. Trivial prompts
pay nothing. Memory-hungry prompts call exactly the layers they need.

This is exactly the same shift the broader LLM ecosystem made when it moved
from "stuff everything into the context window" to "tool-augmented reasoning".
We are doing the latter for code execution (`bash`, `read_file`) — there is
no reason memory should not follow the same pattern.

## 3. What changes structurally

### Today
```
ContextBuilder is a DEPENDENCY of the chat handler.
Memory is INJECTED into the system message.
The LLM is a PASSIVE consumer of pre-fetched context.
Cost is paid PER REQUEST regardless of need.
```

### Proposed
```
ContextBuilder becomes a TOOL REGISTRY.
Memory layers are TOOLS the LLM can call.
The LLM is an ACTIVE consumer of memory.
Cost is paid PER MEMORY CALL the LLM actually makes.
```

The chat handler stops being a memory orchestrator. It becomes a thin
proxy that:

1. Receives the OpenCode request
2. Adds the memory tool definitions to the `tools` array (next to OpenCode's
   own `bash` / `edit_file` tools)
3. Forwards everything to llama.cpp
4. Handles the tool-call loop (more on this below — it's the central design
   question)

## 4. Design options

There are three plausible patterns. Each has cost/complexity trade-offs.

### Pattern A — Proxy intercepts memory tool calls

The proxy knows which tools are "memory tools" and handles them itself,
invisibly to OpenCode. From OpenCode's POV, it sends one chat request and
receives one streamed response — but inside the proxy there may be 1, 2,
or N llama.cpp round-trips.

```
OpenCode  →  POST /v1/chat/completions
              │  tools = [bash, read_file, edit_file]   ← OpenCode's tools
              ▼
          chat_completions
              │
              │  augment tools = [bash, read_file, edit_file,
              │                   recall_decisions,         ← memory tools
              │                   recall_skills,            ← injected by
              │                   recall_recent_sessions,   ← the proxy
              │                   recall_semantic]
              ▼
          forward to llama.cpp
              │
              ▼
          llama.cpp returns: tool_calls = [recall_decisions("KV cache")]
              │
              ▼
          ┌──────────────────────────────────┐
          │ proxy intercepts the tool call    │
          │  - executes EpisodicService.search│
          │  - appends an assistant msg       │
          │  - appends a tool result msg      │
          │  - re-calls llama.cpp             │
          └──────────────────────────────────┘
              │
              ▼
          llama.cpp returns: actual final text
              │
              ▼
          stream final response to OpenCode
```

**Pros**
- OpenCode sees a normal chat completion. No client-side changes.
- Same proxy is the integration point — keeps deployment simple.
- The 4-layer service code stays exactly where it is, just called from a
  different place.
- Memory tools are guaranteed available across every agent that talks to
  the proxy (OpenCode, Continue, Roo Code, future clients). Single point
  of truth.
- Single Langfuse trace can capture both the model's reasoning AND the
  memory tool calls in one continuous flow.

**Cons**
- The proxy becomes stateful across what looks like one request. The
  streaming generator gets gnarly: first call must be non-streaming (need
  to inspect tool_calls before deciding what to do next), only the FINAL
  llama.cpp call streams to OpenCode.
- OpenCode-side tools and memory tools share the same `tools` array. The
  proxy must distinguish them so it doesn't try to execute `bash` itself
  and doesn't forward `recall_decisions` to OpenCode for execution.
- Multi-turn loops: a model might call `recall_decisions`, then
  `recall_semantic`, then start text — the proxy needs a tool-call loop
  with a sane iteration limit.
- Latency: prompts that need memory now pay 2x model round-trips (or 3x,
  4x for chained memory queries).

**Status:** Most aligned with the existing transparent-proxy design. Highest
implementation cost. Cleanest from the agent client's perspective.

---

### Pattern B — Memory exposed via MCP server

Sovereign-memory-server grows a second surface: an MCP (Model Context
Protocol) server alongside the existing chat proxy. OpenCode (and Continue,
Roo Code) configure the MCP server as a tool source. The chat proxy stops
augmenting the request with memory altogether — it becomes a pure
transparent proxy.

```
OpenCode configured with:
  - LLM endpoint  : audittrace-server (transparent proxy)
  - MCP server    : audittrace-server  (memory tools)
  - Local tools   : bash, read_file, edit_file
              │
              ▼
OpenCode discovers MCP tools at startup, advertises them in `tools`
              │
              ▼
chat request → tools = [bash, edit_file, recall_decisions, recall_skills, ...]
              │
              ▼
llama.cpp returns: tool_calls = [recall_decisions("KV cache")]
              │
              ▼
OpenCode sees the tool call, dispatches to the MCP server, gets a result
              │
              ▼
OpenCode loops: appends tool result, re-sends to chat completions, etc.
              │
              ▼
llama.cpp returns final text
```

**Pros**
- Architecturally cleanest separation. Proxy is a proxy. Memory is a service.
- The MCP standard means any agent client that supports MCP gets memory
  tools for free — including future clients we don't know about yet.
- The orchestration loop (call → tool → call → tool → final) lives in
  OpenCode, not in the proxy. The proxy stays simple and stateless.
- Streaming is unchanged — final response is always streamed normally.
- Each tool call is a discrete HTTP request to the MCP server, easily
  observable.

**Cons**
- Requires every client to be an MCP consumer. OpenCode and Continue support
  MCP today; Roo Code's support may be partial.
- Two separate Langfuse traces per memory-using prompt: one for the chat
  completion, one (or more) for the MCP tool calls. Stitching them together
  in the UI requires explicit trace_id correlation.
- More moving parts in the deployment (now there's an MCP HTTP server plus
  the chat proxy, both inside the same Python process or as siblings).
- Less control over the tool selection — depends on whatever schema the
  MCP server publishes; the model chooses without proxy mediation.
- New surface area to secure (MCP endpoints need their own authn/authz).

**Status:** Cleanest separation, most ecosystem-aligned, but requires
client-side MCP support and changes the deployment shape.

---

### Pattern C — Hybrid: ambient profile + tool-based deep dive

A small "ambient" context is always injected — but only the cheap, always-
relevant stuff. Everything else becomes a tool.

**Always injected (ambient, ~50 tokens):**
- User profile one-liner ("You are talking to Luis Filipe, Solutions
  Architect at a regulated enterprise...")
- Project name from the request payload
- Current date in ISO format
- A short hint about the available memory tools ("You can call
  recall_decisions, recall_skills, recall_semantic, or recall_recent_sessions
  if you need historical context.")

**On-demand via tools:**
- `recall_decisions` (episodic — ADRs)
- `recall_skills` (procedural — skill files)
- `recall_recent_sessions` (conversational)
- `recall_semantic` (RAG / ChromaDB)

**Pros**
- The LLM never has to call a tool just to know who the user is or what
  date it is. That information is "ambient" and free.
- The bulk of the cost (4 memory searches, hundreds of tokens of context)
  becomes pay-per-use.
- Backwards-compatible escape hatch: ambient context covers the trivial
  prompts where there's no need for memory at all.
- Smaller / dumber models that don't tool-call well still get *something*
  useful out of the box.

**Cons**
- Two layers of complexity: now we have ambient + tool-based, not just one
  paradigm.
- Risk of the ambient context creeping back to today's bloat over time.
  Needs a hard token budget to enforce minimalism.

**Status:** Lowest risk landing zone. Combines the cost savings of the tool
approach with a safety net for the always-needed identity context.

---

## 5. The orthogonal dimensions

Beyond the three patterns, several decisions are independent:

### 5.1 Tool granularity

| Option | Tools |
|---|---|
| **Fine — one per layer** | `recall_decisions`, `recall_skills`, `recall_recent_sessions`, `recall_semantic` |
| **Coarse — by intent** | `recall_decisions_and_context` (episodic + semantic combined), `recall_skills` |
| **Single uber-tool** | `recall_memory(query, layers=["episodic", "semantic"])` — model picks layers as a parameter |

Fine matches your "each layer is a tool" framing. Coarse is friendlier to
weaker models that may not differentiate well between four similar tools.
Single uber-tool gives maximum flexibility but pushes tool-selection
intelligence into the parameter, which models handle less well than tool
selection itself.

### 5.2 Tool naming style

- **Service-oriented:** `search_episodic`, `query_chromadb`, `load_sessions`
  — accurate to the implementation, opaque to the model.
- **Agentic / verb-oriented:** `recall_past_decisions`, `look_up_relevant_skills`,
  `remember_recent_work` — guides the model toward correct usage by name alone.

Models trained on tool use respond better to verb-oriented names. Same for
descriptions: "Recall ADRs that are relevant to a topic" beats "Search the
episodic memory layer".

### 5.3 Result shape

Today the layers return raw `Document` lists. As tools they could return:

- **Raw matches** — model digests them as-is. Highest fidelity, highest
  token cost in the tool result.
- **Summaries** — proxy generates a short summary of each match. Lower
  token cost, loss of fidelity.
- **Top-k with metadata** — `[{title, snippet, score}]` — middle ground.

### 5.4 Caching within a conversation

If the model calls `recall_decisions("KV cache")` twice in one conversation
(e.g. forgot it asked, or chained reasoning), should the second call hit
the layer or return the cached first result? Argues for a per-`session_id`
LRU cache in the proxy.

### 5.5 Backwards-compatibility / kill switch

Keep the old "always inject" mode behind a feature flag (`AUDITTRACE_MEMORY_MODE=tools`
vs `AUDITTRACE_MEMORY_MODE=inject`)? Useful for:
- A/B comparing the two modes on identical prompts
- A panic-revert path if the new mode misbehaves
- Models that genuinely don't tool-call well

### 5.6 Tool budget / iteration limit

The proxy (Pattern A) needs a hard cap on tool-call rounds — otherwise a
misbehaving model could loop forever. Probably 5-10 rounds. After the cap,
return whatever text the model has and log a warning.

---

## 6. Open questions for Luis

These are the decisions that drive everything else. I deliberately have NOT
picked answers — they're yours to make.

1. **Pattern A, B, or C?** Or some combination — e.g. C as the implementation,
   A as the orchestration mechanism within C?

2. **Which models will use this?** Qwen3-Coder is good at tool calling. The
   embedding/reranker models obviously won't. If you're considering Mistral
   Small or other models for A/B testing, their tool-calling quality matters
   for the design.

3. **Tool granularity — fine or coarse?** Your phrasing was "each layer is
   a tool", which suggests fine. Confirmed?

4. **Should ambient context exist at all?** Pure Pattern A says no — even
   "you are Luis" becomes a tool call. Pattern C says yes — minimal ambient.
   What's the right floor?

5. **Which clients are in scope?** OpenCode is your daily driver. Continue
   and Roo Code are listed in `docs/agent-configuration.md`. If only OpenCode
   matters, MCP becomes more attractive (it's well-supported there). If you
   want all three, the proxy-side approach (Pattern A) is more uniform.

6. **What's the success metric?** I'd suggest: median memory token count
   per request drops by ≥ 80% on a representative set of prompts (mix of
   trivial + memory-hungry). Plus: trivial prompts ("ls /tmp", "curl X")
   produce ZERO memory spans in Langfuse.

7. **Phasing.** Big bang or shadow mode? Shadow mode = run both architectures
   in parallel, the new one as observation only, compare cost/quality before
   cutting over. Adds complexity but de-risks the cutover.

8. **Where does the system prompt that teaches the model about the tools
   live?** Hard-coded in the proxy? Configurable per-project? Per-agent?

9. **Tool result format consistency.** Today each layer returns its own
   shape (Documents, dicts, strings). Tools should return a consistent
   schema. What's the canonical shape — `{matches: [{title, snippet, source, score}], total: N}`?

10. **Failure modes.** What does the LLM see when a memory tool errors?
    A `tool_result` with `{"error": "..."}` body? An exception? A retry?

11. **Async persistence semantics.** See §12 — fire-and-forget, queue,
    outbox, or LISTEN/NOTIFY? What's the durability budget on a crash?

12. **Multi-user identity boundary.** See §13 — single-user assumption is
    going away. When? Big bang or staged? Which layers become per-user vs
    per-team vs global?

---

## 7. Risks and unknowns

**R1 — Smaller models tool-call poorly.** If you ever switch to a smaller
local model, it might never call the memory tools and effectively run blind.
Mitigation: Pattern C ambient floor + a "you SHOULD consider calling X
tools when..." line in the system prompt.

**R2 — Latency penalty for memory-heavy prompts.** Pattern A doubles the
model round-trips. For a 30-second debugging chain, this could add 30-60
seconds. Mitigation: streaming the final call, parallel tool execution
where possible.

**R3 — Trace correlation in Langfuse.** Pattern B produces split traces
(chat + MCP) that need stitching. Pattern A keeps them in one trace but
the trace shape gets deeper. Either way, the existing graph view rendering
will need testing.

**R4 — System prompt size.** Adding 4 tool descriptions costs ~200-500
tokens of system prompt. For many short prompts, that's MORE than the
ambient context we're trying to avoid. Need to measure honestly.

**R5 — Conversation-level state.** The memory tools' results land in the
conversation history. On turn 5, the model sees turn 1's `recall_decisions`
result inline with everything else — that's the right semantics, but it
inflates the KV cache. Mitigation: prune old tool results from the
conversation window above N turns, or summarise.

**R6 — Per-project context.** Today `project` is a request field that
scopes which sessions/RAG collections are queried. As tools, the project
needs to be in the tool args (`recall_recent_sessions(project="X")`) OR
ambient (system prompt tells the model what project it's in, the tool
defaults to that project). Ambient is cleaner.

**R7 — The chat handler test surface explodes.** Today the proxy is
mostly stateless. Pattern A makes it stateful (tool-call loop, accumulator,
iteration limit). The test suite needs to cover the loop with realistic
mock tool sequences.

**R8 — Backwards compatibility with existing Langfuse dashboards.** The
graph view shape will change. Dashboards that count `memory-*-search`
spans will undercount in the new world. Worth a one-time pass through
`docs/langfuse-dashboards.md` after the cutover.

---

## 8. What "good" looks like

A trivial prompt:

```
> do an ls of /tmp
```

Should produce a Langfuse trace that looks like:

```
sovereign-chat-request
  └── llama.cpp call (single, streamed)
        └── tool_call: bash("ls /tmp")
```

Zero memory spans. ~50 tokens of ambient context. ~150ms of latency before
streaming starts.

A memory-heavy prompt:

```
> remind me what we decided about KV cache compression and why
```

Should produce a trace like:

```
sovereign-chat-request
  ├── llama.cpp call #1 (non-streamed, decides to use a tool)
  │     └── tool_call: recall_decisions("KV cache compression")
  │           └── episodic search → ADR-009 returned
  ├── llama.cpp call #2 (streamed, generates the answer)
        └── final text streamed to OpenCode
```

Two memory spans (the recall + its execution), one final stream. The model
chose precisely the right layer; we paid for nothing else.

---

## 9. Phased implementation (sketch — depends on chosen pattern)

This is a strawman to show what the work would look like under Pattern C
+ Pattern A orchestration. Numbers are deliberately rough — refine after
the design decision.

### Phase 0 — Measurement (1 day)
- Add a `--trace-only` mode that logs which memory layers a request would
  query, without injecting them. Run for a day on real OpenCode traffic.
- Quantify: how often does each layer return non-empty results? What's
  the actual hit rate per layer per prompt class?
- Output: a baseline that the new design must beat.

### Phase 1 — Tool definitions + ambient context (2-3 days)
- Define the tool schemas in a new `src/audittrace/tools.py`.
- Build the ambient context generator (profile + project + date + tool
  hints) — small, fast, no I/O.
- Add `AUDITTRACE_MEMORY_MODE` env var defaulting to `inject` (current
  behaviour).

### Phase 2 — Proxy-side tool orchestration loop (3-5 days)
- Modify `chat_completions` to:
  1. Detect mode
  2. In tool mode: build minimal system message, augment `tools` with
     memory tools, run the tool-call loop with a hard iteration cap.
  3. In inject mode: current behaviour unchanged.
- New helper `_handle_memory_tool_call(name, args)` that maps a tool call
  to the existing service method.
- New tests for the tool-call loop (mock llama.cpp returning tool_calls,
  assert the loop terminates, assert tool execution flows).

### Phase 3 — Streaming the final response (2 days)
- The first llama.cpp call must be non-streaming so the proxy can inspect
  tool_calls. The FINAL call in the loop streams normally.
- Test: streaming SSE bytes are byte-equal in tool mode for prompts that
  trigger zero tool calls.

### Phase 4 — Langfuse trace shape verification (1-2 days)
- Make sure the proxy-internal tool execution lands as nested spans under
  the parent chat span.
- Update `docs/langfuse-dashboards.md` if any dashboards depend on the old
  span names.

### Phase 5 — Cutover (1 day + canary period)
- Flip `AUDITTRACE_MEMORY_MODE=tools` in `.env`.
- Run for a week as primary. Keep `inject` mode reachable via env var.
- After a week, decide whether to delete the inject path entirely.

### Phase 6 — ADR-025 (0.5 days)
- Once stable, write the architectural decision capturing the why and the
  trade-offs picked. Reference this brainstorm.

---

## 10. What this brainstorm does NOT cover

Out of scope intentionally:

- **Memory writes.** Today the conversational layer is written to
  programmatically (via `/session/summary`). Should the LLM be able to
  WRITE memory too via tools (`save_decision`, `record_session`)? That's
  a separate, much bigger conversation about agency, audit, and trust.
- **Memory layer growth.** Adding new layers (e.g. a "project context"
  layer that tracks current branch, last commit, etc.) is orthogonal —
  whatever pattern we pick should be straightforward to extend.
- **Cross-conversation linking.** Today `session_id` clusters traces in
  Langfuse. The tool model doesn't change that, but it might change what
  data the conversational tool returns.
- **Cryptographic audit.** Section 13 surfaces the *case* for
  signed/append-only audit (regulated enterprise context). Picking the
  technology — Postgres append-only constraint, S3 Object Lock, signed
  commits, on-chain anchoring — is its own ADR.

> **Previously listed as out-of-scope, now IN scope** (added 2026-04-11
> after Luis raised them post-walk):
> - **Async persistence** — see §12.
> - **Multi-user / multi-tenant** — see §13.

---

## 11. What actually happened

> **Replaces the "Next steps" outline.** Each of the six planned steps
> was executed in the same day the brainstorm was written. The
> sequence and outcome are captured here for the historical record.

1. ✅ **Doc was read.** Framing held up under scrutiny. No major
   revisions needed before picking a direction.
2. ✅ **Questions 1-6 in section 6 were answered** in a dialog that
   produced the ADR-025 decisions listed in the status block at the
   top of this file. Specifically: Pattern C+A, Qwen3.5-35B-A3B,
   fine-grained tools, verb-oriented naming, OpenCode first, kill
   switch via `AUDITTRACE_MEMORY_MODE`, iteration cap configurable.
3. ⚠ **Phase 0 measurement was SKIPPED** as an intentional trade-off —
   the token cost and latency estimates in §1 were deemed close enough
   to proceed without instrumentation first. The Phase 7b dogfood
   canary (ADR-025 §Implementation phases) is where real numbers get
   collected, not via an upfront measurement phase.
4. ✅ **§12 (async persistence) and §13 (multi-user) were sequenced
   against memory-as-tools.** Multi-user went first — its Phase 2
   shipped in the morning of 2026-04-11 — then memory-as-tools shipped
   with multi-user awareness from day one. Async persistence remained
   deferred to its own future ADR because it is orthogonal to both
   features on the output side.
5. ✅ **Promoted to ADR-025** (memory-as-tools, Proposed). See
   `docs/ADR-025-memory-as-tools.md`. Multi-user design graduated to
   `docs/ADR-026-multi-user-identity.md` with `Status: Accepted` at
   the end of 2026-04-11 evening once Phases 4/5a/5b/7 landed.
   Async persistence ADR is not yet written.
6. ✅ **The "spike one tool" step was skipped** in favour of shipping
   **all four tools atomically** — `recall_decisions`, `recall_skills`,
   `recall_recent_sessions`, `recall_semantic`. The cost of the spike
   was comparable to the cost of the full set because the handlers
   are trivial wrappers around existing services. All four landed in
   `04c2459` (ADR-025 Phase 2). Live-verified end-to-end in Phase 7a
   with the model calling `recall_decisions` against the real
   Qwen3.5-35B model and getting ADR-009 back from the filesystem.

### Current status of the three dimensions this brainstorm opened

| Dimension | Status | Authoritative doc |
|---|---|---|
| **Memory-as-tools** (§§1-11) | Phases 0-6 shipped, Phase 7a live-verified, Phase 7b canary + Phase 8 status flip pending | `docs/ADR-025-memory-as-tools.md` |
| **Async persistence** (§12) | Deferred — still synchronous, captured as a risk the session doc flags for later | No ADR yet; see §12 below for the original design space |
| **Multi-user identity** (§13) | **Accepted** — all of Option B (Phases 0-7) landed end of 2026-04-11 | `docs/ADR-026-multi-user-identity.md` §15, §16 |

---

## 12. Async conversation persistence

> **Added 2026-04-11 (post-walk).** Originally out of scope; promoted in
> after Luis observed that synchronous persistence is the wrong default.

### 12.1 The problem today

Inside `chat.py:_iter_and_capture` (the streaming generator), after the
last SSE chunk has been yielded:

```python
_persist_interaction(...)              # sync Postgres write
await _record_langfuse_output(...)     # async, but still in-line
```

Both run **inside the streaming generator's tail**, which means FastAPI
does not consider the response fully closed until they both return. From
OpenCode's POV, the cursor "hangs" for whatever those two operations
take. Today that's typically <50ms, so it's not visible — but:

- A slow Postgres (lock contention, vacuum, replication lag) stalls every
  user-visible response.
- A slow Langfuse (the proxy is HTTP-posting to the ingestion API in the
  same task) stalls the response close.
- A Postgres outage takes down chat completions even though the LLM has
  already produced the answer.
- The 5-second `_record_langfuse_output` timeout we accepted in ADR-024
  is a worst-case latency floor that should never have been there.

**Persistence is fire-and-forget by nature.** The user does not care
*when* the row lands. They care that the response stream finishes the
moment the LLM stops talking.

### 12.2 Design space

| Pattern | Durability | Infra | Complexity | Suitable for |
|---|---|---|---|---|
| **`asyncio.create_task`** | None — task dies with process | None | Trivial | Single-node, OK losing ≤ N pending writes on crash |
| **In-process queue + worker task** | Same as above, slightly bigger window | None | Low | Same as above + simple back-pressure |
| **Outbox (local file/SQLite buffer)** | Survives process restart | None | Medium | Single-node, "must not lose data" |
| **Redis queue + worker** | Survives process; needs Redis up | Redis (already runs for Langfuse) | Medium | Multi-node, retries, Langfuse already uses this |
| **Postgres LISTEN/NOTIFY** | Buffered in DB itself | None new | Medium | Want a worker pool fanout, no new infra |
| **Kafka / RabbitMQ** | Full durability + replay | Significant | High | Enterprise multi-tenant, audit replay |

### 12.3 Trade-offs to surface

- **Simplicity vs. durability.** `asyncio.create_task` is one line. The
  outbox pattern is tens of lines plus a worker. Pick the right rung
  for the *current* trust requirement, with a clear upgrade path for
  later.
- **Blast radius.** A Redis queue means the chat path now has a Redis
  hard dependency for persistence. If Redis dies, do we drop writes
  silently, buffer locally, or fail the request? All three are valid;
  pick on purpose.
- **Backpressure.** What happens when the queue grows faster than the
  worker drains? Memory bloat in-process? Push back on incoming requests?
  Drop oldest? The single-user case never hits this; the multi-user
  case absolutely will.
- **Where Langfuse posts go.** `_record_langfuse_output` is *also* an
  out-of-band side effect. It should ride the same async machinery as
  Postgres persistence — not get its own ad-hoc background task.

### 12.4 Open questions

1. **Durability budget.** Acceptable to lose 1-2 messages on a hard crash,
   or must everything land?
2. **Latency target.** What's the maximum delay we tolerate between
   "response streamed" and "row in Postgres"? Sub-second? 1 minute?
3. **Retry behavior on DB outage.** Block? Buffer? Fail loudly to a
   dead-letter store?
4. **Same channel for Postgres and Langfuse**, or two separate paths?
5. **Visibility.** Where does the operator see "queue depth"?

### 12.5 Interaction with memory-as-tools

**Mostly orthogonal.** Async persistence is a property of the *output
side* (write the interaction record after it happens). Memory-as-tools
is a property of the *input side* (decide which memory context to
inject). They can be sequenced independently:

- Async persistence first → no behavioural change for the user, but
  removes tail latency from every chat completion.
- Memory-as-tools second → big behavioural change, but the persistence
  path is already async by then so the tool round-trips don't compound
  the latency problem.

If anything, doing **async persistence first** is the safer order: it
removes a class of latency before we add the new tool-call round-trips.

### 12.6 Risks

- **R-A1: Lost writes on crash.** Mitigation: pick a pattern with the
  durability budget to match the requirement, and document the loss
  window explicitly.
- **R-A2: Worker lag invisible to operators.** Mitigation: queue depth
  metric exposed via `/metrics`, plus a Langfuse counter.
- **R-A3: Async write surfacing as race condition in tests.** The current
  test `test_chat_proxy_persists_interaction` queries Postgres
  immediately after the response. Mitigation: add a `flush_persistence`
  test hook OR an explicit `await persistence_complete()` for tests
  only.
- **R-A4: Outbox file growing unbounded.** If we pick the outbox pattern
  and the worker is offline, the local file fills the disk. Mitigation:
  rotate + cap.

---

## 13. Multi-user / enterprise / accountability

> **Added 2026-04-11 (post-walk).** Originally out of scope; promoted in
> after Luis raised the liability scenario: *"a user asks an agent to do
> a task, the agent does it, and then there's a liability — who did
> what?"*

This is the section that makes the project enterprise-grade. Today's
single-user assumption is the biggest unstated constraint in the entire
codebase, and unwinding it touches almost everything.

### 13.1 Where the single-user assumption hides

A non-exhaustive list of places that quietly assume one user:

- **`interactions.source` field.** Stores `"opencode"` / `"continue"` /
  `"roocode"` — the *agent name*, not the *human*. Today there is no
  field for the human at all.
- **Memory layers.** `EpisodicService.search(query)` returns ALL ADRs.
  `ProceduralService.search(query)` returns ALL skills. There is no
  notion of "ADRs Luis has access to" vs "ADRs Alice has access to".
- **ChromaDB collections.** Single namespace per project, not per user.
- **`_compute_session_id`.** Hashes `(source, date, first_message)` —
  no user component. Two different users hitting the same agent on the
  same day with the same prompt would get the same session_id. Wrong.
- **Langfuse `langfuse.user.id` attribute.** Already populated, but with
  the agent name, not a real user identity.
- **Auth.** Wired (Keycloak ADR-022, JWT ADR-023) but
  `AUDITTRACE_AUTH_ENABLED=false` for local dev. Flipping it on is a
  one-flag change in env, but the *consequences* are repo-wide.
- **`/session/summary` endpoint.** Persists with no user attribution.
- **The CLAUDE.md memory profile.** Currently hard-codes "Luis Filipe"
  in the procedural skills. In a multi-user world, the *profile* itself
  is per-user.

### 13.2 The liability scenario

You phrased it concretely: *"a user asks an agent to do a task, the
agent does it, and then there's a liability — who did what?"*

Concrete examples in increasing severity:

1. **"The agent ran `rm -rf /tmp/foo` on a shared dev box."** Need: user
   identity, prompt, tool call, timestamp, all linked.
2. **"The agent leaked customer PII into a Langfuse trace."** Need:
   per-user trace boundaries; one user must not be able to read another
   user's traces.
3. **"The agent committed code to the wrong branch under another
   user's git identity."** Need: tool authorization scope (which repos,
   which branches), and the agent must run under the *requesting user's*
   credentials, not a service account.
4. **"The agent placed a financial trade based on an interpretation of
   ADR-X."** Need: full context reconstructibility (what ADR-X said *at
   the time*, not now), 4-eye approval workflow on high-impact tools,
   immutable audit.
5. **"The agent gave compliance-relevant advice that turned out to be
   wrong."** Need: full prompt + memory context + tool calls + model
   version + timestamp, all replayable.

The common denominator is **reconstructibility under audit**. The
audittrace-server's foundational ambition (ADR-014: *full agentic
trace capture*) was always about this — but ADR-014 implicitly assumed
one user. In a multi-user world, reconstructibility must be **per user
session**, with hard boundaries.

### 13.3 Dimensions

**Identity.**
- Who is "the user"? OAuth2 subject claim from Keycloak? Email? UUID?
- Identity is **already** carried in the JWT we validate (ADR-023), we
  just don't propagate it past the auth dependency. Plumbing it through
  the request → memory layers → persistence is mostly mechanical.

**Memory scoping (the real architectural decision).**

| Layer | Per-user? | Per-team? | Per-project? | Global? |
|---|---|---|---|---|
| **Episodic (ADRs)** | Some | Probably | **Yes** | Some (public ADRs) |
| **Procedural (skills)** | Personal skills (rare) | Team skills | — | **Yes** (most skills) |
| **Conversational (sessions)** | **Yes** | — | — | — |
| **Semantic (RAG / ChromaDB)** | — | Per collection | Per collection | Some |

The strawman: **conversational is per-user**, **semantic is
per-project (with per-collection ACLs)**, **procedural is mostly
global (skills are public knowledge)**, **episodic is per-project with
ACLs**. But this is a hypothesis; the actual answer depends on how Luis
intends to use this in an enterprise setting.

**Tool authorization (couples directly to memory-as-tools).**

If memory becomes tool-driven (§§ 1-9), every tool call is now an
authorization decision:
- `recall_decisions(query)` → which projects can this user see?
- `recall_recent_sessions(project)` → can this user read this project's
  sessions, or only their own within the project?
- `recall_skills(query)` → easier (mostly global), but per-user skills
  exist.

This is RBAC at minimum, more likely ABAC (attribute-based: user attrs
× resource attrs × action). Keycloak supports both.

**Audit trail.**
- **Append-only Postgres** (constraint or trigger blocks UPDATE/DELETE
  on `interactions`) → cheap, sufficient for most enterprise needs.
- **S3 Object Lock** (one row → one object, immutable for retention
  period) → compliance-grade, more expensive, queryable via Athena.
- **Both** → fast queries from Postgres, immutable archive in S3.
- **Cryptographic anchoring** (sign each row, anchor batches in a
  Merkle tree) → paranoid-grade. Probably too much for now, but worth
  flagging the design space goes there.

**Aggregation.**
- "User X spent Y tokens this month" → need user_id on interactions +
  Langfuse `user.id` + a roll-up query.
- "Team A consumed N memory tool calls this week" → same plus a team
  membership table.
- These are reporting, not architecture. They become trivial once the
  identity propagation is done.

**Right to be forgotten (GDPR / Swiss DPA).**
- A user requests deletion → all their interactions, conversational
  memory, and Langfuse traces must be removable.
- Conflicts with append-only audit. Resolution: either soft-delete with
  PII redaction, or hard-delete with a retention exception for
  legally-required records.
- Worth a dedicated thinking session before any deployment that takes
  external traffic.

### 13.4 Open questions

1. **Identity provider.** Keycloak (already wired) is the obvious answer.
   Confirmed?
2. **Multi-tenant data model.** Schema-per-tenant, row-level security,
   separate databases? The first is simplest at small scale, the third
   scales hardest. Where's the organisation on this?
3. **Memory scoping defaults.** The strawman in §13.3 — conversational
   per-user, procedural mostly global, episodic + semantic per-project
   — is one opinion. What's yours?
4. **Tool authorization model.** RBAC-only, or ABAC? Who manages the
   policies?
5. **Audit trail format.** Postgres-only, Postgres + S3, signed?
6. **Retention policy.** How long do interactions live? Different per
   layer?
7. **Right to be forgotten approach.** Soft-delete with redaction, or
   hard-delete with audit exemption?
8. **When does this happen?** Today's stack is single-user. Going
   multi-user is a *huge* migration. Is this:
   - (a) Theoretical — capture the design now, implement when there's
     a second user
   - (b) Imminent — scoped to the next quarter
   - (c) Foundational — must precede any other major work because
     everything else builds on the wrong assumption

### 13.5 Interaction with memory-as-tools

> **Status: ACCEPTED 2026-04-11.** This recommendation has been promoted
> to a design decision. See `ADR-026` for the
> tactical follow-on:
> - Identity is mandatory across the entire request path from day one
> - Scopes drive tool availability (tool registry as authorization boundary)
> - Cross-user isolation enforced at five layers (schema, query, RLS,
>   ChromaDB metadata filter, dedicated tests)
> - First client is OpenCode with PAT-based auth; OAuth2 device flow
>   with Keycloak is the explicit Phase 7 follow-up
> - Interfaces designed today as if implementation were complete; the
>   first iteration uses sentinel values where the multi-user surface
>   isn't filled in yet

**This is where the two topics couple directly.**

If memory-as-tools (§§ 1-9) and multi-user (§13) are designed
independently, the migration to multi-user later will be painful — every
tool will need its access-control logic retrofitted.

If they are designed *together*, the tool registry becomes the
authorization boundary from day one:

```
recall_decisions(query)
   │
   ▼
 tool registry
   │
   ├─► identity from JWT (already validated by middleware)
   ├─► resolve user → accessible projects
   ├─► EpisodicService.search(query, projects=[...accessible...])
   │
   ▼
 tool result (filtered by access scope)
```

The tool registry is the natural place to enforce "user A can read
project X but not project Y". The memory services themselves stay
project-blind; the registry adds the filter.

**Recommendation (offered, not decided):** treat §13 as a *design
constraint* for the memory-as-tools work, not a separate later phase.
Even if multi-user implementation happens later, the **interfaces**
should be designed today as if user identity is mandatory. That means:

- Every tool function takes a `user_context` parameter from day one
  (in single-user mode it's always Luis; in multi-user it's resolved
  from JWT)
- `interactions` schema gains a `user_id` column from day one (NULL =
  legacy single-user mode)
- Langfuse `user.id` is populated from JWT subject claim, falling back
  to the agent name in single-user mode

This is "future-proof the interface, defer the implementation". Cheap
to do early, expensive to retrofit later.

### 13.6 Risks

- **R-B1: Schema migration sprawl.** Adding `user_id` to every table
  is a multi-migration job. Mitigation: do it in one go before more
  tables exist.
- **R-B2: Auth-on means every existing client breaks.** OpenCode,
  Continue, Roo Code all need to acquire JWTs. Mitigation: keep the
  flag for now, but complete the multi-user *interface* changes
  regardless.
- **R-B3: Memory leakage between users.** A bug in the scoping filter
  could let user A see user B's sessions. This is the worst possible
  bug in this category. Mitigation: dedicated tests for cross-user
  isolation; row-level security in Postgres as a belt-and-braces.
- **R-B4: Performance.** Per-user filters on every memory query.
  Mitigation: indexes, partitioning by user_id at scale.
- **R-B5: ChromaDB scoping is awkward.** ChromaDB collections are
  the natural unit of isolation. "One collection per user × project"
  could explode the collection count. Mitigation: per-collection
  metadata filter instead, or migrate to a different vector store.

---

*Brainstorm, not a decision. No code touched. The architecture folder
holds the seed for the next big move on this stack — now covering
memory-as-tools (§§ 1-11), async persistence (§12), and multi-user
governance (§13).*
