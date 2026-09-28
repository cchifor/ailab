#!/usr/bin/env python3
"""Mock-server tests for kubernetes/apps/apps/gitea/gitea-actions-run-retention.sh.

    python scripts/tests/gitea-actions-run-retention-mock.py

Starts a stdlib HTTP server that plays the Gitea API (org repos, completed-run listing id DESC with
offset pagination, GET run, DELETE run) AND Prometheus (/api/v1/query for the replication gate), runs
the script against it under a scenario, and asserts the exit code, which runs were deleted, the summary
line, and that the token only ever arrived in an Authorization header.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "kubernetes" / "apps" / "apps" / "gitea" / "gitea-actions-run-retention.sh"
BASH = next((b for b in (r"C:/Program Files/Git/usr/bin/bash.exe", r"C:/Program Files/Git/bin/bash.exe")
             if os.path.exists(b)), "sh")
TOKEN = "tok-SECRET-" + "9f3e" * 20
DAY = 86400
NOW = int(time.time())
RETENTION_DAYS = 16
OLD = NOW - 40 * DAY          # eligible
RECENT = NOW - 1 * DAY        # not eligible


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class State:
    def __init__(self, scenario):
        self.scenario = scenario
        self.repos = {}            # full_name -> {id: run}
        self.calls = []            # (method, path)
        self.deleted = []          # (repo, id)
        self.token_leak = False
        self.unauth = 0
        self.list_calls = 0
        self.firing = "0"
        self.prom_down = False
        self.lock = threading.Lock()

    def add(self, repo, rid, status="completed", completed=OLD):
        self.repos.setdefault(repo, {})[rid] = {"id": rid, "status": status,
                                                "completed_at": None if completed is None else iso(completed)}

    def completed_desc(self, repo):
        return sorted((r for r in self.repos.get(repo, {}).values() if r["status"] == "completed"),
                      key=lambda r: -r["id"])


class Handler(BaseHTTPRequestHandler):
    state: State = None

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body=None):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _auth_ok(self):
        if TOKEN in self.path:
            self.state.token_leak = True
        return self.headers.get("Authorization") == f"token {TOKEN}"

    def do_GET(self):
        st = self.state
        u = urlparse(self.path)
        q = parse_qs(u.query)
        with st.lock:
            st.calls.append(("GET", u.path))
            if u.path == "/api/v1/query":  # prometheus
                if st.prom_down:
                    return self._send(503, {"status": "error"})
                return self._send(200, {"status": "success", "data": {"resultType": "vector", "result":
                                        ([] if st.firing == "0" else [{"metric": {}, "value": [NOW, st.firing]}])}})
            if not self._auth_ok():
                st.unauth += 1
                return self._send(401, {"message": "token is required"})
            if st.scenario == "unauthorized":
                return self._send(403, {"message": "token does not have at least one of required scope(s)"})
            parts = u.path.split("/")
            if u.path.startswith("/api/v1/orgs/"):
                page = int(q.get("page", ["1"])[0]); limit = int(q.get("limit", ["50"])[0])
                names = sorted(n for n in st.repos if n.startswith("cchifor/"))
                return self._send(200, [{"full_name": n} for n in names[(page - 1) * limit: page * limit]])
            # /api/v1/repos/{owner}/{repo}/actions/runs[/{id}] -> ['', api, v1, repos, owner, repo, actions, runs, id?]
            if len(parts) >= 8 and parts[3] == "repos" and parts[6] == "actions" and parts[7] == "runs":
                repo = f"{parts[4]}/{parts[5]}"
                if len(parts) == 9 and parts[8].isdigit():  # GET run
                    run = st.repos.get(repo, {}).get(int(parts[8]))
                    if not run:
                        return self._send(404, {"message": "not found"})
                    if st.scenario == "rerun-before-delete" and run["id"] == 3:
                        run = dict(run, status="running", completed_at=None)
                    return self._send(200, run)
                # listing
                st.list_calls += 1
                if st.scenario == "shift" and st.list_calls % 2 == 0:
                    # a run completes between page reads: a new high id appears at the front
                    nid = max(st.repos[repo]) + 1
                    st.add(repo, nid, completed=RECENT)
                page = int(q.get("page", ["1"])[0]); limit = int(q.get("limit", ["50"])[0])
                runs = st.completed_desc(repo)
                return self._send(200, {"total_count": len(runs), "workflow_runs": runs[(page - 1) * limit: page * limit]})
            return self._send(404, {"message": "not found"})

    def do_DELETE(self):
        st = self.state
        u = urlparse(self.path)
        with st.lock:
            st.calls.append(("DELETE", u.path))
            if not self._auth_ok():
                st.unauth += 1
                return self._send(401, {"message": "token is required"})
            parts = u.path.split("/")
            repo = f"{parts[4]}/{parts[5]}"; rid = int(parts[8])
            run = st.repos.get(repo, {}).get(rid)
            if not run:
                return self._send(404, {"message": "not found"})
            if st.scenario == "delete-500":
                return self._send(500, {"message": "boom"})
            if run["status"] != "completed":
                return self._send(400, {"message": "this workflow run is not done"})
            del st.repos[repo][rid]
            st.deleted.append((repo, rid))
            return self._send(204)


def run_script(state, port, env_over):
    env = dict(os.environ)
    env.update({"GITEA_URL": f"http://127.0.0.1:{port}", "PROM_URL": f"http://127.0.0.1:{port}",
                "GITEA_TOKEN": TOKEN, "ORG": "cchifor", "RETENTION_DAYS": str(RETENTION_DAYS),
                "MAX_DELETES_PER_RUN": "100", "DELETE_PAUSE_SECONDS": "0", "MAX_PAGES_PER_REPO": "20",
                "GATE_EVERY": "2", "DRY_RUN": "false", "WORK": tempfile.mkdtemp(prefix="retention-")})
    env.update(env_over)
    p = subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout + p.stderr


def scenario(name, setup, env_over=None):
    state = State(name)
    setup(state)
    Handler.state = state
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    try:
        rc, out = run_script(state, srv.server_address[1], env_over or {})
    finally:
        srv.shutdown()
    assert not state.token_leak, f"{name}: token leaked into a URL"
    assert TOKEN not in out, f"{name}: token printed"
    return state, rc, out


def summary(out):
    line = [l for l in out.splitlines() if l.startswith("retention: deleted=")]
    assert line, f"no summary line in:\n{out}"
    return dict(kv.split("=", 1) for kv in line[-1].split()[1:])


def two_repos(st, n_old=120, n_recent=60):
    # platform: 120 old (ids 1..120) then 60 recent (121..180)  -> 4 pages, eligible at the tail
    for i in range(1, n_old + 1):
        st.add("cchifor/platform", i, completed=OLD)
    for i in range(n_old + 1, n_old + n_recent + 1):
        st.add("cchifor/platform", i, completed=RECENT)
    for i in range(1, 6):
        st.add("cchifor/ailab", i, completed=OLD)
    st.add("cchifor/ailab", 6, completed=RECENT)


def main():
    failures = 0

    def check(name, cond, detail=""):
        nonlocal failures
        print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
        if not cond:
            failures += 1

    # 1. basic: global budget across repos, oldest first, ineligible never deleted
    st, rc, out = scenario("basic", two_repos, {"MAX_DELETES_PER_RUN": "30"})
    s = summary(out)
    check("basic exit 0", rc == 0, out[-800:])
    check("basic deleted exactly 30", len(st.deleted) == 30 and s["deleted"] == "30", str(len(st.deleted)))
    check("basic only eligible ids", all(st.repos or True for _ in [0]) and all(rid <= 120 for r, rid in st.deleted if r.endswith("platform")) and all(rid <= 5 for r, rid in st.deleted if r.endswith("ailab")))
    check("basic deferred reported", s["repos_deferred"] != "none")
    check("basic used Authorization header", st.unauth == 0)

    # 2. everything eligible drains fully with page re-reads (deletions shift pages)
    st, rc, out = scenario("drain", two_repos, {"MAX_DELETES_PER_RUN": "500"})
    s = summary(out)
    check("drain exit 0", rc == 0, out[-500:])
    check("drain deleted all 125 old", len(st.deleted) == 125, str(len(st.deleted)))
    check("drain recent untouched", len(st.completed_desc("cchifor/platform")) == 60 and len(st.completed_desc("cchifor/ailab")) == 1)

    # 3. completions arrive between page reads: no crash, nothing ineligible deleted, progress made
    st, rc, out = scenario("shift", two_repos, {"MAX_DELETES_PER_RUN": "500"})
    check("shift exit 0", rc == 0, out[-500:])
    check("shift nothing recent deleted", all(rid <= 120 for r, rid in st.deleted if r.endswith("platform")))
    check("shift made progress", len(st.deleted) >= 100, str(len(st.deleted)))

    # 4. ineligible tail page (reruns) followed by eligible pages: NO early stop
    def tail_rerun(st):
        for i in range(1, 51):            # ids 1..50 = last page, all rerun recently
            st.add("cchifor/platform", i, completed=RECENT)
        for i in range(51, 101):          # ids 51..100 = eligible
            st.add("cchifor/platform", i, completed=OLD)
        for i in range(101, 111):
            st.add("cchifor/platform", i, completed=RECENT)
    st, rc, out = scenario("tail-ineligible", tail_rerun, {"MAX_DELETES_PER_RUN": "500"})
    check("tail-ineligible exit 0", rc == 0, out[-500:])
    check("tail-ineligible deleted the 50 eligible", sorted(rid for _, rid in st.deleted) == list(range(51, 101)), str(len(st.deleted)))

    # 5. null completed_at and the boundary are not eligible
    def edge(st):
        st.add("cchifor/platform", 1, completed=None)
        # the script computes its cutoff from ITS clock: keep the boundary run 30 min newer than the
        # cutoff so the suite's own runtime cannot age it into eligibility
        st.add("cchifor/platform", 2, completed=int(time.time()) - RETENTION_DAYS * DAY + 1800)
        st.add("cchifor/platform", 3, completed=OLD)
    st, rc, out = scenario("edge", edge)
    check("edge exit 0", rc == 0, out[-400:])
    check("edge only the old run deleted", st.deleted == [("cchifor/platform", 3)], str(st.deleted))

    # 6. rerun between listing and delete: revalidation skips it
    def rerun(st):
        st.add("cchifor/platform", 3, completed=OLD)
        st.add("cchifor/platform", 4, completed=OLD)
    st, rc, out = scenario("rerun-before-delete", rerun)
    s = summary(out)
    check("rerun exit 0 and skipped", rc == 0 and s["skipped_revalidation"] == "1" and st.deleted == [("cchifor/platform", 4)], str(st.deleted) + out[-300:])

    # 7. 403 => exit 1 with a summary, no deletes
    st, rc, out = scenario("unauthorized", two_repos)
    check("unauthorized exit 1", rc == 1 and "HTTP 403" in out and not st.deleted, out[-300:])

    # 8. DELETE 500 => counted, stop, exit 1
    st, rc, out = scenario("delete-500", two_repos)
    s = summary(out)
    check("delete-500 exit 1", rc == 1 and s["deleted"] == "1" and not st.deleted, out[-300:])

    # 9. gate firing => exit 2, nothing deleted
    def firing(st):
        two_repos(st); st.firing = "2"
    st, rc, out = scenario("gate-firing", firing)
    check("gate-firing exit 2", rc == 2 and not st.deleted and "gate=paused" in out, out[-300:])

    # 10. prometheus down => exit 2 (fail closed)
    def down(st):
        two_repos(st); st.prom_down = True
    st, rc, out = scenario("prom-down", down)
    check("prom-down exit 2", rc == 2 and not st.deleted, out[-300:])

    # 11. gate re-checked mid-run: flips to firing after the first deletions
    class Flip(State):
        pass
    def midrun(st):
        two_repos(st)
    st, rc, out = scenario("gate-midrun", midrun, {"GATE_EVERY": "3"})
    check("gate-midrun baseline exit 0", rc == 0)
    # emulate the flip: run again with firing set after N calls
    st2 = State("gate-midrun-flip"); two_repos(st2)
    orig = Handler.do_DELETE
    def do_DELETE(self):
        orig(self)
        if len(self.state.deleted) >= 3:
            self.state.firing = "1"
    Handler.do_DELETE = do_DELETE
    try:
        Handler.state = st2
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        rc, out = run_script(st2, srv.server_address[1], {"GATE_EVERY": "3"})
        srv.shutdown()
    finally:
        Handler.do_DELETE = orig
    check("gate-midrun-flip exit 2 after 3", rc == 2 and len(st2.deleted) == 3, f"{rc} {len(st2.deleted)} {out[-300:]}")

    # 12. dry run: no DELETE calls at all
    st, rc, out = scenario("dry", two_repos, {"DRY_RUN": "true"})
    check("dry exit 0, no DELETE, would-delete listed", rc == 0 and not any(m == "DELETE" for m, _ in st.calls) and "would delete" in out, out[-300:])

    print(f"\n{'ALL PASSED' if failures == 0 else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
