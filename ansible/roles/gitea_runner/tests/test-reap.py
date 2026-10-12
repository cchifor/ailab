#!/usr/bin/env python3
"""Behavioural tests for gitea-runner-reap.py (the job-boundary leftover reaper).

Runs ON the live runner VMs (runner-scripts workflow) as `runner`, the user act_runner runs as. Nothing
may touch real host state: /proc, the cgroup tree, journalctl, docker, systemctl, logger and every
signal are injected fakes, os.kill is patched to raise, and every fixture PID is above the kernel's
pid_max, so even a broken patch could not signal a real process.

Usage: python3 ansible/roles/gitea_runner/tests/test-reap.py   (exit 0 = pass)
"""
import datetime
import importlib.util
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "files" / "gitea-runner-reap.py"
ENV_TEMPLATE = HERE.parent / "templates" / "gitea-runner-reclaim.env.j2"

BTIME = 1_700_000_000          # epoch of boot
NOW_BOOT = 50_000.0            # seconds since boot at "now"
NOW = BTIME + NOW_BOOT
CLK = 100
P = 9_000_000                  # > pid_max (4194304): no fixture PID can name a real process
MAINPID = P + 100
SERVICE = "gitea-act-runner.service"
WORKDIR = "/home/runner/act-runner/work"
TZ = datetime.timezone(datetime.timedelta(hours=3))
# Verbatim from cloud-ci-9's journal on 2026-10-12: the format fixture.
CAPTURED = ('time="2026-10-12T13:56:15+03:00" level=info msg="task 434665 repo is cchifor/platform '
            'https://github.com https://git.chifor.me"')


def task_msg(boot_s, task=4242, repo="cchifor/platform"):
    t = datetime.datetime.fromtimestamp(BTIME + boot_s, TZ).strftime("%Y-%m-%dT%H:%M:%S%z")
    return f'time="{t[:-2]}:{t[-2:]}" level=info msg="task {task} repo is {repo} https://github.com https://git.chifor.me"'


def iso(boot_s):
    return datetime.datetime.fromtimestamp(BTIME + boot_s, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


FAKE_JOURNALCTL = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["FX"]
open(os.path.join(d, "journalctl.args"), "a").write(json.dumps(sys.argv[1:]) + "\n")
if "--after-cursor" in sys.argv and os.path.exists(os.path.join(d, "cursor.fail")):
    sys.exit(1)
cp = os.path.join(d, "journal.calls"); n = int(open(cp).read()) if os.path.exists(cp) else 0
open(cp, "w").write(str(n + 1))
batches = json.load(open(os.path.join(d, "journal.json")))
for e in batches[min(n, len(batches) - 1)]:
    print(json.dumps(e))
'''

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["FX"]; a = sys.argv[1:]
open(os.path.join(d, "docker.log"), "a").write(" ".join(a) + "\n")
fx = json.load(open(os.path.join(d, "docker.json")))
def out(x): print(json.dumps(x))
if a[:1] == ["ps"]: print("\n".join(c["Id"] for c in fx["containers"]))
elif a[:1] == ["inspect"]:
    if fx.get("inspect_fail"): sys.exit(1)
    out([c for c in fx["containers"] if c["Id"] in a[1:]])
elif a[:2] == ["network", "ls"]: print("\n".join(n["Id"] for n in fx["networks"]))
elif a[:2] == ["network", "inspect"]: out([n for n in fx["networks"] if n["Id"] in a[2:]])
elif a[:2] == ["volume", "ls"]: print("\n".join(v["Name"] for v in fx["volumes"]))
elif a[:2] == ["volume", "inspect"]: out([v for v in fx["volumes"] if v["Name"] in a[2:]])
'''


class Fx:
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
        (self.bin / "journalctl").write_text(FAKE_JOURNALCTL)
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "systemctl").write_text(f"#!/usr/bin/env bash\necho {MAINPID}\n")
        (self.bin / "logger").write_text('#!/usr/bin/env bash\nshift 3; printf "%s\\n" "$*" >> "$FX/logger.log"\n')
        (self.bin / "kill").write_text('#!/usr/bin/env bash\nprintf "%s %s\\n" "$1" "$2" >> "$FX/kill.log"\n')
        for f in self.bin.iterdir():
            f.chmod(0o755)
        self.add_proc(MAINPID, 1, 1000, comm="act_runner")

    def add_proc(self, pid, ppid, start_boot, state="S", comm="sleep", in_cgroup=True, cpu=0):
        d = self.proc / str(pid); d.mkdir(exist_ok=True)
        rest = [state, str(ppid)] + ["0"] * 9 + [str(cpu), "0"] + ["0"] * 6 + [str(int(start_boot * CLK))] + ["0"] * 5
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(rest) + "\n")
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_text(comm + "\0--port=4786\0--token\0hunter2\0")
        if in_cgroup:
            with open(self.cg / "cgroup.procs", "a") as fh:
                fh.write(f"{pid}\n")

    def journal(self, batches):
        (self.root / "journal.json").write_text(json.dumps(batches))

    def entry(self, boot_s, mono_lag=0.2, msg=None, cursor=None):
        return {"MESSAGE": msg or task_msg(boot_s), "_PID": str(MAINPID), "_COMM": "act_runner",
                "_BOOT_ID": "0123456789abcdef0123456789abcdef",
                "__MONOTONIC_TIMESTAMP": str(int((boot_s + mono_lag) * 1e6)),
                "__CURSOR": cursor or f"s=1;i={int(boot_s)}"}

    def docker(self, containers=(), networks=(), volumes=(), inspect_fail=False):
        (self.root / "docker.json").write_text(json.dumps(
            {"containers": list(containers), "networks": list(networks), "volumes": list(volumes),
             "inspect_fail": inspect_fail}))

    def env(self, **over):
        e = {"FX": str(self.root), "GITEA_RECLAIM_ENV_FILE": "/nonexistent",
             "GITEA_RUNNER_SERVICE": SERVICE, "GITEA_RUNNER_WORKDIR_PARENT": WORKDIR,
             "GITEA_RUNNER_CONFIG_DIR": str(self.root), "GITEA_RUNNER_JOB_TIMEOUT": "3h",
             "GITEA_REAP_MODE": "log", "GITEA_REAP_TERM_WAIT_SEC": "0", "GITEA_REAP_STATE": str(self.root / "state.json"),
             "GITEA_RECLAIM_BEACON": "1", "GITEA_RECLAIM_TEXTFILE_DIR": str(self.textfile),
             "GITEA_CLEANUP_PEER_SERVICES": "none", "GITEA_CLEANUP_INFRA_EXCLUDE_RE": "buildkit|buildx",
             "GITEA_REAP_PROC_ROOT": str(self.proc), "GITEA_REAP_CGROUP_ROOT": str(self.root / "cg"),
             "GITEA_REAP_SYSTEMCTL": str(self.bin / "systemctl"), "GITEA_REAP_JOURNALCTL": str(self.bin / "journalctl"),
             "GITEA_REAP_DOCKER": str(self.bin / "docker"), "GITEA_REAP_LOGGER": str(self.bin / "logger"),
             "GITEA_REAP_KILL_CMD": str(self.bin / "kill"), "GITEA_REAP_BOOT_ID_FILE": str(self.boot_id),
             "GITEA_REAP_NOW": str(NOW), "GITEA_REAP_CLK_TCK": str(CLK)}
        e.update(over)
        return e

    def load(self, **over):
        env = self.env(**over)
        p = mock.patch.dict(os.environ, env)
        p.start()
        try:
            spec = importlib.util.spec_from_file_location(f"reap_{id(self)}_{len(os.listdir(self.root))}", SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            p.stop()
        return mod, env

    def run(self, **over):
        mod, env = self.load(**over)
        with mock.patch.dict(os.environ, env), mock.patch("os.kill", side_effect=AssertionError("a real signal was sent")):
            return mod.main()

    def text(self, name):
        p = self.root / name
        return p.read_text() if p.exists() else ""

    def kills(self):
        return [tuple(int(x) for x in l.split()) for l in self.text("kill.log").splitlines()]

    def state(self):
        return json.loads(self.text("state.json") or "{}")

    def prom(self):
        p = self.textfile / "gitea_runner_reap.prom"
        return p.read_text() if p.exists() else ""

    def would(self):
        return [l for l in self.text("logger.log").splitlines() if l.startswith("would-reap")]


def ctr(cid, name, created, started, image="alpine", labels=None, mounts=(), networks=()):
    return {"Id": cid, "Name": "/" + name, "Created": iso(created),
            "State": {"StartedAt": iso(started) if started is not None else "0001-01-01T00:00:00Z"},
            "Config": {"Image": image, "Labels": labels or {}}, "Mounts": list(mounts),
            "NetworkSettings": {"Networks": {n: {} for n in networks}}}


def compose(project, service, workdir=WORKDIR + "/abc/hostexecutor"):
    return {"com.docker.compose.project": project, "com.docker.compose.service": service,
            "com.docker.compose.project.working_dir": workdir}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = Fx(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class BoundaryTests(Base):
    def test_captured_line_offset_is_parsed(self):
        # a large receive lag makes the logrus time the earlier estimate, so the +03:00 offset decides B
        logged = datetime.datetime.fromisoformat("2026-10-12T13:56:15+03:00").timestamp()
        entry = self.fx.entry(0, msg=CAPTURED)
        entry["__MONOTONIC_TIMESTAMP"] = str(int((logged - BTIME + 3600) * 1e6))
        self.fx.journal([[entry]])
        self.fx.run()
        st = self.fx.state()
        self.assertEqual((st["b_task"], st["b_repo"]), ("434665", "cchifor/platform"))
        self.assertAlmostEqual(st["b_boot"], logged - BTIME - 5, places=3)

    def test_receive_lag_never_pushes_the_boundary_later(self):
        self.fx.journal([[self.fx.entry(49_990, mono_lag=30.0)]])
        self.fx.run()
        self.assertAlmostEqual(self.fx.state()["b_boot"], 49_990 - 5, places=3)

    def test_unanchored_line_is_ignored_and_filters_are_passed(self):
        self.fx.journal([[self.fx.entry(49_990, msg="x " + task_msg(49_990))]])
        self.fx.run()
        self.assertNotIn("b_boot", self.fx.state())
        args = self.fx.text("journalctl.args")
        for f in (f"_PID={MAINPID}", "_COMM=act_runner", f"_SYSTEMD_UNIT={SERVICE}"):
            self.assertIn(f, args)

    def test_no_task_line_uses_only_the_timeout_floor(self):
        self.fx.add_proc(P + 200, 1, 30_000)   # older than the floor (38_600)
        self.fx.add_proc(P + 201, 1, 45_000)   # 1.4h old: could belong to a live job
        self.fx.run(GITEA_REAP_MODE="enforce")
        self.assertEqual(sorted({p for _, p in self.fx.kills()}), [P + 200])

    def test_late_edge_falls_back_to_the_floor(self):
        self.fx.journal([[self.fx.entry(49_000)]])   # B is 1000s old at the first tick (a deploy, a reboot)
        self.fx.add_proc(P + 200, 1, 45_000)         # before B, after the floor: the late edge must not take it
        self.fx.run(GITEA_REAP_MODE="enforce")
        self.assertEqual(self.fx.kills(), [])
        self.assertIn("late-edge", self.fx.state()["last_pass"]["kind"])

    def test_refusals_capacity_comm_and_logged_once(self):
        (self.fx.root / "config.yaml").write_text("runner:\n  capacity: 2\n")
        self.fx.add_proc(P + 200, 1, 10)
        self.fx.run(GITEA_REAP_MODE="enforce")
        self.fx.run(GITEA_REAP_MODE="enforce", GITEA_REAP_NOW=str(NOW + 5))
        self.assertEqual(self.fx.kills(), [])
        self.assertEqual(self.fx.text("logger.log").count("refuse: act_runner capacity is not 1"), 1)
        self.assertIn("gitea_runner_reap_refused 1", self.fx.prom())
        (self.fx.root / "config.yaml").write_text("runner:\n  capacity: 1\n")
        (self.fx.proc / str(MAINPID) / "comm").write_text("bash\n")
        self.fx.run(GITEA_REAP_MODE="enforce", GITEA_REAP_NOW=str(NOW + 10))
        self.assertIn("not act_runner", self.fx.text("logger.log"))
        self.assertEqual(self.fx.kills(), [])

    def test_boundary_moving_mid_pass_is_recomputed(self):
        self.fx.journal([[self.fx.entry(49_985)], [self.fx.entry(49_990, cursor="s=1;i=49991")], []])
        self.fx.add_proc(P + 200, 1, 49_983)   # after the old B (49_980), before the new one (49_985)
        self.fx.run(GITEA_REAP_MODE="enforce")
        self.assertIn(P + 200, {p for _, p in self.fx.kills()})
        self.assertAlmostEqual(self.fx.state()["b_boot"], 49_990 - 5, places=3)

    def test_dead_cursor_is_cleared(self):
        self.fx.journal([[self.fx.entry(49_990)], []])
        self.fx.run()
        self.assertIn("cursor", self.fx.state())
        (self.fx.root / "cursor.fail").write_text("")
        self.fx.run(GITEA_REAP_NOW=str(NOW + 5))
        self.assertNotIn("cursor", self.fx.state())


class ProcessTests(Base):
    def setUp(self):
        super().setUp()
        self.fx.journal([[self.fx.entry(49_990)]])      # B ~= 49_985, a fresh edge
        self.fx.add_proc(P + 101, MAINPID, 10)          # the live job's step: descendant
        self.fx.add_proc(P + 200, 1, 20_000)            # a leftover, re-parented
        self.fx.add_proc(P + 201, 1, 49_995)            # started after B: the job's own server
        self.fx.add_proc(P + 202, 1, 20_000, state="Z")
        self.fx.add_proc(P + 203, 99_999_999, 49_992)   # broken parent chain, after B

    def test_log_mode_reports_masked_and_signals_nothing(self):
        self.fx.run()
        self.assertEqual(self.fx.kills(), [])
        log = self.fx.text("logger.log")
        self.assertIn(f"would-reap process pid={P + 200}", log)
        self.assertNotIn("hunter2", log)
        for pid in (P + 101, P + 201, P + 202, P + 203):
            self.assertNotIn(f"pid={pid} ", log)

    def test_enforce_terms_then_kills_only_the_leftover(self):
        self.fx.run(GITEA_REAP_MODE="enforce")
        self.assertEqual(self.fx.kills(), [(15, P + 200), (9, P + 200)])

    def test_pid_reuse_between_snapshot_and_signal_is_skipped(self):
        mod, env = self.fx.load(GITEA_REAP_MODE="enforce")
        orig = mod.process_candidates

        def snapshot_then_reuse(mainpid, e_boot):
            out = orig(mainpid, e_boot)
            self.fx.add_proc(P + 200, 1, 49_999, in_cgroup=False)   # pid now names a NEW process
            return out
        mod.process_candidates = snapshot_then_reuse
        with mock.patch.dict(os.environ, env), mock.patch("os.kill", side_effect=AssertionError):
            mod.main()
        self.assertEqual(self.fx.kills(), [])

    def test_no_real_signal_without_the_kill_indirection(self):
        with self.assertRaises(AssertionError):
            self.fx.run(GITEA_REAP_MODE="enforce", GITEA_REAP_KILL_CMD="")


class DockerTests(Base):
    def setUp(self):
        super().setUp()
        self.fx.journal([[self.fx.entry(49_990)]])

    def test_container_rules(self):
        self.fx.docker(containers=[
            ctr("c1", "old-leftover", 20_000, 20_000),
            ctr("c2", "restarted", 20_000, 49_995),
            ctr("c3", "new", 49_996, 49_996),
            ctr("c4", "buildx_buildkit_builder0", 100, 100, image="moby/buildkit:buildx-stable-1"),
            ctr("c5", "dsh-conductor-image-pin", 100, 100, labels={"io.dsh.conductor.image-pin": "0.1.0"}),
            ctr("c6", "kept-by-label", 100, 100, labels={"ci.keep": ""}),
            ctr("c7", "never-started", 20_000, None)])
        self.fx.run()
        w = "\n".join(self.fx.would())
        self.assertIn("container old-leftover", w)
        self.assertIn("container never-started", w)
        for n in ("restarted", " new ", "buildx", "image-pin", "kept-by-label"):
            self.assertNotIn(n, w)

    def test_pin_kept_by_the_host_var_regex_alone(self):
        self.fx.docker(containers=[ctr("c5", "dsh-conductor-image-pin", 100, 100, image="sha256:318b")])
        self.fx.run(GITEA_CLEANUP_INFRA_EXCLUDE_RE="buildkit|buildx| /dsh-conductor-image-pin$")
        self.assertEqual(self.fx.would(), [])

    def test_inspect_failure_fails_closed(self):
        self.fx.docker(containers=[ctr("c1", "live-db", 49_996, 49_996, mounts=[{"Type": "volume", "Name": "jobvol"}])],
                       volumes=[{"Name": "jobvol", "CreatedAt": iso(20_000), "Labels": {"com.docker.compose.project": "p"}}],
                       inspect_fail=True)
        self.fx.run()
        self.assertEqual(self.fx.would(), [])
        self.assertIn("gitea_runner_reap_docker_skipped 1", self.fx.prom())

    def test_network_without_its_reference_set_fails_closed(self):
        self.fx.docker(networks=[{"Id": "n1", "Name": "stale_default", "Created": iso(20_000),
                                  "Labels": {"com.docker.compose.project": "stale"}}])   # no Containers
        self.fx.run()
        self.assertEqual(self.fx.would(), [])
        self.assertIn("gitea_runner_reap_docker_skipped 1", self.fx.prom())

    def test_malformed_volume_or_container_record_fails_closed(self):
        for fx_kw in ({"volumes": [{"Name": "platform_x", "CreatedAt": 12345, "Labels": {"com.docker.compose.project": "p"}}]},
                      {"containers": [{"Id": "c1", "Name": "/x", "Created": iso(100), "State": {}, "Config": {}, "Mounts": [],
                                       "NetworkSettings": {"Networks": {}}}]}):
            with self.subTest(**{k: "malformed" for k in fx_kw}):
                (self.fx.root / "state.json").unlink(missing_ok=True)
                self.fx.docker(**fx_kw)
                self.fx.run()
                self.assertEqual(self.fx.would(), [])
                self.assertIn("gitea_runner_reap_docker_skipped 1", self.fx.prom())

    def test_volume_with_null_labels_is_valid_and_kept(self):
        self.fx.docker(volumes=[{"Name": "named_cache", "CreatedAt": iso(100), "Labels": None}])
        self.fx.run()
        self.assertEqual(self.fx.would(), [])
        self.assertIn("gitea_runner_reap_docker_skipped 0", self.fx.prom())

    def test_missing_timestamps_are_never_ancient(self):
        self.fx.docker(volumes=[{"Name": "noage", "CreatedAt": "", "Labels": {"com.docker.compose.project": "p"}}],
                       networks=[{"Id": "n9", "Name": "noage_net", "Created": "garbage", "Labels": {"com.docker.compose.project": "p"}, "Containers": {}}])
        self.fx.run()
        self.assertEqual(self.fx.would(), [])

    def test_project_veto_spares_a_project_the_current_job_runs(self):
        self.fx.docker(containers=[ctr("old", "hostexecutor-db-1", 20_000, 20_000, labels=compose("hostexecutor", "db")),
                                   ctr("new", "hostexecutor-web-1", 49_997, 49_997, labels=compose("hostexecutor", "web"))],
                       volumes=[{"Name": "hostexecutor_relay-db", "CreatedAt": iso(20_000), "Labels": {"com.docker.compose.project": "hostexecutor"}}])
        self.fx.run()
        self.assertEqual(self.fx.would(), [])

    def test_peer_hosts_attribute_containers_networks_and_volumes(self):
        self.fx.docker(
            containers=[ctr("a", "gitea-old", 100, 100, labels=compose("gproj", "db")),
                        ctr("b", "github-old", 100, 100, labels=compose("ghproj", "db", workdir="/home/runner/actions-runner/_work/x")),
                        ctr("d", "dotdot", 100, 100, labels=compose("dd", "db", workdir=WORKDIR + "/../../etc"))],
            volumes=[{"Name": "gproj_data", "CreatedAt": iso(100), "Labels": {"com.docker.compose.project": "gproj"}},
                     {"Name": "ghproj_data", "CreatedAt": iso(100), "Labels": {"com.docker.compose.project": "ghproj"}},
                     {"Name": "b" * 64, "CreatedAt": iso(100), "Labels": {"com.docker.volume.anonymous": ""}}],
            networks=[{"Id": "n1", "Name": "ghproj_default", "Created": iso(100), "Labels": {"com.docker.compose.project": "ghproj"}, "Containers": {}}])
        self.fx.run(GITEA_CLEANUP_PEER_SERVICES="actions.runner.cchifor-platform.service")
        w = "\n".join(self.fx.would())
        self.assertIn("gitea-old", w)
        self.assertIn("gproj_data", w)
        for n in ("github-old", "dotdot", "ghproj_data", "ghproj_default", "b" * 64):
            self.assertNotIn(n, w)

    def test_volume_and_network_reference_rules(self):
        self.fx.docker(
            containers=[ctr("live", "live-job-db", 49_996, 49_996, mounts=[{"Type": "volume", "Name": "kept_used"}], networks=["usednet"]),
                        ctr("old", "old-db", 20_000, 20_000, mounts=[{"Type": "volume", "Name": "via_candidate"}])],
            volumes=[{"Name": "platform_pgdata", "CreatedAt": iso(20_000), "Labels": {"com.docker.compose.project": "platform"}},
                     {"Name": "a" * 64, "CreatedAt": iso(20_000), "Labels": {"com.docker.volume.anonymous": ""}},
                     {"Name": "named_cache", "CreatedAt": iso(20_000), "Labels": {}},
                     {"Name": "kept_used", "CreatedAt": iso(20_000), "Labels": {"com.docker.compose.project": "x"}},
                     {"Name": "via_candidate", "CreatedAt": iso(20_000), "Labels": {"com.docker.compose.project": "y"}},
                     {"Name": "new_vol", "CreatedAt": iso(49_998), "Labels": {"com.docker.compose.project": "z"}},
                     {"Name": "keep_vol", "CreatedAt": iso(20_000), "Labels": {"com.docker.compose.project": "w", "ci.keep": ""}}],
            networks=[{"Id": "n1", "Name": "stale_default", "Created": iso(20_000), "Labels": {"com.docker.compose.project": "stale"}, "Containers": {}},
                      {"Id": "n2", "Name": "usednet", "Created": iso(20_000), "Labels": {"com.docker.compose.project": "u"}, "Containers": {"live": {}}},
                      {"Id": "n3", "Name": "bridge", "Created": iso(10), "Labels": {}, "Containers": {}}])
        self.fx.run()
        w = "\n".join(self.fx.would())
        for n in ("platform_pgdata", "a" * 64, "via_candidate", "stale_default"):
            self.assertIn(n, w)
        for n in ("named_cache", "kept_used", "new_vol", "keep_vol", "usednet", "bridge"):
            self.assertNotIn(n, w)

    def test_enforce_removes_and_log_never_does(self):
        self.fx.docker(containers=[ctr("c1", "old-leftover", 20_000, 20_000)],
                       volumes=[{"Name": "platform_x", "CreatedAt": iso(20_000), "Labels": {"com.docker.compose.project": "platform"}}])
        self.fx.run()
        removals = [l for l in self.fx.text("docker.log").splitlines() if l.startswith(("rm ", "network rm ", "volume rm "))]
        self.assertEqual(removals, [])
        (self.fx.root / "state.json").unlink()
        self.fx.run(GITEA_REAP_MODE="enforce")
        log = self.fx.text("docker.log")
        self.assertIn("rm -f c1", log)
        self.assertIn("volume rm platform_x", log)
        self.assertGreaterEqual(log.count("ps -aq --no-trunc"), 2, "enforce takes a second snapshot before removing")


class AdoptionAndBeaconTests(Base):
    def tick(self, dt, **kw):
        self.fx.run(GITEA_REAP_NOW=str(NOW + dt), **kw)

    def test_network_join_by_a_newer_container_is_adoption(self):
        self.fx.journal([[self.fx.entry(49_990)], []])
        net = {"Id": "n1", "Name": "stale_default", "Created": iso(20_000), "Labels": {"com.docker.compose.project": "stale"}, "Containers": {}}
        self.fx.docker(networks=[net])
        self.fx.run()
        self.fx.docker(containers=[ctr("j", "job-web", 50_040, 50_040, networks=["stale_default"])],
                       networks=[dict(net, Containers={"j": {}})])
        self.tick(60)
        self.assertIn("ADOPTED would-reap network stale_default", self.fx.text("logger.log"))
        self.assertIn("gitea_runner_reap_adopted_48h 1", self.fx.prom())

    def test_same_project_different_service_is_not_adoption(self):
        self.fx.journal([[self.fx.entry(49_990)], []])
        self.fx.docker(containers=[ctr("old", "hostexecutor-relay-db-1", 20_000, 20_000, labels=compose("hostexecutor", "relay-db"))])
        self.fx.run()
        self.fx.docker(containers=[ctr("old", "hostexecutor-relay-db-1", 20_000, 20_000, labels=compose("hostexecutor", "relay-db")),
                                   ctr("new", "hostexecutor-web-1", 50_040, 50_040, labels=compose("hostexecutor", "web"))])
        self.tick(60)
        self.assertNotIn("ADOPTED", self.fx.text("logger.log"))

    def test_same_project_and_service_is_adoption(self):
        self.fx.journal([[self.fx.entry(49_990)], []])
        self.fx.docker(containers=[ctr("old", "p-db-1", 20_000, 20_000, labels=compose("p", "db"))])
        self.fx.run()
        self.fx.docker(containers=[ctr("old", "p-db-1", 20_000, 20_000, labels=compose("p", "db")),
                                   ctr("new", "p-db-2", 50_040, 50_040, labels=compose("p", "db"))])
        self.tick(60)
        self.assertIn("ADOPTED would-reap container p-db-1", self.fx.text("logger.log"))

    def test_process_burning_cpu_after_being_reported_is_adoption(self):
        self.fx.journal([[self.fx.entry(49_990)], []])
        self.fx.add_proc(P + 200, 1, 20_000, cpu=50)
        self.fx.run()
        self.fx.add_proc(P + 200, 1, 20_000, cpu=50 + 300, in_cgroup=False)
        self.tick(60)
        self.assertIn(f"ADOPTED would-reap process pid {P + 200}", self.fx.text("logger.log"))

    def test_beacon_keeps_the_last_pass_counts_between_passes(self):
        self.fx.journal([[self.fx.entry(49_990)], []])
        self.fx.add_proc(P + 200, 1, 20_000)
        self.fx.run()
        self.tick(5)   # no edge, no periodic: a plain tick
        prom = self.fx.prom()
        self.assertIn('gitea_runner_reap_last_candidates{kind="process"} 1', prom)
        self.assertIn("gitea_runner_reap_boundary_age_seconds 20\n", prom)   # B = 49_990 - 5, now + 5
        self.assertIn("gitea_runner_reap_mode 1", prom)

    def test_kill_switch_publishes_mode_zero_and_does_nothing(self):
        self.fx.add_proc(P + 200, 1, 10)
        self.fx.run(GITEA_REAP_MODE="off")
        self.assertIn("gitea_runner_reap_mode 0", self.fx.prom())
        self.assertEqual(self.fx.text("journalctl.args"), "")
        self.assertEqual(self.fx.kills(), [])

    def test_missing_boundary_reports_minus_one(self):
        self.fx.run()
        self.assertIn("gitea_runner_reap_boundary_age_seconds -1", self.fx.prom())


class EnvDriftTests(unittest.TestCase):
    # tunables that deliberately stay at their script defaults (no role var)
    SCRIPT_DEFAULT_ONLY = {"GITEA_REAP_PROBE_SEC", "GITEA_REAP_TERM_WAIT_SEC", "GITEA_REAP_EDGE_MAX_LAG_SEC",
                           "GITEA_REAP_DEADLINE_SEC", "GITEA_REAP_ADOPT_CPU_TICKS"}

    def test_every_tunable_the_reaper_reads_is_rendered_in_the_env_file(self):
        keys = set(re.findall(r'\bcfg\("([A-Z_]+)"', SCRIPT.read_text())) - self.SCRIPT_DEFAULT_ONLY
        rendered = set(re.findall(r"^([A-Z_]+)=", ENV_TEMPLATE.read_text(), re.M))
        self.assertEqual(sorted(keys - rendered), [], "reaper tunables missing from gitea-runner-reclaim.env.j2")

    def test_injection_points_never_come_from_the_env_file(self):
        src = SCRIPT.read_text()
        for name in ("GITEA_REAP_NOW", "GITEA_REAP_KILL_CMD", "GITEA_REAP_PROC_ROOT", "GITEA_REAP_DOCKER"):
            self.assertIn(f'inj("{name}"', src)
            self.assertNotIn(f'cfg("{name}"', src)


if __name__ == "__main__":
    unittest.main(verbosity=1)
