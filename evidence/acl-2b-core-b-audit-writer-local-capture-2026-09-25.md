# Evidence — Sovereign Authorization Layer, ACL 2b-core-B audit writer, local capture (2026-09-25/26, fix round 3)

**Scope of this evidence file.** ACL 2b-core-B
(`2026-09-25-SPEC-acl-2b-core-B-audit-writer-CONSOLIDATED-v2.md`,
`sha256:a78f1dba57c1b64fd3478bd2efe791525102d1c3a1b71aa75d9b5c13a7a18f39`).
This WU builds `services/console_acl/_audit.py` — the closed audit-writer
interface (`record`/`record_denial`) for every future sovereign ACL write
— and adds **no route**. Per the spec's own §11, ADR-049 Rule 2 is
unmeetable here by construction; this file satisfies Rule 1
(Verification) and gives the harness-level Rule-3-shaped capture, same
precedent as WU-2a's own routeless evidence file.

**This is fix round 3, after a ONE-BLOCKER independent-review REJECT.**
Round 1 fixed 7 findings including a real security defect (F1). Round 2
fixed 3 narrower findings (B1/B2/B3), all "the code is right and the
claim about it is wrong" — and B1/B3 and every non-blocking item from
that round were accepted as closed, verified independently by a 23-row
neuter table in the reviewer's own venv. **Round 2's OWN replacement
claim for B2 was ALSO wrong** — see §0c. §0a is round 1's original
history; §0b is round 2's; §0c (this round) corrects §0b's own mistake.
Nothing is silently edited: each round's wrong claim stays visible,
annotated, next to its correction — this project's append-only-decision-
log discipline applied to a build record.

## 0a. What round 1 got wrong, and the fix (F1–F7)

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

## 0b. What round 2 got wrong, and the fix (B1/B2/B3) — "the code is right and the claim about it is wrong"

**B1 (F6, not actually closed in round 1).** Round 1's F6 fix covered
`record()` only. This evidence file's round-1 §1 table row for F6 said
`record_denial()` was "indirectly covered by the SAME N7/F3 neuters" —
**FALSE**: those neuters change the `trace_id` VALUE; they assert
nothing about what the hash COVERS. Forcing `record_denial`'s
`content_hash` to be computed with `trace_id` forced to `None` WHILE the
persisted row kept its real (non-null) `trace_id` left the whole suite
GREEN. **Fixed**: `TestContentHashCoversANonNullTraceId::
test_denial_content_hash_verifies_with_a_real_non_null_trace_id`
(new, aiosqlite). Verified live: applying that exact neuter (a
`_hash_fields = {**fields, "trace_id": None}` substitution feeding
`_content_hash`, row keeping the real `trace_id`) failed exactly this
one new test, 26 others in the file unaffected; restored, `diff -q`
byte-identical.

**B2 (F2, the aiosqlite claim was false as written).** ⚠ **This
paragraph's OWN mechanism claim was ALSO wrong — see §0c below for the
correction. Kept verbatim, not silently edited, per this project's
append-only-decision-log discipline: a correction is a new claim, and
the wrong one stays visible next to it.** Round 1's N3 aiosqlite test
comment said *"`db`'s transaction is STILL OPEN here — the failed flush
has not been rolled back yet."* This was WRONG about the DATABASE:
aiosqlite's DBAPI driver issues `BEGIN`/`ROLLBACK` around the failed
`flush()` itself, so the DB-level transaction has ALREADY rolled back by
the time `record_denial` runs; only the SQLAlchemy ORM `Session` object
is still "pending rollback" from the ORM's own point of view. Genuine
DB-level transaction overlap is impossible to reproduce on SQLite at
all — holding a transaction open at the DB level (a Core `execute` a
second writer must wait behind) makes the second write hit `database is
locked`, because SQLite cannot have two writers overlap, aborted or not.
**The guard itself was never wrong** — both N3 aiosqlite assertions
still go RED if `record_denial` is forced to write via the caller's own
session (proven: see §3b below). **Fixed (round 2, since further
corrected — §0c)**: the test is renamed
(`test_denial_row_survives_while_the_orm_session_is_still_pending_
rollback`), its docstring and the module docstring now state plainly
that aiosqlite proves ORM-SESSION-level independence only; genuine
DB-level overlap is proven on Postgres ALONE, where an aborted
transaction is NOT auto-rolled-back by the driver — the client must
issue `ROLLBACK` explicitly, so the transaction genuinely stays open at
the DB level.

**B3 (F4, the identity test proved nothing).** CPython interns
identifier-like string literals. Restoring the EXACT round-1 defect — a
local `EVENT_CLASS_ACL_AUTHZ = "acl_authz"` copy re-typed directly in
`_audit.py`, instead of the import — left
`_audit.EVENT_CLASS_ACL_AUTHZ is memory_scan.EVENT_CLASS_ACL_AUTHZ`
**`True`** and the whole suite GREEN, because CPython's string interning
makes the local copy and the imported name reference the SAME cached
string object for this exact literal. `test_audit_module_imports_the_
canonical_constant_not_a_copy` proved nothing about imports — no runtime
`is`-check on a string literal can distinguish "imported" from
"re-typed" in CPython. **Fixed**: that test is REMOVED, not repaired.
The structural fix (`_audit.py` importing `EVENT_CLASS_ACL_AUTHZ` from
`routes/memory_scan.py` rather than defining its own copy) stands — it
is still the right thing to do, and a genuinely DRIFTED VALUE (e.g. a
typo) is still caught by `test_literal_value_is_exactly_acl_authz` and
`TestEventClassValues` (`tests/test_memory_routes.py`) — but "does
`_audit.py` import rather than copy" is a code-review property, not a
test property, for this specific kind of literal.

**Non-blocking, folded in:**

- Forged-identity docstrings (module docstring, `record()`,
  `record_denial()`) now say "WHEN THE AMBIENT CONTEXTVAR IS BOUND" —
  unbound (non-request code only) the forged row's cross-check is a
  documented no-op (`resolve_user_sub`'s own design: "the
  token-resolved `user_id` governs" when unbound), not a defect, and
  Postgres RLS is the layer that refuses a mismatch in that case
  instead.
- The Postgres "unbound ContextVar" test previously lived in
  `TestForgedUserIdRefusedOnPostgres` as if it were forged-identity
  specific. It is not: with the ContextVar unbound, RLS refuses ANY
  subject, forged or legitimate — there is nothing forgery-specific
  about that refusal. Moved to `TestAuditWriterRealPostgres` and renamed
  `test_n4_unbound_ambient_identity_refuses_any_subject_forged_or_not`,
  named for what it actually proves (the same §5.4 fail-closed path as
  its sibling N4 test).
- The build record's gates line no longer cites `_audit.py`'s coverage
  as a count ("100%/100%") beside the D22 note — PASS/FAIL only, per
  D22, is now the ONLY thing cited for coverage.
- The F2 falsifiability proof is now a COMMITTED, real pytest test
  (`test_n3_a_shared_session_would_fail_where_the_real_writer_succeeds`,
  `tests/test_acl_ownership_rls.py::TestAuditWriterRealPostgres`) rather
  than an uncommitted throwaway script — see §3b. ⚠ **Round 3 softens
  this to "demonstrated"** — see §0c.

## 0c. Fix round 3 — round 2's B2 correction was ALSO wrong ("a correction is a new claim")

Round 2 replaced round 1's false claim ("the transaction is STILL OPEN")
with ANOTHER false claim: *"aiosqlite's DBAPI **driver** issues
`BEGIN`/`ROLLBACK`"*, contrasted with *"Postgres does NOT
auto-rollback."* Both halves were wrong, and the build record cited "an
engine-event trace" as showing the driver did it — **that trace never
showed that.**

**The corrected mechanism, INSTRUMENTED this round, not asserted:**

> **SQLAlchemy rolls back the connection when an ORM `flush()` fails, on
> ANY database.** This is not aiosqlite-specific and it is not the DBAPI
> driver doing it — it is SQLAlchemy's own flush-error handling. The
> REAL difference between the two N3 tests was never SQLite-vs-Postgres;
> it is **HOW EACH ONE ABORTS**: the aiosqlite test aborts via an ORM
> `flush()`, so only the ORM `Session` object is left "pending
> rollback" (the connection itself was already rolled back by
> SQLAlchemy). The Postgres `test_n3_denial_row_survives_an_rls_aborted_
> transaction` aborts via a Core-level `execute(text(...))` with no
> flush, which leaves the transaction genuinely OPEN AND ABORTED at the
> database level (SQLAlchemy has nothing to roll back on its own,
> because no flush ran).

**Instrumented, not asserted, in two places:**

1. `tests/test_console_acl_audit_writer.py::TestDenialRowSurvivesRollback::
   test_denial_row_survives_while_the_orm_session_is_still_pending_rollback`
   now attaches a connection-level `event.listen(engine, "rollback",
   ...)` listener BEFORE the failed `flush()`, and asserts the event
   fired — proving, not asserting in prose, that SQLAlchemy rolled back
   the connection.
2. NEW: `tests/test_acl_ownership_rls.py::TestAuditWriterRealPostgres::
   test_n3_variant_aborting_via_orm_flush_the_shape_2b_core_a_will_have`
   — the SAME instrumentation, on the real Postgres harness, aborting
   via an ORM `flush()` (the shape 2b-core-A's future write methods will
   actually use, unlike the Core-`execute` abort the pre-existing N3 PG
   test uses). Run, this box, 2026-09-25:

```
$ .venv/bin/pytest tests/test_acl_ownership_rls.py::TestAuditWriterRealPostgres::test_n3_variant_aborting_via_orm_flush_the_shape_2b_core_a_will_have -v --no-cov
tests/test_acl_ownership_rls.py::TestAuditWriterRealPostgres::test_n3_variant_aborting_via_orm_flush_the_shape_2b_core_a_will_have PASSED
============================== 1 passed in 1.55s ===============================
```

**The finding, stated plainly (a finding for 2b-core-A's own build, not
a defect in this WU):** the `rollback` event fires on Postgres too — a
failed ORM `flush()` rolls back the connection immediately, on Postgres
exactly as on aiosqlite. **Consequence:** a future 2b-core-A write
method that flushes an ACL-entry INSERT and has it refused will find the
transaction ALREADY CLOSED by the time it calls a denial writer — the
genuinely-open-and-aborted scenario the pre-existing PG N3 test proves
survival under is the Core-`execute` abort shape, not the ORM-`flush`
shape 2b-core-A will actually have. 2b-core-A's own build should verify
`record_denial`'s independent-session behaviour against ITS actual
abort shape (flush-based), not assume the Core-`execute` proof
transfers.

## 1. Per-guard neuter table (N1–N8 plus F1/F3/F4/F5/F6), `cmp`-verified restore

| Guard | Test(s) | Mechanism | RED confirmed | Restored |
|---|---|---|---|---|
| N1 — success row, full payload | `TestRecordSuccess` (aiosqlite) / `test_n1_...` (PG) | Direct behavioural assertion against a real write (no fault injected — this guard IS the happy path) | n/a by design | n/a |
| N2 — denial row, separately | `TestRecordDenial` (aiosqlite) / `test_n2_...` (PG) | Direct behavioural assertion against a real denial write | n/a by design | n/a |
| N3 — denial survives rollback | `TestDenialRowSurvivesRollback` (aiosqlite — flush-based abort, ORM-session independence) / `test_n3_denial_row_survives_an_rls_aborted_transaction` (PG — Core-`execute`-based abort, genuine DB-level overlap) / `test_n3_variant_aborting_via_orm_flush_...` (PG — flush-based abort, the 2b-core-A shape, §0c) | The abort SHAPE, not the dialect, is what differs: an ORM `flush()` failure makes SQLAlchemy roll back the connection immediately, on EITHER dialect (instrumented via a `"rollback"` connection event in both the aiosqlite and the new PG-flush test); a Core `execute()` failure with no flush leaves the transaction genuinely open-and-aborted at the DB level (the pre-existing PG test) | YES — the connection-level rollback event fires and is asserted in both flush-based tests; the shared-session simulation (§3b) DEMONSTRATES (not "proves" — see §3b's own softened wording) the independent-session requirement | n/a (no source file is modified by any of these tests) |
| N4 — writer raises, fail-closed | `TestFailClosed` (aiosqlite+PG) + `test_n4_no_ambient_identity_...` (PG) | Monkeypatched `_content_hash`/`get_postgres_factory` to raise; separately, a swallowing `try/except` wrapped `record_denial`'s `db.commit()` | YES — `test_n4_no_ambient_identity_...` failed under the swallow-neuter | YES — `diff -q` byte-identical restore |
| N5 — append-only trigger, behavioural | `TestAppendOnlyTriggerNeuter` | `DROP TRIGGER interactions_append_only` (admin connection) | YES — UPDATE succeeded while dropped | YES — single `CREATE TRIGGER`; `pg_trigger` (tgname **+** `pg_get_triggerdef`, fix round 1 — name-only comparison replaced) compared to the pre-drop capture, exact match |
| N6 — §10.1 limitation pin | `TestCrossSubjectAuditReadIsImpossible` (exactly one test) | Not neutered — neutering a limitation-pin would fabricate a "control", contradicting its purpose | n/a by design | n/a |
| N7 — `record()`'s trace_id derivation | `TestTraceIdDerivation` | Replaced `current_trace_id_hex()`'s call site with **`None`** (spec's literal — round 1 used a hard-coded 32-hex string, corrected here) | YES — exactly 1 of 2 tests fails with `None` (the positive match test; the NULL-trap test is unaffected since it already expects `None`). The hard-coded-hex variant (round 1's neuter) fails BOTH tests — reproduced below for the record | YES — `diff -q` byte-identical restore |
| F3 — `record_denial()`'s trace_id derivation | `TestDenialTraceIdDerivation` | Replaced `record_denial`'s `current_trace_id_hex()` call site with `None` | YES — 1 of 2 tests fails (25 of 26 total file tests still pass) | YES — `diff -q` byte-identical restore |
| F1/N8 — forged `UserContext` refused (bound ContextVar — the forgery-specific guard) | `TestForgedIdentityIsRefused` (aiosqlite) / `test_record_denial_refuses_a_forged_user_context_when_ambient_identity_bound` (PG) | Reverted `resolve_user_sub` calls to the round-1 bare-emptiness check (both call sites) | YES — both aiosqlite forged-context tests fail (`DID NOT RAISE ConsoleStoreScopeError`) | YES — `diff -q` byte-identical restore |
| N4 (not forgery-specific — round 2 correction, B/non-blocking) — unbound ContextVar refuses ANY subject | `test_n4_unbound_ambient_identity_refuses_any_subject_forged_or_not` (PG, `TestAuditWriterRealPostgres` — moved from `TestForgedUserIdRefusedOnPostgres`, renamed) | n/a — direct behavioural assertion; the same §5.4 fail-closed path as `test_n4_no_ambient_identity_...` | n/a by design | n/a |
| F4 — `EVENT_CLASS_ACL_AUTHZ` pinning | `TestEventClassPinning` | Changed `memory_scan.py`'s constant to `"acl_authx"` | YES — `test_literal_value_is_exactly_acl_authz` fails | YES — `diff -q` byte-identical restore |
| F5 — `session_id` derivation | `TestSessionIdDerivation` | Hard-coded `session_id = None` at both call sites | YES — both stamping tests fail | YES — `diff -q` byte-identical restore |
| F6/B1 — content_hash, non-null trace_id, BOTH `record()` and `record_denial()` | `TestContentHashCoversANonNullTraceId` (2 tests, round 2 adds the `record_denial` half) | `_hash_fields = {**fields, "trace_id": None}` fed to `_content_hash` while the row keeps its real `trace_id` | YES — the `record_denial` variant fails exactly the new test, 26 others unaffected | YES — `diff -q` byte-identical restore |

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

## 3b. F2's falsifiability demonstration, now committed (B2 non-blocking fix, softened per round-3 review)

`tests/test_acl_ownership_rls.py::TestAuditWriterRealPostgres::
test_n3_a_shared_session_would_fail_where_the_real_writer_succeeds`
replaces the round-1/round-2 uncommitted throwaway script. It provokes a
real RLS abort (mismatched `user_sub` INSERT into `console_acl_entries`),
then — on the SAME still-aborted session — attempts a raw INSERT
simulating what a "shared session" `record_denial` would do; this raises
Postgres's own `current transaction is aborted` error. It then repeats
the identical abort on a SEPARATE session and calls the REAL, unmodified
`record_denial`, which succeeds. Run, this box, 2026-09-25:

```
$ .venv/bin/pytest tests/test_acl_ownership_rls.py::TestAuditWriterRealPostgres::test_n3_a_shared_session_would_fail_where_the_real_writer_succeeds -v --no-cov
tests/test_acl_ownership_rls.py::TestAuditWriterRealPostgres::test_n3_a_shared_session_would_fail_where_the_real_writer_succeeds PASSED
============================== 1 passed in 1.54s ===============================
```

**Round 3 correction (non-blocking, folded in): this test DEMONSTRATES
the property; it does not "prove" or act as a regression "guard" the way
round 2's wording claimed.** Its "bad" half is raw SQL asserting a
Postgres PROPERTY (a shared, already-aborted session cannot be used) —
it never calls `_audit.record_denial` at all, so no future regression in
the WRITER can turn this half red. Its "good" half calls the real writer
under the same abort, but that is not new coverage:
`test_n3_denial_row_survives_an_rls_aborted_transaction` already proves
the writer succeeds under a Core-`execute` abort. What WOULD actually
redden all three N3-family tests is neutering the writer itself (making
`record_denial` reuse the caller's session) — which is exactly what
§2/§0a's F1 neuter methodology and the `TestFailClosed` monkeypatches
already do for OTHER guards, and which this specific test does not do.
This test is retained as a permanent, readable DEMONSTRATION of the
underlying Postgres behaviour the guard depends on — valuable for a
future reader, but not itself a redundant-with-`test_n3` regression
guard.

## 4. Full verbose runs, this box, 2026-09-25, unmodified (post-fix-round-3) code

```
$ .venv/bin/pytest tests/test_console_acl_audit_writer.py -v --no-cov
collected 26 items
... (26 passed — TestRecordSuccess x2, TestRecordDenial x3,
     TestDenialRowSurvivesRollback x1 (instrumented with a
     connection-level rollback-event assertion, round 3),
     TestFailClosed x3, TestTraceIdDerivation x2,
     TestDenialTraceIdDerivation x2, TestEventClassPinning x2
     (identity test REMOVED, B3), TestSessionIdDerivation x3,
     TestContentHashCoversANonNullTraceId x2 (record_denial half
     added, B1), TestForgedIdentityIsRefused x3,
     TestUserIdAndGrantedByDerivation x3)
============================== 26 passed in 2.47s ==============================

$ .venv/bin/pytest tests/test_acl_ownership_rls.py -v --no-cov
collected 30 items
... (17 pre-existing WU-2a tests UNCHANGED + 13 ACL-2b-core-B tests —
     round 3 adds test_n3_variant_aborting_via_orm_flush_the_shape_2b_
     core_a_will_have, all PASSED)
============================== 30 passed in 3.93s ==============================
```

`git diff main -- tests/test_acl_ownership_rls.py`: 1024 insertions, **0
deletions** (the §2 frozen-exception requirement, re-verified after fix
round 3 — was 911/0 after round 2). `git diff main -- src/audittrace/
migrations/versions/032_....py`: empty (migration 032 untouched).

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

**Round 2 additions to this section.** Round 1's claim that a forged
`user_context` is refused "before any session opens" carried no
qualifier — corrected (module docstring, `record()`, `record_denial()`)
to state this holds WHEN THE AMBIENT CONTEXTVAR IS BOUND (every real
request path); unbound (non-request code only), the cross-check is a
documented no-op and Postgres RLS is the layer that refuses instead.
Round 1's N3 aiosqlite comment ("`db`'s transaction is STILL OPEN") is
corrected per B2 above. Round 1's F4 "proof" via object identity
(`_audit.EVENT_CLASS_ACL_AUTHZ is memory_scan.EVENT_CLASS_ACL_AUTHZ`) is
retracted per B3 above — CPython string interning makes that check
incapable of proving what it claimed.
