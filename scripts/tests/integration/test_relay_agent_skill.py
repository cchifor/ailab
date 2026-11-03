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
import fcntl
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
import time
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[3]
HOOK = ROOT / 'ansible/roles/relay_agent_skill/files/claude_skill_hook.py'
FILES_HELPER = ROOT / 'ansible/roles/relay_agent_skill/files/skill_files.py'
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
          '# Not a template: {{ relay_agent_skill_user }} {% if true %}x{% endif %} {# kept #}\n'
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
        # The role's own validation, then a path swapped by `swap_script`, then its writes: what a
        # change between the checks and the writes would meet.
        cls.swap_play = cls.root / 'swap-play.yml'
        cls.swap_play.write_text(yaml.safe_dump([dict(hosts='localhost', gather_facts=False,
            environment=dict(PATH='{{ test_path }}'), tasks=[
                {'ansible.builtin.include_role': {'name': 'relay_agent_skill', 'tasks_from': 'validate.yml'}},
                {'name': 'Swap a path after validation', 'ansible.builtin.command': {'argv': ['{{ swap_script }}']}},
                {'ansible.builtin.include_role': {'name': 'relay_agent_skill',
                                                  'tasks_from': '{{ relay_agent_skill_state }}.yml'}}])]))
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

    def role(self, updates=None, success=True, check=False, omit=(), play=None):
        values = {**self.values, **(updates or {})}
        for key in omit:
            values.pop(key, None)
        extra = self.root / 'vars.json'
        extra.write_text(json.dumps(values))
        command = ['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(play or self.play),
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

    def elsewhere(self, name='elsewhere'):
        """A directory outside the home, mode 0755, holding one file: a link's target that must stay as it is."""
        target = self.root / name
        shutil.rmtree(target, ignore_errors=True)
        (target / 'relay-bus').mkdir(parents=True)
        (target / 'relay-bus/SKILL.md').write_text('not the role\'s')
        (target / 'relay-bus/relay-bus.py').write_text('not the role\'s')
        (target / 'keep.txt').write_text('keep')
        for path in (target, target / 'relay-bus'):
            path.chmod(0o755)
        return target

    def test_05_a_symlink_anywhere_on_the_way_is_refused_before_any_write(self):
        for path in ['.config', '.config/relay', '.config/relay/agents', '.claude', '.claude/skills',
                     '.claude/skills/relay-bus', '.codex', '.codex/skills', '.codex/skills/relay-bus']:
            with self.subTest(path=path):
                self.setUp()
                target = self.elsewhere()
                link = self.home / path
                if link.exists():
                    shutil.rmtree(link)
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target)
                before, outside = snapshot(self.home), snapshot(target)
                for check in (False, True):
                    out = self.role(success=False, check=check)
                    self.assertEqual(self.failed_task(out), "Dry-run the skill's file changes")
                    self.assertIn(f'~/{path} is not a directory (a symlink or a file)', out)
                    self.assertEqual(snapshot(self.home), before)
                    self.assertEqual((snapshot(target), target.stat().st_mode & 0o777), (outside, 0o755))
        # Not a directory: a file where a directory goes.
        self.setUp()
        (self.home / '.codex/skills').mkdir(parents=True)
        (self.home / '.codex/skills/relay-bus').write_text('not a directory')
        before = snapshot(self.home)
        out = self.role(success=False)
        self.assertEqual(self.failed_task(out), "Dry-run the skill's file changes")
        self.assertIn('~/.codex/skills/relay-bus is not a directory', out)
        self.assertEqual(snapshot(self.home), before)

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
        self.assertRegex(out, r'changed=2\s')  # The hook and the skill.
        self.assertRegex(out, r'Remove the relay-bus skill for Claude Code and Codex\] \**\nchanged')
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

    def test_12_a_destination_that_is_not_a_regular_file_is_refused_before_any_write(self):
        for path, kind in [('.claude/skills/relay-bus/SKILL.md', 'a directory'),
                           ('.codex/skills/relay-bus/relay-bus.py', 'a directory'),
                           ('.config/relay/bus.json', 'a directory'),
                           ('.claude/skills/relay-bus/relay-bus.py', 'a symlink')]:
            with self.subTest(path=path):
                self.setUp()
                destination = self.home / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                outside = self.root / 'outside.txt'
                outside.write_text('not the role\'s')
                if kind == 'a directory':
                    destination.mkdir()
                    (destination / 'inside.txt').write_text('kept')
                else:
                    destination.symlink_to(outside)
                before = snapshot(self.home)
                for check in (False, True):
                    out = self.role(success=False, check=check)
                    self.assertEqual(self.failed_task(out), "Dry-run the skill's file changes")
                    self.assertIn(f'~/{path} is not a regular file', out)
                    self.assertEqual(snapshot(self.home), before)  # Its contents too.
                    self.assertEqual(outside.read_text(), 'not the role\'s')

    def swap(self, script):
        swap = self.root / 'swap.sh'
        swap.write_text('#!/bin/sh\nset -e\n' + script)
        swap.chmod(0o755)
        return {'swap_script': str(swap)}

    def test_13_a_path_swapped_for_a_symlink_after_validation_is_never_followed(self):
        # Directories, ancestors included, and a file: the writes walk the home through no-follow
        # descriptors, so a link that appears after validate.yml stops them before any change.
        for path in ['.config', '.config/relay', '.claude/skills', '.codex/skills/relay-bus/SKILL.md']:
            with self.subTest(path=path):
                self.setUp()
                if path.endswith('SKILL.md'):
                    self.role()
                target = self.elsewhere()
                link = self.home / path
                moved = self.root / 'moved'
                shutil.rmtree(moved, ignore_errors=True)
                link.parent.mkdir(parents=True, exist_ok=True)
                to = target / 'relay-bus/SKILL.md' if path.endswith('SKILL.md') else target
                script = (f'mv {link} {moved}\n' if link.exists() else '') + f'ln -s {to} {link}\n'
                outside = snapshot(target)
                out = self.role(self.swap(script), success=False, play=self.swap_play)
                self.assertEqual(self.failed_task(out), "Install the pinned skill files, the private Relay directories and bus.json")
                self.assertIn(f'~/{path} is not a', out)
                self.assertIn('nothing was changed', out)
                self.assertEqual((snapshot(target), target.stat().st_mode & 0o777), (outside, 0o755))
                self.assertTrue(link.is_symlink())

    def test_14_the_installed_files_and_directories_carry_the_users_ownership_and_modes(self):
        # ~/.claude existing with the user's own mode keeps it; the role's directories get theirs.
        (self.home / '.claude').chmod(0o750)
        self.role()
        self.assertEqual((self.home / '.claude').stat().st_mode & 0o777, 0o750)
        for path in [*SKILL_PATHS, '.config/relay/bus.json', '.claude/skills/relay-bus', '.codex/skills/relay-bus',
                     '.config/relay', '.config/relay/agents', '.codex', '.codex/skills']:
            self.assertEqual((self.home / path).stat().st_uid, os.getuid(), path)
        leftovers = [p for p in self.home.rglob('*.tmp')]
        self.assertEqual(leftovers, [])

    def test_15_absent_removes_only_the_users_regular_files_and_never_follows_a_link(self):
        absent = dict(relay_agent_skill_state='absent')
        # A link anywhere from ~/.claude down is refused before the hook changes, in check mode too.
        for path in ['.claude/skills', '.claude/skills/relay-bus', '.codex', '.claude/skills/relay-bus/SKILL.md']:
            with self.subTest(path=path):
                self.setUp()
                self.role()
                target = self.elsewhere()
                link = self.home / path
                if link.is_dir():
                    shutil.rmtree(link)
                else:
                    link.unlink()
                link.symlink_to(target / 'relay-bus/SKILL.md' if path.endswith('SKILL.md') else target)
                before, outside = snapshot(self.home), snapshot(target)
                for check in (False, True):
                    out = self.role(absent, success=False, check=check)
                    self.assertEqual(self.failed_task(out), "Dry-run the skill's file changes")
                    self.assertIn('the role never', out)
                    self.assertEqual(snapshot(self.home), before)
                    self.assertEqual(snapshot(target), outside)
        # A link swapped in after the dry run: the hook goes, the removal refuses, the target stays.
        self.setUp()
        self.role()
        target = self.elsewhere()
        skills = self.home / '.claude/skills'
        outside = snapshot(target)
        out = self.role(dict(absent, **self.swap(f'mv {skills} {self.root}/moved-skills\nln -s {target} {skills}\n')),
                        success=False, play=self.swap_play)
        self.assertEqual(self.failed_task(out), 'Remove the relay-bus skill for Claude Code and Codex')
        self.assertEqual(snapshot(target), outside)
        self.assertEqual(json.loads(self.settings.read_text()), USER_SETTINGS)
        shutil.rmtree(self.root / 'moved-skills')
        # Anything else in the skill directory stays, and so does the directory.
        self.setUp()
        self.role()
        extra = self.home / '.codex/skills/relay-bus/notes.txt'
        extra.write_text('the user\'s')
        self.role(absent)
        self.assertEqual(sorted(p.name for p in extra.parent.iterdir()), ['notes.txt'])
        self.assertFalse((self.home / '.claude/skills/relay-bus').exists())

    def test_16_every_write_and_removal_runs_as_the_user_through_the_no_follow_helper(self):
        # What a fixture without root cannot show by running: present.yml and absent.yml are each one
        # block run as the user, and nothing in them writes but the helpers (no file or copy module).
        tasks = ROOT / 'ansible/roles/relay_agent_skill/tasks'
        for name in ('present.yml', 'absent.yml'):
            with self.subTest(name=name):
                plays = yaml.safe_load((tasks / name).read_text())
                self.assertEqual(len(plays), 1)
                block = plays[0]
                self.assertEqual((block.get('become'), block.get('become_user')), (True, '{{ relay_agent_skill_user }}'))
                for task in block['block']:
                    self.assertFalse({'ansible.builtin.file', 'ansible.builtin.copy', 'ansible.builtin.template',
                                      'ansible.builtin.shell'} & set(task), task['name'])
        hook_task = yaml.safe_load((tasks / 'hook.yml').read_text())[0]
        self.assertEqual(hook_task['become_user'], '{{ relay_agent_skill_user }}')


def files_payload(skill=SKILL_MD, helper=HELPER, origin='https://relay.invalid'):
    return {'origin': origin, 'files': [
        {'name': 'SKILL.md', 'mode': '0644', 'sha256': hashlib.sha256(skill.encode()).hexdigest(), 'content': skill},
        {'name': 'relay-bus.py', 'mode': '0755', 'sha256': hashlib.sha256(helper.encode()).hexdigest(), 'content': helper}]}


class SkillFilesScript(unittest.TestCase):
    """files/skill_files.py on its own, run as the role runs it: `python3 -I -c <source>`."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill-files-')
        self.home = Path(self.temp.name) / 'home'
        self.home.mkdir()
        spec = importlib.util.spec_from_file_location('skill_files', FILES_HELPER)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def tearDown(self):
        self.temp.cleanup()

    def run_files(self, state='present', payload=None, check=False):
        argv = [sys.executable, '-I', '-c', FILES_HELPER.read_text(), '--home', str(self.home), '--state', state]
        stdin = json.dumps(files_payload() if payload is None else payload) if state == 'present' else ''
        return subprocess.run(argv + (['--check'] if check else []), input=stdin, text=True,
                              capture_output=True, timeout=30)

    def in_process(self, state='present', payload=None):
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO(json.dumps(files_payload() if payload is None else payload))
        with mock.patch('sys.stdin', stdin), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.module.main(['--home', str(self.home), '--state', state])
        return code, out.getvalue(), err.getvalue()

    def test_present_creates_everything_with_its_modes_and_reruns_unchanged(self):
        before = snapshot(self.home)
        result = self.run_files(check=True)
        self.assertEqual((result.returncode, result.stdout), (0, 'changed\n'), result.stderr)
        self.assertEqual(snapshot(self.home), before)
        result = self.run_files()
        self.assertEqual((result.returncode, result.stdout), (0, 'changed\n'), result.stderr)
        modes = {'.config': 0o700, '.config/relay': 0o700, '.config/relay/agents': 0o700, '.config/relay/bus.json': 0o600,
                 '.claude': 0o700, '.claude/skills': 0o755, '.claude/skills/relay-bus': 0o755,
                 '.claude/skills/relay-bus/SKILL.md': 0o644, '.claude/skills/relay-bus/relay-bus.py': 0o755,
                 '.codex': 0o700, '.codex/skills': 0o755, '.codex/skills/relay-bus': 0o755,
                 '.codex/skills/relay-bus/SKILL.md': 0o644, '.codex/skills/relay-bus/relay-bus.py': 0o755}
        self.assertEqual({str(p.relative_to(self.home)): p.stat().st_mode & 0o7777 for p in self.home.rglob('*')}, modes)
        self.assertEqual((self.home / '.codex/skills/relay-bus/relay-bus.py').read_text(), HELPER)
        self.assertEqual((self.home / '.config/relay/bus.json').read_text(), '{\n    "origin": "https://relay.invalid"\n}\n')
        self.assertEqual(self.run_files().stdout, 'unchanged\n')

    def test_a_file_that_does_not_hash_to_its_pin_is_never_written(self):
        payload = files_payload()
        payload['files'][1]['sha256'] = 'c' * 64
        for check in (True, False):
            result = self.run_files(payload=payload, check=check)
            self.assertEqual((result.returncode, result.stdout), (1, ''))
            self.assertIn('relay-bus.py does not hash to its pinned sha256', result.stderr)
        self.assertEqual(list(self.home.iterdir()), [])
        for bad in [{'origin': 'x', 'files': payload['files'][:1]},
                    {'origin': 'x', 'files': [{**payload['files'][0], 'name': '../escape'}, payload['files'][1]]},
                    {'origin': 'x', 'files': [{**payload['files'][0], 'mode': '0777'}, payload['files'][1]]}, []]:
            self.assertEqual(self.run_files(payload=bad).returncode, 1, bad)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_existing_modes_are_corrected_only_where_the_role_owns_the_directory(self):
        (self.home / '.config/relay/agents').mkdir(parents=True, mode=0o755)
        (self.home / '.config/relay').chmod(0o755)
        (self.home / '.config').chmod(0o755)
        key = self.home / '.config/relay/agents/default.key'
        key.write_text(KEY_SECRET)
        key.chmod(0o600)
        (self.home / '.claude/skills').mkdir(parents=True)
        (self.home / '.claude').chmod(0o711)
        result = self.run_files()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({path: (self.home / path).stat().st_mode & 0o777 for path in
                          ('.config', '.config/relay', '.config/relay/agents', '.claude')},
                         {'.config': 0o755, '.config/relay': 0o700, '.config/relay/agents': 0o700, '.claude': 0o711})
        self.assertEqual((key.read_text(), key.stat().st_mode & 0o777), (KEY_SECRET, 0o600))

    def test_a_link_or_wrong_kind_anywhere_is_refused_and_nothing_changes(self):
        cases = ['.config', '.config/relay/agents', '.codex/skills', '.claude/skills/relay-bus/SKILL.md',
                 '.config/relay/bus.json']
        for path in cases:
            for kind in ('link', 'other'):
                with self.subTest(path=path, kind=kind):
                    self.tearDown()
                    self.setUp()
                    target = self.home.parent / 'elsewhere'
                    target.mkdir()
                    node = self.home / path
                    node.parent.mkdir(parents=True, exist_ok=True)
                    is_file = '.' in path.rsplit('/', 1)[-1].lstrip('.')
                    if kind == 'link':
                        node.symlink_to(target)
                    elif is_file:
                        node.mkdir()  # A directory where a file goes.
                    else:
                        node.write_text('a file where a directory goes')
                    before = snapshot(self.home)
                    for check in (True, False):
                        result = self.run_files(check=check)
                        self.assertEqual((result.returncode, result.stdout), (1, ''), result.stderr)
                        self.assertIn(f'~/{path} is not a', result.stderr)
                        self.assertIn('nothing was changed', result.stderr)
                        self.assertEqual(snapshot(self.home), before)
                        self.assertEqual(list(target.iterdir()), [])

    def test_a_link_swapped_in_between_the_checks_and_the_writes_is_never_followed(self):
        target = self.home.parent / 'elsewhere'
        target.mkdir()
        real = self.module.present

        def swap_after_the_checks(tree, files, write):
            if write:  # The checks passed; now the home changes under the writes.
                (self.home / '.config').rename(self.home.parent / 'moved-config')
                (self.home / '.config').symlink_to(target)
            return real(tree, files, write)
        (self.home / '.config/relay').mkdir(parents=True)
        with mock.patch.object(self.module, 'present', swap_after_the_checks):
            code, out, err = self.in_process()
        self.assertEqual((code, out), (1, ''))
        self.assertIn('~/.config is not a directory', err)
        self.assertIn('it stopped part way', err)
        self.assertEqual(list(target.iterdir()), [])
        # A file swapped for a link between the passes: the write pass checks it again and refuses it.
        self.tearDown()
        self.setUp()
        self.assertEqual(self.run_files().returncode, 0)
        outside = self.home.parent / 'outside.txt'
        outside.write_text('not the role\'s')
        skill = self.home / '.claude/skills/relay-bus/SKILL.md'
        real = self.module.present

        def link_the_file(tree, files, write):
            if write:
                skill.unlink()
                skill.symlink_to(outside)
            return real(tree, files, write)
        with mock.patch.object(self.module, 'present', link_the_file):
            code, out, err = self.in_process(payload=files_payload(skill=SKILL_MD + 'next\n'))
        self.assertEqual((code, out), (1, ''))
        self.assertIn('~/.claude/skills/relay-bus/SKILL.md is not a regular file', err)
        self.assertEqual(outside.read_text(), 'not the role\'s')
        self.assertTrue(skill.is_symlink())
        # And in the last instant, between that check and the rename: the rename replaces the link.
        skill.unlink()
        temporary = self.home / '.claude/skills/relay-bus/.SKILL.md.x.tmp'
        temporary.write_text('new')
        skill.symlink_to(outside)
        parent = os.open(skill.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.replace(temporary.name, skill.name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            os.close(parent)
        self.assertEqual((outside.read_text(), skill.is_symlink(), skill.read_text()), ('not the role\'s', False, 'new'))

    def test_another_users_path_is_refused(self):
        self.assertEqual(self.run_files().returncode, 0)
        for state in ('present', 'absent'):
            with mock.patch.object(self.module.os, 'geteuid', return_value=os.geteuid() + 1):
                code, out, err = self.in_process(state=state)
            self.assertEqual((code, out), (1, ''))
            self.assertIn('is owned by uid', err)
        self.assertTrue((self.home / '.claude/skills/relay-bus/SKILL.md').exists())

    def test_absent_removes_the_two_files_and_the_emptied_directories_only(self):
        self.run_files()
        (self.home / '.claude/skills/other-skill').mkdir()
        before = snapshot(self.home)
        result = self.run_files('absent', check=True)
        self.assertEqual((result.returncode, result.stdout), (0, 'changed\n'), result.stderr)
        self.assertEqual(snapshot(self.home), before)
        result = self.run_files('absent')
        self.assertEqual((result.returncode, result.stdout), (0, 'changed\n'), result.stderr)
        self.assertEqual(sorted(str(p.relative_to(self.home)) for p in self.home.rglob('*') if '.config' not in str(p)),
                         ['.claude', '.claude/skills', '.claude/skills/other-skill', '.codex', '.codex/skills'])
        self.assertTrue((self.home / '.config/relay/bus.json').exists())  # Never absent's.
        self.assertEqual(self.run_files('absent').stdout, 'unchanged\n')

    def test_absent_refuses_links_and_other_kinds_and_removes_nothing(self):
        for path in ['.claude', '.claude/skills', '.codex/skills/relay-bus', '.codex/skills/relay-bus/SKILL.md']:
            with self.subTest(path=path):
                self.tearDown()
                self.setUp()
                self.run_files()
                target = self.home.parent / 'elsewhere'
                target.mkdir()
                (target / 'SKILL.md').write_text('not the role\'s')
                link = self.home / path
                if link.is_dir():
                    shutil.rmtree(link)
                else:
                    link.unlink()
                link.symlink_to(target / 'SKILL.md' if path.endswith('.md') else target)
                before = snapshot(self.home)
                for check in (True, False):
                    result = self.run_files('absent', check=check)
                    self.assertEqual((result.returncode, result.stdout), (1, ''), result.stderr)
                    self.assertIn('nothing was changed', result.stderr)
                    self.assertEqual(snapshot(self.home), before)
                    self.assertEqual(sorted(p.name for p in target.iterdir()), ['SKILL.md'])
        # A directory where a file goes, in the second tree: the first is checked but not yet changed.
        self.tearDown()
        self.setUp()
        self.run_files()
        (self.home / '.codex/skills/relay-bus/SKILL.md').unlink()
        (self.home / '.codex/skills/relay-bus/SKILL.md').mkdir()
        result = self.run_files('absent')
        self.assertEqual(result.returncode, 1)
        self.assertIn('is not a regular file', result.stderr)
        self.assertTrue((self.home / '.claude/skills/relay-bus/SKILL.md').exists())


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
        # The write took the shared lock: its file stays, empty and private; no temporary file is left.
        self.assertEqual(sorted(p.name for p in self.settings.parent.iterdir()), ['.settings.json.lock', 'settings.json'])
        self.assertEqual(stat.S_IMODE((self.settings.parent / '.settings.json.lock').stat().st_mode), 0o600)

    def test_output_is_ansible_to_nice_json_so_the_status_line_task_never_rewrites_it(self):
        from ansible.plugins.filter.core import to_nice_json
        self.write({'statusLine': {'type': 'command', 'command': '/usr/bin/node /x.js', 'refreshInterval': 5},
                    'model': 'opus', 'note': 'café'})
        self.assertEqual(self.run_hook().stdout, 'changed\n')
        text = self.settings.read_text()
        self.assertEqual(text, to_nice_json(json.loads(text)))


def converge_in_child(module, argv, pause, inside):
    """A forked merger: `module.main(argv)`, sleeping `pause` s inside its rename once `inside` is set.

    The pause sits between the final re-read and os.replace: the window in which two mergers that
    both passed the re-read would each rename, the later one dropping the earlier one's change.
    """
    real = os.replace

    def slow_replace(*args, **kwargs):
        if inside is not None:
            inside.set()
            time.sleep(pause)
        return real(*args, **kwargs)
    module.os.replace = slow_replace  # This child's os module only.
    with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
        code = module.main(argv)
    os._exit(code)


class SharedSettingsLock(unittest.TestCase):
    """claude_relay_hook.py (relay_connector) and claude_skill_hook.py share one lock and protocol."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='claude-settings-lock-')
        self.home = Path(self.temp.name)
        self.settings = self.home / '.claude/settings.json'
        self.settings.parent.mkdir(mode=0o700)
        self.lock = self.settings.parent / '.settings.json.lock'

    def tearDown(self):
        self.temp.cleanup()

    def test_both_helpers_carry_the_same_lock_protocol(self):
        import inspect
        for name in ('LOCK_NAME', 'LOCK_WAIT'):
            self.assertEqual(getattr(hook, name), getattr(connector_hook, name), name)
        self.assertEqual(hook.LOCK_NAME, '.settings.json.lock')
        for function in ('settings_lock', 'converge_locked'):
            self.assertEqual(inspect.getsource(getattr(hook, function)),
                             inspect.getsource(getattr(connector_hook, function)), function)
        # main() differs only in the name its error message gives.
        self.assertEqual(inspect.getsource(hook.main).replace('claude_skill_hook.py', 'claude_relay_hook.py'),
                         inspect.getsource(connector_hook.main))

    def overlap(self, first, second):
        """Run `first` until it is inside its rename, start `second`, and wait for both."""
        import multiprocessing
        context = multiprocessing.get_context('fork')
        inside = context.Event()
        a = context.Process(target=converge_in_child, args=(*first, 1.5, inside))
        a.start()
        self.assertTrue(inside.wait(20), 'the first merger never reached its rename')
        b = context.Process(target=converge_in_child, args=(*second, 0, None))
        b.start()
        for process in (a, b):
            process.join(60)
        self.assertEqual((a.exitcode, b.exitcode), (0, 0))

    def test_overlapping_connector_absent_and_skill_present_keep_the_skill_hook_only(self):
        user_hook = {'type': 'command', 'command': '/usr/local/bin/user-prompt-hook'}
        connector_absent = (connector_hook, ['--settings', str(self.settings), '--state', 'absent'])
        skill_present = (hook, ['--settings', str(self.settings), '--state', 'present', '--timeout', '3'])
        for first, second in [(connector_absent, skill_present), (skill_present, connector_absent)]:
            with self.subTest(first=first[0].__name__):
                # The connector role's v0.8.0 state: the user's hook and the old inbox-hint entry.
                self.settings.write_text(json.dumps(
                    {'model': 'opus', 'hooks': {'UserPromptSubmit': [{'hooks': [user_hook]}, {'hooks': [OLD_HINT]}]}}))
                self.overlap(first, second)
                self.assertEqual(json.loads(self.settings.read_text()),
                                 {'model': 'opus', 'hooks': {'UserPromptSubmit': [{'hooks': [user_hook]},
                                                                                  {'hooks': [SKILL_ENTRY]}]}})
                self.assertEqual(sorted(p.name for p in self.settings.parent.iterdir()),
                                 ['.settings.json.lock', 'settings.json'])

    def test_a_held_lock_holds_back_both_helpers_and_check_mode_needs_none(self):
        self.settings.write_text(json.dumps({'hooks': {'UserPromptSubmit': [{'hooks': [OLD_HINT]}]}}))
        before = self.settings.read_bytes()
        holder = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX)
            for module, argv in [(hook, ['--state', 'present', '--timeout', '3']),
                                 (connector_hook, ['--state', 'absent'])]:
                with self.subTest(module=module.__name__):
                    out, err = io.StringIO(), io.StringIO()
                    with mock.patch.object(module, 'LOCK_WAIT', 0.3), contextlib.redirect_stdout(out), \
                            contextlib.redirect_stderr(err):
                        code = module.main(['--settings', str(self.settings), *argv])
                    self.assertEqual((code, out.getvalue()), (1, ''))
                    self.assertIn('another merge has held', err.getvalue())
                    self.assertEqual(self.settings.read_bytes(), before)
                    # A report needs no lock.
                    with contextlib.redirect_stdout(out):
                        self.assertEqual(module.main(['--settings', str(self.settings), *argv, '--check']), 0)
        finally:
            os.close(holder)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hook.main(['--settings', str(self.settings), '--state', 'present', '--timeout', '3']), 0)

    def test_the_lock_is_never_taken_through_a_symlink_or_from_another_kind_of_file(self):
        self.settings.write_text('{}')
        elsewhere = self.home / 'elsewhere.lock'
        elsewhere.write_text('')
        self.lock.symlink_to(elsewhere)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(hook.main(['--settings', str(self.settings), '--state', 'present', '--timeout', '3']), 1)
        self.assertIn('cannot open the lock', err.getvalue())
        self.assertEqual(self.settings.read_text(), '{}')
        self.lock.unlink()
        self.lock.mkdir()
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(connector_hook.main(['--settings', str(self.settings), '--state', 'present',
                                                  '--command', OLD_HINT['command'], '--timeout', '2']), 1)
        self.assertIn('lock', err.getvalue())
        self.assertEqual(self.settings.read_text(), '{}')


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
