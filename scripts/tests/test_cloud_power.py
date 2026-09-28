#!/usr/bin/env python3
"""Unit tests for the scheduled OFF in kubernetes/apps/apps/cloud-power/app.py.

No network: Gitea is a transport-level fake (so the REAL request building, pagination and response
validation run under test), the state ConfigMap is an in-memory store with resourceVersion
semantics, the node shutdown is a recorder and the host probe is a dict. Run:

    python -m unittest discover -s scripts/tests -p "test_*.py"
"""
import importlib.util
import json
import pathlib
import threading
import unittest
import urllib.parse

_MOD_PATH = pathlib.Path(__file__).resolve().parents[2] / "kubernetes" / "apps" / "apps" / "cloud-power" / "app.py"
_spec = importlib.util.spec_from_file_location("cloud_power", _MOD_PATH)
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)  # must NOT perform any I/O at import time
app.log = lambda msg: None      # keep decision lines out of the test output

ORG = "cchifor"
RUNNERS = "/orgs/%s/actions/runners" % ORG
JOBS = "/orgs/%s/actions/jobs" % ORG


class FakeGitea:
    """Serves the org runners / jobs endpoints the way Gitea 1.26 does (page+limit, total_count)."""

    def __init__(self, runners, jobs=None):
        self.runners = {r["id"]: dict(r) for r in runners}
        self.jobs = list(jobs or [])
        self.patches = []          # (id, disabled)
        self.fail = {}             # (method, path-prefix) -> status | "transport" | "applied-then-lost"
        self.envelope = {}         # path -> callable(page, body_dict) -> body_dict (malformed replies)
        self.patch_gate = None     # threading.Event: PATCHes block until it is set

    def __call__(self, method, url, headers, body):
        assert headers["Authorization"] == "token T"
        u = urllib.parse.urlparse(url)
        path = u.path[len("/api/v1"):]
        q = dict(urllib.parse.parse_qsl(u.query))
        lost = False
        for (m, p), how in self.fail.items():
            if m == method and path.startswith(p):
                if how == "applied-then-lost":
                    lost = True
                    continue
                if how == "transport":
                    raise app.GiteaError("%s %s: TimeoutError" % (method, path))
                return how, b'{"message":"nope"}'
        if method == "GET" and path == RUNNERS:
            return 200, self._page(list(self.runners.values()), "runners", q, self.envelope.get(path))
        if method == "GET" and path == JOBS:
            assert q.get("status") == "in_progress"
            return 200, self._page(self.jobs, "jobs", q, self.envelope.get(path))
        if method == "PATCH" and path.startswith(RUNNERS + "/"):
            if self.patch_gate is not None:
                self.patch_gate.wait(5)
            rid = int(path.rsplit("/", 1)[1])
            if rid not in self.runners:
                return 404, b'{"message":"not found"}'
            self.runners[rid]["disabled"] = json.loads(body)["disabled"]
            self.patches.append((rid, self.runners[rid]["disabled"]))
            if lost:
                raise app.GiteaError("PATCH %s: TimeoutError (applied, reply lost)" % path)
            return 200, json.dumps(self.runners[rid]).encode()
        return 404, b"{}"

    @staticmethod
    def _page(items, key, q, envelope=None):
        page, limit = int(q["page"]), int(q["limit"])
        body = {key: items[(page - 1) * limit: page * limit], "total_count": len(items)}
        if envelope:
            body = envelope(page, body)
        return json.dumps(body).encode()

    def disabled(self, rid):
        return self.runners[rid]["disabled"]


class MemStore:
    def __init__(self):
        self.data, self.rv, self.fail_writes, self.conflicts = None, None, 0, 0

    def read(self):
        return (dict(self.data) if self.data is not None else None), self.rv

    def write(self, data, rv):
        if getattr(self, "down", False):
            raise app.StateError("configmap write: HTTP 503")
        if self.conflicts:
            self.conflicts -= 1
            self.rv = (self.rv or 0) + 1           # someone else wrote in between
            raise app.StateConflict("configmap write: HTTP 409")
        if self.fail_writes:
            self.fail_writes -= 1
            raise app.StateError("configmap write: HTTP 500")
        if rv != self.rv:
            raise app.StateConflict("configmap write: HTTP 409")
        self.data = dict(data)
        self.rv = (self.rv or 0) + 1
        return self.rv

    def schedule(self):
        return json.loads(self.data["schedule"]) if self.data and self.data.get("schedule") else None


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def runner(rid, name, status="online", busy=False, disabled=False):
    return {"id": rid, "name": name, "status": status, "busy": busy, "disabled": disabled}


def job(name, runner_name):
    return {"id": 1, "name": name, "runner_name": runner_name, "status": "in_progress",
            "started_at": "2026-09-28T20:00:00Z", "html_url": "https://git/x"}


class Base(unittest.TestCase):
    def setUp(self):
        self.fg = FakeGitea([runner(1, "cloud-ci-1"), runner(2, "cloud-ci-3"),
                             runner(3, "ci-runner-1"), runner(4, "cloud-ci-6", status="offline")])
        self.store = MemStore()
        self.clock = Clock()
        self.hosts = {"cloud1": True, "cloud2": True, "cloud3": True}
        self.shutdowns = []
        self.shutdown_result = [{"node": n, "result": "shutdown requested"} for n in ("cloud1", "cloud2", "cloud3")]
        self.s = self.new_sched()

    def new_sched(self):
        def shutdown():
            self.shutdowns.append(self.clock())
            return [dict(r) for r in self.shutdown_result]
        return app.Scheduler(app.Gitea("http://gitea:3000", ORG, "T", transport=self.fg), self.store,
                             shutdown, host_up_fn=lambda n: self.hosts[n], clock=self.clock,
                             prefix="cloud-ci-", drain_max=11700, offline_wait=2400, clear_polls=2)

    def tick(self, n=1, dt=20):
        for _ in range(n):
            self.clock.t += dt
            self.s.tick()

    def phase(self):
        return self.s.view["phase"]


class ScheduleTests(Base):
    def test_pauses_only_enabled_cloud_runners_and_persists_first(self):
        view = self.s.schedule("op@x")
        self.assertEqual(view["phase"], "draining")
        self.assertTrue(self.fg.disabled(1) and self.fg.disabled(2) and self.fg.disabled(4))
        self.assertFalse(self.fg.disabled(3), "ailab runners are never touched")
        st = self.store.schedule()
        self.assertEqual((st["phase"], st["v"]), ("draining", 1))
        self.assertEqual(sorted(r["name"] for r in st["runners"]), ["cloud-ci-1", "cloud-ci-3", "cloud-ci-6"])

    def test_operator_disabled_runner_is_never_reenabled(self):
        self.fg.runners[2]["disabled"] = True
        self.s.schedule("op")
        self.assertEqual(self.s.view["skipped"], ["cloud-ci-3"])
        self.s.cancel("op")
        self.assertTrue(self.fg.disabled(2))
        self.assertFalse(self.fg.disabled(1))
        self.assertNotIn((2, False), self.fg.patches)

    def test_second_schedule_refused(self):
        self.s.schedule("op")
        with self.assertRaises(app.Refused):
            self.s.schedule("op")

    def test_concurrent_schedules_exactly_one_wins(self):
        results = []

        def go():
            try:
                self.s.schedule("op")
                results.append("ok")
            except app.Refused:
                results.append("refused")
        ts = [threading.Thread(target=go) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(results), ["ok", "refused", "refused", "refused"])

    def test_zero_cloud_runners_refused(self):
        self.fg.runners = {3: runner(3, "ci-runner-1")}
        with self.assertRaises(app.Refused):
            self.s.schedule("op")
        self.assertIsNone(self.store.data)

    def test_state_write_failure_changes_nothing(self):
        self.store.fail_writes = 1
        with self.assertRaises(app.StateError):
            self.s.schedule("op")
        self.assertEqual(self.fg.patches, [])
        self.assertIsNone(self.s.state)

    def test_gitea_down_refuses_without_state(self):
        self.fg.fail[("GET", "/orgs/")] = "transport"
        with self.assertRaises(app.GiteaError):
            self.s.schedule("op")
        self.assertIsNone(self.store.data)

    def test_failed_pause_rolls_back_and_retries_failed_reenable(self):
        self.fg.fail[("PATCH", RUNNERS + "/2")] = 500
        with self.assertRaises(app.GiteaError):
            self.s.schedule("op")
        self.assertFalse(self.fg.disabled(1), "the runner paused before the failure is re-enabled")
        self.assertEqual(self.phase(), "releasing", "cloud-ci-3's re-enable failed too; kept for retry")
        self.assertEqual(self.store.schedule()["phase"], "releasing")
        del self.fg.fail[("PATCH", RUNNERS + "/2")]
        self.tick()
        self.assertEqual(self.phase(), "idle")
        self.assertEqual(self.s.last["result"], "error")
        self.assertEqual(self.shutdowns, [])

    def test_no_token_refuses(self):
        s = app.Scheduler(app.Gitea("http://g", ORG, "", transport=self.fg), self.store, list)
        with self.assertRaises(app.GiteaError):
            s.schedule("op")

    def test_malformed_runner_object_refused(self):
        del self.fg.runners[1]["disabled"]
        with self.assertRaises(app.GiteaError):
            self.s.schedule("op")
        self.assertEqual(self.fg.patches, [])


class DrainTests(Base):
    def test_waits_for_in_flight_job_then_needs_two_clear_polls(self):
        self.fg.jobs = [job("gatekeeper", "cloud-ci-1"), job("lint", "ci-runner-1")]
        self.s.schedule("op")
        self.tick(45)                                  # 15 minutes of a running job
        self.assertEqual(self.shutdowns, [])
        self.assertEqual([j["runner"] for j in self.s.view["inflight"]], ["cloud-ci-1"])
        self.fg.jobs = [job("lint", "ci-runner-1")]    # ailab jobs never hold the drain
        self.tick()
        self.assertEqual(self.shutdowns, [], "one clear poll is not enough")
        self.tick()
        self.assertEqual(len(self.shutdowns), 1)
        self.assertEqual(self.phase(), "powering_off")

    def test_busy_flag_alone_also_holds(self):
        self.s.schedule("op")
        self.fg.runners[2]["busy"] = True
        self.tick(5)
        self.assertEqual(self.shutdowns, [])
        self.assertEqual(self.s.view["inflight"][0]["runner"], "cloud-ci-3")

    def test_job_holds_while_busy_flaps_false(self):
        self.fg.jobs = [job("e2e", "cloud-ci-3")]      # busy=False, as seen mid-job on 2026-09-16
        self.s.schedule("op")
        self.tick(5)
        self.assertEqual(self.shutdowns, [])

    def test_job_on_operator_disabled_cloud_runner_still_holds(self):
        self.fg.runners[2]["disabled"] = True
        self.fg.jobs = [job("e2e", "cloud-ci-3")]
        self.s.schedule("op")
        self.tick(5)
        self.assertEqual(self.shutdowns, [])

    def test_gitea_error_is_never_an_all_clear(self):
        self.s.schedule("op")
        self.tick()
        self.fg.fail[("GET", JOBS)] = 502
        self.tick(10)
        self.assertEqual(self.shutdowns, [])
        self.assertIn("Gitea", self.s.view["note"])
        del self.fg.fail[("GET", JOBS)]
        self.tick()
        self.assertEqual(self.shutdowns, [], "the error reset the clear-poll count")
        self.tick()
        self.assertEqual(len(self.shutdowns), 1)

    def test_malformed_job_object_is_not_an_all_clear(self):
        self.fg.jobs = [{"name": "x", "status": "in_progress"}]   # runner_name missing
        self.s.schedule("op")
        self.tick(5)
        self.assertEqual(self.shutdowns, [])

    def test_runner_reenabled_mid_drain_is_paused_again(self):
        self.fg.jobs = [job("x", "cloud-ci-1")]
        self.s.schedule("op")
        self.fg.runners[1]["disabled"] = False
        self.tick()
        self.assertTrue(self.fg.disabled(1))

    def test_new_cloud_runner_mid_drain_is_paused_and_owned(self):
        self.s.schedule("op")
        self.tick()
        self.fg.runners[7] = runner(7, "cloud-ci-7")
        self.tick()
        self.assertTrue(self.fg.disabled(7))
        self.assertIn("cloud-ci-7", [r["name"] for r in self.store.schedule()["runners"]])
        self.assertEqual(self.shutdowns, [], "the re-pause reset the clear count")
        self.s.cancel("op")
        self.assertFalse(self.fg.disabled(7))

    def test_deadline_stalls_instead_of_powering_off(self):
        self.fg.jobs = [job("stuck", "cloud-ci-1")]
        self.s.schedule("op")
        self.tick(dt=11700)
        self.assertEqual(self.shutdowns, [])
        self.assertEqual(self.phase(), "stalled")
        self.assertTrue(self.fg.disabled(1), "runners stay paused while stalled")
        self.tick(5)
        self.assertEqual(self.shutdowns, [])

    def test_paginates_the_job_list(self):
        self.fg.jobs = [job("n%d" % i, "ci-runner-1") for i in range(120)] + [job("late", "cloud-ci-1")]
        self.s.schedule("op")
        self.tick(3)
        self.assertEqual(self.shutdowns, [], "a cloud job on page 3 must still be seen")


class PowerOffTests(Base):
    def drain(self):
        self.s.schedule("op")
        self.tick(2)
        self.assertEqual(len(self.shutdowns), 1)

    def all_down(self):
        self.hosts.update(cloud1=False, cloud2=False, cloud3=False)

    def test_release_needs_hosts_dark_5min_and_runner_offline(self):
        self.drain()
        self.fg.runners[1]["status"] = self.fg.runners[2]["status"] = "offline"
        self.tick(3)
        self.assertTrue(self.fg.disabled(1), "offline in Gitea but its host still answers")
        self.all_down()
        self.fg.runners[2]["status"] = "online"        # Gitea has not noticed yet
        self.tick(14)                                   # 280 s dark: not yet
        self.assertTrue(self.fg.disabled(1), "a few minutes dark could be a network outage")
        self.tick(2)                                    # >= 300 s dark
        self.assertFalse(self.fg.disabled(1) or self.fg.disabled(4))
        self.assertTrue(self.fg.disabled(2), "runner still online in Gitea stays paused")
        self.fg.runners[2]["status"] = "offline"
        self.tick()
        self.assertEqual(self.phase(), "idle")
        self.assertEqual(self.s.last["result"], "off")
        self.assertIsNone(self.store.schedule())

    def test_network_outage_that_ends_early_releases_nothing(self):
        self.drain()
        self.all_down()                                 # :8006 unreachable AND runners offline
        for r in self.fg.runners.values():
            r["status"] = "offline"
        self.tick(10)                                   # 200 s
        self.hosts.update(cloud1=True, cloud2=True, cloud3=True)
        for r in self.fg.runners.values():
            r["status"] = "online"
        self.tick(5)
        self.assertTrue(self.fg.disabled(1) and self.fg.disabled(2))
        self.assertEqual(self.phase(), "powering_off")

    def test_hosts_woken_after_confirmed_dark_releases(self):
        self.drain()
        self.all_down()
        self.tick(16)                                   # confirmed dark; Gitea still says online
        self.assertTrue(self.fg.disabled(1))
        self.hosts.update(cloud1=True)                  # ON pressed / RTC wake
        self.tick()
        self.assertFalse(self.fg.disabled(1) or self.fg.disabled(2))
        self.assertEqual(self.phase(), "idle")
        self.assertEqual(self.s.last["result"], "off")

    def test_host_that_never_goes_down_stalls_then_cancel_allowed(self):
        self.drain()
        self.hosts.update(cloud1=False, cloud2=False)
        self.tick(dt=2401)
        self.assertEqual(self.phase(), "stalled")
        self.assertIn("cloud3", self.s.view["stall"]["message"])
        self.assertTrue(self.fg.disabled(1) and self.fg.disabled(2))
        self.s.cancel("op")                             # > poweroff.target's 30 min: not mid-shutdown
        self.assertFalse(self.fg.disabled(1) or self.fg.disabled(2))
        self.assertEqual(self.phase(), "idle")

    def test_shutdown_reply_lost_is_waited_for_not_stalled(self):
        self.shutdown_result = [{"node": "cloud1", "result": "ERROR (outcome unknown): timed out"},
                                {"node": "cloud3", "result": "shutdown requested"}]
        self.drain()
        self.assertEqual(self.phase(), "powering_off")
        self.assertEqual(self.store.schedule()["waiting"], ["cloud1", "cloud3"])
        self.hosts.update(cloud1=False, cloud3=False)   # cloud2 was never asked; it may stay up
        self.fg.runners[1]["status"] = self.fg.runners[2]["status"] = "offline"
        self.tick(16)
        self.assertEqual(self.phase(), "idle")

    def test_shutdown_reply_lost_and_host_up_stalls_and_keeps_runners_paused(self):
        self.shutdown_result = [{"node": "cloud1", "result": "ERROR (outcome unknown): timed out"}]
        self.drain()
        self.tick(60)
        self.assertEqual(self.phase(), "powering_off", "uncertain: never released early")
        self.assertTrue(self.fg.disabled(1))
        self.tick(dt=2400)
        self.assertEqual(self.phase(), "stalled")
        self.assertTrue(self.fg.disabled(1))

    def test_nothing_sent_stalls_at_once_and_cancel_is_allowed(self):
        self.shutdown_result = [{"node": "cloud1", "result": "REFUSED (not sent): pin mismatch"},
                                {"node": "cloud3", "result": "already off"}]
        self.drain()
        self.assertEqual(self.phase(), "stalled")
        self.assertTrue(self.fg.disabled(1))
        self.s.cancel("op")
        self.assertFalse(self.fg.disabled(1))

    def test_every_node_already_off_releases(self):
        self.shutdown_result = [{"node": "cloud1", "result": "already off"}]
        self.fg.runners[1]["status"] = self.fg.runners[2]["status"] = "offline"
        self.drain()
        self.tick(16)
        self.assertEqual(self.phase(), "idle")
        self.assertFalse(self.fg.disabled(1))

    def test_cancel_refused_while_powering_off(self):
        self.drain()
        with self.assertRaises(app.Refused):
            self.s.cancel("op")


class CancelAndRestartTests(Base):
    def test_cancel_reenables_and_goes_idle(self):
        self.fg.jobs = [job("x", "cloud-ci-1")]
        self.s.schedule("op")
        self.s.cancel("op")
        self.assertFalse(self.fg.disabled(1) or self.fg.disabled(2) or self.fg.disabled(4))
        self.assertEqual(self.phase(), "idle")
        self.assertEqual(self.s.last["result"], "cancelled")
        self.tick(5)
        self.assertEqual(self.shutdowns, [])

    def test_cancel_retries_a_failed_reenable(self):
        self.s.schedule("op")
        self.fg.fail[("PATCH", RUNNERS + "/1")] = 500
        self.s.cancel("op")
        self.assertEqual(self.phase(), "releasing")
        del self.fg.fail[("PATCH", RUNNERS + "/1")]
        self.tick()
        self.assertFalse(self.fg.disabled(1))
        self.assertEqual(self.phase(), "idle")

    def test_cancel_state_write_failure_keeps_schedule(self):
        self.s.schedule("op")
        self.store.fail_writes = 1
        with self.assertRaises(app.StateError):
            self.s.cancel("op")
        self.assertEqual(self.phase(), "draining")
        self.assertTrue(self.fg.disabled(1), "nothing re-enabled before the cancel was persisted")

    def test_restart_mid_drain_resumes(self):
        self.fg.jobs = [job("x", "cloud-ci-1")]
        self.s.schedule("op")
        self.s = self.new_sched()                      # the pod restarted
        self.tick()
        self.assertEqual(self.phase(), "draining")
        self.fg.jobs = []
        self.tick(2)
        self.assertEqual(len(self.shutdowns), 1)

    def test_stale_resume_past_the_deadline_does_not_power_off(self):
        self.s.schedule("op")
        self.clock.t += 11 * 3600                      # controller down all night
        self.s = self.new_sched()
        self.tick()
        self.assertEqual(self.shutdowns, [])
        self.assertEqual(self.phase(), "stalled")

    def _persist_unsent_shutdown(self, age):
        self.s.schedule("op")
        st = dict(self.store.schedule(), phase="powering_off", powering_off_at=self.clock.t - age,
                  shutdown_sent=False)
        self.store.data["schedule"] = json.dumps(st)
        self.s = self.new_sched()

    def test_restart_with_unrecorded_shutdown_never_replays(self):
        self._persist_unsent_shutdown(age=30)           # crash mid-dispatch; a host may have woken
        self.tick(5)
        self.assertEqual(self.shutdowns, [], "an unrecorded shutdown is never replayed")
        self.assertEqual(self.phase(), "stalled")

    def test_cancel_after_unrecorded_shutdown_waits_out_a_possible_shutdown(self):
        self._persist_unsent_shutdown(age=30)
        self.tick()
        with self.assertRaises(app.Refused):
            self.s.cancel("op")                         # a host may be shutting down right now
        self.assertTrue(self.fg.disabled(1))
        self.clock.t += 2400
        self.s.cancel("op")
        self.assertFalse(self.fg.disabled(1))
        self.assertEqual(self.phase(), "idle")

    def test_conflict_reloads_instead_of_overwriting(self):
        self.s.schedule("op")
        self.store.conflicts = 1
        self.fg.jobs = []
        self.tick(2)                                   # the power-off transition hits the 409
        self.assertEqual(self.shutdowns, [], "no side effect after a failed persist")
        self.assertFalse(self.s.loaded)
        self.tick(3)                                   # re-read, re-observe, then act
        self.assertEqual(len(self.shutdowns), 1)

    def test_unreadable_state_blocks_scheduling(self):
        class Broken(MemStore):
            def read(self):
                raise app.StateError("GET configmap: HTTP 403")
        self.store = Broken()
        self.s = self.new_sched()
        self.tick()
        self.assertEqual(self.phase(), "unknown")
        with self.assertRaises(app.StateError):
            self.s.schedule("op")
        self.assertEqual(self.fg.patches, [])

    def test_unknown_state_version_fails_closed(self):
        self.store.data, self.store.rv = {"schedule": json.dumps({"v": 99, "phase": "draining"})}, 1
        self.tick()
        self.assertEqual(self.phase(), "unknown")
        with self.assertRaises(app.StateError):
            self.s.schedule("op")
        with self.assertRaises(app.StateError):
            self.s.cancel("op")
        self.assertEqual((self.fg.patches, self.shutdowns), ([], []))


class ImplReviewRegressionTests(Base):
    """codex impl-review round 1: each case reproduced a wrong power-off or a stranded runner."""

    def test_missing_collection_key_is_not_an_all_clear(self):
        self.fg.jobs = [job("gatekeeper", "cloud-ci-1")]
        self.s.schedule("op")
        self.fg.envelope[JOBS] = lambda page, b: {"total_count": b["total_count"]}
        self.tick(5)
        self.assertEqual(self.shutdowns, [])

    def test_missing_total_count_is_refused(self):
        self.s.schedule("op")
        self.fg.envelope[JOBS] = lambda page, b: {"jobs": b["jobs"]}
        self.tick(5)
        self.assertEqual(self.shutdowns, [])

    def test_premature_empty_page_is_refused(self):
        self.fg.jobs = [job("n%d" % i, "ci-runner-1") for i in range(60)] + [job("late", "cloud-ci-1")]
        self.s.schedule("op")
        self.fg.envelope[JOBS] = lambda page, b: dict(b, jobs=[]) if page == 2 else b
        self.tick(5)
        self.assertEqual(self.shutdowns, [])

    def test_mistyped_field_is_refused(self):
        self.fg.runners[1]["busy"] = "no"
        with self.assertRaises(app.GiteaError):
            self.s.schedule("op")

    def test_null_runner_name_counts_as_in_flight(self):
        self.fg.jobs = [job("orphan", None)]
        self.s.schedule("op")
        self.tick(5)
        self.assertEqual(self.shutdowns, [])
        self.assertEqual(self.s.view["inflight"][0]["runner"], "(unknown runner)")

    def test_null_list_with_zero_total_is_empty(self):
        self.s.schedule("op")
        self.fg.envelope[JOBS] = lambda page, b: {"jobs": None, "total_count": 0}
        self.tick(2)
        self.assertEqual(len(self.shutdowns), 1)

    def test_failed_pause_during_configmap_outage_never_resurrects(self):
        self.fg.fail[("PATCH", RUNNERS + "/2")] = 500
        orig_write = self.store.write

        def write_then_break(data, rv):             # the `pausing` write lands, then the outage
            r = orig_write(data, rv)
            self.store.down = True
            return r
        self.store.write = write_then_break
        with self.assertRaises(app.GiteaError):
            self.s.schedule("op")
        self.assertEqual(self.store.schedule()["phase"], "pausing")
        self.assertTrue(self.fg.disabled(1), "nothing re-enabled before `releasing` is durable")
        self.store.write = orig_write
        self.s = self.new_sched()                    # restart during the outage
        self.tick(3)
        self.assertEqual(self.shutdowns, [])
        self.store.down = False
        del self.fg.fail[("PATCH", RUNNERS + "/2")]
        self.tick(2)
        self.assertEqual(self.shutdowns, [], "a failed OFF is never powered off after a restart")
        self.assertFalse(self.fg.disabled(1) or self.fg.disabled(2))
        self.assertEqual(self.phase(), "idle")

    def test_final_write_failure_is_retried_and_blocks_side_effects(self):
        self.s.schedule("op")
        self.store.down = True
        with self.assertRaises(app.StateError):
            self.s.cancel("op")
        self.assertTrue(self.fg.disabled(1), "cancel not durable -> nothing re-enabled")
        self.store.down = False
        self.s.cancel("op")
        self.assertEqual(self.phase(), "idle")
        self.assertIsNone(self.store.schedule())

    def test_new_runner_adopted_before_pause_survives_lost_reply(self):
        self.s.schedule("op")
        self.fg.runners[7] = runner(7, "cloud-ci-7")
        self.fg.fail[("PATCH", RUNNERS + "/7")] = "applied-then-lost"
        self.tick()
        self.assertTrue(self.fg.disabled(7))
        self.assertIn(7, [r["id"] for r in self.store.schedule()["runners"]])
        del self.fg.fail[("PATCH", RUNNERS + "/7")]
        self.s.cancel("op")
        self.assertFalse(self.fg.disabled(7), "the adopted runner is handed back")

    def test_new_runner_not_paused_when_adoption_cannot_be_persisted(self):
        self.s.schedule("op")
        self.fg.runners[7] = runner(7, "cloud-ci-7")
        self.store.down = True
        self.tick()
        self.assertFalse(self.fg.disabled(7), "never paused without a durable owner")
        self.store.down = False
        self.tick()
        self.assertTrue(self.fg.disabled(7))

    def test_on_during_a_schedule_still_being_set_up_cancels_it(self):
        gate = threading.Event()
        self.fg.patch_gate = gate
        t = threading.Thread(target=self.s.schedule, args=("op",))
        t.start()
        while not self.store.data:                   # `pausing` persisted, first PATCH blocked
            pass
        result = {}
        w = threading.Thread(target=lambda: result.update(r=self.s.cancel_for_wake("op")))
        w.start()
        gate.set()
        t.join(5)
        w.join(5)
        self.assertEqual(result["r"][0], True, "ON waited for the schedule, then withdrew it")
        self.fg.patch_gate = None
        self.tick(3)
        self.assertEqual(self.shutdowns, [])
        self.assertFalse(self.fg.disabled(1) or self.fg.disabled(2))

    def test_on_while_powering_off_reports_instead_of_cancelling(self):
        self.s.schedule("op")
        self.tick(2)
        cancelled, note = self.s.cancel_for_wake("op")
        self.assertFalse(cancelled)
        self.assertIn("shutting down", note)

    def test_on_fails_when_the_cancel_cannot_be_persisted(self):
        self.s.schedule("op")
        self.store.down = True
        with self.assertRaises(app.StateError):
            self.s.cancel_for_wake("op")


class GiteaClientTests(unittest.TestCase):
    def test_refuses_a_truncated_list(self):
        fg = FakeGitea([runner(i, "r%d" % i) for i in range(1, 30)])
        g = app.Gitea("http://g", ORG, "T", transport=fg)
        with self.assertRaises(app.GiteaError):
            g._paged(RUNNERS, "runners", app.RUNNER_FIELDS, limit=5, max_pages=2)

    def test_http_error_carries_status(self):
        g = app.Gitea("http://g", ORG, "T", transport=FakeGitea([]))
        with self.assertRaises(app.GiteaError) as cm:
            g.set_disabled(99, True)
        self.assertEqual(cm.exception.status, 404)

    def test_redirects_are_not_followed(self):
        handler = app._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "http://evil/"))


class CsrfTests(unittest.TestCase):
    def same_origin(self, **headers):
        h = type("H", (), {"headers": headers})()
        return app.Handler._same_origin(h)

    def test_dashboard_origin_allowed(self):
        self.assertTrue(self.same_origin(**{"Origin": "https://home.chifor.me", "Sec-Fetch-Site": "same-origin"}))

    def test_cross_site_refused(self):
        self.assertFalse(self.same_origin(**{"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}))
        self.assertFalse(self.same_origin(**{"Origin": "https://evil.example"}))
        self.assertFalse(self.same_origin(**{"Sec-Fetch-Site": "same-site"}))

    def test_non_browser_caller_allowed(self):
        self.assertTrue(self.same_origin())


class StatusEndpointTests(unittest.TestCase):
    def test_status_carries_a_fresh_clock_even_with_a_frozen_snapshot(self):
        import http.server
        import urllib.request as ur

        class Frozen:
            view = {"phase": "draining", "now": 1000.0, "last_tick": 1000.0}
        old = (app.SCHED, app.status, app.ALLOW_FROM)
        app.SCHED, app.status = Frozen(), (lambda: {"nodes": [], "up": 0, "total": 3, "state": "off"})
        app.ALLOW_FROM = [app.ipaddress.ip_network("127.0.0.1/32")]
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            url = "http://127.0.0.1:%d%s/api/status" % (srv.server_address[1], app.BASE)
            body = json.load(ur.urlopen(url, timeout=5))
            self.assertGreater(body["served_at"] - body["schedule"]["last_tick"], 3 * body["poll_sec"],
                               "a hung worker must read as stale to the page")
        finally:
            srv.shutdown()
            srv.server_close()
            app.SCHED, app.status, app.ALLOW_FROM = old


if __name__ == "__main__":
    unittest.main()
