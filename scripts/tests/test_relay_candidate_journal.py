"""Inactive renderer/initializer contracts; these tests never call Kubernetes."""
from pathlib import Path
import hashlib
import base64
import subprocess
import sys
import importlib.util
import json
import os
import re
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / 'kubernetes/optional/relay-candidate-journal'
spec = importlib.util.spec_from_file_location('candidate_journal_render', BUNDLE / 'render.py')
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)


class JournalBundle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def pin(self, name, data):
        raw = data if isinstance(data, str) else json.dumps(data)
        path = self.directory / name
        path.write_text(raw)
        path.chmod(0o600)
        return {'path': str(path), 'sha256': hashlib.sha256(raw.encode()).hexdigest()}

    def fixture(self):
        image = 'registry.example/relay@sha256:' + 'a' * 64
        ledger = {'origin': render.ORIGIN, 'caSha256': 'b' * 64, 'replyPublicKey': 'c' * 43,
                  'databaseIdentity': 'database', 'gateId': 'gate', 'witnessSha256': 'd' * 64}
        run = {'kind': 'relay.candidate-run', 'payload': {'hub': {'image': image}, 'ledger': ledger,
               'externalEpoch': 'epoch', 'namespaces': [{'name': 'candidate-control', 'purpose': 'control'},
                                                      {'name': 'candidate-execution', 'purpose': 'execution'}]}}
        witness = {k: ledger[k] for k in ['databaseIdentity', 'gateId', 'witnessSha256']}
        witness['externalEpoch'] = 'epoch'
        config = {'version': 1, 'relaySourceCommit': 'e' * 40, 'relayImage': image,
                  'run': self.pin('run.json', run), 'witness': self.pin('witness.json', witness),
                  'databaseCa': self.pin('ca.pem', 'synthetic public CA'),
                  'tlsCertificate': self.pin('certificate.pem', 'synthetic public certificate')}
        P = render.PUBLIC
        service_config = {'version': 1, 'run': {'path': P + 'run.json', 'sha256': config['run']['sha256']},
            'witness': {'path': P + 'witness.json', 'sha256': config['witness']['sha256']},
            'trustedIssuerKeys': {'fixture': 'f' * 43},
            'journal': {k: ledger[k] for k in ['origin', 'caSha256', 'replyPublicKey']},
            'replyPrivateKeyFile': P + 'reply-key.pem', 'listen': {'host': '0.0.0.0', 'port': 8443},
            'tls': {'certificate': {'path': P + 'tls.crt', 'sha256': config['tlsCertificate']['sha256']}, 'privateKeyFile': P + 'tls-key.pem'},
            'database': {'ca': {'path': P + 'db-ca.pem', 'sha256': config['databaseCa']['sha256']},
                         'agentConnectionStringFile': P + 'agent-dsn', 'holderConnectionStringFile': P + 'holder-dsn'}}
        config['config'] = self.pin('config.json', service_config)
        return config, service_config, run

    def test_inactive_service_root_only_contract_and_no_operator_credential(self):
        config, _, _ = self.fixture()
        objects = render.service(config)
        deployment = next(o for o in objects if o['kind'] == 'Deployment')
        self.assertEqual(deployment['spec']['replicas'], 0)
        pod = deployment['spec']['template']['spec']
        self.assertEqual(pod['securityContext']['runAsUser'], 0)
        self.assertNotIn('fsGroup', pod['securityContext'])
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(pod['affinity'], render.AFFINITY)
        container = pod['containers'][0]
        self.assertEqual(container['securityContext']['capabilities'], {'drop': ['ALL']})
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
        self.assertNotIn('env', container)
        self.assertTrue(all(m['readOnly'] and m.get('subPath') for m in container['volumeMounts']))
        secret = next(v for v in pod['volumes'] if v['name'] == 'runtime')['secret']
        self.assertEqual(secret['defaultMode'], 0o400)
        self.assertEqual([v['key'] for v in secret['items']], ['reply-key.pem', 'agent-dsn', 'holder-dsn'])
        self.assertNotIn('operator-dsn', json.dumps(pod))
        self.assertNotIn('operator_login', json.dumps(pod))
        policy = next(o for o in objects if o['kind'] == 'NetworkPolicy')
        peers = policy['spec']['ingress'][0]['from']
        self.assertTrue(all('namespaceSelector' in p and 'podSelector' in p for p in peers))
        cm = next(o for o in objects if o['kind'] == 'ConfigMap')
        self.assertTrue(cm['immutable'])
        self.assertEqual(cm['data']['config.json'], Path(config['config']['path']).read_text())

    def test_service_refuses_unpinned_image_source_and_mismatched_fixed_paths(self):
        for mutation in ['image', 'source', 'config-path', 'run-image', 'witness', 'namespace', 'extra']:
            with self.subTest(mutation=mutation):
                config, service, run = self.fixture()
                if mutation == 'image': config['relayImage'] = 'registry.example/relay:v0.9.1'
                if mutation == 'source': config['relaySourceCommit'] = '0' * 40
                if mutation == 'config-path':
                    service['database']['agentConnectionStringFile'] = '/etc/operator-dsn'
                    config['config'] = self.pin('config.json', service)
                if mutation == 'run-image':
                    run['payload']['hub']['image'] = 'registry.example/old@sha256:' + 'f' * 64
                    config['run'] = self.pin('run.json', run)
                if mutation == 'witness':
                    service['witness']['sha256'] = 'f' * 64
                    config['config'] = self.pin('config.json', service)
                if mutation == 'namespace':
                    run['payload']['namespaces'][0]['name'] = 'kube-system'
                    config['run'] = self.pin('run.json', run)
                if mutation == 'extra': config['operatorPassword'] = 'synthetic-not-accepted'
                with self.assertRaises((ValueError, KeyError)): render.service(config)

    def test_pinned_file_rejects_symlink_hardlink_writable_and_wrong_hash(self):
        for mutation in ['symlink', 'hardlink', 'writable', 'hash']:
            with self.subTest(mutation=mutation):
                pin = self.pin('input-' + mutation, '{}')
                path = Path(pin['path'])
                if mutation == 'symlink':
                    other = self.directory / 'symbolic'; other.symlink_to(path); pin['path'] = str(other)
                if mutation == 'hardlink': os.link(path, self.directory / 'hard')
                if mutation == 'writable': path.chmod(0o666)
                if mutation == 'hash': pin['sha256'] = 'f' * 64
                with self.assertRaises((ValueError, OSError)): render.pinned(pin)

    def test_init_requires_both_pins_and_suspends_job(self):
        config = {'version': 1, 'relaySourceCommit': 'e' * 40,
                  'schema001': self.pin('001.sql', 'BEGIN;\nCOMMIT;\n'),
                  'schema002': self.pin('002.sql', 'BEGIN;\nCOMMIT;\n')}
        job = render.initialize(config)[-1]
        self.assertEqual(job['kind'], 'Job')
        self.assertTrue(job['spec']['suspend'])
        self.assertEqual(job['spec']['backoffLimit'], 0)
        env = job['spec']['template']['spec']['containers'][0]['env']
        self.assertEqual(env[1]['value'], config['schema001']['sha256'])
        config['schema002']['sha256'] = 'f' * 64
        with self.assertRaises(ValueError): render.initialize(config)

    def test_source_wrappers_verify_pins_before_pgdata_and_stop_before_marker(self):
        script = (BUNDLE / 'initialize/initialize.sh').read_text()
        self.assertLess(script.index('sha256sum -c'), script.index('mkdir "$data_root/.initialization-started"'))
        self.assertLess(script.index('stop\ntrap - EXIT'), script.index("printf '2\\n'"))
        self.assertNotIn('INSERT INTO worker_candidate.', script)
        self.assertNotIn('PASSWORD ', script)
        start = (BUNDLE / 'start.sh').read_text()
        self.assertNotIn('initdb', start)
        self.assertIn('!= 2', start)
        allowed = re.search(r'case "\$mode" in ([0-7|]+)\)', start).group(1).split('|')
        self.assertEqual({int(mode, 8) for mode in allowed}, {0o700, 0o750, 0o2700, 0o2750})
        self.assertTrue(all(int(mode, 8) & 0o027 == 0 for mode in allowed))

    def test_base_is_optional_off_retained_and_not_on_worker_pool(self):
        objects = list(yaml.safe_load_all((BUNDLE / 'postgres.yaml').read_text()))
        db = next(o for o in objects if o['kind'] == 'StatefulSet')
        self.assertEqual(db['spec']['replicas'], 0)
        self.assertEqual(db['spec']['template']['spec']['affinity'], render.AFFINITY)
        namespaces = list(yaml.safe_load_all((BUNDLE / 'namespace.yaml').read_text()))
        self.assertEqual({n['metadata']['name']: n['metadata']['labels']['pod-security.kubernetes.io/enforce'] for n in namespaces},
                         {render.DB_NS: 'restricted', render.SERVICE_NS: 'baseline'})
        claim = yaml.safe_load((BUNDLE / 'storage.yaml').read_text())
        self.assertEqual(claim['metadata']['annotations']['kustomize.toolkit.fluxcd.io/prune'], 'disabled')
        for path in (ROOT / 'kubernetes').rglob('kustomization.yaml'):
            if BUNDLE not in path.parents:
                self.assertNotIn('optional/relay-candidate-journal', path.read_text())


class TlsLifecycle(unittest.TestCase):
    setUp = JournalBundle.setUp
    pin = JournalBundle.pin
    fixture = JournalBundle.fixture
    def pair(self):
        key = subprocess.run(['openssl', 'genpkey', '-algorithm', 'ED25519'], capture_output=True, check=True, timeout=5).stdout
        certificate = subprocess.run(['openssl', 'req', '-new', '-x509', '-key', '/dev/stdin',
            '-subj', '/CN=synthetic-journal-fixture', '-days', '1'], input=key, capture_output=True, check=True, timeout=5).stdout
        return certificate, key

    def snapshot_module(self):
        spec = importlib.util.spec_from_file_location('snapshot_tls', BUNDLE / 'snapshot-tls.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def secret(self, cert, key):
        return {'kind':'Secret','type':'kubernetes.io/tls','metadata':{'name':render.NAME+'-tls','namespace':render.SERVICE_NS},
                'data':{'tls.crt':base64.b64encode(cert).decode(),'tls.key':base64.b64encode(key).decode()}}

    def test_recreation_keeps_reviewed_pair_when_cert_manager_rotates(self):
        module = self.snapshot_module()
        cert, key = self.pair()
        pin = hashlib.sha256(cert).hexdigest()
        snapshot = module.snapshot(self.secret(cert, key), pin)
        self.assertTrue(snapshot['immutable'])
        config, service, _ = self.fixture()
        config['tlsCertificate'] = self.pin('certificate.pem', cert.decode())
        service['tls']['certificate']['sha256'] = pin
        config['config'] = self.pin('config.json', service)
        def referenced_secret():
            deployment = next(o for o in render.service(config) if o['kind']=='Deployment')
            return next(v for v in deployment['spec']['template']['spec']['volumes'] if v['name']=='tls')['secret']['secretName']
        self.assertEqual(referenced_secret(), snapshot['metadata']['name'])
        new_cert, new_key = self.pair()
        renewed = self.secret(new_cert, new_key)
        with self.assertRaisesRegex(ValueError, 'certificate-pin-mismatch'):
            module.snapshot(renewed, pin)
        self.assertEqual(referenced_secret(), snapshot['metadata']['name'])
        self.assertNotEqual(referenced_secret(), renewed['metadata']['name'])
        with self.assertRaisesRegex(ValueError, 'certificate-key-pair-mismatch'):
            module.snapshot(self.secret(cert, new_key), pin)

    def test_refusal_identifies_hash_error_without_echoing_input(self):
        raw = self.pin('diagnostic.json', {'sensitiveByMistake':'synthetic-do-not-echo'})
        result = subprocess.run([sys.executable, '-B', str(BUNDLE/'render.py'), 'service', raw['path'], 'f'*64],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode,1)
        self.assertIn('file-sha256-mismatch',result.stderr)
        self.assertNotIn('synthetic-do-not-echo',result.stderr)
        self.assertEqual(result.stdout,'')


if __name__ == '__main__':
    unittest.main()
