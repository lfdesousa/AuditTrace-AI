---
spec_ref: "sdlc/specs (private, ~/work/audittrace-private/specs/): 2026-09-17-SPEC-sovereign-authorization-layer-acl-WU0-ratified-candidate.md (RATIFIED) + 2026-09-18-SPEC-ADDENDUM-I-ACL-operator-ratification-and-three-rulings.md (RATIFIED, wins on conflict). Memory-server key: decisions collection, filename-derived (feedback_spec_first_persist_before_build); re-confirmed present via recall this round (see recall_evidence)."
spec_hash: "sha256:e2204a4cbd44dd396e38789d62149204bdc5b76d749e8e830deec3b102d7b1aa (WU-0 candidate) + sha256:c527cf835ac1fe046ec5482d26e20fa498fbcbb3156610a7375a5e5a2bbc3cc4 (ADDENDUM I) — both re-verified against the on-disk files this round via sha256sum; unchanged from fix round 1 (specs are immutable, feedback_ratified_spec_immutable)."
recall_evidence: "STEP-2 query 'sovereign ACL provisioner axis leg-3 parity-only claim correction docstring retraction' via scripts.deploy.memory.recall_deploy_lessons(front_door=https://audittrace.local, limit=300) returned 300 lessons (recall limit=300 per feedback_recall_limit_counts_chunks_not_documents), including this round's own governing artefact (review-verdict-acl-wu1-fixround1-20260918.outcome-reject.md) and the orchestrator decisions record (2026-09-18-ORCHESTRATOR-DECISIONS-mongo-elim-and-acl-arc.md) — confirming the memory server is healthy again post the round-1 credential outage and that this round's dispatch is durably recorded server-side, not just in-conversation."
log_key: "0b0cdd4d-04c3-428f-ab9d-37b47429c381/episodic/build-record-acl-wu1-fix-round-2-2026-09-18.md"
index_status: "{\"status\": \"indexed\", \"collections\": {\"decisions\": 11}, \"total_chunks\": 11, \"duration_s\": 0.37}"
branch: "feat/sovereign-acl-read-path"
commit: "2efd21a0735fb39d61abd798c1984bf90b43e2a9"
gates: "make test: PASS (5650 passed, 0 failed, 0 skipped in 628.25s; zero-skip policy enforced by scripts/check per junit.xml). Per-file coverage gate: PASS (155 files checked, lines >= 90% AND branches >= 90% on 131 file(s) with branches — per register D22, PASS/FAIL cited, not file/line counts). ruff check: clean (repo-wide). ruff format --check: clean on the one touched file (tests/test_chart_drift_guards.py); the same 15 pre-existing docs/evidence .md files flagged by a repo-wide format check as fix-round-1's build record (unrelated, out of scope, verified none is a file this round touched). mypy: clean on tests/test_chart_drift_guards.py and src/audittrace/services/console_acl/_postgres.py (the two files this round's diff + neuter-reproduction touched). make lint: PASS (semgrep 0 findings, ruff clean, formatter clean). make helm-lint: not re-run this round — chart unchanged (docstring/prose-only round; single alembic head b3f8a1c6d9e2 re-confirmed via `alembic heads`, no new migration). Integration gate: not run this round — no product route or runtime behaviour changed (prose/docstring correction + reproduced-then-restored neuter probes only)."
---

# Build record — Sovereign ACL WU-1, fix round 2 (2026-09-18)

## Scope

Fix round 2 on the WU-1 read path, addressing the independent
reviewer's re-verification of fix round 1 (`14711b3` + build record
`6a3d6c4`). The reviewer's own words: *"No code hole was found. F1
and F2 are genuinely fixed and every guard I attacked held."* This
round is **prose-and-docstring only** — two claim corrections (F3,
F4), no test-behaviour change, no product code change. The
orchestrator dispatched this round with the F3 scope decision already
made (claim-correction route, not guard-strengthening — see below);
this build record documents that the decision was independently
re-verified against the real `ALL_SCOPES`/provisioner data before
being applied, not merely relayed.

## F3 — the leg-(3) "closes it generically" claim was false; corrected

`test_provisioner_ensure_loop_sets_match` (added in `14711b3`) asserts
**only** `script_union == cm_union` — the two provisioners agree with
each other. It never checks `ALL_SCOPES ⊆ (script_union | cm_union)`.
`14711b3`'s commit message claimed leg (3) asserts every `ALL_SCOPES`
entry is *"present in both provisioners' combined ensure-loop array
set"* and the class docstring claimed the guard *"closes it
generically so the NEXT new scope cannot repeat F1 even if nobody
remembers to add a bespoke governance class for it."* Both false for
the provisioner axis, as the reviewer demonstrated: a new
`ALL_SCOPES` entry declared + bound in both realms but present in
**neither** provisioner passes sub-invariant 3 outright (the two
provisioners still agree — by both omitting it), while remaining
permanently unmintable on an existing (non-fresh-import) cluster —
exactly F1's failure mode, on the upgrade path, surviving the guard.

**Verified independently before applying the fix** (not just taken on
the reviewer's or orchestrator's word): read
`test_provisioner_ensure_loop_sets_match`'s body directly
(`tests/test_chart_drift_guards.py`, `TestAllScopesRegisteredInControlPlane`)
— confirmed it contains exactly one assertion,
`script_union == cm_union`, with no reference to `ALL_SCOPES` anywhere
in the method. Cross-checked `scripts/setup-memory-scopes.sh` against
`auth.py::ALL_SCOPES`: the entries genuinely absent from that
provisioner — `audittrace:audit`, `audittrace:context`,
`audittrace:index`, `audittrace:query`, `audittrace:scan:retrigger`,
`memory:episodic:read`, `memory:procedural:read`,
`memory:upload:write` — are exactly the core chat/query/audit scopes
that have no business in a memory-*scopes* provisioner. This confirms
the orchestrator's scope decision was correct on the data: forcing
`ALL_SCOPES ⊆ union` into `test_provisioner_ensure_loop_sets_match`
would misfile `audittrace:query` and its siblings into the wrong
script, a worse outcome than an honest, narrower parity-only guard.

**Fix applied** (commit `2efd21a`, this branch — `14711b3` itself was
**not** rewritten; its SHA is already cited by the fix-round-1 build
record logged to the memory server):

1. Reworded sub-invariant 3 in `TestAllScopesRegisteredInControlPlane`'s
   docstring to **"Provisioner parity ONLY — NOT an `ALL_SCOPES`
   membership check"**, naming the genuine `ALL_SCOPES` entries that
   legitimately belong to no memory-scopes provisioner and explaining
   why a strict subset check would be the wrong fix.
2. Deleted the false absolute *"closes it generically ... cannot
   repeat F1"* from the class docstring; the class-level intro now
   says sub-invariants 1 and 2 close the two REALM axes generically,
   while sub-invariant 3 is parity-only and does **not**.
3. Added an explicit **Residual risk** paragraph to the docstring
   naming the exact unclosed failure mode (a scope declared+bound in
   both realms, added to `ALL_SCOPES`, but present in neither
   provisioner — invisible on a fresh import, permanently 403 on an
   upgrade) and the follow-up WU it needs (a per-scope
   provisioner-ownership map deciding which provisioner, if any, owns
   each `ALL_SCOPES` entry).
4. Retracted `14711b3`'s commit-message wording via commit `2efd21a`'s
   own message — a quoted retraction, per the branch's established
   convention (this branch does not rewrite prior commits whose SHA is
   already cited elsewhere).

**No test assertions changed.** Re-ran
`TestAllScopesRegisteredInControlPlane`'s 4 sub-tests targeted
(`pytest tests/test_chart_drift_guards.py -k
TestAllScopesRegisteredInControlPlane`): 4 passed, unmodified
behaviour — the docstring is the only diff. Full `make test` (below)
confirms nothing else regressed.

**What the build record's own leg-(3) wording already got right (not
touched):** fix round 1's build record described leg (3) as
*"Provisioner parity — the combined 'ensure each scope exists' loop's
array-reference union is identical between the two files"* — accurate
as written; only the **commit message** and the **class docstring**
overstated it. This record does not amend `evidence/build-record-acl-
wu1-fix-round-1-2026-09-18.md` (durable artefact, already logged
server-side under `log_key`
`0b0cdd4d-04c3-428f-ab9d-37b47429c381/episodic/build-record-acl-wu1-
fix-round-1-2026-09-18.md`) — this new record supersedes it in
narrative terms without rewriting it, matching the branch's
immutable-artefact convention for specs.

## F4 — the A1 mechanism claim was false; the real mechanism is better than claimed

Fix round 1's build record states, for A1: *"Neutering
`_mock_rls_visible`'s owner branch ... turned this ONE new test RED
(`[mock]` param; `[postgres]` stayed GREEN, as expected — Postgres RLS
is enforced by the database, untouched by the Python-side neuter)."*
**False.** `PostgresConsoleAclEntriesService`'s own test-module
docstring (`tests/test_console_acl_service.py`, module-level) states
it runs against `InMemoryPostgresFactory` — **aiosqlite, no real
PostgreSQL, no database-enforced RLS in this param at all.** `[postgres]`
staying green was never evidence of DB-enforced RLS; it stayed green
only because the `[mock]`-only neuter (`_mock_rls_visible`) never
touched the Postgres-path code.

**Reproduced myself, this round, one neuter per run, `cmp`-restored
after each** (per `feedback_neuter_proofs_use_targeted_tests` +
`feedback_unpinnable_claim_check_your_own_techniques` — these are my
own numbers, not carried over from the reviewer's report):

| # | File / line | Neuter | Command | Result |
|---|---|---|---|---|
| 1 | `src/audittrace/services/console_acl/_postgres.py:102` — `_rls_mirror_clause`'s owner disjunct (`ConsoleAclEntry.user_sub == user_context.user_id`) | deleted the disjunct from the `or_(...)` entirely | `pytest tests/test_console_acl_service.py -q --no-cov` | **1 failed, 181 passed** — the sole failure is `TestOwnerPrincipalIds::test_owner_branch_alone_makes_a_grant_to_someone_else_visible[postgres]`. `[mock]` unaffected (different code path). |
| 2 | `src/audittrace/services/console_acl/_postgres.py:93` — `_owner_scope_clause`'s body (`ConsoleAclEntry.user_sub == user_context.user_id`) | replaced with an always-true tautology (`ConsoleAclEntry.user_sub != "__neuter_probe_sentinel__"`) — proves the filter's necessity for cross-owner isolation by removing its restriction entirely | `pytest tests/test_console_acl_service.py -q --no-cov` | **2 failed, 180 passed** — `TestAuditReadsIncludeExpired::test_find_entries_by_resource_owner_scoped[postgres]` and `TestMixedRowScenarios::test_find_entries_by_principal_skips_other_owner_and_type[postgres]`. |

Both neuters restored individually via `cp` from a pre-neuter copy,
`cmp -s` confirmed byte-identical to the pre-neuter tree before moving
to the next, and again after the second restore (`git status --short`
clean). **Both `[postgres]` branches ARE falsifiable — on aiosqlite,
by the Python-side SQLAlchemy filter, exactly as the reviewer's own
line-102/line-93 neuters showed** (the reviewer reported 1 and 6 RED
respectively for the same two lines using a different neuter
technique on line 93 — an inversion rather than a widening; both
techniques independently prove the same two lines are load-bearing,
the exact RED count is technique-dependent, not a discrepancy in the
underlying finding).

**Corrected claim for the record:** `[postgres]` in this suite means
**aiosqlite via `InMemoryPostgresFactory`, not a real PostgreSQL
server** — there is **no database-enforced RLS anywhere in this test
module**. The RLS-mirror clauses (`_rls_mirror_clause`,
`_owner_scope_clause`) are Python-side SQLAlchemy filters that mirror
what migration 031's real Postgres `USING` clause enforces in
production; this module's `[postgres]` param proves the SQLAlchemy
mirror is correct on aiosqlite, not that production RLS is enforced —
that is `tests/test_console_acl_routes.py` + a real PostgreSQL
instance's job (belt-and-suspenders, per
`feedback_unit_tests_miss_rls`), unchanged by this round. Several of
this WU's "×2 implementations" framing (mock + postgres) rests on this
distinction; it is worth stating plainly rather than leaving the false
"DB-enforced" framing to mislead a future reader of the fix-round-1
record.

## Absolutes self-check

* No new unqualified "generically closes" / "cannot repeat" claim
  introduced by this round's docstring edit — the corrected text names
  precisely which axis is closed (1, 2) and which is not (3), with the
  residual risk spelled out.
* Every RED/GREEN count above is this round's own `pytest` output,
  captured live, not copied from the reviewer's dispatch or from
  fix-round-1's record.
* The A1 correction above states plainly that no DB-enforced RLS is
  exercised in this test module — not just that the neuter is
  falsifiable, but what it actually proves and does not prove.

## Deviations from the reviewer's dispatch

None. F3 followed the orchestrator's claim-correction route exactly
(re-verified against the real data, as documented above, rather than
taken on faith); F4 corrected the mechanism claim and re-derived both
neuter proofs independently, as asked.
