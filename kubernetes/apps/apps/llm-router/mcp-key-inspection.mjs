// Read-only investigation of the failed acceptance session. Only saved metadata
// is returned; no credential values, discovery, proposals or inference calls.
const sessionId = '7a57f124-a8f5-49c3-84df-0f1ddc30b25f';
const token = process.env.ROUTER_MCP_ACCEPTANCE_TOKEN;
let id = 0;
async function rpc(method, params) {
  const response = await fetch('http://llm-router.llm-router.svc.cluster.local/admin/mcp', {
    method: 'POST', redirect: 'error', signal: AbortSignal.timeout(95_000),
    headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json', accept: 'application/json, text/event-stream', 'mcp-protocol-version': '2025-03-26' },
    body: JSON.stringify({ jsonrpc: '2.0', id: ++id, method, params }),
  });
  if (!response.ok) throw new Error('HTTP failure');
  const result = await response.json();
  if (result.error || result.result?.isError) throw new Error('MCP failure');
  return result.result;
}
try {
  if (!token || token.length < 24) throw new Error('Missing credential');
  await rpc('initialize', { protocolVersion: '2025-03-26', capabilities: {}, clientInfo: { name: 'gitops-mcp-key-inspection', version: '1' } });
  const result = await rpc('tools/call', { name: 'list_secrets', arguments: { sessionId } });
  const data = result.structuredContent;
  if (data?.truncated || !Array.isArray(data?.data?.secrets)) throw new Error('Incomplete metadata');
  const candidates = data.data.secrets.filter(s => Array.isArray(s.usedBy) && !s.usedBy.length && /anthropic|claude/i.test(s.label ?? ''));
  // Explicit metadata projection: never log the whole tool response or exception.
  console.log(JSON.stringify({ event: 'mcp.key_metadata', sessionId, candidates: candidates.map(s => ({ ref: s.ref, label: s.label, createdAt: s.createdAt, lastUsedAt: s.lastUsedAt })) }));
} catch {
  console.log(JSON.stringify({ event: 'mcp.key_inspection_failed', sessionId }));
  process.exitCode = 1;
}
