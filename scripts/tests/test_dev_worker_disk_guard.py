"""Tests for the dev-worker `disk-guard` (ansible/roles/dev_worker/files/disk-guard).

The guard's contract, pinned here:
  * Above --low on every watched fs it runs nothing (and writes a heartbeat).
  * Under --low it runs the ladder in order and stops as soon as every low fs is >= --target.
  * Docker steps run only when the docker filesystem is the low one: a full / is not fixed by
    pruning /workspace's build cache.
  * The whole-worktree step exists only with --worktrees-remove (the role's prune mode).
  * Every step ran and a fs is still under --low → exhausted=1 (the alert's signal).
  * --dry-run runs nothing.
  * While a docker client builds/pulls, docker steps wait (a prune's containerd GC kills in-flight
    pulls) — unless the docker fs is under --critical. Waiting is not "exhausted".
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


class Disk:
    """Free fractions per path; each executed step frees `gain[step]` on `fs[step]`."""

    def __init__(self, free, gain=None, size=100 * 10**9):
        self.free = dict(free)
        self.gain = gain or {}
        self.size = size
        self.ran = []

    def measure(self, p):
        return self.free[p]

    def avail(self, p):
        return int(self.free[p] * self.size)

    def run(self, step):
        self.ran.append(step.name)
        for p, g in self.gain.get(step.name, {}).items():
            self.free[p] = min(1.0, self.free[p] + g)
        return 0


def opts(*extra, same_fs=lambda a, b: a == b):
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


class _Shared(dict):
    """Every key reads and writes the same value: two paths on one filesystem."""

    def __getitem__(self, _k):
        return dict.__getitem__(self, "/")

    def __setitem__(self, _k, v):
        dict.__setitem__(self, "/", v)

    def items(self):
        return [(k, self["/"]) for k in self.keys()]


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

    def test_root_fs_low_skips_docker_steps(self):
        d = Disk({"/": 0.05, "/workspace": 0.60})
        dg.guard(opts("--worktrees-remove"), d.measure, d.avail, d.run, quiet, idle)
        self.assertEqual(d.ran, ["worktrees", "deps"])

    def test_worktree_step_only_in_remove_mode(self):
        d = Disk({"/": 0.40, "/workspace": 0.01})
        dg.guard(opts(), d.measure, d.avail, d.run, quiet, idle)
        self.assertNotIn("worktrees", d.ran)

    def test_exhausted_when_still_low(self):
        d = Disk({"/": 0.40, "/workspace": 0.02}, gain={"deps": {"/workspace": 0.05}})
        m = dg.guard(opts(), d.measure, d.avail, d.run, quiet, idle)
        self.assertEqual(m["exhausted"], 1)

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

    def test_busy_argv(self):
        for argv in (["/usr/bin/docker", "build", "."], ["docker", "compose", "up", "-d"],
                     ["/usr/libexec/docker/cli-plugins/docker-buildx", "buildx", "build", "."],
                     ["docker", "run", "--rm", "postgres"], ["docker", "pull", "x"]):
            self.assertTrue(dg.busy_argv(argv), argv)
        for argv in (["docker", "ps"], ["docker", "logs", "-f", "x"], ["dockerd"],
                     ["containerd-shim-runc-v2", "-namespace", "moby"], ["python3", "run"],
                     ["docker", "compose", "logs", "-f"]):
            self.assertFalse(dg.busy_argv(argv), argv)

    def test_docker_busy_reads_proc(self):
        with tempfile.TemporaryDirectory() as proc:
            for pid, argv in (("10", ["bash"]), ("11", ["/usr/bin/docker", "pull", "pg"]),
                              ("self", ["x"])):
                (pathlib.Path(proc) / pid).mkdir()
                (pathlib.Path(proc) / pid / "cmdline").write_bytes(
                    b"".join(a.encode() + bytes([0]) for a in argv))
            self.assertEqual(dg.docker_busy(proc), "/usr/bin/docker pull pg")
            (pathlib.Path(proc) / "11" / "cmdline").write_bytes(b"docker" + bytes([0]) + b"ps")
            self.assertEqual(dg.docker_busy(proc), "")

    def test_cleanup_lock_held_is_deferred_not_exhausted(self):
        d = Disk({"/": 0.40, "/workspace": 0.02})
        def locked(step):
            d.ran.append(step.name)
            return dg.LOCKED if step.argv[0].endswith("cleanup") else 0
        m = dg.guard(opts(), d.measure, d.avail, locked, quiet, idle)
        self.assertEqual(m["steps_run"], 2)     # build-cache, anon-volumes; cleanup steps locked
        self.assertEqual((m["deferred"], m["exhausted"]), (1, 0))

    def test_reclaimed_bytes_counted_once_per_filesystem(self):
        d = Disk({"/": 0.05, "/workspace": 0.05}, gain={"build-cache": {"/": 0.30}})
        d.free = _Shared(d.free)                # both paths are one filesystem
        m = dg.guard(opts(same_fs=lambda a, b: True), d.measure, d.avail, d.run, quiet, idle)
        self.assertEqual(m["reclaimed_bytes"], int(0.35 * 100 * 10**9) - int(0.05 * 100 * 10**9))

    def test_argument_validation(self):
        with self.assertRaises(SystemExit):
            dg.parse_args(["--low", "30", "--target", "20"])
        with self.assertRaises(SystemExit):
            dg.parse_args(["--critical", "20", "--low", "15"])
        with self.assertRaises(SystemExit):
            dg.parse_args(["--watch", "/", "--docker-fs", "/workspace"])

    def test_prom_render_and_atomic_write(self):
        m = {"triggered": 1, "steps_run": 2, "reclaimed_bytes": 5, "exhausted": 0, "deferred": 0,
             "free_ratio": {"/": 0.5, "/workspace": 0.26}}
        text = dg.render_prom(m, 1700000000)
        self.assertIn("dev_worker_disk_guard_last_run_timestamp_seconds 1700000000\n", text)
        self.assertIn('dev_worker_disk_guard_free_ratio{mountpoint="/workspace"} 0.2600\n', text)
        with tempfile.TemporaryDirectory() as d:
            dg.write_prom(text, d)
            files = list(pathlib.Path(d).iterdir())
            self.assertEqual([f.name for f in files], [dg.PROM])
            self.assertEqual(files[0].read_text(), text)


if __name__ == "__main__":
    unittest.main()
