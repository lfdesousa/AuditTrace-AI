# ADR-041: Product boundary — memory-server is the product; eight named dependencies are the market

- **Status:** Accepted
- **Date:** 2026-04-18
- **Context:** ADR-014 (Python package structure), ADR-018 (4-layer memory port), ADR-024 (proxy pass-through), ADR-025 (memory-as-tools), ADR-026 (multi-user identity), ADR-028 (observability aggregation), ADR-037 (agent-tool audit boundary)

## Context

The roadmap gathered through Phase 0 surfaces a classification that has been implicit in every architectural commitment to date but has never been written down. The project ships one thing — a memory-server — and consumes eight distinct pieces of market-standard infrastructure (IdP, PostgreSQL, Redis, object storage, vector DB, secret manager, LLM inference endpoint, observability stack). Whether we want that classification to be *public, committed, and enforced* is itself an architectural decision.

The drift risk is real: LLM-tooling projects in this space routinely blur the product-dependency boundary until the project is claiming to *be* an IdP, *be* an observability platform, *be* a vector database. At that point every claim in the project's audit story degrades in proportion to the number of claims it now has to uphold on infrastructure it neither built nor should be maintaining. The project's regulator-grade credibility depends on holding the boundary deliberately.

Two prior feedback signals from practitioners in regulated industries have validated this framing: (a) the distinction between marketing-grade *"AI explainability statement"* and regulator-grade *reconstructibility* as the separator of credible from performative implementations; (b) enterprise architects' expectation that a product integrates cleanly with the infrastructure they already run, rather than asking them to replace it. Both point to the same design discipline: narrow product scope, well-documented integration surfaces, honest dependency contracts.

## Decision

**AuditTrace-AI is a single product: the memory-server container image plus its Helm chart plus its numbered ADRs. It depends on eight named components, each consumed through a documented interface with a minimum security posture and a set of production alternatives.**

The eight dependencies are:

1. Identity Provider (OAuth2 / OIDC, JWKS)
2. PostgreSQL (RLS-capable, recommended HA)
3. Redis
4. Object Storage (S3 API)
5. Vector Database (similarity + filter-aware)
6. Secret Manager
7. LLM Inference Endpoint (OpenAI-compatible)
8. Observability Stack (OpenTelemetry + Tempo + Langfuse + Loki + Prometheus)

Each dependency's interface, default, alternatives, minimum posture, and current gap are documented in `docs/architecture/product-and-dependencies.md`. Swapping a default for an enterprise alternative is a Helm values change plus a documented integration guide — never a code fork.

The Helm chart supports three deployment profiles that differ only in which dependencies are bundled versus brought-your-own: **Profile A** (laptop, everything bundled), **Profile B** (single-host on-premises, mixed), **Profile C** (enterprise, all eight external). The same chart, the same ADRs, the same tests pass on all three.

## Non-goals (explicit scope limits)

We will not build, resell, maintain HA for, or otherwise assume product responsibility for:

- an Identity Provider,
- a relational database engine,
- a key-value cache,
- an object store,
- a vector database,
- a secret manager,
- an LLM inference server,
- an observability platform.

Each of these is a mature market segment with incumbent vendors and open-source leaders. Integration is our responsibility; operation is the enterprise's.

## Consequences

### For the pitch

The product surface now reads cleanly: *"one memory-server, eight well-understood dependencies, narrow scope by design".* This is the positioning that survives a head-of-Enterprise-Architecture read. It is the inverse of the platform-scope-creep pattern common in this space.

### For the Helm chart

The chart stays structured so that every dependency has:
- a `<dep>.enabled` toggle for bundled vs BYO,
- a connection-URL / endpoint / credential reference when BYO,
- a documented minimum-version matrix.

This already exists for PostgreSQL, Redis, ChromaDB (via existing subchart conditions), MinIO, Keycloak. It will be extended to the remaining dependencies in roadmap phases 1.1 (secret manager via Vault / ESO), 1.2 (IdP via Keycloak brokering), 1.3 (PostgreSQL HA), 1.4 (LLM inference on multiple hardware profiles).

### For the code

Interfaces stay thin. Memory-server code already depends only on:
- SQLAlchemy Engine + Session (any RLS-capable Postgres)
- redis-py Redis client
- boto3-compatible S3 client (via MinIO or AWS SDK)
- ChromaDB HTTP client — abstracted behind the `SemanticService` port (ADR-018), so a future pgvector or Qdrant port is a new implementation of the existing port, not a rewrite.
- httpx calls to an OpenAI-compatible endpoint
- OpenTelemetry SDK + Langfuse SDK for observability

No code coupling to any specific vendor instance. The product's code surface is dependency-shape-aware but vendor-instance-agnostic.

### For the audit story

Claims of reconstructibility, RLS enforcement, and trust-boundary clarity (ADR-037) are uniformly scoped to "what the memory-server does". Claims about the dependencies' internal correctness — Postgres actually enforces its RLS policies, Vault actually rotates its keys, Keycloak actually verifies JWT signatures — are the dependencies' responsibility, documented in their own certifications / attestations / audits.

This division of liability is precisely what regulated enterprises expect from a vendor-in-stack: we attest to our layer, each dependency attests to theirs, and the enterprise architect composes the total assurance.

### For future ADRs

Any subsequent architectural decision that touches a dependency must either (a) describe how the change preserves the integration contract, or (b) explicitly update the dependency's entry in `product-and-dependencies.md`. Adding a ninth dependency would require a new ADR that argues why the scope expansion is warranted; the default is to reject such proposals.

## Alternatives considered

**Alternative 1: Platform-mode shipment.** Ship AuditTrace-AI as an all-in-one platform that includes managed versions of every dependency. *Rejected* — increases operational surface beyond what one architect can credibly maintain; dilutes audit story; competes with incumbent infrastructure vendors who are better at their own components; violates Luis's explicit framing (2026-04-18).

**Alternative 2: Product-plus-packs model.** Core memory-server + separately maintained integration "packs" for each major alternative (Vault pack, Okta pack, Triton pack, etc.). *Deferred, not rejected* — a reasonable future shape once adoption justifies the maintenance burden, but premature today with a single architect and no external deployments.

**Alternative 3: Reference-deployment repository.** Keep the memory-server chart minimal (BYO everything) and ship a separate `audittrace-reference-deployment` repository that bundles specific Bitnami charts for dev / pilot. *Also deferred* — conceptually clean but fragments the newcomer-experience story; keeping it all in one chart with profile-based overlays is simpler today.

## Cross-references

- **`docs/architecture/product-and-dependencies.md`** — the long-form narrative with per-dependency contracts and deployment profiles. This ADR is the formal decision; that file is the reference implementation of it.
- **`docs/roadmap.md`** — dated closure of the current gaps (Phases 1.1–1.4 target 2026-05-16).
- **ADR-018** — 4-layer memory port, which is the code-level reason the vector DB dependency can be swapped.
- **ADR-024** — chat proxy pass-through, which is the reason the LLM inference dependency can be swapped.
- **ADR-026** — multi-user identity and RLS posture, which the PostgreSQL dependency must support.
- **ADR-037** — agent-tool audit boundary; this ADR is the infrastructure-layer counterpart to ADR-037's agent-layer boundary. Both are honest-scoping commitments.
