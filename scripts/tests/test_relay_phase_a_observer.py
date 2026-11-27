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
                      on_fault=self.faults.append, heartbeat_s=0.2)
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
        for k in ("post-open", "observation", "permission", "observation", "recover-history", "observation",
                  "reconcile-choice", "observation"):
            self.step(st, "checkpoint", k, c if k != "permission" else {})
        self.step(st, "checkpoint", "pre-launch", {"provider": "qwen", "runId": QWEN["run"], "effort": False})
        with self.assertRaises(O.Refuse):  # retire-branch belongs to the effort run only
            O.admit(st, "checkpoint", "retire-branch", {"provider": "qwen", "runId": QWEN["run"]}, cfg)

    def test_caps(self):
        st = O.ObserverState()
        self.step(st, "deployment")
        with self.assertRaises(O.Refuse):
            O.admit(st, "deployment", None, {}, cfg_obj())


# ------------------------------------------------------------------------------------------ collectors, faked
class FakeRemote:
    """Scripted answers keyed by `what`; records every call."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def _ans(self, what):
        self.calls.append(what)
        a = self.answers[what]
        return a.pop(0) if isinstance(a, list) else a

    def sql(self, name, variables, budget, obs):
        self.calls.append(("sql", name, dict(variables)))
        rows = self.answers["sql:" + name]
        obs.setdefault("sql", {})[name] = rows
        return rows

    def dw(self, args, budget, obs, what, cap=60.0):
        return self._ans(what)

    def router(self, args, budget, obs, what, cap=60.0):
        return self._ans(what)


class FakeHost:
    def __init__(self, remote):
        self.cfg, self.remote, self.state = cfg_obj(), remote, O.ObserverState()
        self.run_start_ms = 1_000
        self.ledger = None
        self.ev = type("E", (), {"write": lambda *a, **k: None})()

    marker_command = H.ObserverHost.marker_command
    native_home = H.ObserverHost.native_home


def budget(ms=30000):
    return O.Budget(ms, threading.Event())


def renewed_rows(codex_state="ready"):
    def row(p, cred, key, state, exp):
        return {"binding_id": p["binding"], "binding_state": state, "binding_revision": 2,
                "current_credential_id": cred if state == "ready" else None,
                "pending_credential_id": cred if state != "ready" else None, "credential_id": cred,
                "router_key_id": key, "credential_generation": 3, "namespace": "default",
                "connection_id": "babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8", "expires_ms": exp}
    return [row(CODEX, u(1), "key_" + "a" * 32, codex_state, 9_000_000), row(QWEN, u(2), "key_" + "b" * 32, "ready",
                                                                              8_000_000)]


def renewed_ctx():
    def prof(p, cred, key, exp):
        return {"id": p["id"], "run": p["run"], "binding": p["binding"], "credential": cred, "key": key,
                "namespace": "default", "connection": "babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8", "generation": 3,
                "revision": 2, "expiresAt": exp, "protocol": "x", "definitionHash": "y"}
    return {"version": 1, "kind": "renewed-credentials", "expiresAt": 8_000_000,
            "profiles": {"codex": prof(CODEX, u(1), "key_" + "a" * 32, 9_000_000),
                         "qwen": prof(QWEN, u(2), "key_" + "b" * 32, 8_000_000)}}


class RenewedCredentials(unittest.TestCase):
    def test_ready_and_installing_match_and_any_drift_fails(self):
        for state in ("ready", "installing"):
            h = FakeHost(FakeRemote({"sql:renewed-credentials.sql": renewed_rows(state)}))
            v = C.c_renewed_credentials(h, {}, renewed_ctx(), budget(), 0, {})
            self.assertEqual(v["expiresAt"], 8_000_000)
            self.assertEqual(h.state.renewed["codex"]["key"], "key_" + "a" * 32)
        ctx = renewed_ctx()
        ctx["profiles"]["qwen"]["revision"] = 9
        with self.assertRaises(O.Failed):
            C.c_renewed_credentials(FakeHost(FakeRemote({"sql:renewed-credentials.sql": renewed_rows()})), {}, ctx,
                                    budget(), 0, {})
        rows = renewed_rows()
        rows[0]["router_key_id"] = CODEX["historicalKey"]
        ctx = renewed_ctx()
        ctx["profiles"]["codex"]["key"] = CODEX["historicalKey"]
        with self.assertRaises(O.Failed):  # a historical key is never a renewed one
            C.c_renewed_credentials(FakeHost(FakeRemote({"sql:renewed-credentials.sql": rows})), {}, ctx, budget(),
                                    0, {})


def bound_run(h, provider="codex", effort=False):
    r = O.RunState(provider, CFG["effortRun"] if effort else CFG["profiles"][provider]["run"], effort, phase="open")
    r.session_id, r.generation = u(10), 1
    h.state.runs.append(r)
    h.state.stage = "runs"
    h.state.sessions[r.session_id] = r
    return r


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

    def run_c8(self, ctx, rows):
        self.h.remote = FakeRemote({"sql:permission.sql": rows})
        return C.c_permission(self.h, {}, ctx, budget(), 0, {})

    def test_relay_read_exact_input_only(self):
        desc = 'Input: {"limit": 5, "after": "12"}'
        self.assertTrue(self.run_c8(perm_ctx(self.r, "relay-message-read", "allow_once", desc, C.READ_TITLE),
                                    perm_row("allow_once", desc, C.READ_TITLE)))
        for bad in ('{"limit": 51}', '{"after": "012"}', '{"channelId": "' + u(1) + '"}', '{"send": true}'):
            with self.assertRaises(O.Failed):
                self.run_c8(perm_ctx(self.r, "relay-message-read", "allow_once", bad, C.READ_TITLE),
                            perm_row("allow_once", bad, C.READ_TITLE))

    def test_marker_decisions_are_bound_to_the_command(self):
        denied = self.h.marker_command("codex", self.r.run_id, "denied")
        effects = self.h.marker_command("codex", self.r.run_id, "effects")
        d1, d2 = json.dumps({"command": denied}), json.dumps({"command": effects, "timeout": 30})
        with self.assertRaises(O.Failed):  # the denied command may only be rejected
            self.run_c8(perm_ctx(self.r, "marker", "allow_once", d1, "t", expected=denied),
                        perm_row("allow_once", d1, "t"))
        self.assertTrue(self.run_c8(perm_ctx(self.r, "marker", "reject_once", d1, "t", expected=denied),
                                    perm_row("reject_once", d1, "t")))
        self.assertTrue(self.run_c8(perm_ctx(self.r, "marker", "allow_once", d2, "t", expected=effects),
                                    perm_row("allow_once", d2, "t")))
        with self.assertRaises(O.Failed):  # each once per run
            self.run_c8(perm_ctx(self.r, "marker", "allow_once", d2, "t", expected=effects),
                        perm_row("allow_once", d2, "t"))
        other = json.dumps({"command": "rm -rf /"})
        with self.assertRaises(O.Failed):
            self.run_c8(perm_ctx(self.r, "marker", "allow_once", other, "t", expected="rm -rf /"),
                        perm_row("allow_once", other, "t"))

    def test_unbound_session_expired_or_ambiguous_option_refused(self):
        desc = '{"limit": 5}'
        ctx = perm_ctx(self.r, "relay-message-read", "allow_once", desc, C.READ_TITLE)
        ctx["sessionId"] = u(99)
        with self.assertRaises(O.Refuse):
            self.run_c8(ctx, perm_row("allow_once", desc, C.READ_TITLE))
        with self.assertRaises(O.Failed):
            self.run_c8(perm_ctx(self.r, "relay-message-read", "allow_once", desc, C.READ_TITLE),
                        perm_row("allow_once", desc, C.READ_TITLE, unexpired=False))
        rows = perm_row("allow_once", desc, C.READ_TITLE)
        rows[0]["context"]["options"].append({"optionId": "o1", "kind": "allow_once", "name": "dup"})
        with self.assertRaises(O.Failed):
            self.run_c8(perm_ctx(self.r, "relay-message-read", "allow_once", desc, C.READ_TITLE), rows)


class Gates(unittest.TestCase):
    def test_c10_and_the_projection_stay_unsupported(self):
        with self.assertRaises(O.Unsupported):
            C.c_reconcile_choice(None, {}, {}, budget(), 0, {})
        with self.assertRaises(O.Unsupported):
            C.c_predecessor(None, {}, {}, budget(), 0, {})


# ------------------------------------------------------------------------------------------ ledger, pre-run, C4
class LedgerTests(unittest.TestCase):
    def test_records_checksummed_torn_and_corrupt_refused(self):
        d = tempfile.mkdtemp()
        L = H.Ledger(d, u(5))
        L.require_empty()
        L.append(op="x", phase="intent")
        self.assertEqual(len(L.records()), 1)
        with self.assertRaises(O.Refuse):
            L.require_empty()
        raw = L.path.read_bytes()
        L.path.write_bytes(raw[:-1])  # torn
        with self.assertRaises(O.Fault):
            L.records()
        L.path.write_bytes(raw.replace(b'"op":"x"', b'"op":"y"'))  # checksum mismatch
        with self.assertRaises(O.Fault):
            L.records()


def prerun_facts():
    h = CFG["holder"]
    return {"receiptSha256": h["receiptSha256"], "stateDirMode": "0o700",
            "receipt": {"operationId": h["preparationId"], "newState": h["stateDir"], "oldState": h["oldStateDir"]},
            "units": {"relay-holder.service": {"active": "inactive", "argv": h["holderArgv"], "dropIns": ""},
                      "relay-holder-control-plane.service": {"active": "inactive", "argv": h["reconciliationArgv"],
                                                             "dropIns": ""}},
            "journal": {"admission": {"paused": True, "operationId": h["pauseOperationId"], "revision": 1}}}


def started(paused=True):
    return {"started": True, "holder": {
        "hello": {"incarnation": u(7), "journalId": u(7), "sessions": 0},
        "status": {"incarnation": u(7), "admissionPaused": paused, "admissionRevision": 1, "activeSessions": 0,
                   "pendingCommands": 0, "nativeStateReady": True, "sealed": False},
        "units": {"relay-holder.service": {"mainPid": 42}}}}


class PreRun(unittest.TestCase):
    def host(self, answers):
        h = FakeHost(FakeRemote(answers))
        h.ledger = H.Ledger(tempfile.mkdtemp(), CFG["holder"]["preparationId"])
        return h

    def test_starts_paused_and_records(self):
        h = self.host({"prerun": prerun_facts(), "mutate-start-holder": started()})
        self.assertEqual(H.prerun(h, budget())["incarnation"], u(7))
        self.assertEqual([r["phase"] for r in h.ledger.records()], ["intent", "receipt"])

    def test_an_unpaused_start_is_contained(self):
        h = self.host({"prerun": prerun_facts(), "mutate-start-holder": started(paused=False),
                       "mutate-stop-holder (containment)": {"stopped": True}})
        with self.assertRaises(O.Fault):
            H.prerun(h, budget())
        self.assertIn("mutate-stop-holder (containment)", h.remote.calls)
        self.assertEqual(h.ledger.records()[-1]["outcome"], "contained-unpaused")

    def test_drifted_units_or_journal_refuse_before_any_write(self):
        f = prerun_facts()
        f["journal"]["admission"]["paused"] = False
        h = self.host({"prerun": f})
        with self.assertRaises(O.Failed):
            H.prerun(h, budget())
        self.assertNotIn("mutate-start-holder", h.remote.calls)
        self.assertEqual(h.ledger.records(), [])


class Handoff(unittest.TestCase):
    def test_outside_the_window_no_mutation(self):
        h = FakeHost(FakeRemote({}))
        h.ledger = H.Ledger(tempfile.mkdtemp(), CFG["holder"]["preparationId"])
        h.lease_ok = lambda: True
        h.state.window = (0, 1)  # long past
        h.state.holder = {"incarnation": u(7), "journal": u(7), "mainPid": 42, "revision": 1}
        rec = {"hello": {"incarnation": u(7), "journalId": u(7), "nativeRecovery": 2, "nativeStateReady": True,
                         "sessions": 0, "profileDigests": []},
               "status": {"admissionPaused": True, "admissionRevision": 1, "activeSessions": 0, "pendingCommands": 0,
                          "nativeStateReady": True, "sealed": False},
               "binarySha256": CFG["holder"]["qualified"]["binarySha256"], "profilesSha256": CFG["holder"]["profilesSha256"],
               "units": {"relay-holder.service": {"mainPid": 42}, "relay-holder-control-plane.service": {"active": "inactive"}},
               "observedMs": 1}
        h.remote = FakeRemote({"holder": rec})
        h.authorize_mutation = lambda b: H.ObserverHost.authorize_mutation(h, b)
        ctx = {"qualifiedHolder": CFG["holder"]["qualified"], "oldHolderId": CFG["holder"]["oldHolderId"],
               "oldJournalId": CFG["holder"]["oldJournalId"], "startedAt": 1}
        with self.assertRaises(O.Failed):
            H.c_preserving_holder(h, {}, ctx, budget(), 0, {})
        self.assertEqual(h.ledger.records(), [])
        self.assertNotIn("mutate-resume", h.remote.calls)


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
