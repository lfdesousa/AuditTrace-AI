# Evidence — fix round 1, ConsoleStoreBase domain-descriptor seal (F1), WIP session 2026-09-16

Honest snapshot at the point the operator called time. Full ledger is in the
build record (`build-record-consolestorebase-fixround1-20260916-wip.md`);
this file is the referenced evidence artefact per ADR-049.

## Targeted pytest — final state, all touched files

```
$ .venv/bin/pytest tests/console_store/ tests/test_console_store_rls_postgres.py \
    tests/test_console_tool_favorites_domain.py tests/test_console_tool_favorites_routes.py \
    tests/test_console_tool_favorites_service.py tests/bff/test_console_tool_favorites.py \
    tests/bff/test_console_tool_favorites_scopes.py -q --no-cov
...
============================= 272 passed in 12.84s =============================
```

Real Postgres RLS (Docker, `postgres:16` ephemeral container): 4/4 passed
(`tests/test_console_store_rls_postgres.py`).

## Guard A non-vacuity — `ConsoleDomain.__setattr__`/`__delattr__` (F1)

Neutered (`__setattr__`/`__delattr__` replaced with plain
`object.__setattr__`/`object.__delattr__`) → targeted run:

```
FAILED tests/console_store/test_domain_descriptor_sealed.py::TestDomainDescriptorSealed::test_the_original_exploit_line_is_refused_verbatim
FAILED tests/console_store/test_domain_descriptor_sealed.py::TestDomainDescriptorSealed::test_deleting_a_descriptor_attribute_is_refused
2 failed, 5 passed in 0.17s
```

Restored, `diff` against the pre-neuter copy → identical (no output) → re-run:

```
7 passed in 0.14s
```

## Guard B non-vacuity — generalised write-once check (`_domain` aspect only)

Neutered (the `name in self.__dict__` branch removed from
`ConsoleStoreBase.__setattr__`, leaving only the `SEALED_STORE_MEMBERS`
check) → targeted run:

```
FAILED tests/console_store/test_domain_descriptor_sealed.py::TestDomainReassignmentSealed::test_reassigning_domain_after_construction_is_refused
FAILED tests/console_store/test_domain_descriptor_sealed.py::TestDomainReassignmentSealed::test_domain_swap_cannot_smuggle_a_reserved_column_past_the_one_time_check
2 failed, 19 passed in 0.22s
```

Restored, `diff` against the pre-neuter copy → identical (no output).

**GAP, disclosed honestly:** this neuter cycle only exercises the
PRE-EXISTING `_domain` aspect of the write-once check. There is NO dedicated
regression test this session proving `_sessions`/`_model`-shaped instance
attributes are independently write-once-protected — the generalisation from
`_domain`-only to "every instance attribute" is argued in the `_base.py`
docstring and demonstrated manually at the REPL (`store._model = Evil`
succeeded pre-fix, confirmed via direct interpreter reproduction, not saved
here), but not captured as an automated, neuter-provable test. Flagged as
PARTIAL in the build record.

## Guard C — `_refuse_reserved_value_columns` (F1 item 2, point-of-use)

NOT independently neuter-proven this session via a dedicated single-guard
cycle. It IS exercised by
`TestReservedColumnRefusedAtPointOfUse::test_reproduction_without_guard_c_the_stamp_is_nulled`,
which neuters `validate_domain` AND `_refuse_reserved_value_columns`
TOGETHER (a combined, not per-guard, proof) and observes the raw DB read:

```
tests/console_store/test_domain_descriptor_sealed.py::TestReservedColumnRefusedAtPointOfUse
  test_refuses_a_domain_that_bypassed_construction_validation PASSED
  test_reproduction_without_guard_c_the_stamp_is_nulled PASSED
```

The second test's own internal assertion IS the raw-DB-read witness: an
honest row's `trace_id` equals the real span id, the hostile row's
`trace_id` is `None` — but this was NOT re-verified this session by
neutering Guard C ALONE (leaving `validate_domain` intact) and confirming a
RED specific to Guard C's own removal. Flagged as PARTIAL in the build
record.

## F1 headline regression (verbatim exploit line) — raw DB witness

`TestDomainDescriptorSealed::test_the_original_exploit_line_is_refused_verbatim`
runs the exact line from the spec
(`store.domain.value_columns = (*store.domain.value_columns, "trace_id")`),
asserts it raises `ConsoleStoreSealedError`, then performs a REAL write
through an active OTel span and reads the row back via `raw_rows()`
(bypassing the guarded API), asserting `raw[row["id"]]["trace_id"] ==
expected_trace`. This IS a raw-DB-read witness, run and GREEN this session
(see the full-suite line above). It was NOT, however, run against the
UNFIXED (pre-round) source to show the ORIGINAL failing `trace_id=None`
shape end-to-end in ONE continuous session transcript — the closest
equivalent is the combined-neuter reproduction above (Guard A + Guard C
together via `validate_domain`/`ConsoleDomain.__setattr__` bypass), which
DOES show `trace_id=None` on the attacked row.

## mypy / ruff / frozen-invariant checks (this session)

```
$ .venv/bin/mypy src/audittrace/services/console_store/ tests/console_store/ tests/test_console_store_rls_postgres.py
Success: no issues found in 20 source files

$ .venv/bin/pre-commit run mypy --files <same files>
mypy.....................................................................Passed

$ .venv/bin/ruff check <same files>
All checks passed!

$ git diff --stat ac5f4fa -- src/audittrace/routes/ src/audittrace/dependencies.py src/audittrace/db/ charts/
(empty)
```

`make test` (full suite, ~19 min) was **NOT run this session** — the
operator called time before it could be scheduled. This is disclosed, not
papered over.
