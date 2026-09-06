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
import pathlib
import sqlite3
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

    def test_a_git_quoted_path_is_unnameable_not_missparsed(self):
        """Git quotes any path with a non-ASCII byte and writes C/octal escapes, putting the
        a//b/ prefix INSIDE the quotes — so a naive strip yields a wrong name. The docstring
        promised such a section is excluded; now it actually is."""
        raw = (b'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
               b'--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n@@ -0,0 +1 @@\n+x\n')
        sec = self.m.split_sections(raw)[0]
        self.assertIsNone(sec["b"])
        self.assertEqual("unparsable path", self.m.section_reason(sec))

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

    def test_binary_patch_marker(self):
        self.assertEqual("binary", self._reason("x.dat", b"\nBinary files a/x.dat and b/x.dat differ\n"))

    def test_non_utf8_quarantines_one_file(self):
        """platform#1074 carried a 0xf6 byte that failed EVERY attempt for 22h. Naming the one
        unreadable file beats feeding the model replacement characters for the whole PR."""
        self.assertEqual("non-utf8", self._reason("weird.py", b"+caf\xf6\n"))

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
        # A policy exclusion does not downgrade the verdict — only a capacity drop does.
        self.assertIn(f"head={head} verdict=clean", body)

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


if __name__ == "__main__":
    unittest.main()
