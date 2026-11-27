"""relay_phase_a_host.py: the phase A observer's host glue, the durable handoff ledger, the pre-run holder step and the
preserving-holder handoff (C4) (plans/2026-11-27-relay-phase-a-observer-plan.md, sections 5, 6 and 9).

The handoff is the observer's only mutation. Every mutating stage is preceded by a durable ledger intent and followed
by a terminal receipt; nothing is ever rolled back; a later run that finds ANY ledger record refuses (no automatic
re-entry: the operator classifies a previous attempt read-only first, as the runbook describes).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import relay_phase_a_collectors as C
from relay_phase_a_observer import Evidence, Failed, Fault, Host, Refuse, canonical, is_uuid, need, sha256_bytes


# ------------------------------------------------------------------------------------------ ledger
class Ledger:
    """Append-only JSONL with a per-record checksum, outside any run directory. Torn or unreadable = unusable."""

    def __init__(self, root: str, preparation: str):
        need(is_uuid(preparation), "ledger preparation", Fault)
        self.dir = Path(root)
        self.path = self.dir / f"{preparation}.jsonl"
        self.seq = 0

    @staticmethod
    def _crc(rec: dict) -> str:
        return sha256_bytes(canonical({k: v for k, v in rec.items() if k != "crc"}).encode())[:16]

    def records(self) -> list:
        if not self.path.exists():
            return []
        out = []
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise Fault("the handoff ledger has a torn record")
        for line in raw.splitlines():
            try:
                rec = json.loads(line)
            except ValueError as e:
                raise Fault("the handoff ledger is unreadable") from e
            if not isinstance(rec, dict) or rec.get("crc") != self._crc(rec):
                raise Fault("the handoff ledger has a corrupt record")
            out.append(rec)
        return out

    def require_empty(self) -> None:
        if self.records():
            raise Refuse("the handoff ledger already holds a previous attempt; classify it read-only first "
                         "(docs/runbooks/relay.md, observer ledger recovery)")

    def append(self, **rec) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)  # under %LOCALAPPDATA%: private to the user by inheritance
        if os.name != "nt":
            os.chmod(self.dir, 0o700)
        self.seq += 1
        rec.update(seq=self.seq, t=round(time.time(), 3))
        rec["crc"] = self._crc(rec)
        line = (json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n").encode()
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            if os.write(fd, line) != len(line):
                raise Fault("short ledger write")
            os.fsync(fd)
        finally:
            os.close(fd)


# ------------------------------------------------------------------------------------------ the host
class ObserverHost(Host):
    """Host plus the facts the collectors need: remote access, the run start and cutoff, the Lease, the predecessor."""

    def __init__(self, *, gate, cfg: C.Config, evidence: Evidence, on_fault, run_start_ms: int, runner_cutoff_ms: int,
                 lease_ok, predecessor_bytes: bytes, ledger: Ledger, remote: C.Remote | None = None):
        super().__init__(gate=gate, collectors={**C.COLLECTORS, ("checkpoint", "preserving-holder"): c_preserving_holder},
                         cfg=cfg, evidence=evidence, on_fault=on_fault)
        self.remote = remote or C.Remote(cfg)
        self.run_start_ms, self._cutoff, self.lease_ok = run_start_ms, runner_cutoff_ms, lease_ok
        self.predecessor_bytes, self.ledger = predecessor_bytes, ledger
        self.recv_ms = 0

    def handle(self, seq, frame, recv_ms=None):
        self.recv_ms = recv_ms if recv_ms is not None else int(time.time() * 1000)
        return super().handle(seq, frame, recv_ms)

    def runner_cutoff_ms(self) -> int:
        return self._cutoff

    def predecessor_ok(self) -> bool:
        return sha256_bytes(self.predecessor_bytes) == self.cfg.raw["predecessor"]["rawSha256"]

    def predecessor_canonical(self) -> str:
        return sha256_bytes(canonical(json.loads(self.predecessor_bytes)).encode())

    def predecessor_marker_files(self) -> dict:
        return self.cfg.raw["predecessor"]["markerFiles"]

    def marker_command(self, provider: str, run: str, kind: str) -> str:
        """The driver's markerCommand() rendering (owner-phase-a.js), from the pinned workspace root."""
        need(provider in ("codex", "qwen") and is_uuid(run) and kind in ("denied", "effects"), "marker selection")
        root = self.cfg.raw["markerRoot"] + "/" + self.cfg.profile(provider)["id"]
        return ("umask 077; printf '%s\\n' 'relay-pi-" + provider + "-" + run + "' >> '" + root + "/acceptance-" + run
                + "." + kind + "'")

    def native_home(self, run) -> str:
        need(is_uuid(run.session_id), "native home session")
        return self.cfg.raw["holder"]["stateDir"] + "/native-homes/" + run.session_id

    def authorize_mutation(self, budget) -> None:
        """Before EVERY mutating stage: the Lease, no cancellation, and inside the C3 window."""
        budget.check(2.0)
        need(self.lease_ok(), "the Lease is no longer held", Fault)
        now = int(time.time() * 1000)
        w = self.state.window
        need(w is not None and w[0] <= now < w[1], "outside the granted replacement window", Failed)


# ------------------------------------------------------------------------------------------ pre-run (orchestrator)
def prerun(host: ObserverHost, budget) -> dict:
    """Before the gate and the Runner, under the Lease: verify the pinned handoff state read-only, then start ONLY the
    holder and prove it came up paused. A holder that comes up unpaused is stopped at once (containment)."""
    h = host.cfg.raw["holder"]
    obs: dict = {}
    host.ledger.require_empty()
    prerun_checks(host, budget, obs)
    host.ledger.append(op="pre-run-start", phase="intent", stateDir=h["stateDir"])
    started = host.remote.dw(["mutate-start-holder", h["stateDir"]], budget, obs, "mutate-start-holder", cap=70)
    hrec = started.get("holder") or {}
    status, hello = hrec.get("status"), hrec.get("hello")
    paused = bool(status) and status.get("admissionPaused") is True
    if not (started.get("started") and paused):
        if hello is not None and not paused:
            stop = host.remote.dw(["mutate-stop-holder"], budget, obs, "mutate-stop-holder (containment)", cap=40)
            host.ledger.append(op="pre-run-start", phase="receipt", outcome="contained-unpaused", stop=stop)
        else:
            host.ledger.append(op="pre-run-start", phase="receipt", outcome="failed")
        raise Fault("the holder did not start paused")
    need(status["activeSessions"] == 0 and status["pendingCommands"] == 0 and status["nativeStateReady"] is True
         and not status["sealed"] and hello["sessions"] == 0 and hello["incarnation"] == status["incarnation"],
         "the started holder is not drained and ready", Fault)
    inc = hello["incarnation"]
    host.state.holder = {"incarnation": inc, "journal": hello["journalId"],
                         "mainPid": hrec["units"]["relay-holder.service"]["mainPid"],
                         "revision": status["admissionRevision"]}
    host.ledger.append(op="pre-run-start", phase="receipt", outcome="ok", **host.state.holder)
    host.ev.write(event="pre-run", outcome="ok", observations=obs, holder=host.state.holder)
    return host.state.holder


def prerun_checks(host, budget, obs) -> dict:
    """The pre-run step's read-only half (also the probe's): the pinned receipt, units, state dir and paused journal."""
    h = host.cfg.raw["holder"]
    facts = host.remote.dw(["prerun", h["stateDir"], h["preparationId"]], budget, obs, "prerun")
    r, u, j = facts["receipt"], facts["units"], facts["journal"]
    need(facts["receiptSha256"] == h["receiptSha256"] and r["operationId"] == h["preparationId"]
         and r["newState"] == h["stateDir"] and r["oldState"] == h["oldStateDir"], "the handoff receipt is not the pinned one",
         Failed)
    hold, rec = u["relay-holder.service"], u["relay-holder-control-plane.service"]
    need(hold["active"] == "inactive" and rec["active"] == "inactive", "a holder unit is already active", Failed)
    need(hold["argv"] == h["holderArgv"] and rec["argv"] == h["reconciliationArgv"] and not hold["dropIns"]
         and not rec["dropIns"], "the units are not the pinned ones", Failed)
    need(facts["stateDirMode"] == "0o700", "the state directory is not private", Failed)
    need(j["admission"] is not None and j["admission"]["paused"] is True
         and j["admission"]["operationId"] == h["pauseOperationId"], "the journal does not end paused", Failed)
    return facts




# ------------------------------------------------------------------------------------------ C4
def c_preserving_holder(host: ObserverHost, req, ctx, budget, gate_now, obs):
    cfg, h = host.cfg, host.cfg.raw["holder"]
    q = ctx.get("qualifiedHolder") or {}
    need(q == h["qualified"], "C4 qualified holder is not the pinned one")
    need(ctx.get("oldHolderId") == h["oldHolderId"] and ctx.get("oldJournalId") == h["oldJournalId"],
         "C4 predecessor identities are not the pinned ones")
    started_at = ctx.get("startedAt")
    need(isinstance(started_at, int) and started_at > 0, "C4 startedAt")
    pre = host.state.holder
    need(pre is not None, "the pre-run step did not start the holder", Fault)
    cur = C.holder_record(host, budget, obs)
    need(cur["incarnation"] == pre["incarnation"] and cur["holderMainPid"] == pre["mainPid"]
         and cur["admissionPaused"] is True and cur["activeSessions"] == 0 and cur["pendingCommands"] == 0,
         "the holder changed since the pre-run step", Failed)
    rev = cur["admissionRevision"]
    op = str(uuid.uuid4())
    host.authorize_mutation(budget)
    host.ledger.append(op="resume", phase="intent", operationId=op, revision=rev)
    res = host.remote.dw(["mutate-resume", h["stateDir"], str(rev), op], budget, obs, "mutate-resume", cap=40)
    jr = (res.get("journal") or {}).get("admission") or {}
    ok = res.get("resumed") is True and (res.get("status") or {}).get("admissionPaused") is False \
        and jr.get("paused") is False and jr.get("operationId") == op
    host.ledger.append(op="resume", phase="receipt", outcome="ok" if ok else "failed", operationId=op)
    need(ok, "the resume was not confirmed by status and journal", Failed)
    host.authorize_mutation(budget)
    host.ledger.append(op="enable-start", phase="intent")
    es = host.remote.dw(["mutate-enable-start"], budget, obs, "mutate-enable-start", cap=30)
    ru = (es.get("units") or {}).get("relay-holder-control-plane.service") or {}
    hu = (es.get("units") or {}).get("relay-holder.service") or {}
    ok = es.get("enabled") is True and es.get("started") is True and ru.get("active") == "active" \
        and ru.get("sub") == "running" and ru.get("enabled") == "enabled" and hu.get("enabled") == "enabled" \
        and ru.get("argv") == h["reconciliationArgv"] and ru.get("restarts") == 0
    host.ledger.append(op="enable-start", phase="receipt", outcome="ok" if ok else "failed")
    need(ok, "reconciliation is not running cleanly from the pinned unit", Failed)
    resumed_ms = int(time.time() * 1000)
    while True:  # read back: holder unchanged and resumed; the hub's health row after the resume
        fin = C.holder_record(host, budget, obs)
        rows = host.remote.sql("holder-health.sql", {"holder_id": pre["incarnation"], "tenant": cfg.raw["tenant"]},
                               budget, obs)
        good = len(rows) == 1 and rows[0]["journal_id"] == pre["journal"] and rows[0]["admission_paused"] is False \
            and rows[0]["native_state_ready"] is True and rows[0]["observed_ms"] > resumed_ms - 2000
        if good:
            break
        budget.sleep(3.0)
    need(fin["incarnation"] == pre["incarnation"] and fin["holderMainPid"] == pre["mainPid"]
         and fin["admissionPaused"] is False and fin["activeSessions"] == 0 and fin["pendingCommands"] == 0
         and fin["nativeStateReady"] and fin["nativeRecovery"] == 2, "the resumed holder is not ready", Failed)
    host.ledger.append(op="handoff", phase="receipt", outcome="ok", holder=pre["incarnation"])
    observed = int(time.time() * 1000)
    need(observed >= started_at, "C4 observation precedes its start")
    return {
        "holderId": pre["incarnation"], "journalId": pre["journal"],
        "oldHolderId": ctx["oldHolderId"], "oldJournalId": ctx["oldJournalId"],
        "preparationId": h["preparationId"], "stateDirectory": h["stateDir"],
        "handoffReceipt": {"path": h["receiptPath"], "sha256": h["receiptSha256"]},
        "observedUtc": C.utc(observed), "qualifiedRelease": q["release"], "holderSource": q["source"],
        "holderBinarySha256": fin["binarySha256"], "profilesSha256": fin["profilesSha256"],
        "nativeRecovery": fin["nativeRecovery"], "nativeStateReady": True, "activeSessions": 0, "pendingCommands": 0,
        "admissionPaused": False, "preservedOldStateConfirmed": True, "operatorVerified": True,
    }


# ------------------------------------------------------------------------------------------ the read-only probe
class _ProbeHost:
    """Just enough of ObserverHost for the read-only collectors' helpers (no gate, no Lease, no authority)."""

    def __init__(self, cfg, remote):
        from relay_phase_a_observer import ObserverState
        self.cfg, self.remote, self.state = cfg, remote, ObserverState()


PREDECESSOR_SESSION = "45724087-335a-4c55-ad0f-cb278afe608a"


def probe(repo: Path) -> dict:
    """Every read-only source the observer uses, against the live estate, with its latency. Phase-dependent
    predicates (renewed installation, live permissions, recovery, retirement) are reported as not yet applicable."""
    import threading
    from relay_phase_a_observer import Budget
    root = Path(repo) / "scripts"
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / "relay-phase-a").rglob("*") if p.is_file()}
    cfg = C.Config.load(files["relay-phase-a/observer-config.json"], files)
    cfg.raw["devWorker"]["knownHosts"] = str(root / cfg.raw["devWorker"]["knownHosts"])
    host = _ProbeHost(cfg, C.Remote(cfg))
    checks = {}

    def check(name, fn):
        obs: dict = {}
        t0 = time.monotonic()
        try:
            detail = fn(Budget(90_000, threading.Event()), obs)
            checks[name] = {"ok": True, "ms": int((time.monotonic() - t0) * 1000), "detail": detail,
                            "calls": obs.get("calls")}
        except Exception as e:  # noqa: BLE001 - every check is reported; none aborts the probe
            checks[name] = {"ok": False, "ms": int((time.monotonic() - t0) * 1000),
                            "error": type(e).__name__ + ": " + str(e)[:200], "calls": obs.get("calls")}

    def now_ms():
        return int(time.time() * 1000)

    check("hub-deployment", lambda b, o: C.deployment(host, b, o))
    check("router-pod-identity", lambda b, o: [host.remote.router_pod(b, o),
                                               host.remote.router(["identity"], b, o, "identity")][1])
    for p in ("codex", "qwen"):
        key, prof = cfg.profile(p)["historicalKey"], cfg.profile(p)
        check(f"trajectory-shape-{p}", lambda b, o, key=key, p=p: {
            k: v for k, v in C.trajectory(host, key, p, since_after=now_ms() - 1, budget=b, obs=o,
                                          label="probe").items() if k != "rows"})
        check(f"marker-baseline-{p}", lambda b, o, p=p, prof=prof: C.marker(host, p, prof["run"], "baseline", 0, b, o)
              ["files"])
        check(f"route-{p}", lambda b, o, p=p, prof=prof: host.remote.router(
            ["route", "canary-pi-" + p, prof["account"], prof["model"]], b, o, "route"))
    check("clocks", lambda b, o: [C.clocks(host, b, o, now_ms(), now_ms()), o["clocks"]][1])
    t = cfg.raw["tenant"]
    sqls = {
        "renewed-credentials.sql": {"codex_binding": cfg.profile("codex")["binding"],
                                    "qwen_binding": cfg.profile("qwen")["binding"], "tenant": t},
        "session-launch.sql": {"session_id": PREDECESSOR_SESSION, "tenant": t},
        "session-commands.sql": {"session_id": PREDECESSOR_SESSION, "generation": 1, "tenant": t},
        "session-permissions.sql": {"session_id": PREDECESSOR_SESSION, "tenant": t},
        "permission.sql": {"permission_id": "70fb1b8e-f7cc-4cc3-b98f-66f83e46d1ab",
                           "session_id": PREDECESSOR_SESSION, "tenant": t},
        "holder-health.sql": {"holder_id": cfg.raw["holder"]["oldHolderId"], "tenant": t},
        "delivery.sql": {"launch_id": "4a49cdab-0e0e-4dd2-866d-d87b1238c2f2", "session_id": PREDECESSOR_SESSION,
                         "binding_id": cfg.profile("codex")["binding"],
                         "credential_id": "87482892-42ab-40a5-aa03-01c41ec5f6df", "tenant": t},
    }
    for name, v in sqls.items():
        check("sql-" + name, lambda b, o, name=name, v=v: {"rows": len(host.remote.sql(name, v, b, o)),
                                                           "keys": sorted({k for r in o["sql"][name] for k in r})})
    check("prerun-read-only", lambda b, o: {k: v for k, v in prerun_checks(host, b, o).items() if k != "units"})
    check("native-transcript-format", lambda b, o: host.remote.dw(
        ["transcript", cfg.raw["holder"]["oldStateDir"] + "/native-homes/" + PREDECESSOR_SESSION], b, o,
        "transcript"))
    check("predecessor-staging", lambda b, o: _probe_staging())
    na = ["renewed installation (C6)", "live permissions (C8)", "recovery (C9)", "retirement (C11)",
          "reconcile field (C10, gate)", "predecessor projection (gate)"]
    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks, "notYetApplicable": na}


def _probe_staging() -> dict:
    """The predecessor staging on the real Docker backend: 10001-owned, 0400, single-link, read-only mount."""
    import subprocess
    import uuid as _uuid
    from relay_operator_session import PROXY_IMAGE

    def run(args, **kw):
        return subprocess.run(["docker"] + args, capture_output=True, timeout=120, **kw)

    vol = "relay-observer-probe-" + _uuid.uuid4().hex[:8]
    stat_py = ("import os, stat, json; s = os.lstat('/predecessor/report.json'); print(json.dumps({'uid': s.st_uid, "
               "'mode': oct(stat.S_IMODE(s.st_mode)), 'links': s.st_nlink, 'regular': stat.S_ISREG(s.st_mode), "
               "'writable': os.access('/predecessor/report.json', os.W_OK)}))")
    try:
        need(run(["volume", "create", vol]).returncode == 0, "volume create", Failed)
        p = run(["run", "-i", "--rm", "--network", "none", "--mount", f"type=volume,src={vol},dst=/p", PROXY_IMAGE,
                 "sh", "-c", "umask 077 && cat > /p/report.json && chown 10001:10001 /p /p/report.json && chmod 0400 "
                 "/p/report.json && chmod 0500 /p"], input=b'{"probe": true}')
        need(p.returncode == 0, "staging failed", Failed)
        q = run(["run", "--rm", "--network", "none", "--user", "10001:10001", "--mount",
                 f"type=volume,src={vol},dst=/predecessor,readonly", PROXY_IMAGE, "python3", "-c", stat_py])
        d = json.loads(q.stdout)
        need(d == {"uid": 10001, "mode": "0o400", "links": 1, "regular": True, "writable": False},
             "staged predecessor is not 10001/0400/single-link/read-only", Failed)
        return d
    finally:
        run(["volume", "rm", "-f", vol])
