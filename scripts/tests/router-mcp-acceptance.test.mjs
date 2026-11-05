import { test } from 'node:test';
import assert from 'node:assert/strict';
import { checkSetup, runAcceptance } from '../../kubernetes/apps/apps/llm-router/mcp-acceptance.mjs';

const before = { accounts: [{ id: 'existing' }], routes: [{ alias: 'existing' }] };
const secretRef = 'store:sec_fixture';
const valid = () => ({
  data: { proposed: { alias: 'claude-api', provider: 'anthropic', mode: 'new', account: 'anthropic', model: 'claude-fixture' } },
  proposal: { proposalId: 'proposal-fixture', diff: [
    { op: '+', text: `account anthropic  provider=anthropic access=api key=${secretRef} concurrency=4` },
    { op: '+', text: 'route claude-api  → anthropic/claude-fixture  strategy=priority retries=1 wait 300s · idle 120s' },
  ] },
});

test('accepts only the requested additive hosted Anthropic setup', () => {
  assert.equal(checkSetup(valid(), before, secretRef, ['claude-fixture']).account, 'anthropic');
});

for (const [name, change] of [
  ['existing configuration removal', p => p.proposal.diff.push({ op: '-', text: 'route existing' })],
  ['existing account reuse', p => { p.data.proposed.account = 'existing'; }],
  ['different route', p => { p.data.proposed.alias = 'existing'; }],
  ['different provider', p => { p.data.proposed.provider = 'claude'; }],
  ['replacement mode', p => { p.data.proposed.mode = 'replace'; }],
  ['undiscovered model', p => { p.data.proposed.model = 'invented'; }],
  ['custom base URL', p => { p.proposal.diff[0].text += ' baseUrl=https://unexpected.invalid'; }],
  ['wrong credential reference', p => { p.proposal.diff[0].text = p.proposal.diff[0].text.replace(secretRef, 'store:sec_other'); }],
  ['wrong route destination', p => { p.proposal.diff[1].text = p.proposal.diff[1].text.replace('anthropic/claude-fixture', 'existing/other'); }],
  ['truncated result', p => { p.truncated = true; }],
]) test(`refuses ${name} before approval`, () => {
  const proposal = valid(); change(proposal);
  assert.throws(() => checkSetup(proposal, before, secretRef, ['claude-fixture']));
});

test('refuses a concurrently existing claude-api route', () => {
  assert.throws(() => checkSetup(valid(), { ...before, routes: [{ alias: 'claude-api' }] }, secretRef, ['claude-fixture']));
});

test('does not log upstream errors or retry a failed tool', async t => {
  const log = [], requests = [];
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    const request = JSON.parse(options.body); requests.push(request);
    let result;
    if (request.method === 'initialize') result = { serverInfo: { name: 'llm-router' } };
    else if (request.method === 'tools/list') result = { tools: ['setup_provider', 'probe_provider', 'router_resolve_proposal', 'router_request', 'audit_log'].map(name => ({ name })) };
    else result = { isError: true, content: [{ type: 'text', text: 'sensitive-upstream-error' }] };
    return Response.json({ jsonrpc: '2.0', id: request.id, result });
  });
  await assert.rejects(runAcceptance({ url: 'http://fixture.invalid/admin/mcp', token: 'fixture-token-never-log', log: x => log.push(x) }), /MCP acceptance failed/);
  assert.equal(requests.filter(r => r.method === 'tools/call').length, 1);
  assert(!log.join('').includes('sensitive-upstream-error'));
  assert(!log.join('').includes('fixture-token-never-log'));
  assert(log.join('').includes('router_open_session'));
});


test('malformed successful JSON never leaks its response snippet', async t => {
  const log = [];
  t.mock.method(globalThis, 'fetch', async () => new Response('{"credential":"sensitive-malformed-json-marker", broken', { status: 200 }));
  await assert.rejects(runAcceptance({ url: 'http://fixture.invalid/admin/mcp', token: 'fixture-token', log: x => log.push(x) }), /MCP acceptance failed/);
  assert(!log.join('').includes('sensitive-malformed-json-marker'));
  assert(log.join('').includes('MCP response was not valid JSON'));
});

test('unexpected library errors never contribute their message to logs', async t => {
  const log = [];
  t.mock.method(globalThis, 'fetch', async () => { throw new Error('sensitive-library-error-marker'); });
  await assert.rejects(runAcceptance({ url: 'http://fixture.invalid/admin/mcp', token: 'fixture-token', log: x => log.push(x) }), /MCP acceptance failed/);
  assert(!log.join('').includes('sensitive-library-error-marker'));
  assert(log.join('').includes('Transport or response failure'));
});
