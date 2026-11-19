"""pve_base iocost (tag iocost): the helper script against a fake sysfs, the unit template, the defaults and host_vars.

The kernel side (blk-iocost on the real NVMe, io.weight on a qemu.slice scope) can only be checked on a host; the
runbook's verify steps cover that.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'ansible/roles/pve_base'
HELPER = ROLE / 'files/ailab-iocost.sh'
CP_VMS = {'ai-node1': 4001, 'ai-node2': 4002, 'ai-node3': 4003}


class Helper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.sysfs = base / 'sys'
        self.cgroup = base / 'cgroup'
        (self.sysfs / 'block/nvme0n1').mkdir(parents=True)
        (self.sysfs / 'block/nvme0n1/dev').write_text('259:0\n')
        self.cgroup.mkdir()
        (self.cgroup / 'io.cost.qos').write_text('')
        (self.cgroup / 'io.cost.model').write_text('')

    def tearDown(self):
        self.tmp.cleanup()

    def run_helper(self, *args):
        env = dict(os.environ, AILAB_SYSFS=str(self.sysfs), AILAB_CGROUP=str(self.cgroup))
        return subprocess.run(['sh', str(HELPER), *args], env=env, capture_output=True, text=True)

    def test_enable_writes_the_auto_model_then_turns_qos_on(self):
        r = self.run_helper('enable', 'nvme0n1')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.cgroup / 'io.cost.model').read_text(), '259:0 ctrl=auto\n')
        self.assertEqual((self.cgroup / 'io.cost.qos').read_text(), '259:0 enable=1 ctrl=auto\n')

    def test_disable_turns_qos_off_and_leaves_the_model(self):
        r = self.run_helper('disable', 'nvme0n1')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.cgroup / 'io.cost.qos').read_text(), '259:0 enable=0\n')
        self.assertEqual((self.cgroup / 'io.cost.model').read_text(), '')

    def test_status_prints_only_this_device(self):
        (self.cgroup / 'io.cost.qos').write_text('8:0 enable=0 ctrl=auto\n259:0 enable=1 ctrl=auto rpct=0.00\n')
        (self.cgroup / 'io.cost.model').write_text('259:0 ctrl=auto model=linear\n')
        r = self.run_helper('status', 'nvme0n1')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ['259:0 enable=1 ctrl=auto rpct=0.00', '259:0 ctrl=auto model=linear'])

    def test_refuses_bad_or_missing_devices_and_actions(self):
        for args in (('enable', '../nvme0n1'), ('enable', 'nvme9n9'), ('enable', ''), ('frobnicate', 'nvme0n1'), ()):
            with self.subTest(args=args):
                r = self.run_helper(*args)
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual((self.cgroup / 'io.cost.qos').read_text(), '')
                self.assertEqual((self.cgroup / 'io.cost.model').read_text(), '')

    def test_refuses_an_unexpected_dev_file(self):
        (self.sysfs / 'block/nvme0n1/dev').write_text('259:0 extra\n')
        r = self.run_helper('enable', 'nvme0n1')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.cgroup / 'io.cost.qos').read_text(), '')


class RoleConfig(unittest.TestCase):
    def test_defaults_are_off_with_no_weights(self):
        d = yaml.safe_load((ROLE / 'defaults/main.yml').read_text())
        self.assertIs(d['pve_base_iocost_enabled'], False)
        self.assertEqual(d['pve_base_iocost_device'], 'nvme0n1')
        self.assertEqual(d['pve_base_iocost_vm_weights'], {})

    def test_each_node_protects_exactly_its_own_control_plane_vm(self):
        for host, vm in CP_VMS.items():
            with self.subTest(host=host):
                hv = yaml.safe_load((ROOT / f'ansible/host_vars/{host}.yml').read_text())
                self.assertIs(hv['pve_base_iocost_enabled'], True)
                self.assertEqual(hv['pve_base_iocost_vm_weights'], {vm: 10000})

    def test_unit_template_runs_the_helper_on_the_device(self):
        src = (ROLE / 'templates/ailab-iocost.service.j2').read_text()
        out = jinja2.Template(src, undefined=jinja2.StrictUndefined).render(pve_base_iocost_device='nvme0n1')
        self.assertIn('ExecStart=/usr/local/sbin/ailab-iocost enable nvme0n1', out)
        self.assertIn('ExecStop=/usr/local/sbin/ailab-iocost disable nvme0n1', out)
        self.assertIn('RemainAfterExit=yes', out)
        self.assertIn('WantedBy=multi-user.target', out)

    def test_every_iocost_task_carries_the_tag(self):
        tasks = yaml.safe_load((ROLE / 'tasks/main.yml').read_text())
        mine = [t for t in tasks if 'iocost' in t.get('name', '') or 'io.weight' in t.get('name', '')
                or 'protected VM' in t.get('name', '')]
        self.assertGreaterEqual(len(mine), 10)
        for t in mine:
            with self.subTest(task=t['name']):
                self.assertIn('iocost', t.get('tags', []))


if __name__ == '__main__':
    unittest.main()
