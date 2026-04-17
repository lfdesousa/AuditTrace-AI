# ADR-034: Long-Running Generation — Per-Chunk Idle Timeout, SSE Keep-Alive, X-Thinking

**Status:** Accepted
**Date:** 2026-04-17
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-024 (proxy pass-through), ADR-033 (error envelope), ADR-025 (memory-as-tools)

## Context

On 2026-04-15 a production chat request ran for exactly 5 minutes and was
killed by `SOVEREIGN_LLAMA_PROXY_TIMEOUT=300`, surfacing as an HTTP 500
(before ADR-033's error envelope fix). Luis waited out a similar request
and discovered it was valid Qwen `<think>` reasoning — the model was
deliberating on a complex architectural prompt for approximately 15
minutes before generating a high-quality answer.

The eval harness (N=10, `ambiguous` category) showed an 80% timeout rate
on analytical prompts in tools mode. Post-investigation, these were
largely thinking-overrun-timeout artefacts, not genuine capability
failures. The flat 300s total timeout was cutting off valid model work.

Three problems required simultaneous resolution:

1. **Flat total timeout kills valid long-running generation.** The
   `httpx.AsyncClient(timeout=300)` timeout applies to the entire
   streaming connection, not per-chunk. A 15-minute response with
   continuous token flow is killed identically to a genuinely stalled
   connection.

2. **No progress signal during quiet periods.** When Qwen enters
   `<think>` mode, it may produce no SSE data frames for extended
   periods while reasoning internally. The client receives no
   indication that the server is alive, leading to user abandonment
   and reverse-proxy idle-connection drops (Traefik default: 180s,
   future Istio ingress gateway: configurable but finite).

3. **Thinking depth is a server heuristic, not a user choice.** The
   target user of AuditTrace-AI is an architect or researcher asking
   genuinely hard questions. Whether to engage deep reasoning should
   be a user-expressed knob, not an opaque server decision.

### Alternatives considered

- **Increase `SOVEREIGN_LLAMA_PROXY_TIMEOUT` to 900s.** Band-aid.
  Delays the failure without fixing the architecture, wastes resources
  on genuinely stalled connections.

- **Async job pattern (POST → 202 + job_id, GET polls).** The
  k8s-correct long-term answer (Phase 5 on the roadmap), but a large
  lift that breaks the OpenAI `/v1/chat/completions` contract's
  synchronous semantics. Deferred until k8s deployment is imminent.

## Decision

Three changes, shipped as independent commits for reviewability:

### §1. Per-chunk idle timeout

Replace the flat total timeout with a per-chunk idle timeout. The
streaming path uses `asyncio.wait()` with a persistent `__anext__`
task: if no SSE line arrives within `SOVEREIGN_LLAMA_CHUNK_TIMEOUT`
(default 120s), the stream is considered stalled and `httpx.ReadTimeout`
is raised — triggering the existing ADR-033 error envelope and audit
trail. As long as tokens keep flowing, the stream stays alive
indefinitely.

The httpx client is constructed with a granular `httpx.Timeout`:

```python
httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
```

Connect timeout is hardcoded at 10s — if we cannot TCP-connect to
llama-server in 10s, the server is down. The `read=None` disables
httpx's total-stream timeout; the per-chunk helper handles idle
detection.

Non-streaming and tool-loop paths use the same granular Timeout with
`read=llama_chunk_timeout`.

### §2. SSE keep-alive comment frames

When `SOVEREIGN_SSE_KEEPALIVE_INTERVAL` > 0 (default 15s), the idle
timeout helper yields `None` every *keepalive_interval* seconds during
quiet periods. The streaming generator converts `None` yields to
`: keep-alive\n\n` — an SSE comment frame (RFC 8895 §9.2.3) that is
invisible to JSON parsers, ignored by conformant SSE clients, and
keeps the TCP connection alive through reverse proxies.

After `SOVEREIGN_LLAMA_CHUNK_TIMEOUT` total silence (accumulated across
keep-alive cycles), `httpx.ReadTimeout` is raised. A real data line
arriving between keep-alive cycles resets the idle clock.

### §3. X-Thinking header

Parse `X-Thinking: deep | fast | auto` from the HTTP request header:

| Value | Effect |
|-------|--------|
| `deep` | Inject `chat_template_kwargs.enable_thinking = true` into the proxy payload |
| `fast` | Inject `chat_template_kwargs.enable_thinking = false` |
| `auto` (default, or absent) | Leave payload untouched — model default applies |

The header is symmetric with `X-Project` (ADR-029). Per Luis's
principle (2026-04-15 22:10 CEST): "Depth is a user-expressed knob,
not a server heuristic." Applied before the inject/tools branch so
both paths honour it. Existing `chat_template_kwargs` fields in the
request body are preserved (the helper uses `setdefault`).

### Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `SOVEREIGN_LLAMA_CHUNK_TIMEOUT` | 120 | Per-chunk idle timeout (seconds). Stream stalls beyond this trigger 504. |
| `SOVEREIGN_SSE_KEEPALIVE_INTERVAL` | 15 | Keep-alive comment frame interval (seconds). 0 disables. |
| `SOVEREIGN_LLAMA_PROXY_TIMEOUT` | 120 | **Deprecated.** Retained in config for backward compatibility; no longer referenced by hot paths. |

## Consequences

**What becomes easier:**

- Long-running `<think>` generation completes regardless of total
  duration. A 15-minute valid reasoning session survives as long as
  tokens flow within 120s of each other — directly solving the
  2026-04-15 incident.
- Clients receive keep-alive frames during quiet periods, eliminating
  the "is it broken or thinking?" ambiguity. Users can trust that
  silence means deliberation, not failure.
- Per-request thinking depth gives operators and power users explicit
  control over the performance/depth trade-off via a single HTTP
  header.
- k8s/ZTA forward-compatible: SSE keep-alive frames prevent Istio
  ingress gateway idle-connection drops without gateway-level
  configuration.

**What becomes harder:**

- The per-chunk idle timeout model is more complex than a flat timeout.
  The `asyncio.wait()` + persistent `__anext__` task pattern requires
  careful cleanup in the `finally` block to avoid leaked tasks.
- `SOVEREIGN_LLAMA_PROXY_TIMEOUT` is now a no-op in the hot paths.
  Operators who tuned it must migrate to `SOVEREIGN_LLAMA_CHUNK_TIMEOUT`.
  The old variable remains in config to avoid startup errors on
  existing `.env` files.
- Clients that stream raw bytes (not SSE-aware parsers) will see
  `: keep-alive\n\n` lines in their output. This is correct per the
  SSE spec but may surprise `curl` users.

**Validation:**

- 558 tests pass, 94.86% coverage, per-file gate ≥ 90%.
- 9 new tests in `test_chat_long_running.py`: idle timeout (4) +
  SSE keep-alive (5).
- 10 new tests in `test_chat_proxy.py`: X-Thinking header parsing
  and payload injection.
- All existing failure-audit and OpenAI-compatibility tests pass
  unchanged — the `httpx.ReadTimeout` raise reuses the same error
  handler pipeline.
