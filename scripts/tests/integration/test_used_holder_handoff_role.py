#!/usr/bin/env python3
"""Real Ansible transfer/role gate, private temporary c4 home, fake systemd only.

Run offline as UID1000 with disposable /home/c4 tmpfs owned by UID1000.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('used_fixture', ROOT / 'scripts/tests/test_prepare_used_holder_handoff.py')
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class HandoffRole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getuid() != 1000 or not Path('/home/c4').is_dir():
            raise RuntimeError('Requires disposable c4 home and UID1000')
        cls.case = fixture.UsedHandoff('runTest')
        cls.case.fixture_home = Path('/home/c4')
        cls.case.setUp()
        cls.addClassCleanup(cls.case.doCleanups)
        cls.result = cls.case.prepare()
        temporary = tempfile.TemporaryDirectory(prefix='handoff-role-')
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name)
        commands = cls.root / 'bin'
        commands.mkdir()
        systemctl = commands / 'systemctl'
        systemctl.write_text('''#!/usr/bin/env python3
import json, pathlib, sys
assert sys.argv[1:3] == ['--user', 'show'] and len(sys.argv) == 5
name = sys.argv[3]
assert name in ('relay-holder.service', 'relay-holder-control-plane.service')
print('Id='+name+'\\nActiveState=inactive\\nSubState=dead\\nLoadState=loaded\\nMainPID=0\\nControlGroup=\\nUnitFileState=disabled\\nFragmentPath=/home/c4/.config/systemd/user/'+name+'\\nDropInPaths=')
''')
        systemctl.chmod(0o755)
        # configure.yml resolves the real execution account before the gate.
        # This isolated container has UID1000 as tester, so only its passwd
        # lookup is supplied as c4 metadata; no account or source task changes.
        getent = commands / 'getent'
        getent.write_text('''#!/usr/bin/env python3
import sys
assert sys.argv[1:] == ['passwd', 'c4']
print('c4:x:1000:1000::/home/c4:/bin/bash')
''')
        getent.chmod(0o755)
        # End configure at its first read-only platform check after the gate.
        # Installer, archive and service tasks must never be reached here.
        sentinel = commands / 'stat'
        sentinel.write_text('''#!/usr/bin/env python3
import sys
assert sys.argv[1:] == ['-fc', '%T', '/sys/fs/cgroup']
print('handoff-configure-boundary')
''')
        sentinel.chmod(0o755)
        cls.values = dict(relay_holder_home='/home/c4', relay_holder_user='c4', relay_holder_uid=1000,
                          relay_holder_state=cls.result['newState'],
                          relay_holder_profile_state=str(cls.case.state),
                          relay_holder_handoff_receipt=cls.result['receipt'],
                          relay_holder_handoff_receipt_sha256=cls.result['receiptSha256'],
                          relay_holder_profiles_sha256=cls.result['profilesSha256'])
        cls.env = {**os.environ, 'PATH': str(commands) + ':' + os.environ['PATH'],
                   'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': str(cls.root / 'local'),
                   'ANSIBLE_REMOTE_TEMP': str(cls.root / 'remote'), 'HOME': str(cls.root)}
        cls.play = cls.root / 'play.yml'
        cls.play.write_text(yaml.safe_dump([dict(hosts='localhost', gather_facts=False,
            tasks=[{'ansible.builtin.include_role': dict(name=str(ROOT / 'ansible/roles/relay_holder'), tasks_from='handoff')}])]))

    def run_role(self, values, tasks_from='handoff'):
        self.play.write_text(yaml.safe_dump([dict(hosts='localhost', gather_facts=False,
            tasks=[{'ansible.builtin.include_role': dict(name=str(ROOT / 'ansible/roles/relay_holder'), tasks_from=tasks_from)}])]))
        variables = self.root / 'variables.json'
        variables.write_text(json.dumps(values))
        result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(self.play),
                                 '-e', '@' + str(variables), '-e', 'ansible_python_interpreter=' + sys.executable],
                                capture_output=True, text=True, env=self.env, timeout=60)
        return result

    def test_01_complete_receipt_transfers_self_contained_verifier(self):
        result = self.run_role(self.values)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('changed=0', result.stdout)
        self.case.unchanged()
        self.assertEqual(list(self.case.next.iterdir()), [])

    def test_02_default_same_base_requires_no_receipt_and_skips_transfer(self):
        result = self.run_role(dict(relay_holder_state=str(self.case.state), relay_holder_uid=1000))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('skipped=1', result.stdout)

    def test_03_start_and_unpinned_split_refuse_before_script(self):
        for change in (dict(relay_holder_start=True), dict(relay_holder_handoff_receipt=''),
                       dict(relay_holder_handoff_receipt_sha256='not-a-pin'), dict(relay_holder_home='/other')):
            with self.subTest(change=change):
                result = self.run_role({**self.values, **change})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Split profile paths require', result.stdout)
                self.case.unchanged()

    def test_04_valid_looking_but_wrong_receipt_hash_reaches_remote_refusal(self):
        result = self.run_role({**self.values, 'relay_holder_handoff_receipt_sha256': 'f' * 64})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refused', result.stdout)
        self.assertIn('Receipt SHA mismatch', result.stdout)
        self.assertNotIn('Traceback', result.stdout)
        self.case.unchanged()

    def test_05_shell_characters_are_passed_as_data_and_refused(self):
        marker = self.root / 'must-not-run'
        for field in ('relay_holder_state', 'relay_holder_profile_state', 'relay_holder_profiles_sha256'):
            with self.subTest(field=field):
                values = {**self.values, field: self.values[field] + '; touch ' + str(marker) + '; #'}
                result = self.run_role(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Handoff scope mismatch', result.stdout)
                self.assertFalse(marker.exists())
                self.case.unchanged()

    def test_06_configure_verifies_before_installation_or_directory_writes(self):
        values = {**self.values,
                  'relay_holder_image': 'registry.chifor.me/relay/control-plane@sha256:' + 'a' * 64,
                  'relay_holder_source_commit': 'b' * 40,
                  'relay_holder_binary_sha256': 'c' * 64,
                  'relay_holder_connector_sha256': 'd' * 64,
                  'relay_holder_connector_version': '0.11.3',
                  'relay_holder_profiles_source': '/evidence/profiles.json',
                  'relay_holder_connector_source': '/evidence/relay-connector',
                  'relay_holder_archives': [dict(component=component, source='/evidence/' + component + '.tgz',
                                                sha256='e' * 64, manifestSha256='f' * 64, nodeVersion='22.23.2')
                                           for component in ('holder', 'pi-linux-x64')]}
        before = [fixture.h.directory_identity(path, 1000, True)
                  for path in (self.case.state, self.case.next)]
        for valid in (False, True):
            with self.subTest(valid_receipt=valid):
                current = values if valid else {**values, 'relay_holder_handoff_receipt_sha256': 'f' * 64}
                result = self.run_role(current, tasks_from='configure')
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('Authenticate stopped handoff and preserved state', result.stdout)
                if valid:
                    self.assertIn('Require cgroup v2', result.stdout)
                    self.assertIn('handoff-configure-boundary', result.stdout)
                    self.assertNotIn('Receipt SHA mismatch', result.stdout)
                else:
                    self.assertIn('Receipt SHA mismatch', result.stdout)
                    self.assertNotIn('Require cgroup v2', result.stdout)
                self.assertIn('changed=0', result.stdout)
                for name in ('Pull the reviewed immutable installer image', 'Create root-owned installation roots',
                             'Create private execution-user directories'):
                    self.assertNotIn(name, result.stdout)
                self.assertEqual([fixture.h.directory_identity(path, 1000, True)
                                  for path in (self.case.state, self.case.next)], before)
                self.case.unchanged()
                self.assertEqual(list(self.case.next.iterdir()), [])

    def test_07_configure_does_not_manage_the_retained_profile_root(self):
        tasks = yaml.safe_load((ROOT / 'ansible/roles/relay_holder/tasks/configure.yml').read_text())
        directories = next(task for task in tasks if task.get('name') == 'Create private execution-user directories')
        self.assertEqual(directories['loop'], ['{{ relay_holder_state }}', '{{ relay_holder_config }}'])


if __name__ == '__main__':
    unittest.main()
