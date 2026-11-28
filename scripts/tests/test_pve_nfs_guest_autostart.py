"""pve-nfs-guest-autostart: start onboot guests whose NFS storage was late at boot.

Drives the real script against fake /etc/pve lxc and qemu-server directories, a fake storage.cfg, and
a stub `pct`/`qm` that records what it was asked to start. 2026-10-30: after a power cut the QNAP
served NFS minutes after pve-guests ran, both LLM containers failed lxc.hook.pre-start and stayed down
until started by hand. 2026-11-27: the NAS was powered on after the hosts, and the three Talos
control-plane VMs (disks on qnap-nfs) stayed stopped the same way.
"""
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'ansible/roles/pve_nfs_guest_autostart/files/pve-nfs-guest-autostart.sh'
BASH = shutil.which('bash')

PCT_STUB = """#!/usr/bin/env bash
# status <ctid> -> "status: <state>" from $STATE_DIR/<ctid> (default stopped), or exit 2 when
# $STATE_DIR/fail-status-<ctid> exists; after answering, a $STATE_DIR/next-<ctid> becomes the new
# state (a deterministic "changed after the first look"). start <ctid> -> log it, fail when
# fail-<ctid> exists, and fail once when fail-once-<ctid> exists.
case "$1" in
  status) [ -f "$STATE_DIR/fail-status-$2" ] && { echo "unable to read config" >&2; exit 2; }
          echo "status: $(cat "$STATE_DIR/$2" 2>/dev/null || echo stopped)"
          [ -f "$STATE_DIR/next-$2" ] && mv "$STATE_DIR/next-$2" "$STATE_DIR/$2"; exit 0;;
  start) echo "$2" >> "$STATE_DIR/started"
         if [ -f "$STATE_DIR/fail-once-$2" ]; then rm -f "$STATE_DIR/fail-once-$2"; exit 1; fi
         [ ! -f "$STATE_DIR/fail-$2" ];;
esac
"""


@unittest.skipUnless(BASH, 'bash is required')
class PveNfsGuestAutostartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conf = self.tmp / 'lxc'
        self.conf.mkdir()
        self.state = self.tmp / 'state'
        self.state.mkdir()
        # The brackets make a regex-unsafe prefix: it must be compared literally.
        self.prefix = self.tmp / 'mnt[1]' / 'pve'
        self.prefix.mkdir(parents=True)
        self.ha = self.tmp / 'resources.cfg'
        self.vmconf = self.tmp / 'qemu-server'
        self.vmconf.mkdir()
        self.storage = self.tmp / 'storage.cfg'
        self.storage.write_text(
            'dir: local\n\tpath /var/lib/vz\n\tcontent iso\n\n'
            'lvmthin: local-lvm\n\tthinpool data\n\tvgname pve\n\n'
            f'nfs: qnap-nfs\n\texport /pve-nfs\n\tpath {(self.prefix / "qnap-nfs").as_posix()}\n'
            '\tserver 10.55.0.254\n\tcontent images\n', newline='\n')
        pct = self.tmp / 'pct'
        pct.write_text(PCT_STUB, newline='\n')
        pct.chmod(0o755)
        self.pct = pct
        self.env = {}

    def nfs_src(self, name='qnap-nfs/models', create=True):
        src = self.prefix / name
        if create:
            src.mkdir(parents=True, exist_ok=True)
        return src

    def ct(self, ctid, *lines, state=None):
        (self.conf / f'{ctid}.conf').write_text('\n'.join(lines) + '\n', newline='\n')
        if state:
            self.set_state(ctid, state)

    def vm(self, vmid, *lines, state=None):
        (self.vmconf / f'{vmid}.conf').write_text('\n'.join(lines) + '\n', newline='\n')
        if state:
            self.set_state(vmid, state)

    def vm_images(self, vmid, create=True):
        d = self.prefix / 'qnap-nfs' / 'images' / str(vmid)
        if create:
            d.mkdir(parents=True, exist_ok=True)
        return d

    def set_state(self, ctid, state):
        (self.state / str(ctid)).write_text(state)

    def run_script(self, wait_s=2):
        env = dict(os.environ, CONF_DIR=self.conf.as_posix(), PCT=self.pct.as_posix(),
                   VM_CONF_DIR=self.vmconf.as_posix(), QM=self.pct.as_posix(),
                   STORAGE_CFG=self.storage.as_posix(),
                   MOUNT_PREFIX=self.prefix.as_posix() + '/', STATE_DIR=self.state.as_posix(),
                   HA_RESOURCES=self.ha.as_posix(), WAIT_S=str(wait_s), STEP_S='1', PROBE_S='2')
        env.update(self.env)
        return subprocess.run([BASH, SCRIPT.as_posix()], env=env, capture_output=True, text=True,
                              timeout=60)

    def started(self):
        f = self.state / 'started'
        return f.read_text().split() if f.exists() else []

    def later(self, seconds, fn):
        t = threading.Timer(seconds, fn)
        t.start()
        self.addCleanup(t.cancel)

    def test_starts_a_stopped_onboot_container_once_its_nfs_source_exists(self):
        src = self.nfs_src()
        self.ct(5002, 'onboot: 1', f'mp0: {src.as_posix()},mp=/models')
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), ['5002'])

    def test_reads_the_volume_key_spelling(self):
        src = self.nfs_src()
        self.ct(5002, 'onboot: 1', f'mp0: mp=/models,volume={src.as_posix()},backup=0')
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), ['5002'])

    def test_waits_for_a_source_that_appears_late(self):
        src = self.nfs_src(create=False)
        self.ct(5002, 'onboot: 1', f'mp0: {src.as_posix()},mp=/models')
        self.later(1.5, lambda: src.mkdir(parents=True))
        run = self.run_script(wait_s=10)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), ['5002'])

    def test_gives_up_after_the_budget_and_fails_the_unit(self):
        src = self.nfs_src(create=False)
        self.ct(5002, 'onboot: 1', f'mp0: {src.as_posix()},mp=/models')
        start = time.monotonic()
        run = self.run_script(wait_s=2)
        self.assertLess(time.monotonic() - start, 30)
        self.assertEqual(run.returncode, 1)
        self.assertIn('NOT starting it', run.stdout)
        self.assertEqual(self.started(), [])

    def test_a_hung_probe_is_abandoned_and_the_budget_still_holds(self):
        # A probe that never returns (a hard NFS mount still coming up) must not hold the unit.
        hang = self.tmp / 'hang'
        hang.write_text('#!/usr/bin/env bash\nsleep 30\n', newline='\n')
        hang.chmod(0o755)
        self.env['PROBE_CMD'] = hang.as_posix()
        src = self.nfs_src()
        self.ct(5002, 'onboot: 1', f'mp0: {src.as_posix()},mp=/models')
        start = time.monotonic()
        run = self.run_script(wait_s=2)
        self.assertLess(time.monotonic() - start, 20)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(self.started(), [])

    def test_the_budget_bounds_the_whole_run_not_each_candidate(self):
        # Five candidates behind a hung NFS mount: once the shared budget is spent the rest are
        # given up without another (possibly hung) probe each.
        hang = self.tmp / 'hang'
        hang.write_text('#!/usr/bin/env bash\nsleep 30\n', newline='\n')
        hang.chmod(0o755)
        self.env.update(PROBE_CMD=hang.as_posix(), PROBE_S='3')
        src = self.nfs_src()
        for ctid in range(5001, 5006):
            self.ct(ctid, 'onboot: 1', f'mp0: {src.as_posix()},mp=/models')
        start = time.monotonic()
        run = self.run_script(wait_s=1)
        self.assertLess(time.monotonic() - start, 9)   # one probe per candidate would be >= 15 s
        self.assertEqual(run.returncode, 1)
        self.assertEqual(run.stdout.count('NOT starting it'), 5)
        self.assertEqual(self.started(), [])

    def test_leaves_running_containers_alone(self):
        src = self.nfs_src()
        self.ct(5002, 'onboot: 1', f'mp0: {src.as_posix()},mp=/models', state='running')
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), [])

    def test_a_container_stopped_after_the_scan_is_not_a_failed_boot_start(self):
        # 5002 waits for its late source; meanwhile the operator stops 5003, which was running at
        # the scan. 5003 must stay stopped.
        late = self.nfs_src('late', create=False)
        src = self.nfs_src()
        self.ct(5002, 'onboot: 1', f'mp0: {late.as_posix()},mp=/models')
        self.ct(5003, 'onboot: 1', f'mp0: {src.as_posix()},mp=/models', state='running')
        (self.state / 'next-5003').write_text('stopped')   # stopped right after the scan saw it
        self.later(1.5, lambda: late.mkdir(parents=True))
        run = self.run_script(wait_s=10)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), ['5002'])

    def test_rechecks_onboot_before_starting(self):
        src = self.nfs_src(create=False)
        self.ct(5002, 'onboot: 1', f'mp0: {src.as_posix()},mp=/models')

        def change_mind():
            self.ct(5002, 'onboot: 0', f'mp0: {src.as_posix()},mp=/models')
            src.mkdir(parents=True)
        self.later(1.5, change_mind)
        run = self.run_script(wait_s=10)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('no longer onboot', run.stdout)
        self.assertEqual(self.started(), [])

    def test_rechecks_state_before_starting(self):
        # Started by hand during the wait: no second start, no false failure.
        src = self.nfs_src(create=False)
        self.ct(5002, 'onboot: 1', f'mp0: {src.as_posix()},mp=/models')

        def started_by_hand():
            self.set_state(5002, 'running')
            src.mkdir(parents=True)
        self.later(1.5, started_by_hand)
        run = self.run_script(wait_s=10)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), [])

    def test_leaves_ha_managed_containers_to_ha_manager(self):
        src = self.nfs_src()
        self.ha.write_text('ct: 5002\n\tstate stopped\n', newline='\n')
        self.ct(5002, 'onboot: 1', f'mp0: {src.as_posix()},mp=/models')
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), [])

    def test_a_failed_status_fails_the_unit(self):
        src = self.nfs_src()
        self.ct(5002, 'onboot: 1', f'mp0: {src.as_posix()},mp=/models')
        (self.state / 'fail-status-5002').write_text('')
        run = self.run_script()
        self.assertEqual(run.returncode, 1)
        self.assertIn('pct status failed', run.stderr)
        self.assertEqual(self.started(), [])

    def test_ignores_containers_without_onboot(self):
        src = self.nfs_src()
        self.ct(5002, 'onboot: 0', f'mp0: {src.as_posix()},mp=/models')
        self.ct(5003, f'mp0: {src.as_posix()},mp=/models')
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), [])

    def test_ignores_containers_with_no_mnt_pve_mount_point(self):
        # pve-guests already decided these; a volume mount point is not this failure.
        self.ct(5004, 'onboot: 1', 'mp0: local-lvm:vm-5004-disk-1,mp=/data')
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), [])

    def test_snapshot_sections_do_not_count(self):
        src = self.nfs_src()
        self.ct(5002, 'onboot: 1', 'mp0: local-lvm:vm-5002-disk-1,mp=/data', '',
                '[pre-upgrade]', f'mp1: {src.as_posix()},mp=/models')
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), [])

    def test_a_failed_start_fails_the_unit_but_still_tries_the_others(self):
        src = self.nfs_src()
        self.ct(5002, 'onboot: 1', f'mp0: {src.as_posix()},mp=/models')
        self.ct(5003, 'onboot: 1', f'mp0: {src.as_posix()},mp=/models')
        (self.state / 'fail-5002').write_text('')
        run = self.run_script(wait_s=10)
        self.assertEqual(run.returncode, 1)
        # 5002 is tried START_ATTEMPTS (3) times on successive passes; 5003 starts in the first pass.
        self.assertEqual(self.started(), ['5002', '5003', '5002', '5002'])
        self.assertIn('CT 5002: start failed 3 time(s); NOT starting it', run.stdout)

    def test_a_transiently_refused_vm_start_is_retried(self):
        # qm start can briefly refuse ("storage is not online") right after the share mounts.
        self.vm_images(4001)
        self.vm(4001, 'onboot: 1', self.CP_DISK)
        (self.state / 'fail-once-4001').write_text('')
        run = self.run_script(wait_s=10)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), ['4001', '4001'])
        self.assertIn('start attempt 1/3 failed; retrying', run.stdout)

    # --- VMs with a disk on an NFS storage (the Talos control planes, ADR 0043) ---------------------
    CP_DISK = 'scsi0: qnap-nfs:4001/vm-4001-disk-0.raw,discard=on,iothread=1,size=64G'

    def test_starts_a_stopped_onboot_vm_once_its_nfs_storage_is_mounted(self):
        self.vm_images(4001)
        self.vm(4001, 'onboot: 1', self.CP_DISK, 'ide2: local-lvm:vm-4001-cloudinit,media=cdrom')
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), ['4001'])
        self.assertIn('VM 4001: started', run.stdout)

    def test_waits_for_a_vm_storage_that_mounts_late(self):
        images = self.vm_images(4001, create=False)
        self.vm(4001, 'onboot: 1', self.CP_DISK)
        self.later(1.5, lambda: images.mkdir(parents=True))
        run = self.run_script(wait_s=10)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), ['4001'])

    def test_a_vm_whose_storage_never_mounts_is_not_started(self):
        self.vm(4001, 'onboot: 1', self.CP_DISK)
        run = self.run_script(wait_s=2)
        self.assertEqual(run.returncode, 1)
        self.assertIn('VM 4001:', run.stdout)
        self.assertIn('NOT starting it', run.stdout)
        self.assertEqual(self.started(), [])

    def test_ignores_vms_without_an_nfs_disk(self):
        # pve-guests already decided these: a local-lvm disk, or an NFS volume that is only an ISO.
        self.vm(4101, 'onboot: 1', 'scsi0: local-lvm:vm-4101-disk-0,size=200G')
        self.vm(4102, 'onboot: 1', 'scsi0: local-lvm:vm-4102-disk-0,size=200G',
                'ide2: qnap-nfs:iso/talos.iso,media=cdrom')
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), [])

    def test_ignores_vm_templates_running_vms_and_vms_without_onboot(self):
        self.vm_images(4001)
        self.vm(4001, 'onboot: 1', 'template: 1', self.CP_DISK)
        self.vm_images(4002)
        self.vm(4002, 'onboot: 1', self.CP_DISK.replace('4001', '4002'), state='running')
        self.vm_images(4003)
        self.vm(4003, 'onboot: 0', self.CP_DISK.replace('4001', '4003'))
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), [])

    def test_leaves_ha_managed_vms_to_ha_manager(self):
        self.vm_images(4001)
        self.ha.write_text('vm: 4001\n\tstate started\n', newline='\n')
        self.vm(4001, 'onboot: 1', self.CP_DISK)
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), [])

    def test_a_vm_started_by_hand_during_the_wait_is_not_started_again(self):
        images = self.vm_images(4001, create=False)
        self.vm(4001, 'onboot: 1', self.CP_DISK)

        def started_by_hand():
            self.set_state(4001, 'running')
            images.mkdir(parents=True)
        self.later(1.5, started_by_hand)
        run = self.run_script(wait_s=10)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), [])

    def test_a_container_whose_source_never_appears_does_not_hold_up_a_ready_vm(self):
        # Candidates are collected containers first. The CP's storage is ready at once; it must start
        # in the first pass, not after the container's whole budget.
        missing = self.nfs_src('never', create=False)
        self.ct(5002, 'onboot: 1', f'mp0: {missing.as_posix()},mp=/models')
        self.vm_images(4001)
        self.vm(4001, 'onboot: 1', self.CP_DISK)
        start = time.monotonic()
        run = self.run_script(wait_s=6)
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertEqual(self.started(), ['4001'])
        # The first pass, not the container's 6 s budget: a loaded CI runner can report 1 s here.
        self.assertRegex(run.stdout, r'VM 4001: started \(its NFS storage was ready after [0-2]s\)')
        self.assertIn('CT 5002:', run.stdout)
        self.assertIn('NOT starting it', run.stdout)
        self.assertGreaterEqual(time.monotonic() - start, 5, 'the container still gets its whole budget')

    def test_containers_and_vms_share_one_pass(self):
        src = self.nfs_src()
        self.ct(5002, 'onboot: 1', f'mp0: {src.as_posix()},mp=/models')
        self.vm_images(4001)
        self.vm(4001, 'onboot: 1', self.CP_DISK)
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(sorted(self.started()), ['4001', '5002'])


if __name__ == '__main__':
    unittest.main()
