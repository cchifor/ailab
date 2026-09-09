// Loopback relay for dsh. dsh binds 127.0.0.1 ONLY -- `dsh web` supplies the bind from its own
// Cordis patch (packages/bundle/web-app/cordis.patch.yml) and rejects --host 0.0.0.0 at the argument
// parser, so a Service cannot reach it. This carries the cloudflared-only ingress port to that
// loopback listener, and attaches a dsh session to requests that arrive without one.
//
// WHY THIS PARSES HTTP AT ALL (it was a byte pipe until 2026-09-09).
// dsh gates the browser with a SECOND login of its own, on top of Cloudflare Access, and two
// properties of that login made it unusable for a remotely-reached deployment:
//
//   1. The cookie is minted only by visiting /?token=<launch token>, and processLaunchToken
//      (@deepseek-ai/dsh-client-connection) is crypto.randomBytes cached in a per-process WeakMap
//      with no env or config override. It is therefore NEW on every process start -- and this pod
//      rolls whenever any file in the dsh configMapGenerator changes, which during active work on
//      dsh has been most days. Every roll silently invalidated the URL in the browser's bookmark.
//   2. That cookie is `SameSite=Strict`, hardcoded in the same package's sessionCookie() with no
//      configuration knob. When an expired Access session sends the browser to
//      <team>.cloudflareaccess.com and Cloudflare redirects back, the return trip is a TOP-LEVEL
//      CROSS-SITE navigation, and a Strict cookie is withheld on exactly that. Confirmed on the
//      wire from the failing request: it arrives with `sec-fetch-site: cross-site`, carries
//      CF_Authorization and CF_AppSession, and carries NO dsh-auth-* cookie. dsh answers 401
//      "dsh web authentication required".
//
// Together those meant dsh was unreachable every morning from a browser that had been working the
// day before, and unreachable outright from any device that had never held the cookie.
//
// WHY ATTACHING A SESSION HERE IS SOUND. Cloudflare Access is the ONLY per-person gate in front of
// dsh -- kubernetes/infra/cloudflare/access.tf says so in as many words, because dsh itself cannot
// tell one person from another -- and the NetworkPolicy admits this port from the cloudflared pod
// and nothing else. Every request that reaches this listener has therefore already been
// authenticated by Access. dsh's own cookie is re-proving something already proven, so this relay
// supplies it rather than making a human do it.
//
// WHAT THIS DELIBERATELY DOES NOT DO. It never rewrites, synthesises or drops Host or Origin. dsh's
// /api trust fence requires Host to be a declared authority and, when Origin is present, requires
// Origin's authority to MATCH Host, and it rejects `Origin: null` outright. Every header is
// forwarded exactly as received; the ONLY one this touches is Cookie, and only to APPEND a cookie
// that is not already there. A request that already carries a dsh-auth cookie is passed through
// untouched, so a browser that has its own valid session keeps using it.
//
// FAIL-OPEN, BY DESIGN. If the signing secret cannot be read or parsed, this proxies without
// injecting anything and logs loudly. That degrades to exactly the previous behaviour (dsh asks for
// its token) rather than failing shut and taking the UI offline.
const http = require('http');
const fs = require('fs');
const { createHash, createHmac } = require('crypto');

const LISTEN = Number(process.env.RELAY_PORT || 8080);
const TARGET = Number(process.env.DSH_PORT || 3080);
const IDLE_MS = Number(process.env.RELAY_IDLE_MS || 300000); // must outlast SSE/WS idle gaps
const AUTHORITY = process.env.DSH_AUTHORITY || '';
const CREDENTIALS = process.env.DSH_CREDENTIALS || '/dsh-home/.credentials.yaml';

// The asserted cookie lifetime. dsh re-checks `expiresAt - issuedAt <= cookieMaxAgeDays` on EVERY
// request, so a long-lived value would start failing the moment that setting were lowered or its
// Cordis patch failed to apply. A short span re-minted continuously can never trip that check,
// whatever cookieMaxAgeDays happens to be, and costs one HMAC every REMINT_MS.
const SPAN_MS = 12 * 60 * 60 * 1000;
const REMINT_MS = 5 * 60 * 1000;

// Mirrors encodeBase64Url/cookieName/encodeCookie in @deepseek-ai/dsh-client-connection. Node's
// 'base64url' produces the same unpadded -/_ alphabet that package builds by hand. The cookie NAME
// was verified against a live mint before this was written: base64url(sha256("dsh.chifor.me"))
// reproduces the observed dsh-auth-s9ueMP9M_... exactly.
const cookieName = (authority) =>
  'dsh-auth-' + createHash('sha256').update(authority).digest().toString('base64url');

function readSecret() {
  // The store is `records: { <key>: { kind: grant, payload: { version: 1, secret: <base64url> } } }`
  // and browser-session auth is the only grant dsh keeps here. Taking the first `secret:` is
  // therefore right today and FAILS LOUDLY rather than silently if that ever stops being true: a
  // wrong key yields a bad HMAC, dsh rejects the cookie, and the 401 is immediately visible.
  const text = fs.readFileSync(CREDENTIALS, 'utf8');
  const match = /^\s+secret:\s*(\S+)\s*$/m.exec(text);
  if (match === null) throw new Error(`no secret found in ${CREDENTIALS}`);
  const secret = Buffer.from(match[1], 'base64url');
  if (secret.byteLength !== 32) throw new Error(`secret is ${secret.byteLength} bytes, expected 32`);
  return secret;
}

let secret = null;
try {
  secret = readSecret();
  console.log(`relay: session injection ARMED for ${JSON.stringify(AUTHORITY)}`);
} catch (err) {
  console.error(`relay: session injection DISABLED -- ${err.message}`);
  console.error('relay: proxying without it; dsh will ask the browser for its launch token.');
}
if (secret !== null && AUTHORITY === '') {
  secret = null;
  console.error('relay: session injection DISABLED -- DSH_AUTHORITY is empty.');
}

let minted = { value: '', at: 0 };
function sessionCookie() {
  const now = Date.now();
  if (minted.value !== '' && now - minted.at < REMINT_MS) return minted.value;
  const body = Buffer.from(
    JSON.stringify({ version: 1, authority: AUTHORITY, issuedAt: now, expiresAt: now + SPAN_MS }),
    'utf8',
  ).toString('base64url');
  const signature = createHmac('sha256', secret).update(body).digest().toString('base64url');
  minted = { value: `v1.${body}.${signature}`, at: now };
  return minted.value;
}

/** Read one cookie by exact name, with dsh's own segment semantics (cookieValue in that package). */
function hasCookie(headerValue, name) {
  if (headerValue === undefined) return false;
  for (const segment of headerValue.split(';')) {
    const at = segment.indexOf('=');
    if (at !== -1 && segment.slice(0, at).trim() === name) return true;
  }
  return false;
}

/**
 * Append the session cookie when, and only when, this request lacks one. Returns a COPY: the
 * incoming headers object is never mutated, so Host and Origin reach dsh exactly as the browser
 * sent them. Injection is scoped to the one configured authority -- a request arriving under any
 * other Host is proxied untouched and left to dsh's own trust fence to refuse.
 */
function withSession(headers) {
  const out = { ...headers };
  if (secret === null || out.host !== AUTHORITY) return out;
  const name = cookieName(AUTHORITY);
  if (hasCookie(out.cookie, name)) return out;
  const pair = `${name}=${sessionCookie()}`;
  out.cookie = out.cookie === undefined || out.cookie === '' ? pair : `${out.cookie}; ${pair}`;
  return out;
}

const server = http.createServer((req, res) => {
  const upstream = http.request(
    { host: '127.0.0.1', port: TARGET, method: req.method, path: req.url, headers: withSession(req.headers) },
    (upstreamRes) => {
      res.writeHead(upstreamRes.statusCode, upstreamRes.headers);
      upstreamRes.pipe(res);
    },
  );
  upstream.on('error', () => {
    if (!res.headersSent) res.writeHead(502, { 'content-type': 'text/plain; charset=utf-8' });
    res.end('relay: upstream unavailable\n');
  });
  req.pipe(upstream);
});

// WebSocket. /api/remote.mux carries the whole UI, so this has to work as well as the byte pipe did.
// The 101 is replayed verbatim and both sockets are then piped raw -- past the handshake this is the
// same dumb relay it always was.
server.on('upgrade', (req, clientSocket, head) => {
  const upstream = http.request({
    host: '127.0.0.1', port: TARGET, method: req.method, path: req.url, headers: withSession(req.headers),
  });
  upstream.on('upgrade', (upstreamRes, upstreamSocket, upstreamHead) => {
    const lines = [`HTTP/1.1 ${upstreamRes.statusCode} ${upstreamRes.statusMessage}`];
    for (const [key, value] of Object.entries(upstreamRes.headers)) {
      for (const one of Array.isArray(value) ? value : [value]) lines.push(`${key}: ${one}`);
    }
    clientSocket.write(lines.join('\r\n') + '\r\n\r\n');
    if (upstreamHead !== undefined && upstreamHead.length > 0) clientSocket.write(upstreamHead);
    if (head !== undefined && head.length > 0) upstreamSocket.write(head);

    // Teardown, carried over from the byte relay: 'close' means that socket is finished BOTH ways,
    // so destroy the peer rather than end() it. end() here is what leaks -- it shuts the writable
    // side and leaves the readable side open forever on a socket nobody will read again.
    const kill = (s) => () => { if (!s.destroyed) s.destroy(); };
    clientSocket.on('close', kill(upstreamSocket));
    upstreamSocket.on('close', kill(clientSocket));
    clientSocket.on('error', kill(upstreamSocket));
    upstreamSocket.on('error', kill(clientSocket));
    for (const [a, b] of [[clientSocket, upstreamSocket], [upstreamSocket, clientSocket]]) {
      a.setTimeout(IDLE_MS, () => { a.destroy(); if (!b.destroyed) b.destroy(); });
    }
    clientSocket.pipe(upstreamSocket);
    upstreamSocket.pipe(clientSocket);
  });
  upstream.on('error', () => { if (!clientSocket.destroyed) clientSocket.destroy(); });
  upstream.end();
});

server.on('clientError', (err, socket) => { if (!socket.destroyed) socket.destroy(); });
server.listen(LISTEN, '0.0.0.0', () => console.log(`relay 0.0.0.0:${LISTEN} -> 127.0.0.1:${TARGET}`));
