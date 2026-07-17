# ADR-058 — Recursive self-audit: the recorder records its own security review

**Status:** Proposed (gated on external review + build; see Tasks)
**Date:** 2026-06-27
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-029 (audit-trail completeness — the "every call chain fully
visible" invariant this extends to the recorder's own review), ADR-048
(ingestion + content-control — `interactions.event_class` precedent reused
here), ADR-049 (test/evidence/reconstructibility gate — the V&V discipline
this records as a first-class event), `main_signed.pdf` §7
(Sovereignty–Reconstructibility Gap — the academic anchor).

## Context

The recorder witnesses every model decision and every content-scan verdict and
makes them queryable, attributable, and reconstructable. But it does **not**
record the evidence of its own security review — the rules of engagement, the
framework mapping, the questions and verdicts, the findings, the deferrals, the
teardown. Post-publication review named this precisely: *if the recorder records
everything except the evidence that it was itself reviewed, the evidentiary chain
has a gap at its own foundation.* The recorder must become a **witness to its own
governability**.

This is not a logging convenience. It is the recursive case of the product's own
thesis ("queryable is not the same as auditable"): a recorder that applies a
weaker standard to its own records than to everything else has answered the
governance question by contradicting it.

## Decision

Record the security assessment **into the same audit pipeline, as first-class
queryable events, through the recorder's own front door** — an authenticated
request under a narrowly scoped credential, the identical discipline the
assessment itself demands. No new datastore, no new external dependency: the
recorder audits itself with the machinery it already uses to audit everything
else, which is exactly why it is credible.

1. **Taxonomy.** A new `event_class = "assessment"` (alongside `interaction` /
   `security`), with `source = "security-assessment"`, `project = "self-audit"`.
   Runtime security alerts do not page on assessments by default.
2. **Front-door ingest.** A new `POST /audit/assessments`, authenticated, gated by
   a dedicated least-privilege scope `audittrace:assessment:ingest` — distinct
   from the broad read scope. Write and read are **separate grants**.
3. **Shape.** One assessment fans out to **1 header row + N child rows**
   (`assessment_question` / `assessment_finding` / `assessment_deferral`),
   correlated by an `assessment_id` and each child by a stable `finding_id`.
4. **Ownership.** Rows are **owner-scoped** under the existing row-level security
   (RLS): the assessor owns them and reads them back under their own identity; a
   different operator sees nothing. The recorder's tenant-isolation property is
   turned **inward** onto its own review.
5. **Reconstruction.** `GET /interactions` gains an `event_class` filter, so an
   independent reader pulls a whole self-review in one query
   (`event_class=assessment & assessment_id=…`) and follows each row to its
   `trace_id`.
6. **Reuse, not rebuild.** The capability touches only the audit API and the audit
   store; it reuses the identity layer and the tracing already in place. Low-risk
   by construction — the right posture for the component everything else trusts.

### Refinements adopted from external review (fold in before build)

- **Contemporaneity.** A client-supplied timestamp proves nothing. The span time
  (`trace_id`) is already an off-client anchor; add a **server-set `created_at`**
  written by the store at insert time so the row's clock is independent of the
  writer. (Residual: absolute contemporaneity needs an external time authority —
  named, not hidden.)
- **Append-only by construction.** Today's RLS is tenant isolation (`FOR ALL`) and
  does **not** prevent the writing credential from amending its own rows. The
  assessment role is granted **INSERT only** (no UPDATE/DELETE); a **hash-chain**
  (each row hashes its predecessor) makes mutation by a higher-privileged actor
  *detectable* past that line.
- **Record what, not only that.** Capture the **raw test artefacts** as child
  evidence — request (method, path, scoped-token claims, body hash) and response
  (status, body hash / redacted) — so a reader can **re-derive** the verdict.
  Full payloads in an S3-compatible object store (in-cluster MinIO / AWS S3 per
  ADR-006), referenced by hash; redacted + hashed in the audit store.
- **Credential chain of trust.** State honestly that the chain terminates at the
  operator today (self-administered IdP) → *builder-repeatable*, not yet
  third-party. The off-operator termination is the independence track (see Tasks).
- **Cadence.** Bring the testing *schedule* into the trail (due marker, overdue
  check, material-change trigger), not just each point-in-time test.

## ISO/IEC 42001:2023 & EU AI Act mapping

This is a **mapping to clauses + intent, not a claim of conformance or
certification** (the gap assessment is in progress; the goal is to show the design
is aimed at an externally recognised boundary, not a bespoke one). Verbatim
normative text is not reproduced — control identifiers and clause numbers only.

| Decision element | ISO/IEC 42001:2023 | EU AI Act |
|---|---|---|
| Recording the self-review as first-class event logs | **A.6.2.8** (recording of event logs); **9.2** (internal audit) | **Art 12** (record-keeping) |
| Re-deriving verdicts from raw artefacts; V&V as a recorded event | **A.6.2.4** (verification & validation) | Art 12 |
| Contemporaneity (server-set time + span anchor) | A.6.2.8; **9.1** (monitoring & measurement) | **Art 12** (automatic, contemporaneous logging) |
| Append-only + integrity (INSERT-only grant, hash-chain) | A.6.2.8; **7.5.3** (control of documented information) | **Art 19** (logs kept under provider control) |
| Separate write/read grants; owner-scoped; role separation | **5.3** (roles, responsibilities, authorities); **A.3.2** (AI roles) | — |
| Independence — auditor objectivity (off-operator chain) | **9.2** (audit programme; auditors selected to ensure objectivity & impartiality) | — |
| Cadence governance (schedule, overdue, change-trigger) | **9.2** (programme incl. frequency); **9.3** (management review) | — |

The gaps a 42001 gap assessment surfaces cluster at exactly these management-system
clauses (9.2 / 9.3 / 5.3 / A.3.2); the strengths cluster at the life-cycle controls
(A.6.2.8 event logging — effectively the product — and A.6.2.4 V&V). #328 + the
independence track are the concrete treatment for the clauses the standard itself
flags as needing independence.

## Consequences

**Positive.** The recorder closes its own recursive witness gap with its existing,
already-governed machinery; the self-review is owner-scoped, reconstructable, and
trace-linked; the change is additive and low-risk (no new datastore, no change to
the OpenAI `/v1/chat/completions` surface).

**Negative / accepted.** Editing the pinned `event_class` closed set is a
deliberate, reviewed change (its test exists to force this conversation). The
`assessment_id`-in-`session_id` overloading is a convenience that must be
documented so it is not mistaken for a chat session.

**Scope boundary — explicitly deferred:**
- **Independence / third-party reproducibility** — recording + reconstructing the
  self-review is necessary but is *not* proof a non-builder can follow the
  procedure and reach a comparable result. Separate track.
- **Decision-layer / steerable-model governance** — out of scope here; its own ADR.

## Tasks

| # | Status | Task |
|---|---|---|
| **#331** | in progress | External review call (gates moving this ADR Proposed → Accepted) |
| **#328** | pending | Build the recursive self-audit per this ADR (fold in the four refinements: contemporaneity / append-only / `finding_id` / raw artefacts) |
| **#338** | pending | Assessment cadence governance — schedule, overdue check, material-change trigger (ISO 9.2/9.3) |
| **#337** | pending | Exaggeration-resistance — claim-vs-record reconciliation (raw artefacts make overstatement detectable) |
| **#329** | pending | Independence — third-party-reproducible re-run packet; off-operator credential chain (ISO 9.2 objectivity) |
| **#330** | pending | Decision-layer governance design (deferred sibling) |
| **#332** | in progress | ISO/IEC 42001 gap assessment — feed this mapping into the Statement of Applicability rows |

> **Implementation note:** the detailed design spec, the architecture diagrams
> (C4 L1/L2 + sequence), and the assessment content live in the private working
> set, per the project's public/private documentation split. This ADR is the
> public, terse decision record distilled from them.
