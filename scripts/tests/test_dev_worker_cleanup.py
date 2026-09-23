"""Tests for the dev-worker `cleanup` tool (ansible/roles/dev_worker/files/cleanup).

The tool's contract, pinned here:
  * It removes exactly what its plan lists, and the plan is a pure function of docker state.
  * A stack is only "stale" when NOTHING in it was created or started within --stale-days, or
    when its compose working directory is gone. A stack someone touched recently is kept.
  * Stopped containers of a stack that still has running containers are kept (they are the
    live stack's one-shot jobs).
  * An image is only removable when no container that SURVIVES the plan uses it.
  * Only anonymous, unattached volumes go by default; named stack volumes only with the stack
    and only when the stack is orphaned or --volumes is set.
  * Tool caches are opt-in (--caches): emptying them under a running install breaks it.
  * Without --y it needs an interactive yes; a non-interactive run without --y removes nothing.
"""
import importlib.machinery
import importlib.util
import pathlib
import unittest
from datetime import datetime, timedelta, timezone

_PATH = pathlib.Path(__file__).resolve().parents[2] / "ansible/roles/dev_worker/files/cleanup"
_loader = importlib.machinery.SourceFileLoader("dw_cleanup", str(_PATH))
_spec = importlib.util.spec_from_loader("dw_cleanup", _loader)
cl = importlib.util.module_from_spec(_spec)
_loader.exec_module(cl)  # must not touch docker at import time

NOW = datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc)


def ago(**kw):
    return NOW - timedelta(**kw)


def ctr(cid, *, project=None, workdir=None, running=False, created, started=None, finished=None,
        image="img-a", mounts=(), restarts=0):
    labels = {}
    if project:
        labels["com.docker.compose.project"] = project
        labels["com.docker.compose.project.working_dir"] = workdir or f"/ws/{project}"
    return cl.Container(
        id=cid, name=f"n-{cid}", image_id=image, labels=labels, running=running,
        created=created, started=started or created, finished=finished, mounts=tuple(mounts),
        restart_count=restarts,
    )


def img(iid, *, created, size=1_000_000_000, tags=("repo:tag",)):
    return cl.Image(id=iid, tags=tuple(tags), created=created, size=size)


def vol(name, *, project=None, size=100):
    labels = {"com.docker.compose.project": project} if project else {}
    return cl.Volume(name=name, labels=labels, size=size)


ANON = "a" * 64


class PlanTest(unittest.TestCase):
    def plan(self, containers=(), images=(), vols=(), existing_dirs=None, boot_times=(), **opts):
        o = cl.Options(**{**dict(stale_days=14, stopped_hours=24, image_days=7, cache_keep_gb=10,
                                 keep_stacks=False, volumes=False, caches=False), **opts})
        dirs = existing_dirs if existing_dirs is not None else {c.labels.get(
            "com.docker.compose.project.working_dir") for c in containers} - {None}
        return cl.build_plan(list(containers), list(images), list(vols), build_cache_bytes=0,
                             tool_caches=[], opts=o, now=NOW, dir_exists=lambda p: p in dirs,
                             boot_times=boot_times)

    def ids(self, plan, kind):
        return sorted(i for a in plan if a.kind == kind for i in a.ids)

    def test_old_stack_is_stale_recent_stack_is_kept(self):
        cs = [ctr("old1", project="old", running=True, created=ago(days=35)),
              ctr("new1", project="wip", running=True, created=ago(days=8))]
        p = self.plan(cs)
        self.assertEqual(self.ids(p, "stack"), ["old1"])

    def test_one_recent_start_keeps_the_whole_stack(self):
        cs = [ctr("s1", project="p", running=True, created=ago(days=30)),
              ctr("s2", project="p", running=True, created=ago(days=30), started=ago(hours=2))]
        self.assertEqual(self.ids(self.plan(cs), "stack"), [])

    def test_orphaned_stack_goes_at_any_age_with_its_volumes(self):
        cs = [ctr("o1", project="gone", running=True, created=ago(hours=1),
                  mounts=["gone_pgdata"])]
        vs = [vol("gone_pgdata", project="gone")]
        p = self.plan(cs, vols=vs, existing_dirs=set())
        self.assertEqual(self.ids(p, "stack"), ["o1"])
        self.assertEqual(self.ids(p, "volume"), ["gone_pgdata"])

    def test_stale_stack_keeps_named_volumes_unless_asked(self):
        cs = [ctr("x1", project="old", created=ago(days=40), mounts=["old_db"])]
        vs = [vol("old_db", project="old")]
        self.assertEqual(self.ids(self.plan(cs, vols=vs), "volume"), [])
        self.assertEqual(self.ids(self.plan(cs, vols=vs, volumes=True), "volume"), ["old_db"])

    def test_restart_at_boot_is_not_activity(self):
        # A 5-week-old stack whose containers the daemon restarted at boot 10 hours ago.
        boot = ago(hours=10)
        cs = [ctr("b1", project="old", running=True, created=ago(days=35),
                  started=boot + timedelta(minutes=2))]
        self.assertEqual(self.ids(self.plan(cs, boot_times=[boot]), "stack"), ["b1"])
        # Docker can stamp the boot-time start slightly BEFORE now-minus-uptime (clock stepped
        # at boot), which must still count as the boot restart (seen live on dev-worker-3).
        cs0 = [ctr("b0", project="old", running=True, created=ago(days=35),
                   started=boot - timedelta(seconds=40))]
        self.assertEqual(self.ids(self.plan(cs0, boot_times=[boot]), "stack"), ["b0"])
        # ...but a start well after boot is someone using it.
        cs2 = [ctr("b2", project="used", running=True, created=ago(days=35),
                   started=boot + timedelta(hours=3))]
        self.assertEqual(self.ids(self.plan(cs2, boot_times=[boot]), "stack"), [])

    def test_restart_at_an_earlier_boot_is_not_activity_either(self):
        # dev-worker-3: stopped workers still carry the StartedAt of the 2026-09-11 reboot.
        earlier, current = ago(days=12), ago(hours=10)
        cs = [ctr("w1", project="old", created=ago(days=35), started=earlier + timedelta(minutes=2)),
              ctr("w2", project="old", running=True, created=ago(days=35), started=current)]
        self.assertEqual(self.ids(self.plan(cs, boot_times=[current]), "stack"), [])
        self.assertEqual(self.ids(self.plan(cs, boot_times=[current, earlier]), "stack"),
                         ["w1", "w2"])

    def test_crash_loop_restart_is_not_activity(self):
        # dev-worker-4: platform-a7-airlock-1, 2137 policy restarts in 3 weeks, "started" 10 s ago.
        cs = [ctr("loop", project="a7", running=True, created=ago(days=22),
                  started=ago(seconds=10), restarts=2137),
              ctr("seed", project="a7", created=ago(days=22), finished=ago(days=22))]
        self.assertEqual(self.ids(self.plan(cs), "stack"), ["loop", "seed"])

    def test_fully_stopped_recent_stack_is_kept_whole(self):
        # review (#847): `compose stop` on WIP two days ago must not lose its containers after
        # --stopped-hours; only the 14-day stack rule may remove a stack's containers.
        cs = [ctr("s1", project="wip", created=ago(days=3), finished=ago(days=2)),
              ctr("s2", project="wip", created=ago(days=3), finished=ago(days=2))]
        p = self.plan(cs)
        self.assertEqual(self.ids(p, "container"), [])
        self.assertEqual(self.ids(p, "stack"), [])

    def test_recent_exit_is_activity_but_not_a_shutdown_or_crash_loop(self):
        # review (#847): a long job that exited an hour ago is in use...
        cs = [ctr("job", project="batch", created=ago(days=30), started=ago(days=30),
                  finished=ago(hours=1))]
        self.assertEqual(self.ids(self.plan(cs), "stack"), [])
        # ...but a stop caused by a reboot (finish right before a boot) is not,
        boot = ago(hours=10)
        cs2 = [ctr("r", project="old", created=ago(days=30), finished=boot - timedelta(seconds=20))]
        self.assertEqual(self.ids(self.plan(cs2, boot_times=[boot]), "stack"), ["r"])
        # ...and neither is a crash-loop exit.
        cs3 = [ctr("c", project="loop", created=ago(days=30), finished=ago(minutes=1), restarts=40)]
        self.assertEqual(self.ids(self.plan(cs3), "stack"), ["c"])

    def test_keep_stacks_disables_stack_removal(self):
        cs = [ctr("old1", project="old", running=True, created=ago(days=35))]
        self.assertEqual(self.ids(self.plan(cs, keep_stacks=True), "stack"), [])

    def test_stopped_containers_of_a_live_stack_are_kept(self):
        cs = [ctr("web", project="live", running=True, created=ago(days=3)),
              ctr("migrate", project="live", created=ago(days=3), finished=ago(days=3)),
              ctr("lone", created=ago(days=5), finished=ago(days=5)),
              ctr("fresh", created=ago(hours=2), finished=ago(hours=1))]
        self.assertEqual(self.ids(self.plan(cs), "container"), ["lone"])

    def test_image_used_by_a_surviving_container_is_kept(self):
        cs = [ctr("old1", project="old", running=True, created=ago(days=35), image="img-old"),
              ctr("keep", project="wip", running=True, created=ago(days=1), image="img-keep")]
        ims = [img("img-old", created=ago(days=40)), img("img-keep", created=ago(days=40)),
               img("img-unused", created=ago(days=10)), img("img-young", created=ago(days=2))]
        self.assertEqual(self.ids(self.plan(cs, ims), "image"), ["img-old", "img-unused"])

    def test_image_removal_targets_every_tag(self):
        # `docker rmi <id>` refuses an image with several tags ("referenced in multiple
        # repositories", seen live on dev-worker-3); removing every tag deletes it without -f.
        ims = [img("sha256:multi", created=ago(days=30), tags=("postgres:16", "mirror/postgres:16")),
               img("sha256:bare", created=ago(days=30), tags=())]
        p = self.plan([], ims)
        cmds = {a.ids[0]: a.command for a in p if a.kind == "image"}
        self.assertEqual(cmds["sha256:multi"], ["docker", "rmi", "postgres:16", "mirror/postgres:16"])
        self.assertEqual(cmds["sha256:bare"], ["docker", "rmi", "sha256:bare"])

    def test_only_anonymous_unattached_volumes_by_default(self):
        cs = [ctr("c", created=ago(hours=1), running=True, mounts=["b" * 64])]
        vs = [vol(ANON), vol("b" * 64), vol("named_thing"), vol("c" * 64, project="p")]
        self.assertEqual(self.ids(self.plan(cs, vols=vs), "volume"), [ANON])

    def test_tool_caches_are_opt_in(self):
        o = cl.Options(stale_days=14, stopped_hours=24, image_days=7, cache_keep_gb=10,
                       keep_stacks=False, volumes=False, caches=False)
        tc = [cl.CacheItem(name="npm cache", path="/h/.npm/_cacache", size=5, command=["true"])]
        p = cl.build_plan([], [], [], build_cache_bytes=0, tool_caches=tc, opts=o, now=NOW,
                          dir_exists=lambda p: True)
        self.assertEqual([a for a in p if a.kind == "cache"], [])
        o2 = cl.Options(**{**o.__dict__, "caches": True})
        p2 = cl.build_plan([], [], [], build_cache_bytes=0, tool_caches=tc, opts=o2, now=NOW,
                           dir_exists=lambda p: True)
        self.assertEqual([a.label for a in p2 if a.kind == "cache"], ["npm cache"])

    def test_build_cache_keeps_the_newest_n_gb(self):
        # `buildx prune --filter until=` removes NOTHING on buildx 0.37 / BuildKit 0.33 (the old
        # weekly timer logged "Total: 0B" while 20 GB sat unused), so cleanup caps the cache by
        # size instead: keep the most recently used N GB, estimate only the excess.
        o = cl.Options(stale_days=14, stopped_hours=24, image_days=7, cache_keep_gb=10,
                       keep_stacks=False, volumes=False, caches=False)
        p = cl.build_plan([], [], [], build_cache_bytes=25 * 10**9, tool_caches=[], opts=o,
                          now=NOW, dir_exists=lambda p: True)
        (a,) = p
        self.assertEqual(a.size, 15 * 10**9)
        self.assertEqual(a.command, ["docker", "buildx", "prune", "-f", "--max-used-space", "10GB"])
        under = cl.build_plan([], [], [], build_cache_bytes=9 * 10**9, tool_caches=[], opts=o,
                              now=NOW, dir_exists=lambda p: True)
        self.assertEqual(under, [])

    def test_uv_prune_size_is_not_counted_as_freed(self):
        o = cl.Options(stale_days=14, stopped_hours=24, image_days=7, cache_keep_gb=10,
                       keep_stacks=False, volumes=False, caches=True)
        tc = [cl.CacheItem(name="uv cache (prune unused)", path="/h/.cache/uv", size=7 * 10**9,
                           command=["uv", "cache", "prune"], exact=False)]
        (a,) = cl.build_plan([], [], [], build_cache_bytes=0, tool_caches=tc, opts=o, now=NOW,
                             dir_exists=lambda p: True)
        self.assertIsNone(a.size)
        self.assertIn("7.0 GB", a.detail)

    def test_build_cache_row_only_when_reclaimable(self):
        o = cl.Options(stale_days=14, stopped_hours=24, image_days=7, cache_keep_gb=10,
                       keep_stacks=False, volumes=False, caches=False)
        empty = cl.build_plan([], [], [], build_cache_bytes=0, tool_caches=[], opts=o, now=NOW,
                              dir_exists=lambda p: True)
        some = cl.build_plan([], [], [], build_cache_bytes=11 * 10**9, tool_caches=[], opts=o, now=NOW,
                             dir_exists=lambda p: True)
        self.assertEqual([a.kind for a in empty], [])
        self.assertEqual([a.kind for a in some], ["buildcache"])


class DisplayTest(unittest.TestCase):
    def test_grouping_keeps_the_plan_exact_but_the_table_short(self):
        plan = ([cl.Action("container", f"s-{i}", "stackA", "2d", None, [f"c{i}"]) for i in range(5)]
                + [cl.Action("volume", "v" * 40, "anonymous", "", 10, [f"{i:064x}"]) for i in range(4)]
                + [cl.Action("image", f"img{i}", "id", "9d", 100 * i, [f"i{i}"]) for i in range(15)])
        rows = cl.display_rows(plan, show_all=False)
        cats = [r[0] for r in rows]
        self.assertEqual(cats.count("Stopped container"), 1)
        self.assertIn("5 stopped containers", rows[cats.index("Stopped container")][2])
        self.assertEqual(cats.count("Unused volume"), 1)
        self.assertEqual(cats.count("Unused image"), 11)         # 10 largest + "N more"
        more = [r for r in rows if "more images" in r[1]]
        self.assertEqual(len(more), 1)
        self.assertIn("5 more", more[0][1])                      # the 5 smallest roll up
        self.assertEqual(more[0][4], cl.fmt_size(sum(100 * i for i in range(5))))
        self.assertEqual(len(cl.display_rows(plan, show_all=True)), 5 + 4 + 15)


class ExecuteTest(unittest.TestCase):
    def run_exec(self, plan, fresh=None, stack_now=None, tag_ids=None):
        calls = []
        orig_fp, orig_tag = cl.live_stack_fingerprint, cl.tag_image_id
        cl.live_stack_fingerprint = lambda label: (stack_now or {}).get(label, frozenset())
        cl.tag_image_id = lambda tag: (tag_ids or {}).get(tag)

        class R:
            returncode, stdout, stderr = 0, "", ""

        orig = cl.sh
        cl.sh = lambda args, check=True: (calls.append(list(args)), R())[1]
        try:
            failures, skipped = cl.execute(plan, fresh_plan=fresh if fresh is not None else plan)
        finally:
            cl.sh = orig
            cl.live_stack_fingerprint, cl.tag_image_id = orig_fp, orig_tag
        return calls, failures, skipped

    def test_container_removal_leaves_volumes_to_the_volume_actions(self):
        # review (#847): `docker rm -v` deleted anonymous volumes that were ALSO planned as volume
        # actions, so the later `volume rm` failed and the run exited 1.
        plan = [cl.Action("stack", "old", "", "", None, ["c1"], fingerprint=frozenset({("c1", None, None)})),
                cl.Action("container", "lone", "", "", None, ["c2"]),
                cl.Action("volume", "v", "anonymous", "", 1, ["v" * 64])]
        calls, failures, _ = self.run_exec(plan, stack_now={"old": frozenset({("c1", None, None)})})
        rms = [c for c in calls if c[:2] == ["docker", "rm"]]
        self.assertTrue(rms)
        self.assertTrue(all("-v" not in c for c in rms), rms)
        self.assertIn(["docker", "rm", "c2"], rms)              # stopped: no force
        self.assertIn(["docker", "volume", "rm", "v" * 64], calls)
        self.assertEqual(failures, 0)

    def test_items_that_changed_since_the_scan_are_skipped(self):
        # review (#847): something started while the prompt was open must not be force-removed.
        fp = frozenset({("c1", None, None)})
        plan = [cl.Action("stack", "old", "", "", None, ["c1"], fingerprint=fp),
                cl.Action("container", "lone", "", "", None, ["c2"])]
        fresh = [cl.Action("stack", "old", "", "", None, ["c1"], fingerprint=fp)]  # c2 started
        calls, _, skipped = self.run_exec(plan, fresh, stack_now={"old": fp})
        self.assertEqual(skipped, 1)
        self.assertFalse(any("c2" in c for c in calls))


class ExecuteRound2Test(ExecuteTest):
    def test_stack_touched_during_execution_is_skipped(self):
        # review round 2 (#847): re-check each stack right before `rm -f`, not only at the rescan.
        fp = frozenset({("c1", ago(days=30), None)})
        plan = [cl.Action("stack", "old", "", "", None, ["c1"], fingerprint=fp)]
        restarted = frozenset({("c1", ago(seconds=5), None)})
        calls, _, skipped = self.run_exec(plan, stack_now={"old": restarted})
        self.assertEqual(skipped, 1)
        self.assertFalse(any(c[:3] == ["docker", "rm", "-f"] for c in calls))

    def test_image_tags_are_re_resolved_before_removal(self):
        # review round 2 (#847): a tag moved to a new image since the scan must not be removed.
        plan = [cl.Action("image", "app:1", "x", "9d", 1, ["sha256:old"],
                          ["docker", "rmi", "app:1", "app:latest"])]
        calls, _, _ = self.run_exec(plan, tag_ids={"app:1": "sha256:old", "app:latest": "sha256:new"})
        self.assertIn(["docker", "rmi", "app:1"], calls)
        calls, _, _ = self.run_exec(plan, tag_ids={"app:1": "sha256:new", "app:latest": "sha256:new"})
        self.assertIn(["docker", "rmi", "sha256:old"], calls)    # no tag left: remove by ID
        self.assertFalse(any("app:1" in c or "app:latest" in c for c in calls))


class HelpersTest(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(cl.parse_size("97B"), 97)
        self.assertEqual(cl.parse_size("12.5kB"), 12_500)
        self.assertEqual(cl.parse_size("575.5MB*"), 575_500_000)
        self.assertEqual(cl.parse_size("2.645GB"), 2_645_000_000)
        self.assertEqual(cl.parse_size("0B"), 0)

    def test_parse_time_docker_and_buildx_formats(self):
        a = cl.parse_time("2026-08-22T20:37:05.637367041Z")
        b = cl.parse_time("2026-08-22 20:37:05.637367041 +0000 UTC")
        self.assertEqual(a, b)
        self.assertIsNone(cl.parse_time("0001-01-01T00:00:00Z"))

    def test_age_rendering(self):
        self.assertEqual(cl.fmt_age(timedelta(days=36)), "5w1d")
        self.assertEqual(cl.fmt_age(timedelta(days=3, hours=4)), "3d")
        self.assertEqual(cl.fmt_age(timedelta(hours=5)), "5h")

    def test_yes_aliases(self):
        for flag in ("--y", "-y", "--yes"):
            self.assertTrue(cl.parse_args([flag]).yes, flag)
        self.assertFalse(cl.parse_args([]).yes)

    def test_confirm_refuses_non_interactive_without_yes(self):
        self.assertFalse(cl.confirm(yes=False, interactive=False, ask=lambda: "y"))
        self.assertTrue(cl.confirm(yes=True, interactive=False, ask=lambda: "n"))
        self.assertTrue(cl.confirm(yes=False, interactive=True, ask=lambda: "yes"))
        self.assertFalse(cl.confirm(yes=False, interactive=True, ask=lambda: ""))
        self.assertFalse(cl.confirm(yes=False, interactive=True, ask=lambda: "no"))


if __name__ == "__main__":
    unittest.main()
