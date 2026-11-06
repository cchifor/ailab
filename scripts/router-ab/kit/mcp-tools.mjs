// kit/mcp-tools.mjs ROOT LOG - stdio MCP server for the agentic direct arm: the shared tools (tools.mjs) under
// their real names, EXECUTED in ROOT. One JSON line per call is appended to LOG (tool, ok, output head, time).
import fs from 'node:fs';
import readline from 'node:readline';
import { TOOLS, execute } from './tools.mjs';

const [root, log] = process.argv.slice(2);
const send = (o) => process.stdout.write(JSON.stringify(o) + '\n');
readline.createInterface({ input: process.stdin }).on('line', (line) => {
  let m;
  try { m = JSON.parse(line); } catch { return; }
  if (m.id === undefined) return;
  if (m.method === 'initialize') {
    send({ jsonrpc: '2.0', id: m.id, result: { protocolVersion: m.params?.protocolVersion ?? '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'tools', version: '1' } } });
  } else if (m.method === 'tools/list') {
    send({ jsonrpc: '2.0', id: m.id, result: { tools: TOOLS.map((t) => ({ name: t.name, description: t.description, inputSchema: t.parameters })) } });
  } else if (m.method === 'tools/call') {
    const r = execute(root, m.params?.name, m.params?.arguments ?? {});
    fs.appendFileSync(log, JSON.stringify({ t: Date.now(), tool: m.params?.name, ok: r.ok, out: r.output.slice(0, 120) }) + '\n');
    send({ jsonrpc: '2.0', id: m.id, result: { content: [{ type: 'text', text: r.output }], isError: !r.ok } });
  } else {
    send({ jsonrpc: '2.0', id: m.id, error: { code: -32601, message: 'Unsupported' } });
  }
});
