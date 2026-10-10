#!/usr/bin/env python3
"""Exercise real Ansible against disposable files and a strict service boundary.

No credentials, network, sudo or host service changes. TMPDIR belongs on /workspace.
"""
import grp
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import subprocess
import tempfile
import unittest
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[3]
TOKEN = 'fixture_connector_credential_never_log_or_copy'


class ConnectorRole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='relay-connector-iac-')
        cls.root = Path(cls.temp.name)
        user = pwd.getpwuid(os.getuid())
        home = cls.root / 'home'
        home.mkdir(mode=0o700)
        cls.identity = home / 'connector.json'
        host = str(uuid.uuid4())
        cls.unit = f'relay-connector-{host}.service'
        cls.identity.write_text(json.dumps(dict(id=host, url='https://relay.example',
            phase='approved', token=TOKEN, agents={'existing-agent': 'preserve'},
            service=dict(account=user.pw_name, name='Existing worker', sockets=['']))))
        cls.identity.chmod(0o600)
        cls.original_identity = cls.identity.read_bytes()
        artifact = cls.root / 'connector-bin'
        artifact.write_text('#!/bin/sh\n[ "$#" = 1 ] && [ "$1" = --version ] || exit 78\nprintf "relay-connector 0.3.5\\n"\n')
        artifact.chmod(0o755)
        cls.arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}[platform.machine()]
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        cls.values = dict(relay_connector_enabled=True, relay_connector_manage_service=False,
            relay_connector_user=user.pw_name, relay_connector_group=grp.getgrgid(user.pw_gid).gr_name,
            relay_connector_home=str(home), relay_connector_source=str(artifact),
            relay_connector_artifact_dir=str(cls.root / 'artifacts'),
            relay_connector_artifact_owner=user.pw_name,
            relay_connector_artifact_group=grp.getgrgid(user.pw_gid).gr_name,
            relay_connector_arch=cls.arch,
            relay_connector_release=dict(version='0.3.5', sourceCommit='a' * 40, sha256={cls.arch: digest}),
            relay_connector_origin='https://relay.example', relay_connector_host_id=host,
            relay_connector_state_file=str(cls.identity), relay_connector_name='Existing worker',
            relay_connector_sockets=[''])
        cls.unit_path = home / '.config/systemd/user' / cls.unit
        cls.receipt = cls.root / 'artifacts' / f'applied-{host}.json'
        commands = cls.root / 'commands'
        commands.mkdir()
        cls.state = cls.root / 'state.json'
        cls.default_state = dict(staging=[], linger='yes', dropins='', fragment=str(cls.unit_path),
            active='inactive', enabled=False, fail_restart=False)
        cls.state.write_text(json.dumps(cls.default_state))
        cls.command_log = cls.root / 'commands.jsonl'
        systemctl = commands / 'systemctl'
        systemctl.write_text('#!/usr/bin/env python3\n' + '''import json, pathlib, sys
root = pathlib.Path(ROOT_PATH)
state = json.loads((root/'state.json').read_text())
args = [x for x in sys.argv[1:] if x not in ('--user', '-l')]
with (root/'commands.jsonl').open('a') as log: log.write(json.dumps(args)+'\\n')
unit = UNIT_NAME
if args == ['list-units', '--all', '--type=service', '--output=json', '--no-pager', unit]:
    if state['staging'] == 'unreachable': sys.exit(1)
    print(json.dumps(state['staging']))
elif args == ['show', unit, '--property=FragmentPath', '--property=DropInPaths']:
    print('FragmentPath='+state['fragment']+'\\nDropInPaths='+state['dropins'])
elif args == ['show', unit]:
    print('LoadState=loaded\\nActiveState='+state['active']+'\\nUnitFileState='+('enabled' if state['enabled'] else 'disabled'))
elif args == ['is-enabled', unit]:
    print('enabled' if state['enabled'] else 'disabled')
    sys.exit(0 if state['enabled'] else 1)
elif args == ['enable', unit]: state['enabled'] = True
elif args in [['restart', unit], ['start', unit]]:
    if state['fail_restart']: sys.exit(1)
    state['active'] = 'active'
elif args == ['daemon-reload']: pass
else: sys.exit('unexpected service command: '+repr(args))
(root/'state.json').write_text(json.dumps(state))
'''.replace('ROOT_PATH', repr(str(cls.root))).replace('UNIT_NAME', repr(cls.unit)))
        systemctl.chmod(0o755)
        loginctl = commands / 'loginctl'
        loginctl.write_text('#!/usr/bin/env python3\nimport json,pathlib,sys\n' +
            f"assert sys.argv[1:] == ['show-user','{os.getuid()}','--property=Linger','--value']\n" +
            f"print(json.loads(pathlib.Path({str(cls.state)!r}).read_text())['linger'])\n")
        loginctl.chmod(0o755)
        cls.play = cls.root / 'play.yml'
        cls.play.write_text(yaml.safe_dump([dict(hosts='localhost', gather_facts=False,
            environment=dict(PATH=str(commands)+':'+os.environ['PATH']),
            roles=[dict(role='relay_connector')])]))
        cfg = cls.root / 'ansible.cfg'
        cfg.write_text('[defaults]\nroles_path='+str(ROOT/'ansible/roles')+'\nretry_files_enabled=False\n')
        cls.env = {**os.environ, 'ANSIBLE_CONFIG': str(cfg), 'ANSIBLE_NOCOLOR': '1',
            'ANSIBLE_LOCAL_TEMP': str(cls.root/'ansible-tmp')}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def state_update(self, **values):
        self.state.write_text(json.dumps({**json.loads(self.state.read_text()), **values}))

    def role(self, updates=None, success=True, check=False):
        values = {**self.values, **(updates or {})}
        extra = self.root/'vars.json'
        extra.write_text(json.dumps(values))
        self.command_log.write_text('')
        command = ['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(self.play),
            '--diff', '-e', '@'+str(extra)] + (['--check'] if check else [])
        result = subprocess.run(command, env=self.env, text=True, capture_output=True, timeout=120)
        output = result.stdout+result.stderr
        self.assertNotIn(TOKEN, output)
        self.assertEqual(result.returncode == 0, success, output[-7000:])
        self.commands = [json.loads(line) for line in self.command_log.read_text().splitlines()]
        if not values['relay_connector_manage_service']:
            self.assertTrue(all(c[0] == 'list-units' for c in self.commands), self.commands)
        return output

    def test_01_disabled_does_nothing(self):
        out = self.role(dict(relay_connector_enabled=False, relay_connector_source='missing'))
        self.assertRegex(out, r'changed=0\s')
        self.assertFalse((self.root/'artifacts').exists())

    def test_02_bad_hash_and_arch_fail_before_writes(self):
        self.role(dict(relay_connector_release={**self.values['relay_connector_release'],
            'sha256': {self.arch: 'b'*64}}), success=False)
        self.role(dict(relay_connector_arch='not-a-worker-arch'), success=False)
        self.assertFalse((self.root/'artifacts').exists())

    def test_03_stage_preserves_identity_and_reruns_without_changes(self):
        self.role()
        self.assertEqual(self.identity.read_bytes(), self.original_identity)
        self.assertEqual(self.identity.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.receipt.exists())
        self.assertRegex(self.role(), r'changed=0\s')
        for f in (self.root/'artifacts').rglob('*'):
            if f.is_file(): self.assertNotIn(TOKEN.encode(), f.read_bytes())
        unit = self.unit_path.read_text()
        self.assertNotIn(TOKEN, unit)
        self.assertIn(' --state '+str(self.identity)+' serve', unit)
        self.assertIn('KillMode=control-group', unit)
        self.assertNotIn('control-plane', unit)
        runtime = self.root/'systemd-runtime'
        runtime.mkdir(mode=0o700)
        result = subprocess.run(['systemd-analyze','--user','verify',str(self.unit_path)],
            text=True, capture_output=True, timeout=15,
            env={**os.environ,'XDG_RUNTIME_DIR':str(runtime)})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_04_identity_and_service_inputs_are_not_repaired_silently(self):
        original = json.loads(self.original_identity)
        try:
            self.identity.chmod(0o640)
            self.role(success=False)
            self.identity.chmod(0o600)
            for changed in [dict(phase='pending'), dict(id=str(uuid.uuid4())),
                            dict(service={**original['service'], 'account':'foreign'})]:
                self.identity.write_text(json.dumps({**original, **changed}))
                self.role(success=False)
                self.assertEqual(json.loads(self.identity.read_text()), {**original, **changed})
        finally:
            self.identity.write_bytes(self.original_identity)
            self.identity.chmod(0o600)
        self.role(dict(relay_connector_sockets=['/different/socket']), success=False)
        self.role(dict(relay_connector_unit_dir=str(self.root)+'/unsafe%unit'), success=False)

    def test_05_staging_denies_active_unknown_and_unreachable_service(self):
        unit = self.unit_path.read_bytes()
        try:
            for state in [[dict(active='active',sub='running')],
                          [dict(active='inactive',sub='dead',job='start')], {}, 'unreachable']:
                self.state_update(staging=state)
                self.role(success=False)
                self.assertEqual(self.unit_path.read_bytes(), unit)
        finally: self.state_update(staging=[])

    def test_06_activation_requires_linger_and_unshadowed_unit(self):
        try:
            self.state_update(linger='no')
            self.role(dict(relay_connector_manage_service=True), success=False)
            self.assertEqual(self.commands, [])
            self.state_update(linger='yes', dropins='/unexpected/override.conf')
            self.role(dict(relay_connector_manage_service=True), success=False)
            self.assertFalse(any(c[0] in ['restart','start','enable'] for c in self.commands))
            self.assertFalse(self.receipt.exists())
        finally: self.state_update(**self.default_state)

    def test_07_activation_receipt_survives_interrupted_restart_and_reruns(self):
        self.state_update(fail_restart=True)
        self.role(dict(relay_connector_manage_service=True), success=False)
        self.assertFalse(self.receipt.exists())
        self.state_update(fail_restart=False)
        self.role(dict(relay_connector_manage_service=True))
        self.assertEqual([c for c in self.commands if c[0] in ['restart','start']], [['start',self.unit]])
        self.assertEqual(json.loads(self.receipt.read_text())['binarySha256'],
            self.values['relay_connector_release']['sha256'][self.arch])
        self.assertRegex(self.role(dict(relay_connector_manage_service=True)), r'changed=0\s')
        self.assertFalse(any(c[0] in ['restart','start','enable'] for c in self.commands))
        self.assertEqual(self.identity.read_bytes(), self.original_identity)
        # Changed immutable bytes select a new path and restart only this unit.
        updated = self.root/'updated-connector'
        updated.write_bytes(Path(self.values['relay_connector_source']).read_bytes()+b'# new release fixture\n')
        digest = hashlib.sha256(updated.read_bytes()).hexdigest()
        self.role(dict(relay_connector_manage_service=True, relay_connector_source=str(updated),
            relay_connector_release={**self.values['relay_connector_release'], 'sha256': {self.arch: digest}}))
        self.assertEqual([c for c in self.commands if c[0]=='restart'], [['restart',self.unit]])
        self.assertEqual(json.loads(self.receipt.read_text())['binarySha256'], digest)
        self.role(dict(relay_connector_manage_service=True))  # Reviewed repin rollback.
        self.assertEqual([c for c in self.commands if c[0]=='restart'], [['restart',self.unit]])
        self.assertEqual(self.identity.read_bytes(), self.original_identity)

    def test_08_check_mode_never_changes_services(self):
        receipt = self.receipt.read_bytes()
        self.role(dict(relay_connector_manage_service=True), check=True)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.receipt.read_bytes(), receipt)


if __name__ == '__main__':
    unittest.main()
