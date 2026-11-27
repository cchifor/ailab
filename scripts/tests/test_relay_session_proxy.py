"""Tests for scripts/relay-session-proxy.py (plans/2026-11-27-relay-operator-session-plan.md).

The planner and the Unix-socket server are exercised against a fake upstream that records every call, so a refused
request is proven to never reach Relay. Response fixtures follow the deployed v0.14.1 shapes (snake_case rows).
Linux only (AF_UNIX); run in docker python:3.13.
"""
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("proxy", ROOT / "scripts" / "relay-session-proxy.py")
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

CODEX_B, QWEN_B = "a0f57684-747f-4c3e-8d90-e684b7262590", "d2871eec-8f5a-4afe-a917-014f0ae8e14d"
CODEX_P = "1d9a7d6e-4b26-49fd-9053-7b0a22e1baae"
CWD = P.WORKSPACES + CODEX_P
ACTOR = "11111111-1111-4111-8111-111111111111"
START = datetime(2026, 11, 27, 22, 0, 0, tzinfo=timezone.utc)


def u():
    return str(uuid.uuid4())


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


class Fake:
    """Canned upstream: route function -> (status, body). Records (method, path, query, body)."""

    def __init__(self):
        self.calls, self.handlers, self.delay = [], {}, 0.0

    def call(self, method, path, query, body, budget):
        self.calls.append((method, path, list(query), body))
        if self.delay:
            time.sleep(self.delay)
        for (m, prefix), fn in self.handlers.items():
            if m == method and path.startswith(prefix):
                st, obj = fn(path, body)
                return st, "application/json", json.dumps(obj).encode(), {}
        return 200, "application/json", b"{}", {}


def fresh_state(runner_in=600, cleanup_in=900):
    P.STATE = P.State()
    P.STATE.actor_id, P.STATE.run_start = ACTOR, START
    now = time.monotonic()
    P.STATE.runner_deadline, P.STATE.cleanup_deadline = now + runner_in, now + cleanup_in


def launch_body(op=None, **over):
    b = {"operationId": op or u(), "profileId": CODEX_P, "revision": 1, "cwd": CWD}
    b.update(over)
    return b


def launch_row(op, launch_id, sid=None, created=START + timedelta(seconds=5), actor=ACTOR, **over):
    r = {"id": launch_id, "actor_id": actor, "operation_id": op, "profile_id": CODEX_P, "profile_revision": 1,
         "cwd": CWD, "session_id": sid, "created_at": iso(created), "resume_from": None, "resume_generation": None}
    r.update(over)
    return r


def learn_session(op=None, sid=None):
    """Drive a launch through the planner + learning; returns (sid, launch id)."""
    op, lid, sid = op or u(), u(), sid or u()
    plan = P.plan_request("POST", "/api/agent-launches", [], launch_body(op))
    plan.learn(launch_row(op, lid))
    P.plan_request("GET", "/api/agent-launches", [], None).learn([launch_row(op, lid, sid)])
    return sid, lid


class Planner(unittest.TestCase):
    def setUp(self):
        fresh_state()

    def refused(self, method, path, query=(), body=None):
        with self.assertRaises(P.Refuse):
            P.plan_request(method, path, list(query), body)

    def test_reads_and_fixed_scope(self):
        for path in ("/api/router-credentials", "/api/agent-profiles", "/api/agent-launches"):
            self.assertEqual(P.plan_request("GET", path, [], None).path, path)
        self.refused("GET", "/api/auth/me")
        self.refused("GET", "/api/router-credentials", [("limit", "5")])
        self.refused("GET", "/api/router-credentials/connections")
        self.refused("POST", f"/api/router-credentials/{u()}/rotate", body={"operationId": u(), "revision": 1})
        self.refused("POST", f"/api/router-credentials/{CODEX_B}/revoke", body={"operationId": u(), "revision": 1})

    def test_rotate_once_per_binding_and_strict_body(self):
        plan = P.plan_request("POST", f"/api/router-credentials/{CODEX_B}/rotate", [],
                              {"operationId": u(), "revision": 3})
        self.assertEqual(plan.body["revision"], 3)
        self.refused("POST", f"/api/router-credentials/{CODEX_B}/rotate", body={"operationId": u(), "revision": 4})
        self.refused("POST", f"/api/router-credentials/{QWEN_B}/rotate", body={"operationId": u(), "revision": 1,
                                                                                 "extra": 1})
        self.refused("POST", f"/api/router-credentials/{QWEN_B}/rotate", body={"operationId": u(), "revision": True})
        P.plan_request("POST", f"/api/router-credentials/{QWEN_B}/rotate", [], {"operationId": u(), "revision": 1})

    def test_launch_scope(self):
        self.refused("POST", "/api/agent-launches", body=launch_body(profileId=u()))
        self.refused("POST", "/api/agent-launches", body=launch_body(revision=2))
        self.refused("POST", "/api/agent-launches", body=launch_body(cwd=CWD + "/sub"))
        self.refused("POST", "/api/agent-launches", body={**launch_body(), "purpose": "x"})
        self.refused("POST", "/api/agent-launches", body=launch_body(operationId="not-a-uuid"))

    def test_operation_ids_fresh_and_startup_inventory_refused(self):
        op = u()
        P.plan_request("POST", "/api/agent-launches", [], launch_body(op))
        self.refused("POST", "/api/agent-launches", body=launch_body(op))
        old = u()
        P.STATE.startup_ops.add(old)
        self.refused("POST", "/api/agent-launches", body=launch_body(old))

    def test_learning_provenance(self):
        sid, lid = learn_session()
        self.assertIn(sid, P.STATE.sessions)
        # same actor, same operation id, same profile/cwd, but created before this run's login: never learned
        op2, lid2 = u(), u()
        P.plan_request("POST", "/api/agent-launches", [], launch_body(op2))
        P.learn_launch(launch_row(op2, lid2, u(), created=START - timedelta(seconds=30)))
        self.assertNotIn(lid2, P.STATE.launches)
        # another actor, or an operation this run never dispatched
        P.learn_launch(launch_row(op2, lid2, u(), actor=u()))
        P.learn_launch(launch_row(u(), u(), u()))
        self.assertNotIn(lid2, P.STATE.launches)
        # a mismatched profile/cwd for the dispatched intent
        P.learn_launch(launch_row(op2, lid2, u(), cwd="/tmp"))
        self.assertNotIn(lid2, P.STATE.launches)
        # lost POST response: read-only reconciliation through the inventory learns it
        P.plan_request("GET", "/api/agent-launches", [], None).learn([launch_row(op2, lid2, "22222222-2222-4222-8222-222222222222")])
        self.assertIn("22222222-2222-4222-8222-222222222222", P.STATE.sessions)

    def test_unlearned_and_predecessor(self):
        self.refused("GET", f"/api/agent-sessions/{u()}")
        pred = P.PREDECESSOR_SESSION
        for sub in ("", "/control", "/router", "/permissions"):
            P.plan_request("GET", f"/api/agent-sessions/{pred}{sub}", [], None)
        P.plan_request("GET", f"/api/agent-sessions/{pred}/operations/{u()}", [], None)
        P.plan_request("GET", f"/api/agent-permissions/{P.PREDECESSOR_PERMISSION}", [], None)
        self.refused("POST", f"/api/agent-sessions/{pred}/control", body={"generation": 1})
        self.refused("POST", f"/api/agent-sessions/{pred}/resume", body={"operationId": u(), "generation": 1})
        self.refused("POST", f"/api/agent-permissions/{P.PREDECESSOR_PERMISSION}/decision",
                     body={"operationId": u(), "generation": 1, "outcome": {"outcome": "cancelled"}})
        self.refused("GET", f"/api/agent-sessions/{pred}/native-recovery")

    def test_control_commands_and_cross_session(self):
        sid, _ = learn_session()
        other, _ = learn_session()
        plan = P.plan_request("POST", f"/api/agent-sessions/{sid}/control", [], {"generation": 2})
        lease = u()
        plan.learn({"id": lease, "generation": 2, "expires_at": iso(START)})
        P.plan_request("POST", f"/api/agent-sessions/{sid}/control", [], {"generation": 2, "leaseId": lease})
        self.refused("POST", f"/api/agent-sessions/{sid}/control", body={"generation": 3, "leaseId": lease})
        self.refused("POST", f"/api/agent-sessions/{other}/control", body={"generation": 2, "leaseId": lease})
        self.refused("DELETE", f"/api/agent-sessions/{other}/control/{lease}")
        P.plan_request("DELETE", f"/api/agent-sessions/{sid}/control/{lease}", [], None)
        cmd = {"operationId": u(), "leaseId": lease, "generation": 2, "kind": "prompt", "controlEpoch": 0,
               "text": "hello"}
        P.plan_request("POST", f"/api/agent-sessions/{sid}/commands", [], cmd)
        self.refused("POST", f"/api/agent-sessions/{other}/commands", body={**cmd, "operationId": u()})
        self.refused("POST", f"/api/agent-sessions/{sid}/commands", body={**cmd, "operationId": u(),
                                                                          "questionReply": {}})
        for bad in ({"kind": "set-mode", "modeId": "x", "controlEpoch": 0, "settingsRevision": 1},
                    {"kind": "native-command", "name": "compact", "input": "", "controlEpoch": 0,
                     "settingsRevision": 1},
                    {"kind": "set-config", "configId": "thought_level", "value": "xhigh", "controlEpoch": 0,
                     "settingsRevision": 1},
                    {"kind": "set-config", "configId": "model", "value": "low", "controlEpoch": 0,
                     "settingsRevision": 1}):
            self.refused("POST", f"/api/agent-sessions/{sid}/commands",
                         body={"operationId": u(), "leaseId": lease, "generation": 2, **bad})
        P.plan_request("POST", f"/api/agent-sessions/{sid}/commands", [],
                       {"operationId": u(), "leaseId": lease, "generation": 2, "kind": "set-config",
                        "configId": "thought_level", "value": "high", "controlEpoch": 1, "settingsRevision": 2})
        # operations: only this run's, under their own session
        P.plan_request("GET", f"/api/agent-sessions/{sid}/operations/{cmd['operationId']}", [], None)
        self.refused("GET", f"/api/agent-sessions/{other}/operations/{cmd['operationId']}")
        self.refused("GET", f"/api/agent-sessions/{sid}/operations/{u()}")

    def test_permissions_union_once(self):
        sid, _ = learn_session()
        pid = u()
        P.plan_request("GET", f"/api/agent-sessions/{sid}/permissions", [], None).learn([{"id": pid, "state": "pending"}])
        self.refused("POST", f"/api/agent-permissions/{pid}/decision",
                     body={"operationId": u(), "generation": 1, "optionId": "allow"})
        self.refused("POST", f"/api/agent-permissions/{pid}/decision",
                     body={"operationId": u(), "generation": 1, "outcome": {"outcome": "selected"}})
        plan = P.plan_request("POST", f"/api/agent-permissions/{pid}/decision", [],
                              {"operationId": u(), "generation": 1,
                               "outcome": {"outcome": "selected", "optionId": "allow_once"}})
        self.assertEqual(plan.body["outcome"], {"outcome": "selected", "optionId": "allow_once"})
        self.refused("POST", f"/api/agent-permissions/{pid}/decision",
                     body={"operationId": u(), "generation": 1, "outcome": {"outcome": "cancelled"}})
        self.refused("POST", f"/api/agent-permissions/{u()}/decision",
                     body={"operationId": u(), "generation": 1, "outcome": {"outcome": "cancelled"}})

    def test_resume_provenance_and_acknowledgement(self):
        sid, lid = learn_session()
        self.refused("POST", f"/api/agent-sessions/{sid}/resume",
                     body={"operationId": u(), "generation": 1, "acknowledgeUnknownToolEffects": True})
        op = u()
        plan = P.plan_request("POST", f"/api/agent-sessions/{sid}/resume", [],
                              {"operationId": op, "generation": 4, "acknowledgeUnknownToolEffects": False})
        self.assertEqual(plan.body, {"operationId": op, "generation": 4})
        new_lid, new_sid = u(), u()
        P.learn_launch(launch_row(op, new_lid, new_sid, resume_from=u(), resume_generation=4))
        self.assertNotIn(new_lid, P.STATE.launches)  # wrong source session
        P.learn_launch(launch_row(op, new_lid, new_sid, resume_from=sid, resume_generation=4))
        self.assertIn(new_sid, P.STATE.sessions)
        self.assertEqual(P.STATE.launches[new_lid]["profile_id"], CODEX_P)

    def test_resume_row_must_keep_the_source_profile(self):
        sid, _ = learn_session()
        op = u()
        P.plan_request("POST", f"/api/agent-sessions/{sid}/resume", [], {"operationId": op, "generation": 2})
        for bad in ({"profile_id": u()}, {"profile_revision": 2}, {"cwd": "/tmp"}):
            lid = u()
            P.learn_launch(launch_row(op, lid, u(), resume_from=sid, resume_generation=2, **bad))
            self.assertNotIn(lid, P.STATE.launches, bad)

    def test_once_only_reservations_are_atomic(self):
        results = []
        barrier = threading.Barrier(8)

        def go():
            barrier.wait()
            try:
                P.plan_request("POST", f"/api/router-credentials/{CODEX_B}/rotate", [],
                               {"operationId": u(), "revision": 1})
                results.append("ok")
            except P.Refuse:
                results.append("refused")
        ts = [threading.Thread(target=go) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(results.count("ok"), 1)

    def test_prompt_byte_limit(self):
        sid, _ = learn_session()
        plan = P.plan_request("POST", f"/api/agent-sessions/{sid}/control", [], {"generation": 1})
        lease = u()
        plan.learn({"id": lease, "generation": 1, "expires_at": iso(START)})
        base = {"leaseId": lease, "generation": 1, "kind": "prompt", "controlEpoch": 0}
        P.plan_request("POST", f"/api/agent-sessions/{sid}/commands", [],
                       {**base, "operationId": u(), "text": "a" * P.MAX_PROMPT_BYTES})
        self.refused("POST", f"/api/agent-sessions/{sid}/commands",
                     body={**base, "operationId": u(), "text": "é" * (P.MAX_PROMPT_BYTES // 2 + 1)})

    def test_timeline_query(self):
        sid, _ = learn_session()
        path = f"/api/conversations/sessions/{sid}/timeline"
        P.plan_request("GET", path, [("after", "12"), ("limit", "200"),
                                     ("promptsAfter", "2026-11-27 22:00:01.123+00|" + u())], None)
        self.refused("GET", path, [("limit", "201")])
        self.refused("GET", path, [("limit", "1"), ("limit", "2")])
        self.refused("GET", path, [("promptsAfter", "yesterday")])
        self.refused("GET", path, [("q", "x")])
        self.refused("GET", f"/api/conversations/sessions/{u()}/timeline")

    def test_cleanup_phase(self):
        sid, _ = learn_session()
        plan = P.plan_request("POST", f"/api/agent-sessions/{sid}/control", [], {"generation": 1})
        lease = u()
        plan.learn({"id": lease, "generation": 1, "expires_at": iso(START)})
        pid = u()
        P.plan_request("GET", f"/api/agent-sessions/{sid}/permissions", [], None).learn([{"id": pid}])
        P.STATE.runner_deadline = time.monotonic() - 1
        P.plan_request("POST", f"/api/agent-sessions/{sid}/control", [], {"generation": 1, "leaseId": lease})
        self.refused("POST", f"/api/agent-sessions/{sid}/control", body={"generation": 1})
        P.plan_request("POST", f"/api/agent-sessions/{sid}/commands", [],
                       {"operationId": u(), "leaseId": lease, "generation": 1, "kind": "close"})
        self.refused("POST", f"/api/agent-sessions/{sid}/commands",
                     body={"operationId": u(), "leaseId": lease, "generation": 1, "kind": "prompt",
                           "controlEpoch": 0, "text": "more"})
        self.refused("POST", f"/api/agent-permissions/{pid}/decision",
                     body={"operationId": u(), "generation": 1,
                           "outcome": {"outcome": "selected", "optionId": "allow_once"}})
        P.plan_request("POST", f"/api/agent-permissions/{pid}/decision", [],
                       {"operationId": u(), "generation": 1, "outcome": {"outcome": "cancelled"}})
        self.refused("POST", "/api/agent-launches", body=launch_body())
        self.refused("POST", f"/api/router-credentials/{CODEX_B}/rotate", body={"operationId": u(), "revision": 1})
        P.STATE.cleanup_deadline = time.monotonic() - 1
        self.refused("GET", "/api/agent-profiles")

    def test_duplicate_json_keys(self):
        with self.assertRaises(P.Refuse):
            P.parse_body(b'{"generation": 1, "generation": 2}')
        with self.assertRaises(P.Refuse):
            P.parse_body(b'[1]')

    def test_credentials_learned_only_for_fixed_bindings(self):
        cred, foreign = u(), u()
        P.plan_request("GET", "/api/router-credentials", [], None).learn(
            {"bindings": [], "consumers": [], "credentials": [{"id": cred, "binding_id": CODEX_B},
                                                               {"id": foreign, "binding_id": u()}]})
        P.plan_request("GET", f"/api/router-credentials/keys/{cred}", [], None)
        self.refused("GET", f"/api/router-credentials/keys/{foreign}")


class SocketServer(unittest.TestCase):
    """The real server on a Unix socket, against the fake upstream."""

    def setUp(self):
        fresh_state()
        self.fake = Fake()
        P.UPSTREAM = self.fake
        self.dir = tempfile.mkdtemp()
        self.sock = os.path.join(self.dir, "p.sock")
        self.csock = os.path.join(self.dir, "c.sock")
        self.server = P.Server(self.sock, control=False)
        self.cserver = P.Server(self.csock, control=True)
        for srv in (self.server, self.cserver):
            threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def tearDown(self):
        for srv in (self.server, self.cserver):
            srv.shutdown()
            srv.server_close()

    def raw(self, data: bytes, timeout=5.0, sock=None) -> bytes:
        if sock is None:  # control-lease routes go to the control socket, like the real adapter
            first = data.split(b"\r\n", 1)[0].decode(errors="replace").split(" ")
            sock = self.csock if len(first) > 1 and P._is_control(first[1]) else self.sock
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(sock)
        s.sendall(data)
        out = b""
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            out += chunk
        s.close()
        return out

    def req(self, method, target, body=None, headers=None):
        payload = b"" if body is None else json.dumps(body).encode()
        h = {"Host": "x"}
        if body is not None:
            h["Content-Type"] = "application/json"
            h["Content-Length"] = str(len(payload))
        h.update(headers or {})
        head = f"{method} {target} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
        resp = self.raw(head.encode() + payload)
        status = int(resp.split(b" ", 2)[1])
        return status, resp

    def test_allowed_get_reaches_upstream_without_client_headers(self):
        self.fake.handlers[("GET", "/api/agent-profiles")] = lambda p, b: (200, [{"id": "x"}])
        st, resp = self.req("GET", "/api/agent-profiles",
                            headers={"Cookie": "relay_session=stolen", "Authorization": "Bearer x", "Origin": "evil"})
        self.assertEqual(st, 200)
        self.assertEqual(self.fake.calls, [("GET", "/api/agent-profiles", [], None)])
        self.assertNotIn(b"Set-Cookie", resp)

    def test_refusals_never_reach_upstream(self):
        bad = [("GET", "/api/auth/me", None), ("GET", "/api/agent-profiles/../auth/me", None),
               ("GET", "/api/agent-sessions/%2e%2e/x", None), ("GET", "//api/agent-profiles", None),
               ("GET", "http://relay.chifor.me/api/agent-profiles", None), ("GET", "/api/agent-profiles?x=1", None),
               ("POST", "/api/agent-launches", {"operationId": u(), "profileId": u(), "revision": 1, "cwd": CWD}),
               ("DELETE", f"/api/agent-sessions/{u()}/control/{u()}", None)]
        for method, target, body in bad:
            st, _ = self.req(method, target, body)
            self.assertIn(st, (400, 403), target)
        self.assertEqual(self.fake.calls, [])

    def test_framing_refused(self):
        st, _ = self.req("POST", "/api/agent-launches", None,
                         headers={"Transfer-Encoding": "chunked"})
        self.assertIn(st, (400, 411))
        big = {"operationId": u(), "profileId": CODEX_P, "revision": 1, "cwd": CWD, "pad": "x" * (70 * 1024)}
        try:  # refused before the body is read: a 413, or a reset while the client is still sending
            st, _ = self.req("POST", "/api/agent-launches", big)
            self.assertEqual(st, 413)
        except ConnectionResetError:
            pass
        dup = b'{"operationId": "%s", "operationId": "%s", "profileId": "%s", "revision": 1, "cwd": "%s"}' % (
            u().encode(), u().encode(), CODEX_P.encode(), CWD.encode())
        head = (b"POST /api/agent-launches HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + str(len(dup)).encode() + b"\r\n\r\n")
        resp = self.raw(head + dup)
        self.assertIn(b" 400 ", resp.split(b"\r\n", 1)[0] + b" ")
        self.assertEqual(self.fake.calls, [])

    def test_launch_learning_end_to_end(self):
        op, lid, sid = u(), u(), u()
        self.fake.handlers[("POST", "/api/agent-launches")] = lambda p, b: (202, launch_row(b["operationId"], lid))
        self.fake.handlers[("GET", "/api/agent-launches")] = lambda p, b: (200, [launch_row(op, lid, sid)])
        self.fake.handlers[("GET", "/api/agent-sessions/")] = lambda p, b: (200, {"id": sid})
        st, _ = self.req("GET", f"/api/agent-sessions/{sid}")
        self.assertEqual(st, 403)
        st, _ = self.req("POST", "/api/agent-launches", launch_body(op))
        self.assertEqual(st, 202)
        st, _ = self.req("GET", "/api/agent-launches")
        st, _ = self.req("GET", f"/api/agent-sessions/{sid}")
        self.assertEqual(st, 200)

    def test_upstream_failure_on_mutation_is_uncertain(self):
        def boom(p, b):
            raise TimeoutError()

        self.fake.handlers[("POST", "/api/agent-launches")] = boom
        st, resp = self.req("POST", "/api/agent-launches", launch_body())
        self.assertEqual(st, 504)
        self.assertIn(b"uncertain", resp)

    def test_general_socket_saturation_does_not_block_lease_renewal(self):
        sid, _ = learn_session()
        lease = u()
        P.learn_lease(sid, {"id": lease, "generation": 1, "expires_at": iso(START)})
        stalled = []
        for _ in range(P.GENERAL_READERS + 4):  # the ENTIRE general reader pool, and more, never finish a request
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(self.sock)
            s.sendall(b"GET /api/agent-profiles HTTP/1.1\r\n")
            stalled.append(s)
        time.sleep(0.3)
        st, _ = self.req("POST", f"/api/agent-sessions/{sid}/control", {"generation": 1, "leaseId": lease})
        self.assertNotEqual(st, 503)  # the control socket has its own pool
        for s in stalled:
            s.close()

    def test_control_routes_only_on_the_control_socket(self):
        sid, _ = learn_session()
        lease = u()
        P.learn_lease(sid, {"id": lease, "generation": 1, "expires_at": iso(START)})
        body = json.dumps({"generation": 1, "leaseId": lease}).encode()
        head = (f"POST /api/agent-sessions/{sid}/control HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n").encode()
        resp = self.raw(head + body, sock=self.sock)  # control on the general socket
        self.assertIn(b"use-the-control-socket", resp)
        resp = self.raw(b"GET /api/agent-profiles HTTP/1.1\r\nHost: x\r\n\r\n", sock=self.csock)
        self.assertIn(b"control-socket-is-control-only", resp)
        self.assertEqual(self.fake.calls, [])

    def test_slow_upstream_does_not_block_lease_renewal(self):
        sid, _ = learn_session()
        lease = u()
        P.learn_lease(sid, {"id": lease, "generation": 1, "expires_at": iso(START)})
        stalled = []

        def slow(p, b):
            time.sleep(3.0)
            return 200, []
        self.fake.handlers[("GET", "/api/agent-profiles")] = slow
        # every handler is registered BEFORE the busy calls start: the fake iterates this dict on each call
        self.fake.handlers[("POST", f"/api/agent-sessions/{sid}/control")] = (
            lambda p, b: (200, {"id": lease, "generation": 1, "expires_at": iso(START)}))
        busy = [threading.Thread(target=self.req, args=("GET", "/api/agent-profiles"), daemon=True)
                for _ in range(P.GENERAL_SLOTS)]  # every general handler slot busy upstream
        for t in busy:
            t.start()
        time.sleep(0.5)
        t0 = time.monotonic()
        st, _ = self.req("POST", f"/api/agent-sessions/{sid}/control", {"generation": 1, "leaseId": lease})
        self.assertEqual(st, 200)
        self.assertLess(time.monotonic() - t0, 1.0)
        st, _ = self.req("GET", "/api/agent-profiles")  # a 7th general request is turned away, not queued
        self.assertEqual(st, 503)
        for s in stalled:
            s.close()
        for t in busy:
            t.join()

    def test_incomplete_request_is_cut_at_the_absolute_deadline(self):
        saved, P.REQUEST_DEADLINE = P.REQUEST_DEADLINE, 0.5
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(self.sock)
            s.sendall(b"GET /api/agent-profiles HTTP/1.1\r\n")  # trickle: never finish the head
            t0 = time.monotonic()
            try:
                while s.recv(1024):
                    pass
            except OSError:
                pass
            self.assertLess(time.monotonic() - t0, 3.0)
            s.close()
        finally:
            P.REQUEST_DEADLINE = saved

    def test_refusal_log_has_no_raw_target_or_client_keys(self):
        import io
        buf, saved = io.StringIO(), sys.stderr
        sys.stderr = buf
        try:
            self.req("GET", "/api/auth/me?token=SECRET-VALUE-123")
            self.req("POST", "/api/agent-launches", {**launch_body(), "leakykey-SECRET": 1})
        finally:
            sys.stderr = saved
        out = buf.getvalue()
        self.assertNotIn("SECRET", out)
        self.assertIn('"reason": "route-not-allowed"', out)

    def test_cleanup_phase_rechecked_at_dispatch(self):
        plan = P.plan_request("POST", "/api/agent-launches", [], launch_body())  # planned during the run
        P.STATE.runner_deadline = time.monotonic() - 1  # the phase flips before dispatch
        h = P.Handler.__new__(P.Handler)
        with self.assertRaises(P.Refuse):
            h._dispatch(plan)
        self.assertEqual(self.fake.calls, [])

    def test_closed_after_cleanup_deadline(self):
        P.STATE.cleanup_deadline = time.monotonic() - 1
        st, _ = self.req("GET", "/api/agent-profiles")
        self.assertEqual(st, 403)
        self.assertEqual(self.fake.calls, [])


class UpstreamClient(unittest.TestCase):
    """Upstream.call against a real local HTTP server (TLS swapped for plain HTTP to it)."""

    def setUp(self):
        import http.client
        import http.server
        self.mode = "close"
        test = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                if test.mode == "close":
                    body = b'{"ok": true}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.send_header("Set-Cookie", "relay_session=rotated; Path=/")
                    self.end_headers()
                    self.wfile.write(body)
                else:  # trickle a long body one byte at a time
                    self.send_response(200)
                    self.send_header("Content-Length", "1000")
                    self.end_headers()
                    for _ in range(1000):
                        try:
                            self.wfile.write(b"x")
                            self.wfile.flush()
                        except OSError:
                            return
                        time.sleep(0.05)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

        class Local(P.Upstream):  # the real call() over a plain local socket (no TLS)
            def _open(self, end, holder, expired):
                s = socket.create_connection(("127.0.0.1", port), timeout=max(0.05, end - time.monotonic()))
                holder["sock"] = s
                return s
        self.Local = Local

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_connection_close_response_and_cookie_absorbed(self):
        up = self.Local({"relay_session": "orig"})
        st, _, data, _ = up.call("GET", "/x", [], None, 5)
        self.assertEqual((st, json.loads(data)), (200, {"ok": True}))
        self.assertEqual(up.cookies["relay_session"], "rotated")

    def test_trickling_body_hits_the_absolute_deadline(self):
        self.mode = "trickle"
        up = self.Local({"relay_session": "x"})
        t0 = time.monotonic()
        with self.assertRaises(TimeoutError):
            up.call("GET", "/x", [], None, 1.0)
        self.assertLess(time.monotonic() - t0, 2.5)

    def test_connect_is_bounded_by_the_deadline(self):
        saved = P.RESOLVER.addrs
        P.RESOLVER.addrs = [(socket.AF_INET, ("10.255.255.1", 443))]  # unroutable: connect hangs or fails
        try:
            t0 = time.monotonic()
            with self.assertRaises((TimeoutError, OSError)):
                P.Upstream({"relay_session": "x"}).call("GET", "/x", [], None, 0.5)
            self.assertLess(time.monotonic() - t0, 2.0)
        finally:
            P.RESOLVER.addrs = saved

    def test_safe_route_logs_only_the_fixed_vocabulary(self):
        self.assertEqual(P.safe_route("/api/auth/secret-password?token=x"), "/api/auth/*")
        self.assertEqual(P.safe_route(f"/api/agent-sessions/{u()}/control"), "/api/agent-sessions/:id/control")


if __name__ == "__main__":
    unittest.main()
