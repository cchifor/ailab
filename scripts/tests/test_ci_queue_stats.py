#!/usr/bin/env python3
"""Unit tests for scripts/ci-queue-stats.py (pure parts only; no network)."""
import importlib.util
import pathlib
import unittest

_MOD_PATH = pathlib.Path(__file__).resolve().parents[1] / "ci-queue-stats.py"
_spec = importlib.util.spec_from_file_location("ci_queue_stats", _MOD_PATH)
cqs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cqs)  # must NOT perform any I/O at import time


class ParseTs(unittest.TestCase):
    def test_rfc3339_z_and_offset(self):
        self.assertAlmostEqual(cqs.parse_ts("2026-09-23T10:00:00Z"), cqs.parse_ts("2026-09-23T12:00:00+02:00"))

    def test_unset_sentinels_are_none(self):
        for v in ("0001-01-01T00:00:00Z", "1970-01-01T00:00:00Z", "", None, "garbage"):
            self.assertIsNone(cqs.parse_ts(v), v)


class Percentile(unittest.TestCase):
    def test_empty_is_none(self):
        self.assertIsNone(cqs.percentile([], 50))

    def test_nearest_rank(self):
        xs = [10, 20, 30, 40, 50]
        self.assertEqual(cqs.percentile(xs, 50), 30)
        self.assertEqual(cqs.percentile(xs, 90), 50)
        self.assertEqual(cqs.percentile(xs, 0), 10)


class Summarize(unittest.TestCase):
    def job(self, created, started, completed, runner="ci-runner-1"):
        return {"created": created, "started": started, "completed": completed, "runner": runner}

    def test_wait_and_run_split(self):
        jobs = [self.job(0, 100, 130), self.job(0, 600, 620, "cloud-ci-1"), self.job(0, None, None)]
        s = cqs.summarize(jobs, days=1)
        self.assertEqual(s["jobs"], 3)
        self.assertEqual(s["wait_s"]["n"], 2)  # the never-started job counts as a job, not a wait
        self.assertEqual(s["wait_s"]["over_5min"], 1)
        self.assertEqual(s["run_s"]["p50"], 20)
        self.assertEqual(s["by_runner"], {"ci-runner-1": 1, "cloud-ci-1": 1, "(none)": 1})

    def test_zero_days_does_not_divide_by_zero(self):
        self.assertGreater(cqs.summarize([self.job(0, 1, 2)], days=0)["jobs_per_day"], 0)


if __name__ == "__main__":
    unittest.main()
