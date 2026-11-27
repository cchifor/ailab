-- C6: this launch's install/render receipt for the renewed credential, with its active consumer lease.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL search_path = pg_catalog;
SELECT json_build_object(
  'holder_id', i.holder_id, 'generation', i.generation, 'install_state', i.state,
  'receipt_status', i.receipt->>'status', 'rendered', i.rendered_at IS NOT NULL, 'render_failure', i.render_failure,
  'consumer_state', c.state, 'consumer_lease_ok', c.lease_expires_at > now())::text
FROM public.holder_router_installations i
LEFT JOIN public.router_credential_consumers c ON c.tenant_id = i.tenant_id AND c.binding_id = i.binding_id
  AND c.credential_id = i.credential_id AND c.generation = i.generation AND c.state = 'active'
WHERE i.tenant_id = :'tenant'::uuid AND i.launch_id = :'launch_id'::uuid AND i.session_id = :'session_id'::uuid
  AND i.binding_id = :'binding_id'::uuid AND i.credential_id = :'credential_id'::uuid;
COMMIT;
