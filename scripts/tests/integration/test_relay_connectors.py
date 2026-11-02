#!/usr/bin/env python3
"""Exercise real Ansible against disposable files and a strict service boundary.

No credentials, network, sudo or host service changes. TMPDIR belongs on /workspace.
Also unit-tests the role's Claude Code settings helper (files/claude_relay_hook.py).
"""
import grp
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import pwd
import subprocess
import sys
import tempfile
import unittest
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[3]
TOKEN = 'fixture_connector_credential_never_log_or_copy'
HOOK = ROOT / 'ansible/roles/relay_connector/files/claude_relay_hook.py'
_spec = importlib.util.spec_from_file_location('claude_relay_hook', HOOK)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

# A user's own Claude Code settings: every key and hook here must survive the role.
USER_SETTINGS = {
    'model': 'opus',
    'statusLine': {'type': 'command', 'command': '/usr/bin/node /home/c4/.claude/statusline.js',
                   'refreshInterval': 5},
    'permissions': {'allow': ['Bash(git status)']},
    'hooks': {
        'UserPromptSubmit': [{'hooks': [{'type': 'command', 'command': '/usr/local/bin/user-prompt-hook'}]}],
        'Stop': [{'matcher': '', 'hooks': [{'type': 'command', 'command': 'notify-send done'}]}],
    },
}
USER_MCP = {'type': 'stdio', 'command': '/usr/local/bin/other-mcp', 'args': [], 'env': {}}
USER_CLAUDE_CONFIG = {'numStartups': 7, 'mcpServers': {'other': USER_MCP},
                      'primaryApiKey': 'fixture_claude_api_key_never_log'}


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
            f"value=json.loads(pathlib.Path({str(cls.state)!r}).read_text())['linger']\n" +
            "if value=='missing-session': sys.exit(1)\nprint(value)\n")
        loginctl.chmod(0o755)
        getent = commands / 'getent'
        getent.write_text('#!/usr/bin/env python3\nimport json,pathlib,subprocess,sys\n'+
            f"state=json.loads(pathlib.Path({str(cls.state)!r}).read_text())\n"+
            "out=subprocess.check_output(['/usr/bin/getent',*sys.argv[1:]],text=True).strip()\n"+
            "if sys.argv[1]=='passwd' and state.get('passwdHome'):\n"+
            "    fields=out.split(':'); fields[5]=state['passwdHome']; fields[3]='0'; out=':'.join(fields)\n"+
            "print(out)\n")
        getent.chmod(0o755)
        # Claude Code's CLI, limited to the two calls the role may make. Like the real one, it
        # refuses to add a name that exists or remove one that does not.
        cls.claude_log = cls.root / 'claude.jsonl'
        claude = commands / 'claude'
        claude.write_text('#!/usr/bin/env python3\n' + '''import json, os, pathlib, sys
args = sys.argv[1:]
with pathlib.Path(LOG_PATH).open('a') as log:
    log.write(json.dumps(dict(args=args, home=os.environ.get('HOME'), cwd=os.getcwd()))+'\\n')
config = pathlib.Path(os.environ['HOME'])/'.claude.json'
data = json.loads(config.read_text()) if config.exists() else {}
servers = data.setdefault('mcpServers', {})
if len(args) == 6 and args[:5] == ['mcp', 'add-json', '--scope', 'user', 'relay']:
    if 'relay' in servers: sys.exit('MCP server relay already exists in user config')
    entry = json.loads(args[5])
    entry.setdefault('env', {})
    servers['relay'] = entry
elif args == ['mcp', 'remove', '--scope', 'user', 'relay']:
    if 'relay' not in servers: sys.exit('No user-scoped MCP server found with name: relay')
    del servers['relay']
else: sys.exit('unexpected claude invocation: '+repr(args))
config.write_text(json.dumps(data, indent=2))
'''.replace('LOG_PATH', repr(str(cls.claude_log))))
        claude.chmod(0o755)
        (home / '.npm-global/bin').mkdir(parents=True)
        (home / '.npm-global/bin/claude').symlink_to(claude)  # The role's default CLI path.
        cls.home = home
        cls.settings = home / '.claude/settings.json'
        cls.claude_json = home / '.claude.json'
        cls.claude_artifact = cls.root / 'connector-bin-0.8.0'
        cls.claude_artifact.write_text(artifact.read_text().replace('0.3.5', '0.8.0'))
        cls.claude_artifact.chmod(0o755)
        cls.claude_values = cls.pin(cls.claude_artifact, '0.8.0')
        cls.present = {**cls.claude_values, 'relay_connector_claude_messaging': 'present'}
        cls.absent = {**cls.claude_values, 'relay_connector_claude_messaging': 'absent'}
        cls.play = cls.root / 'play.yml'
        cls.play.write_text(yaml.safe_dump([dict(hosts='localhost', gather_facts=False,
            environment=dict(PATH=str(commands)+':'+os.environ['PATH']),
            roles=[dict(role='relay_connector')],
            post_tasks=[{'ansible.builtin.debug': {'msg':'derived-account={{ relay_connector_home }}:{{ relay_connector_group }}'},
                         'when':'relay_connector_enabled | bool'}])]))
        cfg = cls.root / 'ansible.cfg'
        cfg.write_text('[defaults]\nroles_path='+str(ROOT/'ansible/roles')+'\nretry_files_enabled=False\n')
        cls.env = {**os.environ, 'ANSIBLE_CONFIG': str(cfg), 'ANSIBLE_NOCOLOR': '1',
            'ANSIBLE_LOCAL_TEMP': str(cls.root/'ansible-tmp')}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def pin(cls, artifact, version):
        digest = hashlib.sha256(Path(artifact).read_bytes()).hexdigest()
        return dict(relay_connector_source=str(artifact),
            relay_connector_release=dict(version=version, sourceCommit='b'*40, sha256={cls.arch: digest}))

    def state_update(self, **values):
        self.state.write_text(json.dumps({**json.loads(self.state.read_text()), **values}))

    def role(self, updates=None, success=True, check=False, omit=()):
        values = {**self.values, **(updates or {})}
        for key in omit: values.pop(key, None)
        extra = self.root/'vars.json'
        extra.write_text(json.dumps(values))
        self.command_log.write_text('')
        self.claude_log.write_text('')
        command = ['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(self.play),
            '--diff', '-e', '@'+str(extra)] + (['--check'] if check else [])
        result = subprocess.run(command, env=self.env, text=True, capture_output=True, timeout=120)
        output = result.stdout+result.stderr
        self.assertNotIn(TOKEN, output)
        self.assertNotIn(USER_CLAUDE_CONFIG['primaryApiKey'], output)
        self.assertEqual(result.returncode == 0, success, output[-7000:])
        self.commands = [json.loads(line) for line in self.command_log.read_text().splitlines()]
        self.claude_calls = [json.loads(line) for line in self.claude_log.read_text().splitlines()]
        if not values['relay_connector_manage_service']:
            self.assertTrue(all(c[0] == 'list-units' for c in self.commands), self.commands)
        return output

    @staticmethod
    def failed_task(output):
        """Name of the role task that failed the run."""
        head = output[:output.index('fatal: [localhost]')]
        return head[head.rindex('TASK [relay_connector : ') + len('TASK [relay_connector : '):].split('] *')[0]

    # Claude Code messaging fixtures and probes.
    def seed_claude(self):
        """The user's own settings.json (0640) and ~/.claude.json, without anything from Relay."""
        self.settings.parent.mkdir(mode=0o700, exist_ok=True)
        self.settings.write_text(json.dumps(USER_SETTINGS, indent=2))
        self.settings.chmod(0o640)
        self.claude_json.write_text(json.dumps(USER_CLAUDE_CONFIG, indent=2))
        self.claude_json.chmod(0o600)

    def snapshot(self):
        """Bytes and mode of every file the role could touch: the worker's artifacts and the home."""
        files = {}
        for top in (self.root/'artifacts', self.home):
            for path in sorted(top.rglob('*')):
                files[str(path)] = (path.read_bytes() if path.is_file() else None, path.lstat().st_mode)
        return files

    def connector_path(self, artifact):
        return f"{self.root}/artifacts/{hashlib.sha256(Path(artifact).read_bytes()).hexdigest()}/relay-connector"

    def expected_mcp(self, artifact):
        return dict(type='stdio', command=self.connector_path(artifact), args=['--state', str(self.identity), 'mcp'])

    def expected_hook(self, artifact):
        return dict(type='command', command=f'{self.connector_path(artifact)} --state {self.identity} inbox-hint',
                    timeout=2)

    def mcp_relay(self):
        relay = json.loads(self.claude_json.read_text())['mcpServers'].get('relay')
        return relay and {key: relay[key] for key in ('type', 'command', 'args')}

    def relay_hooks(self):
        groups = json.loads(self.settings.read_text()).get('hooks', {}).get('UserPromptSubmit', [])
        return [entry for group in groups for entry in group['hooks'] if hook.is_relay_inbox_hint(entry)]

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
            self.identity.write_text(json.dumps({k:v for k,v in original.items() if k != 'phase'}))
            self.role(success=False)
            self.assertNotIn('phase', json.loads(self.identity.read_text()))
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
            self.state_update(linger='missing-session')
            out=self.role(dict(relay_connector_manage_service=True), success=False)
            self.assertIn('Converge worker linger through the reviewed account bootstrap', out)
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

    def test_09_nonstandard_passwd_home_and_primary_group_are_derived(self):
        nonstandard = str(self.root/'nonstandard-home')
        self.state_update(passwdHome=nonstandard)
        try:
            out=self.role(check=True, omit=['relay_connector_home','relay_connector_group'])
            self.assertIn('derived-account='+nonstandard+':'+grp.getgrgid(0).gr_name, out)
            self.assertFalse(Path(nonstandard).exists())
        finally: self.state_update(passwdHome=None)

    def test_10_claude_messaging_needs_connector_0_8_0_before_any_write(self):
        self.seed_claude()
        before = self.snapshot()
        gate = 'Require a connector release that serves Claude Code messaging'
        out = self.role(dict(relay_connector_claude_messaging='present'), success=False)  # The 0.3.5 pin.
        self.assertEqual(self.failed_task(out), gate)
        self.assertIn('needs relay-connector 0.8.0 or later', out)
        self.assertIn('this host pins 0.3.5', out)
        # Refused before the binary is installed: a 0.7.9 pin's new bytes never reach the worker.
        pre = {**self.pin(self.claude_artifact, '0.7.9'), 'relay_connector_claude_messaging': 'present'}
        self.assertEqual(self.failed_task(self.role(pre, success=False)), gate)
        out = self.role({**self.claude_values, 'relay_connector_claude_messaging': 'enabled'}, success=False)
        self.assertEqual(self.failed_task(out), 'Require a known Claude Code messaging state')
        out = self.role({**self.present, 'relay_connector_claude_bin': str(self.root/'missing/claude')}, success=False)
        self.assertEqual(self.failed_task(out), 'Require the Claude Code CLI before registering Relay messaging')
        out = self.role({**self.present, 'relay_connector_claude_bin': '/opt/claude; rm'}, success=False)
        self.assertEqual(self.failed_task(out), 'Reject an unsafe Claude Code path or hook timeout')
        out = self.role({**self.present, 'relay_connector_claude_hook_timeout': 0}, success=False)
        self.assertEqual(self.failed_task(out), 'Reject an unsafe Claude Code path or hook timeout')
        self.assertEqual(self.claude_calls, [])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(Path(self.connector_path(self.claude_artifact)).parent.exists())

    def test_11_claude_present_registers_mcp_and_hook_keeping_user_settings(self):
        self.seed_claude()
        self.role(self.present)
        self.assertEqual(len(self.claude_calls), 1, self.claude_calls)
        args = self.claude_calls[0]['args']
        self.assertEqual((args[:5], len(args)), (['mcp', 'add-json', '--scope', 'user', 'relay'], 6))
        self.assertEqual(json.loads(args[5]), self.expected_mcp(self.claude_artifact))
        self.assertEqual((self.claude_calls[0]['home'], self.claude_calls[0]['cwd']), (str(self.home),) * 2)
        config = json.loads(self.claude_json.read_text())
        self.assertEqual(self.mcp_relay(), self.expected_mcp(self.claude_artifact))
        self.assertEqual({k: v for k, v in config.items() if k != 'mcpServers'},
            {k: v for k, v in USER_CLAUDE_CONFIG.items() if k != 'mcpServers'})
        self.assertEqual(config['mcpServers']['other'], USER_MCP)
        settings = json.loads(self.settings.read_text())
        self.assertEqual({k: v for k, v in settings.items() if k != 'hooks'},
            {k: v for k, v in USER_SETTINGS.items() if k != 'hooks'})
        self.assertEqual(settings['hooks'], {**USER_SETTINGS['hooks'], 'UserPromptSubmit':
            USER_SETTINGS['hooks']['UserPromptSubmit'] + [{'hooks': [self.expected_hook(self.claude_artifact)]}]})
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.settings.stat().st_uid, os.getuid())
        self.assertRegex(self.role(self.present), r'changed=0\s')
        self.assertEqual(self.claude_calls, [])

    def test_12_claude_new_artifact_replaces_mcp_and_hook(self):
        self.seed_claude()
        self.role(self.present)
        # A hand-added copy inside the user's own group goes too; the user's hook beside it stays.
        settings = json.loads(self.settings.read_text())
        settings['hooks']['UserPromptSubmit'][0]['hooks'].append(
            {'type': 'command', 'command': 'relay-connector --state /elsewhere inbox-hint'})
        self.settings.write_text(json.dumps(settings))
        rebuilt = self.root/'connector-bin-0.8.0-rebuilt'
        rebuilt.write_bytes(self.claude_artifact.read_bytes()+b'# rebuilt release fixture\n')
        rebuilt.chmod(0o755)
        updated = {**self.pin(rebuilt, '0.8.0'), 'relay_connector_claude_messaging': 'present'}
        self.role(updated)
        self.assertEqual([c['args'][:2] for c in self.claude_calls], [['mcp', 'remove'], ['mcp', 'add-json']])
        self.assertEqual(self.claude_calls[0]['args'], ['mcp', 'remove', '--scope', 'user', 'relay'])
        self.assertEqual(self.mcp_relay(), self.expected_mcp(rebuilt))
        self.assertEqual(self.relay_hooks(), [self.expected_hook(rebuilt)])
        groups = json.loads(self.settings.read_text())['hooks']['UserPromptSubmit']
        self.assertEqual(groups, USER_SETTINGS['hooks']['UserPromptSubmit'] + [{'hooks': [self.expected_hook(rebuilt)]}])
        self.assertRegex(self.role(updated), r'changed=0\s')
        self.assertEqual(self.claude_calls, [])

    def test_13_claude_absent_removes_both_and_keeps_user_hooks(self):
        self.seed_claude()
        self.role(self.present)
        self.role(self.absent)
        self.assertEqual([c['args'] for c in self.claude_calls], [['mcp', 'remove', '--scope', 'user', 'relay']])
        self.assertIsNone(self.mcp_relay())
        self.assertEqual(json.loads(self.claude_json.read_text())['mcpServers'], {'other': USER_MCP})
        self.assertEqual(json.loads(self.settings.read_text()), USER_SETTINGS)
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o640)
        self.assertRegex(self.role(self.absent), r'changed=0\s')
        self.assertEqual(self.claude_calls, [])

    def test_14_claude_check_mode_changes_nothing(self):
        self.seed_claude()
        self.role(self.claude_values)  # Binary and unit current, so only messaging would change.
        before = self.snapshot()
        out = self.role(self.present, check=True)
        self.assertIn('Would register the user-scope relay MCP server', out)
        self.assertRegex(out, r'changed=2\s')
        self.assertEqual(self.claude_calls, [])
        self.assertEqual(self.snapshot(), before)
        self.role(self.present)
        before = self.snapshot()
        out = self.role(self.absent, check=True)
        self.assertIn('Would remove the user-scope relay MCP server', out)
        self.assertRegex(out, r'changed=2\s')
        self.assertEqual(self.claude_calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_15_claude_unparsable_files_fail_loudly_before_any_change(self):
        self.seed_claude()
        self.role(self.claude_values)
        self.settings.write_text('{"model": "opus",')
        before = self.snapshot()
        out = self.role(self.present, success=False)
        self.assertEqual(self.failed_task(out), "Converge the Relay inbox hook in the user's Claude Code settings")
        self.assertIn('is not valid JSON', out)
        self.assertIn('nothing was written', out)
        self.assertEqual(self.claude_calls, [])
        self.assertEqual(self.snapshot(), before)
        # ~/.claude.json is only read, never displayed, even when it does not parse.
        self.seed_claude()
        self.claude_json.write_text(json.dumps(USER_CLAUDE_CONFIG)[:-1])
        before = self.snapshot()
        out = self.role(self.present, success=False)  # role() also asserts the API key never appears.
        self.assertEqual(self.failed_task(out), 'Compare the user-scope relay MCP server with the pinned connector')
        self.assertEqual(self.claude_calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_16_claude_unmanaged_touches_nothing(self):
        self.seed_claude()
        self.role(self.present)
        # Undo both by hand, so a managed run would act and an unmanaged one must not.
        config = json.loads(self.claude_json.read_text())
        del config['mcpServers']['relay']
        self.claude_json.write_text(json.dumps(config))
        self.settings.write_text(json.dumps(USER_SETTINGS))
        before = self.snapshot()
        self.assertRegex(self.role(self.claude_values), r'changed=0\s')
        self.assertRegex(self.role({**self.claude_values, 'relay_connector_claude_bin': '/nonexistent claude'}),
            r'changed=0\s')
        self.assertEqual(self.claude_calls, [])
        self.assertEqual(self.snapshot(), before)


RELAY_HINT = ('/opt/relay/connector/' + 'a' * 64 + '/relay-connector'
              ' --state /home/c4/.config/relay-connector/connector.json inbox-hint')
RELAY_ENTRY = {'type': 'command', 'command': RELAY_HINT, 'timeout': 2}


class ClaudeRelayHookScript(unittest.TestCase):
    """files/claude_relay_hook.py on its own: matching, merging and the file it writes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='claude-relay-hook-')
        self.home = Path(self.temp.name)
        self.settings = self.home/'.claude/settings.json'

    def tearDown(self):
        self.temp.cleanup()

    def run_hook(self, state='present', check=False, command=RELAY_HINT, stdin=False):
        argv = ['--settings', str(self.settings), '--state', state, '--command', command, '--timeout', '2']
        argv += ['--check'] if check else []
        if stdin:  # How the role runs it: piped to `python3 -`, nothing left on the worker.
            return subprocess.run([sys.executable, '-', *argv], input=HOOK.read_text(),
                                  text=True, capture_output=True, timeout=30)
        return subprocess.run([sys.executable, str(HOOK), *argv], text=True, capture_output=True, timeout=30)

    def write(self, data, mode=0o644):
        self.settings.parent.mkdir(mode=0o700, exist_ok=True)
        self.settings.write_text(data if isinstance(data, str) else json.dumps(data, indent=2))
        self.settings.chmod(mode)

    def test_matches_only_relay_connector_inbox_hint_commands(self):
        for command in [RELAY_HINT, 'relay-connector inbox-hint', "exec '/opt/relay dir/relay-connector' inbox-hint",
                        '/usr/bin/env relay-connector --state "/a b" inbox-hint']:
            self.assertTrue(hook.is_relay_inbox_hint({'type': 'command', 'command': command}), command)
        for entry in [{'type': 'command', 'command': '/opt/relay/connector/x/relay-connector --state /s serve'},
                      {'type': 'command', 'command': 'echo inbox-hint relay-connector'},
                      {'type': 'command', 'command': '/opt/relay-connector-old inbox-hint'},
                      {'type': 'command'}, {'command': 7}, 'relay-connector inbox-hint', None]:
            self.assertFalse(hook.is_relay_inbox_hint(entry), entry)

    def test_new_file_is_created_0644_in_a_private_claude_dir_and_rerun_is_unchanged(self):
        result = self.run_hook(stdin=True)
        self.assertEqual((result.returncode, result.stdout), (0, 'changed\n'), result.stderr)
        self.assertEqual(self.settings.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o644)
        self.assertEqual(json.loads(self.settings.read_text()), {'hooks': {'UserPromptSubmit': [{'hooks': [RELAY_ENTRY]}]}})
        inode, text = self.settings.stat().st_ino, self.settings.read_bytes()
        result = self.run_hook(stdin=True)
        self.assertEqual((result.returncode, result.stdout), (0, 'unchanged\n'), result.stderr)
        self.assertEqual((self.settings.stat().st_ino, self.settings.read_bytes()), (inode, text))

    def test_absent_on_a_missing_file_creates_nothing(self):
        result = self.run_hook(state='absent')
        self.assertEqual((result.returncode, result.stdout), (0, 'unchanged\n'), result.stderr)
        self.assertFalse(self.settings.parent.exists())

    def test_present_replaces_every_relay_entry_and_keeps_everything_else(self):
        user_hook = {'type': 'command', 'command': '/usr/local/bin/user-prompt-hook'}
        not_relay = {'hooks': [{'type': 'command', 'command': '/opt/relay/connector/x/relay-connector --state /s serve'},
                               {'type': 'command', 'command': 'echo inbox-hint'}]}
        other_event = {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'relay-connector inbox-hint'}]}
        settings = {
            'model': 'opus', 'note': 'café ☕', 'env': {'A': '1'},
            'hooks': {
                'UserPromptSubmit': [
                    {'hooks': [user_hook, {'type': 'command', 'command': 'exec /opt/relay/old/relay-connector inbox-hint'}]},
                    {'matcher': '', 'hooks': [{'type': 'command', 'command': 'relay-connector inbox-hint', 'timeout': 9}]},
                    {'hooks': []},
                    not_relay,
                ],
                'PreToolUse': [other_event],
            },
        }
        self.write(settings, mode=0o600)
        result = self.run_hook()
        self.assertEqual((result.returncode, result.stdout), (0, 'changed\n'), result.stderr)
        written = json.loads(self.settings.read_text())
        self.assertEqual(written, {**settings, 'hooks': {
            'UserPromptSubmit': [{'hooks': [user_hook]}, {'hooks': []}, not_relay, {'hooks': [RELAY_ENTRY]}],
            'PreToolUse': [other_event]}})
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_hook().stdout, 'unchanged\n')

    def test_absent_drops_only_what_its_removal_emptied(self):
        relay_group = {'hooks': [RELAY_ENTRY]}
        stop = [{'hooks': [{'type': 'command', 'command': 'notify-send done'}]}]
        cases = [  # (settings, after absent, after present and then absent)
            ({'model': 'x', 'hooks': {'UserPromptSubmit': [relay_group]}}, {'model': 'x'}, {'model': 'x'}),
            ({'hooks': {'UserPromptSubmit': [relay_group], 'Stop': stop}}, {'hooks': {'Stop': stop}},
             {'hooks': {'Stop': stop}}),
            ({'model': 'x'}, {'model': 'x'}, {'model': 'x'}),
            # The user's own empty containers stay, unless it was the removal that emptied them.
            ({'hooks': {'UserPromptSubmit': []}}, {'hooks': {'UserPromptSubmit': []}}, {}),
            ({'hooks': {}}, {'hooks': {}}, {}),
        ]
        for before, after_absent, after_round_trip in cases:
            original = json.loads(json.dumps(before))
            self.assertEqual(hook.converge(before, 'absent'), after_absent, before)
            self.assertEqual(hook.converge(hook.converge(before, 'present', RELAY_HINT, 2), 'absent'),
                             after_round_trip, before)
            self.assertEqual(before, original)  # converge() returns a copy.

    def test_symlinked_settings_keep_the_link_and_the_target_mode(self):
        dotfiles = self.home/'dotfiles'
        dotfiles.mkdir()
        target = dotfiles/'claude-settings.json'
        target.write_text('{"model": "opus"}')
        target.chmod(0o600)
        self.settings.parent.mkdir(mode=0o700)
        self.settings.symlink_to(target)
        result = self.run_hook()
        self.assertEqual((result.returncode, result.stdout), (0, 'changed\n'), result.stderr)
        self.assertTrue(self.settings.is_symlink())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(target.read_text())['hooks']['UserPromptSubmit'], [{'hooks': [RELAY_ENTRY]}])
        self.assertEqual(sorted(p.name for p in dotfiles.iterdir()), ['claude-settings.json'])

    def test_check_reports_without_writing(self):
        result = self.run_hook(check=True)
        self.assertEqual((result.returncode, result.stdout), (0, 'changed\n'), result.stderr)
        self.assertFalse(self.settings.parent.exists())
        self.write({'model': 'opus'})
        before = self.settings.read_bytes()
        self.assertEqual(self.run_hook(check=True).stdout, 'changed\n')
        self.assertEqual(self.settings.read_bytes(), before)
        self.write({'model': 'opus', 'hooks': {'UserPromptSubmit': [{'hooks': [RELAY_ENTRY]}]}})
        self.assertEqual(self.run_hook(state='absent', check=True).stdout, 'changed\n')
        self.assertEqual(self.run_hook(check=True).stdout, 'unchanged\n')

    def test_unparsable_or_misshapen_settings_fail_without_writing(self):
        for content in ['{"model": "opus",', '', '[]', '"text"', '{"hooks": []}', '{"hooks": null}',
                        '{"hooks": {"UserPromptSubmit": {}}}', b'\xff\xfe{}']:
            self.settings.parent.mkdir(mode=0o700, exist_ok=True)
            if isinstance(content, bytes): self.settings.write_bytes(content)
            else: self.settings.write_text(content)
            before = self.settings.read_bytes()
            for state in ('present', 'absent'):
                result = self.run_hook(state=state)
                self.assertEqual(result.returncode, 1, (content, state, result.stdout))
                self.assertIn('nothing was written', result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertEqual(self.settings.read_bytes(), before)
                self.assertEqual([p.name for p in self.settings.parent.iterdir()], ['settings.json'])

    def test_present_refuses_a_command_it_would_not_recognise_on_the_next_run(self):
        for command in ['/usr/bin/true', '/opt/relay/connector/x/relay-connector --state /s serve']:
            result = self.run_hook(command=command)
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn('inbox-hint', result.stderr)
            self.assertFalse(self.settings.parent.exists())

    def test_output_is_ansible_to_nice_json_so_the_status_line_task_never_rewrites_it(self):
        from ansible.plugins.filter.core import to_nice_json
        self.write({'statusLine': {'type': 'command', 'command': '/usr/bin/node /x.js', 'refreshInterval': 5},
                    'model': 'opus', 'note': 'café'})
        self.assertEqual(self.run_hook().stdout, 'changed\n')
        text = self.settings.read_text()
        self.assertEqual(text, to_nice_json(json.loads(text)))


if __name__ == '__main__':
    unittest.main()
