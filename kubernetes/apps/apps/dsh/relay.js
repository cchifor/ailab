// Loopback relay. dsh binds 127.0.0.1 ONLY -- the shipped `dsh web` command supplies the bind from
// its own Cordis patch (packages/bundle/web-app/cordis.patch.yml) and rejects --host 0.0.0.0 at the
// argument parser, and it never consults settings.yaml for it. A Service therefore cannot reach it.
//
// This is a RAW TCP relay on purpose. dsh's /api trust fence requires Host to be a declared
// authority and, when Origin is present, requires Origin's authority to MATCH Host -- and it
// rejects `Origin: null` outright. An HTTP-aware proxy invites exactly the mistake of rewriting or
// manufacturing one of those headers; a byte relay cannot. It also carries the WebSocket upgrade for
// /api/remote.mux for free, since it never parses the stream.
const net = require('net');
const LISTEN = Number(process.env.RELAY_PORT || 8080);
const TARGET = Number(process.env.DSH_PORT || 3080);

net.createServer((client) => {
  const upstream = net.connect(TARGET, '127.0.0.1');
  const bin = (a, b) => { a.on('error', () => b.destroy()); a.on('close', () => b.end()); };
  bin(client, upstream); bin(upstream, client);
  upstream.on('connect', () => { client.pipe(upstream); upstream.pipe(client); });
}).listen(LISTEN, '0.0.0.0', () => console.log(`relay 0.0.0.0:${LISTEN} -> 127.0.0.1:${TARGET}`));
