"""relay_phase_a_host.py: the phase A observer's host glue, the durable handoff ledger, the pre-run holder step and the
preserving-holder handoff (C4) (plans/2026-11-27-relay-phase-a-observer-plan.md, sections 5, 6 and 9).

The handoff is the observer's only mutation. The ledger is reserved EXCLUSIVELY before the first one; every mutating
stage is preceded by a durable intent and ends with a terminal receipt, including on an exception or timeout (then a
read-only readback classifies the state; terminating a local command is never taken to mean the remote one stopped).
Nothing is ever rolled back, and a later run that finds the ledger refuses (no automatic re-entry: the operator
classifies the previous attempt read-only first, as the runbook describes).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import relay_phase_a_collectors as C
from relay_phase_a_observer import (Deadline, Evidence, Failed, Fault, Host, Refuse, canonical, is_uuid, need,
                                    sha256_bytes)


# ------------------------------------------------------------------------------------------ ledger
class Ledger:
    """Append-only JSONL with a per-record checksum, outside any run directory, in a directory private to the user.
    `reserve()` creates the file EXCLUSIVELY (a competing or stale run cannot pass the emptiness check too)."""

    def __init__(self, root: str, preparation: str):
        need(is_uuid(preparation), "ledger preparation", Fault)
        self.dir = Path(root)
        self.path = self.dir / f"{preparation}.jsonl"
        self.seq = 0
        self.fd = None

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

    def _private_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":  # %LOCALAPPDATA% inheritance is NOT private (other local principals can read it)
            user = os.environ.get("USERNAME") or ""
            need(bool(user), "no USERNAME for the ledger ACL", Fault)
            p = subprocess.run(["icacls", str(self.dir), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
                               capture_output=True, timeout=30)
            need(p.returncode == 0, "the ledger directory ACL could not be set", Fault)
            acl = subprocess.run(["icacls", str(self.dir)], capture_output=True, timeout=30).stdout.decode(
                errors="replace")
            grants = [line.strip() for line in acl.splitlines()[0:] if ":(" in line]
            need(grants and all(f"\\{user}:" in g or g.split(" ", 1)[-1].startswith(user + ":") for g in grants)
                 and len(grants) == 1, "the ledger directory is not private to the user", Fault)
        else:
            os.chmod(self.dir, 0o700)
            need((os.stat(self.dir).st_mode & 0o077) == 0, "the ledger directory is not private", Fault)

    def reserve(self, **rec) -> None:
        """Exclusive creation with the first record; refuses if any ledger exists for this preparation."""
        self._private_dir()
        if self.path.exists():
            self.records()  # a torn/corrupt ledger is a Fault; a readable one is a refusal
            raise Refuse("the handoff ledger already holds a previous attempt; classify it read-only first "
                         "(docs/runbooks/relay.md, observer ledger recovery)")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            self.fd = os.open(self.path, flags, 0o600)
        except FileExistsError as e:
            raise Refuse("the handoff ledger was created concurrently") from e
        self.append(**rec)

    def append(self, **rec) -> None:
        need(self.fd is not None, "the ledger is not reserved", Fault)
        self.seq += 1
        rec.update(seq=self.seq, t=round(time.time(), 3))
        rec["crc"] = self._crc(rec)
        line = (json.dumps(rec, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode()
        view = memoryview(line)
        while view:
            n = os.write(self.fd, view)
            if n <= 0:
                raise Fault("short ledger write")
            view = view[n:]
        os.fsync(self.fd)

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


# ------------------------------------------------------------------------------------------ the host
class ObserverHost(Host):
    """Host plus the facts the collectors need: remote access, the run start and cutoff, the Lease, the predecessor."""

    def __init__(self, *, gate, cfg: C.Config, evidence: Evidence, on_fault, run_start_ms: int, runner_cutoff_ms: int,
                 lease_ok, predecessor_bytes: bytes, predecessor_sha256: str, ledger: Ledger,
                 remote: C.Remote | None = None):
        super().__init__(gate=gate, collectors={**C.COLLECTORS, ("checkpoint", "preserving-holder"): c_preserving_holder},
                         cfg=cfg, evidence=evidence, on_fault=on_fault, lease_ok=lease_ok)
        self.remote = remote or C.Remote(cfg)
        self.run_start_ms, self._cutoff = run_start_ms, runner_cutoff_ms
        self.predecessor_bytes, self.predecessor_sha256, self.ledger = predecessor_bytes, predecessor_sha256, ledger
        self._history = None

    def runner_cutoff_ms(self) -> int:
        return self._cutoff

    def predecessor_ok(self) -> bool:
        return sha256_bytes(self.predecessor_bytes) == self.predecessor_sha256

    def predecessor_canonical(self) -> str:
        return sha256_bytes(canonical(json.loads(self.predecessor_bytes)).encode())

    def predecessor_history(self) -> dict:
        """The stopped run's original scope and marker baseline, from the independent report (the driver's own
        stoppedPredecessor() reads runs[0].firstSnapshot and the single pre-launch evidence record)."""
        if self._history is None:
            rep = json.loads(self.predecessor_bytes)
            run = rep["runs"][0]
            pre = [e for e in rep["evidence"] if e.get("kind") == "pre-launch"]
            need(len(pre) == 1 and run.get("runId") == self.cfg.profile("codex")["run"], "predecessor report shape",
                 Failed)
            scope = run["firstSnapshot"]["scope"]
            files = pre[0]["marker"]["files"]
            need(isinstance(scope, dict) and isinstance(files, dict), "predecessor report shape", Failed)
            self._history = {"scope": scope, "markerFiles": files}
        return self._history

    def marker_command(self, provider: str, run: str, kind: str) -> str:
        """The driver's markerCommand() rendering (owner-phase-a.js), from the pinned workspace root."""
        need(provider in ("codex", "qwen") and is_uuid(run) and kind in ("denied", "effects"), "marker selection")
        root = self.cfg.raw["markerRoot"] + "/" + self.cfg.profile(provider)["id"]
        return ("umask 077; printf '%s\\n' 'relay-pi-" + provider + "-" + run + "' >> '" + root + "/acceptance-" + run
                + "." + kind + "'")

    def native_home(self, run) -> str:
        need(is_uuid(run.session_id), "native home session")
        return self.cfg.raw["holder"]["stateDir"] + "/native-homes/" + run.session_id

    def mutation_cap(self, budget, cap: float) -> float:
        """Before EVERY mutating stage: the Lease, no cancellation, inside the C3 window; the stage is bounded by the
        least of its cap, the request budget, the window end and the runner cutoff."""
        budget.check(2.0)
        need(self.lease_ok(), "the Lease is no longer held", Fault)
        now = int(time.time() * 1000)
        w = self.state.window
        need(w is not None and w[0] <= now < w[1], "outside the granted replacement window", Failed)
        left = min(w[1], self._cutoff) - now
        need(left > 5000, "too little of the window is left for a mutation", Failed)
        return min(cap, left / 1000.0, budget.remaining() - 2.0)


# ------------------------------------------------------------------------------------------ pre-run (orchestrator)
def prerun_checks(host, budget, obs) -> dict:
    """The pre-run's read-only half (also the probe's): the pinned receipt and its preserved old journal, the sealed
    old root, the prepared unsealed new root, the installed artifacts and the units' effective definitions."""
    h = host.cfg.raw["holder"]
    f = host.remote.dw(["prerun", h["stateDir"], h["preparationId"]], budget, obs, "prerun")
    r, u, j, pj = f["receipt"], f["units"], f["journal"], f["receipt"]["preservedJournal"]
    need(f["receiptSha256"] == h["receiptSha256"] and r["operationId"] == h["preparationId"]
         and r["newState"] == h["stateDir"] and r["oldState"] == h["oldStateDir"]
         and r["state"] == "prepared-stopped-new-state", "the handoff receipt is not the pinned one", Failed)
    need(pj["incarnation"] == h["oldHolderId"] and pj["journalId"] == h["oldJournalId"] and pj["sealId"] == h["sealId"]
         and pj["sha256"] == h["oldJournalSha256"] and pj["unknownCommands"] == 0,
         "the receipt does not preserve the pinned old journal", Failed)
    last = f["oldJournalLast"] or {}
    need(f["oldJournalSha256"] == h["oldJournalSha256"] and last.get("kind") == "holder-maintenance-seal"
         and last.get("sealId") == h["sealId"] and last.get("sealed") is True
         and last.get("expectedIncarnation") == h["oldHolderId"], "the old root is not sealed as preserved", Failed)
    need(j["lastStartedIncarnation"] == h["newJournalId"] and j["admission"] is not None
         and j["admission"]["paused"] is True and j["admission"]["operationId"] == h["pauseOperationId"],
         "the new root's journal is not the prepared, paused one", Failed)
    need(f["artifacts"] == h["artifacts"], "an installed unit, service or profiles file is not the pinned one", Failed)
    hold, rec = u["relay-holder.service"], u["relay-holder-control-plane.service"]
    need(hold["active"] == "inactive" and rec["active"] == "inactive", "a holder unit is already active", Failed)
    need(hold["argv"] == h["holderArgv"] and rec["argv"] == h["reconciliationArgv"] and not hold["dropIns"]
         and not rec["dropIns"], "the units are not the pinned ones", Failed)
    need(f["stateDirMode"] == "0o700", "the state directory is not private", Failed)
    obs["preservation"] = {"oldJournalSha256": f["oldJournalSha256"], "sealId": last.get("sealId"),
                           "newJournal": j["lastStartedIncarnation"], "artifacts": f["artifacts"]}
    return f


def prerun(host: ObserverHost, budget) -> dict:
    """Before the gate and the Runner, under the Lease and before any Relay authority: verify everything read-only,
    pin the Router identity, reserve the ledger, then start ONLY the holder. dw.py contains an unpaused start on the
    host itself; a lost response is classified by an independent readback, and contained if needed."""
    h = host.cfg.raw["holder"]
    obs: dict = {}
    need(host.lease_ok(), "the Lease is not held", Fault)
    prerun_checks(host, budget, obs)
    host.state.preservation = obs["preservation"]
    C.pin_router(host, budget, obs)
    host.ledger.reserve(op="pre-run-start", phase="intent", stateDir=h["stateDir"])
    need(host.lease_ok(), "the Lease was lost before the start", Fault)
    try:
        started = host.remote.dw(["mutate-start-holder", h["stateDir"]], budget, obs, "mutate-start-holder", cap=130)
    except BaseException as e:  # noqa: BLE001 - a lost response: classify on the host, contain if unpaused
        outcome = _classify_start(host, budget, obs)
        host.ledger.append(op="pre-run-start", phase="receipt", outcome=outcome, error=type(e).__name__)
        host.ev.write(event="pre-run", outcome=outcome, observations=obs)
        raise Fault("the holder start's response was lost (" + outcome + ")") from e
    hrec = started.get("holder") or {}
    status, hello = hrec.get("status"), hrec.get("hello")
    if started.get("paused") is not True:
        outcome = "contained-unpaused" if started.get("contained") else "unpaused-NOT-contained"
        host.ledger.append(op="pre-run-start", phase="receipt", outcome=outcome, unit=started.get("unitAfterStop"))
        host.ev.write(event="pre-run", outcome=outcome, observations=obs)
        raise Fault("the holder did not start paused (" + outcome + ")")
    ok = bool(status) and bool(hello) and status["activeSessions"] == 0 and status["pendingCommands"] == 0 \
        and status["nativeStateReady"] is True and not status["sealed"] and hello["sessions"] == 0 \
        and hello["incarnation"] == status["incarnation"] and hello["journalId"] == h["newJournalId"]
    if not ok:
        host.ledger.append(op="pre-run-start", phase="receipt", outcome="started-paused-not-ready")
        raise Fault("the started holder is not drained and ready")
    host.state.holder = {"incarnation": hello["incarnation"], "journal": hello["journalId"],
                         "mainPid": hrec["units"]["relay-holder.service"]["mainPid"],
                         "revision": status["admissionRevision"]}
    host.ledger.append(op="pre-run-start", phase="receipt", outcome="ok", **host.state.holder)
    host.ev.write(event="pre-run", outcome="ok", observations=obs, holder=host.state.holder)
    return host.state.holder


def _classify_start(host, budget, obs) -> str:
    """Read-only first; stop the holder only when it is positively running unpaused (or its state is unreadable
    while the unit is active)."""
    from relay_phase_a_observer import Budget
    b = Budget(60_000, __import__("threading").Event())  # its own budget: the request budget may be spent
    try:
        rec = host.remote.dw(["holder", host.cfg.raw["holder"]["stateDir"]], b, obs, "classify-holder", cap=50)
    except BaseException:  # noqa: BLE001
        return "unknown-unreadable"
    active = rec["units"]["relay-holder.service"]["active"]
    paused = (rec.get("status") or {}).get("admissionPaused")
    if active != "active":
        return "not-running"
    if paused is True:
        return "running-paused"
    try:
        stop = host.remote.dw(["mutate-stop-holder"], b, obs, "mutate-stop-holder (containment)", cap=40)
        return "contained-unpaused" if stop.get("stopped") else "unpaused-NOT-contained"
    except BaseException:  # noqa: BLE001
        return "unpaused-containment-unknown"


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
    need(pre is not None and host.state.preservation is not None, "the pre-run step did not complete", Fault)
    cur = C.holder_record(host, budget, obs)
    need(cur["incarnation"] == pre["incarnation"] and cur["holderMainPid"] == pre["mainPid"]
         and cur["admissionPaused"] is True and cur["activeSessions"] == 0 and cur["pendingCommands"] == 0,
         "the holder changed since the pre-run step", Failed)
    rev = cur["admissionRevision"]
    op = str(uuid.uuid4())
    stage = None
    try:
        cap = host.mutation_cap(budget, 40)
        stage = "resume"
        host.ledger.append(op="resume", phase="intent", operationId=op, revision=rev)
        res = host.remote.dw(["mutate-resume", h["stateDir"], str(rev), op], budget, obs, "mutate-resume", cap=cap)
        jr = (res.get("journal") or {}).get("admission") or {}
        ok = res.get("resumed") is True and (res.get("status") or {}).get("admissionPaused") is False \
            and jr.get("paused") is False and jr.get("operationId") == op
        host.ledger.append(op="resume", phase="receipt", outcome="ok" if ok else "failed", operationId=op)
        stage = None
        need(ok, "the resume was not confirmed by status and journal", Failed)
        cap = host.mutation_cap(budget, 30)
        stage = "enable-start"
        host.ledger.append(op="enable-start", phase="intent")
        es = host.remote.dw(["mutate-enable-start"], budget, obs, "mutate-enable-start", cap=cap)
        ru = (es.get("units") or {}).get("relay-holder-control-plane.service") or {}
        hu = (es.get("units") or {}).get("relay-holder.service") or {}
        ok = es.get("enabled") is True and es.get("started") is True and ru.get("active") == "active" \
            and ru.get("sub") == "running" and ru.get("enabled") == "enabled" and hu.get("enabled") == "enabled" \
            and ru.get("argv") == h["reconciliationArgv"] and ru.get("restarts") == 0
        host.ledger.append(op="enable-start", phase="receipt", outcome="ok" if ok else "failed")
        stage = None
        need(ok, "reconciliation is not running cleanly from the pinned unit", Failed)
        resumed_ms = int(time.time() * 1000)
        while True:  # read back: holder unchanged and resumed; the hub's health row after the resume
            fin = C.holder_record(host, budget, obs)
            rows = host.remote.sql("holder-health.sql", {"holder_id": pre["incarnation"], "tenant": cfg.raw["tenant"]},
                                   budget, obs)
            if len(rows) == 1 and rows[0]["journal_id"] == pre["journal"] and rows[0]["admission_paused"] is False \
                    and rows[0]["native_state_ready"] is True and rows[0]["observed_ms"] > resumed_ms - 2000:
                break
            budget.sleep(3.0)
        need(fin["incarnation"] == pre["incarnation"] and fin["holderMainPid"] == pre["mainPid"]
             and fin["admissionPaused"] is False and fin["activeSessions"] == 0 and fin["pendingCommands"] == 0
             and fin["nativeStateReady"] and fin["nativeRecovery"] == 2, "the resumed holder is not ready", Failed)
    except BaseException as e:
        outcome = _classify_handoff(host, obs)
        host.ledger.append(op="handoff", phase="receipt", outcome="failed", interruptedStage=stage,
                           error=type(e).__name__, state=outcome)
        raise
    host.ledger.append(op="handoff", phase="receipt", outcome="ok", holder=pre["incarnation"])
    observed = C.runner_floor(host, gate_now)
    need(observed >= started_at, "C4 observation precedes its start", Failed)
    return {
        "holderId": pre["incarnation"], "journalId": pre["journal"],
        "oldHolderId": ctx["oldHolderId"], "oldJournalId": ctx["oldJournalId"],
        "preparationId": h["preparationId"], "stateDirectory": h["stateDir"],
        "handoffReceipt": {"path": h["receiptPath"], "sha256": h["receiptSha256"]},
        "observedUtc": C.utc(observed), "qualifiedRelease": q["release"], "holderSource": q["source"],
        "holderBinarySha256": fin["binarySha256"], "profilesSha256": fin["profilesSha256"],
        "nativeRecovery": fin["nativeRecovery"], "nativeStateReady": True, "activeSessions": 0, "pendingCommands": 0,
        "admissionPaused": False,
        # established by the pre-run's verified preservation facts (sealed old journal, receipt, artifacts)
        "preservedOldStateConfirmed": host.state.preservation is not None, "operatorVerified": True,
    }


def _classify_handoff(host, obs) -> dict:
    """The actual service and admission state after a failed or interrupted stage (read-only, its own budget)."""
    from relay_phase_a_observer import Budget
    b = Budget(45_000, __import__("threading").Event())
    try:
        rec = host.remote.dw(["holder", host.cfg.raw["holder"]["stateDir"]], b, obs, "classify-handoff", cap=40)
        st = rec.get("status") or {}
        adm = (rec.get("journal") or {}).get("admission") or {}
        return {"holder": rec["units"]["relay-holder.service"]["active"],
                "reconciliation": rec["units"]["relay-holder-control-plane.service"]["active"],
                "admissionPaused": st.get("admissionPaused"), "journalAdmissionOp": adm.get("operationId"),
                "journalAdmissionPaused": adm.get("paused")}
    except BaseException as e:  # noqa: BLE001
        return {"unreadable": type(e).__name__}


# ------------------------------------------------------------------------------------------ the read-only probe
class _ProbeHost:
    """Just enough of ObserverHost for the read-only collectors' helpers (no gate, no Lease, no authority)."""

    def __init__(self, cfg, remote):
        from relay_phase_a_observer import ObserverState
        self.cfg, self.remote, self.state = cfg, remote, ObserverState()
        self.recv_ms = int(time.time() * 1000)


PREDECESSOR_SESSION = "45724087-335a-4c55-ad0f-cb278afe608a"


def probe(repo: Path) -> dict:
    """Every read-only source the observer uses, against the live estate, with its latency. A check passes only when
    its result establishes the fact (not merely when the call returned); phase-dependent facts and the open gates are
    reported as not yet applicable, never as passed."""
    import threading
    from relay_phase_a_observer import Budget
    root = Path(repo) / "scripts"
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / "relay-phase-a").rglob("*") if p.is_file()}
    cfg = C.Config.load(files["relay-phase-a/observer-config.json"], files)
    cfg.raw["devWorker"]["knownHosts"] = str(root / cfg.raw["devWorker"]["knownHosts"])
    host = _ProbeHost(cfg, C.Remote(cfg))
    checks = {}

    def check(name, fn, ok=lambda d: True):
        obs: dict = {}
        t0 = time.monotonic()
        try:
            detail = fn(Budget(90_000, threading.Event()), obs)
            good = bool(ok(detail))
            checks[name] = {"ok": good, "ms": int((time.monotonic() - t0) * 1000), "detail": detail,
                            "calls": obs.get("calls"), **({} if good else {"error": "the result does not establish it"})}
        except Exception as e:  # noqa: BLE001 - every check is reported; none aborts the probe
            checks[name] = {"ok": False, "ms": int((time.monotonic() - t0) * 1000),
                            "error": type(e).__name__ + ": " + str(e)[:200], "calls": obs.get("calls")}

    def now_ms():
        return int(time.time() * 1000)

    check("hub-deployment", lambda b, o: C.deployment(host, b, o), lambda d: d["release"])
    check("router-pin", lambda b, o: C.pin_router(host, b, o), lambda d: d["uid"] and d["store"])
    for p in ("codex", "qwen"):
        key, prof = cfg.profile(p)["historicalKey"], cfg.profile(p)
        check(f"trajectory-envelope-{p}", lambda b, o, key=key, p=p: {
            k: v for k, v in C.trajectory(host, key, p, since_after=now_ms() - 1, budget=b, obs=o,
                                          label="probe").items() if k != "rows"},
            lambda d: d["version"] == 1 and d["scope"]["since"] <= d["requestedAt"] <= d["observedAt"])
        check(f"marker-baseline-{p}", lambda b, o, p=p, prof=prof: C.marker(host, p, prof["run"], "baseline", 0, b, o)
              ["files"], lambda d: d == {"denied": {"state": "absent"}, "effects": {"state": "absent"}})
        check(f"route-{p}", lambda b, o, p=p, prof=prof: host.remote.router(
            ["route", "canary-pi-" + p, prof["account"], prof["model"]], b, o, "route"), lambda d: d.get("ok") is True)
    check("clocks-router-devworker", lambda b, o: _probe_clocks(host, b, o), lambda d: d["worstPairMs"] < C.CLOCK_MS)
    t = cfg.raw["tenant"]
    sqls = {
        "renewed-credentials.sql": ({"codex_binding": cfg.profile("codex")["binding"],
                                     "qwen_binding": cfg.profile("qwen")["binding"], "tenant": t}, 2),
        "session-launch.sql": ({"session_id": PREDECESSOR_SESSION, "tenant": t}, 1),
        "session-state.sql": ({"session_id": PREDECESSOR_SESSION, "tenant": t}, 1),
        "session-resume.sql": ({"resume_from": PREDECESSOR_SESSION, "tenant": t}, 0),
        "session-commands.sql": ({"session_id": PREDECESSOR_SESSION, "generation": 1, "tenant": t}, 4),
        "permission-decisions.sql": ({"session_id": PREDECESSOR_SESSION, "tenant": t}, 1),
        "permission.sql": ({"permission_id": "70fb1b8e-f7cc-4cc3-b98f-66f83e46d1ab",
                            "session_id": PREDECESSOR_SESSION, "tenant": t}, 1),
        "holder-health.sql": ({"holder_id": cfg.raw["holder"]["oldHolderId"], "tenant": t}, 1),
        "delivery.sql": ({"launch_id": "4a49cdab-0e0e-4dd2-866d-d87b1238c2f2", "session_id": PREDECESSOR_SESSION,
                          "binding_id": cfg.profile("codex")["binding"], "host_id": cfg.raw["hostId"],
                          "credential_id": "87482892-42ab-40a5-aa03-01c41ec5f6df", "tenant": t}, 1),
        "delivery-reuse.sql": ({"binding_id": cfg.profile("codex")["binding"], "host_id": cfg.raw["hostId"],
                                "credential_id": "87482892-42ab-40a5-aa03-01c41ec5f6df", "tenant": t}, 1),
    }
    for name, (v, want) in sqls.items():  # shape and the predecessor's known cardinality
        check("sql-" + name, lambda b, o, name=name, v=v: {"rows": len(host.remote.sql(name, v, b, o))},
              lambda d, want=want: d["rows"] == want)
    check("prerun-read-only", lambda b, o: [prerun_checks(host, b, o), o["preservation"]][1], lambda d: d["sealId"])
    check("native-transcript-format", lambda b, o: host.remote.dw(
        ["transcript", cfg.raw["holder"]["oldStateDir"] + "/native-homes/" + PREDECESSOR_SESSION], b, o,
        "transcript"), lambda d: d.get("state") == "observed" and d.get("toolCalls") == 1 and not d.get("unsettled"))
    check("predecessor-staging", lambda b, o: _probe_staging(), lambda d: True)
    na = ["the historical C3 window (needs the recovered predecessor report)", "renewed installation (C6)",
          "live permissions (C8)", "recovery (C9)", "the runner clock (C5, needs the gate)",
          "reconcile field (C10, gate)", "retirement sources (C11, gate)", "predecessor projection (gate)"]
    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks, "notYetApplicable": na}


def _probe_clocks(host, budget, obs) -> dict:
    """The host, Router and dev-worker clocks (the runner's needs the gate, so it is checked only inside a run)."""
    def bracket(fn):
        w0, m0 = int(time.time() * 1000), time.monotonic()
        v = fn()
        return v, w0, w0 + int((time.monotonic() - m0) * 1000)
    pod, a0, a1 = bracket(lambda: host.remote.router(["identity"], budget, obs, "clock:router")["podNow"])
    dwn, b0, b1 = bracket(lambda: host.remote.dw(["clock"], budget, obs, "clock:dw")["now"])
    iv = {"router": (pod - a1, pod - a0), "devWorker": (dwn - b1, dwn - b0), "host": (0, 0)}
    names, worst = list(iv), 0
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            worst = max(worst, abs(iv[a][1] - iv[b][0]), abs(iv[b][1] - iv[a][0]))
    return {"intervalsMs": iv, "worstPairMs": worst}


def _probe_staging() -> dict:
    """The predecessor staging on the real Docker backend: 10001-owned, 0400, single-link, read-only mount."""
    from relay_operator_session import PROXY_IMAGE

    def run(args, **kw):
        return subprocess.run(["docker"] + args, capture_output=True, timeout=120, **kw)

    vol = "relay-observer-probe-" + uuid.uuid4().hex[:8]
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
