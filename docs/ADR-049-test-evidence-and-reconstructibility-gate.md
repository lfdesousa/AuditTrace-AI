# ADR-049 — Test, Evidence, and Reconstructibility Gate

**Status:** Accepted
**Date:** 2026-05-07
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-029 (audit trail completeness — the "every call chain
fully visible" invariant this ADR operationalises for the dev loop),
ADR-041 (product boundary and dependencies — defines what "the public
API surface" means for the validation layer below), ADR-046 (async
chat persistence — pattern precedent for "deploy + verify in cluster
before declaring shipped"), `main_signed.pdf` §7
(Sovereignty-Reconstructibility Gap — the academic anchor).

## Context

By the morning of 2026-05-07 this project had three written-down
rules about test+evidence:

1. `feedback_test_and_evidence` (2026-04-18) — every meaningful
   block modified needs a test AND captured live-system evidence
   before commit.
2. `feedback_no_more_drifts` (2026-05-03) — every chart/code
   change introducing chart↔cluster coupling needs an automated
   provisioner OR a drift-guard test.
3. `feedback_no_security_control_shortcuts` (2026-04-25) — never
   disable mTLS / AP / NetworkPolicy / RBAC to fix a connectivity
   error.

All three were available to the assistant in MEMORY.md every
session. None of them prevented the failure that prompted this
ADR. The PDF tier-A work that day produced 929 unit tests, deployed
a new image to the cluster, and was proposed for commit with the
explicit claim "Tier 3 complete" — but the actual feature
(`/memory/index` producing the new chunk metadata through the
production code path) was never exercised end-to-end. When the
gap was caught, the pivot attempt was to bypass the auth surface
via `kubectl exec` into MinIO with root credentials. Two further
memories had to be written *during* that recovery
(`feedback_evidence_self_check_before_commit`,
`feedback_no_unauthorized_testing_paths`) — both formalising rules
that, again, would not by themselves prevent recurrence.

The pattern is clear: **memory is necessary but not sufficient**.
Documentation alone does not enforce. Under deadline pressure the
soft guidance loses to the conventional engineering reflex of
"tests pass = ship." A regulated / sovereign / audit-grade system
cannot afford that reflex; the tests are what we use to convince
*ourselves* a feature works, but the evidence is what
demonstrates it to a third party who wasn't present.

This ADR makes the gate mechanically enforced.

## Decision

Adopt a four-rule **Test, Evidence, and Reconstructibility Gate**,
enforced by six layers (defense in depth) and overridable by no
mechanism.

### The four rules

1. **Verification** — Unit tests pass and per-file coverage is
   ≥ 90 %. Already enforced by `make test` +
   `scripts/check-per-file-coverage.py`. **No commit otherwise.**
2. **Validation** — Every meaningful change is exercised
   end-to-end through the **public audittrace API** with a
   **properly-scoped JWT**, against **a deployed image in the
   cluster**. No bypass paths. (`kubectl exec` into a backend pod
   to perform what the API should do, MinIO root credentials,
   scope-skipping, mocked authentication at the edge — all
   forbidden by `feedback_no_unauthorized_testing_paths`.)
3. **Reconstruction** — The artefacts a third party would need to
   reproduce the validation verdict (image tag deployed,
   `verify-deploy` summary, the API call's request + response,
   the trace ID, the audit-row ID, the ChromaDB / MinIO /
   Postgres query result that proves the side effect) are
   captured AND referenced from both the commit body and the
   PR body.
4. **No override.** Cluster down? Bring it up first. Missing
   scope on the operator's token? Re-login through Device Flow
   with the right scope. Dependency unavailable? Provision it.
   The right answer to friction is to remove the friction
   through the proper path, not bypass the gate. Hotfix?
   Hotfixes are still subject to all three above — the
   speed-up comes from having pre-prepared evidence templates,
   not from skipping evidence.

### Why these specific rules

The rule set maps cleanly onto the **V&V** discipline from the
software-assurance literature and onto the
**Sovereignty-Reconstructibility Gap** formalised in
`main_signed.pdf` §7:

| Rule | Discipline | Question answered | Required artefact |
|---|---|---|---|
| 1 — Unit + coverage | **Verification** | "Are we building the artefact right?" | pytest output (count + pass/fail), coverage XML showing ≥ 90 % per file |
| 2 — E2E through API + auth | **Validation** | "Are we building the right thing for the user?" | API call request + response, recorded against a deployed image, made with a JWT carrying the production-equivalent scope set |
| 3 — Captured artefacts | **Reconstruction** | "Can a third party prove from durable artefacts that this worked?" | Image tag, `verify-deploy` summary, trace ID, audit-row ID, side-effect query result |
| 4 — No override | **Discipline** | "Will the rule survive a hard week?" | The rule itself, as a binary gate that does not negotiate |

Rule 2's emphasis on the *public API* is not stylistic — it
follows from ADR-041's product boundary. The audittrace product
is its named dependencies and the documented API between them.
A test that exercises a function via `pytest` proves the function
runs; a test that exercises it via `POST /memory/index` with a
real JWT proves the *product* works. Only the latter validates
the boundary the customer / auditor / operator actually sees.

Rule 3 is the dev-loop embodiment of the
Sovereignty-Reconstructibility Gap. The paper's claim is that a
sovereign system's claims about itself must be reconstructible
from the artefacts the system retains. For our software process,
this means: every PR's claim "this code works" must be
reconstructible from its recorded evidence by a reviewer who was
not present when the work was done. ADR-029 already enforces
this for runtime audit trails (`user_id` + `session_id` +
`trace_id` + `interaction_id` + `response_id` on every audit
row). ADR-049 extends it from runtime to the development process
itself.

Rule 4 is the lesson of the 2026-05-07 incident. Every override
mechanism, however well-intentioned, becomes the de facto path
under deadline pressure. *No* override is the only stable
position.

### The six enforcement layers

The same rule, enforced redundantly, so a failure in any single
layer does not bypass the gate.

1. **This ADR** — the durable architectural record. Cited by all
   other layers.
2. **`CLAUDE.md` + `AGENTS.md` addenda** — surface the gate at
   the top of every contributor / agent session.
3. **`.github/pull_request_template.md`** — every PR opens with
   the three required sections (Verification / Validation /
   Reconstruction) pre-populated as empty checklists. Visible
   friction at PR-creation time.
4. **CI `evidence-check` job** — parses the merged PR body via
   `gh api`. Hard-fails the merge if any required section is
   missing or empty. Cannot be bypassed by a "force-merge" because
   branch protection requires the check.
5. **`scripts/check-commit-evidence.sh` pre-commit hook** —
   refuses local commits to `src/audittrace/routes/**` or
   `src/audittrace/services/**` that do not reference an evidence
   artefact (URL to PR draft body, evidence-file path, or trace ID
   matching `[a-f0-9]{32}`). Catches the failure at the earliest
   point — before the work even leaves the laptop.
6. **Global `TEST-EVIDENCE-GATE` skill** in
   `~/work/claude-config/skills/` — cross-project signpost so the
   principle is visible when working on any
   audited / regulated / production-Python service. Body is a
   thin pointer back to this ADR; this ADR is canonical.

Each layer is independently sufficient to catch *some* class of
failure. The combination catches the class that motivated the
ADR: an assistant or contributor proposing commit-shape questions
without having captured evidence.

## Consequences

### Positive

- The conventional engineering reflex ("tests pass → ship") is
  no longer a single point of failure.
- A reviewer reading any merged PR can reconstruct the live
  evidence of the change without spelunking through Slack /
  chat history / out-of-band notes.
- The dev loop's own audit trail matches the runtime audit trail
  (ADR-029) in completeness — symmetry between "what we say
  the system does" and "what we say the development process
  does."
- The rule scales with project complexity: as more services
  land, the gate keeps the validation surface honest. The
  alternative (drift between what tests assert and what
  production does) compounds with size.

### Negative

- Slows down every commit and every PR by the time required to
  capture evidence. Acceptable cost — the morning's incident
  ate hours that an upfront 5-minute evidence capture would have
  prevented.
- Pre-commit hook adds friction to genuine docs/refactor work
  that doesn't need evidence; mitigated by the path-skip
  heuristic (changes only to `docs/`, `tests/`, `*.md` skip the
  hook).
- Hard-fail in CI means cluster outages block merges. This is
  not a bug — the rule is "fix the cluster first." A regulated
  system cannot ship code whose effect on production state is
  unverified.

### Risks (and mitigations)

- **Risk:** the rule is honoured in letter not spirit — evidence
  sections filled with copy-pasted boilerplate that doesn't
  actually demonstrate the change. **Mitigation:** code review
  remains the line of defense. The PR template's "Reconstruction"
  section asks for *specific* artefact IDs that a reviewer can
  follow and verify.
- **Risk:** evidence drift — captured in the PR body but
  artefacts (Tempo traces, audit rows) age out before the PR is
  reviewed. **Mitigation:** retention windows on Tempo + Loki +
  Postgres are sized to outlast typical PR lifetimes. If a PR
  sits longer than retention, refresh the evidence on the next
  push.
- **Risk:** the gate becomes the work — contributors spending
  more time on evidence formatting than on code. **Mitigation:**
  evidence formats are templates, not free-form. Pasting four
  command outputs and four IDs satisfies the gate.

## Acceptance criteria

This ADR moves to **Accepted** at creation. It is considered
**fully implemented** when:

1. `docs/ADR-049-test-evidence-and-reconstructibility-gate.md`
   merged on `main`.
2. `CLAUDE.md` and `AGENTS.md` carry the gate addendum referencing
   this ADR.
3. `.github/pull_request_template.md` exists and pre-populates
   every new PR with the three required sections.
4. `evidence-check` job in `.github/workflows/ci.yml` triggers on
   every PR to `main` and parses the PR body for the three
   required sections; missing/empty sections fail the check.
5. `scripts/check-commit-evidence.sh` exists and is registered in
   `.pre-commit-config.yaml` under `repo: local`. The hook fails
   commits to `src/audittrace/routes/**` or
   `src/audittrace/services/**` whose commit message lacks an
   evidence reference.
6. `~/work/claude-config/skills/TEST-EVIDENCE-GATE/SKILL.md`
   exists as the global signpost referencing this ADR.
7. Negative-tests pass: a deliberately empty commit message on a
   route-touching change is refused by the hook; a PR with
   stripped-out evidence sections is failed by the CI job; a
   docs-only commit is allowed through both.

## Cross-references

- `feedback_test_and_evidence` — the original written rule. This
  ADR makes it enforceable.
- `feedback_evidence_self_check_before_commit` — the assistant-
  side self-check that complements the mechanical gate.
- `feedback_no_unauthorized_testing_paths` — the rule against
  bypass paths, particularly relevant to Rule 2 above.
- ADR-029 — runtime audit-trail completeness. ADR-049 is the
  dev-loop counterpart.
- ADR-041 — what "the public API" means for Rule 2.
- `main_signed.pdf` §7 — Sovereignty-Reconstructibility Gap, the
  framework Rule 3 derives from.
