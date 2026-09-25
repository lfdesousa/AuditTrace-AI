# Evidence — Mongo-audit WU-A1 (classification + verdict core), 2026-09-25

**Spec:** `~/work/audittrace-private/specs/2026-09-25-SPEC-mongo-audit-sanctioned-read-route-CONSOLIDATED-v5.md`
(`sha256:b47de202681f36114e5568531e8ed11d320dbc63e3c4a976292d34663035ff72`).

**Scope:** WU-A1 only — `src/audittrace/services/mongo_audit_classification.py` and
`src/audittrace/services/mongo_audit_verdict.py`, plus their test files. Pure code: no HTTP, no
`pymongo`, no config, no chart. WU-A2 (route/reader/config/Bruno) and WU-B (chart/live) are out of
scope and not built here.

## Rule 1 — Verification (unit + coverage)

`make test` (the actual Makefile target — full repo suite incl. `bff`/`scripts/*`, with
`--junit-xml` for the zero-skip check): **5778 passed, 0 failed, 0 skipped** (2026-09-25,
646.90s / 0:10:46), total coverage 99.04%. Per-file coverage gate
(`scripts/check-per-file-coverage.py`, invoked by `make test` itself): **PASS** (161 files
checked, lines ≥ 90%, branches ≥ 90% on 137 files with branches — register D22: cite PASS/FAIL
only, never a percentage). Zero-skip policy (`scripts/check-no-skipped-tests.py`, also invoked by
`make test`): **PASS** — "No skipped tests in junit.xml. Good." The two new modules themselves,
from that run's own `coverage.xml`:

| File | Statements | Missed | Branches | Missed | Line % | Branch % |
|---|---|---|---|---|---|---|
| `src/audittrace/services/mongo_audit_classification.py` | 21 | 0 | 4 | 0 | 100% | 100% |
| `src/audittrace/services/mongo_audit_verdict.py` | 94 | 0 | 24 | 0 | 100% | 100% |

`ruff check` + `ruff format --check`: PASS on all four new/changed files. `mypy`: PASS on both the
`.venv` (2.3.1) and the pinned `v1.8.0` pre-commit hook (`pre-commit run mypy --files ...`).
`alembic heads`: single head `742a8b743c94` — unchanged; A1 adds no migration.

## Rule 2 — Validation (public API + scoped JWT, deployed image)

**Not applicable to WU-A1 by construction, per the ratified spec itself** ("⚠ ADR-049 Rule 2 is
unmeetable in A1 by construction (no route). Do not add one to satisfy it."). This WU adds no
route, no FastAPI dependency, no deployable surface — `compute_audit()` and `build_response()`
are pure functions over plain dict/list inputs, proven entirely by Rule 1 + Rule 3 below. The
route that will make Rule 2 meaningful is WU-A2's `GET /system/mongo-audit`, dispatched only
after this WU is independently reviewed (spec §11.2). Citing this note here rather than
fabricating a live call is the fail-closed choice the spec names explicitly.

## Rule 3 — Reconstruction

For pure code with no HTTP surface, the reconstruction artefact is the **per-guard neuter
table**: every guard is broken (RED), observed, then byte-identically restored (`diff -q` against
a pre-neuter backup) and re-confirmed GREEN, one guard at a time
([[feedback_neuter_guards_individually_never_batched]]). Every neuter below was run against the
*production* source files in this worktree (`/home/lfdesousa/work/at-wt-mongo-audit-a1`), not a
simulated copy — the one intentional exception is noted at T7-5's row.

| Test | Guard | Neuter applied | Result before restore | Restored + confirmed GREEN |
|---|---|---|---|---|
| T3 | Verdict algebra | `control_passed = True` (unconditional) | RED — 2 tests fail (`users=0` → wrongly PASS-eligible; FAIL-precedence case) | Yes — `diff -q` clean, 3/3 pass |
| T5a | Errors never coerced to 0 | "name in neither map" branch returns `(0, None)` instead of `(None, "missing")` | RED — 1 test fails (missing-report case) | Yes — 3/3 pass |
| T5b | Errors win over counts | Swapped precedence: check `counts` before `errors` | RED — 1 test fails (both-maps case) | Yes — 3/3 pass |
| T6 | Classification typo never silently passes | `classify()` does a case-insensitive lookup (`.lower()` both sides) | RED — 1 test fails (the deliberately-wrong-case mapping key now matches) | Yes — 14/14 pass |
| T6b-1 | `system.` prefix rule, not wider | `_OPERATIONAL_PREFIX` widened from `"system."` to `"system"` | RED — 1 test fails (`systemfoo`/`xsystem.views` wrongly OPERATIONAL) | Yes — 6/6 pass |
| T6b-2 | `system.buckets.` carve-out | Dropped the carve-out condition entirely | RED — 1 test fails (`system.buckets.readings` wrongly OPERATIONAL) | Yes — 13/13 pass |
| T12 | Classification census + version | Flipped `agents` from USER to OPERATIONAL | RED — 3 tests fail: membership (USER/OPERATIONAL counts) **and** the pinned-literal version test | Yes — 14/14 pass |
| T7-1 | (i) keys / (ii) computed | Added `@computed_field total` on `MongoAuditResponse` | RED on (i) and (ii) as predicted | Yes — 23/23 pass |
| T7-2 | (i) keys / (iii) value | Declared a plain `total: int = 0` field | RED on (i) and (iii) as predicted | Yes — 23/23 pass |
| T7-3 | (iii) value only | `Field(serialization_alias="total")` on `duration_ms` | (i) GREEN, (iii) RED — exactly the asymmetry the alias layer exists to catch | Yes — 23/23 pass |
| T7-4 | (iii) value only | `@field_serializer("duration_ms")` returning `v + 1` | (i) and (ii) GREEN, (iii) RED — exactly as spec'd | Yes — 23/23 pass |
| T7-5 | (iv) declarations, independent of (i)-(iii) | Data-conditional `@field_serializer("count")` on `CollectionRow` (`v * 1000 if name.startswith("zz")`), applied to the **production** class | (i), (ii), (iii) GREEN (fixture rows never start with `"zz"`); **(iv) RED** on `test_no_decorators[CollectionRow]` — the asymmetry proof, run against production code, not a simulated subclass | Yes — 23/23 pass |
| T7-6 | (iv) alias declaration | `Field(alias="c")` on `CollectionRow.count` | (iv) RED on `test_no_field_carries_an_alias[CollectionRow]` | Yes — 23/23 pass |

**Restore verification:** after every T7 neuter and every classification neuter, the modified
file was overwritten with a byte-identical pre-neuter backup and diffed (`diff -q
src/.../mongo_audit_verdict.py backups/mongo_audit_verdict.py.orig` → no output, i.e. identical)
before moving to the next neuter — never a batched restore.

**T7's own permanent regression test** (`TestT7Asymmetry` in
`tests/test_mongo_audit_verdict.py`) additionally encodes the same asymmetry as a standing,
always-run test (a locally-defined `PoisonedRow(CollectionRow)` subclass with the same
data-conditional serialiser) so a future regression is caught without requiring a manual neuter
round — this is in *addition* to, not instead of, the production-code neuter (T7-5) above.

**Deterministic identity:** `CLASSIFICATION_VERSION` (the sha256 of the canonical JSON of the
47-row ratified table) is
`edbb3b70fed61e1a1d87d2e8440da16793459f70600ca6dfbc4f08ef080a515c` — recomputed from the
committed source in this worktree, pinned as a literal in
`tests/test_mongo_audit_classification.py` (never re-derived from the same mapping it is meant
to guard — see T12's row above for why that would be tautological).

**Classification census reconciliation:** 47 named collections = 34 USER + 13 OPERATIONAL,
matching spec §6 exactly (verified by `TestClassificationCensus` against an independent literal
set reproduced from the spec, not merely the module's own values).

## Rule 4 — No override

No gate was bypassed. The friction this WU hit was the ADR-049 commit-evidence pre-commit hook
firing on a `src/audittrace/services/**` change with no live HTTP surface to reference — the fix
is this evidence file (Rule 1 + Rule 3 captured, Rule 2 explicitly named not-applicable-by-spec),
not `--no-verify`.
