import fcntl
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
import uuid

SOURCE = Path(__file__).resolve().parents[1] / 'restage-inference-canary.py'
SPEC = importlib.util.spec_from_file_location('restage', SOURCE)
restage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restage)


@unittest.skipUnless(os.name == 'posix', 'Linux filesystem contract')
class Restage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.state = self.home / '.local/state/relay-holder'
        for root in ('native-homes', 'workspaces'):
            for profile in restage.PROFILES:
                path = self.state / root / profile
                path.mkdir(parents=True, mode=0o700)
        for path in [self.home, *self.home.rglob('*')]:
            path.chmod(0o700)
        self.original = {}
        self.hashes = {}
        for key, (rel, mode) in restage.FILES.items():
            path = self.home / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            body = ('old bytes ' + key + '\n').encode()
            path.write_bytes(body)
            path.chmod(mode)
            self.original[key] = body
            self.hashes[key] = hashlib.sha256(body).hexdigest()
        self.operation = str(uuid.uuid4())

    def status(self, name):
        return {'Id': name, 'ActiveState': 'inactive', 'SubState': 'dead', 'MainPID': '0',
                'ControlGroup': '', 'UnitFileState': 'disabled', 'DropInPaths': '',
                'FragmentPath': str(self.home / '.config/systemd/user' / name)}

    def prepare(self, **kwargs):
        return restage.prepare(self.operation, self.hashes, home=self.home, uid=os.getuid(),
                               status=kwargs.pop('status', self.status), **kwargs)

    def test_preserves_bytes_and_placeholders_without_start(self):
        result = self.prepare()
        backup = Path(result['backup'])
        for key, (rel, _) in restage.FILES.items():
            self.assertFalse((self.home / rel).exists())
            self.assertEqual((backup / key).read_bytes(), self.original[key])
        for profile in restage.PROFILES:
            self.assertTrue((backup / ('placeholder-' + profile)).is_dir())
            self.assertTrue((self.state / 'workspaces' / profile).is_dir())
        self.assertEqual(list((self.state / 'native-homes').iterdir()), [])
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertTrue((backup / 'complete.json').is_file())
        self.assertTrue((self.state / 'holder.lock').is_file())
        self.assertFalse((self.state / 'journal.jsonl').exists())

    def test_partial_failure_preserves_every_byte_and_refuses_retry(self):
        count = 0
        def fail(src, dst):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError('injected failure')
            restage.rename_noreplace(src, dst)
        with self.assertRaisesRegex(OSError, 'injected'):
            self.prepare(move=fail)
        backup = self.home / '.config/relay-holder/restaging' / self.operation
        for key, (rel, _) in restage.FILES.items():
            locations = [p for p in (self.home / rel, backup / key) if p.exists()]
            self.assertEqual(len(locations), 1)
            self.assertEqual(locations[0].read_bytes(), self.original[key])
        self.assertFalse((backup / 'complete.json').exists())
        with self.assertRaises(ValueError):
            self.prepare()

    def test_active_enabled_pid_dropin_and_wrong_fragment_refused(self):
        for field, value in [('ActiveState', 'active'), ('UnitFileState', 'enabled'),
                             ('MainPID', '17'), ('DropInPaths', '/override'),
                             ('ControlGroup', '/occupied'), ('FragmentPath', '/elsewhere')]:
            with self.subTest(field=field):
                def status(name):
                    return {**self.status(name), field: value}
                with self.assertRaisesRegex(ValueError, 'stopped'):
                    self.prepare(status=status)
                self.assertFalse((self.state / 'holder.lock').exists())

    def test_hash_refusal(self):
        self.hashes['profiles'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'SHA mismatch'):
            self.prepare()
        self.assertFalse((self.state / 'holder.lock').exists())

    def test_symlink_file_refused(self):
        path = self.home / restage.FILES['profiles'][0]
        target = path.with_suffix('.saved')
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaises(OSError):
            self.prepare()

    def test_hardlink_file_refused(self):
        path = self.home / restage.FILES['profiles'][0]
        os.link(path, path.with_suffix('.linked'))
        with self.assertRaisesRegex(ValueError, 'Unsafe file metadata'):
            self.prepare()

    def test_symlink_placeholder_refused(self):
        path = self.state / 'native-homes' / restage.PROFILES[0]
        target = self.home / 'preserved-placeholder'
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'Unsafe directory'):
            self.prepare()

    def test_nonempty_placeholder_and_unknown_state_refused(self):
        extra = self.state / 'native-homes' / restage.PROFILES[0] / 'history'
        extra.write_text('preserve')
        with self.assertRaisesRegex(ValueError, 'not empty'):
            self.prepare()
        self.assertEqual(extra.read_text(), 'preserve')
        extra.unlink()
        (self.state / 'journal.jsonl').write_text('preserve journal')
        with self.assertRaisesRegex(ValueError, 'never-used'):
            self.prepare()

    def test_reused_operation_refused(self):
        backup = self.home / '.config/relay-holder/restaging' / self.operation
        backup.mkdir(parents=True, mode=0o700)
        backup.parent.chmod(0o700)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.prepare()

    def test_no_clobber(self):
        source, destination = self.home / 'source', self.home / 'destination'
        source.write_text('old'); destination.write_text('keep')
        with self.assertRaises(FileExistsError):
            restage.rename_noreplace(source, destination)
        self.assertEqual(source.read_text(), 'old')
        self.assertEqual(destination.read_text(), 'keep')

    def test_startup_lock_held_for_every_move(self):
        calls = []
        def move(src, dst):
            with (self.state / 'holder.lock').open('r+') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            calls.append(src)
            restage.rename_noreplace(src, dst)
        self.prepare(move=move)
        self.assertEqual(len(calls), 7)
        with (self.state / 'holder.lock').open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == '__main__':
    unittest.main()
