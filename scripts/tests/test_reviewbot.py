"""Unit tests for ansible/roles/pr_reviewer/files/reviewbot.py.

WHY THESE EXIST: reviewbot's failure handling is a state machine (retry vs quarantine,
per-class attempt budgets, a shared wall-clock budget across two subprocesses, enqueue
dedupe/coalescing) and `py_compile` proves none of it. The 2026-09-04 incident was caused by
a *policy* value being wrong, not by a syntax error: the claude persona's 300 s deadline was
shorter than its real run-time distribution, every timeout burned the whole budget of a
single-threaded worker, and `enqueue()` would have re-queued a quarantined head forever
because 'quarantined' was missing from one SQL tuple. Every test below pins one of those.

Stdlib `unittest` only, no pytest: .gitea/workflows/broker-inventory.yaml runs
`python -m unittest discover -s scripts/tests` and the runner has no pytest.

reviewbot.py reads its config at IMPORT time (`CFG = json.load(open(sys.argv[1] ...))`), so
each test loads a fresh module object against a throwaway config + sqlite file.
"""
import importlib.util
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time as real_time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "ansible" / "roles" / "pr_reviewer" / "files" / "reviewbot.py"

BASE_CFG = {
    "persona": "test",
    "listen": "127.0.0.1",
    "port": 18477,
    "gitea_url": "https://git.invalid",
    "repos": ["o/r"],
    "ignore_authors": [],
    "llm_cmd": ["/bin/true"],
    "llm_kind": "claude",
    "llm_model": "m",
    "llm_fallback_model": "fb",
    "llm_sudo_user": "",
    "automerge": False,
    "merge_personas": ["test"],
    "merge_authors": [],
    "pin_authors": [],
    "llm_timeout_s": 900,
    "llm_fallback_min_s": 60,
    "llm_effort": "medium",
    "max_diff_bytes": 400000,
    "max_raw_bytes": 10485760,
    "exclude_globs": [],
    "doc_globs": [],
    "max_comments": 15,
    "max_attempts": 5,
    "max_timeout_attempts": 2,
    "reconcile_s": 300,
}


def load(tmp, **overrides):
    """Import reviewbot.py as a fresh module bound to a throwaway config."""
    d = pathlib.Path(tmp)
    (d / "pat").write_text("token\n", encoding="utf-8")
    (d / "hook").write_text("secret\n", encoding="utf-8")
    cfg = dict(BASE_CFG, **overrides)
    cfg.update({
        "pat_file": str(d / "pat"),
        "webhook_secret_file": str(d / "hook"),
        "posting_disable_flag": str(d / "posting-disabled"),
        "inhibit_flag": str(d / "inhibit"),
        "state_db": str(d / "state.sqlite"),
        "textfile": str(d / "reviewbot.prom"),
    })
    cfg_path = d / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    saved = sys.argv
    sys.argv = ["reviewbot.py", str(cfg_path)]
    try:
        spec = importlib.util.spec_from_file_location("reviewbot_under_test", SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.argv = saved
    return mod


class ErrorTextTest(unittest.TestCase):
    """llm_error_text runs on the failure path; it must never raise, and must prefer the
    channel that actually carries the reason. ailab#482 recorded a harmless startup warning
    from stderr as the cause of a failed review because stdout was discarded."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)

    def test_prefers_stdout_envelope_over_stderr_warning(self):
        out = json.dumps({"subtype": "error_max_turns", "is_error": True})
        txt = self.m.llm_error_text(1, out, 'Permission deny rule "LS" matches no known tool')
        self.assertIn("error_max_turns", txt)
        self.assertIn("llm exit 1", txt)

    def test_malformed_stdout_does_not_raise(self):
        for bad in ('{"truncated": ', "", "not json at all", "[1,2,3]", "null", '{"result": null}'):
            with self.subTest(bad=bad):
                txt = self.m.llm_error_text(2, bad, "stderr tail")
                self.assertIsInstance(txt, str)
                self.assertIn("llm exit 2", txt)

    def test_non_string_fields_are_tolerated(self):
        txt = self.m.llm_error_text(1, json.dumps({"result": {"nested": "object"}}), "")
        self.assertIsInstance(txt, str)

    def test_exit_code_is_always_preserved(self):
        self.assertIn("llm exit 137", self.m.llm_error_text(137, "", ""))


class FailurePolicyTest(unittest.TestCase):
    """The retry/quarantine budgets. Deadline failures are capped lower than fast ones and
    counted SEPARATELY -- mixing them mis-quarantines a job that hit one transient error."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)
        self.timeout = self.m.subprocess.TimeoutExpired(cmd=["claude"], timeout=900)
        self.fast = RuntimeError("llm exit 1: transient")

    def test_two_timeouts_quarantine(self):
        state, attempts, timeouts, _ = self.m.next_failure_state(self.timeout, 0, 0)
        self.assertEqual(("retry", 1, 1), (state, attempts, timeouts))
        state, attempts, timeouts, note = self.m.next_failure_state(self.timeout, attempts, timeouts)
        self.assertEqual("quarantined", state)
        self.assertIn("2 timed-out attempts", note)

    def test_fast_error_then_timeout_does_not_quarantine(self):
        """The mis-quarantine this design exists to avoid: one fast transient failure must not
        consume the expensive-failure allowance."""
        state, attempts, timeouts, _ = self.m.next_failure_state(self.fast, 0, 0)
        self.assertEqual(("retry", 1, 0), (state, attempts, timeouts))
        state, attempts, timeouts, _ = self.m.next_failure_state(self.timeout, attempts, timeouts)
        self.assertEqual("retry", state, "one fast error + one timeout must still get a retry")
        self.assertEqual(1, timeouts)

    def test_fast_errors_still_use_the_total_ceiling(self):
        attempts = timeouts = 0
        states = []
        for _ in range(5):
            state, attempts, timeouts, _ = self.m.next_failure_state(self.fast, attempts, timeouts)
            states.append(state)
        self.assertEqual(["retry"] * 4 + ["quarantined"], states)
        self.assertEqual(0, timeouts)

    def test_timeout_note_is_readable(self):
        """A TimeoutExpired stringifies to the whole argv list; the journal was full of them."""
        self.assertEqual("llm deadline exceeded after 900s", self.m.fail_note(self.timeout))


class SharedBudgetTest(unittest.TestCase):
    """llm_timeout_s is the budget for the WHOLE run_llm operation. It used to be applied per
    subprocess, so a near-timeout primary plus a full fallback could spend 2x -- 30 minutes of
    a single-threaded worker at the new 900 s value, and 150 min across the 5 attempts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)
        self.calls = []

        class Clock:
            now = 1000.0
            def monotonic(self):
                return Clock.now
            def time(self):
                return real_time.time()
            def sleep(self, _n):
                pass
            def strftime(self, *a):
                return real_time.strftime(*a)

        self.clock = Clock()
        self.m.time = self.clock

    def _fake_run(self, elapsed, rc, stdout):
        """Replace subprocess.run, recording the timeout each call was given AND enforcing
        it. A fake that ignores its own timeout would let the budget assertions pass
        vacuously -- the whole point is that the deadline is real."""
        def run(args, **kw):
            timeout = kw.get("timeout")
            self.calls.append({"args": args, "timeout": timeout})
            type(self.clock).now += elapsed
            if timeout is not None and elapsed > timeout:
                raise self.m.subprocess.TimeoutExpired(cmd=args, timeout=timeout)
            return self.m.subprocess.CompletedProcess(args, rc, stdout, "")
        return run

    def _ok_stdout(self):
        return json.dumps({"result": json.dumps({"summary": "s", "findings": []}),
                           "usage": {"output_tokens": 1234}})

    def test_primary_gets_the_full_budget(self):
        self.m.subprocess.run = self._fake_run(10, 0, self._ok_stdout())
        self.m.run_llm("t", "d", "diff")
        self.assertAlmostEqual(900, self.calls[0]["timeout"], delta=1)

    def test_fallback_only_gets_what_is_left(self):
        self.m.subprocess.run = self._fake_run(300, 1, "{}")
        with self.assertRaises(RuntimeError):
            self.m.run_llm("t", "d", "diff")
        self.assertEqual(2, len(self.calls), "fallback should have run")
        self.assertAlmostEqual(600, self.calls[1]["timeout"], delta=2,
                               msg="fallback must inherit the REMAINING budget, not a fresh one")

    def test_a_rescued_primary_failure_is_still_counted(self):
        """THE 4-DAY BLIND SPOT: a primary failure the fallback RESCUES raises nothing and
        increments no failure counter, because the review succeeded. reviewer-1 served 463
        reviews that way after its `claude-fable-5` quota went on 2026-09-06 - correct output
        the whole time, and no series anywhere said the pinned model had stopped being used."""
        self.m.subprocess.run = self._fake_run(10, 0, self._ok_stdout())
        self.m.run_llm("t", "d", "diff")
        self.assertEqual({}, {k: v for k, v in _meta(self.m).items()
                              if k in ("llm_primary_failed_total", "llm_fallback_used_total")},
                         "a healthy primary must not touch either counter")

        # Primary fails fast, fallback succeeds -> the review is fine, the counters still fire.
        calls = []

        def run(args, **kw):
            calls.append(args)
            type(self.clock).now += 10
            if len(calls) == 1:
                return self.m.subprocess.CompletedProcess(args, 1, "quota gone", "")
            return self.m.subprocess.CompletedProcess(args, 0, self._ok_stdout(), "")
        self.m.subprocess.run = run
        self.m.run_llm("t", "d", "diff")
        meta = _meta(self.m)
        self.assertEqual("1.0", meta["llm_primary_failed_total"])
        self.assertEqual("1.0", meta["llm_fallback_used_total"])
        self.assertNotIn("llm_failures_total", meta,
                         "the review SUCCEEDED - it must not be billed as a failed review")

    def test_a_primary_failure_counts_even_when_the_fallback_is_skipped(self):
        """Otherwise the counter measures the net rather than the thing it is catching."""
        self.m.subprocess.run = self._fake_run(880, 1, "{}")
        with self.assertRaises(self.m.ExpensiveFailure):
            self.m.run_llm("t", "d", "diff")
        meta = _meta(self.m)
        self.assertEqual("1.0", meta["llm_primary_failed_total"])
        self.assertNotIn("llm_fallback_used_total", meta,
                         "the fallback never ran; it must not be counted as used")

    def test_fallback_skipped_when_budget_is_nearly_spent(self):
        self.m.subprocess.run = self._fake_run(880, 1, "{}")
        with self.assertRaises(self.m.ExpensiveFailure):
            self.m.run_llm("t", "d", "diff")
        self.assertEqual(1, len(self.calls),
                         "only 20s left (< llm_fallback_min_s=60): fallback must be skipped")

    def test_a_costly_nonzero_exit_is_classified_as_a_budget_failure(self):
        """THE REGRESSION THIS EXISTS FOR: a primary that burns 880 of its 900s and then exits
        nonzero is not a cheap error. Classified as one it would get max_attempts (5) further
        tries -- 75 minutes of the single-threaded worker for one PR."""
        self.m.subprocess.run = self._fake_run(880, 1, "{}")
        with self.assertRaises(self.m.ExpensiveFailure) as ctx:
            self.m.run_llm("t", "d", "diff")
        self.assertTrue(self.m.is_budget_failure(ctx.exception))

    def test_a_long_run_that_fails_on_MALFORMED_OUTPUT_is_also_expensive(self):
        """The round-2 blocker: cost was classified only at the nonzero-exit site, so a run
        that burned the budget and then failed on unparseable output was billed cheap and got
        five more full-length retries (~75 min for one PR)."""
        self.m.subprocess.run = self._fake_run(880, 0, json.dumps({"result": "no json here"}))
        with self.assertRaises(self.m.ExpensiveFailure):
            self.m.run_llm("t", "d", "diff")

    def test_a_long_run_that_fails_on_MISSING_FIELDS_is_also_expensive(self):
        body = json.dumps({"result": json.dumps({"nope": 1})})
        self.m.subprocess.run = self._fake_run(880, 0, body)
        with self.assertRaises(self.m.ExpensiveFailure):
            self.m.run_llm("t", "d", "diff")

    def test_a_quick_malformed_output_stays_cheap(self):
        self.m.subprocess.run = self._fake_run(5, 0, json.dumps({"result": "no json here"}))
        with self.assertRaises(RuntimeError) as ctx:
            self.m.run_llm("t", "d", "diff")
        self.assertNotIsInstance(ctx.exception, self.m.ExpensiveFailure)

    def test_a_cheap_nonzero_exit_stays_a_fast_failure(self):
        self.m.subprocess.run = self._fake_run(5, 1, "{}")
        with self.assertRaises(RuntimeError) as ctx:
            self.m.run_llm("t", "d", "diff")
        self.assertNotIsInstance(ctx.exception, self.m.ExpensiveFailure)
        self.assertFalse(self.m.is_budget_failure(ctx.exception))

    def test_an_already_expired_budget_does_not_launch_a_run(self):
        """`max(1.0, remaining)` used to start a fresh subprocess after the budget was gone."""
        self.m.subprocess.run = self._fake_run(0, 0, self._ok_stdout())
        type(self.clock).now += 10000  # setup itself blew the deadline
        original = self.m.time.monotonic

        class Expired:
            now = type(self.clock).now
            def monotonic(self_inner):
                type(self.clock).now += 10000
                return type(self.clock).now
            def time(self_inner):
                return real_time.time()
            def sleep(self_inner, _n):
                pass
            def strftime(self_inner, *a):
                return real_time.strftime(*a)

        self.m.time = Expired()
        with self.assertRaises(self.m.subprocess.TimeoutExpired):
            self.m.run_llm("t", "d", "diff")
        self.assertEqual([], self.calls)
        self.m.time = self.clock
        self.assertTrue(callable(original))

    def test_a_non_mapping_usage_field_cannot_raise(self):
        """A truthy non-dict `usage` used to raise AttributeError out of the success path."""
        bad = json.dumps({"result": json.dumps({"summary": "s", "findings": []}),
                          "usage": "not-a-mapping"})
        self.m.subprocess.run = self._fake_run(10, 0, bad)
        out = self.m.run_llm("t", "d", "diff")
        self.assertEqual([], out["findings"])

    def test_telemetry_failure_cannot_mask_the_real_exception(self):
        """record_gauge runs in `finally`; if it raised, it would replace the in-flight
        TimeoutExpired with a database error and the worker would then bill the attempt to the
        wrong budget. Breaks the DATABASE rather than stubbing record_gauge out -- the
        swallowing is inside record_gauge, so replacing it would remove the thing under test."""
        self.m.CFG["state_db"] = "/nonexistent-dir/state.sqlite"
        self.m.subprocess.run = self._fake_run(1000, 0, self._ok_stdout())
        with self.assertRaises(self.m.subprocess.TimeoutExpired):
            self.m.run_llm("t", "d", "diff")

    def test_output_token_and_duration_telemetry_recorded(self):
        self.m.subprocess.run = self._fake_run(42, 0, self._ok_stdout())
        self.m.run_llm("t", "d", "diff")
        c = sqlite3.connect(self.m.CFG["state_db"])
        meta = dict(c.execute("SELECT k,v FROM meta"))
        c.close()
        self.assertEqual(1234.0, float(meta["llm_output_tokens"]))
        self.assertEqual(42.0, float(meta["llm_seconds"]))
        self.assertEqual(42.0, float(meta["llm_seconds_max"]))


class QueueStateTest(unittest.TestCase):
    """enqueue() dedupe/coalescing and the quarantine lifecycle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)
        self.A = "a" * 40
        self.B = "b" * 40

    def _rows(self):
        c = sqlite3.connect(self.m.CFG["state_db"])
        rows = list(c.execute("SELECT head_sha,state FROM jobs ORDER BY id"))
        c.close()
        return rows

    def _set_state(self, sha, state):
        c = sqlite3.connect(self.m.CFG["state_db"])
        c.execute("UPDATE jobs SET state=?, updated=? WHERE head_sha=?",
                  (state, real_time.time(), sha))
        c.commit()
        c.close()

    def test_quarantined_head_is_not_re_enqueued(self):
        """The infinite loop: 'quarantined' was missing from the dedupe tuple, so the
        reconciler re-queued a hopeless head every 300 s forever."""
        self.m.enqueue("o/r", 1, self.A, "webhook")
        self._set_state(self.A, "quarantined")
        self.m.enqueue("o/r", 1, self.A, "reconcile")
        self.assertEqual([(self.A, "quarantined")], self._rows())

    def test_new_head_retires_a_quarantined_row(self):
        """Without this the give-up is permanent: a push would not clear the row, so the
        gauge and its alert would stay up forever."""
        self.m.enqueue("o/r", 1, self.A, "webhook")
        self._set_state(self.A, "quarantined")
        self.m.enqueue("o/r", 1, self.B, "head-moved")
        self.assertEqual([(self.A, "superseded"), (self.B, "queued")], self._rows())

    def test_requeue_restores_a_quarantined_job(self):
        self.m.enqueue("o/r", 1, self.A, "webhook")
        self._set_state(self.A, "quarantined")
        self.assertEqual(0, self.m.requeue("o/r", 1))
        self.assertEqual([(self.A, "queued")], self._rows())

    def test_requeue_reports_when_there_is_nothing_to_do(self):
        self.assertEqual(1, self.m.requeue("o/r", 99))

    def test_requeue_clears_both_attempt_counters(self):
        self.m.enqueue("o/r", 1, self.A, "webhook")
        c = sqlite3.connect(self.m.CFG["state_db"])
        c.execute("UPDATE jobs SET state='quarantined', attempts=5, timeout_attempts=2")
        c.commit()
        c.close()
        self.m.requeue("o/r", 1)
        c = sqlite3.connect(self.m.CFG["state_db"])
        self.assertEqual((0, 0), c.execute(
            "SELECT attempts,timeout_attempts FROM jobs").fetchone())
        c.close()

    def test_still_coalesces_queued_heads(self):
        self.m.enqueue("o/r", 1, self.A, "webhook")
        self.m.enqueue("o/r", 1, self.B, "head-moved")
        self.assertEqual([(self.A, "superseded"), (self.B, "queued")], self._rows())


class WorkerIterationTest(unittest.TestCase):
    """worker_once() round-trips through the database, which is where the interesting
    behaviour actually lives: which failures burn the deadline budget, that the counters
    survive into a later attempt, and that a completed head clears a stale quarantine."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)
        self.A = "a" * 40
        self.B = "b" * 40

    def _job(self):
        c = self.m.db()
        row = c.execute("SELECT state,attempts,timeout_attempts,note FROM jobs "
                        "ORDER BY id DESC LIMIT 1").fetchone()
        c.close()
        return row

    def test_idle_worker_returns_none(self):
        self.assertIsNone(self.m.worker_once())

    def test_a_deadline_failure_persists_the_timeout_counter(self):
        self.m.enqueue("o/r", 1, self.A, "webhook")
        self.m.review_job = lambda *a: (_ for _ in ()).throw(
            self.m.subprocess.TimeoutExpired(cmd=["claude"], timeout=900))
        self.assertIsNotNone(self.m.worker_once())
        state, attempts, timeouts, _ = self._job()
        self.assertEqual(("retry", 1, 1), (state, attempts, timeouts))

    def test_two_deadline_failures_quarantine_through_the_database(self):
        self.m.enqueue("o/r", 1, self.A, "webhook")
        self.m.review_job = lambda *a: (_ for _ in ()).throw(
            self.m.subprocess.TimeoutExpired(cmd=["claude"], timeout=900))
        self.m.worker_once()
        c = self.m.db()
        c.execute("UPDATE jobs SET next_at=0")   # skip the retry backoff
        c.commit()
        c.close()
        self.m.worker_once()
        state, _attempts, timeouts, note = self._job()
        self.assertEqual("quarantined", state)
        self.assertEqual(2, timeouts)
        self.assertIn("2 timed-out attempts", note)

    def test_an_expensive_nonzero_exit_counts_against_the_deadline_budget(self):
        self.m.enqueue("o/r", 1, self.A, "webhook")
        self.m.review_job = lambda *a: (_ for _ in ()).throw(
            self.m.ExpensiveFailure("llm exit 1 [after 880s]"))
        self.m.worker_once()
        _state, _attempts, timeouts, _note = self._job()
        self.assertEqual(1, timeouts, "a near-deadline nonzero exit is not a cheap failure")

    def test_timeout_counter_survives_a_later_success(self):
        self.m.enqueue("o/r", 1, self.A, "webhook")
        self.m.review_job = lambda *a: (_ for _ in ()).throw(
            self.m.subprocess.TimeoutExpired(cmd=["claude"], timeout=900))
        self.m.worker_once()
        c = self.m.db()
        c.execute("UPDATE jobs SET next_at=0")
        c.commit()
        c.close()
        self.m.review_job = lambda *a: ("done", 7, "1 inline / 0 demoted / clean")
        self.m.worker_once()
        state, _attempts, timeouts, _note = self._job()
        self.assertEqual("done", state)
        self.assertEqual(1, timeouts, "the counter must not be silently reset by a success")

    def test_a_completed_head_retires_a_quarantine_left_on_an_older_head(self):
        """The enqueue race: head B is queued while head A is still RUNNING, so A is not
        retired by enqueue and its later give-up would keep the alert up for 24h about a PR
        that B reviewed perfectly well."""
        self.m.enqueue("o/r", 1, self.A, "webhook")
        c = self.m.db()
        c.execute("UPDATE jobs SET state='quarantined' WHERE head_sha=?", (self.A,))
        c.commit()
        c.close()
        self.m.enqueue("o/r", 1, self.B, "head-moved")
        c = self.m.db()
        c.execute("UPDATE jobs SET state='quarantined' WHERE head_sha=?", (self.A,))  # A lost the race
        c.commit()
        c.close()
        self.m.review_job = lambda *a: ("done", 7, "clean")
        self.m.worker_once()
        c = self.m.db()
        rows = dict(c.execute("SELECT head_sha,state FROM jobs"))
        c.close()
        self.assertEqual("superseded", rows[self.A])
        self.assertEqual("done", rows[self.B])

    def test_dedupe_path_also_retires_an_older_head_quarantine(self):
        old = real_time.time() - 3600
        c = self.m.db()
        c.execute("INSERT INTO jobs(repo,pr,head_sha,state,created,updated) "
                  "VALUES('o/r',1,?,'quarantined',?,?)", (self.A, old, old))
        c.commit()
        c.close()
        self.m.enqueue("o/r", 1, self.B, "webhook")     # newer head
        self.m.enqueue("o/r", 1, self.B, "reconcile")   # dedupe hit
        c = self.m.db()
        rows = dict(c.execute("SELECT head_sha,state FROM jobs"))
        c.close()
        self.assertEqual("superseded", rows[self.A])

    def test_a_delayed_webhook_for_a_stale_head_cannot_clear_a_live_quarantine(self):
        """enqueue() also runs for a LATE webhook carrying an old head. If that retired the
        CURRENT head's quarantine, the reconciler would re-run it with fresh counters."""
        self.m.enqueue("o/r", 1, self.A, "webhook")             # old head, reviewed
        c = self.m.db()
        c.execute("UPDATE jobs SET state='done' WHERE head_sha=?", (self.A,))
        c.commit()
        c.close()
        self.m.enqueue("o/r", 1, self.B, "head-moved")          # current head
        c = self.m.db()
        c.execute("UPDATE jobs SET state='quarantined' WHERE head_sha=?", (self.B,))
        c.commit()
        c.close()
        self.m.enqueue("o/r", 1, self.A, "webhook")             # the DELAYED delivery
        c = self.m.db()
        rows = dict(c.execute("SELECT head_sha,state FROM jobs"))
        c.close()
        self.assertEqual("quarantined", rows[self.B],
                         "a stale head must not retire the current head's quarantine")

    def test_a_delayed_webhook_for_a_SUPERSEDED_head_cannot_clear_a_live_quarantine(self):
        """The hole both reviewers found on ailab#486, and the one my first test MISSED.

        The earlier version of this scenario gave the old head a 'done' row, which IS in the
        dedupe SELECT, so the delayed delivery took the dedupe path and was safely ignored.
        A head whose own row is 'SUPERSEDED' is not in that SELECT: it falls through to the
        INSERT path, whose coalesce UPDATE then clears the CURRENT head's live quarantine and
        re-enqueues the stale head with fresh counters - including, before the ambiguous-POST
        carve-out, one the operator had not cleared."""
        self.m.enqueue("o/r", 1, self.A, "webhook")          # head A
        self.m.enqueue("o/r", 1, self.B, "head-moved")       # A -> superseded, B queued
        c = self.m.db()
        self.assertEqual("superseded",
                         c.execute("SELECT state FROM jobs WHERE head_sha=?", (self.A,)).fetchone()[0])
        c.execute("UPDATE jobs SET state='quarantined' WHERE head_sha=?", (self.B,))
        c.commit()
        c.close()

        self.m.enqueue("o/r", 1, self.A, "webhook")          # the DELAYED delivery

        c = self.m.db()
        rows = dict(c.execute("SELECT head_sha,state FROM jobs WHERE state<>'superseded'"))
        c.close()
        self.assertEqual("quarantined", rows.get(self.B),
                         "a stale head reached the coalesce path and cleared the live quarantine")

    def test_an_ambiguous_post_quarantine_is_never_auto_retired(self):
        """Only an operator (with --force) decides that one: auto-clearing it would let the
        reconciler re-run a review that may already have landed."""
        old = real_time.time() - 3600
        c = self.m.db()
        c.execute("INSERT INTO jobs(repo,pr,head_sha,state,created,updated,note) "
                  "VALUES('o/r',1,?,'quarantined',?,?,'ambiguous POST: timed out')",
                  (self.A, old, old))
        c.commit()
        c.close()
        self.m.enqueue("o/r", 1, self.B, "head-moved")   # new head: retires ordinary ones
        self.m.review_job = lambda *a: ("done", 7, "clean")
        self.m.worker_once()                            # completion: retires ordinary ones
        c = self.m.db()
        rows = dict(c.execute("SELECT head_sha,state FROM jobs"))
        c.close()
        self.assertEqual("quarantined", rows[self.A])


class QuarantineSweepTest(unittest.TestCase):
    """A quarantine on a closed/merged PR is not actionable and must not hold the alert up for
    its full 24h window; a transient API error must NOT silently clear one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)
        self.A = "a" * 40
        self.m.enqueue("o/r", 1, self.A, "webhook")
        c = self.m.db()
        c.execute("UPDATE jobs SET state='quarantined'")
        c.commit()
        c.close()

    def _state(self):
        c = self.m.db()
        st = c.execute("SELECT state FROM jobs").fetchone()[0]
        c.close()
        return st

    def test_closed_pr_quarantine_is_retired(self):
        self.m.api = lambda *a, **k: {"state": "closed"}
        self.m.retire_closed_quarantines()
        self.assertEqual("superseded", self._state())

    def test_open_pr_quarantine_is_kept(self):
        self.m.api = lambda *a, **k: {"state": "open"}
        self.m.retire_closed_quarantines()
        self.assertEqual("quarantined", self._state())

    def test_an_unexpected_payload_is_not_treated_as_closed(self):
        """`state != "open"` would read `{}`, an error object or a schema change as proof the
        PR is closed and silently clear a live quarantine."""
        self.m.api = lambda *a, **k: {}
        self.m.retire_closed_quarantines()
        self.assertEqual("quarantined", self._state())

    def test_sweep_does_not_clear_an_ambiguous_post(self):
        c = self.m.db()
        c.execute("UPDATE jobs SET note='ambiguous POST: read timed out'")
        c.commit()
        c.close()
        self.m.api = lambda *a, **k: {"state": "closed"}
        self.m.retire_closed_quarantines()
        self.assertEqual("quarantined", self._state())

    def test_api_error_leaves_the_row_alone(self):
        """Staying noisy is the safe failure: clearing on error would hide a real strand."""
        def boom(*_a, **_k):
            raise OSError("connection reset by peer")
        self.m.api = boom
        self.m.retire_closed_quarantines()
        self.assertEqual("quarantined", self._state())


class ConcurrencyTest(unittest.TestCase):
    """Four threads share one plain (non-reentrant) db_lock and one sqlite file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)

    def test_ticker_and_worker_do_not_deadlock_or_corrupt(self):
        """write_metrics() swallows its own exceptions, so "no exception escaped" is not
        evidence it worked: the file and the counters are asserted afterwards, and
        integrity_check proves the interleaved writes did not corrupt the database.
        review_job is stubbed so this exercises the worker transition rather than DNS."""
        errors = []
        self.m.review_job = lambda *a: ("done", 1, "clean")
        start = threading.Barrier(2)                    # force real overlap

        def spin_metrics():
            try:
                start.wait(timeout=30)
                for _ in range(40):
                    self.m.write_metrics()
            except Exception as e:                      # noqa: BLE001 - reported below
                errors.append(e)

        def spin_jobs():
            try:
                start.wait(timeout=30)
                for i in range(40):
                    self.m.enqueue("o/r", i, f"{i:040x}", "webhook")
                    self.m.bump_meta("llm_timeouts_total")
                    self.m.worker_once()
            except Exception as e:                      # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=spin_metrics), threading.Thread(target=spin_jobs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertFalse([t for t in threads if t.is_alive()], "deadlock: thread still running")
        self.assertEqual([], errors)

        text = pathlib.Path(self.m.CFG["textfile"]).read_text(encoding="utf-8")
        self.assertIn("reviewbot_heartbeat_timestamp_seconds", text)
        self.assertIn("reviewbot_llm_timeouts_total", text)
        c = self.m.db()
        self.assertEqual("ok", c.execute("PRAGMA integrity_check").fetchone()[0])
        self.assertEqual(40, c.execute("SELECT COUNT(*) FROM jobs WHERE state='done'").fetchone()[0])
        self.assertEqual(40.0, float(
            c.execute("SELECT v FROM meta WHERE k='llm_timeouts_total'").fetchone()[0]))
        c.close()

    def test_telemetry_never_propagates(self):
        """bump_meta runs inside the worker's exception handler; a raise there would escape
        the handler and kill the only worker thread."""
        self.m.CFG["state_db"] = "/nonexistent-dir/state.sqlite"
        self.m.bump_meta("llm_timeouts_total")      # must not raise
        self.m.record_gauge("llm_seconds", 1.0)     # must not raise


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)
        self.A = "a" * 40
        self._argv = sys.argv

    def tearDown(self):
        sys.argv = self._argv

    def _main(self, *args):
        sys.argv = ["reviewbot.py", "cfg"] + list(args)
        return self.m.main()

    def test_unknown_option_does_not_start_the_daemon(self):
        self.assertEqual(2, self._main("--oops"))

    def test_wrong_arity_is_rejected(self):
        self.assertEqual(2, self._main("--requeue", "o/r"))

    def test_non_integer_pr_is_rejected_without_a_traceback(self):
        self.assertEqual(2, self._main("--requeue", "o/r", "not-a-number"))

    def test_requeue_with_no_match_returns_1(self):
        self.assertEqual(1, self._main("--requeue", "o/r", "42"))

    def test_requeue_refuses_an_ambiguous_post_without_force(self):
        """A retry is cheap but NOT idempotent: Gitea may commit the original POST after the
        requeued worker's marker check and before its own, double-posting the review."""
        self.m.enqueue("o/r", 1, self.A, "webhook")
        c = self.m.db()
        c.execute("UPDATE jobs SET state='quarantined', note='ambiguous POST: timed out'")
        c.commit()
        c.close()
        self.assertEqual(2, self._main("--requeue", "o/r", "1"))
        c = self.m.db()
        self.assertEqual("quarantined", c.execute("SELECT state FROM jobs").fetchone()[0])
        c.close()

    def test_force_overrides_the_ambiguous_post_refusal(self):
        self.m.enqueue("o/r", 1, self.A, "webhook")
        c = self.m.db()
        c.execute("UPDATE jobs SET state='quarantined', note='ambiguous POST: timed out'")
        c.commit()
        c.close()
        self.assertEqual(0, self._main("--requeue", "o/r", "1", "--force"))
        c = self.m.db()
        self.assertEqual("queued", c.execute("SELECT state FROM jobs").fetchone()[0])
        c.close()

    def test_exhausted_quarantine_requeues_without_force(self):
        self.m.enqueue("o/r", 1, self.A, "webhook")
        c = self.m.db()
        c.execute("UPDATE jobs SET state='quarantined', "
                  "note='deadline exhausted after 2 timed-out attempts'")
        c.commit()
        c.close()
        self.assertEqual(0, self._main("--requeue", "o/r", "1"))


class StaleHeadTest(unittest.TestCase):
    """Residual found by both personas on ailab#486: the enqueue cutoff stopped a stale head
    SUPERSEDING newer rows, but it was still re-inserted as a fresh job, and the completion
    retirement matched ANY other head while its comment promised earlier-only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)
        self.A = "a" * 40
        self.B = "b" * 40

    def _rows(self):
        c = self.m.db()
        rows = list(c.execute("SELECT head_sha,state FROM jobs ORDER BY id"))
        c.close()
        return rows

    def test_a_late_webhook_does_not_resurrect_a_superseded_head(self):
        self.m.enqueue("o/r", 1, self.A, "webhook")
        self.m.enqueue("o/r", 1, self.B, "head-moved")      # A -> superseded
        before = self._rows()
        self.m.enqueue("o/r", 1, self.A, "pull_request")    # the late delivery
        self.assertEqual(before, self._rows(), "a stale head must not be re-inserted")

    def test_the_reconciler_may_still_resurrect_it(self):
        """A force-push back to an earlier SHA makes that head current again. The reconciler
        read it from the API, so it is authoritative and must NOT be ignored - otherwise the
        PR is silently stranded with no verdict."""
        self.m.enqueue("o/r", 1, self.A, "webhook")
        self.m.enqueue("o/r", 1, self.B, "head-moved")
        self.m.enqueue("o/r", 1, self.A, "reconcile")
        self.assertIn((self.A, "queued"), self._rows())

    def test_completion_retires_only_EARLIER_quarantines(self):
        """The comment said EARLIER; the SQL said any-other-head. A job for a stale head that
        reached done could therefore clear the CURRENT head's live quarantine."""
        old = real_time.time() - 3600
        c = self.m.db()
        # A is the OLD head being processed; B is the CURRENT head, quarantined.
        c.execute("INSERT INTO jobs(repo,pr,head_sha,state,created,updated,next_at) "
                  "VALUES('o/r',1,?,'queued',?,?,0)", (self.A, old, old))
        c.execute("INSERT INTO jobs(repo,pr,head_sha,state,created,updated) "
                  "VALUES('o/r',1,?,'quarantined',?,?)",
                  (self.B, real_time.time(), real_time.time()))
        c.commit()
        c.close()
        self.m.review_job = lambda *a: ("done", 7, "clean")
        self.m.worker_once()                                # processes A (older created)
        c = self.m.db()
        state = c.execute("SELECT state FROM jobs WHERE head_sha=?", (self.B,)).fetchone()[0]
        c.close()
        self.assertEqual("quarantined", state,
                         "a stale head completing must not clear the current head's quarantine")


# The verbatim text the claude CLI returned on 2026-09-06, which quarantined 8 PRs.
REAL_LIMIT_TEXT = ("llm exit 1: subtype=success result=You've hit your session limit \u00b7 "
                   "resets 4:20pm (UTC) [stderr: empty]")


# Every limit message this estate has actually produced, verbatim from the journal, with the
# action each one REQUIRES. They are not interchangeable: the account-scoped ones must park,
# the model-scoped ones must fall back. Table-driven precisely because the previous code had a
# single regex and therefore a single answer for all of them. Both CLIs are in here: the
# predicate is shared, so a wording either persona can produce belongs in the same table.
LIMIT_MESSAGES = [
    # (label, error text, expect_park)
    ("weekly/account",
     "llm exit 1: subtype=success result=You've hit your weekly limit \u00b7 "
     "resets 2am (UTC) [stderr: empty]", True),
    ("session/account",
     "llm exit 1: subtype=success result=You've hit your session limit \u00b7 "
     "resets 4:20pm (UTC) [stderr: empty]", True),
    ("Fable 5/model (2026-09-06 wording)",
     "llm exit 1: subtype=success result=You've reached your Fable 5 limit. Run "
     "/usage-credits to continue or switch models with /model. [stderr: empty]", False),
    # Found live on 2026-09-10 while repointing reviewer-1 at an account with quota. Same
    # meaning, different sentence - and it did NOT match a pattern written for the first one
    # hours earlier, which is why MODEL_LIMIT_RE is anchored on the two words they share.
    ("Fable 5/model (2026-09-10 wording)",
     "llm exit 1: subtype=success result=You're out of usage credits. Run /usage-credits "
     "to keep using Fable 5 or /model to switch models. [stderr: empty]", False),
    # The codex persona's wording (2026-09-15). ACCOUNT-scoped: the login is out of window,
    # and there is no other model to move to - `gpt-6` is rejected outright for a ChatGPT
    # account, so a fallback here would only be a second refusal. Note it carries the word
    # `credits` WITHOUT the CLI's `/usage-credits` remedy, which is exactly the near-miss
    # MODEL_LIMIT_RE's two-token anchor was written to survive.
    ("codex usage window/account",
     "codex produced no output (exit 1): ERROR: You've hit your usage limit. Visit "
     "https://chatgpt.com/codex/settings/usage to purchase more credits or try again at "
     "Sep 21st, 2026 9:38 AM.", True),
]


class LimitScopeTest(unittest.TestCase):
    """Park or fall back is decided by the SCOPE of the limit, never by the word "limit".

    THE INCIDENT (2026-09-10, ailab#633): reviewer-1's account hit `You've hit your weekly
    limit \u00b7 resets 2am (UTC)`. RATE_LIMIT_RE matched only session/usage/rate, so instead
    of parking, _run_llm() took the fallback branch and ran `opus` on the SAME exhausted
    account - 45 times, zero parks, and jobs quarantined at attempt 5 against a limit 13 hours
    from resetting.

    THE TRAP IN FIXING IT: the obvious repair - match any "...limit" - is worse than the bug.
    `You've reached your Fable 5 limit ... switch models with /model` is MODEL-scoped, was seen
    463 times over 4 days from 2026-09-06, and the fallback rescued every single review. Parking
    on it would convert a non-incident into an outage. Hence MODEL_LIMIT_RE, checked first, and
    anchored on the CLI's own remedy ("switch models with /model") rather than on "reached your
    ... limit" - which would also match "reached your weekly limit" and reintroduce the bug."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)

    def test_each_real_message_selects_the_right_action(self):
        for label, text, expect_park in LIMIT_MESSAGES:
            with self.subTest(label=label):
                model = bool(self.m.MODEL_LIMIT_RE.search(text))
                account = bool(self.m.RATE_LIMIT_RE.search(text))
                # This is the exact predicate _run_llm() uses.
                parks = (not model) and account
                self.assertEqual(expect_park, parks,
                                 "%s: model=%s account=%s" % (label, model, account))

    def test_model_scope_wins_over_account_scope(self):
        """Precedence, pinned on its own: if a message ever matched BOTH, falling back is the
        recoverable choice and parking is not."""
        # Must carry BOTH remedy tokens, or it is not the CLI's remedy at all - that is
        # what the tightening in ailab#634 established, and a weaker example here would
        # assert the precedence against a message that no longer matches MODEL_LIMIT_RE.
        both = ("You've reached your weekly limit. Run /usage-credits to continue "
                "or switch models with /model.")
        self.assertTrue(self.m.MODEL_LIMIT_RE.search(both))
        self.assertTrue(self.m.RATE_LIMIT_RE.search(both))
        self.assertFalse((not self.m.MODEL_LIMIT_RE.search(both))
                         and bool(self.m.RATE_LIMIT_RE.search(both)))

    def test_review_prose_cannot_SUPPRESS_a_real_park(self):
        """reviewer-claude on ailab#634, and the mirror of the test below.

        MODEL_LIMIT_RE is searched against llm_error_text(), whose detail includes the
        envelope's `result` - i.e. MODEL-AUTHORED text. Since the predicate is `not MODEL and
        RATE`, a spurious MODEL match suppresses a park that should happen, which is how this
        incident started. A genuine account-limit message that merely happens to carry the
        words `switch models` - a review of THIS file would - must still park."""
        text = ("llm exit 1: subtype=success result=You've hit your weekly limit \u00b7 resets "
                "2am (UTC). The reviewer suggested we switch models for the next run. "
                "[stderr: empty]")
        self.assertTrue(self.m.RATE_LIMIT_RE.search(text))
        self.assertIsNone(self.m.MODEL_LIMIT_RE.search(text),
                          "bare 'switch models' prose must not read as the CLI's remedy")
        self.assertTrue((not self.m.MODEL_LIMIT_RE.search(text))
                        and bool(self.m.RATE_LIMIT_RE.search(text)),
                        "a real account limit must still park")

    def test_the_remedy_needs_both_tokens_in_either_order(self):
        """Both observed phrasings carry `/usage-credits` AND `switch models`, in opposite
        orders - so neither order may be hard-coded, and neither token alone may qualify."""
        for text in (LIMIT_MESSAGES[2][1], LIMIT_MESSAGES[3][1]):
            with self.subTest(text=text[:52]):
                self.assertIsNotNone(self.m.MODEL_LIMIT_RE.search(text))
        for half in ("Run /usage-credits to continue.", "you could switch models instead"):
            with self.subTest(half=half):
                self.assertIsNone(self.m.MODEL_LIMIT_RE.search(half),
                                  "one token alone is not the CLI's remedy")

    def test_ordinary_review_prose_does_not_park_the_persona(self):
        """A park stops the SEAT, and with one seat that is the whole persona - every repo, not
        one PR - so a false
        positive is expensive. These are the near-misses the word boundaries exist for."""
        for text in ("This PR adds a rate limiter; the daily limitation is documented.",
                     "raise the concurrency limits for the weekly digest job",
                     "delimit the field with a comma"):
            with self.subTest(text=text[:34]):
                self.assertFalse(
                    (not self.m.MODEL_LIMIT_RE.search(text))
                    and bool(self.m.RATE_LIMIT_RE.search(text)),
                    "would have parked the whole persona on: %s" % text)

    def test_a_bare_hour_reset_is_parsed_not_discarded(self):
        """`resets 2am (UTC)` carries a real reset time. RESET_RE used to require HH:MM, so it
        parsed nothing and park() fell back to DEFAULT_PARK_S - a 15-minute retry loop against
        a limit 13 hours out."""
        at = self.m.parse_reset("You've hit your weekly limit \u00b7 resets 2am (UTC)")
        self.assertIsNotNone(at, "a whole-hour reset must still parse")
        self.assertEqual((2, 0), real_time.gmtime(at)[3:5])
        self.assertGreater(at, real_time.time())

    def test_optional_minutes_did_not_break_the_HH_MM_form(self):
        for text, hhmm in (("resets 4:20pm (UTC)", (16, 20)),
                           ("resets 11:20am (UTC)", (11, 20)),
                           ("resets 12:30am (UTC)", (0, 30)),
                           ("resets 9am (UTC)", (9, 0)),
                           ("resets 12pm (UTC)", (12, 0)),
                           ("resets 12am (UTC)", (0, 0))):
            with self.subTest(text=text):
                self.assertEqual(hhmm, real_time.gmtime(self.m.parse_reset(text))[3:5])

    def test_a_bare_hour_reset_does_not_raise(self):
        """int(None) on the now-optional minutes group would surface as an ordinary failure and
        defeat the park on exactly the messages RESET_RE was widened to read."""
        for text in ("resets 2am (UTC)", "resets 2 (UTC)", "resets 23 (UTC)"):
            with self.subTest(text=text):
                self.m.parse_reset(text)  # must not raise

    def test_a_parked_persona_still_never_consumes_an_attempt(self):
        """The property the whole park exists for, re-asserted for the weekly wording."""
        text = LIMIT_MESSAGES[0][1]
        e = self.m.RateLimited(text, self.m.parse_reset(text))
        state, attempts, timeouts, note = self.m.next_failure_state(e, 4, 1)
        self.assertEqual(("retry", 4, 1), (state, attempts, timeouts))


# Verbatim stderr from reviewer-2, on every attempt of every job between 2026-09-14 21:47 and
# 2026-09-15 06:33. codex writes its refusal to STDERR and exits 1 having written no output
# file, so it arrives at the "codex produced no output" raise rather than through any envelope
# the claude branch knows how to read.
CODEX_LIMIT_STDERR = ("ERROR: You've hit your usage limit. Visit "
                      "https://chatgpt.com/codex/settings/usage to purchase more credits or "
                      "try again at Sep 21st, 2026 9:38 AM.\n")


class CodexLimitParkTest(unittest.TestCase):
    """The park is the CLAUDE branch's, and the codex branch never had it (2026-09-15).

    THE INCIDENT: reviewer-2's codex login ran out of usage window at 2026-09-14 21:47. The
    claude branch would have parked the persona and left every job's attempt budget alone; the
    codex branch raised a plain RuntimeError, so each job spent all 5 attempts against the same
    wall and quarantined. 11 jobs quarantined, 10 PRs sat blocked on `codex=no review` for ~9 h,
    and because quarantine is deliberately sticky the reconciler never retried them - they
    needed `--requeue` by hand, against a window that had in fact reopened by 06:33.

    That is the SAME failure the claude branch was fixed for on 2026-09-06 (8 PRs, empty queue,
    the reviewer looking idle and healthy throughout). One persona having the fix is the whole
    bug: the automerge lane needs EVERY persona clean, so a codex quarantine storm stops the
    lane just as dead as a claude one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # llm_fallback_model is deliberately empty: _run_llm's fallback branch is claude-only,
        # so for codex the park is the ONLY thing between a spent window and a quarantine.
        # Matches host_vars/reviewer-2.yml, which sets no fallback.
        self.m = load(self.tmp.name, llm_kind="codex", llm_model="gpt-6-astra",
                      llm_fallback_model="", llm_timeout_s=600, llm_sudo_user="")
        self.addCleanup(setattr, self.m, "RATE_LIMITED_UNTIL", 0.0)

    def _fake_run(self, stderr, elapsed=0.0):
        """codex exits 1 and writes no --output-last-message file, which is what makes the
        output empty. Nothing about that path is stubbed: the test drives the real one."""
        def run(args, **kw):
            if elapsed:
                type(self.clock).now += elapsed
            return self.m.subprocess.CompletedProcess(args, 1, "", stderr)
        return run

    def _freeze_clock(self):
        class Clock:
            now = 1000.0

            def monotonic(self):
                return Clock.now

            def time(self):
                return real_time.time()

            def sleep(self, _n):
                pass

            def strftime(self, *a):
                return real_time.strftime(*a)

        self.clock = Clock()
        self.m.time = self.clock

    def test_a_spent_codex_window_parks_instead_of_failing_the_job(self):
        self.m.subprocess.run = self._fake_run(CODEX_LIMIT_STDERR)
        with self.assertRaises(self.m.RateLimited):
            self.m.run_llm("t", "d", "diff")

    def test_the_park_leaves_the_attempt_budget_untouched(self):
        """The property that decides quarantine-or-not, asserted through the real policy
        function rather than by re-reading the raise site."""
        self.m.subprocess.run = self._fake_run(CODEX_LIMIT_STDERR)
        state = attempts = timeouts = None
        try:
            self.m.run_llm("t", "d", "diff")
        except self.m.RateLimited as e:
            state, attempts, timeouts, _note = self.m.next_failure_state(e, 4, 1)
        self.assertEqual(("retry", 4, 1), (state, attempts, timeouts),
                         "attempt 5 against a spent window is what quarantined 11 jobs")

    def test_an_ordinary_empty_output_is_still_an_ordinary_failure(self):
        """The park must stay narrow. An empty output with no limit in it is a real failure and
        MUST keep burning attempts - otherwise a broken CLI parks the persona forever."""
        self.m.subprocess.run = self._fake_run("ERROR: stream disconnected before completion")
        with self.assertRaises(RuntimeError) as cm:
            self.m.run_llm("t", "d", "diff")
        self.assertNotIsInstance(cm.exception, self.m.RateLimited)
        state, attempts, _t, _n = self.m.next_failure_state(cm.exception, 4, 0)
        self.assertEqual(("quarantined", 5), (state, attempts))

    def test_a_slow_refusal_is_a_park_and_not_a_deadline_failure(self):
        """run_llm re-bills any failure past a third of the budget as an ExpensiveFailure, which
        the worker charges to the DEADLINE cap (2, not 5) - and an ExpensiveFailure is not a
        RateLimited, so the park would be silently dropped for exactly the slow refusals. codex
        can stream for minutes before the window cuts it off, so this is not hypothetical."""
        self._freeze_clock()
        self.m.subprocess.run = self._fake_run(CODEX_LIMIT_STDERR, elapsed=400.0)
        with self.assertRaises(self.m.RateLimited):
            self.m.run_llm("t", "d", "diff")

    def test_trailing_stderr_cannot_push_the_refusal_out_of_the_park_decision(self):
        """reviewer-claude, round 1 of ailab#729. The journal line keeps only the last 300
        chars of stderr, and for one commit that truncated string was what the park was
        decided on - so a refusal followed by any trailing output would have missed the
        match and quarantined, which is the exact storm this class exists to prevent. The
        decision reads the whole stream; only the message is trimmed."""
        noise = "\n".join("codex: reconnecting to stream (attempt %d)" % i for i in range(1, 12))
        self.assertGreater(len(noise), 300, "the trailing noise must overflow the window")
        self.m.subprocess.run = self._fake_run(CODEX_LIMIT_STDERR + noise)
        with self.assertRaises(self.m.RateLimited) as cm:
            self.m.run_llm("t", "d", "diff")
        self.assertNotIn("usage limit", str(cm.exception),
                         "the reported message is still the (truncated) tail")

    def test_the_weekly_date_in_the_message_is_deliberately_not_parsed(self):
        """codex names a WEEKLY reset ("try again at Sep 21st, 2026 9:38 AM") while the thing
        that actually reopens is the rolling window - on 2026-09-15 the very PRs that failed all
        morning reviewed clean at 06:33, six days before the date in the message. RESET_RE does
        not read that format, so the park falls to DEFAULT_PARK_S and re-probes every 15
        minutes, which is the behaviour we want. Pinned so that "improving" parse_reset to read
        it would have to argue with this test first."""
        self.assertIsNone(self.m.parse_reset(CODEX_LIMIT_STDERR))
        at = self.m.park(self.m.parse_reset(CODEX_LIMIT_STDERR))
        self.assertLessEqual(at - real_time.time(), self.m.DEFAULT_PARK_S + 1)


class RateLimitTest(unittest.TestCase):
    """A subscription rate limit is an ACCOUNT condition, not a PR failure.

    2026-09-06: every queued PR burned its 5 attempts against the same account-wide wall and
    quarantined, leaving 8 PRs permanently unreviewed behind an EMPTY QUEUE — the reviewer
    looked idle and healthy while nothing was being reviewed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)
        self.addCleanup(setattr, self.m, "RATE_LIMITED_UNTIL", 0.0)

    def test_the_real_error_text_is_recognised(self):
        self.assertTrue(self.m.RATE_LIMIT_RE.search(REAL_LIMIT_TEXT))

    def test_reset_time_is_parsed_from_the_error(self):
        at = self.m.parse_reset(REAL_LIMIT_TEXT)
        self.assertIsNotNone(at)
        self.assertGreater(at, real_time.time())
        self.assertEqual((16, 20), real_time.gmtime(at)[3:5])

    def test_am_pm_and_midnight_edges(self):
        for text, hh in (("resets 11:20am (UTC)", 11), ("resets 12:30am (UTC)", 0),
                         ("resets 12:30pm (UTC)", 12), ("resets 4:20pm (UTC)", 16)):
            with self.subTest(text=text):
                self.assertEqual(hh, real_time.gmtime(self.m.parse_reset(text))[3])

    def test_an_unparsable_reset_is_not_fatal(self):
        self.assertIsNone(self.m.parse_reset("you have hit your session limit, try later"))

    def test_a_far_future_reset_cannot_wedge_the_worker(self):
        """A misparse must not park the persona for a week."""
        at = self.m.park(real_time.time() + 90 * 86400)
        self.assertLessEqual(at - real_time.time(), self.m.MAX_PARK_S + 1)

    def test_no_reset_time_still_parks_for_the_default(self):
        at = self.m.park(None)
        self.assertGreater(at - real_time.time(), 60)

    def test_it_never_consumes_an_attempt(self):
        """THE fix. Attempts must be unchanged, so no number of rate-limit windows can
        exhaust a PR's retry budget and quarantine it."""
        e = self.m.RateLimited(REAL_LIMIT_TEXT, real_time.time() + 600)
        state, attempts, timeouts, note = self.m.next_failure_state(e, 4, 1)
        self.assertEqual(("retry", 4, 1), (state, attempts, timeouts))
        self.assertIn("no attempt consumed", note)

    def test_it_never_quarantines_even_at_the_attempt_ceiling(self):
        e = self.m.RateLimited(REAL_LIMIT_TEXT, None)
        for attempts in (0, 4, 5, 99):
            with self.subTest(attempts=attempts):
                self.assertEqual("retry", self.m.next_failure_state(e, attempts, 0)[0])

    def test_it_is_not_billed_to_the_deadline_budget(self):
        e = self.m.RateLimited(REAL_LIMIT_TEXT, None)
        self.assertFalse(self.m.is_budget_failure(e))

    def test_the_worker_defers_the_job_to_the_reset_and_keeps_the_queue(self):
        head = "a" * 40
        self.m.enqueue("o/r", 1, head, "webhook")
        reset = real_time.time() + 1200

        def boom(*a, **k):
            raise self.m.RateLimited(REAL_LIMIT_TEXT, reset)
        self.m.review_job = boom
        self.m.worker_once()

        c = self.m.db()
        state, attempts, next_at = c.execute(
            "SELECT state, attempts, next_at FROM jobs WHERE head_sha=?", (head,)).fetchone()
        c.close()
        self.assertEqual("retry", state)
        self.assertEqual(0, attempts, "the PR must not pay for the subscription")
        self.assertAlmostEqual(reset, next_at, delta=60,
                               msg="deferred to the reset, not an exponential backoff")
        self.assertGreater(self.m.RATE_LIMITED_UNTIL, real_time.time())

    def test_the_whole_worker_parks_rather_than_walking_the_queue_into_the_wall(self):
        """8 PRs quarantined because each one discovered the same wall separately."""
        for i in range(3):
            self.m.enqueue("o/r", i + 10, f"{i:040x}", "webhook")
        calls = []

        def boom(jid, repo, pr, head):
            calls.append(pr)
            raise self.m.RateLimited(REAL_LIMIT_TEXT, real_time.time() + 900)
        self.m.review_job = boom
        for _ in range(3):
            self.m.worker_once()
        self.assertEqual(1, len(calls), "only the first job may hit the wall")

    def test_recovery_after_the_reset(self):
        self.m.RATE_LIMITED_UNTIL = real_time.time() - 1
        self.m.enqueue("o/r", 20, "b" * 40, "webhook")
        self.m.review_job = lambda *a: ("done", 7, "clean")
        self.assertIsNotNone(self.m.worker_once())


class AuxRunTest(unittest.TestCase):
    """Auxiliary subprocesses (reading the model's output file, the auth file) share the
    operation budget, but their OWN timeout must not be billed as an LLM deadline: run_llm
    re-raises TimeoutExpired verbatim and the worker charges it against the cap of 2, even
    when the model itself finished quickly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)

    def test_a_timeout_becomes_an_ordinary_failure(self):
        def boom(args, **kw):
            raise self.m.subprocess.TimeoutExpired(cmd=args, timeout=kw.get("timeout"))
        self.m.subprocess.run = boom
        with self.assertRaises(RuntimeError) as ctx:
            self.m.aux_run(["sudo", "cat", "x"], lambda: 500.0)
        self.assertNotIsInstance(ctx.exception, self.m.subprocess.TimeoutExpired)
        self.assertFalse(self.m.is_budget_failure(ctx.exception),
                         "an auxiliary read must not consume the deadline budget")

    def test_it_never_outlives_the_remaining_budget(self):
        seen = {}

        def record(args, **kw):
            seen["timeout"] = kw.get("timeout")
            return self.m.subprocess.CompletedProcess(args, 0, "", "")
        self.m.subprocess.run = record
        self.m.aux_run(["sudo", "cat", "x"], lambda: 5.0)
        self.assertEqual(5.0, seen["timeout"], "must not use a fixed 60s past the deadline")
        self.m.aux_run(["sudo", "cat", "x"], lambda: 500.0)
        self.assertEqual(60.0, seen["timeout"], "and must still cap at 60s when time is ample")


def _sec(path, body=b"+x\n", old=None):
    """One diff section for `path`, byte-exact in the shape git emits."""
    old = old or path
    return (b"diff --git a/" + old.encode() + b" b/" + path.encode() + b"\n"
            b"--- a/" + old.encode() + b"\n+++ b/" + path.encode() + b"\n"
            b"@@ -0,0 +1 @@\n" + body)


class SplitSectionsTest(unittest.TestCase):
    """Byte accounting is the whole safety property: if the split is not exact, the diff the
    model reads is not the diff whose hunk coordinates get validated."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)

    def test_round_trips_byte_for_byte(self):
        raw = _sec("a.py") + _sec("b/c.py") + _sec("d.md")
        secs = self.m.split_sections(raw)
        self.assertEqual(3, len(secs))
        self.assertEqual(raw, b"".join(x["data"] for x in secs))

    def test_captures_both_rename_paths(self):
        sec = self.m.split_sections(_sec("new.py", old="old.py"))[0]
        self.assertEqual(("old.py", "new.py"), (sec["a"], sec["b"]))

    def test_unquoted_path_with_spaces_is_parsed(self):
        """Git emits paths containing spaces UNQUOTED in the `diff --git` line. Measured on
        platform#1084: 8 of 58 headers looked like
        `a/corpus/Auto Advantage Finance - Binder Packet.pdf b/...`, and a header regex built
        on \S+ missed every one — which made the whole 3 MB PR unparsable when dropping its
        PDFs would have left 968 bytes of real content to review."""
        raw = _sec("dir/Auto Advantage - Packet.pdf")
        secs = self.m.split_sections(raw)
        self.assertEqual(1, len(secs))
        self.assertEqual("dir/Auto Advantage - Packet.pdf", secs[0]["b"])
        self.assertEqual(raw, secs[0]["data"])

    def test_a_git_quoted_path_is_decoded_and_stays_reviewable(self):
        """Git quotes any path with a non-ASCII byte and writes C/octal escapes, putting the
        a//b/ prefix INSIDE the quotes. The escape format is fully specified, so the name is
        decodable: excluding every such file dropped anyone's accented filename from every
        review AND gave a PR a one-character way to exclude its own payload."""
        raw = (b'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
               b'--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n@@ -0,0 +1 @@\n+x\n')
        sec = self.m.split_sections(raw)[0]
        self.assertEqual("caf\u00e9.py", sec["b"])
        self.assertEqual("caf\u00e9.py", sec["a"])
        self.assertIsNone(self.m.section_reason(sec), "a normal .py file, oddly named")

    def test_quoted_path_escapes(self):
        """The decoder accepts exactly git's escape set and refuses everything else — a
        malformed token must stay 'unparsable path', not become a guessed name."""
        u = self.m.unquote_path
        self.assertEqual("caf\u00e9.py", u(b'"caf\\303\\251.py"'))
        self.assertEqual('a b"c\td.py', u(b'"a b\\"c\\td.py"'))
        self.assertEqual("back\\slash.py", u(b'"back\\\\slash.py"'))
        # int(x, 8) would accept every one of the octal forms below; git writes exactly
        # three octal digits and nothing else.
        for bad in (b'"unterminated', b'no quotes at all', b'"\\q.py"', b'"\\77"',
                    b'"\\400.py"', b'"\\303.py"', b'"a"b"',
                    b'"a/\\0o7.py"', b'"a/\\1_0.py"', b'"a/\\ 12.py"', b'"a/\\089.py"'):
            with self.subTest(bad=bad):
                self.assertIsNone(u(bad))

    def test_an_unnameable_section_is_excluded_not_fatal(self):
        """One section we cannot name must not discard the other 57."""
        raw = _sec("good.py") + b"diff --git nonsense\n@@ -0,0 +1 @@\n+x\n"
        secs = self.m.split_sections(raw)
        self.assertEqual(2, len(secs))
        self.assertEqual(raw, b"".join(x["data"] for x in secs))
        self.assertIsNone(secs[1]["b"])
        self.assertEqual("unparsable path", self.m.section_reason(secs[1]))

    def test_preamble_is_refused(self):
        with self.assertRaises(ValueError):
            self.m.split_sections(b"warning: something\n" + _sec("a.py"))

    def test_empty_is_refused(self):
        with self.assertRaises(ValueError):
            self.m.split_sections(b"")


class SectionReasonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)

    def _reason(self, path, body=b"+x\n", old=None):
        return self.m.section_reason(self.m.split_sections(_sec(path, body, old))[0])

    def test_lockfiles_and_vendor_are_generated(self):
        for path in ("uv.lock", "sub/poetry.lock", "package-lock.json",
                     "vendor/x.go", "src/vendored/y.py", "node_modules/z.js", "a.min.js"):
            with self.subTest(path=path):
                self.assertEqual("generated", self._reason(path))

    def test_binary_extensions(self):
        for path in ("corpus/report.pdf", "img/logo.PNG", "dist/app.wasm"):
            with self.subTest(path=path):
                self.assertEqual("binary", self._reason(path))

    def test_binary_patch_marker_is_its_own_reason(self):
        """Git's marker is reached only for a path the glob and extension rules would have
        READ, so it reports the file's bytes, not policy — and one NUL inside payload.sh is
        enough to produce it. Distinct reason, because this one caps the verdict."""
        self.assertEqual("binary content",
                         self._reason("x.dat", b"\nBinary files a/x.dat and b/x.dat differ\n"))
        self.assertEqual("binary content",
                         self._reason("payload.sh", b"\nGIT binary patch\nliteral 4\nzc$x\n"))

    def test_a_source_file_that_merely_names_the_marker_is_still_reviewed(self):
        """Every line of a diff BODY carries a +/-/space prefix, so a column-0 marker can only
        be git's own. A bare substring search made THIS module classify itself as binary and
        drop out of its own review: the diff of reviewbot.py contains the marker as source."""
        body = (b'+    if BIN_MARKER_RE.search(sec["data"]):  # GIT binary patch\n'
                b'+# Binary files a/x and b/x differ  <- named in a comment\n')
        self.assertIsNone(self._reason("reviewbot.py", body))

    def test_non_utf8_quarantines_one_file(self):
        """platform#1074 carried a 0xf6 byte that failed EVERY attempt for 22h. Naming the one
        unreadable file beats feeding the model replacement characters for the whole PR."""
        self.assertEqual("non-utf8", self._reason("weird.py", b"+caf\xf6\n"))

    def test_a_pure_rename_is_nameable(self):
        """A 100%-similarity rename carries no ---/+++ and its `diff --git a/old b/new` header
        disagrees across the ` b/` split by definition. Before the rename lines were read, every
        such section was 'unparsable path' — which now caps the verdict, so a plain file move
        would have blocked automerge on any PR that contained one."""
        raw = (b"diff --git a/old/x.py b/new/x.py\nsimilarity index 100%\n"
               b"rename from old/x.py\nrename to new/x.py\n")
        sec = self.m.split_sections(raw)[0]
        self.assertEqual(("old/x.py", "new/x.py"), (sec["a"], sec["b"]))
        self.assertIsNone(self.m.section_reason(sec))

    def test_a_rename_is_disqualified_by_either_side(self):
        self.assertEqual("generated", self._reason("deps.txt", old="uv.lock"))

    def test_ordinary_code_and_fixtures_are_reviewable(self):
        """The safety property. Measured across 56 merged PRs, a fixtures/golden/testdata
        directory rule would have excluded ONLY tests/golden/... and tests/unit/fixtures/*.json
        — exactly where a regression hides. These must stay in scope."""
        for path in ("src/app.py", "tests/golden/broker.json", "tests/unit/fixtures/usage.json",
                     "testdata/case.yaml", "requirements.txt"):
            with self.subTest(path=path):
                self.assertIsNone(self._reason(path))


class PlanCoverageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)

    def _plan(self, raw, cap):
        self.m.CFG["max_diff_bytes"] = cap
        return self.m.plan_coverage(raw)

    def test_everything_fits(self):
        diff, dropped, cov = self._plan(_sec("a.py") + _sec("b.md"), 100000)
        self.assertEqual("full", cov)
        self.assertEqual([], dropped)
        self.assertIn("a.py", diff)

    def test_generated_exclusion_does_not_downgrade(self):
        """A lockfile is excluded by POLICY, not capacity — the reviewer can still vouch for
        the code. Marking every lockfile-touching PR partial would block ~7% of merges."""
        raw = _sec("uv.lock", b"+" + b"h" * 5000 + b"\n") + _sec("a.py")
        diff, dropped, cov = self._plan(raw, 100000)
        self.assertEqual("full", cov)
        self.assertEqual(["uv.lock"], [p for p, _, _ in dropped])
        self.assertNotIn("uv.lock", diff)

    def test_docs_shed_largest_first_and_downgrade_to_partial(self):
        raw = (_sec("code.py", b"+" + b"c" * 200 + b"\n")
               + _sec("small.md", b"+" + b"s" * 200 + b"\n")
               + _sec("big.md", b"+" + b"b" * 3000 + b"\n"))
        diff, dropped, cov = self._plan(raw, 1000)
        self.assertEqual("partial", cov)
        self.assertEqual(["big.md"], [p for p, r, _ in dropped if r == "dropped: size cap"])
        self.assertIn("code.py", diff, "code is never dropped")
        self.assertIn("small.md", diff, "only shed what is needed, largest first")

    def test_code_alone_over_cap_is_an_honest_skip(self):
        diff, dropped, cov = self._plan(_sec("huge.py", b"+" + b"x" * 5000 + b"\n"), 1000)
        self.assertEqual("over", cov)
        self.assertIsNone(diff)

    def test_a_diff_that_quotes_a_diff_header_does_not_inflate_the_count(self):
        """Self-referential: this very test file adds lines containing `diff --git a/`, and a
        substring count would read them as extra files and make the coverage table lie."""
        body = b'+    raw = b"diff --git a/x b/x"\n'
        raw = _sec("uv.lock", b"+lock\n") + _sec("tests/t.py", body)
        diff, dropped, cov = self._plan(raw, 100000)
        kept = len(self.m.SECTION_START.findall(diff.encode()))
        self.assertEqual(1, kept, "one real section, despite the literal in the added line")
        self.assertEqual(2, kept + len(dropped))

    def test_the_over_cap_notice_names_what_is_actually_too_big(self):
        """When nothing is reviewed, the author needs the BLOCKING files — not a list of the
        lockfiles we already excluded. Largest first, so the top of the table is what to split
        out. Measured need: platform#1081 is 406 KB of code with nothing droppable."""
        raw = (_sec("uv.lock", b"+" + b"l" * 100 + b"\n")
               + _sec("small.py", b"+" + b"s" * 100 + b"\n")
               + _sec("enormous.py", b"+" + b"x" * 5000 + b"\n"))
        diff, dropped, cov = self._plan(raw, 1000)
        self.assertEqual("over", cov)
        self.assertIn(("uv.lock", "generated"), [(p, r) for p, r, _ in dropped])
        over = [p for p, r, _ in dropped if r == "not reviewed: over cap"]
        self.assertEqual(["enormous.py", "small.py"], over)

    def test_nothing_reviewable_is_not_clean(self):
        diff, dropped, cov = self._plan(_sec("uv.lock"), 100000)
        self.assertEqual("none", cov)
        self.assertIsNone(diff)

    def test_the_measured_platform_1074_shape(self):
        """Reproduces the real composition: 53% lockfile, 22% docs, 24% code. The lockfile goes
        by policy; the docs go only because what remains still does not fit; every line of code
        is reviewed."""
        raw = (_sec("uv.lock", b"+" + b"l" * 5320 + b"\n")
               + _sec("plan-a.md", b"+" + b"d" * 1100 + b"\n")
               + _sec("plan-b.md", b"+" + b"d" * 1100 + b"\n")
               + _sec("strategies/c_ledger.py", b"+" + b"p" * 1200 + b"\n")
               + _sec("strategies/e_library.py", b"+" + b"p" * 1200 + b"\n"))
        diff, dropped, cov = self._plan(raw, 2800)
        self.assertEqual("partial", cov)
        self.assertIn("c_ledger.py", diff)
        self.assertIn("e_library.py", diff)
        self.assertNotIn("uv.lock", diff)
        reasons = {p: r for p, r, _ in dropped}
        self.assertEqual("generated", reasons["uv.lock"])
        self.assertTrue(any(r == "dropped: size cap" for r in reasons.values()))

    def test_unparsable_diff_raises_rather_than_guessing(self):
        with self.assertRaises(ValueError):
            self._plan(b"not a diff at all\n", 100000)


class CoverageTableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)

    def test_paths_are_neutralised(self):
        """Diff paths are attacker-controlled and land in a posted comment and in the prompt."""
        evil = "a/`code`|x\ny.py"
        t = self.m.coverage_table([(evil, "generated", 1)], 10, 1, 20, 2)
        self.assertNotIn("`code`", t)
        self.assertNotIn("\ny.py", t)

    def test_reports_the_arithmetic(self):
        t = self.m.coverage_table([("a.md", "dropped: size cap", 40)], 60, 2, 100, 3)
        self.assertIn("1 of 3 files", t)
        self.assertIn("40 of 100 bytes", t)


class RoundCountingTest(unittest.TestCase):
    """A skipped head must not advance the convergence counter. From round 3 the severity
    ladder stops holding the merge on `important` findings, so three over-cap skips used to buy
    a PR its first real review under relaxed rules — measured live on platform#1074/#1072/#1081,
    each sitting at round 4 with zero reviews between them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)

    def _job(self, head, verdict):
        c = self.m.db()
        c.execute("INSERT INTO jobs(repo,pr,head_sha,state,verdict,created,updated) "
                  "VALUES('o/r',1,?,'done',?,?,?)", (head, verdict, real_time.time(), real_time.time()))
        c.commit()
        c.close()

    def test_skipped_heads_do_not_advance_the_round(self):
        for i in range(3):
            self._job(f"{i:040x}", "skipped")
        self.assertEqual(1, self.m.review_round("o/r", 1))

    def test_real_reviews_advance_the_round(self):
        self._job("a" * 40, "clean")
        self._job("b" * 40, "findings")
        self.assertEqual(3, self.m.review_round("o/r", 1))

    def test_partial_does_not_advance_the_round(self):
        self._job("a" * 40, "partial")
        self.assertEqual(1, self.m.review_round("o/r", 1))

    def test_historical_skips_are_backfilled_not_counted(self):
        """THE headline bug this change claimed to fix, and nearly did not. Over-cap skips
        reached state='done' long before the verdict column existed, so counting every NULL
        verdict as a real review preserved the exact inflation being removed. Measured live:
        13 such rows across 7 PRs; platform#1074/#1072/#1081 were each at round 4 having never
        been reviewed once. Their note is the only surviving evidence, and the migration
        backfills from it.

        Built on a PRE-migration schema on purpose: the backfill fires once, when the column is
        created, which in production is the first start after deploy — with the legacy rows
        already present."""
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        dbp = pathlib.Path(d.name) / "state.sqlite"
        old = sqlite3.connect(dbp)
        old.execute("""CREATE TABLE jobs(
            id INTEGER PRIMARY KEY, repo TEXT, pr INTEGER, head_sha TEXT,
            state TEXT, attempts INTEGER DEFAULT 0, next_at REAL DEFAULT 0,
            created REAL, updated REAL, review_id INTEGER, note TEXT)""")
        for i in range(3):
            old.execute("INSERT INTO jobs(repo,pr,head_sha,state,note) "
                        "VALUES('o/r',1,?,'done',?)",
                        (f"{i:040x}", f"diff {i} B over size cap - skipped"))
        # one genuine review on a fourth head, which must still count
        old.execute("INSERT INTO jobs(repo,pr,head_sha,state,note) "
                    "VALUES('o/r',1,?,'done','2 inline / 0 demoted / findings')", ("f" * 40,))
        old.commit()
        old.close()

        m2 = load(d.name)                     # importing runs the migration + backfill
        self.assertEqual(2, m2.review_round("o/r", 1),
                         "only the real review counts; the three skips are backfilled")
        c = m2.db()
        self.assertEqual(3, c.execute(
            "SELECT COUNT(*) FROM jobs WHERE verdict='skipped'").fetchone()[0])
        c.close()

    def test_a_partial_review_with_findings_still_does_not_advance(self):
        """The downgrade to verdict='partial' only fires on a clean result, so a capacity-
        limited review that finds a blocker is recorded as 'findings' — correct for the merge
        gate, but still an incomplete read. Coverage is tracked separately for exactly this."""
        c = self.m.db()
        for i in range(2):
            c.execute("INSERT INTO jobs(repo,pr,head_sha,state,verdict,coverage,created,updated) "
                      "VALUES('o/r',2,?,'done','findings','partial',?,?)",
                      (f"{i:040x}", real_time.time(), real_time.time()))
        c.commit()
        c.close()
        self.assertEqual(1, self.m.review_round("o/r", 2),
                         "two partial reviews must not reach round 3's relaxed severity")

    def test_full_coverage_findings_do_advance(self):
        c = self.m.db()
        c.execute("INSERT INTO jobs(repo,pr,head_sha,state,verdict,coverage,created,updated) "
                  "VALUES('o/r',3,?,'done','findings','full',?,?)",
                  ("a" * 40, real_time.time(), real_time.time()))
        c.commit()
        c.close()
        self.assertEqual(2, self.m.review_round("o/r", 3))

    def test_legacy_rows_without_a_verdict_still_count(self):
        """Rows written before the verdict column existed were all real reviews."""
        self._job("a" * 40, None)
        self.assertEqual(2, self.m.review_round("o/r", 1))


class MigrationTest(unittest.TestCase):
    def test_adds_timeout_attempts_to_an_existing_database(self):
        """Deploys restart onto an existing state.sqlite; CREATE TABLE IF NOT EXISTS would
        leave the new column missing and every worker UPDATE would fail."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dbp = pathlib.Path(tmp.name) / "state.sqlite"
        old = sqlite3.connect(dbp)
        old.execute("""CREATE TABLE jobs(
            id INTEGER PRIMARY KEY, repo TEXT, pr INTEGER, head_sha TEXT,
            state TEXT, attempts INTEGER DEFAULT 0, next_at REAL DEFAULT 0,
            created REAL, updated REAL, review_id INTEGER, note TEXT)""")
        old.execute("INSERT INTO jobs(repo,pr,head_sha,state) VALUES('o/r',1,'c','queued')")
        old.commit()
        old.close()

        m = load(tmp.name)
        c = m.db()
        cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
        self.assertIn("timeout_attempts", cols)
        self.assertEqual((0,), c.execute("SELECT timeout_attempts FROM jobs").fetchone())
        c.close()


class MetricsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)

    def _emit(self):
        self.m.write_metrics()
        return dict(
            line.split(" ", 1) for line in
            pathlib.Path(self.m.CFG["textfile"]).read_text(encoding="utf-8").splitlines()
        )

    def test_new_series_are_emitted(self):
        got = self._emit()
        names = {k.split("{")[0] for k in got}
        for expected in ("reviewbot_running_job_age_seconds",
                         "reviewbot_quarantined_recent_jobs",
                         "reviewbot_llm_timeouts_total",
                         "reviewbot_llm_failures_total",
                         "reviewbot_llm_seconds_last",
                         "reviewbot_llm_seconds_max",
                         "reviewbot_llm_output_tokens_last",
                         "reviewbot_llm_output_tokens_max"):
            self.assertIn(expected, names)

    def test_running_job_age_is_zero_when_idle_and_positive_when_running(self):
        got = self._emit()
        age = [v for k, v in got.items() if k.startswith("reviewbot_running_job_age_seconds")][0]
        self.assertEqual(0, float(age))

        c = self.m.db()
        c.execute("INSERT INTO jobs(repo,pr,head_sha,state,created,updated) "
                  "VALUES('o/r',1,'c','running',?,?)",
                  (real_time.time() - 500, real_time.time() - 500))
        c.commit()
        c.close()
        got = self._emit()
        age = [v for k, v in got.items() if k.startswith("reviewbot_running_job_age_seconds")][0]
        self.assertGreater(float(age), 400, "a wedged worker must be visible behind a fresh heartbeat")

    def test_quarantined_recent_window_excludes_old_rows(self):
        """The cumulative gauge never falls for a PR that was closed rather than pushed to, so
        alerting on it would latch forever; the 24h window is what self-clears."""
        c = self.m.db()
        c.execute("INSERT INTO jobs(repo,pr,head_sha,state,created,updated) "
                  "VALUES('o/r',1,'c','quarantined',0,?)", (real_time.time() - 200000,))
        c.commit()
        c.close()
        got = self._emit()
        recent = [v for k, v in got.items() if k.startswith("reviewbot_quarantined_recent_jobs")][0]
        total = [v for k, v in got.items() if k.startswith("reviewbot_quarantined_jobs")][0]
        self.assertEqual(0, float(recent))
        self.assertEqual(1, float(total))

    def test_counters_survive_and_accumulate(self):
        self.m.bump_meta("llm_timeouts_total")
        self.m.bump_meta("llm_timeouts_total")
        got = self._emit()
        val = [v for k, v in got.items() if k.startswith("reviewbot_llm_timeouts_total")][0]
        self.assertEqual(2, float(val))


class ToolDenyTest(unittest.TestCase):
    def test_no_unknown_tool_names_in_the_deny_list(self):
        """"LS" matched no tool in claude CLI 2.x, so the CLI warned on EVERY run and that
        warning went on to masquerade as a review failure."""
        src = SRC.read_text(encoding="utf-8")
        args_block = src.split("--disallowedTools", 1)[1].split("]", 1)[0]
        self.assertNotIn('"LS"', args_block)
        for kept in ("Bash", "Read", "Grep", "Glob", "Write", "Edit"):
            self.assertIn(f'"{kept}"', args_block)




class SizeCapTest(unittest.TestCase):
    """The over-cap path must be VISIBLE and the cap must be measured on bytes.

    Two 2026-09 incidents pin this class. agentforge-platform#194 (480 KB) hit the cap and
    review_job returned 'done' with no Gitea write, so the PR showed no review at all and a
    skip was indistinguishable from a reviewer outage. platform#1074 diffed a PDF corpus as
    text: `api(raw=True)` decoded the body strictly BEFORE the cap was checked, raised
    UnicodeDecodeError on every attempt and quarantined the job (ReviewbotQuarantined)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name, max_diff_bytes=100)
        self.posted = []
        self.llm_calls = []
        m = self.m
        m.pr_ok = lambda repo, pr, head: {"head": {"sha": head}, "user": {"login": "someone"},
                                          "title": "t", "body": ""}
        m.existing_marker = lambda repo, pr, head: None
        m.maybe_merge = lambda repo, pr: None
        m.convergence_context = lambda *a, **k: ""
        m.review_round = lambda repo, pr: 1

        def run_llm(*a, **k):
            self.llm_calls.append(a)
            return {"findings": [], "summary": "looks fine"}
        m.run_llm = run_llm

    def _fake_api(self, diff_bytes):
        posted = self.posted

        def api(path, method="GET", body=None, raw=False):
            if path.endswith(".diff"):
                assert raw, "the diff must be fetched raw (bytes)"
                return diff_bytes
            if method == "POST":
                posted.append((path, body))
                return {"id": 77}
            return {}
        self.m.api = api

    def test_over_cap_posts_a_visible_skip_and_never_calls_the_model(self):
        """Unchanged contract: CODE alone over the cap is still an honest skip, visible in
        Gitea, with no model call. Only the reachable path changed — the diff is now
        partitioned first, and this file survives every exclusion tier."""
        head = "a" * 40
        raw = _sec("big.py", b"+" + b"x" * 200 + b"\n")
        self._fake_api(raw)
        state, rid, note = self.m.review_job(1, "o/r", 5, head)
        self.assertEqual(state, "done")
        self.assertEqual(rid, 77)
        self.assertIn("skipped", note)
        self.assertEqual(self.llm_calls, [])
        self.assertEqual(len(self.posted), 1)
        path, body = self.posted[0]
        self.assertEqual(path, "/repos/o/r/pulls/5/reviews")
        self.assertEqual(body["event"], "COMMENT")
        self.assertEqual(body["comments"], [])
        self.assertIn(f"{len(raw):,} bytes", body["body"])
        self.assertIn("over the size cap", body["body"])
        self.assertIn(f"<!-- review-bot:v1 persona=test head={head} verdict=skipped -->",
                      body["body"])
        # The marker the skip carries is credible from the persona's own account and
        # reads back as a non-clean verdict, which is what keeps the automerge lane shut.
        rv = {"body": body["body"], "user": {"login": "reviewer-test"}}
        self.assertEqual(self.m.marker_of(rv).group(3), "skipped")

    def test_a_non_utf8_file_no_longer_costs_the_whole_review(self):
        """platform#1074 diffed a PDF corpus as text and a strict decode killed EVERY attempt
        for 22h. Tolerant decoding fixed the crash but fed the model replacement characters for
        the whole PR; now the unreadable FILE is excluded by name and the rest is still
        reviewed. The cap is still measured on wire bytes, never on a decoded length."""
        head = "b" * 40
        m = load(self.tmp.name, max_diff_bytes=100000)
        self.setUp_module(m)
        self._fake_api(_sec("weird.py", b"+caf\xf6\n") + _sec("good.py", b"+ok\n"))
        state, rid, note = m.review_job(2, "o/r", 6, head)
        self.assertEqual(state, "done")
        self.assertEqual(len(self.llm_calls), 1, "the readable file must still be reviewed")
        sent = self.llm_calls[0][2]
        self.assertIn("good.py", sent)
        self.assertNotIn("weird.py", sent, "the unreadable file is not in the prompt diff")
        body = self.posted[0][1]["body"]
        self.assertIn("weird.py", body, "but it IS named in the posted coverage table")
        self.assertIn("non-utf8", body)
        # An exclusion triggered by a byte the AUTHOR controls inside an otherwise reviewable
        # file caps the verdict: the merge gate is a clean-only allowlist, so a PR can no
        # longer quarantine its own payload with one 0xf6 and merge it unread. Policy
        # exclusions (globs, binary extensions, git's own marker) still do not downgrade.
        self.assertIn(f"head={head} verdict=partial", body)
        self.assertNotIn("verdict=clean", body)

    def setUp_module(self, m):
        """Re-point the stubs at a freshly loaded module (a second `load` in one test)."""
        m.pr_ok = lambda repo, pr, head: {"head": {"sha": head}, "user": {"login": "someone"},
                                          "title": "t", "body": ""}
        m.existing_marker = lambda repo, pr, head: None
        m.maybe_merge = lambda repo, pr: None
        m.convergence_context = lambda *a, **k: ""
        m.review_round = lambda repo, pr: 1

        def run_llm(*a, **k):
            self.llm_calls.append(a)
            return {"findings": [], "summary": "looks fine"}
        m.run_llm = run_llm
        self.m = m

    def test_skipped_verdict_blocks_automerge(self):
        head = "c" * 40
        m = load(self.tmp.name, automerge=True, merge_authors=["someone"], merge_personas=["test"])
        skip = m.skip_body(head, 123456)
        calls = []

        def api(path, method="GET", body=None, raw=False):
            calls.append((method, path))
            if path == "/repos/o/r/pulls/7":
                return {"state": "open", "draft": False, "mergeable": True,
                        "user": {"login": "someone"}, "labels": [], "head": {"sha": head}}
            if path.startswith("/repos/o/r/pulls/7/reviews"):
                return [{"id": 1, "body": skip, "user": {"login": "reviewer-test"}}]
            if path.endswith("/status"):
                return {"state": "success"}
            return {}
        m.api = api
        m.maybe_merge("o/r", 7)
        self.assertFalse(any(meth == "POST" for meth, _ in calls),
                         f"a skipped verdict must never merge: {calls}")

    def test_policy_exclusions_still_do_not_downgrade(self):
        """The other half of the contract: a lockfile or a .png must not cost the verdict, or
        ~7% of merges stall for files no reviewer can vouch for either way."""
        m = load(self.tmp.name, max_diff_bytes=100000)
        raw = _sec("uv.lock") + _sec("img/logo.png") + _sec("good.py", b"+ok\n")
        diff, dropped, coverage = m.plan_coverage(raw)
        self.assertEqual("full", coverage)
        self.assertEqual({"generated", "binary"}, {r for _, r, _ in dropped})
        self.assertIn("good.py", diff)

    def test_git_binary_marker_on_a_reviewable_path_caps_the_verdict(self):
        """codex cross-review, round 4: one NUL byte in payload.sh makes git emit its own
        binary marker for it, so an authentic marker is still an author-controlled trigger."""
        m = load(self.tmp.name, max_diff_bytes=100000)
        raw = (_sec("payload.sh", b"\nBinary files /dev/null and b/payload.sh differ\n")
               + _sec("good.py", b"+ok\n"))
        diff, dropped, coverage = m.plan_coverage(raw)
        self.assertEqual("partial", coverage, "an unreviewed executable must not ride a clean")
        self.assertEqual([("payload.sh", "binary content", len(_sec(
            "payload.sh", b"\nBinary files /dev/null and b/payload.sh differ\n")))], dropped)
        self.assertIn("good.py", diff)

    def test_author_triggered_exclusion_blocks_automerge(self):
        """The end-to-end property the cap exists for: a head whose only non-clean signal is
        an author-triggered exclusion must not merge."""
        head = "d" * 40
        m = load(self.tmp.name, automerge=True, merge_authors=["someone"], merge_personas=["test"])
        marker = f"<!-- review-bot:v1 persona=test head={head} verdict=partial -->"
        calls = []

        def api(path, method="GET", body=None, raw=False):
            calls.append((method, path))
            if path == "/repos/o/r/pulls/7":
                return {"state": "open", "draft": False, "mergeable": True,
                        "user": {"login": "someone"}, "labels": [], "head": {"sha": head}}
            if path.startswith("/repos/o/r/pulls/7/reviews"):
                return [{"id": 1, "body": f"partial\n\n{marker}",
                         "user": {"login": "reviewer-test"}}]
            if path.endswith("/status"):
                return {"state": "success"}
            return {}
        m.api = api
        m.maybe_merge("o/r", 7)
        self.assertFalse(any(meth == "POST" for meth, _ in calls),
                         f"a partial verdict must never merge: {calls}")

    def test_api_raw_returns_bytes_without_decoding(self):
        m = self.m

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"\xff\xfe not utf-8"
        # `m.urllib` IS the process-wide urllib package: patch it scoped, never assign.
        with mock.patch.object(m.urllib.request, "urlopen", lambda req, timeout=60: Resp()):
            self.assertEqual(m.api("/x", raw=True), b"\xff\xfe not utf-8")



class _StopLoop(Exception):
    """Breaks reconciler()'s `while True` after exactly one pass."""


def _pr(number, sha="a" * 40, login="human", draft=False):
    return {"number": number, "draft": draft, "user": {"login": login},
            "head": {"sha": sha}}


class _FaultyConn:
    """A real connection that fails at a chosen point INSIDE commit_sweep().

    The atomicity tests are worthless without this: faulting before commit_sweep() runs proves
    only that an unreached function writes nothing, which is true of a broken implementation too.
    Verified — with the faults injected here, flipping db() to isolation_level=None (autocommit)
    turns these tests RED, while the pre-commit faults alone leave them green."""

    def __init__(self, real, fail_sql=None, fail_on_commit=False):
        self._real, self._fail_sql, self._fail_commit = real, fail_sql, fail_on_commit

    def execute(self, sql, *a):
        if self._fail_sql is not None and self._fail_sql(sql, a[0] if a else ()):
            raise sqlite3.OperationalError("injected write failure")
        return self._real.execute(sql, *a)

    def commit(self):
        if self._fail_commit:
            raise sqlite3.OperationalError("injected commit failure")
        return self._real.commit()

    def close(self):
        return self._real.close()


def _sweep(m, failing=(), marker_raises=(), cleanup_raises=False, enqueue_exc=None,
           enqueue_exc_repo=None, fail_sql=None, fail_on_commit=False):
    """Run ONE reconciler() pass. `failing` repos raise on their pulls listing; repos in
    `marker_raises` instead fail later in the body (existing_marker), which is the case a
    guard around the listing call ALONE would miss."""
    def api(path, *a, **kw):
        repo = path.split("/repos/", 1)[1].split("/pulls", 1)[0]
        if repo in failing:
            raise RuntimeError(f"HTTP Error 404: {repo}")
        return [_pr(1)]

    def existing_marker(repo, pr, sha):
        if repo in marker_raises:
            raise RuntimeError("marker read blew up")
        return False

    def enqueue(repo, pr, sha, source):
        if enqueue_exc is not None and (enqueue_exc_repo is None or repo == enqueue_exc_repo):
            raise enqueue_exc
        calls.append(repo)

    def cleanup():
        if cleanup_raises:
            raise RuntimeError("quarantine sweep failed")

    def sleep(secs):
        if secs == m.CFG["reconcile_s"]:
            raise _StopLoop

    real_db = m.db

    def db():
        c = real_db()
        if fail_sql is None and not fail_on_commit:
            return c
        return _FaultyConn(c, fail_sql, fail_on_commit)

    calls = []
    with mock.patch.object(m, "api", api), \
            mock.patch.object(m, "existing_marker", existing_marker), \
            mock.patch.object(m, "enqueue", enqueue), \
            mock.patch.object(m, "maybe_merge", lambda *a, **kw: None), \
            mock.patch.object(m, "retire_closed_quarantines", cleanup), \
            mock.patch.object(m, "db", db), \
            mock.patch("time.sleep", sleep):
        try:
            m.reconciler()
        except _StopLoop:
            pass
    return calls


def _meta(m):
    # m.db() rather than a bare connect: it creates the schema idempotently, so this works even
    # on a database no code path has touched yet. Closed in `finally` because an open handle
    # blocks TemporaryDirectory cleanup on Windows.
    c = m.db()
    try:
        return dict(c.execute("SELECT k,v FROM meta"))
    finally:
        c.close()


def _jobs(m, repo):
    c = m.db()
    try:
        return c.execute("SELECT COUNT(*) FROM jobs WHERE repo=?", (repo,)).fetchone()[0]
    finally:
        c.close()


def _exported(m):
    """write_metrics() output as {metric_line_key: value}, keyed by the full labelled name."""
    m.write_metrics()
    out = {}
    for line in pathlib.Path(m.CFG["textfile"]).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        name, _, val = line.rpartition(" ")
        out[name] = float(val)
    return out


class ReconcileIsolationTest(unittest.TestCase):
    """The sweep must survive one unreachable repo.

    THE INCIDENT SHAPE THIS PINS: reconciler() used to wrap the whole `for repo` loop in ONE
    try/except, and api() is a bare urlopen that raises HTTPError on 404. So deleting a repo
    that was still in the allowlist aborted the ENTIRE sweep every cycle - every repo after it
    was never polled, retire_closed_quarantines() never ran, and last_reconcile never advanced.
    Nothing alerted on any of that (2026-09-10 audit)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repos = ["o/first", "o/middle", "o/last"]
        self.m = load(self.tmp.name, repos=self.repos)

    def test_a_failing_first_repo_does_not_stop_the_rest(self):
        swept = _sweep(self.m, failing={"o/first"})
        self.assertEqual(swept, ["o/middle", "o/last"])
        meta = _meta(self.m)
        self.assertEqual(meta[self.m.REPO_FAILED_PREFIX + "o/first"], "1")
        self.assertEqual(meta[self.m.REPO_FAILED_PREFIX + "o/middle"], "0")
        self.assertEqual(meta[self.m.REPO_FAILED_PREFIX + "o/last"], "0")
        self.assertIn("last_reconcile", meta)

    def test_a_failing_middle_repo_does_not_stop_the_rest(self):
        swept = _sweep(self.m, failing={"o/middle"})
        self.assertEqual(swept, ["o/first", "o/last"])
        self.assertEqual(_meta(self.m)[self.m.REPO_FAILED_PREFIX + "o/middle"], "1")

    def test_a_malformed_pr_does_not_blame_the_previous_one(self):
        # A tuple assignment evaluates its RHS before binding either name, so the first cut left
        # the PRIOR pr's number in at_pr and logged `o/first#17 [enqueue]` for a failure that
        # happened while parsing the element AFTER #17.
        def api(path, *a, **kw):
            repo = path.split("/repos/", 1)[1].split("/pulls", 1)[0]
            return [_pr(17), "malformed"] if repo == "o/first" else [_pr(1)]

        logged = []
        with mock.patch.object(self.m, "api", api), \
                mock.patch.object(self.m, "existing_marker", lambda *a: False), \
                mock.patch.object(self.m, "enqueue", lambda *a: None), \
                mock.patch.object(self.m, "maybe_merge", lambda *a, **kw: None), \
                mock.patch.object(self.m, "retire_closed_quarantines", lambda: None), \
                mock.patch.object(self.m, "log", lambda *a: logged.append(" ".join(map(str, a)))), \
                mock.patch("time.sleep", side_effect=_StopLoop):
            try:
                self.m.reconciler()
            except _StopLoop:
                pass
        line = [l for l in logged if "reconcile o/first" in l]
        self.assertTrue(line, f"no reconcile log line: {logged}")
        self.assertIn("[parse]", line[0])
        self.assertNotIn("#17", line[0])

    def test_a_marker_failure_names_the_pr_and_the_operation(self):
        _sweep(self.m, marker_raises={"o/first"})
        # Proven via the gauge; the log shape itself is asserted in the test above.
        self.assertEqual(_meta(self.m)[self.m.REPO_FAILED_PREFIX + "o/first"], "1")

    def test_a_failure_AFTER_the_listing_call_is_also_repo_scoped(self):
        # Guarding only api() would leave this failing exactly as before: existing_marker()
        # runs inside the per-PR body, past the listing.
        swept = _sweep(self.m, marker_raises={"o/first"})
        self.assertEqual(swept, ["o/middle", "o/last"])
        self.assertEqual(_meta(self.m)[self.m.REPO_FAILED_PREFIX + "o/first"], "1")

    def test_every_repo_failing_still_records_a_completed_sweep(self):
        _sweep(self.m, failing=set(self.repos))
        meta = _meta(self.m)
        self.assertIn("last_reconcile", meta)
        for r in self.repos:
            self.assertEqual(meta[self.m.REPO_FAILED_PREFIX + r], "1")

    def test_recovery_clears_the_failure_flag(self):
        _sweep(self.m, failing={"o/first"})
        self.assertEqual(_meta(self.m)[self.m.REPO_FAILED_PREFIX + "o/first"], "1")
        _sweep(self.m)
        self.assertEqual(_meta(self.m)[self.m.REPO_FAILED_PREFIX + "o/first"], "0")


class ReconcileAtomicityTest(unittest.TestCase):
    """A cycle publishes all-or-nothing.

    Writing each repo's result as it is produced exports a HALF-FINISHED sweep: a recovered
    repo's gauge drops to 0 while last_reconcile still names the older, completed cycle - an
    operator watches an alert clear with no sweep behind it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name, repos=["o/a", "o/b"])
        # A completed snapshot to protect: o/a failed, and a known completion time.
        _sweep(self.m, failing={"o/a"})
        self.before = _meta(self.m)

    def test_a_sqlite_failure_is_fatal_to_the_cycle_and_changes_nothing(self):
        # o/a would have recovered this cycle; the state store dies partway. The previous
        # snapshot must survive INTACT rather than publishing a/0 under the old timestamp.
        _sweep(self.m, enqueue_exc=sqlite3.OperationalError("database is locked"))
        self.assertEqual(_meta(self.m), self.before)

    def test_a_cleanup_failure_is_fatal_to_the_cycle_and_changes_nothing(self):
        _sweep(self.m, cleanup_raises=True)
        self.assertEqual(_meta(self.m), self.before)

    def test_a_sqlite_failure_is_not_filed_as_a_repo_failure(self):
        # The specific mis-classification: a dead database is not "one repo is sad".
        _sweep(self.m, enqueue_exc=sqlite3.OperationalError("database is locked"))
        self.assertEqual(_meta(self.m)[self.m.REPO_FAILED_PREFIX + "o/a"], "1")  # unchanged
        self.assertEqual(_meta(self.m)["last_reconcile"], self.before["last_reconcile"])

    def test_a_later_repo_failing_after_an_earlier_one_recovered_publishes_neither(self):
        # o/a recovers, o/b then kills the cycle on the state store. Publishing per repo would
        # export o/a=0 under the OLD timestamp; the whole cycle must be discarded instead.
        _sweep(self.m, enqueue_exc=sqlite3.OperationalError("database is locked"),
               enqueue_exc_repo="o/b")
        self.assertEqual(_meta(self.m), self.before)

    # --- faults INSIDE commit_sweep(): the three that actually pin the transaction ------------
    # Without these the suite passes even with db() flipped to autocommit (verified).

    def test_a_failure_on_a_later_gauge_write_rolls_back_the_earlier_ones(self):
        # o/a's recovery (1 -> 0) is written first, then o/b's write dies. If the earlier INSERT
        # were already durable, o/a would read 0 with the old timestamp: an alert clearing with
        # no completed sweep behind it.
        def fail_second_gauge(sql, params):
            return ("INSERT OR REPLACE INTO meta" in sql
                    and params and str(params[0]).endswith("o/b"))
        _sweep(self.m, fail_sql=fail_second_gauge)
        self.assertEqual(_meta(self.m), self.before)

    def test_a_failure_on_the_timestamp_write_rolls_back_every_gauge(self):
        # 'last_reconcile' is INLINE in that statement's SQL, not a bound parameter — matching on
        # params[0] silently never fires and the test passes while injecting nothing.
        def fail_timestamp(sql, params):
            return "INSERT OR REPLACE INTO meta VALUES('last_reconcile'" in sql
        _sweep(self.m, fail_sql=fail_timestamp)
        self.assertEqual(_meta(self.m), self.before)

    def test_a_failure_at_commit_leaves_the_previous_snapshot_intact(self):
        _sweep(self.m, fail_on_commit=True)
        self.assertEqual(_meta(self.m), self.before)


class ReconcileMetricsTest(unittest.TestCase):
    """The gauge has to actually reach the textfile.

    write_metrics() builds its `gauges` dict from an explicit `WHERE k IN (...)` whitelist, so a
    metric added only to the render list exports 0 forever. These pin the separate prefix read."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name, repos=["o/a", "o/b"])

    def test_exports_one_labelled_series_per_configured_repo(self):
        _sweep(self.m, failing={"o/a"})
        ex = _exported(self.m)
        self.assertEqual(ex['reviewbot_reconcile_repo_failed{persona="test",repo="o/a"}'], 1.0)
        self.assertEqual(ex['reviewbot_reconcile_repo_failed{persona="test",repo="o/b"}'], 0.0)
        self.assertIn('reviewbot_last_reconcile_timestamp_seconds{persona="test"}', ex)

    def test_a_repo_dropped_from_the_allowlist_stops_being_exported(self):
        # The retirement case: the row lingers in `meta`, but a de-configured repo must not keep
        # exporting its last value - that would alert forever on a repo removed on purpose.
        _sweep(self.m, failing={"o/a"})
        m2 = load(self.tmp.name, repos=["o/b"])
        ex = _exported(m2)
        self.assertNotIn('reviewbot_reconcile_repo_failed{persona="test",repo="o/a"}', ex)
        self.assertEqual(ex['reviewbot_reconcile_repo_failed{persona="test",repo="o/b"}'], 0.0)

    def test_results_survive_a_restart(self):
        # meta is on disk, so a redeploy restart must not blank the failure state.
        _sweep(self.m, failing={"o/a"})
        m2 = load(self.tmp.name, repos=["o/a", "o/b"])
        ex = _exported(m2)
        self.assertEqual(ex['reviewbot_reconcile_repo_failed{persona="test",repo="o/a"}'], 1.0)

    def test_a_repo_never_swept_is_omitted_rather_than_reported_clean(self):
        # Fresh database, no sweep yet: absent is honest, 0 would be a lie that reads "clean".
        ex = _exported(self.m)
        self.assertNotIn('reviewbot_reconcile_repo_failed{persona="test",repo="o/a"}', ex)

    def test_persona_is_escaped_on_every_metric_not_just_the_new_one(self):
        # The first fix escaped `repo` and the persona on the NEW series only; the other eleven
        # emissions still interpolated persona raw, so one quote there corrupted the textfile
        # just as effectively.
        m = load(self.tmp.name, persona='te"st', repos=["o/a"])
        _sweep(m)
        m.write_metrics()
        text = pathlib.Path(m.CFG["textfile"]).read_text(encoding="utf-8")
        self.assertNotIn('persona="te"st"', text)
        self.assertIn(r'persona="te\"st"', text)
        # Heartbeat is the first line built and does not go through the new code path at all.
        self.assertTrue(any(ln.startswith("reviewbot_heartbeat_timestamp_seconds")
                            and r'persona="te\"st"' in ln for ln in text.splitlines()))

    def test_every_exported_line_parses_as_one_metric_sample(self):
        # A label-body regex of `.*` accepts the malformed output it is supposed to catch, so
        # this walks the labels properly: quotes may appear only escaped.
        m = load(self.tmp.name, persona='p"q\\r', repos=['o/a"b', "o/c\\d"])
        _sweep(m)
        m.write_metrics()
        for ln in pathlib.Path(m.CFG["textfile"]).read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            name, _, rest = ln.partition("{")
            self.assertRegex(name.strip(), r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
            if not rest:
                continue
            labels, _, value = rest.rpartition("}")
            self.assertRegex(value.strip(), r"^-?[0-9.eE+-]+$")
            # Exposition format proper: a comma-separated run of name="value", where value may
            # contain a quote/backslash/newline ONLY as an escape pair. A label body of `.*`
            # accepts exactly the corruption this is meant to catch.
            self.assertRegex(
                labels,
                r'^[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\\n]|\\.)*"'
                r'(?:,[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\\n]|\\.)*")*$',
                f"malformed label block: {labels!r}")

    def test_a_repo_name_with_a_quote_does_not_corrupt_the_textfile(self):
        # repo is the first FREE-FORM label value this exporter emits. node_exporter rejects the
        # whole textfile on one malformed line, so an unescaped `"` would delete every reviewbot
        # metric on the host — not merely this series.
        odd = 'o/we"ird\\slash'
        m = load(self.tmp.name, repos=[odd])
        _sweep(m)
        m.write_metrics()
        text = pathlib.Path(m.CFG["textfile"]).read_text(encoding="utf-8")
        line = [ln for ln in text.splitlines() if "reconcile_repo_failed" in ln][0]
        self.assertIn(r'repo="o/we\"ird\\slash"', line)
        # Every emitted line must still be one metric with exactly one value.
        for ln in text.splitlines():
            if ln.strip():
                self.assertRegex(ln, r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{.*\})? -?[0-9.eE+]+$")


class AllowlistDefaultsTest(unittest.TestCase):
    """Guards the SHIPPED allowlist, not a synthetic one.

    The behavioural enqueue tests use repos=["o/kept"], so restoring cchifor/review-bot-fixture to
    the role defaults would leave them green. This is the test that would actually go red."""

    EXPECTED = ["cchifor/ailab", "cchifor/agentforge", "cchifor/platform",
                "cchifor/agentforge-platform"]

    @staticmethod
    def _parse_repos(text):
        """Read the whole pr_reviewer_repos sequence, stdlib only, FAILING CLOSED.

        Not PyYAML: the "Script unit tests" CI step installs no dependencies and PyYAML is NOT on
        that runner (see test_cp_env's header), so a module-level `import yaml` would take every
        test in this file down in the one place this guard has to run.

        Not a single regex either. `^pr_reviewer_repos:\\n((?:\\s+-\\s+\\S+\\n)+)` stops at the
        first line it cannot match, so appending a comment and then the fixture underneath the
        four real entries left it GREEN — the truncation IS the bypass. This walks to the end of
        the block instead, and raises on anything it does not understand rather than returning a
        short list."""
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if ln.startswith("pr_reviewer_repos:"):
                rest = ln.split(":", 1)[1].strip()
                if rest:  # flow style, or a value on the key line
                    raise AssertionError(f"unhandled flow-style allowlist: {ln!r}")
                start = i + 1
                break
        else:
            raise AssertionError("pr_reviewer_repos: not found")

        repos = []
        for ln in lines[start:]:
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue                      # blank / comment INSIDE the block: skip, don't stop
            if not ln[:1].isspace():
                break                         # dedent to column 0 = next top-level key
            body = ln.strip()
            if not body.startswith("- "):
                raise AssertionError(f"unparsable line in allowlist block: {ln!r}")
            item = body[2:].split(" #", 1)[0].strip()      # drop an inline comment
            if item[:1] in ("'", '"'):                      # tolerate quoting
                item = item[1:-1] if item[-1:] == item[:1] else item.strip("'\"")
            repos.append(item)
        return repos

    def test_the_retired_fixture_is_not_in_the_role_defaults(self):
        text = (ROOT / "ansible" / "roles" / "pr_reviewer" / "defaults" / "main.yml").read_text(
            encoding="utf-8")
        repos = self._parse_repos(text)
        self.assertNotIn("cchifor/review-bot-fixture", repos)
        self.assertEqual(repos, self.EXPECTED)

    def test_the_parser_catches_a_fixture_smuggled_in_past_a_comment(self):
        # The exact bypasses that made the first version of this test useless.
        base = ("pr_reviewer_repos:\n" + "".join(f"  - {r}\n" for r in self.EXPECTED))
        for evil in (base + "  # preserved smoke test\n  - cchifor/review-bot-fixture\n",
                     base + "  - cchifor/review-bot-fixture # smoke test\n",
                     base.replace("  - cchifor/ailab\n", '  - "cchifor/ailab"\n')
                     + "  - cchifor/review-bot-fixture\n"):
            with self.subTest(evil=evil.splitlines()[-1]):
                self.assertIn("cchifor/review-bot-fixture", self._parse_repos(evil))

    def test_the_parser_is_not_confused_by_quoting_or_trailing_keys(self):
        text = ('pr_reviewer_repos:\n  - "cchifor/ailab"\n'
                "  - 'cchifor/platform'  # inline\n\nnext_key: 1\n  - not-a-repo\n")
        self.assertEqual(self._parse_repos(text), ["cchifor/ailab", "cchifor/platform"])

    def test_the_parser_fails_closed_on_a_shape_it_cannot_read(self):
        with self.assertRaises(AssertionError):
            self._parse_repos("pr_reviewer_repos: [cchifor/ailab]\n")
        with self.assertRaises(AssertionError):
            self._parse_repos("some_other_key: 1\n")


class ReconcileAdmissionTest(unittest.TestCase):
    """CFG["repos"] has TWO consumers - the sweep and the webhook admission gate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name, repos=["o/kept"])

    def test_enqueue_drops_a_repo_outside_the_allowlist(self):
        self.m.enqueue("o/retired", 1, "b" * 40, "webhook")
        self.assertEqual(_jobs(self.m, "o/retired"), 0)

    def test_enqueue_accepts_an_allowlisted_repo(self):
        self.m.enqueue("o/kept", 1, "b" * 40, "webhook")
        self.assertEqual(_jobs(self.m, "o/kept"), 1)


def _held_sweep(m, prs, failing=(), verdict_blocked=()):
    """One reconciler() pass where every PR already HAS this persona's marker, so the body
    reaches maybe_merge() instead of enqueue(). `verdict_blocked` are the PR numbers whose
    maybe_merge reports the verdict gate."""
    def api(path, *a, **kw):
        repo = path.split("/repos/", 1)[1].split("/pulls", 1)[0]
        if repo in failing:
            raise RuntimeError("HTTP Error 404: " + repo)
        return [_pr(n) for n in prs.get(repo, ())]

    def maybe_merge(repo, pr):
        return "verdicts" if pr in verdict_blocked else None

    def sleep(secs):
        if secs == m.CFG["reconcile_s"]:
            raise _StopLoop

    with mock.patch.object(m, "api", api), \
            mock.patch.object(m, "existing_marker", lambda *a: True), \
            mock.patch.object(m, "maybe_merge", maybe_merge), \
            mock.patch.object(m, "retire_closed_quarantines", lambda: None), \
            mock.patch("time.sleep", sleep):
        try:
            m.reconciler()
        except _StopLoop:
            pass


class MergeBlockedVisibilityTest(unittest.TestCase):
    """A PR held by the verdict gate must be visible in the log AND in a series.

    THE INCIDENT SHAPE THIS PINS (2026-09-10, ailab#616 + #619): maybe_merge()'s verdict gate
    returned with no log() and no metric, while the branch-protection path right below it has
    always logged its 405. So a PR that was merge-ready except for one persona's verdict was
    indistinguishable from a PR nobody had looked at - nothing to grep, nothing to alert on.
    Both sat (12 h and 8 h) with the peer persona approved and CI green while 19 sibling
    renovate PRs merged around them, and were found only by reading the PR list by hand.

    The convergence ladder cannot rescue these: it relaxes from round 3, review_round() counts
    distinct fully-reviewed HEADS, and a renovate PR keeps one head for life."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.head = "e" * 40

    def _pr_api(self, m, verdicts, labels=(), author="renovate-bot", ci="success"):
        marks = ["ok\n\n<!-- review-bot:v1 persona=%s head=%s verdict=%s -->" % (p, self.head, v)
                 for p, v in verdicts.items()]

        def api(path, method="GET", body=None, raw=False):
            if path == "/repos/o/r/pulls/7":
                return {"state": "open", "draft": False, "mergeable": True,
                        "user": {"login": author},
                        "labels": [{"name": n} for n in labels],
                        "head": {"sha": self.head}}
            if path.startswith("/repos/o/r/pulls/7/reviews"):
                return [{"id": i, "body": b, "user": {"login": "reviewer-" + p}}
                        for i, (p, b) in enumerate(zip(verdicts, marks))]
            if path.endswith("/status"):
                return {"state": ci}
            self.fail("unexpected call: %s %s" % (method, path))
        m.api = api
        return api

    def test_a_pr_whose_CI_is_not_green_is_not_a_verdict_block(self):
        """reviewer-codex, round 1: the verdict check used to run BEFORE the status check, so
        a PR with red or pending CI *and* a non-clean verdict landed in the gauge - and
        ReviewbotMergeBlocked would then page with a remedy ("fix the finding, or merge over
        it") that is not the blocker. Both are bare early returns, so the order cannot change
        what merges; it decides only what a held PR is reported as. "verdicts" has to mean the
        verdict gate is the SOLE remaining blocker, or the alert's whole premise is wrong."""
        for ci in ("pending", "failure", "error"):
            with self.subTest(ci=ci):
                m = load(self.tmp.name, automerge=True, merge_authors=["renovate-bot"],
                         merge_personas=["claude"], persona="claude")
                self._pr_api(m, {"claude": "findings"}, ci=ci)
                self.assertIsNone(m.maybe_merge("o/r", 7))

    def test_the_verdict_block_still_reports_when_CI_IS_green(self):
        """The other half - moving the status check earlier must not silence the real case."""
        m = load(self.tmp.name, automerge=True, merge_authors=["renovate-bot"],
                 merge_personas=["claude"], persona="claude")
        self._pr_api(m, {"claude": "findings"}, ci="success")
        self.assertEqual("verdicts", m.maybe_merge("o/r", 7))

    def test_a_held_pr_names_the_persona_that_is_short(self):
        m = load(self.tmp.name, automerge=True, merge_authors=["renovate-bot"],
                 merge_personas=["claude", "codex"], persona="claude")
        self._pr_api(m, {"claude": "findings", "codex": "clean"})
        logged = []
        with mock.patch.object(m, "log", lambda *a: logged.append(" ".join(map(str, a)))):
            self.assertEqual("verdicts", m.maybe_merge("o/r", 7))
        line = " ".join(logged)
        self.assertIn("o/r#7", line)
        self.assertIn("claude=findings", line)
        self.assertNotIn("codex", line, "only the personas actually short belong in the line")

    def test_a_persona_that_never_reviewed_is_named_too(self):
        m = load(self.tmp.name, automerge=True, merge_authors=["renovate-bot"],
                 merge_personas=["claude", "codex"], persona="claude")
        self._pr_api(m, {"claude": "clean"})
        logged = []
        with mock.patch.object(m, "log", lambda *a: logged.append(" ".join(map(str, a)))):
            self.assertEqual("verdicts", m.maybe_merge("o/r", 7))
        self.assertIn("codex=no review", " ".join(logged))

    def test_a_pr_held_for_a_DIFFERENT_reason_is_not_reported_as_a_verdict_block(self):
        """no-automerge is a deliberate human brake, not a stall - counting it would make the
        gauge fire on exactly the PRs somebody has already taken responsibility for."""
        m = load(self.tmp.name, automerge=True, merge_authors=["renovate-bot"],
                 merge_personas=["claude"], persona="claude")
        self._pr_api(m, {"claude": "findings"}, labels=["no-automerge"])
        self.assertIsNone(m.maybe_merge("o/r", 7))

    def test_a_third_party_pr_is_not_reported_either(self):
        m = load(self.tmp.name, automerge=True, merge_authors=["renovate-bot"],
                 merge_personas=["claude"], persona="claude")
        self._pr_api(m, {"claude": "findings"}, author="a-stranger")
        self.assertIsNone(m.maybe_merge("o/r", 7))

    def test_the_sweep_publishes_the_count_and_the_age(self):
        m = load(self.tmp.name, repos=["o/r"])
        c = m.db()
        try:
            c.execute("INSERT INTO jobs(repo,pr,head_sha,state,created,updated) "
                      "VALUES('o/r',7,?,'done',?,?)",
                      (_pr(7)["head"]["sha"], real_time.time() - 7200,
                       real_time.time() - 7200))
            c.commit()
        finally:
            c.close()
        _held_sweep(m, {"o/r": [7]}, verdict_blocked={7})
        meta = _meta(m)
        self.assertEqual("1", meta["merge_blocked_prs"])
        self.assertGreater(float(meta["merge_blocked_seconds"]), 7000,
                           "age must run from the review that landed on the current head")

    def test_the_gauge_falls_when_the_block_clears(self):
        """NON-LATCHING is the whole design. A cumulative counter would keep firing forever
        after a single stuck PR merged - the trap reviewbot_quarantined_recent_jobs already
        exists to dodge."""
        m = load(self.tmp.name, repos=["o/r"])
        _held_sweep(m, {"o/r": [7]}, verdict_blocked={7})
        self.assertEqual("1", _meta(m)["merge_blocked_prs"])
        _held_sweep(m, {"o/r": [7]})          # same PR, now clean
        self.assertEqual("0", _meta(m)["merge_blocked_prs"])
        self.assertEqual("0", _meta(m)["merge_blocked_seconds"])

    def test_a_pr_that_vanishes_clears_the_gauge_with_no_reaper(self):
        m = load(self.tmp.name, repos=["o/r"])
        _held_sweep(m, {"o/r": [7]}, verdict_blocked={7})
        self.assertEqual("1", _meta(m)["merge_blocked_prs"])
        _held_sweep(m, {"o/r": []})           # merged or closed
        self.assertEqual("0", _meta(m)["merge_blocked_prs"])

    def test_a_failed_repo_does_not_publish_a_partial_count(self):
        """A repo whose sweep died was only partly enumerated. Publishing what it managed to
        see shrinks the gauge on exactly the cycles that went wrong, which reads as recovery."""
        m = load(self.tmp.name, repos=["o/good", "o/bad"])
        _held_sweep(m, {"o/good": [1], "o/bad": [2]}, verdict_blocked={1, 2})
        self.assertEqual("2", _meta(m)["merge_blocked_prs"])
        _held_sweep(m, {"o/good": [1], "o/bad": [2]}, failing={"o/bad"},
                    verdict_blocked={1, 2})
        meta = _meta(m)
        self.assertEqual("1", meta["merge_blocked_prs"],
                         "only the repo that completed may contribute")
        self.assertEqual("1", meta[m.REPO_FAILED_PREFIX + "o/bad"],
                         "and the undercount must be accompanied by its repo-failed series")

    def test_the_new_series_reach_the_textfile(self):
        """write_metrics() builds `gauges` from an explicit `WHERE k IN (...)` whitelist AND a
        separate render list. A key added to only one of the two exports 0 forever - the exact
        trap ReconcileMetricsTest was written for."""
        m = load(self.tmp.name, repos=["o/r"])
        _held_sweep(m, {"o/r": [7]}, verdict_blocked={7})
        m.bump_meta("llm_primary_failed_total", 3)
        m.bump_meta("llm_fallback_used_total", 2)
        exported = _exported(m)
        self.assertEqual(1.0, exported['reviewbot_merge_blocked_prs{persona="test"}'])
        self.assertIn('reviewbot_merge_blocked_seconds{persona="test"}', exported)
        self.assertEqual(3.0, exported['reviewbot_llm_primary_failed_total{persona="test"}'])
        self.assertEqual(2.0, exported['reviewbot_llm_fallback_used_total{persona="test"}'])


SEATS_3 = [{"name": "a", "sudo_user": "runa"},
           {"name": "b", "sudo_user": "runb"},
           {"name": "c", "sudo_user": "runc"}]


class SeatRotationTest(unittest.TestCase):
    """Rotating across several subscriptions instead of parking the whole worker.

    THE INVARIANT UNDER TEST is the one the 2026-09-15 episode proved and the 09-14 episode
    before it lacked: an exhausted account consumes NO attempt, leaves the queue intact and
    quarantines nothing. Rotation adds routing ABOVE that; every test here exists to show it
    did not erode it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name, llm_kind="codex", llm_model="gpt-6-astra",
                      llm_fallback_model="", llm_timeout_s=600, llm_sudo_user="",
                      llm_seats=SEATS_3)
        self.calls = []

    def _runner(self, refusing_users, elapsed=0.0):
        """codex refuses for the named users and answers for the rest. Dispatch is on the sudo
        USER at argv[3], which is the only thing that differs between seats."""
        answer = json.dumps({"summary": "s", "findings": []})

        def run(args, **kw):
            CP = self.m.subprocess.CompletedProcess
            if args[0] != "sudo":
                return CP(args, 0, "", "")
            user, verb = args[3], args[4]
            if verb == "mktemp":
                return CP(args, 0, f"/tmp/reviewbot-llm-{user}\n", "")
            if verb.startswith("HOME="):
                self.calls.append(user)
                if elapsed:
                    type(self.clock).now += elapsed
                if user in refusing_users:
                    return CP(args, 1, "", CODEX_LIMIT_STDERR)
                return CP(args, 0, "", "")
            if verb == "cat":
                if args[5].endswith("auth.json"):
                    return CP(args, 0, "{}", "")
                return CP(args, 0, "" if user in refusing_users else answer, "")
            return CP(args, 0, "", "")
        return run

    def _freeze(self):
        class Clock:
            now = 1000.0
            def monotonic(self): return Clock.now
            def time(self): return real_time.time()
            def sleep(self, _n): pass
            def strftime(self, *a): return real_time.strftime(*a)
            # gmtime is NOT optional: fail_note() calls time.gmtime(e.reset_at) for any parsable
            # reset, and parse_reset() calls it too. Without it a rotation test that freezes the
            # clock dies on AttributeError, which reads like a production bug in fail_note.
            def gmtime(self, *a): return real_time.gmtime(*a)
        self.clock = Clock()
        self.m.time = self.clock

    # 1 ---------------------------------------------------------------------------------
    def test_a_refusal_rotates_to_the_next_seat_and_consumes_no_attempt(self):
        self.m.subprocess.run = self._runner({"runa"})
        self.m.run_llm("t", "d", "diff")
        self.assertEqual(["runa", "runb"], self.calls, "should have moved a -> b")
        self.assertTrue(self.m.seat_parked("a"))
        self.assertFalse(self.m.seat_parked("b"))
        self.assertEqual("b", self.m.CURRENT_SEAT)

    def test_rotation_does_not_touch_the_attempt_budget(self):
        e = self.m.RateLimited(CODEX_LIMIT_STDERR, None)
        self.assertEqual(("retry", 4, 1), self.m.next_failure_state(e, 4, 1)[:3])

    # 2 ---------------------------------------------------------------------------------
    def test_every_seat_refusing_parks_the_worker_and_keeps_the_queue(self):
        self.m.subprocess.run = self._runner({"runa", "runb", "runc"})
        head = "a" * 40
        self.m.enqueue("o/r", 1, head, "webhook")
        self.m.review_job = lambda *a: self.m.run_llm("t", "d", "diff")
        self.m.worker_once()
        c = self.m.db()
        state, attempts = c.execute("SELECT state, attempts FROM jobs WHERE head_sha=?",
                                    (head,)).fetchone()
        c.close()
        self.assertEqual("retry", state, "must never quarantine on an account condition")
        self.assertEqual(0, attempts, "the PR must not pay for the subscription")
        self.assertEqual(0, self.m.seats_available())
        self.assertGreater(self.m.RATE_LIMITED_UNTIL, real_time.time())

    def test_one_refusal_per_seat_when_all_are_spent(self):
        self.m.subprocess.run = self._runner({"runa", "runb", "runc"})
        with self.assertRaises(self.m.RateLimited):
            self.m.run_llm("t", "d", "diff")
        self.assertEqual(["runa", "runb", "runc"], self.calls,
                         "each seat tried exactly once - `exclude=tried` bounds the loop")

    # 3 ---------------------------------------------------------------------------------
    def test_seat_deadlines_are_independent(self):
        self.m.park(real_time.time() + 5000, seat="a")
        self.assertTrue(self.m.seat_parked("a"))
        self.assertFalse(self.m.seat_parked("b"))
        self.assertEqual(2, self.m.seats_available())
        self.assertEqual(0.0, self.m.all_parked_until(), "a free seat means the worker runs")
        self.assertEqual(0.0, self.m.RATE_LIMITED_UNTIL)

    def test_the_global_takes_the_EARLIEST_reopening_not_the_latest(self):
        """A latched global would wedge the worker for MAX_PARK_S after the short seat came
        back - which is why park() assigns instead of max()ing at the global level."""
        now = real_time.time()
        self.m.park(now + 6 * 3600, seat="a")
        self.m.park(now + 6 * 3600, seat="b")
        self.m.park(now + 900, seat="c")
        self.assertAlmostEqual(self.m.SEAT_PARKED_UNTIL["c"], self.m.RATE_LIMITED_UNTIL, delta=2)
        self.assertLess(self.m.RATE_LIMITED_UNTIL - now, 1000)

    # 5 ---------------------------------------------------------------------------------
    def test_a_refusal_with_no_budget_left_defers_instead_of_rotating(self):
        self._freeze()
        self.m.subprocess.run = self._runner({"runa"}, elapsed=580)  # 600s budget
        with self.assertRaises(self.m.RateLimited) as cm:
            self.m.run_llm("t", "d", "diff")
        self.assertEqual(["runa"], self.calls, "must not start seat b on 20s of budget")
        self.assertTrue(getattr(cm.exception, "deferred_for_budget", False))
        self.assertTrue(self.m.seat_parked("a"), "the refusing seat is still parked")
        self.assertFalse(self.m.seat_parked("b"), "and the untried seat is NOT")

    def test_the_budget_defer_is_still_free(self):
        """It raises RateLimited, so next_failure_state charges nothing. A sibling exception
        class here would land in worker_once's generic handler, be promoted to
        ExpensiveFailure, and quarantine on the second occurrence."""
        self._freeze()
        self.m.subprocess.run = self._runner({"runa"}, elapsed=580)
        try:
            self.m.run_llm("t", "d", "diff")
        except self.m.RateLimited as e:
            self.assertEqual(("retry", 3, 0), self.m.next_failure_state(e, 3, 0)[:3])
            self.assertFalse(self.m.is_budget_failure(e))
        else:
            self.fail("expected RateLimited")

    # 6 ---------------------------------------------------------------------------------
    def test_selection_is_sticky_not_round_robin(self):
        self.m.subprocess.run = self._runner(set())
        for _ in range(4):
            self.m.run_llm("t", "d", "diff")
        self.assertEqual(["runa"] * 4, self.calls, "healthy seat must be reused, not rotated")

    def test_a_reopened_seat_does_not_pull_the_rotation_back(self):
        """Sticky means it STAYS moved. Drifting back to seat A each time its 900s park lapsed
        would re-walk it into the same wall every 15 minutes - round-robin by another route."""
        self.m.subprocess.run = self._runner({"runa"})
        self.m.run_llm("t", "d", "diff")            # a refuses -> now on b
        self.m.SEAT_PARKED_UNTIL["a"] = 0.0         # a's window reopens
        self.calls.clear()
        self.m.subprocess.run = self._runner(set())
        self.m.run_llm("t", "d", "diff")
        self.assertEqual(["runb"], self.calls)

    # 7 ---------------------------------------------------------------------------------
    def test_an_empty_seat_list_is_the_single_seat_path(self):
        m = load(tempfile.mkdtemp(), llm_kind="codex", llm_model="gpt-6-astra",
                 llm_fallback_model="", llm_sudo_user="")
        self.assertEqual(["default"], [s["name"] for s in m.SEATS])
        self.assertEqual("default", m.CURRENT_SEAT)
        self.assertEqual(0.0, m.all_parked_until())
        until = m.park(None)
        self.assertGreater(until, real_time.time())
        self.assertAlmostEqual(until, m.RATE_LIMITED_UNTIL, delta=1,
                               msg="one seat: the global IS that seat's deadline, as before")

    def test_the_synthetic_seat_carries_the_configured_sudo_user(self):
        m = load(tempfile.mkdtemp(), llm_kind="codex", llm_sudo_user="codexrun")
        self.assertEqual("codexrun", m.SEAT_BY_NAME["default"]["sudo_user"])

    # 8 ---------------------------------------------------------------------------------
    def test_park_state_is_deliberately_forgotten_on_restart(self):
        """In-memory ON PURPOSE. A restart re-probes one seat and re-parks it, costing a single
        refused call per spent seat, and main()'s startup recovery clears the deferred timers
        (`UPDATE jobs SET next_at=0 WHERE state='retry'`) so the queue is claimable at once.
        Persisting it would buy nothing and add a durability problem."""
        self.m.park(real_time.time() + 5000, seat="a")
        self.assertTrue(self.m.seat_parked("a"))
        fresh = load(self.tmp.name, llm_kind="codex", llm_seats=SEATS_3)
        self.assertFalse(fresh.seat_parked("a"), "a restart forgets the park")
        self.assertEqual(0.0, fresh.RATE_LIMITED_UNTIL)

    # 4 (name guard; the account_id guard needs sudo and is covered in SeatGuardTest) --
    def test_duplicate_seat_names_are_dropped_before_they_can_be_exported(self):
        m = load(tempfile.mkdtemp(), llm_kind="codex",
                 llm_seats=[{"name": "a", "sudo_user": "runa"},
                            {"name": "a", "sudo_user": "runb"},
                            {"name": "", "sudo_user": "runc"}])
        self.assertEqual(["a"], [s["name"] for s in m.SEATS],
                         "a duplicate series makes node_exporter reject the WHOLE textfile")

    def test_degraded_capacity_is_REPRESENTABLE_not_just_exported(self):
        """seats_total vs seats_distinct is the whole degradation signal, and it was dead:
        both were derived from the post-resolution list, so they were equal by construction and
        Phase 3's ReviewbotSeatsDegraded could never have fired. This fails on the old code."""
        m = load(tempfile.mkdtemp(), llm_kind="codex",
                 llm_seats=[{"name": "a", "sudo_user": "runa"},
                            {"name": "a", "sudo_user": "runb"},      # dropped: duplicate name
                            {"name": "c", "sudo_user": "runc"}])
        m.write_metrics()
        got = dict(line.split(" ", 1) for line in
                   pathlib.Path(m.CFG["textfile"]).read_text(encoding="utf-8").splitlines())
        total = float(got['reviewbot_llm_seats_total{persona="test"}'])
        distinct = float(got['reviewbot_llm_seats_distinct{persona="test"}'])
        self.assertEqual(3.0, total, "total must be what was CONFIGURED")
        self.assertEqual(2.0, distinct, "distinct must be what is USABLE")
        self.assertLess(distinct, total, "the degradation signal must be able to be true")

    def _all_probes_fail(self):
        m = load(tempfile.mkdtemp(), llm_kind="codex", llm_seats=SEATS_3)
        self.calls = []

        def run(args, **kw):
            self.calls.append(list(args))
            return m.subprocess.CompletedProcess(args, 1, "", "sudo: unknown user")
        m.subprocess.run = run
        m.resolve_seats()
        return m

    def test_when_no_seat_passes_its_probe_none_is_handed_work(self):
        """Two wrong answers here, and the first fix shipped the second of them. Dropping every
        seat idles the reviewer silently forever; keeping them unchanged leaves them selectable,
        so the worker immediately picks one already known unreachable - and a broken seat raises
        an ordinary error, never RateLimited, so it never parks and the PR quarantines."""
        m = self._all_probes_fail()
        self.assertTrue(self.calls, "the probe must actually have run")
        self.assertEqual(0, m.seats_available(), "no seat may be selectable")
        self.assertIsNone(m.active_seat(), "the selector must not offer a known-bad seat")

    def test_that_parking_is_lossless_rather_than_a_quarantine(self):
        """Parking is chosen precisely because it is the path that consumes no attempt."""
        m = self._all_probes_fail()
        e = m.RateLimited("x", None)
        self.assertEqual(("retry", 4, 1), m.next_failure_state(e, 4, 1)[:3])
        self.assertGreater(m.RATE_LIMITED_UNTIL, real_time.time(),
                           "the worker's own gate must hold it off")

    def test_the_seats_are_kept_so_the_park_can_lapse_and_re_probe(self):
        """Dropped seats could never recover; parked ones come back on their own."""
        m = self._all_probes_fail()
        self.assertEqual(["a", "b", "c"], [s["name"] for s in m.SEATS])
        for name in ("a", "b", "c"):
            m.SEAT_PARKED_UNTIL[name] = 0.0
        self.assertEqual(3, m.seats_available(), "recovery needs no restart")

    def test_seat_home_defaults_to_the_users_home_but_can_be_overridden(self):
        """A seat provisioned by hand does not necessarily live at /home/<user> — the third
        licence sat at /home/c4/.codex-seat3 before it had a user. With the path hard-coded
        such a seat is not merely awkward, it is UNEXPRESSIBLE, and the credential scan then
        fails at a path that does not exist, which fails open."""
        m = load(tempfile.mkdtemp(), llm_kind="codex",
                 llm_seats=[{"name": "a", "sudo_user": "runa"},
                            {"name": "b", "sudo_user": "runb", "home": "/home/c4/.codex-seat3"}])
        self.assertEqual("/home/runa", m.seat_home("a"))
        self.assertEqual("/home/c4/.codex-seat3", m.seat_home("b"))

    def test_the_credential_scan_uses_the_seats_home(self):
        m = load(tempfile.mkdtemp(), llm_kind="codex", llm_model="gpt-6-astra",
                 llm_fallback_model="",
                 llm_seats=[{"name": "a", "sudo_user": "runa", "home": "/srv/seat-a"}])
        seen = []
        answer = json.dumps({"summary": "s", "findings": []})

        def run(args, **kw):
            CP = m.subprocess.CompletedProcess
            if args[0] != "sudo":
                return CP(args, 0, "", "")
            seen.append(list(args))
            verb = args[4]
            if verb == "mktemp":
                return CP(args, 0, "/tmp/reviewbot-llm-a\n", "")
            if verb.startswith("HOME="):
                return CP(args, 0, "", "")
            if verb == "cat":
                return CP(args, 0, "{}" if args[5].endswith("auth.json") else answer, "")
            return CP(args, 0, "", "")
        m.subprocess.run = run
        m.run_llm("t", "d", "diff")
        scan = [c for c in seen if c[4] == "cat" and c[5].endswith("auth.json")]
        self.assertEqual(["/srv/seat-a/.codex/auth.json"], [c[5] for c in scan])
        home = [c for c in seen if c[4].startswith("HOME=")]
        self.assertEqual("HOME=/srv/seat-a", home[0][4], "the model run must get the same HOME")

    def test_an_unreadable_credential_file_is_COUNTED_not_silently_skipped(self):
        """The scan had no else-branch: an unreadable auth.json made it a no-op that looked
        exactly like a scan which ran and found nothing, while the output went out anyway."""
        m = load(tempfile.mkdtemp(), llm_kind="codex", llm_model="gpt-6-astra",
                 llm_fallback_model="", llm_seats=[{"name": "a", "sudo_user": "runa"}])
        answer = json.dumps({"summary": "s", "findings": []})

        def run(args, **kw):
            CP = m.subprocess.CompletedProcess
            if args[0] != "sudo":
                return CP(args, 0, "", "")
            verb = args[4]
            if verb == "mktemp":
                return CP(args, 0, "/tmp/reviewbot-llm-a\n", "")
            if verb.startswith("HOME="):
                return CP(args, 0, "", "")
            if verb == "cat":
                if args[5].endswith("auth.json"):
                    return CP(args, 1, "", "cat: No such file")   # unreadable
                return CP(args, 0, answer, "")
            return CP(args, 0, "", "")
        m.subprocess.run = run
        m.run_llm("t", "d", "diff")                      # must still succeed
        c = m.db()
        val = c.execute("SELECT v FROM meta WHERE k='llm_credscan_skipped_total'").fetchone()
        c.close()
        self.assertIsNotNone(val, "a skipped scan must leave a trace")
        self.assertEqual(1.0, float(val[0]))

    def test_the_budget_defer_note_does_not_claim_a_long_wait(self):
        """`waiting until <reset>` describes the REFUSING seat. Printing it when another seat is
        free sends an operator hunting a multi-hour window for a 60-second backoff."""
        e = self.m.RateLimited("upstream text", real_time.time() + 5000)
        e.seat = "a"
        e.deferred_for_budget = True
        note = self.m.fail_note(e)
        self.assertIn("retrying shortly", note)
        self.assertIn("[seat: a]", note)
        self.assertNotIn("waiting until", note)
        self.assertIn("no attempt consumed", note)
        self.assertIn("rate-limited", note, "the runbook greps for this")
        self.assertFalse(note.startswith("ambiguous POST"))
        self.assertLess(len(note), 320)

    def test_the_ordinary_park_note_is_unchanged(self):
        e = self.m.RateLimited("upstream text", None)
        self.assertTrue(self.m.fail_note(e).startswith(
            "subscription rate-limited, waiting until "))

    def _meta(self, key):
        c = self.m.db()
        row = c.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        c.close()
        return float(row[0]) if row else 0.0

    def test_the_seat_that_produced_the_review_is_the_one_credited(self):
        """Counting successes per seat is the ONLY way to tell a seat that is merely busy from
        one that can never serve. Sticky selection reaches a seat only when the stickier ones
        are spent, so an unusable seat emits nothing at all until the estate needs it."""
        self.m.subprocess.run = self._runner({"runa"})      # a refuses, b serves
        self.m.run_llm("t", "d", "diff")
        self.assertEqual(0.0, self._meta("seat_reviews_total.a"),
                         "a refused; it must not be credited with a review")
        self.assertEqual(1.0, self._meta("seat_reviews_total.b"))
        self.assertEqual(1.0, self._meta("seat_parks_total.a"))
        self.assertEqual(0.0, self._meta("seat_parks_total.b"))

    def test_a_seat_that_only_ever_parks_is_credited_with_nothing(self):
        """The shape of an account with no Codex entitlement: tried, parked, served nothing."""
        self.m.subprocess.run = self._runner({"runa", "runb", "runc"})
        with self.assertRaises(self.m.RateLimited):
            self.m.run_llm("t", "d", "diff")
        for name in ("a", "b", "c"):
            self.assertEqual(1.0, self._meta(f"seat_parks_total.{name}"))
            self.assertEqual(0.0, self._meta(f"seat_reviews_total.{name}"))

    def test_a_busy_seat_accrues_BOTH_counters(self):
        """The discriminating case. A busy estate parks seats constantly, so parking alone
        cannot be the alert signal - the healthy seat must show successes too."""
        self.m.subprocess.run = self._runner({"runa"})
        for _ in range(3):
            self.m.run_llm("t", "d", "diff")
        self.assertEqual(1.0, self._meta("seat_parks_total.a"), "a parked once, then rotation left it")
        self.assertEqual(3.0, self._meta("seat_reviews_total.b"), "b did the work")

    def test_both_per_seat_counters_reach_the_textfile_including_zeros(self):
        """Zero is the signal for the never-serves rule, so an omitted series would make the
        expression unable to match rather than merely quiet."""
        self.m.subprocess.run = self._runner({"runa"})
        self.m.run_llm("t", "d", "diff")
        self.m.write_metrics()
        got = dict(line.split(" ", 1) for line in
                   pathlib.Path(self.m.CFG["textfile"]).read_text(encoding="utf-8").splitlines())
        self.assertEqual(0.0, float(got['reviewbot_llm_seat_reviews_total{persona="test",seat="a"}']))
        self.assertEqual(1.0, float(got['reviewbot_llm_seat_reviews_total{persona="test",seat="b"}']))
        self.assertEqual(0.0, float(got['reviewbot_llm_seat_reviews_total{persona="test",seat="c"}']),
                         "an untried seat must still export a zero")
        self.assertEqual(1.0, float(got['reviewbot_llm_seat_parks_total{persona="test",seat="a"}']))

    def test_the_seat_series_reach_the_textfile(self):
        self.m.park(real_time.time() + 300, seat="b")
        m = self.m
        m.write_metrics()
        got = dict(line.split(" ", 1) for line in
                   pathlib.Path(m.CFG["textfile"]).read_text(encoding="utf-8").splitlines())
        self.assertEqual(3.0, float(got['reviewbot_llm_seats_total{persona="test"}']))
        self.assertEqual(3.0, float(got['reviewbot_llm_seats_distinct{persona="test"}']))
        self.assertEqual(2.0, float(got['reviewbot_llm_seats_available{persona="test"}']))
        self.assertEqual(1.0, float(got['reviewbot_llm_seat_parked{persona="test",seat="b"}']))
        # 0, not absent: Phase 3's `seat_parked == 1 for: 30m` needs zeros to RESOLVE.
        self.assertEqual(0.0, float(got['reviewbot_llm_seat_parked{persona="test",seat="a"}']))
        self.assertIn('reviewbot_llm_active_seat_info{persona="test",seat="a"}', got)
        self.assertIn('reviewbot_llm_seat_switches_total{persona="test"}', got)


class IsolatedSeatUserTest(unittest.TestCase):
    """CHARACTERISATION of the llm_sudo_user path, which had NO coverage at all.

    `llm_sudo_user` is "" in BASE_CFG and in CodexLimitParkTest, so until this class existed
    nothing exercised wrap_sudo, the isolated mktemp, the `cat` of the model's answer, the
    credential scan, or the `rm -rf` cleanup — the five places that bind a review to ONE OS
    user, and therefore the five that a multi-seat refactor can silently break. These tests
    assert the CURRENT behaviour so that a change to it has to be deliberate.

    The fake dispatches on argv POSITION, not on path strings: out_file is built with
    os.path.join, which is backslash-joined when the suite runs on Windows, so matching on a
    literal "/tmp/..." substring would silently classify every call as the same branch."""

    ANSWER = json.dumps({"summary": "s", "findings": []})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name, llm_kind="codex", llm_model="gpt-6-astra",
                      llm_fallback_model="", llm_sudo_user="codexrun")
        self.addCleanup(setattr, self.m, "RATE_LIMITED_UNTIL", 0.0)
        self.calls = []

    def _fake_run(self, auth_json='{"tokens":{"account_id":"acct-A"}}'):
        def run(args, **kw):
            self.calls.append(list(args))
            CP = self.m.subprocess.CompletedProcess
            if args[0] != "sudo":
                return CP(args, 0, "", "")
            verb = args[4]
            if verb == "mktemp":
                return CP(args, 0, "/tmp/reviewbot-llm-AAAAAA\n", "")
            if verb.startswith("HOME="):
                return CP(args, 0, "", "")          # the model run itself
            if verb == "cat":
                if args[5].endswith("auth.json"):
                    return CP(args, 0, auth_json, "")
                return CP(args, 0, self.ANSWER, "")  # the answer file
            if verb == "rm":
                return CP(args, 0, "", "")
            return CP(args, 0, "", "")
        return run

    def _sudo_calls(self, verb_pred):
        return [c for c in self.calls if c[0] == "sudo" and verb_pred(c[4])]

    def test_the_model_runs_as_the_isolated_user_with_its_own_HOME(self):
        self.m.subprocess.run = self._fake_run()
        self.m.run_llm("t", "d", "diff")
        model = self._sudo_calls(lambda v: v.startswith("HOME="))
        self.assertEqual(1, len(model))
        self.assertEqual(["sudo", "-n", "-u", "codexrun", "HOME=/home/codexrun"], model[0][:5])

    def test_the_answer_is_read_from_the_isolated_tmpdir_not_the_workdir(self):
        """The out dir is 0700 and owned by the isolated user; c4 never opens it directly."""
        self.m.subprocess.run = self._fake_run()
        self.m.run_llm("t", "d", "diff")
        answer = self._sudo_calls(lambda v: v == "cat")
        answer = [c for c in answer if not c[5].endswith("auth.json")]
        self.assertEqual(1, len(answer))
        self.assertIn("reviewbot-llm-AAAAAA", answer[0][5],
                      "the answer must come from the mktemp'd dir, not the service workdir")

    def test_the_credential_scan_reads_the_SAME_user_that_produced_the_output(self):
        """THE one that matters for rotation. Asserted on the argv, not the return value: a
        scan of the WRONG seat's auth.json still exits 0 and still finds nothing, so a
        return-value assertion passes while the protection is gone."""
        self.m.subprocess.run = self._fake_run()
        self.m.run_llm("t", "d", "diff")
        model = self._sudo_calls(lambda v: v.startswith("HOME="))[0]
        scan = [c for c in self._sudo_calls(lambda v: v == "cat") if c[5].endswith("auth.json")]
        self.assertEqual(1, len(scan))
        self.assertEqual(model[3], scan[0][3], "scan ran as a different user than the model")
        self.assertEqual("/home/codexrun/.codex/auth.json", scan[0][5])

    def test_credential_material_in_the_output_is_refused(self):
        """The scan is the reason the isolated user exists: model output is about to be
        posted publicly, so a token appearing in it must fail the review, not publish."""
        token = "x" * 40
        answer = json.dumps({"summary": "leak " + token, "findings": []})
        self.__class__.ANSWER = answer
        self.addCleanup(setattr, self.__class__, "ANSWER",
                        json.dumps({"summary": "s", "findings": []}))
        self.m.subprocess.run = self._fake_run(
            auth_json=json.dumps({"tokens": {"access_token": token}}))
        with self.assertRaises(RuntimeError) as cm:
            self.m.run_llm("t", "d", "diff")
        self.assertIn("credential material", str(cm.exception))

    def test_the_tmpdir_is_cleaned_up_as_the_isolated_user(self):
        self.m.subprocess.run = self._fake_run()
        self.m.run_llm("t", "d", "diff")
        rm = self._sudo_calls(lambda v: v == "rm")
        self.assertEqual(1, len(rm))
        self.assertEqual("codexrun", rm[0][3])
        self.assertIn("reviewbot-llm-AAAAAA", rm[0][6])


class RateLimitNoteDetailTest(unittest.TestCase):
    """The park note must carry the UPSTREAM refusal, not just our own wait.

    2026-09-15: the codex persona parked 46 times over 11h36m holding 7 PRs, and the only
    record of why was the fixed string `waiting until ~15m` — `journalctl -u reviewbot |
    grep -E 'usage limit|credits|weekly'` over the whole episode returns nothing. A
    15-minute blip and a spent weekly window render identically, so telling them apart
    needed a live probe against the CLI. The PREVIOUS episode's text survived only because
    the pre-park code billed it as an ordinary failure and stored it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)

    def test_the_upstream_text_reaches_the_note(self):
        note = self.m.fail_note(self.m.RateLimited(REAL_LIMIT_TEXT, None))
        self.assertIn("upstream:", note)
        self.assertIn("session limit", note)

    def test_the_prefix_is_unchanged_so_the_note_prefix_queries_still_match(self):
        """enqueue(), worker() and retire_closed_quarantines() match jobs.note by PREFIX
        (`NOT LIKE 'ambiguous POST%'`) and --requeue uses .startswith, so the leading text is
        load-bearing: prepending the upstream detail would reclassify the row."""
        note = self.m.fail_note(self.m.RateLimited(REAL_LIMIT_TEXT, None))
        self.assertTrue(note.startswith("subscription rate-limited, waiting until "))
        self.assertFalse(note.startswith("ambiguous POST"))

    def test_the_park_duration_wording_is_preserved(self):
        """`~15m` is DEFAULT_PARK_S, NOT a parsed reset — it is what says the window is
        UNKNOWN rather than known and close."""
        note = self.m.fail_note(self.m.RateLimited(REAL_LIMIT_TEXT, None))
        self.assertIn(f"~{self.m.DEFAULT_PARK_S // 60}m", note)
        self.assertIn("no attempt consumed", note)

    def test_a_parsed_reset_still_renders_its_wall_clock_time(self):
        at = real_time.time() + 3600
        note = self.m.fail_note(self.m.RateLimited(REAL_LIMIT_TEXT, at))
        self.assertIn(real_time.strftime("%H:%M UTC", real_time.gmtime(at)), note)

    def test_the_detail_is_bounded_and_single_line(self):
        """It goes to the journal AND to jobs.note. Unbounded multi-line stderr is how the
        journal filled with argv dumps before fail_note existed."""
        note = self.m.fail_note(self.m.RateLimited("x\ny\n" + "z" * 5000, None))
        self.assertNotIn("\n", note)
        self.assertLess(len(note), 320)

    def test_it_still_consumes_no_attempt(self):
        """The note is telemetry; it must not disturb the policy it describes."""
        e = self.m.RateLimited(REAL_LIMIT_TEXT, None)
        self.assertEqual(("retry", 4, 1), self.m.next_failure_state(e, 4, 1)[:3])


class ModelPinStampTest(unittest.TestCase):
    """llm_primary_failed_total / llm_fallback_used_total describe exactly ONE pin.

    2026-09-16: reviewer-1's textfile still read 363 == 363 — the signature of a primary
    failing every single call — 23 h and 103 clean reviews after the repoint that retired the
    exhausted `claude-fable-5` pin which actually produced them. ReviewbotPrimaryModelDown
    reads increase()[6h] and was never fooled; the human reading the raw textfile was, and
    that is what gets read first."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name, llm_model="old-pin")

    def _emit(self):
        self.m.write_metrics()
        return dict(line.split(" ", 1) for line in
                    pathlib.Path(self.m.CFG["textfile"])
                    .read_text(encoding="utf-8").splitlines())

    def _meta(self):
        c = self.m.db()
        rows = dict(c.execute("SELECT k,v FROM meta WHERE k LIKE 'llm_%'"))
        c.close()
        return rows

    def test_a_first_sight_stamp_does_not_discard_history(self):
        """On a host that predates the stamp we cannot know which pin the totals belong to,
        and the counter is the only record there is — so seed, do not delete."""
        self.m.bump_meta("llm_primary_failed_total", 363)
        self.m.bump_meta("llm_fallback_used_total", 363)
        self._emit()
        meta = self._meta()
        self.assertEqual(363.0, float(meta["llm_primary_failed_total"]))
        self.assertEqual(363.0, float(meta["llm_fallback_used_total"]))
        self.assertEqual("old-pin", meta["llm_model_stamp"])

    def test_an_unchanged_pin_leaves_the_counters_alone(self):
        self.m.bump_meta("llm_primary_failed_total", 5)
        self._emit()
        self._emit()
        self.assertEqual(5.0, float(self._meta()["llm_primary_failed_total"]))

    def test_a_changed_pin_resets_both_counters(self):
        self.m.bump_meta("llm_primary_failed_total", 363)
        self.m.bump_meta("llm_fallback_used_total", 363)
        self._emit()                               # seeds the stamp at old-pin
        self.m.CFG["llm_model"] = "new-pin"
        self._emit()                               # observes the change
        meta = self._meta()
        self.assertEqual(0.0, float(meta["llm_primary_failed_total"]))
        self.assertEqual(0.0, float(meta["llm_fallback_used_total"]))
        self.assertEqual("new-pin", meta["llm_model_stamp"])

    def test_unrelated_counters_survive_a_pin_change(self):
        """Only the two MODEL-scoped counters reset. llm_failures_total counts whole reviews
        and says nothing about which model was asked."""
        self.m.bump_meta("llm_failures_total", 9)
        self.m.bump_meta("llm_timeouts_total", 4)
        self._emit()
        self.m.CFG["llm_model"] = "new-pin"
        self._emit()
        meta = self._meta()
        self.assertEqual(9.0, float(meta["llm_failures_total"]))
        self.assertEqual(4.0, float(meta["llm_timeouts_total"]))

    def test_the_pin_is_named_in_the_textfile(self):
        got = self._emit()
        keys = [k for k in got if k.startswith("reviewbot_llm_primary_model_info")]
        self.assertEqual(1, len(keys))
        self.assertIn('model="old-pin"', keys[0])
        self.assertEqual(1.0, float(got[keys[0]]))

    def test_an_unset_pin_is_named_explicitly_rather_than_blank(self):
        """An empty llm_model means the ACCOUNT DEFAULT, a distinct state from any named
        model; a blank label value would read as a missing one."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        m = load(tmp.name, llm_model="")
        m.write_metrics()
        text = pathlib.Path(m.CFG["textfile"]).read_text(encoding="utf-8")
        self.assertIn('model="(account default)"', text)

    def test_a_seeded_stamp_reports_its_counters_as_UNattributable(self):
        """THE reviewer-1 case (reviewer-claude, round 1 of ailab#742): 363/363 accumulated
        under a pin that has ALREADY been repointed away from. Seeding without resetting is
        correct — they are not the new pin's failures and must not be deleted on a guess — but
        naming the current pin beside them would assert opus produced failures it never did."""
        self.m.bump_meta("llm_primary_failed_total", 363)
        self.m.bump_meta("llm_fallback_used_total", 363)
        got = self._emit()
        self.assertEqual(0.0, float(got['reviewbot_llm_counters_pin_scoped{persona="test"}']),
                         "seeded stamp must not claim the counters belong to the current pin")
        self.assertEqual(363.0, float(got['reviewbot_llm_primary_failed_total{persona="test"}']),
                         "and must not delete them either")

    def test_an_observed_repoint_makes_the_counters_attributable(self):
        self.m.bump_meta("llm_primary_failed_total", 363)
        self._emit()                               # seeds at old-pin
        self.m.CFG["llm_model"] = "new-pin"
        got = self._emit()                         # observes the change
        self.assertEqual(1.0, float(got['reviewbot_llm_counters_pin_scoped{persona="test"}']))
        self.assertEqual(0.0, float(got['reviewbot_llm_primary_failed_total{persona="test"}']),
                         "counters reset, so they now genuinely describe the named pin")

    def test_attributability_survives_restarts_once_observed(self):
        """The flag is durable in `meta`, not in-process: a restart must not silently downgrade
        an attributable counter back to 'may predate the pin'."""
        self._emit()
        self.m.CFG["llm_model"] = "new-pin"
        self._emit()
        fresh = load(self.tmp.name, llm_model="new-pin")   # same DB, new module object
        fresh.write_metrics()
        text = pathlib.Path(fresh.CFG["textfile"]).read_text(encoding="utf-8")
        self.assertIn('reviewbot_llm_counters_pin_scoped{persona="test"} 1', text)

    def test_a_pin_carrying_a_quote_cannot_break_the_whole_textfile(self):
        """node_exporter rejects the ENTIRE file on one malformed line, so an unescaped label
        value would delete every reviewbot metric on the host — the trap the repo label below
        it was escaped for."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        m = load(tmp.name, llm_model='ev"il')
        m.write_metrics()
        text = pathlib.Path(m.CFG["textfile"]).read_text(encoding="utf-8")
        self.assertIn(r'model="ev\"il"', text)


# ── claude seats (plans/2026-09-18-claude-seat-rotation-plan.md, PR 1) ────────────────────────
# Captured at IMPORT, before any test runs: `self.m.subprocess` IS the stdlib module, so every
# `self.m.subprocess.run = fake` above rebinds subprocess.run for the whole process and no test
# restores it. The two tests below that spawn a real child must reach the real function.
_REAL_RUN = subprocess.run
CLAUDE_USAGE_PY = SRC.parent / "claude-usage.py"
CLAUDE_SEAT_SH = SRC.parent / "claude-seat.sh"
CLAUDE_SEATS = [{"name": "a", "sudo_user": "runa"},
                {"name": "b", "sudo_user": "runb"},
                {"name": "c", "sudo_user": "runc"}]
# The envelope the claude CLI returned on reviewer-1 on 2026-09-18 (exit 1, api_error_status
# 429) — ACCOUNT-scoped, so it must park the seat and move on.
CLAUDE_WEEKLY_ENVELOPE = json.dumps({
    "type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
    "result": "You've hit your weekly limit · resets Sep 19, 8pm (UTC)",
    "usage": {"output_tokens": 0}})
# The 2026-09-10 Fable wording — MODEL-scoped, so today's answer is the same-seat fallback.
CLAUDE_FABLE_ENVELOPE = json.dumps({
    "type": "result", "subtype": "success", "is_error": True,
    "result": "You're out of usage credits. Run /usage-credits to keep using Fable 5 or "
              "/model to switch models.",
    "usage": {"output_tokens": 0}})


def _review_envelope(summary="s"):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "result": json.dumps({"summary": summary, "findings": []}),
                       "usage": {"output_tokens": 7}})


class ClaudeSeatTest(unittest.TestCase):
    """The claude persona under the seat rotation reviewer-2 already runs.

    2026-09-18: reviewer-1's ONE account hit its weekly limit and the persona idled for 21 h
    with 9 jobs queued while reviewer-2 held 7 merge-blocked PRs waiting for the claude
    verdict. The rotation, the lossless park and the sticky selection are kind-agnostic and
    are CHARACTERISED here rather than re-tested; what is new for claude is (1) the seat's
    credential reaching the CLI through a wrapper in the seat's own HOME, (2) resolve_seats()
    learning claude identities from the usage probe, and (3) the pre-post credential scan
    covering the claude token.

    The fake dispatches on argv POSITION, like IsolatedSeatUserTest: the out paths are
    os.path.join'd and backslashed when the suite runs on Windows."""

    WRAPPER = "/usr/local/lib/reviewbot/claude-seat.sh"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name, llm_kind="claude", llm_model="fable",
                      llm_fallback_model="opus", llm_cmd=[self.WRAPPER], llm_sudo_user="",
                      llm_seats=CLAUDE_SEATS)
        self.addCleanup(setattr, self.m, "RATE_LIMITED_UNTIL", 0.0)
        self.calls, self.models_run = [], []
        self.answer = _review_envelope()

    def _runner(self, refusing=(), fable_limited=(), tokens=None, creds=None, identities=None,
                unreachable=(), probe_broken=(), broken=None, cat_hangs=()):
        """`tokens` maps user -> the text of <home>/.claude/oauth-token; `creds` maps user ->
        the text of <home>/.claude/.credentials.json. A user in neither has NO readable
        credential. `identities` maps user -> account uuid the usage probe reports. `broken`
        maps user -> the `result` text of a non-limit failure (exit 1 on every model).
        `cat_hangs`: users whose credential read times out (aux_run turns that into an
        ordinary RuntimeError)."""
        tokens, creds = tokens or {}, creds or {}
        identities, broken = identities or {}, broken or {}

        def run(args, **kw):
            self.calls.append(list(args))
            CP = self.m.subprocess.CompletedProcess
            if args[0] != "sudo":
                return CP(args, 0, "", "")
            user, verb = args[3], args[4]
            if verb == "true":
                return CP(args, 1 if user in unreachable else 0, "", "")
            if verb == "mktemp":
                return CP(args, 0, f"/tmp/reviewbot-llm-{user}\n", "")
            if verb.startswith("HOME="):
                if args[5] == self.m.USAGE_PROBE:
                    if user in probe_broken:
                        return CP(args, 1, "", "boom")
                    acct = identities.get(user)
                    doc = {"ok": bool(acct), "error": "" if acct else "no credential",
                           "account": ({"uuid": acct, "email": f"{user}@example.test",
                                        "plan": "max"} if acct else {}),
                           "limits": []}
                    return CP(args, 0, json.dumps(doc), "")
                model = args[args.index("--model") + 1] if "--model" in args else ""
                self.models_run.append((user, model))
                if user in refusing:
                    return CP(args, 1, CLAUDE_WEEKLY_ENVELOPE, "")
                if user in fable_limited and model == "fable":
                    return CP(args, 1, CLAUDE_FABLE_ENVELOPE, "")
                if user in broken:
                    return CP(args, 1, json.dumps({"type": "result", "subtype": "success",
                                                   "is_error": True, "result": broken[user]}), "")
                return CP(args, 0, self.answer, "")
            if verb == "cat":
                path = args[5]
                if user in cat_hangs:
                    raise self.m.subprocess.TimeoutExpired(args, kw.get("timeout", 1))
                if path.endswith("oauth-token") and user in tokens:
                    return CP(args, 0, tokens[user] + "\n", "")
                if path.endswith(".credentials.json") and user in creds:
                    return CP(args, 0, creds[user], "")
                return CP(args, 1, "", "cat: No such file or directory")
            return CP(args, 0, "", "")
        return run

    def _sudo(self, pred):
        return [c for c in self.calls if c[0] == "sudo" and pred(c)]

    # ---- characterisation: the rotation is kind-agnostic ------------------------------------
    def test_a_weekly_limit_on_seat_a_rotates_to_b_and_parks_a(self):
        """CHARACTERISATION. The claude envelope carries the account-scoped text in `result`;
        llm_error_text() surfaces it, RATE_LIMIT_RE parks the seat, run_llm moves on."""
        self.m.subprocess.run = self._runner(refusing={"runa"},
                                             tokens={"runa": "T" * 40, "runb": "U" * 40})
        self.m.run_llm("t", "d", "diff")
        self.assertEqual([("runa", "fable"), ("runb", "fable")], self.models_run)
        self.assertTrue(self.m.seat_parked("a"))
        self.assertFalse(self.m.seat_parked("b"))
        self.assertEqual("b", self.m.CURRENT_SEAT)

    def test_a_fable_limit_moves_to_the_next_seat_on_the_same_tier(self):
        """PR 2 (the ladder): a MODEL-scoped refusal parks that SEAT+TIER pair and moves to the
        next seat on the SAME tier - the account still serves fable elsewhere - instead of
        dropping to the fallback pin on the exhausted seat as PR 1 characterised. The legacy
        pin pair (llm_model + llm_fallback_model) is the ladder here: [fable, opus]."""
        self.m.subprocess.run = self._runner(fable_limited={"runa"},
                                             tokens={"runa": "T" * 40, "runb": "U" * 40})
        self.m.run_llm("t", "d", "diff")
        self.assertEqual([("runa", "fable"), ("runb", "fable")], self.models_run)
        self.assertTrue(self.m.model_parked("a", "fable"))
        self.assertFalse(self.m.seat_parked("a"), "a model limit is not an account limit")
        self.assertEqual(("b", "fable"), (self.m.CURRENT_SEAT, self.m.CURRENT_MODEL))

    def test_the_model_runs_through_the_wrapper_as_the_seat_user_with_its_HOME(self):
        """The token can only reach the CLI from inside the seat's HOME (sudo resets the
        environment), so the entry point must be the wrapper, run as that user, with HOME set."""
        self.m.subprocess.run = self._runner(tokens={"runa": "T" * 40})
        self.m.run_llm("t", "d", "diff")
        model = self._sudo(lambda c: c[4].startswith("HOME=") and c[5] == self.WRAPPER)
        self.assertEqual(1, len(model))
        self.assertEqual(["sudo", "-n", "-u", "runa", "HOME=/home/runa", self.WRAPPER, "-p"],
                         model[0][:7])
        self.assertEqual("fable", model[0][model[0].index("--model") + 1])

    # ---- resolve_seats(): claude identities come from the usage probe -------------------------
    def test_two_seats_on_one_account_collapse_to_one(self):
        """Rotating inside one account is the doomed-retry loop the park prevents. The codex
        path reads tokens.account_id; the claude path has no such file, so identity is the
        profile's account uuid, obtained the only way the service user may: by running the
        probe AS the seat."""
        self.m.subprocess.run = self._runner(identities={"runa": "acct-1", "runb": "acct-1",
                                                         "runc": "acct-2"})
        self.m.resolve_seats()
        self.assertEqual(["a", "c"], [s["name"] for s in self.m.SEATS])
        self.assertEqual(3, self.m.SEATS_CONFIGURED, "configured stays 3: that gap IS the alert")

    def test_a_seat_that_cannot_be_sudoed_to_is_dropped(self):
        self.m.subprocess.run = self._runner(identities={"runa": "acct-1", "runb": "acct-2",
                                                         "runc": "acct-3"},
                                             unreachable={"runb"})
        self.m.resolve_seats()
        self.assertEqual(["a", "c"], [s["name"] for s in self.m.SEATS])

    def test_a_failed_probe_keeps_the_seat_with_an_unknown_identity(self):
        """Unknown is not "the same as another unknown": b stays, while c (a real duplicate of
        a) still collapses."""
        self.m.subprocess.run = self._runner(identities={"runa": "acct-1", "runc": "acct-1"},
                                             probe_broken={"runb"})
        self.m.resolve_seats()
        self.assertEqual(["a", "b"], [s["name"] for s in self.m.SEATS])

    def test_the_identity_probe_runs_as_the_seat_with_its_HOME(self):
        self.m.subprocess.run = self._runner(identities={"runa": "1", "runb": "2", "runc": "3"})
        self.m.resolve_seats()
        probes = self._sudo(lambda c: c[4].startswith("HOME=") and c[5] == self.m.USAGE_PROBE)
        self.assertEqual([["sudo", "-n", "-u", u, f"HOME=/home/{u}", self.m.USAGE_PROBE]
                          for u in ("runa", "runb", "runc")], probes)

    # ---- the pre-post credential scan covers the claude token -------------------------------
    def test_the_scan_reads_the_seat_token_as_the_SAME_user_that_ran_the_model(self):
        self.m.subprocess.run = self._runner(tokens={"runa": "T" * 40})
        self.m.run_llm("t", "d", "diff")
        model = self._sudo(lambda c: c[4].startswith("HOME=") and c[5] == self.WRAPPER)[0]
        scan = self._sudo(lambda c: c[4] == "cat" and c[5].endswith("oauth-token"))
        self.assertEqual(1, len(scan))
        self.assertEqual(model[3], scan[0][3], "scan ran as a different user than the model")
        self.assertEqual("/home/runa/.claude/oauth-token", scan[0][5])

    def test_the_seat_token_in_the_output_is_refused(self):
        token = "T" * 40
        self.answer = _review_envelope(summary="leak " + token)
        self.m.subprocess.run = self._runner(tokens={"runa": token})
        with self.assertRaises(RuntimeError) as cm:
            self.m.run_llm("t", "d", "diff")
        self.assertIn("credential material", str(cm.exception))

    def test_a_credentials_json_login_is_scanned_when_there_is_no_token_file(self):
        """A seat provisioned by browser login instead of a token file keeps its credential in
        .credentials.json; both of its tokens must be scanned for. (The single-seat service
        user is NOT scanned - it has no isolated credential, and --disallowedTools is its
        protection, unchanged.)"""
        refresh = "R" * 40
        self.answer = _review_envelope(summary="leak " + refresh)
        self.m.subprocess.run = self._runner(creds={"runa": json.dumps(
            {"claudeAiOauth": {"accessToken": "A" * 40, "refreshToken": refresh}})})
        with self.assertRaises(RuntimeError) as cm:
            self.m.run_llm("t", "d", "diff")
        self.assertIn("credential material", str(cm.exception))

    def _skipped(self):
        c = self.m.db()
        v = c.execute("SELECT v FROM meta WHERE k='llm_credscan_skipped_total'").fetchone()
        c.close()
        return float(v[0]) if v else 0.0

    def test_no_readable_credential_counts_a_skipped_scan_and_still_posts(self):
        """The same visibility rule as codex: "I could not check" must be counted, never
        silent, and must not block the review (mistake prevention, not a boundary)."""
        self.m.subprocess.run = self._runner()
        self.m.run_llm("t", "d", "diff")
        self.assertEqual(1.0, self._skipped())

    def test_an_empty_token_file_falls_through_to_the_browser_login_scan(self):
        """codex review of PR 1: a readable-but-empty oauth-token used to end the scan with
        nothing to look for and nothing counted - the browser login beside it went unscanned.
        Same fall-through as claude-usage.py's read_credential()."""
        refresh = "R" * 40
        self.answer = _review_envelope(summary="leak " + refresh)
        self.m.subprocess.run = self._runner(tokens={"runa": ""}, creds={"runa": json.dumps(
            {"claudeAiOauth": {"accessToken": "A" * 40, "refreshToken": refresh}})})
        with self.assertRaises(RuntimeError) as cm:
            self.m.run_llm("t", "d", "diff")
        self.assertIn("credential material", str(cm.exception))

    def test_an_empty_token_file_and_no_login_is_a_counted_skip(self):
        self.m.subprocess.run = self._runner(tokens={"runa": ""})
        self.m.run_llm("t", "d", "diff")
        self.assertEqual(1.0, self._skipped())

    def test_a_token_in_failure_text_is_redacted_from_the_error_and_the_journal(self):
        """codex review of PR 1: the scan covered only SUCCESSFUL output. A failing run's
        stdout/stderr goes through llm_error_text() into the exception and the journal (which
        ships to Loki), and a CLI that echoes a malformed credential in its error - as Python's
        own header validation does - would post it there. Every error path must be scrubbed."""
        token = "T" * 40
        logged = []
        self.m.log = lambda *a: logged.append(" ".join(str(x) for x in a))
        self.m.subprocess.run = self._runner(tokens={"runa": token},
                                             broken={"runa": "API Error: 401 bad token " + token})
        with self.assertRaises(RuntimeError) as cm:
            self.m.run_llm("t", "d", "diff")
        self.assertNotIn(token, str(cm.exception))
        self.assertIn("<redacted>", str(cm.exception))
        self.assertEqual([], [ln for ln in logged if token in ln],
                         "the seat token reached the journal")

    def test_an_unreadable_credential_withholds_the_failure_text_instead_of_publishing_it(self):
        """reviewer-codex on ailab#777: the first cut read the secrets lazily, on the error
        path, with the budget that was left - and when that read failed it cached an empty
        list and returned the ORIGINAL text, i.e. it failed open precisely when a near-deadline
        run had spent the budget. The secrets are now read BEFORE the model runs, and a read
        that fails withholds the diagnostic rather than publishing it unscrubbed."""
        token = "T" * 40
        logged = []
        self.m.log = lambda *a: logged.append(" ".join(str(x) for x in a))
        self.m.subprocess.run = self._runner(tokens={"runa": token}, cat_hangs={"runa"},
                                             broken={"runa": "API Error: 401 bad token " + token})
        with self.assertRaises(RuntimeError) as cm:
            self.m.run_llm("t", "d", "diff")
        self.assertNotIn(token, str(cm.exception))
        self.assertIn("withheld", str(cm.exception))
        self.assertEqual([], [ln for ln in logged if token in ln],
                         "the seat token reached the journal")

    def test_a_withheld_diagnostic_still_parks_on_a_rate_limit(self):
        """Classification and the reset parse must read the RAW text: withholding it from the
        exception is a publication decision, and must not turn a park into an ordinary
        failure that bills the PR an attempt."""
        self.m.subprocess.run = self._runner(refusing={"runa", "runb", "runc"},
                                             cat_hangs={"runa", "runb", "runc"})
        with self.assertRaises(self.m.RateLimited) as cm:
            self.m.run_llm("t", "d", "diff")
        self.assertTrue(self.m.seat_parked("a"))
        self.assertIn("withheld", str(cm.exception))
        self.assertNotIn("weekly limit", str(cm.exception))


def _load_probe():
    spec = importlib.util.spec_from_file_location("claude_usage_under_test", CLAUDE_USAGE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ClaudeUsageProbeTest(unittest.TestCase):
    """files/claude-usage.py: runs AS a seat user, reads that HOME's credential, and turns
    /api/oauth/profile + /api/oauth/usage into ONE small document reviewbot can consume.

    Shape pinned against the payload measured on reviewer-1 on 2026-09-18. The `http` seam is
    injected so no test opens a socket; main() is exercised once end to end with no credential
    at all, which is the one path that needs no network."""

    PROFILE = {"account": {"uuid": "acct-1", "email": "seat@example.test", "full_name": "x"},
               "organization": {"rate_limit_tier": "default_claude_max_20x",
                                "subscription_status": "active"}}
    USAGE = {"limits": [
        {"kind": "session", "group": "session", "percent": 0, "severity": "normal",
         "resets_at": None, "scope": None, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 100, "severity": "critical",
         "resets_at": "2026-09-19T19:59:59.670651+00:00", "scope": None, "is_active": True},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 100, "severity": "critical",
         "resets_at": "2026-09-19T19:59:59.670966+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
         "is_active": False}]}

    def setUp(self):
        self.p = _load_probe()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        (self.home / ".claude").mkdir()
        self.seen = []

    def _http(self, profile=(200, None), usage=(200, None)):
        bodies = {"profile": (profile[0], json.dumps(self.PROFILE) if profile[1] is None else profile[1]),
                  "usage": (usage[0], json.dumps(self.USAGE) if usage[1] is None else usage[1])}

        def http(url, token):
            self.seen.append((url.rsplit("/", 1)[-1], token))
            return bodies[url.rsplit("/", 1)[-1]]
        return http

    def test_the_token_file_is_read_first(self):
        (self.home / ".claude" / "oauth-token").write_text("tok-file\n", encoding="utf-8")
        (self.home / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "tok-login"}}), encoding="utf-8")
        self.assertEqual("tok-file", self.p.read_credential(str(self.home)))

    def test_a_credentials_json_login_is_the_fallback(self):
        (self.home / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "tok-login"}}), encoding="utf-8")
        self.assertEqual("tok-login", self.p.read_credential(str(self.home)))

    def test_no_credential_is_an_ok_false_document_not_an_exception(self):
        doc = self.p.probe(str(self.home), self._http())
        self.assertFalse(doc["ok"])
        self.assertIn("credential", doc["error"])
        self.assertEqual(({}, []), (doc["account"], doc["limits"]))
        self.assertEqual([], self.seen, "no credential must mean no request")

    def test_iso_timestamps_become_epochs_and_null_stays_null(self):
        self.assertEqual(1789847999, self.p.iso_epoch("2026-09-19T19:59:59.670651+00:00"))
        self.assertIsNone(self.p.iso_epoch(None))
        self.assertIsNone(self.p.iso_epoch("not a date"))

    def test_the_document_shape(self):
        (self.home / ".claude" / "oauth-token").write_text("tok\n", encoding="utf-8")
        doc = self.p.probe(str(self.home), self._http())
        self.assertTrue(doc["ok"])
        self.assertEqual("", doc["error"])
        self.assertEqual({"uuid": "acct-1", "email": "seat@example.test",
                          "plan": "default_claude_max_20x"}, doc["account"])
        self.assertEqual({"kind": "weekly_all", "model": "", "percent": 100.0,
                          "resets_at": 1789847999, "active": True}, doc["limits"][1])
        self.assertEqual("Fable", doc["limits"][2]["model"])
        self.assertEqual({"kind": "session", "model": "", "percent": 0.0,
                          "resets_at": None, "active": False}, doc["limits"][0])
        self.assertEqual([("profile", "tok"), ("usage", "tok")], self.seen)

    def test_an_http_failure_is_ok_false_with_the_status_in_the_error(self):
        (self.home / ".claude" / "oauth-token").write_text("tok\n", encoding="utf-8")
        doc = self.p.probe(str(self.home), self._http(usage=(401, "unauthorized")))
        self.assertFalse(doc["ok"])
        self.assertIn("401", doc["error"])
        self.assertEqual([], doc["limits"])

    def _main(self):
        r = _REAL_RUN([sys.executable, str(CLAUDE_USAGE_PY)], capture_output=True,
                      text=True, timeout=60, env={**os.environ, "HOME": str(self.home)})
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertTrue(r.stdout.strip(), f"no document on stdout; stderr={r.stderr!r}")
        return json.loads(r.stdout)

    def test_main_prints_one_json_document_and_exits_zero_without_a_credential(self):
        self.assertFalse(self._main()["ok"])

    # ---- codex review of PR 1: the "one document, exit 0" contract on every path --------------
    def test_a_malformed_token_file_is_a_document_not_a_traceback(self):
        """UnicodeDecodeError is a ValueError, which `except OSError` never caught."""
        (self.home / ".claude" / "oauth-token").write_bytes(b"\xff\xfe\x00bad")
        doc = self.p.probe(str(self.home), self._http())
        self.assertFalse(doc["ok"])
        self.assertIn("credential", doc["error"])
        self.assertEqual([], self.seen, "a malformed credential must never be sent")
        self.assertFalse(self._main()["ok"])

    def test_a_token_with_embedded_whitespace_is_refused_and_never_echoed(self):
        """Python's header validation raises `ValueError: Invalid header value b'Bearer
        <token>'` for such a token - the one exception whose text IS the credential."""
        (self.home / ".claude" / "oauth-token").write_text("sk-secret-part\nsk-secret-tail\n",
                                                          encoding="utf-8")
        doc = self.p.probe(str(self.home), self._http())
        self.assertFalse(doc["ok"])
        self.assertNotIn("sk-secret", doc["error"])
        self.assertEqual([], self.seen)

    def test_exception_text_never_carries_the_token(self):
        (self.home / ".claude" / "oauth-token").write_text("tok-secret-value-1234567890\n",
                                                          encoding="utf-8")

        def http(url, token):
            raise ValueError(f"Invalid header value b'Bearer {token}'")
        doc = self.p.probe(str(self.home), http)
        self.assertFalse(doc["ok"])
        self.assertNotIn("tok-secret", doc["error"])
        self.assertIn("ValueError", doc["error"])

    def test_a_profile_failure_does_not_skip_the_usage_call(self):
        """The two results are documented as independent; one try block made them not."""
        (self.home / ".claude" / "oauth-token").write_text("tok\n", encoding="utf-8")
        good = self._http()

        def http(url, token):
            if url.endswith("/profile"):
                raise RuntimeError("boom")
            return good(url, token)
        doc = self.p.probe(str(self.home), http)
        self.assertTrue(doc["ok"])
        self.assertEqual({}, doc["account"])
        self.assertIn("profile", doc["error"])
        self.assertEqual(3, len(doc["limits"]))


def _bash():
    """A bash that receives the environment we pass it. On Windows `shutil.which("bash")` finds
    System32\\bash.exe, which is WSL: it drops every Windows variable and has no /usr/bin/claude,
    so the wrapper fails for reasons that have nothing to do with the wrapper. Prefer Git Bash
    there; elsewhere (the Linux Gitea runners) plain bash is the real thing."""
    if sys.platform == "win32":
        for cand in (os.environ.get("CLAUDE_CODE_GIT_BASH_PATH"),
                     r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
            if cand and os.path.exists(cand):
                return cand
        return None
    return shutil.which("bash")


@unittest.skipUnless(_bash(), "needs a non-WSL bash (the Gitea runners are Linux)")
class ClaudeSeatWrapperTest(unittest.TestCase):
    """files/claude-seat.sh: the seat's long-lived token reaches the CLI through the
    ENVIRONMENT, from inside the seat's own HOME — never on argv, which sudo would show to
    every process on the host."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = pathlib.Path(self.tmp.name) / "home"
        (home / ".claude").mkdir(parents=True)
        self.home = home
        stub = pathlib.Path(self.tmp.name) / "claude-stub.sh"
        stub.write_text('#!/bin/sh\nprintf "%s|%s" "${CLAUDE_CODE_OAUTH_TOKEN-unset}" "$*"\n',
                        encoding="utf-8")
        stub.chmod(0o755)
        self.stub = stub

    def _run(self):
        fwd = lambda p: str(p).replace("\\", "/")
        env = {**os.environ, "HOME": fwd(self.home), "REVIEWBOT_CLAUDE_BIN": fwd(self.stub)}
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        r = _REAL_RUN([_bash(), fwd(CLAUDE_SEAT_SH), "-p", "hi"], env=env,
                      capture_output=True, text=True, timeout=60)
        self.assertEqual(0, r.returncode, r.stderr)
        return r.stdout

    def test_the_token_file_is_exported_and_the_cli_is_execd_with_the_arguments(self):
        (self.home / ".claude" / "oauth-token").write_text("tok123\n", encoding="utf-8")
        self.assertEqual("tok123|-p hi", self._run())

    def test_without_a_token_file_the_environment_is_left_alone(self):
        """The single-seat host keeps its .credentials.json login; the wrapper must not
        invent an empty token that would shadow it."""
        self.assertEqual("unset|-p hi", self._run())


# ── model ladder + usage watchdog (plan PR 2) ─────────────────────────────────────────────────
CLAUDE_404_ENVELOPE = json.dumps({
    "type": "result", "subtype": "success", "is_error": True, "api_error_status": 404,
    "result": "There's an issue with the selected model (fable). It may not exist or you may "
              "not have access to it. Run --model to pick a different model.",
    "usage": {"output_tokens": 0}})
LADDER = ["fable", "opus", "sonnet"]


def _ladder_module(tmp, **overrides):
    cfg = dict(llm_kind="claude", llm_model="fable", llm_fallback_model="opus",
               llm_models=LADDER, llm_cmd=["/usr/local/lib/reviewbot/claude-seat.sh"],
               llm_sudo_user="", llm_seats=CLAUDE_SEATS, usage_poll_s=0)
    cfg.update(overrides)
    return load(tmp, **cfg)


class ModelLadderTest(unittest.TestCase):
    """fable -> opus -> sonnet, tier-major, sticky within a tier.

    Any seat that can serve fable beats the current seat on opus; a tier is left only when
    every seat is parked for it; a park is per (seat, model) for a MODEL-scoped refusal and per
    seat for an ACCOUNT-scoped one. THE INVARIANT IS STILL THE LOSSLESS PARK: no attempt
    consumed, queue intact, nothing quarantined, whatever the scope."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = _ladder_module(self.tmp.name)
        self.addCleanup(setattr, self.m, "RATE_LIMITED_UNTIL", 0.0)
        self.models_run = []
        self.answer = _review_envelope()

    def _runner(self, model_limited=(), account_limited=(), notfound=(), broken=None):
        """Keys are (sudo user, model). `broken` maps a pair to a non-limit failure text."""
        broken = broken or {}

        def run(args, **kw):
            CP = self.m.subprocess.CompletedProcess
            if args[0] != "sudo":
                return CP(args, 0, "", "")
            user, verb = args[3], args[4]
            if verb == "mktemp":
                return CP(args, 0, f"/tmp/reviewbot-llm-{user}\n", "")
            if verb.startswith("HOME=") and args[5] != self.m.USAGE_PROBE:
                model = args[args.index("--model") + 1] if "--model" in args else ""
                self.models_run.append((user, model))
                if user in account_limited:
                    return CP(args, 1, CLAUDE_WEEKLY_ENVELOPE, "")
                if (user, model) in model_limited:
                    return CP(args, 1, CLAUDE_FABLE_ENVELOPE, "")
                if (user, model) in notfound:
                    return CP(args, 1, CLAUDE_404_ENVELOPE, "")
                if (user, model) in broken:
                    return CP(args, 1, json.dumps({"type": "result", "subtype": "success",
                                                   "is_error": True,
                                                   "result": broken[(user, model)]}), "")
                return CP(args, 0, self.answer, "")
            if verb == "cat":
                return CP(args, 1, "", "no such file")
            return CP(args, 0, "", "")
        return run

    def _meta(self, key):
        c = self.m.db()
        v = c.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        c.close()
        return float(v[0]) if v else 0.0

    def test_tiers_come_from_llm_models_and_fall_back_to_the_pin_pair(self):
        self.assertEqual(LADDER, self.m.MODELS)
        self.assertEqual("fable", self.m.CURRENT_MODEL)
        legacy = load(self.tmp.name)                     # BASE_CFG: llm_model m, fallback fb
        self.assertEqual(["m", "fb"], legacy.MODELS, "empty llm_models is the pin pair")
        codex = load(self.tmp.name, llm_kind="codex", llm_model="gpt-6-astra",
                     llm_fallback_model="")
        self.assertEqual(["gpt-6-astra"], codex.MODELS)

    def test_a_blank_primary_stays_the_account_default_tier_ahead_of_the_fallback(self):
        """reviewer-codex on ailab#780: a host with `llm_model: ""` (the account default,
        i.e. no --model flag) and a fallback used to run the default first and the fallback
        only after a failure. Dropping the blank made the fallback the ONLY tier, so every
        review ran on it and was counted as primary service. The blank is a tier."""
        m = load(self.tmp.name, llm_model="", llm_fallback_model="fb")
        self.assertEqual(["", "fb"], m.MODELS)
        self.assertEqual("", m.CURRENT_MODEL)
        both = load(self.tmp.name, llm_model="", llm_fallback_model="")
        self.assertEqual([""], both.MODELS)
        self.assertEqual({(s["name"], mdl) for s in self.m.SEATS for mdl in LADDER},
                         set(self.m.MODEL_PARKED_UNTIL), "pre-seeded, fixed-size, per pair")

    def test_a_model_limit_on_seat_a_moves_to_seat_b_on_the_same_tier(self):
        self.m.subprocess.run = self._runner(model_limited={("runa", "fable")})
        self.m.run_llm("t", "d", "diff")
        self.assertEqual([("runa", "fable"), ("runb", "fable")], self.models_run)
        self.assertTrue(self.m.model_parked("a", "fable"))
        self.assertFalse(self.m.model_parked("a", "opus"))
        self.assertFalse(self.m.seat_parked("a"))
        self.assertEqual(("b", "fable"), (self.m.CURRENT_SEAT, self.m.CURRENT_MODEL))
        self.assertEqual(1.0, self._meta("llm_seat_switches_total"))
        self.assertEqual(0.0, self._meta("llm_model_switches_total"))

    def test_an_account_limit_parks_the_seat_for_every_tier(self):
        self.m.subprocess.run = self._runner(account_limited={"runa"})
        self.m.run_llm("t", "d", "diff")
        self.assertEqual([("runa", "fable"), ("runb", "fable")], self.models_run)
        self.assertTrue(self.m.seat_parked("a"))
        self.assertFalse(self.m.seat_usable("a"))
        self.assertFalse(self.m.model_parked("a", "opus"), "the pair table is untouched")

    def test_descends_a_tier_only_when_every_seat_is_parked_for_it_and_on_the_sticky_seat(self):
        self.m.subprocess.run = self._runner(
            model_limited={("runa", "fable"), ("runb", "fable"), ("runc", "fable")})
        self.m.run_llm("t", "d", "diff")
        self.assertEqual([("runa", "fable"), ("runb", "fable"), ("runc", "fable"),
                          ("runc", "opus")], self.models_run)
        self.assertEqual(("c", "opus"), (self.m.CURRENT_SEAT, self.m.CURRENT_MODEL))
        self.assertEqual(1.0, self._meta("llm_model_switches_total"))
        self.assertEqual(1.0, self._meta("llm_fallback_used_total"),
                         "served below the top tier - the PrimaryModelDown signal")

    def test_a_missing_model_parks_the_pair_for_MAX_PARK_S(self):
        """A floating alias that stops resolving must not quarantine every PR: the 404 is
        model-scoped, parked long, and the persona serves on from the next candidate."""
        self.m.subprocess.run = self._runner(notfound={("runa", "fable")})
        before = real_time.time()
        self.m.run_llm("t", "d", "diff")
        self.assertEqual([("runa", "fable"), ("runb", "fable")], self.models_run)
        self.assertGreaterEqual(self.m.model_parked_until("a", "fable") - before,
                                self.m.MAX_PARK_S - 5)
        self.assertEqual(0.0, self.m.MODEL_PARKED_UNTIL[("a", "fable")],
                         "an unserveable model is not a spent window: its own table")

    def test_an_ordinary_error_on_the_top_tier_descends_on_the_same_seat_without_parking(self):
        """Today's fallback, generalised: a non-limit failure is not evidence about the account,
        so nothing parks; the next tier on the SAME seat gets the rest of the budget."""
        self.m.subprocess.run = self._runner(broken={("runa", "fable"): "API Error: 500"})
        self.m.run_llm("t", "d", "diff")
        self.assertEqual([("runa", "fable"), ("runa", "opus")], self.models_run)
        self.assertFalse(self.m.model_parked("a", "fable"))
        self.assertEqual(("a", "opus"), (self.m.CURRENT_SEAT, self.m.CURRENT_MODEL))
        self.assertEqual(1.0, self._meta("llm_primary_failed_total"))
        self.assertEqual(1.0, self._meta("llm_fallback_used_total"))

    def test_every_tier_erroring_bills_exactly_one_attempt(self):
        self.m.subprocess.run = self._runner(broken={("runa", m): "API Error: 500" for m in LADDER})
        with self.assertRaises(RuntimeError) as cm:
            self.m.run_llm("t", "d", "diff")
        self.assertNotIsInstance(cm.exception, self.m.RateLimited)
        self.assertEqual([("runa", "fable"), ("runa", "opus"), ("runa", "sonnet")],
                         self.models_run)
        self.assertEqual(("retry", 1, 0), self.m.next_failure_state(cm.exception, 0, 0)[:3])

    def test_the_loop_is_bounded_by_seats_times_models_and_consumes_no_attempt(self):
        self.m.subprocess.run = self._runner(
            model_limited={(u, mdl) for u in ("runa", "runb", "runc") for mdl in LADDER})
        with self.assertRaises(self.m.RateLimited) as cm:
            self.m.run_llm("t", "d", "diff")
        self.assertEqual(9, len(self.models_run))
        self.assertEqual(0, self.m.seats_available())
        self.assertEqual(("retry", 4, 1), self.m.next_failure_state(cm.exception, 4, 1)[:3])

    def test_availability_and_reopening_understand_tiers(self):
        now = real_time.time()
        for i, mdl in enumerate(LADDER):
            self.m.park_model("a", mdl, now + 100 * (i + 1))
        self.m.park(now + 80, seat="b")               # above park()'s 60 s floor
        self.assertFalse(self.m.seat_usable("a"), "every tier parked = seat unusable")
        self.assertTrue(self.m.seat_usable("b") is False and self.m.seat_usable("c") is True)
        self.assertEqual(1, self.m.seats_available())
        self.assertEqual(0.0, self.m.all_parked_until(), "one usable seat = no global wall")
        self.m.park(now + 400, seat="c")
        self.assertAlmostEqual(now + 80, self.m.all_parked_until(), delta=2,
                               msg="the EARLIEST reopening across seats, b's")
        self.assertAlmostEqual(now + 100, self.m.seat_reopens_at("a"), delta=2,
                               msg="a reopens when its earliest tier does")

    # ---- codex review of PR 2 ---------------------------------------------------------------
    def test_fallback_used_counts_reviews_SERVED_below_the_top_tier_not_transitions(self):
        """The startup poll can park fable on every seat before any review runs; reviews then
        start directly on opus with no transition to count, and ReviewbotPrimaryModelDown
        (increase(fallback_used)[6h] >= 10) would stay silent through hours of fallback
        service. Count what the alert is about: a review served below the top tier."""
        now = real_time.time()
        for s in "abc":
            self.m.park_model(s, "fable", now + 3000)
        self.m.subprocess.run = self._runner()
        self.m.run_llm("t", "d", "diff")
        self.m.run_llm("t", "d", "diff")
        self.assertEqual([("runa", "opus"), ("runa", "opus")], self.models_run)
        self.assertEqual(2.0, self._meta("llm_fallback_used_total"))

    def test_model_switches_count_every_committed_model_change(self):
        """A ModelError descent changes the model just as a refusal does; the counter follows
        the COMMITTED model, not the code path that changed it."""
        self.m.subprocess.run = self._runner(broken={("runa", "fable"): "API Error: 500"})
        self.m.run_llm("t", "d", "diff")
        self.assertEqual(("a", "opus"), (self.m.CURRENT_SEAT, self.m.CURRENT_MODEL))
        self.assertEqual(1.0, self._meta("llm_model_switches_total"))

    def test_the_choice_is_tier_major_then_sticky_seat_then_seat_order(self):
        now = real_time.time()
        self.m.use_seat("c")
        self.assertEqual(("c", "fable"), self.m.active_choice(now), "sticky seat first")
        self.m.park_model("c", "fable", now + 900)
        self.assertEqual(("a", "fable"), self.m.active_choice(now),
                         "another seat on the top tier beats the sticky seat's next tier")
        for s in ("a", "b"):
            self.m.park_model(s, "fable", now + 900)
        self.assertEqual(("c", "opus"), self.m.active_choice(now), "descent stays on the sticky seat")
        self.assertIsNone(self.m.active_choice(now, exclude={(s, mdl) for s in "abc" for mdl in LADDER}))


class UsageWatchdogTest(unittest.TestCase):
    """poll_usage(): the API's numbers park and unpark seats and tiers, and climb back up the
    ladder - hourly, never within a tier. No network: probe_usage is replaced per test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = _ladder_module(self.tmp.name, usage_poll_s=3600)
        self.addCleanup(setattr, self.m, "RATE_LIMITED_UNTIL", 0.0)
        self.now = real_time.time()

    def _doc(self, email="seat@example.test", uuid="acct-1", limits=(), ok=True):
        return {"ok": ok, "error": "" if ok else "usage: HTTP 401",
                "account": {"uuid": uuid, "email": email, "plan": "max"} if ok else {},
                "limits": [{"kind": k, "model": mdl, "percent": float(p),
                            "resets_at": (None if r is None else self.now + r), "active": p >= 100}
                           for (k, mdl, p, r) in limits]}

    def _probe(self, docs):
        """docs: seat name -> document (missing seats get an ok=false document)."""
        def probe(seat, timeout=30):
            name = seat["name"] if isinstance(seat, dict) else seat
            return docs.get(name, self._doc(ok=False))
        self.m.probe_usage = probe

    def test_a_spent_weekly_window_parks_the_seat_until_the_api_reset(self):
        self.m.apply_usage("a", self._doc(limits=[("weekly_all", "", 100, 3000)]), self.now)
        self.assertTrue(self.m.seat_parked("a"))
        self.assertAlmostEqual(self.now + 3000, self.m.SEAT_PARKED_UNTIL["a"], delta=2)

    def test_below_100_percent_clears_a_park_the_api_no_longer_supports(self):
        self.m.park(self.now + 800, seat="a")
        self.m.apply_usage("a", self._doc(limits=[("weekly_all", "", 40, 3000)]), self.now)
        self.assertFalse(self.m.seat_parked("a"), "the API's word wins over a text-derived guess")

    def test_a_fable_scoped_limit_parks_the_fable_tier_only(self):
        self.m.apply_usage("a", self._doc(limits=[("weekly_all", "", 60, 3000),
                                                  ("weekly_scoped", "Fable", 100, 3000)]), self.now)
        self.assertTrue(self.m.model_parked("a", "fable"))
        self.assertFalse(self.m.model_parked("a", "opus"))
        self.assertTrue(self.m.seat_usable("a"))
        self.m.apply_usage("a", self._doc(limits=[("weekly_scoped", "Fable", 20, 3000)]), self.now)
        self.assertFalse(self.m.model_parked("a", "fable"), "and the API reopens it")

    def test_an_unknown_scope_changes_no_park_but_reaches_the_snapshot(self):
        self.m.apply_usage("a", self._doc(limits=[("weekly_scoped", "Haiku", 100, 3000),
                                                  ("session", "", 5, None)]), self.now)
        self.assertTrue(self.m.seat_usable("a"))
        self.assertFalse(any(self.m.model_parked("a", mdl) for mdl in LADDER))
        self.assertEqual({"weekly_haiku": (100.0, self.now + 3000), "session": (5.0, None)},
                         self.m.USAGE_SNAPSHOT["a"]["limits"])

    def test_parks_from_the_api_are_clamped_so_a_dead_poller_cannot_wedge_a_week(self):
        self.m.apply_usage("a", self._doc(limits=[("weekly_all", "", 100, 7 * 86400)]), self.now)
        self.assertLessEqual(self.m.SEAT_PARKED_UNTIL["a"] - self.now, self.m.MAX_PARK_S + 1)

    def test_a_failed_probe_keeps_parks_and_the_last_known_windows(self):
        self.m.apply_usage("a", self._doc(limits=[("weekly_all", "", 100, 3000)]), self.now)
        self.m.apply_usage("a", self._doc(ok=False), self.now + 10)
        self.assertTrue(self.m.seat_parked("a"))
        snap = self.m.USAGE_SNAPSHOT["a"]
        self.assertFalse(snap["ok"])
        self.assertEqual({"weekly_all": (100.0, self.now + 3000)}, snap["limits"])
        self.assertEqual("acct-1", snap["account"]["uuid"], "identity is not forgotten on a 401")

    def test_the_poll_climbs_back_to_fable_on_a_better_seat_but_never_moves_within_a_tier(self):
        self.m.use_seat("c")
        self.m.CURRENT_MODEL = "opus"        # start there without counting it as a switch
        self._probe({"a": self._doc(limits=[("weekly_all", "", 50, 3000)]),
                     "b": self._doc(uuid="acct-2", limits=[("weekly_all", "", 50, 3000)]),
                     "c": self._doc(uuid="acct-3", limits=[("weekly_scoped", "Fable", 100, 3000)])})
        self.m.poll_usage(self.now)
        self.assertEqual(("a", "fable"), (self.m.CURRENT_SEAT, self.m.CURRENT_MODEL))
        c = self.m.db()
        switches = c.execute("SELECT v FROM meta WHERE k='llm_model_switches_total'").fetchone()
        c.close()
        self.assertEqual(1.0, float(switches[0]))
        self.m.use_seat("b")                                   # sticky within the tier:
        self.m.poll_usage(self.now + 60)
        self.assertEqual(("b", "fable"), (self.m.CURRENT_SEAT, self.m.CURRENT_MODEL),
                         "seat a is free on fable too, but the poll never moves within a tier")

    def test_the_poll_refreshes_the_global_wall(self):
        self._probe({s: self._doc(uuid=f"acct-{s}", limits=[("weekly_all", "", 100, 3000)])
                     for s in "abc"})
        self.m.poll_usage(self.now)
        self.assertEqual(0, self.m.seats_available())
        self.assertGreater(self.m.RATE_LIMITED_UNTIL, self.now)

    # ---- codex review of PR 2 ---------------------------------------------------------------
    def test_a_deferral_wakes_within_the_default_park_even_when_the_wall_is_hours_away(self):
        """A job deferred to a six-hour wall kept that next_at after the watchdog unparked the
        seat an hour later. The worker gate (RATE_LIMITED_UNTIL) already holds work while every
        seat is parked, so a deferral only needs to wake within DEFAULT_PARK_S and let the gate
        decide - an unpark then takes effect within 15 minutes, not hours."""
        head = "a" * 40
        self.m.enqueue("o/r", 1, head, "webhook")
        for s in "abc":
            self.m.park(self.now + 6 * 3600, seat=s)

        def refuse(*a):
            raise self.m.RateLimited("You've hit your weekly limit", self.now + 6 * 3600)
        self.m.review_job = refuse
        self.m.RATE_LIMITED_UNTIL = 0.0                 # let worker_once claim the job
        self.m.worker_once()
        c = self.m.db()
        state, next_at, attempts = c.execute(
            "SELECT state,next_at,attempts FROM jobs WHERE head_sha=?", (head,)).fetchone()
        c.close()
        self.assertEqual(("retry", 0), (state, attempts))
        self.assertLessEqual(next_at, self.now + self.m.DEFAULT_PARK_S + 5)

    def test_apply_usage_refreshes_the_global_wall_under_the_park_lock(self):
        """Unparking must publish a fresh wall itself, atomically with the table it changed:
        a worker computing a stale wall from the old tables must not be able to re-publish
        it over the unpark."""
        self.assertTrue(hasattr(self.m.park_lock, "acquire"))
        for s in "abc":
            self.m.park(self.now + 6 * 3600, seat=s)
        self.assertGreater(self.m.RATE_LIMITED_UNTIL, self.now)
        self.m.apply_usage("b", self._doc(limits=[("weekly_all", "", 40, 3000)]), self.now)
        self.assertEqual(0.0, self.m.RATE_LIMITED_UNTIL, "one usable seat = the wall is down")

    def test_a_watchdog_park_counts_once_and_an_extension_does_not(self):
        """ReviewbotSeatNeverServes needs increase(seat_parks_total) > 0: a seat the API keeps
        parked must be countable as parked, but re-extending it hourly must not read as a
        seat that 'keeps running out'."""
        self.m.apply_usage("a", self._doc(limits=[("weekly_all", "", 100, 3000)]), self.now)
        self.m.apply_usage("a", self._doc(limits=[("weekly_all", "", 100, 3500)]), self.now + 60)
        self.m.apply_usage("a", self._doc(limits=[("weekly_scoped", "Fable", 100, 3000)]), self.now)
        c = self.m.db()
        rows = dict(c.execute("SELECT k,v FROM meta WHERE k LIKE '%parks_total%'"))
        c.close()
        self.assertEqual(1.0, float(rows.get("seat_parks_total.a") or 0), rows)
        self.assertEqual(1.0, float(rows.get("model_parks_total.a.fable") or 0), rows)

    def test_a_scope_that_matches_several_tiers_parks_all_of_them(self):
        """Two configured names for one model share one window; parking only the first
        substring match would leave the other eligible for the same refusal."""
        m = _ladder_module(self.tmp.name, llm_models=["claude-opus-5", "opus", "sonnet"],
                           usage_poll_s=3600)
        m.apply_usage("a", {"ok": True, "error": "", "account": {"uuid": "u"},
                            "limits": [{"kind": "weekly_scoped", "model": "Opus", "percent": 100.0,
                                        "resets_at": self.now + 3000, "active": False}]}, self.now)
        self.assertTrue(m.model_parked("a", "claude-opus-5"))
        self.assertTrue(m.model_parked("a", "opus"))
        self.assertFalse(m.model_parked("a", "sonnet"))

    def test_the_poll_does_not_clear_a_park_set_because_the_cli_cannot_serve_the_model(self):
        """reviewer-claude on ailab#780: the API's scoped window says nothing about whether the
        installed CLI resolves the alias. Clearing the pair on `Fable 20%` re-armed a broken
        alias every hour, and each poll then cost up to len(SEATS) doomed 404 calls before the
        ladder re-parked and descended. A limit park and an unserveable park are different
        facts and live in different tables; the poll owns only the first."""
        self.m.park_model("a", "fable", self.now + self.m.MAX_PARK_S, unserveable=True)
        self.m.park_model("b", "fable", self.now + 900)
        for s in "ab":
            self.m.apply_usage(s, self._doc(uuid=f"acct-{s}",
                                            limits=[("weekly_scoped", "Fable", 20, 3000)]), self.now)
        self.assertTrue(self.m.model_parked("a", "fable"), "unserveable: the API cannot vouch for it")
        self.assertFalse(self.m.model_parked("b", "fable"), "a limit park is the API's to clear")
        self.assertEqual(("b", "fable"), self.m.active_choice(self.now),
                         "the ladder routes around the unserveable pair on the same tier")

    def test_a_zero_interval_starts_nothing(self):
        off = _ladder_module(self.tmp.name, usage_poll_s=0)
        off.time = type("T", (), {"sleep": staticmethod(lambda n: (_ for _ in ()).throw(
            AssertionError("the ticker must not sleep when disabled"))),
            "time": staticmethod(real_time.time), "monotonic": staticmethod(real_time.monotonic),
            "gmtime": staticmethod(real_time.gmtime), "strftime": staticmethod(real_time.strftime)})()
        off.usage_ticker()                                    # returns, no loop
        self.assertFalse(off.usage_poller_enabled())
        self.assertTrue(self.m.usage_poller_enabled())


class ParseResetDateTest(unittest.TestCase):
    """The weekly refusal names a DATE - `resets Sep 20, 11pm (UTC)` - which the time-only
    pattern never matched, so every such park was the 15-minute default (measured on all three
    seats, 2026-09-18)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)

    def test_the_dated_form_is_parsed_to_the_named_day_and_hour(self):
        for text, expect in (("You've hit your weekly limit · resets Sep 20, 11pm (UTC)", (9, 20, 23, 0)),
                             ("resets Sep 19, 8pm (UTC)", (9, 19, 20, 0)),
                             ("resets Sep 20, 5am (UTC)", (9, 20, 5, 0)),
                             ("resets Oct 3, 12:30am (UTC)", (10, 3, 0, 30))):
            with self.subTest(text=text):
                at = self.m.parse_reset(text)
                self.assertIsNotNone(at)
                self.assertEqual(expect, real_time.gmtime(at)[1:5])

    def test_a_dated_reset_already_past_rolls_to_next_year(self):
        now = real_time.gmtime()
        past_month = "Jan" if now.tm_mon > 1 else "Dec"
        at = self.m.parse_reset(f"resets {past_month} 2, 3am (UTC)")
        self.assertGreater(at, real_time.time())

    def test_the_time_only_forms_still_parse(self):
        self.assertEqual((16, 20), real_time.gmtime(self.m.parse_reset("resets 4:20pm (UTC)"))[3:5])
        self.assertIsNone(self.m.parse_reset("resets never"))

    def test_a_message_naming_both_forms_takes_the_earlier_reset(self):
        """codex review of PR 2: a message that names a session reset (hours) and a weekly one
        (days) must not park to the later one - a park that lapses re-probes for free, a park
        that overshoots idles the persona."""
        text = "session resets 11pm (UTC); weekly resets Jan 2, 12pm (UTC)"
        at = self.m.parse_reset(text)
        self.assertEqual(23, real_time.gmtime(at)[3])
        self.assertLess(at, real_time.time() + 86400 + 60)


class LadderMetricsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = _ladder_module(self.tmp.name, usage_poll_s=3600)
        self.addCleanup(setattr, self.m, "RATE_LIMITED_UNTIL", 0.0)

    def _emit(self):
        self.m.write_metrics()
        lines = pathlib.Path(self.m.CFG["textfile"]).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), len(set(l.split(" ")[0] for l in lines)),
                         "a duplicate series makes node_exporter drop the WHOLE textfile")
        return dict(l.split(" ", 1) for l in lines)

    def test_the_ladder_series_exist_with_zeros_for_every_pair(self):
        got = self._emit()
        self.assertEqual("1", got['reviewbot_llm_active_model_info{persona="test",model="fable"}'])
        for s in "abc":
            for mdl in LADDER:
                self.assertEqual("0", got[f'reviewbot_llm_model_parked{{persona="test",seat="{s}",model="{mdl}"}}'])
                self.assertIn(f'reviewbot_llm_model_parks_total{{persona="test",seat="{s}",model="{mdl}"}}', got)
        self.assertIn('reviewbot_llm_model_switches_total{persona="test"}', got)
        self.assertNotIn('reviewbot_llm_usage_probe_ok{persona="test",seat="a"}', got,
                         "no probe has run: no probe series, so the alert cannot fire on nothing")

    def test_a_park_and_a_probe_show_up(self):
        now = real_time.time()
        self.m.park_model("b", "fable", now + 500)
        self.m.apply_usage("a", {"ok": True, "error": "", "account": {"uuid": "u", "email": 'x"y@example.test', "plan": "max"},
                                 "limits": [{"kind": "weekly_all", "model": "", "percent": 42.0, "resets_at": now + 100, "active": False},
                                            {"kind": "weekly_scoped", "model": "Fable", "percent": 100.0, "resets_at": now + 100, "active": False},
                                            {"kind": "session", "model": "", "percent": 3.0, "resets_at": None, "active": False}]}, now)
        got = self._emit()
        self.assertEqual("1", got['reviewbot_llm_model_parked{persona="test",seat="b",model="fable"}'])
        self.assertEqual("1", got['reviewbot_llm_usage_probe_ok{persona="test",seat="a"}'])
        self.assertEqual("1", got['reviewbot_llm_seat_info{persona="test",seat="a",email="x\\"y@example.test",plan="max"}'])
        self.assertEqual("42", got['reviewbot_llm_usage_percent{persona="test",seat="a",limit="weekly_all"}'])
        self.assertEqual("100", got['reviewbot_llm_usage_percent{persona="test",seat="a",limit="weekly_fable"}'])
        self.assertEqual("0", got['reviewbot_llm_usage_resets_at_seconds{persona="test",seat="a",limit="session"}'])
        self.assertEqual(f"{now + 100:.0f}", got['reviewbot_llm_usage_resets_at_seconds{persona="test",seat="a",limit="weekly_all"}'])
        self.assertEqual("3", got['reviewbot_llm_seats_available{persona="test"}'],
                         "a and b are parked on fable only - still usable on opus - and c is free")


if __name__ == "__main__":
    unittest.main()
