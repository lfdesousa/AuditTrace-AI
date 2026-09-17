# Evidence — Sovereign Authorization Layer, WU-1 console-ACL store, local capture (2026-09-17)

**Scope of this evidence file.** WU-1 (READ PATH ONLY) of the Sovereign
Authorization Layer EPIC, per the ratified spec
(`2026-09-17-SPEC-sovereign-authorization-layer-acl-WU0-ratified-candidate.md`)
and its operator ratification (`2026-09-18-SPEC-ADDENDUM-I-ACL-operator-
ratification-and-three-rulings.md`). This file satisfies ADR-049 Rule 1
(Verification) in full and gives a reconstructible Rule-3-shaped capture
(neuter-proof of every guard, through the real Postgres-backed service via
`InMemoryPostgresFactory`/aiosqlite AND through the real FastAPI
`create_app()` object via `TestClient`, not mocked). It does **NOT** satisfy
Rule 2 (Validation through a deployed image + public API + scoped JWT) —
that rides the later fork ACL-shim WU (WU-4), mirroring the
`wu-agents-console-agents-store`/`wu-files`/`wu-presets`/`wu-prompts`/
`wu-tool-favorites` precedents: no product route in the fork consumes this
store yet (WU-1 builds the store; WU-4 wires the chokepoint).

## 0. Task zero — the blast-radius measurement (before any schema work)

Queried the LIVE MongoDB instance backing the LibreChat console
(`audittrace` namespace, pod `audittrace-librechat-mongodb-0`, database
`LibreChat`):

```
$ kubectl -n audittrace exec audittrace-librechat-mongodb-0 -c mongodb -- \
    mongosh --quiet --eval '
      const db2 = db.getSiblingDB("LibreChat");
      print("total aclentries: " + db2.aclentries.countDocuments({}));
      printjson(db2.aclentries.aggregate([{$group:{_id:"$principalType",count:{$sum:1}}}]).toArray());
      printjson(db2.aclentries.aggregate([{$match:{principalType:"group"}},{$group:{_id:"$resourceType",count:{$sum:1}}}]).toArray());
    '
total aclentries: 0
--- by principalType ---
[]
--- group rows by resourceType ---
[]
```

**Result: ZERO group-principal ACL rows, ZERO ACL rows of any kind, ZERO
users depending on group-only access.** ADDENDUM I ruling 1 (groups
disabled) is FREE — no live access is revoked. Verified the collection's
live indexes (`getIndexes()`) match the ratified spec's field names
exactly (`principalId`, `principalType`, `resourceType`, `resourceId`,
`tenantId`, `permBits`, `inheritedFrom`, `expiredAt` with a genuine
`expireAfterSeconds: 0` TTL index) and confirmed `groups`/`promptgroups`
collections are also empty (`groups count: 0`), so no group-membership
data exists to be orphaned by the ruling either.

**Named, tested behaviour for existing Mongo group-grants on the read
path:** refused at the schema level. Since `console_acl_entries` has a DB
CHECK constraint (`ck_console_acl_entries_principal_type`) that rejects
`principal_type='group'` outright, no group-principal row can ever exist
in the sovereign store — the read path therefore never encounters one,
by construction, not by a filter that could later be forgotten. Proven in
§3 (`test_group_principal_is_refused`).

## 1. Full test-suite run (Rule 1 — Verification)

```
$ make test
...
Required test coverage of 90% reached. Total coverage: 99.02%
1 failed, 5602 passed, 2 warnings in 568.80s (0:09:28)
FAILED tests/test_release_bump_files_ssot.py::test_make_release_dirties_exactly_the_ssot_set
```

The one failure is the SAME documented transient as every prior
MongoDB-elimination WU's evidence capture (`wu-agents`/`wu-files`/
`wu-presets`/`wu-prompts`/`wu-tool-favorites`): the test spawns a
throwaway nested `git worktree` at `HEAD` but symlinks THIS worktree's
editable-installed `.venv`, so the nested worktree's `make release` run
imports the CURRENT (pre-commit) `src/audittrace` tree via the
editable-install pointer instead of its own freshly-checked-out copy,
regenerating an OpenAPI snapshot that already contains this WU's new
`/console/acl/*` paths — which `HEAD`, pre-commit, does not yet have.
Root-caused independently this session (not merely asserted): confirmed
the SAME test passes cleanly (a) on the unrelated `AuditTrace-AI` main
checkout's own pinned venv, and (b) in a brand-new throwaway worktree
built directly from `main@e77f26d` with a fresh venv — isolating the
cause to THIS worktree's editable-install symlink picking up the
uncommitted `/console/acl` routes, not a dependency-version drift (ruled
out by reinstalling this worktree's venv from the main checkout's exact
`pip freeze` and reproducing the identical failure). Regenerated the
OpenAPI snapshot via `make openapi-export` (§2) so the diff is captured
NOW, in this commit, rather than left latent — resolves per §7 once HEAD
includes this commit (same mechanism as every prior WU).

Per-file coverage gate and zero-skip policy, run directly (the Makefile
recipe aborted before reaching them because the one pytest failure above
returned non-zero):

```
$ .venv/bin/python scripts/check-per-file-coverage.py
per-file coverage gate: PASS (155 files checked, lines >= 90%, branches >= 90% on 131 file(s) with branches)

$ .venv/bin/python scripts/check-no-skipped-tests.py
[no-skip-check] No skipped tests in junit.xml. Good.
```

New-file coverage (line + branch), isolated run:

```
$ .venv/bin/pytest tests/test_console_acl_migration.py tests/test_console_acl_service.py \
    tests/test_console_acl_routes.py \
    --cov=src/audittrace/services/console_acl --cov=src/audittrace/routes/console_acl \
    --cov-report=term-missing -q --no-cov-on-fail
src/audittrace/routes/console_acl.py                           59      0      2      0   100%
src/audittrace/services/console_acl/__init__.py                26      0      0      0   100%
src/audittrace/services/console_acl/_mock.py                  226      0    124      0   100%
src/audittrace/services/console_acl/_postgres.py              125      0     26      0   100%
186 passed in 103.72s
```

100% lines AND branches on all four new files (`__init__.py`/`_mock.py`/
`_postgres.py`/`routes/console_acl.py`). `ruff check`, `ruff format
--check`, and `mypy` all green on every new/touched file, including
against the pre-commit-pinned mypy v1.8.0 hook:

```
$ .venv/bin/mypy src/audittrace/services/console_acl/ src/audittrace/routes/console_acl.py \
    src/audittrace/migrations/versions/031_create_console_acl_entries.py \
    src/audittrace/db/models.py src/audittrace/dependencies.py src/audittrace/models.py \
    src/audittrace/auth.py src/audittrace/server.py
Success: no issues found in 10 source files

$ .venv/bin/pre-commit run mypy --files <same file set>
mypy.....................................................................Passed
```

Alembic single-head check:

```
$ .venv/bin/python -m alembic heads
b3f8a1c6d9e2 (head)
```

`b3f8a1c6d9e2` chains `down_revision = "9d4e2b7f1c63"` (migration 030,
the Tool-Favorites domain's head) per the ratified spec's instruction to
chain the real current head.

## 2. OpenAPI drift gate — additive-only confirmation

```
$ make openapi-export
✅ Regenerated:
   tests/fixtures/openapi.snapshot.yaml
   docs/reference/audittrace/openapi.yaml

$ git diff docs/reference/audittrace/openapi.yaml | grep -E "^-" | grep -v "^--- "
(no output — zero removed/modified lines, pure additions)
```

The new `/console/acl/*` paths + `ConsoleAcl*` schemas + the new
`memory:acl:read-own` OAuth2 scope entry are pure insertions.
`/v1/chat/completions` is byte-for-byte unchanged.

## 3. Per-guard neuter table — every RED on a side effect or captured VALUE, `cmp`-restored

One neuter per run; each restore verified `cmp`-byte-identical against a
pre-neuter backup before moving to the next guard. Full commands/output
captured live during the build; summarised per guard below (targeted
reruns, not the full suite, per `feedback_neuter_proofs_use_targeted_
tests`).

**Guard 1 — PUBLIC-as-everyone (ranked/probed FIRST).** Primary
enforcement is the schema CHECK (`ck_console_acl_entries_public_
principal_null`) — neutering it (allowing a non-PUBLIC row with NULL
`principal_id`) makes `test_public_row_with_principal_id_is_refused` and
`test_non_public_row_missing_principal_id_is_refused` go RED
(`IntegrityError` stops being raised). **Redundancy check (a guard
declared redundant must be SHOWN redundant, naming the sibling):**
replaced the service layer's explicit `principal_type == 'public'`
literal (in `_principals_clause`/`_rls_mirror_clause`) with a
NULL-inference anti-pattern (`principal_id IS NULL`) and re-ran the
FULL ACL suite (132 tests) — all stayed GREEN, because the CHECK
constraint makes the two formulations equivalent (no row can ever have
`principal_id IS NULL` except a genuine PUBLIC row). The service-layer
literal is therefore defense-in-depth, not the load-bearing guard; the
CHECK constraint is. Restored `cmp`-identical.

**Guard 2 — bitmask containment vs equality, PER BIT.** Neutered
`_contains_bit` (Postgres, `perm_bits == bit` instead of `& bit == bit`)
and `_mock`'s equivalent inline check, one implementation per run:
`TestBitmaskContainment::test_containment_grants_when_bit_is_set_among_
others` went RED for EACH of the 4 bits independently (VIEW=1, EDIT=2,
DELETE=4, SHARE=8) on both implementations (8 RED results total across 2
runs), while the "denies when bit absent" tests stayed GREEN (equality
still denies correctly there — confirming the neuter targeted
containment specifically). Restored `cmp`-identical both times.

**Guard 3 — principal-pair binding.** First neuter attempt
(`_principals_clause`'s non-public branch dropping the `principal_id`
comparison) left the two originally-written tests GREEN — investigated
and found BOTH were shielded by the `_rls_mirror_clause` sibling (the
seeded row's owner wasn't the caller, so RLS-mirror alone already denied
visibility regardless of the neuter). Added
`test_owner_cannot_inherit_a_grant_made_to_someone_else` — the CALLER
OWNS the resource (`user_sub == caller`, so RLS-mirror's owner branch
alone would make the row visible) but the row's actual principal is a
different user. This isolates the two guards: neutering `_principals_
clause` (Postgres) and `_mock_matches_principal` (Mock) each
independently turned this ONE test RED while its two siblings stayed
GREEN (correctly shown redundant there). Restored `cmp`-identical both
times.

**Guard 4 — expired-row filtering, the two classes tested SEPARATELY
(ADDENDUM I ruling 2).** Neutered `_not_expired_clause` (Postgres, made
trivially true) and `_mock_not_expired` (Mock, `return True`), one
implementation per run: all 5 `TestExpiredRowFiltering` tests went RED
on each implementation (10 RED results total), while all 3
`TestAuditReadsIncludeExpired` tests stayed GREEN on each implementation
— confirming the audit-read/authorization-decision split is REAL, not
accidental (an expired grant stays queryable for audit while excluded
from every authorization decision, exactly ruling 2's requirement).
Restored `cmp`-identical both times.

**Guard 5 — RLS cross-user isolation through the real HTTP route.**
Neutered `_rls_mirror_clause` (Postgres, `ConsoleAclEntry.id.is_not(None)`
— trivially true) and `_mock_rls_visible` (Mock, `return True`).
`tests/test_console_acl_routes.py`'s `TestCrossUserIsolation` suite
(HTTP-route-level) stayed unexpectedly GREEN under this neuter —
investigated and found every currently-wired route (`has_permission`/
`get_effective_permissions`/`find_accessible_resources`/`get_sole_owned_
resource_ids`) ALSO applies a caller-specific principal filter
independently of RLS-mirror (since these methods answer "what can I
[the caller] do", the caller's own resolved principal set is a SEPARATE,
sufficient guard — see `_principals_clause`/explicit `principal_id ==
user_context.user_id` filters). Isolated the ACTUAL load-bearing case:
`get_owner_principal_ids` has NO separate caller-scoped filter (it
resolves an ARBITRARY resource's owner, not "my own" anything). Added
`test_owner_lookup_is_isolated_by_caller` (a resource wholly unrelated to
the caller — different owner, different principal, not PUBLIC) —
neutering `_rls_mirror_clause`/`_mock_rls_visible` turned this ONE test
RED on each implementation, confirming RLS-mirror is the SOLE guard for
that method while being provably redundant (shown, not merely asserted)
for the five caller-scoped read methods. Restored `cmp`-identical both
times; full ACL suite (134 tests) re-confirmed GREEN after each restore.

**Guard 6 — the group-principal CHECK, at the database.** Neutering
`ck_console_acl_entries_principal_type` ALONE (widening it to allow
`'group'`) left `test_group_principal_is_refused` GREEN — the sibling
`ck_console_acl_entries_principal_model_matches_type` constraint
independently rejects a `('group','Group')` pair too (it has no branch
permitting a `group` principal_type at all), so removing one CHECK alone
does not expose the bug (defense-in-depth, shown by the same-run test
staying green). Widening BOTH constraints together (the true, minimal
neuter of "group principals are refused") made the test go RED — a
group-typed `INSERT` was accepted. Restored `cmp`-identical
(`cmp` confirmed against the pre-neuter backup); re-ran the full
migration test file (26 tests) GREEN.

Final confirmation, all guards restored, full ACL suite green:

```
$ .venv/bin/pytest tests/test_console_acl_migration.py tests/test_console_acl_service.py \
    tests/test_console_acl_routes.py -q --no-cov
196 passed in 28.78s

$ cmp <pre-neuter-backup> src/audittrace/db/models.py && echo OK
OK
$ cmp <pre-neuter-backup> src/audittrace/services/console_acl/_postgres.py && echo OK
OK
$ cmp <pre-neuter-backup> src/audittrace/services/console_acl/_mock.py && echo OK
OK
```

## 4. RLS proof through the real route (Postgres-backed `TestClient`)

`tests/test_console_acl_routes.py::TestCrossUserIsolation` drives the
REAL `require_user` cold path with distinct real `sub`s (never a
`dependency_overrides` swap — see the module docstring for why that
distinction matters for the RLS ContextVar binding), against the
`PostgresConsoleAclEntriesService` wired by `create_test_container()`
(the same aiosqlite-backed factory `InMemoryPostgresFactory` every
sibling console-* domain's route tests use):

- `test_user_b_cannot_see_user_as_private_grant` — bob's
  `GET /console/acl/agent/{alice's resource}/permissions` and
  `/has-permission` both return the deny-by-default zero/false answer;
  alice's own call sees her grant.
- `test_user_b_cannot_see_user_as_owned_resources` — bob's
  `GET /console/acl/agent/sole-owned` is empty; alice's own call lists
  her resource.
- `test_user_b_cannot_see_user_as_accessible_resources` — same shape for
  `GET /console/acl/agent/accessible`.
- `test_public_row_is_visible_to_every_caller` (non-vacuity companion) —
  a genuinely PUBLIC grant IS visible to two unrelated random subs,
  proving the isolation wall does not over-block.
- `test_hostile_query_cannot_impersonate_another_principal` — a hostile
  `principal_id`/`principal_type` query-string parameter on the
  `has-permission` route (no request model declares such a field; FastAPI
  silently ignores unbound query params) has zero effect: bob still
  cannot see alice's grant.

## 5. Absolutes self-check — no absolutes without proof, across ALL FOUR surfaces

- **Source comments:** the one exception to "never compare `perm_bits`
  with `=`" (`get_owner_principal_ids`'s deliberate exact-equality) is
  named explicitly in its own docstring as "the SOLE exception in this
  package", not stated as an unqualified absolute elsewhere.
- **Prose (this file):** §3 states each redundancy finding as an
  EMPIRICAL result ("left GREEN", "confirmed RED") with the exact test
  run that produced it, never as an assumed property.
- **Tables (docstrings):** `ConsoleAclEntry`'s class docstring (db/
  models.py) states the perm_bits rule with the same named, singular
  exception, not as a blanket claim.
- **Commit message:** describes `perm_bits` containment as used
  "throughout except the one documented, deliberate exact-equality
  exception in the ownerContact.js fold" — matches the code and this
  file exactly; no unqualified "never"/"always" claim about the ACL
  store as a whole (the store IS read-only in WU-1 — stated as a scope
  fact, not a permanent absolute, since WU-2 explicitly adds writes).

## 6. What this file does NOT claim

No image was built, no `helm upgrade` ran, no pod was hit through the
public API with a scoped JWT against a deployed image — Rule 2/3 (live
E2E) is explicitly deferred to the later fork ACL-shim WU (WU-4) per the
ratified spec's own WU boundary (WU-1 builds the store; WU-4 wires the
chokepoint) — mirroring every prior MongoDB-elimination WU's evidence
capture in this repo.

## 7. Post-commit re-run (the release-bump-files transient resolves)

To be re-confirmed immediately after the commit this evidence file lands
with, exactly as every prior WU's evidence file documents: once HEAD
equals the working tree, the nested throwaway worktree the
`test_release_bump_files_ssot` test spawns checks out the SAME
(committed) source the shared editable `.venv` resolves to, and the
spurious extra OpenAPI-regen diff disappears.
