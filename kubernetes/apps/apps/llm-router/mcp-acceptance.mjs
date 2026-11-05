// One-shot GitOps acceptance. The management token stays in this pod's environment.
// No raw tool result, credential, model reply, or private conversation is logged.
import { pathToFileURL } from 'node:url';

class AcceptanceError extends Error {}
function requireThat(ok, message) { if (!ok) throw new AcceptanceError(message); }
const alias = 'claude-api';

export function checkSetup(result, before, secretRef, models) {
  const p = result.data?.proposed, proposal = result.proposal;
  requireThat(p?.alias === alias && p.provider === 'anthropic' && p.mode === 'new', 'Unexpected provider or alias in proposal');
  requireThat(/^anthropic(?:-\d+)?$/.test(p.account) && !before.accounts.some(a => a.id === p.account), 'Proposal does not create a new Anthropic account');
  requireThat(models.includes(p.model), 'Proposed model was not discovered');
  requireThat(!before.routes.some(r => r.alias === alias), 'The route already exists');
  const diff = proposal?.diff;
  requireThat(Array.isArray(diff) && diff.length === 2 && diff.every(d => d.op === '+'), 'Setup changes existing configuration');
  const account = diff.find(d => d.text.startsWith(`account ${p.account}  `));
  const route = diff.find(d => d.text.startsWith(`route ${alias}  `));
  requireThat(account?.text.startsWith(`account ${p.account}  provider=anthropic access=api key=${secretRef} concurrency=`) && !account.text.includes('baseUrl='), 'Unexpected account connection');
  requireThat(route?.text.startsWith(`route ${alias}  → ${p.account}/${p.model}  strategy=`), 'Unexpected route target');
  requireThat(typeof proposal.proposalId === 'string' && !result.truncated, 'Proposal is incomplete');
  return p;
}

export async function runAcceptance({ url, token, log = console.log, waitForEndpointMs = 0 }) {
  let id = 0, sessionId, stage = 'initialize';
  const emit = (event, fields = {}) => log(JSON.stringify({ event, ...fields }));
  const rpc = async (method, params = {}) => {
    const response = await fetch(url, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(95_000),
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json', accept: 'application/json, text/event-stream', 'mcp-protocol-version': '2025-03-26' },
      body: JSON.stringify({ jsonrpc: '2.0', id: ++id, method, params }),
    });
    requireThat(response.ok, `MCP HTTP ${response.status}`);
    let body;
    try { body = await response.json(); }
    catch { throw new AcceptanceError('MCP response was not valid JSON'); }
    requireThat(!body.error, 'MCP protocol error');
    return body.result;
  };
  const call = async (name, args = {}, scoped = true) => {
    stage = name;
    const result = await rpc('tools/call', { name, arguments: { ...(scoped ? { sessionId } : {}), ...args } });
    requireThat(!result.isError, `Tool refused: ${name}`);
    requireThat(result.structuredContent && !result.structuredContent.truncated, `Incomplete tool result: ${name}`);
    return result.structuredContent;
  };
  try {
    const deadline = Date.now() + waitForEndpointMs;
    let init;
    // The Job may start before the Recreate rollout. Retry only read-only initialization,
    // never a tool/approval whose outcome could already have committed.
    for (;;) {
      try { init = await rpc('initialize', { protocolVersion: '2025-03-26', capabilities: {}, clientInfo: { name: 'gitops-mcp-acceptance', version: '1' } }); break; }
      catch (error) {
        if (Date.now() >= deadline || !(error instanceof TypeError || (error instanceof AcceptanceError && /^MCP HTTP (404|502|503|504)$/.test(error.message)))) throw error;
        await new Promise(resolve => setTimeout(resolve, 5000));
      }
    }
    requireThat(init.serverInfo?.name === 'llm-router', 'Unexpected MCP server');
    const listed = await rpc('tools/list');
    for (const name of ['setup_provider', 'probe_provider', 'router_resolve_proposal', 'router_request', 'audit_log']) requireThat(listed.tools.some(t => t.name === name), `Missing tool: ${name}`);
    sessionId = (await call('router_open_session', { title: 'MCP release acceptance / Anthropic setup' }, false)).sessionId;
    emit('mcp.connected', { sessionId, tools: listed.tools.length });
    await call('router_status');
    await call('router_requests', { limit: 1 }, false);
    await call('audit_log', { limit: 5 });
    const before = (await call('list_routes')).data;
    let accountId, model;
    const existing = before.routes.find(r => r.alias === alias);
    if (existing) {
      requireThat(existing.deployments?.length === 1 && existing.deployments[0].enabled, 'Existing claude-api route requires investigation');
      accountId = existing.deployments[0].account; model = existing.deployments[0].model;
      const account = (await call('inspect_account', { account: accountId })).data;
      requireThat(account.provider === 'anthropic' && account.access === 'api' && !account.baseUrl, 'Existing route is not the hosted Anthropic API');
      await call('probe_provider', { provider: 'anthropic', secretRef: account.secretRef });
      emit('anthropic.existing_route_verified', { accountId, model });
    } else {
      const secrets = (await call('list_secrets')).data.secrets;
      // Use only one unambiguous, unused, explicitly labelled Anthropic/Claude key.
      // Never probe unrelated saved credentials or guess from credential values.
      const candidates = secrets.filter(s => !s.usedBy.length && /anthropic|claude/i.test(s.label ?? ''));
      emit('anthropic.saved_key_candidates', { count: candidates.length });
      requireThat(candidates.length === 1, 'Need one unambiguous unused Anthropic/Claude labelled key');
      const secretRef = candidates[0].ref;
      const connection = { provider: 'anthropic', secretRef }; // Hosted API: baseUrl is absent.
      const discovery = (await call('probe_provider', connection)).data;
      requireThat(discovery.listed && discovery.models?.length, 'Anthropic discovery returned no models');
      emit('anthropic.discovery_passed', { models: discovery.models.length });
      const planned = await call('setup_provider', { ...connection, alias, mode: 'new' });
      const proposed = checkSetup(planned, before, secretRef, discovery.models);
      // The user authorized adding this provider. Approve only this validated, additive proposal;
      // a refreshed proposal or any existing configuration change stops this one-shot job.
      emit('anthropic.additive_proposal_verified', { sessionId, proposalId: planned.proposal.proposalId, accountId: proposed.account, model: proposed.model });
      const applied = await call('router_resolve_proposal', { proposalId: planned.proposal.proposalId, decision: 'approve' });
      requireThat(applied.outcome === 'approved', 'Approval requires another review; inspect the durable session');
      requireThat(applied.result?.data?.alias === alias && applied.result.data.account === proposed.account, 'Unexpected setup result');
      accountId = proposed.account; model = proposed.model;
      const routes = (await call('list_routes')).data.routes;
      requireThat(routes.some(r => r.alias === alias && r.deployments?.length === 1 && r.deployments[0].account === accountId && r.deployments[0].model === model), 'Created route was not readable');
      emit('anthropic.setup_applied', { accountId, model, revision: applied.result.data.revision });
    }
    const test = await call('test_route', { alias, prompt: 'Reply with exactly: router MCP verified' });
    requireThat(test.proposal?.action === 'spend' && test.proposal.proposalId, 'Missing spend proposal');
    const tested = await call('router_resolve_proposal', { proposalId: test.proposal.proposalId, decision: 'approve' });
    requireThat(tested.outcome === 'approved' && tested.result?.data?.ok === true, 'Real Anthropic inference failed; inspect the durable session');
    const trajectoryId = tested.result.data.trajectoryId;
    const journal = (await call('router_request', { id: trajectoryId }, false)).request;
    requireThat(journal.status === 'complete' && journal.accountId === accountId && !('captured' in journal), 'Unexpected request journal');
    emit('anthropic.inference_passed', { accountId, model, trajectoryId, latencyMs: tested.result.data.latencyMs });
    await call('router_inspect_session', { limit: 5 });
    await call('router_close_session');
    emit('mcp.acceptance_passed', { accountId, model, trajectoryId });
  } catch (error) {
    // Only explicitly tagged local messages can be logged. Parser/transport/library errors
    // may quote upstream payloads, credentials or URLs in their message.
    emit('mcp.acceptance_failed', { stage, sessionId, reason: error instanceof AcceptanceError ? error.message : 'Transport or response failure' });
    throw new Error('MCP acceptance failed; see the safe stage summary and durable session');
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const token = process.env.ROUTER_MCP_ACCEPTANCE_TOKEN;
  requireThat(token && token.length >= 24, 'Missing management credential');
  await runAcceptance({ url: 'http://llm-router.llm-router.svc.cluster.local/admin/mcp', token, waitForEndpointMs: 240_000 });
}
