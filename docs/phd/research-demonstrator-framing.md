# AuditTrace-AI as a PhD research demonstrator — framing note for Dr Sven Schewe

**Purpose.** This note sits between the formal paper and the running code. It names the research problem, proposes the evaluation protocol, places the contribution in the literature, and points to the implementation artefacts that demonstrate the framework is measurable today.

**Artefacts in this repository, for a reviewer:**
1. **`main_signed.pdf`** — *"Sovereign Local AI: Why On-Device LLM Inference on Unified Memory Hardware Outperforms Commercial API Stacks for Regulated Industries"* (March 2026). SwissSign-signed for authorship non-repudiation and temporal anchoring.
2. **`docs/reconstructibility-walkthrough.md`** — end-to-end implementation walkthrough with real trace_ids, real screenshots, and a 60-second reproducibility drill against the running k3s cluster.
3. **`src/audittrace/`, `charts/audittrace/`** — the source tree: FastAPI proxy, 4-layer memory architecture, Istio ZTA Helm chart, 610 passing tests with a zero-skip policy enforced in CI.
4. **This note** — the research framing that connects the three.

---

## Research problem — the Sovereignty-Reconstructibility Gap

**Statement.** In regulated industries, production LLM deployments face a dual constraint that commercial API stacks cannot simultaneously satisfy:

- **Sovereignty constraint** (GDPR Article 44 + emerging jurisdiction-specific AI regulation): data must not leave the controller's infrastructure; model inference, memory, and audit artefacts must all be reconstructible under national jurisdiction.
- **Reconstructibility constraint** (EU AI Act Article 12, analogous instruments in UK AI SCM, US NIST AI RMF): for any model-generated output, a competent authority must be able to reconstruct *who asked, what was retrieved, what the model answered, and which artefacts informed the answer* — at the granularity of an individual user on a specific date.

The gap: commercial stacks are opaque at every layer the regulation requires to be transparent. The existing literature treats sovereignty (federated learning, on-device inference) and reconstructibility (observability, audit logging) as independent problems. The research contribution of this work is the observation that **they are the same problem under two names** — and that satisfying both simultaneously is an architectural problem with a single shape, not two problems with compatible solutions.

## Novel contribution

An architectural framework and implementation demonstrating that **sovereignty + reconstructibility is achievable as an OpenAI-compatible drop-in** — preserving the existing SDK ecosystem (openai-python, LangChain, OpenCode, Continue, Cursor) while enforcing the regulatory constraints underneath.

The key architectural insight: memory-as-tools (cf. ADR-025) reframes retrieval-augmented generation from a pre-prompt injection into a per-turn LLM-driven tool-call loop. Every memory-layer access becomes an explicit, auditable event with its own span, its own row, its own scope authorisation — not a hidden pre-prompt that the auditor cannot dissect. This reduces reconstructibility to an observability problem (well-solved) rather than a compliance-theatre problem (unsolved).

## Evaluation protocol (proposed)

A framework is evaluable; this one is. I propose three measurable criteria, each operationalised against the running demonstrator:

1. **Reconstructibility completeness.** For a population of N user requests, what fraction can be reconstructed end-to-end across the audit systems (Postgres `interactions` + `tool_calls`, Langfuse traces, Tempo spans, Loki logs) from a single starting identifier (`user_id`, `session_id`, or `trace_id`)? Current demonstrator: 100 % for N=100 (a hand-reviewed subset); machine verification script in the repo's 60-second operator drill.
2. **Reconstructibility latency.** Given a regulatory subpoena naming a `user_id` and a date, how long to produce the complete audit bundle (question + answer + memory layers consulted + failure classification)? Current demonstrator: single-digit seconds at the REST API level (`GET /interactions?user_id=...&since=...` + `GET /sessions?...`); full cross-system reconstruction via the 4-hop script (*see* `docs/reconstructibility-walkthrough.md § 60-second operator drill*)*.
3. **Trust-boundary honesty.** What fraction of a user's activity is claimed as audited vs what is actually audited? The framework explicitly documents the negative (ADR-037): agent-side tool execution (bash, read, edit, grep) is out of scope because the memory-server does not execute it. An evaluation dimension most implementations obscure.

A future experimental protocol — useful for an empirical chapter — would vary the memory-mode (`inject` vs `tools`) across a controlled prompt set and measure reconstructibility + latency + storage cost per mode. The N=100 eval from 2026-04-17 (`docs/eval-memory-modes-*.md`) is the seed of this experimental apparatus.

## Relationship to prior work

**My own (Liverpool, 2022):** MSc *Implementing an Enhanced Monitoring System in the Cloud for Distributed Microservices Based Applications*, Merit 64. ML-based anomaly detection on cloud-deployed microservices, validated on a physical Raspberry Pi cluster. The dissertation's examiner-noted limitation was "insufficient generated data for model evaluation" — a research gap. AuditTrace-AI closes that gap: it produces real, signed, reproducible audit artefacts from a running stack. The narrative arc is **Pi cluster testbed (2022) → allaboutdata.eu production AWS data-lakehouse (2024) → AuditTrace-AI reconstructibility framework (2026)**; same architectural instinct, four years of depth.

**Observability tradition (external):** Dapper (Sigelman et al., 2010) for distributed tracing as a causal reconstruction primitive; the OpenTelemetry specification (since 2019) as the vendor-neutral instrumentation substrate. The framework leans heavily on both: every cross-system link in the reconstructibility walk rides on a `trace_id` that OTel propagates across process boundaries.

**AI audit tradition:** NIST AI RMF (2023) and ISO/IEC 42001 (2024) as the "what should be auditable" standards; EU AI Act Article 12 as the legal instrument that turns those into obligation. Existing compliance-framework work is largely descriptive (what artefacts must exist); this contribution is prescriptive (how to produce them in a specific deployable architecture).

**Sovereignty tradition:** federated learning (McMahan et al., 2017 onwards) as the inference-side answer; data-residency primitives (Kubernetes zones, sealed-secret vaults) as the storage-side answer. This framework treats sovereignty as a composition constraint over both, not a new primitive.

## Methodological posture

The demonstrator is built to a **falsifiability** standard, not a "look at my prototype" standard. Specifically:

- Every claim in the walkthrough is backed by a live command a reviewer can run (commands are in the repository, not redacted).
- Every architectural decision is recorded in a numbered ADR with its trade-offs, alternatives rejected, and consequences explicit — the record is append-only and reviewable.
- Tests enforce the invariants the framework claims (per-user RLS isolation, zero-skip CI, reconstructibility surface tests). A future regression that violates the claims breaks CI.
- The formal paper is SwissSign-signed (authorship + temporal non-repudiation) — the research priority claim is itself reconstructible.

**What this research is NOT.** Not a formal-methods verification of the audit pipeline. Not a statistically powered human-factors evaluation (N=100 is a smoke test, not a study). Not a market-adoption analysis. These are all viable PhD-adjacent directions but are deliberately scoped out of the current demonstrator. Naming them explicitly is part of the methodological posture.

## Candidate PhD research programme

Four directions in which the demonstrator could drive a multi-year programme; any two could constitute a defensible thesis:

1. **Formal specification of reconstructibility.** The Sovereignty-Reconstructibility Gap is informally defined in the paper. A PhD contribution would be a formal specification (ideally in a temporal logic a tool like PRISM can model-check) of the reconstructibility predicate, plus automated verification that a given audit pipeline satisfies it.
2. **Self-adaptive memory-mode routing.** ADR-031 in the repository scopes per-request routing between `inject` and `tools` modes based on prompt shape. A PhD contribution would be a self-adaptive controller (PMC-style) that learns the routing policy online, with a formal QoS guarantee.
3. **Empirical reconstructibility at scale.** The N=100 eval is a start. A PhD-grade study would vary workload (N=10⁴+), user population (multi-tenant), and failure class (injection attacks, partial-failure scenarios) to characterise the reconstructibility envelope under adversarial conditions.
4. **Audit-trail adversarial robustness.** Can a bad actor (malicious user, compromised agent client, curious insider) *degrade* reconstructibility without triggering any detectable signal? A PhD chapter on this would combine threat modelling with empirical red-teaming.

I would welcome Dr Schewe's guidance on which of these directions aligns best with Liverpool's current research strength, and on how the existing implementation could be shaped to maximise academic leverage.

## Suggested reading order for a reviewer

1. **`main_signed.pdf`** — the formal paper. ~45 min read. Establishes the research problem and proposed framework.
2. **`docs/reconstructibility-walkthrough.md`** — the implementation evidence. ~15 min read including inspecting the embedded screenshots. Establishes that the framework is running.
3. **This document.** ~10 min read. Names the research framing and the candidate programme.
4. **Ad-libitum deep-dive into any of `docs/ADR-*`** — 24 numbered architectural decision records, each 1–3 pages, covering every architectural commitment.

Happy to follow this up with a video walkthrough of the running system if that would help.

---

*Luis Filipe de Sousa · allaboutdata.eu*
*Solutions Architect, 20+ years financial-services technology*
*MSc Big Data Analytics (Liverpool, 2022, Merit)*
*MIT MicroMasters in Supply Chain Management (in progress)*
*Based in Aigle, Vaud, Switzerland*
