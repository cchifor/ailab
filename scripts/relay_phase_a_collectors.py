"""relay_phase_a_collectors.py: the phase A observer's collectors and confirmation policy
(plans/2026-11-27-relay-phase-a-observer-plan.md, sections 4, 6 and 7).

Each collector returns exactly the object the driver's validator asks for, built from observations made in THIS call
with the operator's host credentials (kubectl into the llm-router pod, read-only psql in relay-postgres-0, ssh to
dev-worker-1 running the verified remote scripts over stdin). A failed named check raises Failed; an evidence source
that is not established raises Unsupported (a gate, never a pass). Every observation is projected into `obs` for the
evidence line; untrusted text is logged only as a hash.
"""
from __future__ import annotations

import datetime
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from relay_phase_a_observer import (Deadline, Failed, Refuse, RunState, Unsupported, canonical, is_int, is_uuid,
                                    need, run_for, safe_text, sha256_bytes)

KEY = re.compile(r"^key_[0-9a-f]{32}$")
CHANNEL = "b4f9f5f7-840f-4e62-b6cd-e16c9d46dff1"
READ_TITLE = "Allow mcp__relay__relay_messages_read?"
MAX_CURSOR = 9223372036854775807
CLOCK_MS = 4000


def iso_ms(s) -> int:
    need(isinstance(s, str), "time expected", Failed)
    try:
        return int(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError as e:
        raise Failed("unparseable time") from e


def utc(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat(timespec="milliseconds") \
        .replace("+00:00", "Z")


# ------------------------------------------------------------------------------------------ config
@dataclass
class Config:
    raw: dict
    files: dict  # verified script/SQL bytes by relative path

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
    """kubectl / ssh with hard local timeouts; stdout bytes or Failed. `runner` is injectable for tests."""

    def __init__(self, cfg: Config, runner=subprocess.run):
        self.cfg, self.runner = cfg, runner

    def _run(self, argv, stdin: bytes | None, budget, cap: float, what: str, obs: dict) -> bytes:
        t = min(cap, budget.check(0.5))
        try:
            p = self.runner(argv, input=stdin, capture_output=True, timeout=t)
        except subprocess.TimeoutExpired as e:
            obs.setdefault("calls", []).append({"what": what, "rc": "timeout"})
            raise Deadline(what + " timed out") from e
        obs.setdefault("calls", []).append({"what": what, "rc": p.returncode, "stderr": safe_text(p.stderr)})
        if p.returncode != 0:
            raise Failed(what + " failed")
        return p.stdout

    def kubectl(self, ns: str, args: list, budget, obs, what: str, stdin=None, cap=30.0) -> bytes:
        return self._run(["kubectl", "--context", self.cfg.raw["kubeContext"], "-n", ns] + args, stdin, budget, cap,
                         what, obs)

    def router(self, mode_args: list, budget, obs, what: str, cap=60.0) -> dict:
        r = self.cfg.raw["router"]
        pod = obs.get("_routerPod") or self.router_pod(budget, obs)
        raw = self.kubectl(r["namespace"], ["exec", "-i", pod["name"], "-c", r["container"], "--", "node", "-"]
                           + mode_args, budget, obs, what, stdin=self.cfg.script("relay-phase-a/remote/router.js"),
                           cap=cap)
        return json.loads(raw)

    def router_pod(self, budget, obs) -> dict:
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
        pod = {"name": p["metadata"]["name"], "uid": p["metadata"]["uid"], "restarts": cs[0]["restartCount"]}
        obs["_routerPod"] = pod
        obs["routerPod"] = pod
        return pod

    def dw(self, args: list, budget, obs, what: str, cap=60.0) -> dict:
        h = self.cfg.raw["devWorker"]
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o",
                "UserKnownHostsFile=" + h["knownHosts"], "-o", "ConnectTimeout=8", h["user"] + "@" + h["host"],
                "timeout --kill-after=2 " + str(int(cap)) + " python3 -I - " + " ".join(args)]
        for a in args:  # every argument is a validated token: no shell metacharacters reach the remote shell
            need(re.fullmatch(r"[A-Za-z0-9_./:-]{1,200}", a) is not None, "remote argument", Refuse)
        return json.loads(self._run(argv, self.cfg.script("relay-phase-a/remote/dw.py"), budget, cap + 8, what, obs))

    def sql(self, name: str, variables: dict, budget, obs) -> list:
        """One verified read-only SQL file over stdin; values only as psql variables; rows as JSON objects."""
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
        obs.setdefault("sql", {})[name] = rows
        return rows


# ------------------------------------------------------------------------------------------ shared observations
def trajectory(host, provider_key: str, provider: str, *, since=None, since_after=None, not_before=0,
               budget, obs, label: str) -> dict:
    """The driver's snapshot envelope; unsettled rows are re-polled (same since) inside the budget."""
    route = "canary-pi-" + provider
    pod_before = host.state.router_pod
    while True:
        mode = ["fixed", str(since)] if since is not None else ["after", str(since_after)]
        r = host.remote.router(["trajectory", provider_key, route] + mode + [str(int(not_before))], budget, obs,
                               "trajectory:" + label)
        env = r["envelope"]
        env["source"] = host.cfg.raw["router"]["source"]  # verified against the deployment annotation above
        ident = r["identity"]
        coverage(host, ident, obs)
        if r["unsettled"] == 0:
            break
        since = env["scope"]["since"]
        budget.sleep(2.0)
    need(pod_before is None or obs.get("_routerPod", {}).get("uid") == pod_before["uid"], "Router pod changed", Failed)
    obs.setdefault("trajectories", []).append({"label": label, "since": env["scope"]["since"], "rows": len(env["rows"]),
                                               "totalAttempts": env["totalAttempts"],
                                               "sha256": sha256_bytes(canonical(env).encode())})
    return env


def coverage(host, ident: dict, obs: dict) -> None:
    """coverageConfirmed's facts: the pinned Router pod and capture config and store, unchanged since the pre-run."""
    pod = obs["_routerPod"]
    want = host.state.router_pod
    need(ident.get("captureLevel") not in (None, "off"), "Router capture disabled", Failed)
    if want is None:
        host.state.router_pod = {"uid": pod["uid"], "restarts": pod["restarts"], "capture": ident["captureSha256"],
                                 "store": ident["storeSha256"]}
        return
    need(pod["uid"] == want["uid"] and pod["restarts"] == want["restarts"], "Router pod restarted or replaced", Failed)
    need(ident["captureSha256"] == want["capture"] and ident["storeSha256"] == want["store"],
         "Router capture config or store changed", Failed)


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
    rec = host.remote.dw(["holder", h["stateDir"]], budget, obs, "holder")
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
        "profileDigests": hello["profileDigests"], "observedMs": rec["observedMs"],
    }
    for k, v in merged.items():
        need(v is not None, "holder field missing: " + k, Failed)
    need(is_uuid(merged["incarnation"]) and is_uuid(merged["journalId"]), "holder identity", Failed)
    q = h["qualified"]
    need(merged["binarySha256"] == q["binarySha256"], "holder binary is not the qualified one", Failed)
    need(merged["profilesSha256"] == h["profilesSha256"], "holder profiles drifted", Failed)
    obs["holder"] = merged
    return merged


def clocks(host, budget, obs, gate_now: int, host_recv_ms: int) -> None:
    """C5: every pair of clocks provably within 4 s. Each remote sample is bracketed by host wall-clock stamps."""
    import time
    samples = {}
    t0 = int(time.time() * 1000)
    pod = host.remote.router(["identity"], budget, obs, "clock:router")["podNow"]
    t1 = int(time.time() * 1000)
    dwn = host.remote.dw(["clock"], budget, obs, "clock:dw")["now"]
    t2 = int(time.time() * 1000)
    # offset intervals relative to the host: remote - host lies in [sample - t_after, sample - t_before]
    samples["router"] = (pod - t1, pod - t0)
    samples["devWorker"] = (dwn - t2, dwn - t1)
    # the gate stamps gateNow just before writing the frame; the reader stamps its receipt on reading it. The pipe
    # transit (gate -> docker -> host, a local pipe) is ASSUMED to be under 500 ms; the interval is exact only under
    # that assumption, and the reader's receipt stamps are in the evidence to check it after the run.
    samples["runner"] = (gate_now - host_recv_ms, gate_now - (host_recv_ms - 500))
    samples["host"] = (0, 0)
    worst = 0
    names = list(samples)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            lo_a, hi_a = samples[a]
            lo_b, hi_b = samples[b]
            worst = max(worst, abs(hi_a - lo_b), abs(hi_b - lo_a))
    obs["clocks"] = {"intervalsMs": samples, "worstPairMs": worst}
    need(worst < CLOCK_MS, "clock skew cannot be proven under 4 s", Failed)


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
    return {"hubSource": rec["source"], "ready": True, "observedUtc": utc(int(__import__("time").time() * 1000))}


def c_renewal_deployment(host, req, ctx, budget, gate_now, obs):
    need(ctx.get("source") == host.cfg.raw["hub"]["runSource"], "C1 source is not the pinned run source")
    rec = deployment(host, budget, obs)
    need(rec["source"] == ctx["source"], "deployed hub source is not the run source", Failed)
    need(host.predecessor_ok(), "the preserved predecessor report does not match its pin", Failed)
    return {"confirmMetadataReview": True, "reportDownloaded": True, "operatorVerified": True,
            "deploymentVerified": True, "hubSource": rec["source"],
            "observedUtc": utc(int(__import__("time").time() * 1000))}


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
        r = by_binding.get(pin["binding"])
        need(r is not None, "binding row missing", Failed)
        state = r["binding_state"]
        cred = r["current_credential_id"] if state == "ready" else r["pending_credential_id"]
        need(state in ("ready", "installing") and (state == "ready" or r["current_credential_id"] is None),
             "binding state is neither ready nor installing", Failed)
        need(cred is not None and cred == r["credential_id"], "selected credential differs", Failed)
        exp = r["expires_ms"]
        need(p.get("binding") == pin["binding"] and p.get("id") == pin["id"] and p.get("run") == pin["run"]
             and p.get("credential") == cred and p.get("key") == r["router_key_id"]
             and p.get("generation") == r["credential_generation"] and p.get("revision") == r["binding_revision"]
             and p.get("namespace") == r["namespace"] == "default" and p.get("connection") == r["connection_id"]
             and p.get("expiresAt") == exp, "the renewed profile projection differs from the database", Failed)
        need(KEY.match(r["router_key_id"]) is not None and r["router_key_id"] != pin["historicalKey"],
             "renewed key is historical or malformed", Failed)
        renewed[provider] = {"key": r["router_key_id"], "credential": cred, "binding": pin["binding"],
                             "generation": r["credential_generation"], "expiresAt": exp}
    need(expires == min(v["expiresAt"] for v in renewed.values()), "C2 expiry is not the earlier one", Failed)
    host.state.renewed, host.state.expires_at = renewed, expires
    return {"confirmRenewedCredentials": True, "profiles": profiles, "expiresAt": expires}


def c_stopped_replacement(host, req, ctx, budget, gate_now, obs):
    import time
    old = ctx.get("oldScope") or {}
    pin = host.cfg.profile("codex")
    need(ctx.get("provider") == "codex" and ctx.get("runId") == pin["run"], "C3 run")
    need(ctx.get("source") == host.cfg.raw["router"]["source"], "C3 Router source")
    need(old.get("principal") == "key:" + pin["historicalKey"] and old.get("route") == "canary-pi-codex"
         and old.get("namespace") == "default" and is_int(old.get("since"), 1), "C3 old scope is not the pinned one")
    need(ctx.get("canonicalReportSha256") == host.predecessor_canonical(), "C3 report hash differs", Failed)
    need(ctx.get("carriedPrompts") == 1 and ctx.get("carriedAttempts") == 2, "C3 accounting")
    nb = ctx.get("notBefore")
    need(is_int(nb, 1), "C3 notBefore")
    env = trajectory(host, pin["historicalKey"], "codex", since=old["since"], not_before=nb, budget=budget, obs=obs,
                     label="historical")
    ids = sorted(r["id"] for r in env["rows"])
    want = host.cfg.raw["historicalRows"]  # [{"prefix","status"}]
    need(len(env["rows"]) == 2 and all(r["attempts"] == 1 for r in env["rows"]), "historical rows changed", Failed)
    for w in want:
        need(any(r["id"].startswith(w["prefix"]) and r["status"] == w["status"] for r in env["rows"]),
             "historical row missing: " + w["prefix"], Failed)
    m = marker(host, "codex", pin["run"], "baseline", nb, budget, obs)
    need(m["files"] == host.predecessor_marker_files(), "the codex marker baseline changed", Failed)
    budget.check()
    start = int(time.time() * 1000) - 2000  # stamped at reply time: the validator requires now - start <= 120 s
    end = min(start + 900_000, host.state.expires_at - 60_000, host.runner_cutoff_ms())
    need(end > start + 300_000, "less than 5 minutes left for the replacement window", Failed)
    host.state.window = (start, end)
    host.state.historical = {"trajectory": env, "marker": m}
    obs["window"] = {"start": start, "end": end, "rowIds": ids}
    return {"confirmReplacement": True, "reportDownloaded": True, "operatorVerified": True,
            "priorReportCanonicalSha256": ctx["canonicalReportSha256"], "windowStartUtc": utc(start),
            "windowEndUtc": utc(end), "trajectory": env, "marker": m}


def c_pre_launch(host, req, ctx, budget, gate_now, obs):
    import time
    provider = ctx.get("provider")
    need(provider in ("codex", "qwen") and ctx.get("phase") == "baseline", "C5 provider/phase")
    need(ctx.get("source") == host.cfg.raw["router"]["source"], "C5 Router source")
    renewed = host.state.renewed[provider]
    sc = ctx.get("scope") or {}
    need(sc == {"namespace": "default", "principal": "key:" + renewed["key"], "route": "canary-pi-" + provider},
         "C5 scope is not the renewed principal")
    need(ctx.get("holderId") == host.state.holder["incarnation"] and ctx.get("journalId") == host.state.holder["journal"],
         "C5 holder is not the handed-off holder")
    hrec = holder_record(host, budget, obs)
    need(hrec["incarnation"] == ctx["holderId"] and hrec["journalId"] == ctx["journalId"], "holder identity", Failed)
    need(hrec["sessions"] == 0 and hrec["activeSessions"] == 0 and hrec["pendingCommands"] == 0
         and hrec["admissionPaused"] is False and hrec["nativeStateReady"] and not hrec["sealed"]
         and hrec["reconciliation"] == "active", "holder not ready for a launch", Failed)
    pin = host.cfg.profile(provider)
    route = host.remote.router(["route", "canary-pi-" + provider, pin["account"], pin["model"]], budget, obs, "route")
    need(route.get("ok") is True, "the canary route is not the single approved target", Failed)
    clocks(host, budget, obs, gate_now, host.recv_ms)
    after = ctx.get("sinceAfter")
    need(after is None or is_int(after, 1), "sinceAfter")
    env = trajectory(host, renewed["key"], provider, since_after=after or int(time.time() * 1000) - 1, not_before=0,
                     budget=budget, obs=obs, label="pre-launch")
    m = marker(host, provider, ctx["runId"], "baseline", 0, budget, obs)
    run = RunState(provider, ctx["runId"], bool(ctx.get("effort")))
    run.c5_trajectory = run.last_trajectory = env
    run.baseline_marker = m
    host.state.pending = run  # appended by advance() only when this reply is sent
    return {"trajectory": env, "marker": m, "coverageConfirmed": True, "holderReadyConfirmed": True,
            "routeConfirmed": True, "clocksWithin5s": True}


def bind_session(host, run, session_id: str, budget, obs, *, resume_from=None) -> dict:
    """A session belongs to this execution only through its agent_session_launches row (plan section 3)."""
    rows = host.remote.sql("session-launch.sql", {"session_id": session_id, "tenant": host.cfg.raw["tenant"]}, budget,
                           obs)
    need(len(rows) == 1, "no single launch row for the session", Failed)
    r = rows[0]
    pin = host.cfg.profile(run.provider)
    need(r["actor_id"] == host.cfg.raw["jitUserId"] and is_int(r["created_ms"]) and r["created_ms"] >= host.run_start_ms,
         "the session was not launched by this execution's JIT actor", Failed)
    need(r["profile_id"] == pin["id"] and r["profile_revision"] == 1 and r["cwd"] == pin["cwd"],
         "the launch profile/cwd is not the pinned one", Failed)
    need(r["holder_id"] == host.state.holder["incarnation"] and r["holder_journal_id"] == host.state.holder["journal"],
         "the launch holder is not the handed-off holder", Failed)
    if resume_from is not None:
        need(r["resume_from"] == resume_from and r["resume_native_id"] == run.native_id
             and r["resume_acknowledge_unknown_tool_effects"] is False, "the resume is not bound to the closed session",
             Failed)
    host.state.sessions[session_id] = run
    return r


def c_post_open(host, req, ctx, budget, gate_now, obs):
    run = run_for(host.state, ctx)
    need(ctx.get("phase") == "baseline" and is_uuid(ctx.get("sessionId")) and is_int(ctx.get("generation"), 1)
         and is_int(ctx.get("notBefore"), 1), "C6 context")
    launch = bind_session(host, run, ctx["sessionId"], budget, obs)
    run.session_id, run.generation, run.launch_id = ctx["sessionId"], ctx["generation"], launch["id"]
    run.native_id = launch.get("native_id")
    renewed = host.state.renewed[run.provider]
    rows = host.remote.sql("delivery.sql", {"launch_id": launch["id"], "session_id": ctx["sessionId"],
                                            "binding_id": renewed["binding"], "credential_id": renewed["credential"],
                                            "tenant": host.cfg.raw["tenant"]}, budget, obs)
    need(len(rows) == 1, "no single install receipt for this launch", Failed)
    r = rows[0]
    need(r["holder_id"] == host.state.holder["incarnation"] and r["generation"] == renewed["generation"]
         and r["rendered"] is True and r["render_failure"] is None and r["install_state"] == "completed"
         and r["receipt_status"] == "completed",
         "the install/render receipt is not terminal-installed", Failed)
    need(r["consumer_state"] == "active" and r["consumer_lease_ok"] is True, "no active consumer lease", Failed)
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
    return out


def c_permission(host, req, ctx, budget, gate_now, obs):
    run = run_for(host.state, ctx)
    sid, gen, kind, opt = ctx.get("sessionId"), ctx.get("generation"), ctx.get("decisionKind"), ctx.get("optionId")
    need(sid == run.session_id and host.state.sessions.get(sid) is run and gen == run.generation,
         "C8 session is not this run's bound session")
    perm = ctx.get("permission") or {}
    need(is_uuid(perm.get("id")) and isinstance(opt, str) and kind in ("allow_once", "reject_once"), "C8 context")
    rows = host.remote.sql("permission.sql", {"permission_id": perm["id"], "session_id": sid,
                                              "tenant": host.cfg.raw["tenant"]}, budget, obs)
    need(len(rows) == 1, "no single pending permission", Failed)
    r = rows[0]
    need(r["state"] == "pending" and r["unexpired"] is True, "the permission is not pending and unexpired", Failed)
    pctx = r["context"] or {}
    options = [o for o in pctx.get("options") or [] if isinstance(o, dict)]
    matching = [o for o in options if o.get("optionId") == opt]
    need(len(matching) == 1 and matching[0].get("kind") == kind, "the option is not exactly one of that kind", Failed)
    title, desc = pctx.get("title"), pctx.get("description")
    obs["permission"] = {"title": safe_text(title), "description": safe_text(desc), "kind": kind}
    purpose = ctx.get("purpose")
    if purpose == "relay-message-read":
        need(title == READ_TITLE and kind == "allow_once", "relay read must be the exact allow_once read", Failed)
        inp = read_input(desc)
        need(run.reads < host.cfg.raw["maxReadsPerRun"], "relay read cap reached", Failed)
        run.reads += 1
        obs["permission"]["read"] = sorted(inp)
        return True
    need(purpose == "marker", "unknown permission purpose", Failed)
    need(not run.effort, "no marker permission in the effort run", Failed)
    cmd = marker_input(desc)
    renders = {k: host.marker_command(run.provider, run.run_id, k) for k in ("denied", "effects")}
    step = [k for k, v in renders.items() if v == cmd]
    need(len(step) == 1 and ctx.get("expectedCommand") == cmd, "the marker command is not the rendering", Failed)
    want = {"denied": "reject_once", "effects": "allow_once"}[step[0]]
    need(kind == want, "the decision is not bound to this command", Failed)
    need(run.decisions[step[0]] == 0, "this marker decision was already confirmed", Failed)
    run.decisions[step[0]] += 1
    obs["permission"]["marker"] = step[0]
    return True


def _desc_json(desc) -> dict:
    need(isinstance(desc, str) and len(desc) <= 16384, "permission description", Failed)
    start = desc.find("{")
    need(start >= 0, "permission input missing", Failed)
    try:
        v = json.loads(desc[start:])
    except ValueError as e:
        raise Failed("permission input malformed") from e
    need(isinstance(v, dict), "permission input", Failed)
    return v


def read_input(desc) -> dict:
    v = _desc_json(desc)
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
    v = _desc_json(desc)
    need(set(v) <= {"command", "timeout"} and isinstance(v.get("command"), str), "marker input shape", Failed)
    if "timeout" in v:
        need(is_int(v["timeout"], 1) and v["timeout"] <= 120, "marker timeout", Failed)
    return v["command"]


def c_recover_history(host, req, ctx, budget, gate_now, obs):
    run = run_for(host.state, ctx)
    need(ctx.get("sessionId") == run.session_id and ctx.get("generation") == run.generation, "C9 session")
    need(ctx.get("holderId") == host.state.holder["incarnation"] and ctx.get("journalId") == host.state.holder["journal"],
         "C9 holder")
    hrec = holder_record(host, budget, obs)
    need(hrec["incarnation"] == ctx["holderId"] and hrec["journalId"] == ctx["journalId"] and hrec["sessions"] == 0
         and hrec["activeSessions"] == 0 and hrec["pendingCommands"] == 0, "holder not settled", Failed)
    rows = host.remote.sql("session-commands.sql", {"session_id": run.session_id, "generation": run.generation,
                                                    "tenant": host.cfg.raw["tenant"]}, budget, obs)
    control = [r for r in rows if r["table"] == "control"]
    agent = [r for r in rows if r["table"] == "agent"]
    need(control and all(r["state"] == "completed" and r["receipt_status"] == "completed" for r in control),
         "a control command is not completed", Failed)
    need(all(r["kind"] == "permission-decision" and r["receipt_status"] == "accepted" for r in agent),
         "an agent command is not an accepted permission decision", Failed)
    perms = host.remote.sql("session-permissions.sql", {"session_id": run.session_id,
                                                        "tenant": host.cfg.raw["tenant"]}, budget, obs)
    need(all(p["state"] != "pending" for p in perms), "a permission is still pending", Failed)
    t = host.remote.dw(["transcript", host.native_home(run)], budget, obs, "transcript")
    if t.get("state") != "observed":
        raise Unsupported("the native transcript source is not established")
    need(t["unsettled"] == [], "a native tool call has no result", Failed)
    want = sorted(sha256_bytes(host.marker_command(run.provider, run.run_id, k).encode()) for k in ("denied", "effects"))
    need(sorted(s["sha256"] for s in t["shellCommands"]) == want, "shell calls are not exactly the two markers", Failed)
    m = marker(host, run.provider, run.run_id, "approved", 0, budget, obs)
    need(m["files"]["effects"].get("sha256") == run.approved_marker_sha, "the approved marker changed", Failed)
    return True


def c_reconcile_choice(host, req, ctx, budget, gate_now, obs):
    raise Unsupported("no authoritative reconstruction-required field is pinned (plan section 7, gate)")


def c_retire_branch(host, req, ctx, budget, gate_now, obs):
    run = run_for(host.state, ctx)
    need(run.effort and ctx.get("sessionId") == run.session_id and ctx.get("generation") == run.generation, "C11 run")
    t = host.remote.dw(["transcript", host.native_home(run)], budget, obs, "transcript")
    if t.get("state") != "observed":
        raise Unsupported("the native transcript source is not established")
    need(t["thinkingLevels"] and t["thinkingLevels"][-1] == "high", "the applied effort is not high", Failed)
    need(t["configChangedNotices"], "no SESSION_CONFIGURATION_CHANGED notice in the native record", Failed)
    rows = host.remote.sql("session-commands.sql", {"session_id": run.session_id, "generation": run.generation,
                                                    "tenant": host.cfg.raw["tenant"]}, budget, obs)
    need(any(r["kind"] == "prompt" and r["stop_reason"] == "refusal" for r in rows), "no refused probe prompt", Failed)
    renewed = host.state.renewed[run.provider]
    env = trajectory(host, renewed["key"], run.provider, since=run.last_trajectory["scope"]["since"], not_before=0,
                     budget=budget, obs=obs, label="retire")
    rows_retained(run.last_trajectory, env)
    need(env["totalAttempts"] == run.last_trajectory["totalAttempts"], "attempts were added", Failed)
    return True


def c_predecessor(host, req, ctx, budget, gate_now, obs):
    raise Unsupported("the predecessor projection pin contract is unresolved (plan section 6, gate)")


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
