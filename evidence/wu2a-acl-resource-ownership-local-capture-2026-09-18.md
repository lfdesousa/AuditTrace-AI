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

## 1. The §1 escalation — before/after, against a REAL Postgres

`tests/test_acl_ownership_rls.py` brings up a throwaway `postgres:16`
container (or uses `AUDITTRACE_TEST_POSTGRES_URL`), creates a
non-superuser `NOBYPASSRLS` LOGIN role (superusers bypass RLS
unconditionally — a superuser "proof" proves nothing), and runs the
REAL migration 031 (`TestBeforeFix`) or 031-then-032 (`TestAfterFix`)
via Alembic's own `Operations` bound to the live connection — not a
hand-copied DDL string. Full verbose run, this box, 2026-09-18:

```
$ .venv/bin/pytest tests/test_acl_ownership_rls.py -v --no-cov
collected 11 items

tests/test_acl_ownership_rls.py::TestBeforeFix::test_escalation_attacker_grants_self_access_on_victims_agent PASSED [  9%]
tests/test_acl_ownership_rls.py::TestBeforeFix::test_h1_any_user_deletes_any_public_grant PASSED [ 18%]
tests/test_acl_ownership_rls.py::TestBeforeFix::test_h2_any_user_takes_ownership_of_a_public_grant PASSED [ 27%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_escalation_insert_on_agent_is_blocked PASSED [ 36%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_escalation_insert_on_prompt_group_is_blocked PASSED [ 45%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_h1_delete_of_public_row_by_non_owner_is_blocked PASSED [ 54%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_h2_update_ownership_takeover_is_blocked PASSED [ 63%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_owner_can_still_grant_on_their_own_agent PASSED [ 72%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_owner_can_still_grant_on_their_own_prompt_group PASSED [ 81%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_unmapped_resource_type_is_refused_at_the_db_layer PASSED [ 90%]
tests/test_acl_ownership_rls.py::TestAfterFix::test_select_read_path_contract_is_unchanged PASSED [100%]

============================== 11 passed in 2.04s ==============================
```

**Before (`TestBeforeFix`, migration 031 alone — what `main` ships
today):** every attack SUCCEEDS —
`test_escalation_attacker_grants_self_access_on_victims_agent` inserts
a grant naming `agent-1` (owned by `owner-sub-0001`) with
`user_sub=attacker-sub-0002` and it is accepted; `test_h1_...` shows a
non-owner `DELETE` of a public row affects 1 row; `test_h2_...` shows a
non-owner `UPDATE ... SET user_sub=<attacker>` succeeds and the row's
`user_sub` column reads back as the attacker's sub.

**After (`TestAfterFix`, 031 then 032):** the SAME three attacks each
raise `sqlalchemy.exc.DBAPIError` matching `"row-level security"` (INSERT)
or affect 0 rows (`UPDATE`/`DELETE`); the SAME legitimate operations
(owner inserts a grant on their own `agent`, and separately on their own
`promptGroup`) still succeed; an unmapped `resource_type`
(`mcpServer`) is refused at the DB layer too; the SELECT contract
(public rows visible to everyone including with no GUC bound; private
`user`-principal rows gated to owner + named principal only) is
unchanged from migration 031.

## 2. Per-`resource_type` ownership table

| `resource_type` | Sovereign store | Owner can INSERT a grant | Non-owner INSERT blocked |
|---|---|---|---|
| `agent` | `console_agents` (migration 028) | YES (`test_owner_can_still_grant_on_their_own_agent`) | YES (`test_escalation_insert_on_agent_is_blocked`) |
| `promptGroup` | `console_prompt_groups` (migration 025) | YES (`test_owner_can_still_grant_on_their_own_prompt_group`) | YES (`test_escalation_insert_on_prompt_group_is_blocked`) |
| `mcpServer` | none yet | N/A — refused for EVERYONE incl. would-be owner (`test_unmapped_resource_type_is_refused_at_the_db_layer`) | YES (by construction — the OR-chain evaluates FALSE) |
| `remoteAgent` | none yet | N/A (same as `mcpServer`, not individually re-proven — same OR-chain branch) | — |
| `skill` | none yet | N/A (same as `mcpServer`) | — |
| `sharedLink` | none yet | N/A (same as `mcpServer`) | — |

Application-layer parity (`tests/test_console_acl_ownership.py`,
15 tests): `owns()` proven per resource_type individually — owner/
non-owner/nonexistent-id for `agent` and `promptGroup` each (6 tests),
`UnknownResourceTypeError` proven for each of the 4 unresolved types
individually plus a bogus type plus the error-message content (9
tests). `RESOLVED_RESOURCE_TYPES == {"agent", "promptGroup"}` and
`UNRESOLVED_RESOURCE_TYPES == {"mcpServer", "remoteAgent", "skill",
"sharedLink"}` pinned directly (`TestEnumerationIsCommitted`).

## 3. H-1 / H-2 outcome

Both hypotheses from §2 of the spec were **CONFIRMED, not refuted** —
constructed the exact SQL against a real, non-superuser Postgres role:

* **H-1** (any authenticated user may `DELETE` any PUBLIC ACL row) —
  CONFIRMED before the fix (`test_h1_any_user_deletes_any_public_grant`,
  `rowcount == 1`); CLOSED after (`test_h1_delete_of_public_row_by_non_owner_is_blocked`,
  `rowcount == 0`).
* **H-2** (any authenticated user may take ownership of a PUBLIC row
  via `UPDATE ... SET user_sub`) — CONFIRMED before the fix
  (`test_h2_any_user_takes_ownership_of_a_public_grant`, the row's
  `user_sub` column reads back as the attacker's sub); CLOSED after
  (`test_h2_update_ownership_takeover_is_blocked`, `rowcount == 0`,
  `user_sub` unchanged).

No real-Postgres path was unavailable on this box (Docker confirmed
up, ephemeral `postgres:16` brought up successfully) — no OPEN verdict
needed for either hypothesis.

## 4. Neuter table — one guard per run, `cmp`-verified byte-identical restore

Every neuter below was run individually against the byte-identical
restored files (verified with `cmp` before/after each round); no batch
neuter was used.

| # | Guard | Neuter | RED (what failed) | Restored |
|---|---|---|---|---|
| N1 | Migration 032 INSERT ownership subquery | Dropped `AND {_OWNERSHIP_SUBQUERY}` from the INSERT `WITH CHECK`, real migration file | `test_escalation_insert_on_agent_is_blocked`, `test_escalation_insert_on_prompt_group_is_blocked`, `test_unmapped_resource_type_is_refused_at_the_db_layer` (3 failed) | `cmp` clean |
| N2 | Migration 032 UPDATE/DELETE owner-only `USING` | Widened `USING` back to the broad owner-OR-principal-OR-public predicate | `test_h1_delete_of_public_row_by_non_owner_is_blocked` (rowcount 1 not 0); `test_h2_update_ownership_takeover_is_blocked` (still blocked, but by the WITH CHECK ownership subquery alone — RLS `ProgrammingError`, proving that guard is an independent, non-redundant second layer for H-2) | `cmp` clean |
| N3 | Migration 032 fail-closed for unmapped `resource_type` | Added `resource_type NOT IN ('agent','promptGroup') OR (...)` to the ownership subquery (simulated a "default-permit" bug) | ONLY `test_unmapped_resource_type_is_refused_at_the_db_layer` (1 failed) — all 7 sibling `TestAfterFix` tests stayed green, proving this guard is independently tested, not piggy-backing on another's pass | `cmp` clean |
| N4 | `owns()` fail-closed (`_ownership.py`) | `raise UnknownResourceTypeError(...)` → `return False` | All 6 `TestUnknownResourceTypeFailsClosed` tests (parametrized ×4 + 2) — `Failed: DID NOT RAISE UnknownResourceTypeError` | `cmp` clean |
| N5 | Per-`resource_type` dispatch, `promptGroup` arm | Removed `"promptGroup": _owns_prompt_group` from `_OWNERSHIP_RESOLVERS` | ONLY the 3 `TestOwnsPromptGroup` tests + 2 enumeration tests (5 failed) — all 3 `TestOwnsAgent` tests stayed green, proving a dead resolver arm for ONE type cannot hide behind another type's pass | `cmp` clean |
| N6 | Migration 032 SELECT read-path regression guard | Dropped the `principal_type = 'user' AND principal_id = ...` disjunct from `_SELECT_USING` | ONLY `test_select_read_path_contract_is_unchanged` (1 failed, private-row visibility to its named principal went from 1 to 0) | `cmp` clean |

**Declared-redundant claim, proven:** N2's result shows the UPDATE
policy's owner-only `USING` clause is, for H-2 SPECIFICALLY, redundant
with the WITH CHECK ownership subquery (neutering `USING` alone still
left H-2 blocked by the sibling guard) — proven by neutering it ALONE,
per the house standard, rather than asserted.

## 5. Full local test suite (Rule 1 — Verification)

```
$ .venv/bin/pytest tests/ -q --cov=... --cov-fail-under=90
================= 5735 passed, 2 warnings in 743.64s (0:12:23) =================
Required test coverage of 90% reached. Total coverage: 99.04%

$ .venv/bin/python scripts/check-per-file-coverage.py
per-file coverage gate: PASS (lines >= 90%, branches >= 90%)

$ .venv/bin/python scripts/check-no-skipped-tests.py
[no-skip-check] No skipped tests in junit.xml. Good.
```

New files' own coverage this run: `services/console_acl/_ownership.py`
100% lines / 100% branches (29 stmts, 2 branches); the 2 pre-existing
warnings are unrelated `_flush_pdf_manifest` coroutine warnings in
`tests/test_memory_routes.py`, not introduced by this WU. Per D22
(`coverage xml` file-count non-determinism), only the gate's PASS/FAIL
and this WU's own files' numbers are cited above — not the total
checked-file count, which is known to vary between runs from one fixed
`.coverage`.

## 6. Single alembic head

```
$ .venv/bin/python -c "... script.get_heads() ..."
heads: ['742a8b743c94']
```

`742a8b743c94` (migration 032) chains onto `b3f8a1c6d9e2` (migration
031, the real head at dispatch on `main @ 12a6216`).
