#!/usr/bin/env python3
"""cloud-power - power control for the SEPARATE `pve` GPU cluster (cloud1/2/3).

Surfaced as ON/OFF buttons on home.chifor.me. Two operations, both hard-wired:

  wake      -> Wake-on-LAN magic packets to three burned-in MACs
  shutdown  -> Proxmox API `POST /nodes/{node}/status {command: shutdown}`

WHY THIS POD IS hostNetwork
  A magic packet must reach the LAN as an actual layer-2 broadcast. A normal pod cannot do
  that: Cilium will not carry 255.255.255.255 off the node, and Linux does not forward
  directed broadcasts (net.ipv4.conf.all.bc_forwarding defaults to 0), so 192.168.0.255 dies
  on the node too. Unicast WoL is not a way out either - a powered-off host has no ARP entry.
  Host networking is therefore a requirement of the feature, not a convenience.

WHY THIS POD AUTHENTICATES ITSELF
  hostNetwork means the listener is bound to the NODE's LAN address, so 192.168.0.4x:8127 is
  reachable by anything on 192.168.0.0/24 - CI runners and dev-workers included - WITHOUT
  passing through oauth2-proxy/Authelia. The SSO gate in front therefore protects the browser
  path only, and cannot be the sole control. The real boundary is ALLOW_FROM below: the peer
  address must be inside the cluster. A LAN host cannot forge one of those and still complete
  a TCP handshake.

SAFETY - this service can NEVER touch an ai-node
  NODES is a module constant and no request field selects a host: /api/wake and /api/shutdown
  take no target parameter at all. Adding one would be the bug, so don't.
"""
import ipaddress
import json
import os
import secrets
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- the ONLY hosts this service may act on -------------------------------------------------
# MACs are the PERMANENT (ethtool -P) addresses. active-backup bonding stamps the bond's MAC
# onto every slave, and after power-off the bond does not exist, so a packet aimed at the bond
# address is silently ignored. Mirrors cloudlab scripts/wol.py - keep the two in step.
NODES = [
    {"name": "cloud1", "ip": "192.168.0.20", "mac": "00:e2:59:01:a6:62"},
    {"name": "cloud2", "ip": "192.168.0.21", "mac": "00:e2:59:01:a6:52"},
    {"name": "cloud3", "ip": "192.168.0.22", "mac": "34:97:f6:31:a3:95"},
]

PORT = int(os.environ.get("PORT", "8127"))
BASE = os.environ.get("BASE_PATH", "/cloud-power").rstrip("/")
PVE_TOKEN_ID = os.environ.get("PVE_TOKEN_ID", "")
PVE_TOKEN_SECRET = os.environ.get("PVE_TOKEN_SECRET", "")
CONFIRM_TTL = int(os.environ.get("CONFIRM_TTL", "30"))

# Peers allowed to call the API. The pod CIDR covers oauth2-proxy; the node addresses are here
# because Cilium may SNAT pod->node-IP traffic to the node, which would otherwise look external.
ALLOW_FROM = [
    ipaddress.ip_network(c.strip())
    for c in os.environ.get(
        "ALLOW_FROM",
        "10.244.0.0/16,192.168.0.41/32,192.168.0.42/32,192.168.0.43/32,"
        "192.168.0.47/32,192.168.0.48/32,192.168.0.49/32,127.0.0.1/32",
    ).split(",")
    if c.strip()
]

# Broadcast targets. 255.255.255.255 is the important one: it maps to an ff:ff:ff:ff:ff:ff frame
# that every NIC on the segment sees regardless of subnet mask - and the cloud nodes are /23
# while ailab is /24, so a single directed broadcast would NOT cover both.
BROADCASTS = ["255.255.255.255", "192.168.0.255", "192.168.1.255"]
WOL_PORTS = (9, 7)

_confirms = {}
_lock = threading.Lock()


def log(msg):
    print("[cloud-power] " + msg, flush=True)


# --- wake -----------------------------------------------------------------------------------
def magic(mac):
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16


def wake_all():
    out = []
    for n in NODES:
        pkt = magic(n["mac"])
        sent = 0
        for bcast in BROADCASTS:
            for port in WOL_PORTS:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    s.settimeout(2)
                    s.sendto(pkt, (bcast, port))
                    s.close()
                    sent += 1
                except OSError as e:
                    log("wake {} -> {}:{} failed: {}".format(n["name"], bcast, port, e))
        out.append({"node": n["name"], "mac": n["mac"], "packets": sent})
        log("wake {} ({}): {} packets".format(n["name"], n["mac"], sent))
    return out


# --- status ---------------------------------------------------------------------------------
def node_up(ip, timeout=2.0):
    """TCP-connect to the Proxmox web port. Deliberately not ICMP: a raw socket would need
    CAP_NET_RAW, and 'pveproxy answers' is a better readiness signal than 'the kernel replies'."""
    try:
        with socket.create_connection((ip, 8006), timeout=timeout):
            return True
    except OSError:
        return False


def pve(ip, path, method="GET", data=None, timeout=15.0):
    """Call the Proxmox API. TLS verification is off: these nodes carry the PVE self-signed
    cluster certificate, there is no internal CA to pin, and the peer is a hard-coded RFC1918
    address on our own LAN reached with a token that grants only power control."""
    url = "https://{}:8006/api2/json{}".format(ip, path)
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", "PVEAPIToken={}={}".format(PVE_TOKEN_ID, PVE_TOKEN_SECRET))
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
        return json.load(r).get("data")


def guests_running():
    """Running LXCs/VMs across the cluster - what an OFF click would actually stop."""
    found = []
    for n in NODES:
        if not node_up(n["ip"], timeout=1.5):
            continue
        for kind in ("lxc", "qemu"):
            try:
                for g in pve(n["ip"], "/nodes/{}/{}".format(n["name"], kind), timeout=8) or []:
                    if g.get("status") == "running":
                        found.append({"node": n["name"], "type": kind,
                                      "vmid": g.get("vmid"), "name": g.get("name") or ""})
            except Exception as e:
                log("guest list {}/{}: {}".format(n["name"], kind, e))
    return found


def status():
    res = []
    lock = threading.Lock()

    def probe(n):
        u = node_up(n["ip"])
        with lock:
            res.append({"name": n["name"], "ip": n["ip"], "up": u})

    ts = [threading.Thread(target=probe, args=(n,)) for n in NODES]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=5)
    res.sort(key=lambda r: r["name"])
    up = sum(1 for r in res if r["up"])
    state = "on" if up == len(NODES) else ("off" if up == 0 else "partial")
    return {"nodes": res, "up": up, "total": len(NODES), "state": state}


# --- shutdown -------------------------------------------------------------------------------
def shutdown_all():
    """Ask each ONLINE node to shut down. PVE stops its guests via pve-guests.service, and the
    host's own cloud-rtc-wake unit arms the RTC alarm on the way down (see cloudlab), so the
    hardware backstop depends on neither this service nor which path triggered the shutdown."""
    out = []
    for n in NODES:
        if not node_up(n["ip"]):
            out.append({"node": n["name"], "result": "already off"})
            continue
        try:
            pve(n["ip"], "/nodes/{}/status".format(n["name"]),
                method="POST", data={"command": "shutdown"})
            out.append({"node": n["name"], "result": "shutdown requested"})
            log("shutdown requested: " + n["name"])
        except Exception as e:
            out.append({"node": n["name"], "result": "ERROR: {}".format(e)})
            log("shutdown {} FAILED: {}".format(n["name"], e))
    return out


def new_confirm():
    tok = secrets.token_urlsafe(16)
    now = time.time()
    with _lock:
        for k, v in list(_confirms.items()):
            if v < now:
                del _confirms[k]
        _confirms[tok] = now + CONFIRM_TTL
    return tok


def take_confirm(tok):
    with _lock:
        exp = _confirms.pop(tok, None)
    return exp is not None and exp >= time.time()


# --- HTTP -----------------------------------------------------------------------------------
PAGE = """<!doctype html><meta charset=utf-8><title>Cloud GPU power</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 body{background:#0f172a;color:#e2e8f0;font:15px/1.5 system-ui,sans-serif;margin:0;padding:2rem}
 .card{max-width:34rem;margin:0 auto;background:#1e293b;border:1px solid #334155;border-radius:.75rem;padding:1.5rem}
 h1{font-size:1.1rem;margin:0 0 1rem}
 .n{display:flex;gap:.5rem;align-items:center;padding:.35rem 0;font-family:ui-monospace,monospace}
 .dot{width:.65rem;height:.65rem;border-radius:50%;background:#64748b}
 .up{background:#22c55e}.down{background:#ef4444}
 button{font:inherit;font-weight:600;border:0;border-radius:.5rem;padding:.6rem 1.4rem;cursor:pointer;color:#fff}
 .on{background:#16a34a}.off{background:#dc2626}
 button:disabled{opacity:.5;cursor:not-allowed}
 .row{display:flex;gap:.75rem;margin-top:1rem}
 pre{white-space:pre-wrap;background:#0f172a;border-radius:.5rem;padding:.75rem;font-size:.8rem;color:#94a3b8}
</style>
<div class=card>
 <h1>Cloud GPU cluster</h1>
 <div id=nodes></div>
 <div class=row><button class=on id=bon>Turn ON</button><button class=off id=boff>Turn OFF</button></div>
 <pre id=out>loading...</pre>
</div>
<script>
const B=location.pathname.replace(/\\/$/,'');
const out=document.getElementById('out');
async function refresh(){
 try{const r=await fetch(B+'/api/status',{credentials:'same-origin'});const s=await r.json();
  document.getElementById('nodes').innerHTML=s.nodes.map(n=>
   '<div class=n><span class="dot '+(n.up?'up':'down')+'"></span>'+n.name+' <span style=color:#64748b>'+n.ip+'</span></div>').join('');
  out.textContent=s.up+'/'+s.total+' nodes up ('+s.state+')';
 }catch(e){out.textContent='status failed: '+e}
}
document.getElementById('bon').onclick=async()=>{
 out.textContent='sending wake packets...';
 const r=await fetch(B+'/api/wake',{method:'POST',credentials:'same-origin'});
 out.textContent=JSON.stringify(await r.json(),null,1)+'\\n\\nNodes take several minutes to POST.';
};
let pending=null;
document.getElementById('boff').onclick=async()=>{
 const b=document.getElementById('boff');
 if(!pending){
  const r=await fetch(B+'/api/shutdown/preflight',{method:'POST',credentials:'same-origin'});
  const p=await r.json(); pending=p.confirm;
  out.textContent='WILL STOP:\\n'+(p.guests.length?p.guests.map(g=>'  '+g.node+' '+g.type+' '+g.vmid+' '+g.name).join('\\n'):'  (no running guests)')
   +'\\n\\nClick OFF again within '+p.expires_in+'s to confirm.';
  b.textContent='CONFIRM OFF';
  setTimeout(()=>{if(pending){pending=null;b.textContent='Turn OFF';out.textContent='confirmation expired'}},(p.expires_in||30)*1000);
  return;
 }
 const r=await fetch(B+'/api/shutdown',{method:'POST',credentials:'same-origin',
   headers:{'content-type':'application/json'},body:JSON.stringify({confirm:pending})});
 pending=null;b.textContent='Turn OFF';
 out.textContent=JSON.stringify(await r.json(),null,1);
};
refresh();setInterval(refresh,15000);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "cloud-power"

    def log_message(self, fmt, *a):  # quieter default access log
        pass

    def _peer_ok(self):
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return any(ip in net for net in ALLOW_FROM)

    def _send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        p = urllib.parse.urlparse(self.path).path.rstrip("/")
        return p[len(BASE):] if BASE and p.startswith(BASE) else p

    def do_GET(self):
        r = self._route()
        if r in ("/healthz", "/livez"):
            return self._send(200, {"ok": True})
        if not self._peer_ok():
            log("DENIED {} from {}".format(r or "/", self.client_address[0]))
            return self._send(403, {"error": "forbidden: caller outside the cluster"})
        if r == "/api/status":
            return self._send(200, status())
        if r in ("", "/", "/index.html"):
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        r = self._route()
        if not self._peer_ok():
            log("DENIED {} from {}".format(r or "/", self.client_address[0]))
            return self._send(403, {"error": "forbidden: caller outside the cluster"})
        who = self.headers.get("X-Forwarded-Email") or self.headers.get("X-Forwarded-User") or "?"
        if r == "/api/wake":
            log("WAKE by {} from {}".format(who, self.client_address[0]))
            return self._send(200, {"action": "wake", "results": wake_all()})
        if r == "/api/shutdown/preflight":
            g = guests_running()
            return self._send(200, {"guests": g, "confirm": new_confirm(),
                                    "expires_in": CONFIRM_TTL})
        if r == "/api/shutdown":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                req = {}
            if not take_confirm(str(req.get("confirm", ""))):
                # Single-use and time-boxed: a replayed or stale token must not power anything off.
                return self._send(409, {"error": "missing/expired confirmation; run preflight"})
            log("SHUTDOWN by {} from {}".format(who, self.client_address[0]))
            return self._send(200, {"action": "shutdown", "results": shutdown_all()})
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    if not PVE_TOKEN_ID or not PVE_TOKEN_SECRET:
        # Wake still works without it; only shutdown needs the API token. Warn, don't die.
        log("WARNING: PVE token not configured - shutdown will fail, wake is unaffected")
    log("listening on :{} base={} allow={}".format(
        PORT, BASE or "/", ",".join(str(n) for n in ALLOW_FROM)))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
