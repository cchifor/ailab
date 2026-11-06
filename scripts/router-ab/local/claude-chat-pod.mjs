// local/claude-chat-pod.mjs SPEC_BASE64 - the Claude seat's router arm for chat tasks. It runs INSIDE the router pod
// (core/context.mjs inPod) with the pod's own admin bearer, never printed: POST /v1/chat/completions on the local
// listener, model claude/<model>, the spec's system prompt, turns and tools. No tool is executed (chat tasks only score
// the tool choice). Prints one JSON object: texts, the last turn's tool calls, statuses, wall time, trajectory ids.
const spec = JSON.parse(Buffer.from(process.argv[2], 'base64').toString('utf8'));
const H = { authorization: 'Bearer ' + process.env.ROUTER_ADMIN_TOKEN, 'content-type': 'application/json' };
const messages = [{ role: 'system', content: spec.system }];
const out = { texts: [], calls: [], statuses: [], ms: 0, trajectories: [], usage: [] };
for (const turn of spec.turns) {
  messages.push({ role: 'user', content: turn });
  const t0 = Date.now();
  const r = await fetch('http://127.0.0.1:8787/v1/chat/completions', { method: 'POST', headers: H,
    body: JSON.stringify({ model: `claude/${spec.model}`, messages, stream: false, ...(spec.tools?.length ? { tools: spec.tools } : {}) }) });
  out.ms += Date.now() - t0;
  const txt = await r.text(); let j = {}; try { j = JSON.parse(txt); } catch { /* reported by status */ }
  const msg = j.choices?.[0]?.message ?? {};
  out.statuses.push(r.status); out.trajectories.push(r.headers.get('x-trajectory-id')); out.usage.push(j.usage ?? null);
  out.texts.push(msg.content ?? '');
  out.calls = (msg.tool_calls ?? []).map((c) => ({ name: c.function?.name, arguments: c.function?.arguments }));
  if (!r.ok) break;
  messages.push({ role: 'assistant', content: msg.content ?? '' });
}
console.log(JSON.stringify(out));
