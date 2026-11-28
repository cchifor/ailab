-- C4 readback: the hub's health row for the handed-off holder.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL search_path = pg_catalog;
SELECT json_build_object('holder_id', h.holder_id, 'journal_id', h.journal_id, 'admission_paused', h.admission_paused,
  'native_state_ready', h.native_state_ready, 'native_recovery_version', h.native_recovery_version,
  'observed_ms', floor(extract(epoch FROM h.observed_at) * 1000)::bigint)::text
FROM public.agent_holder_health h
WHERE h.tenant_id = :'tenant'::uuid AND h.holder_id = :'holder_id'::uuid;
COMMIT;
