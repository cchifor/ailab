// Loopback relay for dsh. dsh binds 127.0.0.1 ONLY -- `dsh web` supplies the bind from its own
// Cordis patch (packages/bundle/web-app/cordis.patch.yml) and rejects --host 0.0.0.0 at the argument
// parser, so a Service cannot reach it. This carries the cloudflared-only ingress port to that
// loopback listener, and attaches a dsh session to requests that arrive without a usable one.
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
// forwarded exactly as received; the ONLY one this touches is Cookie.
//
// FAIL-OPEN, BY DESIGN. If the signing secret cannot be read or parsed, this proxies without
// injecting anything and logs loudly. That degrades to exactly the previous behaviour (dsh asks for
// its token) rather than failing shut and taking the UI offline.
const http = require('http');
const fs = require('fs');
const { createHash, createHmac, timingSafeEqual } = require('crypto');

const LISTEN = Number(process.env.RELAY_PORT || 8080);
const TARGET = Number(process.env.DSH_PORT || 3080);
const IDLE_MS = Number(process.env.RELAY_IDLE_MS || 300000); // must outlast SSE/WS idle gaps
const CREDENTIALS = process.env.DSH_CREDENTIALS || '/dsh-home/.credentials.yaml';

// Normalised exactly the way dsh derives it (requestAuthority: `new URL('http://'+host).host`), so
// the comparison below and the cookie NAME agree with what dsh will compute for the same request.
// Comparing the raw Host string instead would silently skip injection for a differing case or a
// stray `:443`, and dsh would answer 401 with nothing to say why.
function normalizeAuthority(host) {
  if (host === undefined || host === '') return undefined;
  try {
    return new URL(`http://${host}`).host;
  } catch {
    return undefined;
  }
}
const AUTHORITY = normalizeAuthority(process.env.DSH_AUTHORITY || '');
const AUTHORITY_HOST = AUTHORITY === undefined ? undefined : new URL(`http://${AUTHORITY}`).hostname;

/**
 * The authority to mint FOR, or undefined to leave this request alone. Matching on hostname and
 * minting for the request's OWN normalised authority mirrors dsh two ways at once: its port-less
 * `trustedHosts` entry matches that hostname on any port, and its cookie name is a hash of the
 * authority THIS request carries. Comparing the whole string instead would mean a stray `:443`
 * produced a cookie under a name dsh never looks for -- injected, ignored, and still 401.
 */
function targetAuthority(host) {
  const authority = normalizeAuthority(host);
  if (authority === undefined || AUTHORITY_HOST === undefined) return undefined;
  return new URL(`http://${authority}`).hostname === AUTHORITY_HOST ? authority : undefined;
}

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
if (AUTHORITY === undefined) {
  console.error('relay: session injection DISABLED -- DSH_AUTHORITY is empty or unparsable.');
} else {
  try {
    secret = readSecret();
    console.log(`relay: session injection ARMED for ${JSON.stringify(AUTHORITY)}`);
  } catch (err) {
    console.error(`relay: session injection DISABLED -- ${err.message}`);
    console.error('relay: proxying without it; dsh will ask the browser for its launch token.');
  }
}

const minted = new Map();
function sessionCookie(authority) {
  const now = Date.now();
  const cached = minted.get(authority);
  if (cached !== undefined && now - cached.at < REMINT_MS) return cached.value;
  const body = Buffer.from(
    JSON.stringify({ version: 1, authority, issuedAt: now, expiresAt: now + SPAN_MS }),
    'utf8',
  ).toString('base64url');
  const signature = createHmac('sha256', secret).update(body).digest().toString('base64url');
  const value = `v1.${body}.${signature}`;
  minted.set(authority, { value, at: now });
  return value;
}

/**
 * Whether a cookie the browser already holds would actually satisfy dsh -- the same checks
 * decodeCookie/isAuthenticated make: v1 shape, HMAC under this secret, and an unexpired window
 * bound to this authority. PRESENCE IS NOT VALIDITY: a browser still carrying the expired or
 * differently-signed cookie from an earlier pod would otherwise suppress injection and go on
 * receiving 401 forever, which is the exact failure this whole change exists to remove.
 */
function isUsableSession(value, authority) {
  const parts = value.split('.');
  if (parts.length !== 3 || parts[0] !== 'v1') return false;
  const expected = createHmac('sha256', secret).update(parts[1]).digest();
  const actual = Buffer.from(parts[2], 'base64url');
  if (actual.byteLength !== expected.byteLength || !timingSafeEqual(actual, expected)) return false;
  let payload;
  try {
    payload = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'));
  } catch {
    return false;
  }
  if (payload === null || typeof payload !== 'object') return false;
  const now = Date.now();
  return payload.version === 1 && payload.authority === authority
    && Number.isSafeInteger(payload.issuedAt) && Number.isSafeInteger(payload.expiresAt)
    && payload.issuedAt <= now && payload.expiresAt > now;
}

/** Split a Cookie header into segments, keeping dsh's own `cookieValue` semantics. */
function splitCookies(headerValue) {
  return headerValue === undefined || headerValue === '' ? [] : headerValue.split(';');
}
const segmentName = (segment) => {
  const at = segment.indexOf('=');
  return at === -1 ? segment.trim() : segment.slice(0, at).trim();
};

/**
 * Give the request a session dsh will accept. Returns a COPY: the incoming headers object is never
 * mutated, so Host and Origin reach dsh exactly as the browser sent them. The only header touched is
 * Cookie. A browser session that is genuinely valid is left alone; one that is absent, expired or
 * unverifiable is REPLACED, so a stale cookie cannot pin the user out. Injection is scoped to the
 * one configured authority -- any other Host is proxied untouched, for dsh's fence to refuse.
 */
function withSession(headers) {
  const out = { ...headers };
  if (secret === null) return out;
  const authority = targetAuthority(out.host);
  if (authority === undefined) return out;
  const name = cookieName(authority);
  const kept = [];
  let usable = false;
  for (const segment of splitCookies(out.cookie)) {
    if (segmentName(segment) !== name) {
      kept.push(segment.trim());
      continue;
    }
    const at = segment.indexOf('=');
    if (at !== -1 && isUsableSession(segment.slice(at + 1).trim(), authority)) {
      usable = true;
      kept.push(segment.trim());
    }
    // An unusable dsh-auth segment is dropped rather than kept, so exactly one is ever sent.
  }
  if (!usable) kept.push(`${name}=${sessionCookie(authority)}`);
  out.cookie = kept.join('; ');
  return out;
}

// Hop-by-hop headers (RFC 9110 s7.6.1) describe ONE connection and must not be relayed onto the
// next. Copying the upstream's `connection` downstream was measurably wrong: a `connection: x-hop`
// reached the client, which then reads x-hop as a keep-alive directive, and a `connection: close`
// would make the browser tear down a socket the relay still considers pooled. `transfer-encoding`
// goes too -- Node has already de-chunked the body and re-frames it on the way out, so replaying
// the upstream's framing can only contradict it.
const HOP_BY_HOP = new Set([
  'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'te', 'trailer', 'trailers', 'transfer-encoding', 'upgrade',
]);
function endToEnd(headers) {
  const out = {};
  for (const [key, value] of Object.entries(headers)) {
    if (!HOP_BY_HOP.has(key.toLowerCase())) out[key] = value;
  }
  return out;
}

/** Serialize a status line + headers for a socket we are writing by hand (the upgrade path). */
function statusLines(res, extra) {
  const lines = [`HTTP/1.1 ${res.statusCode} ${res.statusMessage}`];
  for (const [key, value] of Object.entries(res.headers)) {
    for (const one of Array.isArray(value) ? value : [value]) lines.push(`${key}: ${one}`);
  }
  for (const one of extra) lines.push(one);
  return lines.join('\r\n') + '\r\n\r\n';
}

const server = http.createServer((req, res) => {
  const upstream = http.request(
    { host: '127.0.0.1', port: TARGET, method: req.method, path: req.url, headers: withSession(req.headers) },
    (upstreamRes) => {
      res.writeHead(upstreamRes.statusCode, endToEnd(upstreamRes.headers));
      // pipe with `end: false` so trailers can still be attached: addTrailers() must land before
      // end(), and a plain pipe() calls end() the instant the body finishes. Backpressure is still
      // pipe's. dsh does not currently send trailers, but a proxy that silently ate them would be
      // a trap for whatever does later.
      upstreamRes.pipe(res, { end: false });
      upstreamRes.on('end', () => {
        const trailers = upstreamRes.trailers;
        if (trailers !== undefined && Object.keys(trailers).length > 0) res.addTrailers(trailers);
        res.end();
      });
    },
  );
  upstream.on('error', () => {
    if (!res.headersSent) res.writeHead(502, { 'content-type': 'text/plain; charset=utf-8' });
    res.end('relay: upstream unavailable\n');
  });
  req.pipe(upstream);
});

// WebSocket. /api/remote.mux carries the whole UI, so this has to work as well as the byte pipe did.
server.on('upgrade', (req, clientSocket, head) => {
  // Armed BEFORE the handshake resolves. Everything below used to be installed only inside the
  // 'upgrade' callback, so an upstream that never answered left this descriptor open forever.
  clientSocket.setTimeout(IDLE_MS, () => clientSocket.destroy());
  clientSocket.on('error', () => clientSocket.destroy());

  const upstream = http.request({
    host: '127.0.0.1', port: TARGET, method: req.method, path: req.url, headers: withSession(req.headers),
  });

  // dsh answered the handshake with an ORDINARY response instead of 101 -- the 401/403 its trust
  // fence is designed to return, or any 5xx. http.request emits 'response', not 'upgrade' or
  // 'error', so without this the browser's WebSocket hangs and the descriptor leaks. The body is
  // relayed under `connection: close` with the framing headers dropped: Node has already de-chunked
  // it, so replaying the upstream's own transfer-encoding or content-length would misframe it.
  upstream.on('response', (upstreamRes) => {
    // content-length goes as well as the hop-by-hop set: the body is written straight to the socket
    // and the connection then closed, so length is signalled by EOF and any inherited count could
    // only contradict it.
    upstreamRes.headers = endToEnd(upstreamRes.headers);
    delete upstreamRes.headers['content-length'];
    clientSocket.write(statusLines(upstreamRes, ['connection: close']));
    upstreamRes.on('data', (chunk) => clientSocket.write(chunk));
    upstreamRes.on('end', () => { if (!clientSocket.destroyed) clientSocket.end(); });
    upstreamRes.on('error', () => { if (!clientSocket.destroyed) clientSocket.destroy(); });
  });

  upstream.on('upgrade', (upstreamRes, upstreamSocket, upstreamHead) => {
    clientSocket.write(statusLines(upstreamRes, []));
    if (upstreamHead !== undefined && upstreamHead.length > 0) clientSocket.write(upstreamHead);
    if (head !== undefined && head.length > 0) upstreamSocket.write(head);
    clientSocket.setNoDelay(true);
    upstreamSocket.setNoDelay(true);

    // Teardown, carried over from the byte relay: 'close' means that socket is finished BOTH ways,
    // so destroy the peer rather than end() it. end() here is what leaks -- it shuts the writable
    // side and leaves the readable side open forever on a socket nobody will read again.
    const kill = (s) => () => { if (!s.destroyed) s.destroy(); };
    clientSocket.on('close', kill(upstreamSocket));
    upstreamSocket.on('close', kill(clientSocket));
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

server.on('clientError', (err, socket) => {
  if (!socket.destroyed && socket.writable) socket.end('HTTP/1.1 400 Bad Request\r\nconnection: close\r\n\r\n');
  if (!socket.destroyed) socket.destroy();
});
server.listen(LISTEN, '0.0.0.0', () => console.log(`relay 0.0.0.0:${LISTEN} -> 127.0.0.1:${TARGET}`));
