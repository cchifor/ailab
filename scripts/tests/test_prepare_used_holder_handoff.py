#!/usr/bin/env python3
"""Real files, locks and no-clobber moves; only systemd status is a fixture."""
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

SPEC = importlib.util.spec_from_file_location('handoff', Path(__file__).parents[1] / 'prepare-used-holder-handoff.py')
h = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(h)


class UsedHandoff(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='used-handoff-')
        self.addCleanup(temporary.cleanup)
        self.home = getattr(self, 'fixture_home', Path(temporary.name))
        self.uid = os.getuid()
        self.operation, self.incarnation, self.seal, self.session, self.command = [str(uuid.uuid4()) for _ in range(5)]
        self.state, self.next, self.backup = h.paths(self.home, self.operation)
        for rel in ('.config/relay-holder', '.config/systemd/user', '.local/state/relay-holder'):
            (self.home / rel).mkdir(parents=True, mode=0o700)
        for name in ('native-homes', 'tool-bindings', 'profile-homes', 'workspaces'):
            (self.state / name).mkdir(mode=0o700)
        for name in ('holder.lock', 'connector-control.lock'):
            (self.state / name).touch(mode=0o600)
        self.native = self.state / 'native-homes' / self.session
        self.native.mkdir(mode=0o700)
        self.put(self.native / 'conversation.jsonl', b'retained private history')
        self.put(self.native / 'relay-retained-state.json', b'{"blocked":true}')
        values = []
        for profile in h.PROFILES:
            for name in ('profile-homes', 'workspaces'):
                (self.state / name / profile).mkdir(mode=0o700)
            values.append(dict(id=profile, executionUid=self.uid, qualification='protocol-fixture',
                               native=dict(kind='pi'), credentialHome=str(self.state / 'profile-homes' / profile),
                               roots=[str(self.state / 'workspaces' / profile)],
                               router=dict(bindingId=profile, credentialFile=str(self.home / '.local/state/relay/router-managed'
                                                                                 / profile / 'credential.json'))))
        self.marker = self.state / 'workspaces' / h.PROFILES[0] / 'existing-marker'
        self.put(self.marker, b'never-reset-marker')
        self.put(self.home / h.FILES['profiles'][0], json.dumps(values).encode())
        service = dict(version=1, binary='/opt/relay-holder/pinned/relay-holder', binarySha256='a' * 64,
                       stateDirectory=str(self.state), profilesFile=str(self.home / h.FILES['profiles'][0]))
        self.put(self.home / h.FILES['service'][0], json.dumps(service).encode())
        for name, flags in zip(h.UNITS, (
            f'--qualification --state-dir "{self.state}" --profiles "{service["profilesFile"]}"',
            f'--state /private/connector.json control-plane --holder-dir "{self.state}"')):
            self.put(self.home / h.FILES[name][0],
                     f'[Service]\nExecStart={service["binary"]} {flags}\nKillMode=control-group\n'.encode(), 0o644)
        self.events = [dict(kind='holder-started', version=1),
                       dict(kind='command-accepted', commandId=self.command, digest='d' * 64, sessionId=self.session),
                       dict(kind='session-opened', sessionId=self.session, value=dict(generation=1)),
                       dict(kind='command-result', commandId=self.command, status='completed', result=dict(generation=1)),
                       dict(kind='native-state-retained', sessionId=self.session, value=dict(blocked=True)),
                       dict(kind='session-ended', sessionId=self.session, value=dict(reason='closed')),
                       dict(kind='holder-admission', operationId=str(uuid.uuid4()), revision=1, paused=True),
                       dict(kind='holder-maintenance-seal', operationId=self.seal, revision=2,
                            expectedIncarnation=self.incarnation, sealId=self.seal, sealed=True)]
        self.value = dict(version=1, operationId=self.operation,
                          expectedFiles={name: hashlib.sha256((self.home / rel).read_bytes()).hexdigest()
                                         for name, (rel, _) in h.FILES.items()},
                          journal=dict(journalId=self.incarnation, incarnation=self.incarnation, sealId=self.seal))
        self.write_journal()
        self.original = {path: (path.read_bytes(), h.stamp(path.stat())) for path in (
            self.state / 'journal.jsonl', self.native / 'conversation.jsonl', self.marker,
            self.home / h.FILES['profiles'][0])}

    def put(self, path, data, mode=0o600):
        path.write_bytes(data)
        path.chmod(mode)

    def write_journal(self):
        data = b''.join(json.dumps(dict(version=1, sequence=i, incarnation=self.incarnation, event=event)).encode() + b'\n'
                        for i, event in enumerate(self.events, 1))
        self.put(self.state / 'journal.jsonl', data)
        self.value['journal']['sha256'] = hashlib.sha256(data).hexdigest()

    def status(self, name):
        return dict(Id=name, ActiveState='inactive', SubState='dead', LoadState='loaded', MainPID='0',
                    ControlGroup='', UnitFileState='disabled', FragmentPath=str(self.home / h.FILES[name][0]), DropInPaths='')

    def prepare(self, **kwargs):
        return h.prepare(self.value, home=self.home, uid=self.uid, status=kwargs.pop('status', self.status), **kwargs)

    def verify(self, result, **kwargs):
        return h.verify(result['receipt'], result['receiptSha256'], str(self.next), str(self.state),
                        self.value['expectedFiles']['profiles'], home=self.home, uid=self.uid,
                        status=kwargs.pop('status', self.status), **kwargs)

    def unchanged(self):
        for path, (data, stamp) in self.original.items():
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(h.stamp(path.stat()), stamp)

    def test_used_sealed_state_preserved_new_root_empty_and_receipt_verifies(self):
        native_before = h.tree_snapshot(self.state / 'native-homes', self.uid)
        result = self.prepare()
        self.unchanged()
        self.assertEqual(list(self.next.iterdir()), [])
        self.assertEqual(h.tree_snapshot(self.state / 'native-homes', self.uid), native_before)
        self.assertTrue(self.verify(result)['verified'])
        saved = json.loads(Path(result['receipt']).read_text())
        self.assertNotIn('retained private history', json.dumps(saved))
        self.assertNotIn('never-reset-marker', json.dumps(saved))
        for name in ('service', *h.UNITS):
            self.assertFalse((self.home / h.FILES[name][0]).exists())
            self.assertEqual(hashlib.sha256((self.backup / name).read_bytes()).hexdigest(), self.value['expectedFiles'][name])
        with self.assertRaises(ValueError):
            self.prepare()

    def test_stopped_status_requires_disabled_empty_cgroup_and_no_overrides(self):
        for field, value in (('ActiveState', 'active'), ('SubState', 'failed'), ('MainPID', '123'),
                             ('ControlGroup', '/still-alive'), ('UnitFileState', 'enabled'),
                             ('DropInPaths', '/other.conf'), ('FragmentPath', '/other.service')):
            with self.subTest(field=field):
                def changed(name):
                    return {**self.status(name), field: value}
                with self.assertRaises(ValueError):
                    self.prepare(status=changed)
                self.assertFalse(self.next.exists())
                self.unchanged()

    def test_existing_destination_or_receipt_directory_refused(self):
        for path in (self.next, self.backup):
            with self.subTest(path=path):
                path.mkdir(parents=True, mode=0o700)
                try:
                    with self.assertRaises(ValueError):
                        self.prepare()
                    self.unchanged()
                finally:
                    path.rmdir()

    def test_held_singleton_lock_refused(self):
        for name in ('holder.lock', 'connector-control.lock'):
            with self.subTest(name=name), (self.state / name).open('r+b') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    self.prepare()
                self.assertFalse(self.next.exists())

    def test_journal_missing_pause_seal_or_drain_refused(self):
        original = copy.deepcopy(self.events)
        variants = [original[:-1], original[:6] + original[7:],
                    [e for e in original if e['kind'] != 'session-ended'],
                    [e for e in original if e['kind'] != 'command-result'],
                    original + [dict(kind='future-event')]]
        for events in variants:
            with self.subTest(events=events):
                self.events = events
                self.write_journal()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.next.exists())

    def test_journal_identity_sequence_compaction_duplicate_fields_and_tail_refused(self):
        path = self.state / 'journal.jsonl'
        original = path.read_bytes()
        for data in (original[:-1], original.replace(b'"sequence": 2', b'"sequence": 9'),
                     original.replace(b'"version": 1', b'"version": 2', 1),
                     original.replace(b'"version": 1', b'"version": 1,"version": 1', 1)):
            self.put(path, data)
            self.value['journal']['sha256'] = hashlib.sha256(data).hexdigest()
            with self.assertRaises(ValueError):
                self.prepare()
        self.put(path, original)
        self.value['journal']['sha256'] = hashlib.sha256(original).hexdigest()
        for field in ('incarnation', 'journalId', 'sealId'):
            saved = self.value['journal'][field]
            self.value['journal'][field] = str(uuid.uuid4())
            with self.assertRaises(ValueError):
                self.prepare()
            self.value['journal'][field] = saved

    def test_terminal_unknown_remains_unknown_and_never_grants_recovery(self):
        self.events[3]['status'] = 'unknown'
        self.write_journal()
        result = self.prepare()
        saved = json.loads(Path(result['receipt']).read_text())
        self.assertEqual(saved['preserved']['journal']['unknownCommands'], 1)
        self.assertEqual(list(self.next.iterdir()), [])

    def test_symlink_or_hardlink_journal_and_lock_refused(self):
        for name in ('journal.jsonl', 'holder.lock'):
            for link in ('symbolic', 'hard'):
                path = self.state / name
                saved = self.home / 'original-file'
                with self.subTest(name=name, link=link):
                    if link == 'symbolic':
                        path.rename(saved)
                        path.symlink_to(saved)
                    else:
                        os.link(path, saved)
                    try:
                        with self.assertRaises((ValueError, OSError)):
                            self.prepare()
                        self.assertFalse(self.next.exists())
                    finally:
                        if link == 'symbolic':
                            path.unlink()
                            saved.rename(path)
                        else:
                            saved.unlink()

    def test_native_symlink_is_preserved_never_followed(self):
        target = self.home / 'outside-secret'
        self.put(target, b'must-not-be-read')
        link = self.native / 'external-link'
        link.symlink_to(target)
        first = h.tree_snapshot(self.state / 'native-homes', self.uid)
        self.put(target, b'outside-change-irrelevant')
        self.assertEqual(h.tree_snapshot(self.state / 'native-homes', self.uid), first)
        self.prepare()
        self.assertTrue(link.is_symlink())

    def test_partial_move_failure_preserves_all_locations_and_refuses_retry(self):
        count = 0
        def fail(source, destination):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('injected interruption')
            h.rename_noreplace(source, destination)
        with self.assertRaisesRegex(OSError, 'injected'):
            self.prepare(move=fail)
        self.unchanged()
        self.assertFalse((self.backup / 'complete.json').exists())
        self.assertTrue((self.backup / 'service').is_file())
        self.assertTrue((self.home / h.FILES[h.UNITS[0]][0]).is_file())
        with self.assertRaises(ValueError):
            self.prepare()

    def test_move_collision_never_overwrites(self):
        def collision(source, destination):
            self.put(destination, b'collision')
            h.rename_noreplace(source, destination)
        with self.assertRaises(FileExistsError):
            self.prepare(move=collision)
        self.assertEqual((self.backup / 'service').read_bytes(), b'collision')
        self.assertTrue((self.home / h.FILES['service'][0]).exists())

    def test_mutation_during_preparation_never_gets_complete_receipt(self):
        def change(source, destination):
            h.rename_noreplace(source, destination)
            self.put(self.marker, b'changed-marker')
        with self.assertRaisesRegex(ValueError, 'Profile data changed'):
            self.prepare(move=change)
        self.assertFalse((self.backup / 'complete.json').exists())

    def test_receipt_scope_tamper_and_used_new_state_refused(self):
        result = self.prepare()
        bad = {**result, 'receiptSha256': 'f' * 64}
        with self.assertRaisesRegex(ValueError, 'Receipt SHA'):
            self.verify(bad)
        with self.assertRaisesRegex(ValueError, 'scope mismatch'):
            h.verify(result['receipt'], result['receiptSha256'], str(self.state), str(self.state),
                     self.value['expectedFiles']['profiles'], home=self.home, uid=self.uid, status=self.status)
        self.put(self.next / 'journal.jsonl', b'new-incarnation')
        with self.assertRaisesRegex(ValueError, 'no longer empty'):
            self.verify(result)

    def test_preserved_state_and_marker_drift_after_preparation_refused(self):
        result = self.prepare()
        path = self.native / 'conversation.jsonl'
        self.put(path, b'drift')
        with self.assertRaisesRegex(ValueError, 'Retained state changed'):
            self.verify(result)

    def test_old_connector_socket_mismatch_refused_before_writes(self):
        path = self.home / h.FILES[h.UNITS[1]][0]
        self.put(path, path.read_bytes().replace(str(self.state).encode(), b'/other-state'), 0o644)
        self.value['expectedFiles'][h.UNITS[1]] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'socket path mismatch'):
            self.prepare()
        self.assertFalse(self.next.exists())

    def test_directory_or_lock_replacement_during_move_refused(self):
        for name in ('native-homes', 'holder.lock'):
            with self.subTest(name=name):
                case = UsedHandoff('runTest')
                case.setUp()
                self.addCleanup(case.doCleanups)
                original, preserved = case.state / name, case.home / ('saved-' + name)
                def replace(source, destination):
                    h.rename_noreplace(source, destination)
                    original.rename(preserved)
                    if name == 'native-homes':
                        original.mkdir(mode=0o700)
                    else:
                        original.touch(mode=0o600)
                with self.assertRaisesRegex(ValueError, 'changed'):
                    case.prepare(move=replace)
                self.assertTrue(preserved.exists())
                self.assertFalse((case.backup / 'complete.json').exists())

    def test_last_move_cannot_hide_new_state_mutation(self):
        def change(source, destination):
            h.rename_noreplace(source, destination)
            if source.name == h.UNITS[1]:
                self.put(self.next / 'journal.jsonl', b'unexpected-writer')
        with self.assertRaisesRegex(ValueError, 'New state changed'):
            self.prepare(move=change)
        self.assertFalse((self.backup / 'complete.json').exists())
        self.unchanged()

    def test_profile_home_and_backup_drift_refuse_verification(self):
        result = self.prepare()
        self.put(self.state / 'profile-homes' / h.PROFILES[0] / 'unexpected', b'changed')
        with self.assertRaisesRegex(ValueError, 'Staging profile data changed'):
            self.verify(result)
        (self.state / 'profile-homes' / h.PROFILES[0] / 'unexpected').unlink()
        # A different fixture isolates backup drift from changed directory times.
        other = UsedHandoff('runTest')
        other.setUp()
        self.addCleanup(other.doCleanups)
        result = other.prepare()
        other.put(other.backup / 'service', b'changed')
        with self.assertRaisesRegex(ValueError, 'Backup changed'):
            other.verify(result)


if __name__ == '__main__':
    unittest.main()
