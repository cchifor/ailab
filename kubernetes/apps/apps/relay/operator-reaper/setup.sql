-- The relay-operator-reaper's database objects (idempotent). Run as the postgres superuser by the Job
-- relay-operator-reaper-setup. The reaper role's password comes from the environment (REAPER_PASSWORD, its SOPS
-- Secret) through \getenv, so it never appears on psql's argv.
--
-- ops.relay_operator_reap(fence) deletes ONLY the JIT automation identity's Relay sessions and memberships (the subject is
-- a literal here, not a parameter), runs as relay_owner (SECURITY DEFINER, fixed search_path, bounded locks), and
-- returns the counts. The login role relay_operator_reaper may execute that one function and nothing else.
\set ON_ERROR_STOP on
\getenv pw REAPER_PASSWORD
BEGIN;
CREATE SCHEMA IF NOT EXISTS ops AUTHORIZATION relay_owner;
REVOKE ALL ON SCHEMA ops FROM PUBLIC;

-- The unfenced first version is replaced (a fenced overload must not leave the old one callable).
DROP FUNCTION IF EXISTS ops.relay_operator_reap();
-- `fence` is the reaper's own Lease expires-at (epoch seconds, as the API server holds it). Nothing is deleted
-- unless at least 30 s of that hold remain on THIS clock: a reaper that stalled past its hold (a successor may
-- already hold the Lease and have granted admin) fails closed instead of revoking the successor's grant.
CREATE OR REPLACE FUNCTION ops.relay_operator_reap(fence bigint)
  RETURNS TABLE (sessions_deleted bigint, memberships_deleted bigint)
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
  SET lock_timeout = '5s'
  SET statement_timeout = '15s'
AS $$
DECLARE
  s bigint;
  m bigint;
BEGIN
  IF fence IS NULL OR extract(epoch FROM clock_timestamp()) > fence - 30 THEN
    RAISE EXCEPTION 'relay_operator_reap: the reaper Lease fence has passed or is too close; nothing deleted';
  END IF;
  DELETE FROM public.sessions
   WHERE user_id IN (SELECT id FROM public.users
                      WHERE subject = 'https://id.chifor.me/realms/relay|72e99c0a-fce0-42f1-8e2a-af1363b5aa4f');
  GET DIAGNOSTICS s = ROW_COUNT;
  DELETE FROM public.memberships
   WHERE user_id IN (SELECT id FROM public.users
                      WHERE subject = 'https://id.chifor.me/realms/relay|72e99c0a-fce0-42f1-8e2a-af1363b5aa4f');
  GET DIAGNOSTICS m = ROW_COUNT;
  RETURN QUERY SELECT s, m;
END
$$;
ALTER FUNCTION ops.relay_operator_reap(bigint) OWNER TO relay_owner;
REVOKE ALL ON FUNCTION ops.relay_operator_reap(bigint) FROM PUBLIC;

SELECT 'CREATE ROLE relay_operator_reaper LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'relay_operator_reaper') \gexec
ALTER ROLE relay_operator_reaper LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 2 PASSWORD :'pw';
ALTER ROLE relay_operator_reaper SET statement_timeout = '20s';
GRANT CONNECT ON DATABASE relay TO relay_operator_reaper;
GRANT USAGE ON SCHEMA ops TO relay_operator_reaper;
GRANT EXECUTE ON FUNCTION ops.relay_operator_reap(bigint) TO relay_operator_reaper;
COMMIT;
SELECT 'reaper objects ready: ' || count(*) FROM pg_proc WHERE proname = 'relay_operator_reap';
