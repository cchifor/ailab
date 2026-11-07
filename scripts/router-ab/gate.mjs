#!/usr/bin/env node
// gate.mjs - the router release gate: for each provider, Layer (a) (what the model receives, the same request direct and
// via the router) and short Layer (b) tasks in ABBA pairs, judged against the §9.6 margins. Runs on the operator
// workstation (kubectl context admin@ai, ssh to the worker). See README.md.
//
//   node scripts/router-ab/gate.mjs                           # every provider, the gate tasks, 3 pairs
//   node scripts/router-ab/gate.mjs --providers claude --tasks textkit-agentic --pairs 2
//   node scripts/router-ab/gate.mjs --plan                    # print what would run, run nothing
// Options: --providers a,b  --tasks id,..  --all (include gate:false tasks)  --pairs N  --worker user@host
//          --tap-port N  --out FILE (also write the Markdown report)
//          --keep (debugging only: leave the temp dirs, recordings included, for the operator to delete)
import fs from 'node:fs';
import path from 'node:path';
import { abba } from './core/abba.mjs';
import { createContext, HERE } from './core/context.mjs';
import { evaluate, evaluateLayerA } from './core/margins.mjs';
import { render } from './core/report.mjs';
import { loadProviders, selectProviders } from './providers/index.mjs';
import { TASKS, tasksFor } from './tasks/registry.mjs';

const args = Object.fromEntries(process.argv.slice(2).reduce((a, x, i, all) => (x.startsWith('--') ? [...a, [x.slice(2), all[i + 1] && !all[i + 1].startsWith('--') ? all[i + 1] : true]] : a), []));
const list = (v) => (typeof v === 'string' ? v.split(',') : undefined);
const usage = (msg) => { console.error(`gate: ${msg}`); process.exit(2); };
const known = await loadProviders();
const providers = selectProviders(known, list(args.providers));
const pairs = Number(args.pairs ?? 3);
// a typo must not certify an untested release: refuse anything that would quietly run less than was asked for
if (!Number.isInteger(pairs) || pairs < 1) usage(`--pairs must be a positive integer, got ${args.pairs}`);
for (const n of list(args.providers) ?? []) if (!known.some((p) => p.name === n)) usage(`unknown provider ${n}`);
for (const n of list(args.tasks) ?? []) if (!TASKS.some((t) => t.id === n)) usage(`unknown task ${n}`);
const plan = providers.map((p) => ({ p, tasks: tasksFor(p, { only: list(args.tasks), all: !!args.all }) })).filter((x) => x.tasks.length);
if (!plan.length) usage('nothing selected: no provider runs these tasks');
for (const n of list(args.tasks) ?? []) if (!plan.some((x) => x.tasks.some((t) => t.id === n))) usage(`no selected provider runs task ${n}`);
for (const n of list(args.providers) ?? []) if (!plan.some((x) => x.p.name === n)) usage(`provider ${n} runs none of the selected tasks`);

if (args.plan) {
  for (const { p, tasks } of plan) console.log(`${p.name}: Layer (a), then ${tasks.map((t) => t.id).join(', ') || '(no tasks)'} x ${pairs} pairs ABBA`);
  for (const p of known.filter((x) => !providers.includes(x))) if (p.optIn) console.log(`${p.name}: opt-in, not run (name it in --providers)`);
  process.exit(0);
}

const ctx = createContext({ worker: args.worker, kubeContext: args['kube-context'] });
ctx.tapPort = Number(args['tap-port'] ?? 18610);
const startedAt = new Date().toISOString();
const results = [];
let exitCode = 3; // until a report says otherwise: a gate that could not run has judged nothing
try {
  ctx.log(`gate ${ctx.id}: deploying the kit to ${ctx.worker}:${ctx.remote}`);
  for (const [from, to] of [[path.join(HERE, 'kit'), `${ctx.remote}/kit`], [path.join(HERE, 'tasks', 'fixtures'), `${ctx.remote}/fixtures`]]) {
    const r = ctx.push(from, to);
    if (r.status !== 0) throw new Error(`could not deploy to the worker: ${(r.stderr || '').slice(-160)}`);
  }
  const tap = ctx.ssh(`mkdir -p -m 700 ${ctx.remote}/kit/rec ${ctx.remote}/kit/runs && (TAP_FWD=${plan.some((x) => x.p.forwarder) ? 1 : 0} setsid nohup node ${ctx.remote}/kit/tap.mjs ${ctx.tapPort} ${ctx.remote}/kit/rec > ${ctx.remote}/tap.log 2>&1 < /dev/null &) && for i in 1 2 3 4 5 6 7 8 9 10; do grep -q 'abtap on' ${ctx.remote}/tap.log && break; sleep 0.5; done; cat ${ctx.remote}/tap.log`);
  if (!/abtap on/.test(tap.stdout ?? '')) throw new Error(`the tap did not start: ${(tap.stdout ?? '').slice(-160)}`);
  for (const { p, tasks } of plan) {
    let layerA = { checks: [], verdict: 'n/a' };
    const taskResults = [];
    // a provider that cannot run says nothing about the router: INVALID, and the report is still written
    try {
      ctx.log(`${p.name}: setup`); await p.setup?.(ctx);
      ctx.log(`${p.name}: Layer (a)`);
      layerA = evaluateLayerA(await p.layerA(ctx));
      let throttled = false;
      for (const task of tasks) {
        if (throttled) { taskResults.push({ id: task.id, rows: [], pairs: 0, verdict: 'INVALID (seat, failover or harness; rerun)', notes: ['skipped: the seat is throttled'] }); continue; }
        const runs = [];
        for (const { pair, arm } of abba(pairs)) {
          const runId = `${p.name}-${task.id}-${pair}-${arm}`;
          ctx.log(`${runId}: start`);
          let metrics;
          try { metrics = await p.run(ctx, task, arm, runId); } catch (e) { metrics = { pass: false, invalid: true, detail: `harness error: ${e.message}`, wall_s: null, router_errors: 0, retries: 0 }; }
          ctx.log(`${runId}: ${metrics.pass ? 'pass' : 'FAIL'} ${metrics.detail ?? ''} (${metrics.wall_s} s)`);
          runs.push({ pair, arm, metrics });
          // the task is already INVALID: stop spending the seat on it
          if (metrics.invalid) { throttled = /seat throttled/.test(metrics.detail); break; }
        }
        const v = evaluate(runs, task.known);
        // a run that could not be compared (a seat limit, a failover between accounts, a harness error) says nothing about
        // the router
        if (runs.some((r) => r.metrics.invalid)) v.verdict = 'INVALID (seat, failover or harness; rerun)';
        taskResults.push({ id: task.id, ...v, notes: [...v.notes, ...runs.filter((r) => !r.metrics.pass).map((r) => `${r.arm} pair ${r.pair} failed: ${r.metrics.detail ?? ''}`)] });
      }
    } catch (e) {
      ctx.log(`${p.name}: harness error: ${e.message}`);
      taskResults.push({ id: '(provider stopped)', rows: [], pairs: 0, verdict: 'INVALID (seat, failover or harness; rerun)', notes: [`harness error: ${String(e.message).slice(0, 200)}`] });
    }
    results.push({ name: p.name, describe: p.describe, layerA, tasks: taskResults });
    try { await p.teardown?.(ctx); } catch (e) { ctx.log(`${p.name}: teardown: ${e.message}`); }
  }
  const report = render({ image: ctx.image(), startedAt, providers: results });
  console.log(report.text);
  if (typeof args.out === 'string') fs.writeFileSync(args.out, report.text + '\n');
  exitCode = report.overall === 'PASS' ? 0 : report.overall === 'FAIL' ? 1 : 3; // 3: RERUN (6 pairs decide) or INCOMPLETE
} catch (e) {
  ctx.log(`INCOMPLETE: the gate could not run: ${e.message}`);
} finally {
  if (args.keep) ctx.log(`--keep: left ${ctx.worker}:${ctx.remote} and ${ctx.local} (recordings included): delete them`);
  else { ctx.log(`teardown: deleting ${ctx.worker}:${ctx.remote} (recordings included) and ${ctx.local}`); ctx.teardown(); }
}
process.exit(exitCode);
