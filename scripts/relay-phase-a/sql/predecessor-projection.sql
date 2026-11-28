-- Hub5f90/schema93. Exactly three independently retained operations, metadata only.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL search_path = pg_catalog;
SELECT json_build_object(
  'schemaVersions', (SELECT json_agg(version ORDER BY version) FROM public.schema_migrations),
  'databaseName', current_database(),
  'observedMs', floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint,
  'commands', COALESCE((SELECT json_agg(json_build_object(
    'id', c.id, 'operation_id', c.operation_id, 'session_id', c.session_id,
    'generation', c.generation, 'kind', c.kind, 'state', c.state,
    'receipt', json_build_object('status', c.receipt->>'status', 'result',
      CASE WHEN c.kind='prompt' THEN json_build_object('stopReason', c.receipt->'result'->>'stopReason')
           ELSE json_build_object('cancelSent', c.receipt->'result'->'cancelSent') END))
    ORDER BY CASE c.operation_id WHEN :'prompt_operation'::uuid THEN 1
                                WHEN :'cancel_operation'::uuid THEN 2 ELSE 3 END)
    FROM public.agent_control_commands c
    WHERE c.tenant_id=:'tenant'::uuid AND c.session_id=:'session_id'::uuid
      AND c.generation=:'generation'::integer
      AND c.operation_id IN (:'prompt_operation'::uuid, :'cancel_operation'::uuid, :'close_operation'::uuid)), '[]'::json)
)::text;
COMMIT;
