-- The hub's authoritative session state: generation, rebinding, control epoch and holder.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL search_path = pg_catalog;
SELECT json_build_object('id', s.id, 'state', s.state, 'generation', s.generation, 'rebinding', s.rebinding,
  'control_epoch', s.control_epoch, 'holder_id', s.holder_id, 'native_session_id', s.native_session_id)::text
FROM public.agent_sessions s
WHERE s.tenant_id = :'tenant'::uuid AND s.id = :'session_id'::uuid;
COMMIT;
