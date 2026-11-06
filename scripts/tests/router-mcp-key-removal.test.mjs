import { test } from 'node:test';
import assert from 'node:assert/strict';
import { checkRemoval, runRemoval } from '../../kubernetes/apps/apps/llm-router/mcp-key-removal.mjs';

const targets = [
  { ref: 'store:sec_subscription', label: 'claude subscription token' },
  { ref: 'store:sec_api', label: 'anthropic' },
];
const proposal = target => ({ proposal: { kind: 'approval', proposalId: 'fixture-proposal', diff: [{ op: '-', text: `secret ${target.ref} (${target.label})` }] } });

test('approves only the complete exact single-key removal diff', () => {
  assert.equal(checkRemoval(proposal(targets[0]), targets[0]), 'fixture-proposal');
  for (const change of [
    p => { p.truncated = true; },
    p => { p.proposal.diff.push({ op: '-', text: 'account unrelated' }); },
    p => { p.proposal.diff[0].op = '+'; },
    p => { p.proposal.diff[0].text = 'secret sec_other (anthropic)'; },
    p => { delete p.proposal.proposalId; },
  ]) {
    const result = proposal(targets[0]); change(result);
    assert.throws(() => checkRemoval(result, targets[0]));
  }
});

function mockRouter(t, { used = false, refreshed = false, transportFailure = false } = {}) {
  const calls = [], remaining = [...targets, { ref: 'store:sec_unrelated', label: 'new key' }];
  let current;
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    if (transportFailure) throw new Error('sensitive-transport-detail');
    const request = JSON.parse(options.body);
    if (request.method === 'initialize') return Response.json({ result: { serverInfo: { name: 'llm-router' } } });
    assert.equal(request.method, 'tools/call');
    const { name, arguments: args } = request.params; calls.push(name);
    let result;
    if (name === 'router_open_session') result = { sessionId: 'fixture-session' };
    else if (name === 'list_secrets') result = { data: { secrets: remaining.map(s => ({ ...s, usedBy: used && s.ref === targets[1].ref ? ['existing'] : [] })) } };
    else if (name === 'remove_secret') { current = targets.find(s => s.ref === `store:${args.id}`); assert(current); result = proposal(current); }
    else if (name === 'router_resolve_proposal') {
      assert.equal(args.decision, 'approve'); assert.equal(args.proposalId, 'fixture-proposal');
      if (refreshed) result = { outcome: 'review_required', proposal: { proposalId: 'refreshed' } };
      else { remaining.splice(remaining.findIndex(s => s.ref === current.ref), 1); result = { outcome: 'approved', result: { data: { id: current.ref.slice(6) } } }; }
    } else assert.fail(`Unexpected tool ${name}`);
    return Response.json({ result: { structuredContent: result } });
  });
  return { calls, remaining };
}
const options = log => ({ url: 'http://fixture.invalid', token: 'fixture-management-token-never-log', targets, log: x => log.push(x) });

test('removes exactly the two requested keys and verifies their absence', async t => {
  const { calls, remaining } = mockRouter(t), log = [];
  await runRemoval(options(log));
  assert.deepEqual(remaining.map(s => s.ref), ['store:sec_unrelated']);
  assert.equal(calls.filter(n => n === 'router_resolve_proposal').length, 2);
  assert.equal(calls.at(-1), 'list_secrets');
  assert.equal(JSON.parse(log.at(-1)).event, 'mcp.key_removal_passed');
  assert(!log.join('').includes('fixture-management-token-never-log'));
});

test('checks both keys are unused before proposing either removal', async t => {
  const { calls } = mockRouter(t, { used: true }), log = [];
  await assert.rejects(runRemoval(options(log)));
  assert(!calls.includes('remove_secret'));
  assert(log.join('').includes('Saved key is in use or usage is unknown'));
});

test('a refreshed proposal is not approved again or retried', async t => {
  const { calls, remaining } = mockRouter(t, { refreshed: true }), log = [];
  await assert.rejects(runRemoval(options(log)));
  assert.equal(calls.filter(n => n === 'router_resolve_proposal').length, 1);
  assert.equal(calls.filter(n => n === 'remove_secret').length, 1);
  assert.equal(remaining.length, 3);
});

test('transport failures never expose their text or the management credential', async t => {
  mockRouter(t, { transportFailure: true }); const log = [];
  await assert.rejects(runRemoval(options(log)));
  assert(!log.join('').includes('sensitive-transport-detail'));
  assert(!log.join('').includes('fixture-management-token-never-log'));
  assert.equal(JSON.parse(log.at(-1)).reason, 'Transport or response failure');
});
