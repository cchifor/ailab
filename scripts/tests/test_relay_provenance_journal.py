"""Offline rendering contracts for the inactive provenance source."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml


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
        for n in range(1, 10):
            config['schema' + str(n).zfill(3)] = self.file(str(n) + '.sql', 'BEGIN;\nCOMMIT;\n')
        output = self.render('initialize', config)
        self.assertEqual(output.returncode, 0, output.stderr)
        resource = json.loads(output.stdout)
        self.assertEqual((resource['apiVersion'], resource['kind']), ('v1', 'List'))
        objects = resource['items']
        job = objects[-1]
        self.assertTrue(job['spec']['suspend'])
        self.assertEqual(job['spec']['template']['spec']['containers'][0]['env'][-1]['name'], 'SCHEMA_009_SHA256')
        self.assertEqual(set(objects[0]['data']), {f'{n:03d}.sql' for n in range(1, 10)})
        config['schema008']['sha256'] = HASH
        self.assertEqual(self.render('initialize', config).returncode, 78)

    def test_upgrade_pins_only_missing_migrations_and_stays_suspended(self):
        for previous in (5, 6, 7):
            with self.subTest(previous=previous):
                config = {'version': 1, 'relaySourceCommit': COMMIT, 'fromVersion': previous}
                for n in range(previous + 1, 9):
                    config[f'schema{n:03d}'] = self.file(f'{n:03d}.sql', 'BEGIN;\nCOMMIT;\n')
                output = self.render('upgrade', config)
                self.assertEqual(output.returncode, 0, output.stderr)
                schema, script, job = json.loads(output.stdout)['items']
                self.assertEqual(set(schema['data']), {f'{n:03d}.sql' for n in range(previous + 1, 9)})
                self.assertEqual(set(script['data']), {'upgrade.sh'})
                self.assertTrue(job['spec']['suspend'])
                self.assertEqual(job['spec']['backoffLimit'], 0)
                pod = job['spec']['template']['spec']
                self.assertFalse(pod['automountServiceAccountToken'])
                self.assertEqual(pod['restartPolicy'], 'Never')
                self.assertNotIn('fsGroup', pod['securityContext'])
                self.assertNotIn('fsGroupChangePolicy', pod['securityContext'])
                self.assertEqual(pod['securityContext']['runAsUser'], 70)
                self.assertEqual(pod['tolerations'], [{'key': 'dedicated', 'operator': 'Equal',
                                                      'value': 'agent', 'effect': 'NoSchedule'}])
                self.assertEqual(job['spec']['template']['metadata']['labels']['app'], 'relay-provenance-journal-upgrade')
                self.assertEqual(pod['containers'][0]['command'], ['/bin/sh', '/initialize/upgrade.sh'])
                self.assertEqual(pod['containers'][0]['env'][-1], {'name': 'FROM_SCHEMA_VERSION', 'value': str(previous)})
                config['schema008']['sha256'] = HASH
                self.assertEqual(self.render('upgrade', config).returncode, 78)

    def test_upgrade_refuses_missing_extra_or_unsupported_inputs(self):
        base = {'version': 1, 'relaySourceCommit': COMMIT, 'fromVersion': 7,
                'schema008': self.file('008.sql', 'BEGIN;\nCOMMIT;\n')}
        for previous in (True, '7', 4, 8, None):
            self.assertEqual(self.render('upgrade', {**base, 'fromVersion': previous}).returncode, 78)
        self.assertEqual(self.render('upgrade', {**base, 'fromVersion': 5}).returncode, 78)
        self.assertEqual(self.render('upgrade', {**base, 'schema006': base['schema008']}).returncode, 78)
        multiple = self.file('multiple.sql', 'BEGIN;\nCOMMIT;\nBEGIN;\nCOMMIT;\n')
        self.assertEqual(self.render('upgrade', {**base, 'schema008': multiple}).returncode, 78)

    def test_schema9_upgrade_is_separate_one_shot_and_suspended(self):
        config = {'version': 1, 'relaySourceCommit': COMMIT, 'fromVersion': 8,
                  'schema009': self.file('009.sql', 'BEGIN;\nCOMMIT;\n')}
        output = self.render('upgrade9', config)
        self.assertEqual(output.returncode, 0, output.stderr)
        schema, script, job = json.loads(output.stdout)['items']
        self.assertEqual(set(schema['data']), {'009.sql'})
        self.assertEqual(set(script['data']), {'upgrade9.sh'})
        self.assertEqual(job['metadata']['name'], 'relay-provenance-journal-upgrade-8-to-9')
        self.assertTrue(job['spec']['suspend'])
        self.assertEqual(job['spec']['backoffLimit'], 0)
        pod = job['spec']['template']['spec']
        self.assertEqual(pod['containers'][0]['command'], ['/bin/sh', '/initialize/upgrade9.sh'])
        self.assertEqual(pod['containers'][0]['env'][-1], {'name': 'FROM_SCHEMA_VERSION', 'value': '8'})
        self.assertNotIn('fsGroup', pod['securityContext'])
        self.assertNotIn('fsGroupChangePolicy', pod['securityContext'])
        for previous in (True, '8', 5, 6, 7, 9, None):
            self.assertEqual(self.render('upgrade9', {**config, 'fromVersion': previous}).returncode, 78)
        self.assertEqual(self.render('upgrade9', {**config, 'schema008': config['schema009']}).returncode, 78)
        malformed = self.file('009-malformed.sql', 'BEGIN;\nCOMMIT;\nBEGIN;\nCOMMIT;\n')
        self.assertEqual(self.render('upgrade9', {**config, 'schema009': malformed}).returncode, 78)

    def test_runtime_refuses_old_schema_or_pending_upgrade_before_postgres(self):
        data = self.dir / 'pgdata'
        data.mkdir()
        (data / 'PG_VERSION').write_text('17\n')
        schema = data / 'relay-provenance-journal-schema-version'
        pending = data / 'relay-provenance-journal-upgrade-pending'
        marker = self.dir / 'postgres-started'
        postgres = self.dir / 'postgres'
        postgres.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
        postgres.chmod(0o755)
        stat = self.dir / 'stat'
        stat.write_text('#!/bin/sh\ncase "$2" in %u) echo 70;; %a) echo 700;; *) exit 1;; esac\n')
        stat.chmod(0o755)
        env = dict(os.environ, PGDATA=str(data), PATH=str(self.dir) + ':/usr/bin:/bin')
        script = ROOT / 'kubernetes/optional/relay-provenance-journal/start.sh'
        def run():
            return subprocess.run(['sh', str(script)], env=env, text=True, capture_output=True).returncode
        for previous in (5, 6, 7, 8):
            schema.write_text(str(previous) + '\n')
            self.assertEqual(run(), 78)
            self.assertFalse(marker.exists())
        schema.write_text('9\n')
        pending.write_text('from=8 to=9\n')
        self.assertEqual(run(), 78)
        self.assertFalse(marker.exists())
        pending.unlink()
        pending.symlink_to(self.dir / 'missing')
        self.assertEqual(run(), 78)
        self.assertFalse(marker.exists())
        pending.unlink()
        self.assertEqual(run(), 0)
        self.assertTrue(marker.exists())

    def test_upgrade_validates_every_pin_before_hashing_or_data_changes(self):
        marker = self.dir / 'hash-called'
        sha = self.dir / 'sha256sum'
        sha.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 99\n')
        sha.chmod(0o755)
        env = dict(os.environ, PGDATA='/var/lib/postgresql/data/pgdata', FROM_SCHEMA_VERSION='5',
                   SCHEMA_006_SHA256=HASH, SCHEMA_007_SHA256=HASH, SCHEMA_008_SHA256=HASH,
                   PATH=str(self.dir) + ':/usr/bin:/bin')
        script = ROOT / 'kubernetes/optional/relay-provenance-journal/initialize/upgrade.sh'
        for invalid in ('SCHEMA_006_SHA256', 'SCHEMA_007_SHA256', 'SCHEMA_008_SHA256'):
            with self.subTest(invalid=invalid):
                result = subprocess.run(['sh', str(script)], env={**env, invalid: 'invalid'}, text=True, capture_output=True)
                self.assertEqual(result.returncode, 78)
                self.assertFalse(marker.exists())

    def test_schema9_upgrade_validates_009_pin_before_touching_retained_data(self):
        marker = self.dir / 'hash-called-009'
        sha = self.dir / 'sha256sum-009'
        sha.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 99\n')
        sha.chmod(0o755)
        env = dict(os.environ, PGDATA='/var/lib/postgresql/data/pgdata', FROM_SCHEMA_VERSION='8',
                   SCHEMA_009_SHA256=HASH, PATH=str(self.dir) + ':/usr/bin:/bin')
        script = ROOT / 'kubernetes/optional/relay-provenance-journal/initialize/upgrade9.sh'
        result = subprocess.run(['sh', str(script)], env={**env, 'SCHEMA_009_SHA256': 'invalid'}, text=True, capture_output=True)
        self.assertEqual(result.returncode, 78)
        self.assertFalse(marker.exists())

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

    def test_presence_source_requires_pins_and_observer_secret(self):
        config = {'version': 1, 'relaySourceCommit': COMMIT,
                  'relayImage': 'registry.example/relay@sha256:' + HASH,
                  'tlsCertificate': self.file('presence-tls.crt', 'public leaf'),
                  'clientCa': self.file('client-ca.pem', 'public ca'),
                  'operatorRootHex': HASH, 'clientCertSha256': HASH,
                  'clients': [{'namespace': 'strive-ailab', 'podLabels': {'app': 'relay-supervisor'}}]}
        output = self.render('presence', config)
        self.assertEqual(output.returncode, 0, output.stderr)
        items = json.loads(output.stdout)['items']
        deploy = items[2]
        self.assertEqual(deploy['spec']['replicas'], 0)
        pod = deploy['spec']['template']['spec']
        self.assertEqual(pod['containers'][0]['command'][-1], '/app/dist/scripts/serve-runtime-presence-source.js')
        self.assertEqual(pod['volumes'][1]['secret']['items'], [{'key': 'observer-dsn', 'path': 'observer-dsn'}])
        self.assertTrue(all('subPath' in m for m in pod['containers'][0]['volumeMounts']))
        config['clientCa'] = self.file('new-client-ca.pem', 'new public ca')
        rotated = json.loads(self.render('presence', config).stdout)['items']
        self.assertNotEqual(items[0]['metadata']['name'], rotated[0]['metadata']['name'])
        config['clientCertSha256'] = '0' * 64
        self.assertEqual(self.render('presence', config).returncode, 78)

    def test_database_ingress_admits_both_source_listeners(self):
        raw = (ROOT / 'kubernetes/optional/relay-provenance-journal/network-policy.yaml').read_text()
        policies = list(yaml.safe_load_all(raw))
        database = next(p for p in policies if p['metadata']['name'] == 'postgres-clients')
        peers = database['spec']['ingress'][0]['from']
        self.assertEqual({p['podSelector']['matchLabels']['app'] for p in peers},
                         {'relay-provenance-journal', 'relay-runtime-presence-source'})
        self.assertTrue(all(p['namespaceSelector']['matchLabels']['kubernetes.io/metadata.name'] ==
                            'relay-provenance-journal' for p in peers))

    def test_initializer_refuses_missing_pin_before_hash_command(self):
        marker = self.dir / 'hash-called'
        for tool in ('sha256sum', 'mkdir'):
            executable = self.dir / tool
            executable.write_text('#!/bin/sh\nprintf called > "' + str(marker) + '"\nexit 99\n')
            executable.chmod(0o755)
        env = dict(os.environ, PGDATA='/var/lib/postgresql/data/pgdata',
                   SCHEMA_001_SHA256='a' * 64, SCHEMA_002_SHA256='',
                   PATH=str(self.dir) + ':/usr/bin:/bin')
        script = ROOT / 'kubernetes/optional/relay-provenance-journal/initialize/initialize.sh'
        result = subprocess.run(['sh', str(script)], env=env, text=True,
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 78)
        self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
