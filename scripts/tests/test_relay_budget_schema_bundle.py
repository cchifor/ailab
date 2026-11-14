"""The schema bundle is public source provenance, never activation or qualification."""
import hashlib
import importlib.util
import json
import re
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
        self.assertEqual(receipt['schemaVersion'], 5)
        self.assertIs(receipt['pinned'], False)
        self.assertEqual(len(self.calls), 1 + len(MODULE.SCHEMAS))
        self.assertEqual(MODULE.SCHEMAS[-1], '005-provenance-purpose.sql')

    def test_receipt_is_pinned_only_for_the_reviewed_commit(self):
        with patch.object(MODULE.subprocess, 'run', side_effect=self.git):
            receipt = MODULE.render(Path('/reviewed/relay'), self.commit, self.output, pin=self.commit)
        self.assertIs(receipt['pinned'], True)

    def test_source_pin_grammar(self):
        committed = MODULE.pinned_commit()
        self.assertRegex(committed, r'^(|[0-9a-f]{40})$')
        pin = Path(self.temp.name) / 'relay-source.env'
        good = '# reviewed\n\nRELAY_BUDGET_SOURCE_COMMIT=' + 'b' * 40 + '\n'
        pin.write_text(good)
        self.assertEqual(MODULE.pinned_commit(pin), 'b' * 40)
        pin.write_text('RELAY_BUDGET_SOURCE_COMMIT=\n')
        self.assertEqual(MODULE.pinned_commit(pin), '')
        for bad in ['', '# only a comment\n',
                    'RELAY_BUDGET_SOURCE_COMMIT=' + 'b' * 39 + '\n',
                    'RELAY_BUDGET_SOURCE_COMMIT=' + 'B' * 40 + '\n',
                    'RELAY_BUDGET_SOURCE_COMMIT=main\n',
                    good + 'RELAY_BUDGET_SOURCE_COMMIT=' + 'c' * 40 + '\n',
                    good + 'OTHER=1\n', ' ' + good.splitlines()[2] + '\n']:
            with self.subTest(bad=bad):
                pin.write_text(bad)
                with self.assertRaises(ValueError):
                    MODULE.pinned_commit(pin)

    def test_unset_pin_refuses_before_git(self):
        with patch.object(MODULE, 'pinned_commit', return_value=''), \
                patch.object(MODULE.subprocess, 'run') as git, \
                patch('sys.argv', ['bundle', '/repo', str(self.output)]), \
                self.assertRaises(SystemExit) as exit_:
            MODULE.main()
        self.assertEqual(exit_.exception.code, 1)
        git.assert_not_called()
        self.assertFalse(self.output.exists())

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
        server = next(doc for doc in yaml.safe_load_all((directory.parent / 'postgres.yaml').read_text()) if doc['kind'] == 'StatefulSet')
        server_image = server['spec']['template']['spec']['containers'][0]['image']
        scripts = yaml.safe_load((directory / 'kustomization.yaml').read_text())
        self.assertNotIn('initialize', base['resources'])
        self.assertEqual(scripts.get('resources', []), [])
        # The single reviewed source pin ships in the script ConfigMap both Jobs mount.
        self.assertIn('relay-source.env', scripts['configMapGenerator'][0]['files'])
        for name, command in [('job.yaml', 'initialize.sh'), ('upgrade-job.yaml', 'upgrade.sh')]:
            job = yaml.safe_load((directory / name).read_text())
            self.assertTrue(job['spec']['suspend'])
            self.assertEqual(job['spec']['backoffLimit'], 0)
            spec = job['spec']['template']['spec']
            self.assertFalse(spec['automountServiceAccountToken'])
            self.assertEqual(spec['restartPolicy'], 'Never')
            container = spec['containers'][0]
            self.assertEqual(container['image'], server_image)
            self.assertEqual(container['command'], ['/bin/sh', '/initialize/' + command])
            env = {item['name']: item['value'] for item in container['env']}
            self.assertEqual(env['RELAY_BUDGET_SCHEMA_MANIFEST_SHA256'], '')
            if command == 'upgrade.sh':
                self.assertNotIn('activeDeadlineSeconds', job['spec'])
                self.assertEqual(env['RELAY_BUDGET_LEGACY_SCHEMA_SHA256'], '')
            self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
            projection = next(v['configMap'] for v in spec['volumes'] if v['name'] == 'schema')
            self.assertEqual({v['key'] for v in projection['items']}, {'source-commit', 'SHA256SUMS', *MODULE.SCHEMAS})


if __name__ == '__main__':
    unittest.main()


class ScriptsAgreeWithTheRenderer(unittest.TestCase):
    """Inventory, apply order and marker live in shell and Python; they must not drift."""

    DIRECTORY = ROOT / 'kubernetes/optional/relay-budget-authority'

    def shell(self, name):
        return (self.DIRECTORY / name).read_text().replace('\\\n', ' ')

    def test_inventory_apply_order_and_marker(self):
        names = list(MODULE.SCHEMAS)
        verify = self.shell('initialize/verify-schema.sh')
        inventory = re.search(r'sha256sum source-commit ([^)]*)\)', verify).group(1).split()
        self.assertEqual(inventory, names)
        self.assertIn(f"printf '{MODULE.SCHEMA_VERSION}\\n'", verify)
        loop = re.search(r'for schema in ([^;]*);', self.shell('initialize/initialize.sh')).group(1).split()
        self.assertEqual(loop, names)
        upgrade = self.shell('initialize/upgrade.sh')
        self.assertEqual(re.search(r'for schema in ([^;]*);', upgrade).group(1).split(), names[1:])
        # A retained version-1 ledger is the only accepted upgrade input.
        self.assertIn('= 1 ] || exit 78', upgrade)
        start = self.shell('start.sh')
        self.assertIn(f'!= {MODULE.SCHEMA_VERSION} ]', start)
        self.assertNotIn('!= 4 ]', start)
