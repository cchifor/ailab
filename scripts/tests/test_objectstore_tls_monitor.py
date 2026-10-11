"""Keep the served-certificate probe private, verified and actually selected."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
MONITORING = ROOT / 'kubernetes/apps/infrastructure/monitoring'


class ObjectstoreTLSMonitorTests(unittest.TestCase):
    def setUp(self):
        self.docs = list(yaml.safe_load_all((MONITORING / 'objectstore-tls-probe.yaml').read_text()))

    def kind(self, kind):
        return next(d for d in self.docs if d['kind'] == kind)

    def test_probe_verifies_the_browser_hostname_with_the_estate_public_ca(self):
        config = yaml.safe_load((MONITORING / 'objectstore-blackbox.yaml').read_text())
        module = config['modules']['objectstore_tls']
        self.assertEqual(module['prober'], 'tcp')
        self.assertTrue(module['tcp']['tls'])
        self.assertEqual(module['tcp']['tls_config'], {
            'ca_file': '/etc/objectstore-ca/ca.crt',
            'server_name': 'forge-objects.taild43998.ts.net',
            'insecure_skip_verify': False,
        })
        probe = self.kind('Probe')
        self.assertEqual(probe['metadata']['labels']['release'], 'kube-prometheus-stack')
        self.assertEqual(probe['spec']['jobName'], 'objectstore-tls')
        self.assertEqual(probe['spec']['targets']['staticConfig']['static'], [
            'objectstore-forge-tls.strive-ailab.svc.cluster.local:443'])
        self.assertEqual(probe['spec']['module'], 'objectstore_tls')
        self.assertEqual((MONITORING / 'objectstore-tls-ca.crt').read_bytes(),
                         (ROOT / 'ansible/roles/openbao_agent/files/ailab-root-ca.crt').read_bytes())

    def test_exporter_has_no_identity_secrets_or_mutable_root(self):
        pod = self.kind('Deployment')['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertNotIn('serviceAccountName', pod)
        self.assertEqual(len(pod['containers']), 1)
        container = pod['containers'][0]
        self.assertRegex(container['image'], r'@sha256:[a-f0-9]{64}$')
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
        self.assertFalse(container['securityContext']['allowPrivilegeEscalation'])
        self.assertEqual(container['securityContext']['capabilities'], {'drop': ['ALL']})
        for volume in pod['volumes']:
            self.assertEqual(set(volume), {'name', 'configMap'})
        for mount in container['volumeMounts']:
            self.assertTrue(mount['readOnly'])
        self.assertNotIn('env', container)
        self.assertNotIn('envFrom', container)

    def test_network_allows_only_prometheus_dns_and_the_exact_provider(self):
        policy = self.kind('NetworkPolicy')['spec']
        self.assertEqual(set(policy['policyTypes']), {'Ingress', 'Egress'})
        self.assertEqual(policy['ingress'], [{
            'from': [{'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'prometheus'}}}],
            'ports': [{'protocol': 'TCP', 'port': 9115}],
        }])
        self.assertEqual(policy['egress'], [
            {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                     'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
             'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]},
            {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'strive-ailab'}},
                     'podSelector': {'matchLabels': {'strive.io/service': 'objectstore'}}}],
             'ports': [{'protocol': 'TCP', 'port': 8443}]},
        ])

    def test_monitor_and_rules_are_reconciled_and_alerts_use_existing_receiver(self):
        kustomization = yaml.safe_load((MONITORING / 'kustomization.yaml').read_text())
        resources = kustomization['resources']
        for name in ['objectstore-tls-probe.yaml', 'objectstore-tls-rules.yaml']:
            self.assertIn(name, resources)
        ca = next(v for v in self.kind('Deployment')['spec']['template']['spec']['volumes']
                  if v['name'] == 'ca')
        self.assertIn({
            'name': ca['configMap']['name'], 'namespace': 'monitoring',
            'files': ['ca.crt=objectstore-tls-ca.crt'],
        }, kustomization.get('configMapGenerator', []))
        config = next(v for v in self.kind('Deployment')['spec']['template']['spec']['volumes']
                      if v['name'] == 'config')
        self.assertIn({
            'name': config['configMap']['name'], 'namespace': 'monitoring',
            'files': ['blackbox.yml=objectstore-blackbox.yaml'],
        }, kustomization.get('configMapGenerator', []))
        self.assertFalse(kustomization.get('generatorOptions', {}).get('disableNameSuffixHash', False))
        for generator in kustomization.get('configMapGenerator', []):
            self.assertFalse(generator.get('options', {}).get('disableNameSuffixHash', False))
        rules = yaml.safe_load((MONITORING / 'objectstore-tls-rules.yaml').read_text())
        self.assertEqual(rules['metadata']['labels']['release'], 'kube-prometheus-stack')
        for rule in rules['spec']['groups'][0]['rules']:
            self.assertIn('kube_service_info', rule['expr'], 'pre-activation alerts must stay quiet')
            self.assertEqual(rule['labels']['namespace'], 'strive-ailab')
        values = yaml.safe_load((MONITORING / 'kube-prometheus-stack.yaml').read_text())['spec']['values']
        self.assertEqual(values['alertmanager']['config']['route']['receiver'], 'ntfy')


if __name__ == '__main__':
    unittest.main()
