"""relay_phase_a_collectors.py: the phase A observer's collectors and confirmation policy
(plans/2026-11-27-relay-phase-a-observer-plan.md, sections 4, 6 and 7).

Each collector returns exactly the object the driver's validator asks for, built from observations made in THIS call
with the operator's host credentials (kubectl into the llm-router pod, read-only psql in relay-postgres-0, ssh to
dev-worker-1 running the verified remote scripts over stdin). A failed named check raises Failed; an evidence source
that is not established raises Unsupported (a gate, never a pass). Only explicit, typed projections reach `obs` (the
evidence line): raw query rows are recorded as a count and a hash, and untrusted text only as a hash.

Times the driver compares with ITS clock (the Runner's) are stamped with a proven lower bound of that clock:
runner_now >= host_now + (gateNow - host receipt), because the gate stamped gateNow before the reader received it.
"""
from __future__ import annotations

import datetime
import copy
import json
import re
import subprocess
import time
from dataclasses import dataclass

from relay_phase_a_observer import (Deadline, Failed, Refuse, RunState, Unsupported, canonical, is_int, is_uuid,
                                    need, run_for, safe_text, sha256_bytes)

KEY = re.compile(r"^key_[0-9a-f]{32}$")
TOKEN = re.compile(r"[A-Za-z0-9_./:-]{1,200}")
CHANNEL = "b4f9f5f7-840f-4e62-b6cd-e16c9d46dff1"
READ_TITLE = "Allow mcp__relay__relay_messages_read?"
MARKER_TITLE = "Allow bash?"
MAX_CURSOR = 9223372036854775807
CLOCK_MS = 4000
SUPPORT_HUB = '5f90d03a6ed291e6c62dfde75938ba07c20d8902'
SUPPORT_ROUTER = '4e4bb34e2f968f9450aabe9cf228d3e70c8bab71'
SUPPORT_HOLDER = '64dc50f47a4867a6d9a7afc2e7d5ae24068c59af'
SUPPORT_PI = '2604c41b69c5d9c50a3324545a51f0bfa38435a0'
SUPPORT_PREDECESSOR_SESSION = '45724087-335a-4c55-ad0f-cb278afe608a'  # pinned A STOPPED_SESSION
CONTENT_DOMAIN = 'relay-predecessor-command-projection/content-v1\n'
SHA = re.compile(r'^[a-f0-9]{64}$')
LABEL = re.compile(r'^[A-Za-z0-9._:-]{1,200}$')
PROFILE_KEYS = {"id", "run", "binding", "credential", "key", "protocol", "definitionHash", "namespace", "connection",
                "generation", "revision", "expiresAt"}


def now_ms() -> int:
    return int(time.time() * 1000)


def utc(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat(timespec="milliseconds") \
        .replace("+00:00", "Z")


def runner_floor(host, gate_now: int) -> int:
    """A proven lower bound of the Runner's clock now: gateNow was stamped before the reader received the frame, so
    runner_now >= gateNow + (monotonic time elapsed since that receipt). No host wall clock is involved."""
    return gate_now + int((time.monotonic() - host.recv_mono) * 1000)


# ------------------------------------------------------------------------------------------ config
@dataclass
class Config:
    raw: dict
    files: dict  # verified script/SQL bytes by path relative to scripts/

    @property
    def runs(self) -> dict:
        return {p: v["run"] for p, v in self.raw["profiles"].items()}

    @property
    def effort_run(self) -> str:
        return self.raw["effortRun"]

    def profile(self, provider: str) -> dict:
        return self.raw["profiles"][provider]

    def script(self, rel: str) -> bytes:
        return self.files[rel]

    @staticmethod
    def load(config_bytes: bytes, files: dict) -> "Config":
        raw = json.loads(config_bytes)
        need(raw.get("version") == 1 and set(raw["profiles"]) == {"codex", "qwen"}, "observer config", Failed)
        for p in raw["profiles"].values():
            need(is_uuid(p["id"]) and is_uuid(p["run"]) and is_uuid(p["binding"]) and KEY.match(p["historicalKey"]),
                 "observer config profile", Failed)
        return Config(raw, files)


# ------------------------------------------------------------------------------------------ remote commands
class Remote:
    """kubectl / ssh bounded by the request budget, locally AND remotely. `runner` is injectable for tests."""

    def __init__(self, cfg: Config, runner=subprocess.run):
        self.cfg, self.runner = cfg, runner

    def _run(self, argv, stdin: bytes | None, budget, t: float, what: str, obs: dict) -> bytes:
        t0 = time.monotonic()
        try:
            p = self.runner(argv, input=stdin, capture_output=True, timeout=t)
        except subprocess.TimeoutExpired as e:
            obs.setdefault("calls", []).append({"what": what, "rc": "timeout", "ms": int((time.monotonic() - t0) * 1000)})
            raise Deadline(what + " timed out") from e
        obs.setdefault("calls", []).append({"what": what, "rc": p.returncode, "ms": int((time.monotonic() - t0) * 1000),
                                            "stderr": safe_text(p.stderr)})
        if p.returncode != 0:
            raise Failed(what + " failed")
        return p.stdout

    def _bound(self, budget, cap: float) -> float:
        return max(1.0, min(cap, budget.check(1.0)))

    def kubectl(self, ns: str, args: list, budget, obs, what: str, stdin=None, cap=30.0) -> bytes:
        return self._run(["kubectl", "--context", self.cfg.raw["kubeContext"], "-n", ns] + args, stdin, budget,
                         self._bound(budget, cap), what, obs)

    def router_pod(self, budget, obs) -> dict:
        """A FRESH read of the single Router pod's identity (never cached across an observation)."""
        r = self.cfg.raw["router"]
        d = json.loads(self.kubectl(r["namespace"], ["get", "deploy", r["deployment"], "-o", "json"], budget, obs,
                                    "router-deploy"))
        src = d["spec"]["template"]["metadata"].get("annotations", {}).get("chifor.me/source-commit")
        need(src == r["source"], "the live Router source is not the pinned one", Failed)
        pods = json.loads(self.kubectl(r["namespace"], ["get", "pods", "-l", r["selector"], "-o", "json"], budget,
                                       obs, "router-pods"))["items"]
        live = [p for p in pods if p["status"].get("phase") == "Running" and not p["metadata"].get("deletionTimestamp")]
        need(len(live) == 1, "exactly one running Router pod expected", Failed)
        p = live[0]
        cs = [c for c in p["status"].get("containerStatuses", []) if c["name"] == r["container"]]
        need(len(cs) == 1 and cs[0].get("ready") is True, "the Router container is not ready", Failed)
        return {"name": p["metadata"]["name"], "uid": p["metadata"]["uid"], "restarts": cs[0]["restartCount"]}

    def router(self, mode_args: list, budget, obs, what: str, cap=60.0, pod=None) -> dict:
        r = self.cfg.raw["router"]
        pod = pod or self.router_pod(budget, obs)
        t = self._bound(budget, cap)
        deadline = now_ms() + int((t - 1.0) * 1000)  # the in-pod script exits at this point and bounds its fetches
        raw = self._run(["kubectl", "--context", self.cfg.raw["kubeContext"], "-n", r["namespace"], "exec", "-i",
                         pod["name"], "-c", r["container"], "--", "node", "-", str(deadline)] + mode_args,
                        self.cfg.script("relay-phase-a/remote/router.js"), budget, t, what, obs)
        return json.loads(raw)

    def dw(self, args: list, budget, obs, what: str, cap=60.0, deadline_arg=False) -> dict:
        """`deadline_arg` appends the script's own deadline (seconds), kept inside the ssh-side bound, so a remote mode
        finishes its steps before that bound could cut it off."""
        h = self.cfg.raw["devWorker"]
        for a in args:  # every argument is a validated token: no shell metacharacters reach the remote shell
            need(TOKEN.fullmatch(a) is not None, "remote argument", Refuse)
        t = self._bound(budget, cap)
        remote = max(1, int(t - 4))  # the remote bound, inside the local one (ssh setup and transit)
        if deadline_arg:
            need(remote > 8, "too little time for the remote step", Failed)
            args = list(args) + [str(remote - 4)]
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o",
                "UserKnownHostsFile=" + h["knownHosts"], "-o", "ConnectTimeout=8", h["user"] + "@" + h["host"],
                f"timeout --kill-after=2 {remote} python3 -I - " + " ".join(args)]
        return json.loads(self._run(argv, self.cfg.script("relay-phase-a/remote/dw.py"), budget, t, what, obs))

    def sql(self, name: str, variables: dict, budget, obs) -> list:
        """One verified read-only SQL file over stdin; values only as psql variables. The evidence gets the row count
        and a hash of the rows, never the rows (collectors record explicit projections themselves)."""
        db = self.cfg.raw["hubDb"]
        argv = ["exec", "-i", db["pod"], "-c", db["container"], "--", "psql", "-X", "-At", "-v", "ON_ERROR_STOP=1",
                "-U", db["user"], "-d", db["database"]]
        for k, v in variables.items():
            need(re.fullmatch(r"[a-z_]{1,40}", k) is not None, "psql variable name", Refuse)
            need(is_uuid(v) or (isinstance(v, int) and not isinstance(v, bool)), "psql variable value", Refuse)
            argv += ["-v", f"{k}={v}"]
        argv += ["-f", "-"]
        raw = self.kubectl(db["namespace"], argv, budget, obs, "sql:" + name,
                           stdin=self.cfg.script("relay-phase-a/sql/" + name), cap=20.0)
        rows = [json.loads(line) for line in raw.decode().splitlines() if line.strip().startswith("{")]
        obs.setdefault("sql", {})[name] = {"rows": len(rows), "sha256": sha256_bytes(canonical(rows).encode())}
        return rows


# ------------------------------------------------------------------------------------------ shared observations
def coverage(host, pod: dict, ident: dict, obs: dict) -> None:
    """coverageConfirmed's facts: the pinned Router pod, capture config and store, unchanged since the pre-run."""
    want = host.state.router_pod
    need(ident.get("captureLevel") not in (None, "off"), "Router capture disabled", Failed)
    need(want is not None, "no pre-run Router identity", Failed)
    need(pod["uid"] == want["uid"] and pod["restarts"] == want["restarts"], "Router pod restarted or replaced", Failed)
    need(ident["captureSha256"] == want["capture"] and ident["storeSha256"] == want["store"],
         "Router capture config or store changed", Failed)


def pin_router(host, budget, obs) -> dict:
    """The pre-run boundary: the Router identity every later observation must still match."""
    pod = host.remote.router_pod(budget, obs)
    ident = host.remote.router(["identity"], budget, obs, "identity", pod=pod)
    need(ident.get("captureLevel") not in (None, "off"), "Router capture disabled", Failed)
    host.state.router_pod = {"uid": pod["uid"], "restarts": pod["restarts"], "capture": ident["captureSha256"],
                             "store": ident["storeSha256"]}
    obs["routerPin"] = dict(host.state.router_pod)
    return host.state.router_pod


def trajectory(host, provider_key: str, provider: str, *, since=None, since_after=None, not_before=0,
               budget, obs, label: str) -> dict:
    """The driver's snapshot envelope. Pod identity is read fresh before and after every poll; unsettled rows are
    re-polled with the same since, and every row seen while polling must still be present at the end."""
    route = "canary-pi-" + provider
    seen = {}
    while True:
        pod = host.remote.router_pod(budget, obs)
        mode = ["fixed", str(since)] if since is not None else ["after", str(since_after)]
        r = host.remote.router(["trajectory", provider_key, route] + mode + [str(int(not_before))], budget, obs,
                               "trajectory:" + label, pod=pod)
        env = r["envelope"]
        env["source"] = host.cfg.raw["router"]["source"]  # verified against the deployment annotation above
        coverage(host, pod, r["identity"], obs)
        after = host.remote.router_pod(budget, obs)
        need(after == pod, "Router pod changed during the observation", Failed)
        ids = {row["id"]: row for row in env["rows"]}
        need(set(seen) <= set(ids), "a row seen while polling vanished", Failed)
        for rid, row in seen.items():
            need(row.get("incomplete") is not False or ids[rid] == row, "a settled row changed", Failed)
        seen = ids
        if r["unsettled"] == 0:
            break
        since = env["scope"]["since"]
        budget.sleep(2.0)
    obs.setdefault("trajectories", []).append({"label": label, "since": env["scope"]["since"], "rows": len(env["rows"]),
                                               "totalAttempts": env["totalAttempts"],
                                               "sha256": sha256_bytes(canonical(env).encode())})
    return env


def rows_retained(prev: dict | None, env: dict) -> None:
    if not prev:
        return
    need(prev["scope"] == env["scope"], "trajectory scope changed", Failed)
    now = {r["id"]: r for r in env["rows"]}
    for r in prev["rows"]:
        need(now.get(r["id"]) == r, "a retained trajectory row changed or vanished", Failed)


def marker(host, provider: str, run_id: str, phase: str, not_before: int, budget, obs) -> dict:
    rep = host.remote.dw(["marker", provider, run_id, phase, str(int(not_before))], budget, obs, "marker:" + phase)
    need(rep.get("state") == "observed" and rep.get("phaseChecksPass") is True, "marker phase checks failed", Failed)
    obs.setdefault("markers", []).append({"phase": phase, "observedUtc": rep["observedUtc"],
                                          "files": rep.get("files")})
    return rep


def holder_record(host, budget, obs) -> dict:
    h = host.cfg.raw["holder"]
    rec = host.remote.dw(["holder", h["stateDir"]], budget, obs, "holder", deadline_arg=True)
    hello, status = rec.get("hello"), rec.get("status")
    need(hello is not None and status is not None, "holder hello or status unavailable", Failed)
    merged = {
        "incarnation": hello["incarnation"], "journalId": hello["journalId"],
        "nativeRecovery": hello["nativeRecovery"], "nativeStateReady": hello["nativeStateReady"] is True
        and status["nativeStateReady"] is True,
        "sessions": hello["sessions"], "activeSessions": status["activeSessions"],
        "pendingCommands": status["pendingCommands"], "admissionPaused": status["admissionPaused"],
        "admissionRevision": status["admissionRevision"], "sealed": status["sealed"],
        "binarySha256": rec["binarySha256"], "profilesSha256": rec["profilesSha256"],
        "holderMainPid": rec["units"]["relay-holder.service"]["mainPid"],
        "reconciliation": rec["units"]["relay-holder-control-plane.service"]["active"],
        "observedMs": rec["observedMs"],
    }
    for k, v in merged.items():
        need(v is not None, "a holder field is missing", Failed)
    need(is_uuid(merged["incarnation"]) and is_uuid(merged["journalId"]), "holder identity", Failed)
    need(merged["journalId"] == h["newJournalId"], "the holder's journal is not the prepared one", Failed)
    need(merged["binarySha256"] == h["qualified"]["binarySha256"], "holder binary is not the qualified one", Failed)
    need(merged["profilesSha256"] == h["profilesSha256"], "holder profiles drifted", Failed)
    obs["holder"] = merged
    return merged


def clocks(host, budget, obs) -> None:
    """C5: every pair of clocks provably within 4 s. ONE wall/monotonic anchor for the whole exchange: every bracket
    stamp is anchor + monotonic elapsed, so a host wall-clock step cannot hide skew (a detected step is refused).
    The Runner's clock comes from a measured round trip to the gate (Host.clock_probe)."""
    w_anchor, m_anchor = now_ms(), time.monotonic()

    def stamp():
        return w_anchor + int((time.monotonic() - m_anchor) * 1000)

    def bracket(fn):
        a = stamp()
        v = fn()
        return v, a, stamp()

    pod, a0, a1 = bracket(lambda: host.remote.router(["identity"], budget, obs, "clock:router")["podNow"])
    dwn, b0, b1 = bracket(lambda: host.remote.dw(["clock"], budget, obs, "clock:dw")["now"])
    m0 = stamp()
    _w0, gate_now, _w1, rtt_ms = host.clock_probe(budget)
    c0, c1 = m0, m0 + rtt_ms + 50
    need(abs(now_ms() - stamp()) < 1000, "the host wall clock stepped during the exchange", Failed)
    # remote - host lies in [sample - stamp_after, sample - stamp_before]
    iv = {"router": (pod - a1, pod - a0), "devWorker": (dwn - b1, dwn - b0), "runner": (gate_now - c1, gate_now - c0),
          "host": (0, 0)}
    worst, names = 0, list(iv)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            worst = max(worst, abs(iv[a][1] - iv[b][0]), abs(iv[b][1] - iv[a][0]))
    obs["clocks"] = {"intervalsMs": iv, "worstPairMs": worst}
    need(worst < CLOCK_MS, "clock skew cannot be proven under 4 s", Failed)


def session_state(host, session_id: str, budget, obs) -> dict:
    rows = host.remote.sql("session-state.sql", {"session_id": session_id, "tenant": host.cfg.raw["tenant"]}, budget,
                           obs)
    need(len(rows) == 1, "no single session row", Failed)
    r = rows[0]
    obs.setdefault("sessions", []).append({k: r[k] for k in ("id", "state", "generation", "rebinding", "control_epoch")})
    return r


# ------------------------------------------------------------------------------------------ hub deployment
def deployment(host, budget, obs) -> dict:
    hub = host.cfg.raw["hub"]
    d = json.loads(host.remote.kubectl(hub["namespace"], ["get", "deploy", hub["deployment"], "-o", "json"], budget,
                                       obs, "hub-deploy"))
    spec, st = d["spec"], d.get("status", {})
    want = spec.get("replicas", 0)
    need(want >= 1 and d["metadata"]["generation"] == st.get("observedGeneration"), "hub rollout not observed", Failed)
    need(st.get("updatedReplicas") == want and st.get("readyReplicas") == want and st.get("availableReplicas") == want,
         "hub not fully ready", Failed)
    sel = ",".join(f"{k}={v}" for k, v in spec["selector"]["matchLabels"].items())
    pods = json.loads(host.remote.kubectl(hub["namespace"], ["get", "pods", "-l", sel, "-o", "json"], budget, obs,
                                          "hub-pods"))["items"]
    live = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
    need(len(live) == want, "hub pod count differs from the spec", Failed)
    receipts = {r["imageDigest"]: r for r in hub["receipts"]}
    digests = set()
    for p in live:
        cs = [c for c in p["status"].get("containerStatuses", []) if c["name"] == hub["container"]]
        need(len(cs) == 1 and cs[0].get("ready") is True, "hub container not ready", Failed)
        digests.add(cs[0].get("imageID", "").rsplit("@", 1)[-1])
    need(len(digests) == 1 and next(iter(digests)) in receipts, "hub image is not a pinned release", Failed)
    rec = receipts[next(iter(digests))]
    obs["hub"] = {"release": rec["release"], "digest": rec["imageDigest"], "source": rec["source"], "replicas": want}
    return rec


# ------------------------------------------------------------------------------------------ collectors
def c_deployment(host, req, ctx, budget, gate_now, obs):
    rec = deployment(host, budget, obs)
    return {"hubSource": rec["source"], "ready": True, "observedUtc": utc(runner_floor(host, gate_now))}


def c_renewal_deployment(host, req, ctx, budget, gate_now, obs):
    need(ctx.get("source") == host.cfg.raw["hub"]["runSource"], "C1 source is not the pinned run source")
    rec = deployment(host, budget, obs)
    need(rec["source"] == ctx["source"], "deployed hub source is not the run source", Failed)
    need(host.predecessor_ok(), "the preserved predecessor report does not match its pin", Failed)
    observed = runner_floor(host, gate_now)
    nb = ctx.get("notBefore")
    need(not is_int(nb) or observed >= nb, "C1 observation precedes its boundary", Failed)
    return {"confirmMetadataReview": True, "reportDownloaded": True, "operatorVerified": True,
            "deploymentVerified": True, "hubSource": rec["source"], "observedUtc": utc(observed)}


def c_renewed_credentials(host, req, ctx, budget, gate_now, obs):
    profiles, expires = ctx.get("profiles"), ctx.get("expiresAt")
    need(isinstance(profiles, dict) and set(profiles) == {"codex", "qwen"} and is_int(expires, 1), "C2 context")
    rows = host.remote.sql("renewed-credentials.sql", {
        "codex_binding": host.cfg.profile("codex")["binding"], "qwen_binding": host.cfg.profile("qwen")["binding"],
        "tenant": host.cfg.raw["tenant"]}, budget, obs)
    need(len(rows) == 2, "exactly two binding rows expected", Failed)
    by_binding = {r["binding_id"]: r for r in rows}
    renewed = {}
    for provider in ("codex", "qwen"):
        pin, p = host.cfg.profile(provider), profiles[provider]
        need(isinstance(p, dict) and set(p) == PROFILE_KEYS, "C2 profile has unexpected or missing fields", Failed)
        r = by_binding.get(pin["binding"])
        need(r is not None, "binding row missing", Failed)
        state = r["binding_state"]
        cred = r["current_credential_id"] if state == "ready" else r["pending_credential_id"]
        need(state in ("ready", "installing") and (state == "ready" or r["current_credential_id"] is None),
             "binding state is neither ready nor installing", Failed)
        need(cred is not None and cred == r["credential_id"], "selected credential differs", Failed)
        exp = r["expires_ms"]
        need(p["id"] == pin["id"] and p["run"] == pin["run"] and p["binding"] == pin["binding"]
             and p["protocol"] == pin["protocol"] and p["definitionHash"] == pin["definitionHash"]
             and p["credential"] == cred and p["key"] == r["router_key_id"]
             and p["generation"] == r["credential_generation"] and p["revision"] == r["binding_revision"]
             and p["namespace"] == r["namespace"] == "default" and p["connection"] == r["connection_id"]
             == host.cfg.raw["connectionId"] and p["expiresAt"] == exp,
             "the renewed profile projection differs from the database", Failed)
        need(KEY.match(r["router_key_id"]) is not None and r["router_key_id"] != pin["historicalKey"],
             "renewed key is historical or malformed", Failed)
        renewed[provider] = {"key": r["router_key_id"], "credential": cred, "binding": pin["binding"],
                             "generation": r["credential_generation"], "expiresAt": exp}
    need(expires == min(v["expiresAt"] for v in renewed.values()), "C2 expiry is not the earlier one", Failed)
    host.state.renewed, host.state.expires_at = renewed, expires
    obs["renewed"] = renewed
    # Every field of both profiles was checked above against the pins and the database: the echo carries no
    # unvalidated input (the driver compares canonical(profiles) with its own read).
    return {"confirmRenewedCredentials": True, "profiles": profiles, "expiresAt": expires}


def c_stopped_replacement(host, req, ctx, budget, gate_now, obs):
    old = ctx.get("oldScope") or {}
    pin = host.cfg.profile("codex")
    hist = host.predecessor_history()  # scope and baseline from the INDEPENDENT report, not from the Runner
    need(ctx.get("provider") == "codex" and ctx.get("runId") == pin["run"], "C3 run")
    need(ctx.get("source") == host.cfg.raw["router"]["source"], "C3 Router source")
    need(old == hist["scope"], "C3 old scope is not the report's original scope")
    need(old.get("principal") == "key:" + pin["historicalKey"], "C3 old principal is not the pinned historical key")
    need(ctx.get("canonicalReportSha256") == host.predecessor_canonical(), "C3 report hash differs", Failed)
    need(ctx.get("carriedPrompts") == 1 and ctx.get("carriedAttempts") == 2, "C3 accounting")
    nb = ctx.get("notBefore")
    need(is_int(nb, 1), "C3 notBefore")
    env = trajectory(host, pin["historicalKey"], "codex", since=hist["scope"]["since"], not_before=nb, budget=budget,
                     obs=obs, label="historical")
    need(len(env["rows"]) == 2 and all(r["attempts"] == 1 for r in env["rows"]), "historical rows changed", Failed)
    for w in host.cfg.raw["historicalRows"]:
        need(any(r["id"].startswith(w["prefix"]) and r["status"] == w["status"] for r in env["rows"]),
             "a historical row is missing", Failed)
    m = marker(host, "codex", pin["run"], "baseline", nb, budget, obs)
    need(m["files"] == hist["markerFiles"], "the codex marker baseline changed since the predecessor", Failed)
    budget.check()
    start = runner_floor(host, gate_now) - 1000  # <= the Runner's now (lower bound), <= 120 s old at its check
    end = min(start + 900_000, host.state.expires_at - 60_000, host.runner_cutoff_ms())
    need(end > start + 300_000, "less than 5 minutes left for the replacement window", Failed)
    host.state.window = (start, end)
    # The same window in HOST MONOTONIC time, conservatively: it opens no earlier than now and closes as early as
    # the Runner's clock could reach `end` (its upper bound: floor + the measured probe round trip).
    _w0, g, _w1, rtt = host.clock_probe(budget)
    mono = time.monotonic()
    runner_hi = g + rtt + 50
    host.state.window_mono = (mono, mono + (end - runner_hi) / 1000.0)
    host.state.historical = {"trajectory": env, "marker": m}
    obs["window"] = {"start": start, "end": end}
    return {"confirmReplacement": True, "reportDownloaded": True, "operatorVerified": True,
            "priorReportCanonicalSha256": ctx["canonicalReportSha256"], "windowStartUtc": utc(start),
            "windowEndUtc": utc(end), "trajectory": env, "marker": m}


def c_pre_launch(host, req, ctx, budget, gate_now, obs):
    provider = ctx.get("provider")
    need(provider in ("codex", "qwen") and ctx.get("phase") == "baseline", "C5 provider/phase")
    need(ctx.get("source") == host.cfg.raw["router"]["source"], "C5 Router source")
    renewed = host.state.renewed[provider]
    sc = ctx.get("scope") or {}
    need(sc == {"namespace": "default", "principal": "key:" + renewed["key"], "route": "canary-pi-" + provider},
         "C5 scope is not the renewed principal")
    need(ctx.get("holderId") == host.state.holder["incarnation"] and ctx.get("journalId") == host.state.holder["journal"],
         "C5 holder is not the handed-off holder")
    prev = host.state.current()
    if prev is not None:  # the previous run is finished only when its session is closed at the hub
        need(prev.phase == "open2" and prev.session_id is not None, "the previous run is not finished", Failed)
        need(session_state(host, prev.session_id, budget, obs)["state"] == "closed", "the previous session is open",
             Failed)
    hrec = holder_record(host, budget, obs)
    need(hrec["incarnation"] == ctx["holderId"] and hrec["journalId"] == ctx["journalId"], "holder identity", Failed)
    need(hrec["sessions"] == 0 and hrec["activeSessions"] == 0 and hrec["pendingCommands"] == 0
         and hrec["admissionPaused"] is False and hrec["nativeStateReady"] and not hrec["sealed"]
         and hrec["reconciliation"] == "active", "holder not ready for a launch", Failed)
    pin = host.cfg.profile(provider)
    route = host.remote.router(["route", "canary-pi-" + provider, pin["account"], pin["model"]], budget, obs, "route")
    need(route.get("ok") is True, "the canary route is not the single approved target", Failed)
    clocks(host, budget, obs)
    after = ctx.get("sinceAfter")
    need(after is None or is_int(after, 1), "sinceAfter")
    env = trajectory(host, renewed["key"], provider, since_after=after if after is not None else now_ms() - 60_000,
                     budget=budget, obs=obs, label="pre-launch")
    m = marker(host, provider, ctx["runId"], "baseline", 0, budget, obs)
    run = RunState(provider, ctx["runId"], bool(ctx.get("effort")))
    run.c5_trajectory = run.last_trajectory = env
    run.baseline_marker = m
    host.state.pending = run  # appended by advance() only when this reply is sent
    return {"trajectory": env, "marker": m, "coverageConfirmed": True, "holderReadyConfirmed": True,
            "routeConfirmed": True, "clocksWithin5s": True}


def bind_session(host, run, session_id: str, budget, obs, *, resume_from=None, after_ms=None,
                 resume_generation=None) -> dict:
    """A session belongs to this execution only through its launch row (plan section 3)."""
    if resume_from is None:
        rows = host.remote.sql("session-launch.sql", {"session_id": session_id, "tenant": host.cfg.raw["tenant"]},
                               budget, obs)
    else:
        rows = host.remote.sql("session-resume.sql", {"resume_from": resume_from, "tenant": host.cfg.raw["tenant"]},
                               budget, obs)
    need(len(rows) == 1, "no single launch row for the session", Failed)
    r = rows[0]
    pin = host.cfg.profile(run.provider)
    need(r["actor_id"] == host.cfg.raw["jitUserId"] and is_int(r["created_ms"])
         and r["created_ms"] >= (after_ms or host.run_start_ms),
         "the session was not launched by this execution's JIT actor", Failed)
    need(r["profile_id"] == pin["id"] and r["profile_revision"] == 1 and r["cwd"] == pin["cwd"],
         "the launch profile/cwd is not the pinned one", Failed)
    need(r["holder_id"] == host.state.holder["incarnation"] and r["holder_journal_id"] == host.state.holder["journal"],
         "the launch holder is not the handed-off holder", Failed)
    need(is_uuid(r.get("session_id")), "the launch has no session", Failed)
    if resume_from is not None:
        need(r["resume_from"] == resume_from and r["resume_generation"] ==
             (run.generation if resume_generation is None else resume_generation)
             and r["resume_native_id"] == run.native_id and r["resume_acknowledge_unknown_tool_effects"] is False,
             "the resume is not bound to the closed session", Failed)
    s = session_state(host, r["session_id"], budget, obs)
    need(s["state"] == "open" and s["rebinding"] is False and s["holder_id"] == host.state.holder["incarnation"],
         "the bound session is not open on the handed-off holder", Failed)
    host.state.sessions[r["session_id"]] = run
    obs["binding"] = {"session": r["session_id"], "launch": r["id"], "resume": resume_from is not None}
    return {**r, "generation": s["generation"]}


def c_post_open(host, req, ctx, budget, gate_now, obs):
    run = run_for(host.state, ctx)
    need(ctx.get("phase") == "baseline" and is_uuid(ctx.get("sessionId")) and is_int(ctx.get("generation"), 1)
         and is_int(ctx.get("notBefore"), 1), "C6 context")
    launch = bind_session(host, run, ctx["sessionId"], budget, obs)
    need(launch["session_id"] == ctx["sessionId"] and launch["generation"] == ctx["generation"],
         "the C6 session or generation differs from the hub", Failed)
    run.session_id, run.generation, run.launch_id = ctx["sessionId"], ctx["generation"], launch["id"]
    run.native_id = launch.get("native_id")
    renewed = host.state.renewed[run.provider]
    v = {"launch_id": launch["id"], "session_id": ctx["sessionId"], "binding_id": renewed["binding"],
         "credential_id": renewed["credential"], "host_id": host.cfg.raw["hostId"], "tenant": host.cfg.raw["tenant"]}
    rows = host.remote.sql("delivery.sql", v, budget, obs)
    if not rows and run.effort:  # the effort launch reuses the credential the codex lifecycle installed
        rows = host.remote.sql("delivery-reuse.sql", {k: v[k] for k in ("binding_id", "credential_id", "host_id",
                                                                         "tenant")}, budget, obs)
    need(len(rows) == 1, "no single install receipt for this credential on this holder", Failed)
    r = rows[0]
    need(r["holder_id"] == host.state.holder["incarnation"] and r["generation"] == renewed["generation"]
         and r["rendered"] is True and r["render_failure"] is None and r["install_state"] == "completed"
         and r["receipt_status"] == "completed", "the install/render receipt is not terminal-installed", Failed)
    need(r["consumer_state"] == "active" and r["consumer_lease_ok"] is True, "no active consumer lease on this host",
         Failed)
    obs["delivery"] = {k: r[k] for k in ("install_state", "receipt_status", "rendered", "consumer_state",
                                         "consumer_lease_ok")}
    f = host.remote.dw(["credfile", renewed["binding"]], budget, obs, "credfile")
    need(f.get("state") == "present" and f.get("regular") is True and f.get("uid") == 1000 and f.get("mode") == "0o600"
         and f.get("links") == 1, "credential.json is not c4/0600/single-link", Failed)
    m = marker(host, run.provider, run.run_id, "baseline", ctx["notBefore"], budget, obs)
    need(m["files"] == run.baseline_marker["files"], "the marker baseline changed after open", Failed)
    return {"marker": m, "deliveryConfirmed": True}


def c_observation(host, req, ctx, budget, gate_now, obs):
    run = run_for(host.state, ctx)
    sc, nb, phase = ctx.get("scope") or {}, ctx.get("notBefore"), ctx.get("phase")
    renewed = host.state.renewed[run.provider]
    need(sc.get("principal") == "key:" + renewed["key"] and sc.get("route") == "canary-pi-" + run.provider
         and sc.get("namespace") == "default" and is_int(sc.get("since"), 1), "C7 scope is not this run's")
    need(run.c5_trajectory is not None and sc["since"] == run.c5_trajectory["scope"]["since"], "C7 since changed")
    need(is_int(nb, 1) and (phase is None or phase in ("baseline", "denied", "approved", "recovered")), "C7 context")
    if run.phase == "recovering":  # launch(true) went straight here: bind the resumed session first
        closed = run.session_id
        need(session_state(host, closed, budget, obs)["state"] == "closed", "the recovered-from session is open",
             Failed)
        launch = bind_session(host, run, None, budget, obs, resume_from=closed, after_ms=run.recover_ms)
        run.session_id, run.generation, run.launch_id = launch["session_id"], launch["generation"], launch["id"]
    env = trajectory(host, renewed["key"], run.provider, since=sc["since"], not_before=nb, budget=budget, obs=obs,
                     label="observation")
    rows_retained(run.last_trajectory, env)
    out = {"trajectory": env, "coverageConfirmed": True}
    if phase is not None:
        m = marker(host, run.provider, run.run_id, phase, nb, budget, obs)
        if phase == "approved":
            run.approved_marker_sha = m["files"]["effects"].get("sha256")
        out["marker"] = m
    run.last_trajectory = env
    if run.effort and run.effort_before is None and env['totalAttempts'] > 0:
        run.effort_before = copy.deepcopy(env)
    return out


def c_permission(host, req, ctx, budget, gate_now, obs):
    run = run_for(host.state, ctx)
    sid, gen, kind, opt = ctx.get("sessionId"), ctx.get("generation"), ctx.get("decisionKind"), ctx.get("optionId")
    need(sid == run.session_id and host.state.sessions.get(sid) is run and gen == run.generation,
         "C8 session is not this run's bound session")
    perm = ctx.get("permission") or {}
    need(is_uuid(perm.get("id")) and isinstance(opt, str) and kind in ("allow_once", "reject_once"), "C8 context")
    s = session_state(host, sid, budget, obs)  # the hub's CURRENT generation and rebinding, not the Runner's
    need(s["state"] == "open" and s["generation"] == gen and s["rebinding"] is False,
         "the session's generation or binding changed", Failed)
    rows = host.remote.sql("permission.sql", {"permission_id": perm["id"], "session_id": sid,
                                              "tenant": host.cfg.raw["tenant"]}, budget, obs)
    need(len(rows) == 1, "no single pending permission", Failed)
    r = rows[0]
    need(r["state"] == "pending" and r["unexpired"] is True, "the permission is not pending and unexpired", Failed)
    pctx = r["context"] or {}
    options = [o for o in pctx.get("options") or [] if isinstance(o, dict)]
    of_kind = [o for o in options if o.get("kind") == kind]
    by_id = [o for o in options if o.get("optionId") == opt]
    need(len(of_kind) == 1 and len(by_id) == 1 and by_id[0] is of_kind[0],
         "the option ID is not the single option of that kind", Failed)
    title, desc = pctx.get("title"), pctx.get("description")
    obs["permission"] = {"id": perm["id"], "title": safe_text(title), "description": safe_text(desc), "kind": kind}
    purpose = ctx.get("purpose")
    if purpose == "relay-message-read":
        need(title == READ_TITLE and kind == "allow_once", "relay read must be the exact allow_once read", Failed)
        read_input(desc)
        need(run.reads < host.cfg.raw["maxReadsPerRun"], "relay read cap reached", Failed)
        run.reads += 1
        run.confirmed.append({"permission": perm["id"], "option": opt, "kind": kind, "step": "read"})
        return True
    need(purpose == "marker", "unknown permission purpose", Failed)
    need(not run.effort, "no marker permission in the effort run", Failed)
    need(title == MARKER_TITLE, "the marker tool is not exactly bash", Failed)
    cmd = marker_input(desc)
    renders = {k: host.marker_command(run.provider, run.run_id, k) for k in ("denied", "effects")}
    step = [k for k, v in renders.items() if v == cmd]
    need(len(step) == 1 and ctx.get("expectedCommand") == cmd, "the marker command is not the rendering", Failed)
    want = {"denied": "reject_once", "effects": "allow_once"}[step[0]]
    need(kind == want, "the decision is not bound to this command", Failed)
    need(run.decisions[step[0]] == 0, "this marker decision was already confirmed", Failed)
    run.decisions[step[0]] += 1
    run.confirmed.append({"permission": perm["id"], "option": opt, "kind": kind, "step": step[0]})
    obs["permission"]["marker"] = step[0]
    return True


def _desc_json(desc, limit: int) -> dict:
    """The driver's rule: the WHOLE description is the JSON input (JSON.parse(c.description))."""
    need(isinstance(desc, str) and len(desc.encode()) <= limit, "permission description", Failed)
    try:
        v = json.loads(desc)
    except ValueError as e:
        raise Failed("permission input malformed") from e
    need(isinstance(v, dict), "permission input", Failed)
    return v


def read_input(desc) -> dict:
    v = _desc_json(desc, 4000)
    need(set(v) <= {"channelId", "after", "limit"}, "relay read input has extra fields", Failed)
    if "channelId" in v:
        need(v["channelId"] == CHANNEL, "relay read channel", Failed)
    if "after" in v:
        a = v["after"]
        need(isinstance(a, str) and re.fullmatch(r"0|[1-9][0-9]*", a) is not None and int(a) <= MAX_CURSOR,
             "relay read cursor", Failed)
    if "limit" in v:
        need(is_int(v["limit"], 1) and v["limit"] <= 50, "relay read limit", Failed)
    return v


def marker_input(desc) -> str:
    v = _desc_json(desc, 16384)
    need(set(v) <= {"command", "timeout"} and isinstance(v.get("command"), str), "marker input shape", Failed)
    if "timeout" in v:
        t = v["timeout"]
        need(isinstance(t, (int, float)) and not isinstance(t, bool) and t == t and 0 < t <= 120, "marker timeout",
             Failed)
    return v["command"]


def c_recover_history(host, req, ctx, budget, gate_now, obs):
    support_sources(host)
    run = run_for(host.state, ctx)
    need(ctx.get("sessionId") == run.session_id and ctx.get("generation") == run.generation, "C9 session")
    need(ctx.get("holderId") == host.state.holder["incarnation"] and ctx.get("journalId") == host.state.holder["journal"],
         "C9 holder")
    hrec = holder_record(host, budget, obs)
    need(hrec["incarnation"] == ctx["holderId"] and hrec["journalId"] == ctx["journalId"] and hrec["sessions"] == 0
         and hrec["activeSessions"] == 0 and hrec["pendingCommands"] == 0, "holder not settled", Failed)
    need(session_state(host, run.session_id, budget, obs)["state"] == "closed", "the session is not closed", Failed)
    rows = host.remote.sql("session-commands.sql", {"session_id": run.session_id, "generation": run.generation,
                                                    "tenant": host.cfg.raw["tenant"]}, budget, obs)
    control = [r for r in rows if r["table"] == "control"]
    agent = [r for r in rows if r["table"] == "agent"]
    need(control and all(r["state"] == "completed" and r["receipt_status"] == "completed" for r in control),
         "a control command is not completed", Failed)
    need(all(r["kind"] == "permission-decision" and r["receipt_status"] == "accepted" for r in agent),
         "an agent command is not an accepted permission decision", Failed)
    perms = host.remote.sql("permission-decisions.sql", {"session_id": run.session_id,
                                                         "tenant": host.cfg.raw["tenant"]}, budget, obs)
    decided = {p["id"]: p for p in perms}
    need(set(decided) == {c["permission"] for c in run.confirmed}, "the session's permissions are not exactly the "
         "confirmed ones", Failed)
    for c in run.confirmed:
        p = decided[c["permission"]]
        need(p["state"] == "resolved" and p["option_id"] == c["option"], "a permission was not resolved as confirmed",
             Failed)
    t = read_native_checkpoint(host, run, budget, obs)
    if t.get("state") != "observed":
        raise Unsupported("the native transcript source is not established")
    need(t["unsettled"] == [] and t["duplicateResults"] == [], "a native tool call is unsettled or duplicated", Failed)
    outcome = {sha256_bytes(host.marker_command(run.provider, run.run_id, k).encode()): k for k in ("denied", "effects")}
    need(set(t['toolNames']) <= {'bash', 'mcp__relay__relay_messages_read'}, 'unapproved native tool', Failed)
    calls = t["shellCommands"]  # every call, in order: multiplicity matters (a repeat is never merged away)
    need(len(calls) == 2 and sorted(c["sha256"] for c in calls) == sorted(outcome),
         "shell calls are not exactly one denied and one effects marker call", Failed)
    for c in calls:
        res, step = c["results"], outcome[c["sha256"]]
        need(len(res) == 1 and isinstance(res[0], bool), "a marker call's native outcome is unknown", Unsupported)
        need(res[0] is (step == "denied"), "a marker call's native outcome differs from the decision", Failed)
    shells = calls
    m = marker(host, run.provider, run.run_id, "approved", 0, budget, obs)
    need(m["files"]["effects"].get("sha256") == run.approved_marker_sha, "the approved marker changed", Failed)
    obs["recovery"] = {"permissions": len(run.confirmed), "shellCalls": len(shells)}
    branch = read_router_branch(host, run, t, budget, obs)
    branch_decision(t['native'], branch['record'])  # no unknown join retained as permission to resume
    run.retained_branch = {'sessionId': run.session_id, 'generation': run.generation,
                           'native': copy.deepcopy(t), 'router': copy.deepcopy(branch)}
    run.recover_ms = now_ms() - 5000
    return True


def c_reconcile_choice(host, req, ctx, budget, gate_now, obs):
    support_sources(host)
    run = checkpoint_run(host, ctx)
    old = run.retained_branch
    need(old is not None and run.session_id != old['sessionId'], 'C9 retained branch unavailable', Unsupported)
    launch = bind_session(host, run, None, budget, obs, resume_from=old['sessionId'], after_ms=run.recover_ms,
                          resume_generation=old['generation'])
    need(launch['session_id'] == run.session_id and launch['resume_generation'] == old['generation'],
         'C10 resume binding changed', Failed)
    idle_evidence(host, run, budget, obs)
    t = read_native_checkpoint(host, run, budget, obs)
    need(t['sourceSessionId'] == old['sessionId'] and t['native'] == old['native']['native']
         and t['sourceSha256'] == old['native']['currentSha256']
         and t['retainedReceiptSha256'] == old['native']['retainedReceiptSha256']
         and SHA.fullmatch(str(t.get('importedSha256', ''))) and t['unsettled'] == [],
         'C10 imported native receipt changed', Failed)
    branch = read_router_branch(host, run, t, budget, obs)
    previous, current = old['router'], branch
    need(all(current[k] == previous[k] for k in ('namespace', 'principal', 'conversation', 'branch', 'key', 'storeSha256'))
         and all(current['record'][k] == previous['record'][k] for k in ('epoch', 'operation', 'state'))
         and current['record']['updatedAt'] >= previous['record']['updatedAt'], 'C10 retained Router branch changed', Failed)
    return {'reconcile': branch_decision(t['native'], branch['record'])}


def c_retire_branch(host, req, ctx, budget, gate_now, obs):
    support_sources(host)
    run = checkpoint_run(host, ctx)
    need(run.effort and run.provider == 'codex' and run.effort_before is not None,
         'C11 retained effort observation unavailable', Unsupported)
    evidence = idle_evidence(host, run, budget, obs)
    validate_retirement(evidence, run.generation)
    before = run.effort_before
    env = trajectory(host, host.state.renewed[run.provider]['key'], run.provider,
                     since=before['scope']['since'], not_before=0, budget=budget, obs=obs, label='retirement')
    rows_retained(before, env)
    need(env['rows'] == before['rows'] and env['totalAttempts'] == before['totalAttempts'],
         'C11 probe added or changed attempts', Failed)
    obs['retirement'] = {'sessionId': run.session_id, 'generation': run.generation,
                         'evidenceSha256': sha256_bytes(canonical(evidence).encode()), 'zeroAttempts': True}
    return True


def c_predecessor(host, req, ctx, budget, gate_now, obs):
    support_sources(host)
    need(host.predecessor_ok(), 'independent predecessor report changed', Failed)
    report = json.loads(host.predecessor_bytes)
    prior = report.get('operations')
    need(isinstance(prior, list), 'predecessor commands unavailable', Unsupported)
    wanted = [{'operationId': c.get('body', {}).get('operationId'), 'kind': c.get('body', {}).get('kind')}
              for c in prior if c.get('body', {}).get('kind') in ('prompt', 'cancel', 'close')]
    need(len(wanted) == 3 and {r['kind'] for r in wanted} == {'prompt', 'cancel', 'close'}
         and all(is_uuid(r['operationId']) for r in wanted), 'predecessor report commands differ', Failed)
    need(ctx.get('source') == SUPPORT_HUB and ctx.get('schemaVersion') == 93
         and ctx.get('sessionId') == SUPPORT_PREDECESSOR_SESSION and ctx.get('generation') == 1
         and ctx.get('priorReportCanonicalSha256') == host.predecessor_canonical()
         and ctx.get('commands') == wanted, 'predecessor context differs from independent report', Failed)
    deployment(host, budget, obs)
    rows = host.remote.sql('predecessor-projection.sql', {'tenant': host.cfg.raw['tenant'],
        'session_id': ctx['sessionId'], 'generation': 1,
        **{r['kind'] + '_operation': r['operationId'] for r in wanted}}, budget, obs)
    need(len(rows) == 1, 'predecessor projection unavailable', Failed)
    row = rows[0]
    supported_schema(row)
    observed = fresh_ms(row.get('observedMs'))
    need(row.get('databaseName') == host.cfg.raw['hubDb']['database'], 'predecessor database changed', Failed)
    commands = row.get('commands')
    need(isinstance(commands, list) and len(commands) == 3, 'predecessor command cardinality', Failed)
    ids = set()
    for expected, c in zip(wanted, commands):
        need(set(c) == {'id', 'operation_id', 'session_id', 'generation', 'kind', 'state', 'receipt'}
             and is_uuid(c['id']) and c['id'] not in ids and c['operation_id'] == expected['operationId']
             and c['kind'] == expected['kind'] and c['session_id'] == ctx['sessionId'] and c['generation'] == 1
             and c['state'] == 'completed', 'predecessor command differs', Failed)
        ids.add(c['id'])
        receipt = c['receipt']
        need(isinstance(receipt, dict) and set(receipt) == {'status', 'result'} and receipt['status'] == 'completed',
             'predecessor receipt not completed', Failed)
        need((c['kind'] == 'prompt' and receipt['result'] in ({'stopReason': 'cancelled'}, {'stopReason': 'end_turn'}))
             or (c['kind'] in ('cancel', 'close') and receipt['result'] == {'cancelSent': True}),
             'predecessor native outcome unknown', Failed)
    db = host.cfg.raw['hubDb']
    label = '.'.join(db[k] for k in ('namespace', 'pod', 'database'))
    need(re.fullmatch(r'[A-Za-z0-9._-]{1,128}', label), 'database label unavailable', Unsupported)
    value = {'kind': 'relay-predecessor-command-projection', 'version': 1, 'source': SUPPORT_HUB,
             'schemaVersion': 93, 'observedUtc': utc(observed), 'tenantId': host.cfg.raw['tenant'],
             'databaseIdentity': label,
             'querySha256': sha256_bytes(host.cfg.script('relay-phase-a/sql/predecessor-projection.sql')),
             'sessionId': ctx['sessionId'], 'generation': 1,
             'priorReportCanonicalSha256': ctx['priorReportCanonicalSha256'], 'commands': commands}
    raw = canonical(value)
    need(len(raw.encode()) <= 65536, 'predecessor projection too large', Failed)
    content = {k: v for k, v in value.items() if k != 'observedUtc'}
    obs['predecessor'] = {'rawSha256': sha256_bytes(raw.encode()),
                          'contentSha256': sha256_bytes((CONTENT_DOMAIN + canonical(content)).encode()),
                          'hashDomain': CONTENT_DOMAIN, 'observedMs': observed}
    return raw


def support_sources(host):
    raw = getattr(getattr(host, 'cfg', None), 'raw', {})
    need(raw.get('hub', {}).get('runSource') == SUPPORT_HUB and raw.get('router', {}).get('source') == SUPPORT_ROUTER
         and raw.get('holder', {}).get('qualified', {}).get('source') == SUPPORT_HOLDER,
         'unqualified observer support source tuple', Unsupported)


def supported_schema(row):
    need(row.get('schemaVersions') == list(range(1, 94)), 'unqualified hub schema', Unsupported)


def fresh_ms(value):
    now = now_ms()
    need(is_int(value, 1) and now - value < 120000 and value - now <= 5000, 'stale source observation', Failed)
    return value


def checkpoint_run(host, ctx):
    run = run_for(host.state, ctx)
    need(ctx.get('sessionId') == run.session_id and ctx.get('generation') == run.generation
         and ctx.get('holderId') == host.state.holder['incarnation']
         and ctx.get('journalId') == host.state.holder['journal'], 'checkpoint ownership differs', Failed)
    return run


def read_native_checkpoint(host, run, budget, obs):
    need(is_uuid(run.native_id), 'native UUID unavailable', Unsupported)
    pin, h = host.cfg.profile(run.provider), host.state.holder
    value = host.remote.dw(['native-checkpoint', host.cfg.raw['holder']['stateDir'], run.session_id,
                           run.native_id, pin['id'], pin['cwd'], h['incarnation'], h['journal']],
                          budget, obs, 'native-checkpoint')
    need(value.get('state') == 'observed' and value.get('version') == 1, 'native source unsupported', Unsupported)
    need(all(value.get(k) == v for k, v in {'sessionId': run.session_id, 'nativeSessionId': run.native_id,
         'profileId': pin['id'], 'generation': run.generation, 'holderId': h['incarnation'],
         'journalId': h['journal']}.items()), 'native checkpoint identity differs', Failed)
    fresh_ms(value.get('observedMs'))
    need(all(SHA.fullmatch(str(value.get(k, ''))) for k in ('retainedReceiptSha256', 'currentSha256',
         'sourceSha256', 'journalSha256', 'profileDigest')), 'native checkpoint provenance missing', Failed)
    obs.setdefault('nativeCheckpoints', []).append(value)
    return value


def read_router_branch(host, run, native, budget, obs):
    n = native['native']
    need(isinstance(n, dict) and all(isinstance(n.get(k), str) and LABEL.fullmatch(n[k])
         for k in ('conversation', 'branch', 'nativeSession')) and n['nativeSession'] == run.native_id,
         'native receipt invalid', Failed)
    principal = 'key:' + host.state.renewed[run.provider]['key']
    pod = host.remote.router_pod(budget, obs)
    ident = host.remote.router(['identity'], budget, obs, 'branch-identity', pod=pod)
    coverage(host, pod, ident, obs)
    value = host.remote.router(['branch', 'default', principal, n['conversation'], n['branch']],
                               budget, obs, 'branch-record', pod=pod)
    expected_key = sha256_bytes(json.dumps(['default', principal, n['conversation'], n['branch']],
                                           separators=(',', ':'), ensure_ascii=False).encode())
    need(value.get('version') == 1 and value.get('key') == expected_key
         and value.get('namespace') == 'default' and value.get('principal') == principal
         and value.get('conversation') == n['conversation'] and value.get('branch') == n['branch']
         and value.get('kvNamespace') == 'inference-session-v1' and value.get('storeSha256') == ident['storeSha256']
         and SHA.fullmatch(str(value.get('rawRecordSha256', '')))
         and value.get('querySha256') == sha256_bytes(b'SELECT value FROM kv WHERE ns=? AND key=?'),
         'Router branch provenance differs', Failed)
    fresh_ms(value.get('requestedAt'))
    fresh_ms(value.get('observedAt'))
    need(is_int(value.get('record', {}).get('updatedAt'))
         and value['record']['updatedAt'] <= value['observedAt'] + 5000,
         'Router branch timestamp is in the future', Failed)
    need(value['observedAt'] >= value['requestedAt'] and host.remote.router_pod(budget, obs) == pod,
         'Router branch source changed', Failed)
    obs.setdefault('branchCheckpoints', []).append(value)
    return value


def branch_decision(native, router):
    need(isinstance(native, dict) and isinstance(router, dict) and router.get('state') in ('complete', 'uncertain')
         and 'successor' not in router and is_int(router.get('updatedAt'))
         and all(isinstance(router.get(k), str) and LABEL.fullmatch(router[k]) for k in ('epoch', 'operation')),
         'Router branch is not safely settled', Failed)
    pending, parent = native.get('pending'), native.get('parent')
    need(all(v is None or (isinstance(v, str) and LABEL.fullmatch(v)) for v in (pending, parent)),
         'native receipt malformed', Failed)
    if pending is not None:
        need(router['operation'] == pending or (parent is not None and router['operation'] == parent
             and router['state'] == 'complete'), 'native pending does not join Router', Failed)
        return True
    need(parent is not None and router['operation'] == parent and router['state'] == 'complete',
         'native parent does not join Router', Failed)
    return False


def idle_evidence(host, run, budget, obs):
    rows = host.remote.sql('retirement-evidence.sql', {'tenant': host.cfg.raw['tenant'],
        'session_id': run.session_id, 'generation': run.generation,
        'holder_id': host.state.holder['incarnation'], 'journal_id': host.state.holder['journal']}, budget, obs)
    need(len(rows) == 1, 'current native work unavailable', Unsupported)
    value = rows[0]
    supported_schema(value)
    fresh_ms(value.get('observedMs'))
    session = value.get('session') or {}
    need(session.get('id') == run.session_id and session.get('generation') == run.generation
         and session.get('state') == 'open' and session.get('rebinding') is False
         and session.get('holder_id') == host.state.holder['incarnation']
         and value.get('pendingPermissions') == 0 and isinstance(value.get('commands'), list)
         and len(value['commands']) <= 200
         and all(c.get('state') == 'completed' and c.get('receiptStatus') == 'completed'
             for c in value['commands']), 'native work is pending or unknown', Failed)
    h = holder_record(host, budget, obs)
    need(h.get('incarnation') == host.state.holder['incarnation']
         and h.get('journalId') == host.state.holder['journal'] and h.get('pendingCommands') == 0,
         'holder native work is pending', Failed)
    return value


def validate_retirement(value, generation):
    commands, events = value['commands'], value.get('events')
    need(isinstance(events, list) and 0 < len(events) <= 2000, 'retirement events incomplete', Unsupported)
    jr = value.get('journalRange') or {}
    need(is_int(jr.get('first'), 1) and is_int(jr.get('last'), 1)
         and jr.get('count') == jr['last'] - jr['first'] + 1, 'holder journal retention gap', Unsupported)
    need(all(is_int(e.get('sequence'), 1) for e in events)
         and [e['sequence'] for e in events] == sorted({e['sequence'] for e in events}),
         'retirement event sequence differs', Failed)
    high = [c for c in commands if c.get('kind') == 'set-config' and c.get('configId') == 'thought_level'
            and c.get('value') == 'high']
    probes = [c for c in commands if c.get('probe') is True]
    need(len(high) == len(probes) == 1, 'exact effort/probe command unavailable', Unsupported)
    high, probe = high[0], probes[0]
    epoch = value['session']['control_epoch']
    need(high.get('controlEpoch') == probe.get('controlEpoch') == epoch and high.get('settingsApplied') is True
         and probe.get('stopReason') in ('end_turn', 'refusal'), 'effort/probe outcome differs', Failed)
    def interval(command):
        accepted = [e for e in events if e.get('kind') == 'command-accepted' and e.get('commandId') == command['id']]
        ended = [e for e in events if e.get('kind') == 'command-result' and e.get('commandId') == command['id']]
        need(len(accepted) == len(ended) == 1 and ended[0].get('status') == 'completed'
             and accepted[0]['sequence'] < ended[0]['sequence'], 'command journal interval unavailable', Unsupported)
        return accepted[0]['sequence'], ended[0]['sequence']
    hs, he = interval(high)
    ps, pe = interval(probe)
    need(he < ps, 'probe preceded applied high setting', Failed)
    need(not any(e.get('kind') == 'command-accepted' and e.get('commandId') not in (high['id'], probe['id'])
                 and hs < e['sequence'] < pe for e in events), 'ambiguous command interval', Unsupported)
    settings = [e for e in events if e.get('kind') == 'session-settings']
    need(settings and hs < settings[-1]['sequence'] < he and settings[-1].get('generation') == generation,
         'applied setting journal correlation unavailable', Unsupported)
    snapshot = settings[-1].get('settings') or {}
    options = [r for r in snapshot.get('configOptions', []) if r.get('id') == 'thought_level']
    need(is_int(high.get('settingsRevision')) and is_int(snapshot.get('revision'), 1)
         and snapshot['revision'] > high['settingsRevision']
         and len(options) == 1 and options[0].get('currentValue') == 'high', 'high setting not observed', Failed)
    notices = [e for e in events if e.get('notice') is True and ps < e['sequence'] < pe]
    need(len(notices) == 1 and notices[0].get('truncated') is not True,
         'exact correlated configuration-change notice unavailable', Unsupported)


COLLECTORS = {
    ("deployment", None): c_deployment,
    ("checkpoint", "renewal-deployment"): c_renewal_deployment,
    ("checkpoint", "renewed-credentials"): c_renewed_credentials,
    ("checkpoint", "stopped-replacement"): c_stopped_replacement,
    ("checkpoint", "pre-launch"): c_pre_launch,
    ("checkpoint", "post-open"): c_post_open,
    ("checkpoint", "observation"): c_observation,
    ("checkpoint", "permission"): c_permission,
    ("checkpoint", "recover-history"): c_recover_history,
    ("checkpoint", "reconcile-choice"): c_reconcile_choice,
    ("checkpoint", "retire-branch"): c_retire_branch,
    ("predecessor", None): c_predecessor,
}
