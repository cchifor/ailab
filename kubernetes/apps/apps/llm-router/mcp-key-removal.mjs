// User-requested removal of two identified saved credentials. Uses only the
// router's normal proposal/approval flow; no raw credential access or inference.
import { realpathSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const TARGETS = [
  { ref: 'store:sec_ebc130865c32ef3c', label: 'claude subscription token' },
  { ref: 'store:sec_18cd60c0c91260f3', label: 'anthropic' },
];
class RemovalError extends Error {
  constructor(reason, httpStatus, rpcCode) { super(reason); this.httpStatus = httpStatus; this.rpcCode = rpcCode; }
}
function requireThat(ok, reason) { if (!ok) throw new RemovalError(reason); }

export function checkRemoval(proposed, target) {
  const proposal = proposed.proposal, diff = proposal?.diff;
  requireThat(!proposed.truncated && proposal?.kind === 'approval' && typeof proposal.proposalId === 'string', 'Incomplete removal proposal');
  requireThat(Array.isArray(diff) && diff.length === 1 && diff[0].op === '-' && diff[0].text === `secret ${target.ref} (${target.label})`, 'Unexpected removal diff');
  return proposal.proposalId;
}

export async function runRemoval({ url, token, targets = TARGETS, log = console.log }) {
  let id = 0, sessionId, stage = 'initialize', targetRef;
  const emit = (event, fields = {}) => log(JSON.stringify({ event, sessionId, ...fields }));
  const rpc = async (method, params) => {
    const response = await fetch(url, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(60_000),
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json', accept: 'application/json, text/event-stream', 'mcp-protocol-version': '2025-03-26' },
      body: JSON.stringify({ jsonrpc: '2.0', id: ++id, method, params }),
    });
    if (!response.ok) throw new RemovalError('HTTP failure', response.status);
    requireThat(response.headers.get('content-type')?.includes('application/json'), 'Expected JSON response');
    let body;
    try { body = await response.json(); } catch { throw new RemovalError('Invalid JSON response'); }
    if (body.error) throw new RemovalError('MCP protocol failure', undefined, Number.isSafeInteger(body.error.code) ? body.error.code : undefined);
    return body.result;
  };
  const call = async (name, args = {}, scoped = true) => {
    stage = name;
    const result = await rpc('tools/call', { name, arguments: { ...(scoped ? { sessionId } : {}), ...args } });
    requireThat(result && !result.isError, `Tool refused: ${name}`);
    requireThat(result.structuredContent && !result.structuredContent.truncated, `Incomplete result: ${name}`);
    return result.structuredContent;
  };
  try {
    requireThat(token && token.length >= 24, 'Missing management credential');
    requireThat(targets.length === 2 && new Set(targets.map(t => t.ref)).size === 2 && targets.every(t => /^store:sec_[a-z0-9]+$/.test(t.ref)), 'Invalid removal targets');
    const init = await rpc('initialize', { protocolVersion: '2025-03-26', capabilities: {}, clientInfo: { name: 'gitops-mcp-key-removal', version: '1' } });
    requireThat(init?.serverInfo?.name === 'llm-router', 'Unexpected MCP server');
    sessionId = (await call('router_open_session', { title: 'User-requested removal of two saved Anthropic/Claude keys' }, false)).sessionId;
    requireThat(typeof sessionId === 'string' && sessionId.length > 0, 'Missing removal session');
    const before = (await call('list_secrets')).data?.secrets;
    requireThat(Array.isArray(before), 'Incomplete saved-key metadata');
    // Validate both before proposing either. New or unrelated saved keys are untouched.
    for (const target of targets) {
      const matches = before.filter(s => s.ref === target.ref);
      requireThat(matches.length === 1, 'Expected saved key is missing or duplicated');
      requireThat(matches[0].label === target.label, 'Expected saved-key label changed');
      requireThat(Array.isArray(matches[0].usedBy) && matches[0].usedBy.length === 0, 'Saved key is in use or usage is unknown');
    }
    for (const target of targets) {
      targetRef = target.ref;
      const secretId = target.ref.slice('store:'.length);
      const proposed = await call('remove_secret', { id: secretId });
      const proposalId = checkRemoval(proposed, target);
      emit('mcp.key_removal_proposal_verified', { ref: target.ref, proposalId });
      const applied = await call('router_resolve_proposal', { proposalId, decision: 'approve' });
      requireThat(applied.outcome === 'approved', 'Removal requires another review; inspect the durable session');
      requireThat(applied.result?.data?.id === secretId, 'Unexpected removal result');
      emit('mcp.key_removed', { ref: target.ref });
    }
    targetRef = undefined;
    const after = (await call('list_secrets')).data?.secrets;
    requireThat(Array.isArray(after) && targets.every(t => !after.some(s => s.ref === t.ref)), 'Removed keys still appear in saved metadata');
    emit('mcp.key_removal_passed', { refs: targets.map(t => t.ref) });
    // Preserve the successful session and normal secret.deleted audit for investigation.
  } catch (error) {
    emit('mcp.key_removal_failed', { stage, targetRef, reason: error instanceof RemovalError ? error.message : 'Transport or response failure',
      ...(error instanceof RemovalError ? { httpStatus: error.httpStatus, rpcCode: error.rpcCode } : {}) });
    throw new Error('MCP key removal failed; inspect the safe summary and durable session before retrying');
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  await runRemoval({ url: 'http://llm-router.llm-router.svc.cluster.local/admin/mcp', token: process.env.ROUTER_MCP_ACCEPTANCE_TOKEN });
}
