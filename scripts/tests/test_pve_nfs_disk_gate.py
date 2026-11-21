"""pve_base nfs_disk_gate: pve-guests waits, bounded, for the NFS storage that holds an onboot VM's disk.

Since 2026-11-20 the Talos control-plane disks are on qnap-nfs (ADR 0043). pve-guests starts the onboot guests
once per boot and never retries, so a CP whose storage was not ready stayed down. The gate is an ExecStartPre of
pve-guests. These tests run the role's real script (ansible/roles/pve_base/files/pve-nfs-disk-gate.sh) against a
fake /etc/pve (qemu-server confs and storage.cfg), a fake /proc/self/mountinfo and a stub `pvesm`. Pinned here:

  * only onboot, non-template VMs of this node count, with their live drives (ide/sata/scsi/virtio/efidisk/
    tpmstate; not unusedN, not snapshot sections, not `none` or passthrough paths), in both spellings
    (`<volid>,...` and `file=<volid>`); a node with nothing on the gated storages passes at once;
  * "mounted" means a gated filesystem type mounted EXACTLY at the storage's path, the topmost mount winning; while
    it is not, the gate asks PVE to activate the storage (`pvesm status --storage`), and stops asking once that hung;
  * "ready" also needs a write of this node's probe file and a read of every such disk image (a linked clone's
    included); an empty export or a failed write is not ready;
  * the budget bounds the whole run, across storages; a hung probe is abandoned, not waited for, and killed with
    its whole process group: no descendant (a read's dd, pvesm's child) outlives the gate, and no job notice;
  * a probe the budget cut short (granted less than its own limit) and that did not finish proves nothing: it never
    replaces a real outcome. An empty export stays "failed" even when the last check's read is cut off, a probe that
    overran its whole limit stays "timed out", and a hang that only the budget ever cut reads "timed out: the wait
    budget ran out first" (the race that flaked test_an_empty_export_is_not_ready under CPU load);
  * on a timeout it exits 1 with a WARNING (priority warning under systemd) and hands over; ready = exit 0;
  * the drop-in runs the gate as a "-" ExecStartPre with the role's settings, and the role wiring.

    python3 -m unittest discover -s scripts/tests -p "test_pve_nfs_disk_gate.py" -v
"""
import errno
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "ansible/roles/pve_base"
SCRIPT = ROLE / "files/pve-nfs-disk-gate.sh"
BASH = shutil.which("bash")

# `pvesm status --storage <id>`: log the call; if $NAS_UP exists, "mount" the storage (append its mountinfo line,
# as PVE's activate_storage would mount it); if $PVESM_HANG exists, hang like a mount on a dead server. The hang is
# a CHILD of the stub named $HANG_MARK (as pvesm's mount.nfs is a child of pvesm), so a test can see whether it
# outlives the gate.
PVESM_STUB = """#!/usr/bin/env bash
echo "$*" >> "$PVESM_CALLS"
[ -f "$PVESM_HANG" ] && (exec -a "$HANG_MARK" sleep 30)
[ -f "$NAS_UP" ] && cat "$NAS_UP" >> "$MOUNTINFO_FILE"
exit 0
"""

STORAGE_CFG = """\
dir: local
\tpath /var/lib/vz
\tcontent import

lvmthin: local-lvm
\tthinpool data
\tvgname pve
\tcontent images,rootdir

nfs: qnap-nfs
\texport /pve-nfs
\tpath {qnap}
\tserver 10.55.0.254
\tcontent vztmpl,import,iso,images,backup
\tnodes ai-node1,ai-node2,ai-node3
\toptions vers=4.0

nfs: other-nfs
\texport /other
\tpath {other}
\tserver 10.55.0.254
\tcontent images
"""

CP = ["boot: order=scsi0;net0", "ide2: local-lvm:vm-4001-cloudinit,media=cdrom", "name: talos-cp1", "onboot: 1",
      "scsi0: qnap-nfs:4001/vm-4001-disk-0.raw,aio=io_uring,backup=1,cache=none,discard=on,iothread=1,size=80G"]


@unittest.skipUnless(BASH, "bash is required")
class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conf = self.tmp / "qemu-server"
        self.conf.mkdir()
        self.qnap = self.tmp / "mnt" / "pve" / "qnap-nfs"
        self.qnap.mkdir(parents=True)
        self.other = self.tmp / "mnt" / "pve" / "other-nfs"
        self.other.mkdir(parents=True)
        self.cfg = self.tmp / "storage.cfg"
        self.cfg.write_text(STORAGE_CFG.format(qnap=self.qnap.as_posix(), other=self.other.as_posix()),
                            encoding="utf-8", newline="\n")
        self.mountinfo = self.tmp / "mountinfo"
        self.mountinfo.write_text("22 1 259:3 / / rw,relatime shared:1 - ext4 /dev/mapper/pve-root rw\n",
                                  encoding="utf-8", newline="\n")
        self.pvesm = self.tmp / "pvesm"
        self.pvesm.write_text(PVESM_STUB, encoding="utf-8", newline="\n")
        self.pvesm.chmod(0o755)
        self.hang_mark = f"gate-test-hang-{self.tmp.name}"
        self.env = {}
        self.addCleanup(self.kill_leftovers)

    # ---- processes a probe left behind (Linux /proc) ----
    def procs_with(self, marker):
        found = []
        for d in Path("/proc").iterdir():
            if not d.name.isdigit() or int(d.name) == os.getpid():
                continue
            try:
                cmd = (d / "cmdline").read_bytes()  # empty for a zombie: dead, only not yet reaped
            except OSError:
                continue
            if marker.encode() in cmd:
                found.append(int(d.name))
        return found

    def survivors(self, marker, settle=2.0):
        """Live processes whose command line holds `marker`, after `settle` s for a SIGKILL to land."""
        if not Path("/proc/self/cmdline").exists():
            self.skipTest("needs /proc")
        deadline = time.monotonic() + settle
        while True:
            found = self.procs_with(marker)
            if not found or time.monotonic() > deadline:
                return found
            time.sleep(0.1)

    def kill_leftovers(self):
        if Path("/proc/self/cmdline").exists():
            for pid in self.procs_with(self.tmp.name):
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass

    # ---- fixtures ----
    def vm(self, vmid, *lines):
        (self.conf / f"{vmid}.conf").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    def image(self, rel, root=None):
        p = (root or self.qnap) / "images" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0" * 8192)
        return p

    def mount_line(self, path, fstype="nfs4", src="10.55.0.254:/pve-nfs"):
        return f"40 22 0:52 / {Path(path).as_posix()} rw,relatime shared:20 - {fstype} {src} rw,vers=4.0\n"

    def mount(self, path=None, fstype="nfs4"):
        with open(self.mountinfo, "a", encoding="utf-8", newline="\n") as f:
            f.write(self.mount_line(path or self.qnap, fstype))

    def calls(self):
        f = self.tmp / "pvesm-calls"
        return f.read_text(encoding="utf-8").splitlines() if f.exists() else []

    def run_gate(self, wait_s=3, **extra):
        env = dict(os.environ, NODE="ai-node9", CONF_DIR=self.conf.as_posix(), STORAGE_CFG=self.cfg.as_posix(),
                   MOUNTINFO=self.mountinfo.as_posix(), MOUNTINFO_FILE=self.mountinfo.as_posix(),
                   PVESM=self.pvesm.as_posix(), PVESM_CALLS=(self.tmp / "pvesm-calls").as_posix(),
                   PVESM_HANG=(self.tmp / "pvesm-hang").as_posix(), NAS_UP=(self.tmp / "nas-up").as_posix(),
                   HANG_MARK=self.hang_mark, WAIT_S=str(wait_s), STEP_S="1", PROBE_S="2", ACTIVATE_S="2")
        env.pop("JOURNAL_STREAM", None)
        env.update(self.env)
        env.update({k: str(v) for k, v in extra.items()})
        t0 = time.monotonic()
        r = subprocess.run([BASH, SCRIPT.as_posix()], env=env, capture_output=True, text=True, timeout=90)
        self.elapsed = time.monotonic() - t0
        self.out = r.stdout + r.stderr
        return r.returncode

    def later(self, seconds, fn):
        t = threading.Timer(seconds, fn)
        t.start()
        self.addCleanup(t.cancel)

    def mountinfo_fifo(self, content, before_serve):
        """Serve the gate's mountinfo from a FIFO, one read per gate check (each check opens it exactly once, as
        the storage is mounted). before_serve(n) runs before the n-th check gets its copy, i.e. strictly after
        check n-1 finished. Returns (the FIFO's path, the list of checks served so far)."""
        fifo = self.tmp / "mountinfo.fifo"
        os.mkfifo(fifo)
        stop = threading.Event()
        served = []

        def writer():
            try:
                return os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as e:
                if e.errno == errno.ENXIO:  # no reader has it open
                    return None
                raise

        def run():
            while not stop.is_set():
                fd = writer()
                if fd is None:
                    time.sleep(0.005)
                    continue
                before_serve(len(served) + 1)
                os.set_blocking(fd, True)
                try:
                    os.write(fd, content.encode())
                except BrokenPipeError:
                    pass
                os.close(fd)
                served.append(len(served) + 1)
                # Wait until that reader has closed, so it cannot be counted twice. The next check opens the file
                # at least STEP_S later.
                until = time.monotonic() + 5
                while not stop.is_set() and time.monotonic() < until:
                    fd = writer()
                    if fd is None:
                        break
                    os.close(fd)
                    time.sleep(0.005)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.addCleanup(t.join, 2)
        self.addCleanup(stop.set)
        return fifo, served

    # ---- ready / nothing to do ----
    def test_a_ready_storage_passes_at_once(self):
        self.vm(4001, *CP)
        self.image("4001/vm-4001-disk-0.raw")
        self.mount()
        self.assertEqual(0, self.run_gate(), self.out)
        self.assertRegex(self.out, r"qnap-nfs ready after [01]s: mounted, writable, serving the disks of VM 4001$")
        self.assertLess(self.elapsed, 5)
        probe = self.qnap / ".pve-nfs-disk-gate" / "ai-node9"
        self.assertTrue(probe.read_text(encoding="utf-8").startswith("ai-node9 "), "the node's probe file")
        self.assertEqual([], self.calls(), "a mounted storage is not activated again")

    def test_nothing_on_the_gated_storage_passes_at_once(self):
        self.vm(4101, "name: ci-runner-1", "onboot: 1", "scsi0: local-lvm:vm-4101-disk-0,size=250G",
                "ide2: local-lvm:vm-4101-cloudinit,media=cdrom")
        self.vm(4002, "onboot: 0", "scsi0: qnap-nfs:4002/vm-4002-disk-0.raw,size=80G")
        self.vm(4003, "scsi0: qnap-nfs:4003/vm-4003-disk-0.raw,size=80G")  # onboot unset = off
        self.vm(9000, "onboot: 1", "template: 1", "scsi0: qnap-nfs:9000/base-9000-disk-0.raw,size=8G")
        self.vm(4004, "onboot: 1", "scsi0: local-lvm:vm-4004-disk-0,size=80G", "unused0: qnap-nfs:4004/vm-4004-disk-0.raw",
                "", "[pre-move]", "scsi0: qnap-nfs:4004/vm-4004-disk-0.raw,size=80G")
        self.assertEqual(0, self.run_gate(), self.out)
        self.assertIn("no onboot VM on ai-node9 has a disk on: qnap-nfs; nothing to wait for", self.out)
        self.assertLess(self.elapsed, 5)
        self.assertFalse((self.qnap / ".pve-nfs-disk-gate").exists(), "nothing probed")
        self.assertEqual([], self.calls())

    # ---- waiting ----
    def test_waits_for_a_late_mount_and_asks_pve_to_activate_meanwhile(self):
        self.vm(4001, *CP)
        self.image("4001/vm-4001-disk-0.raw")
        self.later(1.5, self.mount)
        self.assertEqual(0, self.run_gate(wait_s=15), self.out)
        self.assertRegex(self.out, r"qnap-nfs ready after [1-9][0-9]*s")
        self.assertGreater(self.elapsed, 1.4)
        self.assertIn("status --storage qnap-nfs", self.calls())

    def test_pve_activation_mounts_it(self):
        self.vm(4001, *CP)
        self.image("4001/vm-4001-disk-0.raw")
        (self.tmp / "nas-up").write_text(self.mount_line(self.qnap), encoding="utf-8", newline="\n")
        self.assertEqual(0, self.run_gate(), self.out)
        self.assertEqual(["status --storage qnap-nfs"], self.calls())
        self.assertRegex(self.out, r"qnap-nfs ready after [01]s")

    def test_times_out_hands_over_and_says_how_to_recover(self):
        self.vm(4001, *CP)
        self.image("4001/vm-4001-disk-0.raw")
        self.assertEqual(1, self.run_gate(wait_s=2), self.out)
        self.assertLess(self.elapsed, 10)
        line = [l for l in self.out.splitlines() if "WARNING" in l]
        self.assertEqual(1, len(line), self.out)
        self.assertTrue(line[0].startswith("pve-nfs-disk-gate: WARNING: qnap-nfs not ready after "), line[0])
        self.assertIn(f"not mounted at {self.qnap.as_posix()}. Handing over to pve-guests anyway", line[0])
        self.assertIn("start VM 4001 while qnap-nfs is offline", line[0])
        self.assertIn("'qm start <vmid>'", line[0])
        self.assertGreaterEqual(len(self.calls()), 2, "activation is retried while it returns")

    def test_under_systemd_the_warning_has_priority_warning(self):
        self.vm(4001, *CP)
        self.assertEqual(1, self.run_gate(wait_s=1, JOURNAL_STREAM="8:12345"), self.out)
        self.assertRegex(self.out, r"(?m)^<4>pve-nfs-disk-gate: WARNING: qnap-nfs not ready")
        self.assertRegex(self.out, r"(?m)^pve-nfs-disk-gate: waiting up to 1s for qnap-nfs", "info lines unprefixed")

    def test_only_a_gated_filesystem_exactly_at_the_path_counts(self):
        self.vm(4001, *CP)
        self.image("4001/vm-4001-disk-0.raw")
        self.mount(self.qnap.parent / "qnap-nfs-old")       # beside it
        self.mount(self.qnap.as_posix() + "/images")         # below it
        self.assertEqual(1, self.run_gate(wait_s=1), self.out)
        self.assertIn(f"not mounted at {self.qnap.as_posix()}.", self.out)
        self.mount()
        self.mount(fstype="tmpfs")                           # something mounted over the share
        self.assertEqual(1, self.run_gate(wait_s=1), self.out)
        self.assertIn("(the mount there is tmpfs)", self.out)

    def test_an_empty_export_is_not_ready(self):
        self.vm(4001, *CP)
        self.mount()
        self.assertEqual(1, self.run_gate(wait_s=2), self.out)
        self.assertIn(f"reading {self.qnap.as_posix()}/images/4001/vm-4001-disk-0.raw failed", self.out)

    def test_a_failed_write_is_not_ready(self):
        self.vm(4001, *CP)
        self.image("4001/vm-4001-disk-0.raw")
        self.mount()
        (self.qnap / ".pve-nfs-disk-gate").write_text("a file, so mkdir -p fails (root-proof)", encoding="utf-8")
        self.assertEqual(1, self.run_gate(wait_s=2), self.out)
        self.assertIn(f"the write probe under {self.qnap.as_posix()}/.pve-nfs-disk-gate failed", self.out)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs a FIFO")
    def test_a_hung_read_is_abandoned_with_its_dd_and_the_budget_holds(self):
        # Opening a FIFO with no writer blocks (interruptibly), like I/O on a hard NFS mount whose server stopped
        # answering. Each retry starts a dd; every one must die with its probe's process group, not be orphaned.
        self.vm(4001, *CP)
        (self.qnap / "images" / "4001").mkdir(parents=True)
        fifo = self.qnap / "images" / "4001" / "vm-4001-disk-0.raw"
        os.mkfifo(fifo)
        self.mount()
        self.assertEqual(1, self.run_gate(wait_s=3, PROBE_S=1), self.out)
        self.assertLess(self.elapsed, 10)
        # The first check's read got its whole PROBE_S and overran it: a real "timed out", with no budget note.
        self.assertIn("vm-4001-disk-0.raw timed out. Handing over", self.out)
        self.assertEqual([], self.survivors(fifo.as_posix()), "a timed-out probe's dd outlived the gate")
        self.assertNotIn("Killed", self.out, "no job notice from the shell")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs a FIFO")
    def test_a_read_the_budget_cuts_short_does_not_replace_the_real_failure(self):
        # The race behind the flaky empty-export test, made deterministic. Check 1 finds the export empty (a real
        # "failed"). Before check 2, the image becomes a FIFO, so check 2's read hangs. PROBE_S is far above the
        # budget, so only the budget can stop it. That cut-off read proves nothing, and the reason must stay
        # check 1's "failed".
        self.vm(4001, *CP)
        img = self.qnap / "images" / "4001" / "vm-4001-disk-0.raw"
        img.parent.mkdir(parents=True)

        def before_serve(n):
            if n == 2:
                os.mkfifo(img)

        fifo, served = self.mountinfo_fifo(self.mount_line(self.qnap), before_serve)
        self.assertEqual(1, self.run_gate(wait_s=3, PROBE_S=30, MOUNTINFO=fifo.as_posix()), self.out)
        self.assertGreaterEqual(len(served), 2, "a second check, against the hanging read, must have run")
        self.assertIn(f"reading {img.as_posix()} failed. Handing over", self.out)
        self.assertNotIn("timed out", self.out)
        self.assertEqual([], self.survivors(img.as_posix()), "the cut-off dd outlived the gate")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs a FIFO")
    def test_a_hang_only_the_budget_ever_cut_reads_as_timed_out(self):
        # No check runs to its end: every read hangs, and PROBE_S is above the whole budget. It is still a timeout,
        # and the reason says the budget ran out first.
        self.vm(4001, *CP)
        img = self.qnap / "images" / "4001" / "vm-4001-disk-0.raw"
        img.parent.mkdir(parents=True)
        os.mkfifo(img)
        self.mount()
        self.assertEqual(1, self.run_gate(wait_s=1, PROBE_S=30), self.out)
        self.assertLess(self.elapsed, 10)
        self.assertIn(f"reading {img.as_posix()} timed out: the wait budget ran out first. Handing over", self.out)
        self.assertEqual([], self.survivors(img.as_posix()), "the cut-off dd outlived the gate")

    def test_a_hung_activation_is_tried_once_then_left_to_pvestatd(self):
        self.vm(4001, *CP)
        (self.tmp / "pvesm-hang").write_text("", encoding="utf-8")
        self.assertEqual(1, self.run_gate(wait_s=5, ACTIVATE_S=1), self.out)
        self.assertLess(self.elapsed, 12)
        self.assertEqual(["status --storage qnap-nfs"], self.calls())
        self.assertEqual([], self.survivors(self.hang_mark), "pvesm's hung child outlived the gate")

    def test_the_budget_bounds_the_whole_run_across_storages(self):
        self.vm(4001, *CP, "scsi1: other-nfs:4001/vm-4001-disk-1.raw,size=1G")
        self.assertEqual(1, self.run_gate(wait_s=2, GATE_STORAGES="qnap-nfs other-nfs"), self.out)
        self.assertLess(self.elapsed, 7, "one budget, not one per storage")
        self.assertEqual(2, self.out.count("WARNING"), self.out)

    # ---- what counts as a disk ----
    def test_drive_spellings_and_volume_kinds(self):
        self.vm(4001, "onboot: 1",
                "scsi0: file=qnap-nfs:4001/vm-4001-disk-0.raw,iothread=1,size=80G",
                "virtio1: qnap-nfs:4001/vm-4001-disk-1.qcow2,size=1G",
                "efidisk0: qnap-nfs:4001/vm-4001-disk-2.raw,efitype=4m,size=4M",
                "scsi1: qnap-nfs:9000/base-9000-disk-0.raw/4001/vm-4001-disk-3.raw,size=8G",
                "ide2: qnap-nfs:iso/talos.iso,media=cdrom",
                "ide0: none,media=cdrom",
                "sata0: /dev/disk/by-id/nvme-x,size=1T",
                "unused0: qnap-nfs:4001/vm-4001-disk-9.raw",
                "net0: virtio=BC:24:11:00:00:01,bridge=vmbr0")
        self.mount()
        for rel in ("4001/vm-4001-disk-0.raw", "4001/vm-4001-disk-1.qcow2", "4001/vm-4001-disk-2.raw"):
            self.image(rel)
        self.assertEqual(1, self.run_gate(wait_s=2), self.out)
        self.assertIn("images/4001/vm-4001-disk-3.raw failed", self.out, "a linked clone's own image is read")
        self.image("4001/vm-4001-disk-3.raw")
        self.assertEqual(0, self.run_gate(wait_s=2), self.out)
        self.assertIn("serving the disks of VM 4001", self.out)  # unused0's image is not needed

    def test_several_vms_on_one_storage(self):
        self.vm(4001, *CP)
        self.vm(4002, "onboot: 1", "scsi0: qnap-nfs:4002/vm-4002-disk-0.raw,size=80G")
        self.mount()
        self.image("4001/vm-4001-disk-0.raw")
        self.image("4002/vm-4002-disk-0.raw")
        self.assertEqual(0, self.run_gate(), self.out)
        self.assertIn("serving the disks of VM 4001 4002", self.out)

    # ---- settings it cannot use ----
    def test_a_storage_it_cannot_gate_is_reported_without_waiting(self):
        self.vm(4001, *CP, "scsi1: local-lvm:vm-4001-disk-1,size=8G", "scsi2: gone-nfs:4001/vm-4001-disk-2.raw")
        self.image("4001/vm-4001-disk-0.raw")
        self.mount()
        self.assertEqual(1, self.run_gate(wait_s=30, GATE_STORAGES="local-lvm gone-nfs qnap-nfs"), self.out)
        self.assertLess(self.elapsed, 5)
        self.assertIn("local-lvm is a lvmthin storage, not NFS or CIFS: not waiting for it (VM 4001)", self.out)
        self.assertIn(f"gone-nfs is not in {self.cfg.as_posix()}: not waiting for it (VM 4001)", self.out)
        self.assertRegex(self.out, r"qnap-nfs ready after [01]s")

    def test_a_bad_number_is_refused_at_once(self):
        self.vm(4001, *CP)
        self.assertEqual(1, self.run_gate(wait_s="10m"), self.out)
        self.assertIn("WAIT_S=10m is not a number of seconds; not waiting", self.out)
        self.assertLess(self.elapsed, 5)


class RoleWiring(unittest.TestCase):
    def setUp(self):
        self.defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
        self.tasks = yaml.safe_load((ROLE / "tasks/nfs_disk_gate.yml").read_text(encoding="utf-8"))

    def render(self, **v):
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        src = (ROLE / "templates/pve-guests-nfs-disk-gate.conf.j2").read_text(encoding="utf-8")
        return env.from_string(src).render(**v)

    def test_defaults(self):
        self.assertIs(True, self.defaults["pve_nfs_disk_gate_enabled"])
        self.assertEqual(["qnap-nfs"], self.defaults["pve_nfs_disk_gate_storages"])
        self.assertEqual(600, self.defaults["pve_nfs_disk_gate_timeout_s"])

    def test_the_drop_in_runs_the_gate_as_an_ignored_exec_start_pre(self):
        unit = self.render(pve_nfs_disk_gate_storages=["qnap-nfs"], pve_nfs_disk_gate_timeout_s=600)
        lines = [l for l in unit.splitlines() if l and not l.startswith("#")]
        self.assertEqual(["[Service]", 'ExecStartPre=-/usr/bin/env "GATE_STORAGES=qnap-nfs" WAIT_S=600 '
                                       "/usr/local/sbin/pve-nfs-disk-gate"], lines,
                         "one added ExecStartPre; the stock ExecStart and ordering are untouched")
        two = self.render(pve_nfs_disk_gate_storages=["qnap-nfs", "other-nfs"], pve_nfs_disk_gate_timeout_s="900")
        self.assertIn('"GATE_STORAGES=qnap-nfs other-nfs" WAIT_S=900 ', two)

    def test_the_role_check_matches_systemctl_show(self):
        # The line `systemctl show -p ExecStartPre --value pve-guests.service` printed for the drop-in in the
        # Docker converge test (systemd 257, Debian trixie), after the stock pve-startall-delay stand-in.
        shown = ("{ path=/usr/bin/true ; argv[]=/usr/bin/true ; ignore_errors=yes ; start_time=[n/a] ; "
                 "stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }\n"
                 "{ path=/usr/bin/env ; argv[]=/usr/bin/env GATE_STORAGES=qnap-nfs WAIT_S=600 "
                 "/usr/local/sbin/pve-nfs-disk-gate ; ignore_errors=yes ; start_time=[n/a] ; stop_time=[n/a] ; "
                 "pid=0 ; code=(null) ; status=0/0 }")
        check = next(t for t in self.tasks[1]["block"] if t["name"].startswith("Check that systemd runs"))
        pattern = re.search(r"search\('(.*)'\)", check["ansible.builtin.assert"]["that"][0]).group(1)
        self.assertRegex(shown, pattern)
        gate = "/usr/local/sbin/pve-nfs-disk-gate ; ignore_errors="
        self.assertNotRegex(shown.replace(gate + "yes", gate + "no"), pattern, "without the '-' it must fail")
        self.assertNotRegex(shown.split("\n")[0], pattern, "the stock entry alone must fail")

    def test_every_task_is_tagged_and_none_touches_pve_guests_state(self):
        for t in self.tasks:
            self.assertIn("nfs_disk_gate", t.get("tags", []), t["name"])
        text = (ROLE / "tasks/nfs_disk_gate.yml").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?m)^\s*state:\s*(started|restarted|stopped|reloaded)\b")
        self.assertNotRegex(text, r"(?m)^\s*name:\s*pve-guests")
        main = (ROLE / "tasks/main.yml").read_text(encoding="utf-8")
        self.assertIn("ansible.builtin.import_tasks: nfs_disk_gate.yml", main)
        self.assertIn("--tags nfs_disk_gate", (ROOT / "justfile").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
