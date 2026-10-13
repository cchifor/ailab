-- ailab Keycloak DB bootstrap (ADR 0038) - OPERATOR-RUN ONE-SHOT. A plain SQL file, NOT a resource and
-- NOT in kustomization.yaml: managed.roles (infra-pg.yaml) creates the `keycloak` ROLE but never a
-- DATABASE, postInitSQL runs only at first cluster bootstrap, and a `Database` CR needs CNPG >= 1.25
-- (the estate operator is 1.24.1 - see the note in kustomization.yaml).
--
-- Run ONCE as SUPERUSER via peer auth inside the CURRENT PRIMARY (check first:
-- `kubectl --context admin@ai -n databases get cluster infra-pg -o jsonpath='{.status.currentPrimary}'`):
--
--   kubectl --context admin@ai -n databases exec -i <primary> -c postgres -- \
--     psql -U postgres -v ON_ERROR_STOP=1 -f - < kubernetes/apps/databases/keycloak-db-bootstrap.sql
--
-- Do NOT use litellm-db-bootstrap.yaml's Job variant: it reads a superuser Secret that does not exist on
-- this estate and exits green having created nothing. Idempotent: CREATE ROLE/DATABASE cannot run in a
-- DO block, so psql \gexec. The role's PASSWORD is set by managed.roles from infra-pg-keycloak; the role
-- created here only has to exist. Keycloak OWNS its DB (it runs its own Liquibase migrations at start).
SELECT 'CREATE ROLE keycloak LOGIN'
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'keycloak')\gexec
SELECT 'CREATE DATABASE keycloak OWNER keycloak'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'keycloak')\gexec
