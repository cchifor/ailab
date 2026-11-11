"""Offline rendering contracts for the inactive provenance source."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
RENDER = ROOT / 'kubernetes/optional/relay-provenance-journal/render.py'
COMMIT = 'a' * 40
HASH = 'b' * 64


class RendererTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def file(self, name, data):
        path = self.dir / name
        path.write_text(data)
        return {'path': str(path), 'sha256': hashlib.sha256(data.encode()).hexdigest()}

    def render(self, mode, config):
        pin = self.file('input.json', json.dumps(config))
        return subprocess.run(['python3', '-B', str(RENDER), mode, pin['path'], pin['sha256']],
                              text=True, capture_output=True, check=False)

    def service_config(self):
        public = {key: self.file(key + '.txt', '{}') for key in ('policy', 'authorization', 'witness')}
        public['databaseCa'] = self.file('db-ca.pem', 'public ca')
        public['tlsCertificate'] = self.file('tls.crt', 'public certificate')
        path = '/run/relay-provenance/'
        config = {'version': 1, 'policy': {'path': path + 'policy.json', 'sha256': public['policy']['sha256']},
                  'authority': {'path': path + 'authorization.json', 'sha256': public['authorization']['sha256']},
                  'witness': {'path': path + 'witness.json', 'sha256': public['witness']['sha256']},
                  'pins': {'origin': 'https://relay-provenance-journal.relay-provenance-journal.svc:8443',
                           'caSha256': HASH, 'replyPublicKeyHex': HASH},
                  'replyPrivateKeyFile': path + 'reply-key.pem',
                  'databaseCa': {'path': path + 'db-ca.pem', 'sha256': public['databaseCa']['sha256']},
                  'maxClockSkewMs': 1000,
                  'sources': [{'sourceId': '00000000-0000-4000-8000-000000000001',
                               'readConnectionStringFile': path + 'source-0-read-dsn'},
                              {'sourceId': '00000000-0000-4000-8000-000000000002',
                               'readConnectionStringFile': path + 'source-1-read-dsn',
                               'transitionConnectionStringFile': path + 'source-1-transition-dsn'}],
                  'tls': {'certificate': {'path': path + 'tls.crt', 'sha256': public['tlsCertificate']['sha256']},
                          'privateKeyFile': path + 'tls-key.pem'},
                  'listen': {'host': '0.0.0.0', 'port': 8443}}
        public['config'] = self.file('config.json', json.dumps(config))
        return {'version': 1, 'relaySourceCommit': COMMIT,
                'relayImage': 'registry.example/relay@sha256:' + HASH,
                **public, 'clients': [{'namespace': 'strive-ailab', 'podLabels': {'app': 'relay'}}]}

    def test_suspended_initialization_and_preflight_pins(self):
        config = {'version': 1, 'relaySourceCommit': COMMIT}
        for n in range(1, 6):
            config['schema' + str(n).zfill(3)] = self.file(str(n) + '.sql', 'BEGIN;\nCOMMIT;\n')
        output = self.render('initialize', config)
        self.assertEqual(output.returncode, 0, output.stderr)
        resource = json.loads(output.stdout)
        self.assertEqual((resource['apiVersion'], resource['kind']), ('v1', 'List'))
        objects = resource['items']
        job = objects[-1]
        self.assertTrue(job['spec']['suspend'])
        self.assertEqual(job['spec']['template']['spec']['containers'][0]['env'][-1]['name'], 'SCHEMA_005_SHA256')
        config['schema005']['sha256'] = HASH
        self.assertEqual(self.render('initialize', config).returncode, 78)

    def test_listener_remains_inactive_and_uses_regular_file_mounts(self):
        config = self.service_config()
        output = self.render('service', config)
        self.assertEqual(output.returncode, 0, output.stderr)
        resource = json.loads(output.stdout)
        self.assertEqual((resource['apiVersion'], resource['kind']), ('v1', 'List'))
        objects = resource['items']
        deploy = objects[2]
        self.assertEqual(deploy['spec']['replicas'], 0)
        mounts = deploy['spec']['template']['spec']['containers'][0]['volumeMounts']
        self.assertTrue(all('subPath' in mount and mount['readOnly'] for mount in mounts))
        self.assertEqual(len(objects[3]['spec']['ingress'][0]['from']), 1)
        config['clients'][0]['podLabels'] = {}
        self.assertEqual(self.render('service', config).returncode, 78)


if __name__ == '__main__':
    unittest.main()
