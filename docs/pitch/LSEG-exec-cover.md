# AuditTrace-AI — an audit-by-design alternative to commercial LLM API stacks

**For a head of Enterprise Architecture at a regulated firm. Reading time: 3 minutes. Full evidence in the companion walkthrough.**

---

## The problem

LLMs entered the enterprise faster than audit did. A production chat request today hits an opaque commercial endpoint, reads proprietary vector stores, invokes undocumented internal tools, and returns a response nobody can reconstruct. For regulated industries — financial services, health, public sector — this breaks **EU AI Act Article 12** (reconstructibility) and **GDPR Article 44** (data sovereignty) on contact. The vendors' answer is "trust us"; the regulator's is "prove it".

## The answer

**AuditTrace-AI** is an OpenAI-compatible proxy + 4-layer memory platform where every request is reconstructible from user identity down to the datastore row, end-to-end. Drop-in replacement for `https://api.openai.com/v1` — point any OpenAI SDK at `https://audittrace.local/v1` and the existing code keeps working. What the proxy adds, underneath:

- **Authenticated identity on every request** — Keycloak OAuth2 Device Flow (RFC 8628), JWT-validated with JWKS, `user_id` propagated to every span, log line, and database row.
- **Per-user isolation enforced at the database layer** — Postgres Row-Level Security with `FORCE ROW LEVEL SECURITY` and a non-superuser app role. A bug in the service code can't leak data across users; the database itself refuses.
- **Full audit trail across four memory layers** — episodic (ADRs on MinIO), procedural (skill files on MinIO), conversational (PostgreSQL sessions), semantic (ChromaDB vectors). Every memory tool invocation persists one row with its arguments, result summary, duration, and scope authorisation.
- **Observability triple** — Langfuse for LLM traces, Tempo for OpenTelemetry spans across every outbound edge, Loki for structured logs. Same `trace_id + user_id + session_id` triple propagates through all three.
- **Honest trust boundary** — agent-side tool execution (bash, read, edit, grep) is explicitly out of scope; formalised as ADR-037. Overclaiming would be worse than under-promising.

## The evidence

Running on a single-host k3s cluster with Istio STRICT mTLS and SPIFFE workload identity. Every claim above is demonstrated with real commands in the companion walkthrough — not diagrams of what a deployment could look like, but captures from a deployment that is running as this document is written. The 60-second operator drill at the end walks any engineer through an end-to-end reconstructibility of a single user request in four API calls.

Independent research; no employer IP. Framework documented in the signed paper *"Sovereign Local AI: Why On-Device LLM Inference on Unified Memory Hardware Outperforms Commercial API Stacks for Regulated Industries"* (March 2026), distributed as SwissSign-signed PDF. Code AGPL-v3; architecture IP retained.

## The ask

I'd value a 15-minute read of the walkthrough and a quick call to hear where the framing is weak for a regulated-financial audience. Your feedback shapes where this goes next — pilot, reference architecture, joint paper — and I'd rather get it right before it reaches procurement. If any part of it reads as over-engineered or under-measured, that's exactly what I need to hear.

**Companion document:** `docs/reconstructibility-walkthrough.md` in the same repo. Rendered with real screenshots from the running stack.

**Signed technical framework (PDF, SwissSign):** `main_signed.pdf` in the repo root.

**Repository:** `github.com/lfdesousa/AuditTrace-AI`

Luis Filipe de Sousa · allaboutdata.eu · Swiss-based solutions architect, 20+ years in financial services technology, MSc Big Data Analytics (Liverpool 2022), MIT MicroMasters in Supply Chain Management (in progress).
