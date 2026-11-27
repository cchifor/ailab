"""The `relay-operator.py session` orchestrator (plans/2026-11-27-relay-operator-session-plan.md, section 2).

One bounded run: verify the Runner bundle against a manifest from a REVIEWED ailab commit, take the fenced Kubernetes
Lease, clear orphan authority, grant temporary admin, start the session proxy (scripts/relay-session-proxy.py) and the
Runner (--network none, sharing only the proxy's Unix socket), enforce the runner and cleanup deadlines, then run the
independent, bounded cleanup stages. No caller-supplied Docker arguments; no scope parameters.

Exit codes (precedence order): 3 revoke not confirmed, 4 proxy logout not confirmed, 5 Keycloak logout failed,
6 evidence capture failed, 7 Lease lost, contended or ownership unconfirmed (revocation left to the holder or the
reaper), 9 phase A observer internal fault (gate or thread death, protocol violation, pre-run holder step;
plans/2026-11-27-relay-phase-a-observer-plan.md), 8 Runner failed or exceeded its deadline, 2 refused before any
authority, 0 success.

Destructive cleanup (revoke, the account-wide Keycloak logout, the Lease release) runs ONLY after this run positively
re-confirms Lease ownership (a resourceVersion-conditioned extension of its own Lease, or a conditional takeover of an
absent/expired one). Unknown ownership never revokes: the reaper (a separate, independent IaC backstop) does.

Collaborators are injected (`Ops`) so tests drive every failure path with fakes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

LEASE = "relay-operator-jit"
NAMESPACE = "relay"
PROXY_IMAGE = "python:3.13-bookworm@sha256:073ffebb96ae4d0ed73ccad59f08c47bd84af8a79f5258130f8458ac50ad9ff4"
UID = "10001:10001"
SOCKET_DIR = "/run/relay-session"
MAX_RUNNER_MINUTES = 45
CLEANUP_MINUTES = 10
LEASE_GRACE_S = 300
CLEANUP_LEASE_S = 600  # the extension taken before destructive cleanup
READY_TIMEOUT_S = 120
JOIN_TIMEOUT_S = 10
DIGEST_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REL_PATH = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
REVOKED = "0 memberships, 0 sessions"
PRECEDENCE = [3, 4, 5, 6, 7, 9, 8, 2]


class Refused(Exception):
    """Refused before any authority exists (rc 2)."""


class LeaseLost(Exception):
    """The Lease is held by someone else, or this run no longer holds it (rc 7)."""


class Interrupted(BaseException):
    """SIGTERM/SIGINT, converted so the run stops; cleanup itself defers further signals."""


@dataclass
class Ops:
    """Injected collaborators. Every call is bounded by its own timeout."""
    creds: Callable[[], dict]  # {subject, username, password, keycloak_user_id}
    grant: Callable[[str], None]
    revoke: Callable[[str], str]  # returns counts after the delete
    end_sso: Callable[[str], None]
    kubectl: Callable[..., bytes]  # kubectl(args, stdin=None, timeout=60, check=True)
    docker: Callable[..., bytes]  # docker(args, stdin=None, timeout=60, check=True)
    git_show: Callable[[str, str], bytes]  # git_show(commit, path)
    git_is_reviewed: Callable[[str], bool]  # commit is an ancestor of gitea/main
    popen: Callable[..., subprocess.Popen] = subprocess.Popen
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    tool_dir: str = str(Path(__file__).resolve().parent)


# ---------------------------------------------------------------- manifest + bundle (before any authority)
def load_manifest(ops: Ops, ref: str) -> tuple[str, dict]:
    commit, sep, path = ref.partition(":")
    if not sep or not re.fullmatch(r"[0-9a-f]{40}", commit) or not REL_PATH.match(path):
        raise Refused("--manifest-ref must be FULL_COMMIT_SHA:repo/relative/path.json")
    if not ops.git_is_reviewed(commit):
        raise Refused("the manifest commit is not on reviewed main (gitea/main)")
    try:
        m = json.loads(ops.git_show(commit, path))
    except BaseException as e:  # noqa: BLE001 - SystemExit from the real wrapper, ValueError, timeouts
        raise Refused(f"manifest unreadable at {ref}: {type(e).__name__}")
    if not isinstance(m, dict) or m.get("version") != 1 or \
            set(m) - {"observer"} != {"version", "image", "entrypoint", "files"}:
        raise Refused("manifest must be {version: 1, image, entrypoint, files[, observer]}")
    if "observer" in m:
        o = m["observer"]
        if not isinstance(o, dict) or set(o) != {"files", "config", "predecessorSha256"} or \
                not isinstance(o["files"], dict) or not o["files"] or len(o["files"]) > 40 or \
                not isinstance(o["config"], str) or o["config"] not in o["files"] or \
                not isinstance(o["predecessorSha256"], str) or not SHA256.match(o["predecessorSha256"]):
            raise Refused("manifest observer must be {files, config, predecessorSha256}")
        for rel, digest in o["files"].items():
            if not isinstance(rel, str) or not REL_PATH.match(rel) or ".." in rel.split("/") or \
                    not isinstance(digest, str) or not SHA256.match(digest):
                raise Refused(f"bad observer entry {rel!r}")
    if not isinstance(m["image"], str) or not DIGEST_IMAGE.match(m["image"]):
        raise Refused("manifest image must be a digest reference (name@sha256:...)")
    ep = m["entrypoint"]
    if not isinstance(ep, list) or not ep or not all(isinstance(a, str) and a and "\0" not in a for a in ep):
        raise Refused("manifest entrypoint must be a non-empty list of strings")
    files = m["files"]
    if not isinstance(files, dict) or not files or len(files) > 200:
        raise Refused("manifest files must be a non-empty map of at most 200 entries")
    for rel, digest in files.items():
        if not isinstance(rel, str) or not REL_PATH.match(rel) or ".." in rel.split("/") or \
                not isinstance(digest, str) or not SHA256.match(digest):
            raise Refused(f"bad manifest entry {rel!r}")
    return commit, m


def copy_bundle(src: str, manifest: dict, dest: str) -> dict:
    """Copy the bundle into `dest`, refusing anything outside the manifest, then hash the COPIED bytes."""
    files = manifest["files"]
    root = Path(src)
    if not root.is_dir() or root.is_symlink():
        raise Refused("--bundle must be a real directory")
    seen = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for d in dirnames:
            if (Path(dirpath) / d).is_symlink():
                raise Refused(f"symlinked directory in the bundle: {d}")
        for name in filenames:
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode):
                raise Refused(f"not a regular file: {rel}")
            if rel not in files:
                raise Refused(f"file not in the manifest: {rel}")
            out = Path(dest) / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "rb") as fi, open(out, "wb") as fo:
                shutil.copyfileobj(fi, fo)
            seen.add(rel)
    missing = set(files) - seen
    if missing:
        raise Refused(f"manifest files missing from the bundle: {sorted(missing)[:5]}")
    for rel, want in files.items():  # verify the bytes that will be mounted, not the source
        h = hashlib.sha256((Path(dest) / rel).read_bytes()).hexdigest()
        if h != want:
            raise Refused(f"sha256 mismatch for {rel}")
    return {rel: files[rel] for rel in sorted(files)}


OBSERVER_MODULES = ("relay_phase_a_observer.py", "relay_phase_a_collectors.py", "relay_phase_a_host.py",
                    "relay-observer-gate.py")


def verify_observer(manifest: dict, repo_root: str, predecessor_report: str | None) -> dict:
    """The observer's host code, remote scripts, SQL and config must be byte-identical to the manifest's reviewed
    hashes BEFORE anything is imported or executed; remote scripts and SQL run from these verified bytes only."""
    o = manifest["observer"]
    files = {}
    for rel, want in o["files"].items():
        path = Path(repo_root) / rel
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"observer file is not regular: {rel}")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != want:
            raise Refused(f"observer sha256 mismatch for {rel}")
        files[rel] = data
    for mod in OBSERVER_MODULES:
        if "scripts/" + mod not in files:
            raise Refused(f"the observer manifest does not pin scripts/{mod}")
    if not predecessor_report:
        raise Refused("--predecessor-report is required for an observer manifest")
    st = os.lstat(predecessor_report)
    if not stat.S_ISREG(st.st_mode) or st.st_size <= 0 or st.st_size > 1024 * 1024:
        raise Refused("--predecessor-report must be a regular file of at most 1 MiB")
    pred = Path(predecessor_report).read_bytes()
    if hashlib.sha256(pred).hexdigest() != o["predecessorSha256"]:
        raise Refused("the predecessor report does not match the manifest's predecessorSha256")
    return {"files": files, "config": o["config"], "predecessor": pred, "predecessorSha256": o["predecessorSha256"]}


def _import_observer(tool_dir: str):
    """Import the observer modules FROM THE VERIFIED COPY, never from the working tree (any already-imported copy
    is dropped first)."""
    import importlib
    import sys
    for m in ("relay_phase_a_observer", "relay_phase_a_collectors", "relay_phase_a_host"):
        sys.modules.pop(m, None)
    sys.path.insert(0, tool_dir)
    try:
        O = importlib.import_module("relay_phase_a_observer")
        C = importlib.import_module("relay_phase_a_collectors")
        H = importlib.import_module("relay_phase_a_host")
    finally:
        sys.path.remove(tool_dir)
    for mod in (O, C, H):
        if not str(Path(mod.__file__).resolve()).startswith(str(Path(tool_dir).resolve())):
            raise Refused("an observer module was not loaded from the verified copy")
    return O, C, H


def lease_held(ops: "Ops", holder: str) -> bool:
    """Read-only: this run still holds an unexpired Lease (checked before every observer mutation)."""
    try:
        cur = _lease_get(ops)
    except BaseException:  # noqa: BLE001 - unknown is not held
        return False
    return bool(cur) and _lease_holder(cur) == holder and ops.now() < _lease_exp(cur)


# ---------------------------------------------------------------- the fenced Lease
def _lease_get(ops: Ops) -> dict | None:
    out = ops.kubectl(["-n", NAMESPACE, "get", "lease", LEASE, "-o", "json", "--ignore-not-found"], timeout=30)
    return json.loads(out) if out.strip() else None


def _lease_exp(lease: dict) -> float:
    try:
        return float(((lease.get("metadata") or {}).get("annotations") or {}).get("chifor.me/expires-at", "0"))
    except ValueError:
        return 0.0


def _lease_holder(lease: dict) -> str:
    return (lease.get("spec") or {}).get("holderIdentity") or ""


def _lease_expired(lease: dict, now: float) -> bool:
    return not _lease_holder(lease) or now >= _lease_exp(lease)


def _lease_doc(holder: str, expires_at: float, now: float, rv: str | None = None) -> bytes:
    meta = {"name": LEASE, "namespace": NAMESPACE,
            "annotations": {"chifor.me/expires-at": str(int(expires_at))}}
    if rv:
        meta["resourceVersion"] = rv
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    return json.dumps({"apiVersion": "coordination.k8s.io/v1", "kind": "Lease", "metadata": meta,
                       "spec": {"holderIdentity": holder, "acquireTime": ts, "renewTime": ts,
                                "leaseDurationSeconds": max(1, int(expires_at - now))}}).encode()


def lease_acquire(ops: Ops, holder: str, expires_at: float) -> None:
    """Create, or take over an expired Lease (resourceVersion-conditioned); never a live one."""
    cur = _lease_get(ops)
    if cur is None:
        ops.kubectl(["create", "-f", "-"], stdin=_lease_doc(holder, expires_at, ops.now()), timeout=30)
    elif _lease_expired(cur, ops.now()):
        ops.kubectl(["replace", "-f", "-"],
                    stdin=_lease_doc(holder, expires_at, ops.now(), cur["metadata"]["resourceVersion"]), timeout=30)
    else:
        raise LeaseLost(f"the Lease is held by {_lease_holder(cur)!r}")
    got = _lease_get(ops)
    if not got or _lease_holder(got) != holder:
        raise LeaseLost("lost the Lease race")


def lease_secure(ops: Ops, holder: str, extend_s: float) -> bool | None:
    """Before a destructive step: positively (re)confirm ownership for `extend_s` more seconds.
    True: this run holds a fresh Lease (its own extended, or an absent/expired one conditionally taken).
    False: another holder positively holds a live Lease. None: ownership could not be established (any error)."""
    try:
        cur = _lease_get(ops)
        now = ops.now()
        if cur is not None and _lease_holder(cur) not in ("", holder) and not _lease_expired(cur, now):
            return False
        want = now + extend_s
        if cur is not None and _lease_holder(cur) == holder:
            want = max(want, _lease_exp(cur))  # never shorten this holder's own, longer Lease
        if cur is None:
            ops.kubectl(["create", "-f", "-"], stdin=_lease_doc(holder, want, now), timeout=30)
        else:
            ops.kubectl(["replace", "-f", "-"],
                        stdin=_lease_doc(holder, want, now, cur["metadata"]["resourceVersion"]), timeout=30)
        got = _lease_get(ops)
        return bool(got) and _lease_holder(got) == holder and _lease_exp(got) >= ops.now() + extend_s / 2
    except BaseException:  # noqa: BLE001 - conflict, timeout, apiserver down: ownership unknown
        return None


def lease_release(ops: Ops, holder: str) -> bool:
    cur = _lease_get(ops)
    if not cur or _lease_holder(cur) != holder:
        return False
    ops.kubectl(["replace", "-f", "-"], stdin=_lease_doc("", ops.now(), ops.now(), cur["metadata"]["resourceVersion"]),
                timeout=30)
    return True


# ---------------------------------------------------------------- the run
@dataclass
class Run:
    ops: Ops
    manifest_ref: str
    bundle: str
    runner_minutes: int
    evidence_dir: str
    predecessor_report: str | None = None
    run_id: str = field(default_factory=lambda: "session-" + uuid.uuid4().hex[:12])
    rc: int = 0
    notes: list = field(default_factory=list)

    def __post_init__(self):
        self.evidence_failed = threading.Event()
        self.observer_fault = threading.Event()
        self.observer_plan = None
        self.deferred_signals = []
        self.in_cleanup = False

    def note(self, **kv):
        """Record (always) and print (best effort). Reporting NEVER raises: a closed stdout (a piped consumer that
        exited) must not abort cleanup before the revoke; result.json keeps the full record either way."""
        kv["t"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.notes.append(kv)
        try:
            print("relay-operator session: " + json.dumps(kv), flush=True)
        except (OSError, ValueError):
            pass

    def fail(self, code: int, **kv):
        if self.rc == 0 or (code in PRECEDENCE and (self.rc not in PRECEDENCE or
                                                    PRECEDENCE.index(code) < PRECEDENCE.index(self.rc))):
            self.rc = code
        self.note(failure=code, **kv)

    # ---------------- streaming evidence: never stops draining the producer
    def _stream(self, pipe, sink, ready: threading.Event | None = None, ready_line: list | None = None):
        broken = False
        while True:
            try:
                line = pipe.readline()
            except (OSError, ValueError):  # the pipe itself failed: the capture is incomplete
                self.evidence_failed.set()
                return
            if not line:
                return
            if ready is not None and not ready.is_set() and line.startswith(b"READY "):
                ready_line.append(line)
                ready.set()
            if not broken:
                try:
                    view = memoryview(line)
                    while view:  # an unbuffered write may be short
                        n = sink.write(view)
                        if not n:
                            raise OSError("short write")
                        view = view[n:]
                except (OSError, ValueError):
                    broken = True
                    self.evidence_failed.set()

    def main(self) -> int:
        ops = self.ops
        if not 1 <= self.runner_minutes <= MAX_RUNNER_MINUTES:
            self.fail(2, reason=f"--runner-minutes must be 1..{MAX_RUNNER_MINUTES}")
            return self.rc
        ev = Path(self.evidence_dir)
        try:
            ev.mkdir(mode=0o700, parents=False, exist_ok=False)
        except OSError as e:
            self.fail(2, reason=f"--evidence-dir must be NEW ({type(e).__name__})")
            return self.rc
        work = tempfile.mkdtemp(prefix="relay-session-")
        bundle_copy = os.path.join(work, "bundle")
        try:
            commit, manifest = load_manifest(ops, self.manifest_ref)
            files = copy_bundle(self.bundle, manifest, bundle_copy)
            if "observer" in manifest:
                self.observer_plan = verify_observer(manifest, str(Path(ops.tool_dir).parent),
                                                     self.predecessor_report)
        except Refused as e:
            self.fail(2, reason=str(e))
            shutil.rmtree(work, ignore_errors=True)
            return self.rc
        # evidence sinks are opened BEFORE any authority exists
        try:
            sinks = {name: open(ev / name, "ab", buffering=0) for name in
                     ("proxy.out", "proxy.jsonl", "runner.jsonl", "runner.err")}
        except OSError as e:
            self.fail(2, reason=f"evidence files not writable ({type(e).__name__})")
            shutil.rmtree(work, ignore_errors=True)
            return self.rc
        start = ops.now()
        runner_deadline = start + 60 * self.runner_minutes
        cleanup_deadline = runner_deadline + 60 * CLEANUP_MINUTES
        (ev / "run.json").write_text(json.dumps({
            "runId": self.run_id, "manifestCommit": commit, "manifestRef": self.manifest_ref,
            "image": manifest["image"], "entrypoint": manifest["entrypoint"], "files": files,
            "runnerDeadline": runner_deadline, "cleanupDeadline": cleanup_deadline}, indent=1))

        prev = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}

        def on_signal(signum, _frame):
            if self.in_cleanup:  # cleanup defers signals: every stage still runs
                self.deferred_signals.append(signum)
                return
            raise Interrupted()

        for s in prev:
            signal.signal(s, on_signal)
        names = {"proxy": f"relay-proxy-{self.run_id}", "runner": f"relay-runner-{self.run_id}",
                 "volume": f"relay-session-{self.run_id}", "state": f"relay-state-{self.run_id}",
                 "gate": f"relay-observer-gate-{self.run_id}", "pred": f"relay-predecessor-{self.run_id}"}
        ctx = {"proxy": None, "runner": None, "threads": [], "granted": False, "subject": None, "uid": None,
               "leased": False, "gate": None, "host": None, "pred": False, "ledger": None, "observer_dir": None,
               "observer_tool_dir": None}
        try:
            self._run(ev, names, ctx, manifest, bundle_copy, sinks, runner_deadline, cleanup_deadline)
        except Interrupted:
            self.fail(8, stage="interrupted")
        except LeaseLost as e:
            self.fail(7, stage="lease", reason=str(e))
        except BaseException as e:  # noqa: BLE001 - SystemExit from the real wrappers included
            self.fail(8, stage="error", error=type(e).__name__, detail=str(e)[:200])
        finally:
            self.in_cleanup = True
            self._cleanup(names, ctx, sinks, cleanup_deadline)
            for s, h in prev.items():
                signal.signal(s, h)
            shutil.rmtree(work, ignore_errors=True)
            if self.deferred_signals:
                self.note(stage="cleanup", deferred_signals=len(self.deferred_signals))
            try:
                (ev / "result.json").write_text(json.dumps({"runId": self.run_id, "rc": self.rc,
                                                           "notes": self.notes}, indent=1))
            except OSError:
                self.fail(6, stage="result")
        return self.rc

    def _run(self, ev, names, ctx, manifest, bundle_copy, sinks, runner_deadline, cleanup_deadline):
        ops = self.ops
        lease_acquire(ops, self.run_id, cleanup_deadline + LEASE_GRACE_S)
        ctx["leased"] = True
        self.note(stage="lease", holder=self.run_id)
        ops.docker(["volume", "create", "--driver", "local", "--opt", "type=tmpfs", "--opt", "device=tmpfs",
                    "--opt", "o=size=1m,uid=10001,gid=10001,mode=0700", names["volume"]], timeout=30)
        ops.docker(["volume", "create", names["state"]], timeout=30)  # the library's durable store, KEPT
        ops.docker(["run", "--rm", "--network", "none", "--mount", f"type=volume,src={names['state']},dst=/state",
                    PROXY_IMAGE, "sh", "-c", "chown 10001:10001 /state && chmod 0700 /state && mkdir -p /state/run "
                    "&& chown 10001:10001 /state/run && chmod 0700 /state/run"], timeout=120)
        self.note(stage="state-volume", volume=names["state"])
        if self.observer_plan is not None:
            if not self._observer_prepare(ev, names, ctx, runner_deadline, cleanup_deadline):
                return
        c = ops.creds()
        ctx["subject"], ctx["uid"] = c["subject"], c["keycloak_user_id"]
        username, password = c["username"], c["password"]
        c = None
        orphan = ops.revoke(ctx["subject"])  # clear any orphan authority before granting anew
        if orphan != REVOKED:
            self.fail(3, stage="orphan-clear", readback=orphan)
            return
        ctx["granted"] = True  # inside the cleanup scope from here: a grant may commit and still report an error
        ops.grant(ctx["subject"])
        self.note(stage="granted")
        proxy = ctx["proxy"] = ops.popen(
            ["docker", "run", "-i", "--name", names["proxy"], "--read-only", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges", "--user", UID, "--pids-limit", "128", "--memory", "256m",
             "--mount", f"type=bind,src={ops.tool_dir},dst=/tool,readonly",
             "--mount", f"type=volume,src={names['volume']},dst={SOCKET_DIR}",
             PROXY_IMAGE, "python3", "-I", "/tool/relay-session-proxy.py"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ready, ready_line = threading.Event(), []
        for args in ((proxy.stdout, sinks["proxy.out"], ready, ready_line), (proxy.stderr, sinks["proxy.jsonl"])):
            t = threading.Thread(target=self._stream, args=args, daemon=True)
            t.start()
            ctx["threads"].append(t)
        cfg = {"username": username, "runner_deadline": runner_deadline, "cleanup_deadline": cleanup_deadline}
        proxy.stdin.write((password + "\n" + json.dumps(cfg) + "\n").encode())
        proxy.stdin.close()
        password = None
        if not self._until(lambda: ready.is_set() or proxy.poll() is not None,
                           min(ops.now() + READY_TIMEOUT_S, cleanup_deadline)) or not ready.is_set():
            self.fail(8, stage="proxy", reason="no READY")
            return
        self.note(stage="proxy-ready", ready=ready_line[0].decode(errors="replace").strip()[:300])
        if self.evidence_failed.is_set():  # never start authorized work without its audit trail
            self.fail(6, stage="evidence", reason="the proxy's evidence stream failed before the Runner started")
            return
        extra = []
        if self.observer_plan is not None:
            if not self._observer_start(names, ctx, sinks):
                return
            extra = ["--mount", f"type=volume,src={names['pred']},dst=/predecessor,readonly",
                     "-e", f"RELAY_OBSERVER_SOCKET={SOCKET_DIR}/observer.sock"]
        runner = ctx["runner"] = ops.popen(
            ["docker", "run", "--name", names["runner"], "--network", "none", "--read-only",
             "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", UID, "--pids-limit", "256",
             "--memory", "1g", "--tmpfs", "/tmp:rw,size=64m,uid=10001,gid=10001,mode=0700",
             "--mount", f"type=bind,src={bundle_copy},dst=/bundle,readonly",
             "--mount", f"type=volume,src={names['state']},dst=/state",
             "--mount", f"type=volume,src={names['volume']},dst={SOCKET_DIR}",
             "-e", f"RELAY_SESSION_SOCKET={SOCKET_DIR}/proxy.sock",
             "-e", f"RELAY_SESSION_CONTROL_SOCKET={SOCKET_DIR}/control.sock", "-e", f"RUNNER_DEADLINE={runner_deadline}",
             "-e", f"CLEANUP_DEADLINE={cleanup_deadline}", "-e", "AUTOMATION_DIR=/state/run", *extra,
             "--entrypoint", manifest["entrypoint"][0], manifest["image"], *manifest["entrypoint"][1:]],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for args in ((runner.stdout, sinks["runner.jsonl"]), (runner.stderr, sinks["runner.err"])):
            t = threading.Thread(target=self._stream, args=args, daemon=True)
            t.start()
            ctx["threads"].append(t)
        self.note(stage="runner-started")
        # the run: stop on the Runner's exit, the runner deadline, or an evidence failure
        def observer_down():
            if ctx["host"] is not None and not ctx["host"].alive() and not self.observer_fault.is_set():
                self._on_observer_fault("observer threads not alive (seen by the orchestrator)")
            return self.observer_fault.is_set()

        self._until(lambda: runner.poll() is not None or self.evidence_failed.is_set() or observer_down(),
                    runner_deadline)
        if self.observer_fault.is_set():
            self.fail(9, stage="observer", reason=(ctx["host"].fault_reason if ctx["host"] else None))
        if runner.poll() is None:
            why = ("evidence" if self.evidence_failed.is_set() else
                   "observer" if self.observer_fault.is_set() else "deadline")
            self.note(stage="runner-stop", reason=why, action="SIGTERM")
            ops.docker(["kill", "-s", "TERM", names["runner"]], timeout=20, check=False)
            if not self._until(lambda: runner.poll() is not None, cleanup_deadline - 180):
                self.fail(8, stage="runner", reason="did not finish cleanup before the cutoff; killed")
                ops.docker(["kill", names["runner"]], timeout=20, check=False)
                self._until(lambda: runner.poll() is not None, ops.now() + 20)
            elif why == "deadline":
                self.fail(8, stage="runner", reason="exceeded the runner deadline (cleaned up)")
        if self.evidence_failed.is_set():
            self.fail(6, stage="evidence", reason="a stream could not be written; the run was stopped")
        rrc = runner.poll()
        if rrc not in (0, None):
            self.fail(8, stage="runner", exit=rrc)
        self.note(stage="runner-exited", exit=rrc)

    # ---------------- the phase A observer (plans/2026-11-27-relay-phase-a-observer-plan.md, section 9)
    def _observer_prepare(self, ev, names, ctx, runner_deadline, cleanup_deadline) -> bool:
        """Stage the VERIFIED observer copy and the read-only predecessor volume, build the host from that copy, and
        run the pre-run holder step under the Lease, before any Relay authority. Every failure here is an observer
        fault (9), except an interrupt or a Lease loss, which keep their own codes."""
        plan, ops = self.observer_plan, self.ops
        try:
            stage_dir = Path(tempfile.mkdtemp(prefix="relay-observer-"))
            ctx["observer_dir"] = str(stage_dir)
            for rel, data in plan["files"].items():  # the bytes verify_observer() hashed: nothing else is executed
                out = stage_dir / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
                if hashlib.sha256(out.read_bytes()).hexdigest() != hashlib.sha256(data).hexdigest():
                    raise Refused("the staged observer copy does not match")
            ctx["observer_tool_dir"] = str(stage_dir / "scripts")
            O, C, H = _import_observer(ctx["observer_tool_dir"])
            ops.docker(["volume", "create", names["pred"]], timeout=30)
            ctx["pred"] = True
            ops.docker(["run", "-i", "--rm", "--network", "none", "--mount", f"type=volume,src={names['pred']},dst=/p",
                        PROXY_IMAGE, "sh", "-c", "umask 077 && cat > /p/report.json && chown 10001:10001 /p "
                        "/p/report.json && chmod 0400 /p/report.json && chmod 0500 /p"], stdin=plan["predecessor"],
                       timeout=60)
            cfg = C.Config.load(plan["files"][plan["config"]],
                                {k[len("scripts/"):]: v for k, v in plan["files"].items() if k.startswith("scripts/")})
            kh = Path(self.evidence_dir) / "observer-known_hosts"
            kh.write_bytes(plan["files"]["scripts/" + cfg.raw["devWorker"]["knownHosts"]])
            cfg.raw["devWorker"]["knownHosts"] = str(kh)
            evidence = O.Evidence(str(Path(self.evidence_dir) / "observer.jsonl"))
            ledger = H.Ledger(os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/state"),
                                           "relay-operator", "handoff-ledger"), cfg.raw["holder"]["preparationId"])
            ctx["ledger"] = ledger
            host = ctx["host"] = H.ObserverHost(
                gate=None, cfg=cfg, evidence=evidence, on_fault=self._on_observer_fault,
                run_start_ms=int(ops.now() * 1000), runner_cutoff_ms=int((runner_deadline - 120) * 1000),
                lease_ok=lambda: lease_held(ops, self.run_id), predecessor_bytes=plan["predecessor"],
                predecessor_sha256=plan["predecessorSha256"], ledger=ledger)
            H.prerun(host, O.Budget(min(180_000, int((cleanup_deadline - ops.now()) * 1000)), host.cancelled))
        except (Interrupted, LeaseLost):
            raise
        except Refused as e:
            self.fail(2, stage="observer-prerun", reason=str(e)[:200])
            return False
        except BaseException as e:  # noqa: BLE001 - every other pre-run failure is an observer fault
            if type(e).__name__ == "Refuse":  # an existing ledger: refused before any authority
                self.fail(2, stage="observer-prerun", reason=str(e)[:200])
            else:
                self.fail(9, stage="observer-prerun", error=type(e).__name__, detail=str(e)[:200])
            return False
        self.note(stage="observer-prerun", holder=host.state.holder)
        return True

    def _on_observer_fault(self, reason: str, evidence: bool = False) -> None:
        if evidence:  # the observer's evidence could not be made durable: the broker's evidence path (6)
            self.evidence_failed.set()
        self.observer_fault.set()
        self.note(stage="observer-fault", reason=str(reason)[:200], evidence=evidence)

    def _observer_start(self, names, ctx, sinks) -> bool:
        """Start the gate FROM THE VERIFIED COPY, wait for its ready line, start the host threads."""
        ops, host = self.ops, ctx["host"]
        try:
            sinks["observer-gate.err"] = open(Path(self.evidence_dir) / "observer-gate.err", "ab", buffering=0)
            gate = ctx["gate"] = ops.popen(
                ["docker", "run", "-i", "--name", names["gate"], "--network", "none", "--read-only", "--cap-drop",
                 "ALL", "--security-opt", "no-new-privileges", "--log-driver", "none", "--user", UID, "--pids-limit",
                 "32", "--memory", "64m", "--mount", f"type=bind,src={ctx['observer_tool_dir']},dst=/tool,readonly",
                 "--mount", f"type=volume,src={names['volume']},dst={SOCKET_DIR}",
                 PROXY_IMAGE, "python3", "-I", "/tool/relay-observer-gate.py"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            t = threading.Thread(target=self._stream, args=(gate.stderr, sinks["observer-gate.err"]), daemon=True)
            t.start()
            ctx["threads"].append(t)
            host.gate = gate  # its STDOUT frames are parsed in memory only: never streamed or logged raw
            host.start(ready_timeout=15.0)
        except (Interrupted, LeaseLost):
            raise
        except BaseException as e:  # noqa: BLE001
            self.fail(9, stage="observer-gate", error=type(e).__name__)
            return False
        self.note(stage="observer-ready")
        return True

    def _until(self, cond, deadline: float) -> bool:
        while not cond():
            left = deadline - self.ops.now()
            if left <= 0:
                return False
            self.ops.sleep(min(1.0, left))
        return True

    def _stage(self, name: str, fn):
        """One cleanup stage. Signals are deferred during cleanup; any error is reported and the next stage runs."""
        try:
            return fn()
        except BaseException as e:  # noqa: BLE001
            self.note(stage=name, error=type(e).__name__, detail=str(e)[:200])
            return None

    def _exit_code(self, name: str, proc) -> int | None:
        rc = proc.poll() if proc is not None else None
        if rc is not None:
            return rc
        out = self._stage(f"{name}-inspect", lambda: self.ops.docker(
            ["inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", name], timeout=20, check=False))
        parts = out.decode(errors="replace").split() if out else []
        return int(parts[1]) if len(parts) == 2 and parts[0] == "false" and parts[1].lstrip("-").isdigit() else None

    def _cleanup(self, names, ctx, sinks, cleanup_deadline=None):
        ops, proxy, runner = self.ops, ctx["proxy"], ctx["runner"]
        host = ctx.get("host")
        if host is not None:
            host.stop()  # cancel at once: no collector dispatches after this point
        if runner is not None and runner.poll() is None:
            self._stage("runner-kill", lambda: ops.docker(["kill", names["runner"]], timeout=20, check=False))
            self._stage("runner-wait", lambda: self._until(lambda: runner.poll() is not None, ops.now() + 20))
        gate = ctx.get("gate")
        if host is not None:  # bounded by the remaining cleanup time, keeping 300 s for revoke and logout
            reserve = (cleanup_deadline - 300 - ops.now()) if cleanup_deadline else 10.0
            idle = self._stage("observer-drain", lambda: host.drain(max(0.0, min(10.0, reserve))))
            if idle is not True:
                self.fail(9, stage="observer-drain", reason="a collector was still running; its evidence stays open")
        if gate is not None and gate.poll() is None:
            self._stage("observer-gate-stop", lambda: ops.docker(["stop", "-t", "3", names["gate"]], timeout=15,
                                                                  check=False))
        if host is not None and idle is True:  # closing under a running worker could lose its last line
            self._stage("observer-evidence-close", host.ev.close)
        if ctx.get("ledger") is not None:
            self._stage("observer-ledger-close", ctx["ledger"].close)
        if ctx.get("observer_dir"):
            shutil.rmtree(ctx["observer_dir"], ignore_errors=True)
        if proxy is not None and proxy.poll() is None:  # SIGTERM -> drain 5 s + logout 20 s, inside a 45 s grace
            self._stage("proxy-stop", lambda: ops.docker(["stop", "-t", "45", names["proxy"]], timeout=70,
                                                          check=False))
            self._stage("proxy-wait", lambda: self._until(lambda: proxy.poll() is not None, ops.now() + 15))
        if proxy is not None:
            prc = self._exit_code(names["proxy"], proxy)
            self.note(stage="proxy-exited", exit=prc)
            if prc != 0:  # 4 = logout not confirmed; None = unknown termination; anything else = failure
                self.fail(4, stage="proxy-logout", exit=prc)
        incomplete = False
        for t in ctx["threads"]:  # drain the evidence before anything is removed
            self._stage("evidence-join", lambda t=t: t.join(timeout=JOIN_TIMEOUT_S))
            incomplete = incomplete or t.is_alive()
        if incomplete or self.evidence_failed.is_set():
            self.fail(6, stage="evidence", reason="a log stream failed or did not finish draining")
            for key in ("proxy", "runner"):  # recover what Docker kept, before the containers are removed
                if ctx[key] is not None:
                    def recover(k=key):
                        out = ops.docker(["logs", names[k]], timeout=60, check=False)
                        (Path(self.evidence_dir) / f"{k}.recovered").write_bytes(out or b"")
                        return True
                    self._stage(f"{key}-logs-recover", recover)
        for s in sinks.values():
            self._stage("evidence-close", s.close)
        for key in ("runner", "proxy", "gate"):
            if ctx.get(key) is not None:
                self._stage(f"{key}-remove", lambda k=key: ops.docker(["rm", "-f", names[k]], timeout=30,
                                                                      check=False))
        if ctx.get("pred"):
            self._stage("predecessor-volume-remove", lambda: ops.docker(["volume", "rm", "-f", names["pred"]],
                                                                         timeout=30, check=False))
        self._stage("volume-remove", lambda: ops.docker(["volume", "rm", "-f", names["volume"]], timeout=30,
                                                        check=False))
        if runner is not None:  # export the library's durable store; the volume itself is KEPT
            def export():
                tar = ops.docker(["run", "--rm", "--network", "none", "--mount",
                                  f"type=volume,src={names['state']},dst=/state,readonly", PROXY_IMAGE,
                                  "tar", "-C", "/state", "-cf", "-", "."], timeout=120)
                (Path(self.evidence_dir) / "state.tar").write_bytes(tar)
                return True
            if self._stage("state-export", export) is None:
                self.fail(6, stage="state-export", volume=names["state"])
        if ctx["granted"] and ctx["subject"]:
            owned = self._stage("lease-secure", lambda: lease_secure(ops, self.run_id, CLEANUP_LEASE_S))
            if owned is True:
                after = self._stage("revoke", lambda: ops.revoke(ctx["subject"]))
                self.note(stage="revoke", readback=after)
                if after != REVOKED:
                    self.fail(3, stage="revoke", readback=after)
                if self._stage("keycloak-logout", lambda: (ops.end_sso(ctx["uid"]), True)[1]) is None:
                    self.fail(5, stage="keycloak-logout")
            elif owned is False:
                self.fail(7, stage="lease", reason="a successor holds a live Lease; revocation left to it")
            else:
                self.fail(7, stage="lease", reason="Lease ownership unconfirmed; NOT revoked: the reaper or "
                                                   "`relay-operator.py revoke` must clear it")
        if ctx["leased"]:
            released = self._stage("lease-release", lambda: lease_release(ops, self.run_id))
            if released is None:
                self.fail(7, stage="lease-release", reason="release failed; the Lease expires on its own")


def run_session(ops: Ops, manifest_ref: str, bundle: str, runner_minutes: int, evidence_dir: str,
                predecessor_report: str | None = None) -> int:
    return Run(ops, manifest_ref, bundle, runner_minutes, evidence_dir, predecessor_report).main()
