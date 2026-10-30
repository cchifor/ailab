"""Version-coupled read-only Web artifact hotfix and its native guard tests."""
import pathlib
import re
import shutil
import subprocess
import unittest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = ROOT / 'kubernetes/apps/apps/dsh'

class CommandUiFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pod = yaml.safe_load((APP / 'deployment.yaml').read_text())['spec']['template']['spec']
        cls.kustom = yaml.safe_load((APP / 'kustomization.yaml').read_text())
        cls.init = next(c for c in cls.pod['initContainers'] if c['name'] == 'build-command-ui-fix')
        cls.main = next(c for c in cls.pod['containers'] if c['name'] == 'dsh')

    def test_native_guard_contract(self):
        self.assertIsNotNone(shutil.which('node'), 'Node is required; do not silently skip hotfix guards')
        run = subprocess.run(['node', '--test', str(ROOT / 'scripts/tests/dsh-command-ui-fix.test.mjs')], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_init_order_and_least_access(self):
        names = [c['name'] for c in self.pod['initContainers']]
        self.assertEqual(names.index('build-command-ui-fix'), names.index('wait-for-install') + 1)
        mounts = {m['name']: m for m in self.init['volumeMounts']}
        self.assertEqual(set(mounts), {'app', 'command-ui-fix-source', 'command-ui-fix', 'command-ui-fix-no-api'})
        self.assertTrue(mounts['app']['readOnly'])
        self.assertTrue(mounts['command-ui-fix-source']['readOnly'])
        self.assertEqual(mounts['command-ui-fix-no-api']['mountPath'], '/var/run/secrets/kubernetes.io/serviceaccount')
        self.assertTrue(mounts['command-ui-fix-no-api']['readOnly'])
        self.assertTrue(self.pod['automountServiceAccountToken'])  # existing operator unchanged
        self.assertTrue(self.init['securityContext']['runAsNonRoot'])
        self.assertEqual(self.init['securityContext']['runAsUser'], 1000)
        self.assertEqual(self.init['securityContext']['runAsGroup'], 1000)
        self.assertEqual(self.pod['securityContext']['runAsUser'], 1000)
        self.assertFalse(self.init['securityContext']['allowPrivilegeEscalation'])
        self.assertTrue(self.init['securityContext']['readOnlyRootFilesystem'])
        self.assertEqual(self.init['securityContext']['capabilities']['drop'], ['ALL'])
        self.assertRegex(self.init['image'], r'@sha256:[0-9a-f]{64}$')

    def test_only_the_version_pinned_client_file_is_overlaid(self):
        install = yaml.safe_load((APP / 'install-job.yaml').read_text())
        env = {v['name']: v.get('value') for v in install['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['DSH_VERSION'], '0.1.5-alpha.2', 'Remove/rebase the UI hotfix when upgrading DSH')
        self.assertEqual(env['DSH_BUILD'], 'glibc')
        overlay = [m for m in self.main['volumeMounts'] if m['name'] == 'command-ui-fix']
        self.assertEqual(overlay, [{'name': 'command-ui-fix', 'mountPath': f"/app/{env['DSH_VERSION']}-{env['DSH_BUILD']}/node_modules/@deepseek-ai/dsh-client-ui-chat/lib/client.js", 'subPath': 'client.js', 'readOnly': True}])
        self.assertTrue(next(m for m in self.main['volumeMounts'] if m['name'] == 'app')['readOnly'])
        relay = next(c for c in self.pod['containers'] if c['name'] == 'relay')
        self.assertFalse(any(m['name'].startswith('command-ui-fix') for m in relay['volumeMounts']))

    def test_version_build_are_derived_from_the_install_job(self):
        for name in ['DSH_VERSION', 'DSH_BUILD']:
            matching = [r for r in self.kustom['replacements'] if r['source']['fieldPath'].endswith(f'[name={name}].value')]
            self.assertEqual(len(matching), 1)
            paths = [p for t in matching[0]['targets'] for p in t['fieldPaths']]
            self.assertIn(f'spec.template.spec.initContainers.[name=build-command-ui-fix].env.[name={name}].value', paths)

    def test_separate_content_hashed_script_and_bounded_scratch(self):
        cm = next(c for c in self.kustom['configMapGenerator'] if c['name'] == 'dsh-command-ui-fix')
        self.assertEqual(cm['files'], ['build-command-ui-fix.mjs'])
        self.assertTrue(cm['options']['immutable'])
        self.assertFalse(cm['options'].get('disableNameSuffixHash', False))
        volumes = {v['name']: v for v in self.pod['volumes']}
        self.assertEqual(volumes['command-ui-fix']['emptyDir']['sizeLimit'], '10Mi')
        self.assertIn('emptyDir', volumes['command-ui-fix-no-api'])
        self.assertEqual(volumes['command-ui-fix-source']['configMap']['name'], 'dsh-command-ui-fix')

    def test_artifact_pins_and_no_mutating_source_operations(self):
        source = (APP / 'build-command-ui-fix.mjs').read_text()
        self.assertIn("SOURCE_SHA256='3891fc589652c50be24c6d8e68d35c5d4066ed9ae7b7d57a086d5a34ad20c0d9'", source)
        self.assertIn("OUTPUT_SHA256='9f53814b335eaa3f892fd70e8a22f167b4ba6fef412c598309a17d001bcf847c'", source)
        self.assertIn('fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW', source)
        self.assertIn("equal(sha256(output),OUTPUT_SHA256", source)
        self.assertNotRegex(source, r'(writeFileSync|renameSync|unlinkSync)\(sourceFile')

if __name__ == '__main__':
    unittest.main()
