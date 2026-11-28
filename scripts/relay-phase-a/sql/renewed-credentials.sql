-- C2: both pinned bindings with the credential the driver selected (current when ready, pending when installing).
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL search_path = pg_catalog;
SELECT json_build_object(
  'binding_id', b.id, 'binding_state', b.state, 'binding_revision', b.revision,
  'current_credential_id', b.current_credential_id, 'pending_credential_id', b.pending_credential_id,
  'credential_id', c.id, 'router_key_id', c.router_key_id, 'credential_generation', c.generation,
  'namespace', c.namespace, 'connection_id', c.connection_id,
  'expires_ms', floor(extract(epoch FROM c.expires_at) * 1000)::bigint)::text
FROM public.router_credential_bindings b
LEFT JOIN public.router_credentials c ON c.tenant_id = b.tenant_id AND c.binding_id = b.id
  AND c.id = CASE WHEN b.state = 'ready' THEN b.current_credential_id ELSE b.pending_credential_id END
WHERE b.tenant_id = :'tenant'::uuid AND b.id IN (:'codex_binding'::uuid, :'qwen_binding'::uuid);
COMMIT;
