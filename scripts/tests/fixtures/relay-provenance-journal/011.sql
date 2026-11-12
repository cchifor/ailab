-- Add a bounded live-phase read without changing the installed 005-010 APIs.
-- Requires SQL007; inactive ledger callers refuse if this function is absent.
BEGIN;
SET LOCAL ROLE relay_provenance_owner;

CREATE FUNCTION worker_provenance.read_runtime_presence_current_bound(aid uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,worker_provenance,pg_temp AS $$
DECLARE original_record jsonb;
        eligible_from bigint;
        eligible_until bigint;
        checked_at bigint;
BEGIN
 IF current_setting('transaction_isolation') <> 'read committed' THEN
   RAISE EXCEPTION 'Runtime presence readback requires READ COMMITTED';
 END IF;
 -- Keep the established policy-before-admission order. A caller owns the
 -- transaction and retains these locks through its external witness reads.
 PERFORM worker_provenance.lock_runtime_presence_admission(aid);
 original_record := worker_provenance.read_runtime_presence(aid);
 IF original_record IS NULL THEN
   RAISE EXCEPTION 'Runtime presence current unavailable';
 END IF;
 SELECT (p.document->>'notBefore')::bigint,
        least((p.document->>'expiresAt')::bigint,
              (p.document->>'verificationUntil')::bigint,
              (a.proposal#>>'{original,admittedAbsoluteDeadline}')::bigint)
 INTO STRICT eligible_from,eligible_until
 FROM worker_provenance.admissions a
 JOIN worker_provenance.policies p ON p.digest=a.policy_digest
 WHERE a.id=aid;
 checked_at := floor(extract(epoch FROM clock_timestamp())*1000)::bigint;
 IF eligible_from IS NULL OR eligible_until IS NULL
    OR checked_at < eligible_from OR checked_at >= eligible_until THEN
   RAISE EXCEPTION 'Runtime presence current expired';
 END IF;
 RETURN jsonb_build_object('record',original_record,'notBefore',eligible_from,
                          'notAfter',eligible_until,'checkedAt',checked_at);
END $$;
REVOKE ALL ON FUNCTION worker_provenance.read_runtime_presence_current_bound(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION worker_provenance.read_runtime_presence_current_bound(uuid)
 TO relay_provenance_ledger;
COMMIT;
