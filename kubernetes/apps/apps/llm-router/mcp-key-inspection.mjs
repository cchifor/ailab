// Read-only investigation of the failed acceptance session. Only saved metadata
// is returned; no credential values, discovery, proposals or inference calls.
const sessionId = '7a57f124-a8f5-49c3-84df-0f1ddc30b25f';
const token = process.env.ROUTER_MCP_ACCEPTANCE_TOKEN;
let id = 0;
class InspectionError extends Error {
  constructor(reason, status, rpcCode, toolCode) { super(reason); this.status = status; this.rpcCode = rpcCode; this.toolCode = toolCode; }
}
const TOOL_CODES = new Set(['CONVERSATION_NOT_FOUND', 'INTERACTION_PENDING', 'FORBIDDEN', 'INVALID_REQUEST', 'HISTORY_FULL', 'CANCELLED', 'TOOL_FAILED']);
let stage = 'initialize';
async function rpc(method, params) {
  const response = await fetch('http://llm-router.llm-router.svc.cluster.local/admin/mcp', {
    method: 'POST', redirect: 'error', signal: AbortSignal.timeout(95_000),
    headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json', accept: 'application/json, text/event-stream', 'mcp-protocol-version': '2025-03-26' },
    body: JSON.stringify({ jsonrpc: '2.0', id: ++id, method, params }),
  });
  if (!response.ok) throw new InspectionError('HTTP failure', response.status);
  if (!response.headers.get('content-type')?.includes('application/json')) throw new InspectionError('Expected JSON response');
  let result;
  try { result = await response.json(); } catch { throw new InspectionError('Invalid JSON response'); }
  if (result.error) throw new InspectionError('MCP failure', undefined, Number.isSafeInteger(result.error.code) ? result.error.code : undefined);
  if (result.result?.isError) {
    const code = result.result.structuredContent?.code;
    throw new InspectionError('Tool failure', undefined, undefined, TOOL_CODES.has(code) ? code : 'unknown');
  }
  return result.result;
}
try {
  if (!token || token.length < 24) throw new InspectionError('Missing credential');
  await rpc('initialize', { protocolVersion: '2025-03-26', capabilities: {}, clientInfo: { name: 'gitops-mcp-key-inspection', version: '1' } });
  stage = 'list_secrets';
  const result = await rpc('tools/call', { name: 'list_secrets', arguments: { sessionId } });
  const data = result.structuredContent;
  if (data?.truncated || !Array.isArray(data?.data?.secrets)) throw new InspectionError('Incomplete metadata');
  const candidates = data.data.secrets.filter(s => Array.isArray(s.usedBy) && !s.usedBy.length && /anthropic|claude/i.test(s.label ?? ''));
  // Explicit metadata projection: never log the whole tool response or exception.
  console.log(JSON.stringify({ event: 'mcp.key_metadata', sessionId, total: data.data.secrets.length, matched: candidates.length,
    candidates: candidates.map(s => ({ ref: s.ref, label: s.label, createdAt: s.createdAt, lastUsedAt: s.lastUsedAt })) }));
} catch (error) {
  console.log(JSON.stringify({ event: 'mcp.key_inspection_failed', sessionId, stage, reason: error instanceof InspectionError ? error.message : 'transport',
    ...(error instanceof InspectionError ? { httpStatus: error.status, rpcCode: error.rpcCode, toolCode: error.toolCode } : {}) }));
  process.exitCode = 1;
}
