-- Operator-run bootstrap for llm-router browser auth, NOT a Flux resource.
-- Run with psql -U postgres -v ON_ERROR_STOP=1 on the CURRENT infra-pg primary,
-- as documented in docs/runbooks/llm-router-keycloak.md. No passwords in this file.
-- CNPG managed.roles reconciles the password from infra-pg-llm-router-auth;
-- on an existing cluster it does NOT create the database. No Database CRD (CNPG 1.24).
SELECT 'CREATE ROLE llm_router_auth LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS'
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'llm_router_auth')\gexec
SELECT 'CREATE DATABASE llm_router_auth OWNER llm_router_auth'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'llm_router_auth')\gexec
