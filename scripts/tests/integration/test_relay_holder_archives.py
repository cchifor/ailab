#!/usr/bin/env python3
"""Exercise real archive tasks offline with a strict Docker boundary fixture.

Run as root in a disposable Linux container with ansible-core and PyYAML.
No Docker socket, services, credentials, downloads or worker inventory are used.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
ROLE = ROOT / 'ansible/roles/relay_holder'


class HolderArchives(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.getuid(), 0, 'Run in a disposable root container')
        self.temp = tempfile.TemporaryDirectory(prefix='holder-archives-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transfer = self.root / 'transfer'
        self.transfer.mkdir()
        self.commands = self.root / 'commands'
        self.commands.mkdir()
        self.log = self.root / 'docker.jsonl'
        self.marker = self.root / 'node-executed'
        # This executable is only allowed to run after the real role hashes it.
        self.node = (f'#!/bin/sh\nprintf executed > {self.marker}\n'
                     'printf "v22.23.2\\n"\n').encode()
        self.manifest = dict(node='22.23.2', entries=[dict(
            path='node', type='file', sha256=hashlib.sha256(self.node).hexdigest())])
        docker = self.commands / 'docker'
        docker.write_text('''#!/usr/bin/env python3
import json, pathlib, subprocess, sys
args = sys.argv[1:]
root = pathlib.Path(__file__).resolve().parent.parent
with (root/'docker.jsonl').open('a') as log:
    log.write(json.dumps(args)+'\\n')
assert args[:3] == ['run', '--rm', '--network=none'], args
assert all(x in args for x in ['--read-only', '--cap-drop=ALL',
    '--security-opt=no-new-privileges', '--user=0:0']), args
assert args[-3] == 'fixture-image@sha256:' + 'a'*64, args
config = json.loads(pathlib.Path(args[-1]).read_text())
entrypoint = next(x.split('=', 1)[1] for x in args if x.startswith('--entrypoint='))
if args[-2] == 'dist/scripts/install-pi-runtime.js':
    # Simulate the real image's mismatched default Node: the old task fails here.
    assert entrypoint != 'node', 'image Node 22.23.3 cannot install Pi 22.23.2'
    assert entrypoint == config['sourceDirectory'] + '/node', args
    manifest = json.loads((pathlib.Path(config['sourceDirectory'])/'manifest.json').read_text())
    assert subprocess.check_output([entrypoint, '--version'], text=True).strip() == 'v' + manifest['node']
else:
    assert args[-2] == 'dist/scripts/install-holder-release.js', args
    assert entrypoint == 'node', args
receipt = pathlib.Path(config['installationRoot'])/'fixture-receipt'
changed = not receipt.exists()
receipt.write_text('installed')
print(json.dumps(dict(changed=changed, directory=str(receipt.parent))))
''')
        docker.chmod(0o755)
        self.play = self.root / 'play.yml'
        self.play.write_text(yaml.safe_dump([dict(hosts='localhost', gather_facts=False,
            environment=dict(PATH=str(self.commands) + ':' + os.environ['PATH']),
            tasks=[{'ansible.builtin.include_role': dict(name=str(ROLE), tasks_from='archive')}])]))
        cfg = self.root / 'ansible.cfg'
        cfg.write_text('[defaults]\nretry_files_enabled=False\n')
        self.env = {**os.environ, 'ANSIBLE_CONFIG': str(cfg), 'ANSIBLE_NOCOLOR': '1',
                    'ANSIBLE_LOCAL_TEMP': str(self.root / 'ansible-tmp'),
                    'ANSIBLE_PYTHON_INTERPRETER': sys.executable}
        self.make_archive()

    def make_archive(self, component='pi-linux-x64'):
        manifest = json.dumps(self.manifest).encode()
        archive = self.root / 'release.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for name, data, mode in [('manifest.json', manifest, 0o644), ('node', self.node, 0o755)]:
                entry = tarfile.TarInfo(name)
                entry.size, entry.mode = len(data), mode
                tar.addfile(entry, io.BytesIO(data))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.staged = self.transfer / digest
        self.values = dict(archive=dict(source=str(archive), component=component,
            sha256=digest, manifestSha256=hashlib.sha256(manifest).hexdigest()),
            relay_holder_transfer=str(self.transfer), relay_holder_root=str(self.root / 'holder'),
            relay_holder_pi_root=str(self.root / 'pi'),
            relay_holder_image='fixture-image@sha256:' + 'a'*64)
        if component == 'pi-linux-x64':
            self.values['archive']['nodeVersion'] = '22.23.2'
        for directory in ['holder', 'pi']:
            (self.root / directory).mkdir(exist_ok=True)

    def run_tasks(self, success=True):
        self.log.write_text('')
        self.marker.unlink(missing_ok=True)
        variables = self.root / 'vars.json'
        variables.write_text(json.dumps(self.values))
        result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '-c', 'local',
            str(self.play), '-e', '@' + str(variables)], env=self.env,
            capture_output=True, text=True, timeout=120)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode == 0, success, output[-6000:])
        if not success:
            self.assertEqual(self.log.read_text(), '', output)
            self.assertFalse(self.marker.exists(), output)
        return output

    def test_pi_uses_verified_packaged_node_and_repeat_is_idempotent(self):
        self.run_tasks()
        self.assertTrue(self.marker.exists())
        # Existing receipt behavior records changed=false on the first repeat.
        self.run_tasks()
        receipt = self.transfer / 'pi-linux-x64-installed.json'
        self.assertFalse(json.loads(receipt.read_text())['changed'])
        self.assertRegex(self.run_tasks(), r'changed=0\s')
        self.assertTrue(self.marker.exists())

    def test_holder_keeps_image_node_and_compiled_installer(self):
        self.make_archive(component='holder')
        self.run_tasks()
        self.assertFalse(self.marker.exists())
        self.run_tasks()
        self.assertFalse(json.loads((self.transfer / 'holder-installed.json').read_text())['changed'])
        self.assertRegex(self.run_tasks(), r'changed=0\s')

    def test_changed_node_on_repeat_is_refused_before_docker(self):
        self.run_tasks()
        node = self.staged / 'node'
        node.write_bytes(self.node + b'# drift\n')
        self.run_tasks(success=False)
        self.assertEqual(node.read_bytes(), self.node + b'# drift\n')

    def test_changed_manifest_cannot_authorize_changed_node(self):
        self.run_tasks()
        changed = self.node + b'# drift\n'
        (self.staged / 'node').write_bytes(changed)
        self.manifest['entries'][0]['sha256'] = hashlib.sha256(changed).hexdigest()
        (self.staged / 'manifest.json').write_text(json.dumps(self.manifest))
        self.run_tasks(success=False)

    def test_symlink_node_is_refused_even_with_matching_bytes(self):
        self.run_tasks()
        node = self.staged / 'node'
        node.rename(self.staged / 'other-node')
        node.symlink_to('other-node')
        self.run_tasks(success=False)

    def test_wrong_pinned_version_is_refused(self):
        self.manifest['node'] = '22.23.3'
        self.make_archive()
        self.run_tasks(success=False)

    def test_reviewed_new_node_version_does_not_require_a_role_change(self):
        self.node = self.node.replace(b'22.23.2', b'24.1.0')
        self.manifest['node'] = '24.1.0'
        self.manifest['entries'][0]['sha256'] = hashlib.sha256(self.node).hexdigest()
        self.make_archive()
        self.values['archive']['nodeVersion'] = '24.1.0'
        self.run_tasks()
        self.assertTrue(self.marker.exists())

    def test_missing_or_malformed_version_pin_is_refused(self):
        for pin in [None, '', '22.23', 22]:
            with self.subTest(pin=pin):
                if pin is None:
                    self.values['archive'].pop('nodeVersion', None)
                else:
                    self.values['archive']['nodeVersion'] = pin
                self.run_tasks(success=False)

    def test_duplicate_node_entries_are_refused(self):
        self.manifest['entries'] *= 2
        self.make_archive()
        self.run_tasks(success=False)


class HolderProfileHomes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='holder-profile-homes-', dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.native = self.state / 'native-homes'
        self.native.mkdir(mode=0o700)
        profile_id = '1d9a7d6e-4b26-49fd-9053-7b0a22e1baae'
        self.home = self.state / 'profile-homes' / profile_id
        self.workspace = self.state / 'workspaces' / profile_id
        self.values = dict(relay_holder_state=str(self.state), relay_holder_uid=os.getuid(),
            relay_holder_gid=os.getgid(), relay_holder_pi_root='/opt/fixture',
            relay_holder_archives=[dict(component='pi-linux-x64', manifestSha256='a'*64)],
            relay_holder_profiles=[dict(id=profile_id, executionUid=os.getuid(),
                qualification='protocol-fixture', native=dict(kind='pi', runtimeDirectory='/opt/fixture/'+'a'*64),
                credentialHome=str(self.home), roots=[str(self.workspace)])])
        self.play = self.root / 'play.yml'
        self.play.write_text(yaml.safe_dump([dict(hosts='localhost', gather_facts=False,
            tasks=[{'ansible.builtin.include_role': dict(name=str(ROLE), tasks_from='profile_paths')}])]))
        self.cfg = self.root / 'ansible.cfg'
        self.cfg.write_text('[defaults]\nretry_files_enabled=False\n')

    def run_paths(self, success=True):
        variables = self.root / 'vars.json'
        variables.write_text(json.dumps(self.values))
        result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '-c', 'local',
            str(self.play), '-e', '@' + str(variables)], capture_output=True, text=True, timeout=60,
            env={**os.environ, 'ANSIBLE_CONFIG': str(self.cfg), 'ANSIBLE_NOCOLOR': '1',
                 'ANSIBLE_LOCAL_TEMP': str(self.root / 'ansible-tmp'),
                 'ANSIBLE_PYTHON_INTERPRETER': sys.executable})
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode == 0, success, output[-6000:])
        return output

    def test_profile_staging_leaves_native_root_empty_and_is_idempotent(self):
        self.run_paths()
        for path in [self.home.parent, self.home, self.workspace.parent, self.workspace]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_uid, os.getuid())
        self.assertEqual(list(self.native.iterdir()), [])
        self.assertRegex(self.run_paths(), r'changed=0\s')

    def test_existing_native_evidence_is_preserved(self):
        retained = self.native / 'retained-evidence'
        retained.write_bytes(b'do not clear the holder fence')
        self.run_paths()
        self.assertEqual(retained.read_bytes(), b'do not clear the holder fence')

    def test_old_profile_layout_is_refused_before_directory_creation(self):
        self.values['relay_holder_profiles'][0]['credentialHome'] = str(self.native / self.home.name)
        self.run_paths(success=False)
        self.assertEqual(list(self.native.iterdir()), [])
        self.assertFalse(self.home.exists())
        self.assertFalse(self.workspace.exists())


if __name__ == '__main__':
    unittest.main()
