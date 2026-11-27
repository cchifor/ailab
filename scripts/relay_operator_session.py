"""The `relay-operator.py session` orchestrator (plans/2026-11-27-relay-operator-session-plan.md, section 2).

One bounded run: verify the Runner bundle against a manifest from a REVIEWED ailab commit, take the fenced Kubernetes
Lease, clear orphan authority, grant temporary admin, start the session proxy (scripts/relay-session-proxy.py) and the
Runner (--network none, sharing only the proxy's Unix socket), enforce the runner and cleanup deadlines, then run the
independent, bounded cleanup stages. No caller-supplied Docker arguments; no scope parameters.

Exit codes (precedence order): 3 revoke not confirmed, 4 proxy logout not confirmed, 5 Keycloak logout failed,
6 evidence capture failed, 7 Lease lost or contended, 8 Runner failed or exceeded its deadline, 2 refused before any
authority (bad manifest/bundle/arguments), 0 success.

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
READY_TIMEOUT_S = 120
DIGEST_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REL_PATH = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
REVOKED = "0 memberships, 0 sessions"


class Refused(Exception):
    """Refused before any authority exists (rc 2)."""


class LeaseLost(Exception):
    """The Lease is held by someone else, or this run no longer holds it (rc 7)."""


class Interrupted(BaseException):
    """SIGTERM/SIGINT, converted so every stage still runs."""


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
    except (ValueError, subprocess.CalledProcessError) as e:
        raise Refused(f"manifest unreadable at {ref}: {type(e).__name__}")
    if not isinstance(m, dict) or m.get("version") != 1 or set(m) != {"version", "image", "entrypoint", "files"}:
        raise Refused("manifest must be {version: 1, image, entrypoint, files}")
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


# ---------------------------------------------------------------- the fenced Lease
def _lease_get(ops: Ops) -> dict | None:
    out = ops.kubectl(["-n", NAMESPACE, "get", "lease", LEASE, "-o", "json", "--ignore-not-found"], timeout=30)
    return json.loads(out) if out.strip() else None


def _lease_expired(lease: dict, now: float) -> bool:
    ann = (lease.get("metadata", {}).get("annotations") or {})
    holder = (lease.get("spec") or {}).get("holderIdentity") or ""
    try:
        exp = float(ann.get("chifor.me/expires-at", "0"))
    except ValueError:
        exp = 0.0
    return not holder or now >= exp


def _lease_doc(holder: str, expires_at: float, rv: str | None = None) -> bytes:
    meta = {"name": LEASE, "namespace": NAMESPACE,
            "annotations": {"chifor.me/expires-at": str(int(expires_at))}}
    if rv:
        meta["resourceVersion"] = rv
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    return json.dumps({"apiVersion": "coordination.k8s.io/v1", "kind": "Lease", "metadata": meta,
                       "spec": {"holderIdentity": holder, "acquireTime": now, "renewTime": now,
                                "leaseDurationSeconds": max(1, int(expires_at - time.time()))}}).encode()


def lease_acquire(ops: Ops, holder: str, expires_at: float) -> None:
    cur = _lease_get(ops)
    if cur is None:
        ops.kubectl(["create", "-f", "-"], stdin=_lease_doc(holder, expires_at), timeout=30)
    elif _lease_expired(cur, ops.now()):
        # optimistic concurrency: a concurrent taker makes this replace fail with a conflict
        ops.kubectl(["replace", "-f", "-"], stdin=_lease_doc(holder, expires_at, cur["metadata"]["resourceVersion"]),
                    timeout=30)
    else:
        raise LeaseLost(f"the Lease is held by {cur['spec'].get('holderIdentity')!r}")
    got = _lease_get(ops)
    if not got or got["spec"].get("holderIdentity") != holder:
        raise LeaseLost("lost the Lease race")


def lease_other_live(ops: Ops, holder: str) -> bool:
    """True ONLY when a different holder positively holds an unexpired Lease (a successor run or the reaper)."""
    cur = _lease_get(ops)
    if not cur:
        return False
    other = (cur.get("spec") or {}).get("holderIdentity") or ""
    return bool(other) and other != holder and not _lease_expired(cur, ops.now())


def lease_release(ops: Ops, holder: str) -> bool:
    cur = _lease_get(ops)
    if not cur or cur["spec"].get("holderIdentity") != holder:
        return False
    ops.kubectl(["replace", "-f", "-"], stdin=_lease_doc("", 0, cur["metadata"]["resourceVersion"]), timeout=30)
    return True


# ---------------------------------------------------------------- the run
@dataclass
class Run:
    ops: Ops
    manifest_ref: str
    bundle: str
    runner_minutes: int
    evidence_dir: str
    run_id: str = field(default_factory=lambda: "session-" + uuid.uuid4().hex[:12])
    rc: int = 0
    notes: list = field(default_factory=list)

    def note(self, **kv):
        kv["t"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.notes.append(kv)
        print("relay-operator session: " + json.dumps(kv), flush=True)

    def fail(self, code: int, **kv):
        prec = [3, 4, 5, 6, 7, 8, 2]
        if self.rc == 0 or (code in prec and (self.rc not in prec or prec.index(code) < prec.index(self.rc))):
            self.rc = code
        self.note(failure=code, **kv)

    # ---------------- streaming evidence
    def _stream(self, pipe, path: Path, ready: threading.Event | None = None, ready_line: list | None = None):
        try:
            with open(path, "ab", buffering=0) as f:
                for line in iter(pipe.readline, b""):
                    if ready is not None and not ready.is_set() and line.startswith(b"READY "):
                        ready_line.append(line)
                        ready.set()
                    f.write(line)
        except OSError:
            self.fail(6, stage="evidence", file=path.name)

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
        except Refused as e:
            self.fail(2, reason=str(e))
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

        def interrupt(*_):
            raise Interrupted()

        for s in prev:
            signal.signal(s, interrupt)
        names = {"proxy": f"relay-proxy-{self.run_id}", "runner": f"relay-runner-{self.run_id}",
                 "volume": f"relay-session-{self.run_id}", "state": f"relay-state-{self.run_id}"}
        proxy = runner = None
        threads = []
        granted = False
        subject = uid = None
        try:
            try:
                lease_acquire(ops, self.run_id, cleanup_deadline + LEASE_GRACE_S)
            except LeaseLost as e:
                self.fail(7, stage="lease", reason=str(e))
                return self.rc
            self.note(stage="lease", holder=self.run_id)
            c = ops.creds()
            subject, username, password, uid = c["subject"], c["username"], c["password"], c["keycloak_user_id"]
            c = None
            orphan = ops.revoke(subject)  # clear any orphan authority before granting anew
            if orphan != REVOKED:
                self.fail(3, stage="orphan-clear", readback=orphan)
                return self.rc
            granted = True  # inside the cleanup scope from here: a grant may commit and still report an error
            ops.grant(subject)
            self.note(stage="granted")
            ops.docker(["volume", "create", "--driver", "local", "--opt", "type=tmpfs", "--opt", "device=tmpfs",
                        "--opt", "o=size=1m,uid=10001,gid=10001,mode=0700", names["volume"]], timeout=30)
            # The library's durable store: a named volume (kept after the run), private to the Runner's uid.
            ops.docker(["volume", "create", names["state"]], timeout=30)
            ops.docker(["run", "--rm", "--network", "none", "--mount", f"type=volume,src={names['state']},dst=/state",
                        PROXY_IMAGE, "sh", "-c", "chown 10001:10001 /state && chmod 0700 /state"], timeout=120)
            self.note(stage="state-volume", volume=names["state"])
            proxy = ops.popen(
                ["docker", "run", "--rm", "-i", "--name", names["proxy"], "--read-only", "--cap-drop", "ALL",
                 "--security-opt", "no-new-privileges", "--user", UID, "--pids-limit", "64", "--memory", "256m",
                 "--mount", f"type=bind,src={ops.tool_dir},dst=/tool,readonly",
                 "--mount", f"type=volume,src={names['volume']},dst={SOCKET_DIR}",
                 PROXY_IMAGE, "python3", "-I", "/tool/relay-session-proxy.py"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            ready, ready_line = threading.Event(), []
            threads += [threading.Thread(target=self._stream, args=(proxy.stdout, ev / "proxy.out", ready, ready_line),
                                         daemon=True),
                        threading.Thread(target=self._stream, args=(proxy.stderr, ev / "proxy.jsonl"), daemon=True)]
            for t in threads:
                t.start()
            cfg = {"username": username, "runner_deadline": runner_deadline, "cleanup_deadline": cleanup_deadline}
            proxy.stdin.write((password + "\n" + json.dumps(cfg) + "\n").encode())
            proxy.stdin.close()
            password = None
            if not ready.wait(min(READY_TIMEOUT_S, max(0.0, cleanup_deadline - ops.now()))):
                self.fail(8, stage="proxy", reason="no READY")
                return self.rc
            self.note(stage="proxy-ready", ready=ready_line[0].decode(errors="replace").strip()[:300])
            runner = ops.popen(
                ["docker", "run", "--rm", "--name", names["runner"], "--network", "none", "--read-only",
                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", UID, "--pids-limit", "256",
                 "--memory", "1g", "--tmpfs", "/tmp:rw,size=64m,uid=10001,gid=10001,mode=0700",
                 "--mount", f"type=bind,src={bundle_copy},dst=/bundle,readonly",
                 "--mount", f"type=volume,src={names['state']},dst=/state",
                 "--mount", f"type=volume,src={names['volume']},dst={SOCKET_DIR}",
                 "-e", f"RELAY_SESSION_SOCKET={SOCKET_DIR}/proxy.sock", "-e", f"RUNNER_DEADLINE={runner_deadline}",
                 "-e", f"CLEANUP_DEADLINE={cleanup_deadline}", "-e", "AUTOMATION_DIR=/state/run",
                 "--entrypoint", manifest["entrypoint"][0], manifest["image"], *manifest["entrypoint"][1:]],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for args in ((runner.stdout, ev / "runner.jsonl"), (runner.stderr, ev / "runner.err")):
                t = threading.Thread(target=self._stream, args=args, daemon=True)
                t.start()
                threads.append(t)
            self.note(stage="runner-started")
            if not self._wait(runner, runner_deadline):
                self.note(stage="runner-deadline", action="SIGTERM")
                ops.docker(["kill", "-s", "TERM", names["runner"]], timeout=20, check=False)
                if not self._wait(runner, cleanup_deadline - 180):
                    self.fail(8, stage="runner", reason="did not finish cleanup before the cutoff; killed")
                    ops.docker(["kill", names["runner"]], timeout=20, check=False)
                    self._wait(runner, ops.now() + 20)
                else:
                    self.fail(8, stage="runner", reason="exceeded the runner deadline (cleaned up)")
            rrc = runner.returncode
            if rrc not in (0, None):
                self.fail(8, stage="runner", exit=rrc)
            self.note(stage="runner-exited", exit=rrc)
        except Interrupted:
            self.fail(8, stage="interrupted")
        except LeaseLost as e:
            self.fail(7, stage="lease", reason=str(e))
        except Exception as e:  # noqa: BLE001 - reported; the cleanup stages decide the rc
            self.fail(8, stage="error", error=type(e).__name__, detail=str(e)[:200])
        finally:
            self._cleanup(names, proxy, runner, threads, granted, subject, uid)
            for s, h in prev.items():
                signal.signal(s, h)
            shutil.rmtree(work, ignore_errors=True)
            (ev / "result.json").write_text(json.dumps({"runId": self.run_id, "rc": self.rc, "notes": self.notes},
                                                       indent=1))
        return self.rc

    def _wait(self, proc, deadline: float) -> bool:
        while proc.poll() is None:
            left = deadline - self.ops.now()
            if left <= 0:
                return False
            self.ops.sleep(min(1.0, left))
        return True

    def _stage(self, name: str, fn):
        """One cleanup stage: bounded by its callee's timeouts, never aborts the next stage."""
        while True:
            try:
                return fn()
            except Interrupted:
                self.note(stage=name, interrupted=True)  # finish the stage anyway
                continue
            except BaseException as e:  # noqa: BLE001
                self.note(stage=name, error=type(e).__name__, detail=str(e)[:200])
                return None

    def _cleanup(self, names, proxy, runner, threads, granted, subject, uid):
        ops = self.ops
        if runner is not None and runner.poll() is None:
            self._stage("runner-kill", lambda: ops.docker(["kill", names["runner"]], timeout=20, check=False))
        if proxy is not None and proxy.poll() is None:  # SIGTERM -> the proxy logs out within its 20 s budget
            self._stage("proxy-stop", lambda: ops.docker(["stop", "-t", "30", names["proxy"]], timeout=45,
                                                          check=False))
            self._wait(proxy, ops.now() + 10)
        if proxy is not None:
            prc = proxy.poll()
            self.note(stage="proxy-exited", exit=prc)
            if prc == 4:
                self.fail(4, stage="proxy-logout")
            elif prc not in (0, None):
                self.fail(4, stage="proxy", exit=prc)
        for t in threads:  # drain the evidence before anything is removed
            t.join(timeout=10)
        for key in ("runner", "proxy"):
            self._stage(f"{key}-remove", lambda k=key: ops.docker(["rm", "-f", names[k]], timeout=30, check=False))
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
        if granted and subject:
            other = self._stage("lease-check", lambda: lease_other_live(ops, self.run_id))
            if other is True:  # a successor positively holds a live Lease: never touch its authority
                self.fail(7, stage="lease", reason="a successor holds the Lease; revoke left to it or the reaper")
            else:  # this run's Lease, an expired/released one, or an unknown answer: revoke (no standing admin)
                after = self._stage("revoke", lambda: ops.revoke(subject))
                self.note(stage="revoke", readback=after)
                if after != REVOKED:
                    self.fail(3, stage="revoke", readback=after)
                if self._stage("keycloak-logout", lambda: (ops.end_sso(uid), True)[1]) is None:
                    self.fail(5, stage="keycloak-logout")
        if self._stage("lease-release", lambda: lease_release(ops, self.run_id)) is False and granted:
            self.note(stage="lease-release", released=False)


def run_session(ops: Ops, manifest_ref: str, bundle: str, runner_minutes: int, evidence_dir: str) -> int:
    return Run(ops, manifest_ref, bundle, runner_minutes, evidence_dir).main()
