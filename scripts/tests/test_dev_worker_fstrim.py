"""The dev workers' daily trim of /workspace and / (ansible role dev_worker, tag fstrim).

- The role default is OFF (the daily fleet converge applies the role from main to every worker); each worker
  opts in through host_vars with its own slot.
- The workers take turns: each unit's worst case (timer accuracy, TimeoutStartSec, then TimeoutStopSec for the
  chunk in flight) stays inside the night's free bands (after the 00:00 runner-trim start and infra-pg dump,
  around talos-backup at 01:00, before Velero at 02:00) and never overlaps another worker's.
- The timeouts cover the run budget plus one chunk, and a chunk at the measured worst plus a minute.
- The unit trims exactly dev_worker_fstrim_mounts, with settings node-fstrim.sh reads, from the script the
  Talos node trim uses (one file).
- The root fstab edit uses gitea_runner's three regexes verbatim; applied as ansible.builtin.replace applies
  them, they drop only a whole `discard` token and a second pass changes nothing.
- Neither switch starts a trim: a new slot stops the timer before its file changes, and the kill switch dates the
  stock timer's stamp before starting it (the behaviour itself: the Docker converge test).
Fixture cases mutate the real settings to prove the slot check fails when it should.
"""

import copy
import math
import pathlib
import re
import unittest

import jinja2
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
ROLE = REPO / "ansible/roles/dev_worker"
# Free bands (UTC minutes): after 00:30 (runner trims start and the infra-pg dump at 00:00), not over
# 01:00-01:15 (talos-backup), done by 02:00 (Velero and the talos-backup prune).
BANDS = [(30, 60), (75, 120)]
WORST_SEC_PER_GIB = 23
ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)


def load(path):
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def resolve(v):
    """Render the trim's templated defaults (the mounts list names dev_worker_workspace_mount) against v."""
    def r(x):
        if isinstance(x, str) and "{{" in x:
            return ENV.from_string(x).render(**v)
        if isinstance(x, list):
            return [r(i) for i in x]
        return x
    return {k: (r(x) if k.startswith("dev_worker_fstrim_") else x) for k, x in v.items()}


def worker_vars():
    inv = yaml.safe_load((REPO / "inventory/hosts.yml").read_text(encoding="utf-8"))

    def find(node):
        if isinstance(node, dict):
            if "dev_workers" in node and isinstance(node["dev_workers"], dict):
                return node["dev_workers"]
            for x in node.values():
                r = find(x)
                if r is not None:
                    return r
        return None

    hosts = sorted((find(inv) or {}).get("hosts", {}))
    base = load(ROLE / "defaults/main.yml")
    base.update(load(REPO / "ansible/group_vars/dev_workers.yml"))
    out = {}
    for h in hosts:
        v = dict(base)
        v.update(load(REPO / f"ansible/host_vars/{h}.yml"))
        out[h] = resolve(v)
    return out


def render(name, v):
    return ENV.from_string((ROLE / "templates" / name).read_text(encoding="utf-8")).render(**v)


def key(body, k):
    m = re.findall(r"^" + k + r"=(.*)$", body, re.M)
    return m[-1].strip() if m else None


def problems(by_host):
    out, spans = [], []
    for h, v in sorted(by_host.items()):
        if not v.get("dev_worker_fstrim_enabled"):
            continue
        start = v.get("dev_worker_fstrim_start")
        if not (isinstance(start, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", start)):
            out.append(f"{h}: dev_worker_fstrim_start {start!r} is not a quoted HH:MM")
            continue
        svc, tmr = render("dev-worker-fstrim.service.j2", v), render("dev-worker-fstrim.timer.j2", v)
        if key(tmr, "OnCalendar") != f"*-*-* {start}:00 UTC":
            out.append(f"{h}: OnCalendar {key(tmr, 'OnCalendar')!r} is not the slot")
        if key(tmr, "Persistent") != "false":
            out.append(f"{h}: a missed night must not run late (Persistent=false)")
        acc = key(tmr, "AccuracySec")
        acc_s = int(acc[:-1]) if acc and re.fullmatch(r"\d+s", acc) else 60
        tstart, tstop = int(key(svc, "TimeoutStartSec") or 0), int(key(svc, "TimeoutStopSec") or 0)
        run_s, chunk = int(v["dev_worker_fstrim_run_max_min"]) * 60, int(v["dev_worker_fstrim_chunk_gb"])
        if tstart < run_s + chunk * int(v["dev_worker_fstrim_sec_per_gb"]):
            out.append(f"{h}: TimeoutStartSec {tstart} s does not cover the {run_s} s budget plus one chunk")
        if tstop < chunk * WORST_SEC_PER_GIB + 60:
            out.append(f"{h}: TimeoutStopSec {tstop} s does not cover an in-flight {chunk} GiB chunk")
        s = int(start[:2]) * 60 + int(start[3:])
        e = s + math.ceil((acc_s + tstart + tstop) / 60)
        if not any(a <= s and e <= b for a, b in BANDS):
            out.append(f"{h}: {hm(s)}-{hm(e)} (worst case) is not inside one of the free bands "
                       + ", ".join(f"{hm(a)}-{hm(b)}" for a, b in BANDS))
        spans.append((s, e, h))
    spans.sort()
    for i, a in enumerate(spans):
        for b in spans[i + 1:]:
            if b[0] < a[1]:
                out.append(f"{a[2]} ({hm(a[0])}-{hm(a[1])}) and {b[2]} ({hm(b[0])}-{hm(b[1])}) overlap")
    return out


def fstab_items(tasks_file):
    tasks = yaml.safe_load(tasks_file.read_text(encoding="utf-8"))
    for t in tasks:
        if t.get("ansible.builtin.replace", {}).get("path") == "/etc/fstab":
            return t["loop"]
    raise AssertionError(f"no /etc/fstab replace task in {tasks_file}")


def block(tasks, prefix):
    for t in tasks:
        if str(t.get("name", "")).startswith(prefix) and "block" in t:
            return t["block"]
    raise AssertionError(f"no block named {prefix!r}")


def index(tasks, pred, what):
    hits = [i for i, t in enumerate(tasks) if pred(t)]
    if len(hits) != 1:
        raise AssertionError(f"expected one task that {what}, found {len(hits)}")
    return hits[0]


def systemd(t, unit, state):
    s = t.get("ansible.builtin.systemd") or {}
    return s.get("name") == unit and s.get("state") == state


def apply_replace(text, items):
    for it in items:  # ansible.builtin.replace: Python re, MULTILINE, \g<1> backreferences
        text = re.compile(it["regexp"], re.MULTILINE).sub(it["replace"], text)
    return text


class DevWorkerFstrim(unittest.TestCase):
    def setUp(self):
        self.vars = worker_vars()

    def test_the_workers_are_found(self):
        self.assertEqual(["dev-worker-1", "dev-worker-2", "dev-worker-3", "dev-worker-4"], sorted(self.vars))

    def test_off_in_the_role_and_the_group(self):
        # Per worker, on or off, is host_vars' business: the kill switch is dev_worker_fstrim_enabled: false in one
        # worker's host_vars, so no test may require it on. problems() checks every worker that has it on.
        d = load(ROLE / "defaults/main.yml")
        self.assertIs(False, d["dev_worker_fstrim_enabled"])
        self.assertIs(False, d["dev_worker_root_nodiscard"])
        self.assertNotIn("dev_worker_fstrim_enabled", load(REPO / "ansible/group_vars/dev_workers.yml"))
        self.assertNotIn("dev_worker_root_nodiscard", load(REPO / "ansible/group_vars/dev_workers.yml"))

    def test_a_worker_switched_off_is_not_a_failure(self):
        v = copy.deepcopy(self.vars)
        v["dev-worker-2"]["dev_worker_fstrim_enabled"] = False
        self.assertEqual([], problems(v))

    def test_the_schedule_is_sound(self):
        self.assertEqual([], problems(self.vars))

    def test_the_unit_trims_the_mounts_with_settings_the_script_reads(self):
        v = self.vars["dev-worker-2"]
        self.assertEqual(["/workspace", "/"], v["dev_worker_fstrim_mounts"])
        svc = render("dev-worker-fstrim.service.j2", v)
        self.assertEqual("/usr/local/sbin/node-fstrim /workspace /", key(svc, "ExecStart"))
        self.assertEqual("/workspace /", key(svc, "RequiresMountsFor"))
        script = (REPO / "kubernetes/apps/node-fstrim/node-fstrim.sh").read_text(encoding="utf-8")
        env = render("dev-worker-fstrim.env.j2", v)
        names = re.findall(r"^([A-Z_]+)=", env, re.M)
        self.assertIn("TEXTFILE_DIR", names)
        self.assertIn("STATE_DIR=/var/lib/node-fstrim", env.splitlines())
        self.assertEqual("node-fstrim", key(svc, "StateDirectory"))
        for n in names:
            self.assertRegex(script, r"(?m)^" + n + r'="\$\{' + n + r":-", f"{n} is rendered but never read")
        tasks = (ROLE / "tasks/fstrim.yml").read_text(encoding="utf-8")
        self.assertIn('src: "{{ role_path }}/../../../kubernetes/apps/node-fstrim/node-fstrim.sh"', tasks)

    def test_the_fstab_regexes_are_gitea_runners(self):
        mine = fstab_items(ROLE / "tasks/fstrim.yml")
        runner = fstab_items(REPO / "ansible/roles/gitea_runner/tasks/main.yml")
        self.assertEqual([(i["regexp"], i["replace"]) for i in runner], [(i["regexp"], i["replace"]) for i in mine])

    def test_the_fstab_edit_drops_only_the_discard_token(self):
        items = fstab_items(ROLE / "tasks/fstrim.yml")
        cases = {
            "LABEL=cloudimg-rootfs\t/\t ext4\tdiscard,commit=30,errors=remount-ro\t0 1\n":
                "LABEL=cloudimg-rootfs\t/\t ext4\tcommit=30,errors=remount-ro\t0 1\n",
            "LABEL=cloudimg-rootfs / ext4 commit=30,discard 0 1\n": "LABEL=cloudimg-rootfs / ext4 commit=30 0 1\n",
            "LABEL=cloudimg-rootfs / ext4 discard 0 1\n": "LABEL=cloudimg-rootfs / ext4 defaults 0 1\n",
            "LABEL=cloudimg-rootfs / ext4 nodiscard,commit=30 0 1\n": "LABEL=cloudimg-rootfs / ext4 nodiscard,commit=30 0 1\n",
            "LABEL=workspace /workspace ext4 discard,nofail 0 2\n": "LABEL=workspace /workspace ext4 discard,nofail 0 2\n",
        }
        for before, after in cases.items():
            once = apply_replace(before, items)
            self.assertEqual(after, once, before)
            self.assertEqual(once, apply_replace(once, items), f"second pass changed {once!r}")

    def test_a_new_slot_unloads_the_timer_before_its_file_changes(self):
        # systemd plans a loaded timer from when it last fired or was started, at every reload and start, and fires
        # it at once if the new slot is already due (converge.sh, "a new slot"). Stopped AND disabled before its file
        # is written, the unit is dropped and comes back planning from now; a stop alone, or a restart, is not enough.
        tasks = block(yaml.safe_load((ROLE / "tasks/fstrim.yml").read_text(encoding="utf-8")), "Enabled")
        check = index(tasks, lambda t: (t.get("ansible.builtin.template") or {}).get("src") == "dev-worker-fstrim.timer.j2",
                      "dry-renders the timer")
        stop = index(tasks, lambda t: systemd(t, "dev-worker-fstrim.timer", "stopped"), "stops the timer")
        write = index(tasks, lambda t: "dev-worker-fstrim.timer" in (t.get("loop") or []), "writes the timer")
        start = index(tasks, lambda t: systemd(t, "dev-worker-fstrim.timer", "started"), "starts the timer")
        self.assertTrue(check < stop < write < start, (check, stop, write, start))
        self.assertIs(True, tasks[check].get("check_mode"))
        self.assertIn(f"{tasks[check]['register']} is changed", tasks[stop]["when"])
        self.assertIs(False, tasks[stop]["ansible.builtin.systemd"].get("enabled"))
        self.assertIs(True, tasks[start]["ansible.builtin.systemd"].get("enabled"))
        self.assertIs(True, tasks[start]["ansible.builtin.systemd"].get("daemon_reload"))
        self.assertFalse(any(systemd(t, "dev-worker-fstrim.timer", "restarted") for t in tasks))

    def test_the_stock_timer_is_stopped_before_it_is_masked(self):
        # Masking a running timer makes systemd fail it ("Unit to trigger vanished"): the worker goes `degraded`.
        tasks = block(yaml.safe_load((ROLE / "tasks/fstrim.yml").read_text(encoding="utf-8")), "Enabled")
        stop = index(tasks, lambda t: systemd(t, "fstrim.timer", "stopped"), "stops the stock timer")
        mask = index(tasks, lambda t: (t.get("ansible.builtin.systemd") or {}).get("masked") is True, "masks it")
        self.assertLess(stop, mask)
        self.assertNotIn("masked", tasks[stop]["ansible.builtin.systemd"])

    def test_the_kill_switch_dates_the_stock_timer_before_starting_it(self):
        # Persistent=true: started with its weeks-old stamp, the stock timer would catch up at once (converge.sh).
        takedown = block(block(yaml.safe_load((ROLE / "tasks/fstrim.yml").read_text(encoding="utf-8")), "Disabled"),
                         "Take down")
        touch = index(takedown, lambda t: (t.get("ansible.builtin.file") or {}).get("path")
                      == "/var/lib/systemd/timers/stamp-fstrim.timer", "touches the stamp")
        self.assertEqual("touch", takedown[touch]["ansible.builtin.file"]["state"])
        start = index(takedown, lambda t: systemd(t, "fstrim.timer", "started"), "starts the stock timer")
        self.assertLess(touch, start)

    # ---- the checks bite --------------------------------------------------------------------------
    def mutated(self, host, **kv):
        v = copy.deepcopy(self.vars)
        v[host].update(kv)
        return problems(v)

    def test_two_workers_in_one_slot_overlap(self):
        self.assertTrue(any("overlap" in p for p in self.mutated("dev-worker-2", dev_worker_fstrim_start="00:30")))

    def test_a_longer_budget_runs_into_the_next_worker(self):
        self.assertTrue(any("overlap" in p for p in self.mutated("dev-worker-1", dev_worker_fstrim_run_max_min=7)))

    def test_a_slot_across_talos_backup_is_refused(self):
        self.assertTrue(any("band" in p for p in self.mutated("dev-worker-2", dev_worker_fstrim_start="00:55")))

    def test_a_slot_into_velero_is_refused(self):
        self.assertTrue(any("band" in p for p in self.mutated("dev-worker-4", dev_worker_fstrim_start="01:52")))

    def test_a_slot_at_the_runner_trim_start_is_refused(self):
        self.assertTrue(any("band" in p for p in self.mutated("dev-worker-1", dev_worker_fstrim_start="00:05")))


if __name__ == "__main__":
    unittest.main()
