-- Correct the admission latch on journals where SQL 005 was already applied.
-- Run after 001-005 under the same trusted deployment procedure.
BEGIN;
SET LOCAL ROLE relay_provenance_owner;

CREATE OR REPLACE FUNCTION worker_provenance.latch_runtime_presence_requirement() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,worker_provenance,pg_temp AS $$
BEGIN
  -- The policy-row lock serializes activation and admission only when the
  -- policy lookup below uses a fresh statement snapshot. REPEATABLE READ and
  -- SERIALIZABLE transactions may retain a snapshot from before activation
  -- while waiting for the lock, so refuse them rather than latch stale state.
  IF current_setting('transaction_isolation') <> 'read committed' THEN
    RAISE EXCEPTION 'Runtime presence latch requires READ COMMITTED';
  END IF;
  -- Serialize with activation's FOR UPDATE lock before reading its row.
  -- A concurrent first admission cannot latch the pre-activation snapshot.
  PERFORM 1 FROM worker_provenance.policies
    WHERE digest=NEW.policy_digest FOR KEY SHARE;
  NEW.requires_runtime_presence := EXISTS (
    SELECT 1 FROM worker_provenance.runtime_presence_policies
    WHERE policy_digest=NEW.policy_digest
  );
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION worker_provenance.activate_runtime_presence_policy(d text, root_hex text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,worker_provenance,pg_temp AS $$
DECLARE p worker_provenance.policies%ROWTYPE;
BEGIN
  -- Activation's NOT EXISTS must use a fresh snapshot too. A transaction that
  -- started before a first admission could otherwise miss it after waiting
  -- for the unchanged policy row lock and activate too late.
  IF current_setting('transaction_isolation') <> 'read committed' THEN
    RAISE EXCEPTION 'Runtime presence activation requires READ COMMITTED';
  END IF;
  SELECT * INTO STRICT p FROM worker_provenance.policies WHERE digest=d FOR UPDATE;
  IF NOT COALESCE(
    root_hex ~ '^[a-f0-9]{64}$'
    AND p.document->>'version' = '2'
    AND p.revoked IS FALSE
    AND NOT EXISTS (SELECT 1 FROM worker_provenance.admissions WHERE policy_digest=d),
    false
  ) THEN RAISE EXCEPTION 'Runtime presence policy activation denied'; END IF;
  INSERT INTO worker_provenance.runtime_presence_policies(policy_digest,operator_root_hex)
    VALUES(d,root_hex);
END $$;

COMMIT;
