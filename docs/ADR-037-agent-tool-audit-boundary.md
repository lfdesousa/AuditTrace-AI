# ADR-037: Agent tool calls are out of audit scope; only memory-server-executed tools are captured

- **Status:** Accepted
- **Date:** 2026-04-18
- **Context:** ADR-025 (memory-as-tools), ADR-026 (multi-user identity), ADR-029 (audit trail completeness), ADR-033 (three-audience error envelope)

## Context

The AuditTrace-AI memory-server exposes four memory tools to the agent (`recall_decisions`, `recall_skills`, `recall_recent_sessions`, `recall_semantic`; ADR-025). It also executes them proxy-internally and persists one `tool_calls` row per invocation (ADR-029).

An agentic client such as OpenCode, Continue, or Cursor typically invokes *many more* tools than these four: `bash`, `read`, `edit`, `write`, `grep`, `glob`, `web_fetch`, etc. These live entirely on the client side: the LLM emits a `tool_calls` block in its response, the client executes the tool locally, and the client injects the tool result back into the conversation on its next round-trip. The memory-server sees these invocations only as literal JSON inside the conversation's `messages` list — it never executes them, never gates them, and never inspects their results.

Over the past weeks, while building the reconstructibility story for EU AI Act Article 12 compliance, we have repeatedly stumbled over the question: should memory-server also capture agent-side tool calls? Two forcing functions made the question unavoidable:

1. The interaction's `answer` column records the model's text response verbatim, including any `tool_calls` array the model emitted, rendered as `[tool_call] name(args)` lines. So traces of agent-side calls *are* visible in the audit trail, just unstructured.
2. Today's end-to-end telemetry work (commits `0823b54`…`fa5198a`) surfaces a question of trust boundary: the memory-server's observability now covers its own execution fully, but stops abruptly where the client-side tool-call loop begins.

## Decision

The memory-server's audit boundary coincides with the memory-server process boundary.

- **In scope.** Requests and responses on `/v1/chat/completions` (an `interactions` row per call). Invocations of memory-server-executed tools — today the four ADR-025 recall tools — persisted as `tool_calls` rows, each with a foreign key to its interaction. Authentication and scope-check events (audited via structured logs). Memory retrievals from the 4-layer storage (ChromaDB, PostgreSQL, MinIO, episodic/procedural files) via the `@log_call` aspect.
- **Out of scope.** Agent-side tools — `bash`, `read`, `edit`, `write`, `grep`, `glob`, `web_fetch`, and their peers. The memory-server does not execute these, does not have access to their results, and cannot audit them. They are the client agent's responsibility.

We explicitly reject three alternatives:

1. **Parse-and-store the `tool_calls` array from LLM responses** as a separate audit table. This would give us a record of *what the model asked for* but not *what happened* — the caller can trivially lie about results. A partial audit is worse than an honest boundary.
2. **Require the agent to echo back tool results** via a new `/tool_results` endpoint. This changes the client contract, is opt-in at best, and cannot be enforced — an agent acting in bad faith simply skips the echo. Again, the reconstructibility story is only as strong as the weakest link.
3. **Run the agent harness inside a memory-server-controlled sandbox** (e.g., co-located container). Technically feasible but fundamentally the wrong layer: the memory-server is a memory-and-audit substrate, not an agent runtime. This conflates two concerns.

## Consequences

### For reconstructibility (the EU AI Act Article 12 story)

The memory-server audit trail reconstructs:

- Who asked (`interactions.user_id` = Keycloak `sub`).
- What they asked (`interactions.question`).
- What the memory-server returned (`interactions.answer`, including any client-side tool calls the model emitted).
- How long it took (`interactions.duration_ms`).
- Whether it succeeded (`interactions.status`, `interactions.failure_class` from ADR-033).
- What memory layers were consulted (`tool_calls` rows for memory recalls).

It does *not* reconstruct what the agent did with the model's response on the client side. That is the agent's own concern — OpenCode, for example, has its own transcript logs; Claude Code has a `.claude` session directory; etc.

### For the compliance narrative

When explaining the architecture to an auditor or regulator, the phrasing is precise:

> *"The AuditTrace-AI server keeps a complete record of every LLM interaction it handles, including the inputs, the outputs, and every memory retrieval it executes. Actions taken by the agent on the user's local machine — file edits, shell commands, web fetches — are outside the server's trust boundary and are audited by the agent itself, not by the server."*

This matches the architectural reality (the server never touches those files or runs those commands) and does not overclaim. Overclaiming here would be worse than under-promising: an audit row that exists but cannot be proven against ground truth is a liability.

### For the schema

No changes to `interactions` or `tool_calls`. Migration 007 (ADR-033) already gives us the status/failure_class columns needed for the failure-audit story. The `tool_calls` table continues to carry exactly one row per memory-server-executed memory tool invocation.

### For the `interactions.answer` column

The rendered `[tool_call] name(args)` lines inside `answer` remain purely descriptive. They are the *evidence that the model asked for a tool*, not *evidence the tool ran*. Downstream analytics that want to correlate model-emitted tool calls with agent-side execution need a client-side signal we do not provide.

## Cross-references

- **ADR-025** introduces memory-as-tools and establishes the four recall tools.
- **ADR-026** lays out per-user identity and RLS scoping — both of which apply to `tool_calls` writes.
- **ADR-029** establishes the project-tagging contract and the `/interactions` audit browser.
- **ADR-033** adds status + failure_class columns to `interactions` (migration 007).
- **Telemetry end-to-end work, 2026-04-18** (commits `0823b54`, `20d0fd9`, `93de0ca`, `e7005e0`, `8d32440`, `65a5965`, `fa5198a`) wires Langfuse + Tempo + Loki end-to-end around this boundary.
