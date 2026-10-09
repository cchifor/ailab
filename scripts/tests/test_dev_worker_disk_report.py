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


def snap(d, hours_ago, roots):
    pathlib.Path(d, f"snap-{NOW - hours_ago * 3600}.json").write_text(
        json.dumps({"roots": {k: {"top": v} for k, v in roots.items()}}))


class SnapshotTest(unittest.TestCase):
    def test_closest_to_the_window_never_too_young(self):
        with tempfile.TemporaryDirectory() as d:
            for hours_ago in (2, 18, 23, 26):
                snap(d, hours_ago, {"c4:workspace": {"a": hours_ago}})
            self.assertEqual(dr.baselines(d, NOW, 24), {"c4:workspace": {"a": 23}})
        with tempfile.TemporaryDirectory() as d:
            snap(d, 1, {"c4:workspace": {"a": 1}})
            self.assertEqual(dr.baselines(d, NOW, 24), {})                  # only a young one

    def test_baseline_is_per_root(self):
        # The snapshot closest to 24h lacks the home root (its scan failed then): an older one that
        # has it is used for that root; a root no snapshot has gets no baseline at all.
        with tempfile.TemporaryDirectory() as d:
            snap(d, 24, {"c4:workspace": {"a": 1}})
            snap(d, 27, {"c4:workspace": {"a": 2}, "c4:home": {"h": 3}})
            self.assertEqual(dr.baselines(d, NOW, 24), {"c4:workspace": {"a": 1}, "c4:home": {"h": 3}})

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
        self.assertNotEqual(dr.esc("x" * 300 + "1"), dr.esc("x" * 300 + "2"))   # no collision

    def test_a_non_utf8_name_still_renders_and_writes(self):
        name = b"bad\xff-name".decode("utf-8", "surrogateescape")          # as du hands it over
        v = dr.esc(name)
        v.encode("utf-8")                                                 # must not raise
        model = {"complete": True, "complete_at": NOW, "roots": {"c4:workspace": {
            "user": "c4", "root": "workspace", "total": 1, "top": {name: 25 * GB},
            "kinds": {name: "other"}, "growth": {}, "scanned_at": NOW}}}
        text = dr.render(model, NOW, 1.0, 10, 5 * GB)
        with tempfile.TemporaryDirectory() as d:
            dr.write_atomic(os.path.join(d, "x.prom"), text, 0o644)
            self.assertEqual(os.listdir(d), ["x.prom"])

    def test_no_baseline_for_a_root_means_no_growth(self):
        os.makedirs(self.state)
        snap(self.state, 24, {"c4:home": {"x": 1}})                       # another root only
        scan = {("c4", "workspace"): scan_of(self.root, {"big1": 60 * GB})}
        model = dr.build(["c4"], NOW, scan, self.state, 10, 5 * GB, 24)
        self.assertEqual(model["roots"]["c4:workspace"]["growth"], {})

    def test_a_partial_run_persists_each_root_on_its_own(self):
        # workspace scans, home fails: workspace is saved and snapshotted; home keeps its OWN last
        # values in latest.json but is not in this run's snapshot (it would be a stale baseline).
        os.makedirs(self.state)
        prev = {"complete": True, "complete_at": NOW - 3600, "roots": {"c4:home": {
            "user": "c4", "root": "home", "total": 3 * GB, "top": {".codex": 3 * GB},
            "kinds": {".codex": "other"}, "growth": {}, "scanned_at": NOW - 3600}}}
        pathlib.Path(self.state, "latest.json").write_text(json.dumps(prev))
        scan = {("c4", "workspace"): scan_of(self.root, {"big1": 9 * GB}), ("c4", "home"): None}
        model = dr.build(["c4"], NOW, scan, self.state, 10, 5 * GB, 24)
        dr.save(model, self.state, NOW, 30)
        latest = json.loads(pathlib.Path(self.state, "latest.json").read_text())
        self.assertEqual(set(latest["roots"]), {"c4:workspace", "c4:home"})
        self.assertEqual(latest["complete_at"], NOW - 3600)                # not this partial run
        snapped = json.loads(pathlib.Path(self.state, f"snap-{NOW}.json").read_text())
        self.assertEqual(set(snapped["roots"]), {"c4:workspace"})
        text = dr.render(model, NOW, 1.0, 10, 5 * GB)
        self.assertIn(f"dev_worker_disk_report_last_complete_timestamp_seconds {NOW - 3600}", text)


class ImplReviewTest(unittest.TestCase):
    """Phase B (plans/2026-10-09-dev-worker-disk-hardening-impl-review.md) round 1."""

    def test_non_git_data_beside_checkouts_in_a_grouping_dir_is_exported(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "c4")
            os.makedirs(os.path.join(root, "wt", "one", ".git"))
            os.makedirs(os.path.join(root, "wt", "dump"))
            sizes = {".": 30 * GB, "wt": 30 * GB, "wt/one": 2 * GB, "wt/dump": 25 * GB}
            model = dr.build(["c4"], NOW, {("c4", "workspace"): (root, sizes)},
                             os.path.join(d, "state"), 10, 5 * GB, 24)
            text = dr.render(model, NOW, 1.0, 10, 5 * GB)
            self.assertIn('dir="wt",kind="worktrees"', text)
            self.assertIn(f'dir="wt/dump",kind="other"}} {25 * GB}', text)
            self.assertNotIn('dir="wt/one"', text)

    def test_a_missing_configured_root_is_a_failed_scan(self):
        with tempfile.TemporaryDirectory() as d:
            out = dr.scan_all(["nobody-here"], d, 600, scan=lambda p, left: {".": 1})
            self.assertIn(("nobody-here", "workspace"), out)
            self.assertIsNone(out[("nobody-here", "workspace")])

    def test_a_history_write_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = os.path.join(d, "blocker")
            pathlib.Path(blocker).write_text("")
            self.assertFalse(dr.save({"complete": True, "roots": {}}, os.path.join(blocker, "s"), NOW, 30))
            self.assertTrue(dr.save({"complete": True, "roots": {}}, os.path.join(d, "s"), NOW, 30))
            text = dr.render({"complete": True, "roots": {}, "history_write_failed": 1}, NOW, 1.0, 10, 5 * GB)
            self.assertIn("dev_worker_disk_report_history_write_failed 1", text)


class DuTest(unittest.TestCase):
    class R:
        def __init__(self, out, rc, err=""):
            self.stdout, self.returncode, self.stderr = out, rc, err

    def test_no_total_line_is_a_failed_scan(self):
        run = lambda *a, **k: self.R("4096\t/w/c4/a\n", 1, "du: cannot read directory")
        with self.assertRaises(OSError):
            dr.du("/w/c4", 10, run=run)

    def test_files_vanishing_mid_walk_are_tolerated(self):
        err = "du: cannot access '/w/c4/a/tmp1': No such file or directory\n"
        run = lambda *a, **k: self.R("4096\t/w/c4/a\n8192\t/w/c4\n", 1, err)
        self.assertEqual(dr.du("/w/c4", 10, run=run), {"a": 4096, ".": 8192})

    def test_a_total_after_an_io_or_permission_error_is_a_failed_scan(self):
        for err in ("du: cannot read directory '/w/c4/a/b': Input/output error\n",
                    "du: cannot access '/w/c4/x': No such file or directory\n"
                    "du: cannot read directory '/w/c4/p': Permission denied\n"):
            run = lambda *a, **k: self.R("4096\t/w/c4/a\n8192\t/w/c4\n", 1, err)
            with self.assertRaises(OSError, msg=err):
                dr.du("/w/c4", 10, run=run)


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
