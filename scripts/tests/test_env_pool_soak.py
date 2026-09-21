"""scripts/env-pool-soak.py — the V6 soak report's verdict machine.

The script is loaded by path (hyphenated name). Prometheus/Loki are replaced by a FakeSource that
serves canned series/lines; the cases below pin the verdicts (a total order), the coverage rules
(absence of observations never proves anything), incident/recovery evaluation from the warm
pool's own readyReplicas, per-PID evidence matching, source-timestamp freshness, and Loki's
exclusive-end pagination against a corpus-backed fake.
Run: python3 -m unittest scripts.tests.test_env_pool_soak  (CI: .gitea/workflows/manifests.yaml)
"""
import importlib.util
import pathlib
import sys
import unittest
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "env-pool-soak.py"

spec = importlib.util.spec_from_file_location("env_pool_soak", SCRIPT)
soak = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = soak  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(soak)

T0 = 1_800_000_000.0  # window start (epoch seconds)
T1 = T0 + 3600  # one-hour window
STEP = soak.STEP_SECONDS
NODE = "talos-env-node-1"
POD = "env-std-pool-abcde"
SB = "a73661aed709877460c8ec29908bffe1"


def samples(start, end, value, step=STEP):
    return [[t, str(value)] for t in range(int(start), int(end) + 1, step)]


def series(metric, values):
    return {"metric": metric, "values": values}


def src_line(ts, msg):
    """A containerd record as the relay ships it: its own time= field is the event time."""
    return f'{{"level":"debug","msg":"{msg}","time":"{soak.iso(ts)}"}}'


class FakeSource:
    """Canned answers keyed by the query name that appears in PROM_*_QUERIES / LOKI_QUERIES."""

    def __init__(self, prom=None, instant=None, loki=None, fail=()):
        self.prom = prom or {}
        self.instant = instant or {}
        self.loki = loki or {}
        self.fail = set(fail)

    def _name(self, table, expr):
        for name, e in table.items():
            if e.replace("{window}", "") in expr or expr.startswith(e.split("{window}")[0]):
                return name
        raise AssertionError(f"unknown query {expr!r}")

    def prom_range(self, expr, start, end, step=STEP):
        name = self._name(soak.PROM_RANGE_QUERIES, expr)
        if name in self.fail:
            raise soak.SourceError(f"{name} unreachable")
        return self.prom.get(name, [])

    def prom_instant(self, expr, at):
        if "stop_errors" in self.fail:
            raise soak.SourceError("stop_errors unreachable")
        return self.instant.get("stop_errors", [])

    def loki_range(self, expr, start_ns, end_ns):
        name = self._name(soak.LOKI_QUERIES, expr)
        if name in self.fail:
            raise soak.SourceError(f"{name} unreachable")
        return self.loki.get(name, []), name in self.loki.get("_truncated", ())


def quiet_prom(node=NODE, pod=POD):
    return {
        "node_ready": [series({"node": node, "condition": "Ready", "status": "true"}, samples(T0, T1, 1))],
        "kubelet_up": [series({"node": node, "job": "kubelet"}, samples(T0, T1, 1))],
        "boot_time": [series({"instance": "192.168.0.37:9100"}, samples(T0, T1, 1_799_000_000))],
        "member_age": [series({"pod": pod, "namespace": "testpool"}, [[t, str(t - T0 + 90_000)] for t in range(int(T0), int(T1) + 1, STEP)])],
        "warm_ready": [series({"name": "env-std-pool", "exported_namespace": "testpool"}, samples(T0, T1, 1))],
        "warm_spec": [series({"name": "env-std-pool", "exported_namespace": "testpool"}, samples(T0, T1, 1))],
        "restarts": [series({"pod": pod, "container": "control"}, samples(T0, T1, 0))],
        "alerts": [],
    }


def capacity_dip(prom, gone_from, back_at):
    """Warm capacity 0 in [gone_from, back_at) — the pool's readyReplicas as KSM reports it."""
    vals = samples(T0, T1, 1)
    for s in vals:
        if gone_from <= s[0] < back_at:
            s[1] = "0"
    prom["warm_ready"] = [series({"name": "env-std-pool", "exported_namespace": "testpool"}, vals)]
    return prom


def quiet_loki():
    beats = [(int((T0 + 600 * i) * 1e9), f"heartbeat iter={i} terminating=0") for i in range(1, 6)]
    relay = [(int((T0 + 300 * i) * 1e9), src_line(T0 + 300 * i, f"health check ok n={i}")) for i in range(0, 13)]
    return {"reaper": beats, "watchdog": [], "relay": relay}


def reap_lines(loki, at, sb=SB, pid=5, with_evidence=True, incomplete=False, stage=1, header_then_timeout=False):
    if header_then_timeout:  # the shape the shell harness produces: state= header, then the timeout marker
        loki["reaper"].append((int(at * 1e9), f"evidence stage=1 pod={POD} uid=u sandbox={sb} exe=/usr/local/libexec/virtiofsd pid={pid} state=D wchan=fuse_wait threads=3 tstates=D:1,S:2"))
        loki["reaper"].append((int((at + 0.5) * 1e9), f"evidence stage=1 pod={POD} sandbox={sb} pid={pid} incomplete (timeout 5s or read error)"))
    elif incomplete:
        loki["reaper"].append((int(at * 1e9), f"evidence stage=1 pod={POD} sandbox={sb} pid={pid} incomplete (timeout 5s or read error)"))
    elif with_evidence and stage == 1:
        loki["reaper"].append((int(at * 1e9), f"evidence stage=1 pod={POD} uid=u sandbox={sb} exe=/usr/local/libexec/virtiofsd pid={pid} state=D wchan=fuse_wait threads=3 tstates=D:1,S:2"))
    exe = "/usr/local/libexec/virtiofsd" if stage == 1 else "/usr/local/bin/containerd-shim-kata-v2"
    loki["reaper"].append((int((at + 1) * 1e9), f"reap stage={stage} pod={POD} uid=u sandbox={sb} exe={exe} pid={pid} age=153s"))
    loki["relay"].append((int((at - 60) * 1e9), src_line(at - 60, f"sandbox={sb} vmconsole: INFO: task dockerd blocked")))
    return loki


def with_pod(prom, pod, first, last, age0=100):
    """A second Sandbox pod observed in [first, last] with its own (quiet) restart counter."""
    prom["member_age"].append(series({"pod": pod, "namespace": "testpool"}, [[t, str(t - first + age0)] for t in range(int(first), int(last) + 1, STEP)]))
    prom["restarts"].append(series({"pod": pod, "container": "control"}, samples(first, last, 0)))
    return prom


class VerdictTests(unittest.TestCase):
    def run_report(self, src, now=None):
        return soak.run(src, T0, T1, [NODE], now=now or T1 + 60)

    def test_quiet_window_is_ok(self):
        rep = self.run_report(FakeSource(quiet_prom(), loki=quiet_loki()))
        self.assertEqual(rep.verdict, "OK", rep.markdown())
        self.assertIn(f"| {NODE} | 61 | 0 |", rep.markdown())
        self.assertIn("| 26h00m | 0 |", rep.markdown())  # max age reached: 90 000 s + 3 600 s = 26 h

    def test_contained_recurrence(self):
        loki = reap_lines(quiet_loki(), T0 + 1650)
        loki["watchdog"] = [(int((T0 + 1500) * 1e9), "ready-watchdog: ready-port closed: virtiofs hung")]
        prom = capacity_dip(quiet_prom(), T0 + 1560, T0 + 1980)
        rep = self.run_report(FakeSource(prom, loki=loki))
        self.assertEqual(rep.verdict, "RECURRENCE-CONTAINED", rep.markdown())
        self.assertTrue(any("capacity incident" in r and "inside the bound" in r for r in rep.recurrences))
        self.assertTrue(any("signal watchdog closure" in s_ and "capacity incident" in s_ for s_ in rep.sections))
        self.assertTrue(any("signal stage-1 reap of" in s_ for s_ in rep.sections))

    def test_incident_open_at_window_end_is_unresolved(self):
        prom = capacity_dip(quiet_prom(), T0 + 1500, T1 + 1)
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "UNRESOLVED", rep.markdown())
        self.assertTrue(any("still open" in u for u in rep.unresolved))

    def test_capacity_loss_without_any_signal_is_still_seen(self):
        # an outage already underway at the window start, no closure/reap/restart inside the window
        prom = capacity_dip(quiet_prom(), T0, T1 + 1)
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "UNRESOLVED", rep.markdown())
        self.assertTrue(any("already underway at the first sample" in u and "still open" in u and "--from" in u for u in rep.unresolved))
        # and a short loss (below the alert's 30-minute delay) is contained, not invisible
        prom = capacity_dip(quiet_prom(), T0 + 600, T0 + 780)
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "RECURRENCE-CONTAINED", rep.markdown())

    def test_slow_recovery_past_bound_is_unresolved(self):
        prom = capacity_dip(quiet_prom(), T0 + 600, T0 + 600 + soak.RECOVERY_BOUND_SECONDS + 300)
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "UNRESOLVED", rep.markdown())

    def test_transient_failure_is_not_joined_to_a_later_incident(self):
        loki = quiet_loki()
        loki["watchdog"] = [(int((T0 + 600) * 1e9), "ready-watchdog: dockerd check failed (x) consecutive=1")]
        prom = capacity_dip(quiet_prom(), T0 + 2400, T0 + 2640)  # unrelated 4-minute dip 30 min later
        rep = self.run_report(FakeSource(prom, loki=loki))
        self.assertEqual(rep.verdict, "RECURRENCE-CONTAINED", rep.markdown())
        self.assertTrue(any("watchdog closure" in r and "transient" in r for r in rep.recurrences))
        self.assertFalse(rep.unresolved)

    def test_leased_pod_ready_does_not_mask_empty_warm_pool(self):
        prom = with_pod(quiet_prom(), "env-std-pool-leased", T0, T1, 5000)
        prom = capacity_dip(prom, T0 + 300, T1 + 1)  # the pool's own readyReplicas is 0 while the leased pod stays Ready
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "UNRESOLVED", rep.markdown())

    def test_lease_turnover_is_not_a_recurrence(self):
        prom = with_pod(quiet_prom(), "env-std-pool-leased", T0, T0 + 1140)
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "OK", rep.markdown())
        self.assertTrue(any("lease turnover" in s_ for s_ in rep.sections))

    def test_restart_signal_attaches_to_incident_or_is_transient(self):
        prom = quiet_prom()
        vals = samples(T0, T1, 0)
        for s_ in vals[30:]:
            s_[1] = "1"  # one restart at T0+1800
        prom["restarts"] = [series({"pod": POD, "container": "control"}, vals)]
        rep = self.run_report(FakeSource(capacity_dip(dict(prom), T0 + 1800, T1 + 1), loki=quiet_loki()))
        self.assertEqual(rep.verdict, "UNRESOLVED", rep.markdown())
        prom["warm_ready"] = quiet_prom()["warm_ready"]
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "RECURRENCE-CONTAINED", rep.markdown())
        self.assertTrue(any("restart of" in r and "transient" in r for r in rep.recurrences))

    def test_node_notready_is_prevention_failed(self):
        prom = quiet_prom()
        vals = samples(T0, T1, 1)
        for s_ in vals[10:15]:
            s_[1] = "0"
        prom["node_ready"] = [series({"node": NODE, "condition": "Ready", "status": "true"}, vals)]
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "PREVENTION-FAILED", rep.markdown())

    def test_any_testpool_alert_firing_is_prevention_failed(self):
        for name in ("TestpoolEnvTeardownStuck", "TestpoolNoWarmCapacity", "TestpoolOperatorDown", "EnvNodeKubeletMetricsBlocked"):
            prom = quiet_prom()
            prom["alerts"] = [series({"alertname": name, "alertstate": "firing"}, samples(T0 + 60, T0 + 600, 1))]
            rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
            self.assertEqual(rep.verdict, "PREVENTION-FAILED", name)

    def test_pending_alert_alone_is_not_a_failure(self):
        prom = quiet_prom()
        prom["alerts"] = [series({"alertname": "EnvNodeRuntimeStopErrors", "alertstate": "pending"}, samples(T0, T0 + 300, 1))]
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "OK", rep.markdown())

    def test_failure_keeps_incompleteness_visible_and_blocks_checkpoint(self):
        prom = quiet_prom()
        prom["alerts"] = [series({"alertname": "TestpoolEnvTeardownStuck", "alertstate": "firing"}, samples(T0 + 600, T0 + 1200, 1))]
        rep = self.run_report(FakeSource(prom, loki=quiet_loki(), fail={"relay"}))
        self.assertEqual(rep.verdict, "PREVENTION-FAILED")
        self.assertIn("**Incomplete data:**", rep.markdown())
        self.assertFalse(soak.should_advance_checkpoint(rep))
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertTrue(soak.should_advance_checkpoint(rep))
        rep = self.run_report(FakeSource(capacity_dip(quiet_prom(), T0 + 3000, T1 + 1), loki=quiet_loki()))
        self.assertEqual(rep.verdict, "UNRESOLVED")
        self.assertFalse(soak.should_advance_checkpoint(rep))  # an open incident must not fall out of the next window


class CompletenessTests(unittest.TestCase):
    def run_report(self, src, now=None):
        return soak.run(src, T0, T1, [NODE], now=now or T1 + 60)

    def test_failed_endpoint_is_never_ok(self):
        for failing in ("node_ready", "alerts", "reaper", "stop_errors", "warm_ready"):
            rep = self.run_report(FakeSource(quiet_prom(), loki=quiet_loki(), fail={failing}))
            self.assertEqual(rep.verdict, "INCOMPLETE", failing)

    def test_empty_required_series_is_incomplete(self):
        for key in ("kubelet_up", "boot_time", "warm_ready", "warm_spec", "restarts", "member_age"):
            prom = quiet_prom()
            prom[key] = []
            rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
            self.assertEqual(rep.verdict, "INCOMPLETE", key)

    def test_partial_or_gappy_series_is_incomplete(self):
        prom = quiet_prom()
        prom["node_ready"] = [series({"node": NODE}, samples(T0, T0 + 600, 1))]  # first 10 min only
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "INCOMPLETE", rep.markdown())
        prom = quiet_prom()
        vals = [v for v in samples(T0, T1, 1) if not (T0 + 1200 <= v[0] < T0 + 1800)]  # 10-minute hole (still > 80 % coverage)
        prom["warm_ready"] = [series({"name": "env-std-pool"}, vals)]
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "INCOMPLETE", rep.markdown())
        self.assertTrue(any("no samples" in p for p in rep.problems))

    def test_scrape_target_churn_does_not_split_a_series(self):
        # the node-exporter pod is replaced on every reboot and KSM can roll mid-window: the same
        # metric then arrives as two half-window series that differ only in the target's pod label
        prom = quiet_prom()
        half = T0 + 1800
        prom["boot_time"] = [
            series({"instance": "192.168.0.37:9100", "pod": "node-exporter-old"}, samples(T0, half - STEP, 1_799_000_000)),
            series({"instance": "192.168.0.37:9100", "pod": "node-exporter-new"}, samples(half, T1, 1_799_000_000)),
        ]
        prom["warm_ready"] = [
            series({"name": "env-std-pool", "exported_namespace": "testpool", "pod": "ksm-old"}, samples(T0, half - STEP, 1)),
            series({"name": "env-std-pool", "exported_namespace": "testpool", "pod": "ksm-new"}, samples(half, T1, 1)),
        ]
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "OK", rep.markdown())
        # and a dip in the second half is evaluated (not lost with the "first series only")
        prom["warm_ready"][1]["values"] = [[t_, "0" if half + 600 <= t_ < half + 720 else v] for t_, v in prom["warm_ready"][1]["values"]]
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "RECURRENCE-CONTAINED", rep.markdown())

    def test_missing_expected_node_is_incomplete(self):
        rep = self.run_report(FakeSource(quiet_prom(node="talos-env-node-9"), loki=quiet_loki()))
        self.assertEqual(rep.verdict, "INCOMPLETE")

    def test_heartbeat_gap_truncation_and_retention_are_incomplete(self):
        loki = quiet_loki()
        loki["reaper"] = loki["reaper"][:1]
        self.assertEqual(self.run_report(FakeSource(quiet_prom(), loki=loki)).verdict, "INCOMPLETE")
        loki = quiet_loki()
        loki["_truncated"] = ("relay",)
        self.assertEqual(self.run_report(FakeSource(quiet_prom(), loki=loki)).verdict, "INCOMPLETE")
        self.assertEqual(self.run_report(FakeSource(quiet_prom(), loki=quiet_loki()), now=T0 + 200 * 3600).verdict, "INCOMPLETE")

    def test_relay_gap_by_source_time_is_incomplete(self):
        loki = quiet_loki()
        loki["relay"] = [l for l in loki["relay"] if l[0] < int((T0 + 600) * 1e9) or l[0] > int((T0 + 2700) * 1e9)]
        rep = self.run_report(FakeSource(quiet_prom(), loki=loki))
        self.assertEqual(rep.verdict, "INCOMPLETE", rep.markdown())
        self.assertTrue(any("relay capture gap" in p for p in rep.problems))

    def test_replayed_old_records_are_not_fresh_evidence(self):
        # the relay reconnects every 5 min and replays the same day-old record with a new ingestion
        # timestamp: by source time nothing was captured in this window → gap → INCOMPLETE
        loki = quiet_loki()
        old = src_line(T0 - 86400, "yesterday's line")
        loki["relay"] = [(int((T0 + 300 * i) * 1e9), old) for i in range(0, 13)]
        rep = self.run_report(FakeSource(quiet_prom(), loki=loki))
        self.assertEqual(rep.verdict, "INCOMPLETE", rep.markdown())
        self.assertTrue(any("no cri-log-relay records in the window" in p for p in rep.problems))
        self.assertIn("13 raw, 13 replayed from outside the window, 0 untimestamped) | 0 |", rep.markdown())

    def test_relay_lines_without_source_time_are_reported_not_problems(self):
        loki = quiet_loki()
        loki["relay"] = [(t, "no time field here") for t, _ in loki["relay"]]
        rep = self.run_report(FakeSource(quiet_prom(), loki=loki))
        self.assertEqual(rep.verdict, "INCOMPLETE")  # nothing timestamped → no capture at all
        self.assertTrue(any("no cri-log-relay records in the window" in p for p in rep.problems))
        # one garbled line among a healthy stream is counted, not a problem: it must not hold the
        # checkpoint on the same malformed record forever
        loki = quiet_loki()
        loki["relay"].append((int((T0 + 1000) * 1e9), "relay: connection reset, reconnecting"))
        rep = self.run_report(FakeSource(quiet_prom(), loki=loki))
        self.assertEqual(rep.verdict, "OK", rep.markdown())
        self.assertTrue(soak.should_advance_checkpoint(rep))
        self.assertIn("1 of 14 relay lines carry no containerd time= field", rep.markdown())

    def test_reap_without_complete_evidence_is_incomplete(self):
        for kind in ("none", "incomplete"):
            loki = reap_lines(quiet_loki(), T0 + 1650, with_evidence=False, incomplete=(kind == "incomplete"))
            rep = self.run_report(FakeSource(capacity_dip(quiet_prom(), T0 + 1560, T0 + 1980), loki=loki))
            self.assertEqual(rep.verdict, "INCOMPLETE", kind)
            self.assertTrue(any("no complete evidence dump" in p for p in rep.problems), kind)

    def test_evidence_is_matched_per_sandbox_and_pid(self):
        loki = reap_lines(quiet_loki(), T0 + 1650, sb=SB, pid=5)  # complete
        loki = reap_lines(loki, T0 + 2400, sb="b" * 32, pid=9, with_evidence=False)  # a second sandbox without
        prom = capacity_dip(quiet_prom(), T0 + 1560, T0 + 1980)
        vals = prom["warm_ready"][0]["values"]
        for s_ in vals:
            if T0 + 2340 <= s_[0] < T0 + 2700:
                s_[1] = "0"
        rep = self.run_report(FakeSource(prom, loki=loki))
        self.assertEqual(rep.verdict, "INCOMPLETE", rep.markdown())
        self.assertTrue(any("pid 9" in p for p in rep.problems))
        self.assertFalse(any("pid 5" in p for p in rep.problems))

    def test_reaped_sandbox_without_relay_records_is_incomplete(self):
        loki = reap_lines(quiet_loki(), T0 + 1650)
        loki["relay"] = [l for l in loki["relay"] if SB not in l[1]]
        rep = self.run_report(FakeSource(capacity_dip(quiet_prom(), T0 + 1560, T0 + 1980), loki=loki))
        self.assertEqual(rep.verdict, "INCOMPLETE")
        self.assertTrue(any("no in-window relay" in p for p in rep.problems))

    def test_header_then_timeout_dump_is_not_complete(self):
        loki = reap_lines(quiet_loki(), T0 + 1650, header_then_timeout=True)
        rep = self.run_report(FakeSource(capacity_dip(quiet_prom(), T0 + 1560, T0 + 1980), loki=loki))
        self.assertEqual(rep.verdict, "INCOMPLETE", rep.markdown())
        self.assertTrue(any("timed out after its header" in p for p in rep.problems))

    def test_stage2_reap_is_a_signal_without_needing_evidence(self):
        loki = reap_lines(quiet_loki(), T0 + 1800, stage=2, pid=11)
        rep = self.run_report(FakeSource(quiet_prom(), loki=loki))
        self.assertNotEqual(rep.verdict, "OK", rep.markdown())
        self.assertTrue(any("stage-2 reap" in r for r in rep.recurrences))
        self.assertFalse(any("evidence dump" in p for p in rep.problems))
        loki["relay"] = [l for l in loki["relay"] if SB not in l[1]]
        rep = self.run_report(FakeSource(quiet_prom(), loki=loki))
        self.assertTrue(any("no in-window relay" in p for p in rep.problems))

    def test_signal_just_before_window_end_is_pending(self):
        loki = quiet_loki()
        loki["watchdog"] = [(int((T1 - 5) * 1e9), "ready-watchdog: ready-port closed: virtiofs hung")]
        rep = self.run_report(FakeSource(quiet_prom(), loki=loki))
        self.assertEqual(rep.verdict, "INCOMPLETE", rep.markdown())
        self.assertTrue(any("outcome not observable yet" in p for p in rep.problems))
        self.assertFalse(soak.should_advance_checkpoint(rep))

    def test_recovered_incident_does_not_absorb_a_fresh_closure(self):
        # an incident that recovered 3 min before the window end sits within ±5 min of a closure
        # 5 s before the end — but it cannot show that closure's outcome
        loki = quiet_loki()
        loki["watchdog"] = [(int((T1 - 5) * 1e9), "ready-watchdog: ready-port closed: virtiofs hung")]
        prom = capacity_dip(quiet_prom(), T1 - 240, T1 - 180)
        rep = self.run_report(FakeSource(prom, loki=loki))
        self.assertEqual(rep.verdict, "INCOMPLETE", rep.markdown())
        self.assertTrue(any("outcome not observable yet" in p for p in rep.problems))
        self.assertFalse(any("signal watchdog closure" in s_ for s_ in rep.sections))
        self.assertFalse(soak.should_advance_checkpoint(rep))
        # an incident still OPEN at the end is already unresolved and does absorb it
        prom = capacity_dip(quiet_prom(), T1 - 240, T1 + 1)
        rep = self.run_report(FakeSource(prom, loki=loki))
        self.assertEqual(rep.verdict, "UNRESOLVED", rep.markdown())
        self.assertTrue(any("signal watchdog closure" in s_ for s_ in rep.sections))
        self.assertFalse(soak.should_advance_checkpoint(rep))

    def test_untimestamped_records_never_count_as_capture(self):
        loki = quiet_loki()
        old = src_line(T0 - 86400, "yesterday's line")
        loki["relay"] = []
        for i in range(0, 13):
            loki["relay"] += [(int((T0 + 300 * i) * 1e9), old)] * 3 + [(int((T0 + 300 * i) * 1e9), "untimestamped replay")]
        rep = self.run_report(FakeSource(quiet_prom(), loki=loki))
        self.assertEqual(rep.verdict, "INCOMPLETE", rep.markdown())
        self.assertTrue(any("no cri-log-relay records in the window" in p for p in rep.problems))
        self.assertIn("carry no containerd time= field", rep.markdown())
        self.assertFalse(any("time= field" in p for p in rep.problems))

    def test_pre_window_replay_does_not_create_a_gap(self):
        loki = quiet_loki()
        loki["relay"].append((int((T0 + 10) * 1e9), src_line(T0 - 3600, "an hour before the window")))
        rep = self.run_report(FakeSource(quiet_prom(), loki=loki))
        self.assertEqual(rep.verdict, "OK", rep.markdown())

    def test_incident_underway_at_start_is_not_certified_contained(self):
        prom = capacity_dip(quiet_prom(), T0, T0 + 120)  # deficient at the first sample, back 2 min later
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "INCOMPLETE", rep.markdown())
        self.assertTrue(any("underway at the first sample" in p for p in rep.problems))
        self.assertFalse(rep.recurrences)

    def test_missing_spec_is_incomplete_not_an_outage(self):
        prom = capacity_dip(quiet_prom(), T0, T1 + 1)
        prom["warm_spec"] = []
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "INCOMPLETE", rep.markdown())
        self.assertFalse(rep.unresolved)
        prom = quiet_prom()
        prom["warm_spec"][0]["values"] = prom["warm_spec"][0]["values"][:20]  # spec unobserved for the rest
        prom = capacity_dip(prom, T0 + 1800, T0 + 1860)
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "INCOMPLETE")
        self.assertFalse(rep.recurrences)  # the dip fell where the target was unobserved: not evaluated
        self.assertTrue(any("no warm_spec observation within" in p for p in rep.problems))

    def test_dip_inside_a_short_spec_gap_is_still_an_incident(self):
        # two missing warm_spec scrapes (inside the sample_gaps tolerance) around a 2-sample dip:
        # the last observed target is carried forward, the deficit is evaluated, not dropped
        prom = capacity_dip(quiet_prom(), T0 + 1800, T0 + 1920)
        prom["warm_spec"][0]["values"] = [s_ for s_ in prom["warm_spec"][0]["values"] if not (T0 + 1800 <= s_[0] <= T0 + 1860)]
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "RECURRENCE-CONTAINED", rep.markdown())
        self.assertTrue(any("capacity incident" in r for r in rep.recurrences))
        self.assertFalse(any("no warm_spec observation" in p for p in rep.problems))

    def test_closed_slow_incident_keeps_verdict_but_frees_checkpoint(self):
        prom = capacity_dip(quiet_prom(), T0 + 600, T0 + 600 + 15 * 60)
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "UNRESOLVED")
        self.assertTrue(soak.should_advance_checkpoint(rep))
        rep = self.run_report(FakeSource(capacity_dip(quiet_prom(), T0 + 3000, T1 + 1), loki=quiet_loki()))
        self.assertFalse(soak.should_advance_checkpoint(rep))

    def test_truncated_restart_or_member_history_is_incomplete(self):
        prom = quiet_prom()
        prom["restarts"][0]["values"] = prom["restarts"][0]["values"][:1]  # member observed all hour, counter once
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "INCOMPLETE", rep.markdown())
        prom = quiet_prom()
        prom["member_age"][0]["values"] = prom["member_age"][0]["values"][:20]
        prom["restarts"][0]["values"] = prom["restarts"][0]["values"][:20]  # both histories end at minute 20, no successor
        rep = self.run_report(FakeSource(prom, loki=quiet_loki()))
        self.assertEqual(rep.verdict, "INCOMPLETE", rep.markdown())
        self.assertTrue(any("Sandbox pods (union)" in p for p in rep.problems))


class FakeLoki:
    """A corpus served the way Loki serves query_range: `end` EXCLUSIVE, newest `limit` records."""

    def __init__(self, corpus):
        self.corpus = sorted(corpus, key=lambda r: -r[0])
        self.calls = []

    def __call__(self, url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        start, end, limit = int(q["start"][0]), int(q["end"][0]), int(q["limit"][0])
        self.calls.append((start, end, limit))
        rows = [r for r in self.corpus if start <= r[0] < end][:limit]
        return {"status": "success", "data": {"result": [{"stream": {}, "values": [[str(t), l] for t, l in rows]}]}}


class PaginationTests(unittest.TestCase):
    def make(self, corpus):
        src = soak.Source("http://p", "http://l")
        fake = FakeLoki(corpus)
        src._get = fake
        return src, fake

    def test_boundary_timestamp_shared_by_two_streams_is_not_skipped(self):
        soak.LOKI_PAGE, saved = 3, soak.LOKI_PAGE
        try:
            src, fake = self.make([(300, "c"), (200, "b"), (100, "a1"), (100, "a2"), (50, "z")])
            lines, truncated = src.loki_range("{x}", 0, 400)
            self.assertFalse(truncated)
            self.assertEqual([l for _, l in lines], ["z", "a1", "a2", "b", "c"])
            self.assertEqual(fake.calls[1][1], 101)  # exclusive end re-includes ts=100
        finally:
            soak.LOKI_PAGE = saved

    def test_timestamp_that_alone_fills_a_page_is_reported_truncated(self):
        soak.LOKI_PAGE, saved = 2, soak.LOKI_PAGE
        try:
            src, _ = self.make([(100, "a"), (100, "b"), (100, "c"), (100, "d"), (10, "z")])
            lines, truncated = src.loki_range("{x}", 0, 400)
            self.assertTrue(truncated)
        finally:
            soak.LOKI_PAGE = saved

    def test_exact_page_boundary_terminates(self):
        soak.LOKI_PAGE, saved = 2, soak.LOKI_PAGE
        try:
            src, _ = self.make([(400, "d"), (300, "c"), (200, "b"), (100, "a")])
            lines, truncated = src.loki_range("{x}", 0, 500)
            self.assertFalse(truncated)
            self.assertEqual([l for _, l in lines], ["a", "b", "c", "d"])
        finally:
            soak.LOKI_PAGE = saved


class HelperTests(unittest.TestCase):
    def test_intervals_and_gaps(self):
        vals = [(T0, 1.0), (T0 + 60, 0.0), (T0 + 120, 0.0), (T0 + 180, 1.0), (T0 + 600, 1.0)]
        self.assertEqual(soak.intervals_where(vals, lambda v: v < 1), [(T0 + 60, T0 + 180)])
        self.assertEqual(soak.sample_gaps(vals), [(T0 + 180, T0 + 600)])

    def test_parse_ts_and_source_ts(self):
        self.assertEqual(soak.parse_ts("2026-09-21T00:00:00Z"), soak.parse_ts("2026-09-21T03:00:00+03:00"))
        self.assertEqual(soak.source_ts('{"level":"debug","time":"2026-09-21T00:00:00.123456789Z"}'), soak.parse_ts("2026-09-21T00:00:00.123456Z"))
        self.assertEqual(soak.source_ts('time="2026-09-21T00:00:00Z" level=info msg=x'), soak.parse_ts("2026-09-21T00:00:00Z"))
        self.assertIsNone(soak.source_ts("no timestamp"))


if __name__ == "__main__":
    unittest.main()
