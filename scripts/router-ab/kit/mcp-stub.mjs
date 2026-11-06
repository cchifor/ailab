// kit/mcp-stub.mjs TOOLS_JSON - inert stdio MCP server for the Claude direct arm of chat tasks: lists the A/B's function tools under
// their real names and executes nothing (tools/call returns empty content), like the router's schema-only bridge.
import fs from 'node:fs';
import readline from 'node:readline';
const tools = JSON.parse(fs.readFileSync(process.argv[2], 'utf8')).tools.map(t => ({ name: t.function.name, description: t.function.description, inputSchema: t.function.parameters }));
const rl = readline.createInterface({ input: process.stdin });
const send = (o) => process.stdout.write(JSON.stringify(o) + '\n');
rl.on('line', (line) => {
  let m; try { m = JSON.parse(line); } catch { return; }
  if (m.id === undefined) return;
  if (m.method === 'initialize') send({ jsonrpc: '2.0', id: m.id, result: { protocolVersion: m.params?.protocolVersion ?? '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'tools', version: '1' } } });
  else if (m.method === 'tools/list') send({ jsonrpc: '2.0', id: m.id, result: { tools } });
  else if (m.method === 'tools/call') send({ jsonrpc: '2.0', id: m.id, result: { content: [] } });
  else send({ jsonrpc: '2.0', id: m.id, error: { code: -32601, message: 'Unsupported' } });
});
