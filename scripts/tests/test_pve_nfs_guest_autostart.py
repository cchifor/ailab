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
# status <ctid> -> "status: <state>" from $STATE_DIR/<ctid> (default stopped), or exit 2 when
# $STATE_DIR/fail-status-<ctid> exists; after answering, a $STATE_DIR/next-<ctid> becomes the new
# state (a deterministic "changed after the first look"). start <ctid> -> log it, fail when
# fail-<ctid> exists.
case "$1" in
  status) [ -f "$STATE_DIR/fail-status-$2" ] && { echo "unable to read config" >&2; exit 2; }
          echo "status: $(cat "$STATE_DIR/$2" 2>/dev/null || echo stopped)"
          [ -f "$STATE_DIR/next-$2" ] && mv "$STATE_DIR/next-$2" "$STATE_DIR/$2"; exit 0;;
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
        # The brackets make a regex-unsafe prefix: it must be compared literally.
        self.prefix = self.tmp / 'mnt[1]' / 'pve'
        self.prefix.mkdir(parents=True)
        self.ha = self.tmp / 'resources.cfg'
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

    def set_state(self, ctid, state):
        (self.state / str(ctid)).write_text(state)

    def run_script(self, wait_s=2):
        env = dict(os.environ, CONF_DIR=self.conf.as_posix(), PCT=self.pct.as_posix(),
                   MOUNT_PREFIX=self.prefix.as_posix() + '/', STATE_DIR=self.state.as_posix(),
                   HA_RESOURCES=self.ha.as_posix(), WAIT_S=str(wait_s), STEP_S='1', PROBE_S='2',
                   **self.env)
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
        run = self.run_script()
        self.assertEqual(run.returncode, 1)
        self.assertEqual(self.started(), ['5002', '5003'])


if __name__ == '__main__':
    unittest.main()
