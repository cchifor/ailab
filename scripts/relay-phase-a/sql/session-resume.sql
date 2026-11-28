-- Session binding for a resume: the launch row that resumed the closed session :resume_from.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL search_path = pg_catalog;
SELECT json_build_object(
  'id', l.id, 'session_id', l.session_id, 'actor_id', l.actor_id, 'profile_id', l.profile_id, 'profile_revision', l.profile_revision,
  'cwd', l.cwd, 'state', l.state, 'holder_id', l.holder_id, 'holder_journal_id', l.holder_journal_id,
  'created_ms', floor(extract(epoch FROM l.created_at) * 1000)::bigint,
  'resume_from', l.resume_from, 'resume_generation', l.resume_generation, 'resume_native_id', l.resume_native_id,
  'resume_acknowledge_unknown_tool_effects', l.resume_acknowledge_unknown_tool_effects,
  'native_id', l.receipt->'result'->>'nativeSessionId')::text
FROM public.agent_session_launches l
WHERE l.tenant_id = :'tenant'::uuid AND l.resume_from = :'resume_from'::uuid;
COMMIT;
