# Evidence — WU-6 Part A session-memory GC janitor, local capture (2026-09-06)

**Scope of this evidence file.** This is a **LOCAL-only** work unit —
Sovereign-Attach EPIC WU-6 §1 names Part A ("Session GC") as
"buildable now through the loop" (ADR-059 builder -> independent
reviewer, LOCAL gates only), while Parts B (two-sided release) and C
(deploy + live front-door E2E) are explicitly OPERATOR-GATED and out of
scope here. This file satisfies ADR-049 Rule 1 (Verification) in full
and gives a reconstructible Rule-3-shaped capture (neuter-proof of
every non-vacuity guard named in the spec, through the real production
code paths — the real `PostgresSessionMemoryService`, the real
`/memory/upload` + `/memory/promote` + `/memory/episodic` HTTP routes,
and the real `SessionGCJanitor` loop). It explicitly does **NOT**
satisfy Rule 2 (Validation through a deployed image + public API +
scoped JWT) — that is deferred to WU-6 Part C per the spec, mirroring
the `wu1-session-layer-narrow-ingest-scope-local-capture-2026-09-04.md`
/ `wu4-promote-session-to-durable-local-capture-2026-09-05.md` /
`wu5-same-turn-session-recall-local-capture-2026-09-06.md` precedents.

Spec: `2026-09-06-SPEC-wu6-session-gc-live-e2e-release.md` (sha256
`5bdcb679e123fa3acae0302658254f51b7602b3cc1b4e357ad2097e250009a92`),
Part A / §2 only.

## 1. Full test-suite run (Rule 1 — Verification)

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 98.71%
4277 passed, 2 warnings in 336.11s (0:05:36)
🔒 Enforcing per-file coverage gate (each component >= 90%)...
per-file coverage gate: PASS (105 files checked, lines >= 90%, branches >= 90% on 94 file(s) with branches)
🚫 Enforcing zero-skip policy...
[no-skip-check] No skipped tests in junit.xml. Good.
✅ Tests passed
```

New/touched-file coverage (line + branch), from the full run:

```
src/audittrace/server.py                                   189      0     36      0   100%
src/audittrace/services/session_gc_janitor.py                34      0      2      0   100%
src/audittrace/services/session_memory.py                    116      0     16      0   100%
```

(`config.py` is a pure-declarative `pydantic-settings` field addition,
no branch logic; already 100% via the pre-existing settings-field test
sweep.)

`make lint` (ruff check + ruff format + offline semgrep security-lint)
green:

```
$ make lint
...
Ran 2 rules on 159 files: 0 findings.
✅ Security-lint passed
All checks passed!
✅ Linting passed
293 files already formatted
✅ Formatting passed
```

`make helm-lint` green (chart touched — `values.yaml` GC janitor
knobs):

```
$ make helm-lint
🪖  helm lint (vault.enabled=false)...
1 chart(s) linted, 0 chart(s) failed
🪖  helm template (vault.enabled=true) + vaultSecretFileGuard count...
✅ vaultSecretFileGuard present in 3 workloads
```

`mypy` clean on every touched/new file:

```
$ .venv/bin/mypy src/audittrace/config.py src/audittrace/server.py \
    src/audittrace/services/session_memory.py \
    src/audittrace/services/session_gc_janitor.py \
    tests/test_session_gc_janitor.py tests/test_session_memory_service.py \
    tests/test_wu6_session_gc_promoted_independence.py
Success: no issues found in 7 source files
```

## 2. `/v1` byte-inviolate + `gc_expired` route-unreachability confirmation

```
$ git diff --stat -- 'src/audittrace/routes/*'
(no output — this WU touches ZERO files under routes/)

$ git diff --name-only | grep -i "v1\|chat.py"
none

$ grep -rn "gc_expired" src/audittrace/ | grep -v "services/session_memory.py\|services/session_gc_janitor.py"
(no output)
```

Also asserted mechanically in the test suite itself —
`tests/test_wu6_session_gc_promoted_independence.py::TestGcExpiredNotWiredToAnyRoute::test_no_route_module_calls_gc_expired`
AST-walks every file under `src/audittrace/routes/` and fails if any
attribute access named `gc_expired` is found.

## 3. Neuter-proof — every non-vacuity guard fails RED when broken, GREEN restored

Builder-side falsifiability pass (the independent reviewer re-runs this
mandate-to-fail check itself; this is the builder's own verification
that each guard is real before handing off). Every neuter below was
applied directly to the working file via a scripted single-line edit,
confirmed RED, then restored via the inverse edit — `diff` against a
pre-neuter backup copy confirmed byte-identical restoration after every
guard, immediately before the final `make test` run in §1 was captured.

### 3.1 `created_at_ms < older_than_ms` cutoff — spec §2.5 guard 1

Guard: the `.where(SessionMemoryItem.created_at_ms < older_than_ms)`
clause in `PostgresSessionMemoryService.gc_expired`
(`src/audittrace/services/session_memory.py`). Neutered by removing the
`.where()` clause entirely (every row becomes eligible regardless of
age).

```
$ pytest tests/test_session_memory_service.py -k "cutoff" -v --no-cov
FAILED tests/test_session_memory_service.py::TestPostgresSessionMemoryServiceGcExpired::test_gc_expired_deletes_only_rows_older_than_cutoff - assert 2 == 1
1 failed, 1 passed, 30 deselected

# restored (diff against pre-neuter backup: identical):
$ pytest tests/test_session_memory_service.py -k "cutoff" -q --no-cov
2 passed, 30 deselected in 0.09s
```

### 3.2 Batch `limit` bound — spec §2.5 guard 2

Guard: the `.limit(limit)` call in `PostgresSessionMemoryService.gc_expired`
(`src/audittrace/services/session_memory.py`). Neutered by removing the
`.limit(limit)` call (unbounded delete on a single call).

```
$ pytest tests/test_session_memory_service.py -k "batch_limit" -q --no-cov
FAILED tests/test_session_memory_service.py::TestPostgresSessionMemoryServiceGcExpired::test_gc_expired_respects_batch_limit
1 failed, 1 passed, 30 deselected

# restored (diff against pre-neuter backup: identical):
$ pytest tests/test_session_memory_service.py -k "batch_limit" -q --no-cov
2 passed, 30 deselected in 0.10s
```

The janitor-side batch-cap spy (`test_sweep_never_asks_for_more_than_the_batch_bound`
in `tests/test_session_gc_janitor.py`) independently confirms
`SessionGCJanitor._sweep_once` never passes a `limit` other than
`_SESSION_GC_BATCH_SIZE` (100) to `gc_expired` — the DB-side guard
above and this janitor-side guard are two independent layers of the
same "bounded batch" acceptance criterion.

### 3.3 Promoted-durable independence — spec §2.5 guard 3

Guard: structural — `gc_expired` only ever queries `SessionMemoryItem`;
a WU-4 promote COPIES content into a completely different table
(`MemoryItem`/S3 via `EpisodicService`), which `gc_expired` cannot
reach. Falsifiability was proven by simulating the OBSERVABLE EFFECT
of this guard being broken (a durable copy getting deleted alongside
its session origin) via a throwaway test that manually deletes the
promoted episodic doc between promote and the final read-back the real
guard test performs:

```
$ pytest tests/test_zzz_neuter_guard3_scratch.py -v --no-cov   # throwaway, not committed
FAILED test_guard3_would_catch_a_deleted_durable_copy - AssertionError: EXPECTED FAILURE — guard 3 simulation
assert 404 == 200
1 failed in 0.33s
```

This confirms `tests/test_wu6_session_gc_promoted_independence.py::
TestPromotedDurableSurvivesSessionGC::test_promoted_episodic_copy_survives_gc_of_expired_session_original`'s
final assertion (`post_gc.status_code == 200`, read through the REAL
`GET /memory/episodic/{filename}` route) is load-bearing — it goes RED
the instant the durable copy is missing, exactly the failure mode
guard 3 exists to prevent. The real test (committed) proves the
POSITIVE case end-to-end: upload via real `POST /memory/upload?layer=
session`, promote via real `POST /memory/promote`, force-collect the
session original via `gc_expired(older_than_ms=<far future>)`, confirm
the session original is gone (`read_own` -> `None`) AND the durable
copy survives (`GET /memory/episodic/{filename}` -> 200, content
intact).

```
$ pytest tests/test_wu6_session_gc_promoted_independence.py -v --no-cov
tests/test_wu6_session_gc_promoted_independence.py::TestPromotedDurableSurvivesSessionGC::test_promoted_episodic_copy_survives_gc_of_expired_session_original PASSED
tests/test_wu6_session_gc_promoted_independence.py::TestGcExpiredNotWiredToAnyRoute::test_no_route_module_calls_gc_expired PASSED
2 passed in 0.30s
```

### 3.4 `AUDITTRACE_SESSION_GC_ENABLED` flag — spec §2.5 guard 4

Guard: the `if not settings.session_gc_enabled or not settings.database_url:`
check in `server._maybe_start_session_gc_janitor`. Neutered to
`if False:` (schedules the task unconditionally regardless of the
flag).

```
$ pytest tests/test_session_gc_janitor.py -k "TestMaybeStartSessionGcJanitor" -v --no-cov
FAILED test_does_not_start_when_disabled - AssertionError: assert <Task ...> is None
FAILED test_does_not_start_without_a_configured_database - AssertionError: assert <Task ...> is None
2 failed, 1 passed, 6 deselected

# restored (diff against pre-neuter backup: identical):
$ pytest tests/test_session_gc_janitor.py -k "TestMaybeStartSessionGcJanitor" -q --no-cov
3 passed, 6 deselected in 0.03s
```

## 4. Live dry-run note — why the DB-configured half of the flag gate exists

Not a deploy-time evidence item (Part A stays local per the spec), but
a genuine **infra fix needs live dry-run** finding worth recording: an
initial implementation gated the janitor ONLY on
`AUDITTRACE_SESSION_GC_ENABLED` (default True, no secondary check).
Running the FULL test suite (not just this WU's own test files)
surfaced a real deadlock: `tests/test_routes.py::
test_list_interactions_returns_seeded_rows` hung indefinitely, because
the janitor's very first sweep ran on the `TestClient` lifespan's
anyio-portal thread/loop against the shared aiosqlite
`InMemoryPostgresFactory` engine, while the test body touched the SAME
engine from pytest-asyncio's own (different) loop — aiosqlite's
background-thread driver bridge is loop-bound, so the two loops
deadlocked. Confirmed the unmodified `main` branch's `test_routes.py`
passes in 1.5s (no such background task exists there); confirmed the
hang reproduces with the naive single-flag gate; confirmed it
disappears once the gate also requires `settings.database_url` to be
configured (mirroring `summarizer_enabled and summarizer_db_url`'s
existing precedent) — `test_routes.py` passes in 1.67s with the fix.
This is now a permanent regression guard
(`test_does_not_start_without_a_configured_database`).

## 5. Reconstruction

This file, referenced from the commit body, captures: the full local
gate run (§1), the `/v1`-inviolate + route-unreachability confirmation
(§2), the neuter/restore transcripts for all four spec-named
non-vacuity guards (§3), and a live-dry-run finding that shaped the
final implementation (§4) — sufficient for a third party (the
independent reviewer, or a future engineer) to reconstruct why this
code is shaped the way it is without re-deriving it from scratch.
