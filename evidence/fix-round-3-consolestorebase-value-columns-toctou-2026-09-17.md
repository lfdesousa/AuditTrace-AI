# Evidence — fix round 3, ConsoleStoreBase value_columns TOCTOU (F6), session 2026-09-17

Answers `review-verdict-consolestorebase-wu-a-fixround2-20260917.outcome-reject`
(F6), governed by
`2026-09-17-SPEC-ADDENDUM-D-sealing-mutation-is-not-sealing-value.md`
(`sha256:5c41a95580b9c67f05b07bdb30ffa44939cf2a361660caa8f4c1a07f405a34e3`),
amending the chain: parent `2026-09-13-SPEC-…-abstractions.md` →
`…ADDENDUM-A…` → `fcd9f1d0…` (fix round 1) → `0596986a…` (frozen-dataclass
WITHDRAWN) → `fb1a10f5…` (ADDENDUM C, fix round 2) → this addendum.

## The headline

Every guard through fix round 2 answers "can `value_columns` be
MUTATED?" (axis 1: Guards A/B/C/D). None answers "does a read of it
always return the SAME value?" (axis 2). A domain subclass shaping
`value_columns` as a stateful `property` — or `__getattribute__`
override — answers `validate_domain()`'s reserved-column check with a
SAFE tuple and a LATER read, inside `_insert_values`'s row-assignment
loop, with a HOSTILE one that silently nulls the base-stamped `trace_id`
(M5 / EU AI Act Art 12). No monkeypatch, no `object.__setattr__`, no
metaclass swap — the sanctioned extension point (an ordinary domain
subclass) is enough.

## The fix

`validate_domain()` (now split into `_domain_validate.py` — see "module
sizes" below) reads every declarative surface EXACTLY ONCE into a local
and returns them captured in an immutable `DomainContract`.
`ConsoleStoreBase.__init__` caches it as `self._contract` (write-once,
the same generalised instance-attribute guard as `self._domain`/
`self._sessions`). Every sealed helper in `_base.py`/`_postgres.py`/
`_mock.py` reads `self._contract.X` for these fields from then on —
NEVER `self._domain.X` again. The domain's DYNAMIC hooks (`cap()`,
`defaults()`, `merge()`, `equality_filters()`, `to_item()`) stay
live-invoked on `self._domain` exactly as before — those are meant to
vary per call, and their output is validated against the CACHED
`key_columns`/`value_columns`, never a fresh domain read.

## Live witness (this session, real OTel span + raw SQLite row)

Script run standalone against a fresh SQLite harness — construct with a
`TocTouPropertyDomain` (safe on read #1, hostile on every read after),
one real write through the fixed code, then the SAME fixture through a
neutered `_insert_values` (restored to its pre-ADDENDUM-D shape, reading
`self._domain.value_columns` live):

```
$ PYTHONPATH=. .venv/bin/python /tmp/.../f6_witness.py
[FIXED] span trace_id=f8349bc46c7744933ec86986820aef95
[FIXED] DB ROW trace_id=f8349bc46c7744933ec86986820aef95
[NEUTERED] DB ROW trace_id=None
F6 witness: FIXED stamps correctly, NEUTERED reproduces the null — non-vacuous.
```

The FIXED half proves the row survives despite the domain having already
flipped hostile by construction time (`store.domain.value_columns` reads
`("payload", "priority", "trace_id")` immediately after `__init__`
returns — verified directly in `test_toctou_value_columns_sealed.py`).
The NEUTERED half — restoring `_insert_values` to the exact pre-fix
shape via `type.__setattr__`, then restored in `finally` — reproduces the
ORIGINAL F6 harm on a fresh instance: this is the non-vacuity proof
(Addendum B Req 1), not a bare "no exception was raised".

## Both variants (property AND `__getattribute__`) — one fix covers both

`tests/console_store/test_toctou_value_columns_sealed.py`:

| Variant | GREEN (fixed) test | RED→GREEN (neuter/restore) test |
|---|---|---|
| `property` | `TestPropertyVariantFixed::test_stamp_survives_the_fixed_code` | `::test_neutering_the_read_once_fix_reopens_the_property_toctou` |
| `__getattribute__` | `TestGetattributeVariantFixed::test_stamp_survives_the_fixed_code` | `::test_neutering_the_read_once_fix_reopens_the_getattribute_toctou` |

Both neuter tests: restore `ConsoleStoreBase._insert_values` to the
pre-ADDENDUM-D shape (`type.__setattr__`, the disclosed bypass every
sealed-member neuter in this suite uses) → RED (raw DB shows
`trace_id=None`) → restore in `finally` → a FRESH store (fresh counter)
confirms GREEN again. `cmp`-identical restore: `_base_module.
ConsoleStoreBase.__dict__["_insert_values"] is original` asserted after
restore in both.

`TestCrossUserEscalationStillFails` pins the spec's own honest-scope
note as a regression: bob cannot read alice's row through a
`TocTouPropertyDomain`-shaped store — the escalation attempt the
reviewer tried and failed stays failed.

## The four siblings, audited — DONE, not merely disclosed

`key_columns`, `model`, `order_by`, `has_session_id` (derived from
`model`) are read off `self._domain` in the exact same "live attribute
access, no caching" shape `value_columns` was. All four are pulled into
the SAME `DomainContract` capture, by the SAME single-read mechanism —
not four independent guards, one mechanism.
`tests/console_store/test_toctou_declarative_surfaces_sealed.py`, one
class per sibling, each: safe on the ONE `validate_domain()` read,
hostile on every read after, a REAL store operation through the public
API, non-equality assertion on the write's actual outcome:

| Sibling | Test | What it asserts |
|---|---|---|
| `key_columns` | `TestKeyColumnsPinned` | `upsert` still writes BOTH key columns (`kind` AND `name`) though the live property would answer with only `("kind",)` |
| `model` | `TestModelPinned` | `upsert` still writes a correctly-stamped row though the live property would answer with a non-ORM `object` |
| `order_by` | `TestOrderByPinned` | `list()` still orders `updated_at_ms desc` though the live property would answer with an ordering by a nullable value column |
| `has_session_id` | `TestHasSessionIdPinned` | the row still carries `session_id` though the live `model` would answer with a class lacking that column |

Manually verified non-vacuous (ad hoc, not part of the committed suite):
pointing `store._contract` at the live domain (simulating "no caching
happened") for each of the four fixtures reproduces a real failure —
`key_columns`: `ValueError: key must name exactly ['kind']`; `model`:
`sqlalchemy.exc.ArgumentError: ... got <class 'object'>`; `order_by`:
`TypeError: 'method' object is not iterable`; `has_session_id`: the same
`ArgumentError` shape as `model` (its attack vector IS a hostile
`model`). Confirms the four committed tests are tied to real behaviour,
not tautologies.

### The two-axis table (A-ENUM: structured list restored + extended)

Round 1 had a structured per-accessor enumeration; round 2 dropped it for
a requirement→fix table (A-ENUM finding). Restored here, extended with
axis 2 per R3:

| Surface | Axis 1 — reassignment refused by | Axis 2 — constancy pinned by |
|---|---|---|
| `name` | Guard A (instance) + Guard D (class) | `DomainContract.name`, read once in `validate_domain()`, never re-read |
| `model` | Guard A + Guard D | `DomainContract.model`, read once; every internal `self._domain.model` use replaced with `self._contract.model` |
| `key_columns` | Guard A + Guard D | `DomainContract.key_columns`, read once; `_validated_key`/`_validated_keys`/`batch_get` read the cache |
| `value_columns` (F6) | Guard A + Guard D | `DomainContract.value_columns`, read once; `_refuse_reserved_value_columns`/`_hook_output`/`_insert_values`/`_update_values` all read the cache |
| `order_by` | Guard A + Guard D | `DomainContract.order_by`/`.order_columns`/`.order_directions`, computed once inside `validate_domain()` |
| `has_session_id` (derived) | sealed template member, redefinition refused at class-creation | `DomainContract.has_session_id`, computed once from the cached `model` |
| `default_list_limit`/`max_list_limit` | Guard A + Guard D | `DomainContract.default_list_limit`/`.max_list_limit`, read once; `_clamp_limit`/`_validated_keys` read the cache |
| `cap()`/`defaults()`/`merge()`/`equality_filters()`/`to_item()` | N/A — HOOKS, meant to be live methods | re-validated on EVERY call (`validate_cap`/`validate_equality_filters`, checked against the CACHED key/value columns — R1's "re-validate atomically at use" branch) |

## Corrected absolutes (F6 falsifies two)

* `_base.py`'s module docstring: *"they stamp the reserved columns
  unconditionally"* → corrected to describe the fix (iterate the CACHED
  `self._contract.value_columns`) and disclose that "unconditionally" was
  FALSE under the pre-fix live-read shape.
* `_domain.py`'s module docstring (second-hop paragraph): *"`self._domain.
  value_columns` always resolves to the class attribute"* → corrected to
  scope the claim to an ORDINARY `ClassVar` (true) and explicitly note a
  `property`/`__getattribute__` override makes it resolve to a
  COMPUTATION instead (the fifth-hop defect).

Widened grep re-run (this session's own new prose, plus every file this
round touched):

```
$ grep -noE "\b(always|never|unconditionally|cannot|guarantee[ds]?|un-?enumerated)\b.{0,90}" \
    src/audittrace/services/console_store/_domain.py \
    src/audittrace/services/console_store/_domain_validate.py \
    src/audittrace/services/console_store/_base.py \
    src/audittrace/services/console_store/_postgres.py \
    src/audittrace/services/console_store/_mock.py \
    tests/console_store/test_toctou_value_columns_sealed.py \
    tests/console_store/test_toctou_declarative_surfaces_sealed.py \
    tests/console_store/test_domain_descriptor_sealed.py
```

Every `always`/`unconditionally` hit read and checked against the code:
either a scoped, tested claim (`_filters()` "always passes the CACHED …"
— true by inspection of the four call sites) or already corrected above.
Zero occurrences of the two round-1/round-2 falsified phrasings ("runs
exactly once", "can never be superseded").

## A-LABEL-2 (fifth and last site)

`tests/console_store/test_sealed_classes.py:103` still called Guard C
"defence in depth" — the exact guard R5 (fix round 2) relabelled
LOAD-BEARING. Fixed; docstring now reads "F1 item 2 — LOAD-BEARING, not
defence in depth (SPEC ADDENDUM C R5 relabelled it)".

## A-REC (build-record LOC/test-count claim)

The round-2 evidence file (`evidence/fix-round-2-…md:112`) states the new
test file as "421 LOC"; measured now: `test_extension_point_sealed.py` is
**405 LOC, 14 tests** (`grep -c "    def test_\|    async def test_"`).
This is a correction stated here, not a rewrite of the round-2 file
(immutable history — no operator authorisation was given this session to
reword it, unlike the `9a7f8ee` precedent). This session's OWN new files,
measured the same way: `test_toctou_value_columns_sealed.py` 268 LOC / 5
tests; `test_toctou_declarative_surfaces_sealed.py` 270 LOC / 7 tests.

## Module sizes (PYTHON-ENGINEERING §11, <500 LOC)

Fixing F6 pushed `_domain.py` to 670 LOC and `_base.py` to 567 LOC —
both over the trigger. `_domain.py` split: `ConsoleDomain` (what a domain
IS) stays in `_domain.py` (461 LOC); `DomainContract` +
`validate_domain`/`validate_cap`/`validate_equality_filters` (how a
domain gets VALIDATED) moved to new `_domain_validate.py` (234 LOC).
`_base.py`'s module docstring trimmed (the full two-axis table moved
here, to this evidence file, rather than duplicated in the docstring) to
498 LOC. Final sizes: `_base.py` 498, `_domain.py` 461,
`_domain_validate.py` 234, `_postgres.py` 398, `_mock.py` 181 — all
<500.

## Gates (this session)

- `make test`: **5408 passed**, per-file coverage gate PASS (152 files
  checked, lines+branches ≥90%), zero-skip policy PASS.
  `console_store` package: **100% lines+branches on every file**
  (`_base.py`, `_domain.py`, `_domain_validate.py`, `_postgres.py`,
  `_mock.py`, `_sealing.py`, `_errors.py`, `_context.py`, `_cursor.py`,
  `__init__.py`) — the previously-uncovered sealed template members
  (`snapshot_columns`/`has_session_id`/`order_columns`/
  `order_directions`, now dead from the STORE's own internals since it
  reads the cache instead) are covered directly by
  `TestSealedTemplateMembersStillCorrect`, proving they remain correct,
  callable PUBLIC API even though nothing in this package calls them
  internally any more.
- Real-Postgres RLS (`tests/test_console_store_rls_postgres.py`, Docker
  `postgres:16`, non-superuser `NOBYPASSRLS`, `FORCE ROW LEVEL
  SECURITY`): 4/4 passed, included in the `make test` run above.
- `routes/`/`dependencies.py`/`db/`/`bff/`/`charts/` diff vs `ac5f4fa`:
  empty. `/v1` byte-inviolate (empty diff on `chat.py`/`server.py`).
- `tests/test_console_tool_favorites_routes.py` +
  `tests/test_console_tool_favorites_service.py`: unmodified (0-line
  diff vs `ac5f4fa`), 59/59 passed — the round-1 numeric SETTLED fact
  (59, not 67) re-confirmed unchanged.
- mypy clean on `.venv` AND the pinned pre-commit hook (v1.8.0), all
  touched `src/` files (`pre-commit run mypy` + `pre-commit run
  --all-files`, both green). The one full-`src/` mypy error
  (`trust_store.py:610`) is pre-existing — reproduced identically on
  `git stash` (HEAD before this session's changes).
- `ruff check` + `ruff format --check` clean.
- No `Co-Authored-By` / `Claude-Session` trailer on this commit
  (`git log -1 --format='%B'` — checked, empty).
