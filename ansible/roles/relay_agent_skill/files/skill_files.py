#!/usr/bin/env python3
"""Install or remove the relay-bus skill's files in one user's home, never through a link.

The relay_agent_skill role runs this as the user, never as root (`python3 -I -c <this file>`):
once with --check before anything on the worker changes (tasks/validate.yml), and once to commit
(tasks/present.yml, tasks/absent.yml). Standard library only.

Every path is reached one component at a time from the home directory, each directory opened with
O_NOFOLLOW relative to its parent's descriptor, and every change is made relative to those
descriptors (mkdirat, fchmod, openat with O_EXCL and O_NOFOLLOW, renameat, unlinkat, rmdirat). So
nothing is ever followed. A symlink on the way, a directory where a file goes, any other kind of
file, or a path another user owns is refused (exit 1). A first pass makes every check before the
first change, including that each planned change can be made (the directory it changes is
writable, or about to be given an owner-writable mode by this run, and not on a read-only file
system), so a refusal changes nothing; a path swapped after it meets the same no-follow
operations in the second pass. Ownership and other modes are never repaired: that is the owner's
call, or would need root.

present reads {"origin": ..., "files": [{"name", "mode", "sha256", "content"}, ...]} as JSON on
stdin. Every file's bytes must hash to its pinned sha256 before anything is written.
  - ~/.claude and ~/.codex (0700), their skills/ (0755) and ~/.config (0700) are created when
    missing and otherwise left as they are.
  - ~/.claude/skills/relay-bus and ~/.codex/skills/relay-bus (0755), and ~/.config/relay and
    ~/.config/relay/agents (0700), are created or get those modes. Nothing inside them is touched
    but the files below; key files never are.
  - Each skill file in both skill directories, and ~/.config/relay/bus.json ({"origin": ...}, 0600),
    is written only when its bytes or mode differ: a new temporary file in the same directory, then
    a rename over the old name (which replaces a link there instead of writing through it).
absent removes, from each skill directory, the files the role installs (SKILL.md, relay-bus.py),
and then the directory if that left it empty. Anything else there stays, and so does the directory
then. ~/.config/relay is never touched.

Prints `changed` or `unchanged`; --check reports without changing anything.
"""
import argparse
import contextlib
import errno
import hashlib
import json
import os
import secrets
import stat
import sys

HARNESSES = ('.claude', '.codex')
SKILL_FILES = {'SKILL.md': 0o644, 'relay-bus.py': 0o755}
# (path, mode, whether an existing directory gets the mode too), parents first.
DIRECTORIES = [('.config', 0o700, False), ('.config/relay', 0o700, True), ('.config/relay/agents', 0o700, True)]
for _harness in HARNESSES:
    DIRECTORIES += [(_harness, 0o700, False), (_harness + '/skills', 0o755, False),
                    (_harness + '/skills/relay-bus', 0o755, True)]
NO_FOLLOW_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Refused(Exception):
    pass


class Descriptors:
    """The directories walked so far, opened without following links and closed together."""

    def __init__(self, home):
        self.opened = []
        self.home = self.keep(os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))

    def keep(self, descriptor):
        self.opened.append(descriptor)
        return descriptor

    def directory(self, parent, name, shown):
        """The directory `name` in `parent`, or None when it is missing."""
        try:
            descriptor = os.open(name, NO_FOLLOW_DIRECTORY, dir_fd=parent)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                raise Refused(f'{shown} is not a directory (a symlink or a file); the role never follows '
                              'or replaces it: fix it by hand') from None
            raise
        self.keep(descriptor)
        owner = os.fstat(descriptor).st_uid
        if owner != os.geteuid():
            raise Refused(f'{shown} is owned by uid {owner}, not by this user (uid {os.geteuid()}); '
                          'the role does not repair ownership: fix it by hand')
        return descriptor

    def close(self):
        for descriptor in self.opened:
            os.close(descriptor)


def regular_file(parent, name, shown):
    """The status of the user's regular file `name` in `parent`, or None when it is missing."""
    try:
        status = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(status.st_mode):
        raise Refused(f'{shown} is not a regular file (a symlink, a directory or another kind); the '
                      'role never writes into, through or over it: move it aside')
    if status.st_uid != os.geteuid():
        raise Refused(f'{shown} is owned by uid {status.st_uid}, not by this user (uid {os.geteuid()}); '
                      'the role does not repair ownership: fix it by hand')
    return status


def read_bytes(parent, name, shown):
    """The bytes of the regular file `name` in `parent`. Opened with O_NONBLOCK and checked with
    fstat before reading, so a FIFO swapped in after regular_file() fails at once, never blocks."""
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    with os.fdopen(descriptor, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise Refused(f'{shown} is not a regular file (a symlink, a directory or another kind); the '
                          'role never writes into, through or over it: move it aside')
        return handle.read()


def write_file(parent, name, mode, data):
    """`data` at `name` in `parent`, atomically: a fresh temporary file, then a rename."""
    temporary = f'.{name}.{secrets.token_hex(6)}.tmp'
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent)
        raise


def wanted_files(payload):
    """[(directory, name, mode, bytes)] from the payload, each skill file checked against its pin."""
    if not isinstance(payload, dict) or not isinstance(payload.get('origin'), str) \
            or not isinstance(payload.get('files'), list):
        raise Refused('stdin is not {"origin": ..., "files": [...]}')
    files = []
    for entry in payload['files']:
        name = entry.get('name') if isinstance(entry, dict) else None
        if name not in SKILL_FILES or entry.get('mode') != f'{SKILL_FILES[name]:04o}' \
                or not isinstance(entry.get('content'), str) or not isinstance(entry.get('sha256'), str):
            raise Refused(f'stdin names an unexpected skill file: {name!r}')
        data = entry['content'].encode('utf-8')
        if hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise Refused(f'{name} does not hash to its pinned sha256 {entry["sha256"]}; nothing is installed')
        files += [(harness + '/skills/relay-bus', name, SKILL_FILES[name], data) for harness in HARNESSES]
    if sorted({name for _, name, _, _ in files}) != sorted(SKILL_FILES):
        raise Refused('stdin does not carry both skill files')
    bus = json.dumps({'origin': payload['origin']}, indent=4, sort_keys=True, separators=(',', ': ')) + '\n'
    return files + [('.config/relay', 'bus.json', 0o600, bus.encode('utf-8'))]


def changeable(descriptor, shown, why, repaired=False):
    """Refuse, already in the first pass, a directory whose entries (or mode) this user could not
    change: on a read-only file system, or without write and search permission. `repaired` is a
    directory present() gives an owner-writable mode itself before it changes anything inside."""
    if os.fstatvfs(descriptor).f_flag & os.ST_RDONLY:
        raise Refused(f'{shown} is on a read-only file system, so {why}')
    if not repaired and not os.access('.', os.W_OK | os.X_OK, dir_fd=descriptor):
        raise Refused(f'{shown} is not writable for this user, so {why}; '
                      'the role does not change its mode: fix it by hand')


def present(tree, files, write):
    """One pass over every directory and file `present` manages. Returns whether anything differs.

    The first pass (write=False) also proves every change it plans can be made: each directory it
    would create an entry in, or give a mode, is writable (or about to be made so by the role) and
    not on a read-only file system. So a refusal comes before the first change, never after some.
    """
    changed = False
    opened, repaired = {'': tree.home}, {'': False}
    for path, mode, enforced in DIRECTORIES:
        parent_path = os.path.dirname(path)
        parent, name, shown = opened[parent_path], os.path.basename(path), '~/' + path
        parent_shown = '~/' + parent_path if parent_path else 'the home directory'
        descriptor = None if parent is None else tree.directory(parent, name, shown)
        repaired[path] = False
        if descriptor is None:
            changed = True
            if parent is not None:  # A missing parent is created by this run, writable.
                changeable(parent, parent_shown, f'{shown} cannot be created there', repaired[parent_path])
            if write:
                os.mkdir(name, mode, dir_fd=parent)
                descriptor = tree.directory(parent, name, shown)
                os.fchmod(descriptor, mode)  # mkdir's mode is masked by the umask.
        elif enforced and stat.S_IMODE(os.fstat(descriptor).st_mode) != mode:
            changed = True
            changeable(descriptor, shown, f'its mode cannot be set to {mode:04o}', repaired=True)
            repaired[path] = True  # Every mode the role sets lets the owner write.
            if write:
                os.fchmod(descriptor, mode)
        opened[path] = descriptor
    for directory, name, mode, data in files:
        parent, shown = opened[directory], f'~/{directory}/{name}'
        status = None if parent is None else regular_file(parent, name, shown)
        if status is not None and stat.S_IMODE(status.st_mode) == mode and read_bytes(parent, name, shown) == data:
            continue
        changed = True
        if parent is not None:
            changeable(parent, '~/' + directory, f'{shown} cannot be written there', repaired[directory])
        if write:
            write_file(parent, name, mode, data)
    return changed


def absent(tree, write):
    """One pass over both skill directories. Returns whether anything is (or was) to remove.

    The first pass also proves each removal it plans can be made (the directory it removes from is
    writable and not on a read-only file system), before the first removal.
    """
    plans = []
    for harness in HARNESSES:
        top = tree.directory(tree.home, harness, '~/' + harness)
        skills = None if top is None else tree.directory(top, 'skills', f'~/{harness}/skills')
        skill = None if skills is None else tree.directory(skills, 'relay-bus', f'~/{harness}/skills/relay-bus')
        if skill is None:
            continue
        shown = f'~/{harness}/skills/relay-bus'
        files = [name for name in SKILL_FILES if regular_file(skill, name, f'{shown}/{name}') is not None]
        emptied = not [name for name in os.listdir(skill) if name not in files]
        if files:
            changeable(skill, shown, f'{" and ".join(files)} cannot be removed from it')
        if emptied:
            changeable(skills, f'~/{harness}/skills', f'{shown} cannot be removed from it')
        plans.append((skills, skill, files, emptied))
    if write:
        for skills, skill, files, emptied in plans:
            for name in files:
                os.unlink(name, dir_fd=skill)
            if emptied:
                try:
                    os.rmdir('relay-bus', dir_fd=skills)  # A link or a full directory there fails.
                except OSError as error:
                    if error.errno != errno.ENOTEMPTY:
                        raise  # Something added meanwhile stays, and so does the directory.
    return any(files or emptied for _, _, files, emptied in plans)


def run(args, files, write):
    tree = Descriptors(args.home)
    try:
        return present(tree, files, write) if args.state == 'present' else absent(tree, write)
    finally:
        tree.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog='skill_files.py',
                                     description="Install or remove the relay-bus skill's files, never through a link.")
    parser.add_argument('--home', required=True, help="the user's home directory")
    parser.add_argument('--state', choices=['present', 'absent'], required=True)
    parser.add_argument('--check', action='store_true', help='report without changing anything')
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    stage = 'nothing was changed'
    try:
        files = wanted_files(json.load(sys.stdin)) if args.state == 'present' else None
        changed = run(args, files, write=False)  # Every check, before the first change.
        if changed and not args.check:
            stage = 'it stopped part way'
            run(args, files, write=True)
        print('changed' if changed else 'unchanged')
        return 0
    except (Refused, OSError, ValueError) as error:
        print(f'skill_files.py: {error}; {stage}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
