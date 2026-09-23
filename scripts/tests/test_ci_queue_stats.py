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
        jobs = [self.job(0, 100, 130), self.job(0, 600, 620, "cloud-ci-1"), self.job(0, None, None, "")]
        s = cqs.summarize(jobs, days=1)
        self.assertEqual(s["jobs"], 3)
        self.assertEqual(s["wait_s"]["n"], 2)  # the never-started job counts as a job, not a wait
        self.assertEqual(s["wait_s"]["over_5min"], 1)
        self.assertEqual(s["run_s"]["p50"], 20)
        self.assertEqual(s["by_runner"], {"ci-runner-1": 1, "cloud-ci-1": 1, "(none)": 1})

    def test_zero_days_does_not_divide_by_zero(self):
        self.assertGreater(cqs.summarize([self.job(0, 1, 2)], days=0)["jobs_per_day"], 0)



class NearestRank(unittest.TestCase):
    def test_distinguishes_from_rounded_index(self):
        # the reviewbots' case: nearest-rank p90 of six samples is the 6th value, not the 5th
        self.assertEqual(cqs.percentile([10, 20, 30, 40, 50, 60], 90), 60)
        self.assertEqual(cqs.percentile([10, 20, 30, 40, 50, 60], 50), 30)


class Render(unittest.TestCase):
    def test_no_samples_does_not_crash(self):
        s = cqs.summarize([], days=7)
        s["days"] = 7
        out = cqs.render(s)
        self.assertIn("0 completed jobs", out)
        self.assertIn("no samples", out)

    def test_unset_timestamps_render_not_typeerror(self):
        s = cqs.summarize([{"created": None, "started": None, "completed": None, "runner": ""}], days=1)
        s["days"] = 1
        self.assertIn("no samples", cqs.render(s))

    def test_skipped_repos_listed(self):
        s = cqs.summarize([{"created": 0, "started": 5, "completed": 9, "runner": "r"}], days=1)
        s["days"] = 1
        s["skipped_repos"] = ["cchifor/x: HTTPError 404"]
        self.assertIn("skipped repos: cchifor/x: HTTPError 404", cqs.render(s))


class Paged(unittest.TestCase):
    def test_walks_pages_until_short(self):
        # page 3 is EMPTY: the walk stops there, not on the short page 2 (a clamped MAX_RESPONSE_ITEMS
        # would make every full page look short)
        pages = {1: {"jobs": [{"i": n} for n in range(50)]}, 2: {"jobs": [{"i": 50}]}, 3: {"jobs": []}}
        calls = []

        def fake_get(token, path, params=None):
            calls.append(params["page"])
            return pages[params["page"]]

        orig = cqs.get
        cqs.get = fake_get
        try:
            out = cqs.paged("t", "/x", "jobs")
        finally:
            cqs.get = orig
        self.assertEqual(len(out), 51)
        self.assertEqual(calls, [1, 2, 3])

    def test_clamped_page_size_is_still_walked(self):
        pages = {1: {"jobs": [{"i": n} for n in range(30)]}, 2: {"jobs": [{"i": n} for n in range(30, 45)]}, 3: {"jobs": []}}
        orig = cqs.get
        cqs.get = lambda token, path, params=None: pages[params["page"]]
        try:
            self.assertEqual(len(cqs.paged("t", "/x", "jobs")), 45)
        finally:
            cqs.get = orig

if __name__ == "__main__":
    unittest.main()
