# Evidence — Sovereign Authorization Layer, ACL WU-2a resource-ownership verification, local capture (2026-09-18)

**Scope of this evidence file.** ACL WU-2a
(`2026-09-18-SPEC-acl-wu2a-resource-ownership-verification.md`,
`sha256:8c9e7ff803c420fab7f968dbdd1a51cfb8ea5c5376088dd21f9795e55bc4011d`).
This file satisfies ADR-049 Rule 1 (Verification) in full and gives a
reconstructible Rule-3-shaped capture: a real-Postgres before/after
proof of the vulnerability this WU closes, run through the ACTUAL
migration files (031 then 032), not a hand-retyped copy of their SQL,
plus a per-guard neuter table. It does **NOT** satisfy Rule 2
(Validation through a deployed image + public API + scoped JWT) —
WU-2a adds no route; it mirrors WU-1's own precedent
(`evidence/wu1-console-acl-store-local-capture-2026-09-17.md`): no
product write-path consumes `services/console_acl/_ownership.py` yet
(that is WU-2b), so there is nothing to exercise through `/console/acl`
in this WU. The DB-level control (migration 032) is exercised live
below against a real, non-superuser-role-gated Postgres — the strongest
evidence available before a write route exists.

**Revision note (fix round 1, 2026-09-18).** Independent review
REJECTed the first cut of this file on two grounds, both addressed
below: (F1) the UPDATE-side `resource_id` escalation (spec §3.4's
"updated" case) had no BEHAVIOURAL real-Postgres proof — only a
rendered-SQL text pin, which a reviewer neuter showed is not a
behavioural guard; (F2) §4 below asserted, as *proven*, a FALSE claim
that the UPDATE `WITH CHECK` ownership subquery makes the owner-only
`USING` clause "redundant" for H-2 — corrected in place, not deleted,
with the counter-example that disproves it. Both are fixed by new
tests in `tests/test_acl_ownership_rls.py`, re-run against the
UNMODIFIED, committed migration 032 before any of this round's neuters
were applied.

## 1. The §1 escalation and its UPDATE-side sibling — before/after, against a REAL Postgres

`tests/test_acl_ownership_rls.py` brings up a throwaway `postgres:16`
container (or uses `AUDITTRACE_TEST_POSTGRES_URL`), creates a
non-superuser `NOBYPASSRLS` LOGIN role (superusers bypass RLS
unconditionally — a superuser "proof" proves nothing), and runs the
REAL migration 031 (`TestBeforeFix`) or 031-then-032 (`TestAfterFix`)
via Alembic's own `Operations` bound to the live connection — not a
hand-copied DDL string. Full verbose run, this box, 2026-09-18, against
the migration files exactly as committed (no neuter applied):

```
$ .venv/bin/pytest tests/test_acl_ownership_rls.py -v --no-cov
collected 17 items

tests/test_acl_ownership_rls.py::TestBeforeFix::test_escalation_attacker_grants_self_access_on_victims_agent PASSED [  5%]
tests/test_acl_ownership_rls.py::TestBeforeFix::test_h1_any_user_deletes_any_public_grant PASSED [ 11%]
tests/test_acl_ownership_rls.py::TestBeforeFix::test_h2_any_user_takes_ownership_of_a_public_grant PASSED [ 17%]
tests/test_acl_ownership_rls.py::TestBeforeFix::test_update_resource_id_hijack_via_legitimately_owned_row PASSED [ 23%]
tests/test_acl_ownership_rls.py::TestBeforeFix::test_resource_squatting_blocks_the_legitimate_owner PASSED [ 29%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_escalation_insert_on_agent_is_blocked PASSED [ 35%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_escalation_insert_on_prompt_group_is_blocked PASSED [ 41%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_h1_delete_of_public_row_by_non_owner_is_blocked PASSED [ 47%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_h2_update_ownership_takeover_is_blocked PASSED [ 52%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_h2_general_takeover_blocked_even_when_attacker_repoints_to_owned_resource PASSED [ 58%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_update_resource_id_escalation_is_blocked_for_agent PASSED [ 64%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_update_resource_id_escalation_is_blocked_for_prompt_group PASSED [ 70%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_owner_can_still_grant_on_their_own_agent PASSED [ 76%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_owner_can_still_grant_on_their_own_prompt_group PASSED [ 82%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_unmapped_resource_type_is_refused_at_the_db_layer PASSED [ 88%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_select_read_path_contract_is_unchanged PASSED [ 94%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_squatting_is_prevented_so_the_owner_can_still_grant_publicly PASSED [100%]

============================== 17 passed in 2.45s ==============================
```

**Before (`TestBeforeFix`, migration 031 alone — what `main` ships
today):** every attack SUCCEEDS —
`test_escalation_attacker_grants_self_access_on_victims_agent` inserts
a grant naming `agent-1` (owned by `owner-sub-0001`) with
`user_sub=attacker-sub-0002` and it is accepted; `test_h1_...` shows a
non-owner `DELETE` of a public row affects 1 row; `test_h2_...` shows a
non-owner `UPDATE ... SET user_sub=<attacker>` succeeds and the row's
`user_sub` column reads back as the attacker's sub;
**`test_update_resource_id_hijack_via_legitimately_owned_row`
(fix round 1, F1's "before" half) — the attacker first creates a grant
naming a resource they legitimately own (`agent-attacker`, which
passes even the eventual ownership check trivially since it isn't
enforced pre-032 at all), then `UPDATE ... SET resource_id='agent-1'`
(the victim's resource) — migration 031's `WITH CHECK` only re-checks
`user_sub` (unchanged here), never `resource_id`, so the hijack
succeeds: `rowcount == 1`, the row's `resource_id` column reads back as
`agent-1`.** `test_resource_squatting_blocks_the_legitimate_owner`
(bonus finding) — an attacker's escalation-INSERT squats the victim's
resource with a public grant FIRST, then the victim's own, entirely
legitimate attempt to create their own public grant on their own
resource fails with a unique-constraint violation
(`uq_console_acl_entries_public_resource` allows only one public row
per resource) — a DoS variant of the same root cause.

**After (`TestAfterFix`, 031 then 032):** the SAME attacks each raise
`sqlalchemy.exc.DBAPIError` matching `"row-level security"` (INSERT,
UPDATE) or affect 0 rows (`UPDATE`/`DELETE`); the SAME legitimate
operations (owner inserts a grant on their own `agent`, and separately
on their own `promptGroup`) still succeed; an unmapped `resource_type`
(`mcpServer`) is refused at the DB layer too; the SELECT contract
(public rows visible to everyone including with no GUC bound; private
`user`-principal rows gated to owner + named principal only) is
unchanged from migration 031. **`test_update_resource_id_escalation_is_
blocked_for_agent`/`..._for_prompt_group` (fix round 1, F1 — the
blocking finding)** — the SAME attacker-legitimately-owns-their-own-row
UPDATE-to-victim's-resource attack, proven per resolved `resource_type`
INDIVIDUALLY, now raises `DBAPIError` matching `"row-level security"`
and the row's `resource_id` column is confirmed UNCHANGED afterward.
**`test_h2_general_takeover_blocked_even_when_attacker_repoints_to_
owned_resource` (fix round 1, F2 correction)** — an attacker attempts
`UPDATE ... SET user_sub=<attacker>, resource_id=<attacker's own
agent>` on the victim's PUBLIC row in one statement: `rowcount == 0`,
both columns unchanged. This is the general form of H-2 and it is
blocked by the owner-only `USING` clause alone (the attacker never
gets to select the row for update at all), NOT by the `WITH CHECK`
ownership subquery — see §4 for why the earlier claim that the two are
redundant was wrong. `test_squatting_is_prevented_so_the_owner_can_
still_grant_publicly` (bonus finding, after half) — the squat attempt
itself now fails, so it never occupies the one-public-row-per-resource
slot and the owner's legitimate grant succeeds cleanly.

## 2. Per-`resource_type` ownership table

| `resource_type` | Sovereign store | Owner can INSERT a grant | Non-owner INSERT blocked | Owner-owned-row resource_id UPDATE-hijack blocked |
|---|---|---|---|---|
| `agent` | `console_agents` (migration 028) | YES (`test_owner_can_still_grant_on_their_own_agent`) | YES (`test_escalation_insert_on_agent_is_blocked`) | YES (`test_update_resource_id_escalation_is_blocked_for_agent`) |
| `promptGroup` | `console_prompt_groups` (migration 025) | YES (`test_owner_can_still_grant_on_their_own_prompt_group`) | YES (`test_escalation_insert_on_prompt_group_is_blocked`) | YES (`test_update_resource_id_escalation_is_blocked_for_prompt_group`) |
| `mcpServer` | none yet | N/A — refused for EVERYONE incl. would-be owner (`test_unmapped_resource_type_is_refused_at_the_db_layer`) | YES (by construction — the OR-chain evaluates FALSE) | N/A — same OR-chain branch, not individually re-proven for UPDATE |
| `remoteAgent` | none yet | N/A (same as `mcpServer`, not individually re-proven — same OR-chain branch) | — | — |
| `skill` | none yet | N/A (same as `mcpServer`) | — | — |
| `sharedLink` | none yet | N/A (same as `mcpServer`) | — | — |

Application-layer parity (`tests/test_console_acl_ownership.py`,
15 tests, unchanged this round): `owns()` proven per resource_type
individually — owner/non-owner/nonexistent-id for `agent` and
`promptGroup` each (6 tests), `UnknownResourceTypeError` proven for
each of the 4 unresolved types individually plus a bogus type plus the
error-message content (9 tests). `RESOLVED_RESOURCE_TYPES ==
{"agent", "promptGroup"}` and `UNRESOLVED_RESOURCE_TYPES ==
{"mcpServer", "remoteAgent", "skill", "sharedLink"}` pinned directly
(`TestEnumerationIsCommitted`).

## 3. H-1 / H-2 outcome

Both hypotheses from §2 of the spec were **CONFIRMED, not refuted** —
constructed the exact SQL against a real, non-superuser Postgres role:

* **H-1** (any authenticated user may `DELETE` any PUBLIC ACL row) —
  CONFIRMED before the fix (`test_h1_any_user_deletes_any_public_grant`,
  `rowcount == 1`); CLOSED after (`test_h1_delete_of_public_row_by_non_owner_is_blocked`,
  `rowcount == 0`).
* **H-2** (any authenticated user may take ownership of a PUBLIC row
  via `UPDATE`) — CONFIRMED before the fix in BOTH its narrow form
  (`test_h2_any_user_takes_ownership_of_a_public_grant`, `SET user_sub`
  only) and its general form (equivalent to
  `test_update_resource_id_hijack_via_legitimately_owned_row`'s
  mechanism); CLOSED after in BOTH forms
  (`test_h2_update_ownership_takeover_is_blocked` and
  `test_h2_general_takeover_blocked_even_when_attacker_repoints_to_owned_resource`,
  both `rowcount == 0`, both columns unchanged).

No real-Postgres path was unavailable on this box (Docker confirmed
up, ephemeral `postgres:16` brought up successfully) — no OPEN verdict
needed for either hypothesis.

## 4. Neuter table — one guard per run, `cmp`-verified byte-identical restore

Every neuter below was run individually against the byte-identical
restored files (verified with `cmp` before/after each round); no batch
neuter was used. **N2 was split this round (fix round 1, F3) — the
original build ran it as a single combined UPDATE+DELETE `USING` edit,
which is what produced F2's wrong conclusion; N2a and N2b below are
the correctly isolated re-runs.** **"Reproduce with" names the exact
command scope** (fix round 1, non-blocking reviewer observation) so a
reader can re-run any row without guessing which file/test it applies
to; every count below was re-captured this round from that command's
own output (`tests/test_acl_ownership_rls.py` currently holds 17
tests; `tests/test_console_acl_ownership.py` holds 15 — except where
noted, N5's neuter itself changes that file's effective test count,
explained in its row).

| # | Guard | Neuter | RED (what failed) | Reproduce with | Restored |
|---|---|---|---|---|---|
| N1 | Migration 032 INSERT ownership subquery | Dropped `AND {_OWNERSHIP_SUBQUERY}` from the INSERT `WITH CHECK`, real migration file | `test_escalation_insert_on_agent_is_blocked`, `test_escalation_insert_on_prompt_group_is_blocked`, `test_unmapped_resource_type_is_refused_at_the_db_layer`, `test_squatting_is_prevented_so_the_owner_can_still_grant_publicly` (the last added fix round 1 — it also depends on the escalation-INSERT guard) | `pytest tests/test_acl_ownership_rls.py -q` → `4 failed, 13 passed` | `cmp` clean |
| N2a | Migration 032 UPDATE owner-only `USING`, ALONE | Widened ONLY the UPDATE policy's `USING` back to the broad predicate; DELETE untouched | `test_h2_update_ownership_takeover_is_blocked` (raises `ProgrammingError: new row violates row-level security policy` — the WITH CHECK ownership subquery still rejects THIS narrow variant, since `resource_id` is unchanged and the attacker doesn't own it) AND `test_h2_general_takeover_blocked_even_when_attacker_repoints_to_owned_resource` (`rowcount == 1` — the GENERAL variant, where the attacker also repoints `resource_id` to a resource they own, now SUCCEEDS) | `pytest tests/test_acl_ownership_rls.py -q` → `2 failed, 15 passed` | `cmp` clean |
| N2b | Migration 032 DELETE owner-only `USING`, ALONE | Widened ONLY the DELETE policy's `USING` back to the broad predicate; UPDATE untouched | ONLY `test_h1_delete_of_public_row_by_non_owner_is_blocked` (`rowcount == 1` not `0`) | `pytest tests/test_acl_ownership_rls.py -q` → `1 failed, 16 passed` | `cmp` clean |
| N3 | Migration 032 fail-closed for unmapped `resource_type` | Added `resource_type NOT IN ('agent','promptGroup') OR (...)` to the ownership subquery (simulated a "default-permit" bug) | ONLY `test_unmapped_resource_type_is_refused_at_the_db_layer` — all sibling `TestAfterFix` tests stayed green, proving this guard is independently tested, not piggy-backing on another's pass | `pytest tests/test_acl_ownership_rls.py -q` → `1 failed, 16 passed` | `cmp` clean |
| N4 | `owns()` fail-closed (`_ownership.py`) | `raise UnknownResourceTypeError(...)` → `return False` | All 6 `TestUnknownResourceTypeFailsClosed` tests (parametrized ×4 + 2) — `Failed: DID NOT RAISE UnknownResourceTypeError` | `pytest tests/test_console_acl_ownership.py -q` → `6 failed, 9 passed` | `cmp` clean |
| N5 | Per-`resource_type` dispatch, `promptGroup` arm | Removed `"promptGroup": _owns_prompt_group` from `_OWNERSHIP_RESOLVERS` | The 3 `TestOwnsPromptGroup` tests + 2 enumeration tests, PLUS the `test_unmigrated_resource_type_raises_named_error` parametrize list GROWS by one (`promptGroup` becomes unresolved too, since `UNRESOLVED_RESOURCE_TYPES` is computed live from the dispatch table) — the new `[promptGroup]` case PASSES (it is correctly fail-closed under the neuter), which is why the total shifts from 15 to 16 collected items. All 3 `TestOwnsAgent` tests stayed green, proving a dead resolver arm for ONE type cannot hide behind another type's pass | `pytest tests/test_console_acl_ownership.py -q` → `5 failed, 11 passed` (16 collected, not 15 — the count itself is part of the guard's own falsifiability signal) | `cmp` clean |
| N6 | Migration 032 SELECT read-path regression guard | Dropped the `principal_type = 'user' AND principal_id = ...` disjunct from `_SELECT_USING` | ONLY `test_select_read_path_contract_is_unchanged` (private-row visibility to its named principal went from 1 to 0) | `pytest tests/test_acl_ownership_rls.py -q` → `1 failed, 16 passed` | `cmp` clean |
| N7 | Migration 032 UPDATE ownership subquery (fix round 1, F1) | Dropped `AND {_OWNERSHIP_SUBQUERY}` from the UPDATE `WITH CHECK` ALONE; `USING` left intact | ONLY `test_update_resource_id_escalation_is_blocked_for_agent` and `..._for_prompt_group` (`Failed: DID NOT RAISE DBAPIError`) — `test_h2_general_takeover_blocked_even_when_attacker_repoints_to_owned_resource` stays GREEN under this neuter, confirming it is genuinely protected by `USING`, not by this subquery | `pytest tests/test_acl_ownership_rls.py -q` → `2 failed, 15 passed` | `cmp` clean |
| R8a | Per-`resource_type` independence, `agent` arm (credited to the independent review — not run in fix round 0/1, added here per the reviewer's instruction to record the strongest proof that exists) | Forced the ownership subquery's `agent` branch always-`TRUE` (dropped its `EXISTS` clause entirely — any `resource_id` claimed under `resource_type='agent'` passes) | `test_escalation_insert_on_agent_is_blocked`, `test_update_resource_id_escalation_is_blocked_for_agent`, `test_squatting_is_prevented_so_the_owner_can_still_grant_publicly` (agent-scoped) — every `..._prompt_group`/promptGroup-scoped test stayed GREEN | `pytest tests/test_acl_ownership_rls.py -q` → `3 failed, 14 passed` | `cmp` clean |
| R8b | Per-`resource_type` independence, `promptGroup` arm (credited to the independent review, mirror of R8a) | Forced the ownership subquery's `promptGroup` branch always-`TRUE` (dropped its `EXISTS` clause entirely) | ONLY `test_escalation_insert_on_prompt_group_is_blocked` and `test_update_resource_id_escalation_is_blocked_for_prompt_group` — every agent-scoped test (including `test_squatting_is_prevented_so_the_owner_can_still_grant_publicly`) stayed GREEN | `pytest tests/test_acl_ownership_rls.py -q` → `2 failed, 15 passed` | `cmp` clean |

R8a/R8b are stronger per-`resource_type` independence evidence than
N1/N7 alone: N1/N7 prove the whole subquery is load-bearing; R8a/R8b
prove EACH `resource_type`'s branch is independently load-bearing —
forcing one arm permanently open never lets the other arm's tests go
green-by-accident, which is the strongest form of "a dead resolver arm
for one type cannot hide behind another type's pass" this file
carries. Post-verification: both migration 032 and
`services/console_acl/_ownership.py` are confirmed byte-identical to
the original build after all of N1–N7/R8a/R8b (`sha256sum` after the
last restore: `9a796580b5f25ba993d1d0933071223f0bf967211c88b6998542a25a0d7052bb`
and `875846cb7bd29ad11ca54ae322d811bb283a6aca867b7af7e7613690ad749277`
respectively — unchanged since round 0).

**Corrected finding (fix round 1, F2) — the earlier claim in this
section was FALSE and is retracted, not merely reworded.** N2a proves
the actual shape: with the UPDATE policy's `USING` widened ALONE
(ownership subquery in `WITH CHECK` left intact), the NARROW H-2
variant (`SET user_sub=<attacker>` only, `resource_id` unchanged and
still pointing at a resource the attacker does not own) stays blocked
— but the GENERAL variant (`SET user_sub=<attacker>, resource_id=
<a resource the attacker DOES own>`) SUCCEEDS. **The owner-only
`USING` clause is therefore the ONLY general defence against ACL-row
ownership takeover** — the `WITH CHECK` ownership subquery is a real,
independently-tested guard for a DIFFERENT attack (UPDATE-side
`resource_id` escalation on a row the attacker already legitimately
owns, N7 above), not a redundant backstop for H-2. Filing the causal
lesson: N2's ORIGINAL (fix round 0) form batched the UPDATE and DELETE
`USING` edits into one neuter, and never constructed the two-field
`SET user_sub=..., resource_id=...` variant — the batching is what let
the wrong generalisation stand uncaught.

## 5. What "unbypassable" means, precisely (fix round 1, F4; SUPERSEDED 2026-09-18 — see the amendment banner below)

> **⚠ SUPERSEDED (2026-09-18, factual amendment after PASS — not a new
> build round).** The section below, as written during fix round 1,
> is retained VERBATIM beneath this banner rather than deleted — this
> project keeps its wrong calls visible
> (`feedback_decision_log_is_append_only_keep_the_wrong_ones`). It
> said Postgres RLS "may not be enforcing at runtime on the live
> deployment" and, on that premise, that "WU-2a protects nothing
> today." **That premise was wrong and has been retracted.** The
> orchestrator's original HIGH finding rested on reading a COMMITTED
> Keycloak realm file as live runtime state and concluding a test
> user was newer than it actually is; the finding
> (`~/work/audittrace-private/evidence/2026-09-18-FINDING-postgres-rls-appears-inert-in-production.md`)
> has been RETRACTED and re-seeded with the original text preserved
> beneath its own banner. A decisive front-door test, run by the
> operator with a REAL second-subject token, confirms the opposite of
> what fix round 1 assumed:
>
> ```
> GET /memory/conversational?limit=1000   as auditor-b (sub 094ef0ae…)
> HTTP 200   total = 8
>    094ef0ae-f071-41a7-ad87-b602762829b4   8
> VERDICT: RLS ENFORCING
> ```
>
> Zero of the other user's 505 rows leaked; every row returned was
> auditor-b's own. **Postgres RLS IS enforcing on the live cluster.**
> Corollary for THIS WU: migration 032's DB-level control does not
> merely hold "where RLS is enforced" as a hypothetical — RLS IS
> enforced on the running cluster, so **migration 032's protection
> lands on that cluster today**, subject only to point 2 below (which
> was never contingent on the RLS question and remains true
> unchanged). See §5-amended immediately below for the corrected
> text.

**Original fix-round-1 text (WRONG on point 1, kept for the record):**

> The build record and this file both stand by migration 032 being
> proven unbypassable **where Postgres RLS is enforced** — that is
> exactly what N1–N7 above demonstrate against a real, non-superuser
> Postgres role. **That is not the same claim as "protects production
> today."** Two facts qualify it, both pre-existing and out of this
> WU's scope:
>
> 1. There is an open, HIGH-severity finding that Postgres RLS **may
>    not be enforcing at runtime on the live deployment**
>    (`~/work/audittrace-private/evidence/2026-09-18-FINDING-postgres-rls-appears-inert-in-production.md`,
>    2026-09-18). If RLS is inert at runtime, migration 032's policies
>    are inert with it — **WU-2a protects nothing today** on a
>    cluster in that state. This WU does not cause, worsen, or fix
>    that finding; it is recorded here so this evidence is never read
>    as a runtime guarantee it cannot make.
> 2. `services/console_acl/_ownership.py`'s `owns()` has **zero
>    production callers** as of this WU — WU-2b (the write path) has
>    not shipped. So even independent of the RLS-enforcement
>    question, the application-layer resolver currently protects
>    nothing either, simply because nothing calls it yet.
>
> One thing DOES survive the RLS-inert finding: `get_agent`
> (`services/console_agents.py:346-361`) and `get_group`
> (`services/console_prompts.py:359-375`), which `owns()` delegates
> to, filter `user_sub` **explicitly in the SQL `WHERE` clause**, not
> only via RLS — so `owns()`'s answer is correct even on a cluster
> where RLS is not enforcing, once WU-2b gives it a caller.

## 5-amended. What "unbypassable" means, precisely (CURRENT, 2026-09-18)

Migration 032 is proven unbypassable against a real, non-superuser
Postgres role — that is exactly what N1–N7 above demonstrate. **This
is no longer a contingent claim.** Postgres RLS enforcement on the
live cluster is CONFIRMED by live front-door evidence (the
`auditor-b` two-subject test quoted above, operator-run,
2026-09-18) — so migration 032's DB-level control **does land on the
running cluster today**, not merely "where RLS is enforced" as an
unverified hypothetical.

**One qualification remains, unchanged from fix round 1 and never
contingent on the RLS question:** `services/console_acl/_ownership.py`'s
`owns()` has **zero production callers** as of this WU — WU-2b (the
write path) has not shipped, so nothing in production calls `owns()`
yet. The DB-level policy (migration 032, now confirmed live and
enforcing) is therefore **the only LIVE control** today; the
application-layer resolver is built and correct but dormant until
WU-2b wires a caller to it.

`get_agent` (`services/console_agents.py:346-361`) and `get_group`
(`services/console_prompts.py:359-375`), which `owns()` delegates to,
filter `user_sub` explicitly in the SQL `WHERE` clause in addition to
RLS — belt-and-suspenders, not load-bearing given RLS is now confirmed
enforcing, but worth keeping on record since it means `owns()`'s
answer would stay correct even in a hypothetical future regression of
RLS enforcement, once WU-2b gives it a caller.

## 6. Full local test suite (Rule 1 — Verification)

```
$ .venv/bin/pytest tests/ -q --cov=... --cov-fail-under=90
================= 5741 passed, 2 warnings in 618.05s (0:10:18) =================
Required test coverage of 90% reached. Total coverage: 99.04%

$ .venv/bin/python scripts/check-per-file-coverage.py
per-file coverage gate: PASS (lines >= 90%, branches >= 90%)

$ .venv/bin/python scripts/check-no-skipped-tests.py
[no-skip-check] No skipped tests in junit.xml. Good.
```

New/changed files' own coverage this run:
`services/console_acl/_ownership.py` 100% lines / 100% branches (29
stmts, 2 branches, unchanged this round). Per D22 (`coverage xml`
file-count non-determinism), only the gate's PASS/FAIL and this WU's
own files' numbers are cited — not the total checked-file count, which
is known to vary between runs from one fixed `.coverage`.

## 7. Single alembic head

```
$ .venv/bin/python -c "... script.get_heads() ..."
heads: ['742a8b743c94']
```

`742a8b743c94` (migration 032) chains onto `b3f8a1c6d9e2` (migration
031, the real head at dispatch on `main @ 12a6216`) — unchanged this
round; no migration content was modified, only test coverage was
extended.

## 8. Filed, not fixed (out of scope, noted per reviewer instruction)

`tests/test_rls_isolation.py:317-322` and
`tests/test_console_store_rls_postgres.py:162-165` still hand-write
`CREATE POLICY` DDL rather than executing the real migration files —
the same drift defect this WU's own `test_acl_ownership_rls.py` caught
in itself and fixed (see the module docstring's framing note). Recorded
as a follow-up for whichever WU next touches either file; not fixed
here (touching two pre-existing, unrelated RLS-proof files is a bigger
blast radius than this fix round should take on).
