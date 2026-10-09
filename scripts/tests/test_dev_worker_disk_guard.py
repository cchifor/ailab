"""Tests for the dev-worker `disk-guard` (ansible/roles/dev_worker/files/disk-guard).

The guard's contract, pinned here:
  * Above --low on every watched fs it runs nothing (and writes a heartbeat).
  * Under --low it runs the ladder in order and stops as soon as every low fs is >= --target.
  * A step only runs while ITS filesystem is still short (re-measured before every step): docker
    steps for the docker fs; the worktree/deps steps search only the roots on a short fs — a full /
    never costs /workspace a worktree.
  * The whole-worktree step exists only with --worktrees-remove (the role's prune mode).
  * Every step ran cleanly and a fs is still under --low → exhausted=1 (the alert's signal). A
    deferred or failed step is never "exhausted": reclaim did not get its chance.
  * --dry-run runs nothing.
  * While a docker client builds/pulls, docker steps wait (a prune's containerd GC kills in-flight
    pulls) — unless the docker fs is under --critical. `run`/`create`/`up` count only while young.
"""
import importlib.machinery
import importlib.util
import os
import pathlib
import shutil
import tempfile
import unittest

_PATH = pathlib.Path(__file__).resolve().parents[2] / "ansible/roles/dev_worker/files/disk-guard"
_loader = importlib.machinery.SourceFileLoader("dw_disk_guard", str(_PATH))
_spec = importlib.util.spec_from_loader("dw_disk_guard", _loader)
dg = importlib.util.module_from_spec(_spec)
_loader.exec_module(dg)

# /home and /root live on /, /workspace is its own disk — the workers' layout.
FS = {"/": "/", "/home": "/", "/root": "/", "/workspace": "/workspace"}


def layout(a, b):
    return FS.get(a, a) == FS.get(b, b)


class Disk:
    """Free fractions per path; each executed step frees `gain[step]` on the given paths."""

    def __init__(self, free, gain=None, size=100 * 10**9, rc=None):
        self.free = dict(free)
        self.gain = gain or {}
        self.size = size
        self.rc = rc or {}
        self.ran = []
        self.argv = {}
        self.roots = {}

    def measure(self, p):
        return self.free[p]

    def avail(self, p):
        return int(self.free[p] * self.size)

    def run(self, step):
        self.ran.append(step.name)
        self.argv[step.name] = step.argv
        self.roots[step.name] = step.roots
        for p, g in self.gain.get(step.name, {}).items():
            self.free[p] = min(1.0, self.free[p] + g)
        return self.rc.get(step.name, 0)


def opts(*extra, same_fs=layout):
    o = dg.parse_args(["--watch", "/", "--watch", "/workspace", "--docker-fs", "/workspace",
                       *extra])
    o.same_fs = same_fs
    return o


def quiet(*_a, **_k):
    pass


def idle():
    return ""


def building():
    return "docker buildx build ."


def roots_of(argv):
    return argv[argv.index("--deps-roots") + 1]


class GuardTest(unittest.TestCase):
    def test_healthy_disk_runs_nothing(self):
        d = Disk({"/": 0.40, "/workspace": 0.16})   # between low and target: hysteresis, no-op
        m = dg.guard(opts(), d.measure, d.avail, d.run, quiet, idle)
        self.assertEqual(d.ran, [])
        self.assertEqual((m["triggered"], m["exhausted"], m["steps_run"]), (0, 0, 0))

    def test_ladder_stops_once_target_reached(self):
        d = Disk({"/": 0.40, "/workspace": 0.05},
                 gain={"build-cache": {"/workspace": 0.10}, "anon-volumes": {"/workspace": 0.12}})
        m = dg.guard(opts(), d.measure, d.avail, d.run, quiet, idle)
        self.assertEqual(d.ran, ["build-cache", "anon-volumes"])
        self.assertEqual((m["triggered"], m["exhausted"], m["steps_run"]), (1, 0, 2))
        self.assertEqual(m["reclaimed_bytes"], int(0.27 * 100 * 10**9) - int(0.05 * 100 * 10**9))

    def test_order_least_destructive_first(self):
        d = Disk({"/": 0.40, "/workspace": 0.01})
        dg.guard(opts("--worktrees-remove"), d.measure, d.avail, d.run, quiet, idle)
        self.assertEqual(d.ran, ["build-cache", "anon-volumes", "docker", "worktrees", "deps"])
        self.assertEqual(roots_of(d.argv["deps"]), "/workspace")

    def test_root_fs_low_skips_docker_and_spares_workspace(self):
        d = Disk({"/": 0.05, "/workspace": 0.60})
        dg.guard(opts("--worktrees-remove"), d.measure, d.avail, d.run, quiet, idle)
        self.assertEqual(d.ran, ["codex-releases", "worktrees", "deps"])
        self.assertEqual(d.roots["codex-releases"], ("/home",))
        self.assertEqual(roots_of(d.argv["worktrees"]), "/home")
        self.assertEqual(roots_of(d.argv["deps"]), "/home")

    def test_both_low_walk_steps_search_both(self):
        d = Disk({"/": 0.05, "/workspace": 0.05})
        dg.guard(opts(), d.measure, d.avail, d.run, quiet, idle)
        self.assertEqual(roots_of(d.argv["deps"]), "/workspace,/home")

    def test_docker_steps_stop_once_docker_fs_recovers(self):
        # Both low; build cache fixes /workspace. The rest of the docker ladder must not run just
        # because / is still short, and the walk steps then search / only.
        d = Disk({"/": 0.05, "/workspace": 0.05}, gain={"build-cache": {"/workspace": 0.30}})
        dg.guard(opts(), d.measure, d.avail, d.run, quiet, idle)
        self.assertEqual(d.ran, ["codex-releases", "build-cache", "deps"])
        self.assertEqual(roots_of(d.argv["deps"]), "/home")

    def test_worktree_step_only_in_remove_mode(self):
        d = Disk({"/": 0.40, "/workspace": 0.01})
        dg.guard(opts(), d.measure, d.avail, d.run, quiet, idle)
        self.assertNotIn("worktrees", d.ran)

    def test_exhausted_when_still_low(self):
        d = Disk({"/": 0.40, "/workspace": 0.02}, gain={"deps": {"/workspace": 0.05}})
        m = dg.guard(opts(), d.measure, d.avail, d.run, quiet, idle)
        self.assertEqual((m["exhausted"], m["failed_steps"]), (1, 0))

    def test_failed_step_is_not_exhausted(self):
        d = Disk({"/": 0.40, "/workspace": 0.02}, rc={"docker": 1})
        m = dg.guard(opts(), d.measure, d.avail, d.run, quiet, idle)
        self.assertEqual((m["exhausted"], m["failed_steps"], m["steps_run"]), (0, 1, 4))

    def test_between_low_and_target_after_ladder_is_not_exhausted(self):
        d = Disk({"/": 0.40, "/workspace": 0.02}, gain={"deps": {"/workspace": 0.16}})
        m = dg.guard(opts(), d.measure, d.avail, d.run, quiet, idle)
        self.assertEqual(m["exhausted"], 0)    # 18%: above low, below target

    def test_dry_run_runs_nothing(self):
        d = Disk({"/": 0.40, "/workspace": 0.01})
        m = dg.guard(opts("--dry-run"), d.measure, d.avail, d.run, quiet, idle)
        self.assertEqual(d.ran, [])
        self.assertEqual((m["triggered"], m["exhausted"]), (1, 0))

    def test_cleanup_steps_never_touch_stacks_or_named_volumes(self):
        for s in dg.ladder(opts("--worktrees-remove")):
            self.assertNotIn("--volumes", s.argv)
            if s.name == "docker":
                self.assertIn("--keep-stacks", s.argv)
            if s.name == "anon-volumes":
                self.assertIn("label=com.docker.volume.anonymous", s.argv)
                self.assertNotIn("-a", s.argv)
                self.assertNotIn("--all", s.argv)

    def test_busy_docker_defers_docker_steps_only(self):
        d = Disk({"/": 0.40, "/workspace": 0.08})
        m = dg.guard(opts("--worktrees-remove"), d.measure, d.avail, d.run, quiet, building)
        self.assertEqual(d.ran, ["worktrees", "deps"])
        self.assertEqual((m["deferred"], m["exhausted"]), (1, 0))

    def test_critical_disk_prunes_through_busy_docker(self):
        d = Disk({"/": 0.40, "/workspace": 0.03}, gain={"build-cache": {"/workspace": 0.30}})
        m = dg.guard(opts(), d.measure, d.avail, d.run, quiet, building)
        self.assertEqual(d.ran, ["build-cache"])
        self.assertEqual(m["deferred"], 0)

    def test_busy_is_reread_before_each_docker_step(self):
        d = Disk({"/": 0.40, "/workspace": 0.08})
        answers = iter(["", "docker pull postgres", ""])
        dg.guard(opts(), d.measure, d.avail, d.run, quiet, lambda: next(answers))
        self.assertEqual(d.ran, ["build-cache", "docker", "deps"])

    def test_cleanup_lock_held_is_deferred_not_exhausted(self):
        d = Disk({"/": 0.40, "/workspace": 0.02})

        def locked(step):
            d.ran.append(step.name)
            return dg.LOCKED if step.argv[0].endswith("cleanup") else 0
        m = dg.guard(opts(), d.measure, d.avail, locked, quiet, idle)
        self.assertEqual(m["steps_run"], 2)     # build-cache, anon-volumes; cleanup steps locked
        self.assertEqual((m["deferred"], m["exhausted"]), (1, 0))

    def test_reclaimed_bytes_counted_once_per_filesystem(self):
        shared = {"v": 0.05}

        def run(step):
            shared["v"] = 0.35
            return 0
        m = dg.guard(opts(same_fs=lambda a, b: True), lambda p: shared["v"],
                     lambda p: int(shared["v"] * 100 * 10**9), run, quiet, idle)
        self.assertEqual(m["reclaimed_bytes"], int(0.35 * 100 * 10**9) - int(0.05 * 100 * 10**9))

    def test_busy_kind(self):
        for argv, kind in ((["/usr/bin/docker", "build", "."], "build"),
                           (["/usr/libexec/docker/cli-plugins/docker-buildx", "buildx", "build", "."],
                            "build"),
                           (["docker", "pull", "x"], "build"),
                           (["docker", "compose", "up", "--build"], "build-start"),
                           (["docker", "compose", "build"], "build"),
                           (["docker", "compose", "up", "-d"], "start"),
                           (["docker", "run", "--rm", "postgres"], "start"),
                           (["docker", "ps"], None), (["docker", "logs", "-f", "x"], None),
                           (["dockerd"], None),
                           (["containerd-shim-runc-v2", "-namespace", "moby"], None),
                           (["python3", "run"], None), (["docker", "compose", "logs", "-f"], None)):
            self.assertEqual(dg.busy_kind(argv), kind, argv)

    def test_docker_busy_reads_proc_and_ages_out_attached_clients(self):
        ticks = dg.os.sysconf("SC_CLK_TCK") if hasattr(dg.os, "sysconf") else 100

        def proc_with(procs, uptime=10_000):
            d = tempfile.TemporaryDirectory()
            root = pathlib.Path(d.name)
            (root / "uptime").write_text(f"{uptime}.00 1.00\n")
            for pid, argv, started in procs:
                (root / pid).mkdir()
                (root / pid / "cmdline").write_bytes(b"".join(a.encode() + bytes([0]) for a in argv))
                rest = ["S"] + ["0"] * 18 + [str(int(started * ticks))] + ["0"] * 10
                (root / pid / "stat").write_text(f"{pid} (my (odd) comm) " + " ".join(rest))
            (root / "self").mkdir()
            return d

        with proc_with([("10", ["bash"], 0), ("11", ["/usr/bin/docker", "pull", "pg"], 1)]) as p:
            self.assertEqual(dg.docker_busy(p), "/usr/bin/docker pull pg")   # builds never age out
        with proc_with([("12", ["docker", "run", "--rm", "pg"], 9_900)]) as p:
            self.assertEqual(dg.docker_busy(p), "docker run --rm pg")       # 100 s old: starting
        with proc_with([("13", ["docker", "compose", "up"], 1_000)]) as p:
            self.assertEqual(dg.docker_busy(p), "")                         # attached for 2.5 h
        with proc_with([("14", ["docker", "ps"], 9_999)]) as p:
            self.assertEqual(dg.docker_busy(p), "")
        with proc_with([("15", ["docker", "compose", "up", "--build"], 8_000)]) as p:
            self.assertEqual(dg.docker_busy(p), "docker compose up --build")   # 33 min: may build
        with proc_with([("16", ["docker", "compose", "up", "--build"], 5_000)]) as p:
            self.assertEqual(dg.docker_busy(p), "")                         # 83 min: attached

    def test_heartbeat_on_trigger_and_after_every_step(self):
        d = Disk({"/": 0.40, "/workspace": 0.02})
        beats = []
        dg.guard(opts(), d.measure, d.avail, d.run, quiet, idle,
                 progress=lambda m: beats.append(m["steps_run"]))
        self.assertEqual(beats, [0, 1, 2, 3, 4])

    def test_argument_validation(self):
        with self.assertRaises(SystemExit):
            dg.parse_args(["--low", "30", "--target", "20"])
        with self.assertRaises(SystemExit):
            dg.parse_args(["--critical", "20", "--low", "15"])
        with self.assertRaises(SystemExit):
            dg.parse_args(["--watch", "/", "--docker-fs", "/workspace"])

    def test_prom_render_and_atomic_write(self):
        m = {"triggered": 1, "steps_run": 2, "failed_steps": 0, "reclaimed_bytes": 5,
             "exhausted": 0, "deferred": 0, "free_ratio": {"/": 0.5, "/workspace": 0.26}}
        text = dg.render_prom(m, 1700000000)
        self.assertIn("dev_worker_disk_guard_last_run_timestamp_seconds 1700000000\n", text)
        self.assertIn("dev_worker_disk_guard_failed_steps 0\n", text)
        self.assertIn('dev_worker_disk_guard_free_ratio{mountpoint="/workspace"} 0.2600\n', text)
        with tempfile.TemporaryDirectory() as d:
            dg.write_prom(text, d)
            files = list(pathlib.Path(d).iterdir())
            self.assertEqual([f.name for f in files], [dg.PROM])
            self.assertEqual(files[0].read_text(), text)


    def test_full_ladder_when_both_disks_are_low(self):
        d = Disk({"/": 0.01, "/workspace": 0.01})
        dg.guard(opts("--worktrees-remove"), d.measure, d.avail, d.run, quiet, idle)
        self.assertEqual(d.ran, ["codex-releases", "build-cache", "anon-volumes", "docker",
                                 "worktrees", "deps"])

    def test_codex_releases_only_for_the_root_fs(self):
        d = Disk({"/": 0.40, "/workspace": 0.01})
        dg.guard(opts(), d.measure, d.avail, d.run, quiet, idle)
        self.assertNotIn("codex-releases", d.ran)

    def test_exhausted_names_the_low_fs_and_reports_it(self):
        d = Disk({"/": 0.05, "/workspace": 0.40})
        logs = []
        m = dg.guard(opts(), d.measure, d.avail, d.run, logs.append, idle)
        self.assertEqual(m["exhausted"], 1)
        self.assertEqual(m["exhausted_paths"], ["/"])
        self.assertIn("eligible reclaim was not enough: / (5.0%) is still under 15% free", logs[-1])
        self.assertNotIn("/workspace", logs[-1])

    def test_not_exhausted_no_report(self):
        d = Disk({"/": 0.05, "/workspace": 0.40}, gain={"codex-releases": {"/": 0.30}})
        m = dg.guard(opts(), d.measure, d.avail, d.run, quiet, idle)
        self.assertEqual((m["exhausted"], m.get("exhausted_paths"), d.ran),
                         (0, None, ["codex-releases"]))

    def test_dry_run_previews_in_process_steps_only(self):
        d = Disk({"/": 0.05, "/workspace": 0.05})
        m = dg.guard(opts("--dry-run"), d.measure, d.avail, d.run, quiet, idle)
        self.assertEqual(d.ran, ["codex-releases"])     # bound to dry_run; nothing else is called
        self.assertEqual((m["steps_run"], m["exhausted"], m.get("exhausted_paths")), (0, 0, None))

    def test_run_step_runs_in_process_steps(self):
        seen = []
        ok = dg.Step("x", False, (), 1, fn=lambda roots: seen.append(roots) or 0, roots=("/home",))
        self.assertEqual(dg.run_step(ok), 0)
        self.assertEqual(seen, [("/home",)])

        def boom(roots):
            raise PermissionError("nope")
        self.assertEqual(dg.run_step(dg.Step("y", False, (), 1, fn=boom)), 1)


OLD = 1_000_000.0       # a timestamp long before NOW
NOW = OLD + 30 * 86400


def MTIME(st):
    """The archive tests set mtimes; a test cannot set a ctime, so they judge age by mtime."""
    return st.st_mtime


def touch_tree(path, when):
    """Set the mtime of `path` and everything under it (symlinks themselves untouched)."""
    for dirpath, _dirnames, filenames in os.walk(path):
        for n in filenames:
            os.utime(os.path.join(dirpath, n), (when, when))
        os.utime(dirpath, (when, when))
    if os.path.isfile(path):
        os.utime(path, (when, when))


def rm(path, uid, gid):
    """remove_as_owner's contract (True when gone), without changing user."""
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    else:
        shutil.rmtree(path)
    return not os.path.lexists(path)


class CodexReleasesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name) / "home" / "c4"
        self.daemon = self.home / dg.CODEX_DAEMON
        self.rel = self.daemon / "releases"
        for v in ("0.158.0", "0.159.0", "0.160.0", "0.161.0", "0.162.0", "0.163.0"):
            (self.rel / v / "bin").mkdir(parents=True)
            (self.rel / v / "bin" / "codex").write_bytes(b"x" * 4096)
            touch_tree(self.rel / v, OLD)
        (self.daemon / "current").symlink_to(self.rel / "0.162.0")
        (self.daemon / "auto-update-version").write_text("0.163.0\n")

    def tearDown(self):
        self.tmp.cleanup()

    def prune(self, exes=(), remove=rm, dry_run=False, lock=lambda d: True):
        logs = []
        rc = dg.prune_codex_releases([str(self.home.parent)], exes=set(exes), now=NOW,
                                     log=logs.append, remove=remove, dry_run=dry_run, lock=lock,
                                     stamp=MTIME)
        return rc, sorted(p.name for p in self.rel.iterdir()), logs

    def test_keeps_current_next_running_and_young(self):
        touch_tree(self.rel / "0.160.0", NOW - 60)                  # being installed
        running = os.path.realpath(self.rel / "0.158.0" / "bin" / "codex")
        rc, left, logs = self.prune(exes=[running])
        self.assertEqual(rc, 0)
        self.assertEqual(left, ["0.158.0", "0.160.0", "0.162.0", "0.163.0"])
        self.assertEqual(len(logs), 2)                              # 0.159.0 and 0.161.0

    def test_a_home_itself_is_a_root_too(self):
        dg.prune_codex_releases([str(self.home)], exes=set(), now=NOW, log=quiet, remove=rm,
                                lock=lambda d: True, stamp=MTIME)
        self.assertEqual(sorted(p.name for p in self.rel.iterdir()), ["0.162.0", "0.163.0"])

    def test_dangling_current_skips_the_home(self):
        (self.daemon / "current").unlink()
        (self.daemon / "current").symlink_to(self.rel / "0.999.0")
        rc, left, logs = self.prune()
        self.assertEqual((rc, len(left)), (0, 6))
        self.assertIn("skipped", logs[0])

    def test_current_outside_releases_skips_the_home(self):
        outside = pathlib.Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        (self.daemon / "current").unlink()
        (self.daemon / "current").symlink_to(outside)
        self.assertEqual(len(self.prune()[1]), 6)

    def test_never_follows_a_symlinked_release(self):
        victim = pathlib.Path(self.tmp.name) / "victim"
        victim.mkdir()
        (victim / "keep").write_text("x")
        (self.rel / "0.150.0").symlink_to(victim)
        self.prune()
        self.assertTrue((victim / "keep").exists())
        self.assertTrue((self.rel / "0.150.0").is_symlink())

    def test_symlinked_releases_dir_is_skipped(self):
        real = pathlib.Path(self.tmp.name) / "real-releases"
        self.rel.rename(real)
        self.rel.symlink_to(real)
        (self.daemon / "current").unlink()
        (self.daemon / "current").symlink_to(self.rel / "0.162.0")
        rc, _left, logs = self.prune()
        self.assertEqual(len(list(real.iterdir())), 6)
        self.assertIn("through a symlink", logs[0])

    def test_install_reached_through_a_symlinked_ancestor_is_skipped(self):
        other = pathlib.Path(self.tmp.name) / "other" / "u"
        other.mkdir(parents=True)
        (other / ".codex").symlink_to(self.home / ".codex")
        logs = []
        dg.prune_codex_releases([str(other.parent)], exes=set(), now=NOW, log=logs.append,
                                remove=rm, lock=lambda d: True, stamp=MTIME)
        self.assertEqual(len(list(self.rel.iterdir())), 6)
        self.assertIn("through a symlink", logs[0])

    def test_installer_holding_the_lock_skips_the_home(self):
        rc, left, logs = self.prune(lock=lambda d: None)
        self.assertEqual((rc, len(left)), (0, 6))
        self.assertIn("install.lock", logs[0])

    @unittest.skipUnless(hasattr(os, "geteuid"), "POSIX only")
    def test_installer_lock_is_taken_and_released(self):
        import fcntl
        (self.daemon / "install.lock").write_text("")
        held = dg.installer_lock(str(self.daemon))
        self.assertIsNone(dg.installer_lock(str(self.daemon)))  # flock: a second open file conflicts
        held.close()
        again = dg.installer_lock(str(self.daemon))
        self.assertNotIn(again, (None, True))
        again.close()
        with open(self.daemon / "install.lock") as f:               # an installer holding it
            fcntl.flock(f, fcntl.LOCK_EX)
            self.assertIsNone(dg.installer_lock(str(self.daemon)))
        (self.daemon / "install.lock").unlink()
        self.assertIs(dg.installer_lock(str(self.daemon)), True)

    def test_current_moving_mid_prune_stops_it(self):
        def flip(p, u, g):
            (self.daemon / "current").unlink()
            (self.daemon / "current").symlink_to(self.rel / "0.163.0")
            return rm(p, u, g)
        rc, left, logs = self.prune(remove=flip)
        self.assertEqual(left, ["0.159.0", "0.160.0", "0.161.0", "0.162.0", "0.163.0"])
        self.assertIn("moved mid-prune", logs[-1])

    def test_dry_run_lists_and_removes_nothing(self):
        rc, left, logs = self.prune(dry_run=True)
        self.assertEqual((rc, len(left)), (0, 6))
        self.assertEqual(len(logs), 4)
        self.assertTrue(all("would remove" in line for line in logs))

    def test_failed_removal_is_rc_1(self):
        rc, left, logs = self.prune(remove=lambda p, u, g: False)
        self.assertEqual((rc, len(left)), (1, 6))
        self.assertTrue(all("could not remove" in line for line in logs))

    def test_removes_as_the_releases_dir_owner(self):
        seen = []
        self.prune(remove=lambda p, u, g: seen.append((u, g)) or rm(p, u, g))
        st = os.lstat(self.rel)
        self.assertEqual(set(seen), {(st.st_uid, st.st_gid)})


class ArchiveTrimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.arc = self.root / "archive"
        self.arc.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def entry(self, name, kb, when, as_dir=True):
        p = self.arc / name
        if as_dir:
            (p / "sub").mkdir(parents=True)
            (p / "sub" / "data").write_bytes(os.urandom(kb * 1024))
        else:
            p.write_bytes(os.urandom(kb * 1024))
        touch_tree(p, when)
        return dg.tree_usage(str(p), stamp=MTIME)[0]

    def test_removes_oldest_first_down_to_the_cap(self):
        a = self.entry("a", 64, OLD)
        b = self.entry("b", 64, OLD + 100, as_dir=False)
        c = self.entry("c", 64, OLD + 200)
        r = dg.trim_archive(str(self.arc), c + 1, now=NOW, log=quiet, remove=rm, stamp=MTIME)
        self.assertEqual(sorted(p.name for p in self.arc.iterdir()), ["c"])
        self.assertEqual(r, {"bytes": c, "removed": a + b, "timeout": 0})

    def test_newest_mtime_inside_an_entry_counts(self):
        self.entry("a", 64, OLD)
        b = self.entry("b", 64, OLD + 100)
        os.utime(self.arc / "a" / "sub" / "data", (OLD + 500, OLD + 500))   # a was written last
        dg.trim_archive(str(self.arc), b + 1, now=NOW, log=quiet, remove=rm, stamp=MTIME)
        self.assertEqual(sorted(p.name for p in self.arc.iterdir()), ["a"])

    def test_never_removes_a_young_entry(self):
        self.entry("old", 64, OLD)
        self.entry("young", 64, NOW - 60)
        r = dg.trim_archive(str(self.arc), 1, now=NOW, log=quiet, remove=rm, stamp=MTIME)
        self.assertEqual(sorted(p.name for p in self.arc.iterdir()), ["young"])
        self.assertGreater(r["bytes"], 1)

    def test_under_the_cap_is_a_noop(self):
        a = self.entry("a", 64, OLD)
        self.assertEqual(dg.trim_archive(str(self.arc), a * 10, now=NOW, log=quiet, remove=rm,
                                         stamp=MTIME), {"bytes": a, "removed": 0, "timeout": 0})
        self.assertTrue((self.arc / "a").exists())

    def test_symlinked_entry_counts_and_removes_only_the_link(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "big").write_bytes(os.urandom(256 * 1024))
        (self.arc / "link").symlink_to(outside)
        os.utime(self.arc / "link", (OLD, OLD), follow_symlinks=False)
        self.entry("keep", 64, NOW - 60)
        dg.trim_archive(str(self.arc), 1, now=NOW, log=quiet, remove=rm, stamp=MTIME)
        self.assertFalse((self.arc / "link").is_symlink())
        self.assertTrue((outside / "big").exists())

    def test_archive_dir_symlink_or_missing_is_never_trimmed(self):
        self.entry("a", 64, OLD)
        (self.root / "via-link").symlink_to(self.arc)
        self.assertIsNone(dg.trim_archive(str(self.root / "via-link"), 1, now=NOW, log=quiet,
                                          remove=rm, stamp=MTIME))
        self.assertIsNone(dg.trim_archive(str(self.root / "nope"), 1, now=NOW, log=quiet,
                                          remove=rm, stamp=MTIME))
        self.assertTrue((self.arc / "a").exists())

    def test_dry_run_removes_nothing(self):
        self.entry("a", 64, OLD)
        logs = []
        dg.trim_archive(str(self.arc), 1, now=NOW, log=logs.append, remove=rm, dry_run=True,
                        stamp=MTIME)
        self.assertTrue((self.arc / "a").exists())
        self.assertIn("would remove", logs[0])

    def test_failed_removal_keeps_counting_it(self):
        a = self.entry("a", 64, OLD)
        r = dg.trim_archive(str(self.arc), 1, now=NOW, log=quiet,
                            remove=lambda p, u, g: False, stamp=MTIME)
        self.assertEqual((r["bytes"], r["removed"]), (a, 0))

    def test_all_young_over_the_cap_says_so(self):
        self.entry("young", 64, NOW - 60)
        logs = []
        dg.trim_archive(str(self.arc), 1, now=NOW, log=logs.append, remove=rm, stamp=MTIME)
        self.assertTrue((self.arc / "young").exists())
        self.assertIn("over its 0 GB cap, and everything left changed within the hour", logs[0])

    def test_hard_links_count_once(self):
        a = self.entry("a", 64, OLD)
        (self.arc / "b").mkdir()
        os.link(self.arc / "a" / "sub" / "data", self.arc / "b" / "data")
        touch_tree(self.arc / "b", OLD)
        r = dg.trim_archive(str(self.arc), 10**12, now=NOW, log=quiet, remove=rm, stamp=MTIME)
        self.assertLess(r["bytes"], a + 64 * 1024)

    def test_removing_one_link_of_a_shared_file_frees_nothing_of_it(self):
        # a (oldest) holds a 256 KB file that b also links. Removing a frees only a's own bytes: the
        # shared file still counts while b links it, so b must go too before the archive is under.
        self.entry("a", 64, OLD)
        (self.arc / "a" / "shared").write_bytes(os.urandom(256 * 1024))
        (self.arc / "b").mkdir()
        os.link(self.arc / "a" / "shared", self.arc / "b" / "shared")
        touch_tree(self.arc / "a", OLD)
        touch_tree(self.arc / "b", OLD + 100)
        r = dg.trim_archive(str(self.arc), 128 * 1024, now=NOW, log=quiet, remove=rm, stamp=MTIME)
        self.assertEqual(list(self.arc.iterdir()), [])
        self.assertEqual(r["bytes"], 0)

    def test_flat_files_past_the_budget_remove_nothing(self):
        for n in ("a", "b", "c"):
            self.entry(n, 16, OLD, as_dir=False)
        r = dg.trim_archive(str(self.arc), 1, now=NOW, log=quiet, remove=rm, budget=-1,
                            stamp=MTIME)
        self.assertEqual(r["timeout"], 1)
        self.assertEqual(len(list(self.arc.iterdir())), 3)

    def test_wide_directory_hits_the_deadline_inside_the_scan(self):
        d = self.arc / "wide"
        d.mkdir()
        for i in range(600):
            (d / str(i)).write_bytes(b"")
        clock = iter(range(10**6))
        real = dg.time.monotonic
        dg.time.monotonic = lambda: next(clock)     # each check is one tick later
        try:
            with self.assertRaises(dg.ScanTimeout):
                list(dg.walk_lstat(str(d), os.lstat(d).st_dev, deadline=1))
        finally:
            dg.time.monotonic = real

    def test_scan_over_budget_removes_nothing(self):
        self.entry("a", 64, OLD)
        logs = []
        r = dg.trim_archive(str(self.arc), 1, now=NOW, log=logs.append, remove=rm, budget=-1,
                            stamp=MTIME)
        self.assertEqual((r["timeout"], r["removed"]), (1, 0))
        self.assertTrue((self.arc / "a").exists())
        self.assertIn("nothing removed", logs[0])

    def test_age_counts_ctime_so_a_moved_in_entry_is_young(self):
        f = self.root / "made-long-ago"
        f.write_bytes(b"x" * 4096)
        os.utime(f, (OLD, OLD))                         # mv keeps this mtime...
        os.rename(f, self.arc / "moved-in")             # ...but bumps the ctime
        dg.trim_archive(str(self.arc), 1, log=quiet, remove=rm)      # real clock, default stamp
        self.assertTrue((self.arc / "moved-in").exists())

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() != 0, "POSIX, not as root")
    def test_remove_as_owner_removes_a_tree(self):
        self.entry("a", 64, OLD)
        st = os.lstat(self.arc / "a")
        self.assertTrue(dg.remove_as_owner(str(self.arc / "a"), st.st_uid, st.st_gid))
        self.assertFalse((self.arc / "a").exists())

    def test_remove_as_owner_never_runs_as_root(self):
        self.entry("a", 64, OLD)
        self.assertFalse(dg.remove_as_owner(str(self.arc / "a"), 0, 0))
        self.assertTrue((self.arc / "a").exists())

    def test_each_entry_is_removed_as_its_own_owner(self):
        self.entry("a", 64, OLD)
        st_a = os.lstat(self.arc / "a")
        seen = []
        dg.trim_archive(str(self.arc), 1, now=NOW, log=quiet, stamp=MTIME,
                        remove=lambda p, u, g: seen.append((p, u, g)) or rm(p, u, g))
        self.assertEqual(seen, [(str(self.arc / "a"), st_a.st_uid, st_a.st_gid)])


class ReportTest(unittest.TestCase):
    def test_largest_dirs_once_an_hour(self):
        with tempfile.TemporaryDirectory() as d:
            stamp = os.path.join(d, "stamp")
            calls, logs = [], []

            def du(p):
                calls.append(p)
                return (f"{40 * 10**9}\t{p}/c4/archives\n{87 * 10**9}\t{p}/c4\n"
                        f"{112 * 10**9}\t{p}\n{7 * 10**9}\t{p}/docker\n")
            dg.report_largest(["/workspace"], log=logs.append, stamp=stamp, now=NOW, du=du, top=2)
            self.assertEqual(logs, ["disk-guard: largest under /workspace: /workspace/c4 87.0 GB, "
                                    "/workspace/c4/archives 40.0 GB"])
            dg.report_largest(["/workspace"], log=logs.append, stamp=stamp, now=NOW + 600, du=du)
            self.assertEqual(calls, ["/workspace"])                 # rate-limited
            dg.report_largest(["/"], log=logs.append, stamp=stamp, now=NOW + 600, du=du)
            self.assertEqual(calls, ["/workspace", "/"])            # per filesystem
            dg.report_largest(["/workspace"], log=logs.append, stamp=stamp, now=NOW + 3700, du=du)
            self.assertEqual(calls, ["/workspace", "/", "/workspace"])

    def test_du_timeout_is_logged_not_raised(self):
        with tempfile.TemporaryDirectory() as d:
            logs = []

            def du(p):
                raise dg.subprocess.TimeoutExpired(["du"], 300)
            dg.report_largest(["/workspace"], log=logs.append, stamp=os.path.join(d, "s"),
                              now=NOW, du=du)
            self.assertIn("du timed out", logs[0])

    def test_prom_carries_archive_metrics_when_capped(self):
        m = {"triggered": 0, "steps_run": 0, "failed_steps": 0, "reclaimed_bytes": 0,
             "exhausted": 0, "deferred": 0, "free_ratio": {"/": 0.5},
             "archive_bytes": 123, "archive_removed_bytes": 45, "archive_max_bytes": 678,
             "archive_scan_timeout": 1}
        text = dg.render_prom(m, 1700000000)
        self.assertIn("dev_worker_disk_guard_archive_bytes 123\n", text)
        self.assertIn("dev_worker_disk_guard_archive_removed_bytes 45\n", text)
        self.assertIn("dev_worker_disk_guard_archive_max_bytes 678\n", text)
        self.assertIn("dev_worker_disk_guard_archive_scan_timeout 1\n", text)
        del m["archive_bytes"], m["archive_removed_bytes"], m["archive_max_bytes"]
        self.assertNotIn("archive", dg.render_prom(m, 1700000000))

if __name__ == "__main__":
    unittest.main()
