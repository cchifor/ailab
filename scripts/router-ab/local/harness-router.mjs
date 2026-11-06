// local/harness-router.mjs ROOT LOG [MAXSTEPS] - the router arm of the agentic Claude tasks: drives POST
// /v1/chat/completions (model claude/sonnet, the shared tools), executes each tool call with tools.mjs in ROOT, appends
// the results and loops until a reply without tool calls or MAXSTEPS model calls. The bearer comes from ROUTER_TOKEN
// (never printed). One JSON line per model call and per tool call goes to LOG; the final message list goes beside it.
import fs from 'node:fs';
import { SYSTEM, PROMPT, TOOLS, execute } from '../kit/tools.mjs';

const [root, log, maxSteps = '40'] = process.argv.slice(2);
const url = process.env.ROUTER_URL ?? 'https://router.chifor.me/v1/chat/completions';
const H = { authorization: 'Bearer ' + process.env.ROUTER_TOKEN, 'content-type': 'application/json', 'user-agent': 'git/2.43 router-ab-agentic' };
const tools = TOOLS.map((t) => ({ type: 'function', function: { name: t.name, description: t.description, parameters: t.parameters } }));
const messages = [{ role: 'system', content: SYSTEM }, { role: 'user', content: PROMPT }];
const w = (o) => fs.appendFileSync(log, JSON.stringify(o) + '\n');
// the arm's time is measured from here (this container's start-up is a harness cost, not the router's)
const started = Date.now();

for (let step = 1; step <= Number(maxSteps); step++) {
  const t0 = Date.now();
  let r, j = {}, txt = '';
  try {
    r = await fetch(url, { method: 'POST', headers: H, body: JSON.stringify({ model: process.env.CLAUDE_MODEL ?? 'claude/sonnet', messages, tools, stream: false }) });
    txt = await r.text();
    j = JSON.parse(txt);
  } catch (e) {
    w({ kind: 'model', step, status: r?.status ?? 0, error: String(e).slice(0, 200), body: txt.slice(0, 300) });
    break;
  }
  const msg = j.choices?.[0]?.message ?? {};
  const calls = msg.tool_calls ?? [];
  w({ kind: 'model', step, status: r.status, ms: Date.now() - t0, finish: j.choices?.[0]?.finish_reason, usage: j.usage, calls: calls.map((c) => c.function?.name),
      text: (msg.content ?? '').slice(0, 300), trajectory: r.headers.get('x-trajectory-id'), error: r.ok ? null : txt.slice(0, 300) });
  if (!r.ok) break;
  messages.push({ role: 'assistant', content: msg.content ?? '', ...(calls.length ? { tool_calls: calls } : {}) });
  if (!calls.length) break;
  for (const c of calls) {
    const res = execute(root, c.function?.name, c.function?.arguments ?? '{}');
    w({ kind: 'tool', step, tool: c.function?.name, ok: res.ok, out: res.output.slice(0, 120) });
    messages.push({ role: 'tool', tool_call_id: c.id, content: res.output });
  }
}
w({ kind: 'span', ms: Date.now() - started });
fs.writeFileSync(log.replace(/\.jsonl$/, '.messages.json'), JSON.stringify(messages));
