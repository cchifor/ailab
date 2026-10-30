"""pve-nfs-guest-autostart: start onboot containers whose /mnt/pve bind source was late at boot.

Drives the real script against a fake /etc/pve lxc directory and a stub `pct` that records what it
was asked to start. 2026-10-30: after a power cut the QNAP served NFS minutes after pve-guests ran,
both LLM containers failed lxc.hook.pre-start and stayed down until started by hand.
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
# status <ctid> -> "status: <state>" from $STATE_DIR/<ctid> (default stopped); start <ctid> -> log it
case "$1" in
  status) echo "status: $(cat "$STATE_DIR/$2" 2>/dev/null || echo stopped)";;
  start) echo "$2" >> "$STATE_DIR/started"; [ ! -f "$STATE_DIR/fail-$2" ];;
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
        self.prefix = self.tmp / 'mnt' / 'pve'
        self.prefix.mkdir(parents=True)
        pct = self.tmp / 'pct'
        pct.write_text(PCT_STUB, newline='\n')
        pct.chmod(0o755)
        self.pct = pct

    def nfs_src(self, name='qnap-nfs/models', create=True):
        src = self.prefix / name
        if create:
            src.mkdir(parents=True, exist_ok=True)
        return src

    def ct(self, ctid, *lines, state=None):
        (self.conf / f'{ctid}.conf').write_text('\n'.join(lines) + '\n', newline='\n')
        if state:
            (self.state / str(ctid)).write_text(state)

    def run_script(self, wait_s=2):
        env = dict(os.environ, CONF_DIR=self.conf.as_posix(), PCT=self.pct.as_posix(),
                   MOUNT_PREFIX=self.prefix.as_posix() + '/', STATE_DIR=self.state.as_posix(),
                   WAIT_S=str(wait_s), STEP_S='1', PROBE_S='2')
        return subprocess.run([BASH, SCRIPT.as_posix()], env=env, capture_output=True, text=True,
                              timeout=60)

    def started(self):
        f = self.state / 'started'
        return f.read_text().split() if f.exists() else []

    def test_starts_a_stopped_onboot_container_once_its_nfs_source_exists(self):
        src = self.nfs_src()
        self.ct(5002, 'onboot: 1', f'mp0: {src.as_posix()},mp=/models')
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.started(), ['5002'])

    def test_waits_for_a_source_that_appears_late(self):
        src = self.nfs_src(create=False)
        self.ct(5002, 'onboot: 1', f'mp0: {src.as_posix()},mp=/models')
        threading.Timer(1.5, lambda: src.mkdir(parents=True)).start()
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

    def test_leaves_running_containers_alone(self):
        src = self.nfs_src()
        self.ct(5002, 'onboot: 1', f'mp0: {src.as_posix()},mp=/models', state='running')
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
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
        run = self.run_script()
        self.assertEqual(run.returncode, 1)
        self.assertEqual(self.started(), ['5002', '5003'])


if __name__ == '__main__':
    unittest.main()
