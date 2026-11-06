// providers/claude.mjs - the Claude seat (provider `claude`, transport sdk-resume). Direct: Claude Code (the router's
// validated version) in docker on the worker with the worker's own seat login, flags mirroring the router's launch.
// Via the router: chat tasks from inside the router pod (its own admin bearer) to /v1/chat/completions, model
// claude/<model>; agentic tasks from a harness in docker on the workstation (the admin bearer in its environment only)
// that executes the same workspace tools (kit/tools.mjs) and loops.
// Layer (a): what Anthropic receives, recorded through the tap: the direct `claude -p` against a source-faithful replica
// of the router's launch (kit/claude-replica.mjs). Only the transport's declared differences may appear.
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { HERE } from '../core/context.mjs';
import { CATALOG, CHAT_SYSTEM } from '../tasks/registry.mjs';
import { grade } from '../tasks/graders.mjs';
import { seatsOf } from '../core/seats.mjs';
import { SYSTEM as AGENT_SYSTEM, PROMPT as AGENT_PROMPT } from '../kit/tools.mjs';

// sonnet by default (the gate runs every release; Max seats rate-limit fable sooner); RAB_CLAUDE_MODEL=fable for the
// model the agentic consumers use
const MODEL = process.env.RAB_CLAUDE_MODEL ?? 'sonnet';
const CC_VERSION = '2.1.281';
// the router's name for the seat the worker's own login is on: both arms must run on it
const ACCOUNT = process.env.RAB_CLAUDE_ACCOUNT ?? 'claude-2';
const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64');
const parseStream = (text) => {
  const texts = [], calls = []; let durationMs = 0, msgIds = new Set();
  for (const l of text.split('\n')) {
    let e; try { e = JSON.parse(l); } catch { continue; }
    if (e.type === 'assistant') { msgIds.add(e.message.id); for (const b of e.message.content ?? []) { if (b.type === 'text') texts.push(b.text); if (b.type === 'tool_use') calls.push({ name: b.name, arguments: b.input }); } }
    if (e.type === 'result') { durationMs += e.duration_ms ?? 0; if (e.is_error && e.api_error_status === 429) throw new Error('seat throttled (429): the direct arm cannot run; stop and retry later'); }
  }
  return { text: texts.join(''), calls, durationMs, apiCalls: msgIds.size };
};

function specFor(task, fixtureDir) {
  if (task.kind === 'claude-agentic') return { model: MODEL, system: AGENT_SYSTEM, turns: [AGENT_PROMPT], mode: 'agentic', fixture_dir: fixtureDir, max_turns: task.max_steps ?? 40 };
  return { model: MODEL, system: CHAT_SYSTEM, turns: task.turns, mode: 'chat', tools: task.tools === 'catalog' ? CATALOG : [], max_turns: 1 };
}

export default {
  name: 'claude',
  describe: `Claude seat via Claude Code ${CC_VERSION}, model ${MODEL} (direct: own login; router: claude/${MODEL}, sdk-resume)`,
  kinds: ['chat', 'claude-agentic'],
  async setup(ctx) {
    // the router's validated Claude Code, installed once per worker in a cache (no credential in it)
    const cc = `$HOME/.cache/router-ab/claude-code-${CC_VERSION}`;
    ctx.ssh(`test -x ${cc}/node_modules/@anthropic-ai/claude-code-linux-x64/claude || (mkdir -p ${cc} && docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e npm_config_cache=/tmp/npmc -v ${cc}:/opt/cc node:22-slim npm i --silent --prefix /opt/cc @anthropic-ai/claude-code@${CC_VERSION})`, { timeout: 600_000 });
    ctx.ccDir = cc;
    // the direct arm's time includes `docker run`; measure its fixed start overhead once and take it off
    const r = ctx.ssh('for i in 1 2 3; do s=$(date +%s%N); docker run --rm node:22-slim true; e=$(date +%s%N); echo $(( (e - s) / 1000000 )); done');
    ctx.dockerStart = Math.min(...(r.stdout || '0').trim().split(/\s+/).map(Number)) / 1000;
  },
  async layerA(ctx) {
    const k = ctx.remote + '/kit';
    const spec = { model: MODEL, system: CHAT_SYSTEM, turns: ['How many integers from 1 to 1000 inclusive are divisible by 3 or by 5, but not by 15? Reply with exactly one line: ANSWER=<number>'], mode: 'chat', tools: [] };
    ctx.ssh(`echo ${b64(spec)} | base64 -d > ${ctx.remote}/la-spec.json`);
    ctx.ssh(`TAP_PORT=${ctx.tapPort} CC_DIR=${ctx.ccDir} bash ${k}/claude-direct.sh ${k} la-direct ${ctx.remote}/la-spec.json tap`, { timeout: 300_000 });
    ctx.ssh(`R=${k}/runs/la-replica; mkdir -p $R/home && chmod 700 $R $R/home && export CLAUDE_CODE_OAUTH_TOKEN=$(python3 -c 'import json;print(json.load(open("/home/c4/.claude/.credentials.json"))["claudeAiOauth"]["accessToken"])') && timeout 200 docker run --rm --network host --memory 1g -e CLAUDE_CODE_OAUTH_TOKEN -e ANTHROPIC_BASE_URL=http://127.0.0.1:${ctx.tapPort}/t/anthropic/la-replica -v ${k}:/kit:ro -v ${ctx.ccDir}:/cc:ro -v ${ctx.remote}/la-spec.json:/spec.json:ro -v $R/home:/h -v $R:/out --user "$(id -u):$(id -g)" -w /h node:22-slim node /kit/claude-replica.mjs /spec.json /out/out.jsonl`, { timeout: 300_000 });
    const r = ctx.ssh(`python3 ${k}/claude_diff.py ${k}/rec/la-direct ${k}/rec/la-replica`);
    let d; try { d = JSON.parse(r.stdout); } catch { d = { error: r.stderr.slice(-200) }; }
    if (d.error) return [{ name: 'Anthropic request, direct vs router launch', pass: false, detail: d.error }];
    return [
      { name: 'Anthropic request: no undeclared difference (model, system, thinking, limits, user turns, tool descriptions)', pass: d.undeclared.length === 0, detail: d.undeclared.join('; ') || `equal: ${d.equal.join(', ')}` },
      { name: 'declared differences of the sdk-resume transport (reported, not failed)', pass: true, detail: d.declared.join('; ') || 'none' },
    ];
  },
  async run(ctx, task, arm, runId) {
    const k = ctx.remote + '/kit';
    const fixtures = path.join(HERE, 'tasks', 'fixtures');
    const local = ctx.localDir(runId);
    let texts = [], calls = [], wall = 0, tools = 0, rejected = 0, errors = 0, workspace = null, lastPod = null, agenticTrajectories = [], routerStatuses = [];
    if (arm === 'direct') {
      const spec = specFor(task, task.fixture ? `${ctx.remote}/fixtures/${task.fixture}` : '');
      ctx.ssh(`echo ${b64(spec)} | base64 -d > ${ctx.remote}/${runId}.json && CC_DIR=${ctx.ccDir} bash ${k}/claude-direct.sh ${k} ${runId} ${ctx.remote}/${runId}.json`, { timeout: ((task.cap_min ?? 5) + 2) * 60_000 });
      const tar = spawnSync('ssh', ['-o', 'BatchMode=yes', ctx.worker, `tar -C ${k}/runs/${runId} --exclude=home -cf - .`], { maxBuffer: 1 << 28 });
      spawnSync('tar', ['-C', local, '-xf', '-'], { input: tar.stdout });
      if (!fs.existsSync(`${local}/turn1.jsonl`)) throw new Error('the direct arm produced no output');
      for (let t = 1; fs.existsSync(path.join(local, `turn${t}.jsonl`)); t++) {
        const s = parseStream(fs.readFileSync(path.join(local, `turn${t}.jsonl`), 'utf8'));
        const rs = path.join(local, `turn${t}.jsonl.result_s`);
        texts.push(s.text); calls = s.calls;
        wall += fs.existsSync(rs) ? Math.max(0, Number(fs.readFileSync(rs, 'utf8')) - (ctx.dockerStart ?? 0)) : s.durationMs / 1000;
      }
      if (task.kind === 'claude-agentic') {
        const log = fs.existsSync(path.join(local, 'tools.jsonl')) ? fs.readFileSync(path.join(local, 'tools.jsonl'), 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse) : [];
        tools = log.length; rejected = log.filter((x) => !x.ok).length; workspace = `${local}/ws`;
      }
    } else if (task.kind === 'chat') {
      const r = ctx.inPod(path.join(HERE, 'local', 'claude-chat-pod.mjs'), [b64(specFor(task))]);
      const o = JSON.parse(r.stdout || '{}'); lastPod = o;
      texts = o.texts ?? []; calls = o.calls ?? []; wall = (o.ms ?? 0) / 1000; routerStatuses = o.statuses ?? []; errors = routerStatuses.filter((s) => s !== 200).length;
    } else {
      workspace = `${local}/ws`; fs.cpSync(path.join(fixtures, task.fixture), workspace, { recursive: true });
      spawnSync('git', ['init', '-q', '-b', 'main'], { cwd: workspace }); spawnSync('git', ['add', '-A'], { cwd: workspace });
      spawnSync('git', ['-c', 'user.name=b', '-c', 'user.email=b@l', 'commit', '-q', '-m', 'base'], { cwd: workspace });
      const win = (p) => (process.platform === 'win32' ? spawnSync('cygpath', ['-w', p], { encoding: 'utf8' }).stdout.trim() : p);
      const t0 = Date.now();
      spawnSync('docker', ['run', '--rm', '-e', 'ROUTER_TOKEN', '-e', `CLAUDE_MODEL=claude/${MODEL}`, '-v', `${win(HERE)}:/rab:ro`, '-v', `${win(local)}:/out`, '-v', `${win(workspace)}:/ws`, 'node:22-slim',
        'node', '/rab/local/harness-router.mjs', '/ws', '/out/steps.jsonl', String(task.max_steps ?? 40)],
        { encoding: 'utf8', timeout: (task.cap_min ?? 10) * 60_000, env: { ...process.env, MSYS_NO_PATHCONV: '1', ROUTER_TOKEN: ctx.adminToken() } });
      const steps = fs.existsSync(path.join(local, 'steps.jsonl')) ? fs.readFileSync(path.join(local, 'steps.jsonl'), 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse) : [];
      // the harness's own span (first request to last tool result) leaves out the docker start, as the direct arm does
      const span = steps.find((s) => s.kind === 'span');
      wall = span ? span.ms / 1000 : (Date.now() - t0) / 1000;
      const t = steps.filter((s) => s.kind === 'tool'); tools = t.length; rejected = t.filter((s) => !s.ok).length;
      const client = steps.find((s) => s.kind === 'client-error');
      if (client) throw new Error(`the harness could not reach the router: ${client.error}`);
      routerStatuses = steps.filter((s) => s.kind === 'model').map((s) => s.status);
      errors = routerStatuses.filter((s) => s !== 200).length;
      agenticTrajectories = steps.filter((s) => s.kind === 'model' && s.trajectory).map((s) => s.trajectory);
    }
    let seatNote = '';
    if (arm === 'router') {
      // a seat limit reached through the router says nothing about the router (the direct arm would get it too)
      if (routerStatuses.includes(429)) throw new Error('seat throttled (429) in the router arm: rerun later');
      const ids = task.kind === 'chat' ? (lastPod?.trajectories ?? []) : agenticTrajectories;
      const seats = seatsOf(ctx, ids);
      if (seats.moved) throw new Error(seats.error ? `seat pairing not verified: ${seats.error}` : `the router moved this run between accounts (${seats.accounts.join(', ')}): the pairing is broken`);
      if (seats.accounts[0] !== ACCOUNT) throw new Error(`the router ran this on ${seats.accounts[0]}, the direct arm is on ${ACCOUNT}: not paired`);
      seatNote = ` [${seats.accounts.join(',')}]`;
    }
    const g = grade(task, { texts, calls, workspace, ctx, fixtures });
    return { pass: g.pass, detail: g.detail + seatNote, wall_s: Math.round(wall * 10) / 10, tool_calls: task.kind === 'claude-agentic' ? tools : undefined, rejected: task.kind === 'claude-agentic' ? rejected : undefined, router_errors: arm === 'router' ? errors : 0, retries: 0 };
  },
};
