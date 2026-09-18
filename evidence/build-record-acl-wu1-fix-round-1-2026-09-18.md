---
spec_ref: "sdlc/specs (private, ~/work/audittrace-private/specs/): 2026-09-17-SPEC-sovereign-authorization-layer-acl-WU0-ratified-candidate.md (RATIFIED) + 2026-09-18-SPEC-ADDENDUM-I-ACL-operator-ratification-and-three-rulings.md (RATIFIED, wins on conflict). Memory-server key: not independently re-confirmed this round (see recall_evidence — the server returned 401); house convention is the decisions collection under a key derived from the filename, per feedback_spec_first_persist_before_build."
spec_hash: "sha256:e2204a4cbd44dd396e38789d62149204bdc5b76d749e8e830deec3b102d7b1aa (WU-0 candidate) + sha256:c527cf835ac1fe046ec5482d26e20fa498fbcbb3156610a7375a5e5a2bbc3cc4 (ADDENDUM I) — both verified against the on-disk files this round via sha256sum."
recall_evidence: "STEP-2 query 'sovereign ACL read path scope registration control-plane guard' via scripts.deploy.memory.recall_deploy_lessons(front_door=https://audittrace.local, limit=300) FIRST returned HTTP 401 (0 lessons, best-effort, non-blocking) — the memory server's access AND refresh tokens were both expired at that point (operator-confirmed outage, unrelated to this change). Both governing specs were read directly from disk with sha256 verified (see spec_hash) as the fallback source of truth while the outage was live. The operator resolved the outage (re-login) DURING this round; the SAME query was re-run before STEP-5 and returned 300 lessons (recall limit=300 per feedback_recall_limit_counts_chunks_not_documents), confirming recall is healthy again as of this record."
log_key: "0b0cdd4d-04c3-428f-ab9d-37b47429c381/episodic/build-record-acl-wu1-fix-round-1-2026-09-18.md"
index_status: "{\"status\": \"indexed\", \"collections\": {\"decisions\": 9}, \"total_chunks\": 9}"
branch: "feat/sovereign-acl-read-path"
commit: "14711b36d5b31d574abddabf32e8dbd39cc284c7"
gates: "make test: PASS (5650 passed, 0 failed, 0 skipped in 670.06s; zero-skip policy enforced by scripts/check per junit.xml). Per-file coverage gate: PASS (lines >= 90% AND branches >= 90% on every file with branches — file/line counts deliberately not cited per register D22, non-deterministic at report time). ruff check: clean. ruff format --check: clean (touched files only; 15 unrelated pre-existing docs/evidence .md files also flagged by a repo-wide format check, left untouched — out of this round's scope). mypy: clean on both .venv (mypy 1.x) and the pinned pre-commit hook (mirrors-mypy v1.8.0) for every touched .py file. make helm-lint: PASS (chart lint 0 failed; vaultSecretFileGuard present in 3 workloads). Single alembic head: b3f8a1c6d9e2 (no new migration this round). Integration gate: not run this round (no product route changed; WU-1's live E2E is explicitly deferred to the fork ACL-shim WU-4 per the ratified spec, unchanged by this fix round)."
---

# Build record — Sovereign ACL WU-1, fix round 1 (2026-09-18)

## Scope

Fix round 1 on `8dff6b3` (Sovereign Authorization Layer EPIC, WU-1,
read path only), addressing the independent reviewer's two BLOCKING
findings (F1, F2) and two advisory items (A1, A2). See the reviewer's
dispatch for the full finding text; this record documents what was
built and the falsifiability proof for each.

## F1 — `memory:acl:read-own` registered in all four control-plane sites

Mirrored commit `54c4ab5` (the immediately-preceding sibling WU,
console-tool-favorites) exactly:

| Site | Change |
|---|---|
| `keycloak/realm-audittrace.json` | `clientScopes` entry added; `audittrace-librechat.defaultClientScopes` gains `memory:acl:read-own` |
| `charts/audittrace/files/realm-audittrace.json` | same, chart-shipped copy |
| `charts/audittrace/templates/keycloak/configmap-memory-scopes-script.yaml` | `MEMORY_ACL_READ_SCOPES` array + own bind loop (Step 2v), folded into the shared ensure-loop |
| `scripts/setup-memory-scopes.sh` | same array + bind loop, mirrors the configmap verbatim |

Added `TestKeycloakAclReadOwnScopeGovernance` (provisioner array
parity, bind-loop-targets-librechat-only, forbidden-scope-absence) and
`TestOpencodeClientScopesUnchangedByAcl` (regression guard: OpenCode's
scope set never gains this scope), mirroring the Tool-Favorites
sibling classes. Updated `TestLibrechatConsoleClient.
_EXPECTED_DEFAULT_SCOPES` (exact-set assertion) to include the new
scope.

### The generic guard — `TestAllScopesRegisteredInControlPlane`

Three sub-invariants over every `auth.py::ALL_SCOPES` entry:

1. **Declared** — present as a `clientScopes[].name` in both realm files.
2. **Bound** — granted (default or optional) to at least one client in
   both realm files.
3. **Provisioner parity** — the combined "ensure each scope exists"
   loop's array-reference union is identical between
   `scripts/setup-memory-scopes.sh` and the chart's in-cluster Job
   ConfigMap.

**Non-vacuity, proved by hand this round** (each: neuter → RED →
restore, `diff -q` confirmed byte-identical to the pre-neuter file):

| Sub-guard | Neuter | Result |
|---|---|---|
| (1) declared | deleted the `memory:acl:read-own` clientScopes block from `keycloak/realm-audittrace.json` only | `test_every_scope_declared_in_both_realms` RED (`AssertionError: ... ['memory:acl:read-own']`) |
| (2) bound | removed `memory:acl:read-own` from `audittrace-librechat.defaultClientScopes` in `keycloak/realm-audittrace.json` (declaration left intact) | `test_every_scope_bound_to_some_client` RED |
| (3) provisioner parity | deleted the `MEMORY_ACL_READ_SCOPES` array + its ensure-loop reference from `scripts/setup-memory-scopes.sh` only | `test_provisioner_ensure_loop_sets_match` RED |

All three restored; full `tests/test_chart_drift_guards.py` re-run
GREEN (171/171) after each restore.

**Independent discovery while proving this out (disclosed, not
fixed):** sub-guard (2), run un-neutered against the REAL tree, failed
first — not because of this WU, but because `audittrace:scan:retrigger`
and `memory:upload:write` are ALREADY declared `clientScopes` in both
realm files on `main` with **zero** client (default or optional)
holding either. Pre-existing, unrelated to the ACL domain. Exempted by
name in `_PRE_EXISTING_UNBOUND_SCOPES` (with its own
`test_exemption_list_is_narrow` guard, so the exemption cannot silently
grow to cover a `memory:acl:*` scope). **Not fixed this round** — which
client(s) should hold an admin-grade scope is a security decision
outside this WU's mandate; flagged for a dedicated follow-up.

## F2 — three of the Mock's five `perm_bits` containment sites were vacuous

Per-site neuter table (this run's numbers, `cmp`-restored after each row):

| Site | Location | Test | RED |
|---|---|---|---|
| Postgres `_contains_bit` (shared, 5 call sites) | `_postgres.py:83,137,221,250,278,296` | `TestBitmaskContainment` `[postgres]` | 4 |
| Mock `has_permission` | `_mock.py:167` | `TestBitmaskContainment` `[mock]` | 4 |
| Mock `find_accessible_resources` | `_mock.py:243` | `TestFindAccessibleResourcesBitmaskContainment` (NEW) | 4 |
| Mock `find_public_resource_ids` | `_mock.py:271` | `TestFindPublicResourceIdsBitmaskContainment` (NEW) | 4 |
| Mock sole-owned loop 1 | `_mock.py:298` | `TestSoleOwnedResourceIds::test_not_sole_owner_when_another_user_holds_delete` (pre-existing) | 1 |
| Mock sole-owned loop 2 | `_mock.py:313` | `TestSoleOwnedResourceIds::test_not_sole_owner_when_competitor_holds_delete_among_other_bits` (NEW) | 1 |

Each row's neuter (equality substituted for `&`-containment) was run,
confirmed RED, and restored individually — never batched, per
`feedback_neuter_guards_individually_never_batched`. The evidence file
`evidence/wu1-console-acl-store-local-capture-2026-09-17.md`'s Guard 2
section is rewritten with this exact table, replacing the prior
singular/aggregate wording.

## A1 — RLS-mirror owner branch, falsified alone

Added `TestOwnerPrincipalIds::
test_owner_branch_alone_makes_a_grant_to_someone_else_visible`: seeds
a row where the caller is the granting owner (`user_sub == caller`)
but the grant's principal is someone else entirely
(`principal_id == "someone-else-entirely"`). Every prior test in this
class set `user_sub == principal_id`, so branch 2 (direct-principal
match) always also applied — the owner branch's necessity was never
isolated. Neutering `_mock_rls_visible`'s owner branch (deleting the
`if row.user_sub == user_context.user_id: return True` clause) turned
this ONE new test RED (`[mock]` param; `[postgres]` stayed GREEN, as
expected — Postgres RLS is enforced by the database, untouched by the
Python-side neuter). Restored, `cmp`-identical.

## A2 — `MAX_PERM_BITS` pinned to the migration's CHECK literal

Added `test_perm_bits_check_constraint_upper_bound_matches_max_perm_bits`
to `tests/test_console_acl_migration.py`: regex-extracts migration
031's `perm_bits <= N` upper bound and asserts `N == MAX_PERM_BITS`
(the live Python constant), rather than a bare string match against
the literal `15`. Neuter proof: bumped the migration's literal to `16`
alone — test went RED; restored, `cmp`-identical.

## Absolutes self-check (the F2 gate: no unproven universality claim survives)

* **Source comments** — the new `_ENSURE_LOOP_HEADER_RE` docstring in
  `test_chart_drift_guards.py` states the ensure-loop is identified by
  "TWO OR MORE" array refs (a proven, checked property of this file's
  structure today), not an unqualified claim about all possible bash
  loops.
* **Prose** (this record + the rewritten evidence-file section) —
  every RED/GREEN count above is this run's own pytest output, not
  carried over from the original evidence file.
* **Tables** — the F2 per-site table replaces the prior aggregate
  "8 RED x 2 implementations" figure with six independently-measured
  rows; no total is given.
* **Commit message** — describes the fix as closing "3 of 5" sites (a
  counted, checked claim) and names the pre-existing exemption
  explicitly rather than implying the generic guard is unconditionally
  exception-free.

## Deviations from the reviewer's dispatch

None substantive. One scope addition beyond the letter of the ask: the
generic guard (F1 point 3) surfaced a pre-existing, unrelated
provisioning gap (`audittrace:scan:retrigger`, `memory:upload:write`)
which is disclosed and exempted rather than silently fixed or silently
hidden — see F1 section above and the commit message.
