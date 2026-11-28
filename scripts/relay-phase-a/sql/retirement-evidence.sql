-- Hub5f90/schema93. Do not invent a commandId on updates: retain exact journal order
-- and accepted/result command IDs so the validator can prove the single-flight interval.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL search_path = pg_catalog;
WITH scoped AS (
  SELECT s.id, s.generation, s.state, s.rebinding, s.control_epoch, s.holder_id, s.connector_id
  FROM public.agent_sessions s WHERE s.tenant_id=:'tenant'::uuid AND s.id=:'session_id'::uuid
), commands AS (
  SELECT c.id,c.operation_id,c.kind,c.state,c.control_epoch,c.payload,c.receipt,c.created_at
  FROM public.agent_control_commands c JOIN scoped s ON s.id=c.session_id
  WHERE c.tenant_id=:'tenant'::uuid AND c.generation=:'generation'::integer
  ORDER BY c.created_at,c.id LIMIT 201
), events AS (
  SELECT e.sequence,e.command_id,e.payload
  FROM public.agent_holder_events e JOIN scoped s ON s.id=e.session_id
  WHERE e.tenant_id=:'tenant'::uuid AND e.connector_id=s.connector_id
    AND e.incarnation=:'holder_id'::uuid AND e.journal_id=:'journal_id'::uuid
)
SELECT json_build_object(
  'schemaVersions',(SELECT json_agg(version ORDER BY version) FROM public.schema_migrations),
  'observedMs',floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint,
  'session',(SELECT row_to_json(s) FROM scoped s),
  'journalRange',(SELECT json_build_object('first',min(sequence),'last',max(sequence),'count',
    (SELECT count(*) FROM public.agent_holder_events h, scoped s WHERE h.tenant_id=:'tenant'::uuid
      AND h.connector_id=s.connector_id AND h.journal_id=:'journal_id'::uuid
      AND h.sequence BETWEEN (SELECT min(sequence) FROM events) AND (SELECT max(sequence) FROM events))) FROM events),
  'pendingPermissions',(SELECT count(*) FROM public.permission_requests p
    WHERE p.tenant_id=:'tenant'::uuid AND p.session_id=:'session_id'::uuid AND p.state='pending'),
  'commands',COALESCE((SELECT json_agg(json_build_object(
    'id',c.id,'operationId',c.operation_id,'kind',c.kind,'state',c.state,'controlEpoch',c.control_epoch,
    'configId',CASE WHEN c.kind='set-config' THEN c.payload->>'configId' END,
    'value',CASE WHEN c.kind='set-config' THEN c.payload->>'value' END,
    'settingsRevision',CASE WHEN c.kind='set-config' THEN c.payload->'settingsRevision' END,
    'probe',c.kind='prompt' AND c.payload->>'text'='After any required scoped Relay message read, reply exactly EFFORT_HIGH_PROBE. Do not use other tools, access files or modify anything.',
    'receiptStatus',c.receipt->>'status','settingsApplied',c.receipt->'result'->'settingsApplied',
    'stopReason',c.receipt->'result'->>'stopReason') ORDER BY c.created_at,c.id) FROM commands c),'[]'::json),
  'events',COALESCE((SELECT json_agg(json_build_object(
    'sequence',e.sequence,'commandId',e.command_id,'kind',e.payload->>'kind',
    'status',e.payload->>'status',
    'generation',CASE WHEN e.payload->>'kind'='session-settings' THEN e.payload->'value'->'generation' END,
    'settings',CASE WHEN e.payload->>'kind'='session-settings' THEN json_build_object(
      'revision',e.payload->'value'->'snapshot'->'revision',
      'configOptions',(SELECT json_agg(json_build_object('id',o->>'id','currentValue',o->>'currentValue'))
        FROM jsonb_array_elements(e.payload->'value'->'snapshot'->'configOptions') o WHERE o->>'id'='thought_level')) END,
    'notice',CASE WHEN e.payload->>'kind'='update' AND e.payload->'value'->>'type'='agent_message_chunk'
      THEN e.payload->'value'->>'text'='Router refused changed session configuration (SESSION_CONFIGURATION_CHANGED). Explicit reconciliation is required. No provider or model fallback was used.' ELSE false END,
    'truncated',CASE WHEN e.payload->>'kind'='update' THEN e.payload->'value'->'presentation'->'truncated' END
  ) ORDER BY e.sequence) FROM (SELECT * FROM events ORDER BY sequence LIMIT 2001) e),'[]'::json)
)::text;
COMMIT;
