#!/usr/bin/env python3
"""Unit tests for kubernetes/apps/apps/ci-rerun-watchdog/app.py.

No network: the Gitea API is a transport-level fake (so the REAL pagination and status handling
run under test) and the state ConfigMap is an in-memory store with resourceVersion semantics.
Run:

    python -m unittest discover -s scripts/tests -p "test_*.py"
"""
import importlib.util
import json
import logging
import pathlib
import re
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timezone

_MOD_PATH = pathlib.Path(__file__).resolve().parents[2] / "kubernetes" / "apps" / "apps" / "ci-rerun-watchdog" / "app.py"
_spec = importlib.util.spec_from_file_location("ci_rerun_watchdog", _MOD_PATH)
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)  # must NOT perform any I/O at import time
app.log.addHandler(logging.NullHandler())  # keep decision lines out of the test output

NOW = 1_800_000_000.0
LOST_AT = NOW - 900  # the cloud runner was seen flipping offline 15 min before this scan
REPO = "cchifor/ailab"
CLOUD_LOST, CLOUD_UP, AILAB = 10, 11, 1  # runner ids
WF = ".gitea/workflows/ci.yaml"


def ts(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mk_run(rid, sha="s1", event="pull_request", branch="", conclusion="failure",
           status="completed", completed=NOW - 300, path=WF):
    return {"id": rid, "path": path, "event": event, "head_sha": sha, "head_branch": branch,
            "status": status, "conclusion": conclusion, "completed_at": ts(completed),
            "started_at": ts(completed - 60), "run_attempt": 1, "display_title": "t"}


def mk_job(jid, run_id, runner_id, conclusion="failure", completed=NOW - 300, status="completed"):
    return {"id": jid, "run_id": run_id, "name": "job", "runner_id": runner_id,
            "runner_name": {CLOUD_LOST: "cloud-ci-1", CLOUD_UP: "cloud-ci-2", AILAB: "ci-runner-1"}.get(runner_id, ""),
            "status": status, "conclusion": conclusion, "completed_at": ts(completed)}


def mk_runner(rid, name, status):
    return {"id": rid, "name": name, "status": status, "busy": False, "labels": ["self-hosted-hv"]}


class Seq:
    def __init__(self):
        self.n = 0

    def next(self):
        self.n += 1
        return self.n


class FakeGitea:
    """Answers the endpoints app.GiteaApi uses, paginated like Gitea (page/limit, total_count or
    X-Total-Count). `fail[(method, path_regex, page)]` injects an exception or an HTTP status."""

    def __init__(self, seq):
        self.seq = seq
        self.runners = [mk_runner(CLOUD_LOST, "cloud-ci-1", "offline"),
                        mk_runner(CLOUD_UP, "cloud-ci-2", "idle"),
                        mk_runner(AILAB, "ci-runner-1", "idle")]
        self.org_repos = [{"full_name": REPO, "has_actions": True},
                          {"full_name": "cchifor/unlisted", "has_actions": True},
                          {"full_name": "cchifor/no-actions", "has_actions": False}]
        self.runs = {REPO: []}
        self.jobs = {}
        self.pulls = {REPO: []}
        self.branches = {REPO: {}}
        self.post_responses = {}
        self.posts = []
        self.fail = {}
        self.calls = []
        self.total_override = None  # lie about total_count (incomplete-pagination test)

    def __call__(self, method, path, params=None, body=None, timeout=None):
        params = dict(params or {})
        self.calls.append((method, path, params))
        for (m, rx, page), fail in self.fail.items():
            if m == method and re.search(rx, path) and (page is None or page == params.get("page")):
                if isinstance(fail, Exception):
                    raise fail
                return fail, {}, b""
        if method == "POST":
            m = re.fullmatch(r"/repos/([^/]+/[^/]+)/actions/runs/(\d+)/rerun-failed-jobs", path)
            repo, run_id = m.group(1), int(m.group(2))
            self.posts.append((self.seq.next(), repo, run_id))
            resp = self.post_responses.get((repo, run_id), 201)
            if resp == "timeout":
                raise app.TransportError("timed out")
            return resp, {}, b""
        page, limit = int(params.get("page", 1)), int(params.get("limit", 50))
        m = re.fullmatch(r"/orgs/([^/]+)/actions/runners", path)
        if m:
            return self._page(self.runners, "runners", page, limit)
        m = re.fullmatch(r"/orgs/([^/]+)/repos", path)
        if m:
            return self._page(self.org_repos, None, page, limit)
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/actions/runs", path)
        if m:
            runs = sorted(self.runs.get(m.group(1), []), key=lambda r: -r["id"])
            if params.get("status") == "failure":
                runs = [r for r in runs if r["status"] == "completed" and r["conclusion"] == "failure"]
            if params.get("head_sha"):
                runs = [r for r in runs if r["head_sha"] == params["head_sha"]]
            return self._page(runs, "workflow_runs", page, limit)
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/actions/runs/(\d+)/jobs", path)
        if m:
            return self._page(self.jobs.get((m.group(1), int(m.group(2))), []), "jobs", page, limit)
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls", path)
        if m:
            prs = [p for p in self.pulls.get(m.group(1), []) if p.get("state", "open") == params.get("state", "open")]
            return self._page(prs, None, page, limit)
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/branches/(.+)", path)
        if m:
            sha = self.branches.get(m.group(1), {}).get(urllib.parse.unquote(m.group(2)))
            if sha is None:
                return 404, {}, b'{"message":"branch does not exist"}'
            return 200, {}, json.dumps({"name": m.group(2), "commit": {"id": sha}}).encode()
        return 404, {}, b""

    def _page(self, items, key, page, limit):
        chunk = items[(page - 1) * limit: page * limit]
        total = len(items) if self.total_override is None else self.total_override
        if key is None:
            return 200, {"X-Total-Count": str(total)}, json.dumps(chunk).encode()
        return 200, {}, json.dumps({key: chunk, "total_count": total}).encode()


class FakeState:
    """The state store: read() -> (data, rv); write(data, rv) -> rv, 409 on a stale rv."""

    def __init__(self, seq):
        self.seq = seq
        self.data = None
        self.rv = None
        self.writes = []
        self.conflicts_to_inject = 0
        self.errors_to_inject = 0

    def read(self):
        return (dict(self.data), self.rv) if self.data is not None else (None, None)

    def write(self, data, rv):
        if self.errors_to_inject:
            self.errors_to_inject -= 1
            raise app.StateError("injected write failure")
        if self.conflicts_to_inject:
            self.conflicts_to_inject -= 1
            raise app.StateConflict("injected 409")
        if rv != self.rv:
            raise app.StateConflict("stale resourceVersion")
        self.data = {k: str(v) for k, v in data.items()}
        self.rv = str(int(self.rv or 0) + 1)
        self.writes.append((self.seq.next(), dict(self.data)))
        return self.rv

    def ledger(self, name):
        return json.loads((self.data or {}).get(name, "[]"))

    def seed(self, runner_seen=None, live=None, shadow=None, **extra):
        self.data = {"runner_seen": json.dumps(runner_seen or {}), "live": json.dumps(live or []),
                     "shadow": json.dumps(shadow or [])}
        self.data.update(extra)
        self.rv = "1"


def seen_lost(lost_at=LOST_AT):
    """runner_seen as persisted after an earlier scan watched cloud-ci-1 go offline."""
    return {
        str(CLOUD_LOST): {"name": "cloud-ci-1", "status": "offline", "last_online_ts": lost_at - 60,
                          "last_offline_ts": lost_at, "lost_at": lost_at},
        str(CLOUD_UP): {"name": "cloud-ci-2", "status": "idle", "last_online_ts": NOW - 60},
        str(AILAB): {"name": "ci-runner-1", "status": "idle", "last_online_ts": NOW - 60},
    }


def entry(repo, run_id, reserved_at, outcome="confirmed", **extra):
    e = {"repo": repo, "run_id": run_id, "sha": "old", "path": WF, "event": "pull_request",
         "ref": {"kind": "pr", "number": 1, "head_sha": "old"}, "reserved_at": reserved_at,
         "updated_at": reserved_at, "outcome": outcome, "attempts": 1}
    e.update(extra)
    return e


class Base(unittest.TestCase):
    def setUp(self):
        self.seq = Seq()
        self.gitea = FakeGitea(self.seq)
        self.store = FakeState(self.seq)
        self.store.seed(runner_seen=seen_lost())
        self.metrics = app.Metrics()
        self.api = app.GiteaApi(self.gitea, page_limit=10, max_pages=50)
        app._LOGGED_SKIPS.clear()

    def cfg(self, **over):
        base = dict(dry_run=False, repos=[REPO], gitea_token="x", lookback_seconds=7200)
        base.update(over)
        return app.Config(**base)

    def candidate_pr_run(self, rid=100, sha="s1", pr=7, completed=NOW - 300):
        """A PR run that satisfies every gate: failed on the lost cloud runner after the loss,
        its PR open at that sha, no siblings."""
        self.gitea.runs[REPO].append(mk_run(rid, sha=sha, completed=completed))
        self.gitea.jobs[(REPO, rid)] = [mk_job(rid * 10, rid, CLOUD_LOST, completed=completed)]
        self.gitea.pulls[REPO].append({"number": pr, "head": {"sha": sha, "ref": "feat/x"}, "state": "open"})

    def scan(self, cfg=None, now=NOW):
        return app.scan(self.api, self.store, cfg or self.cfg(), now=now, metrics=self.metrics)

    def assertNoPosts(self):
        self.assertEqual(self.gitea.posts, [])

    def posted_ids(self):
        return [p[2] for p in self.gitea.posts]


# --- G6: the current-head gate ------------------------------------------------------------------
class HeadGate(Base):
    def test_pr_run_resolved_via_pr_api(self):
        self.candidate_pr_run()
        s = self.scan()
        self.assertTrue(s["ok"], s["errors"])
        self.assertEqual(self.posted_ids(), [100])
        live = self.store.ledger("live")
        self.assertEqual([(e["run_id"], e["outcome"], e["ref"]["number"]) for e in live], [(100, "confirmed", 7)])
        self.assertEqual(self.metrics.get("reruns_total", {"repo": REPO, "mode": "live"}), 1)
        self.assertEqual(self.metrics.get("candidates", {"repo": REPO}), 1)
        self.assertEqual(self.metrics.get("last_scan_timestamp_seconds"), NOW)

    def test_pr_closed(self):
        self.candidate_pr_run()
        self.gitea.pulls[REPO] = []  # closed (or merged): the PR API no longer lists it
        s = self.scan()
        self.assertNoPosts()
        self.assertEqual(s["skipped"], {"pr_not_open": 1})
        self.assertEqual(self.store.ledger("live"), [])

    def test_pr_run_superseded_by_newer_sha_on_same_pr(self):
        self.candidate_pr_run()
        self.gitea.pulls[REPO][0]["head"]["sha"] = "s2"  # a newer push moved the PR head
        s = self.scan()
        self.assertNoPosts()
        self.assertEqual(s["skipped"], {"pr_not_open": 1})

    def _push_run(self, rid=200, sha="m1", branch="main"):
        self.gitea.runs[REPO].append(mk_run(rid, sha=sha, event="push", branch=branch))
        self.gitea.jobs[(REPO, rid)] = [mk_job(rid * 10, rid, CLOUD_LOST)]

    def test_push_run_at_branch_tip_is_rerun(self):
        self._push_run()
        self.gitea.branches[REPO]["main"] = "m1"
        self.scan()
        self.assertEqual(self.posted_ids(), [200])
        self.assertEqual(self.store.ledger("live")[0]["ref"], {"kind": "branch", "name": "main", "head_sha": "m1"})

    def test_branch_moved(self):
        self._push_run()
        self.gitea.branches[REPO]["main"] = "m2"
        s = self.scan()
        self.assertNoPosts()
        self.assertEqual(s["skipped"], {"branch_moved": 1})

    def test_branch_gone(self):
        self._push_run()
        s = self.scan()
        self.assertNoPosts()
        self.assertEqual(s["skipped"], {"branch_gone": 1})

    def test_branch_name_with_slash_is_url_encoded(self):
        self._push_run(branch="feat/x")
        self.gitea.branches[REPO]["feat/x"] = "m1"
        self.scan()
        self.assertEqual(self.posted_ids(), [200])
        self.assertTrue(any(p == "/repos/%s/branches/feat%%2Fx" % REPO for _m, p, _q in self.gitea.calls))

    def test_push_run_without_head_branch_is_skipped(self):
        self._push_run(branch="")  # AGit / detached: nothing to be current against
        s = self.scan()
        self.assertNoPosts()
        self.assertEqual(s["skipped"], {"no_ref": 1})


# --- G7: the concurrency-group gate ------------------------------------------------------------
class SiblingGate(Base):
    def test_superseded_by_newer_run_same_path(self):
        self.candidate_pr_run()
        self.gitea.runs[REPO].append(mk_run(101, conclusion="success"))  # same sha, same path, newer
        s = self.scan()
        self.assertNoPosts()
        self.assertEqual(s["skipped"], {"superseded": 1})

    def test_non_terminal_sibling(self):
        self.candidate_pr_run()
        for status in ("in_progress", "queued", "waiting", "pending", "requested", "weird-new-status"):
            self.gitea.runs[REPO] = [r for r in self.gitea.runs[REPO] if r["id"] != 99]
            self.gitea.runs[REPO].append(mk_run(99, conclusion="", status=status))  # OLDER id, still live
            s = self.scan()
            self.assertNoPosts()
            self.assertEqual(s["skipped"], {"sibling_live": 1}, status)

    def test_push_run_of_same_sha_is_not_a_pr_sibling(self):
        # on: [push, pull_request] produces two runs of one workflow for one sha, in DIFFERENT
        # concurrency groups (refs/heads/x vs refs/pull/N/*). The push twin must not block.
        self.candidate_pr_run()
        self.gitea.runs[REPO].append(mk_run(101, event="push", branch="feat/x", conclusion="", status="in_progress"))
        self.scan()
        self.assertEqual(self.posted_ids(), [100])

    def test_other_workflow_is_not_a_sibling(self):
        self.candidate_pr_run()
        self.gitea.runs[REPO].append(mk_run(101, conclusion="", status="in_progress", path=".gitea/workflows/other.yaml"))
        self.scan()
        self.assertEqual(self.posted_ids(), [100])


# --- G1-G5: the pure gates ----------------------------------------------------------------------
class PureGates(Base):
    def ctx(self, **over):
        state = app.State(self.store)
        state.load()
        kw = dict(now=NOW, cfg=self.cfg(**over), cloud={CLOUD_LOST: "cloud-ci-1", CLOUD_UP: "cloud-ci-2"},
                  runner_seen=state.runner_seen, tombstones=state.tombstones(), repo=REPO)
        return app.Ctx(**kw)

    def test_run_level_cancelled_never_selected(self):
        run = mk_run(100, conclusion="cancelled")
        jobs = [mk_job(1, 100, CLOUD_LOST, conclusion="cancelled")]  # even with a lost-cloud job
        self.assertEqual(app.classify(run, jobs, self.ctx()), ("skip", "run_cancelled"))
        self.assertEqual(app.pre_classify(run, self.ctx()), ("skip", "run_cancelled"))

    def test_live_or_non_failure_run_skipped(self):
        self.assertEqual(app.classify(mk_run(100, status="in_progress", conclusion=""), [], self.ctx()), ("skip", "run_live"))
        self.assertEqual(app.classify(mk_run(100, conclusion="success"), [], self.ctx()), ("skip", "not_failure"))

    def test_outside_lookback(self):
        run = mk_run(100, completed=NOW - 7201)
        self.assertEqual(app.classify(run, [mk_job(1, 100, CLOUD_LOST)], self.ctx()), ("skip", "outside_lookback"))
        run = mk_run(100, completed=NOW - 7199)
        self.assertEqual(app.classify(run, [mk_job(1, 100, CLOUD_LOST)], self.ctx())[0], "candidate")

    def test_unset_completed_at_skipped(self):
        run = mk_run(100)
        run["completed_at"] = "0001-01-01T00:00:00Z"
        self.assertEqual(app.classify(run, [mk_job(1, 100, CLOUD_LOST)], self.ctx()), ("skip", "completed_at_unset"))

    def test_unrelated_cancelled_job_blocks_the_run(self):
        jobs = [mk_job(1, 100, CLOUD_LOST), mk_job(2, 100, AILAB, conclusion="cancelled")]
        self.assertEqual(app.classify(mk_run(100), jobs, self.ctx()), ("skip", "job_not_cloud"))
        jobs = [mk_job(1, 100, CLOUD_LOST), mk_job(2, 100, CLOUD_UP, conclusion="cancelled")]
        self.assertEqual(app.classify(mk_run(100), jobs, self.ctx()), ("skip", "runner_not_lost"))

    def test_mixed_ci_runner_failure(self):
        jobs = [mk_job(1, 100, CLOUD_LOST), mk_job(2, 100, AILAB)]
        self.assertEqual(app.classify(mk_run(100), jobs, self.ctx()), ("skip", "job_not_cloud"))

    def test_successful_and_skipped_jobs_do_not_matter(self):
        jobs = [mk_job(1, 100, CLOUD_LOST), mk_job(2, 100, AILAB, conclusion="success"),
                mk_job(3, 100, 0, conclusion="skipped")]
        self.assertEqual(app.classify(mk_run(100), jobs, self.ctx()), ("candidate", "lost_cloud_jobs"))

    def test_no_failed_jobs(self):
        self.assertEqual(app.classify(mk_run(100), [mk_job(1, 100, CLOUD_LOST, conclusion="success")], self.ctx()),
                         ("skip", "no_failed_jobs"))
        self.assertEqual(app.classify(mk_run(100), None, self.ctx()), ("skip", "jobs_unavailable"))

    def test_runner_online(self):
        jobs = [mk_job(1, 100, CLOUD_UP)]
        self.assertEqual(app.classify(mk_run(100), jobs, self.ctx()), ("skip", "runner_not_lost"))

    def test_runner_online_again_clears_lost_at(self):
        # Seeded as lost; this scan sees it idle again -> a failure on it is a real one.
        self.gitea.runners[0]["status"] = "idle"
        self.candidate_pr_run()
        s = self.scan()
        self.assertNoPosts()
        self.assertEqual(s["skipped"], {"runner_not_lost": 1})
        self.assertNotIn("lost_at", json.loads(self.store.data["runner_seen"])[str(CLOUD_LOST)])

    def test_runner_never_observed(self):
        self.store.seed(runner_seen={})  # first scan ever: cloud-ci-1 is offline, but we never saw it online
        self.candidate_pr_run()
        s = self.scan()
        self.assertNoPosts()
        self.assertEqual(s["skipped"], {"runner_not_lost": 1})
        self.assertNotIn("lost_at", json.loads(self.store.data["runner_seen"])[str(CLOUD_LOST)])

    def test_runner_flip_is_stamped_and_correlates_next_scan(self):
        # Scan 1: online. Scan 2: offline -> lost_at. Scan 3: the reap landed -> candidate.
        self.store.seed(runner_seen={})
        self.gitea.runners[0]["status"] = "idle"
        self.scan(now=NOW - 1200)
        self.gitea.runners[0]["status"] = "offline"
        self.scan(now=NOW - 900)
        self.assertEqual(json.loads(self.store.data["runner_seen"])[str(CLOUD_LOST)]["lost_at"], NOW - 900)
        self.candidate_pr_run()  # job completed at NOW-300 = lost_at + 10 min
        self.scan(now=NOW)
        self.assertEqual(self.posted_ids(), [100])

    def test_hours_old_genuine_failure_then_shutdown(self):
        # Failed at 18:00 on a cloud runner; the cloud went to bed at 21:00. Not ours.
        jobs = [mk_job(1, 100, CLOUD_LOST, completed=LOST_AT - 3 * 3600)]
        run = mk_run(100, completed=LOST_AT - 3 * 3600)
        self.assertEqual(app.classify(run, jobs, self.ctx(lookback_seconds=6 * 3600)), ("skip", "loss_uncorrelated"))

    def test_zombie_reap_timing(self):
        jobs = [mk_job(1, 100, CLOUD_LOST, completed=LOST_AT + 600)]
        self.assertEqual(app.classify(mk_run(100), jobs, self.ctx()), ("candidate", "lost_cloud_jobs"))

    def test_drain_cancel_timing(self):
        jobs = [mk_job(1, 100, CLOUD_LOST, conclusion="cancelled", completed=LOST_AT - 60)]
        self.assertEqual(app.classify(mk_run(100), jobs, self.ctx()), ("candidate", "lost_cloud_jobs"))
        jobs = [mk_job(1, 100, CLOUD_LOST, conclusion="cancelled", completed=LOST_AT - 181)]
        self.assertEqual(app.classify(mk_run(100), jobs, self.ctx()), ("skip", "loss_uncorrelated"))

    def test_job_completed_at_unset(self):
        job = mk_job(1, 100, CLOUD_LOST)
        job["completed_at"] = "0001-01-01T00:00:00Z"
        self.assertEqual(app.classify(mk_run(100), [job], self.ctx()), ("skip", "job_completed_at_unset"))

    def test_unknown_runner_id(self):
        for rid in (0, 999, None, "10"):
            jobs = [mk_job(1, 100, CLOUD_LOST), {"id": 2, "runner_id": rid, "conclusion": "failure",
                                                 "status": "completed", "completed_at": ts(NOW - 300)}]
            self.assertEqual(app.classify(mk_run(100), jobs, self.ctx()), ("skip", "job_not_cloud"), rid)

    def test_cloud_set_comes_from_the_full_runners_list(self):
        # A runner named cloud-ci-9 that is NOT in the org list is unknown, never eligible.
        self.candidate_pr_run()
        self.gitea.jobs[(REPO, 100)] = [mk_job(1000, 100, 99)]
        self.store.seed(runner_seen=dict(seen_lost(), **{"99": {"name": "cloud-ci-9", "status": "offline", "lost_at": LOST_AT}}))
        s = self.scan()
        self.assertNoPosts()
        self.assertEqual(s["skipped"], {"job_not_cloud": 1})

    def test_live_job_in_a_done_run_blocks(self):
        jobs = [mk_job(1, 100, CLOUD_LOST), mk_job(2, 100, CLOUD_LOST, conclusion="", status="in_progress")]
        self.assertEqual(app.classify(mk_run(100), jobs, self.ctx()), ("skip", "job_live"))


# --- pagination ----------------------------------------------------------------------------------
class Pagination(Base):
    def test_page_two_failure_found(self):
        # 12 recent failures on ailab runners fill page one (limit 10); the lost-cloud run has
        # the lowest id and lands on page two.
        for i in range(12):
            rid = 500 + i
            self.gitea.runs[REPO].append(mk_run(rid, sha="f%d" % i))
            self.gitea.jobs[(REPO, rid)] = [mk_job(rid * 10, rid, AILAB)]
        self.candidate_pr_run(rid=100)
        s = self.scan()
        self.assertTrue(s["ok"], s["errors"])
        self.assertEqual(self.posted_ids(), [100])
        self.assertEqual(s["skipped"], {"job_not_cloud": 12})

    def test_runs_walk_stops_at_the_lookback(self):
        for i in range(30):
            rid = 500 + i
            self.gitea.runs[REPO].append(mk_run(rid, sha="f%d" % i, completed=NOW - 8000))  # all older than 2h
            self.gitea.jobs[(REPO, rid)] = [mk_job(rid * 10, rid, AILAB)]
        s = self.scan()
        self.assertTrue(s["ok"])
        pages = [q.get("page") for m, p, q in self.gitea.calls if p.endswith("/actions/runs") and q.get("status") == "failure"]
        self.assertEqual(pages, [1])  # the whole first page ended before the cutoff
        self.assertEqual(s["skipped"], {"outside_lookback": 10})

    def test_jobs_page_two_error_means_zero_reruns(self):
        self.candidate_pr_run()
        self.gitea.jobs[(REPO, 100)] = [mk_job(1000 + i, 100, CLOUD_LOST) for i in range(12)]
        self.gitea.fail[("GET", r"/runs/100/jobs$", 2)] = 502
        s = self.scan()
        self.assertFalse(s["ok"])
        self.assertNoPosts()
        self.assertEqual(self.metrics.get("errors_total", {"stage": "jobs"}), 1)
        self.assertEqual(self.metrics.get("last_scan_timestamp_seconds"), 0)  # never set on a failed scan
        self.assertEqual(self.store.ledger("live"), [])

    def test_incomplete_pagination_is_an_error(self):
        # The server's total_count says 12 but the pages carry 10: refuse the set.
        self.candidate_pr_run()
        self.gitea.jobs[(REPO, 100)] = [mk_job(1000 + i, 100, CLOUD_LOST) for i in range(10)]
        self.gitea.total_override = 12
        s = self.scan()
        self.assertFalse(s["ok"])
        self.assertNoPosts()
        self.assertIn("incomplete pagination", s["errors"][0])

    def test_runners_list_error_aborts_before_anything(self):
        self.candidate_pr_run()
        self.gitea.fail[("GET", r"/actions/runners$", None)] = app.TransportError("connection refused")
        s = self.scan()
        self.assertFalse(s["ok"])
        self.assertNoPosts()
        self.assertEqual(self.metrics.get("errors_total", {"stage": "runners"}), 1)
        self.assertEqual(self.store.writes, [])

    def test_org_repos_paginated_and_unlisted_counted(self):
        self.gitea.org_repos += [{"full_name": "cchifor/x%d" % i, "has_actions": True} for i in range(15)]
        s = self.scan()
        self.assertTrue(s["ok"])
        self.assertEqual(self.metrics.get("unlisted_repos"), 16)
        self.assertIn("cchifor/unlisted", s["unlisted_repos"])
        self.assertNotIn("cchifor/no-actions", s["unlisted_repos"])

    def test_pulls_page_two_found(self):
        self.candidate_pr_run(pr=7)
        self.gitea.pulls[REPO] = [{"number": i, "head": {"sha": "z%d" % i}, "state": "open"} for i in range(12)] + self.gitea.pulls[REPO]
        self.scan()
        self.assertEqual(self.posted_ids(), [100])

    def test_page_cap_is_an_error(self):
        self.api = app.GiteaApi(self.gitea, page_limit=1, max_pages=2)  # 3 runners need 3 pages
        s = self.scan()
        self.assertFalse(s["ok"])
        self.assertNoPosts()
        self.assertEqual(self.metrics.get("errors_total", {"stage": "runners"}), 1)
        self.assertIn("more than 2 pages", s["errors"][0])

    def test_malformed_list_is_an_error(self):
        self.candidate_pr_run()
        self.gitea.fail[("GET", r"/runs/100/jobs$", None)] = 200  # 200 with an empty body: no jobs list
        s = self.scan()
        self.assertFalse(s["ok"])
        self.assertEqual(self.metrics.get("errors_total", {"stage": "jobs"}), 1)


# --- ledgers, caps, kill switch -------------------------------------------------------------------
class Ledgers(Base):
    def test_dedupe_across_scans_live(self):
        self.candidate_pr_run()
        self.scan()
        s = self.scan(now=NOW + 60)
        self.assertEqual(self.posted_ids(), [100])
        self.assertEqual(s["skipped"], {"tombstone": 1})
        self.assertEqual(len(self.store.ledger("live")), 1)

    def test_dedupe_across_scans_shadow(self):
        self.candidate_pr_run()
        self.scan(self.cfg(dry_run=True))
        s = self.scan(self.cfg(dry_run=True), now=NOW + 60)
        self.assertNoPosts()
        self.assertEqual(s["skipped"], {"tombstone": 1})
        self.assertEqual(len(self.store.ledger("shadow")), 1)

    def test_shadow_tombstone_blocks_a_later_live_rerun(self):
        self.candidate_pr_run()
        self.scan(self.cfg(dry_run=True))
        s = self.scan(self.cfg(dry_run=False), now=NOW + 60)
        self.assertNoPosts()
        self.assertEqual(s["skipped"], {"tombstone": 1})

    def test_tombstone_survives_after_lookback(self):
        self.candidate_pr_run()
        self.store.seed(runner_seen=seen_lost(), live=[entry(REPO, 100, NOW - 3 * 3600)])  # older than LOOKBACK
        s = self.scan()
        self.assertNoPosts()
        self.assertEqual(s["skipped"], {"tombstone": 1})
        self.assertEqual([e["run_id"] for e in self.store.ledger("live")], [100])

    def test_tombstone_pruned_after_retention(self):
        self.store.seed(runner_seen=seen_lost(), live=[entry(REPO, 100, NOW - 91 * 86400)])
        self.scan()
        self.assertEqual(self.store.ledger("live"), [])

    def test_per_scan_cap(self):
        for i in range(4):
            self.candidate_pr_run(rid=100 + i, sha="s%d" % i, pr=7 + i)
        s = self.scan()
        self.assertEqual(self.posted_ids(), [103, 102, 101])  # newest first; the oldest is capped
        self.assertEqual(s["skipped"], {"cap_scan": 1})
        self.assertEqual(self.metrics.get("candidates", {"repo": REPO}), 4)
        # The capped run is not tombstoned: it is picked up by the next scan.
        self.scan(now=NOW + 60)
        self.assertEqual(sorted(self.posted_ids()), [100, 101, 102, 103])

    def test_per_day_cap(self):
        self.store.seed(runner_seen=seen_lost(), live=[entry(REPO, 900 + i, NOW - 3600) for i in range(20)])
        self.candidate_pr_run()
        s = self.scan()
        self.assertNoPosts()
        self.assertEqual(s["skipped"], {"cap_day": 1})
        self.assertEqual(self.metrics.get("reruns_last_24h", {"mode": "live"}), 20)

    def test_per_day_cap_rolls_off(self):
        self.store.seed(runner_seen=seen_lost(), live=[entry(REPO, 900 + i, NOW - 86401) for i in range(20)])
        self.candidate_pr_run()
        self.scan()
        self.assertEqual(self.posted_ids(), [100])

    def test_ledgers_count_caps_separately(self):
        self.store.seed(runner_seen=seen_lost(), shadow=[entry(REPO, 900 + i, NOW - 3600, outcome="would_rerun") for i in range(20)])
        self.candidate_pr_run()
        self.scan(self.cfg(dry_run=False))
        self.assertEqual(self.posted_ids(), [100])
        self.assertEqual(self.metrics.get("reruns_last_24h", {"mode": "dry_run"}), 20)
        self.assertEqual(self.metrics.get("reruns_last_24h", {"mode": "live"}), 1)

    def test_disabled(self):
        self.store.seed(runner_seen={}, disabled="true")
        self.gitea.runners[0]["status"] = "idle"
        self.candidate_pr_run()
        s = self.scan()
        self.assertTrue(s["ok"])
        self.assertTrue(s["disabled"])
        self.assertNoPosts()
        self.assertEqual(self.metrics.get("disabled"), 1)
        self.assertEqual(self.metrics.get("last_scan_timestamp_seconds"), NOW)
        # Observation continues while disabled, so a loss during the pause is still stamped.
        self.assertEqual(json.loads(self.store.data["runner_seen"])[str(CLOUD_LOST)]["status"], "idle")
        self.assertEqual(self.store.data["disabled"], "true")

    def test_dry_run_issues_no_post_but_reserves_in_shadow(self):
        self.candidate_pr_run()
        s = self.scan(self.cfg(dry_run=True))
        self.assertTrue(s["ok"])
        self.assertNoPosts()
        self.assertEqual(self.store.ledger("live"), [])
        shadow = self.store.ledger("shadow")
        self.assertEqual([(e["run_id"], e["outcome"], e["sha"]) for e in shadow], [(100, "would_rerun", "s1")])
        self.assertEqual(self.metrics.get("reruns_total", {"repo": REPO, "mode": "dry_run"}), 1)
        self.assertEqual(self.metrics.get("reruns_total", {"repo": REPO, "mode": "live"}), 0)
        self.assertEqual(self.metrics.get("dry_run"), 1)
        self.assertEqual(s["reruns"], [{"repo": REPO, "run_id": 100, "mode": "dry_run"}])

    def test_reserve_before_post_ordering(self):
        self.candidate_pr_run()
        self.scan()
        reserving = [seq for seq, data in self.store.writes
                     if any(e["run_id"] == 100 and e["outcome"] == "reserved" for e in json.loads(data["live"]))]
        self.assertTrue(reserving, "no write carried the reservation")
        self.assertLess(min(reserving), self.gitea.posts[0][0])

    def test_failed_reservation_write_means_no_post(self):
        self.candidate_pr_run()
        self.store.errors_to_inject = 1  # the FIRST write is the runner observation
        s = self.scan()
        self.assertFalse(s["ok"])
        self.assertNoPosts()
        self.assertEqual(self.metrics.get("errors_total", {"stage": "state_write"}), 1)

    def test_reservation_write_failure_after_observation_means_no_post(self):
        self.candidate_pr_run()
        real_write = self.store.write
        calls = {"n": 0}

        def flaky(data, rv):
            calls["n"] += 1
            if any(e.get("outcome") == "reserved" for e in json.loads(data.get("live", "[]"))):
                raise app.StateError("disk on fire")
            return real_write(data, rv)

        self.store.write = flaky
        s = self.scan()
        self.assertFalse(s["ok"])
        self.assertNoPosts()

    def test_state_conflict_retried_once_keeping_operator_keys(self):
        self.candidate_pr_run()
        self.store.conflicts_to_inject = 1
        # Simulate the operator's `kubectl patch` landing between our read and write.
        self.store.data["ack_unresolved_before"] = "123"
        s = self.scan()
        self.assertTrue(s["ok"], s["errors"])
        self.assertEqual(self.store.data["ack_unresolved_before"], "123")

    def test_two_conflicts_abort(self):
        self.candidate_pr_run()
        self.store.conflicts_to_inject = 2
        s = self.scan()
        self.assertFalse(s["ok"])
        self.assertNoPosts()
        self.assertEqual(self.metrics.get("errors_total", {"stage": "state_write"}), 1)

    def test_state_created_when_missing(self):
        self.store.data = None
        self.store.rv = None
        self.gitea.runners[0]["status"] = "idle"
        s = self.scan()
        self.assertTrue(s["ok"], s["errors"])
        self.assertIn("runner_seen", self.store.data)

    def test_unlisted_repo_metric_and_candidate_gauge_reset(self):
        self.candidate_pr_run()
        self.scan()
        self.assertEqual(self.metrics.get("candidates", {"repo": REPO}), 1)
        self.scan(now=NOW + 60)
        self.assertEqual(self.metrics.get("candidates", {"repo": REPO}), 0)  # tombstoned now


# --- POST outcomes, reconciliation, collateral ------------------------------------------------------
class Outcomes(Base):
    def test_400_rejected(self):
        self.candidate_pr_run()
        self.gitea.post_responses[(REPO, 100)] = 400
        self.scan()
        e = self.store.ledger("live")[0]
        self.assertEqual((e["outcome"], e["attempts"]), ("rejected", 1))
        self.scan(now=NOW + 60)
        self.assertEqual(self.posted_ids(), [100])  # rejected still tombstones

    def test_head_moved_between_check_and_post_is_rejected_without_post(self):
        self.candidate_pr_run()
        real = self.gitea.__call__

        def moving(method, path, params=None, body=None, timeout=None):
            # The reservation write is the trigger: right after it, the PR moves on.
            if method == "GET" and path.endswith("/pulls") and any("reserved" in d.get("live", "") for _s, d in self.store.writes):
                self.gitea.pulls[REPO][0]["head"]["sha"] = "s2"
            return real(method, path, params, body, timeout)

        self.api = app.GiteaApi(moving, page_limit=10)
        self.scan()
        self.assertNoPosts()
        e = self.store.ledger("live")[0]
        self.assertEqual((e["outcome"], e["reason"]), ("rejected", "pr_not_open"))

    def test_timeout_uncertain_then_run_left_failure_confirms(self):
        self.candidate_pr_run()
        self.gitea.post_responses[(REPO, 100)] = "timeout"
        self.scan()
        e = self.store.ledger("live")[0]
        self.assertEqual((e["outcome"], e["attempts"]), ("uncertain", 1))
        self.assertEqual(self.metrics.get("errors_total", {"stage": "rerun_post"}), 0)
        # Next scan the run is in_progress: the POST did land.
        self.gitea.runs[REPO][0].update(status="in_progress", conclusion="")
        self.scan(now=NOW + 60)
        e = self.store.ledger("live")[0]
        self.assertEqual((e["outcome"], e["attempts"]), ("confirmed", 1))
        self.assertEqual(len(self.gitea.posts), 1)

    def test_timeout_uncertain_then_unchanged_retries_once(self):
        self.candidate_pr_run()
        self.gitea.post_responses[(REPO, 100)] = "timeout"
        self.scan()
        self.gitea.post_responses[(REPO, 100)] = 201
        self.scan(now=NOW + 60)
        e = self.store.ledger("live")[0]
        self.assertEqual((e["outcome"], e["attempts"]), ("confirmed", 2))
        self.assertEqual(self.posted_ids(), [100, 100])

    def test_uncertain_twice_then_unresolved_and_acknowledged(self):
        self.candidate_pr_run()
        self.gitea.post_responses[(REPO, 100)] = "timeout"
        self.scan()
        self.scan(now=NOW + 60)
        e = self.store.ledger("live")[0]
        self.assertEqual((e["outcome"], e["attempts"]), ("uncertain", 2))
        self.scan(now=NOW + 120)
        e = self.store.ledger("live")[0]
        self.assertEqual(e["outcome"], "unresolved")
        self.assertEqual(self.metrics.get("unresolved"), 1)
        self.assertEqual(self.posted_ids(), [100, 100])  # never a third POST
        self.store.data["ack_unresolved_before"] = str(NOW + 120)
        self.scan(now=NOW + 180)
        self.assertEqual(self.metrics.get("unresolved"), 0)
        self.assertEqual(self.store.ledger("live")[0]["outcome"], "unresolved_acked")

    def test_unexpected_status_is_uncertain(self):
        self.candidate_pr_run()
        self.gitea.post_responses[(REPO, 100)] = 503
        self.scan()
        self.assertEqual(self.store.ledger("live")[0]["outcome"], "uncertain")

    def test_reserved_entry_from_a_crash_is_reconciled(self):
        # Crashed after the reservation write, before the POST: no POST happened, run unchanged.
        self.candidate_pr_run()
        self.store.seed(runner_seen=seen_lost(), live=[entry(REPO, 100, NOW - 60, outcome="reserved", attempts=0, sha="s1",
                                                              ref={"kind": "pr", "number": 7, "head_sha": "s1"})])
        self.scan()
        e = self.store.ledger("live")[0]
        self.assertEqual((e["outcome"], e["attempts"]), ("confirmed", 1))
        self.assertEqual(self.posted_ids(), [100])

    def test_reconcile_retry_respects_head_gates(self):
        self.candidate_pr_run()
        self.gitea.post_responses[(REPO, 100)] = "timeout"
        self.scan()
        self.gitea.pulls[REPO] = []  # PR closed before the retry
        self.scan(now=NOW + 60)
        e = self.store.ledger("live")[0]
        self.assertEqual((e["outcome"], e["reason"]), ("rejected", "pr_not_open"))
        self.assertEqual(len(self.gitea.posts), 1)

    def test_collateral_cancel_detected_and_compensated(self):
        self.candidate_pr_run()
        self.scan()  # POST at NOW
        # A push landed seconds after: PR head is s2, and s2's run of the same workflow was
        # cancelled 30 s after our POST (cancel-in-progress by the jobs we queued).
        self.gitea.pulls[REPO][0]["head"]["sha"] = "s2"
        self.gitea.runs[REPO].append(mk_run(102, sha="s2", conclusion="cancelled", completed=NOW + 30))
        s = self.scan(now=NOW + 60)
        self.assertTrue(s["ok"], s["errors"])
        self.assertEqual(self.metrics.get("collateral_cancel_total"), 1)
        self.assertEqual(s["collateral"], [{"repo": REPO, "run_id": 102, "caused_by": 100}])
        self.assertEqual(self.posted_ids(), [100, 102])
        live = {e["run_id"]: e for e in self.store.ledger("live")}
        self.assertEqual(live[102]["collateral_of"], 100)
        self.assertEqual(live[102]["outcome"], "confirmed")
        self.assertTrue(live[100]["post_checked"])
        # Once only.
        self.scan(now=NOW + 120)
        self.assertEqual(self.metrics.get("collateral_cancel_total"), 1)
        self.assertEqual(self.posted_ids(), [100, 102])

    def test_cancel_outside_the_window_is_not_collateral(self):
        self.candidate_pr_run()
        self.scan()
        self.gitea.pulls[REPO][0]["head"]["sha"] = "s2"
        self.gitea.runs[REPO].append(mk_run(102, sha="s2", conclusion="cancelled", completed=NOW + 300))
        self.scan(now=NOW + 360)
        self.assertEqual(self.metrics.get("collateral_cancel_total"), 0)
        self.assertEqual(self.posted_ids(), [100])

    def test_post_check_window_closes(self):
        self.candidate_pr_run()
        self.scan()
        self.scan(now=NOW + 601)
        self.assertTrue(self.store.ledger("live")[0]["post_checked"])
        pulls_calls = [c for c in self.gitea.calls if c[1].endswith("/pulls")]
        n = len(pulls_calls)
        self.scan(now=NOW + 700)
        self.assertEqual(len([c for c in self.gitea.calls if c[1].endswith("/pulls")]), n)  # no more lookups

    def test_no_post_check_in_dry_run(self):
        self.candidate_pr_run()
        self.scan(self.cfg(dry_run=True))
        self.gitea.pulls[REPO][0]["head"]["sha"] = "s2"
        self.gitea.runs[REPO].append(mk_run(102, sha="s2", conclusion="cancelled", completed=NOW + 30))
        self.scan(self.cfg(dry_run=True), now=NOW + 60)
        self.assertEqual(self.metrics.get("collateral_cancel_total"), 0)
        self.assertNoPosts()


# --- the real state store against a fake API server ------------------------------------------------
class KubeStateStore(unittest.TestCase):
    """KubeState against a scripted urlopen: 404 -> create, PUT carries resourceVersion, 409 ->
    StateConflict, other statuses -> StateError, and a lost connection -> StateError."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for name, body in (("token", "sa-token"), ("ca.crt", "not-a-cert"), ("namespace", "ns1")):
            with open(pathlib.Path(self.tmp.name) / name, "w") as f:
                f.write(body)
        self.store = app.KubeState("ns1", "cm1", host="https://api", sa_dir=self.tmp.name)
        self.store._context = lambda: None  # ca.crt above is not a real PEM
        self.requests = []
        self.script = []
        self.orig = app.urllib.request.urlopen
        app.urllib.request.urlopen = self._urlopen

    def tearDown(self):
        app.urllib.request.urlopen = self.orig
        self.tmp.cleanup()

    def _urlopen(self, req, timeout=None, context=None):
        self.requests.append((req.get_method(), req.full_url, req.data, req.get_header("Authorization")))
        status, body = self.script.pop(0)
        if isinstance(status, Exception):
            raise status
        if status >= 400:
            raise app.urllib.error.HTTPError(req.full_url, status, "err", {}, None)

        class Resp:
            def __init__(s):
                s.status = status

            def read(s):
                return json.dumps(body).encode()

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

        return Resp()

    def test_read_missing_then_create(self):
        self.script = [(404, None), (201, {"metadata": {"resourceVersion": "5"}})]
        self.assertEqual(self.store.read(), (None, None))
        rv = self.store.write({"live": "[]"}, None)
        self.assertEqual(rv, "5")
        method, url, data, auth = self.requests[1]
        self.assertEqual((method, url), ("POST", "https://api/api/v1/namespaces/ns1/configmaps"))
        self.assertEqual(auth, "Bearer sa-token")
        body = json.loads(data)
        self.assertEqual(body["data"], {"live": "[]"})
        self.assertNotIn("resourceVersion", body["metadata"])

    def test_read_then_put_with_resource_version(self):
        self.script = [(200, {"metadata": {"resourceVersion": "7"}, "data": {"disabled": "true"}}),
                       (200, {"metadata": {"resourceVersion": "8"}})]
        data, rv = self.store.read()
        self.assertEqual((data, rv), ({"disabled": "true"}, "7"))
        self.assertEqual(self.store.write(data, rv), "8")
        method, url, body, _auth = self.requests[1]
        self.assertEqual((method, url), ("PUT", "https://api/api/v1/namespaces/ns1/configmaps/cm1"))
        self.assertEqual(json.loads(body)["metadata"]["resourceVersion"], "7")

    def test_conflict_and_errors(self):
        self.script = [(409, None)]
        with self.assertRaises(app.StateConflict):
            self.store.write({}, "1")
        self.script = [(403, None)]
        with self.assertRaises(app.StateError):
            self.store.write({}, "1")
        self.script = [(500, None)]
        with self.assertRaises(app.StateError):
            self.store.read()
        self.script = [(app.urllib.error.URLError("down"), None)]
        with self.assertRaises(app.StateError):
            self.store.read()

    def test_state_wrapper_maps_store_errors_to_stages(self):
        self.script = [(500, None)]
        st = app.State(self.store)
        with self.assertRaises(app.ApiError) as cm:
            st.load()
        self.assertEqual(cm.exception.stage, "state_read")
        self.script = [(403, None)]
        with self.assertRaises(app.ApiError) as cm:
            st.save()
        self.assertEqual(cm.exception.stage, "state_write")


# --- plumbing -------------------------------------------------------------------------------------
class Plumbing(unittest.TestCase):
    def test_parse_ts(self):
        self.assertIsNone(app.parse_ts("0001-01-01T00:00:00Z"))
        self.assertIsNone(app.parse_ts("1970-01-01T00:00:00Z"))
        self.assertIsNone(app.parse_ts(""))
        self.assertIsNone(app.parse_ts(None))
        self.assertIsNone(app.parse_ts("garbage"))
        self.assertEqual(app.parse_ts("2026-09-23T10:00:00Z"), app.parse_ts("2026-09-23T13:00:00+03:00"))

    def test_metrics_render_escapes_and_types(self):
        m = app.Metrics()
        m.inc("skipped_total", {"repo": 'a"b\\c', "reason": "x\ny"})
        m.set("last_scan_timestamp_seconds", 1.5)
        out = m.render()
        self.assertIn('ci_rerun_watchdog_skipped_total{reason="x\\ny",repo="a\\"b\\\\c"} 1', out)
        self.assertIn("# TYPE ci_rerun_watchdog_skipped_total counter", out)
        self.assertIn("ci_rerun_watchdog_last_scan_timestamp_seconds 1.5", out)
        self.assertTrue(out.endswith("\n"))

    def test_config_from_env(self):
        cfg = app.Config.from_env({"DRY_RUN": "false", "REPOS": "a/b, c/d", "MAX_RERUNS_PER_DAY": "7",
                                   "STATE_NAMESPACE": "ns", "GITEA_URL": "http://g:3000/"})
        self.assertFalse(cfg.dry_run)
        self.assertEqual(cfg.repos, ["a/b", "c/d"])
        self.assertEqual(cfg.max_reruns_per_day, 7)
        self.assertEqual(cfg.gitea_url, "http://g:3000")
        self.assertTrue(app.Config.from_env({"STATE_NAMESPACE": "ns"}).dry_run)  # the default is shadow
        with self.assertRaises(TypeError):
            app.Config(nope=1)

    def test_transport_sends_git_user_agent(self):
        t = app.UrllibTransport("http://gitea:3000", "tok")
        captured = {}

        class Resp:
            status = 200
            headers = {}

            def read(self):
                return b"[]"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None, context=None):
            captured["req"] = req
            return Resp()

        orig = app.urllib.request.urlopen
        app.urllib.request.urlopen = fake_urlopen
        try:
            t("GET", "/orgs/x/repos", {"page": 1})
        finally:
            app.urllib.request.urlopen = orig
        req = captured["req"]
        self.assertEqual(req.get_header("User-agent"), "git/2.47.0")
        self.assertEqual(req.get_header("Authorization"), "token tok")
        self.assertEqual(req.full_url, "http://gitea:3000/api/v1/orgs/x/repos?page=1")

    def test_transport_error_on_no_answer(self):
        t = app.UrllibTransport("http://gitea:3000", "tok")

        def refuse(req, timeout=None, context=None):
            raise app.urllib.error.URLError("connection refused")

        orig = app.urllib.request.urlopen
        app.urllib.request.urlopen = refuse
        try:
            with self.assertRaises(app.TransportError):
                t("GET", "/orgs/x/repos")
        finally:
            app.urllib.request.urlopen = orig

    def test_http_endpoints(self):
        srv = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            base = "http://127.0.0.1:%d" % srv.server_address[1]
            with app.urllib.request.urlopen(base + "/healthz", timeout=5) as r:
                self.assertEqual(r.status, 200)
                self.assertTrue(json.loads(r.read())["ok"])
            with app.urllib.request.urlopen(base + "/metrics", timeout=5) as r:
                body = r.read().decode()
                self.assertIn("# TYPE ci_rerun_watchdog_reruns_total counter", body)
            with self.assertRaises(app.urllib.error.HTTPError) as cm:
                app.urllib.request.urlopen(base + "/nope", timeout=5)
            self.assertEqual(cm.exception.code, 404)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_ref_family(self):
        self.assertEqual(app.ref_family("pull_request"), "pr")
        self.assertEqual(app.ref_family("pull_request_target"), "pr")
        for ev in ("push", "schedule", "workflow_dispatch", "", None):
            self.assertEqual(app.ref_family(ev), "branch")


if __name__ == "__main__":
    unittest.main()
