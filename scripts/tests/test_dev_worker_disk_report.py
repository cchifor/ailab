"""Tests for the dev-worker `disk-report` (ansible/roles/dev_worker/files/disk-report), W5 of
plans/2026-10-09-dev-worker-disk-hardening-plan.md: hourly per-directory attribution so a human hears
WHICH directory is filling a disk while there is still room.

Pinned here: du parsing; kind classification (a hint, never a deletion criterion); growth computed
over ALL entries before truncating to the top N (a new, fast-growing dir shows even when it is not
yet among the largest); the snapshot closest to the window, never one too young; a failed root keeps
its last complete values and marks the scan incomplete; label escaping and length cap.
"""
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

_PATH = pathlib.Path(__file__).resolve().parents[2] / "ansible/roles/dev_worker/files/disk-report"
_loader = importlib.machinery.SourceFileLoader("dw_disk_report", str(_PATH))
_spec = importlib.util.spec_from_loader("dw_disk_report", _loader)
dr = importlib.util.module_from_spec(_spec)
_loader.exec_module(dr)

GB = 10**9
NOW = 2_000_000_000


class ParseTest(unittest.TestCase):
    def test_parse_du_relative_paths_and_total(self):
        out = ("4096\t/workspace/c4/a/x\n12000\t/workspace/c4/a\n500\t/workspace/c4/f.tar\n"
               "13000\t/workspace/c4\nbogus line\n")
        sizes = dr.parse_du(out, "/workspace/c4/")
        self.assertEqual(sizes, {"a/x": 4096, "a": 12000, "f.tar": 500, ".": 13000})
        self.assertEqual(dr.top_level(sizes), {"a": 12000, "f.tar": 500})

    def test_kinds(self):
        with tempfile.TemporaryDirectory() as d:
            for p in ("repo/.git", "wt/one/.git", "data/sub", ".cache/uv", ".npm", ".tmp", ".cache-npm"):
                os.makedirs(os.path.join(d, p))
            os.makedirs(os.path.join(d, "linked"))
            pathlib.Path(d, "linked", ".git").write_text("gitdir: /x\n")      # a linked worktree
            pathlib.Path(d, "f.tar").write_text("x")
            got = {n: dr.kind_of(d, n) for n in ("repo", "linked", "wt", "data", ".cache", ".npm",
                                                  ".tmp", ".cache-npm", "f.tar")}
            self.assertEqual(got, {"repo": "git", "linked": "git", "wt": "worktrees", "data": "other",
                                   ".cache": "cache", ".npm": "cache", ".tmp": "tmp",
                                   ".cache-npm": "cache", "f.tar": "file"})


class SnapshotTest(unittest.TestCase):
    def test_closest_to_the_window_never_too_young(self):
        with tempfile.TemporaryDirectory() as d:
            for hours_ago in (2, 18, 23, 26):
                ts = NOW - hours_ago * 3600
                pathlib.Path(d, f"snap-{ts}.json").write_text(json.dumps({"h": hours_ago}))
            self.assertEqual(dr.pick_snapshot(d, NOW, 24), {"h": 23})
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, f"snap-{NOW - 3600}.json").write_text("{}")
            self.assertIsNone(dr.pick_snapshot(d, NOW, 24))                # only a young one

    def test_save_keeps_a_window_of_snapshots(self):
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, f"snap-{NOW - 40 * 3600}.json").write_text("{}")
            pathlib.Path(d, f"snap-{NOW - 10 * 3600}.json").write_text("{}")
            dr.save({"complete": True, "roots": {}}, d, NOW, 30)
            names = sorted(os.listdir(d))
            self.assertEqual(names, ["latest.json", f"snap-{NOW - 10 * 3600}.json", f"snap-{NOW}.json"])
            self.assertEqual(os.stat(os.path.join(d, "latest.json")).st_mode & 0o777, 0o600)


def scan_of(root, top):
    sizes = {".": sum(top.values()), **top}
    return (root, sizes)


class BuildRenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state")
        self.root = os.path.join(self.tmp.name, "ws", "c4")
        for name in ("big1", "big2", "newdir", "archives"):
            os.makedirs(os.path.join(self.root, name))
        os.makedirs(os.path.join(self.root, "big1", ".git"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_growth_is_computed_before_truncation(self):
        # A day ago: two big git trees. Now: a NEW dir grew 16 GB, small next to them.
        os.makedirs(self.state)
        then = {"roots": {"c4:workspace": {"top": {"big1": 60 * GB, "big2": 50 * GB}}}}
        pathlib.Path(self.state, f"snap-{NOW - 24 * 3600}.json").write_text(json.dumps(then))
        scan = {("c4", "workspace"): scan_of(self.root, {"big1": 60 * GB, "big2": 50 * GB,
                                                        "newdir": 16 * GB})}
        model = dr.build(["c4"], NOW, scan, self.state, top=1, big=5 * GB, growth_hours=24)
        text = dr.render(model, NOW, 1.0, top=1, big=5 * GB)
        self.assertIn('dev_worker_dir_growth_bytes{user="c4",root="workspace",dir="newdir",kind="other"} '
                      f"{16 * GB}", text)
        self.assertIn('dev_worker_dir_bytes{user="c4",root="workspace",dir="big1",kind="git"}', text)

    def test_large_unowned_data_is_exported_even_outside_the_top_n(self):
        scan = {("c4", "workspace"): scan_of(self.root, {"big1": 60 * GB, "big2": 50 * GB,
                                                        "archives": 22 * GB})}
        model = dr.build(["c4"], NOW, scan, self.state, top=1, big=5 * GB, growth_hours=24)
        text = dr.render(model, NOW, 1.0, top=1, big=5 * GB)
        self.assertIn('dir="archives",kind="other"} ' + str(22 * GB), text)
        self.assertIn('dir="big2",kind="other"} ' + str(50 * GB), text)   # unowned and big too
        self.assertIn(f'dev_worker_dir_user_bytes{{user="c4",root="workspace"}} {132 * GB}', text)
        self.assertIn("dev_worker_disk_report_scan_complete 1", text)
        self.assertNotIn("dev_worker_dir_growth_bytes{", text)          # no baseline yet

    def test_a_failed_root_keeps_its_last_complete_values(self):
        os.makedirs(self.state)
        prev = {"complete": True, "roots": {"c4:workspace": {
            "user": "c4", "root": "workspace", "path": self.root, "total": 7 * GB,
            "top": {"big1": 7 * GB}, "kinds": {"big1": "git"}, "growth": {}, "scanned_at": NOW - 3600}}}
        pathlib.Path(self.state, "latest.json").write_text(json.dumps(prev))
        model = dr.build(["c4"], NOW, {("c4", "workspace"): None}, self.state, 10, 5 * GB, 24)
        text = dr.render(model, NOW, 600.0, 10, 5 * GB)
        self.assertIn("dev_worker_disk_report_scan_complete 0", text)
        self.assertIn(f'dev_worker_disk_report_data_timestamp_seconds{{user="c4",root="workspace"}} {NOW - 3600}',
                      text)
        self.assertIn(f'dev_worker_dir_user_bytes{{user="c4",root="workspace"}} {7 * GB}', text)

    def test_label_escaping_and_cap(self):
        self.assertEqual(dr.esc('a"b\\c\nd'), 'a\\"b\\\\c\\nd')
        self.assertEqual(len(dr.esc("x" * 500)), dr.LABEL_MAX)


class ScanTest(unittest.TestCase):
    def test_budget_and_failures_mark_roots_none(self):
        with tempfile.TemporaryDirectory() as d:
            for u in ("a", "b"):
                os.makedirs(os.path.join(d, u))
            ticks = iter([0, 0, 1000, 1000])            # deadline after the first scan

            def fake(path, left):
                return {".": 1}
            out = dr.scan_all(["a", "b"], d, 600, scan=fake, clock=lambda: next(ticks))
            self.assertEqual(out[("a", "workspace")], (os.path.join(d, "a"), {".": 1}))
            self.assertIsNone(out[("b", "workspace")])

            def boom(path, left):
                raise subprocess.TimeoutExpired(["du"], left)
            out = dr.scan_all(["a"], d, 600, scan=boom)
            self.assertIsNone(out[("a", "workspace")])


if __name__ == "__main__":
    unittest.main()
