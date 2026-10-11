#!/usr/bin/env python3
"""Qualify optional Gitea authority wiring without live credentials or cluster writes."""
import base64
import copy
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
import uuid

import yaml
from test_relay_notifications import docker, KUSTOMIZE, KUBECONFORM, named

ROOT = Path(__file__).resolve().parents[3]
COMPONENTS = ROOT / 'kubernetes/components'


class GiteaAuthority(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getuid() == 0:
            raise AssertionError('Run the fixture as an unprivileged user')
        (ROOT / 'out').mkdir(exist_ok=True)
        cls.scratch = tempfile.TemporaryDirectory(prefix='relay-gitea-', dir=ROOT / 'out')
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.root = Path(cls.scratch.name)
        wrapper = cls.root / 'render'
        wrapper.mkdir()
        cls.config = {
            'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization',
            'resources': [os.path.relpath(ROOT / 'kubernetes/apps/apps/relay', wrapper)],
            'components': [os.path.relpath(COMPONENTS / name, wrapper) for name in [
                'relay-gitea-authority', 'relay-control-monitoring', 'relay-recovery-monitoring',
                'relay-recovery-monitoring-schema46', 'relay-notifications/relay']],
        }
        # The worker must inherit the selected application release, not its fallback.
        cls.release = 'registry.chifor.me/relay/control-plane@sha256:' + '1' * 64
        cls.config['patches'] = [{'target': {'kind': 'Deployment', 'name': 'relay'},
            'patch': json.dumps([{'op': 'replace', 'path': '/spec/template/spec/containers/0/image',
                                 'value': cls.release}])}]
        # Kustomize runs component transformers before parent patches. The final
        # replacement belongs to the activating overlay, after its release patch.
        cls.config['replacements'] = [{
            'source': {'kind': 'Deployment', 'name': 'relay',
                       'fieldPath': 'spec.template.spec.containers.[name=relay].image'},
            'targets': [{'select': {'kind': 'Deployment', 'name': 'relay-gitea-reconciler'},
                         'fieldPaths': ['spec.template.spec.containers.[name=reconciler].image']}],
        }]
        (wrapper / 'kustomization.yaml').write_text(yaml.safe_dump(cls.config))
        raw = docker('run', '--rm', '-v', f'{ROOT}:/work:ro', '-w', '/work', KUSTOMIZE,
                     'build', str(wrapper.relative_to(ROOT))).stdout
        cls.docs = list(yaml.safe_load_all(raw))
        cls.web = named(cls.docs, 'Deployment', 'relay')['spec']['template']['spec']
        cls.worker = named(cls.docs, 'Deployment', 'relay-gitea-reconciler')['spec']['template']['spec']
        checked = [d for d in cls.docs if d['kind'] in ('Deployment', 'ConfigMap', 'Service', 'NetworkPolicy', 'PersistentVolumeClaim')]
        docker('run', '--rm', '-i', KUBECONFORM, '-strict', '-summary', data=yaml.safe_dump_all(checked).encode())
        init = next(c for c in cls.web['initContainers'] if c['name'] == 'gitea-files')
        cm = next(v['configMap']['name'] for v in cls.web['volumes'] if v['name'] == 'gitea-setup')
        (cls.root / 'materialize.mjs').write_text(named(cls.docs, 'ConfigMap', cm)['data']['materialize.mjs'])
        for name in ['input', 'output']:
            (cls.root / name).mkdir()
        cls.container = 'relay-gitea-test-' + uuid.uuid4().hex[:10]
        cls.addClassCleanup(lambda: docker('rm', '-fv', cls.container, check=False))
        docker('run', '--rm', '-d', '--name', cls.container, '--network', 'none', '--read-only',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--user', f'{os.getuid()}:{os.getgid()}',
               '-v', f'{cls.root}/materialize.mjs:/setup/materialize.mjs:ro',
               '-v', f'{cls.root}/input:/input:ro', '-v', f'{cls.root}/output:/output',
               init['image'], 'node', '-e', 'setInterval(()=>{}, 1000)')

    def setUp(self):
        for name in ['input', 'output']:
            for entry in (self.root / name).iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        self.token = 'PRIVATE_CANARY_' + uuid.uuid4().hex
        self.item = dict(id=str(uuid.uuid4()), tenants=[str(uuid.uuid4())],
                         origin='https://git.example', repositories=['cchifor/relay'],
                         pullRequests={'cchifor/relay': 'main'}, ssh=dict(
                             host='git.example', port=22, user='git', hostKey='ssh-ed25519 ' +
                             base64.b64encode(bytes.fromhex('0000000b7373682d6564323535313900000020') + bytes(32)).decode()))
        self.seed()

    def seed(self, items=None):
        (self.root / 'input/deployments.json').write_text(json.dumps(items if items is not None else [self.item]))
        (self.root / 'input' / ('issuer-' + self.item['id'])).write_text(self.token + '\n')

    def run_setup(self, ok=True):
        result = docker('exec', self.container, 'node', '/setup/materialize.mjs', check=False)
        output = (result.stdout + result.stderr).decode()
        self.assertNotIn(self.token, output)
        self.assertNotIn('PRIVATE_CANARY', output)
        self.assertEqual(result.returncode == 0, ok, output)

    def test_private_files_kubelet_input_and_interrupted_restart(self):
        source = self.root / 'input/deployments.json'
        source.rename(self.root / 'input/data')
        source.symlink_to('data')
        self.run_setup()
        directory = self.root / 'output/private'
        config = directory / 'deployments.json'
        expected = [{**self.item, 'enabled': False,
                     'tokenFile': '/run/relay-gitea/private/issuer-' + self.item['id']}]
        self.assertEqual(json.loads(config.read_text()), expected)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for entry in directory.iterdir():
            self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o600)
            self.assertEqual(entry.stat().st_uid, os.getuid())
            self.assertEqual(entry.stat().st_nlink, 1)
            self.assertFalse(entry.is_symlink())
        token = directory / ('issuer-' + self.item['id'])
        self.assertEqual(token.read_text(), self.token)
        (directory / 'deployments.json.new').write_text('interrupted')
        self.run_setup()
        self.assertEqual(json.loads(config.read_text()), expected)
        self.assertFalse((directory / 'deployments.json.new').exists())

    def test_invalid_inputs_preserve_previous_configuration_and_never_log_values(self):
        self.run_setup()
        directory = self.root / 'output/private'
        original = {p.name: p.read_bytes() for p in directory.iterdir()}
        invalid = []
        for key, value in [('id', '../PRIVATE_CANARY'), ('origin', 'http://git.example'),
                           ('origin', 'https://git.example/path'), ('origin', 'https://PRIVATE_CANARY@git.example'),
                           ('tenants', []), ('tenants', ['PRIVATE_CANARY']), ('repositories', ['../PRIVATE_CANARY']),
                           ('enabled', 'true'), ('tokenFile', '/PRIVATE_CANARY'),
                           ('pullRequests', {'cchifor/elsewhere': 'main'}),
                           ('pullRequests', {'cchifor/relay': 'refs/../main'})]:
            item = copy.deepcopy(self.item)
            item[key] = value
            invalid.append([item])
        for key, value in [('hostKey', 'ssh-rsa PRIVATE_CANARY'), ('port', 0), ('user', 'root'),
                           ('host', '-PRIVATE_CANARY'), ('extra', 'PRIVATE_CANARY')]:
            item = copy.deepcopy(self.item)
            item['ssh'][key] = value
            invalid.append([item])
        invalid.extend([[self.item, self.item], {}, [self.item] * 21])
        for items in invalid:
            with self.subTest(items_type=type(items).__name__):
                self.seed(items)
                self.run_setup(False)
                self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, original)
        self.seed()
        token = self.root / 'input' / ('issuer-' + self.item['id'])
        for bad in ['short', 'PRIVATE_CANARY\nline', 'a' * 4097]:
            token.write_text(bad)
            self.run_setup(False)
            self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, original)
        token.unlink()
        token.mkdir()
        self.run_setup(False)

    def test_missing_secret_is_fatal_and_empty_list_grants_no_authority(self):
        (self.root / 'input' / ('issuer-' + self.item['id'])).unlink()
        self.run_setup(False)
        self.assertEqual(list((self.root / 'output').iterdir()), [])
        self.seed([])
        self.run_setup()
        self.assertEqual(json.loads((self.root / 'output/private/deployments.json').read_text()), [])

    def test_worker_matches_release_and_has_only_database_and_gitea_authority(self):
        app = next(c for c in self.web['containers'] if c['name'] == 'relay')
        worker = self.worker['containers'][0]
        self.assertEqual(app['image'], self.release)
        self.assertEqual(worker['image'], self.release)
        self.assertEqual(worker['command'], ['node', 'dist/scripts/reconcile-environment-gitea.js'])
        fallback = yaml.safe_load((COMPONENTS / 'relay-gitea-authority/worker.yaml').read_text())
        self.assertEqual(fallback['spec']['template']['spec']['containers'][0]['image'],
                         'registry.invalid/relay-gitea-reconciler:release-required')
        self.assertNotIn('envFrom', worker)
        refs = {e['name']: e['valueFrom']['secretKeyRef'] for e in worker['env'] if 'valueFrom' in e}
        self.assertEqual(refs, {k: {'name': 'relay-secrets', 'key': k} for k in ['DATABASE_URL', 'APP_DATABASE_URL']})
        self.assertFalse(self.worker['automountServiceAccountToken'])
        self.assertEqual(self.worker['securityContext']['runAsUser'], 1000)
        self.assertEqual(worker['securityContext']['capabilities'], {'drop': ['ALL']})
        self.assertTrue(worker['securityContext']['readOnlyRootFilesystem'])
        self.assertEqual(set(v['name'] for v in self.worker['volumes']),
                         {'gitea-setup', 'gitea-input', 'gitea-files', 'restore-generation'})
        for spec, container in [(self.web, app), (self.worker, worker)]:
            env = {e['name']: e.get('value') for e in container['env']}
            self.assertEqual(env['RELAY_GITEA_ISSUERS_FILE'], '/run/relay-gitea/private/deployments.json')
            self.assertEqual(env['RELAY_RECOVERY_GENERATION_FILE'], '/run/relay-restore/generation.json')
            self.assertNotIn('gitea-input', {m['name'] for m in container['volumeMounts']})
            private = next(m for m in container['volumeMounts'] if m['name'] == 'gitea-files')
            self.assertTrue(private['readOnly'])
            volume = next(v for v in spec['volumes'] if v['name'] == 'gitea-input')
            self.assertEqual(volume['secret'], {'secretName': 'relay-gitea-issuers', 'defaultMode': 288})
            self.assertEqual(next(v['emptyDir'] for v in spec['volumes'] if v['name'] == 'gitea-files'),
                             {'medium': 'Memory', 'sizeLimit': '1Mi'})
            mount = next(m for m in container['volumeMounts'] if m['name'] == 'restore-generation')
            self.assertEqual(mount['subPath'], 'generation.json')
            self.assertTrue(mount['readOnly'])
        self.assertFalse(any(d['kind'] in ['Secret', 'ConfigMap'] and
                             d['metadata']['name'] in ['relay-gitea-issuers', 'relay-recovery-generation'] for d in self.docs))

    def test_worker_network_isolation_and_opt_in_schema(self):
        deployment = named(self.docs, 'Deployment', 'relay-gitea-reconciler')
        labels = deployment['spec']['template']['metadata']['labels']
        selector = named(self.docs, 'Service', 'relay')['spec']['selector']
        self.assertFalse(all(labels.get(k) == v for k, v in selector.items()))
        policy = named(self.docs, 'NetworkPolicy', 'relay-gitea-reconciler')['spec']
        self.assertEqual(policy['podSelector']['matchLabels'], labels)
        self.assertEqual(policy['ingress'], [])
        self.assertEqual(policy['policyTypes'], ['Ingress', 'Egress'])
        self.assertEqual([p['port'] for rule in policy['egress'] for p in rule['ports']], [53, 53, 5432, 443])
        public = policy['egress'][2]['to'][0]['ipBlock']
        self.assertEqual(public['except'], ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'])
        postgres = named(self.docs, 'NetworkPolicy', 'relay-postgres-gitea-worker')['spec']
        self.assertEqual(postgres['ingress'][0]['from'], [{'podSelector': {'matchLabels': labels}}])
        configmaps = [d for d in self.docs if d['kind'] == 'ConfigMap' and d['metadata']['name'].startswith('relay-recovery-policy-')]
        self.assertEqual(len(configmaps), 1)
        self.assertEqual(configmaps[0]['data']['schema-version'], '46')
        for name, version in [('relay-recovery-monitoring', '42'), ('relay-recovery-monitoring-schema43', '43')]:
            baseline = yaml.safe_load((COMPONENTS / name / 'kustomization.yaml').read_text())
            self.assertIn('schema-version=' + version, baseline['configMapGenerator'][0]['literals'])
        overlay = (ROOT / 'kubernetes/apps/apps/relay/kustomization.yaml').read_text()
        for name in ['relay-gitea-authority', 'relay-recovery-monitoring-schema46']:
            self.assertNotIn(name, overlay)


if __name__ == '__main__':
    unittest.main()
