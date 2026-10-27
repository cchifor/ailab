#!/usr/bin/env python3
"""Runtime fixture for the edge proxy: the pinned Caddy, running configs rendered from the real
registry, asserted with real requests. Needs docker. Run from the repo root:

    python3 scripts/tests/edge_runtime.py

Unit tests (test_edge_render.py) check what the renderer writes; this checks what Caddy then DOES
with it, which is where header provenance and route ordering actually live.

Three source networks give the proxy three kinds of TCP peer (each run picks fresh /24s from the
benchmarking range 198.18.0.0/15 and a unique name prefix, so concurrent runs on one CI runner
never share or delete each other's networks):
  trusted    stands in for the LAN (the profile's allow range); .99 is the excluded address,
             standing in for the gateway's NAT-loopback source
  untrusted  stands in for the internet
  tunnel     stands in for the cloudflared pods (the tunnel's trusted proxies)
Every backend is one echo server (it reports the headers it received and X-Test-Backend, the
upstream the proxy chose), and Authelia is a stand-in that allows `X-Test-Auth: allow`.

Two registry variants run: AS COMMITTED (every host tunnel-mode) and DIRECT (edge_default direct,
plus a synthetic private host), so both modes and the private rules are exercised whatever the
rollout phase. The certificate reloader is tested against the same container.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "edge"))
import render  # noqa: E402

DAEMONSET = ROOT / "kubernetes/apps/apps/edge/proxy/daemonset.yaml"
RELOADER = ROOT / "kubernetes/apps/apps/edge/proxy/cert-reloader.sh"
SUPPORT = pathlib.Path(__file__).resolve().parent / "edge_runtime"
IMG_PY = "python:3.12-alpine"
IMG_SSL = "alpine/openssl:3.5.8"
# Filled in by Fixture.up(): the four /24 prefixes this run owns, and the addresses on them.
NETS: dict = {}
PROXY_IP: dict = {}
CLIENT_IP: dict = {}
PROFILE: dict = {}
P = f"edgert{os.getpid()}"  # docker name prefix, unique per run
SENTINELS = ["SENTINEL-JWT-7f3a", "SENTINEL-KEY-91bc", "SENTINEL-AUTH-22de", "SENTINEL-Q-55aa"]


def caddy_image() -> str:
    m = re.search(r"image:\s*(caddy:[^\s]+)", DAEMONSET.read_text())
    if not m:
        sys.exit("no caddy image in daemonset.yaml")
    return m.group(1)


def sh(*args, check=True, capture=True, inp=None):
    r = subprocess.run(list(args), capture_output=capture, text=True, input=inp,
                       env=_env())
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}\n{r.stdout}\n{r.stderr}")
    return r


def _env():
    import os
    e = dict(os.environ)
    e["MSYS_NO_PATHCONV"] = "1"  # Git Bash on Windows would rewrite /container/paths
    return e


def docker(*args, **kw):
    return sh("docker", *args, **kw)


class Fixture:
    def __init__(self, work: pathlib.Path):
        self.work = work
        self.caddy = caddy_image()

    def up(self):
        self.down()
        for _ in range(20):
            k = random.randrange(256)
            prefixes = {"trusted": f"198.18.{k}", "untrusted": f"198.19.{k}",
                        "tunnel": f"198.18.{(k + 128) % 256}", "back": f"198.19.{(k + 128) % 256}"}
            created = []
            for name, prefix in prefixes.items():
                r = docker("network", "create", "--subnet", f"{prefix}.0/24", f"{P}-{name}", check=False)
                if r.returncode != 0:
                    break
                created.append(name)
            if len(created) == len(prefixes):
                break
            for name in created:  # an overlap with another run: release ours and draw again
                docker("network", "rm", f"{P}-{name}", check=False)
        else:
            raise RuntimeError("no free 198.18.0.0/15 subnets for the fixture")
        NETS.update(prefixes)
        PROXY_IP.update({n: f"{prefixes[n]}.10" for n in ("trusted", "untrusted", "tunnel")})
        CLIENT_IP.update({"trusted": f"{prefixes['trusted']}.20", "excluded": f"{prefixes['trusted']}.99",
                          "untrusted": f"{prefixes['untrusted']}.20", "tunnel": f"{prefixes['tunnel']}.20"})
        PROFILE.update({"echo": "http://echo:8080", "authelia": "auth:9091",
                        "allow": [f"{prefixes['trusted']}.0/24"], "exclude": [f"{prefixes['trusted']}.99/32"],
                        "tunnel_trusted": [f"{prefixes['tunnel']}.0/24"]})
        (self.work / "certs").mkdir()
        (self.work / "config").mkdir()
        (self.work / "openbao-ca").mkdir()
        (self.work / "openbao-ca" / "ca.crt").write_text("")
        self.make_cert("certs", "*.chifor.me")
        for kind, alias in (("echo", "echo"), ("auth", "auth")):
            docker("run", "-d", "--name", f"{P}-{kind}", "--network", f"{P}-back",
                   "--network-alias", alias, "-v", f"{SUPPORT}:/t:ro", IMG_PY,
                   "python", "/t/servers.py", kind)

    def make_cert(self, subdir, cn, key_from=None):
        """A self-signed pair in work/<subdir>; key_from reuses another dir's key (mismatch tests)."""
        d = self.work / subdir
        d.mkdir(exist_ok=True)
        if key_from:
            # a certificate for a NEW key, paired with the OLD key file: a mismatched pair
            docker("run", "--rm", "-v", f"{d}:/o", IMG_SSL, "req", "-x509", "-newkey", "rsa:2048",
                   "-nodes", "-days", "2", "-subj", f"/CN={cn}", "-addext",
                   f"subjectAltName=DNS:{cn}", "-keyout", "/o/unused.key", "-out", "/o/tls.crt")
            shutil.copy(self.work / key_from / "tls.key", d / "tls.key")
            return
        docker("run", "--rm", "-v", f"{d}:/o", IMG_SSL, "req", "-x509", "-newkey", "rsa:2048",
               "-nodes", "-days", "2", "-subj", f"/CN={cn}", "-addext", f"subjectAltName=DNS:{cn}",
               "-keyout", "/o/tls.key", "-out", "/o/tls.crt")

    def start_proxy(self, caddyfile: str):
        docker("rm", "-f", f"{P}-proxy", check=False)
        (self.work / "Caddyfile").write_text(caddyfile, encoding="utf-8")
        docker("create", "--name", f"{P}-proxy", "--network", f"{P}-trusted",
               "--ip", PROXY_IP["trusted"], "--network-alias", "edgeproxy",
               "-v", f"{self.work / 'Caddyfile'}:/etc/caddy/Caddyfile:ro",
               "-v", f"{self.work / 'certs'}:/certs:ro",
               "-v", f"{self.work / 'config'}:/config",
               "-v", f"{self.work / 'openbao-ca'}:/openbao-ca:ro",
               "-v", f"{RELOADER}:/reloader.sh:ro",
               self.caddy)
        for net in ("untrusted", "tunnel", "back"):
            args = ["network", "connect", "--alias", "edgeproxy"]
            if net in PROXY_IP:
                args += ["--ip", PROXY_IP[net]]
            docker(*args, f"{P}-{net}", f"{P}-proxy")
        docker("start", f"{P}-proxy")
        for _ in range(50):
            r = self.request("trusted", [{"id": "up", "port": 8082, "host": "x", "target": "/healthz"}])
            if r[0].get("status") == 200:
                return
            time.sleep(0.2)
        raise RuntimeError("proxy did not come up:\n" + docker("logs", f"{P}-proxy", check=False).stderr)

    def request(self, source, cases):
        net = "trusted" if source == "excluded" else source
        f = self.work / f"cases-{source}.json"
        f.write_text(json.dumps(cases))
        r = docker("run", "--rm", "--network", f"{P}-{net}", "--ip", CLIENT_IP[source],
                   "-v", f"{SUPPORT}:/t:ro", "-v", f"{f}:/cases.json:ro", IMG_PY,
                   "python", "/t/client.py", "/cases.json", PROXY_IP[net])
        return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]

    def down(self):
        for c in ("proxy", "echo", "auth"):
            docker("rm", "-f", f"{P}-{c}", check=False)
        for name in ("trusted", "untrusted", "tunnel", "back"):
            docker("network", "rm", f"{P}-{name}", check=False)


# --------------------------------------------------------------------------------------------
# cases. Each: (source, request, expectation). Expectations are checked by `check`.


def direct(id_, host, target="/", sni=None, headers=()):
    return {"id": id_, "port": 8443, "tls": True, "sni": sni or host, "host": host,
            "target": target, "headers": list(headers)}


def tunnel(id_, host, target="/", cf_ip="203.0.113.7", visitor='{"scheme":"https"}', headers=()):
    h = []
    if cf_ip is not None:
        h.append(["CF-Connecting-IP", cf_ip])
    if visitor is not None:
        h.append(["Cf-Visitor", visitor])
    return {"id": id_, "port": 8080, "host": host, "target": target, "headers": h + list(headers)}


SPOOF = [["X-Forwarded-For", "6.6.6.6"], ["X-Real-IP", "6.6.6.6"], ["True-Client-IP", "6.6.6.6"],
         ["Forwarded", "for=6.6.6.6"], ["Remote-User", "mallory"], ["Remote-Groups", "admins"],
         ["X-Forwarded-Host", "evil.example"], ["X-Forwarded-Proto", "http"], ["Cdn-Loop", "x"]]
CF_SPOOF = [["CF-Connecting-IP", "6.6.6.6"], ["Cf-Ray", "spoofed"], ["Cf-Access-Jwt-Assertion", "forged"]]


def echo_headers(res):
    try:
        return {k.lower(): v for k, v in json.loads(res["body"])["headers"]}
    except (ValueError, KeyError, TypeError):
        return None


def expect(res, status, backend=None, client=None, absent=(), present=None, location=None):
    errs = []
    if res.get("error"):
        return [f"transport error {res['error']}"]
    if res["status"] != status:
        errs.append(f"status {res['status']} != {status} (body {res['body'][:120]!r})")
        return errs
    if location and not any(location in v for v in res["headers"].get("location", [])):
        errs.append(f"location {res['headers'].get('location')} lacks {location!r}")
    if backend or client or absent or present:
        eh = echo_headers(res)
        if eh is None:
            return errs + ["not an echo response"]
        if backend and eh.get("x-test-backend") != backend:
            errs.append(f"backend {eh.get('x-test-backend')} != {backend}")
        if client:
            for h in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
                if eh.get(h) != client:
                    errs.append(f"{h}={eh.get(h)!r} != {client}")
            if eh.get("x-forwarded-proto") != "https":
                errs.append(f"x-forwarded-proto={eh.get('x-forwarded-proto')!r}")
        for h in absent:
            if h.lower() in eh:
                errs.append(f"{h} reached the backend: {eh[h.lower()]!r}")
        for h, v in (present or {}).items():
            if eh.get(h.lower()) != v:
                errs.append(f"{h}={eh.get(h.lower())!r} != {v!r}")
    return errs


def suite_committed(reg):
    """The registry as committed (P2: every host tunnel-mode)."""
    be = lambda n: reg["hosts"][n]["backend"]["url"]  # noqa: E731
    T, U, X, C = CLIENT_IP["trusted"], CLIENT_IP["untrusted"], CLIENT_IP["excluded"], CLIENT_IP["tunnel"]
    cases = [
        # direct listener, tunnel-mode host: LAN only, with the header policy
        ("trusted", direct("router-lan", "router.chifor.me", headers=SPOOF + CF_SPOOF),
         lambda r: expect(r, 200, be("router"), T, absent=["Cf-Ray", "Cf-Access-Jwt-Assertion", "Forwarded",
                                                            "True-Client-IP", "Remote-User", "Remote-Groups", "Cdn-Loop"],
                          present={"X-Forwarded-Host": "router.chifor.me"})),
        ("untrusted", direct("router-wan", "router.chifor.me"), lambda r: expect(r, 403)),
        ("excluded", direct("router-gw", "router.chifor.me"), lambda r: expect(r, 403)),
        ("trusted", direct("unknown", "nope.chifor.me"), lambda r: expect(r, 404)),
        ("trusted", direct("unknown-sni", "router.chifor.me", sni="other.example"),
         lambda r: [] if r.get("error") or r.get("status") in (200, 404) else [f"unexpected {r}"]),
        # SNI of one host, Host of another: routing and trust follow Host
        ("untrusted", direct("sni-mismatch", "dsh.chifor.me", sni="router.chifor.me"), lambda r: expect(r, 403)),
        # authelia host
        ("trusted", direct("dsh-anon", "dsh.chifor.me"), lambda r: expect(r, 302, location="sso.chifor.me")),
        ("trusted", direct("dsh-auth", "dsh.chifor.me", headers=[["X-Test-Auth", "allow"], ["Remote-User", "mallory"]]),
         lambda r: expect(r, 200, be("dsh"), T, present={"Remote-User": "alice", "Remote-Groups": "admins"},
                          absent=["Remote-Extra"])),
        # id: Keycloak realm guard
        ("trusted", direct("id-relay", "id.chifor.me", "/realms/relay/protocol/openid-connect/auth?redirect_uri=https%3A%2F%2Fx"),
         lambda r: expect(r, 200, be("id"))),
        ("trusted", direct("id-llmrouter", "id.chifor.me", "/realms/llm-router"), lambda r: expect(r, 200, be("id"))),
        ("trusted", direct("id-res", "id.chifor.me", "/resources/x/login.css"), lambda r: expect(r, 200, be("id"))),
        ("trusted", direct("id-master", "id.chifor.me", "/realms/master/"), lambda r: expect(r, 404)),
        ("trusted", direct("id-prefix", "id.chifor.me", "/realms/relayx/"), lambda r: expect(r, 404)),
        ("trusted", direct("id-root", "id.chifor.me", "/"), lambda r: expect(r, 404)),
        ("trusted", direct("id-dotdot", "id.chifor.me", "/realms/relay/../master/"), lambda r: expect(r, 404)),
        ("trusted", direct("id-enc", "id.chifor.me", "/realms/relay/%2e%2e/master/"), lambda r: expect(r, 404)),
        ("trusted", direct("id-enc-upper", "id.chifor.me", "/realms/relay/%2E%2E/master/"), lambda r: expect(r, 404)),
        ("trusted", direct("id-double", "id.chifor.me", "/realms/relay/%252e%252e/master/"), lambda r: expect(r, 404)),
        ("trusted", direct("id-semi", "id.chifor.me", "/realms/relay;x/"), lambda r: expect(r, 404)),
        ("trusted", direct("id-slash", "id.chifor.me", "/realms/relay%2f..%2fmaster/"), lambda r: expect(r, 404)),
        # trueswarm media split
        ("trusted", direct("ts-media", "trueswarm.chifor.me", "/media/abc"),
         lambda r: expect(r, 200, "http://trueswarm-muse.trueswarm.svc.cluster.local:80")),
        ("trusted", direct("ts-upload", "trueswarm.chifor.me", "/api/v1/uploads/t1/content"),
         lambda r: expect(r, 200, "http://trueswarm-muse.trueswarm.svc.cluster.local:80")),
        ("trusted", direct("ts-deep", "trueswarm.chifor.me", "/media/abc/def"), lambda r: expect(r, 200, be("trueswarm"))),
        ("trusted", direct("ts-root", "trueswarm.chifor.me", "/"), lambda r: expect(r, 200, be("trueswarm"))),
        # vault: /admin is LAN-only on the direct listener; the rest follows the host's mode
        ("trusted", direct("vault-admin-lan", "vault.chifor.me", "/admin"), lambda r: expect(r, 200, be("vault"))),
        # the canary answers with the peer it saw
        ("trusted", direct("canary", "edge-canary.chifor.me"),
         lambda r: [] if r.get("status") == 200 and f"peer={T}" in r["body"] else [f"canary {r}"]),
        # tunnel listener
        ("tunnel", tunnel("tun-router", "router.chifor.me",
                          headers=[["Cf-Ray", "abc-OTP"], ["Cf-Access-Jwt-Assertion", "jwt"], ["X-Real-IP", "6.6.6.6"],
                                   ["Remote-User", "mallory"]]),
         lambda r: expect(r, 200, be("router"), "203.0.113.7", present={"Cf-Ray": "abc-OTP", "Cf-Access-Jwt-Assertion": "jwt"},
                          absent=["Remote-User"])),
        ("tunnel", tunnel("tun-no-ip", "router.chifor.me", cf_ip=None), lambda r: expect(r, 400)),
        ("tunnel", tunnel("tun-list", "router.chifor.me", cf_ip="1.2.3.4, 5.6.7.8"), lambda r: expect(r, 400)),
        ("tunnel", tunnel("tun-dup", "router.chifor.me", headers=[["CF-Connecting-IP", "5.6.7.8"]]), lambda r: expect(r, 400)),
        ("tunnel", tunnel("tun-garbage", "router.chifor.me", cf_ip="not-an-ip"), lambda r: expect(r, 400)),
        ("tunnel", tunnel("tun-v6", "router.chifor.me", cf_ip="2001:db8::7"), lambda r: expect(r, 200, be("router"), "2001:db8::7")),
        ("tunnel", tunnel("tun-no-visitor", "router.chifor.me", visitor=None), lambda r: expect(r, 400)),
        ("tunnel", tunnel("tun-http", "router.chifor.me", "/x?y=1", visitor='{"scheme":"http"}'),
         lambda r: expect(r, 308, location="https://router.chifor.me/x?y=1")),
        ("tunnel", tunnel("tun-unknown", "nope.chifor.me"), lambda r: expect(r, 404)),
        ("tunnel", tunnel("tun-vault-admin", "vault.chifor.me", "/admin"), lambda r: expect(r, 200, be("vault"))),
        ("tunnel", tunnel("tun-dsh-anon", "dsh.chifor.me"), lambda r: expect(r, 302, location="sso.chifor.me")),
        ("tunnel", tunnel("tun-dsh-auth", "dsh.chifor.me", headers=[["X-Test-Auth", "allow"]]),
         lambda r: expect(r, 200, be("dsh"), "203.0.113.7", present={"Remote-User": "alice"})),
        # a non-cloudflared peer on the tunnel port cannot choose its client address
        ("untrusted", tunnel("tun-forged", "router.chifor.me", cf_ip="6.6.6.6"),
         lambda r: [] if r.get("status") in (200,) and echo_headers(r).get("cf-connecting-ip") == U
         else ([] if r.get("status") == 400 else [f"forged CF-Connecting-IP accepted: {r.get('status')} {r.get('body','')[:200]}"])),
        # redirect listener and health
        ("trusted", {"id": "redir-lan", "port": 8081, "host": "router.chifor.me", "target": "/a?b=1"},
         lambda r: expect(r, 308, location="https://router.chifor.me/a?b=1")),
        ("untrusted", {"id": "redir-wan", "port": 8081, "host": "router.chifor.me", "target": "/"}, lambda r: expect(r, 403)),
        ("excluded", {"id": "redir-gw", "port": 8081, "host": "router.chifor.me", "target": "/"}, lambda r: expect(r, 403)),
        ("trusted", {"id": "health", "port": 8082, "host": "x", "target": "/healthz"}, lambda r: expect(r, 200)),
        # log redaction: sentinels through both listeners
        ("trusted", direct("log-direct", "router.chifor.me", "/x?token=" + SENTINELS[3],
                           headers=[["Authorization", "Bearer " + SENTINELS[2]], ["X-Api-Key", SENTINELS[1]]]),
         lambda r: expect(r, 200)),
        ("tunnel", tunnel("log-tunnel", "router.chifor.me", "/x?token=" + SENTINELS[3],
                          headers=[["Cf-Access-Jwt-Assertion", SENTINELS[0]], ["Authorization", "Bearer " + SENTINELS[2]],
                                   ["X-Api-Key", SENTINELS[1]]]),
         lambda r: expect(r, 200)),
    ]
    return cases


def variant_direct(reg):
    r = copy.deepcopy(reg)
    r["edge_default"] = "direct"
    r["hosts"]["test-private"] = {"exposure": "private", "auth": "native", "access_gated": False, "lan_dns": False,
                                  "backend": {"url": "http://private.example.svc:80"}, "smoke": {"status": 200}}
    return r


def suite_direct(reg):
    be = lambda n: reg["hosts"][n]["backend"]["url"]  # noqa: E731
    T, U = CLIENT_IP["trusted"], CLIENT_IP["untrusted"]
    return [
        ("untrusted", direct("d-router-wan", "router.chifor.me", headers=SPOOF + CF_SPOOF),
         lambda r: expect(r, 200, be("router"), U, absent=["Cf-Ray", "Cf-Access-Jwt-Assertion", "Remote-User"])),
        ("excluded", direct("d-router-gw", "router.chifor.me"), lambda r: expect(r, 200, be("router"), CLIENT_IP["excluded"])),
        # access-gated hosts stay pinned to the tunnel: still LAN-only on the direct listener
        ("untrusted", direct("d-dsh-wan", "dsh.chifor.me"), lambda r: expect(r, 403)),
        ("untrusted", direct("d-proxmox-wan", "proxmox.chifor.me"), lambda r: expect(r, 403)),
        ("untrusted", direct("d-dw1-wan", "dw1.chifor.me", headers=[["Cf-Access-Jwt-Assertion", "forged"]]), lambda r: expect(r, 403)),
        # private host: trusted peers only, never through the tunnel
        ("trusted", direct("d-priv-lan", "test-private.chifor.me"), lambda r: expect(r, 200, "http://private.example.svc:80", T)),
        ("untrusted", direct("d-priv-wan", "test-private.chifor.me", headers=SPOOF), lambda r: expect(r, 403)),
        ("excluded", direct("d-priv-gw", "test-private.chifor.me"), lambda r: expect(r, 403)),
        ("tunnel", tunnel("d-priv-tun", "test-private.chifor.me"), lambda r: expect(r, 404)),
        # vault direct: /admin stays LAN-only, every variant of the path
        ("untrusted", direct("d-vault", "vault.chifor.me", "/api/sync"), lambda r: expect(r, 200, be("vault"))),
        ("untrusted", direct("d-vault-admin", "vault.chifor.me", "/admin"), lambda r: expect(r, 403)),
        ("untrusted", direct("d-vault-admin2", "vault.chifor.me", "/admin/users"), lambda r: expect(r, 403)),
        ("untrusted", direct("d-vault-ADMIN", "vault.chifor.me", "/ADMIN"), lambda r: expect(r, 403)),
        ("untrusted", direct("d-vault-dslash", "vault.chifor.me", "//admin"), lambda r: expect(r, 403)),
        ("untrusted", direct("d-vault-enc", "vault.chifor.me", "/%61dmin"), lambda r: expect(r, 403)),
        ("untrusted", direct("d-vault-dot", "vault.chifor.me", "/./admin"), lambda r: expect(r, 404)),
        ("untrusted", direct("d-vault-dotdot", "vault.chifor.me", "/x/../admin"), lambda r: expect(r, 404)),
        ("untrusted", direct("d-vault-enc-dot", "vault.chifor.me", "/x/%2e%2e/admin"), lambda r: expect(r, 404)),
        ("excluded", direct("d-vault-admin-gw", "vault.chifor.me", "/admin"), lambda r: expect(r, 403)),
        ("trusted", direct("d-vault-admin-lan", "vault.chifor.me", "/admin"), lambda r: expect(r, 200, be("vault"))),
        ("untrusted", direct("d-canary", "edge-canary.chifor.me"),
         lambda r: [] if r.get("status") == 200 and f"peer={U}" in r["body"] else [f"canary {r}"]),
    ]


def run_suite(fx, label, reg, suite):
    caddyfile = render.render(reg, PROFILE)
    fx.start_proxy(caddyfile)
    by_source = {}
    for src, case, chk in suite:
        by_source.setdefault(src, []).append((case, chk))
    failures = []
    for src, items in by_source.items():
        results = fx.request(src, [c for c, _ in items])
        got = {r["id"]: r for r in results}
        for case, chk in items:
            res = got.get(case["id"], {"error": "no result"})
            errs = chk(res)
            status = "ok  " if not errs else "FAIL"
            print(f"  {status} [{label}] {src:9s} {case['id']}" + ("" if not errs else f": {'; '.join(errs)}"))
            if errs:
                failures.append(case["id"])
    return failures


def check_logs(fx):
    logs = docker("logs", f"{P}-proxy", check=False)
    text = logs.stdout + logs.stderr
    leaked = [s for s in SENTINELS if s in text]
    print(f"  {'ok  ' if not leaked else 'FAIL'} [logs] no credential sentinel in the access log" +
          (f": leaked {leaked}" if leaked else ""))
    return ["log-redaction"] if leaked else []


def served_fingerprint(fx):
    """SHA-256 of the certificate the proxy presents, read by openssl from the trusted network."""
    r = docker("run", "--rm", "--network", f"{P}-trusted", "--entrypoint", "sh", IMG_SSL, "-c",
               f"echo | openssl s_client -connect {PROXY_IP['trusted']}:8443 -servername router.chifor.me 2>/dev/null "
               "| openssl x509 -noout -fingerprint -sha256", check=False)
    return r.stdout.strip()


def check_reloader(fx):
    """Valid renewal -> served; mismatched pair -> kept out and counted; repaired -> served."""
    failures = []
    docker("exec", "-d", "-e", "RELOAD_INTERVAL=1", f"{P}-proxy", "sh", "/reloader.sh")
    time.sleep(2)
    before = served_fingerprint(fx)
    # 1. a mismatched pair (new cert, old key): must not load
    fx.make_cert("bad", "*.chifor.me", key_from="certs")
    shutil.copy(fx.work / "bad" / "tls.crt", fx.work / "certs" / "tls.crt")
    time.sleep(4)
    after_bad = served_fingerprint(fx)
    metrics = (fx.work / "config" / "reloader" / "metrics").read_text() if (fx.work / "config" / "reloader" / "metrics").exists() else ""
    ok = after_bad == before and "edge_cert_invalid_total 0" not in metrics
    print(f"  {'ok  ' if ok else 'FAIL'} [reloader] mismatched pair kept out (served unchanged, counted invalid)")
    failures += [] if ok else ["reloader-mismatch"]
    # 2. a valid new pair: served within a few passes
    fx.make_cert("good", "*.chifor.me")
    shutil.copy(fx.work / "good" / "tls.key", fx.work / "certs" / "tls.key")
    shutil.copy(fx.work / "good" / "tls.crt", fx.work / "certs" / "tls.crt")
    new = ""
    for _ in range(15):
        time.sleep(1)
        new = served_fingerprint(fx)
        if new and new != before:
            break
    ok = bool(new) and new != before
    print(f"  {'ok  ' if ok else 'FAIL'} [reloader] valid renewal served after reload")
    failures += [] if ok else ["reloader-renewal"]
    return failures


def main() -> int:
    if shutil.which("docker") is None:
        print("edge_runtime: docker is required", file=sys.stderr)
        return 2
    reg = render.load()
    work = pathlib.Path(tempfile.mkdtemp(prefix="edgert-", dir=ROOT / ".edge-runtime-tmp"
                                         if (ROOT / ".edge-runtime-tmp").is_dir() else None))
    fx = Fixture(work)
    failures = []
    try:
        fx.up()
        print("edge runtime fixture: registry as committed")
        failures += run_suite(fx, "committed", reg, suite_committed(reg))
        failures += check_logs(fx)
        print("edge runtime fixture: edge_default direct + a private host")
        failures += run_suite(fx, "direct", variant_direct(reg), suite_direct(reg))
        # auth service down -> fail closed
        docker("stop", f"{P}-auth")
        res = fx.request("trusted", [direct("dsh-auth-down", "dsh.chifor.me", headers=[["X-Test-Auth", "allow"]])])[0]
        ok = res.get("status") == 502
        print(f"  {'ok  ' if ok else 'FAIL'} [direct] auth service down -> 502 (got {res.get('status')})")
        failures += [] if ok else ["auth-down"]
        failures += check_reloader(fx)
    finally:
        if failures:
            print(docker("logs", "--tail", "40", f"{P}-proxy", check=False).stderr[-4000:])
        fx.down()
        shutil.rmtree(work, ignore_errors=True)
    print(f"edge runtime fixture: {'FAILED ' + str(len(failures)) if failures else 'all passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
