#!/usr/bin/env python3
"""Converge Relay's inbox hook in one user's Claude Code settings.json.

The relay_connector role pipes this file to `python3 -` as the settings' owner (tasks/claude.yml).
Standard library only.

It removes every UserPromptSubmit hook entry whose command runs a `relay-connector` binary with
`inbox-hint`. For --state present it then appends one entry for the pinned connector:

    {"hooks": [{"type": "command", "command": <--command>, "timeout": <--timeout>}]}

Everything else in the file is kept. A matcher group is dropped only when this removal leaves it
empty, and so are the UserPromptSubmit list and the `hooks` object, so `absent` undoes `present`.

The file is written only when its content changes: atomically (temp file and rename in the same
directory), keeping the file's mode, or 0644 for a new file in a ~/.claude created 0700. A
symlinked settings.json keeps its link; the target is replaced. Prints `changed` or `unchanged`.
--check reports the same without writing. A file that does not parse, or whose `hooks` is not
the documented shape, is an error (exit 1) and is never rewritten.
"""
import argparse
import copy
import json
import os
import shlex
import stat
import sys
import tempfile

EVENT = 'UserPromptSubmit'


class SettingsError(Exception):
    pass


def is_relay_inbox_hint(hook):
    """True for a hook entry whose command runs a relay-connector binary with `inbox-hint`."""
    command = hook.get('command') if isinstance(hook, dict) else None
    if not isinstance(command, str):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    for index, word in enumerate(words):
        if os.path.basename(word) == 'relay-connector':
            return 'inbox-hint' in words[index + 1:]
    return False


def converge(settings, state, command=None, timeout=None):
    """Return a converged copy of the parsed settings; `settings` itself is not modified."""
    if not isinstance(settings, dict):
        raise SettingsError('the top level is not a JSON object')
    result = copy.deepcopy(settings)
    had_hooks = 'hooks' in result
    hooks = result.get('hooks', {})
    if not isinstance(hooks, dict):
        raise SettingsError("'hooks' is not a JSON object")
    groups = hooks.get(EVENT, [])
    if not isinstance(groups, list):
        raise SettingsError(f"'hooks.{EVENT}' is not a JSON array")

    kept, removed = [], 0
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get('hooks'), list):
            entries = [hook for hook in group['hooks'] if not is_relay_inbox_hint(hook)]
            if len(entries) != len(group['hooks']):
                removed += len(group['hooks']) - len(entries)
                if not entries:
                    continue  # Emptied by this removal: drop the group.
                group = {**group, 'hooks': entries}
        kept.append(group)
    if state == 'present':
        kept.append({'hooks': [{'type': 'command', 'command': command, 'timeout': timeout}]})

    if kept:
        hooks[EVENT] = kept
    elif removed:
        hooks.pop(EVENT, None)
    if hooks or (had_hooks and not removed):
        result['hooks'] = hooks
    else:
        result.pop('hooks', None)
    return result


def render(settings):
    # Byte-identical to Ansible's to_nice_json, which the dev_worker role's status-line task
    # writes into this same file: the two converges never rewrite each other's output.
    return json.dumps(settings, indent=4, sort_keys=True, separators=(',', ': '))


def load(path):
    """Parsed settings, or None for a missing file. Raises SettingsError when it does not parse."""
    try:
        with open(path, 'rb') as handle:
            raw = handle.read()
    except FileNotFoundError:
        return None
    try:
        return json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as error:
        raise SettingsError(f'it is not valid JSON ({error})') from None


def write_atomically(path, text):
    target = os.path.realpath(path)
    directory = os.path.dirname(target)
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        mode = 0o644
        os.makedirs(directory, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.settings.json.', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    try:  # Best effort: the rename is already done.
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog='claude_relay_hook.py',
        description="Converge Relay's inbox hook in one user's Claude Code settings.json.")
    parser.add_argument('--settings', default=os.path.join('~', '.claude', 'settings.json'),
                        help='Claude Code user settings file (default: ~/.claude/settings.json)')
    parser.add_argument('--state', choices=['present', 'absent'], required=True)
    parser.add_argument('--command', help='hook command; required for present')
    parser.add_argument('--timeout', type=int, help='hook timeout in seconds; required for present')
    parser.add_argument('--check', action='store_true', help='report without writing')
    args = parser.parse_args(argv)
    if args.state == 'present':
        if not args.command or not is_relay_inbox_hint({'command': args.command}):
            parser.error('--state present needs a --command that runs relay-connector ... inbox-hint')
        if args.timeout is None or args.timeout < 1:
            parser.error('--state present needs a positive --timeout')
    args.settings = os.path.expanduser(args.settings)
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        current = load(args.settings)
        if current is None and args.state == 'absent':
            print('unchanged')
            return 0
        desired = converge({} if current is None else current, args.state, args.command, args.timeout)
        if current is not None and desired == current:
            print('unchanged')
            return 0
        if not args.check:
            write_atomically(args.settings, render(desired))
    except (SettingsError, OSError) as error:
        print(f'claude_relay_hook.py: {args.settings}: {error}; nothing was written',
              file=sys.stderr)
        return 1
    print('changed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
