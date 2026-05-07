---
title: "tests/test_rls_isolation.py: legacy fallback URL fails on SET ROLE — needs superuser URL or fixture-side GRANT"
labels: ["tests", "dev-env", "tech-debt", "rls"]
priority: P3
---

## Context

`tests/test_rls_isolation.py` has two ways to find a Postgres for its
five RLS integration tests:

1. `AUDITTRACE_TEST_POSTGRES_URL` env var — the documented happy path.
   Docstring example: `postgresql+psycopg2://postgres:<pw>@localhost:15432/audittrace`
   (note: `postgres` superuser).
2. Legacy fallback — if `localhost:15432` is reachable and
   `secrets/postgres_password.txt` exists, it constructs
   `postgresql+psycopg2://audittrace:<pw>@localhost:15432/audittrace`
   (note: `audittrace` non-superuser).

The legacy fallback path is the only one that works without operator
intervention because the postgres-superuser password is not in the
repo (correctly). But the `audittrace` role has `CREATEROLE` and **not**
`ADMIN OPTION` on `rls_test_role`, so:

- `_ensure_test_role` succeeds in CREATE-ing the role on first run.
- `SET LOCAL ROLE rls_test_role` later fails with `permission denied
  to set role` — the connecting role is not a member of the role it
  created. Postgres 16 changed CREATEROLE semantics so the creator no
  longer auto-gets membership.
- A naive fix (`GRANT rls_test_role TO CURRENT_USER` after CREATE)
  fails with `permission denied to grant role: Only roles with the
  ADMIN option may grant this role` — the role is from a previous
  test run, owned by a different connection.

### Observed incident

**2026-05-07 17:00.** While preparing the `fix/vault-auto-unseal-dependency`
PR, `make test` output 5 skipped tests (no Postgres on localhost:15432).
After port-forwarding the cluster Postgres to localhost:15432, the
zero-skip gate flipped to **4 failed / 1 passed** out of the 5 RLS
tests. The 4 failures are all `permission denied to set role`. The one
that passes (`test_rls_is_enabled_on_interactions`) does not need
SET ROLE.

The auto-unseal PR was committed with the 4 failures noted in the PR
body as a known pre-existing dev-env gap. This backlog item exists to
close that gap.

## Fix sketch

Three viable options, in order of preference:

### Primary — fixture creates and grants atomically

In `_ensure_test_role`, do CREATE + GRANT in one transaction so the
connecting role only ever uses a `rls_test_role` it just created
(and therefore has implicit ADMIN OPTION on, per PG-16):

```python
# In a fresh transaction:
DROP ROLE IF EXISTS rls_test_role;       -- requires the audittrace user own it
CREATE ROLE rls_test_role NOLOGIN NOSUPERUSER NOBYPASSRLS;
GRANT rls_test_role TO CURRENT_USER WITH ADMIN OPTION;
```

Caveat: DROP ROLE fails if the role owns objects, so this needs a
REASSIGN OWNED + DROP OWNED first. Tests already drop their throwaway
schemas, but if any GRANT to the test role survives the prior schema's
DROP CASCADE (via dependencies) the DROP ROLE will fail. Worth
prototyping.

### Secondary — provision a `rls_admin` setup role in the cluster

Add a one-time bootstrap step (Helm hook job or `setup-rls-admin.sh`
script) that runs as the postgres superuser at deploy time:

```sql
CREATE ROLE rls_test_role NOLOGIN NOSUPERUSER NOBYPASSRLS;
GRANT rls_test_role TO audittrace WITH ADMIN OPTION;
```

The audittrace user can then SET ROLE freely. Pure infra change,
no test code touched. Best fit for the chart-hardening half-day
(backlog of recurring deploy friction).

### Tertiary — document the env var path as the only supported one

Drop the legacy fallback. Update the test file's pytestmark.skipif
to require `AUDITTRACE_TEST_POSTGRES_URL` (no implicit fallback).
Add a `make test-rls` target that calls `kubectl get secret
audittrace-postgres -o jsonpath` to fetch the postgres password and
export the env var inline. Friction for the operator but
deterministic.

## Acceptance

- `make test` returns 0 with no skipped or failed RLS tests in a
  fresh dev clone where the operator has run the documented setup
  (port-forward + whatever provisioning step the chosen fix mandates).
- The 5 RLS tests run against the same Postgres the production stack
  uses — no separate "test postgres" container.
- Document the chosen path in `AGENTS.md` under "RLS test setup".

## Cross-references

- `feedback_never_skip_tests.md` — zero-skip enforcement is what
  surfaced this gap when the auto-unseal fix triggered a full
  `make test`.
- `feedback_unit_tests_miss_rls.md` — ADR-046 origin story; why this
  test file exists at all.
- `~/work/audittrace-evidence/2026-05-07-vault-autounseal-fix/` — the
  PR that flagged this as a follow-up rather than blocking on it.
- ADR-046 — multi-pod RLS validation; the integration tests this file
  contains are the unit-of-truth.
