#!/usr/bin/env python3
"""Render public, inactive operator manifests; never contact Kubernetes or seed authority."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parent
DB_NS = 'relay-candidate-journal-db'
SERVICE_NS = 'relay-candidate-journal'
NAME = 'relay-candidate-journal'
PG_IMAGE = 'docker.io/library/postgres:17.11-alpine@sha256:3540c0362c809df084ffac0ae69fe794515f9de92692f02178bcdce0ef0302ac'
PUBLIC = '/run/relay-journal/'
ORIGIN = 'https://relay-candidate-journal.relay-candidate-journal.svc:8443'
LABELS = {'app.kubernetes.io/part-of': NAME, 'velero.io/exclude-from-backup': 'true'}
AFFINITY = {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {
    'nodeSelectorTerms': [{'matchExpressions': [
        {'key': 'ailab.io/relay-worker-pool', 'operator': 'DoesNotExist'},
        {'key': 'kubernetes.io/arch', 'operator': 'In', 'values': ['amd64']},
    ]}]}}}
SECURITY = {'privileged': False, 'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
            'capabilities': {'drop': ['ALL']}, 'seccompProfile': {'type': 'RuntimeDefault'}}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def refuse():
    raise ValueError('journal-render-input-refused')


def fields(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        refuse()


def parse(raw):
    def pairs(values):
        obj = {}
        for key, value in values:
            if key in obj:
                refuse()
            obj[key] = value
        return obj
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: refuse())


def pinned(pin):
    fields(pin, ['path', 'sha256'])
    path, expected = pin['path'], pin['sha256']
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path or not re.fullmatch('[a-f0-9]{64}', expected):
        refuse()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid not in [0, os.getuid()] or st.st_mode & 0o022 or st.st_size > 65536 or os.path.realpath('/proc/self/fd/' + str(fd)) != path:
            refuse()
        raw = b''
        while len(raw) <= st.st_size:
            part = os.read(fd, st.st_size + 1 - len(raw))
            if not part:
                break
            raw += part
        if len(raw) != st.st_size or digest(raw) != expected:
            refuse()
        return raw.decode('utf-8', errors='strict')
    finally:
        os.close(fd)


def metadata(name, namespace, annotations=None):
    value = {'name': name, 'namespace': namespace, 'labels': dict(LABELS)}
    if annotations:
        value['annotations'] = annotations
    return value


def configmap(name, namespace, data, annotations=None):
    return {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': metadata(name, namespace, annotations),
            'immutable': True, 'data': data}


def source(config):
    value = config.get('relaySourceCommit')
    if config.get('version') != 1 or not isinstance(value, str) or not re.fullmatch('[a-f0-9]{40}', value) or value == '0' * 40:
        refuse()
    return {'relay.chifor.me/source-commit': value}


def initialize(config):
    fields(config, ['version', 'relaySourceCommit', 'schema001', 'schema002'])
    annotations = source(config)
    sql = {name: pinned(config[key]) for name, key in [
        ('001-journal.sql', 'schema001'), ('002-signed-clients.sql', 'schema002')]}
    # This guards packaging omissions, not arbitrary SQL safety: exact reviewed
    # source and each file's transaction boundaries remain operator trust inputs.
    for raw in sql.values():
        if not re.search(r'^BEGIN;$', raw, re.M) or not re.search(r'^COMMIT;$', raw, re.M):
            refuse()
    suffix = digest((''.join(config[k]['sha256'] for k in ['schema001', 'schema002'])).encode())[:16]
    schema_name = NAME + '-schema-' + suffix
    script = (ROOT / 'initialize/initialize.sh').read_text()
    script_name = NAME + '-initialize-' + digest(script.encode())[:16]
    container = {'name': 'initialize', 'image': PG_IMAGE,
        'command': ['/bin/sh', '/initialize/initialize.sh'],
        'env': [{'name': 'PGDATA', 'value': '/var/lib/postgresql/data/pgdata'},
                {'name': 'SCHEMA_001_SHA256', 'value': config['schema001']['sha256']},
                {'name': 'SCHEMA_002_SHA256', 'value': config['schema002']['sha256']}],
        'securityContext': SECURITY,
        'resources': {'requests': {'cpu': '250m', 'memory': '512Mi', 'ephemeral-storage': '64Mi'},
                      'limits': {'cpu': '1', 'memory': '512Mi', 'ephemeral-storage': '128Mi'}},
        'volumeMounts': [
            {'name': 'database', 'mountPath': '/var/lib/postgresql/data'},
            {'name': 'schema', 'mountPath': '/schema', 'readOnly': True},
            {'name': 'script', 'mountPath': '/initialize', 'readOnly': True},
            {'name': 'run', 'mountPath': '/var/run/postgresql'},
            {'name': 'tmp', 'mountPath': '/tmp'},
        ]}
    job = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': metadata(NAME + '-initialize-once', DB_NS, annotations),
        'spec': {'suspend': True, 'backoffLimit': 0, 'activeDeadlineSeconds': 600,
            'template': {'metadata': {'labels': LABELS, 'annotations': {'backup.velero.io/backup-volumes-excludes': 'database'}},
                'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                    'terminationGracePeriodSeconds': 120, 'affinity': AFFINITY,
                    'tolerations': [{'key': 'dedicated', 'operator': 'Equal', 'value': 'agent', 'effect': 'NoSchedule'}],
                    'securityContext': {'runAsNonRoot': True, 'runAsUser': 70, 'runAsGroup': 70,
                        'fsGroup': 70, 'fsGroupChangePolicy': 'OnRootMismatch', 'seccompProfile': {'type': 'RuntimeDefault'}},
                    'containers': [container], 'volumes': [
                        {'name': 'database', 'persistentVolumeClaim': {'claimName': NAME + '-postgres-data'}},
                        {'name': 'schema', 'configMap': {'name': schema_name, 'defaultMode': 0o444}},
                        {'name': 'script', 'configMap': {'name': script_name, 'defaultMode': 0o444}},
                        {'name': 'run', 'emptyDir': {'medium': 'Memory', 'sizeLimit': '16Mi'}},
                        {'name': 'tmp', 'emptyDir': {'sizeLimit': '32Mi'}},
                    ]}}}}
    return [configmap(schema_name, DB_NS, sql, annotations),
            configmap(script_name, DB_NS, {'initialize.sh': script}, annotations), job]


def service(config):
    fields(config, ['version', 'relaySourceCommit', 'relayImage', 'config', 'run', 'witness', 'databaseCa', 'tlsCertificate'])
    annotations = source(config)
    image = config['relayImage']
    if not isinstance(image, str) or not re.fullmatch(r'[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}', image) or image.endswith('0' * 64):
        refuse()
    raw = {name: pinned(config[key]) for name, key in [
        ('config.json', 'config'), ('run.json', 'run'), ('witness.json', 'witness'),
        ('db-ca.pem', 'databaseCa'), ('tls.crt', 'tlsCertificate')]}
    c, run, witness = parse(raw['config.json']), parse(raw['run.json']), parse(raw['witness.json'])
    fields(c, ['version', 'run', 'witness', 'trustedIssuerKeys', 'journal', 'replyPrivateKeyFile', 'tls', 'listen', 'database'])
    if c['version'] != 1 or c['listen'] != {'host': '0.0.0.0', 'port': 8443}:
        refuse()
    keys = c['trustedIssuerKeys']
    if not isinstance(keys, dict) or not 1 <= len(keys) <= 8 or any(
            not re.fullmatch('[a-z][a-z0-9-]{0,62}', key) or
            not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]{43}', value)
            for key, value in keys.items()):
        refuse()
    for field, source_key, filename in [('run', 'run', 'run.json'), ('witness', 'witness', 'witness.json')]:
        if c[field] != {'path': PUBLIC + filename, 'sha256': config[source_key]['sha256']}:
            refuse()
    if c['replyPrivateKeyFile'] != PUBLIC + 'reply-key.pem' or c['tls'] != {
        'certificate': {'path': PUBLIC + 'tls.crt', 'sha256': config['tlsCertificate']['sha256']},
        'privateKeyFile': PUBLIC + 'tls-key.pem'} or c['database'] != {
        'ca': {'path': PUBLIC + 'db-ca.pem', 'sha256': config['databaseCa']['sha256']},
        'agentConnectionStringFile': PUBLIC + 'agent-dsn', 'holderConnectionStringFile': PUBLIC + 'holder-dsn'}:
        refuse()
    # The compiled service performs cryptographic verification before listening.
    # This pure renderer checks public wiring only and cannot authorize a run.
    p = run['payload']
    if run.get('kind') != 'relay.candidate-run' or p['hub']['image'] != image or p['ledger']['origin'] != ORIGIN or c['journal'] != {
        key: p['ledger'][key] for key in ['origin', 'caSha256', 'replyPublicKey']}:
        refuse()
    if witness.get('externalEpoch') != p['externalEpoch'] or any(witness.get(key) != p['ledger'][key] for key in ['databaseIdentity', 'gateId', 'witnessSha256']):
        refuse()
    namespaces = p['namespaces']
    if len(namespaces) != 2 or {n['purpose'] for n in namespaces} != {'control', 'execution'} or any(not re.fullmatch(r'[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?', n['name']) for n in namespaces):
        refuse()
    if len({n['name'] for n in namespaces}) != 2 or any(n['name'] in [SERVICE_NS, DB_NS, 'kube-system'] for n in namespaces):
        refuse()
    annotations['relay.chifor.me/config-sha256'] = config['config']['sha256']
    cm_name = NAME + '-public-' + config['config']['sha256'][:16]
    mounts = [{'name': 'public', 'mountPath': PUBLIC + filename, 'subPath': filename, 'readOnly': True} for filename in raw]
    mounts += [{'name': 'runtime', 'mountPath': PUBLIC + key, 'subPath': key, 'readOnly': True}
               for key in ['reply-key.pem', 'agent-dsn', 'holder-dsn']]
    mounts += [{'name': 'tls', 'mountPath': PUBLIC + 'tls-key.pem', 'subPath': 'tls.key', 'readOnly': True}]
    app_labels = {**LABELS, 'app': NAME}
    deployment = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': metadata(NAME, SERVICE_NS, annotations),
        'spec': {'replicas': 0, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': {'app': NAME}},
            'template': {'metadata': {'labels': app_labels, 'annotations': annotations}, 'spec': {
                'automountServiceAccountToken': False, 'enableServiceLinks': False,
                'hostNetwork': False, 'hostPID': False, 'hostIPC': False,
                'terminationGracePeriodSeconds': 30, 'affinity': AFFINITY,
                'tolerations': [{'key': 'dedicated', 'operator': 'Equal', 'value': 'agent', 'effect': 'NoSchedule'}],
                'securityContext': {'runAsUser': 0, 'runAsGroup': 0, 'seccompProfile': {'type': 'RuntimeDefault'}},
                'containers': [{'name': 'journal', 'image': image,
                    'command': ['node', '/app/dist/scripts/candidate-journal-service.js', PUBLIC + 'config.json', config['config']['sha256']],
                    'securityContext': SECURITY, 'ports': [{'name': 'https', 'containerPort': 8443}],
                    'resources': {'requests': {'cpu': '100m', 'memory': '128Mi', 'ephemeral-storage': '16Mi'},
                                  'limits': {'cpu': '500m', 'memory': '256Mi', 'ephemeral-storage': '32Mi'}},
                    'readinessProbe': {'tcpSocket': {'port': 'https'}, 'periodSeconds': 5},
                    'livenessProbe': {'tcpSocket': {'port': 'https'}, 'initialDelaySeconds': 15, 'periodSeconds': 10},
                    'volumeMounts': mounts}],
                'volumes': [
                    {'name': 'public', 'configMap': {'name': cm_name, 'defaultMode': 0o444}},
                    {'name': 'runtime', 'secret': {'secretName': NAME + '-runtime', 'defaultMode': 0o400,
                        'items': [{'key': key, 'path': key} for key in ['reply-key.pem', 'agent-dsn', 'holder-dsn']]}},
                    {'name': 'tls', 'secret': {'secretName': NAME + '-tls', 'defaultMode': 0o400,
                        'items': [{'key': 'tls.key', 'path': 'tls.key'}]}},
                ]}}}}
    ingress = [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': n['name']}},
                'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'relay-infrastructure-agent'} if n['purpose'] == 'control' else {'relay.infrastructure/managed': 'true'}}} for n in namespaces]
    policy = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': metadata('journal-clients-and-database', SERVICE_NS),
        'spec': {'podSelector': {'matchLabels': {'app': NAME}}, 'policyTypes': ['Ingress', 'Egress'],
            'ingress': [{'from': ingress, 'ports': [{'protocol': 'TCP', 'port': 8443}]}],
            'egress': [{'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': DB_NS}},
                                'podSelector': {'matchLabels': {'app': NAME + '-postgres'}}}],
                        'ports': [{'protocol': 'TCP', 'port': 5432}]},
                       {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                                'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
                        'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]}]}}
    svc = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': metadata(NAME, SERVICE_NS),
           'spec': {'selector': {'app': NAME}, 'ports': [{'name': 'https', 'port': 8443, 'targetPort': 'https'}]}}
    return [configmap(cm_name, SERVICE_NS, raw, annotations), svc, deployment, policy]


def main(args):
    if len(args) != 3 or args[0] not in ['initialize', 'service']:
        refuse()
    config = parse(pinned({'path': args[1], 'sha256': args[2]}))
    objects = (initialize if args[0] == 'initialize' else service)(config)
    print(json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': objects}, indent=2))


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except Exception:
        print('Candidate journal rendering refused; check reviewed public pins and fixed deployment paths. No cluster changes performed.', file=sys.stderr)
        sys.exit(1)
