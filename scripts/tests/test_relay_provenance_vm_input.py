"""Pinned VM upgrade input generation without an external Relay checkout."""
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/relay-provenance-vm-upgrade-input.py'
SPEC = importlib.util.spec_from_file_location('relay_provenance_vm_input', SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
COMMIT = 'a' * 40


class InputTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'relay'
        folder = self.repo / module.PREFIX
        folder.mkdir(parents=True)
        self.files = {}
        self.expected = {}
        for key, name in module.FILES.items():
            raw = ('BEGIN;\n-- ' + key + '\nCOMMIT;\n').encode()
            (folder / name).write_bytes(raw)
            self.files[name] = raw
            self.expected[key] = hashlib.sha256(raw).hexdigest()

    def git(self, *args):
        if args == ('rev-parse', 'HEAD'):
            return (COMMIT + '\n').encode()
        if args[0] == 'show' and args[1].startswith(COMMIT + ':' + module.PREFIX):
            return self.files[args[1].split('/')[-1]]
        raise AssertionError(args)

    def test_prepares_one_exclusive_private_input_bound_to_commit_bytes(self):
        output = self.root / 'render.json'
        digest = module.prepare(self.repo, COMMIT, output, self.expected, self.git)
        self.assertEqual(digest, hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        config = json.loads(output.read_text())
        self.assertEqual(config['fromVersion'], 11)
        self.assertEqual(config['relaySourceCommit'], COMMIT)
        self.assertEqual({key for key in config if key.endswith('Schema011') or key == 'schema008Repair'},
                         {'schema008Repair', 'vmSchema011'})
        self.assertRaises(FileExistsError, module.prepare, self.repo, COMMIT, output, self.expected, self.git)

    def test_refuses_artifact_drift_and_a_different_commit(self):
        self.assertRaises(ValueError, module.prepare, self.repo, 'b' * 40,
                          self.root / 'wrong.json', self.expected, self.git)
        key = 'vmSchema014'
        file = self.repo / module.PREFIX / module.FILES[key]
        file.write_text('BEGIN;\nCOMMIT;\n')
        self.assertRaises(ValueError, module.prepare, self.repo, COMMIT,
                          self.root / 'drift.json', self.expected, self.git)
        self.assertFalse((self.root / 'drift.json').exists())
        file.write_bytes(self.files[module.FILES[key]])
        def altered_git(*args):
            return b'other bytes' if args[0] == 'show' else self.git(*args)
        self.assertRaises(ValueError, module.prepare, self.repo, COMMIT,
                          self.root / 'mismatch.json', self.expected, altered_git)
        self.assertFalse((self.root / 'mismatch.json').exists())


if __name__ == '__main__':
    unittest.main()
