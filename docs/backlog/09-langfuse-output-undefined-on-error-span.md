---
title: "obs: distinguish 'empty result' from 'timeout / error' in Langfuse span outputs"
labels: ["observability", "tech-debt", "langfuse"]
priority: P2
---

## Context

When an LLM call inside `/v1/chat/completions` terminates early (timeout,
upstream error, cancelled stream), the wrapping `llm.chat.completions`
OpenTelemetry span ends without an `output` attribute set. Langfuse renders
this span with `output: undefined` and `output: null` variations, depending on
the view.

The problem: **the same visual treatment is used for a tool call that
completed successfully and returned an empty collection.** An operator reading
the trace can't distinguish:

- "The request failed at 2 minutes, nothing was returned." (today's
  title-generation case — see backlog #08)
- "The request succeeded. The memory-episodic tool call had zero results."

In a reconstructibility-first system this matters. The trace is how an
auditor learns what happened. "Nothing was returned" and "the call failed"
need to be visually and structurally distinct in the UI and in any export.

Observed 2026-04-20 on trace `87aaaa23b844d3da18782b410e1dfb37`. The initial
user-reported symptom — "I saw errors in the Langfuse traces when accessing
memory there was an output null" — turned out to be a *timeout* on
`llm.chat.completions`, not a memory-tool failure, but the rendering collapsed
both cases to the same-looking artefact. 30+ minutes of misdiagnosis ensued.

Memory `project_session_20260418` documents a pass where "every Langfuse
'undefined' was eliminated" on the success paths. That pass did not cover
error paths; this issue is the complementary fix.

## Fix sketch

### Instrumentation changes (primary)

For every span decorated with the project's OTel decorators (the
`@trace_span` / `langfuse_span` convenience wrappers under
`src/audittrace/observability/`), on exception or early termination:

1. Set span status `error` with a structured reason.
2. Set `output` to a **non-null object** containing:
   ```json
   {
     "error_code": "<stable_enum>",
     "message": "<human readable, no secrets>",
     "elapsed_ms": <integer>,
     "category": "<timeout|upstream|cancelled|validation|unknown>"
   }
   ```
3. Surface the `error_code` as a span attribute (`audittrace.error.code`)
   so it's searchable in Tempo/Loki as well as Langfuse.

### Empty-result convention (secondary)

For tool calls and provider calls that legitimately return nothing, standardise
on a **non-null empty shape**:

```json
{ "results": [], "count": 0, "query": "<redacted or whitelisted>" }
```

This was already partially done on the success paths per the 2026-04-18
"undefined"-cleanup pass; audit and extend to cover the remaining tool
adapters (memory:episodic:read, memory:semantic:read, memory:procedural:read,
memory:conversational:read-own).

### UI / dashboarding (tertiary)

In `docs/langfuse-dashboards.md`, document a saved view that filters by
`status=error` and groups by `error_code` — today there's no canonical way
to answer "show me everything that failed in the last hour".

## Constraints

- **No secrets in error payloads.** Prompts, JWTs, API keys, tool arguments
  that look like credentials must be stripped from the `message` field. Reuse
  the existing redaction utility from `src/audittrace/routes/audit.py`.
- **Error-output serialisation stable across both sinks.** Langfuse and Tempo
  get the same structure. One serialiser, two exporters. Regression test.
- **Backwards-compatible shape.** Downstream consumers (eval notebooks,
  reconstructibility bundles) that currently treat `output == None` as
  "no data" will continue to work: the new error-object is additive, and the
  empty-tool-result convention uses `results: []` which was already the
  intent.
- **Non-negotiable: reconstructibility.** The error output MUST be
  serialisable into the per-user audit bundle alongside successful spans.
  A failed call is still an auditable event — dropping it from the bundle
  would be worse than the current ambiguity.

## Acceptance criteria

- A synthetic test (force a 2-second timeout against a mocked LLM) produces a
  Langfuse span with `status=error`, `output={error_code, message,
  elapsed_ms, category}`, and `audittrace.error.code` attribute.
- A synthetic test (empty memory-episodic-read against a user with no
  episodic rows) produces a Langfuse span with `status=ok`, `output={results:
  [], count: 0, …}`, no `null` field anywhere in the captured output.
- Reconstructibility bundle export for a session that contains one error span
  and one empty-result span shows both, distinguishable by `status`.
- `docs/langfuse-dashboards.md` has a saved-view stanza for
  `status=error` + `error_code` grouping, with a screenshot check-in.

## Related

- Backlog #08 (reasoning-model thinking-overrun — surfaced this ambiguity)
- Every meaningful change needs a test + live-system witness; synthetic
  tests above cover the instrumentation side.
- EU AI Act Art 12 requires the full call chain to remain visible;
  error spans must not drop out of the trace tree, which is what the
  dashboard rendering currently hides.
- ADR-033 (three-audience error envelope) — the error *envelope* at the HTTP
  response layer; this issue covers the *trace* layer
