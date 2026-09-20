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
        self.assertEqual((APP/'conductor-release/dsh-team-conductor-0.1.0.tgz.sha256').read_text().split()[0],sha)
        self.assertFalse((APP/'conductor-release/dsh-team-conductor-0.1.0.tgz').exists())
        generated=yaml.safe_load((APP/'kustomization.yaml').read_text())['configMapGenerator']
        release=next(x for x in generated if x['name']=='dsh-conductor-release')
        self.assertNotIn('conductor-release/dsh-team-conductor-0.1.0.tgz',release['files'])
        for file in ['install-job.yaml','deployment.yaml']:
            spec=yaml.safe_load((APP/file).read_text())['spec']['template']['spec']
            volume=next(v for v in spec['volumes'] if v['name']=='conductor-release')
            artifact=volume['projected']['sources'][1]['configMap']
            self.assertEqual(artifact['name'],'dsh-conductor-artifact-'+sha[:16])
            self.assertEqual(artifact['items'],[{'key':'dsh-team-conductor-0.1.0.tgz','path':'dsh-team-conductor-0.1.0.tgz'}])
        self.assertIn('dsh-team-conductor@file:/app/conductor-artifacts/'+sha+'/dsh-team-conductor-0.1.0.tgz',env['DSH_PLUGINS'])

if __name__=='__main__':
    unittest.main()
