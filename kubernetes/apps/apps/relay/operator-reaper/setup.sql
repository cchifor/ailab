-- The relay-operator-reaper's database objects (idempotent). Run as the postgres superuser by the Job
-- relay-operator-reaper-setup, with psql variable :pw (the reaper role's password from its SOPS Secret).
--
-- ops.relay_operator_reap() deletes ONLY the JIT automation identity's Relay sessions and memberships (the subject is
-- a literal here, not a parameter), runs as relay_owner (SECURITY DEFINER, fixed search_path, bounded locks), and
-- returns the counts. The login role relay_operator_reaper may execute that one function and nothing else.
\set ON_ERROR_STOP on
\getenv pw REAPER_PASSWORD
BEGIN;
CREATE SCHEMA IF NOT EXISTS ops AUTHORIZATION relay_owner;
REVOKE ALL ON SCHEMA ops FROM PUBLIC;

-- Retire the old entry point: it has no deadline and must not remain callable.
DROP FUNCTION IF EXISTS ops.relay_operator_reap();
CREATE OR REPLACE FUNCTION ops.relay_operator_reap(lease_expires bigint)
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
  -- Check on the database clock after connection/dispatch, not at container startup.
  -- 45s permit margin covers connect10s + transaction25s with margin.
  IF lease_expires IS NULL OR lease_expires < extract(epoch FROM clock_timestamp()) + 30
     OR lease_expires > extract(epoch FROM clock_timestamp()) + 180 THEN
    RAISE EXCEPTION 'reaper Lease deadline refused';
  END IF;
  DELETE FROM public.sessions
   WHERE user_id IN (SELECT id FROM public.users
                      WHERE subject = 'https://id.chifor.me/realms/relay|72e99c0a-fce0-42f1-8e2a-af1363b5aa4f');
  GET DIAGNOSTICS s = ROW_COUNT;
  DELETE FROM public.memberships
   WHERE user_id IN (SELECT id FROM public.users
                      WHERE subject = 'https://id.chifor.me/realms/relay|72e99c0a-fce0-42f1-8e2a-af1363b5aa4f');
  GET DIAGNOSTICS m = ROW_COUNT;
  IF extract(epoch FROM clock_timestamp()) >= lease_expires THEN
    RAISE EXCEPTION 'reaper Lease expired; rolling back';
  END IF;
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
ALTER ROLE relay_operator_reaper SET transaction_timeout = '25s';
GRANT CONNECT ON DATABASE relay TO relay_operator_reaper;
GRANT USAGE ON SCHEMA ops TO relay_operator_reaper;
GRANT EXECUTE ON FUNCTION ops.relay_operator_reap(bigint) TO relay_operator_reaper;
COMMIT;
SELECT 'reaper objects ready: ' || count(*) FROM pg_proc WHERE proname = 'relay_operator_reap';
