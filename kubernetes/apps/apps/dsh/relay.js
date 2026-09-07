// Loopback relay. dsh binds 127.0.0.1 ONLY -- `dsh web` supplies the bind from its own Cordis patch
// (packages/bundle/web-app/cordis.patch.yml) and rejects --host 0.0.0.0 at the argument parser, and
// it never consults settings.yaml for it. A Service therefore cannot reach it.
//
// RAW TCP on purpose. dsh's /api trust fence requires Host to be a declared authority and, when
// Origin is present, requires Origin's authority to MATCH Host; it rejects `Origin: null` outright.
// An HTTP-aware proxy invites exactly the mistake of rewriting or manufacturing one of those
// headers -- a byte relay cannot. It also carries the WebSocket upgrade for /api/remote.mux for
// free, because it never parses the stream.
const net = require('net');
const LISTEN = Number(process.env.RELAY_PORT || 8080);
const TARGET = Number(process.env.DSH_PORT || 3080);
const IDLE_MS = Number(process.env.RELAY_IDLE_MS || 300000); // 5 min; must outlast SSE/WS idle gaps

net.createServer({ allowHalfOpen: true }, (client) => {
  const upstream = net.connect({ port: TARGET, host: '127.0.0.1', allowHalfOpen: true });

  // FIN propagation, NOT teardown. Half-close is normal: a client that finishes its request and
  // shuts down its write side still expects the response. Propagate end() so the peer sees EOF.
  client.on('end', () => upstream.end());
  upstream.on('end', () => client.end());

  // Teardown. 'close' means that socket is finished BOTH ways, so nothing more can be delivered to
  // or from it -- destroy the peer rather than end() it. Using end() here is what leaks: it shuts
  // the writable side and leaves the readable side open forever on a socket nobody will ever read,
  // so the descriptor is held until the process exits.
  const kill = (s) => () => { if (!s.destroyed) s.destroy(); };
  client.on('close', kill(upstream));
  upstream.on('close', kill(client));
  client.on('error', kill(upstream));
  upstream.on('error', kill(client));

  // Idle timeout on both ends. Without it a peer that opens a socket and never speaks holds a
  // descriptor indefinitely (slowloris), and an upstream that accepts but never answers wedges the
  // pairing. setTimeout only fires on INACTIVITY, so a long-lived but busy WebSocket is unaffected.
  for (const [a, b] of [[client, upstream], [upstream, client]]) {
    a.setTimeout(IDLE_MS, () => { a.destroy(); if (!b.destroyed) b.destroy(); });
  }

  // pipe() supplies backpressure in both directions; wire it only once upstream is actually up.
  upstream.on('connect', () => { client.pipe(upstream); upstream.pipe(client); });
}).listen(LISTEN, '0.0.0.0', () => console.log(`relay 0.0.0.0:${LISTEN} -> 127.0.0.1:${TARGET}`));
