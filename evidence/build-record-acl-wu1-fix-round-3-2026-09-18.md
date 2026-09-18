---
spec_ref: 'sdlc/specs (private, ~/work/audittrace-private/specs/): 2026-09-17-SPEC-sovereign-authorization-layer-acl-WU0-ratified-candidate.md
  (RATIFIED) + 2026-09-18-SPEC-ADDENDUM-I-ACL-operator-ratification-and-three-rulings.md
  (RATIFIED, wins on conflict). Memory-server key: decisions collection, filename-derived
  (feedback_spec_first_persist_before_build); re-confirmed present via recall this
  round (see recall_evidence).'
spec_hash: "sha256:e2204a4cbd44dd396e38789d62149204bdc5b76d749e8e830deec3b102d7b1aa\
  \ (WU-0 candidate) + sha256:c527cf835ac1fe046ec5482d26e20fa498fbcbb3156610a7375a5e5a2bbc3cc4\
  \ (ADDENDUM I) \u2014 both re-verified against the on-disk files this round via\
  \ sha256sum; unchanged from fix rounds 1 and 2 (specs are immutable, feedback_ratified_spec_immutable)."
recall_evidence: STEP-2 query 'sovereign ACL absent-scope enumeration substring-match
  error 8 vs 10 quoted array membership' via scripts.deploy.memory.recall_deploy_lessons(front_door=https://audittrace.local,
  limit=300) returned 300 lessons (recall limit=300 per feedback_recall_limit_counts_chunks_not_documents),
  confirming the memory server remains healthy and this round's dispatch (the orchestrator's
  F5 finding) is durably recorded server-side.
log_key: 0b0cdd4d-04c3-428f-ab9d-37b47429c381/episodic/build-record-acl-wu1-fix-round-3-2026-09-18.md
index_status: '{"collections": {"decisions": 9}, "duration_s": 0.29, "status": "indexed",
  "total_chunks": 9}'
branch: feat/sovereign-acl-read-path
commit: 06165897597d1b50a4d0af84118b2eb79b361531
gates: "make test: PASS (5650 passed, 0 failed, 0 skipped in 673.93s; zero-skip policy\
  \ enforced by scripts/check-no-skipped-tests.py per junit.xml). Per-file coverage\
  \ gate: PASS (157 files checked, lines >= 90% AND branches >= 90% on 133 file(s)\
  \ with branches \u2014 per register D22, PASS/FAIL cited, not file/line counts).\
  \ ruff check: clean (repo-wide, via make lint). ruff format --check: clean (406\
  \ files already formatted, repo-wide, via make lint). semgrep: 0 findings (2 rules,\
  \ 222 files scanned). mypy: clean on tests/test_chart_drift_guards.py (the only\
  \ file this round's diff touched). make helm-lint: not re-run this round \u2014\
  \ chart unchanged (docstring/build-record-only round; single alembic head b3f8a1c6d9e2\
  \ re-confirmed via `alembic heads`, no new migration). Integration gate: not run\
  \ this round \u2014 no product route or runtime behaviour changed (docstring correction\
  \ only, no test-behaviour change)."
---

# Build record — Sovereign ACL WU-1, fix round 3 (2026-09-18)

## Scope

Fix round 3 on the WU-1 read path, addressing a single finding (F5,
blocking) from the orchestrator's own re-check of fix round 2
(`2efd21a` + build record `7c6648c`), which the reviewer had otherwise
fully upheld ("the reviewer verified and passed everything else, and
said plainly it did not want to file a third finding"). This is the
ORCHESTRATOR's error, not the prior build's, but it propagated through
this branch's artefacts (the fix-round-2 build record and the class
docstring it produced) and is corrected here. **Docstring + new
build-record only — no test-behaviour change, no product code
change.**

## F5 — the absent-scope enumeration was 8, not 10; corrected

Fix round 2's build record (`evidence/build-record-acl-wu1-fix-
round-2-2026-09-18.md`, §F3) claimed:

> the entries genuinely absent from that provisioner — `audittrace:audit`,
> `audittrace:context`, `audittrace:index`, `audittrace:query`,
> `audittrace:scan:retrigger`, `memory:episodic:read`,
> `memory:procedural:read`, `memory:upload:write` — are exactly the
> core chat/query/audit scopes

**False on both counts.** Two more `ALL_SCOPES` entries are absent
from both provisioners' combined ensure-loop arrays —
`memory:semantic:read` and `memory:conversational:read-own` — and
neither is a core chat/query/audit scope, so the enumeration
undercounted AND the "are exactly" framing mischaracterized what it
missed.

**Root cause of the error (mine, the orchestrator's — stated plainly
so it is not repeated):** the round-2 verification used a substring
match against the raw provisioner-script TEXT rather than quoted array
membership. Both missed scopes are named in a `#` comment above
`MEMORY_SESSION_READ_SCOPES` on line 189 of
`scripts/setup-memory-scopes.sh` — a comment mentioning a scope is not
the scope being provisioned, and the substring match could not tell
the difference.

**Re-derived this round, from quoted array membership only — using the
guard's OWN code, never a text-substring check:**

```
from tests.test_chart_drift_guards import TestAllScopesRegisteredInControlPlane as T
from audittrace.auth import ALL_SCOPES
from pathlib import Path

script_union = T._ensure_loop_scope_union(
    Path("scripts/setup-memory-scopes.sh").read_text()
)
cm_union = T._ensure_loop_scope_union(
    Path("charts/audittrace/templates/keycloak/configmap-memory-scopes-script.yaml").read_text()
)
```

Result, captured live this round:

| Quantity | Value |
|---|---|
| `script_union` size | 32 |
| `cm_union` size | 32 |
| `script_union == cm_union` | `True` |
| `ALL_SCOPES` size | 42 |
| `ALL_SCOPES - (script_union \| cm_union)` (absent) | **10** |

The 10 absent entries: `audittrace:audit`, `audittrace:context`,
`audittrace:index`, `audittrace:query`, `audittrace:scan:retrigger`,
`memory:conversational:read-own`, `memory:episodic:read`,
`memory:procedural:read`, `memory:semantic:read`,
`memory:upload:write`.

This method uses `_ensure_loop_scope_union` (which itself calls
`_array_contents`, matching only `array_name=("...")` declarations and
extracting the double-quoted string literals inside) — the exact
mechanism the class's own `test_provisioner_ensure_loop_sets_match`
exercises. Confirmed the two extra entries are absent by grepping for
them directly: `grep -n "memory:conversational:read-own\|memory:semantic:read"
scripts/setup-memory-scopes.sh` returns exactly one hit, line 189, and
it is inside a `#`-prefixed comment line, not a `NAME=(...)` array
body.

## Split, stated honestly — not one homogeneous group

- **8 legitimately belong to no memory-scopes provisioner at all** —
  the core chat/query/audit scopes: `audittrace:audit`,
  `audittrace:context`, `audittrace:index`, `audittrace:query`,
  `audittrace:scan:retrigger`, `memory:episodic:read`,
  `memory:procedural:read`, `memory:upload:write`. Unchanged from
  round 2's list, minus the two mis-included entries below. Forcing
  these into `setup-memory-scopes.sh` to satisfy a strict subset check
  would misfile them into the wrong script.
- **2 are live candidates for the residual risk, NOT established as
  legitimately absent** — `memory:semantic:read` and
  `memory:conversational:read-own`. The provisioner script's own
  comment (line 189, above `MEMORY_SESSION_READ_SCOPES`) calls
  `memory:session:read-own` *"a READ-OWN scope, same family as
  memory:conversational:read-own/memory:semantic:read"* — and that
  sibling scope IS provisioned (`MEMORY_SESSION_READ_SCOPES`). The
  script's own comment treats the family as provisioner-owned; these
  two members of the family are simply missing from any array. This
  record names them as candidates, not as a proven defect (no test in
  this class requires provisioning either), but the "legitimately
  belongs nowhere" framing does not apply to them and the evidence
  points the other way.

## Fix applied (commit `0616589`, this branch)

1. `tests/test_chart_drift_guards.py::TestAllScopesRegisteredInControlPlane`
   — corrected the enumeration to 10, added the 8-vs-2 split with the
   line-189 family evidence above, and **de-future-tensed** the
   "Residual risk" paragraph: it now states the unclosed
   provisioner-parity mode has **two live instances on `main` today**
   (`memory:semantic:read`, `memory:conversational:read-own`), not
   merely a hypothetical future scope. Leg (3)
   (`test_provisioner_ensure_loop_sets_match`) is **not** made strict
   — only the claim about what is and is not "genuinely absent for
   legitimate reasons" is corrected, per decision-log D-B /
   D-B-CORRECTION (the reviewer explicitly did not ask for a strict
   `ALL_SCOPES` subset check, and this round did not either).
2. This build record (new) — carries the corrected enumeration, the
   re-derivation method, and an earned-fresh verification badge (see
   next section). The fix-round-2 build record already logged to the
   memory server (`log_key`
   `0b0cdd4d-04c3-428f-ab9d-37b47429c381/episodic/build-record-acl-
   wu1-fix-round-2-2026-09-18.md`) is left as-is — immutable artefact
   convention already used for specs and prior commits on this branch
   — this record is the correction, not a rewrite.

**No test assertions changed.** Re-ran
`TestAllScopesRegisteredInControlPlane`'s 4 sub-tests targeted
(`pytest tests/test_chart_drift_guards.py -k
TestAllScopesRegisteredInControlPlane`): 4 passed, unmodified
behaviour. Additionally compared the docstring-stripped AST of
`tests/test_chart_drift_guards.py` at the pre-round-3 commit (`7c6648c`)
vs this round's commit (`0616589`): **identical** — the docstring is
the only diff. Full `make test` (gates, above) confirms nothing else
regressed.

## Verification badge — earned fresh this round, not carried over

Fix round 2's build record carried a badge reading *"Verified
independently before applying the fix (not just taken on the
reviewer's or orchestrator's word)"* over an enumeration that turned
out to be wrong — the verification behind that badge reproduced the
orchestrator's CONCLUSION (read the provisioner file, look for the
named scopes as text), not a re-derivation from SOURCE (quoted array
literals). That is the general lesson the dispatching orchestrator
named this round: **a downstream "independent verification" that
re-derives an upstream ANSWER instead of re-deriving from SOURCE is
not verification.**

This round's badge is earned differently: the enumeration above was
produced by calling `TestAllScopesRegisteredInControlPlane`'s own
`_ensure_loop_scope_union` against both provisioner files directly —
the same code path the guard itself runs — and cross-checked with a
literal grep confirming the two previously-miscounted scopes appear
**only** inside a comment, never inside a `NAME=(...)` array body. Both
steps are reproduced above with their literal output, not narrated.
**Badge kept**, on this round's own re-derivation.

## Absolutes self-check

* No new unqualified "genuinely absent" / "are exactly" claim
  introduced without the split (8 legitimate / 2 candidates) spelled
  out alongside it.
* The corrected docstring's "Residual risk" paragraph names its two
  live instances explicitly rather than describing them only as a
  future possibility.
* Every count above (32/32/42/10) is this round's own live Python
  output, captured against the guard's own code, not copied from the
  round-2 record or from the dispatching orchestrator's finding text
  without re-derivation.

## Deviations from the dispatching finding

None. Re-derived from quoted array membership as required (leg 5 of
the dispatch), corrected all three named artefacts (docstring, this
build record's own enumeration, and the commit message via a quoted
retraction in a new commit rather than a rewrite of `2efd21a` or
`14711b3`), split honestly rather than asserting the two candidates
belong nowhere, de-future-tensed the residual-risk paragraph, and did
not make leg (3) strict.
