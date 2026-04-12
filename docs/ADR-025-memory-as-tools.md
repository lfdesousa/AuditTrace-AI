# ADR-025: Memory Layers as LLM-Callable Tools

**Status:** Proposed
**Date:** 2026-04-11
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-010 (async server), ADR-012 (transparent proxy augmentation),
ADR-014 (full agentic trace capture), ADR-018 (four-layer memory port),
ADR-024 (chat proxy pass-through + Langfuse trace decoupling),
ADR-026 §15 (Keycloak-delegated identity, Phase 2 shipped)
**Seed:** `docs/architecture/BRAINSTORM-memory-as-tools.md` (2026-04-11)

## Context

Today every chat completion fires all four memory layers up front, regardless
of whether the prompt needs them. A trivial `do an ls /tmp` pays the same cost
as `explain ADR-014`:

- 4 memory searches fired (`EpisodicService.search`, `ProceduralService.search`,
  `ConversationalService.as_context + load_sessions`, `ChromaSemanticService.search`)
- 4 ChromaDB queries, including two round-trips to the embedding server
- 4 Langfuse spans on every request
- Hundreds to thousands of system-prompt tokens the model will never reference
- ~200–500 ms of latency before `llama.cpp` begins decoding
- KV cache footprint inflated for the full lifetime of the conversation

The intuition is simple: **the LLM knows what it needs to know better than we
do**. The rest of the tool-use ecosystem (bash, read_file, edit_file) already
trusts the model to call what it needs. There is no reason memory should be
different.

The brainstorm document lays out three patterns (Proxy intercepts, MCP server,
Hybrid ambient + tools) and eleven orthogonal decisions. This ADR picks the
landing zone.

### Why not Langchain

The repository inherits `langchain>=0.1.0` and `langchain-community>=0.0.10`
from the early prototype. The only actual runtime usage in `src/` today is
`from langchain_core.documents import Document` in the three filesystem-backed
services — a passive dataclass, not runtime orchestration. `chain.py`,
`backend.py`, and `memory.py` are empty stubs.

Adopting Langchain's `AgentExecutor` / tool-calling agent for memory-as-tools
would:

1. **Violate the ADR-024 transparent-proxy property.** ADR-024 spent three
   iterations removing a Pydantic schema that silently dropped OpenAI fields.
   Langchain's agent loop would re-insert an opaque middleware between
   OpenCode and `llama.cpp` — exactly the failure mode ADR-024 documents.
2. **Complicate `UserContext` threading.** Every tool call must see
   `user_context`, enforce `required_scope` against `user_context.scopes`,
   and write a `ToolCall` audit row with `user_id + granted_scope`. In hand-
   written proxy code these are 3–5 lines per tool. In Langchain's callback
   hierarchy they become a research question about which hook fires when and
   whether `RunnableConfig` context survives the async streaming response
   tail (where ADR-024 already documented problems with context-manager span
   lifetimes).
3. **Add ~1000+ lines of indirection** for a problem that is fundamentally
   dict manipulation. OpenCode and `llama.cpp` both speak the OpenAI
   tool-calling JSON protocol natively; the proxy is manipulating `tools`,
   `tool_calls`, `tool_call_id` dicts inside a streaming SSE response. That
   is a plumbing job, not an agent-framework job.

Consistent with `feedback_idp_owns_identity.md` (don't rebuild what the
underlying system does): don't adopt a framework when the protocol already
does the job.

## Decision

### 1. Hybrid pattern — ambient profile + tool-based deep dive (Brainstorm §4 Pattern C)

A minimal always-injected ambient context covers the cheap, always-relevant
information. Everything else becomes a tool the LLM calls on demand.

**Ambient context (always injected, hard budget: ≤ 200 tokens):**

- User profile one-liner derived from `UserContext` (username + is_admin)
- Current project name from the request payload
- Current date in ISO format (`date.today().isoformat()`)
- A short hint enumerating the available memory tools and when to call them

**Tools (on-demand, pay-per-call):**

| Tool name | Layer | Required scope |
|---|---|---|
| `recall_decisions` | Episodic (ADRs) | `memory:episodic:read` |
| `recall_skills` | Procedural (skill files) | `memory:procedural:read` |
| `recall_recent_sessions` | Conversational (per-user) | `memory:conversational:read-own` |
| `recall_semantic` | Semantic (ChromaDB RAG) | `memory:semantic:read` |

Scope names use the Keycloak convention from DESIGN §15. Admins
(`UserContext.is_admin`) bypass scope gates, consistent with Phase 2.

### 2. Proxy-internal orchestration (Brainstorm §4 Pattern A)

The chat proxy owns the tool-call loop. From OpenCode's point of view the
request is still a single chat completion; inside the proxy there may be
1, 2, or N `llama.cpp` round-trips before the final streamed response.

This is the only pattern that:

- Works uniformly across OpenCode, Continue, and Roo Code without client-side
  MCP support
- Keeps memory tools authoritative from a single point (the proxy), so scope
  filters and audit rows cannot be bypassed by a misconfigured client
- Produces a single unified Langfuse trace with the tool calls nested under
  the parent chat span

The tool-call loop obeys a **configurable hard iteration cap**
(`SOVEREIGN_MEMORY_TOOL_LOOP_MAX_ITERATIONS`, default `5`). Beyond the cap
the proxy returns whatever text the model has accumulated and logs a warning
at `logging.WARNING` level.

### 3. Dynamic, configuration-overridable tool registry

Tools register at import time via a decorator:

```python
# src/sovereign_memory/tools/__init__.py

@register_memory_tool(
    name="recall_decisions",
    description="Recall past architectural decisions (ADRs) relevant to a topic.",
    parameters_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to recall — e.g. 'KV cache compression'",
            },
        },
        "required": ["query"],
    },
    required_scope="memory:episodic:read",
)
async def recall_decisions(user: UserContext, args: dict) -> dict:
    episodic = get_episodic_service()
    matches = episodic.search(user, args["query"])
    return {
        "matches": [
            {"title": m.metadata.get("title"), "snippet": m.page_content[:400]}
            for m in matches
        ],
        "total": len(matches),
    }
```

The decorator populates a module-level `MEMORY_TOOL_REGISTRY: dict[str,
MemoryTool]`. Adding a new memory tool = writing one function with one
decorator; no boilerplate elsewhere.

**Config-file override (optional).** An operator-controlled `tools.toml`
(or equivalent) at runtime can override individual tools to:

- Disable a tool (`enabled = false`)
- Override `required_scope` (for tenant-specific scope names)
- Override `description` (for tenant-specific prompt guidance)
- Override the tool name as seen by the LLM (rare; deployment alias)

The config file **cannot** add new tools or supply new handlers — handlers
must come from code. This is deliberate: new tool handlers are code that
needs review; config is a runtime knob for what operators can safely flip.

Loading order: decorators populate the base registry, then
`tools.toml` is applied on top (if present) via a `config_overrides` pass.
The resulting registry is queryable via `tools_visible_to(user_context)`
which returns the OpenAI-spec tool definitions scoped to the caller.

### 4. Kill switch

A single env var controls the full behaviour:

```
SOVEREIGN_MEMORY_MODE={inject|tools}    # default: inject during rollout
```

- `inject` — current behaviour; 4 layers fired up front, memory merged into
  the system message, no tool definitions added.
- `tools` — new behaviour; ambient profile injected, memory tool definitions
  added to `tools`, proxy runs the tool-call loop.

The mode is read per-request from `config.get_settings().memory_mode` so flipping
the env var does not require a restart (the `@lru_cache`'d `get_settings()`
is cleared at the start of each request via an existing settings helper, or a
dedicated `chat.py`-level read — decision deferred to implementation).

### 5. Audit — one `ToolCall` row per memory tool invocation

Every memory tool call writes one row to the existing `tool_calls` table
(created in DESIGN §15 Phase 0):

- `user_id` — from `UserContext.user_id`
- `agent_type` — from `UserContext.agent_type`
- `tool_name` — `recall_decisions` etc.
- `args` — JSON-serialised tool arguments
- `result_summary` — JSON-serialised truncated result (first 500 chars per match)
- `error` — populated only on handler exception
- `started_at` / `duration_ms`
- `granted_scope` — the `required_scope` that passed the filter
- `interaction_id` — FK to the `interactions` row of the parent chat request

Writing is **synchronous** in this ADR for simplicity. Async persistence is
deferred to a separate ADR (see brainstorm §12).

### 6. Tool result schema — consistent across layers

Every memory tool returns the same top-level shape:

```json
{
  "matches": [
    {"title": "...", "snippet": "...", "source": "ADR-009", "score": 0.87}
  ],
  "total": 3,
  "truncated": false
}
```

The `score` field is present when the layer produces relevance scoring
(semantic always, episodic/procedural never — keyword-matched, binary).
Consumers of the tool result (the LLM) see a stable schema regardless of
which layer answered.

### 7. Langfuse trace shape

Each tool call becomes a nested Langfuse observation under the parent
`sovereign-chat-request` trace. Span name convention:

```
sovereign-chat-request
├── llama.cpp call #1 (non-streamed, decides)
│     └── memory-tool-call: recall_decisions
│           └── episodic-search
├── llama.cpp call #2 (streamed, final)
      └── final text streamed to OpenCode
```

This preserves the ADR-024 single-trace property for a single chat
completion, so existing Langfuse dashboards keep working. Dashboards that
counted the old `memory-episodic-search` spans will undercount in the
new world — documented as a known consequence below.

### 8. Redis-backed per-session tool result caching

Memory tool calls within a single conversation are often repeated
verbatim (the model forgot, or chained reasoning retrieves the same
context). Hitting ChromaDB + Postgres on every repeated call is
wasteful. The sovereign-redis container from DESIGN §15 is already
deployed for `TokenCache`; it is the obvious substrate.

**Design:**

- **Key:** `sovereign:tool-result:<sha256(session_id|tool_name|canonical_args_json)>`
- **Value:** JSON-encoded result dict (the canonical `{matches, total,
  truncated}` shape)
- **TTL:** `SOVEREIGN_MEMORY_TOOL_CACHE_TTL_SECONDS`, default `900` (15
  minutes). Set to `0` to disable caching globally.
- **Namespace disjoint** from `sovereign:token:` — the same Redis
  instance is shared but key prefixes never collide.
- **Write on success only.** Handler exceptions are never cached; the
  next call re-attempts.
- **Cache hits skip the `ToolCall` audit row.** Cache hits have zero
  side effects on memory layers and represent the same result we
  already audited when the cache was populated. Writing a duplicate
  row with a `cache_hit=true` flag would require a schema change
  (`tool_calls.cache_hit` column), which is out of scope for this ADR.
  A DEBUG-level log line records cache hits for operator visibility.
- **Implementation reuses the TokenCache pattern.** A new
  `ToolResultCache` class takes a `redis.Redis` client in its
  constructor and is accessed via a module-level `get_tool_result_cache()`
  singleton — exactly mirroring `identity.TokenCache`.

The sovereign-redis container stays the only Redis dependency; no new
infrastructure.

### 9. In scope / out of scope

**In scope for this ADR:**

- The registry, the decorator, the optional config override, the tool-call
  loop, the ambient context generator, the kill switch, the audit row, the
  trace shape, Phase 2 `UserContext` scope filtering.

**Out of scope (deferred to future ADRs or phases):**

- **Writes.** Tools that modify memory (`save_decision`, `record_session`)
  are not in this ADR — separate conversation about agency, audit, and trust.
- **Async persistence.** Deferred to its own ADR.
- **Memory-layer growth.** Adding new layers (project context, branch state)
  should be straightforward via the decorator, but the design of those layers
  is out of scope here.
- **MCP server.** Pattern B from the brainstorm remains interesting for
  future external consumers but is not part of this cutover. If we do ship
  an MCP surface later, it will share the same `MEMORY_TOOL_REGISTRY`.

## Consequences

### Positive

- **Trivial prompts pay nothing.** `ls /tmp` produces zero memory spans and
  ~50–200 tokens of ambient context.
- **Memory-hungry prompts call exactly what they need.** One layer or four,
  the model chooses.
- **Single point of authority.** The proxy enforces scope filters and writes
  audit rows; no client can bypass them.
- **Ready for multi-user from day one.** Phase 2 `UserContext` plumbing is
  already in place; tools plug into it directly.
- **Dynamic registry.** New memory tools are one function + one decorator.
  No central dispatch table to maintain by hand.
- **Configurable ops knobs.** Iteration cap, mode flip, per-tool overrides —
  operator can tune without code changes.

### Negative / trade-offs

- **Latency floor for memory prompts.** A prompt that actually needs memory
  now pays 2× `llama.cpp` round-trips (model decides → tool executes → model
  answers). For trivial-but-memory-aware prompts this is the same cost as
  today; for memory-heavy prompts it is an extra round-trip. Mitigation:
  streaming the final call, not the decision call; the decision call is
  typically fast because the model emits only a tool_calls block.
- **Chat handler state.** `chat.py` grows from a streaming forwarder into a
  tool-call loop. New test surface: `test_memory_tool_loop.py` covering
  iteration cap, error handling, mixed LLM-tool + memory-tool orchestration.
- **Dashboards drift.** Any Langfuse dashboard that counted
  `memory-episodic-search` spans will undercount after cutover. One-time
  pass through `docs/langfuse-dashboards.md` post-cutover.
- **System prompt size trade-off.** Tool descriptions cost 200–500 tokens
  of system prompt. For very short prompts this is *more* than today's
  ambient profile. The mode flip gives us the A/B toggle to measure this
  on real traffic.
- **Smaller / non-tool-calling models degrade.** If we ever swap to a model
  that tool-calls poorly, `SOVEREIGN_MEMORY_MODE=inject` is the escape hatch.
- **Synchronous audit writes.** A DB hiccup on the `tool_calls` write can
  briefly stall a tool-call response. Acceptable for now; async persistence
  ADR takes this up later.

### Neutral

- **Langchain deprecation.** Shipping this ADR is also the moment to drop
  `langchain` and `langchain-community` from `pyproject.toml`. The one
  surviving usage (`langchain_core.documents.Document`) is retained — it
  pulls in `langchain-core` only, which is a small focused dep. Full
  Langchain removal is deferred to a separate prep commit.

## Success metrics

Quantitative targets measured over a representative prompt mix after one
week of cutover:

1. **Zero memory spans** for trivial prompts (`ls /tmp`, `curl X`, single
   `bash` invocations). Baseline: 4 spans each today.
2. **Median memory token count per request drops by ≥ 80 %** across a
   representative sample of 100 OpenCode prompts.
3. **P95 latency on trivial prompts drops by ≥ 150 ms** (the embedding +
   Chroma round-trip we currently pay up-front).
4. **P95 latency on memory-heavy prompts increases by ≤ 400 ms** (the
   extra `llama.cpp` decision round-trip).
5. **Tool-call loop terminates within cap on 100 % of runs** across the
   same sample. If the cap is ever hit in production, the default (`5`) is
   too low or there is a prompt pathology to investigate.

## Implementation phases

Sequenced so each phase is atomic, testable, and leaves the tree green.

### Phase 0 — Prep (0.5 days)

- New `docs/ADR-025-memory-as-tools.md` (this file) — Proposed status.
- Draft config keys in `config.py:Settings`, all with tests:
  - `SOVEREIGN_MEMORY_MODE` (default `inject`)
  - `SOVEREIGN_MEMORY_TOOL_LOOP_MAX_ITERATIONS` (default `5`)
  - `SOVEREIGN_MEMORY_TOOL_CACHE_TTL_SECONDS` (default `900`; `0` disables)
  - `SOVEREIGN_TOOLS_CONFIG_PATH` (default `tools.toml` at repo root)
- Drop `langchain>=0.1.0` and `langchain-community>=0.0.10` from
  `pyproject.toml`; keep `langchain-core` for the `Document` import.
  Regenerate `requirements.txt`. Verify `make test` still green.

### Phase 1 — Tool registry primitives (1 day)

- New module `src/sovereign_memory/tools/__init__.py`:
  - `MemoryTool` frozen dataclass
  - `MEMORY_TOOL_REGISTRY: dict[str, MemoryTool]`
  - `register_memory_tool(...)` decorator
  - `tools_visible_to(user_context) -> list[dict]` — returns OpenAI-spec
    tool definitions filtered by scope
  - `load_config_overrides(path: Path) -> None` — optional TOML pass
- New tests `tests/test_memory_tools_registry.py`:
  - Decorator registers
  - Scope filter returns admin-visible and non-admin-visible tools correctly
  - Config override disables a tool, overrides scope, overrides description
  - Duplicate-name registration raises

### Phase 2 — Four memory tool handlers + ToolResultCache (1.5 days)

- New module `src/sovereign_memory/tools/memory_handlers.py`:
  - `recall_decisions` wraps `EpisodicService.search`
  - `recall_skills` wraps `ProceduralService.search`
  - `recall_recent_sessions` wraps `ConversationalService.load_sessions`
  - `recall_semantic` wraps `ChromaSemanticService.search`
- Each handler normalises results into the canonical
  `{matches, total, truncated}` schema.
- New `ToolResultCache` class (mirroring `identity.TokenCache` pattern)
  with `get`, `put`, `clear`, `size`, wrapping a `redis.Redis` client.
  Cache key: `sovereign:tool-result:<sha256(session_id|tool_name|canonical_args)>`.
- Registry's tool invocation helper applies cache: check → execute →
  cache on success → return. Exception path does not cache.
- New tests `tests/test_memory_tool_handlers.py`:
  - Each handler returns the canonical shape
  - Each handler threads `user_context` into the underlying service
  - Errors surface as `{"error": "..."}` in the result (not as exceptions)
- New tests `tests/test_tool_result_cache.py` (fakeredis-backed):
  - Cache miss → handler fires → result stored
  - Cache hit → handler does NOT fire → cached result returned
  - Exception path → cache NOT populated
  - TTL=0 disables the cache entirely (handler always fires, nothing stored)
  - Disjoint namespace from `sovereign:token:` prefix

### Phase 3 — Ambient context generator (0.5 days)

- New helper in `src/sovereign_memory/services/context_builder.py`:
  `build_ambient_context(user_context, project, tools_visible) -> str`
- Hard token budget via naive split (`len(text.split()) * 1.3 <= 200`).
- New tests in `tests/test_context_builder.py`.

### Phase 4 — Tool-call loop in chat.py (2 days)

- New helper `src/sovereign_memory/routes/_memory_tool_loop.py`:
  - `run_tool_call_loop(payload, user_context, max_iter) -> (final_body, iterations)`
  - Hand-written async: non-streamed first round, loop until no more
    `tool_calls`, final round streamed back to caller
  - `ToolCall` row written per memory-tool invocation
  - Langfuse nested-span shape per §7
- `chat_completions` routes to the loop when
  `settings.memory_mode == "tools"`, otherwise keeps current inject path.
- New tests `tests/test_memory_tool_loop.py`:
  - Zero tool calls → single pass-through (no extra round-trips)
  - One tool call → two `llama.cpp` calls, final streamed
  - Two chained tool calls → three calls, final streamed
  - Iteration cap hit → returns accumulated text + WARNING log
  - Tool handler raises → error surfaces in tool result, loop continues
  - Scope gate denies → tool never appears in `tools_visible_to`
- New tests in `tests/test_chat_proxy.py` that flip
  `memory_mode="tools"` via a fixture and assert the end-to-end path.

### Phase 5 — Langfuse trace verification (0.5 days)

- Manual verification against live Langfuse: trivial prompt → zero memory
  spans; memory-heavy prompt → nested memory-tool-call spans under the
  parent chat trace.
- Update `docs/architecture/sequence-chat-completions.md` to show the
  loop shape.
- New `docs/architecture/sequence-memory-tool-call.md` sequence diagram.

### Phase 6 — C4 model update (0.5 days)

- `docs/architecture/workspace.dsl`: add `memoryToolRegistry` component
  inside the chat route container, show relationships to the four
  memory layer services.
- Export static site for review.

### Phase 7 — Cutover (0.5 days + canary week)

- Flip `SOVEREIGN_MEMORY_MODE=tools` in `.env` for dev.
- Dogfood via OpenCode for one week.
- After one week, decide whether to delete the `inject` path entirely or
  keep it as a feature flag for model swapping.

### Phase 8 — ADR status flip (0.1 days)

- After Phase 7 canary period passes success metrics, flip this ADR from
  `Proposed` to `Accepted`.
- Promote the brainstorm doc from `docs/architecture/` to an archive
  location (or delete — it's seed material, not authoritative).

## Related documents

- **Seed:** `docs/architecture/BRAINSTORM-memory-as-tools.md`
- **Multi-user context:** `docs/ADR-026-multi-user-identity.md` §15
  (Phase 2 shipped 2026-04-11)
- **Proxy transparency:** `docs/ADR-024-proxy-passthrough-and-langfuse-trace-decoupling.md`
- **Memory layer port:** `docs/ADR-018-four-layer-memory-port.md`
- **Trace capture ambition:** `docs/ADR-014-full-agentic-trace-capture.md`

## Resolved design questions

1. **Config override file format:** **TOML.** Python 3.11+ ships
   `tomllib` in the stdlib and the project already uses TOML for
   `pyproject.toml` — no new parser, no new dependency.
2. **Config override file location:** Repo-local `tools.toml` at the
   project root, overridable by the `SOVEREIGN_TOOLS_CONFIG_PATH` env
   var for operators who deploy from immutable images.
3. **Per-conversation tool result caching:** **In scope for this ADR.**
   The sovereign-redis container from DESIGN §15 makes it cheap — one
   new `ToolResultCache` class mirroring the existing `TokenCache`
   pattern. See §Decision.8 above.
4. **Ambient context `is_admin` exposure:** **Yes, included.** The
   ambient profile tells the model whether the caller is admin so it
   can reason about which tools are worth calling. Minor information-
   disclosure vector, accepted as a trade-off for tool-selection
   quality.

## Deferred action items (tracked for follow-up, not blocking this ADR)

- **Bruno HTTP request collection.** After the tool-call loop is shipped
  and dog-fooded, build a Bruno collection under `tests/bruno/` covering
  the full chat-completions surface: trivial prompt (expect zero memory
  spans), memory-hungry prompt (expect nested memory-tool-call spans),
  tools-mode vs inject-mode A/B, iteration-cap hit, tool handler error.
  The collection becomes the low-ceremony smoke test operators run
  against a freshly-spun stack. Owner: Luis — to be scheduled after
  Phase 7 cutover.
- **Async persistence of `ToolCall` + `InteractionRecord`.** See
  brainstorm §12. Separate ADR once the synchronous write path has
  shown its steady-state cost.
- **Full Langchain removal.** This ADR drops `langchain` and
  `langchain-community` but keeps `langchain-core` for the `Document`
  dataclass. Replacing even that with an in-repo dataclass is a
  ~10-line cleanup once someone has a free afternoon.
- **`cache_hit` column on `tool_calls`.** If auditors ever ask "did the
  LLM see a fresh result or a cached one?", add the column and
  backfill to `false`. Out of scope now.
