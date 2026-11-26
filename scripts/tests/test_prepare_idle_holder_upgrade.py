import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
import uuid

SOURCE = Path(__file__).resolve().parents[1] / 'prepare-idle-holder-upgrade.py'
SPEC = importlib.util.spec_from_file_location('idle_upgrade', SOURCE)
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


@unittest.skipUnless(os.name == 'posix', 'Linux filesystem and flock contract')
class IdleUpgrade(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.state = self.home / '.local/state/relay-holder'
        for name in ('native-homes', 'tool-bindings', 'profile-homes', 'workspaces'):
            (self.state / name).mkdir(parents=True, mode=0o700)
        for name in ('profile-homes', 'workspaces'):
            for profile in upgrade.PROFILES:
                (self.state / name / profile).mkdir(mode=0o700)
        for rel, _mode in upgrade.FILES.values():
            (self.home / rel).parent.mkdir(parents=True, exist_ok=True)
        for path in (self.home, *self.home.rglob('*')):
            path.chmod(0o700)
        self.original, self.hashes = {}, {}
        for key, (rel, mode) in upgrade.FILES.items():
            data = ('reviewed old ' + key + '\n').encode()
            (self.home / rel).write_bytes(data)
            (self.home / rel).chmod(mode)
            self.original[key] = data
            self.hashes[key] = hashlib.sha256(data).hexdigest()
        for name in ('holder.lock', 'connector-control.lock'):
            (self.state / name).touch(mode=0o600)
        self.incarnation, self.seal = str(uuid.uuid4()), str(uuid.uuid4())
        self.events = [dict(kind='holder-started', version=1),
                       dict(kind='holder-admission', operationId=str(uuid.uuid4()), revision=1, paused=True),
                       dict(kind='holder-maintenance-seal', operationId=self.seal, revision=2,
                            expectedIncarnation=self.incarnation, sealId=self.seal, sealed=True)]
        self.value = dict(version=1, operationId=str(uuid.uuid4()), expectedFiles=self.hashes,
                          journal=dict(sha256='', journalId=self.incarnation,
                                       incarnation=self.incarnation, sealId=self.seal))
        self.write_journal()
        # Existing unrelated user files must survive, including credentials.
        self.sentinels = [self.state / 'workspaces' / upgrade.PROFILES[0] / 'retained-user-file',
                          self.state / 'profile-homes' / upgrade.PROFILES[0] / 'retained-file',
                          self.home / '.config/connector-credential']
        for path in self.sentinels:
            path.write_bytes(b'preserve-only-do-not-log')
            path.chmod(0o600)

    def write_journal(self, records=None):
        if records is None:
            records = [dict(version=1, sequence=i, incarnation=self.incarnation, event=event)
                       for i, event in enumerate(self.events, 1)]
        data = b''.join((json.dumps(row, separators=(',', ':')) + '\n').encode() for row in records)
        path = self.state / 'journal.jsonl'
        path.write_bytes(data)
        path.chmod(0o600)
        self.value['journal']['sha256'] = hashlib.sha256(data).hexdigest()
        return data

    def status(self, name):
        row = dict(Id=name, LoadState='loaded', ActiveState='inactive', SubState='dead', MainPID='0',
                   ControlGroup='', UnitFileState='disabled', DropInPaths='',
                   FragmentPath=str(self.home / '.config/systemd/user' / name))
        if not (self.home / '.config/systemd/user' / name).exists():
            row.update(LoadState='not-found', FragmentPath='', UnitFileState='', SubState='not-found')
        return row

    def prepare(self, **kwargs):
        return upgrade.prepare(self.value, home=self.home, uid=os.getuid(),
                               status=kwargs.pop('status', self.status), **kwargs)

    def assert_originals(self):
        for key, (rel, _) in upgrade.FILES.items():
            self.assertEqual((self.home / rel).read_bytes(), self.original[key])
        self.assertFalse((self.home / '.config/relay-holder/idle-upgrades').exists())

    def test_four_files_moved_while_journal_locks_native_state_and_credentials_preserved(self):
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(self.state / 'control.sock'))
        (self.state / 'control.sock').chmod(0o600)
        protected = [self.state / name for name in
                     ('journal.jsonl', 'holder.lock', 'connector-control.lock', 'native-homes',
                      'tool-bindings', 'control.sock', 'profile-homes', 'workspaces')] + self.sentinels
        identities = {p: upgrade.identity(p) for p in protected}
        journal = (self.state / 'journal.jsonl').read_bytes()
        result = self.prepare()
        self.assertEqual(result['state'], 'prepared-stopped-sealed')
        self.assertEqual(result['journal']['admissionRevision'], 2)
        backup = Path(result['backup'])
        for key, (rel, _) in upgrade.FILES.items():
            self.assertFalse((self.home / rel).exists())
            self.assertEqual((backup / key).read_bytes(), self.original[key])
        self.assertEqual(len(list(backup.glob('*moved.json'))), 4)
        self.assertTrue((backup / 'complete.json').is_file())
        self.assertEqual((self.state / 'journal.jsonl').read_bytes(), journal)
        for path, identity in identities.items():
            self.assertEqual(upgrade.identity(path), identity)
        for path in self.sentinels:
            self.assertEqual(path.read_bytes(), b'preserve-only-do-not-log')
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.prepare()

    def test_exact_journal_emitted_by_published_v0130_holder(self):
        # Captured unchanged from verified e843/v0.13.0 holder f70a471bb00b3f61...
        # on 2026-11-26: actual IPC pause+seal, SIGTERM exit0, no profiles/workers.
        # Source artifact receipt149439 and inner manifest ec9ee0fc8a525e75...
        # were verified before execution. This regression needs no installed binary.
        data = (
            b'{"version":1,"sequence":1,"incarnation":"b22d51c3-6caa-47f2-849e-855168cd0be8","event":{"kind":"holder-started","version":1}}\n'
            b'{"version":1,"sequence":2,"incarnation":"b22d51c3-6caa-47f2-849e-855168cd0be8","event":{"kind":"holder-admission","operationId":"96030616-5748-400e-99d8-2732c7a0c270","revision":1,"paused":true}}\n'
            b'{"version":1,"sequence":3,"incarnation":"b22d51c3-6caa-47f2-849e-855168cd0be8","event":{"kind":"holder-maintenance-seal","operationId":"e27bc2fc-e9a3-4fb6-bff0-04cf7a427eef","revision":2,"expectedIncarnation":"b22d51c3-6caa-47f2-849e-855168cd0be8","sealId":"e27bc2fc-e9a3-4fb6-bff0-04cf7a427eef","sealed":true}}\n'
        )
        expected = '033e05cf92d8a8aee7e7e9ad8b24065c3c6a3b9a143453829442530a5eae0556'
        self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
        path = self.state / 'journal.jsonl'
        path.write_bytes(data)
        self.value['journal'] = dict(sha256=expected,
                                     journalId='b22d51c3-6caa-47f2-849e-855168cd0be8',
                                     incarnation='b22d51c3-6caa-47f2-849e-855168cd0be8',
                                     sealId='e27bc2fc-e9a3-4fb6-bff0-04cf7a427eef')
        before = upgrade.identity(path)
        result = self.prepare()
        self.assertEqual(result['journal']['admissionRevision'], 2)
        self.assertEqual(result['journal']['records'], 3)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(upgrade.identity(path), before)

    def test_active_enabled_pid_cgroup_dropin_and_wrong_fragment_refused(self):
        for field, value in [('ActiveState', 'active'), ('UnitFileState', 'enabled'),
                             ('MainPID', '42'), ('ControlGroup', '/occupied'),
                             ('DropInPaths', '/override'), ('FragmentPath', '/wrong')]:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.prepare(status=lambda name: {**self.status(name), field: value})
                self.assert_originals()

    def test_both_existing_locks_are_respected(self):
        for name in ('holder.lock', 'connector-control.lock'):
            with self.subTest(name=name), open(self.state / name, 'r+b') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    self.prepare()
                self.assert_originals()

    def test_missing_lock_is_not_created(self):
        (self.state / 'connector-control.lock').unlink()
        with self.assertRaises(FileNotFoundError):
            self.prepare()
        self.assertFalse((self.state / 'connector-control.lock').exists())
        self.assert_originals()

    def test_any_command_session_refusal_or_unknown_event_disqualifies(self):
        for kind in ('command-accepted', 'command-result', 'command-preflight-rejected',
                     'session-opened', 'session-ended', 'native-state-retained', 'update', 'unknown'):
            with self.subTest(kind=kind):
                self.events.append(dict(kind=kind))
                self.write_journal()
                with self.assertRaisesRegex(ValueError, 'command/session'):
                    self.prepare()
                self.assert_originals()
                self.events.pop()

    def test_nonempty_native_or_bindings_and_unknown_state_refused(self):
        for name in ('native-homes/tail', 'tool-bindings/tail', 'unknown-state'):
            with self.subTest(name=name):
                path = self.state / name
                path.write_bytes(b'preserved')
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assert_originals()
                self.assertEqual(path.read_bytes(), b'preserved')
                path.unlink()

    def test_wrong_file_or_journal_hash_refused(self):
        for values, key in [(self.hashes, 'profiles'), (self.value['journal'], 'sha256')]:
            with self.subTest(key=key):
                old = values[key]
                values[key] = '0' * 64
                with self.assertRaisesRegex(ValueError, 'SHA mismatch'):
                    self.prepare()
                self.assert_originals()
                values[key] = old

    def test_invalid_or_unsealed_journal_refused(self):
        valid = copy.deepcopy(self.events)
        variants = [valid[:1], valid[:2],
                    [valid[0], {**valid[1], 'paused': False}, valid[2]],
                    [valid[0], valid[1], {**valid[2], 'revision': 4}],
                    [valid[0], valid[1], {**valid[2], 'expectedIncarnation': str(uuid.uuid4())}],
                    [valid[0], valid[1], {**valid[2], 'sealId': str(uuid.uuid4())}],
                    [valid[0], valid[1], {**valid[2], 'sealed': 'true'}],
                    [valid[0], valid[1], {**valid[2], 'extra': True}]]
        for events in variants:
            with self.subTest(events=events):
                self.events = events
                self.write_journal()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assert_originals()

    def test_compaction_gap_truncated_duplicate_and_oversized_records_refused(self):
        valid = self.write_journal()
        invalid = [b'{"version":2,"retainedAfter":1}\n', valid[:-1],
                   valid.replace(b'"sequence":2', b'"sequence":4'),
                   valid.replace(b'"version":1', b'"version":1,"version":1', 1),
                   b' ' * upgrade.MAX_RECORD + valid]
        for data in invalid:
            with self.subTest(length=len(data)):
                (self.state / 'journal.jsonl').write_bytes(data)
                self.value['journal']['sha256'] = hashlib.sha256(data).hexdigest()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assert_originals()

    def test_seal_survives_a_prior_idle_restart_without_losing_journal_identity(self):
        old = self.incarnation
        new = str(uuid.uuid4())
        records = [dict(version=1, sequence=i, incarnation=old, event=event)
                   for i, event in enumerate(self.events, 1)]
        records.append(dict(version=1, sequence=4, incarnation=new,
                            event=dict(kind='holder-started', version=1)))
        self.write_journal(records)
        self.value['journal']['incarnation'] = new
        result = self.prepare()
        self.assertEqual(result['journal']['journalId'], old)
        self.assertEqual(result['journal']['incarnation'], new)
        self.assertEqual(result['journal']['sealId'], self.seal)

    def test_wrong_expected_journal_incarnation_or_seal_identity_refused(self):
        for key in ('journalId', 'incarnation', 'sealId'):
            with self.subTest(key=key):
                old = self.value['journal'][key]
                self.value['journal'][key] = str(uuid.uuid4())
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assert_originals()
                self.value['journal'][key] = old

    def test_duplicate_operation_and_unsealed_or_unpaused_tail_refused(self):
        valid = copy.deepcopy(self.events)
        release = dict(kind='holder-maintenance-seal', operationId=str(uuid.uuid4()), revision=3,
                       expectedIncarnation=self.incarnation, sealId=self.seal, sealed=False)
        for tail in ([release], [release, dict(kind='holder-admission', operationId=str(uuid.uuid4()),
                                              revision=4, paused=False)],
                     [{**release, 'operationId': valid[1]['operationId']}]):
            with self.subTest(tail=tail):
                self.events = valid + tail
                self.write_journal()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assert_originals()

    def test_symlink_and_hardlinked_journal_or_configuration_refused(self):
        for name in ('journal', 'profiles'):
            path = self.state / 'journal.jsonl' if name == 'journal' else self.home / upgrade.FILES['profiles'][0]
            for kind in ('symlink', 'hardlink'):
                with self.subTest(name=name, kind=kind):
                    saved = path.with_name(path.name + '.saved')
                    if kind == 'symlink':
                        path.rename(saved)
                        path.symlink_to(saved)
                    else:
                        os.link(path, saved)
                    try:
                        with self.assertRaises((ValueError, OSError)):
                            self.prepare()
                        self.assertFalse((self.home / '.config/relay-holder/idle-upgrades').exists())
                    finally:
                        if kind == 'symlink':
                            path.unlink()
                            saved.rename(path)
                        else:
                            saved.unlink()

    def test_partial_move_failure_retains_all_bytes_and_refuses_automatic_retry(self):
        count = 0
        def fail(source, destination):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError('injected move failure')
            upgrade.common.rename_noreplace(source, destination)
        with self.assertRaisesRegex(OSError, 'injected'):
            self.prepare(move=fail)
        backup = self.home / '.config/relay-holder/idle-upgrades' / self.value['operationId']
        self.assertTrue((backup / '00-prepared.json').exists())
        self.assertFalse((backup / 'complete.json').exists())
        for key, (rel, _) in upgrade.FILES.items():
            locations = [p for p in (self.home / rel, backup / key) if p.exists()]
            self.assertEqual(len(locations), 1)
            self.assertEqual(locations[0].read_bytes(), self.original[key])
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.prepare()

    def test_journal_change_after_first_move_fails_closed_without_complete_receipt(self):
        def mutate(source, destination):
            upgrade.common.rename_noreplace(source, destination)
            with open(self.state / 'journal.jsonl', 'ab') as stream:
                stream.write(b'{}\n')
        with self.assertRaisesRegex(ValueError, 'SHA mismatch'):
            self.prepare(move=mutate)
        backup = self.home / '.config/relay-holder/idle-upgrades' / self.value['operationId']
        self.assertTrue((backup / 'profiles').exists())
        self.assertFalse((backup / 'complete.json').exists())
        self.assertTrue((self.home / upgrade.FILES['service'][0]).exists())

    def test_no_clobber_preserves_concurrently_created_backup_entry(self):
        def collision(source, destination):
            destination.write_bytes(b'unexpected backup')
            upgrade.common.rename_noreplace(source, destination)
        with self.assertRaises(FileExistsError):
            self.prepare(move=collision)
        self.assertEqual((self.home / upgrade.FILES['profiles'][0]).read_bytes(), self.original['profiles'])

    def test_parent_or_lock_replacement_after_first_move_refused(self):
        for name in ('native-homes', 'holder.lock'):
            with self.subTest(name=name):
                # Each fault gets its own prepared fixture; the prior partial
                # transaction must not be silently repaired or retried.
                case = IdleUpgrade('runTest')
                case.setUp()
                self.addCleanup(case.doCleanups)
                original = case.state / name
                saved = case.home / ('preserved-' + name)
                def replace(source, destination):
                    upgrade.common.rename_noreplace(source, destination)
                    original.rename(saved)
                    if name == 'native-homes':
                        original.mkdir(mode=0o700)
                    else:
                        original.touch(mode=0o600)
                with self.assertRaisesRegex(ValueError, 'identity changed'):
                    case.prepare(move=replace)
                self.assertTrue(saved.exists())
                backup = case.home / '.config/relay-holder/idle-upgrades' / case.value['operationId']
                self.assertFalse((backup / 'complete.json').exists())


if __name__ == '__main__':
    unittest.main()
