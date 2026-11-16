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
# The activation overlay and its Flux Kustomization (runbook, "Activation under Flux").
OVERLAY = ROOT / 'kubernetes/apps/relay-candidate-journal'
CLUSTER = ROOT / 'kubernetes/apps/clusters/ai'
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
               'externalEpoch': 'epoch', 'namespaces': [{'name': 'relay-infra-control', 'purpose': 'control'},
                                                      {'name': 'relay-researcher-1', 'purpose': 'execution'}]}}
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

    def test_service_address_is_pinned_for_dns_less_worker_slots(self):
        # Relay's candidate supervisor dials ORIGIN from the worker Pod, which resolves its host through
        # the Pod's measured hostAliases (candidateJournal) to this fixed address: no DNS in the slot.
        config, _, _ = self.fixture()
        svc = next(o for o in render.service(config) if o['kind'] == 'Service' and o['metadata']['name'] == render.NAME)
        self.assertEqual(render.CLUSTER_IP, '10.96.0.42')
        self.assertEqual(svc['spec']['clusterIP'], render.CLUSTER_IP)
        self.assertEqual(svc['spec']['clusterIPs'], [render.CLUSTER_IP])
        self.assertEqual((svc['spec']['ipFamilyPolicy'], svc['spec']['ipFamilies']), ('SingleStack', ['IPv4']))
        self.assertEqual(svc['spec']['ports'], [{'name': 'https', 'port': 8443, 'targetPort': 'https'}])
        self.assertEqual(render.ORIGIN, 'https://relay-candidate-journal.relay-candidate-journal.svc:8443')
        plan = (Path(__file__).resolve().parents[2] / 'docs/network-plan.md').read_text(encoding='utf-8')
        self.assertIn('`' + render.CLUSTER_IP + '`', plan)

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

    def test_protocol_listener_requires_integer_port(self):
        config, service, _ = self.fixture()
        service['listen']['port'] = 8443.0
        config['config'] = self.pin('config.json', service)
        with self.assertRaisesRegex(render.Refusal, '^public-binding-mismatch$'):
            render.service(config)

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
        # Before activation no kustomization outside the bundle named it. The activation PR admits
        # exactly one, the overlay (runbook, "Activation under Flux"); Activation pins its content.
        referencing = sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / 'kubernetes').rglob('kustomization.yaml')
                             if BUNDLE not in path.parents and 'optional/relay-candidate-journal' in path.read_text())
        self.assertEqual(referencing, [(OVERLAY / 'kustomization.yaml').relative_to(ROOT).as_posix()])


def yaml_docs(path):
    return [d for d in yaml.safe_load_all(path.read_text(encoding='utf-8')) if d]


def flux_kustomizations():
    """Every Flux Kustomization under kubernetes/, by (file, name): .yaml, .yml and .json, inside Lists too."""
    found = {}
    for path in [*(ROOT / 'kubernetes').rglob('*.yaml'), *(ROOT / 'kubernetes').rglob('*.yml'),
                 *(ROOT / 'kubernetes').rglob('*.json')]:
        try:
            documents = yaml_docs(path)
        except yaml.YAMLError:
            continue
        while documents:
            d = documents.pop()
            if isinstance(d, dict) and d.get('kind') == 'List' and isinstance(d.get('items'), list):
                documents.extend(d['items'])
            elif (isinstance(d, dict) and d.get('kind') == 'Kustomization'
                    and str(d.get('apiVersion', '')).startswith('kustomize.toolkit.fluxcd.io/')):
                key = (path.relative_to(ROOT).as_posix(), d['metadata']['name'])
                assert key not in found, key
                found[key] = d
    return found


def inside(target, directory):
    return target == directory or directory in target.parents


class Activation(unittest.TestCase):
    """The activation PR: one Flux Kustomization over one overlay, which is the reviewed base, the
    database replica count (0, then 1 after the one-time initialization) and the protocol Service on
    10.96.0.42. The initialization Job and every per-run render stay outside every Flux inventory."""
    setUp = JournalBundle.setUp
    pin = JournalBundle.pin
    fixture = JournalBundle.fixture

    def test_flux_kustomization_encodes_the_runbook_preconditions(self):
        flux = flux_kustomizations()
        mine = {key: d for key, d in flux.items() if key[1] == 'relay-candidate-journal'}
        self.assertEqual(list(mine), [('kubernetes/apps/clusters/ai/relay-candidate-journal.yaml', 'relay-candidate-journal')])
        ks = next(iter(mine.values()))
        self.assertEqual(ks['metadata']['namespace'], 'flux-system')
        spec = ks['spec']
        self.assertEqual(spec['path'], './' + OVERLAY.relative_to(ROOT).as_posix())
        self.assertEqual(spec['sourceRef'], {'kind': 'GitRepository', 'name': 'flux-system'})
        self.assertIs(spec['wait'], True, 'the PVC and both Certificates must gate readiness')
        self.assertIs(spec['prune'], True)
        # Exactly these fields: no suspend, decryption, patches, components, postBuild, targetNamespace,
        # namePrefix/nameSuffix, commonMetadata or force can rename, relabel or reshape what the tests
        # below check in source (a prefix would move 10.96.0.42 to another Service name).
        self.assertEqual(set(spec), {'interval', 'retryInterval', 'timeout', 'dependsOn', 'sourceRef', 'path', 'prune', 'wait'})
        deps = {d['name'] for d in spec['dependsOn']}
        # The StorageClass qnap-iscsi and the ClusterIssuer ailab-ca come first.
        self.assertTrue({'qnap-storage', 'cert-manager-config'} <= deps, deps)
        # wait:true needs the PVC Bound while the database is at zero replicas: no Pod consumes the claim,
        # so the StorageClass must bind Immediately (no WaitForFirstConsumer).
        (storage_class,) = yaml_docs(ROOT / 'kubernetes/apps/qnap-storage/storageclass.yaml')
        self.assertEqual((storage_class['metadata']['name'], storage_class['reclaimPolicy']), ('qnap-iscsi', 'Retain'))
        self.assertEqual(storage_class.get('volumeBindingMode', 'Immediate'), 'Immediate')
        # Every dependency is a defined Flux Kustomization: a typo would wait on `dependency not found`.
        cluster = {name for (path, name) in flux if path.startswith(CLUSTER.relative_to(ROOT).as_posix() + '/')}
        self.assertTrue(deps <= cluster, deps)

    def test_only_the_overlay_reaches_the_bundle_and_nothing_reaches_the_overlay(self):
        # Flux paths, resolved: exactly ours names the overlay, none points into the bundle (its
        # initialize/ above all), and none is an ancestor that Flux would expand without a
        # kustomization.yaml (that generates one over every manifest below it, ours included).
        paths = {key: (ROOT / d['spec'].get('path', '')).resolve() for key, d in flux_kustomizations().items()}
        self.assertEqual([key for key, target in paths.items() if inside(target, OVERLAY)],
                         [('kubernetes/apps/clusters/ai/relay-candidate-journal.yaml', 'relay-candidate-journal')])
        self.assertEqual([key for key, target in paths.items() if inside(target, BUNDLE)], [])
        for key, target in paths.items():
            if OVERLAY.resolve() in target.parents or BUNDLE.resolve() in target.parents:
                continue
            if target in OVERLAY.resolve().parents or target in BUNDLE.resolve().parents:
                self.assertTrue((target / 'kustomization.yaml').is_file(), key)
        # Kustomize entries, resolved, from every kustomization under kubernetes/ (each edge, so any
        # transitive path too): only the overlay names the bundle, only its root directory, and
        # nothing names the overlay, which only its own Flux Kustomization reconciles.
        reached = []
        for path in [*(ROOT / 'kubernetes').rglob('kustomization.yaml'), *(ROOT / 'kubernetes').rglob('kustomization.yml'),
                     *(ROOT / 'kubernetes').rglob('Kustomization')]:
            here = path.parent.resolve()
            # yaml_docs: every document; `or []`: a key present with no entries parses as None.
            entries = [entry for k in yaml_docs(path)
                       for entry in (k.get('resources') or []) + (k.get('components') or []) + (k.get('bases') or [])]
            for entry in entries:
                target = (path.parent / entry).resolve()
                if (inside(target, BUNDLE) and not inside(here, BUNDLE)) or (inside(target, OVERLAY) and not inside(here, OVERLAY)):
                    reached.append((path.parent.relative_to(ROOT).as_posix(), target.relative_to(ROOT).as_posix()))
        self.assertEqual(reached, [(OVERLAY.relative_to(ROOT).as_posix(), BUNDLE.relative_to(ROOT).as_posix())])

    def test_flux_inventory_is_exactly_the_stopped_base_and_the_pinned_service(self):
        # Every object Flux can create here, from the base's own resource files, its one generator and
        # the overlay's Service: no Job, Deployment, Secret or workload beyond the StatefulSet at 0/1.
        base = yaml.safe_load((BUNDLE / 'kustomization.yaml').read_text())
        self.assertEqual(set(base), {'apiVersion', 'kind', 'resources', 'configMapGenerator', 'labels'})
        self.assertEqual(base['resources'], ['namespace.yaml', 'storage.yaml', 'certificate.yaml',
                                             'journal-certificate.yaml', 'postgres.yaml', 'network-policy.yaml'])
        self.assertEqual(base['configMapGenerator'], [{'name': render.NAME + '-postgres', 'namespace': render.DB_NS,
                                                       'files': ['start.sh', 'pg_hba.conf']}])
        self.assertEqual(base['labels'], [{'pairs': {'app.kubernetes.io/part-of': render.NAME, 'velero.io/exclude-from-backup': 'true'},
                                           'includeSelectors': False, 'includeTemplates': True}])
        # Retained state: removing the Flux file must never delete the namespaces or the claim. Checked on
        # the objects the base actually includes.
        retained = [d for entry in base['resources'] for d in yaml_docs(BUNDLE / entry)
                    if d['kind'] in ('Namespace', 'PersistentVolumeClaim')]
        self.assertEqual(len(retained), 3)
        for d in retained:
            self.assertEqual(d['metadata']['annotations']['kustomize.toolkit.fluxcd.io/prune'], 'disabled', d['metadata']['name'])
        inventory = sorted((d['kind'], d['metadata'].get('namespace', ''), d['metadata']['name'])
                           for entry in base['resources'] for d in yaml_docs(BUNDLE / entry))
        inventory += [(d['kind'], d['metadata']['namespace'], d['metadata']['name']) for d in yaml_docs(OVERLAY / 'service.yaml')]
        db, svc = render.DB_NS, render.SERVICE_NS
        self.assertEqual(sorted(inventory), sorted([
            ('Namespace', '', db), ('Namespace', '', svc),
            ('PersistentVolumeClaim', db, render.NAME + '-postgres-data'),
            ('Certificate', db, render.NAME + '-postgres-tls'), ('Certificate', svc, render.NAME + '-tls'),
            ('Service', db, render.NAME + '-postgres'), ('StatefulSet', db, render.NAME + '-postgres'),
            ('NetworkPolicy', db, 'default-deny'), ('NetworkPolicy', svc, 'default-deny'),
            ('NetworkPolicy', db, 'postgres-clients'), ('NetworkPolicy', db, 'operator-client'),
            ('NetworkPolicy', db, 'postgres-operator'),
            ('Service', svc, render.NAME)]))

    def test_overlay_is_the_base_the_replica_count_and_the_pinned_service(self):
        self.assertEqual(sorted(p.name for p in OVERLAY.iterdir()), ['kustomization.yaml', 'replicas.yaml', 'service.yaml'])
        k = yaml.safe_load((OVERLAY / 'kustomization.yaml').read_text())
        self.assertEqual(set(k), {'apiVersion', 'kind', 'resources', 'patches'})
        self.assertEqual(k['resources'], ['../../optional/relay-candidate-journal', 'service.yaml'])
        self.assertEqual(k['patches'], [{'path': 'replicas.yaml'}])
        (patch,) = yaml_docs(OVERLAY / 'replicas.yaml')
        self.assertEqual((patch['kind'], patch['metadata']), ('StatefulSet', {'name': render.NAME + '-postgres', 'namespace': render.DB_NS}))
        self.assertEqual(set(patch['spec']), {'replicas'})
        # Zero until the initialization Job is terminal and its Pod gone; then ONE, never more.
        self.assertIn(patch['spec']['replicas'], (0, 1))
        self.assertIs(type(patch['spec']['replicas']), int)
        (service,) = yaml_docs(OVERLAY / 'service.yaml')
        self.assertEqual(service['kind'], 'Service')

    def test_flux_service_is_exactly_the_rendered_pinned_service(self):
        # The per-run render applies the same object, so it never moves or changes the Service.
        config, _, _ = self.fixture()
        rendered = [o for o in render.service(config) if o['kind'] == 'Service']
        (service,) = yaml_docs(OVERLAY / 'service.yaml')
        self.assertEqual(rendered, [service])
        self.assertEqual(service['spec']['clusterIP'], '10.96.0.42')
        self.assertEqual(service['spec']['clusterIPs'], ['10.96.0.42'])
        self.assertEqual(service['spec']['ports'], [{'name': 'https', 'port': 8443, 'targetPort': 'https'}])
        plan = (ROOT / 'docs/network-plan.md').read_text(encoding='utf-8')
        row = re.search(r'^\| `10\.96\.0\.42` \| `relay-candidate-journal/relay-candidate-journal` \|([^|]*)\|', plan, re.M)
        self.assertIsNotNone(row)
        self.assertIn('`' + (OVERLAY / 'service.yaml').relative_to(ROOT).as_posix() + '`', row.group(1))


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


class MetricsComposition(unittest.TestCase):
    setUp = JournalBundle.setUp
    pin = JournalBundle.pin
    fixture = JournalBundle.fixture

    @classmethod
    def setUpClass(cls):
        # Public synthetic trust material; the generated private key never
        # enters the renderer, fixture files, test output or rendered objects.
        cls.certificate, _ = TlsLifecycle.pair(cls)

    def enable(self, config, service, run, port=None):
        ca = self.pin('metrics-ca.pem', self.certificate.decode())
        run['payload']['ledger']['caSha256'] = ca['sha256']
        config['run'] = self.pin('run.json', run)
        service['run']['sha256'] = config['run']['sha256']
        service['journal']['caSha256'] = ca['sha256']
        service['metrics'] = {'listen': {'host': '0.0.0.0', 'port': port or render.METRICS_PORT},
                              'tokenSha256': '7' * 64, 'expiresAt': '2099-01-01T00:00:00Z'}
        config['metrics'] = {'ca': ca, 'serverName': render.NAME + '.' + render.SERVICE_NS + '.svc',
                             'collectorSecret': {'name': render.METRICS_NAME + '-fixture', 'key': 'token'}}
        if port is not None:
            config['metrics']['port'] = port
        config['config'] = self.pin('config.json', service)

    def test_client_ingress_is_the_researcher_slot_and_its_agent_only(self):
        # relay#277 comment 123028, condition 2 (approved in 123063): the journal admits the researcher
        # slot's managed worker Pods and the infrastructure agent's control namespace, TCP 8443, nothing else.
        config, _, run = self.fixture()
        (policy,) = [o for o in render.service(config) if o['kind'] == 'NetworkPolicy']
        self.assertEqual(policy['metadata']['name'], 'journal-clients-and-database')
        self.assertEqual(policy['spec']['ingress'], [{'from': [
            {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'relay-infra-control'}},
             'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'relay-infrastructure-agent'}}},
            {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'relay-researcher-1'}},
             'podSelector': {'matchLabels': {'relay.infrastructure/managed': 'true'}}}],
            'ports': [{'protocol': 'TCP', 'port': 8443}]}])
        for control, execution in [('relay-infra-control', 'relay-researcher-2'), ('candidate-control', 'relay-researcher-1'),
                                   ('relay-researcher-1', 'relay-infra-control')]:
            with self.subTest(control=control, execution=execution):
                config, service, run = self.fixture()
                run['payload']['namespaces'] = [{'name': control, 'purpose': 'control'},
                                                {'name': execution, 'purpose': 'execution'}]
                config['run'] = self.pin('run.json', run)
                service['run']['sha256'] = config['run']['sha256']
                config['config'] = self.pin('config.json', service)
                with self.assertRaisesRegex(render.Refusal, '^client-namespaces-not-reviewed$'):
                    render.service(config)

    def test_default_off_has_no_metrics_resources_or_port(self):
        config, _, _ = self.fixture()
        objects = render.service(config)
        self.assertEqual([o['kind'] for o in objects], ['ConfigMap', 'Service', 'Deployment', 'NetworkPolicy'])
        self.assertTrue(all(o['metadata']['namespace'] == render.SERVICE_NS for o in objects))
        pod = objects[2]['spec']['template']['spec']
        self.assertEqual(pod['containers'][0]['ports'], [{'name': 'https', 'containerPort': 8443}])
        self.assertNotIn('metrics', objects[0]['data']['config.json'])

    def test_opt_in_preserves_protocol_and_isolates_collector_for_default_and_custom_ports(self):
        for port in [None, 10444]:
            with self.subTest(port=port):
                config, service, run = self.fixture()
                before = render.service(config)
                self.enable(config, service, run, port)
                objects = render.service(config)
                def get(kind, name):
                    return next(o for o in objects if o['kind'] == kind and o['metadata']['name'] == name)
                self.assertEqual(get('Service', render.NAME), before[1])
                self.assertEqual(get('NetworkPolicy', 'journal-clients-and-database'), before[3])
                deployment = get('Deployment', render.NAME)
                self.assertEqual(deployment['spec']['replicas'], 0)
                pod = deployment['spec']['template']['spec']
                self.assertEqual(pod['containers'][0]['ports'], [{'name': 'https', 'containerPort': 8443},
                                                                  {'name': 'metrics', 'containerPort': port or 9444}])
                self.assertEqual(pod['containers'][0]['volumeMounts'], before[2]['spec']['template']['spec']['containers'][0]['volumeMounts'])
                self.assertNotIn(config['metrics']['collectorSecret']['name'], json.dumps(pod))
                metric_service = get('Service', render.METRICS_NAME)
                self.assertEqual(metric_service['spec']['selector'], {'app': render.NAME})
                self.assertEqual(metric_service['spec']['ports'], [{'name': 'metrics', 'port': port or 9444, 'targetPort': 'metrics'}])
                policy = get('NetworkPolicy', 'journal-metrics-collector')['spec']
                self.assertEqual(policy['policyTypes'], ['Ingress'])
                self.assertEqual(policy['ingress'], [{'from': [
                    {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'monitoring'}},
                     'podSelector': {'matchLabels': render.PROMETHEUS_LABELS}}],
                    'ports': [{'protocol': 'TCP', 'port': port or 9444}]}])
                monitor = get('ServiceMonitor', render.METRICS_NAME)
                self.assertEqual(monitor['metadata']['namespace'], 'monitoring')
                self.assertEqual(monitor['metadata']['labels']['release'], 'kube-prometheus-stack')
                self.assertEqual(monitor['spec']['namespaceSelector'], {'matchNames': [render.SERVICE_NS]})
                self.assertEqual(monitor['spec']['selector']['matchLabels'], {'relay-candidate-metrics': 'true'})
                self.assertEqual(monitor['spec']['sampleLimit'], 32)
                endpoint = monitor['spec']['endpoints'][0]
                self.assertEqual(endpoint, {'port': 'metrics', 'path': '/metrics/candidate-journal', 'scheme': 'https',
                    'interval': '30s', 'scrapeTimeout': '10s', 'followRedirects': False, 'honorLabels': False,
                    'authorization': {'type': 'Bearer', 'credentials': config['metrics']['collectorSecret']},
                    'tlsConfig': {'ca': endpoint['tlsConfig']['ca'], 'serverName': config['metrics']['serverName'], 'insecureSkipVerify': False},
                    'relabelings': [{'targetLabel': 'job', 'replacement': 'relay-candidate-journal'}]})
                ca_ref = endpoint['tlsConfig']['ca']['configMap']
                ca_map = get('ConfigMap', ca_ref['name'])
                self.assertEqual(ca_map['metadata']['namespace'], 'monitoring')
                self.assertTrue(ca_map['immutable'])
                self.assertEqual(hashlib.sha256(ca_map['data'][ca_ref['key']].encode()).hexdigest(), run['payload']['ledger']['caSha256'])
                self.assertFalse(any(o['kind'] == 'Secret' for o in objects))

    def test_incomplete_or_unsafe_metrics_configuration_is_refused(self):
        mutations = ['outer-only', 'inner-only', 'null', 'protocol-port', 'port-range', 'boolean-port', 'port-mismatch',
                     'loopback-host', 'missing-verifier', 'zero-verifier', 'raw-token', 'expired', 'invalid-date',
                     'invalid-expiry-shape', 'unrelated-ca', 'invalid-ca', 'private-ca', 'wrong-server-name',
                     'runtime-secret', 'wrong-secret-key', 'selector-override', 'namespace-overlap']
        reasons = {
            'metrics-opt-in-mismatch': ['outer-only', 'inner-only'],
            'configuration-field-set-invalid': ['null', 'missing-verifier', 'raw-token', 'selector-override'],
            'metrics-port-invalid': ['protocol-port', 'port-range', 'boolean-port'],
            'metrics-listener-mismatch': ['port-mismatch', 'loopback-host'],
            'metrics-verifier-invalid': ['zero-verifier'],
            'metrics-credential-expired': ['expired'],
            'metrics-expiry-invalid': ['invalid-date', 'invalid-expiry-shape'],
            'metrics-ca-binding-mismatch': ['unrelated-ca'],
            'metrics-ca-invalid': ['invalid-ca', 'private-ca'],
            'metrics-tls-name-invalid': ['wrong-server-name'],
            'metrics-collector-reference-invalid': ['runtime-secret', 'wrong-secret-key'],
            'metrics-namespace-overlap': ['namespace-overlap'],
        }
        expected = {mutation: reason for reason, cases in reasons.items() for mutation in cases}
        self.assertEqual(set(expected), set(mutations))
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                config, service, run = self.fixture()
                self.enable(config, service, run)
                settings, metrics = config['metrics'], service['metrics']
                if mutation == 'outer-only': del service['metrics']
                if mutation == 'inner-only': del config['metrics']
                if mutation == 'null': config['metrics'] = None
                if mutation == 'protocol-port': settings['port'] = metrics['listen']['port'] = 8443
                if mutation == 'port-range': settings['port'] = metrics['listen']['port'] = 65536
                if mutation == 'boolean-port': settings['port'] = metrics['listen']['port'] = True
                if mutation == 'port-mismatch': metrics['listen']['port'] = 10444
                if mutation == 'loopback-host': metrics['listen']['host'] = '127.0.0.1'
                if mutation == 'missing-verifier': del metrics['tokenSha256']
                if mutation == 'zero-verifier': metrics['tokenSha256'] = '0' * 64
                if mutation == 'raw-token': metrics['token'] = 'synthetic-must-not-be-rendered'
                if mutation == 'expired': metrics['expiresAt'] = '2020-01-01T00:00:00Z'
                if mutation == 'invalid-date': metrics['expiresAt'] = '2099-02-30T00:00:00Z'
                if mutation == 'invalid-expiry-shape': metrics['expiresAt'] = 1
                if mutation == 'unrelated-ca':
                    settings['ca'] = self.pin('unrelated-ca.pem', self.certificate.decode() + '\n')
                if mutation in ['invalid-ca', 'private-ca']:
                    raw = 'not a certificate' if mutation == 'invalid-ca' else self.certificate.decode() + '\n-----BEGIN PRIVATE KEY-----\n'
                    settings['ca'] = self.pin('invalid-ca.pem', raw)
                    run['payload']['ledger']['caSha256'] = settings['ca']['sha256']
                    service['journal']['caSha256'] = settings['ca']['sha256']
                    config['run'] = self.pin('run.json', run)
                    service['run']['sha256'] = config['run']['sha256']
                if mutation == 'wrong-server-name': settings['serverName'] = 'unrelated.example'
                if mutation == 'runtime-secret': settings['collectorSecret']['name'] = render.NAME + '-runtime'
                if mutation == 'wrong-secret-key': settings['collectorSecret']['key'] = 'agent-dsn'
                if mutation == 'selector-override': settings['prometheusSelector'] = {}
                if mutation == 'namespace-overlap':
                    run['payload']['namespaces'][0]['name'] = 'monitoring'
                    config['run'] = self.pin('run.json', run)
                    service['run']['sha256'] = config['run']['sha256']
                config['config'] = self.pin('config.json', service)
                with self.assertRaisesRegex(render.Refusal, '^' + expected[mutation] + '$'): render.service(config)


if __name__ == '__main__':
    unittest.main()
