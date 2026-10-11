"""Run the rules gate with a Docker stub to prove companion fixtures are tested."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class RulesLintFixtureTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        (self.root / 'scripts').mkdir()
        for name in ('rules-lint.sh', 'promrule-spec.py', 'promtest-refs.py'):
            shutil.copy2(ROOT / 'scripts' / name, self.root / 'scripts' / name)
        self.rules = self.root / 'kubernetes'
        self.rules.mkdir()
        (self.rules / 'example-rules.yaml').write_text('''apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: example
spec:
  groups:
    - name: example
      rules:
        - alert: Example
          expr: vector(1)
''')
        self.fixture('example-rules.test.yaml')
        (self.root / 'bin').mkdir()
        docker = self.root / 'bin/docker'
        docker.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
if 'check' in sys.argv:
    print('SUCCESS: 1 rules found')
elif 'test' in sys.argv:
    pathlib.Path(os.environ['DOCKER_TEST_RECEIPT']).write_text(json.dumps(sys.argv))
    sys.exit(int(os.environ.get('DOCKER_TEST_EXIT', '0')))
else:
    sys.exit(99)
''')
        docker.chmod(0o755)
        self.receipt = self.root / 'receipt.json'
        self.env = {**os.environ, 'PATH': str(self.root / 'bin') + ':' + os.environ['PATH'],
                    'DOCKER_TEST_RECEIPT': str(self.receipt)}

    def fixture(self, name, reference='example-rules.yaml'):
        (self.rules / name).write_text(f'rule_files:\n  - {reference}\ntests: []\n')

    def run_gate(self, exit_code=0):
        return subprocess.run(['bash', str(self.root / 'scripts/rules-lint.sh')],
                              env={**self.env, 'DOCKER_TEST_EXIT': str(exit_code)},
                              capture_output=True, text=True)

    def test_primary_fixture_still_runs_without_companions(self):
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('/rules/example-rules.test.yaml', json.loads(self.receipt.read_text()))

    def test_companion_is_executed_and_its_failure_fails_the_gate(self):
        self.fixture('example-rules.renewal.test.yaml')
        result = self.run_gate(exit_code=7)
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertIn('/rules/example-rules.renewal.test.yaml', json.loads(self.receipt.read_text()))

    def test_companion_with_missing_rule_fails_before_testing(self):
        self.fixture('example-rules.renewal.test.yaml', 'missing-rules.yaml')
        result = self.run_gate()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('missing-rules.yaml', result.stdout + result.stderr)
        self.assertFalse(self.receipt.exists())


if __name__ == '__main__':
    unittest.main()
