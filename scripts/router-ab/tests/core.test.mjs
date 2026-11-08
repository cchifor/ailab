// tests/core.test.mjs - the gate's pure core and its registries (node --test; no network, no hosts).
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { abba } from '../core/abba.mjs';
import { evaluate, evaluateLayerA, MARGINS, NO_BASELINE } from '../core/margins.mjs';
import { LOGIN_MARGIN_MS, loginProblem } from '../providers/lib/claude-login.mjs';
import { render } from '../core/report.mjs';
import { loadProviders, selectProviders, validate } from '../providers/index.mjs';
import { TASKS } from '../tasks/registry.mjs';
import { GRADERS } from '../tasks/graders.mjs';
import { seatsOf } from '../core/seats.mjs';

const HERE = path.dirname(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')));
const run = (arm, pair, m) => ({ arm, pair, metrics: { pass: true, wall_s: 10, router_errors: 0, retries: 0, ...m } });
const row = (v, id) => v.rows.find((r) => r.id === id);

test('abba interleaves the arms and alternates who goes first', () => {
  assert.deepEqual(abba(2).map((x) => `${x.pair}${x.arm[0]}`), ['1d', '1r', '2r', '2d']);
  assert.equal(abba(3).length, 6);
});

test('identical arms pass every margin; metrics nobody measured are n/a', () => {
  const runs = [1, 2, 3].flatMap((p) => [run('direct', p, {}), run('router', p, {})]);
  const v = evaluate(runs);
  assert.equal(v.verdict, 'PASS');
  assert.equal(row(v, 'ttft').status, 'n/a');
  assert.equal(row(v, 'success').status, 'PASS');
});

test('success: under 6 pairs one extra failure is NEAR and two are OUT (6 pairs decide); in 6 pairs one passes', () => {
  const mk = (pairs, routerFails) => [...Array(pairs)].flatMap((_, i) => [run('direct', i + 1, {}), run('router', i + 1, { pass: i >= routerFails })]);
  assert.equal(row(evaluate(mk(3, 1)), 'success').status, 'NEAR');
  assert.equal(row(evaluate(mk(3, 2)), 'success').status, 'OUT');
  assert.equal(evaluate(mk(3, 2)).verdict, 'RERUN (6 pairs decide, §9.6)');
  assert.equal(row(evaluate(mk(6, 1)), 'success').status, 'PASS');
  assert.equal(row(evaluate(mk(6, 2)), 'success').status, 'FAIL');
});

const six = (d, r) => [1, 2, 3, 4, 5, 6].flatMap((p) => [run('direct', p, d), run('router', p, r)]);

test('wall time: 1.25x is the bound, NEAR within half a margin; outside it, OUT under 6 pairs and FAIL at 6', () => {
  const mk = (r) => [run('direct', 1, { wall_s: 100 }), run('router', 1, { wall_s: r })];
  assert.equal(row(evaluate(mk(110)), 'wall').status, 'PASS');
  assert.equal(row(evaluate(mk(120)), 'wall').status, 'NEAR');
  assert.equal(row(evaluate(mk(130)), 'wall').status, 'OUT');
  assert.equal(row(evaluate(six({ wall_s: 100 }, { wall_s: 130 })), 'wall').status, 'FAIL');
  assert.equal(evaluate(six({ wall_s: 100 }, { wall_s: 130 })).verdict, 'FAIL');
  // at 6 pairs the decision is made: within half a margin is still inside it
  assert.equal(row(evaluate(six({ wall_s: 100 }, { wall_s: 120 })), 'wall').status, 'NEAR');
  assert.equal(evaluate(six({ wall_s: 100 }, { wall_s: 120 })).verdict, 'PASS');
});

test('TTFT: direct + 2 s; uncached 1.2x; rejected +2 pp; any router error fails', () => {
  assert.equal(row(evaluate(six({ ttft_s: 1 }, { ttft_s: 3.5 })), 'ttft').status, 'FAIL');
  assert.equal(row(evaluate([run('direct', 1, { ttft_s: 1 }), run('router', 1, { ttft_s: 2 })]), 'ttft').status, 'NEAR');
  assert.equal(row(evaluate(six({ uncached_tokens: 100 }, { uncached_tokens: 125 })), 'uncached').status, 'FAIL');
  assert.equal(row(evaluate(six({ tool_calls: 100, rejected: 0 }, { tool_calls: 100, rejected: 3 })), 'tool_validity').status, 'FAIL');
  // decisive: a router error fails at any sample size
  assert.equal(row(evaluate([run('direct', 1, {}), run('router', 1, { router_errors: 1 })]), 'errors').status, 'FAIL');
});

test('wall time is judged per output token where reported: more work is WATCH, a slower router is not excused', () => {
  const m = (wall_s, output_tokens) => ({ wall_s, output_tokens, requests: 5 });
  const more = evaluate(six(m(10, 1000), m(15, 1500)));
  assert.equal(row(more, 'wall').status, 'PASS'); assert.equal(row(more, 'work').status, 'WATCH');
  assert.equal(more.verdict, 'PASS'); assert.match(more.notes[0], /more model work/);
  const slower = evaluate(six(m(10, 1000), m(15, 1000)));
  assert.equal(row(slower, 'wall').status, 'FAIL'); assert.equal(row(slower, 'work').status, 'PASS');
  assert.equal(row(evaluate(six({ wall_s: 10 }, { wall_s: 15 })), 'work').status, 'n/a');
});

test('the unittest grader reads unittest\'s own exit status: failing tests fail, and no pipeline can mask them', () => {
  let cmd;
  const g = (status, clean = true) => GRADERS['python-unittest']({ module: 'test_calc', protect: [] },
    { workspace: '/w', clean: () => clean, run: (_d, _i, c) => { cmd = c; return { status, stdout: status ? 'FAILED (failures=3)\n' : 'OK\n' }; } });
  assert.equal(g(1).pass, false); assert.equal(g(1).detail, 'FAILED (failures=3)');
  assert.equal(g(0).pass, true); assert.equal(g(0, false).pass, false);
  assert.doesNotMatch(cmd, /\|/);
});

test('below the minimum output raw wall is judged: a tiny denominator neither fails nor excuses a session', () => {
  const m = (wall_s, output_tokens) => ({ wall_s, output_tokens, requests: 2 });
  assert.equal(row(evaluate(six(m(7.7, 45), m(7.1, 30))), 'wall').status, 'PASS');
  assert.equal(row(evaluate(six(m(10, 40), m(15, 60))), 'wall').status, 'FAIL');
  assert.equal(row(evaluate(six(m(10, 300), m(15, 40))), 'wall').status, 'FAIL'); // one arm tiny: raw, so no excuse
});

test('Layer (a) needs every check to pass, and no checks is n/a', () => {
  assert.equal(evaluateLayerA([{ name: 'a', pass: true }, { name: 'b', pass: false }]).verdict, 'FAIL');
  assert.equal(evaluateLayerA([]).verdict, 'n/a');
});

test('a task with no direct baseline cannot pass, fail or ask for a rerun', () => {
  const arm = (name, pass, wall_s) => [1, 2, 3].map((p) => run(name, p, { pass, wall_s }));
  // 2026-11-07: an expired login. Direct 0/3 in 2.5 s, router 3/3.
  const v = evaluate([...arm('direct', false, 2.5), ...arm('router', true, 4.9)]);
  assert.equal(v.verdict, NO_BASELINE);
  assert.match(v.notes.join(' '), /no direct run passed/);
  assert.equal(evaluate([...arm('direct', false, 2.5), ...arm('router', false, 4.9)]).verdict, NO_BASELINE);
  const r = render({ image: 'sha256:x', startedAt: 'now', providers: [{ name: 'claude', layerA: evaluateLayerA([{ name: 'a', pass: true }]), tasks: [{ id: 'recall', ...v }] }] });
  assert.equal(r.overall, 'INCOMPLETE');
  assert.match(r.text, /\| claude \| recall \| INVALID \(no direct baseline; rerun\) \|/);
});

test('a task with no direct runs at all has no baseline either', () => {
  assert.equal(evaluate([run('router', 1, {}), run('router', 2, {})]).verdict, NO_BASELINE);
});

test('one direct run passing is still a baseline', () => {
  const runs = [run('direct', 1, {}), run('direct', 2, { pass: false }), run('router', 1, {}), run('router', 2, {})];
  assert.notEqual(evaluate(runs).verdict, NO_BASELINE);
});

test('a Claude login must outlive the run', () => {
  const now = Date.parse('2026-11-07T08:00:00Z');
  assert.equal(loginProblem(now + LOGIN_MARGIN_MS, now), undefined);
  assert.match(loginProblem(now + LOGIN_MARGIN_MS - 1, now), /expires at/);
  assert.match(loginProblem(now - 1000, now), /expired at 2026-11-07T07:59:59/);
  assert.match(loginProblem(Number.NaN, now), /no readable Claude login/);
  assert.match(loginProblem(0, now), /no readable Claude login/);
});

test('the report renders an overall verdict', () => {
  const v = evaluate([run('direct', 1, {}), run('router', 1, {})]);
  const r = render({ image: 'sha256:x', startedAt: 'now', providers: [{ name: 'p', layerA: evaluateLayerA([{ name: 'a', pass: true }]), tasks: [{ id: 't', ...v }] }] });
  assert.equal(r.overall, 'PASS');
  assert.match(r.text, /\| p \| t \| PASS \|/);
});

test('every margin defines its value, check and display', () => {
  for (const m of MARGINS) for (const k of ['id', 'label', 'margin', 'value', 'check', 'show']) assert.ok(m[k], `${m.id}: ${k}`);
});

test('every registered task has a known grader, a runnable kind and its fixture', async () => {
  const providers = await loadProviders(path.join(HERE, 'providers'));
  const ids = new Set();
  for (const t of TASKS) {
    assert.ok(!ids.has(t.id), `duplicate task id ${t.id}`); ids.add(t.id);
    assert.ok(GRADERS[t.grader.name], `${t.id}: grader ${t.grader.name}`);
    for (const name of t.providers) {
      const p = providers.find((x) => x.name === name);
      assert.ok(p, `${t.id}: provider ${name}`);
      assert.ok(p.kinds.includes(t.kind), `${t.id}: ${name} cannot run kind ${t.kind}`);
    }
    if (t.fixture) assert.ok(fs.existsSync(path.join(HERE, 'tasks', 'fixtures', t.fixture)), `${t.id}: fixture ${t.fixture}`);
    if (t.hidden) assert.ok(fs.existsSync(path.join(HERE, 'tasks', 'fixtures', t.hidden)), `${t.id}: hidden ${t.hidden}`);
  }
});

test('a provider missing part of the interface is refused', () => {
  assert.throws(() => validate({ name: 'x', describe: 'y', kinds: [] }, 'x.mjs'), /layerA/);
  assert.throws(() => validate({ name: 'x', describe: 'y', kinds: [], layerA() {}, run() {}, optIn: 'yes' }, 'x.mjs'), /optIn/);
});

test('an opt-in provider runs only when named', () => {
  const known = [{ name: 'a' }, { name: 'b', optIn: true }, { name: 'c', optIn: false }];
  const names = (sel) => sel.map((p) => p.name);
  assert.deepEqual(names(selectProviders(known, undefined)).sort(), ['a', 'c']);
  assert.deepEqual(names(selectProviders(known, ['b', 'a'])).sort(), ['a', 'b']);
});

test('the full gate runs all three qwen backends of the route', async () => {
  const known = await loadProviders(path.join(HERE, 'providers'));
  const names = selectProviders(known, undefined).map((p) => p.name);
  for (const n of ['qwen', 'qwen-cloud3', 'qwen-cloud1']) assert.ok(names.includes(n), n);
});

test('a qwen backend the route does not use is refused at setup, before any run', async () => {
  const p = (await loadProviders(path.join(HERE, 'providers'))).find((x) => x.name === 'qwen-cloud1');
  const sent = [];
  const ctx = (stdout, stderr = '') => ({ remote: '/tmp/x', inPod: (script, args) => { sent.push([path.basename(script), ...args]); return { status: stdout ? 0 : 1, stdout, stderr }; }, ssh: () => ({ status: 0, stdout: '', stderr: '' }) });
  const routed = (accounts) => JSON.stringify({ route: 'qwen3.8-27b', via: 'qwen-27b', accounts });
  await assert.rejects(p.setup(ctx(routed(['lan-192.168.0.28-18020', 'lan-192.168.0.26-8080']))), /lan-192\.168\.0\.27-8080 is not an enabled deployment of route qwen3\.8-27b \(via qwen-27b\)/);
  await assert.rejects(p.setup(ctx('', 'no route default/qwen-27b')), /cannot read route qwen-27b: no route/);
  await assert.rejects(p.setup(ctx('{"error":"x"}')), /cannot read route qwen-27b/); // valid JSON of another shape
  await p.setup(ctx(routed(['lan-192.168.0.28-18020', 'lan-192.168.0.27-8080'])));
  assert.deepEqual(sent[0], ['route-targets.mjs', 'default', 'qwen-27b']);
});

test('the seat guard: one account passes, a second account or an unreadable journal is a broken pairing', () => {
  const ctx = (out) => ({ inPod: () => ({ stdout: typeof out === 'string' ? out : JSON.stringify(out) }) });
  assert.deepEqual(seatsOf(ctx({ a: { accounts: ['claude-2'] }, b: { accounts: ['claude-2', 'claude-2'] } }), ['a', 'b']), { accounts: ['claude-2'], moved: false });
  assert.equal(seatsOf(ctx({ a: { accounts: ['claude-2'] }, b: { accounts: ['claude-3'] } }), ['a', 'b']).moved, true);
  assert.equal(seatsOf(ctx({ a: { accounts: ['claude-2', 'claude-3'] } }), ['a']).moved, true);
  for (const [out, ids] of [['not json', ['a']], [{ a: { error: 404 } }, ['a']], [{}, []], [{}, [null]], [{ a: { accounts: ['claude-2'] } }, ['a', null]]]) {
    const r = seatsOf(ctx(out), ids);
    assert.equal(r.moved, true); assert.ok(r.error);
  }
});

test('a known deviation is judged against its own bound, and says when it no longer shows', () => {
  const runs = (rw) => [run('direct', 1, { wall_s: 10 }), run('router', 1, { wall_s: rw })];
  const known = { wall: { bound: 1.5, reason: 'per-request process start' } };
  assert.equal(evaluate(runs(13)).verdict, 'RERUN (6 pairs decide, §9.6)');
  const k = evaluate(runs(13), known);
  assert.equal(row(k, 'wall').status, 'KNOWN'); assert.equal(k.verdict, 'PASS (known deviation)'); assert.match(k.notes[0], /per-request/);
  assert.equal(row(evaluate(runs(16), known), 'wall').status, 'OUT');
  assert.equal(row(evaluate(runs(12), known), 'wall').status, 'NEAR'); // inside §9.6: not a deviation
  const gone = evaluate(runs(10), known);
  assert.equal(gone.verdict, 'PASS'); assert.match(gone.notes[0], /no longer shows/);
  assert.equal(render({ image: 'x', startedAt: 't', providers: [{ name: 'p', layerA: { verdict: 'PASS', checks: [] }, tasks: [{ id: 't', pairs: 1, ...k }] }] }).overall, 'PASS');
});

test('every known deviation names a boundable margin, a looser bound and a reason', () => {
  for (const t of TASKS) for (const [id, k] of Object.entries(t.known ?? {})) {
    const m = MARGINS.find((x) => x.id === id);
    assert.ok(m && m.bound !== undefined, `${t.id}: ${id} is not a margin with a bound`);
    assert.ok(typeof k.bound === 'number' && k.bound > m.bound, `${t.id}: ${id} bound must loosen the margin`);
    assert.ok(typeof k.reason === 'string' && k.reason.length > 20, `${t.id}: ${id} needs a reason`);
  }
});
