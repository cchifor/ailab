// providers/lib/codex-session.mjs - shared by every provider whose client is the Codex CLI (codex, qwen): one Codex
// session on the worker (kit/codex-arm.sh) through the recording tap, the run's metrics (kit/summarize.py), and the
// workspace and answers pulled back for grading. Not a provider itself (providers/index.mjs loads only top-level files).
import fs from 'node:fs';
import path from 'node:path';
import { HERE } from '../../core/context.mjs';
import { grade } from '../../tasks/graders.mjs';
import { seatsOf } from '../../core/seats.mjs';
import { notPaired } from '../../core/pairing.mjs';

/** The router's name for the account of the ChatGPT login the direct arm runs under (RAB_CODEX_ACCOUNT, as
 * RAB_CLAUDE_ACCOUNT for Claude). The Codex providers hold their router arm to it. No default: the mapping is per worker
 * and a wrong one labels a cross-account comparison paired (dev-worker-1 was codex-2 on 2026-11-05, plans/2026-11-05-
 * router-transparency-and-planning-revalidation.md), so a Codex provider refuses to set up without it. */
export const CODEX_ACCOUNT = process.env.RAB_CODEX_ACCOUNT || undefined;

/** A Codex provider's setup: the paired account must be named. */
export function requireCodexAccount(account = CODEX_ACCOUNT) {
  if (!account) throw new Error('RAB_CODEX_ACCOUNT is not set: set it to the router account of the ChatGPT login the gate runs under (dev-worker-1 was codex-2 on 2026-11-05; confirm it for your worker)');
}

/** The router placed a run on another backend or account than the direct arm's: the provider may run it again.
 * `account`: where the run's first request was placed. */
export class Misplaced extends Error {
  /** account: where the run's first request was placed; accounts: every account its requests reached. */
  constructor(message, account, accounts = [account]) { super(message); this.account = account; this.accounts = accounts; }
}

/** Writes the task's prompts on the worker and runs one session. arm: { target, home, model } from the provider. */
export function runCodexTask(ctx, task, arm, runId, env = {}) {
  const k = ctx.remote + '/kit';
  const ws = `${ctx.remote}/ws/${runId}`;
  const p1 = `${ctx.remote}/prompts/${runId}.1`, p2 = `${ctx.remote}/prompts/${runId}.2`;
  const fx = task.fixture ? `cp -r ${ctx.remote}/fixtures/${task.fixture}/. ${ws}/ && git -C ${ws} init -q -b main && git -C ${ws} add -A && git -C ${ws} -c user.name=b -c user.email=b@l commit -q -m base` : 'true';
  const b64 = (s) => Buffer.from(s).toString('base64');
  ctx.ssh(`mkdir -p ${ws} ${ctx.remote}/prompts && ${fx} && echo ${b64(task.prompt)} | base64 -d > ${p1}` + (task.turns ? ` && echo ${b64(task.turns[0])} | base64 -d > ${p2}` : ''));
  const envs = Object.entries({ TAP_PORT: ctx.tapPort, MODEL: arm.model, EFFORT: arm.effort ?? 'medium', CAP: task.cap_min ?? 10, ...env }).map(([a, b]) => `${a}=${b}`).join(' ');
  ctx.ssh(`${envs} bash ${k}/codex-arm.sh ${k} ${runId} ${arm.target} ${arm.home} ${ws} ${p1} ${task.turns ? p2 : ''}`, { timeout: ((task.cap_min ?? 10) * 2 + 2) * 60_000 });
  const sum = ctx.ssh(`python3 ${k}/summarize.py ${k} ${runId}`);
  let m;
  try { m = JSON.parse(sum.stdout); } catch { throw new Error(`no run summary: ${(sum.stderr || '').slice(-160)}`); }
  if (typeof m.wall_s !== 'number') throw new Error('the run summary has no wall time');
  // a seat limit in either arm says nothing about the router (and must not count as a router error)
  if (m.throttled) throw new Error('seat throttled (429): rerun later');
  // a pooled route may place a new conversation on any seat; moving between seats within the run breaks the pairing
  let seatNote = '', moved;
  if (arm.target === 'router') {
    const seats = seatsOf(ctx, m.trajectories ?? []);
    if (arm.paired) {
      // A Codex arm held to an account (core/pairing.mjs). Only the run's FIRST request decides: its placement is made
      // before the model says anything, so discarding on it cannot select on time, cache, errors or work. A run that
      // starts on the account and moves later (a failover, a spill) is router behaviour: kept in every metric, reported.
      if (seats.error || !seats.first) throw new Error(`seat pairing not verified: ${seats.error ?? seats.firstError}`);
      if (seats.first !== arm.account) throw new Misplaced(`first request placed on ${seats.first}, the direct arm is on ${arm.account}`, seats.first, seats.accounts);
      moved = seats.moved ? seats.accounts : undefined;
    } else if (seats.moved) throw new Error(seats.error ? `seat pairing not verified: ${seats.error}` : `the router moved this run between accounts (${seats.accounts.join(', ')}): the pairing is broken`);
    // a route with several backends may place the run on another server than the direct arm's
    else if (arm.account && seats.accounts[0] !== arm.account) throw new Misplaced(`placed on ${seats.accounts[0]}, the direct arm is on ${arm.account}`);
    seatNote = ` [${seats.accounts.join(',')}]`;
  }
  // answers and workspace back to the workstation, for the grader
  const local = ctx.localDir(runId);
  ctx.pull(`tar -C ${k}/runs/${runId} -cf - last.md last.2.md 2>/dev/null; true`, local);
  let workspace = null;
  if (task.fixture) {
    workspace = `${local}/ws`; fs.mkdirSync(workspace, { recursive: true });
    ctx.pull(`tar -C ${ws} -cf - .`, workspace);
  }
  const read = (f) => (fs.existsSync(path.join(local, f)) ? fs.readFileSync(path.join(local, f), 'utf8') : '');
  const g = grade(task, { texts: [read('last.md'), read('last.2.md')], calls: [], workspace, ctx, fixtures: path.join(HERE, 'tasks', 'fixtures') });
  return { pass: g.pass, detail: g.detail + seatNote, wall_s: m.wall_s, tool_calls: m.tool_calls, rejected: m.rejected, ttft_s: m.ttft_s,
    uncached_tokens: m.uncached_tokens, output_tokens: m.output_tokens, requests: m.requests, router_errors: arm.target === 'router' ? m.transport_errors : 0, retries: m.retries ?? 0,
    ...(moved ? { moved } : {}) };
}

/** runCodexTask, run again while the router places it elsewhere than arm.account. Placement is decided before the model
 * says anything, so discarding by placement cannot favour either arm. It costs one session each time. A paired arm
 * (arm.paired) returns the first placements it discarded (`discarded`), and when still misplaced after the last try
 * returns { unpaired } instead of throwing: that pair is INVALID, not the task (core/pairing.mjs), and the gate runs
 * another pair in its place. */
export function runPlaced(ctx, task, arm, runId, env, tries = 8) {
  const discarded = [], seen = new Set();
  for (let attempt = 1; ; attempt++) {
    try {
      const m = runCodexTask(ctx, task, arm, attempt === 1 ? runId : `${runId}-p${attempt}`, env);
      return arm.paired && discarded.length ? { ...m, discarded } : m;
    } catch (e) {
      if (e instanceof Misplaced && arm.paired) { discarded.push(e.account); for (const a of e.accounts) seen.add(a); }
      if (e instanceof Misplaced && attempt === tries && arm.paired) {
        const why = notPaired([...new Set(discarded)], arm.account);
        // `seen`: every account any discarded run reached; the gate stops a task whose account never shows (core/pairing.mjs)
        return { unpaired: why, discarded, seen: [...seen], account: arm.account, pass: false, detail: why, wall_s: null, router_errors: 0, retries: 0 };
      }
      if (!(e instanceof Misplaced) || attempt === tries) throw e;
      ctx.log(`${runId}: ${e.message}: discarded, running it again`);
    }
  }
}

/** Layer (a): capture a Codex request (a 2-turn smoke session through the tap, at the effort the provider's tasks use),
 * then send that body both ways (kit/same_body.py). `target` is the path captured on; with target 'router' the body holds
 * the items the router relayed, and `direct` names the direct path. `run` names the capture: it must be unique in a gate
 * run, because the body is picked from that run's recordings. Returns the parsed same_body result. */
export function sameBody(ctx, { target, home, model, directModel, routerModel, greedy, n = 2, serverUrl, effort = 'medium', run, direct = target }) {
  const k = ctx.remote + '/kit', ws = `${ctx.remote}/ws/${run}`;
  ctx.ssh(`mkdir -p ${ws} ${ctx.remote}/prompts && touch ${ws}/a ${ws}/b ${ws}/c && printf %s 'Run ls -1 in the current directory with your shell tool, then reply with only the number of entries it printed.' > ${ctx.remote}/prompts/la.1`);
  ctx.ssh(`TAP_PORT=${ctx.tapPort} MODEL=${model} EFFORT=${effort} CAP=4 SERVER_URL=${serverUrl ?? ''} bash ${k}/codex-arm.sh ${k} ${run} ${target} ${home} ${ws} ${ctx.remote}/prompts/la.1`, { timeout: 600_000 });
  // the second POST carries the first turn's output (reasoning, tool call) and its result
  const pick = ctx.ssh(`ls ${k}/rec/${run}/*.meta.json | while read f; do python3 -c 'import json,sys;m=json.load(open(sys.argv[1]));print(sys.argv[1]) if m["method"]=="POST" and m["path"].endswith("/responses") else None' "$f"; done | sed -n 2p`).stdout.trim();
  if (!pick) return { error: 'no request captured' };
  const r = ctx.ssh(`SERVER_URL=${serverUrl ?? ''} python3 ${k}/same_body.py ${pick.replace('.meta.json', '.req')} ${pick} ${direct} ${directModel} ${routerModel} ${n} ${greedy ? 1 : 0}`, { timeout: 900_000 });
  try { return JSON.parse(r.stdout); } catch { return { error: (r.stderr || '').slice(-300) }; }
}
