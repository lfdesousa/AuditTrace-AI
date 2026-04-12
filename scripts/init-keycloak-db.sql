-- Create keycloak database in the same PostgreSQL instance.
-- This script runs only on first initialization (docker-entrypoint-initdb.d).
CREATE DATABASE keycloak;
