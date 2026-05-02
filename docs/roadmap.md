# AuditTrace-AI roadmap — 2026-04-18 to 2026-10-31

This roadmap is deliberately public. It names what is shipped, what is next, what remains unproven, and when each of those stops being unproven. The project is young (first public commit 2026-04-12; `v1.0.0` tagged 2026-04-17), and the credibility of the claims below depends on the dates being met or honestly renegotiated — not on the absence of dates.

## The guiding principle — reconstructibility, not explainability

Feedback from practitioners in regulated industries has been consistent on one point: *"AI explainability statements"* — the kind produced as a marketing artefact to convince clients a system is understood — do not survive regulator scrutiny in finance or healthcare. What does survive is **reconstructibility**: the ability, for any model-generated output, to produce a complete, cryptographically anchored, per-user audit bundle on demand.

AuditTrace-AI is organised around hardening that reconstructibility contract. Every phase below either (a) extends the contract to a new surface (storage, identity, deployment profile), (b) proves the contract under adversarial or scale conditions, or (c) removes a known gap that would break the contract in production. Feature breadth is explicitly a secondary goal; it would be counter-productive to ship features faster than we can prove they preserve reconstructibility.

## Phase 0 — where we are today (2026-04-18)

**Shipped and verifiable on the current `main`:**

- Drop-in OpenAI-compatible `/v1/chat/completions` proxy, strict-superset response shapes, regression-locked against the upstream spec (ADR-024, ADR-033).
- Four-layer memory (episodic, procedural, conversational, semantic) exposed to the LLM via the *memory-as-tools* pattern (ADR-025). Every retrieval is an auditable tool call, not a hidden pre-prompt.
- OAuth2 Device Flow (RFC 8628) via Keycloak; JWKS-validated JWT on every request; per-user context propagated to every span, log, and database row (ADR-022, ADR-023, ADR-026, ADR-032).
- PostgreSQL Row-Level Security with `FORCE ROW LEVEL SECURITY` and a non-superuser application role. Separate minimum-privilege `audittrace_summariser` role for cross-user background jobs. RLS is the enforcement boundary; the service layer is defence-in-depth (ADR-026).
- k3s + Istio `STRICT` mTLS; SPIFFE workload identity; deny-all AuthorizationPolicy plus six per-flow allow rules; Helm chart as the single deployable unit.
- End-to-end observability: Langfuse for LLM traces, Tempo for OpenTelemetry spans on every outbound edge, Loki for structured logs, Prometheus for metrics. Same `user_id` + `session_id` + `trace_id` triple propagates across all four.
- Reconstructibility walkthrough with reproducible 60-second operator drill; 610 passing tests with a zero-skip CI policy; per-file coverage ≥ 90 %.
- Signed framework paper (`main_signed.pdf`) — SwissSign authorship and temporal non-repudiation.

**Known not-shipped and explicitly flagged:**

- Secret management uses Helm values / Kubernetes Secrets with plain strings. No HashiCorp Vault, SOPS, or sealed-secrets integration yet.
- Keycloak is self-hosted; no brokering to Google Workspace / Okta / Microsoft Entra ID yet.
- Single-writer PostgreSQL. No HA, no read replicas.
- Single-tenant model. RLS enforces per-user; schema-per-tenant isolation is not implemented.
- Deployment validated on one hardware profile only (AMD Ryzen AI MAX+, Linux host, ROCm). Apple Silicon and NVIDIA laptops are claimed hardware-agnostic but empirically unproven.
- No cross-device memory consolidation. A consultant working on a laptop cannot yet sync memory back to a company server.
- No formal verification of the reconstructibility predicate.
- Performance characterisation is a 100-probe smoke test (2026-04-17), not a statistically powered distribution study.

## Phase 1 — Production-security gaps closed (target 2026-05-16)

**Why this first.** The gaps named above that will be flagged by any security reviewer in a 90-second read of the deployment manifest. These are table stakes, not differentiators. Clearing them is the precondition for any external pilot conversation.

### 1.1 — HashiCorp Vault integration (ADR-040 candidate)
- Vault Agent Injector sidecar pattern for the memory-server pod.
- Migration path for existing installations using SOPS or External Secrets Operator as alternative back-ends, selected via Helm values.
- All passwords, JWKS-rotation keys, MinIO KMS key, and Langfuse credentials read from the vault at pod start.
- Target milestone: `make k8s-install` against a fresh cluster requires only the vault endpoint + a token; zero plain-string secrets in the Helm values.

### 1.2 — External IdP federation (ADR-044) ✅ Pattern shipped 2026-05-02
- ✅ Keycloak brokering configuration generalised for OIDC IdPs (Google Workspace, Okta, Microsoft Entra ID via the same `setup-idp-federation.sh` provisioner).
- ✅ Per-tenant realm template + tenant-onboarding contract (provisioner script as the contract; per-deployment IdP set lives outside the chart's baseline realm.json).
- ✅ A new enterprise user authenticates via their corporate IdP without any account provisioning in the AuditTrace Keycloak — proven 2026-05-02 against a real Google Workspace tenant (`@allaboutdata.eu`). Federated user landed as Keycloak shadow user via JIT; minted JWT validated end-to-end through `/v1/chat/completions`.
- ✅ Target milestone met: under 1 hour from a fresh repo clone is plausible — the live evidence run took ~1.5h including in-flight cluster networking recovery (kube-proxy iptables refresh) and the `audittrace-webui` PKCE-client gap fix; subsequent runs would skip both.
- ⏳ Microsoft Entra ID `oid`-mapper test still owed — backlog item filed.

### 1.3 — Postgres high availability
- Bitnami PostgreSQL chart with `architecture: replication` (one writer, two readers).
- Audit-read endpoints (`/interactions`, `/sessions`) routed to read replicas; writes remain on the primary.
- Target milestone: a primary-node failure-injection test (`kubectl delete pod`) does not lose any audit row and does not interrupt in-flight reads.

### 1.4 — Z-book deployment profile
- `charts/audittrace/values-zbook.yaml` — laptop-sized Helm overlay.
- `scripts/install-zbook.sh` — one-command install on fresh Linux / macOS (k3d) / Windows (WSL2 + k3d).
- Validation matrix across three hardware profiles: AMD Ryzen AI MAX+, Apple Silicon (M-series), NVIDIA laptop. CPU fallback profile where an accelerator is absent.
- Target milestone: a consultant unfamiliar with the project installs, authenticates, runs a probe, and produces the reconstructibility bundle in under 30 minutes on a fresh laptop.

## Phase 2 — Transition architecture: cross-device memory consolidation (target 2026-07-11)

**Why this next.** The "runs on my machine" story becomes the "consultant-ports-knowledge-across-engagements" story only when memory can leave the device safely. This is the governance and scalability frontier and — based on external feedback — the real value proposition for enterprise deployments.

### 2.1 — ADR-038 sync protocol specification (target 2026-05-30, joint authorship confirmed)

The ADR number is reserved with an empty shell at `docs/ADR-038-memory-sync-protocol.md`; substantive content will be drafted jointly with an external governance reviewer (Otopoetic, confirmed 2026-04-18) rather than written unilaterally. Co-authorship rather than review-and-approve is the deliberate shape.

- Signed envelope schema (row-shaped events for interactions / sessions / tool_calls; MinIO-to-MinIO replication for binary artefacts).
- Idempotency keys, resumable-from-last-ack semantics, conflict resolution for mutable sessions vs immutable interactions.
- Explicit consent ledger with per-category opt-in (chats vs traces vs summaries).
- GDPR Article 17 right-of-erasure as a reverse cascade: company server purge → envelope invalidation → device purge by envelope id.
- Jurisdiction-aware routing: a Swiss consultant with an EU company server routes via an EU endpoint; no cross-jurisdiction leak.
- Target milestone: ADR-038 accepted, co-authored with at least one external governance reviewer for independent audit of the protocol's privacy semantics.

### 2.2 — Reference implementation (target 2026-07-11)
- Queue transport: NATS JetStream as default; pluggable to SQS+KMS or Azure Service Bus via Helm values.
- MinIO-to-MinIO replication with SSE-S3 re-encryption at the destination.
- Consent-ledger service + `/gdpr/erasure` API implementing the reverse cascade.
- Target milestone: a 3-day simulated engagement with consultant-laptop working offline, evening sync to a company server, day-3 GDPR-RoE request that leaves zero residue on either side.

## Phase 3 — Multi-tenant enterprise deployment (target 2026-09-12)

**Why this next.** Phases 1 and 2 serve the single-tenant and cross-device cases. Phase 3 stretches the architecture to the shared-enterprise case.

### 3.1 — Async persistence for interactions and tool_calls
- Chat-path latency decoupled from DB write latency via an optional async queue.
- Durability guarantees maintained via timeout fallback to sync write — the audit row must land, non-negotiable.
- Target milestone: at 100 req/s sustained load, p99 chat latency < 1.5× the sync-persistence baseline, with zero dropped audit rows.

### 3.2 — Schema-per-tenant isolation
- Each Keycloak realm = one PostgreSQL schema; RLS continues to enforce per-user within the schema.
- Tenant-onboarding API (`/admin/tenants`) provisions realm + schema + retention policy as an atomic operation.
- Target milestone: 10 synthetic tenants with 100 synthetic users each coexist on one deployment; cross-tenant leakage testing returns zero incidents.

### 3.3 — Per-tenant cost accounting
- Token usage and storage cost attributable to `(tenant, user, project)` tuples, exposed via Prometheus + a lightweight cost-model sidecar.
- Target milestone: a monthly cost-allocation report for 10 synthetic tenants reconciles to within 1 % of direct Postgres / Langfuse storage measurement.

## Phase 4 — Scale + adversarial characterisation (target 2026-10-31)

**Why this next.** Claims of "enterprise-defensible at scale" require evidence at scale. This phase converts anecdote into data.

### 4.1 — N ≥ 10 000 evaluation
- Extended `scripts/eval-memory-modes.py` with concurrency, synthetic-user population, percentile tracking.
- Head-to-head memory-mode comparison (`inject` vs `tools`) across a controlled prompt set.
- Target milestone: published `docs/eval-memory-modes-n10000.md` with measured distributions, error budgets, and the precise conditions under which each mode outperforms the other.

### 4.2 — Adversarial reconstructibility study
- Threat model: a malicious user, a compromised agent client, a curious insider.
- Experimental questions: can the actor degrade reconstructibility without triggering a detectable signal? What failure modes does the audit trail itself have?
- Target milestone: published `docs/eval-adversarial-reconstructibility.md` with named attack vectors, observed system response, and recommended mitigations. Negative results published as rigorously as positive ones.

## Research programme (parallel track, not on critical path)

Coordinated with the University of Liverpool Department of Computer Science (initial contact 2023, active discussion 2026-04). Programme resumes in late April 2026 following Liverpool's internal academic-hire process.

Four candidate directions detailed in `docs/phd/research-demonstrator-framing.md`. Direction 1 — **formal specification of reconstructibility in a PRISM-compatible temporal logic** — is the strongest fit for Liverpool's formal-methods tradition and is the likely academic anchor.

## Honest risks and renegotiation policy

What could cause dates to slip:

- **Phase 1.1 Vault integration** — Vault Agent Injector has deployment-environment variance (Kubernetes version, CNI, security policies) that can consume weeks. Mitigation: ship External Secrets Operator as the simpler alternative first, Vault second.
- **Phase 2.1 sync protocol specification** — GDPR-RoE semantics over an append-only substrate are legally subtle; the protocol may need counsel review. Expected slippage envelope: up to 2 weeks on 2.1, cascading to 2.2.
- **Phase 3.2 multi-tenant isolation** — schema-per-tenant sounds simple on a whiteboard; in practice, Alembic migration across N schemas plus per-schema role grants is a non-trivial tooling problem.
- **Phase 4 evaluations** — hardware bottlenecks. Qwen 3.6-35B at concurrency N requires GPU memory beyond a single laptop; the eval may require a rented GPU server, which introduces cost and cloud-sovereignty trade-offs worth documenting.

**Renegotiation policy.** If a phase slips by more than 2 weeks against its target, the roadmap is updated on `main`, the cause is documented in this file's commit history, and the slippage is public. No silent moving of goalposts. The credibility of the dates above depends on that discipline.

## What collaborators are invited to provide

- **Regulated-industry readers** — technical review of the walkthrough + this roadmap; specifically, whether Phase 3 and Phase 4 align with what a Tier-1 firm's security function would require before pilot approval.
- **AI-governance practitioners** — joint authoring of ADR-038 (Phase 2.1); independent review of the consent-ledger and GDPR-RoE reverse-cascade semantics.
- **Academic reviewers** — research-programme shaping; peer review of the reconstructibility predicate specification (Research track).

Outreach is organised; relationships with these categories of collaborator are in flight as of this writing.

## Update cadence

This roadmap is reviewed at the close of each phase and after every external review. Minor updates (milestone detail refinement, ADR-number allocation) happen in place. Major scope changes (a phase added or removed, a target slipped by more than 2 weeks) ship as their own commit with a one-paragraph rationale in the commit message.

---

*Last reviewed: 2026-04-18. Next scheduled review: Phase 1.1 shipment (target 2026-04-25).*
