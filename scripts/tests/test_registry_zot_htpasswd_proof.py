"""Tests for ansible/roles/registry_zot/files/zot-htpasswd-proof.py and the role tasks that call it.

An htpasswd change (a new scoped push user, a rotated password, a removal) must reach zot in place, never by a
restart: a restart re-parses the whole store (2026-11-17: a 19 min registry outage). zot v2.1.23's HTPasswdWatcher
reloads the file and logs "loaded htpasswd file" with the user count. At the end of every apply the helper proves the
running zot loaded the file as it is now (forcing a reload of unchanged content, so an earlier unproven write is
re-proven), and the role's logins check the credentials. These tests do not run zot; the live behaviour was measured
on the v2.1.23 binary.
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ROLE_DIR = REPO_ROOT / "ansible" / "roles" / "registry_zot"
HELPER = ROLE_DIR / "files" / "zot-htpasswd-proof.py"

_spec = importlib.util.spec_from_file_location("zot_htpasswd_proof", HELPER)
zhp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(zhp)

T0 = "2026-11-26T20:11:27.632934873Z"
T0_NS = zhp.parse_time_ns(T0)
FUTURE = "2099-01-01T00:00:00.000000001Z"  # later than any mtime a test file gets, including a forced rewrite


def rec(message, users=None, time=T0):
    r = {"message": message, "time": time, "htpasswd-file": "/etc/zot/htpasswd"}
    if users is not None:
        r["users"] = users
    return r


class ParseTimeTest(unittest.TestCase):
    def test_nanosecond_fraction_and_zulu(self):
        self.assertEqual(T0_NS % 1_000_000_000, 632934873)
        self.assertEqual(zhp.parse_time_ns("2026-11-26T20:11:27Z") // 1_000_000_000, T0_NS // 1_000_000_000)

    def test_offsets_are_honoured(self):
        self.assertEqual(zhp.parse_time_ns("2026-11-26T22:11:27.632934873+02:00"), T0_NS)
        self.assertEqual(zhp.parse_time_ns("2026-11-26T15:11:27.632934873-05:00"), T0_NS)

    def test_short_fraction_is_scaled(self):
        self.assertEqual(zhp.parse_time_ns("2026-11-26T20:11:27.5Z") % 1_000_000_000, 500_000_000)

    def test_garbage_is_none(self):
        for v in (None, "", "yesterday", "2026-11-26", "2026-11-26T20:11:27", "2026-11-26T20:11:27.x1Z", 17):
            self.assertIsNone(zhp.parse_time_ns(v), v)


class CountUsersTest(unittest.TestCase):
    def test_counts_distinct_names_like_zot(self):
        text = "ci:$2y$x\npull:$2y$y\n\nnot-a-line\nci:$2y$z\nspecpulse-ci:$2y$w\n"
        self.assertEqual(zhp.count_users(text), 3)
        self.assertEqual(zhp.count_users(""), 0)


class VerdictTest(unittest.TestCase):
    def test_loaded_after_the_write_with_the_count_is_proof(self):
        state, detail = zhp.verdict([rec(zhp.MSG_LOADED, 7)], 7, T0_NS - 1)
        self.assertEqual((state, detail["users"]), ("reloaded", 7))

    def test_same_instant_counts(self):
        self.assertEqual(zhp.verdict([rec(zhp.MSG_LOADED, 7)], 7, T0_NS)[0], "reloaded")

    def test_a_reload_before_the_last_write_is_not_proof(self):
        self.assertEqual(zhp.verdict([rec(zhp.MSG_LOADED, 7)], 7, T0_NS + 1)[0], "pending")

    def test_a_reload_with_another_count_is_not_proof(self):
        self.assertEqual(zhp.verdict([rec(zhp.MSG_LOADED, 6)], 7, T0_NS - 1)[0], "pending")

    def test_a_failed_reload_after_the_write_fails(self):
        self.assertEqual(zhp.verdict([rec(zhp.MSG_FAILED)], 7, T0_NS - 1)[0], "failed")

    def test_a_later_good_reload_wins_over_a_failed_one(self):
        later = "2026-11-26T20:11:28.000000000Z"
        self.assertEqual(zhp.verdict([rec(zhp.MSG_FAILED), rec(zhp.MSG_LOADED, 7, later)], 7, T0_NS - 1)[0],
                         "reloaded")

    def test_the_latest_outcome_decides(self):
        later = "2026-11-26T20:11:28.000000000Z"
        self.assertEqual(zhp.verdict([rec(zhp.MSG_LOADED, 7), rec(zhp.MSG_FAILED, time=later)], 7, T0_NS - 1)[0],
                         "failed")
        state, detail = zhp.verdict([rec(zhp.MSG_LOADED, 7), rec(zhp.MSG_LOADED, 6, later)], 7, T0_NS - 1)
        self.assertEqual((state, detail), ("pending", {"lastLoadedUsers": 6}))

    def test_unrelated_messages_do_not_hide_the_outcome(self):
        later = "2026-11-26T20:11:28.000000000Z"
        records = [rec(zhp.MSG_LOADED, 7), {"message": "some other zot line", "time": later}]
        self.assertEqual(zhp.verdict(records, 7, T0_NS - 1)[0], "reloaded")

    def test_records_without_a_time_are_ignored(self):
        self.assertEqual(zhp.verdict([{"message": zhp.MSG_LOADED, "users": 7}], 7, 0)[0], "pending")


class JournalTest(unittest.TestCase):
    def _argv(self, cursor, since_ns):
        with mock.patch.object(zhp, "_run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=json.dumps(rec(zhp.MSG_LOADED, 1)) + "\nnot json\n")
            out = zhp.journal_records("zot.service", cursor, since_ns)
        return run.call_args[0], out

    def test_after_the_cursor_when_there_is_one(self):
        argv, out = self._argv("s=1;i=2", 5_000_000_000)
        self.assertIn("--after-cursor=s=1;i=2", argv)
        self.assertEqual(len(out), 1)

    def test_since_the_snapshot_time_without_a_cursor(self):
        argv, _ = self._argv("", 5_000_000_000)
        self.assertIn("--since=@5", argv)
        self.assertFalse(any(a.startswith("--after-cursor") for a in argv))


class ProveTest(unittest.TestCase):
    CONTENT = b"ci:$2y$a\npull:$2y$b\nspecpulse-ci:$2y$c\n"

    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        os.write(fd, self.CONTENT)
        os.close(fd)
        os.utime(self.path, ns=(T0_NS - 1000, T0_NS - 1000))
        self.digest = zhp.file_sha256(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def _prove(self, records, pids, timeout=0, before="", cursor="c1", pid_before=4242):
        clock = iter(range(0, 1000))
        journal = records if callable(records) else (lambda unit, cur, since: records)
        with mock.patch.object(zhp, "journal_records", side_effect=journal), \
             mock.patch.object(zhp, "main_pid", side_effect=pids):
            return zhp.prove("zot.service", self.path, pid_before, cursor, 1, before, timeout,
                             sleep=lambda s: None, clock=lambda: next(clock))

    def test_a_changed_file_needs_its_reload(self):
        rc, out = self._prove([rec(zhp.MSG_LOADED, 3)], [4242, 4242], before="0" * 64)
        self.assertEqual((rc, out["mode"], out["pid"]), (0, "reloaded", 4242))

    def test_a_changed_file_without_its_reload_times_out(self):
        rc, out = self._prove([], [4242] * 10, timeout=2, before="0" * 64)
        self.assertEqual((rc, out["mode"]), (2, "unproven"))
        self.assertIn("3 users", out["reason"])

    def test_unchanged_content_is_rewritten_identically_and_its_reload_proven(self):
        # e.g. an earlier run removed a user and its reload went unproven: the file is already final, so the helper
        # makes zot reload it now and proves that reload.
        rc, out = self._prove([rec(zhp.MSG_LOADED, 3, FUTURE)], [4242, 4242], before=self.digest)
        self.assertEqual((rc, out["mode"]), (0, "reloaded-forced"))
        self.assertEqual(Path(self.path).read_bytes(), self.CONTENT)  # same bytes
        self.assertGreater(os.stat(self.path).st_mtime_ns, T0_NS)  # but a fresh write zot sees

    def test_a_forced_reload_must_come_after_the_rewrite(self):
        # the load logged before the rewrite (T0) does not count
        rc, out = self._prove([rec(zhp.MSG_LOADED, 3)], [4242] * 10, timeout=2, before=self.digest)
        self.assertEqual(rc, 2)

    def test_a_new_process_is_accepted_at_once_without_the_journal(self):
        journal = mock.Mock(side_effect=AssertionError("must not read the journal"))
        rc, out = self._prove(journal, [5150], before=self.digest)
        self.assertEqual((rc, out["mode"], out["pid"], out["pidBefore"]), (0, "restarted", 5150, 4242))
        self.assertEqual(Path(self.path).read_bytes(), self.CONTENT)
        os.utime(self.path, ns=(T0_NS - 1000, T0_NS - 1000))

    def test_a_first_start_is_a_new_process(self):
        rc, out = self._prove([], [5150], pid_before=0, cursor="")
        self.assertEqual((rc, out["mode"]), (0, "restarted"))

    def test_zot_not_running_is_unproven(self):
        rc, out = self._prove([], [0])
        self.assertEqual(rc, 2)
        self.assertIn("not running", out["reason"])

    def test_a_pid_change_while_waiting_is_unproven(self):
        rc, out = self._prove([], [4242, 4243], timeout=5, before="0" * 64)
        self.assertEqual(rc, 2)
        self.assertIn("MainPID changed", out["reason"])

    def test_a_failed_reload_waits_for_a_later_success(self):
        later = "2026-11-26T20:11:29.000000000Z"
        calls = {"n": 0}

        def journal(unit, cursor, since):
            calls["n"] += 1
            return [rec(zhp.MSG_FAILED)] + ([rec(zhp.MSG_LOADED, 3, later)] if calls["n"] >= 3 else [])

        rc, out = self._prove(journal, [4242] * 10, timeout=60, before="0" * 64)
        self.assertEqual((rc, out["mode"]), (0, "reloaded"))
        self.assertGreaterEqual(calls["n"], 3)

    def test_a_failed_reload_with_no_later_success_fails_at_the_deadline(self):
        rc, out = self._prove([rec(zhp.MSG_FAILED)], [4242] * 10, timeout=3, before="0" * 64)
        self.assertEqual(rc, 2)
        self.assertIn("failed htpasswd reload", out["reason"])

    def test_no_journal_position_is_unproven(self):
        with mock.patch.object(zhp, "main_pid", return_value=4242):
            self.assertEqual(zhp.prove("zot.service", self.path, 4242, "", 0, "", 1)[0], 2)

    def test_output_never_names_a_user_or_a_digest(self):
        cases = (([rec(zhp.MSG_LOADED, 3)], [4242, 4242], "0" * 64), ([], [4242] * 5, "0" * 64),
                 ([rec(zhp.MSG_LOADED, 3, FUTURE)], [4242, 4242], self.digest), ([], [5150], self.digest))
        for records, pids, before in cases:
            _, out = self._prove(records, pids, timeout=1, before=before)
            for secret in ("specpulse-ci", "$2y$", self.digest):
                self.assertNotIn(secret, json.dumps(out))

    def test_snapshot_cli_reports_pid_cursor_time_and_digest(self):
        with mock.patch.object(zhp, "main_pid", return_value=4242), \
             mock.patch.object(zhp, "journal_cursor", return_value="s=abc;i=1"), \
             mock.patch("builtins.print") as printed:
            self.assertEqual(zhp.main(["snapshot", "--unit", "zot.service", "--file", self.path]), 0)
        out = json.loads(printed.call_args[0][0])
        self.assertEqual({k: out[k] for k in ("pid", "cursor", "sha256")},
                         {"pid": 4242, "cursor": "s=abc;i=1", "sha256": self.digest})
        self.assertIsInstance(out["timeNs"], int)

    def test_a_missing_file_has_no_digest(self):
        self.assertIsNone(zhp.file_sha256(self.path + ".absent"))


def _flatten(tasks):
    for task in tasks:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _flatten(task.get(key) or [])


HTPASSWD_TASKS = (
    "Generate bcrypt htpasswd for the push user",
    "Generate bcrypt htpasswd for the read-only pull user",
    "Remove the htpasswd line of an optional scoped push user with no SOPS password",
    "Generate bcrypt htpasswd for the scoped per-project push users",
)
SNAPSHOT = "Snapshot zot before the htpasswd changes (MainPID and journal cursor)"
VERIFY = "Verify the running zot serves the current htpasswd (reloaded in place, never restarted for it)"
PROVE = "Prove the running zot loaded the current htpasswd file"
LOGIN_CI = "The ci and pull users log in on the running zot"
LOGIN_SCOPED = "Every scoped push user with a SOPS password logs in on the running zot"
RESTART = "Restart zot because its htpasswd state was not proven and a restart is allowed"
STOP = "Stop because the htpasswd state was not proven (no restart without registry_zot_config_allow_restart)"
FLUSH = "Flush handlers so the unit is registered before enable/start"
START = "Enable + start zot"


class TaskWiringTest(unittest.TestCase):
    def setUp(self):
        self.raw = yaml.safe_load((ROLE_DIR / "tasks" / "main.yml").read_text(encoding="utf-8"))
        tasks = list(_flatten(self.raw))
        self.names = [t["name"] for t in tasks]
        self.tasks = {t["name"]: t for t in tasks}
        self.verify = next(t for t in self.raw if t["name"] == VERIFY)

    def test_no_htpasswd_task_restarts_zot(self):
        for name in HTPASSWD_TASKS:
            self.assertNotIn("notify", self.tasks[name], name)
        writers = [t["name"] for t in self.tasks.values() if "community.general.htpasswd" in t]
        self.assertEqual(sorted(writers), sorted(HTPASSWD_TASKS))

    def test_verify_comes_after_the_handler_flush_and_the_start(self):
        # so any restart another task queued (binary, certs, unit, sync credentials, config) has already happened
        order = [SNAPSHOT, *HTPASSWD_TASKS, FLUSH, START, VERIFY, PROVE, LOGIN_CI, LOGIN_SCOPED, RESTART, STOP]
        idx = [self.names.index(n) for n in order]
        self.assertEqual(idx, sorted(idx))
        self.assertEqual(self.names.index(VERIFY), self.names.index(START) + 1)

    def test_snapshot_records_the_state_in_check_mode_too(self):
        t = self.tasks[SNAPSHOT]
        self.assertIs(t["check_mode"], False)
        self.assertIs(t["changed_when"], False)
        argv = t["ansible.builtin.command"]["argv"]
        self.assertEqual(argv[2:5], ["snapshot", "--unit", "zot.service"])
        self.assertEqual(argv[argv.index("--file") + 1], "{{ registry_zot_config_dir }}/htpasswd")

    def test_verify_runs_on_every_real_apply(self):
        # never gated on "this run changed the file": an earlier unproven write (a removal) must still be caught
        self.assertEqual(self.verify["when"], "not ansible_check_mode")
        self.assertEqual([t["name"] for t in self.verify["block"]], [PROVE, LOGIN_CI, LOGIN_SCOPED])
        self.assertEqual([t["name"] for t in self.verify["rescue"]], [RESTART, STOP])

    def test_prove_passes_the_whole_snapshot(self):
        t = self.tasks[PROVE]
        self.assertNotIn("failed_when", t)  # rc != 0 fails the task; the rescue decides
        self.assertNotIn("notify", t)
        self.assertIn("reloaded-forced", t["changed_when"])
        argv = t["ansible.builtin.command"]["argv"]
        for flag, key in (("--pid", ".pid"), ("--cursor", ".cursor"), ("--since-ns", ".timeNs"),
                          ("--before-sha256", ".sha256")):
            self.assertIn("registry_zot_htpasswd_snapshot.stdout | from_json", argv[argv.index(flag) + 1], flag)
            self.assertIn(key, argv[argv.index(flag) + 1], flag)

    def test_logins_are_secret_bounded_and_skipped_for_a_restarting_zot(self):
        for name in (LOGIN_CI, LOGIN_SCOPED):
            t = self.tasks[name]
            uri = t["ansible.builtin.uri"]
            self.assertIs(t["no_log"], True, name)
            self.assertEqual(uri["status_code"], 200)
            self.assertIs(uri["force_basic_auth"], True)
            self.assertEqual(uri["timeout"], "{{ registry_zot_htpasswd_login_timeout }}")
            self.assertEqual(t["retries"], "{{ registry_zot_htpasswd_login_retries }}")
            self.assertIn("== 200", t["until"])
            when = t["when"] if isinstance(t["when"], list) else [t["when"]]
            self.assertIn("(registry_zot_htpasswd_proof.stdout | from_json).mode != 'restarted'", when)
        self.assertEqual(self.tasks[LOGIN_SCOPED]["loop"], "{{ registry_zot_scoped_push_users }}")
        self.assertIn("item.user in (registry_scoped_push_passwords | default({}))", self.tasks[LOGIN_SCOPED]["when"])

    def test_rescue_restarts_only_when_allowed_and_otherwise_fails(self):
        restart, stop = self.tasks[RESTART], self.tasks[STOP]
        self.assertEqual(restart["notify"], "restart zot")
        self.assertEqual(restart["when"], "registry_zot_config_allow_restart | bool")
        self.assertIn("ansible.builtin.fail", stop)
        self.assertEqual(stop["when"], "not (registry_zot_config_allow_restart | bool)")
        self.assertNotIn("notify", stop)

    def test_defaults(self):
        defaults = yaml.safe_load((ROLE_DIR / "defaults" / "main.yml").read_text(encoding="utf-8"))
        self.assertEqual(defaults["registry_zot_htpasswd_proof_timeout"], 30)
        self.assertEqual(defaults["registry_zot_htpasswd_login_timeout"], 5)
        self.assertEqual(defaults["registry_zot_htpasswd_login_retries"], 5)


if __name__ == "__main__":
    unittest.main()
