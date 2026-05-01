---
title: "fix(telemetry): scope langgraph_step counter to a single trace"
labels: ["bug", "observability", "langfuse"]
priority: P3
---

## Context

`src/audittrace/logging_config.py:_langgraph_step_counter` is a single
process-global `itertools.count(1)` whose value is written into every span's
`langgraph_step` attribute (via metadata at `start_as_current_observation`
time and as an OTel `set_attribute`).

```python
_langgraph_step_counter = itertools.count(1)
```

Langfuse uses `langgraph_step` to order nodes in the graph view. With a
process-global counter, two concurrent requests interleave their step numbers
arbitrarily, so request A's nodes might be numbered `[12, 14, 17, 19]` and
request B's `[13, 15, 16, 18]`. The graph still renders but the numbers
become meaningless across the dashboard.

Observed in the wild on 2026-04-11 — a single quiet test trace shows
`langgraph_step: 79` because the counter accumulated across the whole
process lifetime.

## Fix sketch

Scope the counter per-trace using a `contextvars.ContextVar[int]` reset at
the start of each chat request (in `_build_memory_context_with_trace`, just
after the `@observe` span opens). The `@log_call` aspect reads from the
contextvar and increments it atomically.

```python
_LANGGRAPH_STEP: ContextVar[itertools.count] = ContextVar(
    "langgraph_step", default=None
)

def _next_step() -> int:
    counter = _LANGGRAPH_STEP.get()
    if counter is None:
        counter = itertools.count(1)
        _LANGGRAPH_STEP.set(counter)
    return next(counter)
```

## Acceptance criteria

- Two concurrent chat requests in a test produce span numberings starting at
  1 each (assert via concurrent `asyncio.gather` of two `client.post(...)`
  calls and inspecting Langfuse SDK calls).
- Existing tests still pass.
- No measurable latency increase.
