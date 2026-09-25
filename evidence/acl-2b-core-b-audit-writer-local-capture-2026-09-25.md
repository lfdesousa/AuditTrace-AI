# Evidence — Sovereign Authorization Layer, ACL 2b-core-B audit writer, local capture (2026-09-25, fix round 1)

**Scope of this evidence file.** ACL 2b-core-B
(`2026-09-25-SPEC-acl-2b-core-B-audit-writer-CONSOLIDATED-v2.md`,
`sha256:a78f1dba57c1b64fd3478bd2efe791525102d1c3a1b71aa75d9b5c13a7a18f39`).
This WU builds `services/console_acl/_audit.py` — the closed audit-writer
interface (`record`/`record_denial`) for every future sovereign ACL write
— and adds **no route**. Per the spec's own §11, ADR-049 Rule 2 is
unmeetable here by construction; this file satisfies Rule 1
(Verification) and gives the harness-level Rule-3-shaped capture, same
precedent as WU-2a's own routeless evidence file.

**This is fix round 1, after an independent-review REJECT with 7
findings, one (F1) a real security defect.** §0 below states plainly
what was wrong in round 1 and corrects it — no claim from the round-1
evidence file is silently restated; every corrected claim is marked.

## 0. What round 1 got wrong, and the fix (F1–F7)

**F1 (SECURITY).** The round-1 `_audit.py` took a `user_context`
parameter and only checked it for emptiness — it never cross-checked it
against `db.rls.current_user_id()`. A forged `UserContext` (e.g.
`dataclasses.replace(ctx, user_id="attacker-forged-subject")`) passed
through `record()`/`record_denial()` and the forged row landed, with
`user_id`/`granted_by` set to the attacker's subject, on aiosqlite. **This
is fixed**: both functions now call
`console_store.resolve_user_sub(user_context)`, which refuses
(`ConsoleStoreScopeError`) when `user_context.user_id` disagrees with the
bound RLS ContextVar. Proven by re-introducing the round-1 bug (reverting
`resolve_user_sub` to a bare emptiness check) and confirming
`TestForgedIdentityIsRefused`'s two tests go RED (below), then restoring
and confirming GREEN. The round-1 `test_neutering_the_derivation_flips_
the_value_assertion_red` (a monkeypatch of `_require_user_id` that then
asserted the EXACT value the monkeypatch had injected — self-fulfilling,
proved nothing) is REMOVED, not repaired; `TestForgedIdentityIsRefused`
replaces it with a guard that lands on a raised exception the neuter did
not itself set.

**F2.** Round 1's two N3 tests called `record_denial` AFTER the aborted
transaction's `async with` block had already exited (which rolls back on
the raised exception) — so "survives the rollback" held because no
overlap with the abort ever existed. **Fixed**: `record_denial` now runs
INSIDE the same `async with factory() as db:` block, immediately after
the failed `flush()`/`execute()`, WHILE `db`'s transaction is still open
and aborted. Falsifiability verified with a standalone script (not
committed — throwaway, same category as the pattern
`tests/console_store/test_base_stamping.py:42-48` already uses)
simulating the "tempting fix" (`record_denial` reusing the caller's
aborted session instead of opening its own):

```
db aborted as expected: ProgrammingError
BAD PATTERN correctly RED — shared session raised: DBAPIError (sqlalchemy.dialects.postgresql.asyncpg.Error) current transaction is aborted, commands ignored until end of transaction
GOOD PATTERN (real writer) succeeded, denial.id = 1
```

The BAD pattern (session shared with the caller) is refused outright by
Postgres itself; only the REAL writer (independent session, opened via
`get_postgres_factory()`) succeeds — exactly the overlap §5.2 requires.

**F3.** Nothing exercised `record_denial`'s own `trace_id` derivation —
N7 only ever covered `record()`. Neutering `record_denial`'s
`trace_id = current_trace_id_hex()` call site to `trace_id = None` left
every round-1 test GREEN. **Fixed**: `TestDenialTraceIdDerivation` (2
tests: real-span positive, NULL-trace negative) — confirmed this neuter
now fails exactly 1 test (`test_denial_trace_id_is_the_active_spans_id`),
25 others unaffected (verified below).

**F4.** Nothing pinned `EVENT_CLASS_ACL_AUTHZ`; neutering the string to
`"acl_authx"` stayed GREEN across the whole suite, and the round-1
docstring's "imported everywhere it is registered" claim was FALSE
(`memory_scan.py` carried an independent bare literal). **Fixed
structurally, not just with a test**: `EVENT_CLASS_ACL_AUTHZ` is now a
single named constant OWNED by `routes/memory_scan.py` (the canonical
closed-set module) and `_audit.py` IMPORTS it — verified
`_audit.EVENT_CLASS_ACL_AUTHZ is memory_scan.EVENT_CLASS_ACL_AUTHZ`.
`TestEventClassPinning` additionally pins the literal string value
directly (independent of the frozenset construction, so a typo in the
single source is still caught) — confirmed RED under the typo neuter
(below).

**F5.** Nothing exercised `session_id`; hard-coding
`session_id = None` in either writer function stayed GREEN. **Fixed**:
`TestSessionIdDerivation` (3 tests) — confirmed RED under the neuter for
BOTH `record()` and `record_denial()` (below).

**F6.** Every `content_hash` check ran with a NULL `trace_id`. **Fixed**:
`TestContentHashCoversANonNullTraceId` writes inside a real span and
calls `integrity.verify_content_hash(row)` directly on the persisted ORM
row (not a hand-recomputed guess, and not a throwaway script this time —
a real, committed, CI-gated test).

**F7.** This evidence file and the build record are rewritten, not
patched: the §10.1 sentence is now also verbatim IN the build record;
every "takes no parameter for identity" / "the structural rule protects
the aiosqlite path" claim is corrected to reflect the ACTUAL post-fix
mechanism (`user_context` IS a parameter, cross-checked, not trusted);
the per-guard table below states the REAL neuter mechanism for N1–N3
(never "n/a" where a neuter or a real-abort scenario was actually
exercised); N7's neuter is now the spec's literal `None` (matching the
reviewer's own run, "1 failed" — round 1 used a hard-coded 32-hex string
instead, which flips 2 tests, not 1 — both are shown below for the
record).

## 1. Per-guard neuter table (N1–N8 plus F1/F3/F4/F5/F6), `cmp`-verified restore

| Guard | Test(s) | Mechanism | RED confirmed | Restored |
|---|---|---|---|---|
| N1 — success row, full payload | `TestRecordSuccess` (aiosqlite) / `test_n1_...` (PG) | Direct behavioural assertion against a real write (no fault injected — this guard IS the happy path) | n/a by design | n/a |
| N2 — denial row, separately | `TestRecordDenial` (aiosqlite) / `test_n2_...` (PG) | Direct behavioural assertion against a real denial write | n/a by design | n/a |
| N3 — denial survives rollback | `TestDenialRowSurvivesRollback` (aiosqlite, CHECK abort, denial called WHILE the flush's transaction is open) / `test_n3_...` (PG, real RLS `WITH CHECK` abort, same overlap) | The abort itself is the fault; falsifiability proven separately via the "shared session" simulation (§0/F2 above) | YES (BAD-pattern simulation) | n/a (simulation was throwaway, not applied to the shipped module) |
| N4 — writer raises, fail-closed | `TestFailClosed` (aiosqlite+PG) + `test_n4_no_ambient_identity_...` (PG) | Monkeypatched `_content_hash`/`get_postgres_factory` to raise; separately, a swallowing `try/except` wrapped `record_denial`'s `db.commit()` | YES — `test_n4_no_ambient_identity_...` failed under the swallow-neuter | YES — `diff -q` byte-identical restore |
| N5 — append-only trigger, behavioural | `TestAppendOnlyTriggerNeuter` | `DROP TRIGGER interactions_append_only` (admin connection) | YES — UPDATE succeeded while dropped | YES — single `CREATE TRIGGER`; `pg_trigger` (tgname **+** `pg_get_triggerdef`, fix round 1 — name-only comparison replaced) compared to the pre-drop capture, exact match |
| N6 — §10.1 limitation pin | `TestCrossSubjectAuditReadIsImpossible` (exactly one test) | Not neutered — neutering a limitation-pin would fabricate a "control", contradicting its purpose | n/a by design | n/a |
| N7 — `record()`'s trace_id derivation | `TestTraceIdDerivation` | Replaced `current_trace_id_hex()`'s call site with **`None`** (spec's literal — round 1 used a hard-coded 32-hex string, corrected here) | YES — exactly 1 of 2 tests fails with `None` (the positive match test; the NULL-trap test is unaffected since it already expects `None`). The hard-coded-hex variant (round 1's neuter) fails BOTH tests — reproduced below for the record | YES — `diff -q` byte-identical restore |
| F3 — `record_denial()`'s trace_id derivation | `TestDenialTraceIdDerivation` | Replaced `record_denial`'s `current_trace_id_hex()` call site with `None` | YES — 1 of 2 tests fails (25 of 26 total file tests still pass) | YES — `diff -q` byte-identical restore |
| F1/N8 — forged `UserContext` refused | `TestForgedIdentityIsRefused` (aiosqlite) / `test_record_denial_refuses_a_forged_user_context_when_ambient_identity_bound` + `test_record_denial_with_unbound_contextvar_is_refused_by_rls` (PG) | Reverted `resolve_user_sub` calls to the round-1 bare-emptiness check (both call sites) | YES — both aiosqlite forged-context tests fail (`DID NOT RAISE ConsoleStoreScopeError`) | YES — `diff -q` byte-identical restore |
| F4 — `EVENT_CLASS_ACL_AUTHZ` pinning | `TestEventClassPinning` | Changed `memory_scan.py`'s constant to `"acl_authx"` | YES — `test_literal_value_is_exactly_acl_authz` fails | YES — `diff -q` byte-identical restore |
| F5 — `session_id` derivation | `TestSessionIdDerivation` | Hard-coded `session_id = None` at both call sites | YES — both stamping tests fail | YES — `diff -q` byte-identical restore |
| F6 — content_hash, non-null trace_id | `TestContentHashCoversANonNullTraceId` | Covered by the SAME N7/F3 neuters above (trace_id excluded from the hash while present on the row would diverge) | Indirectly covered — not neutered separately | n/a |

All neuters against `src/audittrace/services/console_acl/_audit.py` and
`src/audittrace/routes/memory_scan.py` were applied via `cp` to a
pre-neuter backup, edited in place, run, then restored via `cp` from the
backup with `diff -q` confirming byte-identical restoration before the
next neuter or before commit.

## 2. F1's neuter in detail (the security-defect proof)

Reverting BOTH `resolve_user_sub(user_context)` call sites to
```python
if not user_context.user_id:
    raise ValueError("empty user_id")
user_id = user_context.user_id  # skip the ContextVar cross-check
```
and running `TestForgedIdentityIsRefused`:
```
FAILED tests/test_console_acl_audit_writer.py::TestForgedIdentityIsRefused::test_record_refuses_a_forged_user_context - Failed: DID NOT RAISE ConsoleStoreScopeError
FAILED tests/test_console_acl_audit_writer.py::TestForgedIdentityIsRefused::test_record_denial_refuses_a_forged_user_context - Failed: DID NOT RAISE ConsoleStoreScopeError
2 failed, 1 passed in 0.38s
```
Restored (`diff -q` against the pre-neuter backup: identical), full
26-test aiosqlite file re-run GREEN.

## 3. N7's two neuter variants (why round 1 reported "2 failed" against the reviewer's "1")

Spec's literal `None`, `record()` only:
```
FAILED ...TestTraceIdDerivation::test_trace_id_is_the_active_spans_id_and_matches_on_reconstruction
FAILED ...TestContentHashCoversANonNullTraceId::test_content_hash_verifies_with_a_real_non_null_trace_id
2 failed, 1 passed  # (TestTraceIdDerivation + the new F6 test, run together)
```
Running `TestTraceIdDerivation` ALONE (round 1's scope, before F6's test
existed) with the `None` neuter: **1 failed** (the positive match test),
**1 passed** (the NULL-trap test — unaffected, since it already expects
`None`) — matching the reviewer's own observation exactly. Round 1's
neuter used a hard-coded 32-hex string instead of `None`, which changes
the value in BOTH the active-span AND no-span cases, so BOTH
`TestTraceIdDerivation` tests failed — a stronger neuter than the spec
asked for, but not the one the spec named, hence the discrepancy.

## 4. Full verbose runs, this box, 2026-09-25, unmodified (post-fix) code

```
$ .venv/bin/pytest tests/test_console_acl_audit_writer.py -v --no-cov
collected 26 items
... (26 passed — TestRecordSuccess x2, TestRecordDenial x3,
     TestDenialRowSurvivesRollback x1, TestFailClosed x3,
     TestTraceIdDerivation x2, TestDenialTraceIdDerivation x2,
     TestEventClassPinning x3, TestSessionIdDerivation x3,
     TestContentHashCoversANonNullTraceId x1,
     TestForgedIdentityIsRefused x3, TestUserIdAndGrantedByDerivation x3)
============================== 26 passed in 2.2s ===============================

$ .venv/bin/pytest tests/test_acl_ownership_rls.py -v --no-cov
collected 28 items
... (17 pre-existing WU-2a tests UNCHANGED + 11 ACL-2b-core-B tests, all PASSED)
============================== 28 passed in 8.65s ==============================
```

`git diff main -- tests/test_acl_ownership_rls.py`: 787 insertions, **0
deletions** (the §2 frozen-exception requirement, re-verified after fix
round 1). `git diff main -- src/audittrace/migrations/versions/032_....py`:
empty (migration 032 untouched).

## 5. Single alembic head (§11's exact requirement)

```
$ .venv/bin/python -m alembic -c alembic.ini heads
742a8b743c94 (head)
```

## 6. §10.1's exact sentence (verbatim, per the spec's instruction — also in the build record, per F7)

> A denial row stamped with the attacker's user_id is readable by the
> attacker and invisible to the resource owner or an auditor.

Pinned, never "proven as a control", by
`TestCrossSubjectAuditReadIsImpossible::
test_this_pins_a_limitation_subject_b_sees_zero_of_subject_as_denial_rows`
(exactly one test, real Postgres).

## 7. §10.2 — the missing administrative plane, as TWO privileges

1. **Cross-subject READ on `interactions`** — an auditor cannot read
   another subject's ACL audit rows (§10.1, pinned above).
2. **Cross-subject UPDATE/expire on `console_acl_entries`** — an admin
   cannot revoke another subject's grant (§6's forward ruling for
   2b-core-A, D-S).

Both are the same underlying fact (per-subject RLS is the only boundary)
seen from two different blast radii. Belongs to **no current work
unit** — needs its own backlog item, placed by the operator; not closed
by this record.

## 8. Migration 016 — provisional-live note

016's presence on the LIVE (deployed) database is **unconfirmed** —
committed ≠ live. This build only proves the trigger behaviourally
against a throwaway, freshly-migrated Postgres schema. WU-6's gate is
where the live `pg_trigger` confirmation belongs.

## 9. Corrected claims (F7)

Round 1's module docstring said this module "takes NO ``trace_id``, NO
``session_id``, NO ``user_id``, NO ``granted_by``, NO ``user_sub``
parameter" — **misleading**: it DOES take `user_context: UserContext`,
from which `user_id`/`granted_by` derive. The corrected claim (now in
the docstring): the module takes no `trace_id`/`session_id`/
`granted_by`/`user_sub` parameter, and the `user_context` it DOES take
is cross-checked against the ambient RLS identity via
`resolve_user_sub`, never trusted at face value. Round 1's aiosqlite
"forged" test docstring claimed "the structural rule (no parameter on
the writer's own API) is what protects this path" — **false as an
account of protection against a forged UserContext THROUGH the writer**
(F1 proved the opposite); corrected to state plainly that the raw-INSERT
test answers a different, narrower question (bypassing the writer
entirely), while `TestForgedIdentityIsRefused` is what actually protects
the writer's own API surface.
