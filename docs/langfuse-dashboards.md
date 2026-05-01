# Langfuse Dashboards — Recipes

Langfuse stores dashboards in its own database and does not expose a public
API to import/export them, so they have to be rebuilt manually after a fresh
install. These are the dashboards we run on top of audittrace-ai.

## Span attributes you can group by

Every `@log_call`-decorated function in audittrace-ai emits a span
with these custom attributes:

| Attribute | Example value | Use for |
|---|---|---|
| `sovereign.component` | `memory.episodic`, `memory.procedural`, `memory.conversational`, `memory.semantic`, `memory.builder`, `route.chat`, `route.context`, `db.postgres`, `db.chromadb`, `core.di`, `core.auth`, `core.telemetry` | **grouping** by logical component |
| `sovereign.operation` | `FileEpisodicService.search` | exact function name |
| `sovereign.source` | `opencode`, `continue`, `roocode`, `curl` | filtering by calling agent |
| `sovereign.memory.query` | `"What does ADR-009 say?"` | searching by query text |
| `sovereign.memory.project` | `AuditTrace` | filtering by project |
| `langfuse.session.id` | `opencode-2026-04-10-<hash>` | session grouping |
| `gen_ai.system` | `llama.cpp` | LLM provider filter |
| `gen_ai.request.model` | `qwen3.5` | model filter |
| `gen_ai.usage.input_tokens` / `output_tokens` | `4008` / `30` | token totals |

## Dashboard 1 — Memory layer call counts

Bar chart showing how often each memory layer is hit.

1. **Langfuse → Dashboards → New Dashboard** → name it `Sovereign Memory`
2. **+ Add Widget** → **Bar chart**
3. Configuration:
   - **Data source:** Observations
   - **Filter:** `metadata.attributes.sovereign.component starts with memory.`
   - **Group by:** `metadata.attributes.sovereign.component`
   - **Y-axis:** Count
   - **Time range:** Last 24 hours (or whatever)
4. **Save**

You'll see five bars: `memory.episodic`, `memory.procedural`, `memory.conversational`, `memory.semantic`, `memory.builder`.

## Dashboard 2 — Latency by component (p50/p95)

Line chart of latency percentiles over time.

1. **+ Add Widget** → **Line chart**
2. Configuration:
   - **Data source:** Observations
   - **Filter:** `metadata.attributes.sovereign.component is not null`
   - **Group by:** `metadata.attributes.sovereign.component`
   - **Y-axis:** `latency` → P95
   - **X-axis:** Time (1h buckets)

## Dashboard 3 — Token usage per agent

Stacked bar chart showing prompt + completion tokens grouped by calling agent.

1. **+ Add Widget** → **Bar chart**
2. Configuration:
   - **Data source:** Traces (not observations — usage is on the trace)
   - **Filter:** `metadata.attributes.gen_ai.system = llama.cpp`
   - **Group by:** `metadata.attributes.sovereign.source`
   - **Y-axis:** Sum of `metadata.attributes.gen_ai.usage.input_tokens`
   - **Stack with:** Sum of `metadata.attributes.gen_ai.usage.output_tokens`

## Dashboard 4 — Top queries (table)

Table widget showing the most common questions asked.

1. **+ Add Widget** → **Table**
2. Configuration:
   - **Data source:** Traces
   - **Filter:** `name = audittrace.routes.chat.chat_completions`
   - **Columns:** timestamp, `sovereign.source`, `sovereign.memory.query`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, latency
   - **Sort:** timestamp DESC
   - **Limit:** 50

## Trace tree (built-in, no setup)

Every chat completion trace shows the full call tree under
**Traces → click any trace → Tree view**. The structure is:

```
chat_completions
├── _extract_query
├── DefaultContextBuilder.build_system_context
│   ├── FileEpisodicService.search → load
│   ├── FileProceduralService.search → load
│   ├── PostgresConversationalService.as_context → load_sessions
│   └── ChromaSemanticService.search → query
├── _merge_system_message
└── (httpx call to llama-server, not instrumented)
```

Toggle between **Tree view** and **Timeline (waterfall)** at the top.
The waterfall is the closest equivalent to "graph of calls between components"
from the old stack — each span shows up as a horizontal bar at its depth.

## SQL query for ad-hoc analysis

Langfuse stores observations in ClickHouse. You can query directly via
the **Langfuse SQL Editor** (Settings → SQL Editor in some plans, or
via direct ClickHouse access):

```sql
-- Memory layer call counts in the last hour
SELECT
  attributes['sovereign.component'] AS component,
  count() AS calls,
  quantile(0.50)(latency_ms) AS p50_ms,
  quantile(0.95)(latency_ms) AS p95_ms
FROM observations
WHERE start_time >= now() - INTERVAL 1 HOUR
  AND attributes['sovereign.component'] LIKE 'memory.%'
GROUP BY component
ORDER BY calls DESC;
```
