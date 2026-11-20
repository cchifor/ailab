"""What the Local Drives dashboard's joins RETURN, evaluated by promtool on synthetic series.

The structural tests (test_local_drives_dashboard.py) check the queries' shape; these check their answers.
The dashboard's own queries are taken from the generator, the Grafana variables substituted ($host = two
of three hosts, $__range = 1h), and run as `promql_expr_test`s by `promtool test rules` in the same pinned
Prometheus image scripts/rules-lint.sh uses (Docker, no network). Pinned here:

  * a dm-N number that changes owner in the MIDDLE of a 5-minute window, with different write rates on either
    side: each guest gets exactly its own I/O, not the other's and no false reset (reviewer-codex on #1890),
    and the host's root counts as "<host> root";
  * the untrimmed gap and "filesystem used" count only filesystems on the guest's LOCAL volumes, with
    evidence (reviewer-codex on #1890): a VM whose source volume was kept after its disk moved to the QNAP
    (closed on the host) gets no figure, nor one whose only local volume is an unused disk beside a remote
    one, nor one with one local and one remote disk; a Talos node's iSCSI PVC filesystem is left out; a
    container is matched per volume by device-mapper name, and its unused volume is left out of the gap; a
    bind mount counts once; a guest rebuilt under a new vmid within the hour (two vmids, one name) neither
    fails the query nor moves the figure to the old vmid;
  * allocation, virtual size, allocated %, share of the pool and the pool's metadata %, per guest, named,
    only on the selected hosts, with a cloud-pve guest reusing a vmid kept out;
  * bytes written per day is the raw counter difference, and stays positive across a drive replacement,
    where the raw difference goes negative (reviewer-codex on #1891); no projection before 1% used;
  * the trim table's ages for the four trim jobs.

Needs docker (fails closed without it, like rules-lint.sh).

    python3 -m unittest discover -s scripts/tests -p "test_local_drives_dashboard_promql.py" -v
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
GEN = ROOT / "scripts/gen-local-drives-dashboard.py"
RULES_LINT = ROOT / "scripts/rules-lint.sh"

GiB = 1073741824
MiB = 1048576
H1, H2, H3 = "192.168.0.2:9100", "192.168.0.3:9100", "192.168.0.4:9100"
SUBST = {"$host": "(ai-node1|ai-node2)", "$__rate_interval": "2m", "$__range_s": "3600", "$__range": "1h",
         "$topn": "8"}


def _generator():
    spec = importlib.util.spec_from_file_location("gen_local_drives_dashboard", GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GEN_MOD = _generator()
DASH = GEN_MOD.build()


def sub(expr):
    for k in sorted(SUBST, key=len, reverse=True):
        expr = expr.replace(k, SUBST[k])
    return expr


def panel_expr(title_prefix, ref="A"):
    for p in DASH["panels"]:
        if p["title"].startswith(title_prefix):
            for t in p.get("targets", []):
                if t["refId"] == ref:
                    return sub(t["expr"])
    raise AssertionError(f"no panel {title_prefix!r} {ref}")


def _image():
    m = re.search(r'^PROMETHEUS_IMAGE="([^"]+)"', RULES_LINT.read_text(encoding="utf-8"), re.M)
    assert m, "PROMETHEUS_IMAGE not found in scripts/rules-lint.sh"
    return m.group(1)


class Series:
    def __init__(self):
        self.items = []

    def add(self, name, labels, values):
        self.items.append({"series": name + "{" + ", ".join(f'{k}="{v}"' for k, v in labels.items()) + "}",
                           "values": values})


def sample(labels, value):
    return {"labels": "{" + ", ".join(f'{k}="{v}"' for k, v in labels.items()) + "}", "value": value}


def guest(node, name, typ, vmid):
    return {"name": name, "nodename": node, "type": typ, "vmid": vmid}


CI1 = guest("ai-node1", "ci-runner-1", "qemu", "4101")
MOVED = guest("ai-node1", "moved-vm", "qemu", "4203")
MIXED = guest("ai-node2", "mixed-vm", "qemu", "4206")
CI4 = guest("ai-node1", "ci-runner-4", "qemu", "4104")
DW1 = guest("ai-node1", "dev-worker-1", "qemu", "4201")
REG = guest("ai-node1", "registry", "lxc", "5004")
LLM2 = guest("ai-node2", "ai-llm-2", "lxc", "5002")
CP2 = guest("ai-node2", "talos-cp2", "qemu", "4002")
DW2 = guest("ai-node2", "dev-worker-2", "qemu", "4202")


def estate():
    """Two selected hosts and one that is not; guests of every kind; 70 one-minute samples (0-69m)."""
    s = Series()
    for inst, node in ((H1, "ai-node1"), (H2, "ai-node2"), (H3, "ai-node3")):
        s.add("node_uname_info", {"job": "proxmox-node", "instance": inst, "nodename": node}, "1x2880")
        s.add("pve_disk_size_bytes", {"job": "pve-exporter", "id": f"storage/{node}/local-lvm"}, f"{1755 * GiB}x70")
        s.add("pve_thin_pool_metadata_percent", {"job": "proxmox-node", "instance": inst, "pool": "pve/data"}, "2.5x70")
    for gid, name, node in (("qemu/4101", "ci-runner-1", "ai-node1"), ("qemu/4104", "ci-runner-4", "ai-node1"),
                            ("qemu/4201", "dev-worker-1", "ai-node1"), ("lxc/5004", "registry", "ai-node1"),
                            ("lxc/5002", "ai-llm-2", "ai-node2"), ("qemu/4002", "talos-cp2", "ai-node2"),
                            ("qemu/4202", "dev-worker-2", "ai-node2"), ("qemu/4103", "ci-runner-3", "ai-node3"),
                            ("qemu/4203", "moved-vm", "ai-node1"), ("qemu/4206", "mixed-vm", "ai-node2")):
        s.add("pve_guest_info", {"job": "pve-exporter", "id": gid, "name": name, "node": node,
                                 "type": gid.split("/")[0]}, "1x70")
    # ci-runner-1 was rebuilt: its old vmid 4199 left pve-exporter at 40m, so for the last hour two vmids share its
    # name. The name -> vmid joins must not fail on it, and the current vmid must win (reviewer-claude on #1890).
    s.add("pve_guest_info", {"job": "pve-exporter", "id": "qemu/4199", "name": "ci-runner-1", "node": "ai-node1",
                             "type": "qemu"}, "1x40")
    # cloudlab's exporter reuses vmid 4101
    s.add("pve_guest_info", {"job": "cloud-pve", "id": "qemu/4101", "name": "cloud-ci-1", "node": "cloud1",
                             "type": "qemu"}, "1x70")

    def lv(inst, name, vmid, size, alloc, is_open=1):
        L = {"job": "proxmox-node", "instance": inst, "pool": "pve/data", "lv": name, "vmid": vmid}
        s.add("pve_thin_lv_size_bytes", L, f"{size}x70")
        s.add("pve_thin_lv_allocated_bytes", L, f"{alloc}x70")
        s.add("pve_thin_lv_open", L, f"{is_open}x70")
    lv(H1, "vm-4101-disk-0", "4101", 250 * GiB, 100 * GiB)
    lv(H1, "vm-4101-cloudinit", "4101", 4 * MiB, 393216)
    lv(H1, "vm-4201-disk-0", "4201", 60 * GiB, 30 * GiB)
    lv(H1, "vm-4201-disk-1", "4201", 128 * GiB, 100 * GiB)
    lv(H1, "vm-5004-disk-0", "5004", 16 * GiB, 5 * GiB)
    lv(H2, "vm-5002-disk-0", "5002", 24 * GiB, 10 * GiB)
    lv(H2, "vm-5002-disk-1", "5002", 160 * GiB, 120 * GiB)
    lv(H2, "vm-5002-disk-2", "5002", 8 * GiB, 7 * GiB, is_open=0)  # an unused container volume
    lv(H1, "vm-4203-disk-0", "4203", 80 * GiB, 70 * GiB, is_open=0)  # source kept after a move to the QNAP
    lv(H2, "vm-4206-disk-1", "4206", 16 * GiB, 10 * GiB, is_open=0)  # unused, beside a remote attached disk
    lv(H2, "vm-4002-disk-0", "4002", 76 * GiB, 50 * GiB)
    lv(H2, "vm-4202-disk-0", "4202", 60 * GiB, 40 * GiB)       # its second disk is on the QNAP
    lv(H3, "vm-4103-disk-0", "4103", 250 * GiB, 99 * GiB)      # unselected host

    def node(job, inst, nodename, disks, mounts):
        s.add("node_uname_info", {"job": job, "instance": inst, "nodename": nodename}, "1x70")
        for dev, model in disks:
            s.add("node_disk_info", {"job": job, "instance": inst, "device": dev, "model": model}, "1x70")
        for dev, mp, fstype, size, free in mounts:
            L = {"job": job, "instance": inst, "device": dev, "mountpoint": mp, "fstype": fstype}
            s.add("node_filesystem_size_bytes", L, f"{size}x70")
            s.add("node_filesystem_free_bytes", L, f"{free}x70")
    Q, DVD, ISCSI = "QEMU_HARDDISK", "QEMU_DVD-ROM", "iSCSI_Storage"
    node("ci-runner-node", "192.168.0.14:9100", "ci-runner-1", [("sda", Q), ("sr0", DVD)],
         [("/dev/sda1", "/", "ext4", 240 * GiB, 180 * GiB), ("/dev/sda16", "/boot", "ext4", GiB, GiB // 2),
          ("tmpfs", "/run", "tmpfs", GiB, 0)])
    node("dev-worker-node", "192.168.0.8:9100", "dev-worker-1", [("sda", Q), ("sdb", Q), ("sr0", DVD)],
         [("/dev/sda1", "/", "ext4", 57 * GiB, 37 * GiB), ("/dev/sdb", "/workspace", "ext4", 125 * GiB, 55 * GiB),
          ("/dev/sdb", "/home/c4/ws", "ext4", 125 * GiB, 55 * GiB)])
    node("dev-worker-node", "192.168.0.9:9100", "dev-worker-2", [("sda", Q), ("sdb", Q), ("sr0", DVD)],
         [("/dev/sda1", "/", "ext4", 57 * GiB, 27 * GiB), ("/dev/sdb", "/workspace", "ext4", 125 * GiB, 25 * GiB)])
    node("node-exporter", "192.168.0.42:9100", "talos-cp2", [("sda", Q), ("sr0", DVD), ("sdb", ISCSI)],
         [("/dev/sda5", "/var", "xfs", 76 * GiB, 36 * GiB),
          ("/dev/sdb", "/var/lib/kubelet/plugins/pvc", "xfs", 10 * GiB, 2 * GiB)])
    node("ai-llm-node", "192.168.0.45:9100", "ai-llm-2", [("nvme0n1", "KINGSTON"), ("dm-14", "")],
         [("/dev/mapper/pve-vm--5002--disk--0", "/", "ext4", 24 * GiB, 16 * GiB),
          ("/dev/mapper/pve-vm--5002--disk--1", "/models-local", "ext4", 160 * GiB, 60 * GiB)])
    node("dev-worker-node", "192.168.0.12:9100", "moved-vm", [("sda", Q), ("sr0", DVD)],
         [("/dev/sda1", "/", "ext4", 79 * GiB, 49 * GiB)])
    node("dev-worker-node", "192.168.0.13:9100", "mixed-vm", [("sda", Q), ("sr0", DVD)],
         [("/dev/sda1", "/", "ext4", 39 * GiB, 19 * GiB)])
    node("ci-runner-node", "192.168.0.16:9100", "ci-runner-3", [("sda", Q)],
         [("/dev/sda1", "/", "ext4", 240 * GiB, 200 * GiB)])

    # Host device-mapper I/O on ai-node1: dm-9 belongs to ci-runner-1 until 32m, in the middle of a 5-minute
    # window; then (ci-runner-1 stopped, ci-runner-4 started) to ci-runner-4, and its counter starts again.
    # ci-runner-1 writes 1 MiB/min, ci-runner-4 2 MiB/min, the host root 0.5 MiB/min all hour.
    dm = {"job": "proxmox-node", "instance": H1}
    s.add("node_disk_device_mapper_info", {**dm, "device": "dm-9", "lv_name": "vm-4101-disk-0"}, "1x32 stale")
    s.add("node_disk_device_mapper_info", {**dm, "device": "dm-9", "lv_name": "vm-4104-disk-0"}, "_x33 1x36")
    s.add("node_disk_written_bytes_total", {**dm, "device": "dm-9"}, f"0+{MiB}x32 {2 * MiB}+{2 * MiB}x36")
    s.add("node_disk_device_mapper_info", {**dm, "device": "dm-1", "lv_name": "root"}, "1x70")
    s.add("node_disk_written_bytes_total", {**dm, "device": "dm-1"}, f"0+{MiB // 2}x69")

    # The drives' data-units-written counters, one per minute for 2 days: ai-node1 steady at 1000/min;
    # ai-node2's drive replaced at 36 h (the new drive starts at 100, 500/min, 0% used).
    nv1 = {"job": "proxmox-node", "instance": H1, "device": "nvme0n1"}
    nv2 = {"job": "proxmox-node", "instance": H2, "device": "nvme0n1"}
    s.add("nvme_data_units_written_total", nv1, "0+1000x2880")
    s.add("nvme_data_units_written_total", nv2, "5000000+1000x2160 100+500x719")
    s.add("nvme_percentage_used_ratio", nv1, "0.5x2880")
    s.add("nvme_percentage_used_ratio", nv2, "0.8x2160 0x719")

    # trim beacons (evaluated at 1h = 3600 s)
    s.add("gitea_runner_fstrim_last_run_seconds", {"job": "ci-runner-node", "instance": "192.168.0.14:9100"}, "600x70")
    s.add("gitea_runner_fstrim_last_run_seconds", {"job": "ci-runner-node", "instance": "192.168.0.16:9100"}, "700x70")
    s.add("node_fstrim_last_success_timestamp_seconds", {"job": "dev-worker-node", "instance": "192.168.0.8:9100"},
          "1200x70")
    s.add("pve_lxc_fstrim_volume_last_complete_timestamp_seconds",
          {"job": "proxmox-node", "instance": H2, "ctid": "5002", "volume": "rootfs"}, "1000x70")
    s.add("pve_lxc_fstrim_volume_last_complete_timestamp_seconds",
          {"job": "proxmox-node", "instance": H2, "ctid": "5002", "volume": "mp0"}, "800x70")
    s.add("kube_cronjob_status_last_successful_time", {"namespace": "node-fstrim", "cronjob": "node-fstrim-talos-cp2"},
          "100x70")
    return s.items


def expr_tests():
    T = "Per guest: virtual size"
    W = "Per guest over the selected range"
    nv1 = {"device": "nvme0n1", "instance": H1, "job": "proxmox-node", "nodename": "ai-node1"}
    nv2 = {**nv1, "instance": H2, "nodename": "ai-node2"}
    one = lambda e: f"({e}) > bool 0"  # noqa: E731  presence and sign, where the exact float is not the point
    return [
        # dm-9 changes owner at 32m. ci-runner-1: 0 -> 32 MiB (+ a quarter-minute of extrapolation past its
        # last sample); ci-runner-4: 2 -> 56 MiB from 33m (+ a quarter-minute before its first). In MiB,
        # rounded to 0.25 (increase() extrapolates). Joining after increase() would give ci-runner-4 most of it.
        ("dm owner change mid-window, written MiB", f"round(({panel_expr(W, 'A')}) / {MiB}, 0.25)", "60m",
         [sample(CI1, 32.25), sample(CI4, 54.5),
          sample({"name": "ai-node1 root", "nodename": "ai-node1", "type": "host"}, 30)]),
        # Every volume counts in the pool's figures, unused or not.
        ("virtual size", panel_expr(T, "A"), "60m",
         [sample(CI1, 250 * GiB), sample(DW1, 188 * GiB), sample(REG, 16 * GiB), sample(LLM2, 192 * GiB),
          sample(CP2, 76 * GiB), sample(DW2, 60 * GiB), sample(MOVED, 80 * GiB), sample(MIXED, 16 * GiB)]),
        ("allocated", panel_expr(T, "B"), "60m",
         [sample(CI1, 100 * GiB), sample(DW1, 130 * GiB), sample(REG, 5 * GiB), sample(LLM2, 137 * GiB),
          sample(CP2, 50 * GiB), sample(DW2, 40 * GiB), sample(MOVED, 70 * GiB), sample(MIXED, 10 * GiB)]),
        ("allocated %", panel_expr(T, "C"), "60m",
         [sample(CI1, 40), sample(DW1, 100 * 130 / 188), sample(REG, 31.25), sample(LLM2, 100 * 137 / 192),
          sample(CP2, 100 * 50 / 76), sample(DW2, 100 * 40 / 60), sample(MOVED, 87.5), sample(MIXED, 62.5)]),
        # Local volumes only, with evidence: no dev-worker-2 (one disk elsewhere), no moved-vm (its local
        # volume is the closed source of a move), no mixed-vm (an unused local disk beside a remote one), no
        # registry (no node_exporter); talos-cp2 without its iSCSI PVC.
        ("filesystem used", panel_expr(T, "D"), "60m",
         [sample(CI1, 60.5 * GiB), sample(DW1, 90 * GiB), sample(LLM2, 108 * GiB), sample(CP2, 40 * GiB)]),
        ("untrimmed gap", panel_expr(T, "E"), "60m",
         [sample(CI1, 39.5 * GiB), sample(DW1, 40 * GiB), sample(LLM2, 22 * GiB), sample(CP2, 10 * GiB)]),
        ("untrimmed gap bars", panel_expr("Untrimmed gap per guest on"), "60m",
         [sample(CI1, 39.5 * GiB), sample(DW1, 40 * GiB), sample(LLM2, 22 * GiB), sample(CP2, 10 * GiB)]),
        ("share of pool", panel_expr(T, "F"), "60m",
         [sample(CI1, 100 * 100 / 1755), sample(DW1, 130 * 100 / 1755), sample(REG, 5 * 100 / 1755),
          sample(LLM2, 137 * 100 / 1755), sample(CP2, 50 * 100 / 1755), sample(DW2, 40 * 100 / 1755),
          sample(MOVED, 70 * 100 / 1755), sample(MIXED, 10 * 100 / 1755)]),
        ("pool metadata %", panel_expr("Thin pool used % (data", "B"), "60m",
         [sample({"instance": H1, "job": "proxmox-node", "pool": "pve/data", "nodename": "ai-node1"}, 2.5),
          sample({"instance": H2, "job": "proxmox-node", "pool": "pve/data", "nodename": "ai-node2"}, 2.5)]),
        ("trim ages", panel_expr("Trim jobs"), "60m",
         [sample({"name": "ci-runner-1", "trim": "runner nightly (last run)", "nodename": "ai-node1"}, 3000),
          sample({"name": "dev-worker-1", "trim": "dev-worker daily", "nodename": "ai-node1"}, 2400),
          sample({"name": "ai-llm-2", "trim": "LXC nightly sweep", "nodename": "ai-node2"}, 2800),
          sample({"name": "talos-cp2", "trim": "Talos node-fstrim", "nodename": "ai-node2"}, 3500)]),
        # 24 h of 1000 units/min, exactly, from the raw difference.
        ("written 24 h, steady drive", f'{panel_expr("Written · last 24 h")} and on(instance) '
         f'nvme_data_units_written_total{{instance="{H1}"}}', "48h", [sample(nv1, 1440 * 1000 * 512000)]),
        ("written per day 7 days", one(panel_expr("Written per day · 7-day")), "48h",
         [sample(nv1, 1), sample(nv2, 1)]),
        # The replaced drive reads 0% used: no projection for it; the steady one has one.
        ("days to 100%", one(panel_expr("Days to 100%")), "48h", [sample(nv1, 1)]),
    ]


class DashboardPromQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise AssertionError("no docker: these tests run promtool in the image rules-lint.sh pins")
        # As rules-lint.sh: only the PULL is retried (a registry hiccup is not a verdict); the test is not.
        image = _image()
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
            for _ in range(3):
                if subprocess.run(["docker", "pull", "-q", image], capture_output=True, timeout=600).returncode == 0:
                    break
            else:
                raise AssertionError(f"could not pull {image} after 3 attempts")

    def test_queries_answer_as_expected(self):
        tests = []
        for name, expr, at, exp in expr_tests():
            tests.append({"expr": expr, "eval_time": at, "exp_samples": exp})
        # The replaced drive's 24 h figure is an extrapolated increase(): checked for being there and positive.
        written_nv2 = ("((" + panel_expr("Written · last 24 h") + ') > bool 0) and on(instance) '
                       f'nvme_data_units_written_total{{instance="{H2}"}}')
        tests.append({"expr": written_nv2, "eval_time": "48h", "exp_samples": [
            sample({"device": "nvme0n1", "instance": H2, "job": "proxmox-node", "nodename": "ai-node2"}, 1)]})
        doc = {"rule_files": [], "evaluation_interval": "1m",
               "tests": [{"interval": "1m", "input_series": estate(), "promql_expr_test": tests}]}
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "dash.test.yaml").write_text(yaml.safe_dump(doc, sort_keys=False, width=100000),
                                                    encoding="utf-8")
            # As rules-lint.sh: the temp dir is 0700 and the image runs promtool as `nobody`.
            os.chmod(d, 0o755)
            os.chmod(Path(d) / "dash.test.yaml", 0o644)
            proc = subprocess.run(
                ["docker", "run", "--rm", "--network", "none", "-v", f"{d}:/t:ro", "--entrypoint", "promtool",
                 _image(), "test", "rules", "/t/dash.test.yaml"],
                capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, (proc.stdout + proc.stderr)[-6000:])
        self.assertIn("SUCCESS", proc.stdout)


if __name__ == "__main__":
    unittest.main()
