import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

test('inspection calls only initialize/list_secrets and logs selected metadata', () => {
  const preload = `globalThis.fetch = async (_url, options) => {
    const request = JSON.parse(options.body);
    if (request.method === 'initialize') return Response.json({ result: { serverInfo: { name: 'llm-router' } } });
    if (request.method !== 'tools/call' || request.params.name !== 'list_secrets') throw new Error('unexpected operation');
    return Response.json({ result: { structuredContent: { data: { secrets: [
      { ref: 'store:sec_fixture', label: 'Anthropic API', createdAt: 1, lastUsedAt: null, usedBy: [], value: 'sensitive-extra-field' },
      { ref: 'store:sec_used', label: 'Claude used', usedBy: ['existing'] },
      { ref: 'store:sec_other', label: 'Other', usedBy: [] }
    ] } } } });
  };`;
  const result = spawnSync(process.execPath, ['--import', `data:text/javascript;base64,${Buffer.from(preload).toString('base64')}`, fileURLToPath(new URL('../../kubernetes/apps/apps/llm-router/mcp-key-inspection.mjs', import.meta.url))], {
    encoding: 'utf8', timeout: 10_000, env: { ...process.env, ROUTER_MCP_ACCEPTANCE_TOKEN: 'fixture-management-token-never-log' },
  });
  assert.ifError(result.error);
  assert.equal(result.status, 0);
  assert.equal(JSON.parse(result.stdout).total, 3);
  assert.equal(JSON.parse(result.stdout).matched, 1);
  assert.deepEqual(JSON.parse(result.stdout).candidates, [{ ref: 'store:sec_fixture', label: 'Anthropic API', createdAt: 1, lastUsedAt: null }]);
  assert(!result.stdout.includes('sensitive-extra-field'));
  assert(!result.stdout.includes('fixture-management-token-never-log'));
});

test('inspection never logs raw transport failures', () => {
  const preload = 'globalThis.fetch = async () => { throw new Error("sensitive-error-detail"); };';
  const result = spawnSync(process.execPath, ['--import', `data:text/javascript;base64,${Buffer.from(preload).toString('base64')}`, fileURLToPath(new URL('../../kubernetes/apps/apps/llm-router/mcp-key-inspection.mjs', import.meta.url))], {
    encoding: 'utf8', timeout: 10_000, env: { ...process.env, ROUTER_MCP_ACCEPTANCE_TOKEN: 'fixture-management-token-never-log' },
  });
  assert.ifError(result.error);
  assert.equal(result.status, 1);
  assert.equal(JSON.parse(result.stdout).event, 'mcp.key_inspection_failed');
  assert.equal(JSON.parse(result.stdout).reason, 'transport');
  assert(!result.stdout.includes('sensitive-error-detail'));
  assert.equal(result.stderr, '');
});

for (const [response, reason, httpStatus] of [
  ['new Response("sensitive", { status: 401 })', 'HTTP failure', 401],
  ['new Response("sensitive", { headers: { "content-type": "text/event-stream" } })', 'Expected JSON response', undefined],
  ['new Response("sensitive", { headers: { "content-type": "application/json" } })', 'Invalid JSON response', undefined],
]) test(`inspection reports the safe ${reason} guard`, () => {
  const preload = `globalThis.fetch = async () => ${response};`;
  const result = spawnSync(process.execPath, ['--import', `data:text/javascript;base64,${Buffer.from(preload).toString('base64')}`, fileURLToPath(new URL('../../kubernetes/apps/apps/llm-router/mcp-key-inspection.mjs', import.meta.url))], {
    encoding: 'utf8', timeout: 10_000, env: { ...process.env, ROUTER_MCP_ACCEPTANCE_TOKEN: 'fixture-management-token-never-log' },
  });
  assert.ifError(result.error);
  assert.equal(result.status, 1);
  const event = JSON.parse(result.stdout);
  assert.equal(event.reason, reason);
  assert.equal(event.httpStatus, httpStatus);
  assert(!result.stdout.includes('sensitive'));
});
