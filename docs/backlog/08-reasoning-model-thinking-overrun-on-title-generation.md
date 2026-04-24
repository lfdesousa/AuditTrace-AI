---
title: "feat: mitigate reasoning-model thinking-overrun on title / summary / compaction prompts"
labels: ["enhancement", "developer-experience", "documentation", "observability"]
priority: P2
---

## Context

Observed 2026-04-20 on a fresh OpenCode session pointed at the k3s AuditTrace-AI
deployment with `model: audittrace/qwen3.6`. The user sent one ordinary
analytical prompt ("what do you have about the collection scm supply chain
management"). The main chat-completion completed correctly. **A separate
title-generation call** — fired automatically by OpenCode's `SessionPrompt.ensureTitle()`
at session start — failed with a 2m 0s latency and `output: undefined` in the
Langfuse trace (trace `87aaaa23b844d3da18782b410e1dfb37`, span
`llm.chat.completions` marked ERROR).

Root cause: Qwen3.6-35B is a reasoning model that opens a `<think>` block even
for simple tasks like "generate a one-line title". Confirmed by a control curl
against `/v1/chat/completions` with a "say hi in one word" prompt — the response
body contained `<think>\nThe user wants me to say hi using exactly one word.\n…`
before the actual content. For short prompts the thinking completes in time.
For title-gen prompts (which wrap the user's real message and ask for a meta-summary),
the thinking budget overshoots OpenCode's or the proxy's timeout.

This matches the existing internal-memory note *"eval 'failures' may be
thinking-overrun-timeout"* — the failure mode was known, but no mitigation has
shipped. The user-visible effect in Langfuse (a span with no output, rendered
as `undefined`) is indistinguishable from a real broken-instrumentation bug —
see backlog item #09 for the observability side.

OpenCode's config schema (discovered by reading the compiled `bin/.opencode`) has
two knobs that avoid this entirely:

- Top-level `small_model: "<provider>/<model>"` — used by the title / summary /
  compaction agents when they have no explicit `model` override.
- Per-agent `agent.title.model`, `agent.summary.model`, `agent.compaction.model`
  + optional `prompt` / system-prompt override.

AuditTrace-AI ships no guidance on either. A new user installs, configures
OpenCode with the documented single-model setup, and hits the 2-minute title
timeout on their very first session. **This is a first-impression regression
that a new contributor encounters on day one.**

## Fix sketch — three independent layers

Any subset is independently valuable; the issue tracks whichever combination
we commit to.

### Layer 1 — documentation-only (cheapest, zero backend change)

Add `docs/guides/opencode-setup.md` (or extend the existing Device Flow guide)
with a *Recommended OpenCode config for AuditTrace-AI* section. Include:

- The current `provider.audittrace` block (already documented elsewhere).
- A `small_model` pointer that injects `/no_think` via a
  `provider.audittrace.models.qwen3.6-nothink` entry with a matching system-prompt
  prefix.
- OR a per-agent `agent.title.prompt` override that prepends `/no_think` to the
  title-generation system prompt. Qwen3 honours `/no_think` as an inline switch.
- A note that the same applies to `agent.summary` and `agent.compaction`.

Estimated effort: half a day including verification.

### Layer 2 — proxy-side pattern-match injection (user-agnostic)

Add a FastAPI middleware that inspects the incoming system prompt on
`/v1/chat/completions`. If the prompt matches a configurable whitelist of
"meta-task" patterns (title-gen, summarisation, compaction — each of which
starts with a very specific marker string OpenCode/Continue/other agents emit),
inject `/no_think` at the top of the system message before forwarding to the
inner LLM server.

Governed by a config flag `AUDITTRACE_META_TASK_NOTHINK_ENABLED` (default `false`
until the pattern library is mature). When active, the Langfuse span MUST record
the injection as an explicit `metadata.injected_no_think: true` so
reconstructibility is preserved — per the feedback-memory "OpenAI schema
inviolate" rule, this is additive metadata, not a prompt rewrite visible to the
caller.

Estimated effort: two days including pattern-library + tests + docs.

### Layer 3 — per-model reasoning-budget config (generalised)

Introduce an ADR-scale concept: each model in the provider config carries a
`reasoning_budget` (soft cap on thinking tokens) + `reasoning_strategy`
(`allow` / `strip_think_tag` / `inject_no_think_on_pattern` /
`force_no_think`). The proxy applies the strategy on egress. This folds
layers 1-2 into a principled framework and extends to future reasoning models
that arrive with their own thinking protocols.

Requires an ADR. Candidate ADR-042.

## Constraints

- **OpenAI schema inviolate** (see feedback memory). The request body arriving
  at the inner LLM may be modified; the response body returned to the client
  must remain strict-superset OpenAI. `/v1/chat/completions` POST shape is
  unchanged.
- **Reconstructibility preserved.** Any proxy-side prompt modification MUST
  show in the Langfuse span (`metadata.injected_no_think`, or a dedicated
  `proxy.prompt_modification` span) so an auditor can replay the *actual*
  prompt the model saw, not the caller's version. This is non-negotiable —
  the reconstructibility contract is the reason clients would choose
  AuditTrace-AI at all.
- **Additive only.** Non-reasoning models (Claude, GPT-4) must see zero
  behaviour change. Layer 2 + 3 gated on reasoning-capable model class.
- **Documentation precedes code.** Ship Layer 1 first so a user today can
  unblock themselves without a code change.

## Acceptance criteria

**Layer 1 (required for ticket close)**

- `docs/guides/opencode-setup.md` exists with a tested config sample.
- A clean `opencode` install against a fresh AuditTrace-AI cluster produces a
  first-session title in < 10s (currently 2 minutes → timeout).
- The existing `docs/guides/deployment-runbook.md` cross-links to the new
  guide from the "Step 8" equivalent section.

**Layer 2 (optional, can be a follow-up ticket)**

- `AUDITTRACE_META_TASK_NOTHINK_ENABLED=true` enables the middleware.
- Langfuse trace on a title-gen request shows `metadata.injected_no_think: true`
  and the original + modified prompts both captured.
- Regression test: a non-reasoning-model request is bitwise-unchanged.

**Layer 3 (ADR-042)**

- ADR written + reviewed.
- `reasoning_budget` + `reasoning_strategy` configurable per model.
- Documented migration path from Layer 2 config to Layer 3.

## Evidence (2026-04-20)

- Langfuse trace id available on request (ephemeral, local instance)
- Failing span: `llm.chat.completions`, duration 2 m 0 s, status ERROR
- Control success: the same prompt via direct curl with
  `{"content":"say hi in one word"}` completed in 6 s with a visible
  `<think>` tag in the response.

## Related

- Backlog #09 (Langfuse `undefined` output rendering on timeout / error spans)
- ADR-024 regression precedent (OpenAI schema inviolate — fix must be
  additive, never break `/v1/chat/completions` default POST shape).
