"""Tests for the automated phase A observer (plans/2026-11-27-relay-phase-a-observer-plan.md): the gate sidecar over a
real Unix socket, the host protocol and its faults, the transition table, the confirmation policy, the ledger, the
pre-run containment and the C4 handoff against faked remote commands, and (when node and the pinned driver are present)
the collector outputs and renderings checked by the driver's own functions.
"""
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import relay_phase_a_collectors as C  # noqa: E402
import relay_phase_a_host as H  # noqa: E402
import relay_phase_a_observer as O  # noqa: E402

CFG = json.loads((SCRIPTS / "relay-phase-a" / "observer-config.json").read_text())
CODEX, QWEN = CFG["profiles"]["codex"], CFG["profiles"]["qwen"]
DRIVER = Path(os.environ.get("PHASE_A_DRIVER", SCRIPTS.parent / "docs/runbooks/inference-canary-v0113/owner-phase-a.js"))


def gate_module():
    spec = importlib.util.spec_from_file_location("gate", SCRIPTS / "relay-observer-gate.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def u(n=0):
    return f"{n:08x}-0000-4000-8000-000000000000"


# ------------------------------------------------------------------------------------------ the gate
@unittest.skipUnless(hasattr(socket, "AF_UNIX") and os.name == "posix", "Unix sockets")
class Gate(unittest.TestCase):
    def setUp(self):
        self.G = gate_module()
        self.dir = tempfile.mkdtemp()
        self.sock = os.path.join(self.dir, "observer.sock")
        self.out_r, self.out_w = os.pipe()
        self.in_r, self.in_w = os.pipe()
        self.gate = self.G.Gate(self.sock, os.fdopen(self.out_w, "wb"), os.fdopen(self.in_r, "rb"))
        threading.Thread(target=self.gate.serve, daemon=True).start()
        self.frames = os.fdopen(self.out_r, "rb")
        self.host_in = os.fdopen(self.in_w, "wb")
        self.assertEqual(json.loads(self.frames.readline()), {"ready": True})

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def client(self, line: bytes, read=True):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(self.sock)
        s.sendall(line)
        if not read:
            return s
        buf = b""
        while not buf.endswith(b"\n"):
            c = s.recv(65536)
            if not c:
                break
            buf += c
        s.close()
        return json.loads(buf) if buf else None

    def req(self, deadline_s=10, **over):
        r = {"v": 1, "call": "deployment", "deadline": int(time.time() * 1000) + deadline_s * 1000, "context": {}}
        r.update(over)
        return (json.dumps(r) + "\n").encode()

    def reply_to_next(self, obj):
        f = json.loads(self.frames.readline())
        self.host_in.write((json.dumps({"seq": f["seq"], **obj}) + "\n").encode())
        self.host_in.flush()
        return f

    def test_request_is_framed_and_the_reply_relayed(self):
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", self.client(self.req())))
        t.start()
        f = self.reply_to_next({"v": 1, "ok": True, "value": {"x": 1}})
        t.join(5)
        self.assertEqual(f["seq"], 1)
        self.assertIsInstance(f["gateNow"], int)
        self.assertEqual(f["request"]["call"], "deployment")
        self.assertEqual(out["r"]["value"], {"x": 1})

    def test_malformed_requests_are_refused_without_a_frame(self):
        for bad in (b'{"v":1,"v":1,"call":"deployment","deadline":1,"context":{}}\n', b'{"v":1,"x":NaN}\n',
                    b'not json\n', b"x" * (70 * 1024) + b"\n",
                    (json.dumps({"v": 2, "call": "deployment", "deadline": 1, "context": {}}) + "\n").encode()):
            self.assertEqual(self.client(bad)["code"], "refused")
        self.assertEqual(self.gate.seq, 0)

    def test_disconnect_cancels_terminally_and_a_late_reply_is_discarded(self):
        s = self.client(self.req(), read=False)
        f = json.loads(self.frames.readline())
        s.close()  # the client goes away while the host works
        cancel = json.loads(self.frames.readline())
        self.assertEqual(cancel, {"seq": f["seq"], "cancel": True})
        self.host_in.write((json.dumps({"seq": f["seq"], "v": 1, "ok": True, "value": 1}) + "\n").encode())
        self.host_in.flush()
        self.assertEqual(self.client(self.req())["code"], "cancelled")  # terminal for the rest of the run

    def test_deadline_cancels(self):
        r = self.client(self.req(deadline_s=1))
        f = json.loads(self.frames.readline())
        self.assertEqual(r["code"], "cancelled")
        self.assertEqual(json.loads(self.frames.readline()), {"seq": f["seq"], "cancel": True})

    def test_host_eof_is_observer_down(self):
        self.host_in.close()
        time.sleep(0.3)
        self.assertEqual(self.client(self.req())["code"], "observer-down")

    def test_deep_nesting_and_overflowing_numbers_are_refused_and_the_gate_survives(self):
        deep = ('{"v":1,"call":"deployment","deadline":1,"context":' + "[" * 5000 + "]" * 5000 + "}\n").encode()
        self.assertEqual(self.client(deep)["code"], "refused")
        self.assertEqual(self.client(b'{"v":1,"call":"deployment","deadline":1,"context":{"x":1e999}}\n')["code"],
                         "refused")
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", self.client(self.req())))
        t.start()
        self.reply_to_next({"v": 1, "ok": True, "value": 1})
        t.join(5)
        self.assertEqual(out["r"]["value"], 1)

    def test_back_to_back_requests_are_never_busy(self):
        for i in range(5):
            out = {}
            t = threading.Thread(target=lambda: out.setdefault("r", self.client(self.req())))
            t.start()
            self.reply_to_next({"v": 1, "ok": True, "value": i})
            t.join(5)
            self.assertEqual(out["r"], {"v": 1, "ok": True, "value": i, "seq": i + 1} if "seq" in out["r"]
                             else {"v": 1, "ok": True, "value": i})

    def test_clock_probe_round_trip(self):
        self.host_in.write(b'{"clockProbe": "p1"}\n')
        self.host_in.flush()
        f = json.loads(self.frames.readline())
        self.assertEqual(f["clockProbe"], "p1")
        self.assertIsInstance(f["gateNow"], int)

    def test_second_concurrent_connection_is_busy(self):
        s = self.client(self.req(), read=False)
        self.frames.readline()
        self.assertEqual(self.client(self.req())["code"], "busy")
        s.close()


# ------------------------------------------------------------------------------------------ the host protocol
class FakeGate:
    def __init__(self):
        r, w = os.pipe()
        self.stdout = os.fdopen(r, "rb")
        self._w = os.fdopen(w, "wb")
        self.stdin = io.BytesIO()
        self.exited = None

    def frame(self, obj):
        self._w.write((json.dumps(obj) + "\n").encode())
        self._w.flush()

    def poll(self):
        return self.exited

    def replies(self):
        return [json.loads(x) for x in self.stdin.getvalue().splitlines()]


def cfg_obj():
    return C.Config(json.loads(json.dumps(CFG)), {})


class HostProtocol(unittest.TestCase):
    def make(self, collectors=None):
        self.ev_path = os.path.join(tempfile.mkdtemp(), "observer.jsonl")
        self.gate = FakeGate()
        self.faults = []
        host = O.Host(gate=self.gate, collectors=collectors or {}, cfg=cfg_obj(), evidence=O.Evidence(self.ev_path),
                      on_fault=lambda r, e=False, lease=False: self.faults.append(r), heartbeat_s=0.2)
        self.gate.frame({"ready": True})
        host.start(ready_timeout=3)
        return host

    def deployment_frame(self, seq):
        now = int(time.time() * 1000)
        return {"seq": seq, "gateNow": now, "request": {"v": 1, "call": "deployment", "deadline": now + 30000,
                                                         "context": {}}}

    def wait(self, cond, t=5):
        end = time.time() + t
        while time.time() < end and not cond():
            time.sleep(0.02)
        return cond()

    def test_ok_reply_after_the_evidence_line(self):
        host = self.make({("deployment", None): lambda h, r, c, b, g, o: {"hubSource": "s", "ready": True}})
        self.gate.frame(self.deployment_frame(1))
        self.assertTrue(self.wait(lambda: self.gate.replies()))
        self.assertEqual(self.gate.replies()[0], {"seq": 1, "v": 1, "ok": True, "value": {"hubSource": "s",
                                                                                         "ready": True}})
        lines = [json.loads(x) for x in Path(self.ev_path).read_bytes().splitlines()]
        self.assertEqual(lines[-1]["outcome"], "ok")
        self.assertEqual(host.state.stage, "prelude")
        host.stop()

    def test_failed_and_refused_travel_as_fixed_words_only(self):
        def boom(*a):
            raise O.Failed("secret detail /home/c4/x")
        host = self.make({("deployment", None): boom})
        self.gate.frame(self.deployment_frame(1))
        self.assertTrue(self.wait(lambda: self.gate.replies()))
        self.assertEqual(self.gate.replies()[0], {"seq": 1, "v": 1, "ok": False, "code": "failed"})
        self.assertNotIn(b"secret detail", self.gate.stdin.getvalue())
        self.assertEqual(self.faults, [])
        host.stop()

    def test_a_collector_bug_is_a_fault_never_a_pass(self):
        host = self.make({("deployment", None): lambda *a: 1 / 0})
        self.gate.frame(self.deployment_frame(1))
        self.assertTrue(self.wait(lambda: self.faults))
        self.assertIn("collector-error", self.faults[0])
        self.assertEqual(self.gate.replies()[-1]["code"], "observer-down")
        host.stop()

    def test_seq_gap_and_gate_eof_and_gate_exit_are_faults(self):
        host = self.make()
        self.gate.frame(self.deployment_frame(2))
        self.assertTrue(self.wait(lambda: self.faults))
        self.assertEqual(self.faults[0], "gate-seq-gap")
        host.stop()
        host = self.make()
        self.gate._w.close()
        self.assertTrue(self.wait(lambda: self.faults))
        self.assertEqual(self.faults[0], "gate-eof")
        host.stop()
        host = self.make()
        self.gate.exited = 1
        self.assertTrue(self.wait(lambda: self.faults))
        self.assertEqual(self.faults[0], "gate-exited")
        host.stop()

    def test_out_of_order_kind_is_refused(self):
        host = self.make()
        now = int(time.time() * 1000)
        self.gate.frame({"seq": 1, "gateNow": now, "request": {
            "v": 1, "call": "checkpoint", "kind": "renewed-credentials", "name": "x", "instructionsSha256": "0" * 64,
            "deadline": now + 30000, "context": {"version": 1, "kind": "renewed-credentials"}}})
        self.assertTrue(self.wait(lambda: self.gate.replies()))
        self.assertEqual(self.gate.replies()[0]["code"], "refused")
        host.stop()


# ------------------------------------------------------------------------------------------ the transition table
class Transitions(unittest.TestCase):
    def step(self, st, call, kind=None, ctx=None, cfg=None):
        cfg = cfg or cfg_obj()
        ctx = ctx or {}
        O.admit(st, call, kind, ctx, cfg)
        if kind == "pre-launch":
            st.pending = O.RunState(ctx["provider"], ctx["runId"], bool(ctx.get("effort")))
        O.advance(st, call, kind, ctx, None, cfg)

    def test_the_full_flow_and_its_refusals(self):
        st, cfg = O.ObserverState(), cfg_obj()
        for call, kind in (("deployment", None), ("checkpoint", "renewal-deployment"),
                           ("checkpoint", "renewed-credentials"), ("predecessor", None),
                           ("checkpoint", "stopped-replacement"), ("checkpoint", "preserving-holder")):
            self.step(st, call, kind)
        with self.assertRaises(O.Refuse):  # qwen first is out of the fixed order
            O.admit(st, "checkpoint", "pre-launch", {"provider": "qwen", "runId": QWEN["run"], "effort": False}, cfg)
        self.step(st, "checkpoint", "pre-launch", {"provider": "codex", "runId": CODEX["run"], "effort": False})
        with self.assertRaises(O.Refuse):  # an observation before post-open
            O.admit(st, "checkpoint", "observation", {"provider": "codex", "runId": CODEX["run"]}, cfg)
        c = {"provider": "codex", "runId": CODEX["run"]}
        for k in ("post-open", "observation", "permission", "observation", "recover-history"):
            self.step(st, "checkpoint", k, c if k != "permission" else {})
        with self.assertRaises(O.Refuse):  # after recovery only the recovered zero-attempt observation is admitted
            O.admit(st, "checkpoint", "observation", {**c, "phase": "approved", "zeroAttempts": False}, cfg)
        self.step(st, "checkpoint", "observation", {**c, "phase": "recovered", "zeroAttempts": True})
        for k in ("reconcile-choice", "observation"):
            self.step(st, "checkpoint", k, c)
        self.step(st, "checkpoint", "pre-launch", {"provider": "qwen", "runId": QWEN["run"], "effort": False})
        with self.assertRaises(O.Refuse):  # retire-branch belongs to the effort run only
            O.admit(st, "checkpoint", "retire-branch", {"provider": "qwen", "runId": QWEN["run"]}, cfg)

    def test_caps(self):
        st = O.ObserverState()
        self.step(st, "deployment")
        with self.assertRaises(O.Refuse):
            O.admit(st, "deployment", None, {}, cfg_obj())


# ------------------------------------------------------------------------------------------ collectors, faked
POD = {"name": "llm-router-x", "uid": u(30), "restarts": 0}


class FakeRemote:
    """Scripted answers keyed by `what` (or 'sql:<file>'); a list answers one item per call. Records every call."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def _ans(self, key):
        self.calls.append(key)
        a = self.answers[key]
        if isinstance(a, list):
            return a.pop(0) if len(a) > 1 else a[0]
        if isinstance(a, BaseException):
            raise a
        return a

    def sql(self, name, variables, budget, obs):
        self.calls.append(("sql", name, dict(variables)))
        rows = self.answers["sql:" + name]  # a row list is ONE answer
        obs.setdefault("sql", {})[name] = {"rows": len(rows)}
        return rows

    def dw(self, args, budget, obs, what, cap=60.0, deadline_arg=False):
        return self._ans(what)

    def router(self, args, budget, obs, what, cap=60.0, pod=None):
        return self._ans(what)

    def router_pod(self, budget, obs):
        self.calls.append("router-pod")
        return dict(self.answers.get("router-pod", POD))


class FakeHost:
    def __init__(self, remote):
        self.cfg, self.remote, self.state = cfg_obj(), remote, O.ObserverState()
        self.run_start_ms = 1_000
        self.recv_ms = int(time.time() * 1000)
        self.recv_mono = time.monotonic()
        self.ledger = None
        self.lease_ok = lambda: True
        self.ev = type("E", (), {"write": lambda *a, **k: None})()
        self._cutoff = int(time.time() * 1000) + 3_600_000

    marker_command = H.ObserverHost.marker_command
    native_home = H.ObserverHost.native_home
    mutation_cap = H.ObserverHost.mutation_cap

    def runner_cutoff_ms(self):
        return self._cutoff

    def extend_inflight(self, seconds):
        pass


def budget(ms=30000):
    return O.Budget(ms, threading.Event())


def renewed_rows(codex_state="ready"):
    def row(p, cred, key, state, exp):
        return {"binding_id": p["binding"], "binding_state": state, "binding_revision": 2,
                "current_credential_id": cred if state == "ready" else None,
                "pending_credential_id": cred if state != "ready" else None, "credential_id": cred,
                "router_key_id": key, "credential_generation": 3, "namespace": "default",
                "connection_id": CFG["connectionId"], "expires_ms": exp}
    return [row(CODEX, u(1), "key_" + "a" * 32, codex_state, 9_000_000), row(QWEN, u(2), "key_" + "b" * 32, "ready",
                                                                              8_000_000)]


def renewed_ctx():
    def prof(p, cred, key, exp):
        return {"id": p["id"], "run": p["run"], "binding": p["binding"], "credential": cred, "key": key,
                "namespace": "default", "connection": CFG["connectionId"], "generation": 3, "revision": 2,
                "expiresAt": exp, "protocol": p["protocol"], "definitionHash": p["definitionHash"]}
    return {"version": 1, "kind": "renewed-credentials", "expiresAt": 8_000_000,
            "profiles": {"codex": prof(CODEX, u(1), "key_" + "a" * 32, 9_000_000),
                         "qwen": prof(QWEN, u(2), "key_" + "b" * 32, 8_000_000)}}


class RenewedCredentials(unittest.TestCase):
    def c2(self, rows, ctx):
        return C.c_renewed_credentials(FakeHost(FakeRemote({"sql:renewed-credentials.sql": rows})), {}, ctx,
                                       budget(), 0, {})

    def test_ready_and_installing_match(self):
        for state in ("ready", "installing"):
            self.assertEqual(self.c2(renewed_rows(state), renewed_ctx())["expiresAt"], 8_000_000)

    def test_any_drift_extension_or_historical_key_fails(self):
        for mutate in (lambda c: c["profiles"]["qwen"].update(revision=9),
                       lambda c: c["profiles"]["codex"].update(extra="smuggled"),  # an unvalidated extension
                       lambda c: c["profiles"]["codex"].update(definitionHash="0" * 64)):
            ctx = renewed_ctx()
            mutate(ctx)
            with self.assertRaises(O.Failed):
                self.c2(renewed_rows(), ctx)
        rows, ctx = renewed_rows(), renewed_ctx()
        rows[0]["router_key_id"] = ctx["profiles"]["codex"]["key"] = CODEX["historicalKey"]
        with self.assertRaises(O.Failed):
            self.c2(rows, ctx)


def bound_run(h, provider="codex", effort=False):
    r = O.RunState(provider, CFG["effortRun"] if effort else CFG["profiles"][provider]["run"], effort, phase="open")
    r.session_id, r.generation = u(10), 1
    h.state.runs.append(r)
    h.state.stage = "runs"
    h.state.sessions[r.session_id] = r
    return r


def sess(state="open", generation=1, rebinding=False):
    return [{"id": u(10), "state": state, "generation": generation, "rebinding": rebinding, "control_epoch": 1,
             "holder_id": u(7), "native_session_id": "n"}]


def perm_ctx(r, purpose, kind, desc, title, opt="o1", expected=None):
    return {"version": 1, "kind": "permission", "purpose": purpose, "decisionKind": kind, "optionId": opt,
            "sessionId": r.session_id, "generation": r.generation, "expectedCommand": expected,
            "permission": {"id": u(20), "expiresAt": "x", "context": {"title": title, "description": desc}}}


def perm_row(kind, desc, title, opt="o1", state="pending", unexpired=True):
    return [{"id": u(20), "session_id": u(10), "state": state, "unexpired": unexpired,
             "context": {"title": title, "description": desc, "options": [{"optionId": opt, "kind": kind,
                                                                           "name": "n"}]}}]


class PermissionPolicy(unittest.TestCase):
    def setUp(self):
        self.h = FakeHost(None)
        self.r = bound_run(self.h)

    def run_c8(self, ctx, rows, session=None):
        self.h.remote = FakeRemote({"sql:permission.sql": rows, "sql:session-state.sql": session or sess()})
        obs = {}
        v = C.c_permission(self.h, {}, ctx, budget(), 0, obs)
        self.last_obs = obs
        return v

    def test_relay_read_exact_input_only_and_nothing_raw_is_logged(self):
        desc = '{"limit": 5, "after": "12"}'
        self.assertTrue(self.run_c8(perm_ctx(self.r, "relay-message-read", "allow_once", desc, C.READ_TITLE),
                                    perm_row("allow_once", desc, C.READ_TITLE)))
        self.assertNotIn('"after"', json.dumps(self.last_obs))  # only hashes of the untrusted text
        for bad in ('{"limit": 51}', '{"after": "012"}', '{"channelId": "' + u(1) + '"}', '{"send": true}',
                    'Input: {"limit": 5}'):  # the WHOLE description must be the JSON input
            with self.assertRaises(O.Failed):
                self.run_c8(perm_ctx(self.r, "relay-message-read", "allow_once", bad, C.READ_TITLE),
                            perm_row("allow_once", bad, C.READ_TITLE))

    def test_marker_decisions_are_bound_to_the_command_and_the_bash_tool(self):
        denied = self.h.marker_command("codex", self.r.run_id, "denied")
        effects = self.h.marker_command("codex", self.r.run_id, "effects")
        d1, d2 = json.dumps({"command": denied}), json.dumps({"command": effects, "timeout": 30.5})
        with self.assertRaises(O.Failed):  # the denied command may only be rejected
            self.run_c8(perm_ctx(self.r, "marker", "allow_once", d1, C.MARKER_TITLE, expected=denied),
                        perm_row("allow_once", d1, C.MARKER_TITLE))
        with self.assertRaises(O.Failed):  # not the exact bash tool
            self.run_c8(perm_ctx(self.r, "marker", "reject_once", d1, "Allow sh?", expected=denied),
                        perm_row("reject_once", d1, "Allow sh?"))
        self.assertTrue(self.run_c8(perm_ctx(self.r, "marker", "reject_once", d1, C.MARKER_TITLE, expected=denied),
                                    perm_row("reject_once", d1, C.MARKER_TITLE)))
        self.assertTrue(self.run_c8(perm_ctx(self.r, "marker", "allow_once", d2, C.MARKER_TITLE, expected=effects),
                                    perm_row("allow_once", d2, C.MARKER_TITLE)))
        with self.assertRaises(O.Failed):  # each once per run
            self.run_c8(perm_ctx(self.r, "marker", "allow_once", d2, C.MARKER_TITLE, expected=effects),
                        perm_row("allow_once", d2, C.MARKER_TITLE))
        self.assertEqual([c["step"] for c in self.r.confirmed], ["denied", "effects"])

    def test_authoritative_generation_rebinding_expiry_and_ambiguity(self):
        desc = '{"limit": 5}'
        for session in (sess(generation=999), sess(rebinding=True), sess(state="closed")):
            with self.assertRaises(O.Failed):
                self.run_c8(perm_ctx(self.r, "relay-message-read", "allow_once", desc, C.READ_TITLE),
                            perm_row("allow_once", desc, C.READ_TITLE), session)
        ctx = perm_ctx(self.r, "relay-message-read", "allow_once", desc, C.READ_TITLE)
        ctx["sessionId"] = u(99)
        with self.assertRaises(O.Refuse):
            self.run_c8(ctx, perm_row("allow_once", desc, C.READ_TITLE))
        with self.assertRaises(O.Failed):
            self.run_c8(perm_ctx(self.r, "relay-message-read", "allow_once", desc, C.READ_TITLE),
                        perm_row("allow_once", desc, C.READ_TITLE, unexpired=False))
        rows = perm_row("allow_once", desc, C.READ_TITLE)
        rows[0]["context"]["options"].append({"optionId": "o2", "kind": "allow_once", "name": "dup"})
        with self.assertRaises(O.Failed):
            self.run_c8(perm_ctx(self.r, "relay-message-read", "allow_once", desc, C.READ_TITLE), rows)


def holder_rec(paused=False, pid=42):
    return {"hello": {"incarnation": u(7), "journalId": CFG["holder"]["newJournalId"], "nativeRecovery": 2,
                      "nativeStateReady": True, "sessions": 0},
            "status": {"admissionPaused": paused, "admissionRevision": 1, "activeSessions": 0, "pendingCommands": 0,
                       "nativeStateReady": True, "sealed": False},
            "binarySha256": CFG["holder"]["qualified"]["binarySha256"],
            "profilesSha256": CFG["holder"]["profilesSha256"],
            "units": {"relay-holder.service": {"mainPid": pid, "active": "active"},
                      "relay-holder-control-plane.service": {"active": "active"}},
            "observedMs": 1}


class Recovery(unittest.TestCase):
    def setUp(self):
        self.h = FakeHost(None)
        self.h.state.holder = {"incarnation": u(7), "journal": CFG["holder"]["newJournalId"], "mainPid": 42}
        self.r = bound_run(self.h)
        self.r.native_id = u(11)
        self.h.state.renewed = {'codex': {'key': 'key_' + 'a' * 32}}
        self.h.state.router_pod = {'uid': POD['uid'], 'restarts': 0, 'capture': 'c'*64, 'store': 'd'*64}
        self.r.confirmed = [{"permission": u(21), "option": "rej", "kind": "reject_once", "step": "denied"},
                            {"permission": u(22), "option": "acc", "kind": "allow_once", "step": "effects"}]
        self.r.approved_marker_sha = "e" * 64
        shell = lambda k, err: {"sha256": O.sha256_bytes(self.h.marker_command("codex", self.r.run_id, k).encode()),
                                "results": [err]}
        self.answers = {
            "holder": holder_rec(), "sql:session-state.sql": sess(state="closed"),
            "sql:session-commands.sql": [{"table": "control", "kind": "prompt", "state": "completed",
                                          "receipt_status": "completed", "stop_reason": "end_turn"},
                                         {"table": "agent", "kind": "permission-decision", "state": "unknown",
                                          "receipt_status": "accepted", "stop_reason": None}],
            "sql:permission-decisions.sql": [{"id": u(21), "state": "resolved", "option_id": "rej"},
                                             {"id": u(22), "state": "resolved", "option_id": "acc"}],
            "native-checkpoint": {"state": "observed", "version": 1, "unsettled": [], "duplicateResults": [],
                           "sessionId": self.r.session_id, "nativeSessionId": u(11), "profileId": CODEX['id'],
                           "generation": 1, "holderId": u(7), "journalId": CFG['holder']['newJournalId'],
                           "observedMs": C.now_ms(), "retainedReceiptSha256": 'a'*64, "currentSha256": 'b'*64,
                           "sourceSha256": 'b'*64, "journalSha256": 'c'*64, "profileDigest": 'f'*64,
                           "native": {'conversation':'c','branch':'b','nativeSession':u(11),'parent':'p'},
                           "toolNames": ['bash'],
                           "shellCommands": [shell("denied", True), shell("effects", False)]},
            "marker:approved": {"state": "observed", "phaseChecksPass": True, "observedUtc": "x",
                                "files": {"effects": {"sha256": "e" * 64}}}}
        self.answers['branch-identity'] = {'captureLevel':'metadata','captureSha256':'c'*64,'storeSha256':'d'*64}
        self.answers['branch-record'] = {'version':1,'namespace':'default','principal':'key:key_'+'a'*32,
            'conversation':'c','branch':'b','kvNamespace':'inference-session-v1','storeSha256':'d'*64,
            'key':O.sha256_bytes(json.dumps(['default','key:key_'+'a'*32,'c','b'],separators=(',',':')).encode()),
            'querySha256':O.sha256_bytes(b'SELECT value FROM kv WHERE ns=? AND key=?'),
            'rawRecordSha256':'e'*64,
            'requestedAt':C.now_ms(),'observedAt':C.now_ms(),
            'record':{'epoch':'e','operation':'p','state':'complete','updatedAt':C.now_ms()}}
        self.ctx = {"sessionId": self.r.session_id, "generation": 1, "holderId": u(7),
                    "journalId": CFG["holder"]["newJournalId"], "provider": "codex", "runId": self.r.run_id}

    def c9(self, **over):
        a = dict(self.answers)
        a.update(over)
        self.h.remote = FakeRemote(a)
        return C.c_recover_history(self.h, {}, self.ctx, budget(), 0, {})

    def test_settled_and_reconciled(self):
        self.assertTrue(self.c9())

    def test_missing_evidence_fails(self):
        bad = dict(self.answers["native-checkpoint"])
        bad["shellCommands"] = [dict(s) for s in bad["shellCommands"]]
        bad["shellCommands"][0]["results"] = [False]  # the denied command executed
        with self.assertRaises(O.Failed):
            self.c9(**{'native-checkpoint':bad})
        with self.assertRaises(O.Failed):  # resolved with another option than confirmed
            self.c9(**{"sql:permission-decisions.sql": [{"id": u(21), "state": "resolved", "option_id": "acc"},
                                                         {"id": u(22), "state": "resolved", "option_id": "acc"}]})
        with self.assertRaises(O.Failed):  # an unconfirmed permission exists
            self.c9(**{"sql:permission-decisions.sql": self.answers["sql:permission-decisions.sql"]
                       + [{"id": u(23), "state": "resolved", "option_id": "x"}]})
        with self.assertRaises(O.Failed):  # a control command in an unknown state
            self.c9(**{"sql:session-commands.sql": [{"table": "control", "kind": "prompt", "state": "unknown",
                                                     "receipt_status": "completed", "stop_reason": None}]})
        unknown = dict(self.answers["native-checkpoint"])
        unknown["shellCommands"] = [dict(s, results=[]) for s in unknown["shellCommands"]]
        with self.assertRaises(O.Unsupported):  # the native outcome cannot be established
            self.c9(**{'native-checkpoint':unknown})


class Delivery(unittest.TestCase):
    def test_effort_reuses_an_installed_credential_on_this_host(self):
        h = FakeHost(None)
        h.state.holder = {"incarnation": u(7), "journal": CFG["holder"]["newJournalId"]}
        h.state.renewed = {"codex": {"binding": CODEX["binding"], "credential": u(1), "generation": 3, "key": "k"}}
        r = O.RunState("codex", CFG["effortRun"], True, phase="c5")
        r.baseline_marker = {"files": {"denied": {"state": "absent"}, "effects": {"state": "absent"}}}
        h.state.runs.append(r)
        h.state.stage = "runs"
        launch = {"id": u(40), "session_id": u(41), "actor_id": CFG["jitUserId"], "created_ms": 5_000,
                  "profile_id": CODEX["id"], "profile_revision": 1, "cwd": CODEX["cwd"], "holder_id": u(7),
                  "holder_journal_id": CFG["holder"]["newJournalId"], "native_id": "n"}
        install = {"holder_id": u(7), "generation": 3, "rendered": True, "render_failure": None,
                   "install_state": "completed", "receipt_status": "completed", "consumer_state": "active",
                   "consumer_lease_ok": True}
        h.remote = FakeRemote({"sql:session-launch.sql": [launch],
                               "sql:session-state.sql": [dict(sess()[0], id=u(41))],
                               "sql:delivery.sql": [], "sql:delivery-reuse.sql": [install],
                               "credfile": {"state": "present", "regular": True, "uid": 1000, "mode": "0o600",
                                            "links": 1},
                               "marker:baseline": {"state": "observed", "phaseChecksPass": True, "observedUtc": "x",
                                                   "files": r.baseline_marker["files"]}})
        ctx = {"provider": "codex", "runId": CFG["effortRun"], "phase": "baseline", "sessionId": u(41),
               "generation": 1, "notBefore": 1}
        self.assertEqual(C.c_post_open(h, {}, ctx, budget(), 0, {})["deliveryConfirmed"], True)
        used = [c[1] for c in h.remote.calls if isinstance(c, tuple)]
        self.assertIn("delivery-reuse.sql", used)
        self.assertEqual([c[2]["host_id"] for c in h.remote.calls if isinstance(c, tuple)
                          and c[1] == "delivery.sql"], [CFG["hostId"]])


class RoundTwo(unittest.TestCase):
    def test_an_option_id_shared_by_two_kinds_is_refused(self):
        h = FakeHost(None)
        r = bound_run(h)
        denied = h.marker_command("codex", r.run_id, "denied")
        d = json.dumps({"command": denied})
        rows = perm_row("reject_once", d, C.MARKER_TITLE)
        rows[0]["context"]["options"].append({"optionId": "o1", "kind": "allow_once", "name": "same id"})
        h.remote = FakeRemote({"sql:permission.sql": rows, "sql:session-state.sql": sess()})
        with self.assertRaises(O.Failed):
            C.c_permission(h, {}, perm_ctx(r, "marker", "reject_once", d, C.MARKER_TITLE, expected=denied), budget(),
                           0, {})

    def test_a_repeated_marker_call_is_never_merged_away(self):
        rec = Recovery()
        rec.setUp()
        t = dict(rec.answers["native-checkpoint"])
        t["shellCommands"] = t["shellCommands"] + [dict(t["shellCommands"][1])]  # effects twice
        with self.assertRaises(O.Failed):
            rec.c9(**{"native-checkpoint": t})

    def test_an_evidence_failure_after_an_earlier_fault_is_still_reported(self):
        seen = []
        host = O.Host(gate=FakeGate(), collectors={}, cfg=cfg_obj(),
                      evidence=O.Evidence(os.path.join(tempfile.mkdtemp(), "ev.jsonl")),
                      on_fault=lambda r, e=False, lease=False: seen.append((r, e)))
        host.fault("first")
        host.fault("disk full", evidence=True)
        self.assertEqual(seen, [("first", False), ("disk full", True)])

    def test_runner_floor_uses_monotonic_elapsed_time_only(self):
        h = FakeHost(None)
        h.recv_mono = time.monotonic() - 2.0
        self.assertGreaterEqual(C.runner_floor(h, 1_000_000), 1_000_000 + 1990)

    def test_javascript_integers_and_lone_surrogates(self):
        self.assertEqual(O.canonical(10 ** 21), "1e+21")
        self.assertEqual(O.canonical(9007199254740993), "9007199254740992")
        self.assertEqual(O.canonical("a" + chr(0xD800)), '"a' + chr(92) + 'ud800"')

    @unittest.skipUnless(hasattr(socket, "AF_UNIX") and os.name == "posix", "posix")
    def test_gate_admission_is_released_only_by_its_owner(self):
        G = gate_module()
        g = G.Gate("/nonexistent", io.BytesIO(), io.BytesIO())
        first, second = object(), object()
        g.busy = second  # the successor already holds admission
        g.release(first)  # the previous handler finishing must not clear it
        self.assertIs(g.busy, second)
        g.release(second)
        self.assertIsNone(g.busy)


def dw_namespace():
    """dw.py's functions (not its main), with module globals that a test may replace."""
    src = (SCRIPTS / "relay-phase-a" / "remote" / "dw.py").read_text()
    ns = {"__name__": "dw"}
    exec(compile(src.split("\ndef main():")[0], "dw", "exec"), ns)
    return ns


class RemoteContainment(unittest.TestCase):
    """The ACTUAL remote start mode, with slow reads: containment always happens inside the stop reserve."""

    def setUp(self):
        self.ns = dw_namespace()
        self.calls = []
        ns = self.ns
        ns["STOP_RESERVE_S"], ns["MIN_CONTAINED_START_S"] = 1.0, 0.5
        ns["journal"] = lambda d: {"admission": {"paused": True}}
        ns["systemd"] = lambda unit, deadline=None: {"active": "inactive", "argv": [], "mainPid": 0}
        ns["run_bounded"] = lambda argv, seconds: self.calls.append((argv[2], seconds)) or 0
        ns["hello"] = lambda state_dir, wait_s=0.0, deadline=None: None

    def test_a_slow_status_read_is_contained_before_the_reserve(self):
        def slow_holder(state_dir, deadline=None):  # a status read that takes all the time it is given
            time.sleep(max(0.0, deadline - time.monotonic()))
            return {"status": None}
        self.ns["holder"] = slow_holder
        t0 = time.monotonic()
        out = self.ns["mutate_start_holder"]("/x", 3)
        self.assertLessEqual(time.monotonic() - t0, 3 - 1.0 + 0.6)  # stop issued inside the reserve
        self.assertEqual([c[0] for c in self.calls], ["start", "stop"])
        self.assertEqual((out["paused"], out["contained"]), (False, True))

    @unittest.skipUnless(hasattr(socket, "AF_UNIX") and os.name == "posix", "Unix sockets")
    def test_the_real_hello_stops_at_its_deadline_against_a_trickling_holder(self):
        ns = dw_namespace()  # the ACTUAL hello(), no stubs
        ns["UID"] = os.getuid()
        d = tempfile.mkdtemp()
        path = os.path.join(d, "control.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        os.chmod(path, 0o600)
        srv.listen(1)
        stop = threading.Event()

        def trickle():  # answers one byte every 0.3 s and never a newline
            conn, _ = srv.accept()
            conn.recv(1024)
            while not stop.is_set():
                try:
                    conn.sendall(b"x")
                except OSError:
                    return
                time.sleep(0.3)
        threading.Thread(target=trickle, daemon=True).start()
        t0 = time.monotonic()
        self.assertIsNone(ns["hello"](d, deadline=time.monotonic() + 1.5))
        self.assertLess(time.monotonic() - t0, 2.2)  # bounded by the deadline, not by per-receive timeouts
        stop.set()
        srv.close()

    def test_an_exception_after_the_start_is_contained(self):
        def boom(state_dir, deadline=None):
            raise OSError("socket")
        self.ns["holder"] = boom
        out = self.ns["mutate_start_holder"]("/x", 3)
        self.assertEqual([c[0] for c in self.calls], ["start", "stop"])
        self.assertTrue(out["contained"])

    @unittest.skipUnless(os.name == "posix", "signals")
    def test_sigterm_during_the_readback_still_contains(self):
        import signal as sg

        def term(state_dir, deadline=None):
            os.kill(os.getpid(), sg.SIGTERM)  # the outer timeout's SIGTERM, mid-readback
            time.sleep(0.2)
            return {"status": {"admissionPaused": True}}
        self.ns["holder"] = term
        out = self.ns["mutate_start_holder"]("/x", 3)
        self.assertEqual([c[0] for c in self.calls], ["start", "stop"])
        self.assertTrue(out["contained"])
        self.assertEqual(sg.getsignal(sg.SIGTERM), sg.SIG_DFL)  # the original handler is restored

    def test_the_enable_start_mode_fits_its_own_bound(self):
        runs = []
        self.ns["run_bounded"] = lambda argv, seconds: runs.append((argv[2], seconds)) or 0
        t0 = time.monotonic()
        out = self.ns["mutate_enable_start"](26)
        self.assertEqual([r[0] for r in runs], ["enable", "start"])
        self.assertLessEqual(sum(r[1] for r in runs), 26 - 4)  # both steps fit, leaving the readback its share
        self.assertLess(time.monotonic() - t0, 5)
        self.assertTrue(out["enabled"] and out["started"])
        self.assertEqual(self.ns["mutate_enable_start"](10)["why"], "budget-too-small")

    def test_the_resume_mode_fits_its_own_bound(self):
        seen = []
        self.ns["maintenance_status"] = lambda d, deadline=None: (seen.append(deadline - time.monotonic()), {
            "admissionPaused": True, "admissionRevision": 1, "activeSessions": 0, "pendingCommands": 0,
            "sealed": False})[1]
        self.ns["journal"] = lambda d: {}
        runs = []
        self.ns["run_bounded"] = lambda argv, seconds: runs.append((argv, seconds)) or 0
        out = self.ns["mutate_resume"]("/x", 1, u(9), 32)
        self.assertEqual(len(runs), 1)
        self.assertIn("resume", runs[0][0])  # docker run ... holder-maintenance.js <dir> resume <rev> <op>
        self.assertLessEqual(runs[0][1], 20)
        self.assertLessEqual(seen[0], 10.5)  # the pre-status got at most 10 s
        self.assertLessEqual(seen[1], 32)  # the post-status ends inside the bound
        self.assertTrue(out["resumed"])


class Gates(unittest.TestCase):
    def test_c10_c11_and_projection_without_source_tuple_stay_unsupported(self):
        for fn in (C.c_reconcile_choice, C.c_retire_branch, C.c_predecessor):
            with self.assertRaises(O.Unsupported):
                fn(None, {}, {}, budget(), 0, {})


class Canonical(unittest.TestCase):
    def test_javascript_number_and_key_order(self):
        cases = {1.0: "1", -0.0: "0", 1e-07: "1e-7", 1e-06: "0.000001", 1e16: "10000000000000000", 1e21: "1e+21",
                 1.5e-07: "1.5e-7", 0.1 + 0.2: "0.30000000000000004", 123.456: "123.456", 0.05: "0.05"}
        for v, js in cases.items():
            self.assertEqual(O.js_number(v), js, v)
        self.assertEqual(O.canonical({"b": 1, "a": [1.0, None, True]}), '{"a":[1,null,true],"b":1}')


# ------------------------------------------------------------------------------------------ ledger, pre-run, C4
class LedgerTests(unittest.TestCase):
    def test_exclusive_reservation_checksums_torn_and_corrupt(self):
        d = tempfile.mkdtemp()
        L = H.Ledger(d, u(5))
        L.reserve(op="x", phase="intent")
        self.assertEqual(len(L.records()), 1)
        with self.assertRaises(O.Refuse):  # a second run cannot reserve the same preparation
            H.Ledger(d, u(5)).reserve(op="y", phase="intent")
        L.close()
        raw = L.path.read_bytes()
        L.path.write_bytes(raw[:-1])  # torn
        with self.assertRaises(O.Fault):
            H.Ledger(d, u(5)).reserve(op="y", phase="intent")
        L.path.write_bytes(raw.replace(b'"op":"x"', b'"op":"y"'))  # checksum mismatch
        with self.assertRaises(O.Fault):
            L.records()
        if os.name != "nt":
            self.assertEqual(os.stat(d).st_mode & 0o077, 0)


def prerun_facts():
    h = CFG["holder"]
    return {"receiptSha256": h["receiptSha256"], "stateDirMode": "0o700",
            "receipt": {"operationId": h["preparationId"], "newState": h["stateDir"], "oldState": h["oldStateDir"],
                        "state": "prepared-stopped-new-state",
                        "preservedJournal": {"incarnation": h["oldHolderId"], "journalId": h["oldJournalId"],
                                             "sealId": h["sealId"], "sha256": h["oldJournalSha256"], "records": 40,
                                             "unknownCommands": 0}},
            "oldJournalSha256": h["oldJournalSha256"],
            "oldJournalLast": {"kind": "holder-maintenance-seal", "sealId": h["sealId"], "sealed": True,
                               "expectedIncarnation": h["oldHolderId"]},
            "artifacts": dict(h["artifacts"]),
            "units": {"relay-holder.service": {"active": "inactive", "argv": h["holderArgv"], "dropIns": ""},
                      "relay-holder-control-plane.service": {"active": "inactive", "argv": h["reconciliationArgv"],
                                                             "dropIns": ""}},
            "binarySha256": h["qualified"]["binarySha256"],
            "journal": {"lastStartedIncarnation": h["newJournalId"], "sha256": h["newJournalSha256"],
                        "kinds": ["holder-started", "holder-admission"],
                        "admission": {"paused": True, "operationId": h["pauseOperationId"], "revision": 1}}}


def started(paused=True, contained=False):
    jid = CFG["holder"]["newJournalId"]
    return {"started": True, "paused": paused, "contained": contained, "unitAfterStop": "inactive", "holder": {
        "hello": {"incarnation": jid, "journalId": jid, "sessions": 0},
        "status": {"incarnation": jid, "admissionPaused": paused, "admissionRevision": 1, "activeSessions": 0,
                   "pendingCommands": 0, "nativeStateReady": True, "sealed": False},
        "units": {"relay-holder.service": {"mainPid": 42}}}}


IDENT = {"captureLevel": "baseline", "captureSha256": "c" * 64, "storeSha256": "s" * 64, "podNow": 1}


class PreRun(unittest.TestCase):
    def host(self, answers):
        h = FakeHost(FakeRemote({"identity": IDENT, **answers}))
        h.ledger = H.Ledger(tempfile.mkdtemp(), CFG["holder"]["preparationId"])
        return h

    def test_starts_paused_pins_the_router_and_records(self):
        h = self.host({"prerun": prerun_facts(), "mutate-start-holder": started()})
        self.assertEqual(H.prerun(h, budget(200_000))["journal"], CFG["holder"]["newJournalId"])
        self.assertEqual([r["phase"] for r in h.ledger.records()], ["intent", "receipt"])
        self.assertEqual(h.state.router_pod["uid"], POD["uid"])

    def test_an_unpaused_start_is_contained_on_the_host(self):
        h = self.host({"prerun": prerun_facts(), "mutate-start-holder": started(paused=False, contained=True)})
        with self.assertRaises(O.Fault):
            H.prerun(h, budget(200_000))
        self.assertEqual(h.ledger.records()[-1]["outcome"], "contained-unpaused")

    def test_a_lost_start_response_is_classified_and_contained(self):
        h = self.host({"prerun": prerun_facts(), "mutate-start-holder": O.Deadline("ssh timed out"),
                       "classify-holder": holder_rec(paused=False), "mutate-stop-holder (containment)":
                       {"stopped": True, "contained": True}})
        with self.assertRaises(O.Fault):
            H.prerun(h, budget(200_000))
        self.assertIn("mutate-stop-holder (containment)", h.remote.calls)
        self.assertEqual(h.ledger.records()[-1]["outcome"], "contained-unpaused")

    def test_preservation_drift_refuses_before_any_write(self):
        for mutate in (lambda f: f["journal"]["admission"].update(paused=False),
                       lambda f: f.update(oldJournalSha256="0" * 64),
                       lambda f: f["oldJournalLast"].update(sealed=False),
                       lambda f: f["receipt"]["preservedJournal"].update(unknownCommands=1),
                       lambda f: f["artifacts"].update({"service.json": "0" * 64}),
                       lambda f: f["journal"].update(sha256="0" * 64),  # anything appended to the new root
                       lambda f: f["journal"].update(kinds=["holder-started", "holder-admission", "seal"]),
                       lambda f: f.update(binarySha256="0" * 64)):  # the executable systemd would start
            f = prerun_facts()
            mutate(f)
            h = self.host({"prerun": f})
            with self.assertRaises(O.Failed):
                H.prerun(h, budget(200_000))
            self.assertNotIn("mutate-start-holder", h.remote.calls)
            self.assertFalse(h.ledger.path.exists())

    def test_a_lost_lease_refuses_before_any_write(self):
        h = self.host({"prerun": prerun_facts()})
        h.lease_ok = lambda: False
        with self.assertRaises(O.Fault):
            H.prerun(h, budget(200_000))
        self.assertFalse(h.ledger.path.exists())


class Handoff(unittest.TestCase):
    def host(self, answers, window=None):
        h = FakeHost(FakeRemote(answers))
        h.ledger = H.Ledger(tempfile.mkdtemp(), CFG["holder"]["preparationId"])
        h.ledger.reserve(op="pre-run-start", phase="intent")
        now = int(time.time() * 1000)
        h.state.window = (now - 1000, now + 600_000)
        mono = time.monotonic()
        h.state.window_mono = window or (mono - 1, mono + 600)
        jid = CFG["holder"]["newJournalId"]
        h.state.holder = {"incarnation": u(7), "journal": jid, "mainPid": 42, "revision": 1}
        h.state.preservation = {"sealId": CFG["holder"]["sealId"]}
        return h

    def ctx(self):
        return {"qualifiedHolder": CFG["holder"]["qualified"], "oldHolderId": CFG["holder"]["oldHolderId"],
                "oldJournalId": CFG["holder"]["oldJournalId"], "startedAt": 1}

    def test_outside_the_window_no_mutation(self):
        h = self.host({"holder": holder_rec(paused=True), "classify-handoff": holder_rec(paused=True)}, window=(0, 1))
        with self.assertRaises(O.Failed):
            H.c_preserving_holder(h, {}, self.ctx(), budget(90_000), 0, {})
        self.assertNotIn("mutate-resume", h.remote.calls)
        self.assertEqual(h.ledger.records()[-1]["outcome"], "failed")

    def test_success_records_every_stage(self):
        rec = holder_rec(paused=True)
        h = self.host({"holder": [rec, holder_rec(paused=False)],
                       "mutate-resume": {"resumed": True, "status": {"admissionPaused": False},
                                         "journal": {"admission": {"paused": False, "operationId": None}}},
                       "sql:holder-health.sql": []})
        res = h.remote.answers["mutate-resume"]
        orig = h.remote.dw

        def dw(args, budget, obs, what, cap=60.0, deadline_arg=False):
            if what == "mutate-resume":
                res["journal"]["admission"]["operationId"] = args[3]  # the journal names THIS operation
            return orig(args, budget, obs, what, cap, deadline_arg)
        h.remote.dw = dw
        ru = {"active": "active", "sub": "running", "enabled": "enabled", "argv": CFG["holder"]["reconciliationArgv"],
              "restarts": 0}
        h.remote.answers["mutate-enable-start"] = {"enabled": True, "started": True, "units": {
            "relay-holder-control-plane.service": ru, "relay-holder.service": {"enabled": "enabled"}}}
        h.remote.answers["sql:holder-health.sql"] = [{"journal_id": CFG["holder"]["newJournalId"],
                                                      "admission_paused": False, "native_state_ready": True,
                                                      "observed_ms": int(time.time() * 1000) + 60_000}]
        v = H.c_preserving_holder(h, {}, self.ctx(), budget(90_000), int(time.time() * 1000), {})
        self.assertTrue(v["preservedOldStateConfirmed"])
        Handoff.last_value = v  # checked by the pinned driver's own preservingHandoff() below
        ops = [(r["op"], r["phase"], r.get("outcome")) for r in h.ledger.records()]
        self.assertEqual(ops[1:], [("resume", "intent", None), ("resume", "receipt", "ok"),
                                   ("enable-start", "intent", None), ("enable-start", "receipt", "ok"),
                                   ("handoff", "receipt", "ok")])

    def test_an_interrupted_resume_gets_a_terminal_classified_receipt(self):
        h = self.host({"holder": holder_rec(paused=True), "mutate-resume": O.Deadline("ssh timed out"),
                       "classify-handoff": holder_rec(paused=False)})
        with self.assertRaises(O.Deadline):
            H.c_preserving_holder(h, {}, self.ctx(), budget(90_000), 0, {})
        last = h.ledger.records()[-1]
        self.assertEqual((last["op"], last["outcome"], last["interruptedStage"]), ("handoff", "failed", "resume"))
        self.assertEqual(last["state"]["admissionPaused"], False)


# ------------------------------------------------------------------------------------------ host faults
class HostFaults(unittest.TestCase):
    def test_short_evidence_writes_are_completed_and_failures_are_evidence_faults(self):
        class Short:
            def __init__(self):
                self.data = b""

            def write(self, view):
                chunk = bytes(view[:3])
                self.data += chunk
                return len(chunk)

            tmp = tempfile.TemporaryFile()

            def fileno(self):
                return self.tmp.fileno()  # a real, fsync-able descriptor

        ev = O.Evidence(os.devnull)
        ev.f = Short()
        ev.write(a=1)
        self.assertTrue(ev.f.data.endswith(b"\n"))

        class Broken(Short):
            def write(self, view):
                return 0
        ev.f = Broken()
        with self.assertRaises(O.EvidenceFailure):
            ev.write(a=1)


# ------------------------------------------------------------------------------------------ broker integration
class BrokerIntegration(unittest.TestCase):
    def setUp(self):
        import relay_operator_session as ros
        self.ros = ros
        self.repo = Path(tempfile.mkdtemp())
        (self.repo / "scripts").mkdir()
        self.files = {}
        for name in ros.OBSERVER_MODULES + ("relay-phase-a/observer-config.json",):
            p = self.repo / "scripts" / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"content of " + name.encode())
            self.files["scripts/" + name] = __import__("hashlib").sha256(p.read_bytes()).hexdigest()
        self.pred = self.repo / "report.json"
        self.pred.write_bytes(b'{"report": 1}')
        self.manifest = {"version": 1, "image": "x@sha256:" + "0" * 64, "entrypoint": ["node"],
                         "files": {"a.cjs": "0" * 64},
                         "observer": {"files": self.files, "config": "scripts/relay-phase-a/observer-config.json",
                                      "predecessorSha256": __import__("hashlib").sha256(b'{"report": 1}').hexdigest()}}

    def load(self, m):
        ops = type("Ops", (), {"git_is_reviewed": lambda self, c: True,
                               "git_show": lambda self, c, p: json.dumps(m).encode()})()
        return self.ros.load_manifest(ops, "a" * 40 + ":scripts/relay-phase-a/manifest.json")

    def test_the_observer_block_is_strict(self):
        self.load(self.manifest)
        for bad in ({"files": {}, "config": "x", "predecessorSha256": "0" * 64},
                    {**self.manifest["observer"], "extra": 1},
                    {**self.manifest["observer"], "config": "scripts/not-listed.json"},
                    {**self.manifest["observer"], "files": {"../x.py": "0" * 64}}):
            with self.assertRaises(self.ros.Refused):
                self.load({**self.manifest, "observer": bad})

    def test_verify_observer_pins_every_byte_and_the_predecessor(self):
        got = self.ros.verify_observer(self.manifest, str(self.repo), str(self.pred))
        self.assertEqual(got["predecessor"], b'{"report": 1}')
        (self.repo / "scripts" / "relay_phase_a_host.py").write_bytes(b"tampered")
        with self.assertRaises(self.ros.Refused):
            self.ros.verify_observer(self.manifest, str(self.repo), str(self.pred))
        with self.assertRaises(self.ros.Refused):
            self.ros.verify_observer(self.manifest, str(self.repo), None)

    def test_the_observer_is_imported_from_the_verified_copy_only(self):
        staged = Path(tempfile.mkdtemp()) / "scripts"
        staged.mkdir()
        for m in ("relay_phase_a_observer.py", "relay_phase_a_collectors.py", "relay_phase_a_host.py"):
            shutil.copy(SCRIPTS / m, staged / m)
        O2, C2, H2 = self.ros._import_observer(str(staged))
        for mod in (O2, C2, H2):
            self.assertTrue(str(Path(mod.__file__).resolve()).startswith(str(staged.resolve())))
        for m in ("relay_phase_a_observer", "relay_phase_a_collectors", "relay_phase_a_host"):
            sys.modules.pop(m, None)  # the rest of the suite uses the working-tree modules imported at the top
        sys.modules.update({"relay_phase_a_observer": O, "relay_phase_a_collectors": C, "relay_phase_a_host": H})

    def test_precedence_puts_an_observer_fault_above_the_runner_failure(self):
        self.assertEqual(self.ros.PRECEDENCE, [3, 4, 5, 6, 7, 9, 8, 2])


@unittest.skipUnless(shutil.which("node"), "node")
class ClientNodeTests(unittest.TestCase):
    def test_the_runner_side_client(self):  # script-tests runs only Python: drive the node suite from here
        p = subprocess.run(["node", "--test", str(SCRIPTS / "relay-phase-a" / "observer-client.test.cjs")],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout[-3000:] + p.stderr[-2000:])


# ------------------------------------------------------------------------------------------ the driver itself
@unittest.skipUnless(shutil.which("node") and DRIVER.exists(), "node and the pinned driver")
class DriverAgreement(unittest.TestCase):
    def node(self, js: str):
        p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60,
                           env={**os.environ, "DRIVER": str(DRIVER)})
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_marker_command_rendering_matches(self):
        want = self.node("const d=require(process.env.DRIVER); const P=d.PROFILES, E='" + CFG["effortRun"] + "';"
                         "const out={}; for (const p of ['codex','qwen']) for (const k of ['denied','effects'])"
                         " out[p+k]=d.markerCommand(p,P[p].run,k); console.log(JSON.stringify(out));")
        h = FakeHost(None)
        for p in ("codex", "qwen"):
            for k in ("denied", "effects"):
                self.assertEqual(h.marker_command(p, CFG["profiles"][p]["run"], k), want[p + k])

    def test_canonical_matches_on_a_mixed_document(self):
        doc = {"b": [1, {"z": "é\n\"x\"", "a": None}], "a": True, "c": {"y": -3, "x": " "}}
        js = self.node("const d=require(process.env.DRIVER); console.log(JSON.stringify(d.canonical("
                       + json.dumps(doc) + ")));")
        self.assertEqual(O.canonical(doc), js)

    def test_the_c4_handoff_passes_the_drivers_preserving_handoff(self):
        v = getattr(Handoff, "last_value", None)
        if v is None:
            t = Handoff("test_success_records_every_stage")
            t.test_success_records_every_stage()
            v = Handoff.last_value
        js = ("const d=require(process.env.DRIVER); const v=" + json.dumps(v) + ";"
              "const Q={release:'v0.13.4',source:'64dc50f47a4867a6d9a7afc2e7d5ae24068c59af',"
              "binarySha256:'d406b1f363f590b386ab3eaea4a8cb350c5ac9c49a3dcb26acc34034dfcf6926'};"
              "const prior={holderId:'" + CFG["holder"]["oldHolderId"] + "',journalId:d.HOLDER};"
              "const out=d.preservingHandoff(v,prior,Q,Date.parse(v.observedUtc)-1000,Date.parse(v.observedUtc)+1000);"
              "console.log(JSON.stringify(out.holderId===v.holderId));")
        self.assertTrue(self.node(js))

    def test_marker_report_from_dw_passes_the_driver(self):
        dw = (SCRIPTS / "relay-phase-a" / "remote" / "dw.py").read_text()
        ns = {}
        exec(compile(dw.split("\ndef main():")[0], "dw", "exec"), ns)
        root = tempfile.mkdtemp()
        ns["WORKSPACES"] = root
        ws = Path(root) / CODEX["id"]
        ws.mkdir(mode=0o700)
        ns["UID"] = os.getuid()
        rep = ns["marker"]("codex", CODEX["run"], "baseline", 0)  # reads the temp root, renders the fixed one
        self.assertEqual(rep["state"], "observed")
        self.assertTrue(rep["phaseChecksPass"])
        js = ("const d=require(process.env.DRIVER); const r=" + json.dumps(rep) + ";"
              "d.marker(r,'codex',d.PROFILES.codex.run,'baseline',Date.parse(r.observedUtc)); console.log('true');")
        self.assertTrue(self.node(js))


if __name__ == "__main__":
    unittest.main()
