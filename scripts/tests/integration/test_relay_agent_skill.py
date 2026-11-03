#!/usr/bin/env python3
"""Exercise the relay_agent_skill role with real Ansible against a disposable home.

No credentials, network or host service changes; become only to the test user itself. TMPDIR
belongs on /workspace. Also unit-tests the role's settings helper (files/claude_skill_hook.py) and
the fail-open hook command.

Release qualification: with RELAY_SKILL_RELEASE_DIR naming a directory that holds a release's
relay-bus-SKILL.md and relay-bus.py, one more case installs those real assets and checks that the
real helper accepts the directories and bus.json the role wrote.
"""
import contextlib
import grp
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[3]
HOOK = ROOT / 'ansible/roles/relay_agent_skill/files/claude_skill_hook.py'
_spec = importlib.util.spec_from_file_location('claude_skill_hook', HOOK)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)
CONNECTOR_HOOK = ROOT / 'ansible/roles/relay_connector/files/claude_relay_hook.py'
_spec = importlib.util.spec_from_file_location('claude_relay_hook', CONNECTOR_HOOK)
connector_hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(connector_hook)

COMMAND = ('command -v python3 >/dev/null 2>&1 && '
           'python3 "$HOME/.claude/skills/relay-bus/relay-bus.py" inbox --hook 2>/dev/null; exit 0')
SKILL_ENTRY = {'type': 'command', 'command': COMMAND, 'timeout': 3}
# What the relay_connector role wrote for v0.8.0 messaging: the skill role must never touch it.
OLD_HINT = {'type': 'command', 'timeout': 2, 'command': '/opt/relay/connector/' + 'a' * 64 +
            '/relay-connector --state /home/c4/.config/relay-connector/connector.json inbox-hint'}
KEY_SECRET = 'rak_fixture_agent_key_never_log_or_touch'
SKILL_MD = '---\nname: relay-bus\ndescription: fixture\n---\n\n# Relay message bus (fixture)\n\n<!-- relay-origin -->\n'
HELPER = ('#!/usr/bin/env python3\n"""Fixture relay-bus.py: answers the hook, refuses everything else."""\n'
          'import json, sys\n'
          'if sys.argv[1:] == ["inbox", "--hook"]:\n'
          '    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",'
          ' "additionalContext": "1 unread Relay message; run relay-bus.py read"}}))\n'
          '    sys.exit(0)\n'
          'sys.exit(3)\n')
# A user's own Claude Code settings: every key and hook here must survive the role.
USER_SETTINGS = {
    'model': 'opus',
    'statusLine': {'type': 'command', 'command': '/usr/bin/node /home/c4/.claude/statusline.js', 'refreshInterval': 5},
    'hooks': {
        'UserPromptSubmit': [{'hooks': [{'type': 'command', 'command': '/usr/local/bin/user-prompt-hook'}]},
                             {'hooks': [OLD_HINT]}],
        'Stop': [{'matcher': '', 'hooks': [{'type': 'command', 'command': 'notify-send done'}]}],
    },
}
SKILL_PATHS = [f'{harness}/skills/relay-bus/{name}' for harness in ('.claude', '.codex')
               for name in ('SKILL.md', 'relay-bus.py')]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot(top):
    """Bytes, link target and mode of everything under `top`."""
    files = {}
    for path in sorted(Path(top).rglob('*')):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            files[str(path)] = ('link', os.readlink(path))
        else:
            files[str(path)] = (path.read_bytes() if stat.S_ISREG(info.st_mode) else None, info.st_mode)
    return files


class SkillRole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='relay-agent-skill-iac-')
        cls.root = Path(cls.temp.name)
        cls.user = pwd.getpwuid(os.getuid())
        cls.home = cls.root / 'home'
        cls.release = cls.root / 'release'
        cls.release.mkdir()
        (cls.release / 'relay-bus-SKILL.md').write_text(SKILL_MD)
        (cls.release / 'relay-bus.py').write_text(HELPER)
        cls.pins = {'relay-bus-SKILL.md': sha256(cls.release / 'relay-bus-SKILL.md'),
                    'relay-bus.py': sha256(cls.release / 'relay-bus.py')}
        # First on the play's PATH: a test drops a fake python3 here to stand in for the host's.
        cls.bin = cls.root / 'bin'
        cls.bin.mkdir()
        cls.values = dict(relay_agent_skill_enabled=True, relay_agent_skill_state='present',
            relay_agent_skill_user=cls.user.pw_name, relay_agent_skill_home=str(cls.home),
            relay_agent_skill_source_dir=str(cls.release), relay_agent_skill_origin='https://relay.invalid',
            relay_agent_skill_release=dict(version='0.9.0', sha256=cls.pins),
            test_path=str(cls.bin) + ':' + os.environ['PATH'])
        cls.play = cls.root / 'play.yml'
        cls.play.write_text(yaml.safe_dump([dict(hosts='localhost', gather_facts=False,
            environment=dict(PATH='{{ test_path }}'), roles=[dict(role='relay_agent_skill')])]))
        cfg = cls.root / 'ansible.cfg'
        cfg.write_text('[defaults]\nroles_path=' + str(ROOT / 'ansible/roles') + '\nretry_files_enabled=False\n')
        cls.env = {**os.environ, 'ANSIBLE_CONFIG': str(cfg), 'ANSIBLE_NOCOLOR': '1',
                   'ANSIBLE_LOCAL_TEMP': str(cls.root / 'ansible-tmp')}
        cls.settings = cls.home / '.claude/settings.json'

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        """A fresh account home with the user's own settings and an agent key already in place."""
        if self.home.exists():
            for path in [self.home, *self.home.rglob('*')]:  # Undo any read-only test directory.
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o700)
            shutil.rmtree(self.home)
        self.home.mkdir(mode=0o700)
        (self.home / '.claude').mkdir(mode=0o700)
        self.settings.write_text(json.dumps(USER_SETTINGS, indent=2))
        self.settings.chmod(0o640)
        self.key = self.home / '.config/relay/agents/default.key'
        self.key.parent.mkdir(parents=True)
        self.key.write_text(json.dumps({'origin': 'https://relay.invalid', 'key': KEY_SECRET, 'principalId': 'p'}))
        self.key.chmod(0o600)
        (self.home / '.config/relay').chmod(0o755)  # Wrong on purpose: the role fixes both modes.
        (self.home / '.config/relay/agents').chmod(0o750)
        self.key_before = (self.key.read_bytes(), self.key.stat().st_mode, self.key.stat().st_mtime_ns)
        for fake in self.bin.iterdir():
            fake.unlink()

    def role(self, updates=None, success=True, check=False, omit=()):
        values = {**self.values, **(updates or {})}
        for key in omit:
            values.pop(key, None)
        extra = self.root / 'vars.json'
        extra.write_text(json.dumps(values))
        command = ['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(self.play),
                   '--diff', '-e', '@' + str(extra)] + (['--check'] if check else [])
        result = subprocess.run(command, env=self.env, text=True, capture_output=True, timeout=180)
        output = result.stdout + result.stderr
        self.assertNotIn(KEY_SECRET, output)
        self.assertEqual(result.returncode == 0, success, output[-7000:])
        return output

    @staticmethod
    def failed_task(output):
        """Name of the role task that failed the run (a loop reports `failed:` per item)."""
        head = output[:max(output.rfind('fatal: [localhost]'), output.rfind('failed: [localhost]'))]
        marker = 'TASK [relay_agent_skill : '
        return head[head.rindex(marker) + len(marker):].split('] *')[0]

    def hooks(self):
        return json.loads(self.settings.read_text())['hooks']['UserPromptSubmit']

    def assert_key_untouched(self):
        self.assertEqual((self.key.read_bytes(), self.key.stat().st_mode, self.key.stat().st_mtime_ns), self.key_before)

    def test_01_disabled_does_nothing(self):
        before = snapshot(self.home)
        out = self.role(dict(relay_agent_skill_enabled=False, relay_agent_skill_source_dir='missing'))
        self.assertRegex(out, r'changed=0\s')
        self.assertEqual(snapshot(self.home), before)

    def test_02_placeholder_pins_and_bad_inputs_fail_before_any_write(self):
        before = snapshot(self.home)
        # The role's own defaults: the TODO-at-release pins are empty, so nothing can install.
        out = self.role(omit=['relay_agent_skill_release'], success=False)
        self.assertEqual(self.failed_task(out), 'Require the reviewed skill release pins')
        self.assertIn('(TODO-at-release)', out)
        mismatched = dict(version='0.9.0', sha256={**self.pins, 'relay-bus.py': 'b' * 64})
        out = self.role(dict(relay_agent_skill_release=mismatched), success=False)
        self.assertEqual(self.failed_task(out), 'Reject missing, linked or mismatched release bytes')
        self.assertIn('found: ' + self.pins['relay-bus.py'], out)
        out = self.role(dict(relay_agent_skill_source_dir=str(self.root / 'nowhere')), success=False)
        self.assertEqual(self.failed_task(out), 'Reject missing, linked or mismatched release bytes')
        self.assertIn('found: no regular file', out)
        linked = self.root / 'linked-release'
        linked.mkdir(exist_ok=True)
        for name in self.pins:
            with contextlib.suppress(FileNotFoundError):
                (linked / name).unlink()
            (linked / name).symlink_to(self.release / name)
        out = self.role(dict(relay_agent_skill_source_dir=str(linked)), success=False)
        self.assertIn('found: a symlink', out)
        old = dict(version='0.8.0', sha256=self.pins)
        self.assertEqual(self.failed_task(self.role(dict(relay_agent_skill_release=old), success=False)),
                         'Require the reviewed skill release pins')
        for bad in [dict(relay_agent_skill_origin='http://relay.invalid'),
                    dict(relay_agent_skill_origin='https://relay.invalid/path'),
                    dict(relay_agent_skill_source_dir='relative/dir'),
                    dict(relay_agent_skill_claude_hook_timeout=0)]:
            with self.subTest(bad=bad):
                out = self.role(bad, success=False)
                self.assertEqual(self.failed_task(out),
                                 'Require a controller copy of the release, an HTTPS origin and a sane hook timeout')
        out = self.role(dict(relay_agent_skill_state='enabled'), success=False)
        self.assertEqual(self.failed_task(out), 'Require a known state and a plain account name')
        self.assertEqual(snapshot(self.home), before)

    def test_03_present_installs_the_pinned_skill_origin_and_one_hook_and_reruns_unchanged(self):
        self.role()
        for path in SKILL_PATHS:
            installed = self.home / path
            self.assertEqual(installed.read_text(), SKILL_MD if path.endswith('SKILL.md') else HELPER)
            self.assertEqual(installed.stat().st_mode & 0o7777, 0o644 if path.endswith('SKILL.md') else 0o755)
            self.assertEqual(installed.stat().st_uid, os.getuid())
        modes = {'.claude': 0o700, '.claude/skills': 0o755, '.claude/skills/relay-bus': 0o755,
                 '.codex': 0o700, '.codex/skills': 0o755, '.codex/skills/relay-bus': 0o755,
                 '.config/relay': 0o700, '.config/relay/agents': 0o700}
        self.assertEqual({path: (self.home / path).stat().st_mode & 0o7777 for path in modes}, modes)
        bus = self.home / '.config/relay/bus.json'
        self.assertEqual(json.loads(bus.read_text()), {'origin': 'https://relay.invalid'})
        self.assertEqual(bus.stat().st_mode & 0o7777, 0o600)
        self.assert_key_untouched()
        # The user's hooks stay, the connector's old inbox-hint entry too, and one skill entry is added.
        settings = json.loads(self.settings.read_text())
        self.assertEqual({k: v for k, v in settings.items() if k != 'hooks'},
                         {k: v for k, v in USER_SETTINGS.items() if k != 'hooks'})
        self.assertEqual(settings['hooks'], {**USER_SETTINGS['hooks'], 'UserPromptSubmit':
                         USER_SETTINGS['hooks']['UserPromptSubmit'] + [{'hooks': [SKILL_ENTRY]}]})
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o640)
        before = snapshot(self.home)
        self.assertRegex(self.role(), r'changed=0\s')
        self.assertEqual(snapshot(self.home), before)

    def test_04_a_new_release_replaces_the_files_and_keeps_the_hook(self):
        self.role()
        update = self.root / 'release-next'
        update.mkdir(exist_ok=True)
        (update / 'relay-bus-SKILL.md').write_text(SKILL_MD + '\nNext release.\n')
        (update / 'relay-bus.py').write_text(HELPER + '# next release\n')
        pins = {name: sha256(update / name) for name in self.pins}
        self.role(dict(relay_agent_skill_source_dir=str(update),
                       relay_agent_skill_release=dict(version='0.9.1', sha256=pins)))
        for path in SKILL_PATHS:
            name = 'relay-bus-SKILL.md' if path.endswith('SKILL.md') else 'relay-bus.py'
            self.assertEqual(sha256(self.home / path), pins[name])
        self.assertEqual([group for group in self.hooks() if group['hooks'] == [SKILL_ENTRY]], [{'hooks': [SKILL_ENTRY]}])
        self.assert_key_untouched()

    def test_05_linked_or_foreign_directories_are_refused_before_any_write(self):
        elsewhere = self.root / 'elsewhere'
        shutil.rmtree(elsewhere, ignore_errors=True)
        elsewhere.mkdir(mode=0o755)
        relay = self.home / '.config/relay'
        shutil.rmtree(relay)
        relay.symlink_to(elsewhere)
        before = snapshot(self.home)
        out = self.role(success=False)
        self.assertEqual(self.failed_task(out), 'Require real directories where the skill and its configuration go')
        self.assertIn(f'{relay} exists but is not a directory', out)
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual((list(elsewhere.iterdir()), elsewhere.stat().st_mode & 0o777), ([], 0o755))
        relay.unlink()
        relay.mkdir(mode=0o700)
        skill = self.home / '.codex/skills/relay-bus'
        skill.parent.mkdir(parents=True)
        skill.write_text('not a directory')
        out = self.role(success=False)
        self.assertEqual(self.failed_task(out), 'Require real directories where the skill and its configuration go')
        self.assertFalse((self.home / '.claude/skills').exists())

    def test_06_python_older_than_3_10_or_missing_fails_before_any_write(self):
        before = snapshot(self.home)
        fake = self.bin / 'python3'
        fake.write_text('#!/bin/sh\necho 3.9.18\n')
        fake.chmod(0o755)
        out = self.role(success=False)
        self.assertEqual(self.failed_task(out), 'Require Python 3.10 or later for relay-bus.py')
        self.assertIn('but python3 is 3.9.18', out)
        fake.write_text('#!/bin/sh\necho "python3: broken install" >&2\nexit 1\n')
        out = self.role(success=False)
        self.assertEqual(self.failed_task(out), 'Require Python 3.10 or later for relay-bus.py')
        self.assertIn('python3 did not run: python3: broken install', out)
        self.assertEqual(snapshot(self.home), before)
        for version in ('3.10.0', '3.12.3', '3.13.1'):
            fake.write_text(f'#!/bin/sh\necho {version}\n')
            with self.subTest(version=version):
                self.role(check=True)

    def test_07_unparsable_or_misshapen_settings_fail_before_any_write(self):
        for content, message in [('{"model": "opus",', 'is not valid JSON'),
                                 ('{"hooks": {"UserPromptSubmit": {}}}', 'is not a JSON array')]:
            for state in ('present', 'absent'):
                with self.subTest(content=content, state=state):
                    self.settings.write_text(content)
                    before = snapshot(self.home)
                    out = self.role(dict(relay_agent_skill_state=state), success=False)
                    self.assertEqual(self.failed_task(out), "Dry-run the relay-bus hook merge in the user's Claude Code settings")
                    self.assertIn(message, out)
                    self.assertEqual(snapshot(self.home), before)

    def test_08_check_mode_reports_and_changes_nothing(self):
        before = snapshot(self.home)
        out = self.role(check=True)
        self.assertNotRegex(out, r'changed=0\s')
        self.assertEqual(snapshot(self.home), before)
        self.role()
        before = snapshot(self.home)
        out = self.role(dict(relay_agent_skill_state='absent'), check=True)
        self.assertRegex(out, r'changed=2\s')  # The hook, and the skill directories (one loop task).
        self.assertIn('"state": "absent"', out)
        self.assertEqual(snapshot(self.home), before)

    def test_09_absent_removes_the_hook_and_the_skill_and_keeps_relay_config(self):
        self.role()
        bus = (self.home / '.config/relay/bus.json').read_bytes()
        # absent needs no release: not the pins, not the controller copy, not Python.
        absent = dict(relay_agent_skill_state='absent', relay_agent_skill_source_dir='')
        self.role(absent, omit=['relay_agent_skill_release'])
        self.assertEqual(json.loads(self.settings.read_text()), USER_SETTINGS)
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o640)
        for harness in ('.claude', '.codex'):
            self.assertFalse((self.home / harness / 'skills/relay-bus').exists())
            self.assertTrue((self.home / harness / 'skills').is_dir())
        self.assertEqual((self.home / '.config/relay/bus.json').read_bytes(), bus)
        self.assert_key_untouched()
        before = snapshot(self.home)
        self.assertRegex(self.role(absent, omit=['relay_agent_skill_release']), r'changed=0\s')
        self.assertEqual(snapshot(self.home), before)

    def test_10_absent_on_a_host_that_never_had_the_skill_changes_nothing(self):
        before = snapshot(self.home)
        out = self.role(dict(relay_agent_skill_state='absent'))
        self.assertRegex(out, r'changed=0\s')
        self.assertEqual(snapshot(self.home), before)

    @unittest.skipUnless(os.environ.get('RELAY_SKILL_RELEASE_DIR'), 'RELAY_SKILL_RELEASE_DIR names no release assets')
    def test_11_the_real_helper_accepts_what_the_role_wrote(self):
        release = Path(os.environ['RELAY_SKILL_RELEASE_DIR'])
        pins = {name: sha256(release / name) for name in self.pins}
        # A home without an agent key: the role never writes one.
        self.key.unlink()
        self.role(dict(relay_agent_skill_source_dir=str(release),
                       relay_agent_skill_release=dict(version='0.9.0', sha256=pins)))
        env = {'HOME': str(self.home), 'PATH': os.environ['PATH'], 'LANG': 'C.UTF-8'}
        for harness in ('.claude', '.codex'):
            helper = self.home / harness / 'skills/relay-bus/relay-bus.py'
            with self.subTest(harness=harness):
                # No key yet: "not connected" (4), not a local setup refusal (3) of the directories.
                result = subprocess.run([helper, 'whoami'], env=env, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
                # bus.json's origin is accepted: the request itself fails (.invalid never resolves).
                result = subprocess.run([helper, 'connect', '--name', 'qualification', '--purpose', 'role test'],
                                        env=env, text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
                self.assertNotIn(json.loads(result.stdout.splitlines()[-1])['error']['code'],
                                 ['no_origin', 'invalid_origin', 'unsafe_path', 'invalid_file'])
        # The installed hook, exactly as Claude Code runs it: silent and 0 without a key.
        result = subprocess.run(['/bin/sh', '-c', COMMAND], env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))


class ClaudeSkillHookScript(unittest.TestCase):
    """files/claude_skill_hook.py on its own, and the hook command it writes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='claude-skill-hook-')
        self.home = Path(self.temp.name)
        self.settings = self.home / '.claude/settings.json'

    def tearDown(self):
        self.temp.cleanup()

    def run_hook(self, state='present', check=False, timeout='3', stdin=False):
        argv = ['--settings', str(self.settings), '--state', state]
        argv += ['--timeout', timeout] if timeout is not None else []
        argv += ['--check'] if check else []
        if stdin:  # How the role runs it: piped to `python3 -`, nothing left on the worker.
            return subprocess.run([sys.executable, '-', *argv], input=HOOK.read_text(),
                                  text=True, capture_output=True, timeout=30)
        return subprocess.run([sys.executable, str(HOOK), *argv], text=True, capture_output=True, timeout=30)

    def main_in_process(self, state='present', check=False, before_replace=None, geteuid=None):
        out, err = io.StringIO(), io.StringIO()
        argv = ['--settings', str(self.settings), '--state', state, '--timeout', '3'] + (['--check'] if check else [])
        with contextlib.ExitStack() as stack:
            if before_replace is not None:
                stack.enter_context(mock.patch.object(hook, '_before_replace', before_replace))
            if geteuid is not None:
                stack.enter_context(mock.patch.object(hook.os, 'geteuid', return_value=geteuid))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = hook.main(argv)
        return code, out.getvalue(), err.getvalue()

    def write(self, data, mode=0o644):
        self.settings.parent.mkdir(mode=0o700, exist_ok=True)
        self.settings.write_text(data if isinstance(data, str) else json.dumps(data, indent=2))
        self.settings.chmod(mode)

    def test_the_command_is_the_fail_open_one_the_role_documents(self):
        self.assertEqual(hook.COMMAND, COMMAND)

    def test_matches_only_the_exact_command(self):
        self.assertTrue(hook.is_relay_bus_hook({'type': 'command', 'command': COMMAND}))
        self.assertTrue(hook.is_relay_bus_hook({'type': 'command', 'command': COMMAND, 'timeout': 9}))
        for command in [COMMAND + ' ', ' ' + COMMAND, COMMAND.replace('$HOME', '~'),
                        COMMAND.replace('; exit 0', ''), COMMAND.replace(' 2>/dev/null;', ';'),
                        'python3 "$HOME/.claude/skills/relay-bus/relay-bus.py" inbox --hook',
                        COMMAND + ' && /usr/local/bin/user-action', OLD_HINT['command']]:
            self.assertFalse(hook.is_relay_bus_hook({'type': 'command', 'command': command}), command)
        for entry in [{'type': 'prompt', 'command': COMMAND}, {'command': COMMAND}, COMMAND, None, []]:
            self.assertFalse(hook.is_relay_bus_hook(entry), entry)

    def test_the_two_roles_never_claim_each_others_hook(self):
        self.assertFalse(connector_hook.is_relay_inbox_hint(SKILL_ENTRY))
        self.assertTrue(connector_hook.is_relay_inbox_hint(OLD_HINT))
        self.assertFalse(hook.is_relay_bus_hook(OLD_HINT))

    def test_new_file_is_created_0644_in_a_private_claude_dir_and_rerun_is_unchanged(self):
        result = self.run_hook(stdin=True)
        self.assertEqual((result.returncode, result.stdout), (0, 'changed\n'), result.stderr)
        self.assertEqual(self.settings.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o644)
        self.assertEqual(json.loads(self.settings.read_text()), {'hooks': {'UserPromptSubmit': [{'hooks': [SKILL_ENTRY]}]}})
        inode, text = self.settings.stat().st_ino, self.settings.read_bytes()
        result = self.run_hook(stdin=True)
        self.assertEqual((result.returncode, result.stdout), (0, 'unchanged\n'), result.stderr)
        self.assertEqual((self.settings.stat().st_ino, self.settings.read_bytes()), (inode, text))

    def test_present_keeps_every_other_hook_including_the_connectors_old_one(self):
        user_hook = {'type': 'command', 'command': '/usr/local/bin/user-prompt-hook'}
        mentions = {'type': 'command', 'command': 'python3 ~/.claude/skills/relay-bus/relay-bus.py inbox'}
        settings = {'model': 'opus', 'note': 'café ☕', 'hooks': {
            'UserPromptSubmit': [{'hooks': [user_hook, {**SKILL_ENTRY, 'timeout': 9}]},  # A hand-moved copy.
                                 {'matcher': '', 'hooks': [OLD_HINT, mentions]},
                                 {'hooks': []}],
            'PreToolUse': [{'matcher': 'Bash', 'hooks': [SKILL_ENTRY]}]}}
        self.write(settings, mode=0o600)
        result = self.run_hook()
        self.assertEqual((result.returncode, result.stdout), (0, 'changed\n'), result.stderr)
        self.assertEqual(json.loads(self.settings.read_text()), {**settings, 'hooks': {
            'UserPromptSubmit': [{'hooks': [user_hook]}, {'matcher': '', 'hooks': [OLD_HINT, mentions]},
                                 {'hooks': []}, {'hooks': [SKILL_ENTRY]}],
            'PreToolUse': settings['hooks']['PreToolUse']}})  # Only UserPromptSubmit is the role's.
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_hook().stdout, 'unchanged\n')
        # absent takes out exactly what present added.
        self.assertEqual(self.run_hook(state='absent', timeout=None).stdout, 'changed\n')
        self.assertEqual(json.loads(self.settings.read_text())['hooks']['UserPromptSubmit'],
                         [{'hooks': [user_hook]}, {'matcher': '', 'hooks': [OLD_HINT, mentions]}, {'hooks': []}])

    def test_absent_drops_only_what_its_removal_emptied(self):
        ours = {'hooks': [SKILL_ENTRY]}
        stop = [{'hooks': [{'type': 'command', 'command': 'notify-send done'}]}]
        cases = [  # (settings, after absent, after present and then absent)
            ({'model': 'x', 'hooks': {'UserPromptSubmit': [ours]}}, {'model': 'x'}, {'model': 'x'}),
            ({'hooks': {'UserPromptSubmit': [ours], 'Stop': stop}}, {'hooks': {'Stop': stop}}, {'hooks': {'Stop': stop}}),
            ({'hooks': {'UserPromptSubmit': [{'hooks': [OLD_HINT]}]}}, {'hooks': {'UserPromptSubmit': [{'hooks': [OLD_HINT]}]}},
             {'hooks': {'UserPromptSubmit': [{'hooks': [OLD_HINT]}]}}),
            ({'model': 'x'}, {'model': 'x'}, {'model': 'x'}),
            ({'hooks': {'UserPromptSubmit': []}}, {'hooks': {'UserPromptSubmit': []}}, {}),
            ({'hooks': {}}, {'hooks': {}}, {}),
        ]
        for before, after_absent, after_round_trip in cases:
            original = json.loads(json.dumps(before))
            self.assertEqual(hook.converge(before, 'absent'), after_absent, before)
            self.assertEqual(hook.converge(hook.converge(before, 'present', 3), 'absent'), after_round_trip, before)
            self.assertEqual(before, original)  # converge() returns a copy.

    def test_absent_on_a_missing_file_creates_nothing(self):
        result = self.run_hook(state='absent', timeout=None)
        self.assertEqual((result.returncode, result.stdout), (0, 'unchanged\n'), result.stderr)
        self.assertFalse(self.settings.parent.exists())

    def test_a_changed_timeout_is_put_back(self):
        self.write({'hooks': {'UserPromptSubmit': [{'hooks': [{**SKILL_ENTRY, 'timeout': 30}]}]}})
        self.assertEqual(self.run_hook().stdout, 'changed\n')
        self.assertEqual(json.loads(self.settings.read_text())['hooks']['UserPromptSubmit'], [{'hooks': [SKILL_ENTRY]}])

    def test_check_reports_without_writing(self):
        result = self.run_hook(check=True)
        self.assertEqual((result.returncode, result.stdout), (0, 'changed\n'), result.stderr)
        self.assertFalse(self.settings.parent.exists())
        self.write({'model': 'opus', 'hooks': {'UserPromptSubmit': [{'hooks': [SKILL_ENTRY]}]}})
        before = self.settings.read_bytes()
        self.assertEqual(self.run_hook(state='absent', timeout=None, check=True).stdout, 'changed\n')
        self.assertEqual(self.run_hook(check=True).stdout, 'unchanged\n')
        self.assertEqual(self.settings.read_bytes(), before)

    def test_unparsable_or_misshapen_settings_fail_without_writing(self):
        for content, message in [('{"model": "opus",', 'not valid JSON'), ('null', 'top level is not a JSON object'),
                                 ('{"hooks": []}', "'hooks' is not a JSON object"),
                                 ('{"hooks": {"UserPromptSubmit": {}}}', 'is not a JSON array'),
                                 ('{"hooks": {"UserPromptSubmit": [{"hooks": {}}]}}', "is not an object with a 'hooks' array"),
                                 ('{"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command"}]}]}}',
                                  "is a command hook without a string 'command'"),
                                 ('{"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "x", "timeout": 0}]}]}}',
                                  'is not a positive number of seconds')]:
            for state in ('present', 'absent'):
                for check in (False, True):
                    with self.subTest(content=content, state=state, check=check):
                        self.write(content)
                        result = self.run_hook(state=state, check=check)
                        self.assertEqual((result.returncode, result.stdout), (1, ''), result.stderr)
                        self.assertIn(message, result.stderr)
                        self.assertIn('nothing was written', result.stderr)
                        self.assertEqual(self.settings.read_text(), content)

    def test_present_needs_a_positive_timeout(self):
        for timeout in (None, '0', '-1', 'x'):
            result = self.run_hook(timeout=timeout)
            self.assertEqual(result.returncode, 2, (timeout, result.stdout))
            self.assertFalse(self.settings.parent.exists())

    def test_a_target_owned_by_someone_else_is_refused_only_when_it_would_change(self):
        other = os.geteuid() + 1
        self.write({'model': 'opus'})
        before = self.settings.read_bytes()
        for state, check in [('present', False), ('present', True)]:
            code, out, err = self.main_in_process(state, check, geteuid=other)
            self.assertEqual((code, out), (1, ''))
            self.assertIn(f'it is owned by uid {os.geteuid()}, not by this user (uid {other})', err)
            self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(self.main_in_process('absent', geteuid=other)[:2], (0, 'unchanged\n'))  # Nothing to do.

    def test_symlinked_settings_keep_the_link_and_the_target_mode(self):
        dotfiles = self.home / 'dotfiles'
        dotfiles.mkdir()
        target = dotfiles / 'claude-settings.json'
        target.write_text('{"model": "opus"}')
        target.chmod(0o600)
        self.settings.parent.mkdir(mode=0o700)
        self.settings.symlink_to(target)
        result = self.run_hook()
        self.assertEqual((result.returncode, result.stdout), (0, 'changed\n'), result.stderr)
        self.assertTrue(self.settings.is_symlink())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(target.read_text())['hooks']['UserPromptSubmit'], [{'hooks': [SKILL_ENTRY]}])

    def test_a_concurrent_edit_is_merged_not_overwritten(self):
        self.write({'model': 'opus'})
        edits = []

        def user_edits_once():
            if not edits:
                edits.append(1)
                self.settings.write_text(json.dumps({'model': 'opus', 'theme': 'dark'}))
        code, out, err = self.main_in_process(before_replace=user_edits_once)
        self.assertEqual((code, out), (0, 'changed\n'), err)
        self.assertEqual(json.loads(self.settings.read_text()),
                         {'model': 'opus', 'theme': 'dark', 'hooks': {'UserPromptSubmit': [{'hooks': [SKILL_ENTRY]}]}})
        self.assertEqual([p.name for p in self.settings.parent.iterdir()], ['settings.json'])

    def test_output_is_ansible_to_nice_json_so_the_status_line_task_never_rewrites_it(self):
        from ansible.plugins.filter.core import to_nice_json
        self.write({'statusLine': {'type': 'command', 'command': '/usr/bin/node /x.js', 'refreshInterval': 5},
                    'model': 'opus', 'note': 'café'})
        self.assertEqual(self.run_hook().stdout, 'changed\n')
        text = self.settings.read_text()
        self.assertEqual(text, to_nice_json(json.loads(text)))


class HookCommandFailsOpen(unittest.TestCase):
    """The hook command as Claude Code runs it (`/bin/sh -c`), whatever is or is not installed."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-bus-hook-command-')
        self.home = Path(self.temp.name)
        self.helper = self.home / '.claude/skills/relay-bus/relay-bus.py'
        python = Path(shutil.which('python3')).resolve()
        self.with_python = self.home / 'bin-with-python'
        self.with_python.mkdir()
        (self.with_python / 'python3').symlink_to(python)
        self.without_python = self.home / 'bin-without-python'
        self.without_python.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def run_command(self, path):
        # Only these two directories plus the shell's own builtins: no other python3 can be found.
        return subprocess.run(['/bin/sh', '-c', COMMAND], env={'HOME': str(self.home), 'PATH': str(path)},
                              text=True, capture_output=True, timeout=30)

    def test_a_helper_that_answers_is_passed_through(self):
        self.helper.parent.mkdir(parents=True)
        self.helper.write_text(HELPER)
        result = self.run_command(self.with_python)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')

    def test_a_failing_helper_is_silent_and_exits_0(self):
        self.helper.parent.mkdir(parents=True)
        self.helper.write_text('import sys\nsys.stderr.write("Traceback: boom\\n")\nsys.exit(2)\n')
        result = self.run_command(self.with_python)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))

    def test_a_missing_helper_is_silent_and_exits_0(self):
        result = self.run_command(self.with_python)  # Python itself exits 2 for a missing script.
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))

    def test_no_python3_is_silent_and_exits_0(self):
        self.helper.parent.mkdir(parents=True)
        self.helper.write_text(HELPER)
        result = self.run_command(self.without_python)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))


if __name__ == '__main__':
    unittest.main()
