"""The schema bundle is public source provenance, never activation or qualification."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('budget_bundle', ROOT / 'scripts/relay-budget-schema-bundle.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SchemaBundle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'bundle'
        self.commit = 'a' * 40
        self.calls = []

    def git(self, args, **kwargs):
        self.calls.append(args)
        if 'rev-parse' in args:
            return subprocess.CompletedProcess(args, 0, self.commit + '\n')
        path = args[-1]
        self.assertTrue(path.startswith(self.commit + ':deploy/worker-budget-ledger/'))
        self.assertIn(path.split('/')[-1], MODULE.SCHEMAS)
        return subprocess.CompletedProcess(args, 0, ('-- committed SQL ' + path + '\n').encode())

    def render(self):
        with patch.object(MODULE.subprocess, 'run', side_effect=self.git):
            return MODULE.render(Path('/reviewed/relay'), self.commit, self.output)

    def test_bundle_committed_inventory_and_independent_manifest_pin(self):
        receipt = self.render()
        doc = json.loads((self.output / 'configmap.json').read_text())
        self.assertTrue(doc['immutable'])
        self.assertEqual(doc['metadata']['labels']['velero.io/exclude-from-backup'], 'true')
        self.assertEqual(set(doc['data']), {'source-commit', 'SHA256SUMS', *MODULE.SCHEMAS})
        for name, value in doc['data'].items():
            self.assertEqual((self.output / name).read_bytes(), value.encode())
        sums = doc['data']['SHA256SUMS']
        self.assertEqual(receipt['manifestSha256'], hashlib.sha256(sums.encode()).hexdigest())
        for line in sums.splitlines():
            digest, name = line.split('  ')
            self.assertEqual(digest, hashlib.sha256(doc['data'][name].encode()).hexdigest())
        self.assertEqual(receipt['qualification'], 'not-evaluated')
        self.assertEqual(receipt['schemaVersion'], 4)
        self.assertEqual(len(self.calls), 5)

    def test_moving_or_ambiguous_source_is_rejected_before_git(self):
        with patch.object(MODULE.subprocess, 'run') as git:
            for source in ['main', 'HEAD', 'a' * 39, 'A' * 40, '--help', 'a' * 40 + ':path']:
                with self.subTest(source=source), self.assertRaises(ValueError):
                    MODULE.render(Path('/repo'), source, self.output)
            git.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_existing_bundle_is_not_overwritten(self):
        self.render()
        prior = (self.output / 'configmap.json').read_bytes()
        with self.assertRaises(FileExistsError):
            self.render()
        self.assertEqual((self.output / 'configmap.json').read_bytes(), prior)

    def test_missing_source_file_creates_no_partial_bundle(self):
        def missing(args, **kwargs):
            if args[-1].endswith('004-preflight-errors.sql'):
                raise subprocess.CalledProcessError(128, args)
            return self.git(args, **kwargs)
        with patch.object(MODULE.subprocess, 'run', side_effect=missing), self.assertRaises(subprocess.CalledProcessError):
            MODULE.render(Path('/repo'), self.commit, self.output)
        self.assertFalse(self.output.exists())

    def test_jobs_stay_suspended_standalone_and_require_independent_pins(self):
        directory = ROOT / 'kubernetes/optional/relay-budget-authority/initialize'
        base = yaml.safe_load((directory.parent / 'kustomization.yaml').read_text())
        scripts = yaml.safe_load((directory / 'kustomization.yaml').read_text())
        self.assertNotIn('initialize', base['resources'])
        self.assertEqual(scripts.get('resources', []), [])
        for name, command in [('job.yaml', 'initialize.sh'), ('upgrade-job.yaml', 'upgrade.sh')]:
            job = yaml.safe_load((directory / name).read_text())
            self.assertTrue(job['spec']['suspend'])
            self.assertEqual(job['spec']['backoffLimit'], 0)
            spec = job['spec']['template']['spec']
            self.assertFalse(spec['automountServiceAccountToken'])
            self.assertEqual(spec['restartPolicy'], 'Never')
            container = spec['containers'][0]
            self.assertEqual(container['command'], ['/bin/sh', '/initialize/' + command])
            env = {item['name']: item['value'] for item in container['env']}
            self.assertEqual(env['RELAY_BUDGET_SCHEMA_MANIFEST_SHA256'], '')
            if command == 'upgrade.sh':
                self.assertEqual(env['RELAY_BUDGET_LEGACY_SCHEMA_SHA256'], '')
            self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
            projection = next(v['configMap'] for v in spec['volumes'] if v['name'] == 'schema')
            self.assertEqual({v['key'] for v in projection['items']}, {'source-commit', 'SHA256SUMS', *MODULE.SCHEMAS})


if __name__ == '__main__':
    unittest.main()
