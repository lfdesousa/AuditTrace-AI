# Evidence — fix round 2, ConsoleStoreBase extension-point attack surface (F3/F4/A-R7), session 2026-09-17

Answers `review-verdict-consolestorebase-wu-a-fixround1-20260917.outcome-reject`
(F3, F4, F5, A-R7), governed by
`2026-09-17-SPEC-ADDENDUM-C-the-extension-point-is-the-attack-surface.md`
(`sha256:fb1a10f50bd2f7873c78ca1ca541e66a00daebaee065d9922ef2342c8c3630a6`),
amending the chain: parent `2026-09-13-SPEC-…-abstractions.md` →
`…ADDENDUM-A…` → `2026-09-15-SPEC-…-fix-round-1` (`sha256:fcd9f1d0…`) →
`2026-09-17-SPEC-ADDENDUM-…-WITHDRAWN` (`sha256:0596986a…`) → this addendum.

## What this session closed

1. **F3** — `_SEALED_DOMAIN_MEMBERS` gained `__setattr__`/`__delattr__`
   themselves, so an ordinary domain subclass overriding either is refused
   at CLASS-DEFINITION time by the existing `_refuse_redefinition`
   mechanism (no new code, only a member-list completion).
2. **F4** — `SEALED_STORE_MEMBERS` (store) and `_SEALED_DOMAIN_CLASS_ATTRS`
   (domain) both gained `__setattr__`, `__delattr__`, `__class__`
   themselves, closing three un-enumerated class-level writes that
   disabled `_SealedMeta`/`_DomainMeta`:
   `PostgresConsoleStore.__setattr__ = object.__setattr__`,
   `PostgresConsoleStore.__class__ = ABCMeta`,
   `WidgetDomain.__class__ = ABCMeta`.
3. **A-R7 (advisory, closed)** — new `_GuardedSessionsMeta` seals
   `get_session_scoped` and its own dunders at the class level;
   `_GuardedSessions.get_session_scoped = <evil>` is now refused the same
   way the instance-level `__setattr__`/`__delattr__` already refuse
   instance writes.
4. **F5** — three false absolutes removed/corrected (`_base.py`'s
   "Not reachable today", `test_domain_descriptor_sealed.py`'s "the only
   way", `_domain.py`'s "unconditionally, for its entire lifetime"); the
   no-absolutes grep widened to `cannot|never|not reachable|the only
   way|unconditionally|impossible|always|sole|guaranteed|non-bypassable|
   un-?enumerated|exhaustive` and re-run over every touched file
   (including this session's own new prose).
5. **R5 / THE INVERSE** — Guard C (`_refuse_reserved_value_columns`)
   relabelled LOAD-BEARING, not "defence in depth" / "not reachable
   today": before F3 closed, this guard was the SOLE barrier reachable
   with zero monkeypatch.
6. **A-STALE-1/2** — three stale `tests/test_console_store_*` pointers
   (renamed by A3's decomposition) corrected to their real
   `tests/console_store/*` paths across `_sealing.py`, `_errors.py`,
   `_base.py`, `_postgres.py`, `_domain.py`; `_model` references in
   `_base.py` (6 occurrences originally) now explicitly marked
   NOW-REMOVED rather than presented as live.

## Live witness capture (this session, against the FINAL tree at commit time)

Script run standalone (not via pytest) against a fresh SQLite harness,
capturing a REAL OTel span trace_id and the raw DB row, then each of the
three F4 attack routes and A-R7, in one process:

```
$ .venv/bin/python /tmp/.../f3_f4_ar7_witness.py
[WITNESS F3] class-definition raised: main.<locals>.UnsealedDomain: sealed member(s) may not be overridden: __setattr__
[WITNESS F3] span trace_id=c2d535098c78bb6bfdcf08263a7baa5f
[WITNESS F3] DB ROW trace_id=c2d535098c78bb6bfdcf08263a7baa5f
[WITNESS R2] seal-dunder reassignment refused: PostgresConsoleStore.__setattr__ is sealed
[WITNESS R2] alice sees: ['ALICE-SECRET']
[WITNESS R3] metaclass swap refused: PostgresConsoleStore.__class__ is sealed
[WITNESS R4] domain metaclass swap refused: WidgetDomain.__class__ is a domain descriptor attribute and cannot be reassigned on the class after definition
[WITNESS A-R7] class-level reassignment refused: _GuardedSessions.get_session_scoped is sealed
```

The F3 witness matches the DB-row trace_id to the real span trace_id
(`c2d535098c78bb6bfdcf08263a7baa5f`) — the F1 harm (a nulled `trace_id`)
does not reach a written row via the "ordinary subclass" route, because the
subclass never comes into existence. R2/R3/A-R7 show the attack refused
BEFORE it can reach a state mutation; R2 additionally shows alice's `list()`
still returns only her own row (`['ALICE-SECRET']`), not bob's — the
cross-user read the reject demonstrated does not occur.

## Per-guard non-vacuity table (`tests/console_store/test_extension_point_sealed.py`, new file, this session)

One neuter per run, restored `type.__setattr__`/`type.__delattr__`-bypass
byte-identical between each (verified via `git diff` empty after each
restore), never batched:

| Route | Guard neutered | Attack-refused test | Non-vacuity (neutered) | Side effect asserted |
|---|---|---|---|---|
| F3 (subclass `__setattr__`) | `_SEALED_DOMAIN_MEMBERS` lacks `__setattr__`/`__delattr__` (pre-fix shape, exercised via the ORIGINAL `test_domain_descriptor_sealed.py` Guard-A neuter, now via `type.__setattr__` bypass since the class-level seal now blocks the old `monkeypatch.setattr` route) | `test_overriding_setattr_is_refused_at_class_definition` | `test_neutering_the_seal_lets_the_mutation_through` (existing Guard A file, updated) | tuple mutation succeeds |
| F4 R2 (`Store.__setattr__`) | `SEALED_STORE_MEMBERS - {"__setattr__"}` | `test_reassigning_setattr_on_the_class_is_refused` | `test_neutering_the_seal_dunder_check_yields_a_cross_user_read` | alice's `list()` returns BOB'S row too |
| F4 R3 (`Store.__class__`) | `SEALED_STORE_MEMBERS - {"__class__"}` | `test_reassigning_the_metaclass_is_refused` | `test_neutering_the_class_seal_yields_a_cross_user_read` | alice's `list()` returns BOB'S row too |
| F4 R4 (`Domain.__class__`) | `_SEALED_DOMAIN_CLASS_ATTRS - {"__class__"}` | `test_reassigning_the_domain_metaclass_is_refused` | `test_neutering_the_domain_class_seal_reopens_guard_d` | `value_columns` tuple mutation succeeds (Guard C independently blocks the follow-on write — disclosed in the test docstring, not conflated) |
| A-R7 (`_GuardedSessions.get_session_scoped`) | plain `type` as metaclass (pre-fix shape) | `test_reassigning_get_session_scoped_on_the_class_is_refused` / `test_deleting_get_session_scoped_on_the_class_is_refused` | `test_neutering_the_class_seal_lets_the_opener_method_be_replaced` / `..._be_deleted` | method object identity changes / attribute removed |

Non-sealed pass-through (`test_non_sealed_class_attributes_are_still_settable_and_deletable`
for `_GuardedSessionsMeta`) closes the last coverage gap (`_postgres.py`
lines 89/92-94, the `super().__setattr__`/`super().__delattr__` pass-through
branches) — `_postgres.py` is 100% lines+branches on the final tree.

## Gates (final tree, this session)

- `make test` (full suite): **5396 passed, 0 failed, 0 skipped**, 2
  warnings (pre-existing, unrelated). Per-file coverage gate PASS (151
  files checked, lines+branches ≥90%). Zero-skip policy PASS.
  `console_store` package: **100% lines+branches on every file**
  (`_base.py`, `_domain.py`, `_postgres.py`, `_sealing.py`, `_errors.py`,
  `_context.py`, `_cursor.py`, `_mock.py`, `__init__.py`).
- Real-Postgres RLS (`tests/test_console_store_rls_postgres.py`, Docker
  `postgres:16`, non-superuser `NOBYPASSRLS` app role, `FORCE ROW LEVEL
  SECURITY`): 4/4 passed.
- `routes/` / `dependencies.py` / `db/` / `bff/` / `charts/` diff vs
  `ac5f4fa`: empty. `/v1` OpenAPI diff vs `ac5f4fa`: empty.
- `tests/test_console_tool_favorites_routes.py` +
  `tests/test_console_tool_favorites_service.py`: unmodified (0-line diff
  vs `ac5f4fa`), 59/59 passed.
- mypy clean on `.venv` AND the pinned pre-commit hook (v1.8.0), all
  touched `src/` files. `ruff check` + `ruff format --check` clean.
- Module sizes: `_base.py` 473, `_domain.py` 457, `_postgres.py` 398,
  `_sealing.py` 113, `_errors.py` 65 — all <500 LOC. New test file
  `test_extension_point_sealed.py` 421 LOC.
- No `Co-Authored-By` / `Claude-Session` trailer on this commit
  (`git log -1 --format='%B' | grep -i 'co-authored\|claude-session'` —
  empty).
