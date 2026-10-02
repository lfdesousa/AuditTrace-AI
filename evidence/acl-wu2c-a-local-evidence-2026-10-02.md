# ACL WU-2c-A — local verification evidence (2026-10-02)

Scope of this note: **Verification and local Reconstruction only.** The Rule-2
live end-to-end (through the front door, scoped JWTs, a real LLM call, against
the deployed image) has NOT been run — the change is not deployed. It runs
after merge, release and deploy, from a written operator runbook. Nothing
below is a claim about the live system.

## What was exercised

The real FastAPI app (`create_app()` + `TestClient`) bound to a throwaway
real-Postgres schema (`postgres:16`, migrations 002-017 and 031-033 applied by
running the real migration files), connected as a `NOSUPERUSER NOBYPASSRLS`
role, with `install_rls_listener()` and identities through the real
`require_user` cold path (only the JWT decode is patched). File:
`tests/test_console_acl_routes_rls_postgres.py`.

## Captured through the real route on real Postgres

| artefact | value |
|---|---|
| W1 grant by the resource owner | HTTP 201 |
| response `trace_id` == `console_acl_entries.trace_id` == `interactions.trace_id` | `c31a222f65732e90cdadd44afafe8a77` (three-way equality, `status=success`) |
| W1 grant by a non-owner on the owner's resource | HTTP 403, `failure_class=acl_denied_policy`, `db_error_class=42501`, no row in `console_acl_entries` |
| the denial's trace id (response body == `interactions.trace_id`) | `fd95ba15a5632f95a42666b261a8952e` |
| denial row visible to its subject only | the non-owner sees 1 `failed` row; the owner's `GET /interactions?event_class=acl_authz` shows only the owner's own rows |

The last row is a disclosed LIMITATION, not a control: denial rows are
RLS-scoped to the subject that caused them (no cross-subject auditor read).

## Gates (local)

See the PR body for the final `make test` / lint / typecheck / helm-lint /
CI-shaped results. The neuter table (107 guards, each reddened individually by
a targeted test run and restored byte-for-byte) is recorded in the build record.

## Not established here

The fork's ACL shim and the BFF ACL proxy (WU-4); the O-7 cascade (2c-B); the
live cluster (PostgreSQL 18.3 vs this harness's `postgres:16`); Keycloak's
behaviour for the dedicated client (proven only by the provisioner's read-back
after deploy).
