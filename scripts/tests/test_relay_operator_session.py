"""Fault tests for scripts/relay_operator_session.py (plans/2026-11-27-relay-operator-session-plan.md, section 2).

Every collaborator is a fake: an in-memory Lease store with resourceVersion conflicts, recording docker/kubectl, and
scripted proxy/Runner processes on a controllable clock. Linux (symlinks); run in docker python:3.13.
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import relay_operator_session as ros  # noqa: E402

COMMIT = "a" * 40
IMAGE = "node:22-bookworm-slim@sha256:" + "b" * 64


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def now(self):
        return self.t

    def sleep(self, s):
        import time as _t
        _t.sleep(0.002)  # yield real time, so streaming threads run while the fake clock advances
        self.t += s


class Stream:
    def __init__(self, lines=()):
        self.lines = list(lines)

    def readline(self):
        return self.lines.pop(0) if self.lines else b""


class Sink:
    def __init__(self):
        self.data, self.closed = b"", False

    def write(self, b):
        self.data += b

    def close(self):
        self.closed = True


class Proc:
    def __init__(self, clock, out=(), exit_at=None, rc=0):
        self.clock, self.exit_at, self.rc, self.returncode = clock, exit_at, rc, None
        self.stdout, self.stderr, self.stdin = Stream(out), Stream([b'{"event":"x"}\n']), Sink()

    def poll(self):
        if self.returncode is None and self.exit_at is not None and self.clock.now() >= self.exit_at:
            self.returncode = self.rc
        return self.returncode


class World:
    """The fakes, wired into an ros.Ops."""

    def __init__(self, files=None, reviewed=True, lease=None, proxy_rc=0, runner_after=30, runner_rc=0,
                 runner_term_after=10, ready=True, revoke_result=ros.REVOKED, lease_get_error_after=None):
        self.clock = Clock()
        self.lease, self.rv = lease, 1
        self.calls = []
        self.granted, self.sso = [], []
        self.revokes = 0
        self.revoke_result = revoke_result
        self.proxy = Proc(self.clock, [b'READY {"actor":"x"}\n'] if ready else [], None, proxy_rc)
        self.runner = Proc(self.clock, [b'{"snapshot":1}\n'], self.clock.now() + runner_after, runner_rc)
        self.runner_term_after = runner_term_after
        self.lease_gets = 0
        self.lease_get_error_after = lease_get_error_after
        self.manifest = {"version": 1, "image": IMAGE, "entrypoint": ["node", "/bundle/main.cjs"],
                         "files": files if files is not None else {}}
        self.reviewed = reviewed
        self.ops = ros.Ops(creds=self.creds, grant=self.granted.append, revoke=self.revoke,
                           end_sso=self.sso.append, kubectl=self.kubectl, docker=self.docker,
                           git_show=lambda c, p: json.dumps(self.manifest).encode(),
                           git_is_reviewed=lambda c: self.reviewed, popen=self.popen,
                           now=self.clock.now, sleep=self.clock.sleep, tool_dir="/tool")

    def creds(self):
        return {"subject": "iss|sub", "username": "relay-operator@ailab.invalid", "password": "pw",
                "keycloak_user_id": "kc"}

    def revoke(self, subject):
        self.revokes += 1
        return self.revoke_result

    def kubectl(self, args, stdin=None, timeout=60, check=True):
        self.calls.append(("kubectl", tuple(args)))
        if "get" in args:
            self.lease_gets += 1
            if self.lease_get_error_after is not None and self.lease_gets > self.lease_get_error_after:
                raise RuntimeError("apiserver unreachable")
            return json.dumps(self.lease).encode() if self.lease else b""
        doc = json.loads(stdin)
        if "create" in args:
            if self.lease:
                raise RuntimeError("AlreadyExists")
        elif "replace" in args:
            if not self.lease or doc["metadata"].get("resourceVersion") != self.lease["metadata"]["resourceVersion"]:
                raise RuntimeError("Conflict")
        self.rv += 1
        doc["metadata"]["resourceVersion"] = str(self.rv)
        self.lease = doc
        return b""

    def docker(self, args, stdin=None, timeout=60, check=True):
        self.calls.append(("docker", tuple(args)))
        if args[:1] == ["stop"]:
            self.proxy.returncode = self.proxy.rc
        elif args[:3] == ["kill", "-s", "TERM"]:
            self.runner.exit_at = self.clock.now() + self.runner_term_after
        elif args[:1] == ["kill"]:
            self.runner.returncode = 137
        elif args[:1] == ["run"] and "tar" in args:
            return b"TAR"
        return b""

    def popen(self, args, **kw):
        self.calls.append(("popen", tuple(args)))
        return self.proxy if "/tool/relay-session-proxy.py" in args else self.runner


def bundle(tmp, files):
    d = Path(tmp) / "bundle"
    d.mkdir()
    out = {}
    for rel, data in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        out[rel] = hashlib.sha256(data).hexdigest()
    return str(d), out


def run(w, tmp, bundle_dir, minutes=1):
    ev = os.path.join(tmp, "evidence")
    rc = ros.Run(w.ops, f"{COMMIT}:docs/runbooks/inference-canary-v0113/automation-bundle.json", bundle_dir,
                 minutes, ev, run_id="session-test").main()
    return rc, ev


class Manifest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir, self.files = bundle(self.tmp, {"main.cjs": b"main", "lib/a.cjs": b"a"})

    def test_unreviewed_commit_refused_before_authority(self):
        w = World(self.files, reviewed=False)
        rc, _ = run(w, self.tmp, self.dir)
        self.assertEqual(rc, 2)
        self.assertEqual(w.granted, [])
        self.assertFalse(any(c[0] == "kubectl" for c in w.calls))

    def test_image_must_be_a_digest(self):
        w = World(self.files)
        w.manifest["image"] = "node:22"
        self.assertEqual(run(w, self.tmp, self.dir)[0], 2)

    def test_bundle_extra_symlink_altered_missing(self):
        for mutate in ("extra", "symlink", "altered", "missing"):
            tmp = tempfile.mkdtemp()
            d, files = bundle(tmp, {"main.cjs": b"main", "lib/a.cjs": b"a"})
            if mutate == "extra":
                Path(d, "evil.cjs").write_bytes(b"x")
            elif mutate == "symlink":
                os.symlink("/etc/passwd", os.path.join(d, "link.cjs"))
                files["link.cjs"] = "0" * 64
            elif mutate == "altered":
                Path(d, "main.cjs").write_bytes(b"changed")
            elif mutate == "missing":
                files["gone.cjs"] = "0" * 64
            w = World(files)
            self.assertEqual(run(w, tmp, d)[0], 2, mutate)
            self.assertEqual(w.granted, [], mutate)

    def test_runner_minutes_bounded(self):
        w = World(self.files)
        self.assertEqual(run(w, self.tmp, self.dir, minutes=46)[0], 2)


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir, self.files = bundle(self.tmp, {"main.cjs": b"main"})

    def docker_args(self, w, kind):
        for c in w.calls:
            if c[0] == "popen" and (("/tool/relay-session-proxy.py" in c[1]) == (kind == "proxy")):
                return c[1]
        raise AssertionError("not started: " + kind)

    def test_happy_path(self):
        w = World(self.files)
        rc, ev = run(w, self.tmp, self.dir)
        self.assertEqual(rc, 0, json.loads(Path(ev, "result.json").read_text()))
        self.assertEqual(w.granted, ["iss|sub"])
        self.assertEqual(w.revokes, 2)  # orphan clear before the grant, revoke after
        self.assertEqual(w.sso, ["kc"])
        self.assertEqual(w.lease["spec"]["holderIdentity"], "")  # released
        runner = self.docker_args(w, "runner")
        for flag in ("--network", "none", "--read-only", "--cap-drop", "ALL", "no-new-privileges", IMAGE):
            self.assertIn(flag, runner)
        proxy = self.docker_args(w, "proxy")
        self.assertNotIn("none", proxy)
        self.assertIn("--read-only", proxy)
        self.assertEqual(w.proxy.stdin.data.split(b"\n")[0], b"pw")
        for f in ("run.json", "result.json", "proxy.jsonl", "runner.jsonl", "state.tar"):
            self.assertTrue(Path(ev, f).exists(), f)
        self.assertNotIn(b"pw", Path(ev, "run.json").read_bytes())

    def test_live_lease_refuses_without_authority(self):
        lease = {"metadata": {"name": ros.LEASE, "resourceVersion": "7",
                              "annotations": {"chifor.me/expires-at": str(int(1_800_000_000 + 3600))}},
                 "spec": {"holderIdentity": "session-other"}}
        w = World(self.files, lease=lease)
        rc, _ = run(w, self.tmp, self.dir)
        self.assertEqual(rc, 7)
        self.assertEqual(w.granted, [])
        self.assertEqual(w.revokes, 0)
        self.assertEqual(w.lease["spec"]["holderIdentity"], "session-other")

    def test_expired_lease_taken_over(self):
        lease = {"metadata": {"name": ros.LEASE, "resourceVersion": "7",
                              "annotations": {"chifor.me/expires-at": "1"}}, "spec": {"holderIdentity": "old"}}
        w = World(self.files, lease=lease)
        self.assertEqual(run(w, self.tmp, self.dir)[0], 0)

    def test_runner_overruns_deadline_gets_sigterm(self):
        w = World(self.files, runner_after=10_000, runner_term_after=20)
        rc, _ = run(w, self.tmp, self.dir)
        self.assertEqual(rc, 8)
        self.assertIn(("docker", ("kill", "-s", "TERM", "relay-runner-session-test")), w.calls)
        self.assertEqual(w.revokes, 2)

    def test_runner_never_cleans_up_is_killed(self):
        w = World(self.files, runner_after=10_000, runner_term_after=10_000)
        rc, _ = run(w, self.tmp, self.dir)
        self.assertEqual(rc, 8)
        self.assertIn(("docker", ("kill", "relay-runner-session-test")), w.calls)
        self.assertEqual(w.revokes, 2)

    def test_successor_holds_lease_at_cleanup(self):
        w = World(self.files)
        orig = w.revoke

        def steal_then_revoke(subject):  # after the orphan clear, a successor takes the Lease mid-run
            r = orig(subject)
            if w.revokes == 1:
                w.lease = {"metadata": {"name": ros.LEASE, "resourceVersion": "99",
                                        "annotations": {"chifor.me/expires-at": str(int(w.clock.now() + 9999))}},
                           "spec": {"holderIdentity": "session-successor"}}
            return r
        w.ops.revoke = steal_then_revoke
        rc, _ = run(w, self.tmp, self.dir)
        self.assertEqual(rc, 7)
        self.assertEqual(w.revokes, 1)  # never revoked the successor's authority
        self.assertEqual(w.lease["spec"]["holderIdentity"], "session-successor")

    def test_lease_check_error_never_revokes(self):
        w = World(self.files, lease_get_error_after=2)  # acquire's two reads succeed, every later read fails
        rc, _ = run(w, self.tmp, self.dir)
        self.assertEqual(w.revokes, 1)  # only the orphan clear: unknown ownership leaves it to the reaper
        self.assertEqual(w.sso, [])
        self.assertEqual(rc, 7)

    def test_signal_during_cleanup_is_deferred(self):
        import signal as sig
        w = World(self.files)
        orig = w.docker

        def docker(args, **kw):
            if args[:1] == ["stop"]:  # a SIGTERM arrives in the middle of cleanup
                sig.getsignal(sig.SIGTERM)(sig.SIGTERM, None)
            return orig(args, **kw)
        w.ops.docker = docker
        rc, ev = run(w, self.tmp, self.dir)
        self.assertEqual(w.revokes, 2)
        self.assertEqual(w.sso, ["kc"])
        self.assertEqual(rc, 0)

    def test_no_ready_with_logout_failure_reports_the_higher_precedence(self):
        w = World(self.files, ready=False, proxy_rc=4)
        saved, ros.READY_TIMEOUT_S = ros.READY_TIMEOUT_S, 0.2
        try:
            rc, ev = run(w, self.tmp, self.dir)
        finally:
            ros.READY_TIMEOUT_S = saved
        self.assertEqual(rc, 4)
        self.assertEqual(json.loads(Path(ev, "result.json").read_text())["rc"], 4)

    def test_systemexit_from_a_wrapper_is_a_failure(self):
        w = World(self.files)

        def bad_grant(subject):
            w.granted.append(subject)
            raise SystemExit("relay-operator: member.js ... failed (1)")
        w.ops.grant = bad_grant
        rc, ev = run(w, self.tmp, self.dir)
        self.assertEqual(rc, 8)
        self.assertEqual(json.loads(Path(ev, "result.json").read_text())["rc"], 8)
        self.assertEqual(w.revokes, 2)  # the grant may have committed: still revoked

    def test_unknown_proxy_termination_is_a_logout_failure(self):
        w = World(self.files)
        orig = w.docker

        def docker(args, **kw):
            if args[:1] == ["stop"]:
                return b""  # the stop "succeeds" but the process never reports an exit
            if args[:1] == ["inspect"]:
                return b"true 0"
            return orig(args, **kw)
        w.ops.docker = docker
        rc, _ = run(w, self.tmp, self.dir)
        self.assertEqual(rc, 4)

    def test_evidence_sink_failure_sets_the_flag_and_keeps_draining(self):
        w = World(self.files)
        r = ros.Run(w.ops, "x", "y", 1, os.path.join(self.tmp, "e2"))

        class BrokenSink:
            def write(self, b):
                raise OSError("disk full")
        stream = Stream([b"a" + bytes([10]), b"b" + bytes([10]), b"c" + bytes([10])])
        r._stream(stream, BrokenSink())
        self.assertTrue(r.evidence_failed.is_set())
        self.assertEqual(stream.lines, [])  # fully drained: the producer is never blocked

    def test_revoke_failure_outranks_runner_failure(self):
        w = World(self.files, runner_rc=1)
        orig = w.revoke

        def fail_second(subject):
            r = orig(subject)
            return r if w.revokes == 1 else "1 memberships, 0 sessions"
        w.ops.revoke = fail_second
        self.assertEqual(run(w, self.tmp, self.dir)[0], 3)

    def test_proxy_logout_failure(self):
        w = World(self.files, proxy_rc=4)
        self.assertEqual(run(w, self.tmp, self.dir)[0], 4)

    def test_no_ready_still_cleans_up(self):
        w = World(self.files, ready=False)
        saved, ros.READY_TIMEOUT_S = ros.READY_TIMEOUT_S, 0.2
        try:
            rc, _ = run(w, self.tmp, self.dir)
        finally:
            ros.READY_TIMEOUT_S = saved
        self.assertEqual(rc, 8)
        self.assertEqual(w.revokes, 2)
        self.assertEqual(w.sso, ["kc"])
        self.assertFalse(any(c[0] == "popen" and "--network" in c[1] for c in w.calls))  # no Runner started

    def test_orphan_not_cleared_refuses_grant(self):
        w = World(self.files, revoke_result="1 memberships, 0 sessions")
        rc, _ = run(w, self.tmp, self.dir)
        self.assertEqual(rc, 3)
        self.assertEqual(w.granted, [])

    def test_evidence_dir_must_be_new(self):
        os.mkdir(os.path.join(self.tmp, "evidence"))
        w = World(self.files)
        self.assertEqual(run(w, self.tmp, self.dir)[0], 2)


if __name__ == "__main__":
    unittest.main()
