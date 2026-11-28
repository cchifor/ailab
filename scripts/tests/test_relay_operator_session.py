"""Fault tests for scripts/relay_operator_session.py (plans/2026-11-27-relay-operator-session-plan.md, section 2).

Every collaborator is a fake: an in-memory Lease store with resourceVersion conflicts, recording docker/kubectl, and
scripted proxy/Runner processes on a controllable clock. Linux (symlinks); run in docker python:3.13.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

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

    def test_private_evidence_setup_failure_precedes_material_and_authority(self):
        w = World(self.files)
        with mock.patch.object(ros, "_new_evidence_directory", side_effect=OSError("synthetic ACL failure")), \
                mock.patch.object(ros, "load_manifest", side_effect=AssertionError("manifest read before private directory")):
            self.assertEqual(run(w, self.tmp, self.dir)[0], 2)
        self.assertEqual(w.calls, [])
        self.assertEqual(w.granted, [])
        self.assertEqual(w.revokes, 0)
        self.assertFalse((Path(self.tmp)/"evidence").exists())


class LeaseAndEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir, self.files = bundle(self.tmp, {"main.cjs": b"main"})

    def test_lease_secure_never_shortens_a_longer_own_lease(self):
        w = World(self.files)
        ros.lease_acquire(w.ops, "me", w.clock.now() + 3600)
        self.assertTrue(ros.lease_secure(w.ops, "me", 60))
        self.assertGreaterEqual(float(w.lease["metadata"]["annotations"]["chifor.me/expires-at"]),
                                w.clock.now() + 3599)

    def test_read_failure_sets_the_evidence_flag(self):
        w = World(self.files)
        r = ros.Run(w.ops, "x", "y", 1, os.path.join(self.tmp, "e3"))

        class BadPipe:
            def readline(self):
                raise OSError("broken pipe")

        class Sink:
            def write(self, b):
                return len(b)
        r._stream(BadPipe(), Sink())
        self.assertTrue(r.evidence_failed.is_set())

    def test_evidence_failure_before_the_runner_starts_stops_the_run(self):
        w = World(self.files)
        orig = w.popen

        def popen(args, **kw):
            p = orig(args, **kw)
            if "/tool/relay-session-proxy.py" in args:
                class BadErr:  # the proxy's stderr pipe fails at once
                    def readline(self):
                        raise OSError("gone")
                p.stderr = BadErr()
            return p
        w.ops.popen = popen
        rc, _ = run(w, self.tmp, self.dir)
        self.assertEqual(rc, 6)
        self.assertFalse(any(c[0] == "popen" and "--network" in c[1] for c in w.calls))  # no Runner started
        self.assertEqual(w.revokes, 2)


class BrokenOutput(unittest.TestCase):
    """A closed stdout/stderr (a piped consumer that exited) must never abort cleanup before the revoke."""

    class Broken:
        def write(self, s):
            raise BrokenPipeError("consumer gone")

        def flush(self):
            raise BrokenPipeError("consumer gone")

    def test_session_cleanup_survives_a_broken_stdout(self):
        tmp = tempfile.mkdtemp()
        d, files = bundle(tmp, {"main.cjs": b"main"})
        w = World(files)
        orig = w.ops.grant

        def grant_then_break(subject):
            orig(subject)
            sys.stdout = self.Broken()  # stdout dies right after the grant
        w.ops.grant = grant_then_break
        saved = sys.stdout
        try:
            rc, ev = run(w, tmp, d)
        finally:
            sys.stdout = saved
        self.assertEqual(w.revokes, 2)
        self.assertEqual(w.sso, ["kc"])
        self.assertEqual(w.lease["spec"]["holderIdentity"], "")
        self.assertEqual(json.loads(Path(ev, "result.json").read_text())["rc"], 0)


class CallLifecycle(unittest.TestCase):
    """relay-operator.py call() with every collaborator faked."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("relay_operator", ROOT / "scripts" / "relay-operator.py")
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.w = World({})
        m, w = self.m, self.w
        self.events = []
        m.ops = lambda: (ros, w.ops)
        m.creds = w.creds
        m.revoke = lambda s: (self.events.append("revoke"), ros.REVOKED)[1]
        m.grant = lambda s: self.events.append("grant")
        m.counts = lambda s: "1 memberships, 1 sessions"
        m.end_sso = lambda uid: self.events.append("sso")
        m.run = lambda args, stdin=None, check=True, timeout=120: (self.events.append(" ".join(args[:3])), b"")[1]

    def fake_docker(self, behaviour):
        import subprocess as sp

        class P:
            returncode, stdout, stderr = 0, b"ok", b""

        def runner(args, **kw):
            self.events.append("docker-run")
            return behaviour(args, **kw) or P()
        # self.m.subprocess IS the global subprocess module: patch it reversibly, or every later suite inherits it
        p = mock.patch.object(self.m.subprocess, "run", runner)
        p.start()
        self.addCleanup(p.stop)

    def test_happy_call(self):
        self.fake_docker(lambda a, **k: None)
        self.assertEqual(self.m.call(["me"]), 0)
        self.assertEqual(self.events[:3], ["revoke", "grant", "docker-run"])
        self.assertIn("docker rm -f", self.events)
        self.assertEqual(self.events[-2:], ["revoke", "sso"])
        self.assertEqual(self.w.lease["spec"]["holderIdentity"], "")

    def test_successor_during_the_call_is_never_revoked(self):
        def steal(args, **kw):
            self.w.lease = {"metadata": {"name": ros.LEASE, "resourceVersion": "99",
                                         "annotations": {"chifor.me/expires-at": str(int(self.w.clock.now() + 9999))}},
                            "spec": {"holderIdentity": "session-successor"}}
        self.fake_docker(steal)
        self.assertEqual(self.m.call(["me"]), 7)
        self.assertEqual(self.events.count("revoke"), 1)  # only the orphan clear
        self.assertNotIn("sso", self.events)

    def test_timed_out_container_is_removed_before_the_revoke(self):
        import subprocess as sp

        def hang(args, **kw):
            raise sp.TimeoutExpired(args, 300)
        self.fake_docker(hang)
        rc = self.m.call(["me"])
        self.assertNotEqual(rc, 0)
        i_rm, i_revoke = self.events.index("docker rm -f"), len(self.events) - 1 - self.events[::-1].index("revoke")
        self.assertLess(i_rm, i_revoke)

    def test_call_cleanup_survives_a_broken_stderr(self):
        self.fake_docker(lambda a, **k: None)
        orig_grant = self.m.grant
        saved = sys.stderr

        def grant_then_break(subject):
            orig_grant(subject)
            sys.stderr.inner = BrokenOutput.Broken()  # the wrapped stream dies after the grant
        self.m.grant = grant_then_break
        try:
            rc = self.m.call(["me"])
        finally:
            sys.stderr = saved
        self.assertEqual(self.events[-2:], ["revoke", "sso"])
        self.assertEqual(self.w.lease["spec"]["holderIdentity"], "")
        self.assertEqual(rc, 0)

    def test_signal_during_cleanup_is_deferred(self):
        import signal as sig
        self.fake_docker(lambda a, **k: None)
        orig_sso = self.m.end_sso

        def sso(uid):
            sig.getsignal(sig.SIGTERM)(sig.SIGTERM, None)  # arrives mid-cleanup
            orig_sso(uid)
        self.m.end_sso = sso
        self.assertEqual(self.m.call(["me"]), 0)
        self.assertIn("sso", self.events)
        self.assertEqual(self.w.lease["spec"]["holderIdentity"], "")


class FinalizationPreparation(unittest.TestCase):
    """Broker boundary tests. The validator process result is synthetic; actual CLI/Hub integration is separate."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil_cleanup, self.tmp)
        self.dir, files = bundle(self.tmp, {ros.FINALIZATION_ENTRY: b"synthetic pinned validator entry"})
        self.w = World(files)
        self.w.manifest = {"version": 2, "purpose": ros.FINALIZATION_PURPOSE, "image": IMAGE,
                           "entrypoint": ["node", "/bundle/"+ros.FINALIZATION_ENTRY], "files": files,
                           "finalizationObserver": {"files": {"scripts/config.json": "a"*64}, "config": "scripts/config.json"}}
        self.root = Path(self.tmp)/"inputs"
        self.root.mkdir(mode=0o700)
        self.raws = {name: ('{"synthetic":"'+name+'"}').encode() for name in ("a", "b", "bInputs", "c")}
        pins = {name: ros._digest(raw) for name, raw in self.raws.items()}
        sources = {"hubSource": "1"*40, "routerSource": "2"*40, "observerQualificationSha256": "3"*64}
        self.plan = {"version": 1, "purpose": ros.FINALIZATION_PURPOSE, "runId": str(uuid.uuid4()), "reportPins": pins,
                     "cleanupDeadline": int((self.w.clock.now()+300)*1000), "qualificationSha256": ros._canonical_digest(sources)}
        self.decision = {**self.plan, "bindingIds": sorted(ros.FINALIZATION_BINDINGS), "noFurtherUse": True}
        self.plan["decisionSha256"] = ros._digest(json.dumps(self.decision).encode())
        self.manifest = {"version": 1, "pins": pins, "cleanupDeadline": self.plan["cleanupDeadline"],
                         "decisionSha256": self.plan["decisionSha256"]}
        self.planpath = Path(self.tmp)/"operator-plan.json"
        self.write(self.planpath, self.plan)
        for name, raw in self.raws.items(): self.write(self.root/(name+".json"), raw)
        self.write(self.root/"manifest.json", self.manifest)
        self.write(self.root/"decision.json", self.decision)
        self.expected = ros._digest(self.planpath.read_bytes())
        scope = {"tenantId": "00000000-0000-4000-8000-000000000001", "hostId": "b3b1be1a-275c-4a2e-b515-428b39c43171",
                 "connectionId": "babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8", "executionIdentity": "c4",
                 "bindingIds": sorted(ros.FINALIZATION_BINDINGS), "profileIds": sorted(ros.FINALIZATION_BINDINGS.values())}
        binding, profile = next(iter(ros.FINALIZATION_BINDINGS.items()))
        self.projection = {"version": 1, "purpose": ros.FINALIZATION_PURPOSE, "scope": scope, "reportPins": pins,
                           "decisionSha256": self.plan["decisionSha256"], "cleanupDeadline": self.plan["cleanupDeadline"],
                           "launches": [{"launchId": str(uuid.uuid4()), "sessionId": "45724087-335a-4c55-ad0f-cb278afe608a",
                                         "bindingId": binding, "profileId": profile}], "markers": [], "originalIdle": 60000,
                           "sources": sources}
        original = self.w.ops.docker
        def docker(args, **kwargs):
            result = original(args, **kwargs)
            if "--validate-inputs" in args:
                self.assertFalse(self.w.granted)
                self.assertEqual(self.w.lease_gets, 0)
                return json.dumps(self.projection).encode()
            return result
        self.w.ops.docker = docker

    @staticmethod
    def write(path, value):
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
        path.chmod(0o600)

    def prepare(self):
        evidence = Path(tempfile.mkdtemp(dir=self.tmp))
        return ros.prepare_finalization(self.w.ops, self.w.manifest, self.dir, str(self.root), str(self.planpath),
                                        self.expected, evidence)

    def test_actual_preparation_calls_fixed_validator_without_network_or_authority(self):
        prepared = self.prepare()
        self.assertEqual(prepared["scope"]["sessionIds"], ["45724087-335a-4c55-ad0f-cb278afe608a"])
        self.assertEqual(prepared["raw"]["a.json"], self.raws["a"])
        self.assertEqual(self.w.granted, [])
        self.assertEqual(self.w.lease_gets, 0)
        calls = [c[1] for c in self.w.calls if c[0] == "docker" and "--validate-inputs" in c[1]]
        self.assertEqual(len(calls), 1)
        for value in ("none", "--read-only", "--cap-drop", "ALL", ros.UID, IMAGE):
            self.assertIn(value, calls[0])

    def test_broker_validated_scope_is_accepted_by_real_proxy_before_authority(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("finalization_contract_proxy", ROOT/"scripts/relay-session-proxy.py")
        proxy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(proxy)
        scope = ros._validate_cleanup_scope(self.projection, self.plan)
        proxy.configure_finalization(scope)
        self.assertEqual(proxy.STATE.purpose, ros.FINALIZATION_PURPOSE)
        self.assertEqual(proxy.STATE.historical_sessions, frozenset(scope["sessionIds"]))
        for name in ("FINALIZATION_BINDINGS", "FINALIZATION_PROFILES"):
            with self.subTest(drift=name):
                current = sorted(getattr(proxy, name))
                changed = frozenset([*current[1:], str(uuid.uuid4())])
                proxy.STATE = proxy.State()
                before = (proxy.STATE.purpose, proxy.STATE.historical_sessions)
                with mock.patch.object(proxy, name, changed):
                    with self.assertRaises(proxy.Refuse): proxy.configure_finalization(scope)
                self.assertEqual((proxy.STATE.purpose, proxy.STATE.historical_sessions), before)
        self.assertEqual(self.w.calls, [])
        self.assertEqual(self.w.granted, [])
        self.assertEqual(self.w.revokes, 0)

    def test_plan_hash_mismatch_and_report_tampering_stop_before_validator(self):
        self.expected = "f"*64
        with self.assertRaises(ros.Refused): self.prepare()
        self.expected = ros._digest(self.planpath.read_bytes())
        self.write(self.root/"a.json", b'{"tampered":true}')
        with self.assertRaises(ros.Refused): self.prepare()
        self.assertEqual(self.w.calls, [])

    def test_independent_plan_not_matching_runner_manifest_refuses(self):
        self.manifest["cleanupDeadline"] += 1
        self.write(self.root/"manifest.json", self.manifest)
        with self.assertRaises(ros.Refused): self.prepare()
        self.assertEqual(self.w.calls, [])

    def test_unapproved_source_scope_or_session_projection_refuses_and_removes_owned_volume(self):
        for mutate in ("source", "scope", "session", "purpose"):
            with self.subTest(mutate=mutate):
                saved = json.loads(json.dumps(self.projection))
                if mutate == "source": self.projection["sources"]["hubSource"] = "f"*40
                if mutate == "scope": self.projection["scope"]["hostId"] = str(uuid.uuid4())
                if mutate == "session": self.projection["launches"][0]["profileId"] = str(uuid.uuid4())
                if mutate == "purpose": self.projection["purpose"] = "phase-a"
                with self.assertRaises(ros.Refused): self.prepare()
                self.assertEqual(self.w.calls[-1][1][:3], ("volume", "rm", "-f"))
                self.projection = saved
        self.assertEqual(self.w.granted, [])

    def test_single_use_claim_is_retained_and_prevents_replay(self):
        prepared = self.prepare()
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp}):
            ros.claim_finalization(prepared)
            with self.assertRaises(ros.Refused): ros.claim_finalization(prepared)
            claims = list((Path(self.tmp)/"relay-operator/finalization-ledger").glob("*.json"))
            self.assertEqual(len(claims), 1)
            self.assertEqual(json.loads(claims[0].read_text())["planSha256"], self.expected)

    def test_claim_directory_sync_failure_refuses_before_claim_or_authority(self):
        prepared = self.prepare()
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp}), \
                mock.patch.object(ros.os, "fsync", side_effect=OSError("synthetic directory sync failure")):
            with self.assertRaises(OSError): ros.claim_finalization(prepared)
        self.assertEqual(self.w.granted, [])
        self.assertFalse((Path(self.tmp)/"relay-operator/finalization-ledger"/(self.plan["runId"]+".json")).exists())

    def test_claim_symlink_state_root_refuses_before_creating_ancestors(self):
        prepared = self.prepare()
        actual = Path(self.tmp)/"actual-state"
        actual.mkdir(mode=0o700)
        alias = Path(self.tmp)/"state-alias"
        alias.symlink_to(actual, target_is_directory=True)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(alias)}):
            with self.assertRaises(ros.Refused): ros.claim_finalization(prepared)
        self.assertFalse((actual/"relay-operator").exists())

    def test_claim_writable_parent_refuses_before_leaf_creation(self):
        prepared = self.prepare()
        parent = Path(self.tmp)/"relay-operator"
        parent.mkdir(mode=0o770)
        parent.chmod(0o770)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp}):
            with self.assertRaises(ros.Refused): ros.claim_finalization(prepared)
        self.assertFalse((parent/"finalization-ledger").exists())

    def test_claim_writable_state_root_refuses_before_write(self):
        prepared = self.prepare()
        state = Path(self.tmp)/"unsafe-state"
        state.mkdir(mode=0o770)
        state.chmod(0o770)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(state)}):
            with self.assertRaises(ros.Refused): ros.claim_finalization(prepared)
        self.assertFalse((state/"relay-operator").exists())

    def test_reader_requires_complete_pinned_closure_and_same_client(self):
        paths = ("relay_finalization_observer.py", "relay_phase_a_observer.py", "relay-observer-gate.py",
                 "relay-finalization/hub.sql", "relay-finalization/router.cjs", "relay-finalization/host.py",
                 "relay-finalization/client.cjs", "config.json")
        root = Path(self.tmp)/"reader-source"
        pins = {}
        for rel in paths:
            path = root/"scripts"/rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"{}" if rel == "config.json" else b"synthetic pinned source")
            pins["scripts/"+rel] = ros._digest(path.read_bytes())
        manifest = {"finalizationObserver": {"files": pins, "config": "scripts/config.json"},
                    "files": {"scripts/relay-finalization/client.cjs": pins["scripts/relay-finalization/client.cjs"]}}
        self.assertEqual(len(ros.verify_finalization_observer(manifest, str(root))["files"]), len(paths))
        saved = pins.pop("scripts/relay_phase_a_observer.py")
        with self.assertRaises(ros.Refused): ros.verify_finalization_observer(manifest, str(root))
        pins["scripts/relay_phase_a_observer.py"] = saved
        manifest["files"]["scripts/relay-finalization/client.cjs"] = "f"*64
        with self.assertRaises(ros.Refused): ros.verify_finalization_observer(manifest, str(root))

    def test_reader_uses_verified_framing_module_instead_of_cached_phase_a(self):
        from types import ModuleType
        old = ModuleType("relay_phase_a_observer")
        old.MARKER = "unverified cache"
        source = (b"import relay_phase_a_observer as framing\n"
                  b"def create_finalization_host(**kwargs):\n"
                  b" assert framing.MARKER == 'verified'\n"
                  b" return framing.MARKER\n")
        run = ros.Run(self.w.ops, COMMIT+":manifest.json", self.dir, 45, self.tmp)
        run.finalization = {"projection": self.projection, "plan": self.plan,
                            "raw": {"decision.json": json.dumps(self.decision).encode()}}
        run.finalization_reader = {"files": {"scripts/relay_finalization_observer.py": source,
                                             "scripts/relay_phase_a_observer.py": b"MARKER = 'verified'\n"},
                                   "config": {}}
        context = {}
        try:
            with mock.patch.dict(sys.modules, {"relay_phase_a_observer": old}):
                run._finalization_prepare(context)
                self.assertEqual(context["host"], "verified")
        finally:
            if context.get("observer_dir"): shutil_cleanup(context["observer_dir"])

    def test_deadline_elapsed_during_validation_refuses_before_authority(self):
        original = self.w.ops.docker
        def docker(args, **kwargs):
            result = original(args, **kwargs)
            if "--validate-inputs" in args: self.w.clock.t = self.plan["cleanupDeadline"]/1000
            return result
        self.w.ops.docker = docker
        with self.assertRaises(ros.Refused): self.prepare()
        self.assertEqual(self.w.granted, [])

    def test_unapproved_purpose_or_phase_a_observer_manifest_refused(self):
        for change in ({"purpose": "generic-cleanup"}, {"observer": {}}):
            saved = dict(self.w.manifest)
            self.w.manifest.update(change)
            with self.assertRaises(ros.Refused): ros.load_manifest(self.w.ops, COMMIT+":manifest.json")
            self.w.manifest = saved

    def test_version_two_manifest_duplicate_fields_refuse(self):
        raw = json.dumps(self.w.manifest)
        self.w.ops.git_show = lambda *_: (raw[:-1]+',"purpose":"canary-finalization"}').encode()
        with self.assertRaises(ros.Refused): ros.load_manifest(self.w.ops, COMMIT+":manifest.json")

    def test_lifecycle_uses_exact_cleanup_deadline_and_never_phase_a_prepare(self):
        from types import SimpleNamespace
        calls = []
        def prepare_host(run, ctx):
            calls.append("read-only-host")
            self.assertFalse(self.w.granted)
            ctx["host"] = SimpleNamespace(stop=lambda: calls.append("stop"), drain=lambda _: True,
                                         alive=lambda: True, ev=SimpleNamespace(close=lambda: None), fault_reason=None)
        ev = str(Path(self.tmp)/"lifecycle-evidence")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp}), \
                mock.patch.object(ros, "verify_finalization_observer", return_value={"files": {}, "config": {}}), \
                mock.patch.object(ros.Run, "_finalization_prepare", prepare_host), \
                mock.patch.object(ros.Run, "_observer_prepare", side_effect=AssertionError("Phase A preparation called")), \
                mock.patch.object(ros.Run, "_observer_start", return_value=True):
            run = ros.Run(self.w.ops, COMMIT+":manifest.json", self.dir, 45, ev,
                          finalization_inputs=str(self.root), finalization_plan_file=str(self.planpath),
                          finalization_plan_sha256=self.expected, run_id="session-test")
            self.assertEqual(run.main(), 0)
        config = json.loads(self.w.proxy.stdin.data.splitlines()[1])
        self.assertEqual(config["purpose"], ros.FINALIZATION_PURPOSE)
        self.assertEqual(config["runner_deadline"]*1000, self.plan["cleanupDeadline"])
        self.assertEqual(config["cleanup_deadline"]-config["runner_deadline"], 600)
        self.assertEqual(config["cleanup_scope"]["sessionIds"], ["45724087-335a-4c55-ad0f-cb278afe608a"])
        self.assertEqual(self.w.revokes, 2)
        self.assertEqual(self.w.sso, ["kc"])
        self.assertEqual(calls, ["read-only-host", "stop"])
        runner = next(c[1] for c in self.w.calls if c[0] == "popen" and "--run" in c[1])
        self.assertIn("/state/run/finalization", runner)
        self.assertNotIn("/predecessor", " ".join(runner))
        recorded = json.loads((Path(ev)/"run.json").read_text())
        self.assertEqual(recorded["runnerMinutesRequested"], 45)
        self.assertEqual(recorded["runnerDeadlinePolicy"], "finalization-plan")
        self.assertEqual(recorded["runnerDeadline"]*1000, self.plan["cleanupDeadline"])

    def test_deadline_after_grant_records_stage_and_tears_down_without_runner(self):
        from types import SimpleNamespace
        calls = []
        def prepare_host(run, ctx):
            ctx["host"] = SimpleNamespace(stop=lambda: calls.append("stop"), drain=lambda _: True,
                                         alive=lambda: True, ev=SimpleNamespace(close=lambda: None), fault_reason=None)
        original_grant = self.w.ops.grant
        def grant(subject):
            original_grant(subject)
            self.w.clock.t = self.plan["cleanupDeadline"]/1000
        self.w.ops.grant = grant
        ev = str(Path(self.tmp)/"expired-after-grant")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp}), \
                mock.patch.object(ros, "verify_finalization_observer", return_value={"files": {}, "config": {}}), \
                mock.patch.object(ros.Run, "_finalization_prepare", prepare_host), \
                mock.patch.object(ros.Run, "_observer_start", side_effect=AssertionError("Runner observer started")):
            run = ros.Run(self.w.ops, COMMIT+":manifest.json", self.dir, 45, ev,
                          finalization_inputs=str(self.root), finalization_plan_file=str(self.planpath),
                          finalization_plan_sha256=self.expected, run_id="session-test")
            self.assertNotEqual(run.main(), 0)
        self.assertEqual(self.w.granted, ["iss|sub"])
        self.assertEqual(self.w.revokes, 2)
        self.assertEqual(self.w.sso, ["kc"])
        self.assertEqual(calls, ["stop"])
        self.assertFalse(any(c[0] == "popen" and "--run" in c[1] for c in self.w.calls))
        result = json.loads((Path(ev)/"result.json").read_text())
        self.assertNotEqual(result["rc"], 0)
        self.assertTrue(any(n.get("stage") == "finalization-deadline" for n in result["notes"]))


class FinalizationEntryDiagnostics(unittest.TestCase):
    """Actual fixed entry with synthetic imports; no finalizer qualification or authority."""
    def invoke(self, args, *, core=None, missing=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = ROOT/ros.FINALIZATION_ENTRY
            entry = root/"owner-finalization-entry.cjs"
            entry.write_bytes(source.read_bytes())
            if not missing:
                (root/"owner-phase-a-cli.cjs").write_text("exports.readPredecessor=()=> '{}';")
                (root/"owner-finalization.cjs").write_text(core or "exports.reportArtifactNames=()=> ['a','b','bInputs','c'];")
            inputs = root/"inputs"
            inputs.mkdir()
            return subprocess.run(["node", str(entry), *[str(inputs) if x == "INPUTS" else x for x in args]],
                                  capture_output=True, text=True, timeout=5)

    def test_local_code_is_diagnostic_without_inputs(self):
        result = self.invoke(["--invalid"])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "finalization-entry-refused: entry-arguments\n")

    def test_local_input_files_code_is_preserved(self):
        result = self.invoke(["--validate-inputs", "INPUTS"])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "finalization-entry-refused: input-files\n")

    def test_foreign_message_is_not_trusted_even_when_it_matches_a_local_code(self):
        for message in ("input-files", "secret-value-from-untrusted-error"):
            with self.subTest(message=message):
                result = self.invoke(["--validate-inputs", "INPUTS"],
                    core="exports.reportArtifactNames=()=>{throw Error("+json.dumps(message)+")};")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "finalization-entry-refused: external-failure\n")

    def test_missing_fixed_dependency_is_a_redacted_failure(self):
        result = self.invoke(["--validate-inputs", "INPUTS"], missing=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "finalization-entry-refused: external-failure\n")


def shutil_cleanup(path):
    import shutil
    shutil.rmtree(path)


if __name__ == "__main__":
    unittest.main()
