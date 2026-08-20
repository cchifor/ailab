#!/usr/bin/env python3
"""cloud-power - power control for the SEPARATE `pve` GPU cluster (cloud1/2/3).

Surfaced as ON/OFF buttons on home.chifor.me. One file, TWO roles selected by MODE:

  MODE=wol   hostNetwork. Sends Wake-on-LAN magic packets. Nothing else. No PVE token.
  MODE=api   normal pod networking. Status + shutdown via the Proxmox API; forwards wake to the
             wol service. This is the half that can turn machines OFF.

WHY THE SPLIT
  A magic packet must reach the LAN as a real layer-2 broadcast. A normal pod cannot do that:
  Cilium will not carry 255.255.255.255 off the node, Linux does not forward directed broadcasts,
  and unicast WoL is impossible because a powered-off host has no ARP entry. So the SENDER must be
  hostNetwork - and a hostNetwork listener is bound to the node's LAN address, reachable from
  192.168.0.0/24 without passing through oauth2-proxy.

  Rather than try to authenticate a LAN-exposed port, only the harmless half is exposed there.
  The worst an unauthenticated LAN or in-cluster caller can do against MODE=wol is turn the
  cluster ON. Everything destructive lives in MODE=api, which has ordinary pod networking and is
  fenced by a NetworkPolicy admitting only oauth2-proxy.

SAFETY - this service can NEVER touch an ai-node
  NODES is a module constant and no request field selects a host: the endpoints take no target
  parameter at all. Adding one would be the bug, so don't.
"""
import hashlib
import http.client
import ipaddress
import json
import os
import secrets
import socket
import ssl
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- the ONLY hosts this service may act on -------------------------------------------------
# MACs are the PERMANENT (ethtool -P) addresses. active-backup bonding stamps the bond's MAC onto
# every slave, and after power-off the bond does not exist, so a packet aimed at the bond address
# is silently ignored. Mirrors cloudlab scripts/wol.py - keep the two in step.
NODES = [
    {"name": "cloud1", "ip": "192.168.0.20", "mac": "00:e2:59:01:a6:62"},
    {"name": "cloud2", "ip": "192.168.0.21", "mac": "00:e2:59:01:a6:52"},
    {"name": "cloud3", "ip": "192.168.0.22", "mac": "34:97:f6:31:a3:95"},
]

MODE = os.environ.get("MODE", "api").strip().lower()
PORT = int(os.environ.get("PORT", "8127"))
BASE = os.environ.get("BASE_PATH", "/cloud-power").rstrip("/")
PVE_TOKEN_ID = os.environ.get("PVE_TOKEN_ID", "")
PVE_TOKEN_SECRET = os.environ.get("PVE_TOKEN_SECRET", "")
CONFIRM_TTL = int(os.environ.get("CONFIRM_TTL", "30"))
WOL_URL = os.environ.get("WOL_URL", "http://cloud-power-wol.cloud-power.svc.cluster.local:8127")

# Pinned Proxmox cert fingerprints, "node=AA:BB:...,node2=...". These hosts carry the PVE
# self-signed cluster certificate and there is no internal CA to validate against, so without
# pinning a LAN host could ARP-spoof 192.168.0.2x, present any certificate, and harvest the
# Authorization header. Pinning FAILS CLOSED: an unpinned or mismatched node is never sent the
# token. Re-pin after `pvecm updatecerts` or any cert regeneration - the value is exactly what
# `GET /api2/json/nodes` reports as ssl_fingerprint.
PVE_FINGERPRINTS = {}
for _e in os.environ.get("PVE_FINGERPRINTS", "").split(","):
    if "=" in _e:
        _k, _v = _e.split("=", 1)
        PVE_FINGERPRINTS[_k.strip()] = _v.strip().upper()

# Peers allowed to call. For MODE=api this is belt-and-braces behind the NetworkPolicy; for
# MODE=wol it is the only filter, which is acceptable because that role can only turn things ON.
ALLOW_FROM = [
    ipaddress.ip_network(c.strip())
    for c in os.environ.get(
        "ALLOW_FROM",
        "10.244.0.0/16,192.168.0.41/32,192.168.0.42/32,192.168.0.43/32,"
        "192.168.0.47/32,192.168.0.48/32,192.168.0.49/32,127.0.0.1/32",
    ).split(",")
    if c.strip()
]

# 255.255.255.255 is the important one: it maps to an ff:ff:ff:ff:ff:ff frame that every NIC on the
# segment sees regardless of subnet mask - and the cloud nodes are /23 while ailab is /24, so a
# single directed broadcast would NOT cover both.
BROADCASTS = ["255.255.255.255", "192.168.0.255", "192.168.1.255"]
WOL_PORTS = (9, 7)

_confirms = {}
_lock = threading.Lock()


def log(msg):
    print("[cloud-power/%s] %s" % (MODE, msg), flush=True)


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
                    log("wake %s -> %s:%s failed: %s" % (n["name"], bcast, port, e))
        out.append({"node": n["name"], "mac": n["mac"], "packets": sent})
        log("wake %s (%s): %d packets" % (n["name"], n["mac"], sent))
    return out


def forward_wake():
    """MODE=api has no L2 broadcast path; hand the job to the hostNetwork sender."""
    url = urllib.parse.urlparse(WOL_URL + BASE + "/api/wake")
    conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=20)
    try:
        conn.request("POST", url.path, headers={"Content-Length": "0"})
        r = conn.getresponse()
        return json.loads(r.read() or b"{}")
    finally:
        conn.close()


# --- Proxmox API ----------------------------------------------------------------------------
def node_up(ip, timeout=2.0):
    """TCP-connect to the Proxmox web port. Deliberately not ICMP: a raw socket would need
    CAP_NET_RAW, and 'pveproxy answers' is a better readiness signal than 'the kernel replies'."""
    try:
        with socket.create_connection((ip, 8006), timeout=timeout):
            return True
    except OSError:
        return False


class PinError(Exception):
    pass


def pve(node, path, method="GET", data=None, timeout=15.0):
    """Call the Proxmox API with certificate pinning. The token is written to the socket only
    AFTER the presented certificate matches the pin, so a spoofed host never receives it."""
    name, ip = node["name"], node["ip"]
    expected = PVE_FINGERPRINTS.get(name)
    if not expected:
        raise PinError("no pinned fingerprint for %s; refusing to send the PVE token" % name)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False           # cert CN is the node name, we connect by IP
    ctx.verify_mode = ssl.CERT_NONE      # self-signed cluster CA; the pin below is the real check
    conn = http.client.HTTPSConnection(ip, 8006, context=ctx, timeout=timeout)
    try:
        conn.connect()
        der = conn.sock.getpeercert(binary_form=True)
        got = ":".join("%02X" % b for b in hashlib.sha256(der).digest())
        if got != expected:
            raise PinError("cert fingerprint mismatch for %s: got %s expected %s"
                           % (name, got, expected))
        body = urllib.parse.urlencode(data).encode() if data else None
        headers = {"Authorization": "PVEAPIToken=%s=%s" % (PVE_TOKEN_ID, PVE_TOKEN_SECRET)}
        if body:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, "/api2/json" + path, body=body, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        if r.status >= 400:
            raise RuntimeError("PVE %s %s -> HTTP %d" % (method, path, r.status))
        return json.loads(raw or b"{}").get("data")
    finally:
        conn.close()


def guests_running():
    """Running LXCs/VMs - what an OFF click would actually stop.

    Returns (guests, errors). Enumeration failures are REPORTED, never swallowed: a caller that
    treats an incomplete list as 'nothing is running' would shut the cluster down on a false
    all-clear, so preflight refuses to mint a token when errors is non-empty."""
    found, errors = [], []
    for n in NODES:
        if not node_up(n["ip"], timeout=1.5):
            continue
        for kind in ("lxc", "qemu"):
            try:
                for g in pve(n, "/nodes/%s/%s" % (n["name"], kind), timeout=8) or []:
                    if g.get("status") == "running":
                        found.append({"node": n["name"], "type": kind,
                                      "vmid": g.get("vmid"), "name": g.get("name") or ""})
            except Exception as e:
                errors.append("%s/%s: %s" % (n["name"], kind, e))
                log("guest list %s/%s FAILED: %s" % (n["name"], kind, e))
    return found, errors


def guest_sig(guests):
    """Order-independent signature of the running set, so the confirmation can be checked against
    the state the operator was actually shown."""
    return sorted("%s/%s/%s" % (g["node"], g["type"], g["vmid"]) for g in guests)


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
            pve(n, "/nodes/%s/status" % n["name"], method="POST", data={"command": "shutdown"})
            out.append({"node": n["name"], "result": "shutdown requested"})
            log("shutdown requested: " + n["name"])
        except Exception as e:
            out.append({"node": n["name"], "result": "ERROR: %s" % e})
            log("shutdown %s FAILED: %s" % (n["name"], e))
    return out


def new_confirm(sig):
    tok = secrets.token_urlsafe(16)
    now = time.time()
    with _lock:
        for k, v in list(_confirms.items()):
            if v[0] < now:
                del _confirms[k]
        _confirms[tok] = (now + CONFIRM_TTL, sig)
    return tok


def take_confirm(tok):
    """Single-use: pop under the lock, so two concurrent confirms cannot both succeed."""
    with _lock:
        rec = _confirms.pop(tok, None)
    if rec is None or rec[0] < time.time():
        return None
    return rec[1]


# --- HTTP -----------------------------------------------------------------------------------
PAGE = """<!doctype html><meta charset=utf-8><title>Cloud GPU power</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 body{background:#0f172a;color:#e2e8f0;font:15px/1.5 system-ui,sans-serif;margin:0;padding:2rem}
 .card{max-width:34rem;margin:0 auto;background:#1e293b;border:1px solid #334155;border-radius:.75rem;padding:1.5rem}
 h1{font-size:1.1rem;margin:0 0 1rem}
 .n{display:flex;gap:.5rem;align-items:center;padding:.35rem 0;font-family:ui-monospace,monospace}
 .dot{width:.65rem;height:.65rem;border-radius:50%;background:#64748b}
 button{font:inherit;font-weight:600;border:0;border-radius:.5rem;padding:.6rem 1.4rem;cursor:pointer;color:#fff}
 .row{display:flex;gap:.75rem;margin-top:1rem}
 pre{white-space:pre-wrap;background:#0f172a;border-radius:.5rem;padding:.75rem;font-size:.8rem;color:#94a3b8}
</style>
<div class=card>
 <h1>Cloud GPU cluster</h1><div id=nodes></div>
 <div class=row><button style=background:#16a34a id=bon>Turn ON</button>
 <button style=background:#dc2626 id=boff>Turn OFF</button></div>
 <pre id=out>loading...</pre>
</div>
<script>
const B=location.pathname.replace(/\\/$/,''),out=document.getElementById('out');
async function refresh(){try{const s=await(await fetch(B+'/api/status',{credentials:'same-origin'})).json();
 document.getElementById('nodes').innerHTML=s.nodes.map(n=>'<div class=n><span class=dot style=background:'+
 (n.up?'#22c55e':'#64748b')+'></span>'+n.name+' <span style=color:#64748b>'+n.ip+'</span></div>').join('');
 out.textContent=s.up+'/'+s.total+' nodes up ('+s.state+')';}catch(e){out.textContent='status failed: '+e}}
document.getElementById('bon').onclick=async()=>{out.textContent='sending wake packets...';
 const r=await fetch(B+'/api/wake',{method:'POST',credentials:'same-origin'});
 out.textContent=JSON.stringify(await r.json(),null,1)+'\\n\\nNodes take several minutes to POST.';};
let pending=null;
document.getElementById('boff').onclick=async()=>{const b=document.getElementById('boff');
 if(!pending){const r=await fetch(B+'/api/shutdown/preflight',{method:'POST',credentials:'same-origin'});
  const p=await r.json(); if(!r.ok){out.textContent='preflight refused:\\n'+JSON.stringify(p,null,1);return;}
  pending=p.confirm; out.textContent='WILL STOP:\\n'+(p.guests.length?p.guests.map(g=>'  '+g.node+' '+g.type+' '+g.vmid+' '+g.name).join('\\n'):'  (no running guests)')
   +'\\n\\nClick OFF again within '+p.expires_in+'s to confirm.'; b.textContent='CONFIRM OFF';
  setTimeout(()=>{if(pending){pending=null;b.textContent='Turn OFF';out.textContent='confirmation expired'}},(p.expires_in||30)*1000);
  return;}
 const tok=pending; pending=null; b.textContent='Turn OFF';
 const r=await fetch(B+'/api/shutdown',{method:'POST',credentials:'same-origin',
  headers:{'content-type':'application/json'},body:JSON.stringify({confirm:tok})});
 out.textContent=JSON.stringify(await r.json(),null,1);};
refresh();setInterval(refresh,15000);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "cloud-power"

    def log_message(self, fmt, *a):
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
            return self._send(200, {"ok": True, "mode": MODE})
        if not self._peer_ok():
            log("DENIED %s from %s" % (r or "/", self.client_address[0]))
            return self._send(403, {"error": "forbidden: caller outside the cluster"})
        if MODE == "wol":
            return self._send(404, {"error": "wol sender exposes only POST /api/wake"})
        if r == "/ui.js":
            # The dashboard control itself. Served from here rather than from Homepage's
            # /api/config/custom.js because Cloudflare caches by extension and Homepage sends no
            # Cache-Control, so a UI change stayed invisible for hours behind a stale edge copy.
            # _send always sets Cache-Control: no-store, which Cloudflare honours.
            try:
                with open("/app/ui.js", "rb") as f:
                    return self._send(200, f.read(), "application/javascript; charset=utf-8")
            except OSError as e:
                log("ui.js unreadable: %s" % e)
                return self._send(404, {"error": "ui.js not available"})
        if r == "/api/status":
            return self._send(200, status())
        if r in ("", "/", "/index.html"):
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        r = self._route()
        if not self._peer_ok():
            log("DENIED %s from %s" % (r or "/", self.client_address[0]))
            return self._send(403, {"error": "forbidden: caller outside the cluster"})
        who = self.headers.get("X-Forwarded-Email") or self.headers.get("X-Forwarded-User") or "?"

        if r == "/api/wake":
            if MODE == "wol":
                return self._send(200, {"action": "wake", "results": wake_all()})
            log("WAKE by %s from %s" % (who, self.client_address[0]))
            try:
                return self._send(200, forward_wake())
            except Exception as e:
                return self._send(502, {"error": "wol sender unreachable: %s" % e})

        if MODE == "wol":
            # The hostNetwork half is LAN-reachable, so it must not carry anything destructive.
            return self._send(404, {"error": "wol sender exposes only POST /api/wake"})

        if r == "/api/shutdown/preflight":
            guests, errors = guests_running()
            if errors:
                # FAIL CLOSED. An incomplete enumeration shown as "no running guests" would be a
                # false all-clear, so no token is issued at all.
                return self._send(503, {"error": "could not enumerate guests; refusing to arm OFF",
                                        "errors": errors})
            return self._send(200, {"guests": guests, "confirm": new_confirm(guest_sig(guests)),
                                    "expires_in": CONFIRM_TTL})

        if r == "/api/shutdown":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                req = {}
            sig = take_confirm(str(req.get("confirm", "")))
            if sig is None:
                return self._send(409, {"error": "missing/expired confirmation; run preflight"})
            # Re-check: the operator confirmed a SPECIFIC set of running guests. If a job started
            # in the meantime, that confirmation no longer describes reality.
            guests, errors = guests_running()
            if errors:
                return self._send(503, {"error": "could not re-verify guests; nothing shut down",
                                        "errors": errors})
            if guest_sig(guests) != sig:
                return self._send(409, {"error": "running guests changed since preflight; "
                                                 "nothing shut down - review and confirm again",
                                        "guests": guests})
            log("SHUTDOWN by %s from %s" % (who, self.client_address[0]))
            return self._send(200, {"action": "shutdown", "results": shutdown_all()})

        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    if MODE not in ("api", "wol"):
        raise SystemExit("MODE must be 'api' or 'wol', got %r" % MODE)
    if MODE == "api":
        if not PVE_TOKEN_ID or not PVE_TOKEN_SECRET:
            log("WARNING: PVE token not configured - status/shutdown will fail")
        missing = [n["name"] for n in NODES if n["name"] not in PVE_FINGERPRINTS]
        if missing:
            log("WARNING: no pinned cert fingerprint for %s - those nodes will be REFUSED"
                % ",".join(missing))
    log("listening on :%d base=%s allow=%s" % (PORT, BASE or "/",
                                               ",".join(str(n) for n in ALLOW_FROM)))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
