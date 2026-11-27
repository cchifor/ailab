#!/usr/bin/env python3
"""A bounded Relay session for the automated dev-worker-1 Pi canary phase A (plans/2026-11-27-relay-operator-session-plan.md).

Started ONLY by `scripts/relay-operator.py session`, inside a pinned python image, as a hardened container. It logs in
ONCE as the just-in-time automation identity (relay-admin-call.py's Keycloak flow), holds that one Relay session in
memory, and serves a fixed allowlist of Relay API calls on a Unix socket that only the Runner container (run with
--network none) shares. The Relay control lease binds actor.userId AND the login session, so one session must live for
the whole run; nothing else about it is general-purpose.

    stdin line 1: the password; line 2: JSON {"username": EMAIL, "runner_deadline": EPOCH, "cleanup_deadline": EPOCH}
    stdout: one line "READY {...}" once logged in (admin confirmed) and the startup inventory is taken
    stderr: JSON evidence lines (no bodies, cookies, tokens or response content)
    exit:   0 logout confirmed, 4 logout not confirmed, 2 login/startup failed

Scope is pinned in this file (SCOPE). Request bodies are rebuilt from validated fields; client headers are never
forwarded. Dynamic IDs are usable only once this run's own responses produced them (provenance rules in learn_*).
After runner_deadline only cleanup-shaped calls pass; at cleanup_deadline everything is refused and the proxy logs out.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import re
import signal
import socket
import socketserver
import ssl
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler

# ---------------------------------------------------------------- pinned scope (a change here is a reviewed change)
ORIGIN = "https://relay.chifor.me"
HOST = "relay.chifor.me"
WORKSPACES = "/home/c4/.local/state/relay-holder/workspaces/"
BINDINGS = {
    "a0f57684-747f-4c3e-8d90-e684b7262590": "codex",
    "d2871eec-8f5a-4afe-a917-014f0ae8e14d": "qwen",
}
PROFILES = {  # profile id -> (revision, cwd, binding)
    "1d9a7d6e-4b26-49fd-9053-7b0a22e1baae": (1, WORKSPACES + "1d9a7d6e-4b26-49fd-9053-7b0a22e1baae",
                                             "a0f57684-747f-4c3e-8d90-e684b7262590"),
    "f25c187b-1e50-4c7c-860e-52947bfdf39e": (1, WORKSPACES + "f25c187b-1e50-4c7c-860e-52947bfdf39e",
                                             "d2871eec-8f5a-4afe-a917-014f0ae8e14d"),
}
PREDECESSOR_SESSION = "45724087-335a-4c55-ad0f-cb278afe608a"  # GET-only
PREDECESSOR_PERMISSION = "70fb1b8e-f7cc-4cc3-b98f-66f83e46d1ab"  # GET-only
THOUGHT_LEVELS = {"low", "high"}
EXPECTED_NAME = "relay-operator (automation)"
UA = "relay-session-proxy/1 (ailab operator automation)"

SOCKET_PATH = "/run/relay-session/proxy.sock"
MAX_HEAD = 16 * 1024
MAX_BODY = 64 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
CLIENT_TIMEOUT = 10.0
UPSTREAM_CAP = 30.0
LOGOUT_BUDGET = 20.0
SLOTS = 8  # concurrent handlers; one is always left for control-lease requests

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
UUID_RE = re.compile(r"^" + UUID + r"$")
ISO_CURSOR = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])[ T]([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d{1,6})?"
                        r"([+-]\d{2}(:?\d{2})?|Z)\|" + UUID + r"$")
INT_MAX = 2147483647
SAFE_MAX = 9007199254740991


class Refuse(Exception):
    """A request outside the allowlist or its schema: answered locally (403/400), never sent upstream."""

    def __init__(self, reason: str, status: int = 403):
        super().__init__(reason)
        self.reason, self.status = reason, status


def log(**kv) -> None:
    kv.setdefault("t", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    sys.stderr.write(json.dumps(kv, sort_keys=True) + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- strict JSON / schema helpers
def _no_dupes(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise Refuse(f"duplicate JSON key {k!r}", 400)
        d[k] = v
    return d


def parse_body(raw: bytes) -> dict:
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dupes)
    except Refuse:
        raise
    except (UnicodeDecodeError, ValueError):
        raise Refuse("body is not JSON", 400)
    if not isinstance(obj, dict):
        raise Refuse("body is not a JSON object", 400)
    return obj


def keys(body: dict, required: set, optional: set = frozenset()) -> None:
    extra = set(body) - required - set(optional)
    missing = required - set(body)
    if extra or missing:
        raise Refuse(f"body keys: missing {sorted(missing)}, unexpected {sorted(extra)}", 400)


def uuid_of(v, what: str) -> str:
    if not isinstance(v, str) or not UUID_RE.match(v):
        raise Refuse(f"{what} must be a lowercase UUID", 400)
    return v


def int_of(v, what: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise Refuse(f"{what} must be an integer in {lo}..{hi}", 400)
    return v


# ---------------------------------------------------------------- run state (all mutation under STATE.lock)
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.actor_id: str | None = None
        self.run_start: datetime | None = None
        self.startup_ops: set[str] = set()  # this actor's launch operation_ids that existed at login
        self.ops: dict[str, dict] = {}  # reserved operationId -> {route, sid?, ...}
        self.launch_intents: dict[str, dict] = {}  # launch/resume operationId -> expected provenance
        self.launches: dict[str, dict] = {}  # learned launch id -> {profile_id, revision, cwd, session_id}
        self.sessions: dict[str, str] = {}  # learned session id -> launch id
        self.leases: dict[str, dict] = {}  # learned lease id -> {sid, generation}
        self.permissions: dict[str, str] = {}  # learned permission id -> sid
        self.decided: set[str] = set()
        self.credentials: dict[str, str] = {}  # learned credential id -> binding id
        self.rotated: set[str] = set()
        self.runner_deadline = float("inf")  # monotonic
        self.cleanup_deadline = float("inf")
        self.closed = False


STATE = State()


def phase() -> str:
    now = time.monotonic()
    if STATE.closed or now >= STATE.cleanup_deadline:
        return "closed"
    return "cleanup" if now >= STATE.runner_deadline else "run"


def reserve_op(op: str, record: dict) -> None:
    """Atomically reserve a fresh operationId BEFORE dispatch."""
    with STATE.lock:
        if op in STATE.ops or op in STATE.startup_ops:
            raise Refuse("operationId already used (this run or a launch that existed at login)")
        STATE.ops[op] = record


def learned_session(sid: str, *, allow_predecessor: bool) -> str:
    if sid == PREDECESSOR_SESSION:
        if allow_predecessor:
            return sid
        raise Refuse("the predecessor session is read-only")
    with STATE.lock:
        if sid not in STATE.sessions:
            raise Refuse("session not learned from this run's launches")
    return sid


def _created(row) -> datetime | None:
    v = row.get("created_at")
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def learn_launch(row) -> None:
    """A launch row is learned only if THIS run dispatched its operation, as the JIT actor, after login, with the exact
    dispatched provenance. Works for a POST response and for a read-only inventory reconciliation alike."""
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not UUID_RE.match(row["id"]):
        return
    op = row.get("operation_id")
    with STATE.lock:
        intent = STATE.launch_intents.get(op) if isinstance(op, str) else None
        if intent is None or row.get("actor_id") != STATE.actor_id:
            return
        created = _created(row)
        if created is None or STATE.run_start is None or created < STATE.run_start:
            log(event="learn-refused", reason="launch row predates this run", launch=row["id"])
            return
        if intent["kind"] == "launch":
            ok = (row.get("profile_id"), row.get("profile_revision"), row.get("cwd")) == (
                intent["profile_id"], intent["revision"], intent["cwd"])
        else:  # resume: the row must name the dispatched source session and generation
            ok = (row.get("resume_from"), row.get("resume_generation")) == (intent["sid"], intent["generation"])
        if not ok:
            log(event="learn-refused", reason="launch row does not match the dispatched intent", launch=row["id"])
            return
        entry = STATE.launches.setdefault(row["id"], {
            "profile_id": intent["profile_id"], "revision": intent["revision"], "cwd": intent["cwd"],
            "session_id": None})
        sid = row.get("session_id")
        if isinstance(sid, str) and UUID_RE.match(sid) and entry["session_id"] in (None, sid):
            if sid != PREDECESSOR_SESSION:
                entry["session_id"] = sid
                STATE.sessions.setdefault(sid, row["id"])


def learn_lease(sid: str, row) -> None:
    if isinstance(row, dict) and isinstance(row.get("id"), str) and UUID_RE.match(row["id"]) \
            and isinstance(row.get("generation"), int):
        with STATE.lock:
            prior = STATE.leases.get(row["id"])
            if prior is None or prior["sid"] == sid:
                STATE.leases[row["id"]] = {"sid": sid, "generation": row["generation"]}


def learn_permissions(sid: str, rows) -> None:
    if not isinstance(rows, list):
        return
    with STATE.lock:
        for r in rows:
            pid = r.get("id") if isinstance(r, dict) else None
            if isinstance(pid, str) and UUID_RE.match(pid) and pid != PREDECESSOR_PERMISSION:
                STATE.permissions.setdefault(pid, sid)


def learn_credentials(inv) -> None:
    rows = inv.get("credentials") if isinstance(inv, dict) else None
    if not isinstance(rows, list):
        return
    with STATE.lock:
        for r in rows:
            if isinstance(r, dict) and r.get("binding_id") in BINDINGS and isinstance(r.get("id"), str) \
                    and UUID_RE.match(r["id"]):
                STATE.credentials[r["id"]] = r["binding_id"]


# ---------------------------------------------------------------- the allowlist
class Plan:
    """What to send upstream (rebuilt from validated values) and how to learn from the answer."""

    def __init__(self, route, method, path, query=None, body=None, learn=None, mutation=False, op=None, ids=None):
        self.route, self.method, self.path, self.query = route, method, path, query or []
        self.body, self.learn, self.mutation, self.op, self.ids = body, learn, mutation, op, ids or {}


def _q(query: list, allowed: dict) -> list:
    """Only the listed keys, each at most once, each value validated."""
    seen, out = set(), []
    for k, v in query:
        if k not in allowed:
            raise Refuse(f"query key {k!r} not allowed")
        if k in seen:
            raise Refuse(f"query key {k!r} repeated")
        seen.add(k)
        out.append((k, allowed[k](v)))
    return out


def _after(v):
    if not re.fullmatch(r"\d{1,15}", v):
        raise Refuse("after must be a non-negative integer", 400)
    return v


def _limit(v):
    if not re.fullmatch(r"\d{1,3}", v) or not 1 <= int(v) <= 200:
        raise Refuse("limit must be 1..200", 400)
    return v


def _cursor(v):
    if not ISO_CURSOR.match(v):
        raise Refuse("promptsAfter is not a timeline cursor", 400)
    return v


SESSION_READS = {"", "/router", "/communication", "/control", "/settings", "/permissions"}


def plan_request(method: str, path: str, query: list, body: dict | None) -> Plan:
    ph = phase()
    if ph == "closed":
        raise Refuse("the session is closed (cleanup deadline passed)")
    cleanup = ph == "cleanup"
    if method == "GET" and body is not None:
        raise Refuse("GET with a body", 400)

    # ---- credentials
    if path == "/api/router-credentials" and method == "GET":
        return Plan("credentials", method, path, _q(query, {}), learn=lambda d: learn_credentials(d))
    m = re.fullmatch(r"/api/router-credentials/keys/(" + UUID + ")", path)
    if m and method == "GET":
        with STATE.lock:
            if m.group(1) not in STATE.credentials:
                raise Refuse("credential not learned from a fixed binding")
        return Plan("credential-key", method, path, _q(query, {}), ids={"cred": m.group(1)})
    m = re.fullmatch(r"/api/router-credentials/(" + UUID + ")/rotate", path)
    if m and method == "POST":
        binding = m.group(1)
        if binding not in BINDINGS:
            raise Refuse("binding outside the pinned scope")
        if cleanup:
            raise Refuse("no rotation after the runner deadline")
        keys(body, {"operationId", "revision"})
        op = uuid_of(body["operationId"], "operationId")
        rev = int_of(body["revision"], "revision", 1, SAFE_MAX)
        with STATE.lock:
            if binding in STATE.rotated:
                raise Refuse("this binding was already rotated in this run")
        reserve_op(op, {"route": "rotate", "binding": binding})
        with STATE.lock:
            STATE.rotated.add(binding)
        return Plan("rotate", method, path, _q(query, {}), {"operationId": op, "revision": rev},
                    learn=lambda d: learn_credentials(d), mutation=True, op=op, ids={"binding": binding})

    # ---- profiles and launches
    if path == "/api/agent-profiles" and method == "GET":
        return Plan("profiles", method, path, _q(query, {}))
    if path == "/api/agent-launches" and method == "GET":
        return Plan("launches", method, path, _q(query, {}),
                    learn=lambda d: [learn_launch(r) for r in d] if isinstance(d, list) else None)
    if path == "/api/agent-launches" and method == "POST":
        if cleanup:
            raise Refuse("no launch after the runner deadline")
        keys(body, {"operationId", "profileId", "revision", "cwd"})
        op = uuid_of(body["operationId"], "operationId")
        pid = uuid_of(body["profileId"], "profileId")
        if pid not in PROFILES:
            raise Refuse("profile outside the pinned scope")
        rev, cwd, _ = PROFILES[pid]
        if body["revision"] != rev or isinstance(body["revision"], bool):
            raise Refuse("profile revision is not the pinned one")
        if body["cwd"] != cwd:
            raise Refuse("cwd is not the profile's approved workspace")
        reserve_op(op, {"route": "launch", "profile": pid})
        with STATE.lock:
            STATE.launch_intents[op] = {"kind": "launch", "profile_id": pid, "revision": rev, "cwd": cwd}
        return Plan("launch", method, path, _q(query, {}),
                    {"operationId": op, "profileId": pid, "revision": rev, "cwd": cwd},
                    learn=lambda d: learn_launch(d), mutation=True, op=op, ids={"profile": pid})

    # ---- permissions by id
    m = re.fullmatch(r"/api/agent-permissions/(" + UUID + ")(/decision)?", path)
    if m:
        pid, decision = m.group(1), bool(m.group(2))
        if not decision and method == "GET":
            if pid != PREDECESSOR_PERMISSION:
                with STATE.lock:
                    if pid not in STATE.permissions:
                        raise Refuse("permission not learned under this run's sessions")
            return Plan("permission", method, path, _q(query, {}), ids={"permission": pid})
        if decision and method == "POST":
            with STATE.lock:
                sid = STATE.permissions.get(pid)
            if sid is None:
                raise Refuse("permission not learned under this run's sessions (or read-only)")
            keys(body, {"operationId", "generation", "outcome"})
            op = uuid_of(body["operationId"], "operationId")
            gen = int_of(body["generation"], "generation", 1, SAFE_MAX)
            out = body["outcome"]
            if not isinstance(out, dict) or out.get("outcome") not in ("selected", "cancelled"):
                raise Refuse("outcome must be {outcome: selected, optionId} or {outcome: cancelled}", 400)
            if out["outcome"] == "selected":
                keys(out, {"outcome", "optionId"})
                if not isinstance(out["optionId"], str) or not 1 <= len(out["optionId"]) <= 200:
                    raise Refuse("optionId must be a 1..200 character string", 400)
                if cleanup:
                    raise Refuse("only cancellation of a permission after the runner deadline")
                outcome = {"outcome": "selected", "optionId": out["optionId"]}
            else:
                keys(out, {"outcome"})
                outcome = {"outcome": "cancelled"}
            with STATE.lock:
                if pid in STATE.decided:
                    raise Refuse("this permission was already decided in this run")
            reserve_op(op, {"route": "decision", "sid": sid, "permission": pid})
            with STATE.lock:
                STATE.decided.add(pid)
            return Plan("decision", method, path, _q(query, {}),
                        {"operationId": op, "generation": gen, "outcome": outcome},
                        mutation=True, op=op, ids={"permission": pid, "session": sid})

    # ---- timeline
    m = re.fullmatch(r"/api/conversations/sessions/(" + UUID + ")/timeline", path)
    if m and method == "GET":
        sid = learned_session(m.group(1), allow_predecessor=True)
        return Plan("timeline", method, path, _q(query, {"after": _after, "limit": _limit, "promptsAfter": _cursor}),
                    ids={"session": sid})

    # ---- one session
    m = re.fullmatch(r"/api/agent-sessions/(" + UUID + r")((?:/[a-z-]+)?)(?:/(" + UUID + r"))?", path)
    if not m:
        raise Refuse("route not on the allowlist")
    sid, sub, tail = m.group(1), m.group(2), m.group(3)

    if method == "GET" and tail is None and sub in SESSION_READS:
        learned_session(sid, allow_predecessor=True)
        learn = (lambda d, s=sid: learn_permissions(s, d)) if sub == "/permissions" and sid != PREDECESSOR_SESSION else None
        return Plan("session" + (sub or ""), method, path, _q(query, {}), learn=learn, ids={"session": sid})
    if method == "GET" and sub == "/operations" and tail:
        learned_session(sid, allow_predecessor=True)
        if sid != PREDECESSOR_SESSION:
            with STATE.lock:
                rec = STATE.ops.get(tail)
            if not rec or rec.get("sid") != sid:
                raise Refuse("operation not dispatched by this run for this session")
        return Plan("operation", method, path, _q(query, {}), ids={"session": sid, "operation": tail})
    if method == "GET" and sub == "/native-recovery" and tail is None:
        learned_session(sid, allow_predecessor=False)
        return Plan("native-recovery", method, path, _q(query, {}), ids={"session": sid})

    if method == "POST" and sub == "/control" and tail is None:
        learned_session(sid, allow_predecessor=False)
        if "leaseId" in body:
            keys(body, {"generation", "leaseId"})
            lease = uuid_of(body["leaseId"], "leaseId")
            with STATE.lock:
                known = STATE.leases.get(lease)
            if not known or known["sid"] != sid:
                raise Refuse("lease not learned for this session")
            gen = int_of(body["generation"], "generation", 1, SAFE_MAX)
            if gen != known["generation"]:
                raise Refuse("lease renewal must keep the lease's generation")
            out = {"generation": gen, "leaseId": lease}
        else:
            if cleanup:
                raise Refuse("no new control lease after the runner deadline (renew the existing one)")
            keys(body, {"generation"})
            out = {"generation": int_of(body["generation"], "generation", 1, SAFE_MAX)}
        return Plan("control", method, path, _q(query, {}), out, learn=lambda d, s=sid: learn_lease(s, d),
                    mutation=True, ids={"session": sid})
    if method == "DELETE" and sub == "/control" and tail:
        learned_session(sid, allow_predecessor=False)
        with STATE.lock:
            known = STATE.leases.get(tail)
        if not known or known["sid"] != sid:
            raise Refuse("lease not learned for this session")
        return Plan("control-release", method, path, _q(query, {}), mutation=True, ids={"session": sid, "lease": tail})
    if method == "POST" and sub == "/commands" and tail is None:
        learned_session(sid, allow_predecessor=False)
        return _command(sid, body, cleanup, query)
    if method == "POST" and sub == "/resume" and tail is None:
        learned_session(sid, allow_predecessor=False)
        if cleanup:
            raise Refuse("no resume after the runner deadline")
        keys(body, {"operationId", "generation"}, {"acknowledgeUnknownToolEffects"})
        if body.get("acknowledgeUnknownToolEffects", False) is not False:
            raise Refuse("acknowledgeUnknownToolEffects is never sent by this session")
        op = uuid_of(body["operationId"], "operationId")
        gen = int_of(body["generation"], "generation", 1, INT_MAX)
        with STATE.lock:
            src = STATE.launches[STATE.sessions[sid]]
        reserve_op(op, {"route": "resume", "sid": sid})
        with STATE.lock:
            STATE.launch_intents[op] = {"kind": "resume", "sid": sid, "generation": gen,
                                        "profile_id": src["profile_id"], "revision": src["revision"],
                                        "cwd": src["cwd"]}
        return Plan("resume", method, path, _q(query, {}), {"operationId": op, "generation": gen},
                    learn=lambda d: learn_launch(d), mutation=True, op=op, ids={"session": sid})
    raise Refuse("route not on the allowlist")


def _command(sid: str, body: dict, cleanup: bool, query: list) -> Plan:
    kind = body.get("kind")
    common = {"operationId", "leaseId", "generation", "kind"}
    if kind == "prompt":
        keys(body, common | {"controlEpoch", "text"})
        if not isinstance(body["text"], str) or not 1 <= len(body["text"]) <= 16384:
            raise Refuse("prompt text must be 1..16384 characters", 400)
        extra = {"controlEpoch": int_of(body["controlEpoch"], "controlEpoch", 0, SAFE_MAX), "text": body["text"]}
    elif kind == "set-config":
        keys(body, common | {"controlEpoch", "settingsRevision", "configId", "value"})
        if body["configId"] != "thought_level" or body["value"] not in THOUGHT_LEVELS:
            raise Refuse("set-config is limited to thought_level low/high")
        extra = {"controlEpoch": int_of(body["controlEpoch"], "controlEpoch", 0, SAFE_MAX),
                 "settingsRevision": int_of(body["settingsRevision"], "settingsRevision", 1, SAFE_MAX),
                 "configId": "thought_level", "value": body["value"]}
    elif kind in ("cancel", "close"):
        keys(body, common)
        extra = {}
    else:
        raise Refuse("command kind not on the allowlist")
    if cleanup and kind not in ("cancel", "close"):
        raise Refuse("only cancel/close after the runner deadline")
    op = uuid_of(body["operationId"], "operationId")
    lease = uuid_of(body["leaseId"], "leaseId")
    with STATE.lock:
        known = STATE.leases.get(lease)
    if not known or known["sid"] != sid:
        raise Refuse("lease not learned for this session")
    gen = int_of(body["generation"], "generation", 1, SAFE_MAX)
    reserve_op(op, {"route": "command", "sid": sid, "kind": kind})
    out = {"operationId": op, "leaseId": lease, "generation": gen, "kind": kind, **extra}
    return Plan("command-" + kind, "POST", f"/api/agent-sessions/{sid}/commands", _q(query, {}), out,
                mutation=True, op=op, ids={"session": sid})


# ---------------------------------------------------------------- upstream (bounded, trusted values only)
class Upstream:
    def __init__(self, cookies: dict[str, str]):
        self.cookies = dict(cookies)
        self.lock = threading.Lock()

    def cookie_header(self) -> str:
        with self.lock:
            return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def absorb(self, headers) -> None:
        """Keep a refreshed relay_session inside the proxy; nothing is passed to the client."""
        for h in headers.get_all("Set-Cookie") or []:
            name, _, rest = h.partition("=")
            value = rest.split(";", 1)[0]
            if name.strip() in self.cookies:
                with self.lock:
                    self.cookies[name.strip()] = value

    def call(self, method: str, path: str, query: list, body: dict | None, budget: float):
        if budget <= 0.05:
            raise TimeoutError("no time left before the deadline")
        url = path + ("?" + urllib.parse.urlencode(query) if query else "")
        payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = {"User-Agent": UA, "Accept": "application/json", "Cookie": self.cookie_header()}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        if method != "GET":
            headers["Origin"] = ORIGIN
        end = time.monotonic() + min(budget, UPSTREAM_CAP)
        conn = http.client.HTTPSConnection(HOST, 443, timeout=max(0.1, end - time.monotonic()),
                                           context=ssl.create_default_context())
        try:
            conn.request(method, url, body=payload, headers=headers)
            conn.sock.settimeout(max(0.1, end - time.monotonic()))
            resp = conn.getresponse()
            self.absorb(resp.headers)
            chunks, size = [], 0
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError("upstream response exceeded the deadline")
                conn.sock.settimeout(max(0.1, left))
                chunk = resp.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise ValueError("upstream response exceeds 8 MiB")
                chunks.append(chunk)
            return resp.status, resp.getheader("Content-Type") or "application/json", b"".join(chunks), resp.headers
        finally:
            conn.close()


UPSTREAM: Upstream | None = None


# ---------------------------------------------------------------- the socket server
class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, path):
        super().__init__(path, Handler)
        self.slots = threading.BoundedSemaphore(SLOTS)
        self.general = threading.BoundedSemaphore(SLOTS - 1)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def _is_control(path: str) -> bool:
    return bool(re.fullmatch(r"/api/agent-sessions/" + UUID + r"/control(/" + UUID + ")?", path))


class Handler(BaseHTTPRequestHandler):
    timeout = CLIENT_TIMEOUT
    protocol_version = "HTTP/1.1"
    server_version = "relay-session-proxy"
    sys_version = ""

    def log_message(self, *args):  # the evidence log replaces the default access log
        pass

    def _reply(self, status: int, data: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _refuse(self, method, target, reason, status) -> None:
        log(event="refused", method=method, target=target[:200], reason=reason, status=status)
        self._reply(status, json.dumps({"error": "refused by relay-session-proxy", "reason": reason}).encode())

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (socket.timeout, TimeoutError):
            log(event="client-timeout")
            self.close_connection = True

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")

    def do_DELETE(self):
        self._serve("DELETE")

    def _serve(self, method: str) -> None:
        # The raw target from the request line: http.server normalizes self.path (it collapses a leading "//").
        parts = self.requestline.split(" ")
        target = parts[1] if len(parts) == 3 else ""
        head_size = len(self.requestline) + sum(len(k) + len(v) + 4 for k, v in self.headers.items())
        try:
            if head_size > MAX_HEAD:
                raise Refuse("request head too large", 431)
            if not target.startswith("/") or "#" in target:
                raise Refuse("only origin-form targets", 400)
            raw_path, _, raw_query = target.partition("?")
            if "%" in raw_path or ".." in raw_path or "//" in raw_path or "\\" in raw_path or ";" in raw_path:
                raise Refuse("encoded, dotted or empty path segments are refused", 400)
            try:
                query = urllib.parse.parse_qsl(raw_query, keep_blank_values=True, strict_parsing=bool(raw_query),
                                               max_num_fields=8)
            except ValueError:
                raise Refuse("malformed query", 400)
            if self.headers.get("Transfer-Encoding"):
                raise Refuse("Transfer-Encoding is refused", 400)
            lengths = self.headers.get_all("Content-Length") or []
            body = None
            if method == "POST":
                if len(lengths) != 1 or not lengths[0].isdigit():
                    raise Refuse("POST needs exactly one Content-Length", 411)
                n = int(lengths[0])
                if n > MAX_BODY:
                    raise Refuse("body exceeds 64 KiB", 413)
                raw = self.rfile.read(n)
                if len(raw) != n:
                    raise Refuse("short body", 400)
                body = parse_body(raw)
            elif lengths and lengths != ["0"]:
                raise Refuse(f"{method} with a body", 400)
            general = None
            if not _is_control(raw_path):
                general = self.server.general
                if not general.acquire(blocking=False):
                    raise Refuse("busy (slots reserved for lease renewal)", 503)
            try:
                plan = plan_request(method, raw_path, query, body)
                self._dispatch(plan)
            finally:
                if general is not None:
                    general.release()
        except Refuse as r:
            self._refuse(method, target, r.reason, r.status)

    def _dispatch(self, plan: Plan) -> None:
        start = time.monotonic()
        rec = {"event": "call", "method": plan.method, "route": plan.route, "ids": plan.ids, "operationId": plan.op}
        budget = STATE.cleanup_deadline - start
        if phase() == "closed" or budget <= 0:
            raise Refuse("the session is closed (cleanup deadline passed)")
        try:
            status, ctype, data, _ = UPSTREAM.call(plan.method, plan.path, plan.query, plan.body, budget)
        except Exception as e:  # noqa: BLE001 - every upstream failure is reported, a mutation as uncertain
            outcome = "uncertain" if plan.mutation else "failed"
            log(**rec, dispatched=True, outcome=outcome, error=type(e).__name__,
                ms=int((time.monotonic() - start) * 1000))
            self._reply(504, json.dumps({"error": "upstream " + outcome, "operationId": plan.op}).encode())
            return
        if 300 <= status < 400:
            log(**rec, dispatched=True, status=status, outcome="redirect-refused")
            self._reply(502, json.dumps({"error": "upstream redirect refused"}).encode())
            return
        if 200 <= status < 300 and plan.learn is not None and data:
            try:
                plan.learn(json.loads(data))
            except ValueError:
                pass
        log(**rec, dispatched=True, status=status, ms=int((time.monotonic() - start) * 1000))
        self._reply(status, data, ctype)


# ---------------------------------------------------------------- login, logout, lifecycle
def _load_admin_call():
    spec = importlib.util.spec_from_file_location("relay_admin_call", os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "relay-admin-call.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def login(username: str, password: str):
    rac = _load_admin_call()
    c = rac.Client(ORIGIN)
    rac.login(c, username, password)  # raises rac.Fail; checks admin + the expected name
    st, h, raw = c.request("GET", ORIGIN + "/api/auth/me")
    me = json.loads(raw)
    if st != 200 or me.get("name") != EXPECTED_NAME or me.get("role") != "admin":
        raise rac.Fail("/api/auth/me is not the admin automation identity")
    actor = me.get("userId")
    if not isinstance(actor, str) or not UUID_RE.match(actor):
        raise rac.Fail("/api/auth/me carries no user id")
    server_now = parsedate_to_datetime(h.get("Date")).astimezone(timezone.utc).replace(microsecond=0)
    cookies = {ck.name: ck.value for ck in c.jar if ck.domain.lstrip(".") == HOST}
    if "relay_session" not in cookies:
        raise rac.Fail("no relay_session cookie")
    return actor, server_now, cookies


def logout() -> bool:
    """Logout plus /api/auth/me = 401, within one shared budget."""
    end = time.monotonic() + LOGOUT_BUDGET
    try:
        st, _, _, _ = UPSTREAM.call("POST", "/api/auth/logout", [], {}, end - time.monotonic())
        st2, _, _, _ = UPSTREAM.call("GET", "/api/auth/me", [], None, end - time.monotonic())
    except Exception as e:  # noqa: BLE001
        log(event="logout", closed=False, error=type(e).__name__)
        return False
    closed = st == 200 and st2 == 401
    log(event="logout", status=st, me_after=st2, closed=closed)
    return closed


def startup_inventory() -> None:
    st, _, data, _ = UPSTREAM.call("GET", "/api/agent-launches", [], None, UPSTREAM_CAP)
    rows = json.loads(data) if st == 200 else None
    if not isinstance(rows, list):
        raise RuntimeError(f"startup launch inventory unavailable ({st})")
    with STATE.lock:
        STATE.startup_ops = {r["operation_id"] for r in rows if isinstance(r, dict)
                             and r.get("actor_id") == STATE.actor_id and isinstance(r.get("operation_id"), str)}
    log(event="startup", prior_launch_operations=len(STATE.startup_ops))


def main() -> int:
    global UPSTREAM
    password = sys.stdin.readline().rstrip("\r\n")
    cfg = json.loads(sys.stdin.readline())
    now_wall, now_mono = time.time(), time.monotonic()
    STATE.runner_deadline = now_mono + (float(cfg["runner_deadline"]) - now_wall)
    STATE.cleanup_deadline = now_mono + (float(cfg["cleanup_deadline"]) - now_wall)
    if not (now_mono < STATE.runner_deadline < STATE.cleanup_deadline):
        log(event="startup-failed", reason="deadlines are not in the future and ordered")
        return 2
    try:
        actor, server_now, cookies = login(cfg["username"], password)
    except Exception as e:  # noqa: BLE001
        log(event="startup-failed", reason="login", error=type(e).__name__, detail=str(e)[:200])
        return 2
    password = ""
    UPSTREAM = Upstream(cookies)
    with STATE.lock:
        STATE.actor_id, STATE.run_start = actor, server_now
    try:
        startup_inventory()
    except Exception as e:  # noqa: BLE001
        log(event="startup-failed", reason="inventory", error=type(e).__name__)
        return 0 if logout() else 4

    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    old = os.umask(0o177)
    try:
        server = Server(SOCKET_PATH)
    finally:
        os.umask(old)
    done = threading.Event()

    def stop(*_):
        if not done.is_set():
            done.set()
            STATE.closed = True
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def watchdog():
        while not done.wait(0.5):
            if time.monotonic() >= STATE.cleanup_deadline:
                log(event="deadline", which="cleanup")
                stop()

    threading.Thread(target=watchdog, daemon=True).start()
    sys.stdout.write("READY " + json.dumps({"actor": actor, "runStart": server_now.isoformat(),
                                            "socket": SOCKET_PATH}) + "\n")
    sys.stdout.flush()
    server.serve_forever(poll_interval=0.5)
    server.server_close()
    closed = logout()
    try:
        os.unlink(SOCKET_PATH)
    except OSError:
        pass
    return 0 if closed else 4


if __name__ == "__main__":
    sys.exit(main())
