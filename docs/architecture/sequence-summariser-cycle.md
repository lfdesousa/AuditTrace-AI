# Sequence Diagram: Background Session Summariser Cycle (ADR-030 Part 2)

> **Created 2026-04-15** for ADR-030 Part 2. Companion runtime view
> for the static topology captured in `workspace.dsl`'s
> `sessionSummarizer` component and `summarizerServer` softwareSystem.

The session summariser is an `asyncio.create_task` started from
`server.py::lifespan` when `SOVEREIGN_SUMMARIZER_ENABLED=true`. It
wakes every `SOVEREIGN_SUMMARIZER_INTERVAL_MINUTES` (default 5m),
picks up to `SOVEREIGN_SUMMARIZER_MAX_PER_CYCLE` eligible sessions
(default 10), calls Mistral 7B Instruct v0.3 on `:11437` for each,
and upserts a `SessionRecord` under the session's user identity.
The hybrid `recall_recent_sessions` tool (ADR-030 Part 1) transparently
gives way to real summaries as they accumulate.

**Mistral is the ONLY caller of `:11437` in this repo.** Chat and eval
traffic never touch it — the summariser is architecturally isolated so
background generation does not contend with user-facing latency. The
2026-04-15 contention eval confirmed the footprint is in-the-noise at
`--n-gpu-layers 10` partial offload.

## Component map

```
server.py::lifespan
      │  asyncio.create_task(summarizer.run(), name="session-summarizer")
      ▼
SessionSummarizer.run             (services/session_summarizer.py)
      │ every interval_minutes
      ▼
SessionSummarizer.run_once
      │ eligible = await asyncio.to_thread(_find_eligible)
      ▼
_find_eligible
      │  SET LOCAL row_security = off       ← Postgres table-owner bypass
      │  SELECT ... FROM (SELECT session_id, user_id, project,
      │                        MAX(timestamp) AS last_ts
      │                   FROM interactions
      │                   WHERE session_id IS NOT NULL
      │                   GROUP BY ...) sub
      │   LEFT JOIN sessions s ON s.id = sub.session_id
      │   WHERE sub.last_ts < :threshold
      │   ORDER BY (s.summarized_at IS NULL) DESC, sub.last_ts ASC
      │   LIMIT :fetch_limit                 ← max_per_cycle * 3
      │  Python-side _is_stale(last_ts, summarized_at) filter
      ▼
for each eligible session:
      ▼
_summarise_one (→ _fetch_turns → _call_llm → _parse_llm_response → _persist)
```

The `ORDER BY (s.summarized_at IS NULL) DESC, sub.last_ts ASC` clause
is load-bearing — it prioritises never-summarised sessions so a
backlog of up-to-date older sessions cannot starve newly-idle sessions
from being picked. This is the fix from commit `1428ec1` (the
2026-04-15 starvation bug).

## Happy path — one eligible session, clean summary

```mermaid
sequenceDiagram
    participant Loop as SessionSummarizer.run\n(background asyncio task)
    participant DB as sovereign-postgres\n(owner-role connection, ADR-030 §4)
    participant LLM as llama-server :11437\n(Mistral 7B Instruct v0.3)
    participant Parse as _parse_llm_response

    Note over Loop: Wake every SOVEREIGN_SUMMARIZER_INTERVAL_MINUTES (5m default).\nUsing dedicated owner-role factory when summarizer_postgres_url is set.

    Loop->>DB: BEGIN; SET LOCAL row_security = off
    Loop->>DB: SELECT sub.session_id, sub.user_id, sub.project, sub.last_ts, s.summarized_at\nFROM (SELECT … MAX(timestamp) …) sub\nLEFT JOIN sessions s ON s.id = sub.session_id\nWHERE sub.last_ts < :threshold\nORDER BY (s.summarized_at IS NULL) DESC, sub.last_ts ASC\nLIMIT :fetch_limit
    DB-->>Loop: rows (up to max_per_cycle * 3)
    Loop->>DB: COMMIT

    Note over Loop: _is_stale filter in Python\n(summarized_at IS NULL OR summarized_at < last_ts)\nTruncate to max_per_cycle

    rect rgb(230, 245, 230)
        Note over Loop,LLM: Per-session loop — sequential

        Loop->>DB: BEGIN; SET LOCAL row_security = off
        Loop->>DB: SELECT * FROM interactions WHERE session_id=:sid\nAND user_id=:uid AND project=:proj\nORDER BY timestamp
        DB-->>Loop: [InteractionRecord…]
        Loop->>DB: COMMIT

        Note over Loop: _format_transcript(turns) →\nnumbered Q/A pairs

        Loop->>LLM: POST /v1/chat/completions (stream=false)\n{model: mistral-7b-summarizer,\n messages: [system (strict-JSON schema),\n            user (numbered transcript)],\n temperature: 0.2, max_tokens: 600,\n response_format: {type: "json_object"}}
        LLM-->>Loop: {choices:[{message:{content: "{\"summary\":\"…\",\"key_points\":[…]}"}}]}

        Loop->>Parse: _parse_llm_response(content)
        Note over Parse: Strip markdown fences if any; json.loads;\nreturn None on any parse failure (retry next cycle)
        Parse-->>Loop: {"summary": "…", "key_points": […]}

        Loop->>DB: BEGIN; SET LOCAL app.current_user_id = :uid   ← RLS attribution
        alt SessionRecord exists for this session_id
            Loop->>DB: UPDATE sessions SET summary, key_points, date,\nmodel=mistral-7b-summarizer, summarized_at=NOW()\nWHERE id=:sid
        else never summarised
            Loop->>DB: INSERT INTO sessions (id=session_id, project, date,\nsummary, key_points, model, user_id, summarized_at)
        end
        Loop->>DB: COMMIT
        Note over DB: RLS WITH CHECK passes because app.current_user_id == row.user_id
    end

    Note over Loop: Sleep interval_minutes; loop forever\nuntil lifespan shutdown cancels the task
```

## Retry path — malformed LLM JSON

When the LLM returns anything `_parse_llm_response` cannot parse (empty
body, ``choices: []``, broken JSON after fence-strip), the summariser
**writes nothing** and moves on. `summarized_at` stays NULL (or its
prior stale value); the same session is eligible again on the next
cycle. No partial writes, no data corruption. llama.cpp's
`response_format={"type":"json_object"}` makes this nearly impossible
in practice (grammar-constrained decoding), but other OpenAI-compat
backends may treat it as advisory.

```mermaid
sequenceDiagram
    participant Loop as SessionSummarizer.run
    participant LLM as llama-server :11437
    participant Parse as _parse_llm_response
    participant DB as sovereign-postgres
    participant Log as logger

    Loop->>LLM: POST /v1/chat/completions (strict JSON)
    LLM-->>Loop: {choices:[{message:{content: "not json at all"}}]}

    Loop->>Parse: _parse_llm_response("not json at all")
    Parse-->>Loop: None

    Loop->>Log: warning: malformed JSON response for session=…

    Note over Loop,DB: No DB write — summarized_at unchanged,\nrow is still eligible next cycle.

    Note over Loop: run_once's try/except/continue keeps the cycle alive;\nper-session failures never break the run loop.
```

## Concurrency safeguard — multi-worker case (future)

Today we run ONE summariser worker per memory-server process. If we
ever horizontally scale (multiple memory-server replicas), the
eligibility query's `FOR UPDATE OF s SKIP LOCKED` clause (spelled out
in ADR-030 §4 but currently commented as a design-time consideration,
not wired in the code) prevents two workers from picking the same
row. For the single-worker deployment today it is a no-op; documented
here so the design rationale survives.

## RLS bypass — why the worker needs the owner role

ADR-026 §16 (Phase 4) forces RLS on the `interactions`, `sessions`,
and `tool_calls` tables. The main memory-server connects as
`sovereign_app` — a role without the privilege to bypass RLS via
`SET LOCAL row_security = off`. The summariser reads across every
user's interactions to build audit summaries — a privilege the main
memory-server intentionally does not have. Hence the `summarizer_
postgres_url` setting: when set, a **dedicated owner-role
connection** (as `sovereign`, the schema owner) handles the
eligibility read. Inside each per-session write transaction the
worker re-narrows scope with `SET LOCAL app.current_user_id = :uid`
so the row's `user_id` matches the GUC — RLS `WITH CHECK` passes.

On SQLite (tests), both `SET LOCAL` statements are no-ops because
SQLite has no RLS.

Fixed in commit `1a82eed` after live-validation surfaced the original
`sovereign_app`-connection variant failing with
`query would be affected by row-level security policy`.

## What this doc does NOT cover

- **ADR-030 Part 1 hybrid recall** — that's a read-side behaviour of
  `PostgresConversationalService.load_sessions`, not part of the
  summariser loop. It sits on the chat-request hot path, covered in
  the chat-completions sequence diagram alongside the tool-loop.
- **Eval comparisons of summariser models.** See
  `docs/eval-memory-modes-20260415-*.md`.
- **Langfuse trace shape for summariser calls.** Tempo side confirmed
  (`peer.service=mistral-summariser-llm` edge is live); Langfuse side
  is the open carry-over.

## Related documents

- **ADR-030** — the authoritative design record (hybrid recall + background loop + three-model topology).
- **`sequence-oauth2-flow.md`** — for the per-request identity resolution the summariser bypasses by running under `sovereign` directly.
- **`sequence-memory-tool-call.md`** — for how `recall_recent_sessions` consumes the rows this worker writes.
- **`eval-memory-modes-20260415-contention.md`** — for the empirical Mistral residency cost measurement behind the `--n-gpu-layers 10` default.
