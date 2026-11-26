"""Tests for ansible/roles/registry_zot/files/zot-htpasswd-proof.py and the role tasks that call it.

An htpasswd change (a new scoped push user, a rotated password) must reload zot in place, never restart it: a restart
re-parses the whole store (2026-11-17: a 19 min registry outage). zot v2.1.23's HTPasswdWatcher reloads the file and
logs "loaded htpasswd file" with the user count; the helper proves that line came after the last write, with the file's
user count, from the same MainPID. These tests do not run zot (the live behaviour was measured on the v2.1.23 binary).
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
        self.assertEqual(state, "reloaded")
        self.assertEqual(detail["users"], 7)

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
        state, _ = zhp.verdict([rec(zhp.MSG_FAILED), rec(zhp.MSG_LOADED, 7, later)], 7, T0_NS - 1)
        self.assertEqual(state, "reloaded")

    def test_records_without_a_time_are_ignored(self):
        self.assertEqual(zhp.verdict([{"message": zhp.MSG_LOADED, "users": 7}], 7, 0)[0], "pending")


class ProveTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        os.write(fd, b"ci:$2y$a\npull:$2y$b\nspecpulse-ci:$2y$c\n")
        os.close(fd)
        os.utime(self.path, ns=(T0_NS - 1000, T0_NS - 1000))

    def tearDown(self):
        os.unlink(self.path)

    def _prove(self, records, pids, timeout=0):
        clock = iter(range(0, 1000))
        with mock.patch.object(zhp, "journal_records", return_value=records), \
             mock.patch.object(zhp, "main_pid", side_effect=pids):
            return zhp.prove("zot.service", self.path, 4242, "c1", timeout, sleep=lambda s: None,
                             clock=lambda: next(clock))

    def test_reloaded(self):
        rc, out = self._prove([rec(zhp.MSG_LOADED, 3)], [4242])
        self.assertEqual((rc, out["mode"], out["pid"]), (0, "reloaded", 4242))

    def test_a_changed_pid_is_a_restart_not_a_reload(self):
        rc, out = self._prove([rec(zhp.MSG_LOADED, 3)], [4243])
        self.assertEqual((rc, out["mode"]), (2, "unproven"))
        self.assertIn("MainPID changed", out["reason"])

    def test_no_line_times_out(self):
        rc, out = self._prove([], [4242] * 10, timeout=2)
        self.assertEqual((rc, out["mode"]), (2, "unproven"))
        self.assertIn("3 users", out["reason"])

    def test_a_failed_reload_stops_at_once(self):
        rc, out = self._prove([rec(zhp.MSG_FAILED)], [4242], timeout=60)
        self.assertEqual(rc, 2)
        self.assertIn("failed htpasswd reload", out["reason"])

    def test_nothing_to_prove_without_a_running_zot_or_a_cursor(self):
        self.assertEqual(zhp.prove("zot.service", self.path, 0, "c1", 1)[0], 2)
        self.assertEqual(zhp.prove("zot.service", self.path, 4242, "", 1)[0], 2)

    def test_output_never_names_a_user(self):
        for records, pids in (([rec(zhp.MSG_LOADED, 3)], [4242]), ([], [4242] * 5)):
            _, out = self._prove(records, pids, timeout=1)
            self.assertNotIn("specpulse-ci", json.dumps(out))
            self.assertNotIn("$2y$", json.dumps(out))


def _flatten(tasks):
    for task in tasks:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _flatten(task.get(key) or [])


HTPASSWD_TASKS = {
    "Generate bcrypt htpasswd for the push user": "registry_zot_htpasswd_ci",
    "Generate bcrypt htpasswd for the read-only pull user": "registry_zot_htpasswd_pull",
    "Remove the htpasswd line of an optional scoped push user with no SOPS password": "registry_zot_htpasswd_removed",
    "Generate bcrypt htpasswd for the scoped per-project push users": "registry_zot_htpasswd_scoped",
}
SNAPSHOT = "Snapshot zot before the htpasswd changes (MainPID and journal cursor)"
NOTE = "Note whether this play changed the htpasswd file"
PROVE = "Prove the running zot reloaded the htpasswd file in place (no restart)"
RESTART = "Restart zot when its htpasswd reload was not proven and a restart is allowed"
LOGIN_CI = "The ci and pull users log in after the in-place htpasswd reload"
LOGIN_SCOPED = "Every scoped push user with a SOPS password logs in after the in-place htpasswd reload"


class TaskWiringTest(unittest.TestCase):
    def setUp(self):
        raw = yaml.safe_load((ROLE_DIR / "tasks" / "main.yml").read_text(encoding="utf-8"))
        tasks = list(_flatten(raw))
        self.names = [t["name"] for t in tasks]
        self.tasks = {t["name"]: t for t in tasks}

    def test_no_htpasswd_task_restarts_zot(self):
        for name, reg in HTPASSWD_TASKS.items():
            self.assertNotIn("notify", self.tasks[name], name)
            self.assertEqual(self.tasks[name]["register"], reg)
        for task in self.tasks.values():
            if "community.general.htpasswd" in task:
                self.assertIn(task["name"], HTPASSWD_TASKS)

    def test_order_snapshot_writes_note_prove_restart_logins_then_flush(self):
        order = [SNAPSHOT, *HTPASSWD_TASKS, NOTE, PROVE, RESTART, LOGIN_CI, LOGIN_SCOPED,
                 "Flush handlers so the unit is registered before enable/start", "Enable + start zot"]
        idx = [self.names.index(n) for n in order]
        self.assertEqual(idx, sorted(idx))

    def test_note_covers_every_htpasswd_task(self):
        expr = self.tasks[NOTE]["ansible.builtin.set_fact"]["registry_zot_htpasswd_changed"]
        for reg in HTPASSWD_TASKS.values():
            self.assertIn(f"{reg} is changed", expr)

    def test_snapshot_runs_in_check_mode_and_changes_nothing(self):
        t = self.tasks[SNAPSHOT]
        self.assertIs(t["check_mode"], False)
        self.assertIs(t["changed_when"], False)
        self.assertEqual(t["ansible.builtin.command"]["argv"][2:], ["snapshot", "--unit", "zot.service"])

    def test_prove_only_after_a_change_on_a_running_zot_outside_check_mode(self):
        t = self.tasks[PROVE]
        self.assertEqual(t["when"], ["registry_zot_htpasswd_changed | bool",
                                     "registry_zot_htpasswd_running_pid | int > 0", "not ansible_check_mode"])
        self.assertNotIn("notify", t)
        self.assertEqual(t["failed_when"],
                         "registry_zot_htpasswd_proof.rc != 0 and not (registry_zot_config_allow_restart | bool)")
        argv = t["ansible.builtin.command"]["argv"]
        self.assertEqual(argv[argv.index("--file") + 1], "{{ registry_zot_config_dir }}/htpasswd")

    def test_only_an_allowed_unproven_reload_restarts(self):
        t = self.tasks[RESTART]
        self.assertEqual(t["notify"], "restart zot")
        self.assertEqual(t["when"], ["registry_zot_htpasswd_proof is not skipped", "registry_zot_htpasswd_proof.rc != 0"])
        # without registry_zot_config_allow_restart the prove task already failed the play, so this never runs

    def test_logins_are_secret_and_follow_a_proven_reload(self):
        for name in (LOGIN_CI, LOGIN_SCOPED):
            t = self.tasks[name]
            self.assertIs(t["no_log"], True)
            self.assertEqual(t["when"][:2], ["registry_zot_htpasswd_proof is not skipped",
                                             "registry_zot_htpasswd_proof.rc == 0"])
            self.assertEqual(t["ansible.builtin.uri"]["status_code"], 200)
            self.assertIs(t["ansible.builtin.uri"]["force_basic_auth"], True)
        self.assertEqual(self.tasks[LOGIN_SCOPED]["loop"], "{{ registry_zot_scoped_push_users }}")
        self.assertIn("item.user in (registry_scoped_push_passwords", self.tasks[LOGIN_SCOPED]["when"][2])

    def test_timeout_default(self):
        defaults = yaml.safe_load((ROLE_DIR / "defaults" / "main.yml").read_text(encoding="utf-8"))
        self.assertEqual(defaults["registry_zot_htpasswd_proof_timeout"], 30)


if __name__ == "__main__":
    unittest.main()
