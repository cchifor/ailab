-- Gitea credential inventory — the aggregate views the CNPG exporter reads. OPERATOR-RUN ONE-SHOT, in the
-- gitea-db-bootstrap.yaml pattern: not a Kubernetes resource, never listed in kustomization.yaml, applied
-- by hand, safe to re-run (it redefines everything in one transaction).
--
-- WHY IT EXISTS: CNPG's metrics exporter runs custom queries as the predefined role pg_monitor
-- (`SET ROLE pg_monitor` inside the connection it opens as postgres), and pg_monitor has no privilege on
-- the application's tables. The first version of infra-pg-gitea-credential-queries.yaml selected from
-- "user" / access_token / oauth2_grant directly and failed on 2026-10-07 with
--   ERROR: permission denied for table user (SQLSTATE 42501)
-- (reproduced with `SET ROLE pg_monitor; SELECT count(*) FROM "user"`). Granting pg_monitor SELECT on
-- access_token would hand a role every custom query runs as the token hashes. Instead these views are
-- owned by the superuser that creates them (a view reads its tables with its OWNER's rights, the Postgres
-- default; do NOT set security_invoker), return AGGREGATES ONLY - counts and ages, never a hash, name or id
-- - and pg_monitor gets SELECT on the views and nothing else.
--
-- THE ALLOWLIST lives here, in owner_credentials: the (id, name, scope) of gitea_admin's two documented
-- tokens (docs/runbooks/s2s-identity.md, "Still open"). It is compared inside the view; only the COUNT of
-- non-matching tokens leaves. ROTATING either token gives it a new id, so edit the VALUES list below and
-- re-run this file in the same PR that records the rotation, or GiteaAdminTokenNotAllowlisted fires.
--
-- RUN against the PRIMARY (`kubectl -n databases get cluster infra-pg -o jsonpath='{.status.currentPrimary}'`),
-- as the postgres superuser over the local socket (peer auth, no password), from the repo root:
--
--   kubectl --context admin@ai -n databases exec -i <primary> -c postgres -- \
--     psql -U postgres -d gitea -v ON_ERROR_STOP=1 --single-transaction -f - \
--     < kubernetes/apps/databases/gitea-credential-inventory.sql
--
-- The objects live in the DATABASE, so they replicate to the standby and are in the nightly pg_dump; a
-- failover or a restore needs no re-run.
--
-- IF A GITEA UPGRADE EVER FAILS in a migration with "cannot alter type of a column used by a view" or
-- "cannot drop table ... because other objects depend on it" naming credential_inventory.*, drop the
-- schema, let the upgrade finish, then re-run this file:
--   DROP SCHEMA credential_inventory CASCADE;
-- The views read only access_token(id, uid, name, scope, created_unix), "user"(id, name, is_admin) and
-- oauth2_grant(user_id). Until it is re-run the exporter query fails and GiteaCredentialInventoryMissing
-- fires after 30 minutes, which is the intended signal.

CREATE SCHEMA IF NOT EXISTS credential_inventory;
REVOKE ALL ON SCHEMA credential_inventory FROM PUBLIC;
GRANT USAGE ON SCHEMA credential_inventory TO pg_monitor;

-- Per owner: cchifor (the org account), chifor (the owner), gitea_admin (break-glass) and every site
-- admin. LEFT JOINs from "user", so an owner with nothing to report still yields a row of zeros (a
-- missing row means the account is gone; GiteaCredentialInventoryMissing alerts on that, never on a zero).
DROP VIEW IF EXISTS credential_inventory.owner_credentials;
CREATE VIEW credential_inventory.owner_credentials AS
WITH allowlist (id, name, scope) AS (
  VALUES (1::bigint, 'flux-ailab-read'::text, 'read:repository'::text),
         (12::bigint, 'af-ci-scaler-2941'::text, 'read:admin'::text)
)
SELECT u.name AS owner,
       count(t.id)::float8 AS tokens,
       COALESCE(extract(epoch FROM now()) - min(t.created_unix), 0)::float8 AS oldest_token_age_seconds,
       count(t.id) FILTER (WHERE a.id IS NULL)::float8 AS tokens_not_allowlisted,
       COALESCE(max(g.n), 0)::float8 AS oauth_grants
FROM "user" u
LEFT JOIN access_token t ON t.uid = u.id
LEFT JOIN allowlist a ON a.id = t.id AND a.name = t.name AND a.scope = t.scope
LEFT JOIN (SELECT user_id, count(*) AS n FROM oauth2_grant GROUP BY user_id) g ON g.user_id = u.id
WHERE u.name IN ('cchifor', 'chifor', 'gitea_admin') OR u.is_admin
GROUP BY u.id, u.name;
REVOKE ALL ON credential_inventory.owner_credentials FROM PUBLIC;
GRANT SELECT ON credential_inventory.owner_credentials TO pg_monitor;

DROP VIEW IF EXISTS credential_inventory.site_admins;
CREATE VIEW credential_inventory.site_admins AS
SELECT count(*)::float8 AS total FROM "user" WHERE is_admin;
REVOKE ALL ON credential_inventory.site_admins FROM PUBLIC;
GRANT SELECT ON credential_inventory.site_admins TO pg_monitor;
