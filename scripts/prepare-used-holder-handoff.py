#!/usr/bin/env python3
"""Prepare a stopped, sealed Pi holder for a separate empty state root."""
import argparse
import contextlib
import shlex
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

MAX_JOURNAL = 64 * 1024 * 1024
MAX_RECORD = 64 * 1024
MAX_REVISION = 9007199254740991
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
    """Validate uncheckpointed v1 history; unknown formats never qualify."""
    require(0 < len(data) <= MAX_JOURNAL and data.endswith(b'\n'), 'Invalid journal size or tail')
    require(hashlib.sha256(data).hexdigest() == expected['sha256'], 'Journal SHA mismatch')
    revision, paused, seal, current, first = 0, False, None, None, None
    operations, incarnations = set(), set()
    count = 0
    commands, sessions, opened = {}, set(), set()
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
        if kind not in ('holder-admission', 'holder-maintenance-seal'):
            require(seal is None, 'Worker event after maintenance seal')
            worker_event(event, commands, sessions, opened)
            continue
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
                require(not sessions and all(s != 'accepted' for s in commands.values()),
                        'Seal requires drained sessions and commands')
                seal = seal_id
            else:
                require(seal == seal_id, 'Invalid seal release')
                seal = None
        operations.add(operation)
        revision = event['revision']
    require(not sessions and all(s != 'accepted' for s in commands.values()), 'Undrained journal')
    require(first == expected['journalId'] and current == expected['incarnation'],
            'Journal identity mismatch')
    require(paused and seal == expected['sealId'], 'Holder must remain durably paused and sealed')
    require(preparation_id not in operations, 'Preparation operation already appears in the journal')
    return dict(journalId=first, incarnation=current, sealId=seal,
                admissionRevision=revision, records=count, sha256=expected['sha256'],
                unknownCommands=sum(s == 'unknown' for s in commands.values()))


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


def worker_event(event, commands, sessions, opened):
    # Exact event names in holder2604 runtime.rs. Payloads remain private and are
    # never copied into receipts. A maintenance seal is not an effects receipt.
    kind = event['kind']
    if kind in ('command-accepted', 'command-preflight-rejected'):
        allowed = {'kind', 'commandId', 'digest', 'sessionId', 'nativeRecovery'} if kind == 'command-accepted' else {
            'kind', 'commandId', 'digest', 'result'}
        require(set(event) <= allowed and {'commandId', 'digest'} <= set(event), 'Invalid command record')
        command = canonical_uuid(event['commandId'])
        sha(event['digest'])
        require(command not in commands, 'Duplicate command')
        if 'sessionId' in event:
            canonical_uuid(event['sessionId'])
        commands[command] = 'accepted' if kind == 'command-accepted' else 'rejected'
    elif kind == 'command-result':
        exact_keys(event, ('kind', 'commandId', 'status', 'result'))
        command = canonical_uuid(event['commandId'])
        require(commands.get(command) == 'accepted' and event['status'] in ('completed', 'rejected', 'unknown'),
                'Invalid command result')
        commands[command] = event['status']
    else:
        require(kind in ('session-opened', 'session-ended', 'update', 'session-settings',
                         'elicitation-ended', 'request-ended', 'elicitation-request', 'permission-request',
                         'continued-questions', 'elicitation-answered', 'decision-accepted', 'rebound',
                         'native-state-retained'), 'Unsupported journal event')
        exact_keys(event, ('kind', 'sessionId', 'value'))
        session = canonical_uuid(event['sessionId'])
        require(isinstance(event['value'], dict), 'Invalid session event')
        if kind == 'session-opened':
            require(session not in opened, 'Duplicate session open')
            opened.add(session)
            sessions.add(session)
        elif kind == 'session-ended':
            require(session in sessions, 'Session ended without open')
            sessions.remove(session)
        # Installation errors can emit updates before session-opened. The final
        # seal plus command drain, not the existence of an update, fences them.


def stamp(meta):
    return [meta.st_dev, meta.st_ino, meta.st_uid, meta.st_mode, meta.st_nlink,
            meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns]


def read_bytes(path, uid, mode, limit):
    require(path.resolve() == path, 'Noncanonical file path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid == uid
                and stat.S_IMODE(before.st_mode) == mode and before.st_size <= limit, 'Unsafe file')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            data = stream.read(limit + 1)
        require(len(data) <= limit and stamp(before) == stamp(os.fstat(fd)) == stamp(path.lstat()),
                'File changed during read')
        return data, stamp(before)
    finally:
        os.close(fd)


def tree_snapshot(path, uid):
    """Bounded no-follow inventory: symlink bytes are hashed, never traversed."""
    directory(path, uid, True)
    digest, count, total = hashlib.sha256(), 0, 0

    def visit(item, depth):
        nonlocal count, total
        count += 1
        require(count <= 20000 and depth <= 64, 'Tree inventory bound exceeded')
        before = item.lstat()
        require(before.st_uid == uid, 'Unexpected tree owner')
        row = [str(item.relative_to(path)), stamp(before)]
        if stat.S_ISDIR(before.st_mode):
            require(not before.st_mode & 0o022 and item.resolve() == item, 'Unsafe tree directory')
            names = sorted(item.iterdir())
            for child in names:
                visit(child, depth + 1)
            require(names == sorted(item.iterdir()), 'Tree entries changed')
        elif stat.S_ISREG(before.st_mode):
            require(not before.st_mode & 0o022, 'Writable tree file')
            data, _ = read_bytes(item, uid, stat.S_IMODE(before.st_mode), MAX_JOURNAL)
            total += len(data)
            require(total <= 256 * 1024 * 1024, 'Tree byte bound exceeded')
            row.append(hashlib.sha256(data).hexdigest())
        elif stat.S_ISLNK(before.st_mode):
            row.append(hashlib.sha256(os.fsencode(os.readlink(item))).hexdigest())
        else:
            raise ValueError('Unsupported special tree entry')
        require(stamp(item.lstat()) == stamp(before), 'Tree identity changed')
        digest.update(json.dumps(row, separators=(',', ':'), ensure_ascii=True).encode() + b'\n')

    visit(path, 0)
    return dict(sha256=digest.hexdigest(), entries=count, bytes=total,
                identity=list(directory_identity(path, uid, True)))


@contextlib.contextmanager
def locked(state, uid):
    fds, originals = [], {}
    try:
        for name in ('holder.lock', 'connector-control.lock'):
            path = state / name
            fd = private_open(path, uid)
            fds.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            originals[path] = stamp(os.fstat(fd))
            require(stamp(path.lstat()) == originals[path], 'Lock identity changed')
        yield originals
    finally:
        for fd in reversed(fds):
            os.close(fd)


def ancestry(home, uid):
    for part in ('', '.config', '.config/relay-holder', '.config/systemd', '.config/systemd/user',
                 '.local', '.local/state'):
        directory(home / part, uid)


def paths(home, operation):
    canonical_uuid(operation)
    return (home / '.local/state/relay-holder',
            home / ('.local/state/relay-holder-handoff-' + operation),
            home / '.config/relay-holder/used-handoffs' / operation)


def profiles(home, state, uid, expected):
    data, meta = read_bytes(home / FILES['profiles'][0], uid, 0o600, 1024 * 1024)
    require(hashlib.sha256(data).hexdigest() == expected, 'Profiles SHA changed')
    values = parse_json(data)
    require(isinstance(values, list) and len(values) == 3
            and {p['id'] for p in values} == set(PROFILES), 'Unexpected profiles')
    for p in values:
        require(p['native']['kind'] == 'pi' and p['executionUid'] == uid
                and p['qualification'] == 'protocol-fixture'
                and p['credentialHome'] == str(state / 'profile-homes' / p['id'])
                and p['roots'] == [str(state / 'workspaces' / p['id'])], 'Profile path mismatch')
        require('storage' not in p and 'candidate' not in p and 'persistent' not in p,
                'Unsupported profile topology')
        require(p['router']['credentialFile'] == str(home / '.local/state/relay/router-managed'
                / canonical_uuid(p['router']['bindingId']) / 'credential.json'), 'Renderer path mismatch')
        for path in (Path(p['credentialHome']), Path(p['roots'][0])):
            directory(path, uid, True)
    return meta


def config_alignment(home, state, data):
    config = parse_json(data['service'])
    exact_keys(config, ('version', 'binary', 'binarySha256', 'stateDirectory', 'profilesFile'))
    require(config['version'] == 1 and config['stateDirectory'] == str(state)
            and config['profilesFile'] == str(home / FILES['profiles'][0]), 'Old service topology mismatch')
    sha(config['binarySha256'])
    for name, flag in zip(UNITS, ('--state-dir', '--holder-dir')):
        lines = data[name].decode('utf8').splitlines()
        starts = [line[10:] for line in lines if line.startswith('ExecStart=')]
        require(len(starts) == 1 and lines.count('KillMode=control-group') == 1,
                'Unsupported old unit')
        args = shlex.split(starts[0])
        require(args.count(flag) == 1 and args[args.index(flag) + 1] == str(state), 'Old socket path mismatch')
        if name == UNITS[0]:
            require(args[0] == config['binary'] and args.count('--profiles') == 1
                    and args[args.index('--profiles') + 1] == config['profilesFile']
                    and '--qualification' in args and '--storage-policy' not in args, 'Old holder unit mismatch')


def preserved(state, uid, value):
    _, proof = read_journal(state / 'journal.jsonl', uid, value['journal'], value['operationId'])
    data, meta = read_bytes(state / 'journal.jsonl', uid, 0o600, MAX_JOURNAL)
    require(hashlib.sha256(data).hexdigest() == proof['sha256'], 'Journal changed')
    return dict(journal=proof, journalIdentity=meta,
                native=tree_snapshot(state / 'native-homes', uid),
                toolBindings=tree_snapshot(state / 'tool-bindings', uid))


def prepare(value, *, home=HOME, uid=1000, status=unit_status, move=rename_noreplace):
    """Injected roots/status/mover are test seams, not command-line overrides."""
    value = request(value)
    home = Path(home)
    ancestry(home, uid)
    state, next_state, backup = paths(home, value['operationId'])
    directory(state, uid, True)
    stopped(home, status)
    require(not os.path.lexists(next_state) and not os.path.lexists(backup), 'Handoff destination already exists')
    if os.path.lexists(backup.parent):
        directory(backup.parent, uid, True)
    with locked(state, uid) as locks:
        state_identity = directory_identity(state, uid, True)
        directories = {home / part: directory_identity(home / part, uid) for part in (
            '', '.config', '.config/relay-holder', '.config/systemd', '.config/systemd/user',
            '.local', '.local/state')}
        state_names = {p.name for p in state.iterdir()}
        expected = {'journal.jsonl', 'holder.lock', 'connector-control.lock',
                    'native-homes', 'tool-bindings', 'profile-homes', 'workspaces'}
        require(state_names in (expected, expected | {'control.sock'}), 'Unsupported state entries')
        socket_meta = None
        if 'control.sock' in state_names:
            socket_meta = stamp((state / 'control.sock').lstat())
            require(stat.S_ISSOCK(socket_meta[3]) and socket_meta[2] == uid
                    and stat.S_IMODE(socket_meta[3]) == 0o600, 'Unexpected old socket')
        profile_meta = profiles(home, state, uid, value['expectedFiles']['profiles'])
        old = preserved(state, uid, value)
        workspace = tree_snapshot(state / 'workspaces', uid)
        profile_homes = tree_snapshot(state / 'profile-homes', uid)
        originals, file_data = {}, {}
        for name, (rel, mode) in FILES.items():
            data, meta = read_bytes(home / rel, uid, mode, 1024 * 1024)
            require(hashlib.sha256(data).hexdigest() == value['expectedFiles'][name], 'Configuration SHA mismatch')
            originals[name] = dict(identity=meta, sha256=value['expectedFiles'][name])
            file_data[name] = data
        config_alignment(home, state, file_data)
        moved = set()
        next_identity = None

        def recheck():
            stopped(home, status, moved.intersection(UNITS))
            require(directory_identity(state, uid, True) == state_identity, 'State identity changed')
            require({p.name for p in state.iterdir()} == state_names, 'State entries changed')
            for path, original in directories.items():
                require(directory_identity(path, uid) == original, 'Directory identity changed')
            if socket_meta is not None:
                require(stamp((state / 'control.sock').lstat()) == socket_meta, 'Old socket changed')
            if next_identity is not None:
                require(list(directory_identity(next_state, uid, True)) == next_identity
                        and not any(next_state.iterdir()), 'New state changed')
            for path, meta in locks.items():
                require(stamp(path.lstat()) == meta, 'Lock identity changed')
            require(profiles(home, state, uid, value['expectedFiles']['profiles']) == profile_meta, 'Profiles changed')
            require(preserved(state, uid, value) == old, 'Retained state changed')
            require(tree_snapshot(state / 'workspaces', uid) == workspace
                    and tree_snapshot(state / 'profile-homes', uid) == profile_homes, 'Profile data changed')
            for name, (rel, mode) in FILES.items():
                path = backup / name if name in moved else home / rel
                data, meta = read_bytes(path, uid, mode, 1024 * 1024)
                # rename changes ctime, while identity, contents and mtime stay.
                require(meta[:-1] == originals[name]['identity'][:-1]
                        and hashlib.sha256(data).hexdigest() == originals[name]['sha256'], 'Configuration changed')
                if name in moved:
                    require(not os.path.lexists(home / rel), 'Configuration reappeared')

        recheck()
        backup.parent.mkdir(mode=0o700, exist_ok=True)
        directory(backup.parent, uid, True)
        backup.mkdir(mode=0o700)
        directories[backup.parent] = directory_identity(backup.parent, uid, True)
        directories[backup] = directory_identity(backup, uid, True)
        sync_dir(backup.parent.parent)
        sync_dir(backup.parent)
        receipt(backup / '00-prepared.json', dict(version=1, request=value, preserved=old,
                                                workspace=workspace, profileHomes=profile_homes, files=originals))
        # No journal, socket, lock, native home or authority is copied.
        next_state.mkdir(mode=0o700)
        sync_dir(next_state.parent)
        next_identity = list(directory_identity(next_state, uid, True))
        receipt(backup / '01-empty-state.json', dict(path=str(next_state), identity=next_identity))
        for index, name in enumerate(('service', *UNITS), 2):
            recheck()
            require(not any(next_state.iterdir()) and list(directory_identity(next_state, uid, True)) == next_identity,
                    'New state changed')
            source = home / FILES[name][0]
            move(source, backup / name)
            moved.add(name)
            sync_dir(source.parent)
            sync_dir(backup)
            recheck()
            receipt(backup / ('%02d-moved.json' % index), dict(name=name, sha256=originals[name]['sha256']))
        recheck()
        result = dict(version=1, operationId=value['operationId'], state='prepared-stopped-new-state',
                      request=value, oldState=str(state), newState=str(next_state), newStateIdentity=next_identity,
                      profilesFile=str(home / FILES['profiles'][0]), preserved=old,
                      workspace=workspace, profileHomes=profile_homes, files=originals)
        receipt(backup / 'complete.json', result)
        return dict(state=result['state'], receipt=str(backup / 'complete.json'),
                    receiptSha256=hashlib.sha256((backup / 'complete.json').read_bytes()).hexdigest(),
                    newState=str(next_state), profilesSha256=value['expectedFiles']['profiles'])


def verify(receipt_path, receipt_sha256, new_state, profile_state, profiles_sha256,
           *, home=HOME, uid=1000, status=unit_status):
    """Read-only role gate. Intended only for stopped staging, never activation."""
    home, receipt_path = Path(home), Path(receipt_path)
    ancestry(home, uid)
    sha(receipt_sha256)
    data, _ = read_bytes(receipt_path, uid, 0o600, 128 * 1024)
    require(hashlib.sha256(data).hexdigest() == receipt_sha256, 'Receipt SHA mismatch')
    saved = parse_json(data)
    require(saved['version'] == 1 and saved['state'] == 'prepared-stopped-new-state', 'Incomplete handoff')
    value = request(saved['request'])
    state, next_state, backup = paths(home, value['operationId'])
    require(saved['operationId'] == value['operationId'] and receipt_path == backup / 'complete.json'
            and saved['newState'] == str(next_state) == new_state
            and saved['oldState'] == str(state) == profile_state
            and profiles_sha256 == value['expectedFiles']['profiles']
            and saved['profilesFile'] == str(home / FILES['profiles'][0]), 'Handoff scope mismatch')
    directory(backup.parent, uid, True)
    directory(backup, uid, True)
    # Staging may already have recreated units. They must still be stopped;
    # the regular role later authenticates their exact desired bytes.
    absent = [name for name in UNITS if not os.path.lexists(home / FILES[name][0])]
    stopped(home, status, absent)
    with locked(state, uid):
        profiles(home, state, uid, profiles_sha256)
        require(preserved(state, uid, value) == saved['preserved'], 'Retained state changed')
        require(tree_snapshot(state / 'workspaces', uid) == saved['workspace']
                and tree_snapshot(state / 'profile-homes', uid) == saved['profileHomes'], 'Staging profile data changed')
        require(list(directory_identity(next_state, uid, True)) == saved['newStateIdentity']
                and not any(next_state.iterdir()), 'Fresh state no longer empty')
        for name in ('service', *UNITS):
            data, meta = read_bytes(backup / name, uid, FILES[name][1], 1024 * 1024)
            require(hashlib.sha256(data).hexdigest() == value['expectedFiles'][name]
                    and meta[:-1] == saved['files'][name]['identity'][:-1], 'Backup changed')
    return dict(verified=True, state='stopped-staging-only', newState=str(next_state), profilesSha256=profiles_sha256)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--request', type=Path)
    mode.add_argument('--verify', type=Path)
    parser.add_argument('--receipt-sha256')
    parser.add_argument('--state')
    parser.add_argument('--profile-state')
    parser.add_argument('--profiles-sha256')
    args = parser.parse_args()
    require(os.getuid() == 1000, 'Run only as c4 UID1000')
    if args.request:
        require(not any((args.receipt_sha256, args.state, args.profile_state, args.profiles_sha256)),
                'Unexpected preparation arguments')
        data, _ = read_bytes(args.request, 1000, 0o600, 16384)
        result = prepare(parse_json(data))
    else:
        require(all((args.receipt_sha256, args.state, args.profile_state, args.profiles_sha256)),
                'Explicit receipt and path pins required')
        result = verify(args.verify, args.receipt_sha256, args.state, args.profile_state, args.profiles_sha256)
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Journal, native history and credentials must never reach a traceback.
        print(json.dumps({'state': 'refused', 'complete': False,
                          'next': 'Keep services stopped; inspect private inputs and retained receipts'}))
        raise SystemExit(1)
