# Collaboration roadmap — Z-book sovereign AI platform

**For Dr Ivan Roche, Founder & Principal, Otopoetic — AI Governance & Risk Advisory.**

Sent in response to your 2026-04-12 email. Ahead of our mid/late-April call, this document answers your three questions in architectural detail, then stages the work that takes the platform from its current tagged release to the enterprise-defensible posture you flagged as the actual research frontier.

**The short version.** The "runs on my machine" prototype isn't a prototype any more — it is a tagged `v1.0.0` deployment on a hardened Kubernetes control plane with Zero Trust enforced end-to-end. The platform is the foundation; the Z-book is one deployment profile of it; the enterprise server is another; they share the same architecture. What is not yet shipped — and what this roadmap proposes as the collaboration frontier — is the *transition architecture* you named: how knowledge persists and travels with a consultant while remaining governable at enterprise level. I estimate 4–5 months of focused work to reach that posture, in five phases, each with measurable exit criteria.

---

## Current state (Phase 0 — shipped)

**Repository:** https://github.com/lfdesousa/AuditTrace-AI — tagged `v1.0.0`, 610 passing tests with zero skips, CI-enforced per-file coverage gate.

**Architecture on `main` today:**
- **Runtime** — k3s with Istio `STRICT` mTLS mesh; SPIFFE workload identity on every pod; `deny-all` AuthorizationPolicy plus six per-flow allow rules; Helm chart publishes the whole stack in one `helm install`.
- **Identity** — Keycloak OAuth2 Device Flow (RFC 8628) for human agents; every request validated against a JWKS; per-user context propagated to every span, log line, and database row via a ContextVar + SQLAlchemy `after_begin` listener.
- **Isolation** — Postgres Row-Level Security with `FORCE ROW LEVEL SECURITY` and a non-superuser application role. A bug in service code cannot leak cross-user data; the database refuses.
- **Memory** — four explicit layers (episodic / procedural / conversational / semantic) exposed to the LLM via the *memory-as-tools* pattern (ADR-025): every retrieval is an explicit, auditable tool call, not a hidden pre-prompt injection.
- **Observability** — Langfuse for LLM traces, Tempo for OpenTelemetry spans across every outbound edge (Postgres, ChromaDB, MinIO, Redis, Keycloak, Langfuse, Qwen, nomic, Mistral), Loki for structured logs. Same `trace_id + user_id + session_id` triple propagates through all three stores.
- **OpenAI compatibility** — drop-in replacement for `https://api.openai.com/v1`. Strict-superset response shapes locked by regression tests against a vendored upstream OpenAPI spec; all AuditTrace features are opt-in via custom headers.
- **Reconstructibility** — for any user request, a four-API-call operator drill produces the full audit bundle (question, answer, memory layers consulted, every outbound edge). Documented in `docs/reconstructibility-walkthrough.md` with real screenshots.

**Formal anchor:** the design framework is published as a SwissSign-signed PDF (`main_signed.pdf` in the repository root) — *"Sovereign Local AI: Why On-Device LLM Inference on Unified Memory Hardware Outperforms Commercial API Stacks for Regulated Industries"* (March 2026).

---

## Direct answers to your three questions

### 1. Hardware profile — specific, or hardware-agnostic at scale?

**Hardware-agnostic by construction, measurable on three reference profiles.**

The runtime is container-based end-to-end; the Helm chart makes no assumption about CPU architecture or accelerator vendor. The three inference endpoints (Qwen 3.6-35B for chat, nomic-embed v1.5 for embeddings, Mistral 7B for session summarisation) are swappable via a single environment variable per endpoint — the chart does not bind to any specific quantisation or backend.

Three reference deployment profiles I intend to validate empirically as part of Phase 1:

| Profile | Hardware | Primary use |
|---|---|---|
| **A — Consultant Z-book** | HP ZBook Ultra G1 with Ryzen AI MAX+ 395 (unified memory, ROCm for inference) — or equivalent on Apple Silicon / NVIDIA laptop | On-device sovereign AI for a single consultant. Full stack on the laptop; zero cloud dependency |
| **B — Enterprise on-prem node** | Single rack server, any Linux + k3s (or vanilla Kubernetes), GPU optional | On-premises shared deployment for a small team, tethered to corporate IdP |
| **C — Cloud/sovereign-cloud deployment** | Any Kubernetes distribution (EKS / AKS / OVH / Swiss hosting / on-prem) | Enterprise-scale with horizontal scaling |

The architectural commitment — that is, the contract we intend to hold — is: **the same Helm chart, the same ADRs, the same tests pass on all three profiles**. Where profile-specific optimisation is appropriate (accelerator selection, quantisation choice, retention policies), it lives behind a configuration flag, not a code fork. Any hardware-specific profile is a Helm values overlay, not a branch of the platform.

The Ryzen AI MAX+ is the profile I have personally validated for Profile A because it is the hardware I own. Nothing in the architecture requires it; everything in the architecture assumes it is one of many.

### 2. GDPR-compliant data residency — existing model, or gap to close?

**The single-device story is already privacy-by-design. The cross-device story — memory consolidation post-engagement — is the gap the roadmap is explicitly designed to close.**

Today, on Profile A (consultant Z-book), every datum of an engagement stays on the laptop by construction:

- The three inference endpoints run locally (llama-server on the host, no outbound network call per token).
- The four memory layers persist locally (PostgreSQL + ChromaDB + MinIO, all containerised on the laptop).
- The audit trail (Langfuse + Tempo + Loki) is local-first; no telemetry leaves the device unless explicitly configured.
- The user identifier on every row is the Keycloak `sub`, and RLS forces per-user scoping at the database layer regardless of how the service code is written.

This is the strong version of "data never leaves the jurisdiction" — data never leaves the device until an explicit, auditable sync event says so. Which brings us to the gap.

**The gap: cross-device memory consolidation.** What happens when the consultant ends the engagement and returns home? The knowledge accumulated — decisions, skills, sessions, semantic memory — should consolidate to the company server for three reasons: (a) continuity for the next engagement, (b) organisational learning, (c) GDPR right of access and erasure across the organisation, not just the device.

This is NOT yet implemented; it is Phase 2 of the roadmap below. The architectural shape I propose:

- **Outbound sync: a signed, queueable envelope.** Each memory artefact (interaction row, session, tool_call row, MinIO object) is packaged with its `user_id`, originating device id, timestamp, and a cryptographic signature bound to the consultant's Keycloak identity. The envelope is queued locally; the sync is idempotent and resumable.
- **Transport: authenticated queue + object store**. An authenticated queue (NATS JetStream or AWS SQS + KMS envelope, customer choice) for the row-shaped events; direct MinIO-to-MinIO replication for binary artefacts.
- **Conflict resolution: last-writer-wins on immutable rows, versioned on mutable rows**. Interaction rows are immutable (append-only); sessions are mutable but version-tagged. No cross-engagement merges silently overwrite history.
- **Consent ledger**: each sync operation records a consent event with the consultant's explicit opt-in per category (chats vs traces vs summaries). A GDPR right-of-erasure request triggers a reverse cascade: delete from company server, invalidate signed envelopes, purge local copies by envelope id.
- **Residency boundaries**: the sync protocol is jurisdiction-aware. A consultant working in Switzerland whose company server is in the EU syncs to an EU endpoint; a Belfast-based consultant to a UK endpoint; no cross-jurisdiction leak.

The governance surface sits on *this* protocol more than on the in-session platform, which is why this is the right conversation to have jointly. The in-session platform is conventional ZTA; the sync protocol is where your governance expertise and my architecture experience compound.

### 3. Platform, methodology, or combination?

**Both, by design.**

The *platform* is AuditTrace-AI as it stands today: deployable stack, Helm chart, observability, OAuth2 Device Flow, memory-as-tools, the full audit surface. A customer deploys it; their obligation is fulfilled at the infrastructure layer.

The *methodology* is the architectural framework expressed in the numbered ADRs — in particular:

- **ADR-025** — memory-as-tools: explicit per-turn retrieval, each an auditable event. Methodology-independent of the specific LLM or memory backend.
- **ADR-026** — multi-user identity with RLS as enforcement boundary, service-layer scoping as defence-in-depth. Transferable to any Postgres-backed system.
- **ADR-028** — observability aggregation as a regulatory-first rather than ops-first design (tempo + langfuse + loki + prometheus each chosen for a specific audit claim they enable).
- **ADR-037** — agent-tool audit boundary. The honest negative statement. Methodology-level contribution independent of the implementation.

A customer who cannot deploy the platform for whatever reason (licence incompatibility, existing vendor lock-in, vertical-specific compliance) can adopt the methodology alone and re-implement against their existing stack — the ADRs describe the *why*, not just the *what*. Equally, a customer who wants the platform can deploy it without committing to the methodology, and benefit from the reconstructibility story immediately.

I see these as complementary: the methodology is the argument, the platform is the proof that the argument runs. Both are legitimate products of the collaboration.

---

## Phased roadmap

Each phase has a measurable exit criterion — something an external reviewer could verify. The estimates are focused-effort weeks, from one architect (me) with access to your governance review feedback at each gate. Adding a second engineer could roughly halve calendar time without changing dependency shape.

### Phase 1 — Consultant Z-book deployment profile (4–6 weeks)

**Goal.** Validate the hardware-agnostic claim by shipping a Z-book / laptop deployment profile — Helm chart with laptop-resource values overlay, a single `make install-zbook` target, and a signed attestation of "what runs where" for a typical 8-hour engagement.

**Deliverables:**
- `charts/audittrace/values-zbook.yaml` — laptop-sized resource overlay (one replica of everything, memory-optimised, no HPA).
- `scripts/install-zbook.sh` — one-command install on a fresh Ubuntu / Fedora / macOS (k3d) laptop, from scratch.
- Validation matrix against three hardware profiles (Ryzen AI MAX+, Apple Silicon, NVIDIA laptop). CI to run Profile B in cloud; Profile A + C manually validated.
- Updated `docs/guides/zbook-deployment-runbook.md`.

**Exit criterion:** *A consultant unfamiliar with the project can install, authenticate, run a probe, and produce the reconstructibility bundle in under 30 minutes on a fresh laptop.*

**Dependencies:** None beyond today's codebase. Local work.

**Risks:** GPU driver variance across laptop hardware (ROCm vs CUDA vs Metal). Mitigation: CPU fallback profile that degrades gracefully.

### Phase 2 — Memory consolidation sync protocol (8–10 weeks)

**Goal.** Close the GDPR gap: specify, build, and validate the sync protocol that moves memory artefacts from the Z-book to the company server with end-to-end cryptographic integrity, explicit consent ledger, and jurisdiction-aware routing.

**Sub-phases:**

| 2.a | **Protocol specification** (2 weeks) | Signed envelope schema, idempotency keys, conflict-resolution semantics, consent-event shape. Published as ADR-038. |
| 2.b | **Queue transport** (2–3 weeks) | NATS JetStream reference implementation; pluggable to SQS/KMS or Azure Service Bus per customer preference. Stored in chart as `syncEngine: nats|sqs|azure`. |
| 2.c | **Memory-object replication** (2 weeks) | MinIO → MinIO mirroring with SSE-S3 re-encryption at the destination. |
| 2.d | **Consent ledger + GDPR RoE** (2 weeks) | Append-only ledger of consent events; right-of-erasure reverse cascade (company server purge → envelope invalidation → laptop purge). Surfaces as `/gdpr/erasure` API. |
| 2.e | **End-to-end acceptance test** (1 week) | Simulated 3-day engagement: consultant works on Z-book, syncs each evening, company server reflects state. Right-of-erasure request from day 1 cascades correctly. |

**Exit criterion:** *An external auditor can reconstruct a consultant's full engagement history from the company server alone, verify cryptographic integrity of every row against the consultant's Keycloak identity, and execute a GDPR Article 17 right-of-erasure that leaves zero residue on either side.*

**Dependencies:** Phase 1 complete (Z-book profile stable so the sync originates from a known source). Ivan's governance review of the consent-ledger shape is the key unblock at gate 2.d.

**Risks:** (i) GDPR-RoE semantics across an append-only substrate are legally subtle; need counsel review. (ii) Network-partition resilience of the sync — the laptop may be offline for days. Mitigation: local queue persists; sync is resumable from last acknowledged envelope id.

### Phase 3 — Enterprise IdP federation (4–6 weeks)

**Goal.** Remove the "customer must provision accounts in our Keycloak" friction. Keycloak federates to the customer's existing IdP (Google Workspace, Okta, Microsoft Entra ID, Swiss eID) via OIDC brokering.

**Deliverables:**
- `docs/ADR-039-external-idp-federation.md` — the brokering pattern.
- Keycloak realm templates for three IdPs (Google, Okta, Entra ID).
- Per-tenant realm-per-customer isolation at the IdP layer.
- `/admin/tenants` management API for onboarding a new customer without touching the chart.

**Exit criterion:** *A new enterprise customer is onboarded — realm provisioned, OIDC connection established to their IdP, first user authenticated via their corporate credentials — in under 1 hour from handoff.*

**Dependencies:** None on Phase 2 technically, but Phase 2 before Phase 3 makes sense sequentially: a customer's first question is "is our data safe", not "can I sign in with our SSO".

### Phase 4 — Horizontal scale + per-tenant isolation (8–10 weeks)

**Goal.** Move from "single-node k3s with one replica of everything" to "multi-node Kubernetes with per-tenant isolation beyond RLS". Addresses the "enterprise-defensible at scale" edge of your framing.

**Sub-phases:**

| 4.a | **Async persistence** | Interaction / tool_call writes move to an async queue; chat-path latency decoupled from DB write latency. |
| 4.b | **Postgres read replicas + logical replication** | Audit reads scale horizontally; writer remains single per tenant. |
| 4.c | **Schema-per-tenant isolation** | Beyond RLS: each tenant gets its own Postgres schema for audit tables. RLS continues to enforce per-user within the schema. |
| 4.d | **Per-tenant cost accounting** | Token usage and storage cost attributable to (tenant, user, project) tuples via Prometheus + a lightweight cost-model sidecar. |

**Exit criterion:** *A multi-tenant deployment with 10 synthetic tenants and 100 synthetic users per tenant sustains 1 000 requests/minute with audit reconstructibility preserved at 99th-percentile latency ≤ 2× single-user baseline.*

**Dependencies:** Phase 2 (for the consolidated-memory-at-scale story), Phase 3 (for tenant onboarding mechanics).

### Phase 5 — Research programme alignment (ongoing, in parallel)

**Goal.** Keep an academic layer on top of the commercial platform. Four candidate research directions detailed in `docs/phd/research-demonstrator-framing.md`; at least one is already in early discussion with the University of Liverpool. Research outputs reinforce the commercial positioning without distorting it.

Not on the critical path for the commercial roadmap, but included here because AI governance consultancies with an academic edge (yours, if I understand Otopoetic's model correctly) benefit structurally from a platform that has peer-reviewed backing.

The Liverpool contact is Dr Sven Schewe (Department of Computer Science); discussion on an MPhil / PhD framing is scheduled to resume late April 2026 following Liverpool's ongoing academic-hire process.

---

## Collaboration shape — where our interests might compound

Three areas where I see the collaboration being more than additive:

1. **Governance specification of Phase 2.** The sync protocol's consent ledger, RoE cascade, and jurisdiction routing are exactly the governance-layer questions Otopoetic sees repeatedly across clients. A co-authored specification of this protocol — grounded in the actual implementation rather than on theory — would be a high-signal artefact for both of us. I propose we treat the ADR-038 draft as a joint document, not a review-and-approve handoff.

2. **Audit framework alignment.** The ADRs describe architectural commitments; your firm's framework presumably includes a governance model for what good looks like. Mapping the ADRs onto your framework (NIST AI RMF, ISO/IEC 42001, EU AI Act Article-by-Article) would give both sides a reusable artefact. I would welcome your framework as input; the ADRs are designed to map onto external standards, not to supersede them.

3. **Reference deployment case.** If Otopoetic has a client engagement where this platform would fit, a supervised pilot would validate Phase 1 + Phase 2 against a real governance workload. The platform is open-source (AGPL v3 for the engine; IP retained for framework/ADRs); commercial support and deployment can be structured however fits your client's procurement.

---

## What I would like from the call

Three decisions before we spend further effort:

1. **Is Phase 2 the right next investment, or should Phase 1 be hardened first?** I lean Phase 1 first (the laptop profile is a week of polish); open to your view.
2. **Which of the three collaboration shapes above resonates?** They are not mutually exclusive, but initial focus matters.
3. **Is there a near-term Otopoetic client engagement where a supervised pilot would land cleanly?** Even a 4-week pilot would de-risk Phase 2 substantially.

Two operational questions for the call itself:

- The repository is public; would you like me to pre-share a sanitised pitch variant (`docs/pitch/reconstructibility-one-pager.md`) for any colleague who will join, or is the `docs/` tree sufficient?
- Available dates — I can do most of the second half of April across Central European daytime; happy to flex.

---

*Luis Filipe de Sousa · allaboutdata.eu*
*Solutions Architect · Aigle, Vaud · Based in Switzerland*
*MSc Big Data Analytics (Liverpool, 2022, Merit 64)*
*MIT MicroMasters in Supply Chain Management (in progress)*
