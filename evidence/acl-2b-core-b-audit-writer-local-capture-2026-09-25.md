# Evidence — Sovereign Authorization Layer, ACL 2b-core-B audit writer, local capture (2026-09-25)

**Scope of this evidence file.** ACL 2b-core-B
(`2026-09-25-SPEC-acl-2b-core-B-audit-writer-CONSOLIDATED-v2.md`,
`sha256:a78f1dba57c1b64fd3478bd2efe791525102d1c3a1b71aa75d9b5c13a7a18f39`).
This WU builds `services/console_acl/_audit.py` — the closed audit-writer
interface (`record`/`record_denial`) for every future sovereign ACL write
— and adds **no route**. Per the spec's own §11: *"ADR-049 Rule 2 is
UNMEETABLE here by construction — no route, so no public-API/scoped-JWT
exercise against a deployed image. Reconstruction is harness-level; the
live chain is 2c's acceptance and WU-6's gate. The builder must NOT add a
route to satisfy it; a reviewer must NOT demand live evidence from a
routeless WU."* This file therefore satisfies ADR-049 Rule 1
(Verification) in full and gives the harness-level Rule-3-shaped capture
this WU's own reconstruction discipline (§4) calls for — mirroring the
precedent WU-2a set (`evidence/wu2a-acl-resource-ownership-local-capture-2026-09-18.md`)
for a routeless WU. It does **NOT** satisfy Rule 2 (Validation through a
deployed image) — there is nothing to exercise through a public route
until 2c/2b-core-A land.

## 1. §4.3 — the NULL-trace trap, a REAL captured trace_id, and a verified content_hash

Ad-hoc capture script run against this WU's own code, `.venv/bin/python`,
this box, 2026-09-25 (not committed — throwaway, matches the pattern
`tests/console_store/test_base_stamping.py:42-48` already uses):

```
captured_span_trace_id  = 8b958e0ec98c365aa97a0eed0cd261b7
stored_row_trace_id     = 8b958e0ec98c365aa97a0eed0cd261b7
match                   = True
event_class             = acl_authz
content_hash            = 034c03c84fbade736d40fbf33e8307b4905a9712abc5696d7960e7e564459e92
answer                  = {"acl_entry_ids": ["acl-evidence-1"], "bits": {"DELETE": false, "EDIT": true, "SHARE": false, "VIEW": true}, "expired_at_ms": null, "expired_ids": [], "granted_by": "00000000-0000-0000-0000-000000000001", "perm_bits": 3, "visible_matched_count": 1}
content_hash_verifies   = True
```

`captured_span_trace_id` is a REAL OpenTelemetry span id (32-char hex,
`TracerProvider().get_tracer(...).start_as_current_span(...)`) captured
BEFORE the write, asserted valid, then matched against the persisted
row's `trace_id` — `match = True` is the §4.1 reconstruction proof
non-vacuously exercised once outside the test harness too.
`content_hash_verifies = True` is `integrity.verify_content_hash(row)`
called on the live ORM row (not a hand-recomputed guess) — the §3.1
payload IS hash-covered. `tests/test_console_acl_audit_writer.py::
TestTraceIdDerivation::test_null_trace_is_never_a_valid_match_the_null_trace_trap`
is the companion negative proof: with NO active span, `trace_id` is
`None`, and the §4.3-required ordering (assert 32-hex BEFORE any match)
explicitly rejects that `None` rather than letting a naive `None == None`
comparison pass as "reconstructed".

## 2. Full verbose run — aiosqlite param, this box, 2026-09-25, unmodified code

```
$ .venv/bin/pytest tests/test_console_acl_audit_writer.py -v --no-cov
collected 15 items

tests/test_console_acl_audit_writer.py::TestRecordSuccess::test_record_persists_a_full_payload_row PASSED [  6%]
tests/test_console_acl_audit_writer.py::TestRecordSuccess::test_record_does_not_commit_the_callers_session PASSED [ 13%]
tests/test_console_acl_audit_writer.py::TestRecordDenial::test_record_denial_persists_a_failed_row PASSED [ 20%]
tests/test_console_acl_audit_writer.py::TestRecordDenial::test_unknown_failure_class_is_refused PASSED [ 26%]
tests/test_console_acl_audit_writer.py::TestRecordDenial::test_closed_set_has_exactly_the_five_pinned_values PASSED [ 33%]
tests/test_console_acl_audit_writer.py::TestDenialRowSurvivesRollback::test_denial_row_survives_the_aborted_transaction_it_documents PASSED [ 40%]
tests/test_console_acl_audit_writer.py::TestFailClosed::test_record_propagates_a_broken_content_hash PASSED [ 46%]
tests/test_console_acl_audit_writer.py::TestFailClosed::test_record_denial_propagates_a_broken_content_hash PASSED [ 53%]
tests/test_console_acl_audit_writer.py::TestFailClosed::test_record_denial_propagates_a_broken_postgres_factory PASSED [ 60%]
tests/test_console_acl_audit_writer.py::TestTraceIdDerivation::test_trace_id_is_the_active_spans_id_and_matches_on_reconstruction PASSED [ 66%]
tests/test_console_acl_audit_writer.py::TestTraceIdDerivation::test_null_trace_is_never_a_valid_match_the_null_trace_trap PASSED [ 73%]
tests/test_console_acl_audit_writer.py::TestUserIdAndGrantedByDerivation::test_user_id_and_granted_by_equal_the_context_subject PASSED [ 80%]
tests/test_console_acl_audit_writer.py::TestUserIdAndGrantedByDerivation::test_neutering_the_derivation_flips_the_value_assertion_red PASSED [ 86%]
tests/test_console_acl_audit_writer.py::TestUserIdAndGrantedByDerivation::test_empty_user_id_is_refused PASSED [ 93%]
tests/test_console_acl_audit_writer.py::TestUserIdAndGrantedByDerivation::test_a_forged_user_id_bypassing_the_writer_succeeds_silently_on_aiosqlite PASSED [100%]

============================== 15 passed in 6.53s ==============================
```

## 3. Full verbose run — real Postgres (NOBYPASSRLS role, ephemeral `postgres:16`), this box, 2026-09-25

```
$ .venv/bin/pytest tests/test_acl_ownership_rls.py -v --no-cov
collected 26 items

tests/test_acl_ownership_rls.py::TestBeforeFix::... (17 WU-2a tests, UNCHANGED, all PASSED)
tests/test_acl_ownership_rls.py::TestAuditWriterRealPostgres::test_n1_successful_write_produces_a_full_payload_row PASSED [ 69%]
tests/test_acl_ownership_rls.py::TestAuditWriterRealPostgres::test_n2_denied_write_produces_a_failed_row PASSED [ 73%]
tests/test_acl_ownership_rls.py::TestAuditWriterRealPostgres::test_n3_denial_row_survives_an_rls_aborted_transaction PASSED [ 76%]
tests/test_acl_ownership_rls.py::TestAuditWriterRealPostgres::test_n4_writer_raises_on_a_broken_audit_write PASSED [ 80%]
tests/test_acl_ownership_rls.py::TestAuditWriterRealPostgres::test_n4_no_ambient_identity_gets_with_check_refusal_which_propagates PASSED [ 84%]
tests/test_acl_ownership_rls.py::TestAppendOnlyTriggerNeuter::test_trigger_blocks_update_and_delete_as_nobypassrls PASSED [ 88%]
tests/test_acl_ownership_rls.py::TestAppendOnlyTriggerNeuter::test_neutering_the_trigger_lets_the_mutation_succeed_then_restore PASSED [ 92%]
tests/test_acl_ownership_rls.py::TestCrossSubjectAuditReadIsImpossible::test_this_pins_a_limitation_subject_b_sees_zero_of_subject_as_denial_rows PASSED [ 96%]
tests/test_acl_ownership_rls.py::TestForgedUserIdRefusedOnPostgres::test_a_forged_user_id_bypassing_the_writer_is_refused_by_rls PASSED [100%]

============================== 26 passed in 5.48s ==============================
```

The 17 pre-existing `TestBeforeFix`/`TestAfterFix` tests are BYTE-IDENTICAL
to what shipped in WU-2a (`git diff main -- tests/test_acl_ownership_rls.py`
shows 687 insertions, **0 deletions** — the §2 frozen-exception's exact
requirement) and pass unchanged; the 9 new tests are pure additions.

## 4. Per-guard neuter table (N1-N8), `cmp`-verified restore

| Guard | Test(s) | Neuter applied | Result before restore | Restored? |
|---|---|---|---|---|
| N1 — success row, full payload | `TestRecordSuccess` (aiosqlite) / `test_n1_...` (PG) | n/a — direct behavioural assertion | — | n/a |
| N2 — denial row, separately | `TestRecordDenial` (aiosqlite) / `test_n2_...` (PG) | n/a — direct behavioural assertion | — | n/a |
| N3 — denial row survives rollback | `TestDenialRowSurvivesRollback` (aiosqlite, CHECK abort) / `test_n3_...` (PG, RLS abort) | n/a — direct behavioural assertion against a REAL abort (031 CHECK on aiosqlite; migration-031 RLS `WITH CHECK` on PG) | — | n/a |
| N4 — writer raises, fail-closed | `TestFailClosed` (aiosqlite + PG) | monkeypatched `_content_hash`/`get_postgres_factory` to raise; separately, a swallowing `try/except` was introduced around `record_denial`'s `db.commit()` | RED (`test_n4_no_ambient_identity_...` failed) confirming the assertion is non-vacuous | YES — `diff -q` against a pre-neuter backup showed byte-identical restore |
| N5 — append-only trigger, behavioural | `TestAppendOnlyTriggerNeuter` (PG only) | `DROP TRIGGER interactions_append_only` (admin connection) | UPDATE succeeded while dropped (guard proven real, not vacuous) | YES — restored via the single `CREATE TRIGGER` statement (not `upgrade()`, which would fail: 016 creates TWO triggers); `pg_trigger` compared to the pre-drop capture, exact match |
| N6 — §10.1 limitation pin | `TestCrossSubjectAuditReadIsImpossible` (PG only, exactly one test) | n/a — pins a limitation, is not itself neutered (neutering it would prove a control, contradicting its own purpose) | — | n/a |
| N7 — trace_id derivation | `TestTraceIdDerivation` | replaced `current_trace_id_hex()`'s call site in `record()` with a hard-coded 32-hex string | RED — both `TestTraceIdDerivation` tests failed (the match assertion depends on the REAL span id, not a hard-coded one) | YES — `diff -q` byte-identical restore |
| N8 — user_id/granted_by derivation | `TestUserIdAndGrantedByDerivation` (aiosqlite) / `TestForgedUserIdRefusedOnPostgres` (PG) | `test_neutering_the_derivation_flips_the_value_assertion_red` monkeypatches `_require_user_id` to a hard-coded wrong subject, in-test | RED value assertion demonstrated inline (the test itself proves it, not a separate restore step) | n/a (monkeypatch auto-reverts) |

N4's swallow-try/except neuter and N7's hard-coded-trace_id neuter were
applied directly to `src/audittrace/services/console_acl/_audit.py`,
confirmed RED, then restored via `cp` from a pre-neuter backup and
`diff -q` confirmed byte-identical (`cmp`-verified restore, §9's
requirement) before the file was committed. Full suite re-run GREEN
after each restore (see §5 for the committed-state authoritative run).

## 5. Single alembic head (§11's exact requirement)

```
$ .venv/bin/python -m alembic -c alembic.ini heads
742a8b743c94 (head)
```

Matches the spec's cited real head exactly (migration 032, parent
`b3f8a1c6d9e2` = 031) — not copied from the spec, independently derived
from this run's own `alembic heads` output.

## 6. §10.1's exact sentence (verbatim, per the spec's instruction)

> A denial row stamped with the attacker's user_id is readable by the
> attacker and invisible to the resource owner or an auditor.

Pinned, never "proven as a control", by
`TestCrossSubjectAuditReadIsImpossible::
test_this_pins_a_limitation_subject_b_sees_zero_of_subject_as_denial_rows`
(exactly one test, real Postgres, named so it can never be misread as a
passing security check).

## 7. §10.2 — the missing administrative plane, as TWO privileges

There is no BYPASSRLS administrative plane anywhere in this codebase.
That single fact shows up as **two distinct privileges**, never one:

1. **Cross-subject READ on `interactions`** — an auditor cannot read
   another subject's ACL audit rows (§10.1, pinned above).
2. **Cross-subject UPDATE/expire on `console_acl_entries`** — an admin
   cannot revoke another subject's grant (§6's forward ruling for
   2b-core-A, D-S).

Both are the same underlying fact (per-subject RLS is the only
boundary) seen from two different blast radii. This belongs to **no
current work unit** — not 2b, 2c, or 2d — and needs its own backlog
item; it is NOT considered closed by this build record alone (per the
spec's explicit instruction).

## 8. Migration 016 — provisional-live note

016's presence on the LIVE (deployed) database is **unconfirmed** —
committed ≠ live. This build only proves the trigger behaviourally
against a throwaway, freshly-migrated Postgres schema (§3 above); it
does **not** query the live cluster's `pg_trigger`. WU-6's gate is
where that live confirmation belongs.
