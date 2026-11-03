#!/usr/bin/env python3
"""Converge Relay's inbox hook in one user's Claude Code settings.json.

The relay_connector role pipes this file to `python3 -` as the settings' owner: once with --check
before anything on the worker changes (tasks/claude_preflight.yml), and once more to commit after
the MCP change succeeded (tasks/claude.yml). Standard library only.

It removes every UserPromptSubmit hook entry whose command is, character for character, the
literal form this role writes:

    <absolute path ending in /relay-connector> --state <absolute path> inbox-hint

with single spaces and only characters that need no shell quoting. Anything else is the user's
and stays: a command that mentions Relay, chains it (`;`, `&&`, `|`), redirects it, quotes it or
substitutes into it (`$(...)`, backticks). For --state present it then appends one entry:

    {"hooks": [{"type": "command", "command": <--command>, "timeout": <--timeout>}]}

Everything else in the file is kept. A matcher group is dropped only when this removal leaves it
empty, and so are the UserPromptSubmit list and the `hooks` object, so `absent` undoes `present`.

The file is written only when its content changes, atomically (temp file and rename in the same
directory), with the file's mode and group, or 0644 for a new file in a ~/.claude created 0700.
A symlinked settings.json is resolved once; the link stays and its target is replaced. Just
before the rename the target is read again; if anyone changed it meanwhile (content or any of
device, inode, size, mtime, ctime, owner, group, mode, extended attributes), every check and the
merge are redone from the new state, up to three times.

A write happens only under the lock every estate merger of this file shares (settings_lock, the
same in the relay_agent_skill role's files/claude_skill_hook.py): an exclusive flock on
.settings.json.lock next to the settings path as given (~/.claude/.settings.json.lock), held from
the read through the rename. Without it, two mergers could both pass the re-read above and then
rename in turn, so one would drop the other's change (or put back a hook the other removed).
--check, and a run with nothing to change, take no lock and create nothing.

Errors (exit 1, nothing written, also under --check): a file that does not parse, or whose
UserPromptSubmit hooks are not the documented shape. And, only when the merge would change the
file: a file owned by another user, in a group this user cannot keep, or carrying a POSIX ACL; a
directory with a default POSIX ACL (a new file would inherit it); a directory or file this user
cannot write. A file that is already converged is left alone whatever its owner or ACLs, so it never
blocks a run. Prints `changed` or `unchanged`; --check reports without writing.
"""
import argparse
import contextlib
import copy
import errno
import fcntl
import hashlib
import json
import math
import os
import shlex
import stat
import sys
import tempfile
import time

EVENT = 'UserPromptSubmit'
ATTEMPTS = 3
ACCESS_ACL = 'system.posix_acl_access'
DEFAULT_ACL = 'system.posix_acl_default'
LOCK_NAME = '.settings.json.lock'
LOCK_WAIT = 30.0


class SettingsError(Exception):
    pass


class ChangedMeanwhile(Exception):
    pass


def is_relay_inbox_hint(hook):
    """True only for the exact, literal hook entry this role writes, whatever the paths."""
    if not isinstance(hook, dict) or hook.get('type') != 'command':
        return False
    command = hook.get('command')
    if not isinstance(command, str):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False  # Not ours to interpret: keep it.
    # Literal: every word needs no quoting and the string is exactly those words joined by single
    # spaces, so no shell syntax (;, &&, |, <, >, $(...), backticks, quotes, globs) can hide in it.
    return (command == ' '.join(words) and all(shlex.quote(word) == word for word in words)
            and len(words) == 4
            and words[0].startswith('/') and os.path.basename(words[0]) == 'relay-connector'
            and words[1] == '--state' and words[2].startswith('/')
            and words[3] == 'inbox-hint')


def _positive_seconds(value):
    # Claude Code takes any positive number of seconds, 1.5 as well as 2.
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def validate(groups):
    """Reject UserPromptSubmit groups and entries that are not the documented shape."""
    for index, group in enumerate(groups):
        where = f'hooks.{EVENT}[{index}]'
        if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
            raise SettingsError(f"'{where}' is not an object with a 'hooks' array")
        if 'matcher' in group and not isinstance(group['matcher'], str):
            raise SettingsError(f"'{where}.matcher' is not a string")
        for position, entry in enumerate(group['hooks']):
            at = f'{where}.hooks[{position}]'
            if not isinstance(entry, dict):
                raise SettingsError(f"'{at}' is not an object")
            if not isinstance(entry.get('type'), str) or not entry['type']:
                raise SettingsError(f"'{at}' has no string 'type'")
            if entry['type'] == 'command':
                if not isinstance(entry.get('command'), str) or not entry['command']:
                    raise SettingsError(f"'{at}' is a command hook without a string 'command'")
                if 'timeout' in entry and not _positive_seconds(entry['timeout']):
                    raise SettingsError(f"'{at}.timeout' is not a positive number of seconds")


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
    validate(groups)

    kept, removed = [], 0
    for group in groups:
        entries = [entry for entry in group['hooks'] if not is_relay_inbox_hint(entry)]
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


def _extended_attributes(target):
    try:
        return os.listxattr(target)
    except OSError as error:
        if error.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            return []
        raise


def read(target):
    """(fstat, bytes, xattr names) of the regular file at `target`, or None when it is missing."""
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, 'rb') as handle:
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode):
            raise SettingsError('it is not a regular file')
        return status, handle.read(), _extended_attributes(handle.fileno())


def identity(found):
    """Everything a concurrent change could alter: any difference forces a full re-check."""
    if found is None:
        return None
    status, raw, attributes = found
    return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns,
            status.st_uid, status.st_gid, status.st_mode, tuple(sorted(attributes)),
            hashlib.sha256(raw).hexdigest())


def check_metadata(status, attributes):
    """Refuse a file whose owner, group or ACL a rewrite by this user could not keep."""
    user = os.geteuid()
    if status.st_uid != user:
        raise SettingsError(f'it is owned by uid {status.st_uid}, not by this user (uid {user}); '
                            'give it back to the user')
    if status.st_gid != os.getegid() and status.st_gid not in os.getgroups():
        raise SettingsError(f'its group (gid {status.st_gid}) is not one of this user\'s groups, '
                            'so a rewrite could not keep it')
    if ACCESS_ACL in attributes:
        raise SettingsError('it carries a POSIX ACL, which a rewrite would drop; remove the ACL '
                            '(setfacl -b) or add the hook by hand')


def _existing(directory):
    while not os.path.isdir(directory):
        directory = os.path.dirname(directory)
    return directory


def check_directory(directory):
    """Refuse a directory (or, if it is missing, the parent it would be created in) whose default
    POSIX ACL a new file would inherit."""
    existing = _existing(directory)
    if DEFAULT_ACL in _extended_attributes(existing):
        raise SettingsError(f'{existing} has a default POSIX ACL, which the rewritten file would '
                            f'inherit; remove it (setfacl -k {existing}) or add the hook by hand')


def check_writable(target, found):
    """Refuse, already under --check, a write this user could not make."""
    existing = _existing(os.path.dirname(target))
    if not os.access(existing, os.W_OK | os.X_OK):
        raise SettingsError(f'{existing} is not writable for this user, so the file cannot be '
                            'replaced there')
    if found is not None and not os.access(target, os.W_OK):
        raise SettingsError('it is not writable for this user')


def _before_replace():
    """Test seam: runs between writing the temporary file and re-checking the target."""


def replace(target, found, text):
    """Atomically put `text` at `target`, unless the target is no longer what was `found`."""
    directory = os.path.dirname(target)
    if found is None:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.settings.json.', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            if ACCESS_ACL in _extended_attributes(handle.fileno()):
                raise SettingsError(f'a new file in {directory} inherits a POSIX ACL from its default '
                                    f'ACL; remove it (setfacl -k {directory}) or add the hook by hand')
            handle.write(text)
            handle.flush()
            if found is None:
                os.fchmod(handle.fileno(), 0o644)
            else:
                status = found[0]
                if os.fstat(handle.fileno()).st_gid != status.st_gid:
                    os.fchown(handle.fileno(), -1, status.st_gid)
                os.fchmod(handle.fileno(), stat.S_IMODE(status.st_mode))  # After chown: it clears setgid.
            os.fsync(handle.fileno())
        _before_replace()
        if identity(read(target)) != identity(found):
            raise ChangedMeanwhile()
        if found is None:
            try:
                os.link(temporary, target)  # Fails instead of replacing a file created meanwhile.
            except FileExistsError:
                raise ChangedMeanwhile() from None
        else:
            os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    try:  # Best effort: the rename is already done.
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


@contextlib.contextmanager
def settings_lock(settings):
    """Hold the lock every estate merger of `settings` shares, from its read through its rename.

    An exclusive flock on LOCK_NAME in the directory of the settings path as given (not resolved),
    so ~/.claude/.settings.json.lock for ~/.claude/settings.json. Created 0600, never through a
    symlink, and only a regular file of this user's counts. Waits up to LOCK_WAIT seconds.
    Byte-identical in claude_relay_hook.py (relay_connector) and claude_skill_hook.py
    (relay_agent_skill): one protocol, one lock.
    """
    directory = os.path.dirname(os.path.abspath(settings))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, LOCK_NAME)
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as error:
        raise SettingsError(f'cannot open the lock {path} ({error.strerror})') from None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_uid != os.geteuid():
            raise SettingsError(f'the lock {path} is not a regular file of this user')
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SettingsError(f'another merge has held {path} for {LOCK_WAIT:g} s; '
                                        'converge again once it is done') from None
                time.sleep(0.05)
        yield
    finally:
        os.close(descriptor)  # Releases the flock.


def parse(raw):
    try:
        return json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as error:
        raise SettingsError(f'it is not valid JSON ({error})') from None


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
        if not args.command or not is_relay_inbox_hint({'type': 'command', 'command': args.command}):
            parser.error('--state present needs a literal --command of the form '
                         '"/path/to/relay-connector --state /path/to/state inbox-hint"')
        if args.timeout is None or args.timeout < 1:
            parser.error('--state present needs a positive --timeout')
    args.settings = os.path.expanduser(args.settings)
    return args


def converge_file(target, args, write):
    """One read-check-merge pass, writing only with `write`. Returns 'changed' or 'unchanged'."""
    found = read(target)
    if found is None:
        if args.state == 'absent':
            return 'unchanged'
        current = None
    else:
        current = parse(found[1])  # JSON null is a value like any other, not a missing file.
    desired = converge({} if found is None else current, args.state, args.command, args.timeout)
    if found is not None and desired == current:
        return 'unchanged'
    # Only a rewrite has to keep the owner, group and ACLs, or be allowed at all.
    if found is not None:
        check_metadata(found[0], found[2])
    check_directory(os.path.dirname(target))
    check_writable(target, found)
    if write:
        replace(target, found, render(desired))
    return 'changed'


def converge_locked(target, args):
    """Every pass that writes, under settings_lock, redone from the new state on a change."""
    with settings_lock(args.settings):
        for _ in range(ATTEMPTS):
            try:
                return converge_file(target, args, write=True)
            except ChangedMeanwhile:
                continue  # Re-read, and re-run every check, from the new state.
    raise SettingsError(f'it changed while being updated, {ATTEMPTS} times in a row; '
                        'converge again once it is quiet')


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    target = os.path.realpath(args.settings)  # Resolved once: read and replace the same file.
    try:
        # A first pass without the lock: a report (--check) or nothing to change needs no lock and
        # creates nothing. A change is then made from a fresh read under the lock.
        result = converge_file(target, args, write=False)
        if result == 'changed' and not args.check:
            result = converge_locked(target, args)
        print(result)
        return 0
    except (SettingsError, OSError) as error:
        print(f'claude_relay_hook.py: {args.settings}: {error}; nothing was written',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
