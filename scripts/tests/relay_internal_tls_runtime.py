#!/usr/bin/env python3
"""Runtime fixture for the Relay researcher internal TLS terminators (kubernetes/apps/relay-internal-tls):
the pinned Caddy runs the committed hub.Caddyfile and router.Caddyfile, under the Pods' own security
settings, and real requests check what it DOES with them. Needs docker. Run from the repo root:

    python3 scripts/tests/relay_internal_tls_runtime.py

Per terminator: a worker's forged client-address headers (CF-Connecting-IP, X-Forwarded-For,
X-Real-IP, Forwarded, True-Client-IP, Cf-*, Cdn-Loop, Remote-*) never reach the upstream, which instead
sees the TCP peer in CF-Connecting-IP / X-Forwarded-For / X-Real-IP; the worker's Authorization does
reach it; the request goes to the right upstream; another Host answers 421; another SNI, no SNI and
plain HTTP are never proxied; the JSON access log carries ts, status, request.remote_ip and
request.host, and no credential or query string. Then the cert-reloader sidecar picks up a renewed
certificate without a restart.

Caddy runs as UID 1000 with every capability dropped but NET_BIND_SERVICE, no-new-privileges and a
read-only root, as terminators.yaml sets it, so binding 443 is proven under the Pods' settings. The
upstream Service names are docker network aliases; their port 80 is rewritten to the stand-in's 8080
(asserted to occur exactly once). NOT covered here, by construction: NetworkPolicy and cert-manager.
"""
from __future__ import annotations

import json
import pathlib
import random
import secrets
import shutil
import sys
import tempfile
import time

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
TREE = ROOT / "kubernetes/apps/relay-internal-tls"
CLIENT = ROOT / "scripts/tests/edge_runtime"  # client.py: raw requests with chosen SNI, Host, headers
SUPPORT = pathlib.Path(__file__).resolve().parent / "relay_internal_tls_runtime"
sys.path.insert(0, str(ROOT / "scripts/tests"))
import edge_runtime as er  # noqa: E402  docker(), probe_image(), IMG_PY

LABEL = "ailab.relay-internal-tls-runtime"
RUN = secrets.token_hex(4)
P = f"ritls-{RUN}"
SENTINELS = ("SENTINEL-TOKEN-6b2e", "SENTINEL-QUERY-91f0")
# name -> (public name, Caddyfile, upstream Service host)
TERMINATORS = {
    "hub": ("relay.chifor.me", "hub.Caddyfile", "relay.relay.svc.cluster.local"),
    "router": ("router.chifor.me", "router.Caddyfile", "llm-router-edge.llm-router.svc.cluster.local"),
}
FORGED = [["CF-Connecting-IP", "203.0.113.66"], ["X-Forwarded-For", "203.0.113.66"],
          ["X-Real-IP", "203.0.113.66"], ["Forwarded", "for=203.0.113.66"],
          ["True-Client-IP", "203.0.113.66"], ["Cf-Ray", "forged-ray"], ["Cf-IPCountry", "XX"],
          ["Cdn-Loop", "cloudflare"], ["Remote-User", "admin"], ["X-Forwarded-Proto", "http"],
          ["X-Forwarded-Host", "evil.example"]]
DROPPED = ("forwarded", "true-client-ip", "cf-ray", "cf-ipcountry", "cdn-loop", "remote-user")


def labels():
    return ["--label", f"{LABEL}={RUN}"]


def caddy_image() -> str:
    images = set()
    for doc in yaml.safe_load_all((TREE / "terminators.yaml").read_text(encoding="utf-8")):
        if doc and doc.get("kind") == "Deployment":
            images |= {c["image"] for c in doc["spec"]["template"]["spec"]["containers"]}
    if len(images) != 1:
        sys.exit(f"terminators.yaml: expected one image for every container, found {sorted(images)}")
    return images.pop()


class Fixture:
    def __init__(self, work: pathlib.Path):
        self.work = work
        self.caddy = caddy_image()
        self.ip: dict = {}

    def up(self):
        for _ in range(20):
            k, j = random.randrange(256), random.randrange(256)
            nets = {"client": f"198.19.{k}", "back": f"198.18.{j}"}
            made = []
            for name, prefix in nets.items():
                r = er.docker("network", "create", *labels(), "--subnet", f"{prefix}.0/24", f"{P}-{name}", check=False)
                if r.returncode != 0:
                    break
                made.append(name)
            if len(made) == len(nets):
                break
            for name in made:
                er.docker("network", "rm", f"{P}-{name}", check=False)
        else:
            raise RuntimeError("no free 198.18.0.0/15 subnets for the fixture")
        self.ip = {"client": f"{nets['client']}.100", "hub": f"{nets['client']}.10",
                   "router": f"{nets['client']}.11", "prefix": nets["client"]}
        for name, (_, _, upstream) in TERMINATORS.items():
            er.docker("run", "-d", *labels(), "--name", f"{P}-echo-{name}", "--network", f"{P}-back",
                      "--network-alias", upstream, "-v", f"{SUPPORT}:/t:ro", er.IMG_PY,
                      "python", "/t/echo.py", name)
        for name, (public, caddyfile, upstream) in TERMINATORS.items():
            d = self.work / name
            (d / "etc").mkdir(parents=True)
            text = (TREE / caddyfile).read_text(encoding="utf-8")
            needle = f"reverse_proxy http://{upstream}:80 {{"
            if text.count(needle) != 1:
                sys.exit(f"{caddyfile}: expected exactly one `{needle}`")
            (d / "etc" / "Caddyfile").write_text(text.replace(needle, f"reverse_proxy http://{upstream}:8080 {{"))
            shutil.copy(TREE / "cert-reloader.sh", d / "etc" / "cert-reloader.sh")
            self.make_cert(d / "certs", public)
            self.start(name)

    def make_cert(self, d: pathlib.Path, name: str):
        d.mkdir(parents=True, exist_ok=True)
        er.docker("run", "--rm", *er.user_args(), "-v", f"{d}:/o", er.probe_image(), "req", "-x509",
                  "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "2",
                  "-subj", f"/CN={name}", "-addext", f"subjectAltName=DNS:{name}",
                  "-keyout", "/o/tls.key", "-out", "/o/tls.crt")
        for path in [d, *d.iterdir()]:
            path.chmod(0o755 if path.is_dir() else 0o644)

    def pod_args(self, name: str):
        d = self.work / name
        return ["--user", "1000:1000", "--cap-drop", "ALL", "--cap-add", "NET_BIND_SERVICE",
                "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/data", "--tmpfs", "/config",
                "-e", "XDG_DATA_HOME=/data", "-e", "XDG_CONFIG_HOME=/config",
                "-v", f"{d / 'etc'}:/etc/caddy:ro", "-v", f"{d / 'certs'}:/certs:ro"]

    def start(self, name: str):
        for path in (self.work, self.work / name, self.work / name / "etc"):
            path.chmod(0o755)
        for f in (self.work / name / "etc").iterdir():
            f.chmod(0o644)
        er.docker("create", *labels(), "--name", f"{P}-{name}", "--network", f"{P}-client",
                  "--ip", self.ip[name], *self.pod_args(name), self.caddy,
                  "caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile")
        er.docker("network", "connect", f"{P}-back", f"{P}-{name}")
        er.docker("start", f"{P}-{name}")
        public = TERMINATORS[name][0]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            r = self.request(name, [case("up", public)])
            if r and r[0].get("status") == 200:
                return
            time.sleep(0.5)
        raise RuntimeError(f"{name} did not come up:\n" + self.logs(name)[-3000:])

    def start_reloader(self, name: str):
        er.docker("run", "-d", *labels(), "--name", f"{P}-{name}-reloader", "--network", f"container:{P}-{name}",
                  *self.pod_args(name), "-e", "RELOAD_INTERVAL=1", self.caddy, "sh", "/etc/caddy/cert-reloader.sh")

    def request(self, name: str, cases: list):
        f = self.work / f"cases-{name}.json"
        f.write_text(json.dumps(cases))
        f.chmod(0o644)
        r = er.docker("run", "--rm", *labels(), "--network", f"{P}-client", "--ip", self.ip["client"],
                      "-v", f"{CLIENT}:/t:ro", "-v", f"{f}:/cases.json:ro", er.IMG_PY,
                      "python", "/t/client.py", "/cases.json", self.ip[name], check=False)
        return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]

    def served_fingerprint(self, name: str) -> str:
        r = er.docker("run", "--rm", *labels(), "--network", f"{P}-client", "--entrypoint", "sh", er.probe_image(), "-c",
                      f"echo | openssl s_client -connect {self.ip[name]}:443 -servername {TERMINATORS[name][0]} "
                      "2>/dev/null | openssl x509 -noout -fingerprint -sha256", check=False)
        return r.stdout.strip()

    def file_fingerprint(self, name: str) -> str:
        r = er.docker("run", "--rm", "-v", f"{self.work / name / 'certs'}:/c:ro", "--entrypoint", "sh",
                      er.probe_image(), "-c", "openssl x509 -noout -fingerprint -sha256 -in /c/tls.crt")
        return r.stdout.strip()

    def logs(self, container: str) -> str:
        r = er.docker("logs", f"{P}-{container}", check=False)
        return r.stdout + r.stderr

    def stdout(self, container: str) -> str:
        return er.docker("logs", f"{P}-{container}", check=False).stdout

    def down(self):
        r = er.docker("ps", "-aq", "--filter", f"label={LABEL}={RUN}", check=False)
        for cid in r.stdout.split():
            er.docker("rm", "-f", cid, check=False)
        r = er.docker("network", "ls", "-q", "--filter", f"label={LABEL}={RUN}", check=False)
        for nid in r.stdout.split():
            er.docker("network", "rm", nid, check=False)


def case(id_, host, sni="same", tls=True, headers=(), target="/v1/models"):
    return {"id": id_, "port": 443, "tls": tls, "sni": host if sni == "same" else sni, "host": host,
            "target": target, "headers": [list(h) for h in headers]}


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{(': ' + detail) if detail and not ok else ''}")
    return [] if ok else [label]


def suite(fx: Fixture, name: str) -> list:
    public, _, _ = TERMINATORS[name]
    other = next(v[0] for k, v in TERMINATORS.items() if k != name)
    client = fx.ip["client"]
    auth = ["Authorization", f"Bearer {SENTINELS[0]}"]
    results = {r["id"]: r for r in fx.request(name, [
        case("forged", public, headers=FORGED + [auth], target=f"/v1/models?key={SENTINELS[1]}"),
        case("wrong-host", other, sni=public),
        case("wrong-sni", other),
        case("no-sni", public, sni=None),
        case("plain-http", public, tls=False),
    ])}
    failures = []
    r = results.get("forged", {})
    failures += check(f"{name}: a valid request is proxied", r.get("status") == 200, json.dumps(r)[:400])
    body = json.loads(r.get("body") or "{}") if r.get("status") == 200 else {}
    seen: dict = {}
    for k, v in body.get("headers", []):
        seen.setdefault(k.lower(), []).append(v)
    failures += check(f"{name}: reaches its own upstream", body.get("backend") == name, str(body.get("backend")))
    for header in ("cf-connecting-ip", "x-forwarded-for", "x-real-ip"):
        failures += check(f"{name}: {header} is the TCP peer, never the forged value", seen.get(header) == [client],
                          str(seen.get(header)))
    failures += check(f"{name}: X-Forwarded-Proto https", seen.get("x-forwarded-proto") == ["https"],
                      str(seen.get("x-forwarded-proto")))
    failures += check(f"{name}: X-Forwarded-Host and Host are the public name",
                      seen.get("x-forwarded-host") == [public] and seen.get("host") == [public],
                      f"{seen.get('x-forwarded-host')} {seen.get('host')}")
    for header in DROPPED:
        failures += check(f"{name}: forged {header} removed", header not in seen, str(seen.get(header)))
    failures += check(f"{name}: the worker's Authorization reaches the upstream",
                      seen.get("authorization") == [auth[1]], str(seen.get("authorization")))
    failures += check(f"{name}: no Server header", "server" not in r.get("headers", {}))
    failures += check(f"{name}: another Host answers 421", results.get("wrong-host", {}).get("status") == 421,
                      json.dumps(results.get("wrong-host"))[:300])
    for id_ in ("wrong-sni", "no-sni", "plain-http"):
        got = results.get(id_, {})
        failures += check(f"{name}: {id_} is never proxied",
                          got.get("status") != 200 and '"backend"' not in got.get("body", ""),
                          json.dumps(got)[:300])
    time.sleep(1)
    access = []
    for line in fx.stdout(name).splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and str(entry.get("logger", "")).startswith("http.log.access"):
            access.append(entry)
    mine = [e for e in access if e.get("request", {}).get("remote_ip") == client
            and e.get("request", {}).get("host") == public and e.get("status") == 200]
    failures += check(f"{name}: JSON access log on stdout with ts, status, request.remote_ip, request.host",
                      bool(mine) and all(isinstance(e.get("ts"), (int, float)) for e in mine), f"{len(access)} entries")
    text = fx.logs(name)
    leaked = [s for s in SENTINELS if s in text]
    failures += check(f"{name}: no credential or query string in the logs", not leaked, str(leaked))
    return failures


def reloader(fx: Fixture) -> list:
    name = "hub"
    fx.start_reloader(name)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and '"cert_reloader":"loaded"' not in fx.stdout(f"{name}-reloader"):
        time.sleep(0.5)
    failures = check("reloader: first pass loads the mounted pair",
                     '"cert_reloader":"loaded"' in fx.stdout(f"{name}-reloader"), fx.logs(f"{name}-reloader")[-500:])
    before = fx.served_fingerprint(name)
    fresh = fx.work / "renewed"
    fx.make_cert(fresh, TERMINATORS[name][0])
    for f in ("tls.key", "tls.crt"):
        shutil.copy(fresh / f, fx.work / name / "certs" / f)
    want = fx.file_fingerprint(name)
    deadline = time.monotonic() + 30
    served = before
    while time.monotonic() < deadline:
        served = fx.served_fingerprint(name)
        if served == want:
            break
        time.sleep(1)
    failures += check("reloader: a renewed certificate is served without a restart",
                      bool(want) and served == want and served != before, f"before {before} served {served} want {want}")
    return failures


def main() -> int:
    work = pathlib.Path(tempfile.mkdtemp(prefix="relay-internal-tls-runtime-"))
    fx = Fixture(work)
    failures: list = []
    try:
        fx.up()
        for name in TERMINATORS:
            print(f"relay internal TLS runtime fixture: {name} ({TERMINATORS[name][0]})")
            failures += suite(fx, name)
        print("relay internal TLS runtime fixture: certificate renewal")
        failures += reloader(fx)
    finally:
        if failures:
            for name in TERMINATORS:
                print(fx.logs(name)[-3000:])
        fx.down()
        shutil.rmtree(work, ignore_errors=True)
    print(f"relay internal TLS runtime fixture: {'FAILED ' + str(len(failures)) if failures else 'all passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
