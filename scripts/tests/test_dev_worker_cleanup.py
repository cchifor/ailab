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


# ----------------------------------------------------------------------------- --deps (worktree deps)
#
# Real directory trees in a tempdir; ages are set with os.utime. Linux-only where the code is
# (O_NOFOLLOW, /proc), which is where CI and the workers run.

import os
import stat
import tempfile
import time
from unittest import mock

LINUX = hasattr(os, "O_NOFOLLOW") and os.path.isdir("/proc/self")
DAY = 86400


def write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def age_tree(path, days):
    """Set every mtime under `path` (and `path`) to `days` ago, children before parents."""
    t = time.time() - days * DAY
    for d, dirs, files in os.walk(path, topdown=False):
        for n in files + dirs:
            os.utime(os.path.join(d, n), (t, t), follow_symlinks=False)
    os.utime(path, (t, t))


def make_repo(path, *, linked_from=None):
    """A main checkout (.git dir) or, with linked_from, a linked worktree (.git file)."""
    os.makedirs(path, exist_ok=True)
    if linked_from:
        gd = os.path.join(linked_from, ".git", "worktrees", os.path.basename(path))
        write(os.path.join(gd, "HEAD"), "ref: refs/heads/x\n")
        write(os.path.join(path, ".git"), f"gitdir: {gd}\n")
    else:
        write(os.path.join(path, ".git", "HEAD"), "ref: refs/heads/main\n")
    write(os.path.join(path, "src", "main.py"))
    return path


@unittest.skipUnless(LINUX, "Linux-only code path")
class DepsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.user = os.path.join(self.root, "u")
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self._tmp.cleanup()

    def cutoff(self, days=14):
        return (self.now - timedelta(days=days)).timestamp()

    def find(self, **kw):
        return cl.find_worktrees([self.root], min_uid=0, **kw)

    # discovery

    def test_discovery_finds_main_linked_and_nested_worktrees_but_not_caches(self):
        repo = make_repo(os.path.join(self.user, "platform"))
        linked = make_repo(os.path.join(self.user, ".worktrees", "wt1"), linked_from=repo)
        nested = make_repo(os.path.join(repo, ".claude", "worktrees", "x"), linked_from=repo)
        make_repo(os.path.join(self.user, ".cache", "pkg"))                 # SKIP_NAMES
        make_repo(os.path.join(repo, "node_modules", "dep"))               # inside a dep dir
        self.assertEqual(self.find(), sorted([repo, linked, nested]))

    def test_discovery_never_enters_container_storage(self):
        repo = make_repo(os.path.join(self.user, "platform"))
        storage = os.path.join(self.user, "containerd-root")
        make_repo(os.path.join(storage, "snapshots", "1", "fs", "app"))
        self.assertEqual(self.find(excluded={storage}), [repo])

    def test_discovery_walks_only_user_owned_top_level_dirs(self):
        make_repo(os.path.join(self.user, "platform"))
        uid = os.stat(self.user).st_uid
        self.assertEqual(cl.find_worktrees([self.root], min_uid=uid + 1), [])

    # activity

    def test_idle_worktree_lists_its_dep_dirs(self):
        wt = make_repo(os.path.join(self.user, "wt"))
        write(os.path.join(wt, "node_modules", "a", "index.js"))
        write(os.path.join(wt, "apps", "web", "node_modules", "b", "index.js"))
        write(os.path.join(wt, ".venv", "pyvenv.cfg"))
        write(os.path.join(wt, "Cargo.toml"))
        write(os.path.join(wt, "target", "debug", "bin"))
        age_tree(wt, 30)
        w = cl.scan_worktree(wt, self.cutoff())
        self.assertIsNotNone(w.last_activity)
        self.assertEqual(sorted(os.path.relpath(d, wt) for d in w.deps),
                         [".venv", "apps/web/node_modules", "node_modules", "target"])

    def test_only_real_dep_dirs_count(self):
        wt = make_repo(os.path.join(self.user, "wt"))
        write(os.path.join(wt, ".venv", "notes.txt"))        # no pyvenv.cfg: someone's dir
        write(os.path.join(wt, "target", "report.html"))     # no Cargo.toml beside it
        os.makedirs(os.path.join(self.root, "elsewhere", "node_modules"))
        os.symlink(os.path.join(self.root, "elsewhere", "node_modules"),
                   os.path.join(wt, "node_modules"))
        age_tree(wt, 30)
        self.assertEqual(cl.scan_worktree(wt, self.cutoff()).deps, ())

    def test_any_recent_file_or_git_activity_makes_it_fresh(self):
        wt = make_repo(os.path.join(self.user, "wt"))
        write(os.path.join(wt, "node_modules", "a", "index.js"))
        age_tree(wt, 30)
        write(os.path.join(wt, "src", "deep", "new.py"))
        self.assertIsNone(cl.scan_worktree(wt, self.cutoff()).last_activity)
        age_tree(wt, 30)
        write(os.path.join(wt, ".git", "index"))
        self.assertIsNone(cl.scan_worktree(wt, self.cutoff()).last_activity)

    def test_a_reinstalled_dep_dir_is_activity(self):
        wt = make_repo(os.path.join(self.user, "wt"))
        write(os.path.join(wt, "node_modules", "a", "index.js"))
        age_tree(wt, 30)
        os.utime(os.path.join(wt, "node_modules"))
        self.assertIsNone(cl.scan_worktree(wt, self.cutoff()).last_activity)

    def test_nested_worktrees_are_judged_separately(self):
        parent = make_repo(os.path.join(self.user, "platform"))
        write(os.path.join(parent, "node_modules", "a", "i.js"))
        child = make_repo(os.path.join(parent, ".claude", "worktrees", "x"), linked_from=parent)
        write(os.path.join(child, "node_modules", "b", "i.js"))
        age_tree(parent, 30)
        write(os.path.join(child, "src", "new.py"))          # only the child is active
        p = cl.scan_worktree(parent, self.cutoff())
        self.assertEqual(p.deps, (os.path.join(parent, "node_modules"),))
        self.assertIsNone(cl.scan_worktree(child, self.cutoff()).last_activity)

    def test_an_unreadable_tree_counts_as_in_use(self):
        wt = make_repo(os.path.join(self.user, "wt"))
        write(os.path.join(wt, "node_modules", "a", "index.js"))
        age_tree(wt, 30)
        real = os.scandir

        def flaky(p):
            if p.endswith("/src"):
                raise PermissionError(13, "denied", p)
            return real(p)
        with mock.patch.object(cl.os, "scandir", flaky):
            self.assertIsNone(cl.scan_worktree(wt, self.cutoff()).last_activity)

    # planning

    def idle(self, name="wt"):
        wt = make_repo(os.path.join(self.user, name))
        write(os.path.join(wt, "node_modules", "a", "index.js"))
        age_tree(wt, 30)
        return wt, cl.scan_worktree(wt, self.cutoff())

    def test_plan_pins_each_dir_by_inode(self):
        wt, w = self.idle()
        plan, refused = cl.plan_deps([w], 14, self.now, set(), set())
        self.assertEqual(refused, [])
        [a] = plan
        nm = os.path.join(wt, "node_modules")
        st = os.lstat(nm)
        self.assertEqual(a.ids, [nm])
        self.assertEqual(a.fingerprint, frozenset({(nm, st.st_dev, st.st_ino)}))
        self.assertGreater(a.size, 0)

    def test_in_use_worktrees_are_not_planned(self):
        wt, w = self.idle()
        for busy, binds in (({os.path.join(wt, "src")}, set()),            # a shell's cwd
                            ({os.path.join(wt, "node_modules", "a", "x.so")}, set()),  # mapped
                            (set(), {os.path.join(wt, "src")}),             # bind of a subdir
                            (set(), {self.user})):                          # bind of a parent
            self.assertEqual(cl.plan_deps([w], 14, self.now, busy, binds)[0], [], (busy, binds))
        # A process elsewhere in the same home, or a bind of "/", does not pin it.
        plan, _ = cl.plan_deps([w], 14, self.now, {self.user, os.path.join(self.user, "other")},
                               {"/"})
        self.assertEqual(len(plan), 1)

    def test_dep_dirs_holding_a_checkout_or_a_mount_are_refused(self):
        wt = make_repo(os.path.join(self.user, "wt"))
        write(os.path.join(wt, "node_modules", "linked", ".git", "HEAD"))
        write(os.path.join(wt, "apps", "x", "node_modules", "a", "i.js"))
        age_tree(wt, 30)
        w = cl.scan_worktree(wt, self.cutoff())
        mounted = os.path.join(wt, "apps", "x", "node_modules", "a")
        plan, refused = cl.plan_deps([w], 14, self.now, set(), set(), mounts={mounted})
        self.assertEqual(plan, [])
        self.assertEqual(sorted(why for _, why in refused),
                         ["contains a git checkout", "has a mount inside"])

    # removal

    def remove(self, action):
        return cl.remove_deps(action, 14, self.now, set(), set(), set())

    def test_removal_takes_only_the_dep_dirs(self):
        wt, w = self.idle()
        write(os.path.join(wt, "untracked.txt"))
        age_tree(wt, 30)
        [a] = cl.plan_deps([cl.scan_worktree(wt, self.cutoff())], 14, self.now, set(), set())[0]
        self.assertEqual(self.remove(a), 1)
        self.assertEqual(sorted(os.listdir(wt)), [".git", "src", "untracked.txt"])

    def test_worktree_touched_after_the_plan_is_skipped_whole(self):
        wt, w = self.idle()
        [a] = cl.plan_deps([w], 14, self.now, set(), set())[0]
        write(os.path.join(wt, "src", "new.py"))
        self.assertIsNone(self.remove(a))
        self.assertTrue(os.path.isdir(os.path.join(wt, "node_modules")))

    def test_process_appearing_after_the_plan_skips_it(self):
        wt, w = self.idle()
        [a] = cl.plan_deps([w], 14, self.now, set(), set())[0]
        self.assertIsNone(cl.remove_deps(a, 14, self.now, {wt}, set(), set()))
        self.assertTrue(os.path.isdir(os.path.join(wt, "node_modules")))

    def test_a_checkout_cloned_into_a_dep_dir_after_the_plan_is_kept(self):
        wt, w = self.idle()
        [a] = cl.plan_deps([w], 14, self.now, set(), set())[0]
        nm = os.path.join(wt, "node_modules")
        before = os.lstat(nm)
        write(os.path.join(nm, "a", ".git", "HEAD"))      # inside node_modules/a: nm untouched
        write(os.path.join(nm, "a", "work.py"))
        os.utime(nm, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.remove(a), 0)
        self.assertTrue(os.path.isfile(os.path.join(nm, "a", "work.py")))

    def test_a_replaced_dir_is_not_the_planned_one(self):
        wt, w = self.idle()
        [a] = cl.plan_deps([w], 14, self.now, set(), set())[0]
        nm = os.path.join(wt, "node_modules")
        os.rename(nm, os.path.join(self.root, "old-nm"))
        write(os.path.join(nm, "fresh", "i.js"))
        age_tree(wt, 30)                                   # even with an old mtime
        self.assertEqual(self.remove(a), 0)
        self.assertTrue(os.path.isfile(os.path.join(nm, "fresh", "i.js")))

    def test_a_symlink_swapped_into_the_path_is_not_followed(self):
        wt = make_repo(os.path.join(self.user, "wt"))
        write(os.path.join(wt, "apps", "web", "node_modules", "a", "i.js"))
        age_tree(wt, 30)
        [a] = cl.plan_deps([cl.scan_worktree(wt, self.cutoff())], 14, self.now, set(), set())[0]
        victim = os.path.join(self.root, "victim")
        write(os.path.join(victim, "node_modules", "keep.txt"))
        os.rename(os.path.join(wt, "apps"), os.path.join(self.root, "apps-moved"))
        os.makedirs(os.path.join(wt, "apps"))
        os.symlink(victim, os.path.join(wt, "apps", "web"))
        age_tree(wt, 30)
        nm = os.path.join(wt, "apps", "web", "node_modules")
        st = os.lstat(os.path.join(self.root, "apps-moved", "web", "node_modules"))
        with self.assertRaises(OSError):
            cl.remove_dep_dir(nm, (st.st_dev, st.st_ino))
        self.assertTrue(os.path.isfile(os.path.join(victim, "node_modules", "keep.txt")))

    # CLI

    def test_no_docker_without_deps_has_nothing_to_do(self):
        self.assertEqual(cl.main(["--no-docker"]), 2)

    @unittest.skipIf(LINUX and os.geteuid() == 0, "the root check cannot fail as root")
    def test_deps_removal_needs_root(self):
        with mock.patch.object(cl.shutil, "which", return_value="/usr/bin/docker"):
            self.assertEqual(cl.main(["--deps", "--y", "--no-docker"]), 2)

    def test_no_docker_dry_run_never_runs_docker(self):
        calls = []
        with mock.patch.object(cl, "sh", side_effect=lambda a, check=True: calls.append(a)), \
                mock.patch.object(cl.shutil, "which", return_value=None):
            rc = cl.main(["--no-docker", "--deps", "--dry-run", "--deps-roots", self.root])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])

    def test_failed_docker_ps_is_not_no_containers(self):
        r = mock.Mock(returncode=1, stdout="", stderr="Cannot connect")
        with mock.patch.object(cl, "sh", return_value=r), \
                mock.patch.object(cl.shutil, "which", return_value="/usr/bin/docker"):
            with self.assertRaises(RuntimeError):
                cl.bind_mount_sources()


if __name__ == "__main__":
    unittest.main()
