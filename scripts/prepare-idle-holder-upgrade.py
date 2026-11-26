#!/usr/bin/env python3
"""Prepare four configuration files for an explicitly sealed, never-launched c4 holder."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import uuid

# Reuse the qualified no-clobber moves, private path checks and stopped-unit
# interpretation. The old helper's never-started preparation is never called.
SPEC = importlib.util.spec_from_file_location(
    'holder_restage_primitives', Path(__file__).with_name('restage-inference-canary.py'))
common = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(common)
FILES, UNITS, PROFILES = common.FILES, common.UNITS, common.PROFILES
MAX_REVISION = 9007199254740991
MAX_JOURNAL = 64 * 1024 * 1024
MAX_RECORD = 64 * 1024
require = common.require


def canonical_uuid(value):
    require(isinstance(value, str) and str(uuid.UUID(value)) == value, 'Canonical UUID required')
    return value


def sha(value):
    require(isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value), 'SHA256 required')
    return value


def exact_keys(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys), 'Unexpected object fields')


def parse_json(data):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            require(key not in obj, 'Duplicate JSON field')
            obj[key] = value
        return obj

    def nonfinite(_value):
        raise ValueError('Nonfinite JSON value')

    return json.loads(data, object_pairs_hook=unique, parse_constant=nonfinite)


def request(value):
    exact_keys(value, ('version', 'operationId', 'expectedFiles', 'journal'))
    require(type(value['version']) is int and value['version'] == 1, 'Unsupported request version')
    canonical_uuid(value['operationId'])
    exact_keys(value['expectedFiles'], FILES)
    for digest in value['expectedFiles'].values():
        sha(digest)
    exact_keys(value['journal'], ('sha256', 'journalId', 'incarnation', 'sealId'))
    sha(value['journal']['sha256'])
    for name in ('journalId', 'incarnation', 'sealId'):
        canonical_uuid(value['journal'][name])
    require(value['operationId'] != value['journal']['sealId'],
            'Preparation operation must differ from the maintenance seal')
    return value


def journal_proof(data, expected, preparation_id=None):
    """Mirror the v1 Record and admission transitions, permitting no worker events."""
    require(0 < len(data) <= MAX_JOURNAL and data.endswith(b'\n'), 'Invalid journal size or tail')
    require(hashlib.sha256(data).hexdigest() == expected['sha256'], 'Journal SHA mismatch')
    revision, paused, seal, current, first = 0, False, None, None, None
    operations, incarnations = set(), set()
    count = 0
    # The Rust reader frames with read_until(b'\n'), not the broader splitlines
    # delimiter set. JSON whitespace (including a CR before LF) remains valid.
    for count, body in enumerate(data[:-1].split(b'\n'), 1):
        line = body + b'\n'
        require(len(line) <= MAX_RECORD and line.endswith(b'\n'), 'Invalid journal record boundary')
        record = parse_json(line)
        exact_keys(record, ('version', 'sequence', 'incarnation', 'event'))
        require(type(record['version']) is int and record['version'] == 1,
                'Compacted or unsupported journal refused')
        require(type(record['sequence']) is int and record['sequence'] == count
                and count <= MAX_REVISION, 'Journal sequence gap')
        incarnation = canonical_uuid(record['incarnation'])
        event = record['event']
        require(isinstance(event, dict), 'Journal event must be an object')
        kind = event.get('kind')
        if kind == 'holder-started':
            exact_keys(event, ('kind', 'version'))
            require(type(event['version']) is int and event['version'] == 1,
                    'Unsupported holder protocol')
            require(incarnation not in incarnations, 'Repeated holder incarnation')
            incarnations.add(incarnation)
            current = incarnation
            first = first or incarnation
            continue
        require(current is not None and incarnation == current, 'Journal incarnation changed')
        # No accepted command, installation, preflight refusal, session, update,
        # compaction or unknown event qualifies, even if its outcome was failure.
        require(kind in ('holder-admission', 'holder-maintenance-seal'),
                'Journal contains a command/session or unsupported event')
        keys = ('kind', 'operationId', 'revision', 'paused') if kind == 'holder-admission' else (
            'kind', 'operationId', 'revision', 'expectedIncarnation', 'sealId', 'sealed')
        exact_keys(event, keys)
        operation = canonical_uuid(event['operationId'])
        require(operation not in operations, 'Repeated admission operation')
        require(type(event['revision']) is int and event['revision'] == revision + 1
                and event['revision'] <= MAX_REVISION, 'Invalid admission revision')
        if kind == 'holder-admission':
            require(seal is None and type(event['paused']) is bool, 'Invalid admission transition')
            paused = event['paused']
        else:
            require(paused and type(event['sealed']) is bool, 'Seal requires paused admission')
            require(canonical_uuid(event['expectedIncarnation']) == incarnation,
                    'Maintenance incarnation mismatch')
            seal_id = canonical_uuid(event['sealId'])
            if event['sealed']:
                require(seal is None and seal_id == operation, 'Invalid new seal')
                seal = seal_id
            else:
                require(seal == seal_id, 'Invalid seal release')
                seal = None
        operations.add(operation)
        revision = event['revision']
    require(first == expected['journalId'] and current == expected['incarnation'],
            'Journal identity mismatch')
    require(paused and seal == expected['sealId'], 'Holder must remain durably paused and sealed')
    require(preparation_id not in operations, 'Preparation operation already appears in the journal')
    return dict(journalId=first, incarnation=current, sealId=seal,
                admissionRevision=revision, records=count, sha256=expected['sha256'])


def private_open(path, uid):
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        meta = os.fstat(fd)
        require(stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1 and meta.st_uid == uid
                and stat.S_IMODE(meta.st_mode) == 0o600, 'Unsafe private state file')
        return fd
    except BaseException:
        os.close(fd)
        raise


def identity(path):
    meta = path.lstat()
    return (meta.st_dev, meta.st_ino, meta.st_uid, meta.st_mode, meta.st_nlink, meta.st_size)


def read_journal(path, uid, expected, preparation_id):
    fd = private_open(path, uid)
    try:
        before = identity(path)
        require(before[0:2] == (os.fstat(fd).st_dev, os.fstat(fd).st_ino), 'Journal path changed')
        require(before[-1] <= MAX_JOURNAL, 'Journal exceeds bound')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            data = stream.read(MAX_JOURNAL + 1)
        proof = journal_proof(data, expected, preparation_id)
        require(identity(path) == before, 'Journal changed while reading')
        return before, proof
    finally:
        os.close(fd)


def prepare(value, *, home=common.HOME, uid=1000, status=common.unit_status,
            move=common.rename_noreplace):
    """Path/status/move injection is test-only; the CLI has no such overrides."""
    value = request(value)
    home = Path(home)
    state = home / '.local/state/relay-holder'
    for part in ('', '.config', '.config/relay-holder', '.config/systemd', '.config/systemd/user',
                 '.local', '.local/state'):
        common.directory(home / part, uid)
    common.directory(state, uid, True)
    common.stopped(home, status)
    lock_fds = []
    try:
        # Never create, truncate or unlink singleton locks. Both real services
        # use flock on these exact inodes; hold them until the last receipt sync.
        locks = {}
        for name in ('holder.lock', 'connector-control.lock'):
            path = state / name
            fd = private_open(path, uid)
            lock_fds.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locks[path] = identity(path)
            require(locks[path][0:2] == (os.fstat(fd).st_dev, os.fstat(fd).st_ino), 'Lock path changed')
        expected_names = {'journal.jsonl', 'holder.lock', 'connector-control.lock',
                          'native-homes', 'tool-bindings', 'profile-homes', 'workspaces'}
        names = {p.name for p in state.iterdir()}
        require(names in (expected_names, expected_names | {'control.sock'}), 'Unsupported holder state entries')
        socket_identity = None
        if 'control.sock' in names:
            socket_identity = identity(state / 'control.sock')
            require(stat.S_ISSOCK(socket_identity[3]) and socket_identity[2] == uid
                    and stat.S_IMODE(socket_identity[3]) == 0o600, 'Unexpected holder socket')
        directories = {path: common.directory_identity(path, uid, True) for path in
                       (state, state / 'native-homes', state / 'tool-bindings',
                        state / 'profile-homes', state / 'workspaces', home / '.config/relay-holder')}
        for name in ('profile-homes', 'workspaces'):
            require({p.name for p in (state / name).iterdir()} == set(PROFILES), 'Unexpected profile directory set')
            for profile in PROFILES:
                path = state / name / profile
                directories[path] = common.directory_identity(path, uid, True)
        for path in {(home / rel).parent for rel, _ in FILES.values()}:
            directories[path] = common.directory_identity(path, uid)
        journal_identity, proof = read_journal(state / 'journal.jsonl', uid, value['journal'],
                                             value['operationId'])
        records = {key: common.inspect_file(home / rel, uid, mode, value['expectedFiles'][key])
                   for key, (rel, mode) in FILES.items()}
        backup_root = home / '.config/relay-holder/idle-upgrades'
        if os.path.lexists(backup_root):
            common.directory(backup_root, uid, True)
        backup = backup_root / value['operationId']
        require(not os.path.lexists(backup), 'Operation already exists; inspect retained receipts')
        device = state.stat().st_dev
        require(all(record['device'] == device for record in records.values())
                and (home / '.config/relay-holder').stat().st_dev == device, 'Same filesystem required')
        moved = set()

        def recheck():
            common.stopped(home, status, moved.intersection(UNITS))
            require({p.name for p in state.iterdir()} == names, 'Holder state entries changed')
            for path, original in directories.items():
                require(common.directory_identity(path, uid) == original, 'Directory identity changed')
            for path, original in locks.items():
                require(identity(path) == original, 'Singleton lock identity changed')
            for name in ('native-homes', 'tool-bindings'):
                require(not any((state / name).iterdir()), 'Native or communication state is not empty')
            if socket_identity is not None:
                require(identity(state / 'control.sock') == socket_identity, 'Holder socket changed')
            observed, _ = read_journal(state / 'journal.jsonl', uid, value['journal'],
                                       value['operationId'])
            require(observed == journal_identity, 'Journal inode changed')
            for key, (rel, mode) in FILES.items():
                path = backup / key if key in moved else home / rel
                require(common.inspect_file(path, uid, mode, value['expectedFiles'][key]) == records[key],
                        'Configuration file changed')
                if key in moved:
                    require(not os.path.lexists(home / rel), 'Moved destination reappeared')

        recheck()
        backup_root.mkdir(mode=0o700, exist_ok=True)
        common.directory(backup_root, uid, True)
        require(backup_root.stat().st_dev == device, 'Backup must share filesystem')
        backup.mkdir(mode=0o700)
        directories[backup_root] = common.directory_identity(backup_root, uid, True)
        directories[backup] = common.directory_identity(backup, uid, True)
        common.sync_dir(backup_root.parent)
        common.sync_dir(backup_root)
        common.receipt(backup / '00-prepared.json', dict(version=1, request=value,
                       journal=proof, journalIdentity=journal_identity, files=records))
        for index, (key, (rel, _mode)) in enumerate(FILES.items(), 1):
            recheck()
            source, destination = home / rel, backup / key
            move(source, destination)
            moved.add(key)
            common.sync_dir(source.parent)
            common.sync_dir(backup)
            recheck()
            common.receipt(backup / ('%02d-moved.json' % index),
                           dict(source=str(source), backup=str(destination), identity=records[key]))
        recheck()
        result = dict(version=1, operationId=value['operationId'], state='prepared-stopped-sealed',
                      backup=str(backup), journal=proof,
                      next='Reviewed role with start:false; activation and seal release remain separate')
        common.receipt(backup / 'complete.json', result)
        return result
    finally:
        for fd in reversed(lock_fds):
            os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    args = parser.parse_args()
    require(os.getuid() == 1000, 'Run only as c4 UID1000')
    fd = private_open(args.request, 1000)
    try:
        require(os.fstat(fd).st_size <= 16384, 'Request exceeds bound')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            value = parse_json(stream.read(16385))
    finally:
        os.close(fd)
    print(json.dumps(prepare(value)))


if __name__ == '__main__':
    main()
