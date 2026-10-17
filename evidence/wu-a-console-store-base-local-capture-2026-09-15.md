# Evidence — WU-A `ConsoleStoreBase` foundational abstraction, local capture (2026-09-15)

**Scope of this evidence file.** WU-A of the ratified spec
`2026-09-13-SPEC-sovereign-store-and-adapter-abstractions.md`
(`sha256:37ffe294576abd61ff35b82486a9df9eb82e570e0b8ed6dbabc11cf316c7adc8`) plus its
ratified addendum `2026-09-13-SPEC-ADDENDUM-A-consolestorebase-close-the-escape-hatch.md`
(`sha256:fab97c5140614821012065cb115f7d30d16077bb18660f2e30421900ef7d733d`), which wins
where the two differ. Foundational library work (`ConsoleStoreBase[T]` +
`PostgresConsoleStore[T]` / `MockConsoleStore[T]` + `ConsoleDomain[T]`), migrating the
Tool-Favorites domain onto it as the ONE reference domain the spec requires. No route,
no BFF, no chart change — `routes/console_tool_favorites.py`, `dependencies.py`, the
BFF proxy/scopes and every pre-existing route/BFF test are byte-for-byte untouched
(`git status --short` shows only `services/console_tool_favorites.py` modified plus
the new `services/console_store/` package and its tests). This file satisfies ADR-049
Rule 1 (Verification) in full, including a REAL-Postgres RLS integration proof (Rule 3
shape — the DB itself is the witness), through the pre-existing HTTP-route/BFF suites
run unmodified against the migrated implementation (behavior-equivalence). It does
**NOT** exercise a deployed image + public API + scoped JWT (Rule 2) — there is no new
route surface in this WU; that rides the fork tool-favorites-shim WU and any future
retrofit of the seven other domains onto this base, mirroring the WU-1/WU-presets/
WU-prompts/WU-chatprojects/WU-files/WU-agents/WU-conversation-tags/WU-tool-favorites
precedents' own Rule-2 deferral note.

## 0. This is a finishing pass on a prior builder's work

The base package (`src/audittrace/services/console_store/`, 8 modules) and its three
test files were built by a prior builder session that was killed twice by API rate
limits before it could commit. This capture is from an independent finishing pass:
verifying the proof discipline named in the addendum, individually re-deriving the
non-vacuity of every base invariant (see §3), producing the mandatory surface
enumeration (see §5) — which surfaced one previously-undisclosed, hurry-mode-reachable
residual (`_domain` reassignment post-construction), closed in this same pass (see
§5, item A2) — and writing this evidence file, which did not previously exist for this
WU.

## 1. Full test-suite run (Rule 1 — Verification)

First run (before the `_domain`-reassignment fix, confirming the prior builder's code
was otherwise fully green):

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.98%
5364 passed, 2 warnings in 1147.74s (0:19:07)
per-file coverage gate: PASS (153 files checked, lines >= 90%, branches >= 90% on 129 file(s) with branches)
[no-skip-check] No skipped tests in junit.xml. Good.
✅ Tests passed
```

Full log: `wua-make-test-0915.log` (session scratchpad). Per-file coverage on every
new/modified file, from that run, **100% lines AND branches**:

```
src/audittrace/services/console_store/__init__.py             9      0      0      0   100%
src/audittrace/services/console_store/_base.py             139      0     42      0   100%
src/audittrace/services/console_store/_context.py           42      0      6      0   100%
src/audittrace/services/console_store/_cursor.py            55      0     20      0   100%
src/audittrace/services/console_store/_domain.py            93      0      4      0   100%
src/audittrace/services/console_store/_errors.py            11      0      0      0   100%
src/audittrace/services/console_store/_mock.py             106      0     24      0   100%
src/audittrace/services/console_store/_postgres.py         149      0     26      0   100%
src/audittrace/services/console_store/_sealing.py           36      0     12      0   100%
src/audittrace/services/console_tool_favorites.py           65      0      6      0   100%
```

Second run, AFTER the `_domain`-reassignment fix + its two new tests (this pass's own
code change — re-run per the "if you change code, re-run make test once at the end"
rule, `free -g` confirmed 24 GB available beforehand, well above the 8 GB floor):

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.98%
5366 passed, 2 warnings in 957.70s (0:15:57)
per-file coverage gate: PASS (153 files checked, lines >= 90%, branches >= 90% on 129 file(s) with branches)
[no-skip-check] No skipped tests in junit.xml. Good.
✅ Tests passed
make_test_exit=0
```

Full log: `wua-make-test-final-0915.log` (session scratchpad). `_base.py` grew from
139/42 to 141/44 lines/branches (the new `_domain`-reassignment guard) — still
**100% lines AND branches**; every other new/modified file unchanged at 100%:

```
src/audittrace/services/console_store/__init__.py            9      0      0      0   100%
src/audittrace/services/console_store/_base.py             141      0     44      0   100%
src/audittrace/services/console_store/_context.py           42      0      6      0   100%
src/audittrace/services/console_store/_cursor.py            55      0     20      0   100%
src/audittrace/services/console_store/_domain.py            93      0      4      0   100%
src/audittrace/services/console_store/_errors.py            11      0      0      0   100%
src/audittrace/services/console_store/_mock.py             106      0     24      0   100%
src/audittrace/services/console_store/_postgres.py         149      0     26      0   100%
src/audittrace/services/console_store/_sealing.py           36      0     12      0   100%
src/audittrace/services/console_tool_favorites.py           65      0      6      0   100%
```

Two runs, 5364→5366 passed (net +2 for the new `TestDomainReassignmentSealed` tests),
zero failures either run, zero skips, per-file gate PASS both times.

Third run, AFTER converting the 4 PEP-695 generic class definitions to PEP-484
`TypeVar`/`Generic[T]` + the `_cursor.py::_strict_int` narrowing fix (§6 — required
for `git commit` to pass the pinned pre-commit mypy hook; `free -g` confirmed 22 GB
available beforehand):

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.98%
5366 passed, 2 warnings in 679.57s (0:11:19)
per-file coverage gate: PASS (151 files checked, lines >= 90%, branches >= 90% on 127 file(s) with branches)
[no-skip-check] No skipped tests in junit.xml. Good.
✅ Tests passed
make_test_exit=0
```

Full log: `wua-make-test-final3-0915.log` (session scratchpad). Every new/modified
file still **100% lines AND branches** (line counts shift by ±1 from the syntax
change, not from any behavior change):

```
src/audittrace/services/console_store/__init__.py            9      0      0      0   100%
src/audittrace/services/console_store/_base.py             142      0     44      0   100%
src/audittrace/services/console_store/_context.py           42      0      6      0   100%
src/audittrace/services/console_store/_cursor.py            55      0     20      0   100%
src/audittrace/services/console_store/_domain.py            94      0      4      0   100%
src/audittrace/services/console_store/_errors.py            11      0      0      0   100%
src/audittrace/services/console_store/_mock.py             107      0     24      0   100%
src/audittrace/services/console_store/_postgres.py         150      0     26      0   100%
src/audittrace/services/console_store/_sealing.py           36      0     12      0   100%
src/audittrace/services/console_tool_favorites.py           65      0      6      0   100%
```

Three runs total, 5364/5366/5366 passed, zero failures, zero skips, per-file gate
PASS every time (the 153→151 "files checked" / 129→127 "files with branches" count
shift between runs 2 and 3 tracks pytest's coverage-collection ordering across
runs, not a missing file — every WU-A file is present and 100% in all three).

## 2. Real-Postgres RLS proof (ADDENDUM A §3 — "SQLite does not enforce RLS")

Docker-backed ephemeral `postgres:16` container (portability: target resolved from
`AUDITTRACE_TEST_POSTGRES_URL` when set, else auto-started — never a hardcoded host),
a NON-superuser `NOBYPASSRLS` app role, migration 030's RLS DDL verbatim, through the
migrated `PostgresConsoleToolFavoritesService`:

```
$ .venv/bin/python -m pytest tests/test_console_store_rls_postgres.py -v --no-cov
tests/test_console_store_rls_postgres.py::TestMigratedDomainUnderRealRls::test_bob_cannot_read_or_remove_alices_favorite PASSED
tests/test_console_store_rls_postgres.py::TestMigratedDomainUnderRealRls::test_base_scopes_the_db_layer_even_without_the_request_contextvar PASSED
tests/test_console_store_rls_postgres.py::TestMigratedDomainUnderRealRls::test_with_check_rejects_a_forged_user_sub_at_the_db_layer PASSED
tests/test_console_store_rls_postgres.py::TestMigratedDomainUnderRealRls::test_unscoped_select_through_the_guarded_opener_is_still_db_scoped PASSED
4 passed in 5.93s
```

What each proves, with the DB as witness (not the app layer):
1. Through the service, with the RLS ContextVar bound per caller: bob cannot list/
   remove alice's favorite; a raw `SELECT` as the app role with bob's GUC bound sees
   zero rows; alice's own raw count is 2; with **no GUC bound at all**, a raw `SELECT`
   sees **zero** rows (`safe-by-default`, not "safe only if the app remembers to set
   the GUC").
2. With the ContextVar UNBOUND (background-worker shape), the base pushes the GUC
   itself — the DB layer stays scoped without the request-layer listener.
3. `WITH CHECK` rejects a raw `INSERT` whose `user_sub` disagrees with the bound GUC —
   the DB-layer backstop behind the base's own unconditional stamp.
4. An UNSCOPED `select()` run through the base's own guarded session opener
   (`store._sessions.get_session_scoped(...)`) still returns only the caller's rows —
   confirming the pre-wrapping in `_GuardedSessions.get_session_scoped` (item B12,
   §5) is enforced at the DB layer, not merely by the app-level predicate.

## 3. Individual neuter proofs (STEP 1 — the crux of this pass)

**Finding, stated plainly: the shipped code carried NO neuter-proof record and NO
per-guard table with actually-executed RED→restore→GREEN runs.** The `test_console_
store_base.py` module docstring DOES contain a per-invariant → test-class mapping
table (reproduced below, extended with the new guard), which is a useful map but is
not itself proof of individual, non-batched execution. Per the mandatory recall
(`lesson-neuter-guards-individually-20260913`: *"a batch neuter can't detect a dead
guard"*), every invariant below was neutered ONE AT A TIME in this pass, confirmed
RED against a real side-effect assertion, restored, confirmed byte-identical
(`cmp`), and confirmed GREEN again — before touching the next one. No invariant
stayed vacuously GREEN under its own neuter.

| # | Invariant (file : mechanism) | Neuter applied | Targeted test(s) | RED side effect observed | Restore | Byte-identical |
|---|---|---|---|---|---|---|
| 1 | RLS `user_sub` predicate — Postgres (`_postgres.py::_scoped_select`) | Dropped `.where(model.user_sub == user_sub)` | `TestRlsReadsAreUserScoped` + `TestRlsWritesAreUserScoped` `[sqlite]` (7 tests) | `bob.get(KEY_A)` returned alice's row: `{'user_sub': 'alice-sub', ...}` — `AssertionError: bob read alice's row` | `cmp` | ✅ |
| 2 | RLS `user_sub` predicate — Mock (`_mock.py::_scoped_rows`) | Dropped `if row["user_sub"] != user_sub: continue` | same 7 tests `[mock]` | same 7 tests FAILED | `cmp` | ✅ |
| 3 | `trace_id` stamping (`_base.py::_insert/_update/_delete_values`) | All 3 `stamp.trace_id` → `None` | `TestStamping::test_trace_id_stamped_on_insert_update_and_delete` `[mock]`+`[sqlite]` | 2 failed (no trace_id on any of insert/update/delete) | `cmp` | ✅ |
| 4 | `session_id` stamping (`_base.py::_insert/_update/_delete_values`) | All 3 `stamp.session_id` → `None` | `TestStamping::test_session_id_stamped_from_request_context` `[mock]`+`[sqlite]` | 2 failed (session_id never stamped) | `cmp` | ✅ |
| 5 | D13 tombstone reuse — Postgres (`_postgres.py::upsert`) | `if tombstoned is not None:` → `if False and ...:` | `TestSoftDeleteThenRecreate` `[sqlite]` | `test_delete_then_upsert_untombstones_the_same_row` FAILED — `sqlite3.IntegrityError: UNIQUE constraint failed` (a second row was attempted instead of reusing the tombstone) | `cmp` | ✅ |
| 6 | D13 tombstone reuse — Mock (`_mock.py::upsert`) | `if tombstoned:` → `if False:` | `TestSoftDeleteThenRecreate` `[mock]` | same test FAILED (new row created, `again["id"] != first["id"]`) | `cmp` | ✅ |
| 7 | Caller-metadata rejection (`_base.py::_validated_values`) | `reserved = sorted(...)` → `reserved = []` | `TestCallerMetadataRejected::test_reserved_column_in_values_is_refused_before_any_write` (12 parametrizations) | all 12 FAILED (wrong-exception / no-exception; the reserved-column write attempt was no longer refused by this specific guard) | `cmp` | ✅ |
| 8 | RLS ContextVar cross-check (`_context.py::resolve_user_sub`) | `if bound is not None and bound != sub:` → `if False:` | `TestScopeAnchoring::test_mismatch_with_rls_contextvar_is_refused_before_any_write` + `test_mismatch_refuses_reads_too` `[mock]`+`[sqlite]` | 4 FAILED — identity mismatch silently accepted | `cmp` | ✅ |
| 9 | Session-scope discipline / serialize-before-commit (`_postgres.py::upsert`, update branch) | Moved `snapshot = self._snapshot(active)` to AFTER `await session.commit()` | `TestSessionScopeDiscipline::test_store_survives_attribute_expiry_on_commit` | FAILED — `sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called; can't call await_only() here` (exactly the failure mode the test's own docstring predicts for a lazy-load after commit) | `cmp` | ✅ |
| 10 | Hostile-subclass closure (`_sealing.py::seal_subclass`) | Function body → `return` (no-op) | Standalone probe (below) + shipped `tests/test_console_store_hostile.py::TestSealedStoreClasses` (7 tests) | Probe: `class_created=True construction_exc=None bob_read_alice_secret=True` — the exploit SUCCEEDED, cross-user secret read confirmed. Shipped suite: 7/34 hostile tests FAILED (`DID NOT RAISE`) | `cmp` | ✅ |
| 11 | `_domain` reassignment seal (`_base.py::__setattr__`, THIS PASS's own new guard) | Removed the `name == "_domain"` branch | `TestDomainReassignmentSealed` (2 new tests, this pass) | both FAILED — reassignment silently succeeded | `cmp` | ✅ |

Guard 9 and guard 10 particularly matter for demonstrating non-vacuity in the
strongest available sense: guard 9's neutered failure mode (`MissingGreenlet`) is
qualitatively different from a plain assertion failure — it is the *interpreter*
proving the resource-scope discipline is load-bearing, not the test author's choice
of assertion. Guard 10's standalone probe (`wua_hostile_seal_probe.py`, session
scratchpad) additionally satisfies ADDENDUM A §3's literal instruction — "neuter the
base's closure → the hostile-subclass test goes GREEN (the bypass now succeeds) →
restore → RED again" — by actually instantiating the hostile subclass and reading
cross-user data with the guard removed, not merely observing that `pytest.raises`
failed to see an exception:

```
$ PYTHONPATH=$(pwd) .venv/bin/python wua_hostile_seal_probe.py   # seal_subclass neutered
class_created=True construction_exc=None bob_read_alice_secret=True

$ PYTHONPATH=$(pwd) .venv/bin/python wua_hostile_seal_probe.py   # seal_subclass restored
class_created=False construction_exc=ConsoleStoreSealedError bob_read_alice_secret=False
```

No guard's individual neuter stayed vacuously GREEN — every one of the 11 rows above
produced a real, distinct RED with an observable side effect. There is therefore no
REDUNDANT-BUT-RETAINED finding to disclose from this campaign: each of the 11 guards
is independently load-bearing.

**Final restore verification** — every touched file byte-identical to its
pre-campaign state (backups taken to the session scratchpad before the first edit):

```
$ cmp _base.py.orig  src/audittrace/services/console_store/_base.py      # (pre-fix baseline)
$ cmp _context.py.orig src/audittrace/services/console_store/_context.py
$ cmp _mock.py.orig    src/audittrace/services/console_store/_mock.py
$ cmp _postgres.py.orig src/audittrace/services/console_store/_postgres.py
$ cmp _sealing.py.orig src/audittrace/services/console_store/_sealing.py
(all silent — byte-identical)

$ cmp _base.py.orig2 src/audittrace/services/console_store/_base.py      # (post-fix baseline, guard 11)
(silent — byte-identical)

$ git status --short
 M src/audittrace/services/console_tool_favorites.py
?? src/audittrace/services/console_store/
?? tests/console_store_fixture_domain.py
?? tests/test_console_store_base.py
?? tests/test_console_store_hostile.py
?? tests/test_console_store_rls_postgres.py
?? tests/test_console_tool_favorites_domain.py
(identical shape to the state at the start of this pass — no incidental diff from the
neuter campaign itself; only the two deliberate, disclosed, tested additions — the
_domain-reassignment guard in _base.py and the two tests in test_console_store_
hostile.py — remain in the working tree)
```

## 4. Behavior-equivalence proof — pre-existing suites, unmodified, against the migrated service

`routes/console_tool_favorites.py`, `dependencies.py`, `bff/console_tool_favorites_
proxy.py`, `bff/console_tool_favorites_scopes.py` and their test files are BYTE-
IDENTICAL to `main` (confirmed via `git status --short` above — none listed as
modified). Running them, unmodified, against the migrated `ConsoleStoreBase`-backed
service:

```
$ .venv/bin/python -m pytest tests/test_console_tool_favorites_service.py \
    tests/test_console_tool_favorites_routes.py \
    tests/bff/test_console_tool_favorites.py \
    tests/bff/test_console_tool_favorites_scopes.py \
    tests/test_console_tool_favorites_domain.py \
    --no-cov -q
90 passed in 9.97s
```

`tests/test_console_tool_favorites_domain.py` is new this pass (4 tests) — a small
domain-descriptor-shape check for `ToolFavoritesDomain` itself, additive, not a
replacement for any pre-existing test.

## 5. Surface enumeration (ADDENDUM A §3 — mandatory; a build record missing this is a REJECT)

Everything a `ConsoleDomain[T]` subclass (the actual extension point — domains live
OUTSIDE the package, by composition not inheritance) or any code holding a reference
to a `ConsoleStoreBase[T]` instance can reach, and why each cannot route around the
base's invariants (or is disclosed as a residual/deviation).

### A — what a domain descriptor declares or returns

| # | Surface | Why it cannot route around the invariant |
|---|---|---|
| A1 | `model: ClassVar[type]` | `validate_domain()` requires `REQUIRED_MODEL_COLUMNS` (incl. `user_sub`) present on the model; the guarded builder (`_scoped_select`/`_scoped_rows`) unconditionally filters `model.user_sub == user_sub` regardless of which model a domain names. |
| A2 | `key_columns` / `value_columns: ClassVar[tuple]` | Validated ONCE at construction: disjoint from each other, disjoint from `RESERVED_COLUMNS`, present on the model. **Found + closed this pass:** this one-time check was previously bypassable by reassigning `self._domain` after construction to a descriptor declaring a reserved column as a value column, reaching the insert loop and silently clobbering the stamped `user_sub` to `None` via `defaults().get()`'s omission fallback (§3 items 11, §0). `_domain` is now immutable after `__init__` (`ConsoleStoreBase.__setattr__`), so `validate_domain()` runs exactly once and its result can never be superseded. |
| A3 | `order_by: ClassVar[tuple]` | Restricted to `ORDERABLE_RESERVED = {id, created_at_ms, updated_at_ms}` ∪ key columns — never a value column, never `user_sub`. Ordering reorders an already-correctly-scoped result set; it cannot add or remove rows. |
| A4 | `default_list_limit` / `max_list_limit` | Bound the CALLER's own page size only (`_clamp_limit`); even an inflated `max_list_limit` returns more of the caller's OWN rows, never another user's. |
| A5 | `cap()` hook (live, every write) | Validated every call (`validate_cap`); enforced against `_scoped_count` — a `@final`, sealed, base-owned aggregate built from `_scoped_select`'s own subquery. A domain cannot supply its own COUNT (`lesson-aggregate-queries-must-be-user-scoped-20260913`). |
| A6 | `defaults(key)` hook | Receives a plain dict COPY of the key (mutating it cannot rewrite the real key — `test_defaults_mutating_its_key_view_cannot_change_the_key`); its return value passes through `_hook_output()`, which refuses any `RESERVED_COLUMNS` name and any unknown column name. |
| A7 | `merge(current, patch)` hook | Receives plain dicts of ONLY the domain's declared value columns (never id/user_sub/trace_id/session_id/deleted_at_ms/created_at_ms — `test_hooks_only_ever_receive_plain_dicts`); return value passes through the same `_hook_output()` refusal; even when the hook fabricates a reserved name in its patch (`BlankTraceMergeDomain`), the base's `_update_values` DIRECTLY overwrites `updated_at_ms`/`trace_id`/`session_id` from the stamp AFTER the hook returns — direct assignment always wins. |
| A8 | `equality_filters()` hook (live, every query) | Validated every call (`validate_equality_filters`): may only name columns already in `key_columns ∪ value_columns` — both guaranteed disjoint from `RESERVED_COLUMNS` by A2 (now permanently, per the A2 fix). Composed with AND: can only NARROW, never widen or replace the base's own `user_sub ==` / `deleted_at_ms IS NULL` predicates. |
| A9 | `to_item(row)` hook | Receives a plain dict SNAPSHOT (never an ORM row or session — `test_hooks_only_ever_receive_plain_dicts`); mutating it cannot touch persisted state (`test_to_item_mutating_its_snapshot_cannot_touch_store_state`). |
| A10 | `snapshot_columns` / `has_session_id` / `order_columns` / `order_directions` (sealed template members on `ConsoleDomain`) | Redefinition refused at class-creation time (`seal_members`, `test_overriding_a_sealed_domain_template_member_is_refused`, inherited through the MRO — `test_domain_hierarchy_is_open_but_the_seal_is_inherited`). |

### B — what an instance of `PostgresConsoleStore`/`MockConsoleStore` exposes

| # | Surface | Why it cannot route around the invariant (or disclosed residual) |
|---|---|---|
| B11 | `self._domain` / `.domain` property | Read-only property; underlying attribute immutable after `__init__` (A2's fix, §3 item 11). Its own surface is table A above. |
| B12 | `self._sessions` (`_GuardedSessions`, Postgres only) | `__slots__ = ("_open",)` — no `session_factory` attribute anywhere (`test_store_holds_no_session_factory_or_engine_attribute`); `_open` re-resolves `user_sub` from the token-derived `UserContext` (cross-checked against the RLS ContextVar) and pushes the Postgres RLS GUC BEFORE yielding, so even `store._sessions.get_session_scoped(bob)` is anchored to bob (`test_even_the_guarded_opener_is_anchored_to_the_token`) and an unscoped `select()` run through it is still DB-scoped (§2 item 4). **Disclosed residual:** `_open.__closure__` introspection can recover the raw `session_factory` — deliberate circumvention (arbitrary code execution already assumed), not a hurry-mode path. |
| B13 | `self._rows` (Mock only) | Intentionally the ground-truth read the hostile/base tests themselves use (`_raw()` helper) — `MockConsoleStore` makes no RLS claim about protecting same-process memory; it exists for unit tests / auth-bypass mode, not a production data path. Disclosed non-goal, not a broken invariant. |
| B14 | `self._model` (Postgres only) | Plain reference to `domain.model` (already table-A surface); not query-capable without a session, and every guarded builder that combines it with a session (`_scoped_select`, `_snapshot`, `_assign`) is sealed (B15). |
| B15 | `SEALED_STORE_MEMBERS` (`_read_sub`, `_write_stamp`, `_validated_key(s)`, `_validated_values`, `_hook_output`, `_insert_values`, `_update_values`, `_delete_values`, `_clamp_limit`, `_cursor_values`, `_page`, `_to_item`, `_cap`, `_filters`, `_scoped_select`/`_scoped_rows`, `_scoped_count`, `_snapshot`, `_assign`) | Each `@final`-annotated (`test_sealed_members_are_marked_final_for_the_type_checker`) AND refused at runtime three ways: in-package class-body redefinition, class-level `setattr`/`delattr` (`_SealedMeta`), instance-level shadowing (`ConsoleStoreBase.__setattr__`). **Disclosed residuals (all require pre-existing code-execution rights, i.e. deliberate circumvention, not hurry-mode):** `object.__setattr__`/direct `__dict__` writes; `type.__setattr__(cls, ...)`; `exec(compile(src, "<forged in-package filename>"))` passes the package-fence FILE check (proven both ways: `test_forged_filename_passes_the_fence_only_when_nothing_is_redefined` shows the forged class IS created; `test_in_package_redefinition_of_a_sealed_member_is_refused` shows it STILL cannot redefine a sealed member via the same forged-filename route — so what it inherits is the intact guarded API). |
| B16 | `_SealedMeta` on the class itself | Refuses class-level `Store._scoped_select = ...` independent of any instance. Non-sealed class attributes remain freely settable/deletable by design (`test_non_sealed_class_attributes_are_still_settable_and_deletable`) — only the enumerated security-relevant surface is sealed; over-sealing the whole namespace would violate the addendum's own "ergonomic is not impossible" standard. |
| B17 | `UserContext` argument to every public method | A frozen dataclass the CALLER constructs (token-derived at the route layer, outside this WU). Never trusted blindly: `resolve_user_sub()` cross-checks `user_id` against the independently-bound RLS ContextVar and fails closed on mismatch, BEFORE any I/O (`TestScopeAnchoring`, 4 tests). |

No item above is left un-enumerated; no item asserts more non-vacuity than §3
actually established.

## 6. Static gates — and a real PEP-695-vs-pinned-mypy gate conflict found + fixed

**Finding: `.venv/bin/mypy` (2.3.1, the project's own installed version) is NOT the
actual mechanically-enforced gate.** CI has no separate mypy step (`grep -n mypy
.github/workflows/*.yml` matches only comments); the ONLY mypy enforcement is the
local pre-commit hook, pinned `rev: v1.8.0` — a version that predates PEP 695
(`class Foo[T]:`) support entirely. The prior builder's code used PEP 695 native
generics throughout (`class ConsoleDomain[T](ABC)`, `class ConsoleStoreBase[T]
(ABC, ...)`, etc.). `.venv/bin/mypy` (2.3.1) parses this fine and reported clean —
**but `git commit` itself failed**, because the pinned pre-commit hook cannot parse
PEP 695 at all and silently treats every such class as non-generic, producing 41
errors (`"ConsoleDomain" expects no type arguments, but 1 given`, `Name "T" is not
defined`, etc.) across all 4 generic classes and every parameterized use of them.
This is exactly why "mirror every CI gate locally" means the PINNED tool, not
whichever version happens to be newest in `.venv` — the mismatch would never have
surfaced from a `.venv`-only mypy check.

**Fix (scoped to this WU's own new files, no shared-config change):** converted all
4 class definitions (`ConsoleDomain`, `ConsoleStoreBase`, `PostgresConsoleStore`,
`MockConsoleStore`) from PEP 695 syntax to classic `typing.TypeVar` + `Generic[T]`
— which is, additionally, EXACTLY what the parent spec's own design-requirements
section specifies ("Generic ABC via `typing.Generic[T]` (PEP 484)"), so this is a
spec-compliance fix as well as a gate fix, not merely a workaround. This immediately
collided with the OTHER pinned tool: ruff's `pyupgrade` (`UP046`, `target-version =
"py312"`) recommends the OPPOSITE — PEP 695 syntax — for exactly these two class
definitions. Both tools are correct within their own pinned version's rules; they
simply disagree, because this is the FIRST generic class ever added to this
codebase and the repo has never had to resolve the conflict before. Resolved with a
narrow, commented `# noqa: UP046` on the two base-class definitions (not the two
concrete stores, which don't trigger the rule) — scoped to exactly the two lines in
conflict, with the reasoning written inline so a future maintainer (or the bump of
the mypy pin, whenever that happens through its own change) can find and remove it.
**Disclosed as a deviation-with-reason, not silently absorbed:** a permanent fix
would be bumping `.pre-commit-config.yaml`'s mypy `rev` to a version with PEP 695
support, but that is a repo-wide tooling change with its own blast radius (it could
surface new errors, or fewer, across the whole codebase) — out of scope for a
finishing pass on one WU, and belongs behind its own change if the operator wants it.

Separately, the pinned mypy also caught one PRE-EXISTING issue the newer `.venv`
mypy did not: `_cursor.py::_strict_int`'s `return value` after a compound
`isinstance(value, bool) or not isinstance(value, int)` guard did not narrow to
`int` in v1.8.0's narrower analysis (`Returning Any from function declared to
return "int"`), while 2.3.1 narrows it fine. Fixed with a no-behavior-change
`return int(value)` (equivalent for an already-`int` value).

**All 4 gates, re-run against the ACTUAL pinned versions, after both fixes:**

```
$ .venv/bin/ruff check src/audittrace/services/console_store/ src/audittrace/services/console_tool_favorites.py \
    tests/console_store_fixture_domain.py tests/test_console_store_base.py \
    tests/test_console_store_hostile.py tests/test_console_store_rls_postgres.py \
    tests/test_console_tool_favorites_domain.py
All checks passed!

$ .venv/bin/ruff format --check <same file set>
15 files already formatted

$ .venv/bin/pre-commit run mypy --files <every new/modified .py file>   # THE pinned v1.8.0 gate
mypy.....................................................................Passed

$ .venv/bin/mypy src/audittrace/services/console_store/ src/audittrace/services/console_tool_favorites.py
Success: no issues found in 10 source files
```

No chart touched → `make helm-lint` N/A for this WU. No new migration → no Alembic
head change (the migrated domain reuses migration 030's existing
`console_tool_favorites` table verbatim).

## 7. Trailer check

```
$ git log -1 --format='%B' | grep -i 'co-authored\|claude-session'
(no output)
```
