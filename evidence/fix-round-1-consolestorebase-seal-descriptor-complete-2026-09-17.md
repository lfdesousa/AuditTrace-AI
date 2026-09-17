# Evidence — fix round 1, ConsoleStoreBase domain-descriptor seal (F1), completion, session 2026-09-17

Continuation of `evidence/fix-round-1-consolestorebase-seal-descriptor-wip-2026-09-16.md`
(that file is superseded for the items closed below; it is kept, not
rewritten, as the honest record of what was WIP). Answers
`review-verdict-consolestorebase-wu-a-20260915.outcome-reject`, governed by
`2026-09-15-SPEC-consolestorebase-fix-round-1-seal-the-descriptor.md`
(`sha256:fcd9f1d0…`) as amended by
`2026-09-17-SPEC-ADDENDUM-consolestorebase-frozen-dataclass-recommendation-WITHDRAWN.md`
(`sha256:0596986a…`).

## What this session closed

1. **Guard D (new)** — the class-level ("second hop") mutation surface:
   `WidgetDomain.value_columns = (...)` is a CLASS-level ClassVar
   reassignment, never dispatched to `ConsoleDomain.__setattr__` (an
   INSTANCE method). Found by attacking this round's own new seal, exactly
   as the fix spec demands. Closed with `_DomainMeta(ABCMeta)` in
   `_domain.py`, refusing a fixed, named set of class attributes.
2. **The disclosed PARTIAL** — `_sessions` write-once had no dedicated
   regression test (only a manual REPL reproduction in the WIP evidence
   file). Added one, and its neuter proof shows Guard B is ONE generalised
   check protecting BOTH `_domain` and `_sessions`, not two independent
   guards (see the per-guard table).
3. **The withdrawn spec recommendation** — reproduced the falsifying case
   for `@dataclass(frozen=True, slots=True)` on a `ClassVar`-only domain,
   matching the addendum's claim exactly (script + captured output below).
4. **100% coverage on `_domain.py`** — the new metaclass's non-sealed
   pass-through branch (`__delattr__`'s `super().__delattr__(name)` for a
   non-descriptor name) had no test; added one.

## Per-guard non-vacuity table (one neuter per run, source-level, this session)

Every neuter below was a SOURCE EDIT (not just a monkeypatch inside the
test), run in isolation, restored, `cmp`-verified byte-identical against
the pre-neuter file, then re-run to confirm GREEN. Targeted file:
`tests/console_store/test_domain_descriptor_sealed.py` had 7 tests before
this session, gained 6 new ones this session (2 for `TestSessionsWriteOnceSealed`,
4 for `TestDomainClassLevelSealed`) = 13, the count each row below was run
against. A 14th test (`test_non_descriptor_class_attributes_are_still_settable_and_deletable`,
closing a coverage gap — see below) was added AFTER all four neuter cycles;
it exercises the pass-through (non-sealed) branch of the new metaclass, so
it is unaffected by, and does not need to be re-run against, any of the
four neuters above.

| Guard | What it protects | Neutered how | RED (exact tests) | Restored `cmp` | Re-run |
|---|---|---|---|---|---|
| **A** — `ConsoleDomain.__setattr__`/`__delattr__` (instance) | `store.domain.value_columns = (...)` (the ORIGINAL F1 line) | Replaced both methods' bodies with `object.__setattr__`/`object.__delattr__` pass-through | `TestDomainDescriptorSealed::test_the_original_exploit_line_is_refused_verbatim`, `::test_deleting_a_descriptor_attribute_is_refused` — 2 failed, 11 passed | IDENTICAL | 13 passed |
| **B** — `ConsoleStoreBase.__setattr__`'s generalised write-once check | `store._domain = evil` AND `store._sessions = evil` — ONE check, not two | Removed the `if name in self.__dict__: raise` branch entirely | `TestDomainReassignmentSealed::test_reassigning_domain_after_construction_is_refused`, `::test_domain_swap_cannot_smuggle_a_reserved_column_past_the_one_time_check`, `TestSessionsWriteOnceSealed::test_reassigning_sessions_after_construction_is_refused` — 3 failed, 10 passed | IDENTICAL | 13 passed |
| **C** — `ConsoleStoreBase._refuse_reserved_value_columns` (point-of-use) | A domain that names a reserved column in `value_columns` via a construction path that skipped `validate_domain()` | Replaced the body with `pass` (collision check removed), leaving Guard A/B/`validate_domain()` intact | `TestReservedColumnRefusedAtPointOfUse::test_refuses_a_domain_that_bypassed_construction_validation` — 1 failed, 12 passed | IDENTICAL | 13 passed |
| **D** — `_DomainMeta.__setattr__`/`__delattr__` (class-level, NEW this round) | `WidgetDomain.value_columns = (...)` (the SECOND HOP) | Replaced both methods' bodies with `super().__setattr__`/`super().__delattr__` pass-through | `TestDomainClassLevelSealed::test_class_level_reassignment_is_refused`, `::test_class_level_deletion_is_refused`, `::test_class_level_reassignment_would_have_nulled_trace_id`, `::test_neutering_the_class_level_seal_lets_the_mutation_through` — 4 failed, 9 passed | IDENTICAL | 13 passed |

**FINDING, disclosed per the spec's own "no absolutes without proof"
instruction:** Guard B is **REDUNDANT-BUT-EXTENDED, not two guards**. The
fix-round-1 commit message described `_sessions` write-once as covered by
"the SAME generalised check" as `_domain` — this session's per-guard
neuter confirms that literally: there is exactly ONE branch
(`if name in self.__dict__: raise`) protecting both names, so neutering it
once reds both regression tests simultaneously. This is the *intended*
generalisation (WU-A ADDENDUM A's "close the whole class, not one name at
a time"), not the F-C1 batch-neuter anti-pattern — that anti-pattern is
neutering *two independent guards* at once and calling it one proof; here
there is only one guard to begin with, covering two names by construction.
Labelled honestly rather than inflated into "two independent guards".

Guard A and Guard D are the two INDEPENDENT guards for the domain
descriptor: Guard A is an instance method, Guard D is the metaclass; each
was neutered while the OTHER stayed live, and each reds only its own
tests (see table) — not a shared mechanism.

## F1 headline regression — raw DB witness (unchanged from the WIP session, re-confirmed green this session)

`TestDomainDescriptorSealed::test_the_original_exploit_line_is_refused_verbatim`
still passes (13/13 in the full targeted run below) and still performs the
raw DB read: an honest row's `trace_id` equals the real span id after the
blocked mutation attempt. See the WIP evidence file for the original
capture; re-run captured in "Full targeted run" below.

## The class-level (Guard D) regression — raw DB witness

`TestDomainClassLevelSealed::test_class_level_reassignment_would_have_nulled_trace_id`
performs the SAME shape of proof for the new class-level exploit line
(`WidgetDomain.value_columns = (*…, "trace_id")`, ordinary Python, no
`type.__setattr__` call written out): the mutation is refused
(`ConsoleStoreSealedError`), and a subsequent real write through an active
OTel span still stamps `trace_id` correctly, verified via a raw DB read
(`_raw(store, harness)`), not a bare `raises` assertion.

## Falsifying the withdrawn spec recommendation — reproducible, not asserted

The `2026-09-17-SPEC-ADDENDUM` withdraws the fix spec's own recommended
shape (`@dataclass(frozen=True, slots=True)`) for `ConsoleDomain`. The
claim is reproduced here directly, matching the real shape exactly: the
frozen dataclass is a BASE class never instantiated directly (exactly like
`ConsoleDomain` itself), every column is a `ClassVar` (excluded from
dataclass field discovery), and the object actually instantiated and
mutated is a plain SUBCLASS (exactly like every real domain,
`WidgetDomain(ConsoleDomain)`).

```python
"""Falsifying case for the WITHDRAWN spec recommendation
(@dataclass(frozen=True, slots=True) on a ClassVar-only descriptor).

Mirrors the real ConsoleDomain shape exactly: the frozen dataclass is a
BASE class (never instantiated directly -- exactly like ConsoleDomain
itself), every column is a ClassVar, and the object actually
instantiated and mutated is always a SUBCLASS of it (exactly like every
real domain, e.g. WidgetDomain(ConsoleDomain)). Run directly:
`python3 falsify_frozen_dataclass.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import ClassVar


@dataclass(frozen=True)
class FrozenBaseNoSlots:
    """Zero dataclass fields -- value_columns is a ClassVar, exactly
    like audittrace.services.console_store._domain.ConsoleDomain. Never
    instantiated directly in real use."""

    value_columns: ClassVar[tuple[str, ...]] = ()


class RealDomainNoSlots(FrozenBaseNoSlots):
    """A real domain: a plain subclass, never re-decorated with
    @dataclass -- it inherits the base's generated __setattr__ verbatim,
    exactly like every ConsoleDomain subclass in this codebase."""

    value_columns = ("payload",)


@dataclass(frozen=True, slots=True)
class FrozenBaseWithSlots:
    value_columns: ClassVar[tuple[str, ...]] = ()


class RealDomainWithSlots(FrozenBaseWithSlots):
    value_columns = ("payload",)


print("dataclasses.fields(FrozenBaseNoSlots()) ->", fields(FrozenBaseNoSlots()))
print("(zero fields: value_columns is a ClassVar, dataclass() ignores it)")
print()

# --- Case 1: frozen, no slots, mutated via a SUBCLASS instance ------------
inst = RealDomainNoSlots()
try:
    inst.value_columns = (*inst.value_columns, "trace_id")
    outcome = f"SUCCEEDED (mutated to {inst.value_columns!r}) -- seal is decorative"
except Exception as exc:  # pragma: no cover - falsification script, not a test
    outcome = f"raised {type(exc).__name__}: {exc}"
print(f"RealDomainNoSlots(FrozenBaseNoSlots) instance: "
      f"inst.value_columns = (...) -> {outcome}")

# --- Case 2: frozen + slots=True, mutated via a SUBCLASS instance ---------
try:
    inst2 = RealDomainWithSlots()
    inst2.value_columns = (*inst2.value_columns, "trace_id")
    outcome = f"SUCCEEDED (mutated to {inst2.value_columns!r}) -- seal is decorative"
except TypeError as exc:
    outcome = f"raised TypeError (NOT the intended FrozenInstanceError): {exc}"
except Exception as exc:  # pragma: no cover
    outcome = f"raised {type(exc).__name__}: {exc}"
print(f"RealDomainWithSlots(FrozenBaseWithSlots) instance: "
      f"inst2.value_columns = (...) -> {outcome}")
```

Captured output, this session, `.venv/bin/python falsify_frozen_dataclass.py`:

```
dataclasses.fields(FrozenBaseNoSlots()) -> ()
(zero fields: value_columns is a ClassVar, dataclass() ignores it)

RealDomainNoSlots(FrozenBaseNoSlots) instance: inst.value_columns = (...) -> SUCCEEDED (mutated to ('payload', 'trace_id')) -- seal is decorative
RealDomainWithSlots(FrozenBaseWithSlots) instance: inst2.value_columns = (...) -> raised TypeError (NOT the intended FrozenInstanceError): super(type, obj): obj must be an instance or subtype of type
```

This confirms, empirically and reproducibly, both addendum claims:

1. **`frozen=True` without `slots`:** the mutation on a real (subclass)
   domain instance **SUCCEEDS** — the seal is decorative for exactly the
   reason the addendum states (zero dataclass fields, generated
   `__setattr__` falls through to `super().__setattr__` for any
   `type(self) is not cls`, i.e. every subclass instance).
2. **`frozen=True, slots=True`:** the mutation raises `TypeError`, not the
   intended `FrozenInstanceError` — the stale pre-slots `cls` closure
   breaking `super()`, exactly as the addendum describes. Worse than (1):
   a maintainer expecting a `FrozenInstanceError` (the "correct" seal
   error) gets a confusing `TypeError` instead, and BOTH cases fail to
   protect the descriptor.

The hand-written `__setattr__`/`__delattr__` shipped in `26a9d85` (kept,
not simplified) is confirmed as the correct shape by this reproduction,
not merely argued in a docstring.

## mypy / ruff / frozen-invariant checks (this session)

```
$ .venv/bin/mypy src/audittrace/services/console_store/ tests/console_store/ tests/test_console_store_rls_postgres.py
Success: no issues found in 20 source files

$ .venv/bin/pre-commit run mypy --files src/audittrace/services/console_store/_domain.py tests/console_store/test_domain_descriptor_sealed.py
mypy.....................................................................Passed

$ .venv/bin/ruff check src/audittrace/services/console_store/ tests/console_store/
All checks passed!

$ .venv/bin/ruff format --check src/audittrace/services/console_store/ tests/console_store/
19 files already formatted

$ git diff --stat ac5f4fa -- src/audittrace/routes/ src/audittrace/dependencies.py src/audittrace/db/ charts/
(empty)

$ wc -l src/audittrace/services/console_store/_domain.py tests/console_store/test_domain_descriptor_sealed.py
  377 src/audittrace/services/console_store/_domain.py
  452 tests/console_store/test_domain_descriptor_sealed.py
```

## Full targeted run (this session, final state, --cov-report=term-missing)

```
$ .venv/bin/pytest tests/console_store/ tests/test_console_store_rls_postgres.py \
    tests/test_console_tool_favorites_domain.py tests/test_console_tool_favorites_routes.py \
    tests/test_console_tool_favorites_service.py tests/bff/test_console_tool_favorites.py \
    tests/bff/test_console_tool_favorites_scopes.py -q --cov-report=term-missing
...
src/audittrace/services/console_store/_base.py                149      0     26      0   100%
src/audittrace/services/console_store/_context.py               42      0      6      0   100%
src/audittrace/services/console_store/_cursor.py                55      0     20      0   100%
src/audittrace/services/console_store/_domain.py               108      0      8      0   100%
src/audittrace/services/console_store/_errors.py                11      0      0      0   100%
src/audittrace/services/console_store/_mock.py                 107      0     24      0   100%
src/audittrace/services/console_store/_postgres.py             149      0     26      0   100%
src/audittrace/services/console_store/_sealing.py                36      0     12      0   100%
src/audittrace/services/console_tool_favorites.py                65      0      6      0   100%
============================= 279 passed in 43.05s =============================
```

Real-Postgres RLS (Docker, `postgres:16` ephemeral container, non-superuser
`NOBYPASSRLS` app role): 4/4 passed (`tests/test_console_store_rls_postgres.py`),
run twice this session (once before, once after the Guard D addition), both
green.

## No-absolutes check (spec Acceptance requirement)

Ran, this session (not asserted from memory):

```
$ grep -noE "\b(cannot|never|un-?enumerated)\b.{0,80}" \
    src/audittrace/services/console_store/_domain.py \
    src/audittrace/services/console_store/_base.py \
    tests/console_store/test_domain_descriptor_sealed.py \
    tests/console_store/test_sealed_classes.py
```

53 hits. Read every one: each is either (a) describing what a NAMED,
TESTED guard refuses, immediately followed by or adjacent to the test that
establishes it (e.g. `"...cannot be reassigned on the class after
definition"` — Guard D's error message, proven in the per-guard table
above), or (b) a scoped design statement about a hook contract (`"cannot
name a reserved column"` — enforced by `_hook_output`, tested in
`test_hostile_domain_hooks.py`), never an unscoped totalizing claim about
the system as a whole. Specifically searched for, and found ZERO
occurrences of, the exact phrasings that falsified the two PRIOR rounds'
evidence (`review-verdict-consolestorebase-wu-a-20260915`,
`lesson-unpinnable-claim-check-your-own-techniques-20260915`): "runs
exactly once", "can never be superseded", "left un-enumerated", "no
un-enumerated". This check is itself falsifiable and was actually run
(command above), not asserted.
