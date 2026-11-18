"""Tests for ansible/roles/registry_zot/files/zot-config-apply.py and the role tasks that call it.

The helper decides whether a rendered zot config can be HOT-RELOADED (same inode, zot v2.1.x fsnotify WRITE) or needs
a restart (19 min outage + ~265 GB written to ai-node1 on 2026-11-17). These tests pin the classification, the
inotify/journal parsing, the inode-preserving write, the dry run, and the task wiring. They do not run zot.
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
HELPER = ROLE_DIR / "files" / "zot-config-apply.py"

_spec = importlib.util.spec_from_file_location("zot_config_apply", HELPER)
zca = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(zca)

BASE = {
    "distSpecVersion": "1.1.1",
    "storage": {
        "rootDirectory": "/var/lib/registry/store",
        "gc": True,
        "gcDelay": "2h",
        "gcInterval": "1h",
        "dedupe": True,
        "retention": {"dryRun": False, "policies": [
            {"repositories": ["strive/**"], "deleteUntagged": True,
             "keepTags": [{"patterns": ["^sha-.*"], "mostRecentlyPushedCount": 100}]}]},
    },
    "http": {
        "address": "0.0.0.0",
        "port": "443",
        "auth": {"htpasswd": {"path": "/etc/zot/htpasswd"}},
        "accessControl": {"repositories": {"**": {"anonymousPolicy": ["read"]}}},
    },
    "extensions": {"sync": {"enable": True}},
}


def changed(**edits):
    cfg = json.loads(json.dumps(BASE))
    for path, value in edits.items():
        node = cfg
        keys = path.split("__")
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        if value is DELETE:
            node.pop(keys[-1], None)
        else:
            node[keys[-1]] = value
    return cfg


DELETE = object()


class DiffPathsTest(unittest.TestCase):
    def test_equal_is_empty(self):
        self.assertEqual(zca.diff_paths(BASE, json.loads(json.dumps(BASE))), [])

    def test_nested_scalar_and_added_key(self):
        new = changed(storage__gcDelay="1h", storage__newKey=1)
        self.assertEqual(zca.diff_paths(BASE, new), ["storage.gcDelay", "storage.newKey"])

    def test_lists_compare_whole(self):
        new = changed(storage__retention={"dryRun": False, "policies": [{"repositories": ["x/**"]}]})
        self.assertEqual(zca.diff_paths(BASE, new), ["storage.retention.policies"])


class ClassifyTest(unittest.TestCase):
    def assertMode(self, new, mode, current=BASE):
        self.assertEqual(zca.classify(current, new)[0], mode, zca.classify(current, new))

    def test_first_deploy_restarts(self):
        self.assertMode(BASE, "restart", current=None)

    def test_unchanged(self):
        self.assertMode(json.loads(json.dumps(BASE)), "none")

    def test_retention_change_reloads_while_retention_is_active(self):
        policies = BASE["storage"]["retention"]["policies"] + [{"repositories": ["a/**"], "deleteReferrers": True}]
        self.assertMode(changed(storage__retention__policies=policies), "reload")

    def test_pattern_only_retention_is_not_active(self):
        # zot v2.1.18 isTagsRetentionEnabled: only count/age keepTags rules turn retention (and its reload) on.
        pattern_only = changed(storage__retention={"policies": [{"repositories": ["x/**"], "deleteUntagged": True,
                                                                  "keepTags": [{"patterns": [".*"]}]}]})
        self.assertFalse(zca._retention_active(pattern_only))
        policies = pattern_only["storage"]["retention"]["policies"] + [{"repositories": ["y/**"]}]
        self.assertEqual(zca.classify(pattern_only, changed(storage__retention={"policies": policies}))[0], "restart")

    def test_retention_rules_in_subpaths_count(self):
        sub = changed(storage__retention={"policies": []},
                      storage__subPaths={"/a": {"retention": {"policies": [{"keepTags": [{"pushedWithin": "24h"}]}]}}})
        self.assertTrue(zca._retention_active(sub))

    def test_retention_needs_retention_on_both_sides(self):
        no_retention = changed(storage__retention=DELETE)
        self.assertMode(BASE, "restart", current=no_retention)  # turning retention ON
        self.assertMode(no_retention, "restart")  # turning it OFF

    def test_gc_timing_reloads(self):
        self.assertMode(changed(storage__gcDelay="1h"), "reload")
        self.assertMode(changed(storage__gcInterval="2h"), "reload")

    def test_access_control_rules_reload(self):
        repos = {"**": {"anonymousPolicy": ["read"]}, "strive/**": {"policies": []}}
        self.assertMode(changed(http__accessControl__repositories=repos), "reload")

    def test_access_control_added_or_removed_restarts(self):
        self.assertMode(changed(http__accessControl=DELETE), "restart")

    def test_everything_else_restarts(self):
        for edit in ({"http__port": "8443"}, {"storage__dedupe": False}, {"storage__gc": False},
                     {"extensions__sync": {"enable": False}}, {"http__auth__htpasswd__path": "/x"},
                     {"storage__rootDirectory": "/y"}):
            with self.subTest(edit=edit):
                self.assertMode(changed(**edit), "restart")

    def test_mixed_change_restarts(self):
        self.assertMode(changed(storage__gcDelay="1h", http__port="8443"), "restart")

    def test_prefix_is_a_path_component_not_a_string_prefix(self):
        self.assertMode(changed(storage__retentionX=True), "restart")


class InotifyTest(unittest.TestCase):
    # Real fdinfo captured from the registry LXC on 2026-11-18 (zot pid 8563, fd 3 = the config watch).
    SAMPLE = ("pos:\t0\nflags:\t02000000\nmnt_id:\t1234\n"
              "inotify wd:1 ino:c002a sdev:fc00016 mask:fc6 ignored_mask:0 fhandle-bytes:8 fhandle-type:1 "
              "f_handle:2a000c0056a5e393\n")

    def test_parse(self):
        self.assertEqual(zca.parse_inotify_fdinfo(self.SAMPLE), [(0xC002A, 0xFC00016)])

    def test_kernel_sdev(self):
        self.assertEqual(zca.kernel_sdev(os.makedev(0xFC, 0x16)), 0xFC00016)

    def test_watch_matches_exact_and_unique_inode_fallback(self):
        dev = os.makedev(0xFC, 0x16)
        live = [(0xC002A, 0xFC00016), (0x4037E, 0xFC00016), (0x4037C, 0xFC00016)]  # registry LXC, 2026-11-18
        self.assertTrue(zca.watch_matches(live, 0xC002A, dev))  # exact (ino, s_dev)
        other_dev = os.makedev(0x0, 0x2F)  # btrfs/overlay: st_dev is not the superblock s_dev
        self.assertTrue(zca.watch_matches(live, 0xC002A, other_dev))  # unique inode number -> still watched
        self.assertFalse(zca.watch_matches(live, 0xBEEF, dev))  # replaced file: new inode, not watched
        ambiguous = live + [(0xC002A, 0x1)]
        self.assertFalse(zca.watch_matches(ambiguous, 0xC002A, other_dev))  # two candidates: do not guess


class ReloadVerdictTest(unittest.TestCase):
    def test_verdicts(self):
        c, f, ld = zca.MSG_CHANGED, zca.MSG_FAILED, zca.MSG_LOADED
        self.assertEqual(zca.reload_verdict([]), "none")
        self.assertEqual(zca.reload_verdict(["unrelated", ld]), "none")
        self.assertEqual(zca.reload_verdict([c]), "pending")
        self.assertEqual(zca.reload_verdict([c, "noise", ld]), "ok")
        self.assertEqual(zca.reload_verdict([c, f]), "failed")
        self.assertEqual(zca.reload_verdict([c, f, c, ld]), "ok")  # a read between write and truncate, then good
        self.assertEqual(zca.reload_verdict([c, ld, c]), "pending")  # only the LAST attempt counts
        self.assertEqual(zca.reload_verdict([c, ld, c, f]), "failed")

    def test_live_sequence_from_2026_11_18(self):
        # The journal lines zot v2.1.18 printed after an identical in-place rewrite on the registry LXC.
        live = ["config file changed, trying to reload config",
                "checking if anonymous authorization is the only type of authorization policy configured",
                "config dist-spec version differs from version actually used",
                "loaded new configuration settings",
                "cve config not provided, skipping cve-db update"]
        self.assertEqual(zca.reload_verdict(live), "ok")


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "config.json")
        with open(self.path, "wb") as fh:
            fh.write(b'{"a": "0123456789"}')
        os.chmod(self.path, 0o640)

    def tearDown(self):
        self.dir.cleanup()

    def test_in_place_handles_short_writes(self):
        real = os.pwrite
        with mock.patch.object(zca.os, "pwrite", side_effect=lambda fd, data, off: real(fd, data[:3], off)):
            zca.write_in_place(self.path, b'{"short": "writes"}')
        self.assertEqual(Path(self.path).read_bytes(), b'{"short": "writes"}')

    def test_in_place_keeps_the_inode(self):
        ino = os.stat(self.path).st_ino
        for data in (b'{"a": 1}', b'{"a": "a much longer value than before"}'):
            zca.write_in_place(self.path, data)
            self.assertEqual(os.stat(self.path).st_ino, ino)
            self.assertEqual(Path(self.path).read_bytes(), data)

    def test_atomic_replaces_the_inode_and_keeps_the_mode(self):
        ino = os.stat(self.path).st_ino
        zca.write_atomic(self.path, b'{"b": 2}')
        self.assertNotEqual(os.stat(self.path).st_ino, ino)
        self.assertEqual(os.stat(self.path).st_mode & 0o7777, 0o640)
        self.assertEqual(Path(self.path).read_bytes(), b'{"b": 2}')


class MainTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.current = os.path.join(self.dir.name, "config.json")
        self.candidate = self.current + ".candidate"

    def tearDown(self):
        self.dir.cleanup()

    def _write(self, path, cfg):
        Path(path).write_text(json.dumps(cfg))

    def _run(self, *extra):
        with mock.patch("sys.stdout") as out:
            rc = zca.main(["--current", self.current, "--candidate", self.candidate, *extra])
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        return rc, json.loads(printed)

    def test_dry_run_writes_nothing_and_falls_back_to_restart_without_a_watch(self):
        self._write(self.current, BASE)
        self._write(self.candidate, changed(storage__gcDelay="1h"))
        before = Path(self.current).read_bytes()
        with mock.patch.object(zca, "main_pid", return_value=0):
            rc, result = self._run("--dry-run")
        self.assertEqual((rc, result["mode"], result["dry_run"]), (0, "restart", True))
        self.assertEqual(Path(self.current).read_bytes(), before)
        self.assertFalse(os.path.exists(self.candidate))

    def test_unchanged_is_a_no_op(self):
        self._write(self.current, BASE)
        self._write(self.candidate, BASE)
        rc, result = self._run()
        self.assertEqual((rc, result["mode"]), (0, "none"))
        self.assertFalse(os.path.exists(self.candidate))

    def test_first_deploy_writes_atomically_and_asks_for_a_restart(self):
        self._write(self.candidate, BASE)
        rc, result = self._run()
        self.assertEqual((rc, result["mode"]), (0, "restart"))
        self.assertEqual(json.loads(Path(self.current).read_text()), BASE)

    def test_reload_with_a_detached_watch_falls_back_to_restart(self):
        self._write(self.current, BASE)
        self._write(self.candidate, changed(storage__gcDelay="1h"))
        with mock.patch.object(zca, "main_pid", return_value=4242), \
                mock.patch.object(zca, "watch_alive", return_value=False):
            rc, result = self._run()
        self.assertEqual((rc, result["mode"]), (0, "restart"))
        self.assertEqual(json.loads(Path(self.current).read_text())["storage"]["gcDelay"], "1h")
        self.assertTrue(os.path.exists(self.current + ".unconfirmed"))  # cleared only by the restart handlers

    def test_restart_takes_owner_and_mode_from_the_candidate(self):
        self._write(self.current, BASE)
        os.chmod(self.current, 0o644)
        self._write(self.candidate, changed(http__port="8443"))
        os.chmod(self.candidate, 0o640)
        self._run()
        self.assertEqual(os.stat(self.current).st_mode & 0o7777, 0o640)

    def _failed_reload(self, candidate):
        self._write(self.candidate, candidate)
        with mock.patch.object(zca, "main_pid", return_value=4242), \
                mock.patch.object(zca, "watch_alive", return_value=True), \
                mock.patch.object(zca, "journal_cursor", return_value="c"), \
                mock.patch.object(zca, "journal_messages", return_value=[zca.MSG_CHANGED, zca.MSG_FAILED]), \
                mock.patch.object(zca.time, "sleep"):
            return self._run("--wait", "2", "--settle", "0")

    def test_unproven_reload_fails_restores_the_previous_bytes_and_keeps_a_marker(self):
        self._write(self.current, BASE)
        before = Path(self.current).read_bytes()
        rc, result = self._failed_reload(changed(storage__gcDelay="1h"))
        self.assertEqual((rc, result["mode"]), (2, "reload"))
        self.assertEqual(Path(self.current).read_bytes(), before)  # disk matches what zot still runs
        self.assertTrue(os.path.exists(self.current + ".unconfirmed"))

    def test_after_a_failed_reload_the_next_run_cannot_report_none(self):
        # Review finding on #1847: a rerun must not treat disk equality as convergence.
        self._write(self.current, BASE)
        new = changed(storage__gcDelay="1h")
        self._failed_reload(new)
        Path(self.current).write_text(json.dumps(new))  # even if the disk somehow already holds the candidate
        self._write(self.candidate, new)
        with mock.patch.object(zca, "main_pid", return_value=0):
            rc, result = self._run("--dry-run")
        self.assertEqual((rc, result["mode"]), (0, "restart"))  # no watch -> restart; never "none"
        with mock.patch.object(zca, "main_pid", return_value=4242), \
                mock.patch.object(zca, "watch_alive", return_value=True):
            self._write(self.candidate, new)
            rc, result = self._run("--dry-run")
        self.assertEqual((rc, result["mode"]), (0, "reload"))
        self.assertIn("not confirmed", result["reason"])

    def test_a_proven_reload_clears_the_marker(self):
        self._write(self.current, BASE)
        self._failed_reload(changed(storage__gcDelay="1h"))
        self._write(self.candidate, changed(storage__gcDelay="1h"))
        with mock.patch.object(zca, "main_pid", return_value=4242), \
                mock.patch.object(zca, "watch_alive", return_value=True), \
                mock.patch.object(zca, "journal_cursor", return_value="c"), \
                mock.patch.object(zca, "journal_messages", return_value=[zca.MSG_CHANGED, zca.MSG_LOADED]), \
                mock.patch.object(zca, "http_alive", return_value=True), \
                mock.patch.object(zca.time, "sleep"):
            rc, result = self._run("--wait", "2", "--settle", "0")
        self.assertEqual((rc, result["mode"]), (0, "reload"))
        self.assertFalse(os.path.exists(self.current + ".unconfirmed"))

    def test_reload_without_a_loaded_line_is_not_proven(self):
        self._write(self.current, BASE)
        self._write(self.candidate, changed(storage__gcDelay="1h"))
        clock = iter(range(0, 10_000, 3))
        with mock.patch.object(zca, "main_pid", return_value=4242), \
                mock.patch.object(zca, "watch_alive", return_value=True), \
                mock.patch.object(zca, "journal_cursor", return_value="c"), \
                mock.patch.object(zca, "journal_messages", return_value=[zca.MSG_CHANGED]), \
                mock.patch.object(zca.time, "time", side_effect=lambda: next(clock)), \
                mock.patch.object(zca.time, "sleep"):
            rc, result = self._run("--wait", "6", "--settle", "0")
        self.assertEqual((rc, result["mode"]), (2, "reload"))
        self.assertIn("pending", result["reason"])

    def test_no_journal_cursor_writes_nothing(self):
        self._write(self.current, BASE)
        before = Path(self.current).read_bytes()
        self._write(self.candidate, changed(storage__gcDelay="1h"))
        with mock.patch.object(zca, "main_pid", return_value=4242),                 mock.patch.object(zca, "watch_alive", return_value=True),                 mock.patch.object(zca, "journal_cursor", return_value=None):
            rc, result = self._run("--wait", "2", "--settle", "0")
        self.assertEqual((rc, result["mode"]), (2, "reload"))
        self.assertEqual(Path(self.current).read_bytes(), before)

    def test_transient_failed_then_loaded_is_a_success(self):
        # zot can read the file between pwrite and ftruncate (FAILED), then reload cleanly on the truncate.
        self._write(self.current, BASE)
        self._write(self.candidate, changed(storage__gcDelay="1h"))
        seq = [[zca.MSG_CHANGED, zca.MSG_FAILED],
               [zca.MSG_CHANGED, zca.MSG_FAILED, zca.MSG_CHANGED, zca.MSG_LOADED]]
        calls = iter(seq + [seq[-1]] * 50)
        with mock.patch.object(zca, "main_pid", return_value=4242),                 mock.patch.object(zca, "watch_alive", return_value=True),                 mock.patch.object(zca, "journal_cursor", return_value="c"),                 mock.patch.object(zca, "journal_messages", side_effect=lambda *a: next(calls)),                 mock.patch.object(zca, "http_alive", return_value=True),                 mock.patch.object(zca.time, "sleep"):
            rc, result = self._run("--wait", "30", "--settle", "0")
        self.assertEqual((rc, result["mode"]), (0, "reload"))

    def test_restart_keeps_the_marker_for_the_handler(self):
        self._write(self.current, BASE)
        self._write(self.candidate, changed(http__port="8443"))
        rc, result = self._run()
        self.assertEqual((rc, result["mode"]), (0, "restart"))
        self.assertTrue(os.path.exists(self.current + ".unconfirmed"))  # `restart zot` handlers clear it

    def test_proven_reload(self):
        self._write(self.current, BASE)
        self._write(self.candidate, changed(storage__gcDelay="1h"))
        ino = os.stat(self.current).st_ino
        with mock.patch.object(zca, "main_pid", return_value=4242), \
                mock.patch.object(zca, "watch_alive", return_value=True), \
                mock.patch.object(zca, "journal_cursor", return_value="c"), \
                mock.patch.object(zca, "journal_messages", return_value=[zca.MSG_CHANGED, zca.MSG_LOADED]), \
                mock.patch.object(zca, "http_alive", return_value=True), \
                mock.patch.object(zca.time, "sleep"):
            rc, result = self._run("--wait", "2", "--settle", "0")
        self.assertEqual((rc, result["mode"]), (0, "reload"))
        self.assertEqual(os.stat(self.current).st_ino, ino)  # the watched inode survived
        self.assertFalse(os.path.exists(self.current + ".unconfirmed"))
        self.assertEqual(json.loads(Path(self.current).read_text())["storage"]["gcDelay"], "1h")


class TaskWiringTest(unittest.TestCase):
    def setUp(self):
        tasks = yaml.safe_load((ROLE_DIR / "tasks" / "main.yml").read_text())
        self.names = [t["name"] for t in tasks]
        self.tasks = {t["name"]: t for t in tasks}

    def test_no_task_templates_the_live_config(self):
        for task in self.tasks.values():
            tpl = task.get("ansible.builtin.template") or {}
            self.assertFalse(str(tpl.get("dest", "")).endswith("/config.json"), task["name"])

    def test_candidate_render_validate_apply_restart_in_order(self):
        order = ["Install the zot config apply helper", "Render the zot config candidate",
                 "Validate the zot config candidate (same loader as `zot serve`)",
                 "Apply the zot config (reload in place when only reload-safe keys changed)",
                 "Restart zot when the config change is not reload-safe"]
        idx = [self.names.index(n) for n in order]
        self.assertEqual(idx, sorted(idx))
        self.assertLess(idx[-1], self.names.index("Flush handlers so the unit is registered before enable/start"))

    def test_candidate_keeps_secrets_out_of_logs(self):
        self.assertIs(self.tasks["Render the zot config candidate"]["no_log"], True)

    def test_verify_keeps_config_fragments_out_of_logs(self):
        self.assertIs(self.tasks["Validate the zot config candidate (same loader as `zot serve`)"]["no_log"], True)

    def test_restart_handlers_restart_then_clear_the_marker(self):
        handlers = yaml.safe_load((ROLE_DIR / "handlers" / "main.yml").read_text())
        listening = [h for h in handlers if h.get("listen") == "restart zot"]
        self.assertEqual([h["name"] for h in listening], ["restart zot", "clear the unconfirmed zot config marker"])
        self.assertEqual(listening[0]["ansible.builtin.systemd"]["state"], "restarted")
        self.assertEqual(listening[1]["ansible.builtin.file"],
                         {"path": "{{ registry_zot_config_dir }}/config.json.unconfirmed", "state": "absent"})

    def test_only_a_restart_verdict_notifies_restart(self):
        task = self.tasks["Restart zot when the config change is not reload-safe"]
        self.assertEqual(task["notify"], "restart zot")
        self.assertIn("== 'restart'", task["when"])
        apply = self.tasks["Apply the zot config (reload in place when only reload-safe keys changed)"]
        self.assertNotIn("notify", apply)
        self.assertIn("--dry-run", apply["ansible.builtin.command"]["argv"])
        self.assertIn("ansible_check_mode", apply["ansible.builtin.command"]["argv"])


if __name__ == "__main__":
    unittest.main()
