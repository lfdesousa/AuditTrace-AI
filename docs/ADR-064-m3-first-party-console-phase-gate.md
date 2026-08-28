# ADR-064 — M3 first-party console phase gate

**Status:** Accepted (2026-08-27 — M3 authorized; build begins at WU-1)
**Date:** 2026-08-27
**Deciders:** Luis Filipe de Sousa
**Relates to:** ADR-042 (OIDC Authorization-Code + PKCE; §5 reserves the BFF), ADR-044 (external IdP
federation), ADR-047 (three-model serving topology), ADR-060 (record recall by id / `source_ref`),
ADR-062 (five-layer memory model), ADR-063 (MCP entry interface)
**Status history:**
- 2026-08-24 — Console spec ratified for evaluation (internal planning repo)
- 2026-08-27 — Accepted; M3 phase gate cleared by the operator; two implementation decisions locked

> **Numbering note.** Earlier planning artefacts referred to "the ADR-048 M3 gate." That reference was
> a drift: ADR-048 is *Ingestion content-control*. This ADR-064 is the actual M3 phase gate and
> supersedes that phantom reference wherever it appears.

## Context

1. **M3 is a phase-gated milestone.** The roadmap treats the end-user console as future work, not a
   default-on deliverable. A pilot deployment is a legitimate trigger for M3, but triggering a phase
   gate is an **operator decision** that must be recorded, not inferred by an agent from an intent
   signalled in conversation.
2. **A first-party console is the M3 shell.** It speaks the OpenAI custom-endpoint protocol natively,
   carries OIDC login, and renders SSE progressively. It becomes the *face* of the deployment while
   the AuditTrace orchestrator (`/v1/chat/completions`) remains the sole path every request travels,
   so every answer stays grounded, recalled, and traced.
3. **The one open architectural risk was identity.** A 2026-08-24 spike found the console shell's token
   forwarding is *configurable but off by default* (a shared static `apiKey`), and the per-user path
   (an `OPENID_REUSE_TOKENS` mode plus a `Bearer {{OPENID_TOKEN}}` header) is fragile: the forwarded
   token type (access vs id_token) is undocumented, while `/v1` requires the **access** token with
   `aud=audittrace-server` + scope `audittrace:query`. Gating on that fragile config was judged an
   unacceptable risk.

## Decision

**M3 is authorized. Build begins at WU-1.** Two implementation decisions are locked:

1. **Identity = Backend-for-Frontend (BFF) up front.** Rather than depend on the shell's fragile
   config-only forwarding, a small first-party BFF (ADR-042 §5 **Option A**, a dedicated sidecar that
   reuses the existing `webui/` forwarding pattern) sits between the console and `/v1`. It receives
   whatever per-user token the console forwards, validates it, and performs an **RFC 8693 token
   exchange** (confidential client) to mint an access token with `aud=audittrace-server` +
   `audittrace:query`, then proxies `/v1` **byte-identically**. This makes the access-vs-id_token /
   wrong-audience ambiguity irrelevant — identity becomes deterministic and stays token-derived.
2. **Console posture = stock upstream (pinned digest) + configuration; no source fork for the pilot.**
   No console source change is made. A fork is reserved for a later sovereignty upgrade (no customer
   chat persisted in the console's own store; sidebar reads from the memory-server), kept out of the
   pilot so the deployment stays on upstream security updates.

### The guarantee (evidence-backed)

The console works with the memory server with **no source change**: `/v1` is frozen OpenAI-compatible
and already accepts a per-user access token — proven by the first-party `webui/` harness
(`webui/index.html:512-554`), validated server-side in `src/audittrace/auth.py:322,437` with
`aud=audittrace-server` from `config.py:131` (`sub` → `user_id`, scope `audittrace:query`). The BFF
guarantees the correct audience regardless of what the console forwards.

## Consequences

- **`/v1` stays byte-inviolate.** The only response-path touch in the whole slice is the sources
  trailer, which is flag-gated (`AUDITTRACE_RESPONSE_SOURCES=trailer|off`, default `off` ⇒
  byte-identical) and rides the ADR-049 heavy gate. Everything else is realm / compose / console
  config / BFF / docs.
- **Identity remains token-derived end to end** (ADR-062, EU AI Act Art 12 traceability): every
  console request carries a `user_id` derived from the Keycloak token, through logs, traces, and
  audit rows.
- **The console is a thin shell.** No console-side model roster (one model name, `audittrace-chat`),
  no parallel memory or retrieval path, cloud features off. The console's own datastore is auxiliary
  UX state, wipeable without evidence loss; the audit plane stays the system of record.
- **Sources language honours the retrieval≠causation boundary:** a fixed label states the documents
  were retrieved into context, never a causation claim about generation.
- **A prod-wide security invariant is promoted, not created:** every locally-hosted model endpoint
  must reject unauthenticated traffic (the B1 isolation locks — loopback bind, `--api-key`,
  `--no-webui`, orchestrator-only route). This is engine-agnostic and applies beyond the console
  (WU-4).

## Build sequence (through the ADR-059 loop, memory server fed each step)

WU-0 persist (done) → **WU-1 realm client + redirect-URI hygiene** → WU-2 BFF shim (the identity
guarantee) → WU-3 compose + console config (`console` profile) → WU-4 isolation / egress locks →
WU-5 sources-trailer live-render → WU-6 docs + edge-setup → WU-7 fold-in + edge package tag. Full
plan of record and per-WU gates live in the internal M3 console spec and the buildable backlog
(private planning repo).

## Evidence

- Console spec (ratified, amended 2026-08-27): internal planning repo.
- `/v1` per-user token acceptance: `webui/index.html:512-554`; `src/audittrace/auth.py:322,437`;
  `src/audittrace/config.py:131`.
- Token-forwarding spike finding (why BFF-up-front): the M3 token-forwarding spike, 2026-08-24.
- BFF reservation: ADR-042 §5 (Option A dedicated sidecar vs Option B memory-server-hosted).
