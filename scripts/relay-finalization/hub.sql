-- Schema93. Metadata only; complete counts precede bounded arrays. No secrets,
-- permission text, command payloads or credential material are selected.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL search_path = pg_catalog;
SET LOCAL relay.tenant_id = '00000000-0000-4000-8000-000000000001';
WITH profiles(id,binding) AS (VALUES
 ('1d9a7d6e-4b26-49fd-9053-7b0a22e1baae'::uuid,'a0f57684-747f-4c3e-8d90-e684b7262590'::uuid),
 ('f25c187b-1e50-4c7c-860e-52947bfdf39e'::uuid,'d2871eec-8f5a-4afe-a917-014f0ae8e14d'::uuid),
 ('fdb56ca5-908b-40ee-94b3-aac735dd8622'::uuid,'a8f6211c-d7e2-4ffd-824d-b158e349a8d4'::uuid)
), launches AS (
 SELECT l.id,l.session_id,l.profile_id,l.profile_revision,l.state,l.created_at,p.connector_id,
   p.definition->'holder'->'router'->>'credentialFile' AS credential_file
 FROM public.agent_session_launches l JOIN public.agent_launch_profiles p
 ON p.tenant_id=l.tenant_id AND p.id=l.profile_id AND p.revision=l.profile_revision
 WHERE l.tenant_id='00000000-0000-4000-8000-000000000001'::uuid
 AND (l.profile_id IN (SELECT id FROM profiles) OR EXISTS
   (SELECT 1 FROM public.holder_router_installations i WHERE i.tenant_id=l.tenant_id
    AND i.launch_id=l.id AND i.binding_id IN (SELECT binding FROM profiles)))
), sessions AS (
 SELECT s.id,s.state,s.connector_id,s.profile->>'id' AS profile_id,s.generation,s.rebinding,
 (SELECT count(*) FROM public.agent_control_commands c WHERE c.tenant_id=s.tenant_id AND c.session_id=s.id
   AND (c.state IS NULL OR c.state NOT IN ('completed','rejected','withdrawn'))) +
 (SELECT count(*) FROM public.agent_commands c WHERE c.tenant_id=s.tenant_id AND c.session_id=s.id
   AND (c.delivery IS NULL OR c.delivery NOT IN ('accepted','rejected','withdrawn'))) AS pending_commands,
 (SELECT count(*) FROM public.permission_requests p WHERE p.tenant_id=s.tenant_id AND p.session_id=s.id
   AND (p.state IS NULL OR p.state NOT IN ('resolved','cancelled','expired','interrupted'))) AS pending_permissions,
 (SELECT count(*) FROM public.agent_control_leases c WHERE c.tenant_id=s.tenant_id AND c.session_id=s.id
   AND (c.expires_at IS NULL OR c.expires_at>clock_timestamp())) AS control_leases
 FROM public.agent_sessions s WHERE s.tenant_id='00000000-0000-4000-8000-000000000001'::uuid
 AND (s.profile->>'id' IN (SELECT id::text FROM profiles) OR s.id IN (SELECT session_id FROM launches))
), bindings AS (
 SELECT b.id,b.connection_id,b.host_id,b.execution_identity,b.generation,
 (SELECT count(*) FROM public.router_credential_consumers x WHERE x.tenant_id=b.tenant_id
 AND x.binding_id=b.id AND x.state IS DISTINCT FROM 'released') AS active_consumers
 FROM public.router_credential_bindings b WHERE b.tenant_id='00000000-0000-4000-8000-000000000001'::uuid
 AND b.id IN (SELECT binding FROM profiles)
), credentials AS (
 SELECT c.id,c.binding_id,c.generation,c.router_key_id,c.namespace,c.state,
 c.created_at,c.cleanup_pending,c.revoke_requested
 FROM public.router_credentials c WHERE c.tenant_id='00000000-0000-4000-8000-000000000001'::uuid
 AND c.binding_id IN (SELECT binding FROM profiles)
), installations AS (
 SELECT i.launch_id,i.session_id,i.binding_id,i.credential_id,i.generation,i.command_id,
 i.local_reference,i.cleanup_receipt,i.cleaned_at
 FROM public.holder_router_installations i WHERE i.tenant_id='00000000-0000-4000-8000-000000000001'::uuid
 AND (i.launch_id IN (SELECT id FROM launches) OR i.binding_id IN (SELECT binding FROM profiles))
)
SELECT json_build_object(
 'version',1,'databaseIdentity',current_database(),
 'schemaVersion',(SELECT max(version) FROM public.schema_migrations),
 'observedMs',floor(extract(epoch FROM clock_timestamp())*1000)::bigint,
 'counts',json_build_object('launches',(SELECT count(*) FROM launches),'sessions',(SELECT count(*) FROM sessions),
 'bindings',(SELECT count(*) FROM bindings),'credentials',(SELECT count(*) FROM credentials),
 'installations',(SELECT count(*) FROM installations)),
 'launches',coalesce((SELECT json_agg(x ORDER BY x.id) FROM (SELECT * FROM launches ORDER BY id LIMIT 1001)x),'[]'::json),
 'sessions',coalesce((SELECT json_agg(x ORDER BY x.id) FROM (SELECT * FROM sessions ORDER BY id LIMIT 1001)x),'[]'::json),
 'bindings',coalesce((SELECT json_agg(x ORDER BY x.id) FROM (SELECT * FROM bindings ORDER BY id LIMIT 4)x),'[]'::json),
 'credentials',coalesce((SELECT json_agg(x ORDER BY x.binding_id,x.generation) FROM
   (SELECT * FROM credentials ORDER BY binding_id,generation LIMIT 385)x),'[]'::json),
 'installations',coalesce((SELECT json_agg(x ORDER BY x.launch_id) FROM
   (SELECT * FROM installations ORDER BY launch_id LIMIT 1001)x),'[]'::json),
 'unresolvedOperations',(SELECT count(*) FROM public.router_credential_operations o
   WHERE o.tenant_id='00000000-0000-4000-8000-000000000001'::uuid
   AND o.credential_id IN (SELECT id FROM credentials) AND (o.state IS NULL OR o.state NOT IN ('ready','revoked','failed')))
)::text;
COMMIT;
