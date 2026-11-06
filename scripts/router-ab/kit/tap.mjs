// kit/tap.mjs PORT RECDIR - the gate's recording tap (worker side; RECDIR is deleted at teardown).
// Base URL for a client: http://127.0.0.1:PORT/t/<target>/<run-id>/<upstream path prefix...>
//   targets: router -> https://router.chifor.me   vllm -> http://192.168.0.28:18020   llama -> http://192.168.0.26:8080
//            chatgpt -> https://chatgpt.com
//            fwd -> the control arm (providers/codex-control.mjs): POST .../responses goes to the ChatGPT backend with the
//            host login, read from ~/.codex/auth.json per request, in memory only, never refreshed, written or logged.
//            It does what the router's codex adapter does to a native request and nothing else: the same header allow-list,
//            originator, authorization, ChatGPT-Account-Id. The client's own bearer (the router key) is dropped here.
//            Everything else (the model catalogue) goes to the router, so the client sees what it sees in the router arm.
// Forwards method, headers and body unchanged (accept-encoding removed so SSE is readable), streams the response back
// unchanged. Per request it writes RECDIR/<run>/<seq>.req (raw body bytes), <seq>.meta.json (method, path, status,
// timings, request content-encoding, trajectory id, usage) and <seq>.sse (raw response bytes). It NEVER writes header
// values: the Authorization bearer passes through untouched and unrecorded. Recordings are conversation content:
// RECDIR is 0700, files 0600, deleted after analysis.
import http from 'node:http';
import https from 'node:https';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const [port, recDir] = [Number(process.argv[2]), process.argv[3]];
const TARGETS = {
  router: { mod: https, host: 'router.chifor.me', port: 443 },
  vllm: { mod: http, host: '192.168.0.28', port: 18020 },
  llama: { mod: http, host: '192.168.0.26', port: 8080 },
  chatgpt: { mod: https, host: 'chatgpt.com', port: 443 },
  anthropic: { mod: https, host: 'api.anthropic.com', port: 443 },
  fwd: { mod: https, host: 'router.chifor.me', port: 443 },
};
// packages/modules/protocol/index.ts NATIVE_HEADERS and NATIVE_HEADER_PREFIXES
const NATIVE_HEADERS = new Set(['accept', 'originator', 'user-agent', 'version', 'openai-beta', 'session_id', 'session-id', 'conversation_id', 'thread-id', 'x-client-request-id']);
const NATIVE_PREFIXES = ['x-codex-', 'x-openai-internal-codex-'];
function loginHeaders(req) {
  const { access_token: token, account_id: account } = JSON.parse(fs.readFileSync(path.join(os.homedir(), '.codex', 'auth.json'), 'utf8')).tokens;
  const kept = Object.fromEntries(Object.entries(req.headers).filter(([k]) => NATIVE_HEADERS.has(k) || NATIVE_PREFIXES.some((p) => k.startsWith(p))));
  const body = Object.fromEntries(['content-length', 'content-encoding'].filter((k) => req.headers[k]).map((k) => [k, req.headers[k]]));
  return { originator: 'codex_cli_rs', ...kept, ...body, host: 'chatgpt.com', 'content-type': 'application/json', accept: 'text/event-stream', authorization: `Bearer ${token}`, 'ChatGPT-Account-Id': account };
}
fs.mkdirSync(recDir, { recursive: true, mode: 0o700 });
const agents = { http: new http.Agent({ keepAlive: true, maxSockets: 16 }), https: new https.Agent({ keepAlive: true, maxSockets: 16 }) };
const seqs = {};
const w = (f, d) => fs.writeFileSync(f, d, { mode: 0o600 });

const server = http.createServer((req, res) => {
  const tSent = Date.now();
  const m = /^\/t\/([a-z]+)\/([A-Za-z0-9._-]+)(\/.*)$/.exec(req.url ?? '');
  if (!m || !TARGETS[m[1]]) { res.writeHead(404); res.end('bad tap path'); return; }
  const [, tname, run, upath] = m;
  const control = tname === 'fwd' && req.method === 'POST' && upath.endsWith('/responses');
  const t = control ? TARGETS.chatgpt : TARGETS[tname];
  let headers;
  try { headers = control ? loginHeaders(req) : { ...req.headers, host: t.host }; } catch { res.writeHead(502); res.end('no host login'); return; }
  const dir = path.join(recDir, run);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const seq = String((seqs[run] = (seqs[run] ?? 0) + 1)).padStart(4, '0');
  const base = path.join(dir, seq);
  delete headers['accept-encoding'];
  const meta = { run, target: tname, method: req.method, path: upath.replace(/\?.*$/, ''), tSent, reqEncoding: req.headers['content-encoding'] ?? null,
    reqBytes: 0, status: null, tHead: null, tFirstDelta: null, tFirstOutput: null, tCompleted: null, tEnd: null, terminal: null,
    usage: null, trajectory: null, routerHeaders: {}, counts: {}, bytes: 0, error: null };
  const reqChunks = [];
  const sseOut = fs.createWriteStream(base + '.sse', { mode: 0o600 });
  let done = false;
  const finish = (error) => {
    if (done) return; done = true; meta.tEnd = Date.now(); if (error) meta.error = String(error).slice(0, 200);
    sseOut.end(); w(base + '.req', Buffer.concat(reqChunks)); w(base + '.meta.json', JSON.stringify(meta, null, 1));
  };
  const up = t.mod.request({ host: t.host, port: t.port, method: req.method, path: control ? '/backend-api/codex/responses' : upath, headers, agent: agents[t.mod === https ? 'https' : 'http'] }, (ur) => {
    meta.status = ur.statusCode; meta.tHead = Date.now();
    meta.trajectory = ur.headers['x-trajectory-id'] ?? null;
    for (const [k, v] of Object.entries(ur.headers)) if (k.startsWith('x-router-')) meta.routerHeaders[k] = v; // router declarations, no secrets
    res.writeHead(ur.statusCode ?? 502, ur.headers);
    const sse = !String(ur.headers['content-type'] ?? '').includes('json'); // chatgpt.com's SSE content-type varies
    let buf = '';
    ur.on('data', (chunk) => {
      meta.bytes += chunk.length; sseOut.write(chunk); res.write(chunk);
      if (!sse) return;
      buf += chunk.toString('utf8');
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, i); buf = buf.slice(i + 2);
        const ev = /^event: (.+)$/m.exec(block)?.[1] ?? (/^data: /m.test(block) ? 'data' : null);
        if (!ev) continue;
        const now = Date.now();
        meta.counts[ev] = (meta.counts[ev] ?? 0) + 1;
        if (ev.endsWith('.delta') && meta.tFirstDelta === null) meta.tFirstDelta = now;
        if ((ev === 'response.output_text.delta' || ev === 'response.function_call_arguments.delta') && meta.tFirstOutput === null) meta.tFirstOutput = now;
        if (ev === 'response.completed' || ev === 'response.incomplete' || ev === 'response.failed' || ev === 'error') {
          meta.tCompleted = now; meta.terminal = ev;
          try {
            const data = JSON.parse(/^data: (.+)$/m.exec(block)?.[1] ?? '{}');
            const u = data.response?.usage;
            if (u) meta.usage = { input: u.input_tokens, cached: u.input_tokens_details?.cached_tokens ?? null, output: u.output_tokens,
              reasoning: u.output_tokens_details?.reasoning_tokens ?? null, total: u.total_tokens };
          } catch (e) { meta.error = 'usage parse: ' + e.message; }
        }
      }
    });
    ur.on('end', () => { res.end(); finish(); });
    ur.on('error', (e) => { res.destroy(e); finish(e); });
  });
  up.on('error', (e) => { if (!res.headersSent) { res.writeHead(502); res.end(); } else res.destroy(e); finish(e); });
  req.on('data', (c) => { meta.reqBytes += c.length; reqChunks.push(c); });
  req.pipe(up);
  res.on('close', () => { if (!done) { up.destroy(); finish('client closed'); } });
});
server.on('upgrade', (req, socket) => { fs.appendFileSync(path.join(recDir, 'upgrades.log'), `${new Date().toISOString()} ${String(req.url).split('?')[0]}\n`); socket.end('HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n'); });
server.keepAliveTimeout = 120_000; server.requestTimeout = 0; server.headersTimeout = 0;
server.listen(port, '127.0.0.1', () => console.log(`abtap on 127.0.0.1:${port}, recordings in ${recDir}`));
