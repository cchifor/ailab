#!/usr/bin/env python3
"""A bounded Relay session for the automated dev-worker-1 Pi canary phase A (plans/2026-11-27-relay-operator-session-plan.md).

Started ONLY by `scripts/relay-operator.py session`, inside a pinned python image, as a hardened container. It logs in
ONCE as the just-in-time automation identity (relay-admin-call.py's Keycloak flow), holds that one Relay session in
memory, and serves a fixed allowlist of Relay API calls on a Unix socket that only the Runner container (run with
--network none) shares. The Relay control lease binds actor.userId AND the login session, so one session must live for
the whole run; nothing else about it is general-purpose.

    stdin line 1: the password; line 2: JSON {"username": EMAIL, "runner_deadline": EPOCH, "cleanup_deadline": EPOCH}
    stdout: one line "READY {...}" once logged in (admin confirmed) and the startup inventory is taken
    stderr: JSON evidence lines (fixed reason codes, route names and validated IDs only; never bodies, raw targets,
            cookies, tokens or response content)
    exit:   0 logout confirmed, 4 logout not confirmed (or the session never closed), 2 startup refused before login

Scope is pinned in this file (SCOPE). Request bodies are rebuilt from validated fields; client headers are never
forwarded. Dynamic IDs are usable only once this run's own responses produced them (provenance rules in learn_*).
After runner_deadline only cleanup-shaped calls pass (rechecked at dispatch); at cleanup_deadline everything is refused
and the proxy logs out.

TWO sockets: proxy.sock serves every allowed call except the control lease; control.sock serves ONLY control-lease
acquire/renew/release (POST /api/agent-sessions/:id/control, DELETE .../control/:lease). Each has its own bounded reader
pool (an absolute deadline per request, fired by one shared deadline thread) and its own handler slots, so nothing on
the general socket can starve lease renewal. Upstream: DNS is resolved off the request path; connect, TLS, send and
the whole response run under one absolute deadline, and nothing is sent once it has expired.
"""
from __future__ import annotations

import http.client
import hashlib
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
FINALIZATION_PURPOSE = "canary-finalization"
FINALIZATION_BINDINGS = frozenset((*BINDINGS, "a8f6211c-d7e2-4ffd-824d-b158e349a8d4"))
FINALIZATION_PROFILES = frozenset((*PROFILES, "fdb56ca5-908b-40ee-94b3-aac735dd8622"))
THOUGHT_LEVELS = {"low", "high"}
EXPECTED_NAME = "relay-operator (automation)"
UA = "relay-session-proxy/1 (ailab operator automation)"

SOCKET_PATH = "/run/relay-session/proxy.sock"  # every allowed call except the control lease
CONTROL_SOCKET_PATH = "/run/relay-session/control.sock"  # ONLY control-lease acquire/renew/release
MAX_HEAD = 16 * 1024
MAX_BODY = 64 * 1024
MAX_PROMPT_BYTES = 16 * 1024  # UTF-8 bytes: the server's own limit (control.ts rejects Buffer.byteLength > 16384)
MAX_RESPONSE = 8 * 1024 * 1024
REQUEST_DEADLINE = 5.0  # absolute: the whole request (head + body) must arrive within this
UPSTREAM_CAP = 30.0
STARTUP_INVENTORY_BUDGET = 15.0
LOGIN_BUDGET = 60.0
LOGOUT_BUDGET = 20.0
DRAIN_ON_SHUTDOWN = 5.0  # drain + logout = 25 s, inside the orchestrator's 45 s docker-stop grace
GENERAL_READERS, GENERAL_SLOTS = 24, 6  # the general socket: connections being read, then concurrent handlers
CONTROL_READERS, CONTROL_SLOTS = 4, 2  # the control socket: its own pool, so renewal never starves
ROUTE_WORDS = {  # the only path words ever logged (anything else becomes "*")
    "api", "auth", "me", "logout", "agent-sessions", "agent-launches", "agent-profiles", "agent-permissions",
    "router-credentials", "keys", "rotate", "revoke", "conversations", "sessions", "timeline", "control", "commands",
    "operations", "settings", "permissions", "router", "communication", "native-recovery", "resume", "decision",
    "connections", "usage", "retry", "grants"}

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
UUID_RE = re.compile(r"^" + UUID + r"$")
ISO_CURSOR = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])[ T]([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d{1,6})?"
                        r"([+-]\d{2}(:?\d{2})?|Z)\|" + UUID + r"$")
INT_MAX = 2147483647
SAFE_MAX = 9007199254740991


class Refuse(Exception):
    """A request outside the allowlist or its schema: answered locally, never sent upstream. `code` is a fixed string
    (the only thing logged); `detail` may name client-supplied keys and goes only into the HTTP answer."""

    def __init__(self, code: str, status: int = 403, detail: str = ""):
        super().__init__(code)
        self.code, self.status, self.detail = code, status, detail


def log(**kv) -> None:
    kv.setdefault("t", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    try:
        sys.stderr.write(json.dumps(kv, sort_keys=True) + "\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def safe_route(path: str) -> str:
    """A loggable shape of a client path: words from the fixed ROUTE_WORDS vocabulary and ':id' for UUIDs; anything
    else (including any client-chosen word) becomes '*'. No query, at most 8 segments."""
    segs = []
    for s in path.split("?", 1)[0].split("/")[1:9]:
        segs.append(":id" if UUID_RE.match(s) else (s if s in ROUTE_WORDS else "*"))
    return "/" + "/".join(segs)


class Deadlines:
    """ONE thread fires every absolute deadline (request reads and upstream calls): no per-connection Timer threads,
    so the proxy stays far below the container's pids limit."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items: dict[int, tuple[float, object]] = {}
        self.seq = 0
        self.thread = None

    def add(self, when: float, fn) -> int:
        with self.lock:
            if self.thread is None:
                self.thread = threading.Thread(target=self._loop, daemon=True)
                self.thread.start()
            self.seq += 1
            self.items[self.seq] = (when, fn)
            return self.seq

    def cancel(self, key: int | None) -> None:
        if key is not None:
            with self.lock:
                self.items.pop(key, None)

    def _loop(self):
        while True:
            time.sleep(0.05)
            now, due = time.monotonic(), []
            with self.lock:
                for k, (w, fn) in list(self.items.items()):
                    if now >= w:
                        due.append(fn)
                        del self.items[k]
            for fn in due:
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass


DEADLINES = Deadlines()


class Resolver:
    """DNS off the request path: resolved at startup (a failure there fails startup) and refreshed in the background;
    a request never blocks on getaddrinfo, which no socket timeout can bound."""

    def __init__(self):
        self.lock = threading.Lock()
        self.addrs: list = []

    def refresh(self) -> None:
        infos = socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)
        addrs = [(fam, sa) for fam, _, _, _, sa in infos]
        if addrs:
            with self.lock:
                self.addrs = addrs

    def start(self) -> None:
        self.refresh()

        def loop():
            while True:
                time.sleep(60)
                try:
                    self.refresh()
                except OSError:
                    pass  # keep the last good answer

        threading.Thread(target=loop, daemon=True).start()

    def get(self) -> list:
        with self.lock:
            return list(self.addrs)


RESOLVER = Resolver()


# ---------------------------------------------------------------- strict JSON / schema helpers
def _no_dupes(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise Refuse("duplicate-json-key", 400)
        d[k] = v
    return d


def parse_body(raw: bytes) -> dict:
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dupes)
    except Refuse:
        raise
    except (UnicodeDecodeError, ValueError):
        raise Refuse("body-not-json", 400)
    if not isinstance(obj, dict):
        raise Refuse("body-not-object", 400)
    return obj


def keys(body: dict, required: set, optional: set = frozenset()) -> None:
    extra = set(body) - required - set(optional)
    missing = required - set(body)
    if extra or missing:
        raise Refuse("body-keys", 400, f"missing {sorted(missing)}, unexpected {sorted(extra)[:5]}")


def uuid_of(v, what: str) -> str:
    if not isinstance(v, str) or not UUID_RE.match(v):
        raise Refuse("not-a-uuid", 400, what)
    return v


def int_of(v, what: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise Refuse("bad-integer", 400, what)
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
        self.purpose = "phase-a"
        self.historical_sessions: frozenset[str] = frozenset()  # cleanup GET-only, never learned control ownership
        self.revoked: set[str] = set()
        self.runner_deadline = float("inf")  # monotonic
        self.cleanup_deadline = float("inf")
        self.closed = False
        self.inflight: dict[int, dict] = {}  # dispatch id -> {route, op, mutation}
        self.inflight_seq = 0


STATE = State()


def phase() -> str:
    now = time.monotonic()
    cutoff = STATE.runner_deadline if STATE.purpose == FINALIZATION_PURPOSE else STATE.cleanup_deadline
    if STATE.closed or now >= cutoff:
        return "closed"
    return "cleanup" if now >= STATE.runner_deadline else "run"


def reserve(op: str, record: dict, once: tuple[str, str] | None = None) -> None:
    """ONE locked section: the operationId is fresh (this run and the startup inventory) AND, for once-only routes,
    the binding/permission is unused; both are then recorded together. Call only after full validation."""
    with STATE.lock:
        if op in STATE.ops or op in STATE.startup_ops:
            raise Refuse("operation-id-reused")
        if once is not None:
            bucket = {"rotated": STATE.rotated, "decided": STATE.decided, "revoked": STATE.revoked}[once[0]]
            if once[1] in bucket:
                raise Refuse("already-used-in-this-run")
            bucket.add(once[1])
        STATE.ops[op] = record


def learned_session(sid: str, *, allow_predecessor: bool) -> str:
    if sid == PREDECESSOR_SESSION:
        if allow_predecessor:
            return sid
        raise Refuse("predecessor-read-only")
    with STATE.lock:
        if sid not in STATE.sessions:
            raise Refuse("session-not-learned")
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
            log(event="learn-refused", reason="predates-run", launch=row["id"])
            return
        same_profile = (row.get("profile_id"), row.get("profile_revision"), row.get("cwd")) == (
            intent["profile_id"], intent["revision"], intent["cwd"])
        if intent["kind"] == "launch":
            ok = same_profile
        else:  # resume: the named source session/generation AND the source launch's profile/revision/cwd
            ok = same_profile and (row.get("resume_from"), row.get("resume_generation")) == (
                intent["sid"], intent["generation"])
        if not ok:
            log(event="learn-refused", reason="intent-mismatch", launch=row["id"])
            return
        entry = STATE.launches.setdefault(row["id"], {
            "profile_id": intent["profile_id"], "revision": intent["revision"], "cwd": intent["cwd"],
            "session_id": None})
        sid = row.get("session_id")
        if isinstance(sid, str) and UUID_RE.match(sid) and entry["session_id"] in (None, sid) \
                and sid != PREDECESSOR_SESSION:
            entry["session_id"] = sid
            STATE.sessions.setdefault(sid, row["id"])


def learn_lease(sid: str, row) -> None:
    if isinstance(row, dict) and isinstance(row.get("id"), str) and UUID_RE.match(row["id"]) \
            and isinstance(row.get("generation"), int) and not isinstance(row.get("generation"), bool):
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
            allowed = FINALIZATION_BINDINGS if STATE.purpose == FINALIZATION_PURPOSE else BINDINGS
            if isinstance(r, dict) and r.get("binding_id") in allowed and isinstance(r.get("id"), str) \
                    and UUID_RE.match(r["id"]):
                STATE.credentials[r["id"]] = r["binding_id"]


# ---------------------------------------------------------------- the allowlist
class Plan:
    """What to send upstream (rebuilt from validated values), how to learn from the answer, and whether it may still be
    dispatched in the cleanup phase (rechecked immediately before dispatch)."""

    def __init__(self, route, method, path, query=None, body=None, learn=None, mutation=False, op=None, ids=None,
                 cleanup_ok=False):
        self.route, self.method, self.path, self.query = route, method, path, query or []
        self.body, self.learn, self.mutation, self.op, self.ids = body, learn, mutation, op, ids or {}
        self.cleanup_ok = cleanup_ok or method == "GET"


def _q(query: list, allowed: dict) -> list:
    """Only the listed keys, each at most once, each value validated."""
    seen, out = set(), []
    for k, v in query:
        if k not in allowed:
            raise Refuse("query-key-not-allowed", 403, k[:40])
        if k in seen:
            raise Refuse("query-key-repeated", 403, k[:40])
        seen.add(k)
        out.append((k, allowed[k](v)))
    return out


def _after(v):
    if not re.fullmatch(r"\d{1,15}", v):
        raise Refuse("bad-after", 400)
    return v


def _limit(v):
    if not re.fullmatch(r"\d{1,3}", v) or not 1 <= int(v) <= 200:
        raise Refuse("bad-limit", 400)
    return v


def _cursor(v):
    if not ISO_CURSOR.match(v):
        raise Refuse("bad-cursor", 400)
    return v


SESSION_READS = {"", "/router", "/communication", "/control", "/settings", "/permissions"}


def configure_finalization(scope: dict) -> None:
    """Only the trusted launcher supplies this prevalidated scope on stdin, never a Runner request."""
    if not isinstance(scope, dict):
        raise Refuse("finalization-scope")
    keys(scope, {"tenantId", "hostId", "connectionId", "executionIdentity", "bindingIds", "profileIds", "sessionIds"})
    if (scope["tenantId"] != "00000000-0000-4000-8000-000000000001" or
            scope["hostId"] != "b3b1be1a-275c-4a2e-b515-428b39c43171" or
            scope["connectionId"] != "babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8" or scope["executionIdentity"] != "c4"):
        raise Refuse("finalization-scope")
    for field, expected in (("bindingIds", FINALIZATION_BINDINGS), ("profileIds", FINALIZATION_PROFILES)):
        rows = scope[field]
        if not isinstance(rows, list) or len(rows) != len(expected) or any(not isinstance(v, str) for v in rows) \
                or frozenset(rows) != expected:
            raise Refuse("finalization-scope")
    sessions = scope["sessionIds"]
    if not isinstance(sessions, list) or not 1 <= len(sessions) <= 64:
        raise Refuse("finalization-scope")
    for sid in sessions:
        uuid_of(sid, "historical session")
    if len(set(sessions)) != len(sessions) or PREDECESSOR_SESSION not in sessions:
        raise Refuse("finalization-scope")
    STATE.purpose = FINALIZATION_PURPOSE
    STATE.historical_sessions = frozenset(sessions)


def learn_finalization_key(credential: str, value) -> None:
    """Remember original operation IDs without manufacturing an operation endpoint or response."""
    if not isinstance(value, dict) or not isinstance(value.get("credential"), dict):
        return
    row, ops = value["credential"], value.get("operations")
    with STATE.lock:
        if row.get("id") != credential or row.get("bindingId") != STATE.credentials.get(credential):
            return
        if isinstance(ops, list):
            STATE.startup_ops.update(o["id"] for o in ops if isinstance(o, dict)
                                     and isinstance(o.get("id"), str) and UUID_RE.fullmatch(o["id"]))


def plan_finalization(method: str, path: str, query: list, body) -> Plan:
    """A separate closed allowlist: no inference/rotation/control route can fall through to phase A."""
    q = _q(query, {})
    if method == "GET" and path == "/api/router-credentials":
        return Plan("credentials", method, path, q, learn=learn_credentials)
    m = re.fullmatch(r"/api/router-credentials/keys/(" + UUID + ")", path)
    if method == "GET" and m:
        credential = m.group(1)
        with STATE.lock:
            if credential not in STATE.credentials:
                raise Refuse("credential-not-learned")
        return Plan("credential-key", method, path, q, ids={"cred": credential},
                    learn=lambda value: learn_finalization_key(credential, value))
    m = re.fullmatch(r"/api/agent-sessions/(" + UUID + r")(/control)?", path)
    if method == "GET" and m:
        if m.group(1) not in STATE.historical_sessions:
            raise Refuse("historical-session-out-of-scope")
        return Plan("session" + (m.group(2) or ""), method, path, q, ids={"session": m.group(1)})
    m = re.fullmatch(r"/api/router-credentials/(" + UUID + ")/revoke", path)
    if method == "POST" and m:
        binding = m.group(1)
        if binding not in FINALIZATION_BINDINGS:
            raise Refuse("binding-out-of-scope")
        if not isinstance(body, dict):
            raise Refuse("body-not-object", 400)
        keys(body, {"operationId", "revision"})
        op, rev = uuid_of(body["operationId"], "operationId"), int_of(body["revision"], "revision", 1, SAFE_MAX)
        rebuilt = {"operationId": op, "revision": rev}
        digest = hashlib.sha256(json.dumps(rebuilt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        reserve(op, {"route": "revoke", "binding": binding, "bodySha256": digest}, once=("revoked", binding))
        return Plan("revoke", method, path, q, rebuilt, mutation=True, op=op, ids={"binding": binding})
    raise Refuse("finalization-route-not-allowed")


def plan_request(method: str, path: str, query: list, body: dict | None) -> Plan:
    ph = phase()
    if ph == "closed":
        raise Refuse("session-closed")
    cleanup = ph == "cleanup"
    if method == "GET" and body is not None:
        raise Refuse("get-with-body", 400)
    if STATE.purpose == FINALIZATION_PURPOSE:
        return plan_finalization(method, path, query, body)

    # ---- credentials
    if path == "/api/router-credentials" and method == "GET":
        return Plan("credentials", method, path, _q(query, {}), learn=lambda d: learn_credentials(d))
    m = re.fullmatch(r"/api/router-credentials/keys/(" + UUID + ")", path)
    if m and method == "GET":
        with STATE.lock:
            if m.group(1) not in STATE.credentials:
                raise Refuse("credential-not-learned")
        return Plan("credential-key", method, path, _q(query, {}), ids={"cred": m.group(1)})
    m = re.fullmatch(r"/api/router-credentials/(" + UUID + ")/rotate", path)
    if m and method == "POST":
        binding = m.group(1)
        if binding not in BINDINGS:
            raise Refuse("binding-out-of-scope")
        if cleanup:
            raise Refuse("cleanup-phase")
        keys(body, {"operationId", "revision"})
        op = uuid_of(body["operationId"], "operationId")
        rev = int_of(body["revision"], "revision", 1, SAFE_MAX)
        q = _q(query, {})
        reserve(op, {"route": "rotate", "binding": binding}, once=("rotated", binding))
        return Plan("rotate", method, path, q, {"operationId": op, "revision": rev},
                    learn=lambda d: learn_credentials(d), mutation=True, op=op, ids={"binding": binding})

    # ---- profiles and launches
    if path == "/api/agent-profiles" and method == "GET":
        return Plan("profiles", method, path, _q(query, {}))
    if path == "/api/agent-launches" and method == "GET":
        return Plan("launches", method, path, _q(query, {}),
                    learn=lambda d: [learn_launch(r) for r in d] if isinstance(d, list) else None)
    if path == "/api/agent-launches" and method == "POST":
        if cleanup:
            raise Refuse("cleanup-phase")
        keys(body, {"operationId", "profileId", "revision", "cwd"})
        op = uuid_of(body["operationId"], "operationId")
        pid = uuid_of(body["profileId"], "profileId")
        if pid not in PROFILES:
            raise Refuse("profile-out-of-scope")
        rev, cwd, _ = PROFILES[pid]
        if isinstance(body["revision"], bool) or body["revision"] != rev:
            raise Refuse("profile-revision-not-pinned")
        if body["cwd"] != cwd:
            raise Refuse("cwd-not-approved")
        q = _q(query, {})
        reserve(op, {"route": "launch", "profile": pid})
        with STATE.lock:
            STATE.launch_intents[op] = {"kind": "launch", "profile_id": pid, "revision": rev, "cwd": cwd}
        return Plan("launch", method, path, q, {"operationId": op, "profileId": pid, "revision": rev, "cwd": cwd},
                    learn=lambda d: learn_launch(d), mutation=True, op=op, ids={"profile": pid})

    # ---- permissions by id
    m = re.fullmatch(r"/api/agent-permissions/(" + UUID + ")(/decision)?", path)
    if m:
        pid, decision = m.group(1), bool(m.group(2))
        if not decision and method == "GET":
            if pid != PREDECESSOR_PERMISSION:
                with STATE.lock:
                    if pid not in STATE.permissions:
                        raise Refuse("permission-not-learned")
            return Plan("permission", method, path, _q(query, {}), ids={"permission": pid})
        if decision and method == "POST":
            with STATE.lock:
                sid = STATE.permissions.get(pid)
            if sid is None:
                raise Refuse("permission-not-learned")
            keys(body, {"operationId", "generation", "outcome"})
            op = uuid_of(body["operationId"], "operationId")
            gen = int_of(body["generation"], "generation", 1, SAFE_MAX)
            out = body["outcome"]
            if not isinstance(out, dict) or out.get("outcome") not in ("selected", "cancelled"):
                raise Refuse("bad-outcome", 400)
            if out["outcome"] == "selected":
                keys(out, {"outcome", "optionId"})
                if not isinstance(out["optionId"], str) or not 1 <= len(out["optionId"]) <= 200:
                    raise Refuse("bad-option-id", 400)
                if cleanup:
                    raise Refuse("cleanup-phase")
                outcome, cleanup_ok = {"outcome": "selected", "optionId": out["optionId"]}, False
            else:
                keys(out, {"outcome"})
                outcome, cleanup_ok = {"outcome": "cancelled"}, True
            q = _q(query, {})
            reserve(op, {"route": "decision", "sid": sid, "permission": pid}, once=("decided", pid))
            return Plan("decision", method, path, q, {"operationId": op, "generation": gen, "outcome": outcome},
                        mutation=True, op=op, ids={"permission": pid, "session": sid}, cleanup_ok=cleanup_ok)

    # ---- timeline
    m = re.fullmatch(r"/api/conversations/sessions/(" + UUID + ")/timeline", path)
    if m and method == "GET":
        sid = learned_session(m.group(1), allow_predecessor=True)
        return Plan("timeline", method, path, _q(query, {"after": _after, "limit": _limit, "promptsAfter": _cursor}),
                    ids={"session": sid})

    # ---- one session
    m = re.fullmatch(r"/api/agent-sessions/(" + UUID + r")((?:/[a-z-]+)?)(?:/(" + UUID + r"))?", path)
    if not m:
        raise Refuse("route-not-allowed")
    sid, sub, tail = m.group(1), m.group(2), m.group(3)

    if method == "GET" and tail is None and sub in SESSION_READS:
        learned_session(sid, allow_predecessor=True)
        learn = (lambda d, s=sid: learn_permissions(s, d)) if sub == "/permissions" and sid != PREDECESSOR_SESSION \
            else None
        return Plan("session" + (sub or ""), method, path, _q(query, {}), learn=learn, ids={"session": sid})
    if method == "GET" and sub == "/operations" and tail:
        learned_session(sid, allow_predecessor=True)
        if sid != PREDECESSOR_SESSION:
            with STATE.lock:
                rec = STATE.ops.get(tail)
            if not rec or rec.get("sid") != sid:
                raise Refuse("operation-not-this-run")
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
                raise Refuse("lease-not-learned")
            gen = int_of(body["generation"], "generation", 1, SAFE_MAX)
            if gen != known["generation"]:
                raise Refuse("lease-generation-changed")
            out, cleanup_ok = {"generation": gen, "leaseId": lease}, True
        else:
            if cleanup:
                raise Refuse("cleanup-phase")
            keys(body, {"generation"})
            out, cleanup_ok = {"generation": int_of(body["generation"], "generation", 1, SAFE_MAX)}, False
        return Plan("control", method, path, _q(query, {}), out, learn=lambda d, s=sid: learn_lease(s, d),
                    mutation=True, ids={"session": sid}, cleanup_ok=cleanup_ok)
    if method == "DELETE" and sub == "/control" and tail:
        learned_session(sid, allow_predecessor=False)
        with STATE.lock:
            known = STATE.leases.get(tail)
        if not known or known["sid"] != sid:
            raise Refuse("lease-not-learned")
        return Plan("control-release", method, path, _q(query, {}), mutation=True, ids={"session": sid, "lease": tail},
                    cleanup_ok=True)
    if method == "POST" and sub == "/commands" and tail is None:
        learned_session(sid, allow_predecessor=False)
        return _command(sid, body, cleanup, query)
    if method == "POST" and sub == "/resume" and tail is None:
        learned_session(sid, allow_predecessor=False)
        if cleanup:
            raise Refuse("cleanup-phase")
        keys(body, {"operationId", "generation"}, {"acknowledgeUnknownToolEffects"})
        if body.get("acknowledgeUnknownToolEffects", False) is not False:
            raise Refuse("acknowledgement-never-sent")
        op = uuid_of(body["operationId"], "operationId")
        gen = int_of(body["generation"], "generation", 1, INT_MAX)
        q = _q(query, {})
        with STATE.lock:
            src = STATE.launches[STATE.sessions[sid]]
        reserve(op, {"route": "resume", "sid": sid})
        with STATE.lock:
            STATE.launch_intents[op] = {"kind": "resume", "sid": sid, "generation": gen,
                                        "profile_id": src["profile_id"], "revision": src["revision"],
                                        "cwd": src["cwd"]}
        return Plan("resume", method, path, q, {"operationId": op, "generation": gen},
                    learn=lambda d: learn_launch(d), mutation=True, op=op, ids={"session": sid})
    raise Refuse("route-not-allowed")


def _command(sid: str, body: dict, cleanup: bool, query: list) -> Plan:
    kind = body.get("kind")
    common = {"operationId", "leaseId", "generation", "kind"}
    if kind == "prompt":
        keys(body, common | {"controlEpoch", "text"})
        if not isinstance(body["text"], str) or not body["text"] or \
                len(body["text"].encode("utf-8")) > MAX_PROMPT_BYTES:
            raise Refuse("bad-prompt-text", 400)
        extra = {"controlEpoch": int_of(body["controlEpoch"], "controlEpoch", 0, SAFE_MAX), "text": body["text"]}
    elif kind == "set-config":
        keys(body, common | {"controlEpoch", "settingsRevision", "configId", "value"})
        if body["configId"] != "thought_level" or body["value"] not in THOUGHT_LEVELS:
            raise Refuse("set-config-not-allowed")
        extra = {"controlEpoch": int_of(body["controlEpoch"], "controlEpoch", 0, SAFE_MAX),
                 "settingsRevision": int_of(body["settingsRevision"], "settingsRevision", 1, SAFE_MAX),
                 "configId": "thought_level", "value": body["value"]}
    elif kind in ("cancel", "close"):
        keys(body, common)
        extra = {}
    else:
        raise Refuse("command-kind-not-allowed")
    cleanup_ok = kind in ("cancel", "close")
    if cleanup and not cleanup_ok:
        raise Refuse("cleanup-phase")
    op = uuid_of(body["operationId"], "operationId")
    lease = uuid_of(body["leaseId"], "leaseId")
    with STATE.lock:
        known = STATE.leases.get(lease)
    if not known or known["sid"] != sid:
        raise Refuse("lease-not-learned")
    gen = int_of(body["generation"], "generation", 1, SAFE_MAX)
    q = _q(query, {})
    reserve(op, {"route": "command", "sid": sid, "kind": kind})
    out = {"operationId": op, "leaseId": lease, "generation": gen, "kind": kind, **extra}
    return Plan("command-" + kind, "POST", f"/api/agent-sessions/{sid}/commands", q, out,
                mutation=True, op=op, ids={"session": sid}, cleanup_ok=cleanup_ok)


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

    def _open(self, end: float, holder: dict, expired: threading.Event):
        """TCP connect (to the pre-resolved addresses) and the TLS handshake, all under the absolute deadline: the
        socket exists BEFORE connect, so the deadline's shutdown interrupts a hanging connect or handshake too."""
        last = None
        for fam, addr in RESOLVER.get():
            left = end - time.monotonic()
            if left <= 0 or expired.is_set():
                break
            s = socket.socket(fam, socket.SOCK_STREAM)
            holder["sock"] = s
            s.settimeout(left)
            try:
                s.connect(addr)
            except OSError as e:
                last = e
                s.close()
                continue
            if expired.is_set():
                s.close()
                break
            s.settimeout(max(0.05, end - time.monotonic()))
            tls = ssl.create_default_context().wrap_socket(s, server_hostname=HOST)
            holder["sock"] = tls
            return tls
        raise TimeoutError("upstream connect exceeded the deadline") if expired.is_set() or \
            time.monotonic() >= end else (last or OSError("no upstream address"))

    def call(self, method: str, path: str, query: list, body: dict | None, budget: float):
        """One request under an ABSOLUTE deadline, from connect to the last body byte: the shared deadline thread
        shuts the transport down when the budget is spent, and nothing is sent once it has expired."""
        budget = min(budget, UPSTREAM_CAP)
        if budget <= 0.05:
            raise TimeoutError("no time left before the deadline")
        end = time.monotonic() + budget
        url = path + ("?" + urllib.parse.urlencode(query) if query else "")
        payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = {"User-Agent": UA, "Accept": "application/json", "Cookie": self.cookie_header()}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        if method != "GET":
            headers["Origin"] = ORIGIN
        # An explicit Host: HTTPConnection(HOST, 443) has default_port 80 and would send "relay.chifor.me:443", which
        # differs from the Origin above; http.client omits its own Host when one is supplied.
        headers["Host"] = HOST
        expired = threading.Event()
        holder = {}

        def cut():
            expired.set()
            s = holder.get("sock")
            if s is not None:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        key = DEADLINES.add(end, cut)
        conn = http.client.HTTPConnection(HOST, 443)  # the transport below is already TLS-wrapped
        try:
            sock = self._open(end, holder, expired)
            if expired.is_set() or time.monotonic() >= end:  # never send after the deadline
                raise TimeoutError("deadline reached before sending")
            sock.settimeout(max(0.05, end - time.monotonic()))
            conn.sock = sock
            conn.request(method, url, body=payload, headers=headers)
            resp = conn.getresponse()
            self.absorb(resp.headers)
            chunks, size = [], 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise ValueError("upstream response exceeds 8 MiB")
                chunks.append(chunk)
                if time.monotonic() >= end:
                    raise TimeoutError("upstream response exceeded the deadline")
            if expired.is_set():
                raise TimeoutError("upstream response exceeded the deadline")
            return resp.status, resp.getheader("Content-Type") or "application/json", b"".join(chunks), resp.headers
        except (OSError, http.client.HTTPException, ValueError) as e:
            if expired.is_set():
                raise TimeoutError("upstream deadline") from e
            raise
        finally:
            DEADLINES.cancel(key)
            conn.close()


UPSTREAM: Upstream | None = None


# ---------------------------------------------------------------- the socket server
class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False
    block_on_close = False
    request_queue_size = 64  # the default 5 overflows (EAGAIN on connect) when the Runner opens a burst

    def __init__(self, path, *, control: bool):
        super().__init__(path, Handler)
        self.control_socket = control  # the control socket serves ONLY control-lease routes, the general one never
        readers, slots = (CONTROL_READERS, CONTROL_SLOTS) if control else (GENERAL_READERS, GENERAL_SLOTS)
        self.readers = threading.BoundedSemaphore(readers)  # stage 1: connections being read
        self.slots = threading.BoundedSemaphore(slots)  # stage 2: concurrent handlers

    def process_request(self, request, client_address):
        if not self.readers.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.readers.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address):
        key = None
        try:
            # The absolute request deadline: if the whole request has not arrived in time, the read side is shut down.
            key = DEADLINES.add(time.monotonic() + REQUEST_DEADLINE, lambda: _shut_read(request))
            REQUEST_KEYS[id(request)] = key
            super().process_request_thread(request, client_address)
        finally:
            DEADLINES.cancel(key)
            REQUEST_KEYS.pop(id(request), None)
            self.readers.release()


REQUEST_KEYS: dict[int, int] = {}


def _shut_read(sock) -> None:
    try:
        sock.shutdown(socket.SHUT_RD)
    except OSError:
        pass


def _is_control(path: str) -> bool:
    return bool(re.fullmatch(r"/api/agent-sessions/" + UUID + r"/control(/" + UUID + ")?", path))


class Handler(BaseHTTPRequestHandler):
    timeout = REQUEST_DEADLINE
    protocol_version = "HTTP/1.1"
    server_version = "relay-session-proxy"
    sys_version = ""

    def log_message(self, *args):  # the evidence log replaces the default access log
        pass

    def log_error(self, *args):
        pass

    def _reply(self, status: int, data: bytes, ctype: str = "application/json") -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass
        self.close_connection = True

    def _refuse(self, method, route, r: Refuse) -> None:
        log(event="refused", method=method, route=route, reason=r.code, status=r.status)
        self._reply(r.status, json.dumps({"error": "refused by relay-session-proxy", "reason": r.code,
                                          "detail": r.detail}).encode())

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (socket.timeout, TimeoutError, ConnectionError):
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
        route = safe_route(target) if target.startswith("/") else "*"
        head_size = len(self.requestline) + sum(len(k) + len(v) + 4 for k, v in self.headers.items())
        try:
            if head_size > MAX_HEAD:
                raise Refuse("head-too-large", 431)
            if not target.startswith("/") or "#" in target:
                raise Refuse("not-origin-form", 400)
            raw_path, _, raw_query = target.partition("?")
            if "%" in raw_path or ".." in raw_path or "//" in raw_path or "\\" in raw_path or ";" in raw_path:
                raise Refuse("unsafe-path", 400)
            try:
                query = urllib.parse.parse_qsl(raw_query, keep_blank_values=True, strict_parsing=bool(raw_query),
                                               max_num_fields=8)
            except ValueError:
                raise Refuse("malformed-query", 400)
            if self.headers.get("Transfer-Encoding"):
                raise Refuse("transfer-encoding", 400)
            lengths = self.headers.get_all("Content-Length") or []
            body = None
            if method == "POST":
                if len(lengths) != 1 or not lengths[0].isdigit():
                    raise Refuse("content-length", 411)
                n = int(lengths[0])
                if n > MAX_BODY:
                    raise Refuse("body-too-large", 413)
                raw = self.rfile.read(n)
                if len(raw) != n:
                    raise Refuse("short-body", 400)
                body = parse_body(raw)
            elif lengths and lengths != ["0"]:
                raise Refuse("body-not-allowed", 400)
            DEADLINES.cancel(REQUEST_KEYS.get(id(self.request)))  # the request has arrived; upstream has its own deadline
            if _is_control(raw_path) != self.server.control_socket:
                raise Refuse("use-the-control-socket" if _is_control(raw_path) else "control-socket-is-control-only")
            slot = self.server.slots
            if not slot.acquire(blocking=False):
                raise Refuse("busy", 503)
            try:
                plan = plan_request(method, raw_path, query, body)
                self._dispatch(plan)
            finally:
                slot.release()
        except Refuse as r:
            self._refuse(method, route, r)

    def _dispatch(self, plan: Plan) -> None:
        start = time.monotonic()
        rec = {"event": "call", "method": plan.method, "route": plan.route, "ids": plan.ids, "operationId": plan.op}
        ph = phase()  # rechecked immediately before dispatch: the phase may have changed since planning
        if ph == "closed" or (ph == "cleanup" and not plan.cleanup_ok):
            raise Refuse("session-closed" if ph == "closed" else "cleanup-phase")
        cutoff = (STATE.runner_deadline if STATE.purpose == FINALIZATION_PURPOSE else
                  STATE.cleanup_deadline if plan.cleanup_ok else STATE.runner_deadline)
        budget = cutoff - start
        if budget <= 0:
            raise Refuse("cleanup-phase")
        with STATE.lock:
            STATE.inflight_seq += 1
            key = STATE.inflight_seq
            STATE.inflight[key] = {"route": plan.route, "operationId": plan.op, "mutation": plan.mutation}
        try:
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
        finally:
            with STATE.lock:
                STATE.inflight.pop(key, None)


# ---------------------------------------------------------------- login, logout, lifecycle
def _load_admin_call():
    spec = importlib.util.spec_from_file_location("relay_admin_call", os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "relay-admin-call.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def login(username: str, password: str):
    """Returns (client, actor, server_now, cookies). On failure the exception carries the client (`.client`), so the
    caller can always log out a session that was opened."""
    rac = _load_admin_call()
    c = rac.Client(ORIGIN)
    try:
        rac.login(c, username, password)  # raises rac.Fail; checks admin + the expected name
        st, h, raw = c.request("GET", ORIGIN + "/api/auth/me")
        me = json.loads(raw)
        if st != 200 or me.get("name") != EXPECTED_NAME or me.get("role") != "admin":
            raise rac.Fail("/api/auth/me is not the admin automation identity")
        actor = me.get("userId")
        if not isinstance(actor, str) or not UUID_RE.match(actor):
            raise rac.Fail("/api/auth/me carries no userId")
        server_now = parsedate_to_datetime(h.get("Date")).astimezone(timezone.utc).replace(microsecond=0)
        cookies = {ck.name: ck.value for ck in c.jar if ck.domain.lstrip(".") == HOST}
        if "relay_session" not in cookies:
            raise rac.Fail("no relay_session cookie")
        return c, actor, server_now, cookies
    except BaseException as e:
        e.client = c  # type: ignore[attr-defined]
        raise


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
    if STATE.purpose == FINALIZATION_PURPOSE:
        budget = min(STARTUP_INVENTORY_BUDGET, STATE.runner_deadline - time.monotonic())
        if budget <= 0:
            raise RuntimeError("finalization cutoff reached")
        st, _, data, _ = UPSTREAM.call("GET", "/api/router-credentials", [], None, budget)
        value = json.loads(data) if st == 200 else None
        if not isinstance(value, dict) or not isinstance(value.get("credentials"), list):
            raise RuntimeError("startup credential inventory unavailable")
        learn_credentials(value)
        log(event="startup", purpose=FINALIZATION_PURPOSE, historical_sessions=len(STATE.historical_sessions))
        return
    st, _, data, _ = UPSTREAM.call("GET", "/api/agent-launches", [], None, STARTUP_INVENTORY_BUDGET)
    rows = json.loads(data) if st == 200 else None
    if not isinstance(rows, list):
        raise RuntimeError(f"startup launch inventory unavailable ({st})")
    with STATE.lock:
        STATE.startup_ops = {r["operation_id"] for r in rows if isinstance(r, dict)
                             and r.get("actor_id") == STATE.actor_id and isinstance(r.get("operation_id"), str)}
    log(event="startup", prior_launch_operations=len(STATE.startup_ops))


def drain_inflight(bound: float) -> None:
    """Wait (bounded) for dispatched calls; any mutation still outstanding is recorded as uncertain."""
    end = time.monotonic() + bound
    while time.monotonic() < end:
        with STATE.lock:
            if not STATE.inflight:
                return
        time.sleep(0.1)
    with STATE.lock:
        left = list(STATE.inflight.values())
    for r in left:
        log(event="shutdown-outstanding", route=r["route"], operationId=r["operationId"],
            outcome="uncertain" if r["mutation"] else "abandoned")


def _bounded_login(username: str, password: str, stop_requested: threading.Event):
    """Login in a worker thread, abandoned on a signal or after LOGIN_BUDGET. Returns (result, exception, client):
    an abandoned login may still complete server-side; the orchestrator's revoke deletes every session of the
    identity in the database regardless, so a session this process never learned of cannot outlive the run."""
    box = {}

    def work():
        try:
            box["result"] = login(username, password)
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    end = time.monotonic() + LOGIN_BUDGET
    while t.is_alive() and not stop_requested.is_set() and time.monotonic() < end:
        t.join(0.2)
    if t.is_alive():
        return None, TimeoutError("login abandoned (signal or budget)"), None
    err = box.get("error")
    return box.get("result"), err, getattr(err, "client", None)


def _serve(path: str, control: bool) -> Server:
    if os.path.exists(path):
        os.unlink(path)
    old = os.umask(0o177)
    try:
        return Server(path, control=control)
    finally:
        os.umask(old)


def main() -> int:
    global UPSTREAM
    stop_requested = threading.Event()
    servers: list[Server] = []

    def stop(*_):
        stop_requested.set()
        STATE.closed = True
        for srv in list(servers):
            threading.Thread(target=srv.shutdown, daemon=True).start()

    # Installed before ANY authenticated step: a signal during login or startup still reaches the logout below.
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    password = sys.stdin.readline().rstrip("\r\n")
    cfg = json.loads(sys.stdin.readline())
    now_wall, now_mono = time.time(), time.monotonic()
    STATE.runner_deadline = now_mono + (float(cfg["runner_deadline"]) - now_wall)
    STATE.cleanup_deadline = now_mono + (float(cfg["cleanup_deadline"]) - now_wall)
    if not (now_mono < STATE.runner_deadline < STATE.cleanup_deadline):
        log(event="startup-failed", reason="deadlines")
        return 2
    try:
        if cfg.get("purpose", "phase-a") == FINALIZATION_PURPOSE:
            configure_finalization(cfg.get("cleanup_scope"))
            if STATE.runner_deadline - now_mono > 900:
                raise Refuse("finalization-deadline")
        elif cfg.get("purpose", "phase-a") != "phase-a" or "cleanup_scope" in cfg:
            raise Refuse("purpose-not-allowed")
    except Refuse:
        log(event="startup-failed", reason="purpose-or-scope")
        return 2
    try:
        RESOLVER.start()
    except OSError as e:
        log(event="startup-failed", reason="dns", error=type(e).__name__)
        return 2
    result, err, client = _bounded_login(cfg["username"], password, stop_requested)
    password = ""
    if err is not None:  # a session may have been opened: close it with the client's own jar
        log(event="startup-failed", reason="login", error=type(err).__name__)
        if client is not None and client.has_session():
            UPSTREAM = Upstream({ck.name: ck.value for ck in client.jar if ck.domain.lstrip(".") == HOST})
            return 0 if logout() else 4
        return 4 if isinstance(err, TimeoutError) else 2  # abandoned: closure unconfirmed (the DB revoke covers it)
    _client, actor, server_now, cookies = result
    UPSTREAM = Upstream(cookies)
    with STATE.lock:
        STATE.actor_id, STATE.run_start = actor, server_now
    closed = False
    try:
        if stop_requested.is_set():
            raise InterruptedError("stop requested during login")
        startup_inventory()
        if stop_requested.is_set():
            raise InterruptedError("stop requested during startup")
        servers.extend([_serve(SOCKET_PATH, control=False), _serve(CONTROL_SOCKET_PATH, control=True)])
        if stop_requested.is_set():
            stop()

        def watchdog():
            while not stop_requested.wait(0.5):
                if time.monotonic() >= STATE.cleanup_deadline:
                    log(event="deadline", which="cleanup")
                    stop()

        threading.Thread(target=watchdog, daemon=True).start()
        control_thread = threading.Thread(target=servers[1].serve_forever, kwargs={"poll_interval": 0.5},
                                          daemon=True)
        control_thread.start()
        sys.stdout.write("READY " + json.dumps({"actor": actor, "runStart": server_now.isoformat(),
                                                "socket": SOCKET_PATH, "controlSocket": CONTROL_SOCKET_PATH}) + "\n")
        sys.stdout.flush()
        servers[0].serve_forever(poll_interval=0.5)
        control_thread.join(timeout=2)
        for srv in servers:
            srv.server_close()
        drain_inflight(DRAIN_ON_SHUTDOWN)
    except BaseException as e:  # noqa: BLE001
        log(event="error", error=type(e).__name__)
    finally:
        STATE.closed = True
        closed = logout()
        for p in (SOCKET_PATH, CONTROL_SOCKET_PATH):
            try:
                os.unlink(p)
            except OSError:
                pass
    return 0 if closed else 4


if __name__ == "__main__":
    sys.exit(main())
