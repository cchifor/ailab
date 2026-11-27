-- C8: the pending permission of this session (its context holds title, description, options, toolCallId).
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL search_path = pg_catalog;
SELECT json_build_object('id', p.id, 'session_id', p.session_id, 'state', p.state,
  'unexpired', p.expires_at > now(), 'context', p.context)::text
FROM public.permission_requests p
WHERE p.tenant_id = :'tenant'::uuid AND p.id = :'permission_id'::uuid AND p.session_id = :'session_id'::uuid;
COMMIT;
