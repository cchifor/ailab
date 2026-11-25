#!/usr/bin/env python3
"""Prepare only the never-used, stopped c4 canary; never install or start it."""
import argparse
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid

HOME = Path('/home/c4')
PROFILES = ('1d9a7d6e-4b26-49fd-9053-7b0a22e1baae',
            'f25c187b-1e50-4c7c-860e-52947bfdf39e',
            'fdb56ca5-908b-40ee-94b3-aac735dd8622')
UNITS = ('relay-holder.service', 'relay-holder-control-plane.service')
FILES = {'profiles': ('.config/relay-holder/profiles.json', 0o600),
         'service': ('.config/relay-holder/service.json', 0o600),
         **{name: ('.config/systemd/user/' + name, 0o644) for name in UNITS}}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def directory(path, uid, private=False):
    s = path.lstat()
    require(stat.S_ISDIR(s.st_mode) and path.resolve() == path and s.st_uid == uid
            and not s.st_mode & 0o022, 'Unsafe directory: ' + str(path))
    if private:
        require(stat.S_IMODE(s.st_mode) == 0o700, 'Directory must be private: ' + str(path))
    return s


def inspect_file(path, uid, mode, digest):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        s = os.fstat(fd)
        require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1 and s.st_uid == uid
                and stat.S_IMODE(s.st_mode) == mode and s.st_size <= 1024 * 1024,
                'Unsafe file metadata: ' + str(path))
        with os.fdopen(os.dup(fd), 'rb') as stream:
            actual = hashlib.sha256(stream.read(1024 * 1024 + 1)).hexdigest()
        require(actual == digest, 'Old SHA mismatch: ' + str(path))
        return {'sha256': actual, 'uid': s.st_uid, 'mode': oct(mode),
                'device': s.st_dev, 'inode': s.st_ino, 'bytes': s.st_size}
    finally:
        os.close(fd)


def unit_status(name):
    result = subprocess.run(['systemctl', '--user', 'show', name,
                             '--property=Id,LoadState,ActiveState,SubState,MainPID,ControlGroup,UnitFileState,FragmentPath,DropInPaths'],
                            check=False, capture_output=True, text=True, timeout=10,
                            env={**os.environ, 'XDG_RUNTIME_DIR': '/run/user/1000'})
    row = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    require(result.returncode == 0 or (result.returncode in (1, 4)
            and row.get('LoadState') == 'not-found' and row.get('Id') == name),
            'Cannot inspect user service: ' + name)
    return row


def stopped(home, status, moved_units=()):
    for name in UNITS:
        row = status(name)
        fragment = home / '.config/systemd/user' / name
        expected = {'Id': name, 'ActiveState': 'inactive',
                    'MainPID': '0', 'ControlGroup': '', 'UnitFileState': 'disabled',
                    'FragmentPath': str(fragment), 'DropInPaths': '',
                    'LoadState': 'loaded', 'SubState': 'dead'}
        if name in moved_units:
            require(not os.path.lexists(fragment) and fragment.parent.resolve() == fragment.parent,
                    'Moved unit destination reappeared or changed: ' + name)
            require(row.get('UnitFileState') in ('disabled', '', 'bad')
                    and row.get('FragmentPath') in ('', str(fragment))
                    and row.get('LoadState') in ('loaded', 'not-found')
                    and row.get('SubState') in ('dead', 'not-found'),
                    'Unexpected moved-unit metadata: ' + name)
            for field in ('UnitFileState', 'FragmentPath', 'LoadState', 'SubState'):
                expected.pop(field)
        require(all(row.get(k) == v for k, v in expected.items()), 'Service is not cleanly stopped: ' + name)


def directory_identity(path, uid, private=False):
    s = directory(path, uid, private)
    return (s.st_dev, s.st_ino, s.st_uid, stat.S_IMODE(s.st_mode))


def rename_noreplace(source, destination):
    # Linux atomic no-clobber move: never emulate with a check followed by rename.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def receipt(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    sync_dir(path.parent)


def prepare(operation, expected, *, home=HOME, uid=1000, status=unit_status, move=rename_noreplace):
    """Injected paths/status/mover are test seams; the CLI offers no overrides."""
    require(str(uuid.UUID(operation)) == operation, 'Canonical operation UUID required')
    require(isinstance(expected, dict) and set(expected) == set(FILES)
            and all(isinstance(v, str) and re.fullmatch('[a-f0-9]{64}', v) for v in expected.values()),
            'Exactly four reviewed old SHA256 values required')
    home = Path(home)
    for part in ('', '.config', '.config/relay-holder', '.config/systemd', '.config/systemd/user',
                 '.local', '.local/state'):
        directory(home / part, uid)
    state = home / '.local/state/relay-holder'
    directory(state, uid, True)
    require({p.name for p in state.iterdir()} == {'native-homes', 'workspaces'},
            'State is not never-used (journal/socket/lock/unknown entry)')
    placeholders = {}
    for root in (state / 'native-homes', state / 'workspaces'):
        directory(root, uid, True)
        require({p.name for p in root.iterdir()} == set(PROFILES), 'Unexpected profile directory set')
        for profile in PROFILES:
            child = root / profile
            directory(child, uid, True)
            require(not any(child.iterdir()), 'Profile directory is not empty: ' + str(child))
            if root.name == 'native-homes':
                placeholders[child] = directory_identity(child, uid, True)
    stopped(home, status)
    records = {key: inspect_file(home / rel, uid, mode, expected[key])
               for key, (rel, mode) in FILES.items()}
    parents = {path: directory_identity(path, uid) for path in
               {state, state / 'native-homes', *( (home / rel).parent for rel, _ in FILES.values())}}
    backup_root = home / '.config/relay-holder/restaging'
    if backup_root.exists():
        directory(backup_root, uid, True)
    backup = backup_root / operation
    require(not backup.exists() and not backup.is_symlink(), 'Operation already exists')
    device = state.stat().st_dev
    require((home / '.config/relay-holder').stat().st_dev == device
            and all(r['device'] == device for r in records.values()), 'Same filesystem required')
    for profile in PROFILES:
        require((state / 'native-homes' / profile).stat().st_dev == device, 'Same filesystem required')

    # Leave this inode in place permanently: unlinking a flock file enables split locks.
    lock = os.open(state / 'holder.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(lock, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sync_dir(state)
        stopped(home, status)
        require({p.name for p in state.iterdir()} == {'native-homes', 'workspaces', 'holder.lock'},
                'State changed while acquiring startup lock')
        backup_root.mkdir(mode=0o700, exist_ok=True)
        directory(backup_root, uid, True)
        require(backup_root.stat().st_dev == device, 'Backup must share filesystem')
        backup.mkdir(mode=0o700)  # exclusive operation claim
        parents[backup] = directory_identity(backup, uid, True)
        sync_dir(backup_root)
        plan = [(home / rel, backup / key, key) for key, (rel, _) in FILES.items()]
        plan += [(state / 'native-homes' / p, backup / ('placeholder-' + p), None) for p in PROFILES]
        receipt(backup / '00-prepared.json', {'operation': operation, 'files': records,
                'moves': [{'source': str(src), 'backup': str(dst)} for src, dst, _ in plan]})
        moved_units = set()
        for index, (src, dst, key) in enumerate(plan, 1):
            stopped(home, status, moved_units)
            for parent, identity in parents.items():
                require(directory_identity(parent, uid) == identity, 'Parent directory changed since preflight')
            if key:
                require(inspect_file(src, uid, FILES[key][1], expected[key]) == records[key],
                        'File changed since preflight')
            else:
                require(directory_identity(src, uid, True) == placeholders[src],
                        'Placeholder identity changed since preflight')
                require(not any(src.iterdir()), 'Placeholder changed since preflight')
            move(src, dst)
            if key in UNITS:
                moved_units.add(key)
            sync_dir(src.parent)
            sync_dir(backup)
            receipt(backup / ('%02d-moved.json' % index), {'source': str(src), 'backup': str(dst)})
        require(not any((state / 'native-homes').iterdir()), 'Native root changed during preparation')
        stopped(home, status, moved_units)
        receipt(backup / 'complete.json', {'operation': operation, 'state': 'prepared-stopped',
                'next': 'Reviewed role rerun with start:false; no activation authorized'})
        return {'operation': operation, 'state': 'prepared-stopped', 'backup': str(backup)}
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation', required=True)
    parser.add_argument('--expected-sha256', required=True, type=Path)
    args = parser.parse_args()
    require(os.getuid() == 1000, 'Run only as c4 UID1000')
    expected = json.loads(args.expected_sha256.read_text())
    print(json.dumps(prepare(args.operation, expected)))


if __name__ == '__main__':
    main()
