# Sequence Diagram: build_system_context()

Internal flow of the `DefaultContextBuilder.build_system_context()` method.
Each layer is wrapped in try/except — failure in one layer does not break
the others. The query drives what is retrieved — "hello" returns nothing,
"KV cache compression" returns ADR-009.

```mermaid
sequenceDiagram
    participant Caller as Route Handler
    participant Builder as DefaultContextBuilder
    participant Episodic as FileEpisodicService
    participant FS_ADR as ADR-*.md files
    participant Procedural as FileProceduralService
    participant FS_SKILL as SKILL-*.md files
    participant Conv as PostgresConversationalService
    participant PG as PostgreSQL 16
    participant Semantic as ChromaSemanticService
    participant Chroma as ChromaDB Server

    Caller->>Builder: build_system_context(project, query)

    Note over Builder: Always include identity section<br/>(~50 tokens)

    alt query is None
        Builder-->>Caller: profile section only
    else query provided

        Note over Builder: Layer 1: Episodic

        Builder->>Episodic: search(query)
        Episodic->>FS_ADR: glob("ADR-*.md")
        FS_ADR-->>Episodic: file contents
        Note over Episodic: Keyword filter:<br/>words > 3 chars from query<br/>No arbitrary cap
        Episodic-->>Builder: matched Documents

        Note over Builder: Layer 2: Procedural

        Builder->>Procedural: search(query)
        Procedural->>FS_SKILL: glob("SKILL-*.md")
        FS_SKILL-->>Procedural: file contents
        Note over Procedural: Keyword filter on<br/>skill name + content<br/>No arbitrary cap
        Procedural-->>Builder: matched Documents

        Note over Builder: Layer 3: Conversational

        Builder->>Conv: as_context(project)
        Conv->>PG: SELECT sessions<br/>WHERE project = ?<br/>ORDER BY date DESC<br/>(SQLAlchemy ORM, authenticated)
        PG-->>Conv: recent sessions
        Conv-->>Builder: formatted session summaries

        Note over Builder: Layer 4: Semantic

        Builder->>Semantic: search(query, k=4)
        Semantic->>Chroma: query(query_texts, n_results)<br/>across [decisions, skills]<br/>(Bearer token auth)
        Chroma-->>Semantic: vector search results
        Semantic-->>Builder: Documents with metadata

        Note over Builder: Assemble sections<br/>separated by ---

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

    Note over Builder: Log warning,<br/>layer_stats["episodic"] = 0<br/>Continue to next layer

    Builder->>Procedural: search(query)
    Procedural-->>Builder: results (normal)

    Builder->>Conv: as_context(project)
    Conv-->>Builder: sessions (normal)

    Builder->>Semantic: search(query, k)
    Semantic-->>Builder: results (normal)

    Note over Builder: Return context from<br/>3 working layers
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

    Note over Builder: Assemble ~280 words:<br/>Profile + Selection Rules + Tool list<br/>No memory layer queries fired

    Builder-->>Handler: ambient context string

    Note over Handler: Inject as system message<br/>LLM decides which ONE tool to call<br/>based on selection rules + question intent

    Handler->>LLM: POST /chat/completions<br/>{messages: [system(ambient), user(question)],<br/>tools: [recall_decisions, ...]}

    LLM-->>Handler: {tool_calls: [{name: "recall_decisions", ...}]}

    Note over Handler: LLM selected ONE tool<br/>(not all four)
```
