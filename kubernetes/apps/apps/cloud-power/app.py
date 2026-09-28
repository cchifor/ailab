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

OFF IS SCHEDULED, NOT IMMEDIATE (plans/2026-09-28-cloud-power-scheduled-drain-plan.md)
  The hosts also carry the opportunistic Gitea CI runners (cloud-ci-N, ADR 0032). Confirming OFF
  PAUSES those runners in Gitea (PATCH .../actions/runners/{id} disabled=true: Gitea stops handing
  them tasks, a running task carries on), waits until no cloud runner has a job in flight, and only
  then asks the nodes to shut down. Once each paused runner has gone offline it is re-enabled, so
  the pool is whole again whenever the hosts wake. The schedule lives in the cloud-power-state
  ConfigMap, so a pod restart resumes it instead of forgetting it (or stranding runners paused).
  The previous design drained inside the host shutdown with a fixed 10-minute cap and cancelled
  the long tail (2026-09-24: a job on cloud-ci-3 was cancelled at exactly 10 min).
"""
import copy
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
import urllib.error
import urllib.parse
import urllib.request
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

# --- the scheduled OFF (MODE=api only) -------------------------------------------------------
# In-cluster Gitea, not git.chifor.me: that path leaves the cluster through Cloudflare.
GITEA_URL = os.environ.get("GITEA_URL", "http://gitea-http.gitea.svc.cluster.local:3000")
GITEA_ORG = os.environ.get("GITEA_ORG", "cchifor")
GITEA_TOKEN = os.environ.get("GITEA_TOKEN", "")  # org OWNER, scope write:organization
CLOUD_RUNNER_PREFIX = os.environ.get("CLOUD_RUNNER_PREFIX", "cloud-ci-")
DRAIN_POLL_SEC = int(os.environ.get("DRAIN_POLL_SEC", "20"))
# act_runner's job timeout (gitea_runner_job_timeout, 3h) + margin. Past it, nothing that was
# running when OFF was confirmed can still be alive, so the deadline cannot cut a legitimate job;
# it only bounds a drain stuck on an unreachable Gitea.
DRAIN_MAX_SEC = int(os.environ.get("DRAIN_MAX_SEC", "11700"))
# How long after the shutdown request a host that accepted it (or MAY have: a POST whose reply was
# lost) may stay up before the OFF stalls. Deliberately past systemd's poweroff.target
# JobTimeoutSec (30 min, then poweroff-force): by then a host that is still up is not in the middle
# of shutting down, so CANCEL can hand its runners back without feeding a job to a dying VM.
OFFLINE_WAIT_SEC = int(os.environ.get("OFFLINE_WAIT_SEC", "2400"))
# Consecutive all-clear polls before powering off: one poll can straddle the moment a task is
# being handed out.
DRAIN_CLEAR_POLLS = int(os.environ.get("DRAIN_CLEAR_POLLS", "2"))
STATE_NAMESPACE = os.environ.get("STATE_NAMESPACE", "cloud-power")
STATE_CONFIGMAP = os.environ.get("STATE_CONFIGMAP", "cloud-power-state")
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
# Browser origins allowed to POST (CSRF). CANCEL and ON change state without a confirm token.
ALLOWED_ORIGINS = {o.strip().rstrip("/") for o in
                   os.environ.get("ALLOWED_ORIGINS", "https://home.chifor.me").split(",") if o.strip()}

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
        except PinError as e:
            # Raised BEFORE anything was sent: the host certainly did not get the request.
            out.append({"node": n["name"], "result": "REFUSED (not sent): %s" % e})
            log("shutdown %s NOT SENT: %s" % (n["name"], e))
        except Exception as e:
            # The request may have been accepted before the reply was lost: treat as uncertain.
            out.append({"node": n["name"], "result": "ERROR (outcome unknown): %s" % e})
            log("shutdown %s FAILED (outcome unknown): %s" % (n["name"], e))
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


# --- Gitea: pause / resume the cloud runners -------------------------------------------------
class GiteaError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib copies the Authorization header onto a redirect, to whatever host it points at.
    # Nothing this client calls redirects, so a redirect is an error, never a hop.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
# Field -> accepted types. A missing or mistyped field refuses the WHOLE list: it must never read
# as "not busy", "no runner" or "nothing running".
RUNNER_FIELDS = {"id": (int,), "name": (str,), "disabled": (bool,), "busy": (bool,), "status": (str,)}
# runner_name may be empty/null for a running job whose runner row is gone; the drain counts such a
# job as in flight (it could be on a cloud runner) rather than dropping it.
JOB_FIELDS = {"runner_name": (str, type(None)), "name": (str,)}


class Gitea:
    """The few org-scope calls the drain needs. Every failure RAISES: a caller that could not
    tell whether a job is running must treat that as 'not drained', never as an all-clear.
    Compatibility baseline: Gitea 1.26.1 (runner `disabled`/`busy`/`status`, org jobs endpoint).
    `transport(method, url, headers, body) -> (status, bytes)` is injectable for the tests."""

    def __init__(self, base, org, token, timeout=15, transport=None):
        self.base = base.rstrip("/")
        self.org = org
        self.token = token
        self.timeout = timeout
        self.transport = transport or self._urllib

    def _urllib(self, method, url, headers, body):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with _OPENER.open(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            # The message never carries the token (it is a header, not part of the URL).
            raise GiteaError("%s %s: %s" % (method, url.split("?")[0], type(e).__name__)) from e

    def _call(self, method, path, params=None, body=None):
        if not self.token:
            raise GiteaError("no Gitea token configured")
        url = self.base + "/api/v1" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": "token " + self.token, "Accept": "application/json",
                   "User-Agent": "git/2 cloud-power"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, raw = self.transport(method, url, headers, data)
        if status >= 300:
            raise GiteaError("%s %s -> HTTP %d" % (method, path, status), status)
        try:
            return json.loads(raw or b"null")
        except ValueError as e:
            raise GiteaError("%s %s: response is not JSON" % (method, path)) from e

    def _paged(self, path, key, fields, params=None, limit=50, max_pages=40):
        """Every page must carry `key` and an integer `total_count`, and the pages must add up to
        that total. Anything else - a missing collection, an empty page before the total, a
        mistyped field - is an error, never a shorter list."""
        out = []
        for page in range(1, max_pages + 1):
            q = dict(params or {})
            q.update(page=page, limit=limit)
            d = self._call("GET", path, q)
            if not isinstance(d, dict) or key not in d:
                raise GiteaError("%s: response has no %r (API changed?)" % (path, key))
            total = d.get("total_count")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise GiteaError("%s: response has no usable total_count" % path)
            items = d[key]
            if items is None and total == 0:
                items = []                           # Go encodes an empty slice as null
            if not isinstance(items, list):
                raise GiteaError("%s: %r is not a list" % (path, key))
            for it in items:
                if not isinstance(it, dict) or any(
                        f not in it or not isinstance(it[f], ts) or (isinstance(it[f], bool) and bool not in ts)
                        for f, ts in fields.items()):
                    raise GiteaError("%s: an item lacks or mistypes one of %s (API changed?)"
                                     % (path, ",".join(fields)))
            out.extend(items)
            if len(out) >= total:
                return out
            if not items:
                raise GiteaError("%s: page %d is empty but total_count says %d, got %d"
                                 % (path, page, total, len(out)))
        # A truncated list could hide the one runner or job that matters: refuse it.
        raise GiteaError("%s: more than %d pages, refusing a partial list" % (path, max_pages))

    def runners(self):
        return self._paged("/orgs/%s/actions/runners" % self.org, "runners", RUNNER_FIELDS)

    def set_disabled(self, runner_id, disabled):
        self._call("PATCH", "/orgs/%s/actions/runners/%d" % (self.org, int(runner_id)),
                   body={"disabled": bool(disabled)})

    def running_jobs(self):
        return self._paged("/orgs/%s/actions/jobs" % self.org, "jobs", JOB_FIELDS,
                           {"status": "in_progress"})


# --- schedule state ConfigMap (the ci-rerun-watchdog transport, not its merge-on-conflict) ----
class StateError(Exception):
    pass


class StateConflict(StateError):
    pass


class StateUncertain(StateError):
    """A write whose outcome is unknown (it may have landed). Memory is discarded and the next
    tick re-reads the ConfigMap and acts on what is ACTUALLY stored - never on a guess."""


class KubeState:
    """cloud-power-state through the in-cluster API with the projected SA token. Created at
    runtime, NOT in git, so Flux prune never removes a schedule that is in flight.
    read() -> (data|None, resourceVersion|None); write(data, rv) -> new rv (rv None creates)."""

    def __init__(self, namespace, name, sa_dir=SA_DIR, timeout=10):
        self.namespace = namespace
        self.name = name
        self.host = "https://%s:%s" % (os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc"),
                                       os.environ.get("KUBERNETES_SERVICE_PORT", "443"))
        self.sa_dir = sa_dir
        self.timeout = timeout
        self._ctx = None

    def _call(self, method, path, body=None):
        try:
            if self._ctx is None:
                self._ctx = ssl.create_default_context(cafile=os.path.join(self.sa_dir, "ca.crt"))
            with open(os.path.join(self.sa_dir, "token")) as f:  # re-read: the kubelet rotates it
                tok = f.read().strip()
        except OSError as e:
            raise StateError("service account token/CA unreadable: %s" % e) from e
        req = urllib.request.Request(
            self.host + path, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json",
                     "Accept": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            raise StateError("%s configmap: %s: %s" % (method, type(e).__name__, e)) from e

    def _path(self, named=True):
        p = "/api/v1/namespaces/%s/configmaps" % self.namespace
        return p + "/" + self.name if named else p

    def read(self):
        status, raw = self._call("GET", self._path())
        if status == 404:
            return None, None
        if status != 200:
            raise StateError("GET configmap: HTTP %s" % status)
        obj = json.loads(raw)
        return dict(obj.get("data") or {}), obj.get("metadata", {}).get("resourceVersion")

    def write(self, data, rv):
        body = {"apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": self.name, "namespace": self.namespace,
                             "labels": {"app.kubernetes.io/name": "cloud-power",
                                        "app.kubernetes.io/managed-by": "cloud-power"}},
                "data": {k: str(v) for k, v in data.items()}}
        if rv is None:
            status, raw = self._call("POST", self._path(named=False), body)
        else:
            body["metadata"]["resourceVersion"] = str(rv)
            status, raw = self._call("PUT", self._path(), body)
        if status == 409:
            raise StateConflict("configmap write: HTTP 409 (written by someone else)")
        if status not in (200, 201):
            raise StateError("configmap write: HTTP %s" % status)
        return json.loads(raw).get("metadata", {}).get("resourceVersion")


# --- the schedule ----------------------------------------------------------------------------
class Refused(Exception):
    """The request is not allowed in the current phase (HTTP 409)."""


STATE_VERSION = 1
# The shut-down hosts must stay unreachable this long, continuously, before their runners are
# handed back. Two quick misses could be a network blip that also makes the runners look offline;
# an idle cloud host finishes its shutdown well inside this window.
HOST_DOWN_SEC = 300


def host_up(name):
    for n in NODES:
        if n["name"] == name:
            return node_up(n["ip"])
    return False


class Scheduler:
    """idle -> draining -> powering_off -> idle; releasing and stalled are the ways back out.

      draining      the owned cloud runners are paused in Gitea; wait until no cloud runner has a
                    job in flight (DRAIN_CLEAR_POLLS consecutive clear ticks).
      powering_off  node shutdown sent; an owned runner is re-enabled once every node that accepted
                    the shutdown is seen down AND Gitea reports the runner offline.
      stalled       something did not happen in time (drain deadline, a host that stays up, every
                    shutdown call failed, a stale unsent shutdown). Runners STAY paused; the page
                    shows why; CANCEL hands them back.
      releasing     re-enable every owned runner (retried per tick), then idle.

    Transitions are PERSISTED BEFORE THEY ACT, so a restart at any point resumes the same phase
    and repeats at most an idempotent call (a PATCH to the same value, or a shutdown that is still
    fresh). Only runners this schedule paused are ever re-enabled: one already disabled when OFF
    was confirmed is recorded as skipped and left alone."""

    def __init__(self, gitea, store, shutdown_fn, host_up_fn=host_up, clock=time.time,
                 prefix=CLOUD_RUNNER_PREFIX, drain_max=DRAIN_MAX_SEC, offline_wait=OFFLINE_WAIT_SEC,
                 clear_polls=DRAIN_CLEAR_POLLS):
        self.gitea = gitea
        self.store = store
        self.shutdown_fn = shutdown_fn
        self.host_up_fn = host_up_fn
        self.clock = clock
        self.prefix = prefix
        self.drain_max = drain_max
        self.offline_wait = offline_wait
        self.clear_polls = clear_polls
        self.lock = threading.RLock()
        self.state = None      # the persisted schedule, or None when idle
        self.last = None       # the last finished schedule's outcome, for the page
        self.rv = None
        self.loaded = False
        self.corrupt = ""      # non-empty: the state could not be trusted; everything refuses
        self.clear = 0         # consecutive all-clear ticks (in memory: a restart re-counts)
        self.down_since = None  # since when every shut-down host has been unreachable (in memory)
        self.inflight = []     # what the drain is waiting for, for the page
        self.note = ""         # transient condition worth showing (e.g. Gitea unreachable)
        self.last_tick = None
        self.dirty = False     # memory is ahead of the ConfigMap: no side effects until it is saved
        self.view = {"phase": "unknown", "loaded": False}

    # -- persistence ----------------------------------------------------------------------
    def load(self):
        with self.lock:
            data, rv = self.store.read()
            data = data or {}
            self.corrupt = ""
            try:
                state = json.loads(data["schedule"]) if data.get("schedule") else None
                last = json.loads(data["last"]) if data.get("last") else None
            except ValueError:
                state, last = None, None
                self.corrupt = "the state ConfigMap does not parse"
            if state is not None and (not isinstance(state, dict) or state.get("v") != STATE_VERSION):
                self.corrupt = "unsupported schedule state (v=%r)" % (state.get("v") if isinstance(state, dict) else "?")
            self.state = None if self.corrupt else state
            self.last = last
            self.rv = rv
            self.loaded = True
            self.clear = 0
            self.down_since = None
            if self.corrupt:
                log("STATE UNUSABLE, refusing to act until fixed: %s" % self.corrupt)
            elif self.state:
                log("RESUMING a %s schedule (by %s)" % (self.state.get("phase"), self.state.get("by")))

    def _save(self):
        try:
            self.rv = self.store.write({"schedule": json.dumps(self.state) if self.state else "",
                                        "last": json.dumps(self.last) if self.last else ""}, self.rv)
            self.dirty = False
        except StateConflict:
            # Someone else wrote it. Never overwrite: re-read and re-evaluate on the next tick.
            self.dirty = False
            self.loaded = False
            raise
        except StateError:
            self.dirty = True
            raise

    def _save_quiet(self):
        """Persist; on failure mark the state dirty. While dirty, tick() retries the write FIRST
        and performs no other side effect, so the ConfigMap never lags a completed action for
        longer than one failed write, and a restart can never act on an older decision."""
        try:
            self._save()
            return True
        except StateError as e:
            self.note = "state not persisted: %s" % e
            log("state write FAILED (will retry before anything else): %s" % e)
            return False

    def publish(self):
        """An immutable snapshot for /api/status, so a status poll never waits on a tick that is
        in the middle of a slow Gitea or PVE call."""
        st = self.state
        if self.corrupt:
            phase = "unknown"
        elif st:
            phase = st["phase"]
        else:
            phase = "idle" if self.loaded else "unknown"
        v = {"phase": phase, "loaded": self.loaded, "now": self.clock(), "last": self.last,
             "note": self.corrupt or self.note, "last_tick": self.last_tick}
        if st:
            v.update({k: st.get(k) for k in ("by", "at", "deadline", "reason", "powering_off_at",
                                              "skipped", "stall")})
            v["runners"] = [r["name"] for r in st.get("runners", [])]
            if st["phase"] == "draining":
                v["inflight"] = self.inflight
        self.view = copy.deepcopy(v)
        return self.view

    # -- helpers --------------------------------------------------------------------------
    def _is_cloud(self, name):
        return bool(name) and name.startswith(self.prefix)

    def inflight_jobs(self, runners=None):
        """In-flight work on ANY cloud runner (owned or skipped: an operator-disabled runner can
        still be finishing a job). The job list is authoritative; `busy` (LastActive < 10 s,
        seen flapping mid-job) only ADDS to it. Raises GiteaError."""
        jobs = self.gitea.running_jobs()
        # A running job whose runner cannot be named could be on a cloud runner: count it.
        out = [{"runner": j["runner_name"] or "(unknown runner)", "job": j["name"] or "?",
                "started_at": j.get("started_at"), "url": j.get("html_url")}
               for j in jobs if not j["runner_name"] or self._is_cloud(j["runner_name"])]
        if runners is not None:
            named = {j["runner"] for j in out}
            out += [{"runner": r.get("name"), "job": "(busy)", "started_at": None, "url": None}
                    for r in runners
                    if self._is_cloud(r.get("name")) and r.get("busy") and r.get("name") not in named]
        return out

    def _finish(self, result, message):
        self.last = {"at": self.clock(), "result": result, "message": message}
        self.state = None
        self.clear = 0
        self.down_since = None
        self.inflight = []
        log("schedule finished: %s - %s" % (result, message))
        self._save_quiet()

    def _stall(self, message, cancel_after=None):
        """cancel_after: before this time CANCEL/ON must NOT hand the runners back, because a host
        may still be shutting down (a runner re-enabled now could take a job and die with it)."""
        now = self.clock()
        self.state["phase"] = "stalled"
        self.state["stall"] = {"at": now, "message": message,
                               "cancel_after": max(now, cancel_after or now)}
        self.inflight = []
        log("STALLED (runners stay paused until CANCEL): %s" % message)
        self._save_quiet()

    def _to_releasing(self, result, message):
        self.state["phase"] = "releasing"
        self.state["outcome"] = {"result": result, "message": message}
        log("releasing paused runners (%s): %s" % (result, message))
        if self._save_quiet():                       # persisted BEFORE re-enabling anything
            self._tick_releasing()

    def _require_usable(self):
        if not self.loaded:
            self.load()                              # StateError -> the caller answers 503
        if self.corrupt:
            raise StateError(self.corrupt)

    # -- operator actions -----------------------------------------------------------------
    def schedule(self, who):
        with self.lock:
            self._require_usable()
            if self.state:
                raise Refused("an OFF is already %s" % self.state["phase"])
            cloud = [r for r in self.gitea.runners() if self._is_cloud(r["name"])]
            if not cloud:
                raise Refused("no %s* runner is registered in %s - refusing an OFF that would skip "
                              "the drain (check CLOUD_RUNNER_PREFIX / GITEA_ORG)"
                              % (self.prefix, self.gitea.org))
            targets = [{"id": r["id"], "name": r["name"]} for r in cloud if not r["disabled"]]
            now = self.clock()
            # Persisted as `pausing` BEFORE any PATCH and switched to `draining` only once every
            # runner is paused: a restart that finds `pausing` rolls back, so an OFF whose
            # request failed can never be resurrected into a power-off by a later restart.
            self.state = {"v": STATE_VERSION, "phase": "pausing", "by": who, "at": now,
                          "deadline": now + self.drain_max, "runners": targets,
                          "skipped": [r["name"] for r in cloud if r["disabled"]]}
            try:
                self._save()
            except StateError as e:
                # It may have landed as `pausing`; the next tick re-reads it and rolls a stored
                # `pausing` back (no runner was paused yet, so that re-enable is a no-op).
                self.state = None
                self.loaded = False
                raise StateUncertain("could not confirm the schedule was saved (%s); OFF is not "
                                     "scheduled unless the dashboard shows it" % e) from e
            self.clear = 0
            self.inflight = []
            self.note = ""
            for rec in targets:
                try:
                    self.gitea.set_disabled(rec["id"], True)
                except GiteaError as e:
                    self._to_releasing("error", "could not pause %s (%s); OFF not scheduled"
                                       % (rec["name"], e))
                    self.publish()
                    raise
            self.state["phase"] = "draining"
            try:
                self._save()
            except StateError as e:
                # Outcome unknown: the write may have landed (response lost) or not. Do NOT
                # compensate on a stale resourceVersion; re-read on the next tick: a stored
                # `draining` IS the operator's OFF and proceeds, a stored `pausing` rolls back.
                self.loaded = False
                self.dirty = False
                self.publish()
                raise StateUncertain("could not confirm the schedule was saved (%s); within one "
                                     "poll the dashboard shows whether the OFF is scheduled - an "
                                     "unsaved one rolls back and re-enables the runners" % e) from e
            log("OFF SCHEDULED by %s: paused %s (already disabled, left alone: %s)"
                % (who, ",".join(r["name"] for r in targets) or "-",
                   ",".join(self.state["skipped"]) or "-"))
            return self.publish()

    def cancel(self, who):
        with self.lock:
            self._require_usable()
            if not self.state:
                raise Refused("no OFF is scheduled")
            phase = self.state["phase"]
            if phase == "powering_off":
                raise Refused("the hosts are already shutting down; cancel is no longer possible")
            if phase == "pausing":
                raise Refused("the OFF is still being set up; try again in a moment")
            if phase == "stalled":
                after = (self.state.get("stall") or {}).get("cancel_after") or 0
                if self.clock() < after:
                    raise Refused("a host may still be shutting down; CANCEL is possible from %s"
                                  % time.strftime("%H:%M", time.localtime(after)))
            if phase in ("draining", "stalled"):
                prev = copy.deepcopy(self.state)
                self.state["phase"] = "releasing"
                self.state["outcome"] = {"result": "cancelled", "message": "cancelled by %s" % who}
                try:
                    self._save()                     # persist BEFORE re-enabling
                except StateError as e:
                    # The cancel may or may not have landed: re-read on the next tick and follow
                    # what is stored (releasing -> re-enable, draining -> still scheduled).
                    self.state = prev
                    self.loaded = False
                    self.dirty = False
                    raise StateUncertain("could not confirm the cancel was saved (%s); the "
                                         "dashboard shows the outcome within one poll" % e) from e
                log("OFF CANCELLED by %s (was %s)" % (who, phase))
                self._tick_releasing()
            return self.publish()

    def cancel_for_wake(self, who):
        """ON means "I want the cluster on". Decided under the same lock as schedule(), on the
        CURRENT state (not the published snapshot, which lags a schedule() still pausing runners).
        Returns (cancelled, note). Raises StateError when a pending OFF could not be withdrawn."""
        with self.lock:
            self._require_usable()
            phase = self.state["phase"] if self.state else "idle"
            if phase in ("draining", "stalled"):
                try:
                    self.cancel(who + " (ON button)")
                    return True, ""
                except Refused as e:
                    return False, str(e)             # stalled, a host may still be going down
            if phase == "powering_off":
                return False, ("the hosts are still shutting down - a running host ignores the "
                               "packets; press ON again once they are off")
            return False, ""

    # -- the worker -----------------------------------------------------------------------
    def tick(self):
        with self.lock:
            try:
                if not self.loaded:
                    self.load()
                if self.dirty:
                    self._save()                     # StateError -> nothing else this tick
                    log("state caught up with memory")
                if self.state and not self.corrupt:
                    phase = self.state.get("phase")
                    if phase == "pausing":
                        self._to_releasing("error", "the schedule was never confirmed (a state "
                                           "write failed or the controller restarted while "
                                           "pausing runners); OFF not scheduled")
                    elif phase == "draining":
                        self._tick_draining()
                    elif phase == "powering_off":
                        self._tick_powering_off()
                    elif phase == "releasing":
                        self._tick_releasing()
                    elif phase != "stalled":
                        self._stall("unknown phase %r in the state ConfigMap" % phase)
                self.last_tick = self.clock()
            except StateError as e:
                self.note = "state ConfigMap: %s" % e
                log("tick: %s" % e)
            finally:
                self.publish()

    def _tick_draining(self):
        st = self.state
        now = self.clock()
        if now >= st["deadline"]:
            return self._stall("drain deadline reached %ds after OFF was confirmed with work still "
                               "in flight or Gitea unreadable; hosts left ON" % int(now - st["at"]))
        try:
            runners = self.gitea.runners()
            owned = {r["id"] for r in st["runners"]}
            for r in runners:
                if self._is_cloud(r["name"]) and not r["disabled"]:
                    # Re-enabled mid-drain, or newly registered: the schedule pauses and owns it.
                    # Ownership is PERSISTED FIRST, so a lost PATCH reply or a crash can never
                    # leave it paused with nobody recorded to hand it back.
                    self.clear = 0
                    if r["id"] not in owned:
                        st["runners"].append({"id": r["id"], "name": r["name"]})
                        owned.add(r["id"])
                        if not self._save_quiet():
                            return                   # no PATCH until the adoption is durable
                    self.gitea.set_disabled(r["id"], True)
                    log("runner %s was enabled during the drain; paused it" % r["name"])
            self.inflight = self.inflight_jobs(runners)
        except GiteaError as e:
            self.clear = 0
            self.note = "Gitea: %s - still waiting" % e
            log("drain poll failed, still waiting: %s" % e)
            return
        self.note = ""
        if self.inflight:
            self.clear = 0
            return
        self.clear += 1
        if self.clear >= self.clear_polls:
            self._begin_power_off("drained: no CI job in flight on a cloud runner")

    def _begin_power_off(self, reason):
        st = self.state
        prev = copy.deepcopy(st)
        st.update(phase="powering_off", reason=reason, powering_off_at=self.clock(), shutdown_sent=False)
        try:
            self._save()                             # persist BEFORE shutting anything down
        except StateError:
            self.state = prev
            raise
        log("powering off (%s)" % reason)
        self.inflight = []
        self.down_since = None
        self._send_shutdown()

    def _send_shutdown(self):
        results = self.shutdown_fn()
        st = self.state
        st["shutdown_sent"] = True
        st["shutdown_results"] = results
        # Wait for every host that accepted the shutdown OR may have (reply lost). Only a request
        # that was provably never sent (cert pin refused) or a host already off is not waited for.
        st["waiting"] = [r["node"] for r in results
                         if r.get("result") == "shutdown requested"
                         or str(r.get("result", "")).startswith("ERROR")]
        if not st["waiting"] and not all(r.get("result") == "already off" for r in results):
            return self._stall("no node was sent the shutdown: %s"
                               % "; ".join("%s: %s" % (r.get("node"), r.get("result")) for r in results))
        self._save_quiet()

    def _tick_powering_off(self):
        st = self.state
        now = self.clock()
        if not st.get("shutdown_sent"):
            # Resumed between the persisted transition and the recorded results: some nodes may
            # have been sent the shutdown, others not. NEVER replay it (a host may have gone down
            # and been woken since); stall, and hold CANCEL until any shutdown would be over.
            return self._stall("the controller restarted while sending the shutdown; not replaying "
                               "it. Check the hosts; press OFF again if they are still up",
                               cancel_after=st["powering_off_at"] + self.offline_wait)
        waiting = st.get("waiting")
        if waiting is None:  # a state written before `waiting` existed
            waiting = [r["node"] for r in st.get("shutdown_results", [])
                       if r.get("result") == "shutdown requested"]
        if all(not self.host_up_fn(n) for n in waiting):
            if self.down_since is None:
                self.down_since = now
        else:
            self.down_since = None
            if st.get("hosts_down_at"):
                # They WERE down (confirmed) and one answers again: woken (ON, RTC) before Gitea
                # noticed the runners offline. The power cycle happened; hand everything back.
                return self._to_releasing("off", "%s; the hosts were down and have been woken since"
                                          % (st.get("reason") or "powered off"))
        dark = self.down_since is not None and now - self.down_since >= HOST_DOWN_SEC
        if dark and not st.get("hosts_down_at"):
            st["hosts_down_at"] = now
            if not self._save_quiet():
                return
        if dark:
            try:
                by_id = {r["id"]: r for r in self.gitea.runners()}
            except GiteaError as e:
                self.note = "Gitea: %s" % e
                by_id = None
            if by_id is not None:
                self.note = ""
                keep = []
                for rec in st["runners"]:
                    r = by_id.get(rec["id"])
                    if r is None:
                        log("runner %s no longer registered; nothing to re-enable" % rec["name"])
                        continue
                    if r["status"] != "offline":
                        keep.append(rec)             # Gitea has not seen it stop polling yet
                        continue
                    try:
                        self.gitea.set_disabled(rec["id"], False)
                        log("runner %s: host down and runner offline; re-enabled for the next boot"
                            % rec["name"])
                    except GiteaError as e:
                        log("re-enabling %s failed (will retry): %s" % (rec["name"], e))
                        keep.append(rec)
                if keep != st["runners"]:
                    st["runners"] = keep
                    self._save_quiet()
                if not keep:
                    return self._finish("off", st.get("reason") or "powered off")
        if now - st["powering_off_at"] >= self.offline_wait:
            up = [n for n in waiting if self.host_up_fn(n)]
            self._stall("%s %ds after the shutdown request (%s still paused)"
                        % ("host(s) %s still up" % ",".join(up) if up else "runner(s) still online",
                           self.offline_wait, ",".join(r["name"] for r in st["runners"])))

    def _tick_releasing(self):
        st = self.state
        keep = []
        for rec in st["runners"]:
            try:
                self.gitea.set_disabled(rec["id"], False)
            except GiteaError as e:
                if e.status == 404:
                    continue                         # deregistered: nothing to hand back
                log("re-enabling %s failed (will retry): %s" % (rec["name"], e))
                keep.append(rec)
        st["runners"] = keep
        if keep:
            self.note = "could not re-enable %s yet; retrying" % ",".join(r["name"] for r in keep)
            return self._save_quiet()
        out = st.get("outcome") or {"result": "cancelled", "message": ""}
        self._finish(out["result"], out["message"])


SCHED = None  # the Scheduler, MODE=api only


def worker(sched, poll):
    while True:
        try:
            sched.tick()
        except Exception as e:  # noqa: BLE001 - the loop must survive anything a tick throws
            log("tick crashed: %r" % e)
        time.sleep(poll)


# --- HTTP -----------------------------------------------------------------------------------
PAGE = """<!doctype html><meta charset=utf-8><title>Cloud GPU power</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 /* Rendered INSIDE Homepage's iframe widget, so the page must be chrome-less and transparent:
    the widget container supplies the card background, rounding and spacing. */
 html,body{margin:0;padding:0;background:transparent;color:#e2e8f0;
   font:13px/1.45 ui-sans-serif,system-ui,sans-serif;overflow:hidden}
 .wrap{padding:.55rem .7rem;display:flex;flex-direction:column;gap:.45rem;height:100%;box-sizing:border-box}
 .nodes{display:flex;gap:1rem;flex-wrap:wrap;font-family:ui-monospace,monospace;font-size:.78rem}
 .n{display:flex;align-items:center;gap:.35rem}
 .dot{width:.55rem;height:.55rem;border-radius:50%;background:#64748b;flex:none}
 .row{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
 button{font:inherit;font-weight:700;font-size:.72rem;letter-spacing:.04em;border:0;border-radius:.35rem;
   padding:.34rem 1.15rem;cursor:pointer;color:#fff}
 button:disabled{opacity:.5;cursor:not-allowed}
 #sum{color:#94a3b8;font-size:.75rem}
 #sched{font-size:.75rem;color:#fbbf24}
 #out{color:#94a3b8;font-size:.7rem;white-space:pre-wrap;overflow:auto;flex:1;min-height:0}
</style>
<div class=wrap>
 <div class=row><span id=sum>checking...</span><span id=sched></span></div>
 <div class=nodes id=nodes></div>
 <div class=row>
  <button style=background:#16a34a id=bon>ON</button>
  <button style=background:#dc2626 id=boff>OFF</button>
  <button style="background:#475569;display:none" id=bcancel>CANCEL OFF</button>
 </div>
 <div id=out></div>
</div>
<script>
/* Same-origin: the iframe inherits the Authelia cookie, so these calls are authenticated.
   Everything from Gitea (job and runner names) is rendered with textContent, never innerHTML:
   a workflow author controls a job name, and this page carries the dashboard session. */
const B='/cloud-power',$=id=>document.getElementById(id),out=$('out'),sum=$('sum'),sched=$('sched');
const boff=$('boff'),bcancel=$('bcancel');
const hm=t=>t?new Date(t*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):'?';
const ago=(t,now)=>t?Math.max(0,Math.round((now-t)/60))+'m':'';
const iso=s=>s?Date.parse(s)/1000:null;
let phase='unknown',pending=null,timer=null,showLast=true;
function jobs(list,now){return(list||[]).map(j=>'  '+j.runner+'  '+j.job+(j.started_at?'  ('+ago(iso(j.started_at),now)+')':'')).join('\\n')}
function render(s){
 const c=s.schedule||{};phase=c.phase||'unknown';
 boff.disabled=phase!=='idle';bcancel.disabled=false;bcancel.style.display=(phase==='draining'||phase==='stalled')?'':'none';
 /* served_at is stamped per request; the snapshot's own clock freezes with a hung worker. */
 const lim=3*(s.poll_sec||20)+15,stale=c.last_tick&&s.served_at-c.last_tick>lim?'\\n!! controller has not polled for '+ago(c.last_tick,s.served_at)+' - check the cloud-power pod':'';
 if(phase==='draining'){
  const n=(c.inflight||[]).length;
  sched.textContent='OFF scheduled '+hm(c.at)+' - '+(n?'waiting for '+n+' CI job'+(n>1?'s':''):'no CI job in flight, powering off shortly');
  out.textContent='Cloud runners paused: '+((c.runners||[]).join(', ')||'none')+'\\n'+jobs(c.inflight,c.now)
   +'\\nPowers off when they finish (gives up and stays ON at '+hm(c.deadline)+').'+(c.note?'\\n'+c.note:'')+stale;
 }else if(phase==='powering_off'){
  sched.textContent='powering off ('+(c.reason||'')+')';
  out.textContent='Runners stay paused until their host is down: '+((c.runners||[]).join(', ')||'-')+(c.note?'\\n'+c.note:'')+stale;
 }else if(phase==='stalled'){
  sched.textContent='OFF STALLED - hosts may still be on';
  const ca=(c.stall||{}).cancel_after;bcancel.disabled=!!(ca&&ca>c.now);
  out.textContent=((c.stall||{}).message||'')+(ca&&ca>c.now?' (CANCEL possible from '+hm(ca)+')':'')+'\\nCloud runners still paused: '+((c.runners||[]).join(', ')||'-')
   +'\\nCANCEL OFF hands them back to the pool.'+stale;
 }else if(phase==='releasing'){
  sched.textContent='re-enabling runners...';out.textContent=(c.note||'')+stale;
 }else{
  sched.textContent=phase==='unknown'?'(schedule state unavailable - OFF disabled)':'';
  if(phase==='unknown'&&c.note&&!pending)out.textContent=c.note;
  if(showLast&&c.last&&c.now-c.last.at<12*3600&&!pending){
   out.textContent='last OFF '+hm(c.last.at)+': '+c.last.result+(c.last.message?' - '+c.last.message:'');}
 }}
async function refresh(){
 try{const s=await(await fetch(B+'/api/status',{credentials:'same-origin'})).json();
  sum.textContent=s.up+'/'+s.total+' up';
  const nodes=$('nodes');nodes.replaceChildren(...s.nodes.map(n=>{
   const w=document.createElement('span'),d=document.createElement('span'),l=document.createElement('span');
   w.className='n';d.className='dot';d.style.background=n.up?'#22c55e':'#64748b';
   l.style.color=n.up?'#e2e8f0':'#64748b';l.textContent=n.name;w.append(d,l);return w}));
  render(s);
 }catch(e){sum.textContent='unreachable'}}
$('bon').onclick=async()=>{
 out.textContent='sending wake packets...';showLast=false;
 try{const r=await fetch(B+'/api/wake',{method:'POST',credentials:'same-origin'});const d=await r.json();
  if(!r.ok){out.textContent='wake failed: '+(d.error||r.status);return refresh()}
  out.textContent=(d.cancelled?'Scheduled OFF cancelled, runners re-enabled.\\n':'')+(d.note||'Magic packets sent. Nodes take about a minute to POST.');
  sum.textContent='waking...';
 }catch(e){out.textContent='wake failed: '+e}refresh()};
bcancel.onclick=async()=>{
 out.textContent='cancelling...';showLast=false;
 const r=await fetch(B+'/api/shutdown/cancel',{method:'POST',credentials:'same-origin'});const d=await r.json();
 out.textContent=r.ok?'OFF cancelled; cloud runners re-enabled.':'cancel refused: '+(d.error||'');refresh()};
boff.onclick=async()=>{
 showLast=false;
 if(!pending){
  out.textContent='checking what is running...';
  const r=await fetch(B+'/api/shutdown/preflight',{method:'POST',credentials:'same-origin'});
  const p=await r.json();
  if(!r.ok){out.textContent='preflight refused: '+(p.error||'')+'\\n'+(p.errors||[]).join('\\n');return;}
  pending=p.confirm;
  out.textContent=(p.jobs.length?'WILL WAIT FOR '+p.jobs.length+' CI job(s) on cloud runners:\\n'+jobs(p.jobs,Date.now()/1000)+'\\n':'No CI job in flight on the cloud runners.\\n')
   +'THEN STOPS:\\n'+(p.guests.length?p.guests.map(g=>'  '+g.node+' '+g.type+' '+g.vmid+' '+g.name).join('\\n'):'  (no running guests)')
   +'\\nCloud runners take no new jobs from the moment you confirm.\\nClick OFF again within '+p.expires_in+'s to schedule.';
  boff.textContent='CONFIRM';
  clearTimeout(timer);
  timer=setTimeout(()=>{if(pending){pending=null;boff.textContent='OFF';out.textContent='confirmation expired'}},(p.expires_in||30)*1000);
  return;}
 clearTimeout(timer);const tok=pending;pending=null;boff.textContent='OFF';
 out.textContent='scheduling...';
 const r=await fetch(B+'/api/shutdown',{method:'POST',credentials:'same-origin',
  headers:{'content-type':'application/json'},body:JSON.stringify({confirm:tok})});
 const d=await r.json();
 out.textContent=r.ok?'OFF scheduled.':(d.error||JSON.stringify(d));refresh()};
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

    def _same_origin(self):
        """CSRF gate for the state-changing POSTs. Browsers send Sec-Fetch-Site on every fetch and
        Origin on every POST; a non-browser caller inside the fence (curl from the proxy's
        network) sends neither and is allowed, as before."""
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.rstrip("/") in ALLOWED_ORIGINS

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
        if r == "/api/status":
            s = status()
            s["schedule"] = SCHED.view if SCHED else None
            # Fresh on every request (the snapshot's own "now" freezes with a hung worker), so the
            # page can tell a stalled controller from a long drain.
            s["served_at"] = time.time()
            s["poll_sec"] = DRAIN_POLL_SEC
            return self._send(200, s)
        if r in ("", "/", "/index.html"):
            # Embedded by Homepage's native iframe widget (services.yaml). Also usable
            # standalone. No X-Frame-Options is set, deliberately, so same-origin framing works.
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        r = self._route()
        if not self._peer_ok():
            log("DENIED %s from %s" % (r or "/", self.client_address[0]))
            return self._send(403, {"error": "forbidden: caller outside the cluster"})
        if MODE == "api" and not self._same_origin():
            log("DENIED cross-site POST %s (Origin=%s Sec-Fetch-Site=%s)"
                % (r, self.headers.get("Origin"), self.headers.get("Sec-Fetch-Site")))
            return self._send(403, {"error": "forbidden: cross-site request"})
        who = self.headers.get("X-Forwarded-Email") or self.headers.get("X-Forwarded-User") or "?"

        if r == "/api/wake":
            if MODE == "wol":
                return self._send(200, {"action": "wake", "results": wake_all()})
            log("WAKE by %s from %s" % (who, self.client_address[0]))
            # ON means "I want the cluster on": a pending OFF is withdrawn first, or ON fails.
            try:
                cancelled, note = SCHED.cancel_for_wake(who)
            except StateError as e:
                return self._send(503, {"error": "a scheduled OFF could not be confirmed withdrawn, "
                                                 "so ON was not sent: %s" % e})
            try:
                res = forward_wake()
            except Exception as e:
                return self._send(502, {"error": "wol sender unreachable: %s" % e, "cancelled": cancelled})
            res["cancelled"] = cancelled
            if note:
                res["note"] = note
            return self._send(200, res)

        if MODE == "wol":
            # The hostNetwork half is LAN-reachable, so it must not carry anything destructive.
            return self._send(404, {"error": "wol sender exposes only POST /api/wake"})

        if r == "/api/shutdown/preflight":
            if SCHED.view.get("phase") != "idle":
                return self._send(409, {"error": "an OFF is already %s (or the schedule state is "
                                                 "unreadable)" % SCHED.view.get("phase")})
            guests, errors = guests_running()
            if errors:
                # FAIL CLOSED. An incomplete enumeration shown as "no running guests" would be a
                # false all-clear, so no token is issued at all.
                return self._send(503, {"error": "could not enumerate guests; refusing to arm OFF",
                                        "errors": errors})
            try:
                jobs = SCHED.inflight_jobs()
            except GiteaError as e:
                # The drain cannot work without Gitea, so neither can OFF.
                return self._send(503, {"error": "cannot read CI jobs from Gitea; refusing to arm OFF",
                                        "errors": [str(e)]})
            return self._send(200, {"guests": guests, "jobs": jobs,
                                    "confirm": new_confirm(guest_sig(guests)),
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
            # Re-check: the operator confirmed a SPECIFIC set of running guests. If one started
            # or stopped in the meantime, that confirmation no longer describes reality.
            guests, errors = guests_running()
            if errors:
                return self._send(503, {"error": "could not re-verify guests; nothing scheduled",
                                        "errors": errors})
            if guest_sig(guests) != sig:
                return self._send(409, {"error": "running guests changed since preflight; "
                                                 "nothing scheduled - review and confirm again",
                                        "guests": guests})
            try:
                view = SCHED.schedule(who)
            except Refused as e:
                return self._send(409, {"error": str(e)})
            except GiteaError as e:
                return self._send(502, {"error": "could not pause the cloud runners in Gitea; "
                                                 "nothing scheduled: %s" % e})
            except StateUncertain as e:
                return self._send(503, {"error": str(e)})
            except StateError as e:
                return self._send(503, {"error": "could not read the schedule state; nothing "
                                                 "scheduled: %s" % e})
            return self._send(202, {"action": "scheduled", "schedule": view})

        if r == "/api/shutdown/cancel":
            try:
                view = SCHED.cancel(who)
            except Refused as e:
                return self._send(409, {"error": str(e)})
            except StateUncertain as e:
                return self._send(503, {"error": str(e)})
            except StateError as e:
                return self._send(503, {"error": "could not read the schedule state; nothing "
                                                 "changed: %s" % e})
            return self._send(200, {"action": "cancelled", "schedule": view})

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
        if not GITEA_TOKEN:
            log("WARNING: no Gitea token - OFF will be refused (it cannot pause the runners)")
        SCHED = Scheduler(Gitea(GITEA_URL, GITEA_ORG, GITEA_TOKEN),
                          KubeState(STATE_NAMESPACE, STATE_CONFIGMAP), shutdown_all)
        threading.Thread(target=worker, args=(SCHED, DRAIN_POLL_SEC), daemon=True).start()
    log("listening on :%d base=%s allow=%s" % (PORT, BASE or "/",
                                               ",".join(str(n) for n in ALLOW_FROM)))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
