-- llm-router browser-auth store bootstrap (llm-router#121) - OPERATOR-RUN ONE-SHOT. A plain SQL file, NOT a
-- resource and NOT in kustomization.yaml: managed.roles (infra-pg.yaml) creates the `router_auth` ROLE but never
-- a DATABASE, postInitSQL runs only at first cluster bootstrap, and a `Database` CR needs CNPG >= 1.25 (the estate
-- operator is 1.24.1 - see the note in kustomization.yaml). Same pattern as keycloak-db-bootstrap.sql.
--
-- Run ONCE as SUPERUSER via peer auth inside the CURRENT PRIMARY (check first:
-- `kubectl --context admin@ai -n databases get cluster infra-pg -o jsonpath='{.status.currentPrimary}'`):
--
--   kubectl --context admin@ai -n databases exec -i <primary> -c postgres -- \
--     psql -U postgres -v ON_ERROR_STOP=1 -f - < kubernetes/apps/databases/router-auth-db-bootstrap.sql
--
-- Idempotent: CREATE ROLE/DATABASE cannot run in a DO block, so psql \gexec. The role's PASSWORD is set by
-- managed.roles from infra-pg-router-auth; the role created here only has to exist. The router OWNS its DB: it
-- applies its own schema (llm-router packages/adapters/auth-postgres/migrations) at every start.
SELECT 'CREATE ROLE router_auth LOGIN'
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'router_auth')\gexec
SELECT 'CREATE DATABASE router_auth OWNER router_auth'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'router_auth')\gexec
-- Do NOT revoke CONNECT from PUBLIC here: the nightly infra-pg-dump connects to every database as backup_dumper.
