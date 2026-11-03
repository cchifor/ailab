#!/usr/bin/env python3
"""Exercise real Ansible against disposable files and a strict service boundary.

No credentials, network, sudo or host service changes. TMPDIR belongs on /workspace.
Also unit-tests the role's Claude Code settings helper (files/claude_relay_hook.py).
"""
import contextlib
import grp
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import pwd
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
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
# The relay_agent_skill role's hook (roles/relay_agent_skill/files/claude_skill_hook.py): the >= 0.9.0
# preflight must never take it for the old inbox-hint one.
SKILL_HOOK = {'type': 'command', 'timeout': 3, 'command': (
    'command -v python3 >/dev/null 2>&1 && '
    'python3 "$HOME/.claude/skills/relay-bus/relay-bus.py" inbox --hook 2>/dev/null; exit 0')}
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
        # Claude Code's CLI, limited to the calls the role may make. Like the real one (2.1.316),
        # it stores exactly the JSON it is given and refuses to add a name that exists or remove one
        # that does not. claude-fail.json injects one-shot failures, {"add-json": 1, "remove": 1}, and
        # {"lock-claude-dir": 1} makes ~/.claude read-only after the next successful call.
        cls.claude_log = cls.root / 'claude.jsonl'
        cls.claude_fail = cls.root / 'claude-fail.json'
        claude = commands / 'claude'
        claude.write_text('#!/usr/bin/env python3\n' + '''import json, os, pathlib, sys
args = sys.argv[1:]
with pathlib.Path(LOG_PATH).open('a') as log:
    log.write(json.dumps(dict(args=args, home=os.environ.get('HOME'), cwd=os.getcwd()))+'\\n')
if args == ['--version']:
    print('2.1.316 (Claude Code)')
    sys.exit(0)
config = pathlib.Path(os.environ['HOME'])/'.claude.json'
data = json.loads(config.read_text()) if config.exists() else {}
servers = data.setdefault('mcpServers', {})
if len(args) == 6 and args[:5] == ['mcp', 'add-json', '--scope', 'user', 'relay']: op = 'add-json'
elif args == ['mcp', 'remove', '--scope', 'user', 'relay']: op = 'remove'
else: sys.exit('unexpected claude invocation: '+repr(args))
fail = pathlib.Path(FAIL_PATH)
failures = json.loads(fail.read_text()) if fail.exists() else {}
if failures.get(op, 0) > 0:
    failures[op] -= 1
    fail.write_text(json.dumps(failures))
    sys.exit('injected failure: '+op)
if op == 'add-json':
    if 'relay' in servers: sys.exit('MCP server relay already exists in user config')
    servers['relay'] = json.loads(args[5])
else:
    if 'relay' not in servers: sys.exit('No user-scoped MCP server found with name: relay')
    del servers['relay']
config.write_text(json.dumps(data, indent=2))
if failures.get('lock-claude-dir', 0) > 0:  # The hook write that follows this call will fail.
    failures['lock-claude-dir'] -= 1
    fail.write_text(json.dumps(failures))
    (pathlib.Path(os.environ['HOME'])/'.claude').chmod(0o555)
'''.replace('LOG_PATH', repr(str(cls.claude_log))).replace('FAIL_PATH', repr(str(cls.claude_fail))))
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
        calls = [json.loads(line) for line in self.claude_log.read_text().splitlines()]
        # The preflight's `claude --version` starts, apart from the calls that change something.
        self.claude_starts = [call for call in calls if call['args'] == ['--version']]
        self.claude_calls = [call for call in calls if call['args'] != ['--version']]
        if not values['relay_connector_manage_service']:
            self.assertTrue(all(c[0] == 'list-units' for c in self.commands), self.commands)
        return output

    @staticmethod
    def failed_task(output):
        """Name of the role task that failed the run."""
        head = output[:output.rindex('fatal: [localhost]')]  # A rescue's own failure comes last.
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
        self.assertEqual(self.failed_task(out),
            'Require a Claude Code CLI the user can run before changing the relay MCP server')
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
        # The preflight started the CLI once, the way the change then ran it.
        self.assertEqual([(c['home'], c['cwd']) for c in self.claude_starts], [(str(self.home),) * 2])
        config = json.loads(self.claude_json.read_text())
        self.assertEqual(config['mcpServers']['relay'], self.expected_mcp(self.claude_artifact))
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
        self.assertEqual((self.claude_calls, self.claude_starts), ([], []))  # Nothing to change: no CLI.

    def test_12_claude_new_artifact_replaces_mcp_and_hook(self):
        self.seed_claude()
        self.role(self.present)
        # A hand-added copy inside the user's own group goes too; the user's hook beside it stays.
        settings = json.loads(self.settings.read_text())
        settings['hooks']['UserPromptSubmit'][0]['hooks'].append(
            {'type': 'command', 'command': '/opt/relay/connector/old/relay-connector --state /elsewhere inbox-hint'})
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

    def test_15_claude_unparsable_files_fail_before_the_binary_unit_or_service_change(self):
        # A release change with service management on: past the preflight, this run would install
        # new bytes, rewrite the unit, restart the service and rewrite the receipt.
        self.assertTrue(self.receipt.exists())
        upgrade = self.root/'connector-bin-0.8.0-upgrade'
        upgrade.write_bytes(self.claude_artifact.read_bytes()+b'# upgrade release fixture\n')
        upgrade.chmod(0o755)
        values = {**self.pin(upgrade, '0.8.0'), 'relay_connector_manage_service': True,
                  'relay_connector_claude_messaging': 'present'}
        dry_run = "Dry-run the Relay inbox hook merge in the user's Claude Code settings"
        shape = 'Require the shape Claude Code keeps its configuration in'
        shape_message = 'must be a JSON object whose mcpServers, and mcpServers.relay, are objects'
        cases = [
            (self.settings, '{"model": "opus",', dry_run, 'is not valid JSON'),
            (self.settings, '{"hooks": {"UserPromptSubmit": [{"hooks": {}}]}}', dry_run,
             "is not an object with a 'hooks' array"),
            # ~/.claude.json is only read and never displayed: role() asserts its API key stays out.
            (self.claude_json, json.dumps(USER_CLAUDE_CONFIG)[:-1], shape, "Expecting ',' delimiter"),
        ]
        # Parses, but not as the CLI keeps it: `claude mcp add-json` would only fail after the
        # binary and service changed, and `absent` cannot tell what is registered.
        cases += [(self.claude_json, content, shape, shape_message) for content in [
            'null', '[]', '"text"', json.dumps({**USER_CLAUDE_CONFIG, 'mcpServers': []}),
            json.dumps({**USER_CLAUDE_CONFIG, 'mcpServers': None}),
            json.dumps({**USER_CLAUDE_CONFIG, 'mcpServers': {'other': USER_MCP, 'relay': 'x'}})]]
        for path, content, task, message in cases:
            for state in ('present', 'absent'):
                with self.subTest(path=path.name, content=content[:40], state=state):
                    self.seed_claude()
                    path.write_text(content)
                    before = self.snapshot()
                    out = self.role({**values, 'relay_connector_claude_messaging': state}, success=False)
                    self.assertEqual(self.failed_task(out), task)
                    self.assertIn(message, out)
                    self.assertEqual((self.claude_calls, self.commands), ([], []))
                    # Only a planned MCP change starts the CLI, and only `--version` (read-only).
                    self.assertEqual(len(self.claude_starts), int(path == self.settings and state == 'present'))
                    self.assertEqual(self.snapshot(), before)  # Binary, unit, receipt and both Claude files.
                    self.assertFalse(Path(self.connector_path(upgrade)).parent.exists())

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
        self.assertEqual((self.claude_calls, self.claude_starts), ([], []))
        self.assertEqual(self.snapshot(), before)


    def test_17_claude_cli_must_be_runnable_by_the_user_and_only_when_needed(self):
        cli_task = 'Require a Claude Code CLI the user can run before changing the relay MCP server'
        if os.geteuid() != 0:  # Root may run any file with an x bit; its owner only with the owner's.
            unrunnable = self.root/'claude-not-for-its-owner'
            unrunnable.write_bytes((self.root/'commands/claude').read_bytes())
            unrunnable.chmod(0o655)
            self.seed_claude()
            before = self.snapshot()
            out = self.role({**self.present, 'relay_connector_claude_bin': str(unrunnable)}, success=False)
            self.assertEqual(self.failed_task(out), cli_task)
            self.assertEqual(self.claude_calls, [])
            self.assertEqual(self.snapshot(), before)
        # An x bit is not enough: a CLI whose interpreter the user's environment cannot find (the npm
        # CLI's `#!/usr/bin/env node` under sudo's secure_path) fails before the release changes.
        no_node = self.root/'claude-without-its-interpreter'
        no_node.write_text('#!/usr/bin/env relay-test-missing-node\n')
        no_node.chmod(0o755)
        self.seed_claude()
        before = self.snapshot()
        out = self.role({**self.present, 'relay_connector_claude_bin': str(no_node)}, success=False)
        self.assertEqual(self.failed_task(out), cli_task)
        self.assertIn(f'`{no_node} --version` failed', out)
        self.assertIn('relay-test-missing-node', out)
        self.assertEqual(self.claude_calls, [])
        self.assertEqual(self.snapshot(), before)
        # `absent` with a registered server but no CLI fails before the hook is touched.
        self.seed_claude()
        self.role(self.present)
        before = self.snapshot()
        without_cli = {**self.absent, 'relay_connector_claude_bin': str(self.root/'missing/claude')}
        out = self.role(without_cli, success=False)
        self.assertEqual(self.failed_task(out), cli_task)
        self.assertIn('needed to remove the relay MCP server', out)
        self.assertEqual(self.snapshot(), before)
        # With nothing registered, `absent` needs no CLI and still removes the hook.
        config = json.loads(self.claude_json.read_text())
        del config['mcpServers']['relay']
        self.claude_json.write_text(json.dumps(config))
        self.role(without_cli)
        self.assertEqual(self.claude_calls, [])
        self.assertEqual(json.loads(self.settings.read_text()), USER_SETTINGS)

    def test_18_claude_failed_mcp_change_restores_the_previous_server_and_leaves_the_hook(self):
        stop = 'Stop after the failed Claude Code messaging change'
        stale = {'type': 'stdio', 'command': '/opt/relay/connector/old/relay-connector',
                 'args': ['--state', str(self.identity), 'mcp'], 'env': {'RELAY_TOKEN': 'fixture_relay_env_never_log'}}
        ops = lambda: [call['args'][1] for call in self.claude_calls]
        servers = lambda: json.loads(self.claude_json.read_text())['mcpServers']
        try:
            # Replacing: the add fails after the remove, so the user's definition goes back whole.
            self.seed_claude()
            config = json.loads(self.claude_json.read_text())
            config['mcpServers']['relay'] = stale
            self.claude_json.write_text(json.dumps(config))
            settings = self.settings.read_bytes()
            self.claude_fail.write_text(json.dumps({'add-json': 1}))
            out = self.role(self.present, success=False)
            self.assertNotIn('fixture_relay_env_never_log', out)
            self.assertEqual(self.failed_task(out), stop)
            self.assertIn('injected failure: add-json', out)
            self.assertIn('The relay MCP server was put back as it was before this run.', out)
            self.assertEqual(ops(), ['remove', 'add-json', 'add-json'])
            self.assertEqual(servers(), {'other': USER_MCP, 'relay': stale})
            self.assertEqual(self.settings.read_bytes(), settings)
            self.role(self.present)  # The failure is gone: the rerun converges both.
            self.assertEqual(ops(), ['remove', 'add-json'])
            self.assertEqual(servers()['relay'], self.expected_mcp(self.claude_artifact))
            self.assertEqual(self.relay_hooks(), [self.expected_hook(self.claude_artifact)])
            # A first registration that fails: nothing to restore, nothing registered, no hook.
            self.seed_claude()
            settings = self.settings.read_bytes()
            self.claude_fail.write_text(json.dumps({'add-json': 1}))
            out = self.role(self.present, success=False)
            self.assertEqual(self.failed_task(out), stop)
            self.assertIn('No MCP change had been made, so nothing needed undoing.', out)
            self.assertEqual(ops(), ['add-json'])
            self.assertEqual(servers(), {'other': USER_MCP})
            self.assertEqual(self.settings.read_bytes(), settings)
            self.role(self.present)
            self.assertEqual(servers()['relay'], self.expected_mcp(self.claude_artifact))
            # Removing: a failed remove keeps both the server and the hook.
            settings = self.settings.read_bytes()
            self.claude_fail.write_text(json.dumps({'remove': 1}))
            out = self.role(self.absent, success=False)
            self.assertEqual(self.failed_task(out), stop)
            self.assertIn('No MCP change had been made, so nothing needed undoing.', out)
            self.assertEqual(ops(), ['remove'])
            self.assertEqual(servers()['relay'], self.expected_mcp(self.claude_artifact))
            self.assertEqual(self.settings.read_bytes(), settings)
            self.role(self.absent)
            self.assertEqual(servers(), {'other': USER_MCP})
            self.assertEqual(json.loads(self.settings.read_text()), USER_SETTINGS)
            # The restore can fail too: the run says so, and the next run still converges.
            self.seed_claude()
            config = json.loads(self.claude_json.read_text())
            config['mcpServers']['relay'] = stale
            self.claude_json.write_text(json.dumps(config))
            settings = self.settings.read_bytes()
            self.claude_fail.write_text(json.dumps({'add-json': 2}))
            out = self.role(self.present, success=False)
            self.assertNotIn('fixture_relay_env_never_log', out)
            self.assertEqual(self.failed_task(out), stop)
            self.assertIn('Putting the relay MCP server back failed too', out)
            self.assertEqual(ops(), ['remove', 'add-json', 'add-json'])
            self.assertEqual(servers(), {'other': USER_MCP})
            self.assertEqual(self.settings.read_bytes(), settings)
            self.role(self.present)
            self.assertEqual(servers()['relay'], self.expected_mcp(self.claude_artifact))
            self.assertEqual(self.relay_hooks(), [self.expected_hook(self.claude_artifact)])
        finally:
            self.claude_fail.unlink(missing_ok=True)

    def test_19_claude_unwritable_settings_dir_fails_early_or_puts_the_mcp_server_back(self):
        if os.geteuid() == 0:
            self.skipTest('root writes through directory permissions')
        claude_dir = self.settings.parent
        repinned = self.root/'connector-bin-0.8.0-repin'
        repinned.write_bytes(self.claude_artifact.read_bytes()+b'# repin release fixture\n')
        repinned.chmod(0o755)
        repin = {**self.pin(repinned, '0.8.0'), 'relay_connector_claude_messaging': 'present'}
        hook_task = "Converge the Relay inbox hook in the user's Claude Code settings"
        ops = lambda: [call['args'][1] for call in self.claude_calls]
        servers = lambda: json.loads(self.claude_json.read_text())['mcpServers']
        try:
            self.seed_claude()
            self.role(self.present)  # Registered and hooked at the first 0.8.0 build.
            settings = self.settings.read_bytes()
            # Predictable: a readable but read-only ~/.claude fails the preflight, before any change.
            claude_dir.chmod(0o555)
            for values in (repin, self.absent):
                out = self.role(values, success=False)
                self.assertEqual(self.failed_task(out), "Dry-run the Relay inbox hook merge in the user's Claude Code settings")
                self.assertIn('is not writable for this user', out)
                self.assertEqual(self.claude_calls, [])
                self.assertEqual(servers()['relay'], self.expected_mcp(self.claude_artifact))
                self.assertEqual(self.settings.read_bytes(), settings)
            self.assertFalse(Path(self.connector_path(repinned)).parent.exists())
            claude_dir.chmod(0o700)
            # Late: ~/.claude turns read-only during the MCP change, so the hook write fails after
            # it. The MCP server is put back as it was, and the run says why it stopped.
            for values, expected in [(repin, ['remove', 'add-json', 'remove', 'add-json']),
                                     (self.absent, ['remove', 'add-json'])]:
                self.claude_fail.write_text(json.dumps({'lock-claude-dir': 1}))
                out = self.role(values, success=False)
                claude_dir.chmod(0o700)
                self.assertEqual(self.failed_task(out), 'Stop after the failed Claude Code messaging change')
                self.assertIn(f"failed at '{hook_task}'", out)
                self.assertIn('is not writable for this user', out)
                self.assertIn('The relay MCP server was put back as it was before this run.', out)
                self.assertEqual(ops(), expected)
                self.assertEqual(servers()['relay'], self.expected_mcp(self.claude_artifact))
                self.assertEqual(self.settings.read_bytes(), settings)
            # Writable again: both converge.
            self.role(repin)
            self.assertEqual(servers()['relay'], self.expected_mcp(repinned))
            self.assertEqual(self.relay_hooks(), [self.expected_hook(repinned)])
            self.role(self.absent)
            self.assertEqual(servers(), {'other': USER_MCP})
            self.assertEqual(json.loads(self.settings.read_text()), USER_SETTINGS)
        finally:
            claude_dir.chmod(0o700)
            self.claude_fail.unlink(missing_ok=True)

    def test_20_claude_converged_settings_never_block_a_run(self):
        # Settings the hook helper could not rewrite faithfully (a POSIX ACL on the file, a default
        # ACL on ~/.claude) block only a run that has to rewrite them, never one with nothing to do.
        self.seed_claude()
        self.role(self.present)
        claude_dir = self.settings.parent
        try:
            set_acl(self, self.settings)
            set_acl(self, claude_dir, 'system.posix_acl_default')
            self.assertRegex(self.role(self.present), r'changed=0\s')
            self.assertEqual(self.relay_hooks(), [self.expected_hook(self.claude_artifact)])
            repinned = self.root/'connector-bin-0.8.0-acl'
            repinned.write_bytes(self.claude_artifact.read_bytes()+b'# acl release fixture\n')
            repinned.chmod(0o755)
            before = self.snapshot()
            out = self.role({**self.pin(repinned, '0.8.0'), 'relay_connector_claude_messaging': 'present'},
                            success=False)
            self.assertEqual(self.failed_task(out), "Dry-run the Relay inbox hook merge in the user's Claude Code settings")
            self.assertIn('POSIX ACL', out)
            self.assertEqual((self.claude_calls, self.snapshot()), ([], before))
            self.assertFalse(Path(self.connector_path(repinned)).parent.exists())
        finally:
            for path, name in ((self.settings, 'system.posix_acl_access'), (claude_dir, 'system.posix_acl_default')):
                with contextlib.suppress(OSError):
                    os.removexattr(path, name)

    def seed_hooks(self, *entries, content=None):
        """The user's own settings plus `entries`, each in its own UserPromptSubmit group."""
        self.seed_claude()
        settings = json.loads(json.dumps(USER_SETTINGS))
        settings['hooks']['UserPromptSubmit'] += [{'hooks': [entry]} for entry in entries]
        self.settings.write_text(content if content is not None else json.dumps(settings, indent=2))

    def release(self, version):
        artifact = self.root/f'connector-bin-{version}'
        artifact.write_text(Path(self.values['relay_connector_source']).read_text().replace('0.3.5', version))
        artifact.chmod(0o755)
        return artifact, self.pin(artifact, version)

    def test_21_a_0_9_connector_is_refused_while_the_old_inbox_hint_hook_is_installed(self):
        refuse = 'Refuse a connector without inbox-hint while the old hook may still run it'
        old_hook = self.expected_hook(self.claude_artifact)  # The literal entry `present` wrote for 0.8.0.
        nine, pin9 = self.release('0.9.0')
        ten, pin10 = self.release('0.10.0')
        cases = [  # (hook entries, settings text, values, reason), each refused before any write.
            ((old_hook,), None, pin9, 'the hook is still there'),
            ((old_hook, SKILL_HOOK), None, pin9, 'the hook is still there'),
            ((SKILL_HOOK, old_hook), None, pin10, 'the hook is still there'),
            # Converging `absent` in the same run as the repin is not the order either.
            ((old_hook,), None, {**pin9, 'relay_connector_claude_messaging': 'absent'}, 'the hook is still there'),
            ((), '{"model": "opus",', pin9, 'the file could not be checked ('),
        ]
        for entries, content, values, reason in cases:
            with self.subTest(entries=len(entries), content=content, version=values['relay_connector_release']['version']):
                self.seed_hooks(*entries, content=content)
                before = self.snapshot()
                out = self.role(values, success=False)
                self.assertEqual(self.failed_task(out), refuse)
                self.assertIn(reason, out)
                self.assertIn('Converge this host with relay_connector_claude_messaging: absent', out)
                self.assertEqual((self.claude_calls, self.claude_starts), ([], []))
                self.assertEqual(self.snapshot(), before)
                for artifact in (nine, ten):
                    self.assertFalse(Path(self.connector_path(artifact)).parent.exists())
        # `present` cannot ask for the hook on such a pin either.
        self.seed_hooks()
        out = self.role({**pin9, 'relay_connector_claude_messaging': 'present'}, success=False)
        self.assertEqual(self.failed_task(out), 'Refuse Claude Code messaging on a connector that no longer serves it')
        # A pre-0.9.0 pin is not refused with the old hook in place: that is the pin `absent` converges with.
        self.seed_hooks(old_hook)
        self.role(self.claude_values)
        self.assertEqual(self.relay_hooks(), [old_hook])
        # The relay-bus skill's hook alone, or no settings at all, never trips it.
        self.seed_hooks(SKILL_HOOK)
        settings = self.settings.read_bytes()
        self.role(pin9)
        self.assertTrue(Path(self.connector_path(nine)).exists())
        self.assertEqual(self.settings.read_bytes(), settings)
        self.settings.unlink()
        self.role(pin10)
        self.assertTrue(Path(self.connector_path(ten)).exists())
        self.assertFalse(self.settings.exists())

    def test_22_a_settings_lock_the_hook_could_not_take_fails_the_preflight_before_any_change(self):
        # A release change with service management on: past the preflight, this run would install
        # new bytes, rewrite the unit, restart the service and rewrite the receipt.
        upgrade = self.root/'connector-bin-0.8.0-lock'
        upgrade.write_bytes(self.claude_artifact.read_bytes()+b'# lock release fixture\n')
        upgrade.chmod(0o755)
        dry_run = "Dry-run the Relay inbox hook merge in the user's Claude Code settings"
        lock = self.settings.parent/'.settings.json.lock'
        elsewhere = self.root/'elsewhere.lock'
        elsewhere.write_text('')
        kinds = ['directory', 'symlink'] + ([] if os.geteuid() == 0 else ['read-only'])
        try:
            for kind in kinds:
                for state in ('present', 'absent'):
                    with self.subTest(kind=kind, state=state):
                        # `absent` has the old hook to remove; `present` has it to add.
                        self.seed_hooks(*([self.expected_hook(self.claude_artifact)] if state == 'absent' else []))
                        if lock.is_dir() and not lock.is_symlink():
                            lock.rmdir()
                        elif lock.exists() or lock.is_symlink():
                            lock.unlink()
                        if kind == 'directory':
                            lock.mkdir()
                        elif kind == 'symlink':
                            lock.symlink_to(elsewhere)
                        else:
                            lock.write_text('')
                            lock.chmod(0o400)
                        values = {**self.pin(upgrade, '0.8.0'), 'relay_connector_manage_service': True,
                                  'relay_connector_claude_messaging': state}
                        before = self.snapshot()
                        for check in (False, True):
                            out = self.role(values, success=False, check=check)
                            self.assertEqual(self.failed_task(out), dry_run)
                            self.assertIn(f'the lock {lock} is not', out)
                            self.assertEqual((self.claude_calls, self.commands), ([], []))
                            self.assertEqual(self.snapshot(), before)
                            self.assertFalse(Path(self.connector_path(upgrade)).parent.exists())
                        self.assertEqual(elsewhere.read_text(), '')
        finally:
            if lock.is_dir() and not lock.is_symlink():
                lock.rmdir()
            elif lock.exists() or lock.is_symlink():
                lock.unlink()

    def test_23_a_fifo_in_the_users_claude_files_fails_the_preflights_at_once(self):
        # A FIFO without a writer would block a plain open() (the helper's, or slurp's) forever.
        nine = self.root/'connector-bin-0.9.0-fifo'  # Bytes no earlier test installed.
        nine.write_text(Path(self.values['relay_connector_source']).read_text().replace('0.3.5', '0.9.0')
                        + '# fifo release fixture\n')
        nine.chmod(0o755)
        pin9 = self.pin(nine, '0.9.0')
        fifo_task = 'Require a regular file where Claude Code keeps its configuration'
        try:
            # settings.json: the 0.9.0 preflight cannot tell, so it refuses, at once.
            self.seed_claude()
            self.settings.unlink()
            os.mkfifo(self.settings)
            before = self.snapshot()
            out = self.role(pin9, success=False)
            self.assertEqual(self.failed_task(out), 'Refuse a connector without inbox-hint while the old hook may still run it')
            self.assertIn('the file could not be checked (', out)
            self.assertIn('it is not a regular file (a FIFO', out)
            self.assertEqual(self.snapshot(), before)
            self.assertFalse(Path(self.connector_path(nine)).parent.exists())
            # ~/.claude.json, read by slurp in the Claude Code preflight: refused before it is opened.
            self.settings.unlink()  # Writing the FIFO back to a file would block on it as well.
            self.seed_claude()
            self.claude_json.unlink()
            os.mkfifo(self.claude_json)
            before = self.snapshot()
            for check in (False, True):
                out = self.role(self.absent, success=False, check=check)
                self.assertEqual(self.failed_task(out), fifo_task)
                self.assertIn('.claude.json, or what it links to, is not a regular file', out)
                self.assertEqual(self.snapshot(), before)
        finally:
            for path in (self.settings, self.claude_json):
                if path.is_fifo():
                    path.unlink()


def set_acl(test, path, name='system.posix_acl_access'):
    """Give `path` a minimal POSIX ACL, or skip `test` where the filesystem has none."""
    # Version 2 of the Linux xattr format: user::rw- user:<self>:r-- group::r-- mask::r-- other::---.
    undefined = 0xFFFFFFFF
    acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', tag, perm, ident) for tag, perm, ident in [
        (0x01, 6, undefined), (0x02, 4, os.geteuid()), (0x04, 4, undefined), (0x10, 4, undefined),
        (0x20, 0, undefined)])
    try:
        os.setxattr(path, name, acl)
    except OSError as error:
        test.skipTest(f'no POSIX ACLs on this filesystem: {error}')


RELAY_HINT = ('/opt/relay/connector/' + 'a' * 64 + '/relay-connector'
              ' --state /home/c4/.config/relay-connector/connector.json inbox-hint')
RELAY_ENTRY = {'type': 'command', 'command': RELAY_HINT, 'timeout': 2}
OLD_RELAY = {'type': 'command', 'command': '/opt/relay/connector/old/relay-connector --state /s inbox-hint'}
# Settings each state must rewrite (an older Relay entry), and settings already converged for it.
STALE = {'model': 'opus', 'hooks': {'UserPromptSubmit': [{'hooks': [OLD_RELAY]}]}}
CONVERGED = {'present': {'model': 'opus', 'hooks': {'UserPromptSubmit': [{'hooks': [RELAY_ENTRY]}]}},
             'absent': {'model': 'opus'}}


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

    def run_in_process(self, before_replace, state='present'):
        """main() in this process, with `before_replace` run between its read and its rename."""
        out, err = io.StringIO(), io.StringIO()
        argv = ['--settings', str(self.settings), '--state', state, '--command', RELAY_HINT, '--timeout', '2']
        with mock.patch.object(hook, '_before_replace', before_replace), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hook.main(argv)
        return code, out.getvalue(), err.getvalue()

    def write(self, data, mode=0o644):
        self.settings.parent.mkdir(mode=0o700, exist_ok=True)
        self.settings.write_text(data if isinstance(data, str) else json.dumps(data, indent=2))
        self.settings.chmod(mode)

    def assert_refused(self, message, path=None):
        """Both states fail with `message`, and neither touches the file or leaves a temp file."""
        path = path or self.settings
        before = path.read_bytes()
        for state in ('present', 'absent'):
            for check in (False, True):
                result = self.run_hook(state=state, check=check)
                self.assertEqual((result.returncode, result.stdout), (1, ''), (state, check, result.stderr))
                self.assertIn(message, result.stderr)
                self.assertIn('nothing was written', result.stderr)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual([p.name for p in path.parent.iterdir() if p.name.endswith('.tmp')], [])

    def test_matches_only_the_exact_command_the_role_writes(self):
        for command in [RELAY_HINT, '/x/relay-connector --state /s inbox-hint']:
            self.assertTrue(hook.is_relay_inbox_hint({'type': 'command', 'command': command}), command)
        # Shell syntax inside or after the state path: shlex keeps these as four words, but they are
        # not the literal form `present` writes, so they stay the user's.
        for command in ['/x/relay-connector --state /s;echo inbox-hint',
                        '/x/relay-connector --state /s&&echo inbox-hint',
                        '/x/relay-connector --state /s||echo inbox-hint',
                        '/x/relay-connector --state /s|tee inbox-hint',
                        '/x/relay-connector --state /s& inbox-hint',
                        '/x/relay-connector --state /$(id) inbox-hint',
                        '/x/relay-connector --state /`id` inbox-hint',
                        '/x/relay-connector --state /${HOME} inbox-hint',
                        '/x/relay-connector --state /s>/tmp/out inbox-hint',
                        '/x/relay-connector --state /s<in inbox-hint',
                        '/x/relay-connector --state /s inbox-hint>/tmp/out',
                        '/x/relay-connector --state /s inbox-hint;echo',
                        '/x/relay-connector --state /s/* inbox-hint',
                        "/x/relay-connector --state '/a b' inbox-hint",
                        "'/x/relay-connector' --state /s inbox-hint",
                        '/x/relay-connector --state "/s" inbox-hint',
                        '/x/relay-connector  --state /s inbox-hint',
                        ' /x/relay-connector --state /s inbox-hint',
                        '/x/relay-connector --state /s inbox-hint ',
                        '/x/relay-connector --state /s\ninbox-hint',
                        '/x/relay-connector --state /s\\ inbox-hint',
                        ]:
            self.assertFalse(hook.is_relay_inbox_hint({'type': 'command', 'command': command}), command)
        for command in ['echo relay-connector inbox-hint',
                        'relay-connector inbox-hint',
                        'relay-connector --state /s inbox-hint',  # Not an absolute binary path.
                        '/x/relay-connector --state /s inbox-hint && /usr/local/bin/user-action',
                        '/x/relay-connector --state /s inbox-hint; notify-send done',
                        '/x/relay-connector --state /s inbox-hint | tee /tmp/log',
                        'exec /x/relay-connector --state /s inbox-hint',
                        '/usr/bin/env /x/relay-connector --state /s inbox-hint',
                        '/x/relay-connector inbox-hint',
                        '/x/relay-connector --state relative/state inbox-hint',
                        '/x/relay-connector --state /s serve',
                        '/x/relay-connector --state /s inbox-hint --verbose',
                        '/x/relay-connector-old --state /s inbox-hint',
                        '"/x/relay-connector --state /s inbox-hint',  # Does not parse: kept.
                        ]:
            self.assertFalse(hook.is_relay_inbox_hint({'type': 'command', 'command': command}), command)
        for entry in [{'type': 'prompt', 'command': RELAY_HINT}, {'command': RELAY_HINT}, {'type': 'command'},
                      {'type': 'command', 'command': 7}, RELAY_HINT, None]:
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

    def test_present_replaces_only_exact_relay_entries_and_keeps_everything_else(self):
        user_hook = {'type': 'command', 'command': '/usr/local/bin/user-prompt-hook'}
        mentions = {'hooks': [{'type': 'command', 'command': 'echo relay-connector inbox-hint'},
                              {'type': 'command', 'command': OLD_RELAY['command'] + ' && /usr/local/bin/user-action'},
                              {'type': 'command', 'command': '/opt/relay/connector/x/relay-connector --state /s serve'}]}
        other_event = {'matcher': 'Bash', 'hooks': [OLD_RELAY]}
        settings = {
            'model': 'opus', 'note': 'café ☕', 'env': {'A': '1'},
            'hooks': {
                'UserPromptSubmit': [
                    {'hooks': [user_hook, OLD_RELAY]},
                    {'matcher': '', 'hooks': [{**OLD_RELAY, 'timeout': 9}]},
                    {'hooks': []},
                    mentions,
                ],
                'PreToolUse': [other_event],
            },
        }
        self.write(settings, mode=0o600)
        result = self.run_hook()
        self.assertEqual((result.returncode, result.stdout), (0, 'changed\n'), result.stderr)
        written = json.loads(self.settings.read_text())
        self.assertEqual(written, {**settings, 'hooks': {
            'UserPromptSubmit': [{'hooks': [user_hook]}, {'hooks': []}, mentions, {'hooks': [RELAY_ENTRY]}],
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

    def test_rewrite_keeps_a_supplementary_group(self):
        groups = [gid for gid in os.getgroups() if gid != os.getegid()]
        if not groups:
            self.skipTest('the test user has no supplementary group')
        self.write({'model': 'opus'}, mode=0o640)
        os.chown(self.settings, -1, groups[0])
        result = self.run_hook()
        self.assertEqual((result.returncode, result.stdout), (0, 'changed\n'), result.stderr)
        status = self.settings.stat()
        self.assertEqual((status.st_uid, status.st_gid, status.st_mode & 0o7777), (os.geteuid(), groups[0], 0o640))
        self.assertIn('hooks', json.loads(self.settings.read_text()))

    def assert_left_alone(self, mode=0o644, **patches):
        """A file already converged for either state is 'unchanged', untouched, whatever its owner
        or ACLs: those only matter to a rewrite."""
        for state, data in CONVERGED.items():
            self.settings.write_text(json.dumps(data, indent=2))  # In place: the inode keeps its ACL.
            self.settings.chmod(mode)
            before = self.settings.read_bytes()
            for check in (False, True):
                code, out, err = self.main_in_process(state, check, **patches)
                self.assertEqual((code, out), (0, 'unchanged\n'), (state, check, err))
                self.assertEqual(self.settings.read_bytes(), before)

    def main_in_process(self, state='present', check=False, geteuid=None):
        """main() in this process, optionally as if this user had another uid."""
        out, err = io.StringIO(), io.StringIO()
        argv = ['--settings', str(self.settings), '--state', state, '--command', RELAY_HINT, '--timeout', '2']
        with contextlib.ExitStack() as stack:
            if geteuid is not None:
                stack.enter_context(mock.patch.object(hook.os, 'geteuid', return_value=geteuid))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = hook.main(argv + (['--check'] if check else []))
        return code, out.getvalue(), err.getvalue()

    def test_a_target_owned_by_someone_else_is_refused_only_when_it_would_change(self):
        # As this script sees it, the file belongs to another uid (works as root and as a user).
        other = os.geteuid() + 1
        self.write(STALE)
        before = self.settings.read_bytes()
        for state in ('present', 'absent'):
            for check in (False, True):
                code, out, err = self.main_in_process(state, check, geteuid=other)
                self.assertEqual((code, out), (1, ''), (state, check))
                self.assertIn(f'it is owned by uid {os.geteuid()}, not by this user (uid {other})', err)
                self.assertIn('nothing was written', err)
                self.assertEqual(self.settings.read_bytes(), before)
        self.assert_left_alone(geteuid=other)

    def set_acl(self, path, name='system.posix_acl_access'):
        set_acl(self, path, name)

    def test_a_file_with_a_posix_acl_is_refused_only_when_it_would_change(self):
        self.write(STALE, mode=0o640)
        self.set_acl(self.settings)
        self.assert_refused('POSIX ACL')
        self.assert_left_alone(mode=0o640)
        self.assertIn('system.posix_acl_access', os.listxattr(self.settings))

    def test_a_directory_default_acl_is_refused_only_when_it_would_change(self):
        self.write(STALE)
        self.set_acl(self.settings.parent, 'system.posix_acl_default')
        self.assert_refused('has a default POSIX ACL')
        self.assert_left_alone()
        # A missing ~/.claude would be created under the default ACL of its parent: refused too.
        fresh = self.home/'fresh'
        fresh.mkdir()
        self.set_acl(fresh, 'system.posix_acl_default')
        self.settings = fresh/'.claude/settings.json'
        for check in (True, False):
            result = self.run_hook(check=check)
            self.assertEqual((result.returncode, result.stdout), (1, ''), result.stderr)
            self.assertIn(f'{fresh} has a default POSIX ACL', result.stderr)
            self.assertEqual(list(fresh.iterdir()), [])

    def test_a_temporary_file_that_inherits_an_acl_is_never_published(self):
        self.write({'model': 'opus'})
        before = self.settings.read_bytes()
        self.set_acl(self.settings.parent, 'system.posix_acl_default')
        probe = self.settings.parent/'probe'
        probe.write_text('')
        inherited = 'system.posix_acl_access' in os.listxattr(probe)
        probe.unlink()
        if not inherited:
            self.skipTest('this filesystem does not apply default ACLs to new files')
        # As if the default ACL appeared after the directory check: the temp file check still holds.
        with mock.patch.object(hook, 'check_directory', lambda directory: None):
            code, out, err = self.run_in_process(lambda: None)
        self.assertEqual((code, out), (1, ''))
        self.assertIn('inherits a POSIX ACL from its default ACL', err)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.settings.parent.iterdir()), ['.settings.json.lock', 'settings.json'])

    def test_a_concurrent_chmod_forces_a_fresh_pass_that_keeps_the_new_mode(self):
        self.write({'model': 'opus'}, mode=0o644)
        calls = []

        def chmod_once():
            calls.append(1)
            if len(calls) == 1:
                self.settings.chmod(0o600)  # Same bytes, size and mtime: only ctime and mode move.
        code, out, err = self.run_in_process(chmod_once)
        self.assertEqual((code, out), (0, 'changed\n'), err)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.settings.read_text())['hooks'], {'UserPromptSubmit': [{'hooks': [RELAY_ENTRY]}]})

    def test_a_concurrent_acl_forces_a_fresh_pass_that_refuses_it(self):
        self.write({'model': 'opus'}, mode=0o640)
        before = self.settings.read_bytes()
        probe = self.home/'probe'
        probe.write_text('')
        self.set_acl(probe)  # Skips where ACLs are unsupported.
        code, out, err = self.run_in_process(lambda: self.set_acl(self.settings))
        self.assertEqual((code, out), (1, ''))
        self.assertIn('carries a POSIX ACL', err)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertIn('system.posix_acl_access', os.listxattr(self.settings))
        self.assertEqual(sorted(p.name for p in self.settings.parent.iterdir()), ['.settings.json.lock', 'settings.json'])

    def test_a_change_this_user_cannot_write_is_refused_already_under_check(self):
        if os.geteuid() == 0:
            self.skipTest('root writes through permission bits')
        hooked = {'model': 'opus', 'hooks': {'UserPromptSubmit': [{'hooks': [RELAY_ENTRY]}]}}
        # A readable but read-only ~/.claude, with a change to make for each state.
        for state, data in [('present', {'model': 'opus'}), ('absent', hooked)]:
            self.write(data)
            before = self.settings.read_bytes()
            self.settings.parent.chmod(0o555)
            try:
                for check in (True, False):
                    result = self.run_hook(state=state, check=check)
                    self.assertEqual((result.returncode, result.stdout), (1, ''), (state, check))
                    self.assertIn(f'{self.settings.parent} is not writable for this user', result.stderr)
                    self.assertEqual(self.settings.read_bytes(), before)
            finally:
                self.settings.parent.chmod(0o700)
        # A read-only file in a writable directory.
        self.write({'model': 'opus'}, mode=0o444)
        result = self.run_hook(check=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('it is not writable for this user', result.stderr)
        # With nothing to change there is nothing to write, so nothing to refuse.
        self.settings.chmod(0o644)
        self.write(hooked)
        self.settings.parent.chmod(0o555)
        try:
            self.assertEqual(self.run_hook().stdout, 'unchanged\n')
        finally:
            self.settings.parent.chmod(0o700)

    def test_a_concurrent_edit_is_merged_not_overwritten(self):
        self.write({'model': 'opus'})
        edits = []

        def user_edits_once():
            if not edits:
                edits.append(1)
                self.settings.write_text(json.dumps({'model': 'opus', 'theme': 'dark'}))
        code, out, err = self.run_in_process(user_edits_once)
        self.assertEqual((code, out), (0, 'changed\n'), err)
        self.assertEqual(json.loads(self.settings.read_text()),
                         {'model': 'opus', 'theme': 'dark', 'hooks': {'UserPromptSubmit': [{'hooks': [RELAY_ENTRY]}]}})
        # A file created between the read and the write is merged too, not replaced.
        self.settings.unlink()
        edits.clear()
        code, out, err = self.run_in_process(user_edits_once)
        self.assertEqual((code, out), (0, 'changed\n'), err)
        self.assertEqual(json.loads(self.settings.read_text())['theme'], 'dark')
        self.assertEqual(sorted(p.name for p in self.settings.parent.iterdir()), ['.settings.json.lock', 'settings.json'])

    def test_a_file_that_keeps_changing_fails_and_keeps_the_last_edit(self):
        self.write({'model': 'opus'})
        edits = []

        def user_edits_every_time():
            edits.append(1)
            self.settings.write_text(json.dumps({'model': 'opus', 'edit': len(edits)}))
        code, out, err = self.run_in_process(user_edits_every_time)
        self.assertEqual((code, out), (1, ''))
        self.assertIn('changed while being updated, 3 times in a row', err)
        self.assertIn('nothing was written', err)
        self.assertEqual(json.loads(self.settings.read_text()), {'model': 'opus', 'edit': 3})
        self.assertEqual(sorted(p.name for p in self.settings.parent.iterdir()), ['.settings.json.lock', 'settings.json'])

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
        group = "is not an object with a 'hooks' array"
        entry = "hooks[0]' is not an object"
        no_type, no_command = "has no string 'type'", "is a command hook without a string 'command'"
        timeout = "timeout' is not a positive number of seconds"
        cases = [('{"model": "opus",', 'not valid JSON'), ('', 'not valid JSON'), (b'\xff\xfe{}', 'not valid JSON'),
                 # JSON null is a document, not a missing file: never replaced with fresh settings.
                 ('null', 'top level is not a JSON object'), ('[]', 'top level is not a JSON object'),
                 ('"text"', 'top level is not a JSON object'),
                 ('{"hooks": []}', "'hooks' is not a JSON object"), ('{"hooks": null}', "'hooks' is not a JSON object"),
                 ('{"hooks": {"UserPromptSubmit": {}}}', 'is not a JSON array')]
        cases += [('{"hooks": {"UserPromptSubmit": %s}}' % groups, message) for groups, message in [
            ('[{"hooks": {}}]', group), ('[{}]', group), ('[1]', group), ('[{"matcher": "", "hooks": "x"}]', group),
            ('[{"hooks": [1]}]', entry), ('[{"hooks": [null]}]', entry),
            ('[{"matcher": 1, "hooks": []}]', "matcher' is not a string"),
            ('[{"hooks": [{}]}]', no_type), ('[{"hooks": [{"type": 1}]}]', no_type),
            ('[{"hooks": [{"type": ""}]}]', no_type),
            ('[{"hooks": [{"type": "command"}]}]', no_command),
            ('[{"hooks": [{"type": "command", "command": 1}]}]', no_command),
            ('[{"hooks": [{"type": "command", "command": ""}]}]', no_command),
            *(('[{"hooks": [{"type": "command", "command": "x", "timeout": %s}]}]' % value, timeout)
              for value in ('0', '-1', '-0.5', '0.0', '"5"', 'true', 'null', 'NaN', 'Infinity', '1e999'))]]
        for content, message in cases:
            with self.subTest(content=content):
                self.settings.parent.mkdir(mode=0o700, exist_ok=True)
                if isinstance(content, bytes): self.settings.write_bytes(content)
                else: self.settings.write_text(content)
                self.assert_refused(message)

    def test_a_users_fractional_hook_timeout_is_valid_and_kept(self):
        # Claude Code takes any positive number of seconds; the user's 1.5 must not block the role.
        user_hook = {'type': 'command', 'command': '/usr/local/bin/user-prompt-hook', 'timeout': 1.5}
        self.write({'hooks': {'UserPromptSubmit': [{'hooks': [user_hook]}]}})
        result = self.run_hook()
        self.assertEqual((result.returncode, result.stdout), (0, 'changed\n'), result.stderr)
        self.assertEqual(json.loads(self.settings.read_text())['hooks']['UserPromptSubmit'],
                         [{'hooks': [user_hook]}, {'hooks': [RELAY_ENTRY]}])
        self.assertEqual(self.run_hook(state='absent').stdout, 'changed\n')
        self.assertEqual(json.loads(self.settings.read_text())['hooks']['UserPromptSubmit'], [{'hooks': [user_hook]}])

    def test_present_refuses_a_command_it_would_not_recognise_on_the_next_run(self):
        for command in ['/usr/bin/true', '/opt/relay/connector/x/relay-connector --state /s serve',
                        'relay-connector --state /s inbox-hint', OLD_RELAY['command'] + ' && true',
                        '/x/relay-connector --state /s;echo inbox-hint', '/x/relay-connector --state /$(id) inbox-hint',
                        "/x/relay-connector --state '/a b' inbox-hint"]:
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

    def test_a_write_takes_the_lock_it_shares_with_the_skill_role_and_waits_for_it(self):
        import fcntl
        self.write({'model': 'opus'})
        lock = self.settings.parent / '.settings.json.lock'
        holder = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX)  # As claude_skill_hook.py holds it while it merges.
            out, err = io.StringIO(), io.StringIO()
            argv = ['--settings', str(self.settings), '--state', 'present', '--command', RELAY_HINT, '--timeout', '2']
            with mock.patch.object(hook, 'LOCK_WAIT', 0.3), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(hook.main(argv), 1)
            self.assertIn(f'another merge has held {lock}', err.getvalue())
            self.assertEqual(json.loads(self.settings.read_text()), {'model': 'opus'})
            self.assertEqual(self.run_hook(check=True).stdout, 'changed\n')  # A report takes no lock.
        finally:
            os.close(holder)
        self.assertEqual(self.run_hook().stdout, 'changed\n')
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_check_mode_refuses_a_lock_it_could_not_take_and_creates_none(self):
        self.write({'model': 'opus'})
        before = self.settings.read_bytes()
        lock = self.settings.parent / '.settings.json.lock'
        self.assertEqual(self.run_hook(check=True).stdout, 'changed\n')
        self.assertFalse(lock.exists())  # A missing lock is fine, and --check never creates it.
        elsewhere = self.home / 'elsewhere.lock'
        elsewhere.write_text('')
        cases = [(lock.mkdir, 'is not a regular file of this user'),
                 (lambda: lock.symlink_to(elsewhere), 'is not a regular file of this user')]
        if os.geteuid() != 0:  # Root reads and writes through permission bits.
            cases.append((lambda: (lock.write_text(''), lock.chmod(0o400)), 'is not readable and writable'))
        for make, message in cases:
            make()
            for check in (True, False):
                result = self.run_hook(check=check)
                self.assertEqual((result.returncode, result.stdout), (1, ''), result.stderr)
                self.assertIn(f'the lock {lock} {message}', result.stderr)
                self.assertEqual(self.settings.read_bytes(), before)
            if lock.is_dir() and not lock.is_symlink():
                lock.rmdir()
            else:
                lock.unlink()
        # With nothing to change there is no lock to take, so none is checked.
        self.write({'model': 'opus', 'hooks': {'UserPromptSubmit': [{'hooks': [RELAY_ENTRY]}]}})
        lock.mkdir()
        self.assertEqual(self.run_hook(check=True).stdout, 'unchanged\n')

    def test_a_fifo_settings_json_is_refused_at_once_never_waited_on(self):
        self.settings.parent.mkdir(mode=0o700)
        os.mkfifo(self.settings)
        for state, check in [('absent', True), ('absent', False), ('present', True), ('present', False)]:
            with self.subTest(state=state, check=check):
                result = self.run_hook(state=state, check=check)  # Its 30 s timeout fails a hang.
                self.assertEqual((result.returncode, result.stdout), (1, ''), result.stderr)
                self.assertIn('it is not a regular file (a FIFO', result.stderr)
                self.assertEqual([p.name for p in self.settings.parent.iterdir()], ['settings.json'])

    def test_the_0_9_preflight_finds_only_the_old_literal_hook(self):
        # tasks/legacy_hook_preflight.yml runs `--state absent --check` and refuses on anything but
        # `unchanged` with exit 0.
        self.assertFalse(hook.is_relay_inbox_hint(SKILL_HOOK))
        user = {'type': 'command', 'command': '/usr/local/bin/user-prompt-hook'}
        cases = [((RELAY_ENTRY,), 'changed\n'), ((SKILL_HOOK,), 'unchanged\n'), ((RELAY_ENTRY, SKILL_HOOK), 'changed\n'),
                 ((SKILL_HOOK, OLD_RELAY), 'changed\n'), ((user,), 'unchanged\n'), ((), 'unchanged\n')]
        for entries, expected in cases:
            with self.subTest(entries=entries):
                self.write({'model': 'opus', 'hooks': {'UserPromptSubmit': [{'hooks': [user, *entries]}]}})
                before = self.settings.read_bytes()
                result = self.run_hook(state='absent', check=True)
                self.assertEqual((result.returncode, result.stdout), (0, expected), result.stderr)
                self.assertEqual(self.settings.read_bytes(), before)
        self.settings.unlink()
        self.assertEqual(self.run_hook(state='absent', check=True).stdout, 'unchanged\n')

    def test_the_0_9_version_gates_compare_numerically(self):
        # The exact Jinja test validate.yml uses: `is version('0.9.0', '>=')` (and '<' for `present`).
        from ansible.plugins.test.core import version_compare
        for version, refused in [('0.8.0', False), ('0.8.1', False), ('0.8.9', False), ('0.8.10', False),
                                 ('0.9.0', True), ('0.9.1', True), ('0.9.10', True), ('0.10.0', True),
                                 ('0.09.0', True), ('1.0.0', True), ('10.0.0', True), ('0.0.9', False)]:
            with self.subTest(version=version):
                self.assertEqual(version_compare(version, '0.9.0', '>='), refused)
                self.assertEqual(version_compare(version, '0.9.0', '<'), not refused)


if __name__ == '__main__':
    unittest.main()
