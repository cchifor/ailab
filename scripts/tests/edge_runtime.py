#!/usr/bin/env python3
"""Runtime fixture for the edge proxy: the pinned Caddy, running configs rendered from the real
registry, asserted with real requests. Needs docker. Run from the repo root:

    python3 scripts/tests/edge_runtime.py

Unit tests (test_edge_render.py) check what the renderer writes; this checks what Caddy then DOES
with it, which is where header provenance and route ordering actually live.

Three source networks give the proxy three kinds of TCP peer (each run picks fresh /24s from the
benchmarking range 198.18.0.0/15, and every container and network carries this run's random id as a
label, so concurrent runs on one docker daemon never share or delete each other's resources):
  trusted    stands in for the LAN (the profile's allow range); .99 is the excluded address,
             standing in for the gateway's NAT-loopback source
  untrusted  stands in for the internet
  tunnel     stands in for the cloudflared pods (the tunnel's trusted proxies)
HTTP backends become one echo server, HTTPS backends one TLS echo that also reports the SNI it got,
and Authelia a stand-in (allow / deny / anonymous) that reports the headers it received.

Two registry variants run: AS COMMITTED (every host tunnel-mode) and DIRECT (edge_default direct, a
synthetic private host, vault's /admin made private), so both modes and the private rules are
exercised whatever the rollout phase. The certificate reloader and log redaction are tested last,
after the failure cases that make Caddy write error logs.

NOT covered here, by construction: Kubernetes NetworkPolicy (docker networks do not apply it).
scripts/edge/netpol-check.sh exercises the tunnel listener's policy in the cluster.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import random
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "edge"))
import render  # noqa: E402

DAEMONSET = ROOT / "kubernetes/apps/apps/edge/proxy/daemonset.yaml"
RELOADER = ROOT / "kubernetes/apps/apps/edge/proxy/cert-reloader.sh"
PROBE = ROOT / "kubernetes/apps/apps/edge/proxy/cert-probe.sh"
SUPPORT = pathlib.Path(__file__).resolve().parent / "edge_runtime"
# Pinned by digest, so a fixture failure is always attributable to the diff; IMG_SSL is also the
# DaemonSet's cert-probe image (asserted below).
IMG_PY = "python:3.12-alpine@sha256:5789aa194f757888f41fa3d48e14b6feaa1b57c0af0b56fcb1372d6474d3bff0"
IMG_SSL = "alpine/openssl:3.5.8@sha256:3f25da71f70eba788067daac3f3df03bd1de7a7c52ed89fa93b94ad2c92d986b"
LABEL = "ailab.edge-runtime"  # every resource: LABEL=<run id>, LABEL.started=<unix time>
STALE_AFTER = 2 * 3600       # a run older than this is dead (cancelled without cleanup): removed

# Filled in by Fixture.up(): the four /24 prefixes this run owns, and the addresses on them.
NETS: dict = {}
PROXY_IP: dict = {}
CLIENT_IP: dict = {}
PROFILE: dict = {}
RUN = secrets.token_hex(4)
P = f"edgert-{RUN}"  # docker name prefix, unique per run
SENTINELS = ["SENTINEL-JWT-7f3a", "SENTINEL-KEY-91bc", "SENTINEL-AUTH-22de", "SENTINEL-Q-55aa",
             "SENTINEL-SECRET-3c1e"]


def probe_image_matches() -> bool:
    return IMG_SSL in DAEMONSET.read_text()


def caddy_image() -> str:
    m = re.search(r"image:\s*(caddy:[^\s]+)", DAEMONSET.read_text())
    if not m:
        sys.exit("no caddy image in daemonset.yaml")
    return m.group(1)


def _env():
    e = dict(os.environ)
    e["MSYS_NO_PATHCONV"] = "1"  # Git Bash on Windows would rewrite /container/paths
    return e


def docker(*args, check=True):
    r = subprocess.run(["docker", *args], capture_output=True, text=True, env=_env())
    if check and r.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)}\n{r.stdout}\n{r.stderr}")
    return r


def user_args():
    """Run file-writing containers as the invoking user on POSIX hosts, so the host can read and
    clean what they write (the CI runner is an unprivileged user; docker would write root-owned
    files into the bind mounts)."""
    if hasattr(os, "getuid"):
        return ["--user", f"{os.getuid()}:{os.getgid()}"]
    return []


def labels():
    return ["--label", f"{LABEL}={RUN}", "--label", f"{LABEL}.started={int(time.time())}"]


def remove_stale_runs():
    """Containers and networks left by runs that died without cleanup (cancelled jobs), and only
    those: identified by this fixture's label and an age past STALE_AFTER."""
    now = int(time.time())
    for kind, ls, rm in (("container", ["ps", "-a"], ["rm", "-f"]), ("network", ["network", "ls"], ["network", "rm"])):
        r = docker(*ls, "--filter", f"label={LABEL}", "--format", "{{.ID}}", check=False)
        for oid in r.stdout.split():
            fmt = ("{{index .Config.Labels \"" + LABEL + ".started\"}}" if kind == "container"
                   else "{{index .Labels \"" + LABEL + ".started\"}}")
            started = docker("inspect", "--format", fmt, oid, check=False).stdout.strip()
            if started.isdigit() and now - int(started) > STALE_AFTER:
                docker(*rm, oid, check=False)


class Fixture:
    def __init__(self, work: pathlib.Path):
        self.work = work
        self.caddy = caddy_image()

    def up(self):
        remove_stale_runs()
        for _ in range(20):
            k = random.randrange(256)
            prefixes = {"trusted": f"198.18.{k}", "untrusted": f"198.19.{k}",
                        "tunnel": f"198.18.{(k + 128) % 256}", "back": f"198.19.{(k + 128) % 256}"}
            created = []
            for name, prefix in prefixes.items():
                r = docker("network", "create", *labels(), "--subnet", f"{prefix}.0/24", f"{P}-{name}", check=False)
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
        # trusted = an admin-tier device; automation = inside the LAN but in the excluded statics block
        CLIENT_IP.update({"trusted": f"{prefixes['trusted']}.100", "automation": f"{prefixes['trusted']}.20",
                          "excluded": f"{prefixes['trusted']}.99",
                          "untrusted": f"{prefixes['untrusted']}.20", "tunnel": f"{prefixes['tunnel']}.20"})
        PROFILE.update({"echo": "http://echo:8080", "echo_tls": "https://echotls:8443", "authelia": "auth:9091",
                        "allow": [f"{prefixes['trusted']}.0/24"], "exclude": [f"{prefixes['trusted']}.99/32"],
                        "admin_exclude": [f"{prefixes['trusted']}.0/27"], "admin_include": [],
                        "tunnel_trusted": [f"{prefixes['tunnel']}.0/24"]})
        for d in ("certs", "config", "openbao-ca", "echocerts"):
            (self.work / d).mkdir()
        (self.work / "openbao-ca" / "ca.crt").write_text("")
        self.make_cert("certs", "*.chifor.me")
        self.make_cert("echocerts", "echotls", names=("echo.crt", "echo.key"))
        for kind, alias, extra in (("echo", "echo", []), ("auth", "auth", []),
                                   ("echo-tls", "echotls", ["-v", f"{self.work / 'echocerts'}:/certs:ro"])):
            docker("run", "-d", *labels(), "--name", f"{P}-{kind}", "--network", f"{P}-back",
                   "--network-alias", alias, "-v", f"{SUPPORT}:/t:ro", *extra, IMG_PY,
                   "python", "/t/servers.py", kind)

    def make_cert(self, subdir, cn, key_from=None, names=("tls.crt", "tls.key")):
        """A self-signed pair in work/<subdir>. key_from pairs a NEW certificate with another
        directory's key: a mismatched pair."""
        d = self.work / subdir
        d.mkdir(exist_ok=True)
        crt, key = names
        keyout = "unused.key" if key_from else key
        docker("run", "--rm", *user_args(), "-v", f"{d}:/o", IMG_SSL, "req", "-x509", "-newkey", "rsa:2048",
               "-nodes", "-days", "2", "-subj", f"/CN={cn}", "-addext", f"subjectAltName=DNS:{cn}",
               "-keyout", f"/o/{keyout}", "-out", f"/o/{crt}")
        if key_from:
            shutil.copy(self.work / key_from / "tls.key", d / key)

    def start_proxy(self, caddyfile: str):
        docker("rm", "-f", f"{P}-proxy", check=False)
        (self.work / "Caddyfile").write_text(caddyfile, encoding="utf-8")
        docker("create", *labels(), *user_args(), "--name", f"{P}-proxy", "--network", f"{P}-trusted",
               "--ip", PROXY_IP["trusted"], "--network-alias", "edgeproxy",
               "-e", "XDG_DATA_HOME=/config/data", "-e", "XDG_CONFIG_HOME=/config",
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
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            r = self.request("trusted", [{"id": "up", "port": 8082, "host": "x", "target": "/healthz"}])
            if r and r[0].get("status") == 200:
                return
            time.sleep(0.5)
        raise RuntimeError("proxy did not come up:\n" + docker("logs", f"{P}-proxy", check=False).stderr[-3000:])

    def request(self, source, cases):
        net = "trusted" if source in ("excluded", "automation") else source
        f = self.work / f"cases-{source}.json"
        f.write_text(json.dumps(cases))
        r = docker("run", "--rm", *labels(), "--network", f"{P}-{net}", "--ip", CLIENT_IP[source],
                   "-v", f"{SUPPORT}:/t:ro", "-v", f"{f}:/cases.json:ro", IMG_PY,
                   "python", "/t/client.py", "/cases.json", PROXY_IP[net], check=False)
        return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]

    def logs(self):
        r = docker("logs", f"{P}-proxy", check=False)
        return r.stdout + r.stderr

    def down(self):
        r = docker("ps", "-aq", "--filter", f"label={LABEL}={RUN}", check=False)
        for cid in r.stdout.split():
            docker("rm", "-f", cid, check=False)
        r = docker("network", "ls", "-q", "--filter", f"label={LABEL}={RUN}", check=False)
        for nid in r.stdout.split():
            docker("network", "rm", nid, check=False)


# --------------------------------------------------------------------------------------------
# cases. Each: (source, request, check). Checks return a list of problems (empty = pass).


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


def echo(res):
    try:
        return json.loads(res["body"])
    except (ValueError, KeyError, TypeError):
        return None


def echo_headers(res):
    e = echo(res)
    return {k.lower(): v for k, v in e["headers"]} if e else None


def expect(res, status, backend=None, client=None, absent=(), present=None, location=None, sni=None):
    errs = []
    if res.get("error"):
        return [f"transport error {res['error']}"]
    if res["status"] != status:
        return [f"status {res['status']} != {status} (body {res['body'][:120]!r})"]
    if location and not any(location in v for v in res["headers"].get("location", [])):
        errs.append(f"location {res['headers'].get('location')} lacks {location!r}")
    if backend or client or absent or present or sni:
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
        if sni and echo(res).get("sni") != sni:
            errs.append(f"SNI {echo(res).get('sni')!r} != {sni!r}")
    return errs


def not_backend(res, status):
    """The request was answered by the proxy or the auth service and never reached a backend."""
    if res.get("error"):
        return [f"transport error {res['error']}"]
    errs = [] if res["status"] == status else [f"status {res['status']} != {status}"]
    if echo(res) is not None:
        errs.append("the backend was reached")
    return errs


def suite_committed(reg):
    """The registry as committed (P2: every host tunnel-mode)."""
    be = lambda n: reg["hosts"][n]["backend"]["url"]  # noqa: E731
    T, U = CLIENT_IP["trusted"], CLIENT_IP["untrusted"]
    return [
        # direct listener, tunnel-mode host: LAN only, with the header policy
        ("trusted", direct("router-lan", "router.chifor.me", headers=SPOOF + CF_SPOOF),
         lambda r: expect(r, 200, be("router"), T, absent=["Cf-Ray", "Cf-Access-Jwt-Assertion", "Forwarded",
                                                            "True-Client-IP", "Remote-User", "Remote-Groups", "Cdn-Loop"],
                          present={"X-Forwarded-Host": "router.chifor.me"})),
        ("untrusted", direct("router-wan", "router.chifor.me"), lambda r: expect(r, 403)),
        ("excluded", direct("router-gw", "router.chifor.me"), lambda r: expect(r, 403)),
        # automation (a dev-worker or CI runner): the LAN tier reaches tunnel-mode hosts, never admin surfaces
        ("automation", direct("router-automation", "router.chifor.me"),
         lambda r: expect(r, 200, be("router"), CLIENT_IP["automation"])),
        ("automation", direct("dsh-automation", "dsh.chifor.me", headers=[["X-Test-Auth", "allow"]]),
         lambda r: not_backend(r, 403)),
        ("automation", direct("dw1-automation", "dw1.chifor.me"), lambda r: not_backend(r, 403)),
        ("automation", direct("proxmox-automation", "proxmox.chifor.me"), lambda r: not_backend(r, 403)),
        ("automation", direct("vault-admin-automation", "vault.chifor.me", "/admin"), lambda r: not_backend(r, 403)),
        ("automation", direct("vault-automation", "vault.chifor.me", "/api/sync"), lambda r: expect(r, 200, be("vault"))),
        ("automation", {"id": "redir-automation", "port": 8081, "host": "router.chifor.me", "target": "/"},
         lambda r: expect(r, 308, location="https://router.chifor.me/")),
        ("trusted", direct("unknown", "nope.chifor.me"), lambda r: expect(r, 404)),
        ("trusted", direct("unknown-sni", "router.chifor.me", sni="other.example"),
         lambda r: [] if r.get("error") or r.get("status") in (200, 404) else [f"unexpected {r}"]),
        # SNI of one host, Host of another: routing and trust follow Host
        ("untrusted", direct("sni-mismatch", "dsh.chifor.me", sni="router.chifor.me"), lambda r: expect(r, 403)),
        # HTTPS backends: the public Host, and the configured SNI where the registry sets one
        ("trusted", direct("dw1-host-sni", "dw1.chifor.me"),
         lambda r: expect(r, 200, be("dw1"), T, present={"Host": "dw1.chifor.me"}, sni="dw1.chifor.me")),
        ("trusted", direct("proxmox-host", "proxmox.chifor.me"),
         lambda r: expect(r, 200, be("proxmox"), T, present={"Host": "proxmox.chifor.me"})),
        ("trusted", direct("openbao-host", "openbao.chifor.me"),
         lambda r: expect(r, 200, be("openbao"), T, present={"Host": "openbao.chifor.me"},
                          sni="openbao.openbao.svc.cluster.local")),
        # authelia host: anonymous, allowed (and what Authelia saw), denied (never reaches the backend)
        ("trusted", direct("dsh-anon", "dsh.chifor.me"), lambda r: expect(r, 302, location="sso.chifor.me")),
        ("trusted", direct("dsh-auth", "dsh.chifor.me", headers=[["X-Test-Auth", "allow"], ["Remote-User", "mallory"],
                                                                  ["CF-Connecting-IP", "6.6.6.6"]]),
         lambda r: expect(r, 200, be("dsh"), T, absent=["Remote-Extra"], present={
             "Remote-User": "alice", "Remote-Groups": "admins",
             "Remote-Name": f"cf={T};xff={T};proto=https;host=dsh.chifor.me"})),
        ("trusted", direct("dsh-nonadmin", "dsh.chifor.me", headers=[["X-Test-Auth", "deny"]]),
         lambda r: not_backend(r, 403)),
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
        ("trusted", direct("id-dslash", "id.chifor.me", "//realms/master/"), lambda r: expect(r, 404)),
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
        ("tunnel", tunnel("tun-bad-v4", "router.chifor.me", cf_ip="999.999.999.999"), lambda r: expect(r, 400)),
        ("tunnel", tunnel("tun-bad-v6", "router.chifor.me", cf_ip="1:2:3"), lambda r: expect(r, 400)),
        # forms Caddy's own parser would accept by stripping a port or a zone: refused by shape first
        ("tunnel", tunnel("tun-port", "router.chifor.me", cf_ip="203.0.113.9:443"), lambda r: not_backend(r, 400)),
        ("tunnel", tunnel("tun-zone", "router.chifor.me", cf_ip="fe80::1%eth0"), lambda r: not_backend(r, 400)),
        ("tunnel", tunnel("tun-brackets", "router.chifor.me", cf_ip="[2001:db8::1]"), lambda r: not_backend(r, 400)),
        ("tunnel", tunnel("tun-v6", "router.chifor.me", cf_ip="2001:db8::7"), lambda r: expect(r, 200, be("router"), "2001:db8::7")),
        ("tunnel", tunnel("tun-v4mapped", "router.chifor.me", cf_ip="::ffff:192.0.2.1"),
         lambda r: expect(r, 200, be("router")) + ([] if (echo_headers(r) or {}).get("cf-connecting-ip") in
                                                    ("192.0.2.1", "::ffff:192.0.2.1") else ["v4-mapped address not kept"])),
        ("tunnel", tunnel("tun-no-visitor", "router.chifor.me", visitor=None), lambda r: expect(r, 400)),
        ("tunnel", tunnel("tun-http", "router.chifor.me", "/x?y=1", visitor='{"scheme":"http"}'),
         lambda r: expect(r, 308, location="https://router.chifor.me/x?y=1")),
        ("tunnel", tunnel("tun-unknown", "nope.chifor.me"), lambda r: expect(r, 404)),
        ("tunnel", tunnel("tun-unknown-noip", "nope.chifor.me", cf_ip=None, visitor=None), lambda r: expect(r, 404)),
        ("tunnel", tunnel("tun-unknown-http", "nope.chifor.me", visitor='{"scheme":"http"}'), lambda r: expect(r, 404)),
        ("tunnel", tunnel("tun-vault-admin", "vault.chifor.me", "/admin"), lambda r: expect(r, 200, be("vault"))),
        ("tunnel", tunnel("tun-dsh-anon", "dsh.chifor.me"), lambda r: expect(r, 302, location="sso.chifor.me")),
        ("tunnel", tunnel("tun-dsh-auth", "dsh.chifor.me", headers=[["X-Test-Auth", "allow"]]),
         lambda r: expect(r, 200, be("dsh"), "203.0.113.7", present={
             "Remote-User": "alice", "Remote-Name": "cf=203.0.113.7;xff=203.0.113.7;proto=https;host=dsh.chifor.me"})),
        # a non-cloudflared peer on the tunnel port cannot choose its client address: the proxy
        # ignores its header (the peer is not a trusted proxy), so the client is the peer itself, and
        # "client equals peer" is exactly what the tunnel listener refuses. (In the cluster the
        # NetworkPolicy refuses the connection first: scripts/edge/netpol-check.sh.)
        ("untrusted", tunnel("tun-forged", "router.chifor.me", cf_ip="6.6.6.6"), lambda r: not_backend(r, 400)),
        # redirect listener and health
        ("trusted", {"id": "redir-lan", "port": 8081, "host": "router.chifor.me", "target": "/a?b=1"},
         lambda r: expect(r, 308, location="https://router.chifor.me/a?b=1")),
        ("trusted", {"id": "redir-unknown", "port": 8081, "host": "nope.chifor.me", "target": "/"}, lambda r: expect(r, 404)),
        ("untrusted", {"id": "redir-wan", "port": 8081, "host": "router.chifor.me", "target": "/"}, lambda r: expect(r, 403)),
        ("excluded", {"id": "redir-gw", "port": 8081, "host": "router.chifor.me", "target": "/"}, lambda r: expect(r, 403)),
        ("trusted", {"id": "health", "port": 8082, "host": "x", "target": "/healthz"}, lambda r: expect(r, 200)),
    ]


def sentinel_cases():
    """Credentials and query strings on successful AND failing requests, both listeners; the
    failing ones make Caddy write error logs (forward-auth unreachable, upstream unreachable)."""
    q = "/x?token=" + SENTINELS[3]
    hd = [["Authorization", "Bearer " + SENTINELS[2]], ["X-Api-Key", SENTINELS[1]],
          ["Cf-Access-Client-Secret", SENTINELS[4]]]
    return [
        ("trusted", direct("log-direct", "router.chifor.me", q, headers=hd), lambda r: expect(r, 200)),
        ("tunnel", tunnel("log-tunnel", "router.chifor.me", q, headers=hd + [["Cf-Access-Jwt-Assertion", SENTINELS[0]]]),
         lambda r: expect(r, 200)),
        ("trusted", direct("log-authfail", "dsh.chifor.me", q, headers=hd + [["X-Test-Auth", "allow"]]),
         lambda r: expect(r, 502)),
        ("tunnel", tunnel("log-authfail-tun", "dsh.chifor.me", q,
                          headers=hd + [["X-Test-Auth", "allow"], ["Cf-Access-Jwt-Assertion", SENTINELS[0]]]),
         lambda r: expect(r, 502)),
        ("trusted", direct("log-upstreamfail", "router.chifor.me", q, headers=hd), lambda r: expect(r, 502)),
        ("tunnel", tunnel("log-upstreamfail-tun", "router.chifor.me", q,
                          headers=hd + [["Cf-Access-Jwt-Assertion", SENTINELS[0]]]), lambda r: expect(r, 502)),
    ]


def variant_direct(reg):
    r = copy.deepcopy(reg)
    r["edge_default"] = "direct"
    r["hosts"]["test-private"] = {"exposure": "private", "auth": "native", "access_gated": False, "lan_dns": False,
                                  "backend": {"url": "http://private.example.svc:80"}, "smoke": {"status": 200}}
    for rp in r["hosts"]["vault"]["restricted_paths"]:
        rp["exposure"] = "private"  # P7's shape: /admin private, never through the tunnel
    errors = render.validate(r)
    if errors:
        raise RuntimeError(f"direct variant invalid: {errors}")
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
        ("automation", direct("d-priv-automation", "test-private.chifor.me"), lambda r: not_backend(r, 403)),
        ("automation", direct("d-router-automation", "router.chifor.me"),
         lambda r: expect(r, 200, be("router"), CLIENT_IP["automation"])),
        ("tunnel", tunnel("d-priv-tun", "test-private.chifor.me"), lambda r: expect(r, 404)),
        # vault direct: /admin private now - LAN-only on the direct listener, 404 through the tunnel
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
        ("tunnel", tunnel("d-vault-admin-tun", "vault.chifor.me", "/admin"), lambda r: expect(r, 404)),
        ("tunnel", tunnel("d-vault-admin-tun2", "vault.chifor.me", "/Admin/x"), lambda r: expect(r, 404)),
        ("tunnel", tunnel("d-vault-tun", "vault.chifor.me", "/api/sync"), lambda r: expect(r, 200, be("vault"))),
        ("untrusted", direct("d-canary", "edge-canary.chifor.me"),
         lambda r: [] if r.get("status") == 200 and f"peer={U}" in r["body"] else [f"canary {r}"]),
    ]


def run_suite(fx, label, suite):
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
            print(f"  {'ok  ' if not errs else 'FAIL'} [{label}] {src:9s} {case['id']}" +
                  ("" if not errs else f": {'; '.join(errs)}"))
            if errs:
                failures.append(case["id"])
    return failures


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} [{label}]" + (f" {detail}" if detail else ""))
    return [] if ok else [label]


def served_fingerprint():
    r = docker("run", "--rm", *labels(), "--network", f"{P}-trusted", "--entrypoint", "sh", IMG_SSL, "-c",
               f"echo | openssl s_client -connect {PROXY_IP['trusted']}:8443 -servername router.chifor.me 2>/dev/null "
               "| openssl x509 -noout -fingerprint -sha256", check=False)
    return r.stdout.strip()


def reloader_metrics(fx):
    p = fx.work / "config" / "reloader" / "metrics"
    return p.read_text() if p.exists() else ""


def scrape(fx, path, metric):
    """GET :9180<path> as Prometheus would: 200, the text exposition Content-Type, the metric."""
    r = fx.request("trusted", [{"id": "scrape", "port": 9180, "host": "x", "target": path}])
    if not r:
        return False, "no response"
    ctypes = r[0].get("headers", {}).get("content-type", [])
    ok = (r[0].get("status") == 200 and ctypes == [render.PROM_TEXT]
          and metric in (r[0].get("body") or ""))
    return ok, f"status {r[0].get('status')}, content-type {ctypes!r}"


def wait_for(pred, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(1)
    return False


def check_probe(fx):
    """The cert-probe sidecar reads the certificate Caddy SERVES (run here in the proxy's network
    namespace, as in the pod) and reports its expiry; the fixture certificates live 2 days."""
    docker("run", "-d", *labels(), *user_args(), "--name", f"{P}-probe", "--network", f"container:{P}-proxy",
           "-e", "PROBE_INTERVAL=2", "-v", f"{PROBE}:/probe.sh:ro", "-v", f"{fx.work / 'config'}:/config",
           "--entrypoint", "sh", IMG_SSL, "/probe.sh")
    path = fx.work / "config" / "reloader" / "probe"
    ok = wait_for(lambda: path.exists() and "edge_cert_probe_success 1" in path.read_text(), 30)
    detail = ""
    if ok:
        line = [ln for ln in path.read_text().splitlines() if ln.startswith("edge_cert_served_not_after_seconds ")]
        left = int(line[0].split()[1]) - int(time.time()) if line else -1
        ok = 86400 < left < 3 * 86400
        detail = f"served certificate expires in {left} s"
    failures = check("cert-probe reads the served certificate's expiry", ok, detail)
    return failures + check("/probe-metrics scrapes like Prometheus",
                            *scrape(fx, "/probe-metrics", "edge_cert_probe_success"))


def check_reloader(fx):
    """First pass loads what it finds; a mismatched or malformed pair is kept out and counted; a
    valid renewal is served after the bad one is repaired."""
    failures = []
    # a renewal that lands BEFORE the reloader starts must still be loaded on its first pass
    before = served_fingerprint()
    fx.make_cert("early", "*.chifor.me")
    for f in ("tls.key", "tls.crt"):
        shutil.copy(fx.work / "early" / f, fx.work / "certs" / f)
    docker("exec", "-d", "-e", "RELOAD_INTERVAL=1", f"{P}-proxy", "sh", "/reloader.sh")
    failures += check("reloader first pass loads a pair that changed before it started",
                      wait_for(lambda: served_fingerprint() not in ("", before)))
    failures += check("reloader heartbeat and loaded gauge exported",
                      wait_for(lambda: "edge_cert_files_not_loaded 0" in reloader_metrics(fx)
                               and "edge_cert_reloader_heartbeat_seconds" in reloader_metrics(fx)))
    failures += check("/reloader-metrics scrapes like Prometheus",
                      *scrape(fx, "/reloader-metrics", "edge_cert_reloader_heartbeat_seconds"))
    loaded = served_fingerprint()
    # a mismatched pair (new cert, old key): never loaded, counted, flagged not-loaded
    fx.make_cert("bad", "*.chifor.me", key_from="certs")
    shutil.copy(fx.work / "bad" / "tls.crt", fx.work / "certs" / "tls.crt")
    failures += check("mismatched pair kept out and flagged",
                      wait_for(lambda: "edge_cert_files_not_loaded 1" in reloader_metrics(fx)
                               and "edge_cert_invalid_total 0" not in reloader_metrics(fx))
                      and served_fingerprint() == loaded)
    # a malformed pair: garbage PEM
    (fx.work / "certs" / "tls.crt").write_text("-----BEGIN CERTIFICATE-----\nnot base64\n-----END CERTIFICATE-----\n")
    time.sleep(4)
    failures += check("malformed pair kept out", served_fingerprint() == loaded)
    # repaired with a valid renewal: served
    fx.make_cert("good", "*.chifor.me")
    for f in ("tls.key", "tls.crt"):
        shutil.copy(fx.work / "good" / f, fx.work / "certs" / f)
    failures += check("valid renewal served after repair",
                      wait_for(lambda: served_fingerprint() not in ("", loaded))
                      and wait_for(lambda: "edge_cert_files_not_loaded 0" in reloader_metrics(fx)))
    return failures


def main() -> int:
    if shutil.which("docker") is None:
        print("edge_runtime: docker is required", file=sys.stderr)
        return 2

    def _terminate(signum, _frame):  # a cancelled CI job: let `finally` clean up this run
        raise SystemExit(128 + signum)
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            signal.signal(sig, _terminate)

    if not probe_image_matches():
        print(f"edge_runtime: daemonset.yaml's cert-probe image is not {IMG_SSL}", file=sys.stderr)
        return 1
    reg = render.load()
    work = pathlib.Path(tempfile.mkdtemp(prefix=f"{P}-"))
    fx = Fixture(work)
    failures = []
    try:
        fx.up()
        print(f"edge runtime fixture {RUN}: registry as committed")
        fx.start_proxy(render.render(reg, PROFILE))
        failures += run_suite(fx, "committed", suite_committed(reg))
        print("edge runtime fixture: edge_default direct, a private host, vault /admin private")
        fx.start_proxy(render.render(variant_direct(reg), PROFILE))
        failures += run_suite(fx, "direct", suite_direct(variant_direct(reg)))
        print("edge runtime fixture: failures (auth service and upstreams down) and log redaction")
        fx.start_proxy(render.render(reg, PROFILE))
        docker("stop", f"{P}-auth")
        docker("stop", f"{P}-echo")
        failures += run_suite(fx, "fail", sentinel_cases()[2:])
        docker("start", f"{P}-echo")
        time.sleep(1)
        failures += run_suite(fx, "logs", sentinel_cases()[:2])
        text = fx.logs()
        leaked = [s for s in SENTINELS if s in text]
        failures += check("no credential or query sentinel in the access or error log", not leaked,
                          f"leaked {leaked}" if leaked else "")
        failures += check("the error log was exercised", '"logger":"http.log.error' in text)
        failures += check_probe(fx)
        failures += check_reloader(fx)
    finally:
        if failures:
            print(fx.logs()[-4000:])
        fx.down()
        shutil.rmtree(work, ignore_errors=True)
    print(f"edge runtime fixture: {'FAILED ' + str(len(failures)) if failures else 'all passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
