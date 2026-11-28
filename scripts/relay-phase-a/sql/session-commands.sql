-- C9/C11: every control and agent command of the session at the generation, with its receipt status.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL search_path = pg_catalog;
SELECT json_build_object('table', 'control', 'kind', k.kind, 'state', k.state,
  'receipt_status', k.receipt->>'status', 'stop_reason', k.receipt->'result'->>'stopReason')::text
FROM public.agent_control_commands k
WHERE k.tenant_id = :'tenant'::uuid AND k.session_id = :'session_id'::uuid AND k.generation = :'generation'::integer
UNION ALL
SELECT json_build_object('table', 'agent', 'kind', a.kind, 'state', a.execution,
  'receipt_status', a.receipt->>'status', 'stop_reason', NULL)::text
FROM public.agent_commands a
WHERE a.tenant_id = :'tenant'::uuid AND a.session_id = :'session_id'::uuid AND a.generation = :'generation'::integer;
COMMIT;
