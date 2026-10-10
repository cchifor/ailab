#!/usr/bin/env python3
"""Render the opt-in collector and run private-file setup in its pinned Node image."""
import hashlib
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
COMPONENT = ROOT / 'kubernetes/components/relay-control-monitoring'


class Monitoring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getuid() == 0:
            raise AssertionError('Run the fixture as an unprivileged user')
        (ROOT / 'out').mkdir(exist_ok=True)
        cls.scratch = tempfile.TemporaryDirectory(prefix='relay-metrics-', dir=ROOT / 'out')
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.root = Path(cls.scratch.name)
        wrapper = cls.root / 'render'
        wrapper.mkdir()
        base = ROOT / 'kubernetes/apps/apps/relay'
        (wrapper / 'kustomization.yaml').write_text(yaml.safe_dump({
            'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization',
            'resources': [os.path.relpath(base, wrapper)],
            'components': [os.path.relpath(COMPONENT, wrapper),
                           os.path.relpath(ROOT / 'kubernetes/components/relay-notifications/relay', wrapper)],
        }))
        raw = docker('run', '--rm', '-v', f'{ROOT}:/work:ro', '-w', '/work', KUSTOMIZE,
                     'build', str(wrapper.relative_to(ROOT))).stdout
        cls.docs = list(yaml.safe_load_all(raw))
        cls.spec = named(cls.docs, 'Deployment', 'relay')['spec']['template']['spec']
        init = next(c for c in cls.spec['initContainers'] if c['name'] == 'metrics-files')
        cls.node_image = init['image']
        cm_name = next(v['configMap']['name'] for v in cls.spec['volumes'] if v['name'] == 'metrics-setup')
        cm = named(cls.docs, 'ConfigMap', cm_name)
        (cls.root / 'materialize.mjs').write_text(cm['data']['materialize.mjs'])
        checked = [d for d in cls.docs if d['kind'] in ('Deployment', 'Service', 'ConfigMap', 'NetworkPolicy')]
        docker('run', '--rm', '-i', KUBECONFORM, '-strict', '-summary', data=yaml.safe_dump_all(checked).encode())
        for name in ['input', 'output']:
            (cls.root / name).mkdir()
        cls.container = 'relay-metrics-test-' + uuid.uuid4().hex[:10]
        cls.addClassCleanup(lambda: docker('rm', '-fv', cls.container, check=False))
        docker('run', '--rm', '-d', '--name', cls.container, '--network', 'none', '--read-only',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--user', f'{os.getuid()}:{os.getgid()}',
               '-v', f'{cls.root}/materialize.mjs:/setup/materialize.mjs:ro',
               '-v', f'{cls.root}/input:/input:ro', '-v', f'{cls.root}/output:/output',
               cls.node_image, 'node', '-e', 'setInterval(()=>{}, 1000)')

    def setUp(self):
        for name in ['input', 'output']:
            for entry in (self.root / name).iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        self.config = dict(version=1, tenantId=str(uuid.uuid4()),
                           tokenSha256=hashlib.sha256(b'synthetic-collector-token-not-authority').hexdigest(),
                           expiresAt='2099-01-01T00:00:00Z')
        self.seed()

    def seed(self):
        for filename, key in [('tenant-id', 'tenantId'), ('token-sha256', 'tokenSha256'), ('expires-at', 'expiresAt')]:
            (self.root / 'input' / filename).write_text(self.config[key] + '\n')

    def run_setup(self, ok=True):
        result = docker('exec', self.container, 'node', '/setup/materialize.mjs', check=False)
        output = (result.stdout + result.stderr).decode()
        for value in self.config.values():
            if isinstance(value, str):
                self.assertNotIn(value, output)
        self.assertNotIn('PRIVATE_CANARY', output)
        self.assertEqual(result.returncode == 0, ok, output)
        return result

    def test_private_atomic_setup_and_restart(self):
        # Follow kubelet symlinks on input only; output must be a regular file.
        real = self.root / 'input/tenant-real'
        (self.root / 'input/tenant-id').rename(real)
        (self.root / 'input/tenant-id').symlink_to(real.name)
        self.run_setup()
        directory = self.root / 'output/private'
        target = directory / 'config.json'
        self.assertEqual(json.loads(target.read_text()), self.config)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.stat().st_uid, os.getuid())
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertFalse(target.is_symlink())
        (directory / 'config.json.new').write_text('interrupted')
        self.run_setup()
        self.assertEqual(json.loads(target.read_text()), self.config)
        self.assertFalse((directory / 'config.json.new').exists())

    def test_expired_verifier_does_not_block_application_restart(self):
        # The endpoint checks expiry on each scrape; old verifiers grant no access.
        self.config['expiresAt'] = '2000-01-01T00:00:00Z'
        self.seed()
        self.run_setup()
        self.assertEqual(json.loads((self.root / 'output/private/config.json').read_text()), self.config)

    def test_invalid_inputs_fail_closed_and_keep_last_complete_file(self):
        self.run_setup()
        target = self.root / 'output/private/config.json'
        original = target.read_bytes()
        for filename, bad in [
            ('tenant-id', 'PRIVATE_CANARY'), ('tenant-id', '0' * 36),
            ('token-sha256', 'PRIVATE_CANARY'), ('token-sha256', 'a' * 129),
            ('expires-at', '2099-02-30T00:00:00Z'), ('expires-at', 'PRIVATE_CANARY'),
        ]:
            with self.subTest(filename=filename, case=len(bad)):
                self.seed()
                (self.root / 'input' / filename).write_text(bad)
                self.run_setup(False)
                self.assertEqual(target.read_bytes(), original)
        self.seed()
        (self.root / 'input/tenant-id').unlink()
        (self.root / 'input/tenant-id').mkdir()
        self.run_setup(False)

    def test_collector_has_separate_authority_and_fixed_placement(self):
        monitor = named(self.docs, 'ServiceMonitor', 'relay-control-plane')
        self.assertEqual(monitor['metadata']['namespace'], 'monitoring')
        self.assertEqual(monitor['metadata']['labels']['release'], 'kube-prometheus-stack')
        self.assertEqual(monitor['spec']['namespaceSelector'], {'matchNames': ['relay']})
        endpoint = monitor['spec']['endpoints'][0]
        self.assertEqual(endpoint['path'], '/metrics/agent-control-plane')
        self.assertFalse(endpoint['followRedirects'])
        self.assertEqual(endpoint['authorization'], {'type': 'Bearer', 'credentials': {'name': 'relay-control-collector', 'key': 'token'}})
        service = named(self.docs, 'Service', 'relay')
        for key, value in monitor['spec']['selector']['matchLabels'].items():
            self.assertEqual(service['metadata']['labels'][key], value)
        self.assertEqual(endpoint['port'], service['spec']['ports'][0]['name'])
        self.assertEqual(endpoint['relabelings'], [{'targetLabel': 'job', 'replacement': 'relay-control-plane'}])
        ingress = named(self.docs, 'NetworkPolicy', 'relay-control-metrics')['spec']
        self.assertEqual(ingress['podSelector'], {'matchLabels': {'app': 'relay'}})
        self.assertEqual(ingress['policyTypes'], ['Ingress'])
        source = ingress['ingress'][0]['from'][0]
        self.assertEqual(source['namespaceSelector']['matchLabels'], {'kubernetes.io/metadata.name': 'monitoring'})
        self.assertEqual(source['podSelector']['matchLabels']['app.kubernetes.io/instance'], 'kube-prometheus-stack-prometheus')
        self.assertEqual(ingress['ingress'][0]['ports'], [{'protocol': 'TCP', 'port': 8788}])

    def test_application_only_mounts_verifier_and_preserves_base_workloads(self):
        base_docs = list(yaml.safe_load_all((ROOT / 'kubernetes/apps/apps/relay/relay.yaml').read_text()))
        base = named(base_docs, 'Deployment', 'relay')['spec']['template']['spec']
        # Kubelet uses this fsGroup for 0440 projected Secret readability. The
        # native host-user fixture alone cannot establish cluster file ownership.
        self.assertEqual(base['securityContext']['fsGroup'], 1000)
        self.assertEqual(self.spec['securityContext']['fsGroup'], 1000)
        initializer = next(c for c in self.spec['initContainers'] if c['name'] == 'metrics-files')
        self.assertEqual(initializer['securityContext']['runAsUser'], 1000)
        self.assertEqual(initializer['securityContext']['runAsGroup'], 1000)
        self.assertEqual([c for c in self.spec['initContainers'] if c['name'] not in ('metrics-files', 'notification-files')], base['initContainers'])
        application = next(c for c in self.spec['containers'] if c['name'] == 'relay')
        self.assertEqual(application.get('securityContext', {}).get('runAsUser', self.spec['securityContext'].get('runAsUser')), 1000)
        self.assertEqual(application['image'], next(c for c in base['containers'] if c['name'] == 'relay')['image'])
        env = {e['name']: e.get('value') for e in application['env']}
        self.assertEqual(env['RELAY_CONTROL_METRICS_FILE'], '/run/relay-metrics/private/config.json')
        self.assertNotIn('RELAY_AGENT_CONTROL_PLANE', env)
        self.assertNotIn('metrics-input', {v['name'] for v in application['volumeMounts']})
        inputs = next(v['secret'] for v in self.spec['volumes'] if v['name'] == 'metrics-input')
        self.assertEqual(inputs['secretName'], 'relay-control-metrics')
        self.assertEqual({item['key'] for item in inputs['items']}, {'tenant-id', 'token-sha256', 'expires-at'})
        memory = next(v['emptyDir'] for v in self.spec['volumes'] if v['name'] == 'metrics-files')
        self.assertEqual(memory, {'medium': 'Memory', 'sizeLimit': '1Mi'})
        # This component does not activate itself in either live kustomization.
        for path in ['kubernetes/apps/apps/relay/kustomization.yaml', 'kubernetes/apps/infrastructure/monitoring/kustomization.yaml']:
            self.assertNotIn('relay-control-monitoring', (ROOT / path).read_text())


if __name__ == '__main__':
    unittest.main()
