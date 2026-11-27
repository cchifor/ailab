-- C9: every permission request of the session.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL search_path = pg_catalog;
SELECT json_build_object('id', p.id, 'state', p.state)::text
FROM public.permission_requests p
WHERE p.tenant_id = :'tenant'::uuid AND p.session_id = :'session_id'::uuid;
COMMIT;
