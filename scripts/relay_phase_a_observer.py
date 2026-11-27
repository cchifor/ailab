"""relay_phase_a_observer.py: the host side of the automated phase A observer
(plans/2026-11-27-relay-phase-a-observer-plan.md).

The Runner's credential-less client sends one request per checkpoint through the gate sidecar
(scripts/relay-observer-gate.py), which frames it on its STDOUT as {"seq", "gateNow", "request"}. This module reads
those frames, refuses anything outside the strict per-call schema or the phase A transition table, runs the matching
collector (relay_phase_a_collectors.py) under a host-clamped deadline with the operator's own host credentials, writes
the evidence line (fsynced) BEFORE replying, and answers on the gate's STDIN with {"seq", "v": 1, "ok", ...}.

Threads: a reader (gate stdout), one collector worker, and an independent supervisor that also watches the gate
process. Any fault (gate EOF or death, a protocol violation, a thread death, an evidence failure) is terminal: it is
recorded and reported to the orchestrator through `on_fault`, and every later request is answered `observer-down`.
Only fixed words ever travel back to the Runner; host error text stays in the evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

UUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_REPLY = 512 * 1024
CALLS = ("checkpoint", "deployment", "predecessor")
KINDS = ("renewal-deployment", "renewed-credentials", "stopped-replacement", "preserving-holder", "pre-launch",
         "post-open", "observation", "permission", "recover-history", "reconcile-choice", "retire-branch")
# Host maxima (ms) per request; the shorter of this and the Runner's own deadline applies.
MAX_MS = {"permission": 30_000, "predecessor": 15_000, "deployment": 120_000}
DEFAULT_MAX_MS = 120_000
# Per-kind caps at the inventory maxima (a full run: 3 phases, up to 12 prompts each).
CAPS = {"deployment": 1, "renewal-deployment": 1, "renewed-credentials": 1, "predecessor": 1,
        "stopped-replacement": 1, "preserving-holder": 1, "pre-launch": 3, "post-open": 3, "observation": 40,
        "permission": 40, "recover-history": 2, "reconcile-choice": 2, "retire-branch": 1}
RUN_ORDER = (("codex", False), ("qwen", False), ("codex", True))
PROVIDERS = ("codex", "qwen")
PHASES = ("baseline", "denied", "approved", "recovered")


class Refuse(Exception):
    """The request is outside the schema or the transition table: answer `refused`."""
    code = "refused"


class Failed(Exception):
    """A named check failed: answer `failed` (a phase A evidence outcome, not an observer fault)."""
    code = "failed"


class Unsupported(Exception):
    """The collector's evidence source is not established (a gate): answer `unsupported`."""
    code = "unsupported"


class Fault(Exception):
    """An observer internal fault: terminal, exit code 9."""
    code = "observer-down"


class Deadline(Exception):
    code = "deadline"


# ---------------------------------------------------------------------------------------------- small validators
def need(ok: bool, what: str, exc=Refuse) -> None:
    if not ok:
        raise exc(what)


def is_uuid(v) -> bool:
    return isinstance(v, str) and bool(UUID.match(v))


def is_int(v, lo=0) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= lo


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canonical(v) -> str:
    """The driver's canonical(): sorted keys, no whitespace (JSON.stringify of a key-sorted clone)."""
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def only_keys(d: dict, allowed: set, required: set = frozenset()) -> None:
    need(isinstance(d, dict), "object expected")
    extra = set(d) - set(allowed)
    need(not extra, "unexpected keys: " + ",".join(sorted(extra)))
    missing = set(required) - set(d)
    need(not missing, "missing keys: " + ",".join(sorted(missing)))


# ---------------------------------------------------------------------------------------------- budget
class Budget:
    """One monotonic remaining budget across waits, subprocesses and fsync."""

    def __init__(self, ms: int, cancelled: threading.Event):
        self.end = time.monotonic() + ms / 1000.0
        self.cancelled = cancelled

    def remaining(self) -> float:
        return self.end - time.monotonic()

    def check(self, margin: float = 0.0) -> float:
        if self.cancelled.is_set():
            raise Deadline("cancelled")
        r = self.remaining() - margin
        if r <= 0:
            raise Deadline("budget exhausted")
        return r

    def sleep(self, s: float) -> None:
        if self.cancelled.wait(min(s, max(0.0, self.remaining()))):
            raise Deadline("cancelled")
        self.check()


# ---------------------------------------------------------------------------------------------- evidence
class Evidence:
    """Append-only JSONL, one fsync per line. A failure is a Fault (the broker's evidence-failure path)."""

    def __init__(self, path: str):
        self.lock = threading.Lock()
        self.f = open(path, "ab", buffering=0)

    def write(self, **rec) -> None:
        rec.setdefault("t", round(time.time(), 3))
        line = (json.dumps(rec, separators=(",", ":"), sort_keys=True, default=str) + "\n").encode()
        with self.lock:
            try:
                self.f.write(line)
                os.fsync(self.f.fileno())
            except OSError as e:
                raise Fault(f"evidence write failed ({type(e).__name__})") from e

    def close(self) -> None:
        try:
            self.f.close()
        except OSError:
            pass


def safe_text(s) -> dict:
    """Untrusted text (permission descriptions, stderr) is logged only as its SHA-256 and length."""
    b = s.encode("utf-8", "replace") if isinstance(s, str) else (s or b"")
    return {"sha256": sha256_bytes(b), "len": len(b)}


# ---------------------------------------------------------------------------------------------- retained state
@dataclass
class RunState:
    provider: str
    run_id: str
    effort: bool
    phase: str = "c5"  # c5 -> open -> (recovering -> recovered -> open2 | open2)
    session_id: str | None = None
    generation: int | None = None
    launch_id: str | None = None
    native_id: str | None = None
    c5_trajectory: dict | None = None
    last_trajectory: dict | None = None
    baseline_marker: dict | None = None
    approved_marker_sha: str | None = None
    decisions: dict = field(default_factory=lambda: {"denied": 0, "effects": 0})
    reads: int = 0


@dataclass
class ObserverState:
    stage: str = "start"
    counts: dict = field(default_factory=dict)
    renewed: dict | None = None  # provider -> {key, credentialId, bindingId, generation, revision, expiresAt}
    expires_at: int | None = None
    window: tuple | None = None  # (start_ms, end_ms)
    holder: dict | None = None  # measured at the pre-run step: incarnation, journal, mainpid
    router_pod: dict | None = None  # uid, restarts, configDigest
    historical: dict | None = None  # {"trajectory", "marker"} from C3
    runs: list = field(default_factory=list)  # RunState, in RUN_ORDER
    pending: RunState | None = None  # C5's run, appended when its reply is sent
    sessions: dict = field(default_factory=dict)  # sessionId -> RunState

    def current(self) -> RunState | None:
        return self.runs[-1] if self.runs else None


def run_for(st: ObserverState, ctx: dict) -> RunState:
    """The current run, matched against the request's provider/runId; never parsed from text."""
    r = st.current()
    need(r is not None, "no run in progress")
    if "provider" in ctx:
        need(ctx["provider"] == r.provider, "provider is not the current run's")
    if "runId" in ctx:
        need(ctx["runId"] == r.run_id, "runId is not the current run's")
    return r


def admit(st: ObserverState, call: str, kind: str | None, ctx: dict, cfg) -> None:
    """The phase A transition table (plan section 3). Raises Refuse for anything outside it."""
    key = kind or call
    need(st.counts.get(key, 0) < CAPS[key], f"{key} cap reached")
    s = st.stage
    if call == "deployment":
        need(s == "start", "deployment observation out of order")
        return
    if call == "predecessor":
        need(s == "c2", "predecessor read out of order")
        return
    order = {"renewal-deployment": "prelude", "renewed-credentials": "c1", "stopped-replacement": "c2",
             "preserving-holder": "c3"}
    if kind in order:
        need(s == order[kind], f"{kind} out of order (stage {s})")
        return
    if kind == "pre-launch":
        need(s == "ready" or (s == "runs" and st.current() is not None and st.current().phase == "open2"),
             "pre-launch out of order")
        need(len(st.runs) < len(RUN_ORDER), "all runs already started")
        provider, effort = RUN_ORDER[len(st.runs)]
        run_id = cfg.effort_run if effort else cfg.runs[provider]
        need(ctx.get("provider") == provider and ctx.get("runId") == run_id and bool(ctx.get("effort")) == effort,
             "pre-launch is not the next run in the fixed order")
        return
    need(s == "runs", f"{kind} before any run")
    r = run_for(st, ctx)
    ph = r.phase
    allowed = {
        "post-open": ph == "c5",
        "observation": ph in ("open", "recovering", "open2"),
        "permission": ph in ("open", "open2"),
        "recover-history": ph == "open" and not r.effort,
        "reconcile-choice": ph == "recovered" and not r.effort,
        "retire-branch": ph == "open" and r.effort,
    }
    need(allowed.get(kind, False), f"{kind} not allowed in run phase {ph}")


def advance(st: ObserverState, call: str, kind: str | None, ctx: dict, value, cfg) -> None:
    """Record a returned value (it is 'returned', not 'accepted': the driver's final record decides acceptance)."""
    key = kind or call
    st.counts[key] = st.counts.get(key, 0) + 1
    nxt = {"deployment": "prelude", "renewal-deployment": "c1", "renewed-credentials": "c2",
           "stopped-replacement": "c3", "preserving-holder": "ready"}
    if call == "deployment":
        st.stage = "prelude"
    elif kind in nxt:
        st.stage = nxt[kind]
    elif kind == "pre-launch":
        need(st.pending is not None and st.pending.run_id == ctx["runId"], "pre-launch state missing", Fault)
        st.runs.append(st.pending)
        st.pending = None
        st.stage = "runs"
    elif kind == "post-open":
        st.current().phase = "open"
    elif kind == "recover-history":
        st.current().phase = "recovering"
    elif kind == "observation":
        r = st.current()
        if r.phase == "recovering":
            r.phase = "recovered"
    elif kind in ("reconcile-choice", "retire-branch"):
        st.current().phase = "open2"


# ---------------------------------------------------------------------------------------------- request schema
def check_request(frame: dict) -> tuple[int, int, dict]:
    only_keys(frame, {"seq", "gateNow", "request"}, {"seq", "gateNow", "request"})
    seq, gate_now, req = frame["seq"], frame["gateNow"], frame["request"]
    need(is_int(seq, 1) and is_int(gate_now, 1), "frame header")
    need(isinstance(req, dict) and req.get("v") == 1 and req.get("call") in CALLS, "request header")
    call = req["call"]
    if call == "checkpoint":
        only_keys(req, {"v", "call", "kind", "name", "instructionsSha256", "deadline", "context"},
                  {"v", "call", "kind", "name", "instructionsSha256", "deadline", "context"})
        need(req["kind"] in KINDS, "unknown kind")
        need(isinstance(req["name"], str) and len(req["name"]) <= 400, "name")
        need(isinstance(req["instructionsSha256"], str) and bool(HEX64.match(req["instructionsSha256"])), "hash")
        need(isinstance(req["context"], dict) and req["context"].get("kind") == req["kind"]
             and req["context"].get("version") == 1, "context kind/version")
    else:
        only_keys(req, {"v", "call", "deadline", "context"}, {"v", "call", "deadline", "context"})
        need(isinstance(req["context"], dict), "context")
    need(is_int(req["deadline"], 1), "deadline")
    return seq, gate_now, req


def clamp_ms(req: dict, gate_now: int) -> int:
    key = req.get("kind") if req["call"] == "checkpoint" else req["call"]
    host_max = MAX_MS.get(key, DEFAULT_MAX_MS)
    return max(0, min(host_max, req["deadline"] - gate_now))


# ---------------------------------------------------------------------------------------------- the host
class Host:
    """Drives one gate process. `collectors` maps (call, kind) -> fn(host, req, ctx, budget, gate_now) -> value."""

    def __init__(self, *, gate, collectors: dict, cfg, evidence: Evidence, on_fault: Callable[[str], None],
                 state: ObserverState | None = None, heartbeat_s: float = 5.0):
        self.gate = gate  # has .stdout (readable lines), .stdin (writable), .poll()
        self.collectors, self.cfg, self.ev, self.on_fault = collectors, cfg, evidence, on_fault
        self.state = state or ObserverState()
        self.cancelled = threading.Event()  # the in-flight request's cancel (terminal per the gate's protocol)
        self.down = threading.Event()  # terminal: answer observer-down
        self.stopping = threading.Event()  # the orchestrator's stop
        self.work: queue.Queue = queue.Queue(maxsize=1)
        self.expected_seq = 1
        self.inflight = None
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.heartbeat_s = heartbeat_s
        self.beats = {"reader": time.monotonic(), "worker": time.monotonic()}
        self.fault_reason = None
        self.ready = threading.Event()
        self.threads = []

    # -------------------------------------------------------------- lifecycle
    def start(self, ready_timeout: float = 10.0) -> None:
        for name, fn in (("reader", self._reader), ("worker", self._worker), ("supervisor", self._supervisor)):
            t = threading.Thread(target=self._guard, args=(name, fn), daemon=True, name=f"observer-{name}")
            t.start()
            self.threads.append(t)
        if not self.ready.wait(ready_timeout):
            self.fault("gate-not-ready")
            raise Fault("the gate did not report ready")

    def stop(self) -> None:
        self.stopping.set()
        self.cancelled.set()

    def alive(self) -> bool:
        """For the orchestrator's own loop: the supervisor itself can die, so liveness is also checked from outside."""
        return self.stopping.is_set() or (not self.down.is_set() and all(t.is_alive() for t in self.threads))

    def drain(self, timeout: float = 10.0) -> bool:
        """Bounded: True when the worker is idle."""
        self.stop()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if self.inflight is None:
                    return True
            time.sleep(0.05)
        return False

    def fault(self, reason: str) -> None:
        with self.lock:
            first = self.fault_reason is None
            if first:
                self.fault_reason = reason
        self.down.set()
        self.cancelled.set()
        if first:
            try:
                self.ev.write(event="observer-fault", reason=reason)
            except Fault:
                pass
            try:
                self.on_fault(reason)
            except Exception:  # noqa: BLE001 - the orchestrator's callback must never kill the supervisor
                pass

    def _guard(self, name: str, fn) -> None:
        try:
            fn()
        except BaseException as e:  # noqa: BLE001
            if not self.stopping.is_set():
                self.fault(f"{name}-died:{type(e).__name__}")

    # -------------------------------------------------------------- gate I/O
    def _reply(self, seq: int, obj: dict) -> None:
        line = (json.dumps({"seq": seq, **obj}, separators=(",", ":")) + "\n").encode()
        if len(line) > MAX_REPLY:
            line = (json.dumps({"seq": seq, "v": 1, "ok": False, "code": "failed"}) + "\n").encode()
        with self.write_lock:
            try:
                self.gate.stdin.write(line)
                self.gate.stdin.flush()
            except (OSError, ValueError) as e:
                raise Fault(f"gate stdin ({type(e).__name__})") from e

    def _reader(self) -> None:
        while not self.stopping.is_set():
            line = self.gate.stdout.readline(MAX_REPLY + 2)
            self.beats["reader"] = time.monotonic()
            if not line:
                if not self.stopping.is_set():
                    self.fault("gate-eof")
                return
            try:
                frame = json.loads(line)
            except ValueError:
                self.fault("gate-frame-malformed")
                return
            if frame == {"ready": True}:
                self.ready.set()
                continue
            if isinstance(frame, dict) and set(frame) == {"seq", "cancel"} and frame["cancel"] is True:
                with self.lock:
                    match = self.inflight is not None and self.inflight == frame["seq"]
                self.cancelled.set()  # terminal by protocol: the gate refuses every later connection
                self.ev.write(event="cancel", seq=frame["seq"], inflight=match)
                continue
            self._accept(frame)

    def _accept(self, frame) -> None:
        seq = frame.get("seq") if isinstance(frame, dict) else None
        if not is_int(seq, 1):
            self.fault("gate-frame-no-seq")
            return
        if seq != self.expected_seq:
            self.fault("gate-seq-gap")
            return
        self.expected_seq += 1
        if self.down.is_set() or self.cancelled.is_set():
            self._reply(seq, {"v": 1, "ok": False, "code": "observer-down"})
            return
        try:
            self.work.put_nowait((seq, frame, int(time.time() * 1000)))  # receipt stamped HERE, in the reader
        except queue.Full:
            self.fault("second-outstanding-request")

    def _worker(self) -> None:
        while not self.stopping.is_set():
            try:
                seq, frame, recv_ms = self.work.get(timeout=0.5)
            except queue.Empty:
                self.beats["worker"] = time.monotonic()
                continue
            with self.lock:
                self.inflight = seq
            try:
                self.handle(seq, frame, recv_ms)
            finally:
                with self.lock:
                    self.inflight = None
                self.beats["worker"] = time.monotonic()

    def _supervisor(self) -> None:
        while not self.stopping.is_set():
            time.sleep(min(1.0, self.heartbeat_s))
            if self.gate.poll() is not None and not self.stopping.is_set():
                self.fault("gate-exited")
                return
            dead = [t.name for t in self.threads[:2] if not t.is_alive()]
            if dead and not self.stopping.is_set():
                self.fault("thread-dead:" + ",".join(dead))
                return

    # -------------------------------------------------------------- one request
    def handle(self, seq: int, frame: dict, recv_ms: int | None = None) -> None:
        started = time.time()
        rec = {"event": "request", "seq": seq}
        try:
            seq_, gate_now, req = check_request(frame)
            call, kind = req["call"], req.get("kind")
            ctx = req["context"]
            rec.update(call=call, kind=kind, contextSha256=sha256_bytes(canonical(ctx).encode()),
                       instructionsSha256=req.get("instructionsSha256"), gateNow=gate_now,
                       hostNow=int(started * 1000))
            admit(self.state, call, kind, ctx, self.cfg)
            ms = clamp_ms(req, gate_now)
            need(ms > 0, "deadline already elapsed", Deadline)
            fn = self.collectors.get((call, kind))
            need(fn is not None, "no collector", Unsupported)
            budget = Budget(ms - 1500, self.cancelled)  # reply margin
            obs: dict = {}
            value = fn(self, req, ctx, budget, gate_now, obs)
            budget.check()
            advance(self.state, call, kind, ctx, value, self.cfg)
            body = canonical(value).encode()
            rec.update(outcome="ok", observations=obs, valueSha256=sha256_bytes(body), value=value)
            self.ev.write(**rec, durationMs=int((time.time() - started) * 1000))  # BEFORE the reply
            self._reply(seq, {"v": 1, "ok": True, "value": value})
        except Fault as e:
            self.fault(str(e))
            raise
        except (Refuse, Failed, Unsupported, Deadline) as e:
            rec.update(outcome=e.code, reason=str(e)[:300])
            self.ev.write(**rec, durationMs=int((time.time() - started) * 1000))
            self._reply(seq, {"v": 1, "ok": False, "code": e.code})
        except Exception as e:  # noqa: BLE001 - a collector bug is an observer fault, never a pass
            rec.update(outcome="fault", reason=type(e).__name__)
            try:
                self.ev.write(**rec)
            finally:
                self.fault(f"collector-error:{type(e).__name__}")
            try:
                self._reply(seq, {"v": 1, "ok": False, "code": "observer-down"})
            except Fault:
                pass
