#!/usr/bin/env python3
"""Behavioural tests for gitea-runner-reap.py (the job-boundary leftover reaper).

Runs ON the live runner VMs (runner-scripts workflow) as the `runner` user, the same user act_runner
runs as. So nothing here may touch real host state: /proc, the cgroup tree, journalctl, docker,
systemctl, logger and every signal are injected fakes, and a guard asserts os.kill is never reached.

Usage: python3 ansible/roles/gitea_runner/tests/test-reap.py   (exit 0 = pass)
"""
import datetime
import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "files" / "gitea-runner-reap.py"
ENV_TEMPLATE = HERE.parent / "templates" / "gitea-runner-reclaim.env.j2"

BTIME = 1_700_000_000          # epoch of boot
NOW_BOOT = 50_000.0            # seconds since boot "now"
NOW = BTIME + NOW_BOOT
CLK = 100
MAINPID = 100
SERVICE = "gitea-act-runner.service"
WORKDIR = "/home/runner/act-runner/work"
TZ = datetime.timezone(datetime.timedelta(hours=3))
# The verbatim line captured from cloud-ci-9's journal on 2026-10-12, used as the format fixture.
CAPTURED = ('time="2026-10-12T13:56:15+03:00" level=info msg="task 434665 repo is cchifor/platform '
            'https://github.com https://git.chifor.me"')


def task_msg(boot_s, task=4242, repo="cchifor/platform"):
    t = datetime.datetime.fromtimestamp(BTIME + boot_s, TZ).strftime("%Y-%m-%dT%H:%M:%S%z")
    t = t[:-2] + ":" + t[-2:]
    return f'time="{t}" level=info msg="task {task} repo is {repo} https://github.com https://git.chifor.me"'


def iso(boot_s):
    return datetime.datetime.fromtimestamp(BTIME + boot_s, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.123456789Z")


FAKE_JOURNALCTL = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["FX"]
open(os.path.join(d, "journalctl.args"), "a").write(json.dumps(sys.argv[1:]) + "\n")
calls_path = os.path.join(d, "journal.calls"); n = int(open(calls_path).read()) if os.path.exists(calls_path) else 0
open(calls_path, "w").write(str(n + 1))
batches = json.load(open(os.path.join(d, "journal.json")))   # list of batches, one per call (last repeats)
entries = batches[min(n, len(batches) - 1)]
for e in entries:
    print(json.dumps(e))
'''

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["FX"]; a = sys.argv[1:]
open(os.path.join(d, "docker.log"), "a").write(" ".join(a) + "\n")
fx = json.load(open(os.path.join(d, "docker.json")))
def out(x): print(json.dumps(x))
if a[:1] == ["ps"]: print("\n".join(c["Id"] for c in fx["containers"]))
elif a[:1] == ["inspect"]: out([c for c in fx["containers"] if c["Id"] in a[1:]])
elif a[:2] == ["network", "ls"]: print("\n".join(n["Id"] for n in fx["networks"]))
elif a[:2] == ["network", "inspect"]: out([n for n in fx["networks"] if n["Id"] in a[2:]])
elif a[:2] == ["volume", "ls"]: print("\n".join(v["Name"] for v in fx["volumes"]))
elif a[:2] == ["volume", "inspect"]: out([v for v in fx["volumes"] if v["Name"] in a[2:]])
'''


class Fx:
    """A fake host: /proc, cgroup tree, journal, docker, systemctl, logger, kill."""

    def __init__(self, root):
        self.root = Path(root)
        self.proc = self.root / "proc"; self.proc.mkdir()
        (self.proc / "stat").write_text(f"cpu 1 2 3\nbtime {BTIME}\n")
        self.cg = self.root / "cg" / "system.slice" / SERVICE; self.cg.mkdir(parents=True)
        (self.cg / "cgroup.procs").write_text("")
        self.bin = self.root / "bin"; self.bin.mkdir()
        self.boot_id = self.root / "boot_id"; self.boot_id.write_text("0123456789abcdef0123456789abcdef\n")
        (self.root / "config.yaml").write_text("runner:\n  capacity: 1\n")
        self.textfile = self.root / "textfile"; self.textfile.mkdir()
        self.journal([[]]); self.docker()
        for name, body in (("journalctl", FAKE_JOURNALCTL), ("docker", FAKE_DOCKER)):
            (self.bin / name).write_text(body)
        (self.bin / "systemctl").write_text(f"#!/usr/bin/env bash\necho {MAINPID}\n")
        (self.bin / "logger").write_text('#!/usr/bin/env bash\nshift 3; printf "%s\\n" "$*" >> "$FX/logger.log"\n')
        (self.bin / "kill").write_text('#!/usr/bin/env bash\nprintf "%s %s\\n" "$1" "$2" >> "$FX/kill.log"\n')
        for f in self.bin.iterdir():
            f.chmod(0o755)
        self.add_proc(MAINPID, 1, 1000, comm="act_runner")

    def add_proc(self, pid, ppid, start_boot, state="S", comm="sleep", in_cgroup=True):
        d = self.proc / str(pid); d.mkdir(exist_ok=True)
        rest = [state, str(ppid)] + ["0"] * 17 + [str(int(start_boot * CLK))] + ["0"] * 5
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(rest) + "\n")
        (d / "cmdline").write_text(comm + "\0--flag\0")
        if in_cgroup:
            with open(self.cg / "cgroup.procs", "a") as fh:
                fh.write(f"{pid}\n")

    def journal(self, batches):
        (self.root / "journal.json").write_text(json.dumps(batches))

    def entry(self, boot_s, mono_lag=0.2, pid=MAINPID, msg=None, cursor=None):
        return {"MESSAGE": msg or task_msg(boot_s), "_PID": str(pid), "_COMM": "act_runner",
                "_BOOT_ID": "0123456789abcdef0123456789abcdef",
                "__MONOTONIC_TIMESTAMP": str(int((boot_s + mono_lag) * 1e6)),
                "__CURSOR": cursor or f"s=1;i={int(boot_s)}"}

    def docker(self, containers=(), networks=(), volumes=()):
        (self.root / "docker.json").write_text(json.dumps(
            {"containers": list(containers), "networks": list(networks), "volumes": list(volumes)}))

    def env(self, **over):
        e = {
            "FX": str(self.root), "GITEA_RECLAIM_ENV_FILE": "/nonexistent",
            "GITEA_RUNNER_SERVICE": SERVICE, "GITEA_RUNNER_WORKDIR_PARENT": WORKDIR,
            "GITEA_RUNNER_CONFIG_DIR": str(self.root), "GITEA_RUNNER_JOB_TIMEOUT": "3h",
            "GITEA_REAP_MODE": "log", "GITEA_REAP_TERM_WAIT_SEC": "0", "GITEA_REAP_STATE": str(self.root / "state.json"),
            "GITEA_RECLAIM_BEACON": "1", "GITEA_RECLAIM_TEXTFILE_DIR": str(self.textfile),
            "GITEA_CLEANUP_PEER_SERVICES": "none", "GITEA_CLEANUP_INFRA_EXCLUDE_RE": "buildkit|buildx",
            "GITEA_REAP_PROC_ROOT": str(self.proc), "GITEA_REAP_CGROUP_ROOT": str(self.root / "cg"),
            "GITEA_REAP_SYSTEMCTL": str(self.bin / "systemctl"), "GITEA_REAP_JOURNALCTL": str(self.bin / "journalctl"),
            "GITEA_REAP_DOCKER": str(self.bin / "docker"), "GITEA_REAP_LOGGER": str(self.bin / "logger"),
            "GITEA_REAP_KILL_CMD": str(self.bin / "kill"), "GITEA_REAP_BOOT_ID_FILE": str(self.boot_id),
            "GITEA_REAP_NOW": str(NOW), "GITEA_REAP_CLK_TCK": str(CLK),
        }
        e.update(over)
        return e

    def run(self, **over):
        env = self.env(**over)
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("os.kill", side_effect=AssertionError("a real signal was sent")):
            spec = importlib.util.spec_from_file_location(f"reap_{id(self)}_{len(over)}", SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            rc = mod.main()
        self.mod = mod
        return rc

    def text(self, name):
        p = self.root / name
        return p.read_text() if p.exists() else ""

    def kills(self):
        return [tuple(int(x) for x in l.split()) for l in self.text("kill.log").splitlines()]

    def state(self):
        return json.loads(self.text("state.json") or "{}")


def ctr(cid, name, created, started, image="alpine", labels=None, mounts=(), networks=()):
    return {"Id": cid, "Name": "/" + name, "Created": iso(created), "State": {"StartedAt": iso(started) if started is not None else "0001-01-01T00:00:00Z"},
            "Config": {"Image": image, "Labels": labels or {}}, "Mounts": list(mounts),
            "NetworkSettings": {"Networks": {n: {} for n in networks}}}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = Fx(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class BoundaryTests(Base):
    def test_captured_line_parses_with_its_offset(self):
        m = __import__("re").match  # the module regex is checked through a full run below
        self.fx.journal([[self.fx.entry(0, msg=CAPTURED)]])
        self.fx.run()
        st = self.fx.state()
        logged = datetime.datetime.fromisoformat("2026-10-12T13:56:15+03:00").timestamp()
        self.assertEqual(st["b_task"], "434665")
        self.assertEqual(st["b_repo"], "cchifor/platform")
        # the earlier of (logrus time, journald monotonic) minus the 5s margin; monotonic here is 0.2s
        self.assertAlmostEqual(st["b_boot"], min(logged - BTIME, 0.2) - 5, places=3)

    def test_receive_lag_never_pushes_the_boundary_later(self):
        self.fx.journal([[self.fx.entry(49_000, mono_lag=30.0)]])  # journald received the line 30s late
        self.fx.run()
        self.assertAlmostEqual(self.fx.state()["b_boot"], 49_000 - 5, places=3)

    def test_unanchored_or_forged_line_is_ignored(self):
        forged = "x " + task_msg(49_000)  # not anchored at ^time=
        self.fx.journal([[self.fx.entry(49_000, msg=forged)]])
        self.fx.run()
        self.assertNotIn("b_boot", self.fx.state())
        args = self.fx.text("journalctl.args")
        self.assertIn(f"_PID={MAINPID}", args)
        self.assertIn("_COMM=act_runner", args)
        self.assertIn(f"_SYSTEMD_UNIT={SERVICE}", args)

    def test_no_task_line_uses_only_the_timeout_floor(self):
        # floor = now - (3h + 600s) = 38_600 boot-seconds
        self.fx.add_proc(200, 1, 30_000)   # older than the floor: a leftover
        self.fx.add_proc(201, 1, 45_000)   # 1.4h old: could belong to a live job -> kept
        self.fx.run(GITEA_REAP_MODE="enforce")
        self.assertEqual(sorted({p for _, p in self.fx.kills()}), [200])

    def test_capacity_other_than_one_refuses(self):
        (self.fx.root / "config.yaml").write_text("runner:\n  capacity: 2\n")
        self.fx.add_proc(200, 1, 10)
        self.fx.journal([[self.fx.entry(49_000)]])
        self.fx.run(GITEA_REAP_MODE="enforce")
        self.assertEqual(self.fx.kills(), [])
        self.assertIn("gitea_runner_reap_refused 1", (self.fx.textfile / "gitea_runner_reap.prom").read_text())

    def test_boundary_moving_mid_pass_is_recomputed(self):
        # first read: task at 48_000; the fresh-start guard's re-read sees a NEWER task at 49_000.
        self.fx.journal([[self.fx.entry(48_000)], [self.fx.entry(49_000, cursor="s=1;i=49001")], []])
        self.fx.add_proc(200, 1, 48_500)   # after the old B, before the new one -> a leftover of the old job
        self.fx.run(GITEA_REAP_MODE="enforce")
        self.assertIn(200, {p for _, p in self.fx.kills()})
        self.assertAlmostEqual(self.fx.state()["b_boot"], 49_000 - 5, places=3)


class ProcessTests(Base):
    def setUp(self):
        super().setUp()
        self.fx.journal([[self.fx.entry(49_000)]])               # B ~= 48_995
        self.fx.add_proc(101, MAINPID, 10)                        # the live job's step: descendant
        self.fx.add_proc(200, 1, 20_000)                          # 57h-style leftover, re-parented
        self.fx.add_proc(201, 1, 49_500)                          # started after B: the job's own server
        self.fx.add_proc(202, 1, 20_000, state="Z")               # zombie
        self.fx.add_proc(203, 999_999, 49_100)                    # broken parent chain, after B (kept)

    def test_log_mode_reports_and_signals_nothing(self):
        self.fx.run(GITEA_REAP_MODE="log")
        self.assertEqual(self.fx.kills(), [])
        log = self.fx.text("logger.log")
        self.assertIn("would-reap process pid=200", log)
        for pid in (101, 201, 202, 203):
            self.assertNotIn(f"pid={pid} ", log)

    def test_enforce_terms_then_kills_only_the_leftover(self):
        self.fx.run(GITEA_REAP_MODE="enforce")
        # one pass per tick (the edge pass covers the periodic one); still "alive" after the 0s wait
        self.assertEqual(self.fx.kills(), [(15, 200), (9, 200)])

    def test_periodic_pass_uses_only_the_timeout_floor(self):
        self.fx.run(GITEA_REAP_MODE="log")                                  # tick 1: the edge pass
        (self.fx.root / "logger.log").unlink()
        self.fx.add_proc(204, 1, 40_000)   # before B, but younger than the floor (38_600 + 900s later)
        self.fx.run(GITEA_REAP_MODE="log", GITEA_REAP_NOW=str(NOW + 900))  # tick 2: periodic, no new task
        log = self.fx.text("logger.log")
        self.assertIn("periodic", log)
        self.assertIn("pid=200", log)          # older than the floor: still reported
        self.assertNotIn("pid=204", log)       # a long job could have adopted it; periodic never uses B

    def test_pid_reuse_between_snapshot_and_signal_is_skipped(self):
        real_stat = None

        def run_with_reuse():
            env = self.fx.env(GITEA_REAP_MODE="enforce")
            with mock.patch.dict(os.environ, env), mock.patch("os.kill", side_effect=AssertionError):
                spec = importlib.util.spec_from_file_location("reap_reuse", SCRIPT)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                orig = mod.process_candidates

                def snapshot_then_reuse(mainpid, e_boot):
                    out = orig(mainpid, e_boot)
                    self.fx.add_proc(200, 1, 49_900, in_cgroup=False)  # pid 200 now a NEW process
                    return out
                mod.process_candidates = snapshot_then_reuse
                mod.main()
        run_with_reuse()
        self.assertEqual(self.fx.kills(), [])

    def test_no_real_signal_path_without_the_kill_cmd(self):
        # Guard: with KILL_CMD unset, the code path would call os.kill; the patch turns that into a crash
        # that main() swallows only at __main__, so calling main() directly must raise here.
        with self.assertRaises(AssertionError):
            self.fx.run(GITEA_REAP_MODE="enforce", GITEA_REAP_KILL_CMD="")


class DockerTests(Base):
    def setUp(self):
        super().setUp()
        self.fx.journal([[self.fx.entry(49_000)]])

    def candidates(self, **env):
        self.fx.run(**env)
        return [l for l in self.fx.text("logger.log").splitlines() if l.startswith("would-reap")]

    def test_container_rules(self):
        self.fx.docker(containers=[
            ctr("c1", "old-leftover", 20_000, 20_000),
            ctr("c2", "taken-over", 20_000, 49_200),                       # restarted by the current job
            ctr("c3", "new", 49_300, 49_300),
            ctr("c4", "buildx_buildkit_builder0", 100, 100, image="moby/buildkit:buildx-stable-1"),
            ctr("c5", "dsh-conductor-image-pin", 100, 100, labels={"io.dsh.conductor.image-pin": "0.1.0"}),
            ctr("c6", "kept-by-label", 100, 100, labels={"ci.keep": "true"}),
            ctr("c7", "never-started", 20_000, None),
        ])
        lines = "\n".join(self.candidates())
        self.assertIn("would-reap container old-leftover", lines)
        self.assertIn("would-reap container never-started", lines)
        for name in ("taken-over", " new ", "buildx_buildkit", "image-pin", "kept-by-label"):
            self.assertNotIn(name, lines)

    def test_pin_kept_by_the_host_var_regex_alone(self):
        self.fx.docker(containers=[ctr("c5", "dsh-conductor-image-pin", 100, 100, image="sha256:318b8ae52ecf")])
        lines = self.candidates(GITEA_CLEANUP_INFRA_EXCLUDE_RE="buildkit|buildx| /dsh-conductor-image-pin$")
        self.assertEqual(lines, [])

    def test_peer_hosts_reap_only_workdir_attributed_containers(self):
        self.fx.docker(containers=[
            ctr("a", "compose-in-workdir", 100, 100, labels={"com.docker.compose.project.working_dir": WORKDIR + "/abc/hostexecutor"}),
            ctr("b", "bind-in-workdir", 100, 100, mounts=[{"Type": "bind", "Source": WORKDIR + "/abc/hostexecutor"}]),
            ctr("c", "github-or-unknown", 100, 100),
            ctr("d", "dotdot-escape", 100, 100, labels={"com.docker.compose.project.working_dir": WORKDIR + "/../../etc"}),
        ])
        lines = "\n".join(self.candidates(GITEA_CLEANUP_PEER_SERVICES="actions.runner.cchifor-platform.service"))
        self.assertIn("compose-in-workdir", lines)
        self.assertIn("bind-in-workdir", lines)
        self.assertNotIn("github-or-unknown", lines)
        self.assertNotIn("dotdot-escape", lines)

    def test_volume_and_network_rules(self):
        self.fx.docker(
            containers=[ctr("live", "live-job-db", 49_300, 49_300, mounts=[{"Type": "volume", "Name": "kept_used"}], networks=["usednet"]),
                        ctr("old", "old-db", 20_000, 20_000, mounts=[{"Type": "volume", "Name": "via_candidate"}])],
            volumes=[{"Name": "platform_pgdata", "CreatedAt": iso(20_000), "Labels": {"com.docker.compose.project": "platform"}},
                     {"Name": "a" * 64, "CreatedAt": iso(20_000), "Labels": {"com.docker.volume.anonymous": ""}},
                     {"Name": "named_cache", "CreatedAt": iso(20_000), "Labels": {}},
                     {"Name": "kept_used", "CreatedAt": iso(20_000), "Labels": {"com.docker.compose.project": "x"}},
                     {"Name": "via_candidate", "CreatedAt": iso(20_000), "Labels": {"com.docker.compose.project": "y"}},
                     {"Name": "new_vol", "CreatedAt": iso(49_400), "Labels": {"com.docker.compose.project": "z"}},
                     {"Name": "keep_vol", "CreatedAt": iso(20_000), "Labels": {"com.docker.compose.project": "w", "ci.keep": ""}}],
            networks=[{"Id": "n1", "Name": "hostexecutor_default", "Created": iso(20_000), "Labels": {"com.docker.compose.project": "hostexecutor"}, "Containers": {}},
                      {"Id": "n2", "Name": "usednet", "Created": iso(20_000), "Labels": {"com.docker.compose.project": "u"}, "Containers": {"live": {}}},
                      {"Id": "n3", "Name": "bridge", "Created": iso(10), "Labels": {}, "Containers": {}}])
        lines = "\n".join(self.candidates())
        for name in ("platform_pgdata", "a" * 64, "via_candidate", "hostexecutor_default"):
            self.assertIn(name, lines)
        for name in ("named_cache", "kept_used", "new_vol", "keep_vol", "usednet", "bridge"):
            self.assertNotIn(name, lines)

    def test_enforce_removes_with_docker_and_log_never_does(self):
        self.fx.docker(containers=[ctr("c1", "old-leftover", 20_000, 20_000)],
                       volumes=[{"Name": "platform_x", "CreatedAt": iso(20_000), "Labels": {"com.docker.compose.project": "platform"}}])
        self.fx.run(GITEA_REAP_MODE="log")
        removals = [l for l in self.fx.text("docker.log").splitlines()
                    if l.startswith(("rm ", "network rm ", "volume rm "))]
        self.assertEqual(removals, [], "log mode must never remove anything")
        (self.fx.root / "state.json").unlink()
        self.fx.run(GITEA_REAP_MODE="enforce")
        log = self.fx.text("docker.log")
        self.assertIn("rm -f c1", log)
        self.assertIn("volume rm platform_x", log)


class AdoptionAndBeaconTests(Base):
    def test_newer_container_joining_a_would_reap_network_counts_as_adopted(self):
        self.fx.journal([[self.fx.entry(49_000)], []])
        self.fx.docker(networks=[{"Id": "n1", "Name": "hostexecutor_default", "Created": iso(20_000),
                                  "Labels": {"com.docker.compose.project": "hostexecutor"}, "Containers": {}}])
        self.fx.run()
        self.assertEqual(self.fx.state()["adopted_total"], 0)
        # the job that started at B now runs compose, which reuses the leftover network
        self.fx.docker(containers=[ctr("j", "job-web", 49_100, 49_100, networks=["hostexecutor_default"])],
                       networks=[{"Id": "n1", "Name": "hostexecutor_default", "Created": iso(20_000),
                                  "Labels": {"com.docker.compose.project": "hostexecutor"}, "Containers": {"j": {}}}])
        self.fx.run(GITEA_REAP_NOW=str(NOW + 60))
        self.assertEqual(self.fx.state()["adopted_total"], 1)
        self.assertIn("ADOPTED would-reap network hostexecutor_default", self.fx.text("logger.log"))

    def test_beacon_fields(self):
        self.fx.journal([[self.fx.entry(49_000)]])
        self.fx.add_proc(200, 1, 20_000)
        self.fx.run()
        prom = (self.fx.textfile / "gitea_runner_reap.prom").read_text()
        self.assertIn("gitea_runner_reap_mode 1", prom)
        self.assertIn("gitea_runner_reap_refused 0", prom)
        self.assertRegex(prom, r"gitea_runner_reap_boundary_age_seconds 100[0-9]")
        self.assertIn('gitea_runner_reap_last_candidates{kind="process"} 1', prom)
        self.assertIn('gitea_runner_reap_last_reaped{kind="process"} 0', prom)
        self.assertIn("gitea_runner_reap_adopted_total 0", prom)

    def test_missing_boundary_reports_minus_one(self):
        self.fx.run()
        prom = (self.fx.textfile / "gitea_runner_reap.prom").read_text()
        self.assertIn("gitea_runner_reap_boundary_age_seconds -1", prom)


class EnvDriftTests(unittest.TestCase):
    INJECTION_ONLY = {"GITEA_REAP_PROC_ROOT", "GITEA_REAP_CGROUP_ROOT", "GITEA_REAP_SYSTEMCTL",
                      "GITEA_REAP_JOURNALCTL", "GITEA_REAP_DOCKER", "GITEA_REAP_LOGGER", "GITEA_REAP_KILL_CMD",
                      "GITEA_REAP_BOOT_ID_FILE", "GITEA_REAP_NOW", "GITEA_REAP_CLK_TCK",
                      # tunables deliberately left at their script defaults (no role var):
                      "GITEA_REAP_PROBE_SEC", "GITEA_REAP_TERM_WAIT_SEC"}

    def test_every_tunable_the_reaper_reads_is_rendered_in_the_env_file(self):
        src = SCRIPT.read_text()
        keys = set(re.findall(r'cfg\("([A-Z_]+)"', src)) - self.INJECTION_ONLY
        rendered = set(re.findall(r"^([A-Z_]+)=", ENV_TEMPLATE.read_text(), re.M))
        self.assertEqual(sorted(keys - rendered), [], "reaper tunables missing from gitea-runner-reclaim.env.j2")


if __name__ == "__main__":
    unittest.main(verbosity=1)
