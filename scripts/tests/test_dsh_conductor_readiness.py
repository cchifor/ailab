"""Execute actual Node modules with mocks; real SDK/preflight is a separate gate."""
import json
import pathlib
import shutil
import subprocess
import unittest
import yaml

ROOT=pathlib.Path(__file__).resolve().parents[2]
APP=ROOT/'kubernetes/apps/apps/dsh'

class ConductorDeploymentTests(unittest.TestCase):
    def test_node_modules(self):
        if not shutil.which('node'):
            self.skipTest('Node24+ required for dynamic observer tests')
        version=subprocess.check_output(['node','--version'],text=True)
        if int(version.lstrip('v').split('.')[0])<24:
            self.skipTest('Node24+ required for dynamic observer tests')
        result=subprocess.run(['node','--test','scripts/tests/fixtures/conductor-readiness.test.mjs','scripts/tests/fixtures/conductor-command.test.mjs'],cwd=ROOT,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_durable_parent_and_restart_marker(self):
        deployment=yaml.safe_load((APP/'deployment.yaml').read_text())
        spec=deployment['spec']['template']['spec']
        seed=next(c for c in spec['initContainers'] if c['name']=='seed-settings')['args'][0]
        main=next(c for c in spec['containers'] if c['name']=='dsh')['args'][0]
        self.assertIn('mkdir -p /dsh-home/team-conductor',seed)
        self.assertIn('chmod 0700 /dsh-home/team-conductor',seed)
        for script in (seed,main):
            self.assertIn('rm -f /dsh-home/team-conductor/readiness.json',script)
            self.assertNotIn('rm -f /dsh-home/team-conductor/state.sqlite',script)
            self.assertNotIn('rm -rf /dsh-home/team-conductor',script)
        self.assertLess(main.index('readiness.json'),main.index('exec '))

    def test_authorized_enabled_allowance_and_artifact(self):
        config=json.loads((APP/'conductor.runtime.json').read_text())
        self.assertIs(config['enabled'],True)
        self.assertEqual(config['admission']['maxCalls'],40)
        self.assertEqual(config['admission']['maxReservedTokens'],40000000)
        job=yaml.safe_load((APP/'install-job.yaml').read_text())
        self.assertTrue(job['metadata']['name'].endswith('-ps2'))
        env={x['name']:x.get('value') for x in job['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['DSH_PLUGINSET'],'ps2')
        sha=env['CONDUCTOR_ARTIFACT_SHA256']
        import hashlib
        self.assertEqual(hashlib.sha256((APP/'conductor-release/dsh-team-conductor-0.1.0.tgz').read_bytes()).hexdigest(),sha)
        self.assertIn('dsh-team-conductor@file:/app/conductor-artifacts/'+sha+'/dsh-team-conductor-0.1.0.tgz',env['DSH_PLUGINS'])

if __name__=='__main__':
    unittest.main()
