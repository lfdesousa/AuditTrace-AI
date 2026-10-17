# Evidence — fix round 4, ConsoleStoreBase F7 (per-field neuter, not batched), session 2026-09-17

Answers `review-verdict-consolestorebase-wu-a-fixround3-20260917.outcome-reject`
F7, governed by the SAME spec as round 3,
`2026-09-17-SPEC-ADDENDUM-D-sealing-mutation-is-not-sealing-value.md`
(`sha256:5c41a95580b9c67f05b07bdb30ffa44939cf2a361660caa8f4c1a07f405a34e3`).
**No new spec addendum — F7 is a TEST/EVIDENCE defect, not a code defect
(the reviewer's own framing: "the shipped code is correct").** The shipped
fix (`93dba14`) is unchanged by this round; only tests + this evidence
file + a module split are added.

## The headline

The round-3 reviewer built an independent per-surface neuter (a pytest
plugin outside the repo, on `PYTHONPATH`, wrapping `ConsoleStoreBase.
__init__` to swap `self._contract` for a proxy that re-reads the LIVE
domain for exactly ONE field, serving the cache for all others). Run once
per field against the round-3 committed suite (214 tests):

| field neutered ALONE | committed suite (round 3) | verdict |
|---|---|---|
| `value_columns` | 5 FAILED | proven non-vacuous |
| `key_columns` | 1 FAILED | proven non-vacuous |
| `model` | 2 FAILED | proven non-vacuous |
| `order_by` | 1 FAILED | proven non-vacuous |
| `has_session_id` | 214 passed — GREEN | **load-bearing, unproven** |
| `snapshot_columns` | 214 passed — GREEN | **load-bearing, unproven** |
| `order_columns` | 214 passed — GREEN | **load-bearing, unproven** |
| `order_directions` | 214 passed — GREEN | **load-bearing, unproven** |
| `max_list_limit` | 214 passed — GREEN | **load-bearing, unproven** |

`TestHasSessionIdPinned`'s assertion (`"session_id" in raw[row["id"]]`)
was a dict-key-presence tautology: `SqliteHarness.raw_rows()` builds every
row from `WIDGET_COLUMNS`, which names `"session_id"` unconditionally
(`tests/console_store_fixture_domain.py:75`), so the key is present
whether or not a live re-read of `model` nulled the STAMPED VALUE. The
round-3 evidence file's "manually verified non-vacuous" paragraph for the
four siblings pointed `store._contract` at the LIVE DOMAIN AS A WHOLE —
a BATCHED neuter (`feedback_neuter_guards_individually_never_batched`),
so the `ArgumentError` it reported for `has_session_id` was actually
`model`'s failure, not `has_session_id`'s; the parenthetical "its attack
vector IS a hostile model" is FALSE — `has_session_id()` re-reads
`self.model` live and is not pinned by capturing `model` alone.

## The fix (this round): per-field proofs, one neuter per run

No production code changed. Five new/corrected test proofs, each a
per-field method-level neuter mirroring
`test_toctou_value_columns_sealed.py`'s own established pattern (a
byte-for-byte copy of the ONE consuming method, with ONLY the field under
test switched from `self._contract.X` — cached — to a live
`self._domain.X` / `self._domain.X()` read, installed via
`type.__setattr__` on the sealed member, restored in `finally`
regardless of outcome).

### 1 — `has_session_id`

`tests/console_store/test_toctou_declarative_surfaces_sealed.py`:

* `TestHasSessionIdPinned::test_a_hostile_post_construction_model_does_not_flip_has_session_id`
  — corrected assertion: `bind_session_id("sess-honest-0001")` before the
  write, then `raw[row["id"]]["session_id"] == "sess-honest-0001"` (a
  REAL value witness, not `"session_id" in raw[...]`).
* `TestHasSessionIdNonVacuous::test_neutering_the_read_once_fix_drops_session_id_silently`
  — `_insert_values` patched so ONLY `has_session_id` is read live
  (`value_columns` stays cached); RED: `raw[row["id"]]["session_id"] is
  None` (the stamp silently dropped — the SAME harm class F6 proved for
  `trace_id`, `session_id` this time); restore; a FRESH store then
  re-confirms GREEN (`raw2[row2["id"]]["session_id"] ==
  "sess-honest-0002"`).

**Direct sanity check (this session, not part of the committed suite):**
reverting `_base.py`'s three `if self._contract.has_session_id:` guards to
`if self._domain.has_session_id():` (the pre-fix shape) and re-running
`TestHasSessionIdPinned` alone:

```
$ .venv/bin/python -m pytest tests/console_store/test_toctou_declarative_surfaces_sealed.py::TestHasSessionIdPinned -q --no-cov
FAILED …TestHasSessionIdPinned::test_a_hostile_post_construction_model_does_not_flip_has_session_id
1 failed in 0.11s
```

RED under the reverted source, GREEN on the actual (unmodified) commit —
the corrected test is non-vacuous by direct source-level confirmation, in
addition to the in-tree method-neuter proof above. Source reverted via
`git checkout` immediately after (tree confirmed clean).

### 2 — `snapshot_columns`, `order_columns`, `order_directions`, `max_list_limit`

New file `tests/console_store/test_toctou_derived_contract_fields_sealed.py`
— one hostile fixture + one GREEN test + one RED→GREEN neuter/restore test
per field:

| field | hostile fixture | patched method (ONE field live, rest cached) | harm reproduced |
|---|---|---|---|
| `snapshot_columns` | `TocTouSnapshotColumnsDomain` (`value_columns` safe `("payload","priority")` read #1, hostile `("payload",)` after) | `PostgresConsoleStore._snapshot` | read path silently drops `priority` from the item |
| `order_columns` | `TocTouOrderByLengthDomain` (`order_by` safe 3-tuple read #1, hostile 1-tuple after) | `PostgresConsoleStore.list` (columns list only) | `ValueError: zip() argument 2 is longer than argument 1` in `keyset_predicate` |
| `order_directions` | same `TocTouOrderByLengthDomain`, fresh instance | `PostgresConsoleStore.list` (directions arg only) | `ValueError: zip() argument 2 is shorter than argument 1` in `keyset_predicate` |
| `max_list_limit` | `TocTouMaxListLimitDomain` (safe `50` read #1, hostile `99999` after) | `ConsoleStoreBase._clamp_limit` | `_clamp_limit(99999)` returns `99999` — unbounded page size |

Each field's GREEN test confirms the fixture is hostile post-construction
(`store.domain.<attr>` read directly), then exercises the FIXED code
through the public API (`upsert`/`get`/`list`/`_clamp_limit`) and asserts
the value that would have been wrong under a live re-read. Each field's
non-vacuity test installs the ONE-FIELD-LIVE patched method via
`type.__setattr__`, reproduces the harm from the table above, restores
the original in `finally`, asserts `ConsoleStoreBase.__dict__["_clamp_limit"]
is original` (or the `PostgresConsoleStore` equivalent), and re-confirms
GREEN on a fresh store.

**Direct sanity check (this session):** reverting each of the four real
`self._contract.X` reads to `self._domain.X` (live) in `_postgres.py` /
`_base.py` and re-running the corresponding `test_the_fixed_code_*` test
alone reproduces the EXACT harm from the table:

```
$ # snapshot_columns reverted in _postgres.py:
KeyError: 'priority'   (item["priority"] no longer present)

$ # order_columns reverted in _postgres.py:
ValueError: zip() argument 2 is longer than argument 1

$ # order_directions reverted in _postgres.py:
ValueError: zip() argument 2 is shorter than argument 1

$ # max_list_limit reverted in _base.py:
assert fixed_store._clamp_limit(99999) == 50   # AssertionError: 99999 != 50
```

Each source file reverted via `git checkout` immediately after (tree
confirmed clean each time — `git status --porcelain` empty).

## Correcting the round-3 evidence file's claim (A-REC precedent — do not rewrite history)

`evidence/fix-round-3-consolestorebase-value-columns-toctou-2026-09-17.md:105-113`
("Manually verified non-vacuous... `has_session_id`: the same
`ArgumentError` shape as `model` (its attack vector IS a hostile
`model`)") is **left as-is, immutable** — the same discipline that file's
own A-REC section applied to the round-2 file's "421 LOC" claim ("a
correction stated here, not a rewrite of the round-2 file"). The
correction: **that paragraph's method was a BATCHED whole-`_contract`
neuter, not a per-field one, and the `ArgumentError` it observed was
`model`'s failure surfacing through a `has_session_id`-labelled run, not
proof that `has_session_id` is independently load-bearing.** The
parenthetical "its attack vector IS a hostile model" claimed a redundancy
that was never checked by neutering `has_session_id` ALONE — disproven
this round (`TestHasSessionIdNonVacuous`, above): with `model` STILL
CACHED and only `has_session_id` read live, a hostile `model` (via the
independent `has_session_id()` template-method call, which re-reads
`self.model`) still nulls the `session_id` stamp. `has_session_id` is its
own, independently load-bearing capture, not implied by `model`'s.

`test_toctou_declarative_surfaces_sealed.py`'s own module docstring is
corrected in place (this round, not a separate immutable artefact — it is
a test-file docstring, not a ratified spec or a prior evidence file) with
the same retraction, per `feedback_ratified_spec_immutable`'s scope: that
lesson protects RATIFIED SPECS and prior EVIDENCE FILES, not a test
module's own explanatory prose, which this fix round is explicitly tasked
with correcting.

## Reconciled file count (A-REC precedent, item 4)

Round-3 build record: "152 files checked". Round-3 reviewer's own
reproduction on the SAME commit (`93dba14`): "154 files checked". This
round's fresh `make test` run, on `93dba14` plus this round's test-only
commits:

```
per-file coverage gate: PASS (154 files checked, lines >= 90%, branches >= 90% on 130 file(s) with branches)
```

**154 is correct** — matches the independent reviewer's own reproduction
on the unmodified `93dba14` commit. The round-3 build record's "152" was
wrong; not corrected in place (immutable prior artefact), corrected here.

## Module split (F7 advisory item 5)

`tests/console_store/test_domain_descriptor_sealed.py` had grown to 533
LOC (past the PYTHON-ENGINEERING §11 500-LOC trigger) across four guards
that do not change together (A/D: descriptor sealing; B/B': pointer
write-once; C: reserved-column refusal at point of use). Split by guard,
no test body changed — only the file each class lives in:

| file | guards | LOC | tests |
|---|---|---|---|
| `test_domain_descriptor_sealed.py` | A, D | 220 | 8 |
| `test_domain_reassignment_sealed.py` | B, B' | 182 | 4 |
| `test_domain_reserved_column_guard_sealed.py` | C | 191 | 2 |

14 tests before the split, 14 after (8+4+2) — same tests, same
assertions, new files.

## Gates (this session)

- `make test`: **5417 passed** (round 3: 5408; +9 = the `has_session_id`
  non-vacuity test (+1) and four fields × 2 tests each (+8)), per-file
  coverage gate PASS (154 files checked, lines+branches ≥90%), zero-skip
  policy PASS. Total coverage 98.99%.
- `console_store` package: **100% lines and 100% branches on every
  file** (`_base.py`, `_context.py`, `_cursor.py`, `_domain.py`,
  `_domain_validate.py`, `_errors.py`, `_mock.py`, `_postgres.py`,
  `_sealing.py`, `__init__.py`) — read from the full-suite `coverage.xml`.
- `ruff check` / `ruff format --check`: clean on every file this round
  touched. `mypy` (`.venv`): clean on every file this round touched.
- Frozen invariants: `git diff ac5f4fa..HEAD -- src/audittrace/routes/
  dependencies.py db/ bff/ charts/ server.py chat.py` empty (unchanged
  from round 3 — no production code touched this round).
- Trailers: no `Co-Authored-By`, no `Claude-Session`, no "Generated with"
  on any commit this round.
- Portability: no hardcoded host/URL/IP/path/credential in any file this
  round touched (test-only changes).

## Standing gate

This round is TEST/EVIDENCE-ONLY, per the round-3 reviewer's own framing
("the shipped code is correct; what is missing is proof"). No domain may
be retrofitted onto `ConsoleStoreBase` until a round PASSES independent
review.
