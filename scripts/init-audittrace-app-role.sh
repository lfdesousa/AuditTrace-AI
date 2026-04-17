#!/usr/bin/env bash
# DESIGN §16 Phase 4 — create a non-superuser application role.
#
# Postgres superusers ALWAYS bypass Row-Level Security, regardless of
# FORCE ROW LEVEL SECURITY. The default ``audittrace`` role created by
# the ``postgres`` image via ``POSTGRES_USER`` is a superuser, so the
# RLS policies from migration 005 don't actually bite for the
# application's own queries. This script creates a dedicated
# ``audittrace_app`` role that:
#
#   - Is NOSUPERUSER → RLS actually applies to its queries
#   - Is NOBYPASSRLS → defensive, redundant with NOSUPERUSER
#   - Has LOGIN + password (same as ``audittrace`` in dev)
#   - Owns the three RLS-protected tables so ALTER TABLE, FORCE ROW
#     LEVEL SECURITY and CREATE POLICY work from future migrations
#     without the script having to become a superuser
#   - Has USAGE on the public schema for DDL
#
# The ``audittrace`` superuser is kept for initial bring-up, emergency
# ops, and the postgres image's own housekeeping. The app's hot path
# runs entirely as ``audittrace_app``.
#
# When it runs:
#
#   - **First-time stack bring-up:** mounted into
#     ``/docker-entrypoint-initdb.d/`` by docker-compose. The
#     postgres image runs every ``*.sh`` / ``*.sql`` file in that
#     directory alphabetically on empty-data-dir init. This script
#     comes after ``init-keycloak-db.sql`` and creates the role.
#   - **Existing stack (operator refresh):** run manually via
#     ``docker exec -e AUDITTRACE_APP_PASSWORD=<pw> audittrace-postgres
#     bash /docker-entrypoint-initdb.d/init-audittrace-app-role.sh``
#     after docker-compose is restarted (which re-mounts the file).
#     The script is idempotent so it's safe to re-run.
#
# Environment:
#
#   POSTGRES_USER          — existing superuser (defaults to audittrace)
#   POSTGRES_PASSWORD      — existing superuser password
#   POSTGRES_DB            — target database (defaults to audittrace)
#   AUDITTRACE_APP_PASSWORD — password for the new role;
#                            defaults to POSTGRES_PASSWORD so dev
#                            setups that don't rotate credentials
#                            stay simple

set -euo pipefail

POSTGRES_USER="${POSTGRES_USER:-audittrace}"
POSTGRES_DB="${POSTGRES_DB:-audittrace}"
APP_PASSWORD="${AUDITTRACE_APP_PASSWORD:-${POSTGRES_PASSWORD:-}}"

if [[ -z "${APP_PASSWORD}" ]]; then
    echo "ERROR: Neither AUDITTRACE_APP_PASSWORD nor POSTGRES_PASSWORD is set" >&2
    exit 1
fi

echo "[init-audittrace-app-role] Creating non-superuser app role..."

# We pass the password to psql as a ``--set`` variable, then promote
# it into a session GUC (``audittrace.app_password``) with
# ``set_config`` so the PL/pgSQL DO blocks can read it safely via
# ``current_setting``. This avoids string-interpolation into the
# literal SQL body, which is both ugly and SQL-injection-prone.
psql \
    --username="${POSTGRES_USER}" \
    --dbname="${POSTGRES_DB}" \
    --set ON_ERROR_STOP=on \
    --set "app_password=${APP_PASSWORD}" \
    <<'EOSQL'
-- Hoist the psql variable into a session GUC the DO block can read.
SELECT set_config('audittrace.app_password', :'app_password', false);

-- ────────────────────── Create or rotate role ───────────────────────
DO $$
DECLARE
    pw text := current_setting('audittrace.app_password', true);
BEGIN
    IF pw IS NULL OR pw = '' THEN
        RAISE EXCEPTION 'audittrace.app_password GUC is empty';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'audittrace_app') THEN
        EXECUTE format(
            'CREATE ROLE audittrace_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L',
            pw
        );
        RAISE NOTICE 'Created role audittrace_app (NOSUPERUSER, NOBYPASSRLS, LOGIN)';
    ELSE
        EXECUTE format('ALTER ROLE audittrace_app WITH PASSWORD %L', pw);
        RAISE NOTICE 'Role audittrace_app already existed — password rotated';
    END IF;
END
$$;

-- ───────────────────────────── Grants ─────────────────────────────
GRANT USAGE, CREATE ON SCHEMA public TO audittrace_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO audittrace_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO audittrace_app;

-- Default privileges: every NEW table/sequence created by audittrace
-- (the migration user) automatically grants access to audittrace_app
-- so future migrations don't need per-table GRANT statements.
ALTER DEFAULT PRIVILEGES FOR ROLE audittrace IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO audittrace_app;
ALTER DEFAULT PRIVILEGES FOR ROLE audittrace IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO audittrace_app;

-- ────────── Ownership transfer for RLS-protected tables ─────────
-- audittrace_app MUST own these tables so ALTER TABLE + FORCE ROW
-- LEVEL SECURITY apply to its own queries (FORCE is evaluated
-- against the table owner at plan time).
DO $$
DECLARE
    t text;
BEGIN
    FOR t IN SELECT unnest(ARRAY['interactions', 'sessions', 'tool_calls'])
    LOOP
        IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = t) THEN
            EXECUTE format('ALTER TABLE public.%I OWNER TO audittrace_app', t);
            RAISE NOTICE 'Transferred ownership of public.% to audittrace_app', t;
        END IF;
    END LOOP;
END
$$;

-- Also transfer ownership of the interactions serial sequence so
-- audittrace_app can use nextval() on INSERT.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'interactions_id_seq') THEN
        ALTER SEQUENCE public.interactions_id_seq OWNER TO audittrace_app;
        RAISE NOTICE 'Transferred ownership of interactions_id_seq to audittrace_app';
    END IF;
END
$$;
EOSQL

echo "[init-audittrace-app-role] Done."
