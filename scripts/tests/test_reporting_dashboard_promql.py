"""What the AI Lab Fleet dashboard's two drive views RETURN, evaluated by promtool on synthetic series.

test_reporting_dashboard.py checks the panels' shape; this checks their answers. The queries are read from the
committed ConfigMap (which that test pins to the generator), $host expanded as Grafana expands the hidden
variable's All, and run as
`promql_expr_test`s by `promtool test rules` in the pinned Prometheus image scripts/rules-lint.sh uses
(Docker, no network), through the Local Drives PromQL test's harness. Pinned here:

  * disk latency: (Δ read time + Δ write time) / (Δ reads + Δ writes) in ms, one series per host labelled by
    host name; a host whose drive did nothing in the window has no series (not 0/0); a second drive on the host
    does not mix in;
  * disk usage, one panel for the three hosts: each host's pool used bytes (the pool's own figure), labelled by
    host; ONE capacity series, the max of the hosts' pool sizes; the consumer rows, per host, every guest's disk
    volumes (not its cloud-init drive) summed into its consumer group (each dev worker by name, ci-runners,
    registry, talos-cp, talos-workers for the agent nodes and relay workers, llm, other for the rest and for a
    vmid pve-exporter does not know), each on the host its volumes are on; a cloudlab guest that reuses a vmid
    does not rename an ailab one;
  * disk throughput: each host's bytes/s read (positive) and written (negative, as the network panel draws
    transmit) summed over its whole NVMe drives, a second drive included, its partitions and device-mapper layers
    left out; named by host;
  * pressure stall: 100 x the 5-minute rate of each host's CPU, memory and I/O "some" stall counters, named by
    host.

Needs docker (fails closed without it, like rules-lint.sh).

    python3 -m unittest discover -s scripts/tests -p "test_reporting_dashboard_promql.py" -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

# The harness lives beside this file; on the path also when run as scripts.tests.<module> from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_local_drives_dashboard_promql import Series, _image, sample  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DASH = json.loads(json.loads((ROOT / "kubernetes/apps/infrastructure/monitoring/reporting-dashboard.yaml")
                             .read_text(encoding="utf-8"))["data"]["reporting.json"])

GiB = 1073741824
H1, H2, H3 = "192.168.0.2:9100", "192.168.0.3:9100", "192.168.0.4:9100"
MiB = 1048576
ALL = "(ai-node1|ai-node2|ai-node3)"    # what Grafana puts for the hidden $host variable's All
# Each host's pool used % and size; ai-node3's pool is one GiB bigger, so the capacity line must be the max.
USED_PCT = {"ai-node1": 20, "ai-node2": 30, "ai-node3": 40}
POOL_GIB = {"ai-node1": 1755, "ai-node2": 1755, "ai-node3": 1756}


def expr(title_prefix, ref="A", host=None):
    p = next(p for p in DASH["panels"] if p["title"].startswith(title_prefix))
    e = next(t["expr"] for t in p["targets"] if t["refId"] == ref)
    return e.replace("$host", host) if host else e


def estate():
    """Three hosts, 70 one-minute samples (0-69m)."""
    s = Series()
    for inst, node in ((H1, "ai-node1"), (H2, "ai-node2"), (H3, "ai-node3")):
        s.add("node_uname_info", {"job": "proxmox-node", "instance": inst, "nodename": node}, "1x70")
        pool = {"job": "proxmox-node", "instance": inst, "pool": "pve/data"}
        s.add("pve_thin_pool_size_bytes", pool, f"{POOL_GIB[node] * GiB}x70")
        s.add("pve_thin_pool_data_percent", pool, f"{USED_PCT[node]}x70")

    # Drive counters, per minute. ai-node1: 600 reads at 1 ms and 1200 writes at 10 ms -> 12.6 s / 1800 = 7 ms;
    # its second drive (nvme1n1) is slow and must not count. ai-node2: idle. ai-node3: reads only, 0.5 ms each.
    def drive(inst, dev, reads, read_s, writes, write_s):
        L = {"job": "proxmox-node", "instance": inst, "device": dev}
        s.add("node_disk_reads_completed_total", L, f"0+{reads}x69")
        s.add("node_disk_read_time_seconds_total", L, f"0+{read_s}x69")
        s.add("node_disk_writes_completed_total", L, f"0+{writes}x69")
        s.add("node_disk_write_time_seconds_total", L, f"0+{write_s}x69")
    drive(H1, "nvme0n1", 600, 0.6, 1200, 12)
    drive(H1, "nvme1n1", 10, 5, 10, 5)
    drive(H2, "nvme0n1", 0, 0, 0, 0)
    drive(H3, "nvme0n1", 600, 0.3, 0, 0)

    # Bytes per minute. ai-node1: nvme0n1 reads 1 MiB/s and writes 2 MiB/s, a second drive (nvme1n1) reads 64 KiB/s;
    # its partition and a device-mapper volume carry the same bytes again and must not count. ai-node2 writes
    # 0.5 MiB/s and reads nothing. ai-node3 has no disk byte counters.
    def disk_bytes(inst, dev, read_per_s, written_per_s):
        L = {"job": "proxmox-node", "instance": inst, "device": dev}
        s.add("node_disk_read_bytes_total", L, f"0+{read_per_s * 60}x69")
        s.add("node_disk_written_bytes_total", L, f"0+{written_per_s * 60}x69")
    disk_bytes(H1, "nvme0n1", MiB, 2 * MiB)
    disk_bytes(H1, "nvme1n1", 65536, 0)
    disk_bytes(H1, "nvme0n1p3", 10 * MiB, 10 * MiB)
    disk_bytes(H1, "dm-3", 10 * MiB, 10 * MiB)
    disk_bytes(H2, "nvme0n1", 0, MiB // 2)

    # Pressure stall "some", seconds stalled per minute: 0.6 = 1%, 1.2 = 2%, 3 = 5%, 6 = 10%, 0.06 = 0.1%.
    def psi(inst, cpu, memory, io):
        L = {"job": "proxmox-node", "instance": inst}
        s.add("node_pressure_cpu_waiting_seconds_total", L, f"0+{cpu}x69")
        s.add("node_pressure_memory_waiting_seconds_total", L, f"0+{memory}x69")
        s.add("node_pressure_io_waiting_seconds_total", L, f"0+{io}x69")
    psi(H1, 0.6, 1.2, 3)
    psi(H2, 0, 0.06, 0.6)
    psi(H3, 6, 0, 0)

    guests = (("qemu/4101", "ci-runner-1", "ai-node1"), ("qemu/4104", "ci-runner-4", "ai-node1"),
              ("qemu/4201", "dev-worker-1", "ai-node1"), ("qemu/4204", "dev-worker-3", "ai-node1"),
              ("lxc/5004", "registry", "ai-node1"), ("qemu/4301", "talos-agent-node-1", "ai-node1"),
              ("qemu/4102", "ci-runner-2", "ai-node2"),
              ("qemu/4003", "talos-cp3", "ai-node3"), ("qemu/4303", "talos-agent-node-3", "ai-node3"),
              ("qemu/4304", "relay-worker-1", "ai-node3"), ("qemu/4501", "reviewer-1", "ai-node3"),
              ("lxc/5003", "ai-llm-3", "ai-node3"))
    for gid, name, node in guests:
        s.add("pve_guest_info", {"job": "pve-exporter", "id": gid, "name": name, "node": node,
                                 "type": gid.split("/")[0]}, "1x70")
    # cloudlab's exporter reuses vmid 4101
    s.add("pve_guest_info", {"job": "cloud-pve", "id": "qemu/4101", "name": "cloud-ci-1", "node": "cloud1",
                             "type": "qemu"}, "1x70")

    def lv(inst, name, vmid, alloc):
        s.add("pve_thin_lv_allocated_bytes", {"job": "proxmox-node", "instance": inst, "pool": "pve/data", "lv": name,
                                              "vmid": vmid}, f"{alloc}x70")
    lv(H1, "vm-4101-disk-0", "4101", 100 * GiB)
    lv(H1, "vm-4101-cloudinit", "4101", 393216)                 # not a disk: left out
    lv(H1, "vm-4104-disk-0", "4104", 50 * GiB)
    lv(H1, "vm-4201-disk-0", "4201", 30 * GiB)
    lv(H1, "vm-4201-disk-1", "4201", 100 * GiB)
    lv(H1, "vm-4204-disk-1", "4204", 20 * GiB)
    lv(H1, "vm-5004-disk-0", "5004", 5 * GiB)
    lv(H1, "vm-4301-disk-0", "4301", 40 * GiB)
    lv(H2, "vm-4102-disk-0", "4102", 77 * GiB)                  # ai-node2's: its own row, not ai-node1's
    lv(H3, "vm-4003-disk-0", "4003", 60 * GiB)
    lv(H3, "vm-4303-disk-0", "4303", 40 * GiB)
    lv(H3, "vm-4304-disk-0", "4304", 30 * GiB)
    lv(H3, "vm-4501-disk-0", "4501", 6 * GiB)
    lv(H3, "vm-4999-disk-0", "4999", 2 * GiB)                   # a vmid pve-exporter does not know
    lv(H3, "vm-5003-disk-0", "5003", 9 * GiB)
    return s.items


def expr_tests():
    lat = "Disk Latency per Hypervisor"
    use = "Disk Usage by Consumer"
    tput = "Disk Throughput per Hypervisor"
    psi = "Pressure Stall per Hypervisor"
    h1, h2, h3 = ({"instance": i, "nodename": n} for i, n in ((H1, "ai-node1"), (H2, "ai-node2"), (H3, "ai-node3")))
    n1, n2, n3 = {"nodename": "ai-node1"}, {"nodename": "ai-node2"}, {"nodename": "ai-node3"}
    return [
        ("latency, ms", f"round({expr(lat)}, 0.001)", "60m",
         [sample({"instance": H1, "nodename": "ai-node1"}, 7), sample({"instance": H3, "nodename": "ai-node3"}, 0.5)]),
        ("pool used per host", expr(use, "A", ALL), "60m",
         [sample({"nodename": n}, USED_PCT[n] / 100 * (POOL_GIB[n] * GiB)) for n in USED_PCT]),
        ("one capacity line, the max over the hosts", expr(use, "B", ALL), "60m", [sample({}, 1756 * GiB)]),
        ("disk read, bytes/s", f"round({expr(tput, 'A')})", "60m",
         [sample(h1, MiB + 65536), sample(h2, 0)]),
        ("disk write, bytes/s, negative", f"round({expr(tput, 'B')})", "60m",
         [sample(h1, -2 * MiB), sample(h2, -(MiB // 2))]),
        ("pressure CPU, percent", f"round({expr(psi, 'A')}, 0.001)", "60m",
         [sample(h1, 1), sample(h2, 0), sample(h3, 10)]),
        ("pressure memory, percent", f"round({expr(psi, 'B')}, 0.001)", "60m",
         [sample(h1, 2), sample(h2, 0.1), sample(h3, 0)]),
        ("pressure I/O, percent", f"round({expr(psi, 'C')}, 0.001)", "60m",
         [sample(h1, 5), sample(h2, 1), sample(h3, 0)]),
        ("consumer rows", expr(use, "C", ALL), "60m",
         [sample({**n1, "consumer": "ci-runners"}, 150 * GiB), sample({**n1, "consumer": "dev-worker-1"}, 130 * GiB),
          sample({**n1, "consumer": "dev-worker-3"}, 20 * GiB), sample({**n1, "consumer": "registry"}, 5 * GiB),
          sample({**n1, "consumer": "talos-workers"}, 40 * GiB),
          sample({**n2, "consumer": "ci-runners"}, 77 * GiB),
          sample({**n3, "consumer": "talos-cp"}, 60 * GiB), sample({**n3, "consumer": "talos-workers"}, 70 * GiB),
          sample({**n3, "consumer": "other"}, 8 * GiB), sample({**n3, "consumer": "llm"}, 9 * GiB)]),
    ]


class FleetDrivePromQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise AssertionError("no docker: these tests run promtool in the image rules-lint.sh pins")
        image = _image()
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
            for _ in range(3):
                if subprocess.run(["docker", "pull", "-q", image], capture_output=True, timeout=600).returncode == 0:
                    break
            else:
                raise AssertionError(f"could not pull {image} after 3 attempts")

    def test_queries_answer_as_expected(self):
        tests = [{"expr": e, "eval_time": at, "exp_samples": exp} for _, e, at, exp in expr_tests()]
        doc = {"rule_files": [], "evaluation_interval": "1m",
               "tests": [{"interval": "1m", "input_series": estate(), "promql_expr_test": tests}]}
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "fleet.test.yaml").write_text(yaml.safe_dump(doc, sort_keys=False, width=100000),
                                                     encoding="utf-8")
            # As rules-lint.sh: the temp dir is 0700 and the image runs promtool as `nobody`.
            os.chmod(d, 0o755)
            os.chmod(Path(d) / "fleet.test.yaml", 0o644)
            proc = subprocess.run(
                ["docker", "run", "--rm", "--network", "none", "-v", f"{d}:/t:ro", "--entrypoint", "promtool",
                 _image(), "test", "rules", "/t/fleet.test.yaml"],
                capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, (proc.stdout + proc.stderr)[-6000:])
        self.assertIn("SUCCESS", proc.stdout)


if __name__ == "__main__":
    unittest.main()
