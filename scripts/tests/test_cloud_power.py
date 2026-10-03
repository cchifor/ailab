#!/usr/bin/env python3
"""Unit tests for the scheduled OFF and Auto OFF/ON in kubernetes/apps/apps/cloud-power/app.py.

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


class AmbiguousWriteTests(Base):
    """reviewer-codex on #942: a write that lands but whose response is lost."""

    def _land_then_raise_on(self, nth):
        orig, n = self.store.write, {"i": 0}

        def write(data, rv):
            n["i"] += 1
            r = orig(data, rv)
            if n["i"] == nth:
                raise app.StateError("configmap write: TimeoutError (applied, reply lost)")
            return r
        self.store.write = write
        return orig

    def test_draining_write_landed_reply_lost_is_rolled_back(self):
        orig = self._land_then_raise_on(2)               # 1 = pausing, 2 = draining
        with self.assertRaises(app.StateUncertain):
            self.s.schedule("op")
        self.store.write = orig
        self.assertFalse(self.fg.disabled(1) or self.fg.disabled(2), "rolled back from the stored state")
        self.assertEqual(self.phase(), "idle")
        self.tick(5)
        self.assertEqual(self.shutdowns, [], "an OFF reported as not scheduled never proceeds")

    def test_rejected_attempt_rolled_back_once_the_configmap_is_readable(self):
        self.tick()                                      # loaded while the ConfigMap is fine
        orig = self._land_then_raise_on(2)
        good_read = self.store.read

        def read_then_break():                           # the outage starts after this schedule's load
            raise app.StateError("GET configmap: HTTP 503")
        self.store.read = read_then_break
        with self.assertRaises(app.StateUncertain):
            self.s.schedule("op")
        self.store.write = orig
        self.tick(2)
        self.assertTrue(self.fg.disabled(1), "nothing can be done while the state is unreadable")
        self.store.read = good_read
        self.tick()
        self.assertFalse(self.fg.disabled(1) or self.fg.disabled(2))
        self.tick(5)
        self.assertEqual(self.shutdowns, [])

    def test_draining_write_not_landed_rolls_back(self):
        orig = self.store.write
        n = {"i": 0}

        def write(data, rv):
            n["i"] += 1
            if n["i"] == 2:
                raise app.StateError("configmap write: HTTP 500")
            return orig(data, rv)
        self.store.write = write
        with self.assertRaises(app.StateUncertain):
            self.s.schedule("op")
        self.store.write = orig
        self.assertFalse(self.fg.disabled(1) or self.fg.disabled(2), "rolled back at once")
        self.assertEqual(self.phase(), "idle")
        self.assertEqual(self.s.last["result"], "error")
        self.tick(3)
        self.assertEqual(self.shutdowns, [])

    def test_pausing_write_landed_reply_lost_is_rolled_back_by_the_tick(self):
        orig = self._land_then_raise_on(1)
        with self.assertRaises(app.StateUncertain):
            self.s.schedule("op")
        self.store.write = orig
        self.assertEqual(self.fg.patches, [], "nothing paused")
        self.tick()
        self.assertEqual(self.phase(), "idle")
        self.assertIsNone(self.store.schedule())

    def test_rejection_reported_definitively_only_when_rollback_is_durable(self):
        orig = self._land_then_raise_on(2)
        with self.assertRaises(app.StateUncertain) as cm:
            self.s.schedule("op")
        self.store.write = orig
        self.assertIn("OFF not scheduled", str(cm.exception))
        self.assertIsNone(self.store.schedule(), "the rollback is stored before that answer")

    def test_restart_before_the_rollback_is_stored_is_reported_as_unknown(self):
        self.tick()
        orig = self._land_then_raise_on(2)
        good_read = self.store.read

        def broken():
            raise app.StateError("GET configmap: HTTP 503")
        self.store.read = broken
        with self.assertRaises(app.StateUncertain) as cm:
            self.s.schedule("op")
        self.assertIn("OUTCOME UNKNOWN", str(cm.exception))
        self.assertIn("CANCEL", str(cm.exception))
        # The documented residual: a restart before the store recovers follows the stored OFF,
        # and it is visible and cancellable on the dashboard.
        self.store.write, self.store.read = orig, good_read
        self.s = self.new_sched()
        self.tick()
        self.assertEqual(self.phase(), "draining")
        self.s.cancel("op")
        self.tick(3)
        self.assertEqual(self.shutdowns, [])
        self.assertFalse(self.fg.disabled(1) or self.fg.disabled(2))

    def test_cancel_write_not_landed_is_retried(self):
        self.s.schedule("op")
        self.store.fail_writes = 1
        with self.assertRaises(app.StateUncertain):
            self.s.cancel("op")
        self.assertTrue(self.fg.disabled(1), "no re-enable before the cancel is durable")
        self.tick()
        self.assertFalse(self.fg.disabled(1) or self.fg.disabled(2))
        self.assertEqual(self.phase(), "idle")
        self.assertEqual(self.s.last["result"], "cancelled")
        self.tick(3)
        self.assertEqual(self.shutdowns, [])

    def test_unreadable_configmap_reply_is_a_state_error(self):
        class Garbage:
            def _call(self, method, path, body=None):
                return 200, b"<html>proxy</html>"
        ks = app.KubeState("ns", "cm")
        ks._call = Garbage()._call
        with self.assertRaises(app.StateError):
            ks.read()
        with self.assertRaises(app.StateError):
            ks.write({"schedule": ""}, "1")

    def test_cancel_write_landed_reply_lost_is_honoured(self):
        self.s.schedule("op")
        orig = self._land_then_raise_on(1)
        with self.assertRaises(app.StateUncertain):
            self.s.cancel("op")
        self.store.write = orig
        self.tick()
        self.assertFalse(self.fg.disabled(1) or self.fg.disabled(2))
        self.assertEqual(self.phase(), "idle")
        self.tick(3)
        self.assertEqual(self.shutdowns, [])


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


# --- Auto OFF / Auto ON (plans/2026-10-03-cloud-power-auto-on-off-plan.md) ------------------------
try:
    BUC = app.zoneinfo.ZoneInfo("Europe/Bucharest")
except Exception:  # noqa: BLE001 - no tz database on this machine: the DST cases are skipped
    BUC = None
FIXED = app.datetime.timezone(app.datetime.timedelta(hours=3))


def local(y, mo, d, h, mi, s=0, tz=None):
    return app.datetime.datetime(y, mo, d, h, mi, s, tzinfo=tz or BUC or FIXED).timestamp()


class AutoBase(Base):
    TZ = None

    def setUp(self):
        super().setUp()
        self.clock.t = local(2026, 10, 3, 12, 0, tz=self.tz())
        self.s = self.new_auto_sched()

    def tz(self):
        return self.TZ or BUC or FIXED

    def new_auto_sched(self, **kw):
        def shutdown():
            self.shutdowns.append(self.clock())
            return [dict(r) for r in self.shutdown_result]
        kw.setdefault("tz", self.tz())
        return app.Scheduler(app.Gitea("http://gitea:3000", ORG, "T", transport=self.fg), self.store,
                             shutdown, host_up_fn=lambda n: self.hosts[n], clock=self.clock,
                             prefix="cloud-ci-", drain_max=11700, offline_wait=2400, clear_polls=2, **kw)

    def at(self, h, mi, s=0, day=3, month=10):
        self.clock.t = local(2026, month, day, h, mi, s, tz=self.tz())

    def stored_auto(self):
        return json.loads(self.store.data["auto"])

    def enable_off(self, at="22:00"):
        self.s.set_auto("off", True, at, "op@x")

    def by(self):
        return (self.s.state or {}).get("by")


class AutoOffTests(AutoBase):
    def test_absent_settings_are_the_safe_defaults_and_nothing_fires(self):
        self.store.data = {"schedule": "", "last": ""}
        self.store.rv = 1
        self.s = self.new_auto_sched()
        self.at(22, 0, 10)
        self.s.tick()
        self.assertIsNone(self.s.state)
        a = self.s.view["auto"]
        self.assertEqual((a["off"]["enabled"], a["off"]["at"], a["on"]["enabled"], a["on"]["at"]),
                         (False, "22:00", True, "08:00"))

    def test_fires_once_in_its_slot_through_the_button_path(self):
        self.enable_off()
        self.at(21, 59, 50)
        self.s.tick()
        self.assertIsNone(self.s.state, "not before its time")
        self.at(22, 0, 10)
        self.s.tick()
        self.assertEqual((self.phase(), self.by()), ("draining", "auto-off 22:00"))
        self.assertTrue(self.fg.disabled(1) and self.fg.disabled(2), "the same pause as the button")
        a = self.stored_auto()["off"]
        self.assertEqual((a["slot"], a["result"]), ("2026-10-03T22:00", "scheduled"))
        self.s.cancel("op")
        n = len(self.fg.patches)
        self.tick(30)
        self.assertEqual(self.phase(), "idle", "a cancelled auto-off is never re-fired in its window")
        self.assertEqual(len(self.fg.patches), n)

    def test_grace_window_bounds(self):
        self.enable_off()
        self.at(23, 0, 0)                            # elapsed == grace: too late
        self.s.tick()
        self.assertIsNone(self.s.state)
        self.at(22, 59, 59)
        self.s = self.new_auto_sched()
        self.s.tick()
        self.assertEqual(self.by(), "auto-off 22:00")

    def test_restart_after_the_claim_never_fires_twice(self):
        self.enable_off()
        self.at(22, 0, 10)
        self.s.tick()
        n = len(self.fg.patches)
        self.s.cancel("op")
        self.s = self.new_auto_sched()               # controller restart, same ConfigMap
        self.tick(5)
        self.assertEqual(self.phase(), "idle")
        self.assertEqual(len(self.fg.patches), n + 3, "only CANCEL's three re-enables")

    def test_claim_write_lost_then_flushed_acts_exactly_once(self):
        self.enable_off()
        self.at(22, 0, 10)
        self.store.fail_writes = 1
        self.s.tick()
        self.assertIsNone(self.s.state, "nothing acts before the claim is durable")
        self.assertEqual(self.fg.patches, [])
        self.tick()
        self.assertEqual(self.by(), "auto-off 22:00")
        self.assertEqual(self.stored_auto()["off"]["result"], "scheduled")

    def test_claim_conflict_rereads_then_acts_once(self):
        self.enable_off()
        self.at(22, 0, 10)
        self.store.conflicts = 1
        self.s.tick()
        self.assertIsNone(self.s.state)
        self.tick()
        self.assertEqual(self.by(), "auto-off 22:00")

    def test_manual_off_in_flight_consumes_the_slot(self):
        self.enable_off()
        self.at(21, 55)
        self.s.schedule("op@x")
        self.at(22, 0, 10)
        self.s.tick()
        self.assertEqual(self.stored_auto()["off"]["result"], "not run: an OFF was already draining")
        self.s.cancel("op@x")
        self.tick(30)
        self.assertEqual(self.phase(), "idle", "cancelling the manual OFF does not unleash the auto one")

    def test_on_inside_the_window_consumes_the_slot(self):
        self.enable_off()
        self.at(22, 0, 5)
        self.s.cancel_for_wake("op@x")               # before the worker's first tick in the slot
        self.tick(10)
        self.assertIsNone(self.s.state)
        self.assertEqual(self.stored_auto()["off"]["result"], "not run: ON pressed by op@x")

    def test_on_during_an_auto_off_drain_cancels_it(self):
        self.enable_off()
        self.at(22, 0, 10)
        self.s.tick()
        cancelled, _ = self.s.cancel_for_wake("op@x")
        self.assertTrue(cancelled)
        self.tick(10)
        self.assertEqual(self.phase(), "idle")

    def test_settings_change_inside_the_window_never_fires(self):
        self.at(22, 10)
        self.enable_off("22:00")
        self.tick(5)
        self.assertIsNone(self.s.state)
        self.assertIn("changed by op@x", self.stored_auto()["off"]["result"])
        self.at(22, 0, 10, day=4)                    # but tomorrow's slot does
        self.s.tick()
        self.assertEqual(self.by(), "auto-off 22:00")

    def test_moving_the_time_into_the_past_window_never_fires(self):
        self.enable_off("23:00")
        self.at(22, 30)
        self.s.set_auto("off", True, "22:15", "op@x")
        self.tick(5)
        self.assertIsNone(self.s.state)

    def test_disabling_leaves_an_off_in_flight_alone(self):
        self.enable_off()
        self.at(22, 0, 10)
        self.s.tick()
        self.s.set_auto("off", False, "22:00", "op@x")
        self.assertEqual(self.phase(), "draining", "CANCEL stops a drain, the switch does not")

    def test_no_host_answering_is_recorded_not_acted_on(self):
        self.enable_off()
        self.hosts = {"cloud1": False, "cloud2": False, "cloud3": False}
        self.at(22, 0, 10)
        self.s.tick()
        self.assertIsNone(self.s.state)
        self.assertEqual(self.fg.patches, [])
        self.assertIn("no host answered", self.stored_auto()["off"]["result"])

    def test_refused_is_recorded_and_not_retried(self):
        self.enable_off()
        self.fg.runners = {3: runner(3, "ci-runner-1")}
        self.at(22, 0, 10)
        self.tick(5)
        self.assertIsNone(self.s.state)
        self.assertTrue(self.stored_auto()["off"]["result"].startswith("failed: no cloud-ci-* runner"))

    def test_gitea_error_is_recorded_and_not_retried(self):
        self.enable_off()
        self.fg.fail[("GET", "/orgs/")] = "transport"
        self.at(22, 0, 10)
        self.s.tick()
        self.assertTrue(self.stored_auto()["off"]["result"].startswith("failed:"))
        del self.fg.fail[("GET", "/orgs/")]
        self.tick(5)
        self.assertIsNone(self.s.state, "one attempt per slot")

    def test_crash_inside_schedule_is_reported_not_retried(self):
        self.enable_off()
        self.at(22, 0, 10)
        self.s.tick()
        # Simulate a stored `starting` claim whose OFF never got going (crash before pausing).
        st = json.loads(self.store.data["auto"])
        st["off"]["result"] = "starting"
        self.store.data = {"schedule": "", "last": "", "auto": json.dumps(st)}
        self.s = self.new_auto_sched()
        self.tick()
        self.assertIsNone(self.s.state)
        self.assertIn("interrupted", self.stored_auto()["off"]["result"])

    def test_slot_before_midnight_fires_after_midnight(self):
        self.enable_off("23:50")
        self.at(0, 10, day=4)
        self.s.tick()
        self.assertEqual(self.by(), "auto-off 23:50")
        self.assertEqual(self.stored_auto()["off"]["slot"], "2026-10-03T23:50")

    def test_no_time_zone_disables_auto_off(self):
        self.s = self.new_auto_sched(tz=None, tz_error="AUTO_TZ unusable")
        self.enable_off()
        self.at(22, 0, 10)
        self.s.tick()
        self.assertIsNone(self.s.state)
        self.assertEqual(self.s.view["auto"]["tz_error"], "AUTO_TZ unusable")

    def test_status_view_labels_the_next_slot_in_local_time(self):
        self.enable_off()
        self.at(12, 0)
        self.s.tick()
        off = self.s.view["auto"]["off"]
        self.assertTrue(off["next_label"].startswith("Sat 22:00"), off["next_label"])
        self.assertEqual(off["next_at"], local(2026, 10, 3, 22, 0, tz=self.tz()))


class AutoSettingsTests(AutoBase):
    def test_round_trips_with_the_schedule(self):
        self.s.set_auto("on", False, "07:30", "op@x")
        self.s.schedule("op@x")                      # a schedule write must not drop the settings
        a = self.stored_auto()
        self.assertEqual(a["on"], {"enabled": False, "at": "07:30"})
        self.assertEqual(self.store.schedule()["phase"], "draining")

    def test_conflict_reapplies_only_the_requested_change(self):
        self.s.set_auto("on", False, "07:30", "op@x")
        other = json.loads(self.store.data["auto"])
        other["off"]["enabled"] = True               # someone else saved Auto OFF meanwhile
        self.store.data = dict(self.store.data, auto=json.dumps(other))
        self.store.rv += 1
        self.s.set_auto("on", True, "09:00", "op@x")
        a = self.stored_auto()
        self.assertEqual((a["on"]["at"], a["off"]["enabled"]), ("09:00", True))

    def test_ambiguous_write_is_not_reported_saved(self):
        self.store.fail_writes = 1
        with self.assertRaises(app.StateUncertain):
            self.s.set_auto("on", False, "08:00", "op@x")
        self.assertIsNone(app.desired_from_view(self.s.view), "an unconfirmed value is never mirrored")
        self.s.tick()                                # re-reads what is actually stored
        self.assertEqual(app.desired_from_view(self.s.view), "auto-on=1 wake=08:00")

    def test_refused_while_unsaved_state_is_pending(self):
        self.s.dirty = True
        with self.assertRaises(app.StateError):
            self.s.set_auto("on", False, "08:00", "op@x")

    def test_invalid_stored_value_is_preserved_and_suspends_automation(self):
        self.store.data = {"schedule": "", "last": "", "auto": "{not json"}
        self.store.rv = 1
        self.s = self.new_auto_sched()
        self.at(22, 0, 10)
        self.s.tick()
        self.assertIsNone(self.s.state)
        self.assertIsNone(app.desired_from_view(self.s.view), "never a default in place of an unreadable value")
        self.s.schedule("op@x")                      # manual OFF is unaffected
        self.assertEqual(self.store.data["auto"], "{not json", "written back verbatim")
        self.assertIn("parse", self.s.view["auto"]["error"])
        self.s.cancel("op@x")
        self.s.set_auto("on", True, "08:00", "op@x")  # an explicit save replaces it
        self.assertEqual(self.stored_auto()["on"], {"enabled": True, "at": "08:00"})

    def test_unknown_version_and_bad_shapes_are_invalid(self):
        good = app.default_auto()
        for bad in ({**good, "v": 2}, {**good, "extra": 1},
                    {**good, "on": {"enabled": "yes", "at": "08:00"}},
                    {**good, "on": {"enabled": True, "at": "8:00"}},
                    {**good, "on": {"enabled": True, "at": "08:00\n"}},
                    {**good, "off": {**good["off"], "slot": "yesterday"}}):
            a, err = app.parse_auto(json.dumps(bad))
            self.assertIsNone(a, bad)
            self.assertTrue(err)

    def test_hhmm_is_strict_ascii(self):
        for ok in ("00:00", "08:00", "23:59"):
            self.assertTrue(app.valid_hhmm(ok))
        for bad in ("24:00", "8:00", "08:60", "08:00\n", "٠٨:00", "", None, 800):
            self.assertFalse(app.valid_hhmm(bad), repr(bad))


@unittest.skipIf(BUC is None, "no Europe/Bucharest tz data on this machine")
class AutoDstTests(unittest.TestCase):
    def test_spring_forward_gap_resolves_to_a_real_instant(self):
        # 2026-03-29: 03:00 EET -> 04:00 EEST. 03:30 does not exist; fold=0 = 01:30 UTC.
        ts = app._slot_ts(app.datetime.date(2026, 3, 29), "03:30", BUC)
        utc = app.datetime.datetime(2026, 3, 29, 1, 30, tzinfo=app.datetime.timezone.utc).timestamp()
        self.assertEqual(ts, utc)
        key, slot = app.auto_slot(ts + 60, "03:30", BUC)
        self.assertEqual((key, slot), ("2026-03-29T03:30", ts))

    def test_repeated_autumn_hour_is_one_slot_at_its_first_occurrence(self):
        # 2026-10-25: 04:00 EEST -> 03:00 EET. 03:30 happens twice; fold=0 = 00:30 UTC.
        first = app._slot_ts(app.datetime.date(2026, 10, 25), "03:30", BUC)
        utc = app.datetime.datetime(2026, 10, 25, 0, 30, tzinfo=app.datetime.timezone.utc).timestamp()
        self.assertEqual(first, utc)
        k1, _ = app.auto_slot(first + 10, "03:30", BUC)
        k2, s2 = app.auto_slot(first + 3610, "03:30", BUC)   # the second 03:30 on the wall clock
        self.assertEqual((k1, k2, s2), ("2026-10-25T03:30", "2026-10-25T03:30", first))

    def test_grace_is_measured_in_real_seconds_across_the_change(self):
        # 02:30 EEST on 2026-10-25 is 23:30 UTC; an hour of real time later is 03:30 EEST (first).
        slot = app._slot_ts(app.datetime.date(2026, 10, 25), "02:30", BUC)
        self.assertEqual(app.auto_slot(slot + 3599, "02:30", BUC)[1], slot)


class AutoReviewRegressionTests(AutoBase):
    """codex impl-review round 1 of cloud-power-auto-on-off: each test reproduced its finding."""

    def count_schedules(self):
        calls, orig = [], self.s.schedule

        def counted(who):
            calls.append(who)
            return orig(who)
        self.s.schedule = counted
        return calls

    def test_settings_save_after_an_unconfirmed_cancel_still_cancels(self):
        self.s.schedule("op")
        self.store.fail_writes = 1
        with self.assertRaises(app.StateUncertain):
            self.s.cancel("op")
        self.s.set_auto("on", True, "08:00", "op")   # used to load() and drop the pending CANCEL
        self.tick(5)
        self.assertEqual(self.phase(), "idle")
        self.assertEqual(self.shutdowns, [])

    def test_on_after_a_failed_claim_write_still_stops_the_auto_off(self):
        self.enable_off()
        self.at(22, 0, 10)
        self.store.fail_writes = 1
        self.s.tick()                                # claim not durable: dirty
        self.s.cancel_for_wake("op")                 # flushes the claim AND its withdrawal
        self.tick(10)
        self.assertIsNone(self.s.state)
        self.assertEqual(self.stored_auto()["off"]["result"], "not run: ON pressed by op")

    def test_on_fails_when_the_withdrawal_cannot_be_saved(self):
        self.enable_off()
        self.at(22, 0, 5)
        self.store.down = True
        with self.assertRaises(app.StateError):
            self.s.cancel_for_wake("op")
        self.store.down = False
        self.tick(10)
        self.assertIsNone(self.s.state, "even unsaved, the withdrawal holds in this process")

    def test_cancel_at_the_slot_does_not_start_an_auto_off(self):
        self.enable_off()
        self.at(21, 55)
        self.s.schedule("op")
        self.at(22, 0, 5)
        self.s.cancel("op")                          # before the worker's first tick in the slot
        self.tick(10)
        self.assertEqual(self.phase(), "idle")
        self.assertIn("cancelled by op", self.stored_auto()["off"]["result"])

    def test_claim_landed_reply_lost_acts_exactly_once(self):
        self.enable_off()
        calls = self.count_schedules()
        self.at(22, 0, 10)
        orig, n = self.store.write, {"i": 0}

        def write(data, rv):
            n["i"] += 1
            r = orig(data, rv)
            if n["i"] == 1:                          # the claim lands, its reply is lost
                raise app.StateError("configmap write: TimeoutError (applied, reply lost)")
            return r
        self.store.write = write
        self.s.tick()
        self.assertEqual(calls, [], "nothing acts on an unconfirmed claim")
        self.tick(10)
        self.assertEqual(calls, ["auto-off 22:00"])

    def test_a_claim_left_by_a_previous_controller_is_never_retried(self):
        self.enable_off()
        self.at(22, 0, 10)
        a = self.stored_auto()
        a["off"].update(slot="2026-10-03T22:00", result="claimed")
        self.store.data = dict(self.store.data, auto=json.dumps(a))
        self.s = self.new_auto_sched()               # restart
        calls = self.count_schedules()
        self.tick(10)
        self.assertEqual(calls, [])
        self.assertIn("not retried", self.stored_auto()["off"]["result"])

    def test_refused_is_attempted_exactly_once(self):
        self.enable_off()
        calls = self.count_schedules()
        self.fg.runners = {3: runner(3, "ci-runner-1")}
        self.at(22, 0, 10)
        self.tick(20)
        self.assertEqual(calls, ["auto-off 22:00"])

    def test_reenabling_inside_the_window_never_fires(self):
        self.enable_off()
        calls = self.count_schedules()
        self.at(22, 0, 10)
        self.s.set_auto("off", False, "22:00", "op")
        self.s.set_auto("off", True, "22:00", "op")
        self.tick(10)
        self.assertEqual(calls, [])

    def test_a_conflicting_result_write_stops_the_tick_and_never_refires(self):
        self.enable_off()
        calls = self.count_schedules()
        self.at(22, 0, 10)
        orig, n = self.store.write, {"i": 0}

        def write(data, rv):
            n["i"] += 1
            if n["i"] == 4:                          # claim, pausing, draining, RESULT
                self.store.rv = (self.store.rv or 0) + 1
                raise app.StateConflict("configmap write: HTTP 409")
            return orig(data, rv)
        self.store.write = write
        self.s.tick()
        self.assertFalse(self.s.loaded, "memory no longer trusted")
        patches = len(self.fg.patches)
        self.tick(10)
        self.assertEqual(calls, ["auto-off 22:00"])
        self.assertEqual(self.phase(), "powering_off", "the drain carried on after the re-read")
        self.assertGreaterEqual(len(self.fg.patches), patches)

    def test_empty_stored_value_is_invalid_not_default(self):
        a, err = app.parse_auto("")
        self.assertIsNone(a)
        self.assertIn("empty", err)
        self.assertEqual(app.parse_auto(None)[0], app.default_auto())

    def test_unrepresentable_timestamps_are_invalid_and_labels_never_raise(self):
        good = app.default_auto()
        for bad in (float("inf"), 1e100, -1.0, float("nan")):
            raw = json.dumps({**good, "off": {**good["off"], "result_at": bad}})
            self.assertIsNone(app.parse_auto(raw)[0], bad)
        self.assertIsNone(app.local_label(1e100, self.tz()))

    @unittest.skipIf(BUC is None, "no Europe/Bucharest tz data on this machine")
    def test_the_repeated_autumn_hour_fires_once_through_the_scheduler(self):
        self.s.set_auto("off", True, "03:30", "op")
        calls = self.count_schedules()
        first = app._slot_ts(app.datetime.date(2026, 10, 25), "03:30", BUC)
        self.clock.t = first + 10
        self.s.tick()
        self.s.cancel("op")
        self.clock.t = first + 3600 + 10              # 03:30 on the wall clock again
        self.tick(5)
        self.assertEqual(calls, ["auto-off 03:30"])

    @unittest.skipIf(BUC is None, "no Europe/Bucharest tz data on this machine")
    def test_the_missing_spring_hour_still_fires_once(self):
        self.s.set_auto("off", True, "03:30", "op")
        calls = self.count_schedules()
        slot = app._slot_ts(app.datetime.date(2026, 3, 29), "03:30", BUC)
        self.clock.t = slot - 60
        self.s.tick()
        self.clock.t = slot + 10
        self.tick(5)
        self.assertEqual(calls, ["auto-off 03:30"])


class FakePool:
    def __init__(self, comment=""):
        self.comment = comment
        self.gets = self.sets = 0
        self.fail_get = self.fail_set = None
        self.fail_readback = False
        self.set_lands = True
        self.deadlines = []
        self.gate = None                             # threading.Event: get() blocks until set

    def get(self, deadline=None):
        self.deadlines.append(deadline)
        if self.gate is not None:
            self.gate.wait(5)
        self.gets += 1
        if self.fail_get or (self.fail_readback and self.sets):
            raise app.PoolError(self.fail_get or "read-back failed")
        return self.comment

    def set(self, c, deadline=None):
        self.deadlines.append(deadline)
        self.sets += 1
        if self.set_lands:
            self.comment = c
        if self.fail_set:
            raise app.PoolError(self.fail_set)


class PoolMirrorTests(unittest.TestCase):
    def setUp(self):
        self.pool, self.clock = FakePool("auto-on=1 wake=08:00"), Clock()
        self.want = {"v": None}
        self.m = app.PoolMirror(lambda: self.want["v"], self.pool.get, self.pool.set,
                                clock=self.clock, retry_sec=60, verify_sec=300)

    def test_no_desired_value_no_call(self):
        self.m.sync()
        self.assertEqual(self.pool.gets, 0)
        self.assertFalse(self.m.view()["applied"])
        self.assertTrue(self.m.settled(), "nothing durable to push never holds a power-off")

    def test_writes_and_reads_back_within_a_budget(self):
        self.want["v"] = "auto-on=0 wake=08:00"
        self.m.sync()
        self.assertEqual(self.pool.comment, "auto-on=0 wake=08:00")
        self.assertEqual(self.pool.gets, 2, "read, write, READ BACK")
        self.assertTrue(self.m.view()["applied"])
        self.assertTrue(all(d is not None for d in self.pool.deadlines), "every PVE call is bounded")

    def test_lost_put_reply_is_trusted_only_after_the_read_back(self):
        self.want["v"] = "auto-on=0 wake=08:00"
        self.pool.fail_set = "HTTP timeout"
        self.m.sync()
        self.assertTrue(self.m.view()["applied"])
        self.pool.set_lands = False
        self.want["v"] = "auto-on=1 wake=09:00"
        self.m.sync()
        v = self.m.view()
        self.assertFalse(v["applied"])
        self.assertEqual((v["error"], v["verified_policy"]), ("HTTP timeout", "auto-on=0 wake=08:00"))

    def test_a_failed_read_back_is_uncertain_not_the_old_value(self):
        self.want["v"] = "auto-on=1 wake=08:00"
        self.m.sync()
        self.assertTrue(self.m.view()["applied"])
        self.pool.comment = "auto-on=0 wake=08:00"   # changed behind the mirror's back
        self.pool.set_lands, self.pool.fail_set, self.pool.fail_readback = False, "HTTP 500", True
        self.clock.t += 301
        self.m.sync()
        self.assertIsNone(self.m.view()["verified_policy"])
        self.assertFalse(self.m.view()["applied"])

    def test_applied_follows_the_current_setting_not_the_last_attempt(self):
        self.want["v"] = "auto-on=1 wake=08:00"
        self.m.sync()
        self.want["v"] = "auto-on=0 wake=08:00"      # saved, not yet pushed (lock busy, say)
        self.assertFalse(self.m.view()["applied"])
        self.assertFalse(self.m.settled())

    def test_a_queued_attempt_pushes_the_latest_setting(self):
        self.want["v"] = "auto-on=0 wake=08:00"
        self.pool.gate = threading.Event()
        t = threading.Thread(target=self.m.sync)
        t.start()                                    # holds the lock inside get()
        while self.pool.deadlines == []:
            pass
        self.want["v"] = "auto-on=1 wake=09:30"      # a newer save while the first is in flight
        late = threading.Thread(target=lambda: self.m.sync(force=True, wait=5))
        late.start()
        self.pool.gate.set()
        t.join(5)
        late.join(5)
        self.assertEqual(self.pool.comment, "auto-on=1 wake=09:30")
        self.assertTrue(self.m.view()["applied"])

    def test_retry_and_verify_cadence(self):
        self.want["v"] = "auto-on=0 wake=08:00"
        self.pool.fail_get = "cloud1 unreachable"
        self.m.sync()
        self.clock.t += 30
        self.m.sync()
        self.assertEqual(self.pool.gets, 1, "a failing mirror does not hammer PVE every tick")
        self.pool.fail_get = None
        self.clock.t += 31
        self.m.sync()
        self.assertTrue(self.m.view()["applied"])
        n = self.pool.gets
        self.clock.t += 200
        self.m.sync()
        self.assertEqual(self.pool.gets, n, "verified: re-checked every 300 s, not every tick")
        self.clock.t += 101
        self.m.sync()
        self.assertEqual(self.pool.gets, n + 1)

    def test_a_new_desired_value_is_pushed_at_once(self):
        self.want["v"] = "auto-on=1 wake=08:00"
        self.m.sync()
        self.want["v"] = "auto-on=0 wake=08:00"
        self.m.sync()
        self.assertEqual(self.pool.comment, "auto-on=0 wake=08:00")

    def test_desired_comes_only_from_durable_published_settings(self):
        view = {"loaded": True, "dirty": False, "auto": {"on": {"enabled": False, "at": "07:05"}}}
        self.assertEqual(app.desired_from_view(view), "auto-on=0 wake=07:05")
        for bad in (dict(view, loaded=False), dict(view, dirty=True),
                    dict(view, auto={"error": "bad", "on": view["auto"]["on"]}), {}, None):
            self.assertIsNone(app.desired_from_view(bad))


class PolicyGateTests(AutoBase):
    def test_power_off_waits_a_bounded_time_for_the_policy_and_asks_for_a_push(self):
        self.s = self.new_auto_sched(policy_ready=lambda: False, policy_wait_ticks=3)
        self.s.schedule("op")
        self.tick(2)                                 # drained
        self.assertEqual(self.shutdowns, [])
        self.assertTrue(self.s.take_policy_push())
        self.assertFalse(self.s.take_policy_push(), "consumed once")
        self.tick(3)
        self.assertEqual(len(self.shutdowns), 1, "then it powers off regardless")

    def test_a_settled_policy_does_not_delay_the_power_off(self):
        self.s = self.new_auto_sched(policy_ready=lambda: True)
        self.s.schedule("op")
        self.tick(2)
        self.assertEqual(len(self.shutdowns), 1)

    def test_a_failing_policy_check_never_blocks_the_power_off(self):
        def boom():
            raise RuntimeError("pve down")
        self.s = self.new_auto_sched(policy_ready=boom)
        self.s.schedule("op")
        self.tick(2)
        self.assertEqual(len(self.shutdowns), 1)

    def test_on_during_the_policy_wait_still_cancels(self):
        self.s = self.new_auto_sched(policy_ready=lambda: False, policy_wait_ticks=3)
        self.s.schedule("op")
        self.tick(2)
        cancelled, _ = self.s.cancel_for_wake("op")
        self.assertTrue(cancelled, "the wait holds no lock across PVE calls")
        self.tick(5)
        self.assertEqual(self.shutdowns, [])


class AutoEndpointTests(AutoBase):
    def setUp(self):
        super().setUp()
        import http.server
        self.pool = FakePool()
        self.old = (app.SCHED, app.MIRROR, app.ALLOW_FROM, app.MODE, app.status)
        app.SCHED = self.s
        app.MIRROR = app.PoolMirror(lambda: app.desired_from_view(self.s.view), self.pool.get, self.pool.set,
                                    clock=self.clock)
        app.ALLOW_FROM = [app.ipaddress.ip_network("127.0.0.1/32")]
        app.status = lambda: {"nodes": [], "up": 0, "total": 3, "state": "off"}
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        app.SCHED, app.MIRROR, app.ALLOW_FROM, app.MODE, app.status = self.old

    def post(self, body, headers=None):
        import urllib.request as ur
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = ur.Request("http://127.0.0.1:%d%s/api/auto" % (self.srv.server_address[1], app.BASE),
                         data=data, method="POST", headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        try:
            with ur.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except app.urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_saves_and_mirrors_auto_on(self):
        code, body = self.post({"kind": "on", "enabled": False, "at": "07:45"})
        self.assertEqual(code, 200)
        self.assertEqual(self.pool.comment, "auto-on=0 wake=07:45")
        self.assertTrue(body["auto_on"]["applied"])
        self.assertEqual(self.stored_auto()["on"], {"enabled": False, "at": "07:45"})

    def test_rejects_malformed_bodies(self):
        for bad in ({"kind": "off", "enabled": "true", "at": "22:00"},
                    {"kind": "both", "enabled": True, "at": "22:00"},
                    {"kind": "off", "enabled": True, "at": "22:00", "pool": "other"},
                    {"kind": "off", "enabled": True, "at": "2200"},
                    ["off", True, "22:00"], b"not json", b"x" * 2000):
            code, _ = self.post(bad)
            self.assertEqual(code, 400, bad)
        self.assertIsNone(self.store.data)

    def test_cross_site_refused(self):
        code, _ = self.post({"kind": "off", "enabled": True, "at": "22:00"},
                            {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(code, 403)
        self.assertIsNone(self.store.data)

    def test_the_hostnetwork_wol_half_does_not_serve_it(self):
        app.MODE = "wol"
        code, _ = self.post({"kind": "on", "enabled": False, "at": "08:00"})
        self.assertEqual(code, 404)
        self.assertIsNone(self.store.data)


class AutoReviewRound2Tests(AutoBase):
    """codex impl-review round 2: each test is the reproduction codex described."""

    def fail_nth_write(self, nth, how="lost"):
        """how: 'lost' = not written; 'conflict' = 409 (someone else wrote)."""
        orig, n = self.store.write, {"i": 0}

        def write(data, rv):
            n["i"] += 1
            if n["i"] in (nth if isinstance(nth, tuple) else (nth,)):
                if how == "conflict":
                    self.store.rv = (self.store.rv or 0) + 1
                    raise app.StateConflict("configmap write: HTTP 409")
                raise app.StateError("configmap write: HTTP 500")
            return orig(data, rv)
        self.store.write = write
        return orig

    def restart(self):
        self.s = self.new_auto_sched()

    def test_a_conflict_while_reapplying_a_cancel_keeps_the_intent(self):
        self.s.schedule("op")
        self.fail_nth_write(1)                       # the CANCEL write: not landed
        with self.assertRaises(app.StateUncertain):
            self.s.cancel("op")
        self.fail_nth_write(1, "conflict")           # the reconciliation write: 409
        with self.assertRaises(app.StateError):
            self.s.set_auto("on", True, "08:00", "op")
        self.tick(10)
        self.assertEqual(self.phase(), "idle")
        self.assertEqual(self.shutdowns, [])

    def test_a_repeated_cancel_is_not_reported_done_while_unsaved(self):
        self.s.schedule("op")
        self.store.down = True
        with self.assertRaises(app.StateError):
            self.s.cancel("op")
        with self.assertRaises(app.StateError):
            self.s.cancel("op")                      # still not stored: no false success
        self.store.down = False
        self.tick(5)
        self.assertEqual(self.phase(), "idle")
        self.assertEqual(self.shutdowns, [])

    def test_a_retried_on_persists_the_withdrawal(self):
        self.enable_off()
        self.at(22, 0, 5)
        self.fail_nth_write(1)                       # ON's withdrawal: not landed
        with self.assertRaises(app.StateError):
            self.s.cancel_for_wake("op")
        self.s.cancel_for_wake("op")                 # retried: must make it durable
        self.restart()
        self.tick(10)
        self.assertIsNone(self.s.state)
        self.assertEqual(self.stored_auto()["off"]["result"], "not run: ON pressed by op")

    def test_a_settings_change_beats_a_claim_that_hit_a_conflict(self):
        self.enable_off()
        self.at(22, 0, 10)
        self.fail_nth_write(1, "conflict")           # the claim
        self.s.tick()
        self.s.set_auto("off", False, "22:00", "op")
        self.s.set_auto("off", True, "22:00", "op")
        self.tick(10)
        self.assertIsNone(self.s.state)

    def test_a_retried_cancel_keeps_the_slot_consumed_across_a_restart(self):
        self.enable_off()
        self.at(21, 55)
        self.s.schedule("op")
        self.at(22, 0, 5)
        self.fail_nth_write(1)                       # the first CANCEL: not landed
        with self.assertRaises(app.StateUncertain):
            self.s.cancel("op")
        self.s.cancel("op")                          # retried successfully
        self.restart()                               # before the worker's next tick
        self.tick(10)
        self.assertEqual(self.phase(), "idle")
        self.assertIn("cancelled by op", self.stored_auto()["off"]["result"])

    def test_the_policy_wait_never_turns_a_finished_drain_into_a_stall(self):
        self.s = self.new_auto_sched(policy_ready=lambda: False, policy_wait_ticks=3)
        self.s.drain_max = 50                        # deadline lands inside the policy wait
        self.s.schedule("op")
        self.tick(6)
        self.assertNotEqual(self.phase(), "stalled")
        self.assertEqual(len(self.shutdowns), 1)

    def test_integer_overflow_in_a_timestamp_is_invalid_not_a_crash(self):
        good = app.default_auto()
        raw = json.dumps(good).replace('"result_at": null', '"result_at": 1' + "0" * 400)
        a, err = app.parse_auto(raw)
        self.assertIsNone(a)
        self.assertTrue(err)


class MirrorRound2Tests(unittest.TestCase):
    def test_an_obsolete_attempt_never_writes_and_holds_the_gate(self):
        pool, clock, want = FakePool("auto-on=1 wake=08:00"), Clock(), {"v": "auto-on=1 wake=08:00"}
        m = app.PoolMirror(lambda: want["v"], pool.get, pool.set, clock=clock)
        m.sync()
        self.assertTrue(m.settled())
        want["v"] = "auto-on=0 wake=08:00"
        pool.gate = threading.Event()
        pool.deadlines = []
        t = threading.Thread(target=m.sync)
        t.start()                                    # blocked inside its GET
        while not pool.deadlines:
            pass
        want["v"] = "auto-on=1 wake=08:00"           # re-enabled while it is in flight
        self.assertFalse(m.settled(), "an in-flight attempt for an older policy could still write it")
        pool.gate.set()
        t.join(5)
        self.assertEqual(pool.comment, "auto-on=1 wake=08:00", "the obsolete value was never written")
        self.assertEqual(pool.sets, 0)
        self.assertTrue(m.settled())


class PveDeadlineTests(unittest.TestCase):
    """reviewer-codex on #1049: a real HTTP peer dribbling one byte at a time, through the real
    http.client response object (whose read(n) would wait for all n bytes)."""

    def test_a_dribbling_response_is_cut_at_the_deadline(self):
        import hashlib
        import socket as _s
        import time as _t
        der = b"fake-cert"
        srv = _s.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        stop = threading.Event()

        def serve():
            c, _ = srv.accept()
            c.recv(65536)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
            try:
                while not stop.is_set():
                    c.sendall(b" ")
                    _t.sleep(0.05)
            except OSError:
                pass
            finally:
                c.close()
        threading.Thread(target=serve, daemon=True).start()
        port = srv.getsockname()[1]

        class Sock:
            """The pinned TLS socket, minus TLS: exposes the cert and delegates the rest."""
            def __init__(self, real):
                self._real = real

            def getpeercert(self, binary_form=False):
                return der

            def __getattr__(self, name):
                return getattr(self._real, name)

        class Conn(app.http.client.HTTPConnection):
            def __init__(self, host, port_, context=None, timeout=None):
                super().__init__("127.0.0.1", port, timeout=timeout)

            def connect(self):
                super().connect()
                self.sock = Sock(self.sock)

        old = (app.http.client.HTTPSConnection, dict(app.PVE_FINGERPRINTS))
        app.http.client.HTTPSConnection = Conn
        app.PVE_FINGERPRINTS["t"] = ":".join("%02X" % b for b in hashlib.sha256(der).digest())
        try:
            t0 = _t.time()
            with self.assertRaises(TimeoutError):
                app.pve({"name": "t", "ip": "127.0.0.1"}, "/pools/x", timeout=5, deadline=_t.time() + 0.5)
            self.assertLess(_t.time() - t0, 2, "the budget bounds the whole response, not each read")
        finally:
            stop.set()
            srv.close()
            app.http.client.HTTPSConnection = old[0]
            app.PVE_FINGERPRINTS.clear()
            app.PVE_FINGERPRINTS.update(old[1])


class WolMacTests(unittest.TestCase):
    def test_macs_match_cloudlab_wol_py_when_present(self):
        wol = pathlib.Path(__file__).resolve().parents[3] / "cloudlab" / "scripts" / "wol.py"
        if not wol.exists():
            self.skipTest("no sibling cloudlab checkout")
        import re
        hosts = dict(re.findall(r'"(cloud[0-9])":\s*\("([0-9a-f:]{17})"', wol.read_text(encoding="utf-8")))
        self.assertEqual({n["name"]: n["mac"] for n in app.NODES}, hosts)


if __name__ == "__main__":
    unittest.main()
