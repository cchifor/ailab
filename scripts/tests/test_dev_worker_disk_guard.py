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
import pathlib
import tempfile
import unittest

_PATH = pathlib.Path(__file__).resolve().parents[2] / "ansible/roles/dev_worker/files/disk-guard"
_loader = importlib.machinery.SourceFileLoader("dw_disk_guard", str(_PATH))
_spec = importlib.util.spec_from_loader("dw_disk_guard", _loader)
dg = importlib.util.module_from_spec(_spec)
_loader.exec_module(dg)

# /home lives on /, /workspace is its own disk — the workers' layout.
FS = {"/": "/", "/home": "/", "/workspace": "/workspace"}


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

    def measure(self, p):
        return self.free[p]

    def avail(self, p):
        return int(self.free[p] * self.size)

    def run(self, step):
        self.ran.append(step.name)
        self.argv[step.name] = step.argv
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
        self.assertEqual(d.ran, ["worktrees", "deps"])
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
        self.assertEqual(d.ran, ["build-cache", "deps"])
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
                           (["docker", "compose", "up", "--build"], "start"),
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


if __name__ == "__main__":
    unittest.main()
