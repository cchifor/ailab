#!/usr/bin/env python3
"""Unit tests for scripts/ci-queue-stats.py (pure parts only; no network)."""
import importlib.util
import pathlib
import unittest
from unittest.mock import patch
import io
import json

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
        self.assertIn("0 jobs", out)
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

class Readiness(unittest.TestCase):
    def test_cli_recognizes_rerun_attempt_without_dispatch_override(self):
        run = {'id': 1, 'run_attempt': 2, 'path': 'ci.yml@main'}
        jobs = [{'id': 2, 'created_at': '2026-09-01T00:00:00Z', 'started_at': '2026-10-04T10:11:00Z', 'completed_at': '2026-10-04T10:12:00Z', 'status': 'completed', 'conclusion': 'success'}]
        output = io.StringIO()
        with patch.dict(cqs.os.environ, {'GITEA_TOKEN': 'test-only'}), patch.object(cqs, 'runs_since', return_value=[run]), patch.object(cqs, 'paged', return_value=jobs), patch('sys.stdout', output):
            self.assertEqual(cqs.main(['--json', '--repos', 'o/r']), 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary['wait_s']['n'], 0)
        self.assertEqual(summary['runner_wait_s']['n'], 0)
        self.assertEqual(summary['run_s']['p50'], 60)

    def test_scan_bound_returns_partial_rows_with_explicit_error(self):
        with patch.object(cqs, 'get', return_value={'workflow_runs': [{'id': 1, 'started_at': '2026-10-04T10:00:00Z'}]}):
            with self.assertRaises(cqs.RunScanLimit) as raised:
                cqs.runs_since('t', 'o/r', 0, max_pages=1)
        self.assertEqual(len(raised.exception.runs), 1)

    def test_dependencies_are_not_runner_wait(self):
        t = '2026-10-04T10:'
        run = {'id': 1}
        job = {'id': 2, 'created_at': t+'00:00Z', 'started_at': t+'11:00Z', 'completed_at': t+'12:00Z', 'conclusion': 'success'}
        row = cqs.job_row('o/r', run, job, [{'completed_at': t+'10:00Z'}])
        s = cqs.summarize([row], 1)
        self.assertEqual(s['wait_s']['p50'], 660)
        self.assertEqual(s['runner_wait_s']['p50'], 60)
        self.assertEqual(s['dependency_wait_s']['p50'], 600)

    def test_unknown_readiness_does_not_become_zero(self):
        row = cqs.job_row('o/r', {'id': 1}, {'id': 2, 'created_at': '2026-10-04T10:00:00Z', 'started_at': '2026-10-04T10:11:00Z'})
        self.assertIsNone(cqs.summarize([row], 1)['runner_wait_s']['p50'])

    def test_rerun_dispatch_replaces_original_creation(self):
        row = cqs.job_row('o/r', {'id': 1}, {'id': 2, 'created_at': '2026-09-01T00:00:00Z', 'started_at': '2026-10-04T10:11:00Z'}, [], '2026-10-04T10:10:00Z')
        self.assertEqual(cqs.summarize([row], 1)['runner_wait_s']['p50'], 60)

    def test_cancelled_cost_is_counted_and_clock_skew_not_negative(self):
        s = cqs.summarize([{'created': 1, 'started': 0, 'completed': 60, 'runner': '', 'conclusion': 'cancelled'}], 1)
        self.assertEqual(s['runner_minutes'], 1)
        self.assertEqual(s['outcomes']['cancelled'], 1)
        self.assertEqual(s['wait_s']['n'], 0)

    def test_known_rerun_without_dispatch_has_unknown_queue(self):
        row = cqs.job_row('o/r', {'id': 1}, {'id': 2, 'created_at': '2026-09-01T00:00:00Z', 'started_at': '2026-10-04T10:11:00Z'}, [], known_rerun=True)
        self.assertIsNone(row['created'])
        self.assertEqual(cqs.summarize([row], 1)['wait_s']['n'], 0)

    def test_old_run_does_not_hide_later_page_recent_rerun(self):
        pages = {1: {'workflow_runs': [{'id': 30, 'started_at': '2026-09-01T00:00:00Z'}]}, 2: {'workflow_runs': [{'id': 20, 'started_at': '2026-10-04T00:00:00Z'}]}, 3: {'workflow_runs': []}}
        orig = cqs.get
        cqs.get = lambda token, path, params: pages[params['page']]
        try:
            self.assertEqual([r['id'] for r in cqs.runs_since('t', 'o/r', cqs.parse_ts('2026-10-01T00:00:00Z'))], [20])
        finally:
            cqs.get = orig

if __name__ == "__main__":
    unittest.main()
