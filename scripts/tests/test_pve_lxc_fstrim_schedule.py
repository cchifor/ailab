"""The nightly LXC volume trim's schedule and settings (ansible role pve_base, tag lxc_fstrim).

Each Proxmox host that runs the sweep (pve_lxc_fstrim_enabled) has its own slot. A trim is a discard burst
on the NVMe that the host's etcd member shares, so the hosts take turns:
- the unit's worst case, from the timer's start to the end of TimeoutStartSec plus TimeoutStopSec (a run
  that waits out IO pressure until systemd stops it, then finishes its chunk), stays inside 00:30-05:30 UTC
  and never overlaps another host's;
- TimeoutStartSec covers the run budget plus one chunk at SEC_PER_GB (the sweep's deadline lets a chunk
  start only if it can end by then), and TimeoutStopSec covers one chunk at the measured worst (23 s per
  GiB) plus a minute to unmount;
- a container that has its own trim unit (the registry, docs/runbooks/registry-cache.md) is excluded on
  its host, so no volume is trimmed twice;
- every inner-script setting the role renders is one lxc-fstrim-chunked.sh reads.
Fixture cases mutate the real settings to prove each check fails when it should.

Needs PyYAML and Jinja2 (the CI runners have both; a missing one fails the test, it never skips).
"""

import copy
import math
import pathlib
import re
import unittest

import jinja2
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
ROLE = REPO / "ansible/roles/pve_base"
BAND = ("00:30", "05:30")
WORST_SEC_PER_GIB = 23  # measured worst TRIM cost on these drives (registry-cache.md, ci-runners.md)


def mins(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {} if path.exists() else {}


def pve_hosts():
    inv = yaml.safe_load((REPO / "inventory/hosts.yml").read_text(encoding="utf-8"))

    def find(node):
        if isinstance(node, dict):
            if "pve_nodes" in node:
                return node["pve_nodes"]
            for v in node.values():
                r = find(v)
                if r is not None:
                    return r
        return None

    group = find(inv)
    return sorted((group or {}).get("hosts", {}))


def host_vars():
    base = load(ROLE / "defaults/main.yml")
    base.update(load(REPO / "ansible/group_vars/all.yml"))
    out = {}
    for h in pve_hosts():
        v = dict(base)
        v.update(load(REPO / f"ansible/host_vars/{h}.yml"))
        out[h] = v
    return out


ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)


def render(name, v):
    return ENV.from_string((ROLE / "templates" / name).read_text(encoding="utf-8")).render(**v)


def key(body, k):
    m = re.findall(r"^" + k + r"=(.*)$", body, re.M)
    return m[-1].strip() if m else None


def registry_units():
    """{host: [ctid, ...]} for the hand-installed per-container trim units in registry-cache.md."""
    reg = (REPO / "docs/runbooks/registry-cache.md").read_text(encoding="utf-8")
    sec = re.search(r"^## Thin-volume trim \(weekly, (ai-node\d+)\)$", reg, re.M)
    cts = re.findall(r"^ExecStart=/usr/local/sbin/lxc-fstrim-chunked\.sh (\d+) (?:rootfs|mp\d+)$", reg, re.M)
    return {sec.group(1): [int(c) for c in cts]} if sec else {}


def problems(by_host, registry):
    """Every schedule/settings problem, as text; [] when the schedule is sound."""
    out, spans = [], []
    for h, v in sorted(by_host.items()):
        if not v.get("pve_lxc_fstrim_enabled"):
            continue
        start = v.get("pve_lxc_fstrim_start")
        if not (isinstance(start, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", start)):
            out.append(f"{h}: pve_lxc_fstrim_start {start!r} is not a quoted HH:MM")
            continue
        svc, tmr = render("pve-lxc-fstrim.service.j2", v), render("pve-lxc-fstrim.timer.j2", v)
        cal = key(tmr, "OnCalendar")
        if cal != f"*-*-* {start}:00 UTC":
            out.append(f"{h}: OnCalendar {cal!r} is not the slot {start} UTC")
        if key(tmr, "Persistent") != "false":
            out.append(f"{h}: the timer must not catch up a missed night (Persistent=false)")
        acc = key(tmr, "AccuracySec")
        acc_s = int(acc[:-1]) if acc and re.fullmatch(r"\d+s", acc) else 60
        tstart, tstop = int(key(svc, "TimeoutStartSec") or 0), int(key(svc, "TimeoutStopSec") or 0)
        run_s = int(v["pve_lxc_fstrim_run_max_min"]) * 60
        chunk = int(v["pve_lxc_fstrim_chunk_gb"])
        if tstart < run_s + chunk * int(v["pve_lxc_fstrim_sec_per_gb"]):
            out.append(f"{h}: TimeoutStartSec {tstart} s does not cover the {run_s} s budget plus one chunk")
        if tstop < chunk * WORST_SEC_PER_GIB + 60:
            out.append(f"{h}: TimeoutStopSec {tstop} s does not cover an in-flight {chunk} GiB chunk and the unmount")
        s = mins(start)
        e = s + math.ceil((acc_s + tstart + tstop) / 60)
        if s < mins(BAND[0]) or e > mins(BAND[1]):
            out.append(f"{h}: {hm(s)}-{hm(e)} (worst case) leaves the {BAND[0]}-{BAND[1]} UTC band")
        spans.append((s, e, h))
        excl = {int(c) for c in v.get("pve_lxc_fstrim_exclude", [])}
        for ct in registry.get(h, []):
            if ct not in excl:
                out.append(f"{h}: container {ct} has its own trim unit (registry-cache.md) but is not excluded")
    spans.sort()
    for i, a in enumerate(spans):
        for b in spans[i + 1:]:
            if b[0] < a[1]:
                out.append(f"{a[2]} ({hm(a[0])}-{hm(a[1])}) and {b[2]} ({hm(b[0])}-{hm(b[1])}) overlap")
    return out


class PveLxcFstrimSchedule(unittest.TestCase):
    def setUp(self):
        self.vars = host_vars()
        self.registry = registry_units()

    def test_hosts_and_registry_are_found(self):
        self.assertEqual(["ai-node1", "ai-node2", "ai-node3"], sorted(self.vars))
        # no hand-installed per-container trim unit since CT 5004's decommission (2026-11-22)
        self.assertEqual({}, self.registry)

    def test_a_host_switched_off_is_not_a_failure(self):
        # The kill switch is pve_lxc_fstrim_enabled: false in one host's host_vars: no test may require it on.
        # problems() checks every host that has it on, and ignores the others.
        self.assertEqual([], self.mutated("ai-node2", pve_lxc_fstrim_enabled=False))
        self.assertEqual([], self.mutated("ai-node1", pve_lxc_fstrim_enabled=False))

    def test_the_schedule_is_sound(self):
        self.assertEqual([], problems(self.vars, self.registry))

    def test_the_role_default_is_off(self):
        self.assertIs(False, load(ROLE / "defaults/main.yml")["pve_lxc_fstrim_enabled"])

    def test_rendered_settings_are_ones_the_inner_script_reads(self):
        env = render("pve-lxc-fstrim.env.j2", self.vars["ai-node2"])
        inner = (REPO / "scripts/lxc-fstrim-chunked.sh").read_text(encoding="utf-8")
        sweep = (ROLE / "files/pve-lxc-fstrim.sh").read_text(encoding="utf-8")
        names = re.findall(r"^([A-Z_]+)=", env, re.M)
        self.assertIn("CHUNK_GB", names)
        for n in names:
            src = sweep if n.startswith("PVE_LXC_FSTRIM_") else inner
            self.assertRegex(src, r'(?m)^\s*\w+="\$\{' + n + r":-", f"{n} is rendered but never read")

    # ---- the checks bite --------------------------------------------------------------------------
    def mutated(self, host, **kv):
        v = copy.deepcopy(self.vars)
        v[host].update(kv)
        return problems(v, self.registry)

    def test_two_hosts_in_one_slot_overlap(self):
        self.assertTrue(any("overlap" in p for p in self.mutated("ai-node3", pve_lxc_fstrim_start="04:30")))

    def test_a_longer_budget_runs_into_the_next_host(self):
        self.assertTrue(any("overlap" in p for p in self.mutated("ai-node2", pve_lxc_fstrim_run_max_min=16)))

    def test_a_late_slot_leaves_the_band(self):
        self.assertTrue(any("band" in p for p in self.mutated("ai-node1", pve_lxc_fstrim_start="05:20")))

    def test_an_early_slot_leaves_the_band(self):
        self.assertTrue(any("band" in p for p in self.mutated("ai-node1", pve_lxc_fstrim_start="00:10")))

    def test_a_bigger_chunk_needs_a_longer_stop_and_start(self):
        # the timeouts follow chunk_gb in the template, so the span grows instead of the check failing
        self.assertTrue(any("overlap" in p for p in self.mutated("ai-node2", pve_lxc_fstrim_chunk_gb=16)))

    def test_a_container_with_its_own_unit_must_be_excluded(self):
        # a dedicated unit (like CT 5004's weekly trim was) on a host whose sweep does not exclude the container
        self.assertTrue(any("5099" in p for p in problems(self.vars, {"ai-node1": [5099]})))
        self.assertEqual([], problems(self.vars, {}))

    def test_an_unquoted_start_is_refused(self):
        self.assertTrue(any("quoted" in p for p in self.mutated("ai-node1", pve_lxc_fstrim_start=310)))


if __name__ == "__main__":
    unittest.main()
