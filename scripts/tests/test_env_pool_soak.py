"""scripts/env-pool-soak.py — the V6 soak report's verdict machine.

The script is loaded by path (hyphenated name). Prometheus/Loki are replaced by a FakeSource that
serves canned series/lines; the cases below pin the four verdicts and the rule that a failed
endpoint can never be reported as a clean soak.
Run: python3 -m unittest scripts.tests.test_env_pool_soak  (CI: .gitea/workflows/manifests.yaml)
"""
import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "env-pool-soak.py"

spec = importlib.util.spec_from_file_location("env_pool_soak", SCRIPT)
soak = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = soak  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(soak)

T0 = 1_800_000_000.0  # window start (epoch seconds)
T1 = T0 + 3600  # one-hour window
STEP = soak.STEP_SECONDS


def samples(start, end, value, step=STEP):
    return [[t, str(value)] for t in range(int(start), int(end) + 1, step)]


def series(metric, values):
    return {"metric": metric, "values": values}


class FakeSource:
    """Canned answers keyed by the query name that appears in PROM_*_QUERIES / LOKI_QUERIES."""

    def __init__(self, prom=None, instant=None, loki=None, fail=()):
        self.prom = prom or {}
        self.instant = instant or {}
        self.loki = loki or {}
        self.fail = set(fail)
        self.calls = []

    def _name(self, table, expr):
        for name, e in table.items():
            if e.replace("{window}", "") in expr or expr.startswith(e.split("{window}")[0]):
                return name
        raise AssertionError(f"unknown query {expr!r}")

    def prom_range(self, expr, start, end, step=STEP):
        name = self._name(soak.PROM_RANGE_QUERIES, expr)
        self.calls.append(name)
        if name in self.fail:
            raise soak.SourceError(f"{name} unreachable")
        return self.prom.get(name, [])

    def prom_instant(self, expr, at):
        self.calls.append("stop_errors")
        if "stop_errors" in self.fail:
            raise soak.SourceError("stop_errors unreachable")
        return self.instant.get("stop_errors", [])

    def loki_range(self, expr, start_ns, end_ns):
        name = self._name(soak.LOKI_QUERIES, expr)
        self.calls.append(name)
        if name in self.fail:
            raise soak.SourceError(f"{name} unreachable")
        return self.loki.get(name, []), name in self.loki.get("_truncated", ())


def quiet_prom(node="talos-env-node-1", pod="env-std-pool-abcde"):
    return {
        "node_ready": [series({"node": node, "condition": "Ready", "status": "true"}, samples(T0, T1, 1))],
        "kubelet_up": [series({"node": node, "job": "kubelet"}, samples(T0, T1, 1))],
        "boot_time": [series({"instance": "192.168.0.37:9100"}, samples(T0, T1, 1_799_000_000))],
        "member_age": [series({"pod": pod, "namespace": "testpool"}, [[t, str(t - T0 + 90_000)] for t in range(int(T0), int(T1) + 1, STEP)])],
        "member_ready": [series({}, samples(T0, T1, 1))],
        "restarts": [series({"pod": pod, "container": "control"}, samples(T0, T1, 0))],
        "alerts": [],
    }


def capacity_dip(prom, gone_from, back_at):
    """Warm capacity 0 in [gone_from, back_at) — a rotation seen through kube_pod_status_ready."""
    vals = samples(T0, T1, 1)
    for s in vals:
        if gone_from <= s[0] < back_at:
            s[1] = "0"
    prom["member_ready"] = [series({}, vals)]
    return prom


def quiet_loki():
    beats = [(int((T0 + 600 * i) * 1e9), f"heartbeat iter={i} terminating=0") for i in range(1, 6)]
    relay = [(int((T0 + 300 * i) * 1e9), f'time="x" level=debug msg="health check ok" n={i}') for i in range(0, 13)]
    return {"reaper": beats, "watchdog": [], "relay": relay}


class VerdictTests(unittest.TestCase):
    def run_report(self, src, now=None):
        return soak.run(src, T0, T1, ["talos-env-node-1"], now=now or T1 + 60)

    def test_quiet_window_is_ok(self):
        rep = self.run_report(FakeSource(quiet_prom(), loki=quiet_loki()))
        self.assertEqual(rep.verdict, "OK", rep.markdown())
        self.assertIn("| talos-env-node-1 | 61 | 0 |", rep.markdown())
        self.assertIn("| 26h00m | 0 |", rep.markdown())  # max age reached: 90 000 s + 3 600 s = 26 h

    def test_contained_recurrence(self):
        loki = quiet_loki()
        sb = "a73661aed709877460c8ec29908bffe1"
        loki["reaper"] += [
            (int((T0 + 1800) * 1e9), f"evidence stage=1 pod=env-std-pool-abcde uid=u sandbox={sb} exe=/usr/local/libexec/virtiofsd pid=5 state=T wchan=do_signal_stop threads=3 tstates=T:3"),
            (int((T0 + 1801) * 1e9), f"reap stage=1 pod=env-std-pool-abcde uid=u sandbox={sb} exe=/usr/local/libexec/virtiofsd pid=5 age=153s"),
        ]
        loki["watchdog"] = [(int((T0 + 1500) * 1e9), "ready-watchdog: ready-port closed: virtiofs hung")]
        loki["relay"] += [(int((T0 + 1000) * 1e9), f'level=debug sandbox={sb} msg="vmconsole: INFO: task dockerd blocked"')]
        prom = capacity_dip(quiet_prom(), T0 + 1500, T0 + 1980)
        rep = self.run_report(FakeSource(prom, loki=loki))
        self.assertEqual(rep.verdict, "RECURRENCE-CONTAINED", rep.markdown())
        self.assertTrue(any("reaper kill line" in r for r in rep.recurrences))
        self.assertTrue(any("watchdog closure" in r for r in rep.recurrences))
        self.assertTrue(any("recovery: watchdog closure" in s for s in rep.sections))

    def test_closure_without_recovery_is_unresolved(self):
        loki = quiet_loki()
        loki["watchdog"] = [(int((T0 + 1500) * 1e9), "ready-watchdog: ready-port closed: virtiofs hung")]
        prom = capacity_dip(quiet_prom(), T0 + 1500, T1 + 1)  # never comes back
        rep = self.run_report(FakeSource(prom, loki=loki))
        self.assertEqual(rep.verdict, "UNRESOLVED", rep.markdown())
        self.assertTrue(any("no Ready member observed afterwards" in u for u in rep.unresolved))
        # still open right at the boundary reads as "still open", not as failure
        loki["watchdog"] = [(int((T1 - 120) * 1e9), "ready-watchdog: ready-port closed: virtiofs hung")]
        prom = capacity_dip(quiet_prom(), T1 - 120, T1 + 1)
        rep = self.run_report(FakeSource(prom, loki=loki))
        self.assertEqual(rep.verdict, "UNRESOLVED")
        self.assertTrue(any("still open" in u for u in rep.unresolved))

    def test_slow_recovery_past_bound_is_unresolved(self):
        loki = quiet_loki()
        loki["watchdog"] = [(int((T0 + 600) * 1e9), "ready-watchdog: ready-port closed: dockerd hung")]
        prom = capacity_dip(quiet_prom(), T0 + 600, T0 + 600 + soak.RECOVERY_BOUND_SECONDS + 300)
        rep = self.run_report(FakeSource(prom, loki=loki))
        self.assertEqual(rep.verdict, "UNRESOLVED", rep.markdown())

    def test_any_testpool_alert_firing_is_prevention_failed(self):
        for name in ("TestpoolNoWarmCapacity", "TestpoolOperatorDown", "EnvNodeKubeletMetricsBlocked"):
            prom = quiet_prom()
            prom["alerts"] = [series({"alertname": name, "alertstate": "firing"}, samples(T0 + 60, T0 + 600, 1))]
            rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
            self.assertEqual(rep.verdict, "PREVENTION-FAILED", name)

    def test_failure_keeps_incompleteness_visible(self):
        prom = quiet_prom()
        prom["alerts"] = [series({"alertname": "TestpoolEnvTeardownStuck", "alertstate": "firing"}, samples(T0 + 600, T0 + 1200, 1))]
        loki = quiet_loki()
        loki["relay"] = []
        rep = self.run_report(FakeSource(prom, loki=loki))
        self.assertEqual(rep.verdict, "PREVENTION-FAILED")
        md = rep.markdown()
        self.assertIn("**Prevention failed:**", md)
        self.assertIn("**Incomplete data:**", md)
        self.assertIn("not shipping", md)

    def test_relay_gap_is_incomplete(self):
        loki = quiet_loki()
        loki["relay"] = [l for l in loki["relay"] if l[0] < int((T0 + 600) * 1e9) or l[0] > int((T0 + 2700) * 1e9)]
        rep = self.run_report(FakeSource(quiet_prom(), loki=loki))
        self.assertEqual(rep.verdict, "INCOMPLETE", rep.markdown())
        self.assertTrue(any("relay capture gap" in p for p in rep.problems))

    def test_reap_without_evidence_or_relay_is_incomplete(self):
        loki = quiet_loki()
        sb = "b" * 32
        loki["reaper"] += [(int((T0 + 1801) * 1e9), f"reap stage=1 pod=p uid=u sandbox={sb} exe=/usr/local/bin/cloud-hypervisor pid=7 age=150s")]
        rep = self.run_report(FakeSource(capacity_dip(quiet_prom(), T0 + 1700, T0 + 2100), loki=loki))
        self.assertEqual(rep.verdict, "INCOMPLETE", rep.markdown())
        self.assertTrue(any("without evidence" in p for p in rep.problems))
        self.assertTrue(any("no relay" in p for p in rep.problems))

    def test_node_notready_is_prevention_failed(self):
        prom = quiet_prom()
        vals = samples(T0, T1, 1)
        for s in vals[10:15]:
            s[1] = "0"
        prom["node_ready"] = [series({"node": "talos-env-node-1", "condition": "Ready", "status": "true"}, vals)]
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "PREVENTION-FAILED", rep.markdown())
        self.assertTrue(any("NotReady" in f for f in rep.failures))

    def test_firing_alert_is_prevention_failed_even_if_incomplete(self):
        prom = quiet_prom()
        prom["alerts"] = [series({"alertname": "TestpoolEnvTeardownStuck", "alertstate": "firing"}, samples(T0 + 600, T0 + 1200, 1))]
        rep = self.run_report(FakeSource(prom, loki=quiet_loki(), fail={"relay"}))
        self.assertEqual(rep.verdict, "PREVENTION-FAILED")
        self.assertTrue(any("relay unreachable" in p for p in rep.problems))

    def test_pending_alert_alone_is_not_a_failure(self):
        prom = quiet_prom()
        prom["alerts"] = [series({"alertname": "EnvNodeRuntimeStopErrors", "alertstate": "pending"}, samples(T0, T0 + 300, 1))]
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "OK", rep.markdown())

    def test_failed_endpoint_is_never_ok(self):
        for failing in ("node_ready", "alerts", "reaper", "stop_errors"):
            rep = self.run_report(FakeSource(quiet_prom(), loki=quiet_loki(), fail={failing}))
            self.assertEqual(rep.verdict, "INCOMPLETE", failing)
            self.assertTrue(any("unreachable" in p for p in rep.problems), failing)

    def test_missing_expected_node_is_incomplete(self):
        prom = quiet_prom(node="talos-env-node-9")
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "INCOMPLETE")
        self.assertTrue(any("expected node talos-env-node-1" in p for p in rep.problems))

    def test_heartbeat_gap_and_truncation_are_incomplete(self):
        loki = quiet_loki()
        loki["reaper"] = loki["reaper"][:1]  # one heartbeat at T0+10min, then silence
        rep = self.run_report(FakeSource(quiet_prom(), loki=loki))
        self.assertEqual(rep.verdict, "INCOMPLETE")
        self.assertTrue(any("heartbeat gap" in p for p in rep.problems))
        loki = quiet_loki()
        loki["_truncated"] = ("relay",)
        rep = self.run_report(FakeSource(quiet_prom(), loki=loki))
        self.assertEqual(rep.verdict, "INCOMPLETE")
        self.assertTrue(any("truncated" in p for p in rep.problems))

    def test_window_past_retention_is_incomplete(self):
        rep = self.run_report(FakeSource(quiet_prom(), loki=quiet_loki()), now=T0 + 200 * 3600)
        self.assertEqual(rep.verdict, "INCOMPLETE")
        self.assertTrue(any("retention" in p for p in rep.problems))

    def test_member_replacement_and_reboot_are_recurrences(self):
        prom = quiet_prom()
        old = prom["member_age"][0]["values"][:20]
        new = [[t, str(t - (T0 + 1500))] for t in range(int(T0) + 1500, int(T1) + 1, STEP)]
        prom["member_age"] = [series({"pod": "env-std-pool-old", "namespace": "testpool"}, old), series({"pod": "env-std-pool-new", "namespace": "testpool"}, new)]
        prom["boot_time"] = [series({"instance": "192.168.0.37:9100"}, samples(T0, T0 + 1200, 1_799_000_000) + samples(T0 + 1260, T1, 1_800_001_000))]
        prom = capacity_dip(prom, T0 + 1140, T0 + 1560)
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "RECURRENCE-CONTAINED", rep.markdown())
        self.assertTrue(any("disappeared" in r for r in rep.recurrences))
        self.assertTrue(any("rebooted" in r for r in rep.recurrences))


class PaginationTests(unittest.TestCase):
    """Source.loki_range against a fake HTTP layer: boundary timestamps shared across streams."""

    def make(self, pages):
        src = soak.Source("http://p", "http://l")
        calls = []

        def fake_get(url):
            calls.append(url)
            return pages.pop(0)

        src._get = fake_get
        return src, calls

    @staticmethod
    def page(lines):
        return {"status": "success", "data": {"result": [{"stream": {}, "values": [[str(t), l] for t, l in lines]}]}}

    def test_boundary_timestamp_shared_by_two_streams_is_not_skipped(self):
        soak.LOKI_PAGE, saved = 3, soak.LOKI_PAGE
        try:
            # page 1 (newest) ends at ts 100 with one of two records at 100; page 2 must start AT 100
            p1 = self.page([(300, "c"), (200, "b"), (100, "a1")])
            p2 = self.page([(100, "a2"), (100, "a1"), (50, "z")])
            src, calls = self.make([p1, p2, self.page([])])
            lines, truncated = src.loki_range("{x}", 0, 400)
            self.assertFalse(truncated)
            self.assertEqual([l for _, l in lines], ["z", "a1", "a2", "b", "c"])
            self.assertIn("end=100", calls[1])
        finally:
            soak.LOKI_PAGE = saved

    def test_timestamp_that_alone_fills_a_page_is_reported_truncated(self):
        soak.LOKI_PAGE, saved = 2, soak.LOKI_PAGE
        try:
            same = self.page([(100, "a"), (100, "b")])
            src, _ = self.make([same, same, same])
            lines, truncated = src.loki_range("{x}", 0, 400)
            self.assertTrue(truncated)
            self.assertEqual(len(lines), 2)
        finally:
            soak.LOKI_PAGE = saved


class HelperTests(unittest.TestCase):
    def test_intervals_and_gaps(self):
        vals = [(T0, 1.0), (T0 + 60, 0.0), (T0 + 120, 0.0), (T0 + 180, 1.0), (T0 + 600, 1.0)]
        self.assertEqual(soak.intervals_where(vals, lambda v: v < 1), [(T0 + 60, T0 + 180)])
        self.assertEqual(soak.sample_gaps(vals), [(T0 + 180, T0 + 600)])

    def test_parse_ts_accepts_z_and_offsets(self):
        self.assertEqual(soak.parse_ts("2026-09-21T00:00:00Z"), soak.parse_ts("2026-09-21T03:00:00+03:00"))


if __name__ == "__main__":
    unittest.main()
