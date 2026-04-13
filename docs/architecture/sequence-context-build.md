# Sequence Diagram: build_system_context()

Internal flow of the `DefaultContextBuilder.build_system_context()` method.
Each layer is wrapped in try/except — failure in one layer does not break
the others. The query drives what is retrieved — "hello" returns nothing,
"KV cache compression" returns ADR-009.

```mermaid
sequenceDiagram
    participant Caller as Route Handler
    participant Builder as DefaultContextBuilder
    participant Episodic as S3EpisodicService
    participant MinIO as MinIO (memory-shared)
    participant Procedural as S3ProceduralService
    participant Conv as PostgresConversationalService
    participant PG as PostgreSQL 16
    participant Semantic as ChromaSemanticService
    participant Chroma as ChromaDB Server

    Caller->>Builder: build_system_context(project, query)

    Note over Builder: Always include identity section\n(~50 tokens)

    alt query is None
        Builder-->>Caller: profile section only
    else query provided

        Note over Builder: Layer 1: Episodic

        Builder->>Episodic: search(query)
        Episodic->>MinIO: list_objects("episodic/ADR-*.md")
        MinIO-->>Episodic: objects (cached in memory)
        Note over Episodic: Keyword filter:\nwords > 3 chars from query\nNo arbitrary cap
        Episodic-->>Builder: matched Documents

        Note over Builder: Layer 2: Procedural

        Builder->>Procedural: search(query)
        Procedural->>MinIO: list_objects("procedural/SKILL-*.md")
        MinIO-->>Procedural: objects (cached in memory)
        Note over Procedural: Keyword filter on\nskill name + content\nNo arbitrary cap
        Procedural-->>Builder: matched Documents

        Note over Builder: Layer 3: Conversational

        Builder->>Conv: as_context(project)
        Conv->>PG: SELECT sessions\nWHERE project = ?\nORDER BY date DESC\n(SQLAlchemy ORM, authenticated)
        PG-->>Conv: recent sessions
        Conv-->>Builder: formatted session summaries

        Note over Builder: Layer 4: Semantic

        Builder->>Semantic: search(query, k=4)
        Semantic->>Chroma: query(query_texts, n_results)\nacross [decisions, skills]\n(Bearer token auth)
        Chroma-->>Semantic: vector search results
        Semantic-->>Builder: Documents with metadata

        Note over Builder: Assemble sections\nseparated by ---

        Builder-->>Caller: (context_string, layer_stats)
    end
```

## Exception Isolation

```mermaid
sequenceDiagram
    participant Builder as DefaultContextBuilder
    participant Broken as EpisodicService (broken)
    participant Procedural as ProceduralService
    participant Conv as ConversationalService
    participant Semantic as SemanticService

    Builder->>Broken: search(query)
    Broken--xBuilder: RuntimeError

    Note over Builder: Log warning,\nlayer_stats["episodic"] = 0\nContinue to next layer

    Builder->>Procedural: search(query)
    Procedural-->>Builder: results (normal)

    Builder->>Conv: as_context(project)
    Conv-->>Builder: sessions (normal)

    Builder->>Semantic: search(query, k)
    Semantic-->>Builder: results (normal)

    Note over Builder: Return context from\n3 working layers
```

## Tools-mode: build_ambient_context() (ADR-025, Accepted)

When `SOVEREIGN_MEMORY_MODE=tools`, the full 4-layer context build does NOT run.
Instead, `build_ambient_context()` produces a lightweight system message (~280 words)
containing:

1. **Profile** — username, role, project, date
2. **Selection rules** — intent-to-tool mapping that guides the LLM to pick ONE tool:
   - Architectural decision → `recall_decisions`
   - Methodology/pattern → `recall_skills`
   - Previous session → `recall_recent_sessions`
   - Everything else → `recall_semantic` (fallback)
3. **Tool descriptions** — name + truncated description for each visible tool

The LLM reads the selection rules and calls only the most relevant tool per question.
No proxy-side classifier is needed — the model makes its own routing decision.

```mermaid
sequenceDiagram
    participant Handler as _handle_tools_mode
    participant Registry as tools_visible_to
    participant Builder as build_ambient_context
    participant LLM as llama-server

    Handler->>Registry: tools_visible_to(user)
    Registry-->>Handler: 4 memory tool defs (scope-filtered)

    Handler->>Builder: build_ambient_context(user, project, tools)

    Note over Builder: Assemble ~280 words:\nProfile + Selection Rules + Tool list\nNo memory layer queries fired

    Builder-->>Handler: ambient context string

    Note over Handler: Inject as system message\nLLM decides which ONE tool to call\nbased on selection rules + question intent

    Handler->>LLM: POST /chat/completions\n{messages: [system(ambient), user(question)],\ntools: [recall_decisions, ...]}

    LLM-->>Handler: {tool_calls: [{name: "recall_decisions", ...}]}

    Note over Handler: LLM selected ONE tool\n(not all four)
```
