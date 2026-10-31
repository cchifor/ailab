#!/usr/bin/env node
// bench-router-local.mjs — load, decode, prefill and correctness of the router's local models.
//
// WHY THIS EXISTS. plans/2026-10-31-local-models-improvement-plan.md (P0) needs one frozen harness for
// the before/after comparison of the local models: the same prompts, the same output limits, the same
// metrics, run against the router (the path the Assistant and every API client use) or directly
// against a model server (to qualify a backend before the router may see it).
//
// WHAT IT NEVER DOES. It never reads or sends the router's admin token: router mode takes an API key
// (an api-keys key, role `client`, which /admin/ refuses) from ROUTER_KEY. A server URL in backend mode
// gets no credential at all. It sends only the synthetic prompts below, never production text.
//
// USAGE
//   ROUTER_KEY=... node scripts/bench-router-local.mjs --model local/qwen3.8-27b-fp8 [--model ...]
//       [--router https://router.chifor.me] [--server http://192.168.0.26:8080] [--unload]
//       [--runs 3] [--only decode,prefill,depth,quality,tools,vision,concurrency] [--vision]
//       [--out results.jsonl]
//   node scripts/bench-router-local.mjs --backend http://192.168.0.26:8080 --model qwen3.8-flash-next ...
//
//   --server   the model server behind --model: its llama-swap /running is recorded before and after,
//              and --unload calls its /unload before the cold request (llama-swap only; it evicts
//              whatever is loaded there, so use it in a reserved window).
//   --backend  talk to that server directly instead of the router (qualification of a new backend).
//   --vision   the models take images: run the vision probe. Without it the phase is recorded as
//              skipped (a text-only model would fail it for a reason that is not a regression).
//
// METRICS, and what each one is not:
//   firstGenS      time to the first GENERATED delta of any kind (content, reasoning, tool call). Not
//                  the first byte: the router sends SSE headers and keepalive comments at once, so the
//                  first byte says nothing about the backend. Through the router reasoning is dropped,
//                  so there it is the first content or tool delta.
//   loadS          the cold request's firstGenS minus the same prompt's warm firstGenS (the quality
//                  phase repeats it). An estimate; llama-swap's journal ("Health check passed") is the
//                  authority.
//   firstS         client time to the first usable delta: content or a tool call.
//   servedRoute    the router's x-router-route header, on every record, so a fallback cannot hide.
//   e2e_tok_s      usage.completion_tokens / whole request time (reasoning included, prefill included).
//   decode_tok_s   backend timings when the response carries them (llama.cpp `timings`, backend mode),
//                  else completion_tokens / (total - prompt_tokens / prefill_tok_s of the same model).
//   prefill_tok_s  prompt_tokens / time of a max_tokens 16 request with a unique prompt (no cache hit).
// Every request has its own wall-clock deadline (--deadline, 900 s): a heartbeat- or reasoning-only
// stream cannot hang the run; a deadline hit is a recorded result.
import { appendFileSync } from 'node:fs';
import { deflateSync } from 'node:zlib';

const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : dflt; };
const all = (name) => args.flatMap((a, i) => (a === `--${name}` ? [args[i + 1]] : []));
const flag = (name) => args.includes(`--${name}`);

const BACKEND = opt('backend');
const BASE = (BACKEND ?? opt('router', 'https://router.chifor.me')).replace(/\/+$/, '');
const SERVER = opt('server', BACKEND)?.replace(/\/+$/, '');
const MODELS = all('model');
const RUNS = Number(opt('runs', '3'));
const DEADLINE_MS = Number(opt('deadline', '900')) * 1000;
const OUT = opt('out', `bench-${new Date().toISOString().replace(/[:.]/g, '-')}.jsonl`);
const ONLY = new Set((opt('only', 'decode,prefill,depth,quality,tools,vision,concurrency')).split(','));
const KEY = BACKEND ? undefined : process.env.ROUTER_KEY;
if (!MODELS.length) { console.error('at least one --model'); process.exit(2); }
if (!BACKEND && !KEY) { console.error('router mode needs ROUTER_KEY (an api-keys key; never the admin token)'); process.exit(2); }

const headers = { 'content-type': 'application/json', 'user-agent': 'bench-router-local', ...(KEY ? { authorization: `Bearer ${KEY}` } : {}) };
const record = (o) => { const line = JSON.stringify({ at: new Date().toISOString(), base: BASE, ...o }); appendFileSync(OUT, line + '\n'); console.log(line.length > 400 ? line.slice(0, 400) + '…' : line); };

// ---- prompts (frozen: change them and the before/after comparison is void) -------------------------
const DECODE = 'Describe, step by step and in detail, how a lighthouse Fresnel lens is manufactured, installed and maintained.';
const QUALITY = [
  { id: 'factual', prompt: 'What is the capital of Australia? Answer in one sentence.', expect: /canberra/i },
  { id: 'reasoning', prompt: 'A train leaves at 14:40 and the journey takes 2 hours 35 minutes. At what time does it arrive? Answer with the time only.', expect: /17[:.]15/ },
  { id: 'prose', prompt: 'Explain how a Fresnel lens concentrates light, in about 120 words.', expect: /fresnel|lens|light/i },
];
const filler = (n) => Array.from({ length: n }, (_, i) => `Entry ${i} (${Math.random().toString(36).slice(2, 8)}): the keeper logged the wind, trimmed the wick and polished the lens.`).join('\n');
// 330 entries measured 9,321 prompt tokens on Qwen3.8 (8,965 on Qwen3-Coder) on 2026-10-31; 535 scale to
// ~15k. Every prefill record carries the real prompt_tokens and a sizeWarning when it is >15% off target.
const ENTRIES = { '9k': 330, '15k': 535 }, TARGET = { '9k': 9300, '15k': 15000 };
const LONG = { '15k': filler(ENTRIES['15k']) };
const COUNT_Q = '\n\nHow many entries are listed above? Answer with a number only.';

// A 32x32 solid red PNG, built here so the harness needs no files.
function redPng() {
  const crc = (buf) => { let c, n, t = []; for (n = 0; n < 256; n++) { c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; } let x = 0xffffffff; for (const b of buf) x = t[(x ^ b) & 0xff] ^ (x >>> 8); return (x ^ 0xffffffff) >>> 0; };
  const chunk = (type, data) => { const len = Buffer.alloc(4); len.writeUInt32BE(data.length); const td = Buffer.concat([Buffer.from(type), data]); const c = Buffer.alloc(4); c.writeUInt32BE(crc(td)); return Buffer.concat([len, td, c]); };
  const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(32, 0); ihdr.writeUInt32BE(32, 4); ihdr[8] = 8; ihdr[9] = 2;
  const raw = Buffer.concat(Array.from({ length: 32 }, () => Buffer.concat([Buffer.from([0]), Buffer.from(Array.from({ length: 32 }, () => [220, 20, 20]).flat())])));
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk('IHDR', ihdr), chunk('IDAT', deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]).toString('base64');
}

// A reply that is mostly one repeated word is the HTTP-200 failure that hid cloud3's w4a16 for weeks.
function degenerate(text) {
  const words = text.toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? [];
  if (words.length < 20) return false;
  const counts = new Map(); for (const w of words) counts.set(w, (counts.get(w) ?? 0) + 1);
  return Math.max(...counts.values()) / words.length > 0.3;
}

async function running() {
  if (!SERVER) return undefined;
  try { const j = await (await fetch(`${SERVER}/running`, { signal: AbortSignal.timeout(5000) })).json(); return (j.running ?? []).map((r) => `${r.model}:${r.state}`); } catch { return null; }
}

// One streamed chat completion. Returns timings, usage, content and tool calls.
async function chat(model, messages, { maxTokens, tools } = {}) {
  const t0 = performance.now(), ctl = new AbortController(), timer = setTimeout(() => ctl.abort(new Error('deadline')), DEADLINE_MS);
  const res = { status: 0, firstByteS: null, firstGenS: null, firstS: null, totalS: null, content: '', toolCalls: [], usage: null, timings: null, finish: null, error: null, servedRoute: null };
  try {
    const r = await fetch(`${BASE}/v1/chat/completions`, { method: 'POST', headers, signal: ctl.signal,
      body: JSON.stringify({ model, messages, max_tokens: maxTokens, stream: true, stream_options: { include_usage: true }, ...(tools ? { tools } : {}) }) });
    res.status = r.status; res.servedRoute = r.headers.get('x-router-route');
    if (!r.ok) { res.error = (await r.text()).slice(0, 300); return res; }
    const dec = new TextDecoder(); let buf = '';
    for await (const chunk of r.body) {
      if (res.firstByteS === null) res.firstByteS = (performance.now() - t0) / 1000;
      buf += dec.decode(chunk, { stream: true });
      let i;
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
        if (!line.startsWith('data:')) continue; // SSE comments (keepalives) and blank lines
        const data = line.slice(5).trim(); if (data === '[DONE]') continue;
        let j; try { j = JSON.parse(data); } catch { continue; }
        if (j.error) res.error = JSON.stringify(j.error).slice(0, 300);
        const c = j.choices?.[0];
        if (c?.delta?.content || c?.delta?.reasoning_content || c?.delta?.reasoning || c?.delta?.tool_calls?.length) res.firstGenS ??= (performance.now() - t0) / 1000;
        if (c?.delta?.content) { res.content += c.delta.content; res.firstS ??= (performance.now() - t0) / 1000; }
        for (const tc of c?.delta?.tool_calls ?? []) {
          res.firstS ??= (performance.now() - t0) / 1000;
          const slot = (res.toolCalls[tc.index ?? 0] ??= { id: tc.id, name: '', arguments: '' });
          if (tc.id) slot.id = tc.id; if (tc.function?.name) slot.name += tc.function.name; if (tc.function?.arguments) slot.arguments += tc.function.arguments;
        }
        if (c?.finish_reason) res.finish = c.finish_reason;
        if (j.usage) res.usage = j.usage;
        if (j.timings) res.timings = j.timings;
      }
    }
  } catch (e) { res.error = `${e.name}: ${e.cause?.code ?? e.message}`.slice(0, 300); }
  finally { clearTimeout(timer); res.totalS = (performance.now() - t0) / 1000; }
  return res;
}

const round = (v, d = 1) => (v == null || !Number.isFinite(v) ? null : Number(v.toFixed(d)));
const median = (xs) => { const s = xs.filter((x) => x != null).sort((a, b) => a - b); return s.length ? s[Math.floor((s.length - 1) / 2)] : null; };
const summary = (r) => ({ status: r.status, servedRoute: r.servedRoute, error: r.error, finish: r.finish, firstByteS: round(r.firstByteS, 2), firstGenS: round(r.firstGenS, 2), firstS: round(r.firstS, 2), totalS: round(r.totalS, 2),
  promptTokens: r.usage?.prompt_tokens ?? null, completionTokens: r.usage?.completion_tokens ?? null,
  backendDecode: round(r.timings?.predicted_per_second), backendPrefill: round(r.timings?.prompt_per_second), cacheN: r.timings?.cache_n ?? null,
  degenerate: degenerate(r.content), sample: r.content.slice(0, 120).replace(/\s+/g, ' ') });

async function benchModel(model) {
  record({ model, phase: 'before', running: await running() });
  if (flag('unload') && SERVER) { try { await fetch(`${SERVER}/unload`, { signal: AbortSignal.timeout(130_000) }); } catch { /* recorded by the next state read */ } record({ model, phase: 'unloaded', running: await running() }); }
  const out = { model };

  // Cold: the first request after the state recorded above (process-cold when --unload).
  const cold = await chat(model, [{ role: 'user', content: QUALITY[0].prompt }], { maxTokens: 4096 });
  record({ model, phase: 'cold', ...summary(cold) });

  if (ONLY.has('decode')) {
    const warm = [];
    for (let i = 0; i < RUNS; i++) { const r = await chat(model, [{ role: 'user', content: DECODE }], { maxTokens: 2048 }); warm.push(r); record({ model, phase: `decode${i + 1}`, ...summary(r), e2eTokS: round((r.usage?.completion_tokens ?? 0) / r.totalS) }); }
    out.decodeE2E = median(warm.map((r) => (r.usage?.completion_tokens ?? 0) / r.totalS));
    out.decodeBackend = median(warm.map((r) => r.timings?.predicted_per_second));
  }
  if (ONLY.has('prefill')) {
    for (const size of ['9k', '15k']) {
      const runs = [];
      for (let i = 0; i < RUNS; i++) {
        const r = await chat(model, [{ role: 'user', content: filler(ENTRIES[size]) + COUNT_Q }], { maxTokens: 16 }); runs.push(r);
        const got = r.usage?.prompt_tokens ?? null, off = got ? Math.abs(got - TARGET[size]) / TARGET[size] : null;
        record({ model, phase: `prefill${size}-${i + 1}`, ...summary(r), prefillTokS: round((got ?? 0) / r.totalS), ...(off !== null && off > 0.15 ? { sizeWarning: `prompt_tokens ${got} is ${Math.round(off * 100)}% off the ${TARGET[size]} target` } : {}) });
      }
      out[`prefill${size}`] = median(runs.map((r) => r.timings?.prompt_per_second ?? (r.usage?.prompt_tokens ?? 0) / r.totalS));
    }
  }
  if (ONLY.has('depth')) {
    const r = await chat(model, [{ role: 'user', content: LONG['15k'] + '\n\nSummarise the pattern of the entries above in about 300 words.' }], { maxTokens: 1024 });
    // Without a measured 15k prefill rate the prefill share of the request is unknown: no figure beats a wrong one.
    const pre = out.prefill15k ? (r.usage?.prompt_tokens ?? 0) / out.prefill15k : null;
    out.decodeAt15k = r.timings?.predicted_per_second ?? (pre === null ? null : round((r.usage?.completion_tokens ?? 0) / Math.max(0.001, r.totalS - pre)));
    record({ model, phase: 'depth15k', ...summary(r), decodeAt15k: out.decodeAt15k, ...(out.decodeAt15k === null ? { note: 'no backend timings and no 15k prefill rate in this run: run the prefill phase too' } : {}) });
  }
  if (ONLY.has('quality')) {
    out.quality = [];
    for (const q of QUALITY) {
      const r = await chat(model, [{ role: 'user', content: q.prompt }], { maxTokens: 4096 });
      const pass = r.status === 200 && !r.error && r.finish === 'stop' && r.content.trim().length > 0 && q.expect.test(r.content) && !degenerate(r.content);
      out.quality.push(`${q.id}:${pass ? 'pass' : 'FAIL'}`); record({ model, phase: `quality-${q.id}`, pass, ...summary(r) });
      // The cold request asked the first prompt; this is the same prompt warm.
      if (q === QUALITY[0] && cold.firstGenS != null && r.firstGenS != null) out.loadS = round(cold.firstGenS - r.firstGenS, 1);
    }
  }
  if (ONLY.has('tools')) {
    const tools = [{ type: 'function', function: { name: 'get_tide', description: 'High tide time for a harbour today', parameters: { type: 'object', properties: { harbour: { type: 'string' } }, required: ['harbour'] } } }];
    const ask = [{ role: 'user', content: 'When is high tide in Dover today? Use the tool.' }];
    const r1 = await chat(model, ask, { maxTokens: 4096, tools });
    const call = r1.toolCalls.find((c) => c?.name === 'get_tide');
    let args = null; try { args = JSON.parse(call?.arguments ?? ''); } catch { /* invalid JSON fails below */ }
    let pass = r1.status === 200 && !r1.error && !!call && typeof args?.harbour === 'string', r2 = null;
    if (pass) {
      r2 = await chat(model, [...ask, { role: 'assistant', content: null, tool_calls: [{ id: call.id ?? 'call_0', type: 'function', function: { name: 'get_tide', arguments: call.arguments } }] },
        { role: 'tool', tool_call_id: call.id ?? 'call_0', content: JSON.stringify({ harbour: args.harbour, high_tide: '16:42' }) }], { maxTokens: 4096, tools });
      // The same bar as the quality phase: a clean stop, no stream error, the tool's answer, no degeneration.
      pass = r2.status === 200 && !r2.error && r2.finish === 'stop' && /16[:.]42/.test(r2.content) && !degenerate(r2.content);
    }
    out.tools = pass ? 'pass' : 'FAIL'; record({ model, phase: 'tools', pass, call: call ? { name: call.name, arguments: call.arguments.slice(0, 120) } : null, first: summary(r1), final: r2 ? summary(r2) : null });
  }
  if (ONLY.has('vision') && !flag('vision')) { out.vision = 'skipped'; record({ model, phase: 'vision', skipped: 'no --vision flag: the model is not declared image-capable for this run' }); }
  if (ONLY.has('vision') && flag('vision')) {
    const r = await chat(model, [{ role: 'user', content: [{ type: 'text', text: 'What single colour fills this image? Answer with one word.' }, { type: 'image_url', image_url: { url: `data:image/png;base64,${redPng()}` } }] }], { maxTokens: 4096 });
    out.vision = /red/i.test(r.content) ? 'pass' : 'FAIL'; record({ model, phase: 'vision', pass: out.vision === 'pass', ...summary(r) });
  }
  if (ONLY.has('concurrency')) {
    const both = await Promise.all([0, 1].map(() => chat(model, [{ role: 'user', content: DECODE }], { maxTokens: 512 })));
    out.concurrency = both.every((r) => r.status === 200 && !r.error && !degenerate(r.content) && (r.usage?.completion_tokens ?? 0) > 0) ? 'pass' : 'FAIL';
    record({ model, phase: 'concurrency', pass: out.concurrency === 'pass', runs: both.map(summary) });
  }
  record({ model, phase: 'after', running: await running() });
  record({ model, phase: 'SUMMARY', ...Object.fromEntries(Object.entries(out).map(([k, v]) => [k, typeof v === 'number' ? round(v) : v])) });
}

for (const m of MODELS) await benchModel(m);
console.log(`results: ${OUT}`);
