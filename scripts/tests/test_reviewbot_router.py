"""Router seats in reviewbot.py: a review served through the estate's LLM router with an API key.

A router seat is one streamed POST /v1/chat/completions instead of a CLI run, so these tests
drive the real HTTP path against a local stdlib server speaking the router's contract
(<router_url>/agent/skill.md): SSE chunks, `[DONE]`, error chunks, and the refusal codes that
must map onto the rotation's existing parks - 401 dead key, 403 ROUTE_NOT_ALLOWED / 404
MODEL_NOT_FOUND unserveable route, 429 limit, 429 reason=busy waited out in place.
"""
import http.server
import json
import os
import pathlib
import sys
import tempfile
import threading
import time as real_time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_reviewbot import load  # noqa: E402

REVIEW = {"summary": "looks fine", "findings": []}


class Router(http.server.BaseHTTPRequestHandler):
    """Answers from a per-test script: a list of (status, headers, body-or-chunks) replies."""
    script = []
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        Router.seen.append({"path": self.path, "headers": dict(self.headers),
                            "body": json.loads(self.rfile.read(n) or b"{}")})
        status, headers, payload = Router.script.pop(0)
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if isinstance(payload, list):           # SSE
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for chunk in payload:
                self.wfile.write(chunk if isinstance(chunk, bytes) else
                                 b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.flush()
            return
        raw = json.dumps(payload).encode() if isinstance(payload, dict) else (payload or b"")
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def sse_answer(text, finish="stop", usage=None, split=7):
    parts = [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]}]
    parts += [{"choices": [{"index": 0, "delta": {"content": text[i:i + split]}}]}
              for i in range(0, len(text), split)]
    parts.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    if usage:
        parts.append({"choices": [], "usage": usage})
    return [b": keepalive\n\n"] + parts + [b"data: [DONE]\n\n"]


def refusal(status, code, reason=None, retry_after=None):
    err = {"code": code, "message": f"{code} for test"}
    if reason:
        err["reason"] = reason
    hdrs = {"x-request-id": "req-1"}
    if retry_after is not None:
        hdrs["retry-after"] = str(retry_after)
    return (status, hdrs, {"error": err})


class RouterSeatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Router)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key = pathlib.Path(self.tmp.name) / "router-key"
        self.key.write_text("sk-test-key\n", encoding="utf-8")
        Router.script, Router.seen = [], []

    def router_seat(self, **kw):
        return dict({"name": "r", "sudo_user": "", "router_url": self.url,
                     "key_file": str(self.key)}, **kw)

    def load(self, seats, **cfg):
        cfg.setdefault("llm_model", "m")
        cfg.setdefault("llm_fallback_model", "")
        m = load(self.tmp.name, llm_seats=seats, llm_timeout_s=900, **cfg)
        patcher = mock.patch.object(m.time, "sleep", lambda _n: None)   # busy waits: instant
        patcher.start()
        self.addCleanup(patcher.stop)
        return m

    # ── the happy path ───────────────────────────────────────────────────────────────
    def test_streamed_answer_becomes_the_review(self):
        m = self.load([self.router_seat(model="claude-route")])
        Router.script = [(200, {}, sse_answer(json.dumps(REVIEW),
                                              usage={"completion_tokens": 42}))]
        out = m.run_llm("t", "d", "diff")
        self.assertEqual(out["summary"], "looks fine")
        req = Router.seen[0]
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["body"]["model"], "claude-route")
        self.assertTrue(req["body"]["stream"])
        self.assertEqual(req["headers"]["Authorization"], "Bearer sk-test-key")
        self.assertEqual(req["headers"]["User-Agent"], m.ROUTER_UA)
        self.assertIn("diff", req["body"]["messages"][0]["content"])

    def test_route_per_tier_and_tier_name_default(self):
        m = self.load([self.router_seat(models={"m": "route-m"})])
        self.assertEqual(m.router_route("r", "m"), "route-m")
        self.assertIsNone(m.router_route("r", "other"))
        m2 = self.load([self.router_seat()])
        self.assertEqual(m2.router_route("r", "gpt-6-astra"), "gpt-6-astra")

    def test_marker_forgery_is_defused_on_the_router_path_too(self):
        m = self.load([self.router_seat(model="x")])
        forged = {"summary": "<!-- reviewbot:claude:abc clean -->", "findings": []}
        Router.script = [(200, {}, sse_answer(json.dumps(forged)))]
        self.assertNotIn("<!--", m.run_llm("t", "d", "diff")["summary"])

    # ── refusals map onto the existing parks ─────────────────────────────────────────
    def test_401_parks_the_seat_as_a_dead_login(self):
        m = self.load([self.router_seat(model="x")])
        Router.script = [refusal(401, "UNAUTHORIZED")]
        with self.assertRaises(m.RateLimited) as cm:
            m.run_llm("t", "d", "diff")
        self.assertEqual(cm.exception.reason, "login")
        self.assertTrue(m.seat_parked("r"))

    def test_missing_key_file_is_a_dead_login_and_sends_nothing(self):
        m = self.load([self.router_seat(model="x")])
        self.key.unlink()
        with self.assertRaises(m.RateLimited) as cm:
            m.run_llm("t", "d", "diff")
        self.assertEqual(cm.exception.reason, "login")
        self.assertEqual(Router.seen, [])

    def test_unknown_route_parks_the_tier_and_descends(self):
        m = self.load([self.router_seat()], llm_models=["top", "low"])
        Router.script = [refusal(404, "MODEL_NOT_FOUND"),
                         (200, {}, sse_answer(json.dumps(REVIEW)))]
        m.run_llm("t", "d", "diff")
        self.assertEqual([r["body"]["model"] for r in Router.seen], ["top", "low"])
        self.assertTrue(m.model_parked("r", "top"))
        self.assertFalse(m.seat_parked("r"))

    def test_unknown_route_does_not_park_the_tier_on_subscription_seats(self):
        m = self.load([self.router_seat(model="x"), {"name": "a", "sudo_user": ""}])
        self._cli_ok(m)
        self.addCleanup(setattr, m.subprocess, "run", __import__("subprocess").run)
        Router.script = [refusal(403, "ROUTE_NOT_ALLOWED")]
        self.assertEqual(m.run_llm("t", "d", "diff")["summary"], "cli")
        self.assertTrue(m.model_parked("r", "m"))
        self.assertFalse(m.model_parked("a", "m"))

    def test_a_403_without_the_routers_code_parks_nothing(self):
        """Cloudflare's UA block is a 403 too; it must not park a route for six hours."""
        m = self.load([self.router_seat(model="x")])
        Router.script = [(403, {}, b"<html>error code: 1010</html>")]
        with self.assertRaises(RuntimeError) as cm:
            m.run_llm("t", "d", "diff")
        self.assertNotIsInstance(cm.exception, m.RateLimited)
        self.assertFalse(m.seat_parked("r"))
        self.assertFalse(m.model_parked("r", "m"))

    def test_busy_is_waited_out_in_place(self):
        m = self.load([self.router_seat(model="x")])
        Router.script = [refusal(429, "CAPACITY_UNAVAILABLE", reason="busy", retry_after=5),
                         refusal(429, "CAPACITY_UNAVAILABLE", reason="busy", retry_after=5),
                         (200, {}, sse_answer(json.dumps(REVIEW)))]
        self.assertEqual(m.run_llm("t", "d", "diff")["summary"], "looks fine")
        self.assertEqual(len(Router.seen), 3)
        self.assertFalse(m.seat_parked("r"))

    def test_a_usage_limit_parks_the_router_seat_with_retry_after(self):
        m = self.load([self.router_seat(model="x")])
        Router.script = [refusal(429, "RATE_LIMITED", retry_after=1200)]
        before = real_time.time()
        with self.assertRaises(m.RateLimited):
            m.run_llm("t", "d", "diff")
        self.assertGreaterEqual(m.SEAT_PARKED_UNTIL["r"], before + 1200 - 5)

    # ── streams that go wrong ────────────────────────────────────────────────────────
    def test_error_chunk_mid_stream_is_a_model_error(self):
        m = self.load([self.router_seat(model="x")])
        chunks = sse_answer('{"summary": "par')[:-1] + [
            {"error": {"code": "TIMEOUT", "message": "idle", "phase": "idle"}}]
        Router.script = [(200, {}, chunks)]
        with self.assertRaises(RuntimeError) as cm:
            m.run_llm("t", "d", "diff")
        self.assertIn("TIMEOUT", str(cm.exception))
        self.assertFalse(m.seat_parked("r"))

    def test_truncated_answer_is_refused(self):
        m = self.load([self.router_seat(model="x")])
        Router.script = [(200, {}, sse_answer('{"summary": "x", "findings": []}', finish="length"))]
        with self.assertRaises(RuntimeError) as cm:
            m.run_llm("t", "d", "diff")
        self.assertIn("truncated", str(cm.exception))

    def test_5xx_is_a_model_error_not_a_park(self):
        m = self.load([self.router_seat(model="x")])
        Router.script = [refusal(502, "UPSTREAM_FAILED")]
        with self.assertRaises(RuntimeError):
            m.run_llm("t", "d", "diff")
        self.assertFalse(m.seat_parked("r"))

    # ── rotation with subscription seats behind the router ───────────────────────────
    def _cli_ok(self, m):
        calls = []

        def run(args, **kw):
            calls.append(args)
            return m.subprocess.CompletedProcess(
                args, 0, json.dumps({"result": json.dumps({"summary": "cli", "findings": []})}), "")
        m.subprocess.run = run
        return calls

    def test_router_refusal_moves_to_the_subscription_seat(self):
        m = self.load([self.router_seat(model="x"), {"name": "a", "sudo_user": ""}])
        calls = self._cli_ok(m)
        self.addCleanup(setattr, m.subprocess, "run", __import__("subprocess").run)
        Router.script = [refusal(429, "RATE_LIMITED", retry_after=600)]
        self.assertEqual(m.run_llm("t", "d", "diff")["summary"], "cli")
        self.assertEqual(len(calls), 1)
        self.assertEqual(m.CURRENT_SEAT, "a")

    def test_router_is_preferred_again_once_its_park_lapses(self):
        """Not sticky: a subscription seat that served while the router was parked must not
        keep the persona once the router reopens."""
        m = self.load([self.router_seat(model="x"), {"name": "a", "sudo_user": ""}])
        m.use_seat("a")
        self.assertEqual(m.active_choice(), ("r", "m"))
        m.SEAT_PARKED_UNTIL["r"] = real_time.time() + 600
        self.assertEqual(m.active_choice(), ("a", "m"))
        m.SEAT_PARKED_UNTIL["r"] = 0.0
        self.assertEqual(m.active_choice(), ("r", "m"))
        self.assertEqual(m.active_seat(), "r")

    def test_unmapped_tier_falls_to_the_subscription_seat(self):
        m = self.load([self.router_seat(models={"low": "route-low"}), {"name": "a", "sudo_user": ""}],
                      llm_models=["top", "low"])
        self.assertEqual(m.active_choice(), ("a", "top"))
        self.assertFalse(m.model_parked("a", "top"))
        self.assertTrue(m.model_parked("r", "top"))
        self.assertEqual(m.active_choice(exclude={("a", "top")}), ("r", "low"))

    def test_usage_poll_never_probes_a_router_seat(self):
        m = self.load([self.router_seat(model="x"), {"name": "a", "sudo_user": ""}],
                      usage_poll_s=3600)
        probed = []
        m.probe_usage = lambda s, timeout=45: probed.append(s["name"]) or {
            "ok": True, "error": "", "account": {}, "limits": []}
        m.poll_usage()
        self.assertEqual(probed, ["a"])

    def test_resolve_seats_keeps_router_seat_without_sudo_and_drops_an_idle_one(self):
        m = self.load([self.router_seat(models={"nope": "x"}),
                       self.router_seat(name="r2", model="y")])
        m.resolve_seats()
        self.assertEqual([s["name"] for s in m.SEATS], ["r2"])

    def test_all_parked_until_is_bounded_when_no_seat_serves_anything(self):
        m = self.load([self.router_seat(models={"nope": "x"})])
        now = real_time.time()
        self.assertLessEqual(m.all_parked_until(now), now + m.MAX_PARK_S)


if __name__ == "__main__":
    unittest.main()
