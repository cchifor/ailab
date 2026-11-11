#!/usr/bin/env python3
"""Render inactive provenance resources from reviewed, pinned public inputs."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

ROOT = Path(__file__).resolve().parent
NAME = 'relay-provenance-journal'
DB_NS = NAME + '-db'
SERVICE_NS = NAME
PUBLIC = '/run/relay-provenance/'
ORIGIN = 'https://' + NAME + '.' + SERVICE_NS + '.svc:8443'
PG_IMAGE = 'docker.io/library/postgres:17.11-alpine@sha256:3540c0362c809df084ffac0ae69fe794515f9de92692f02178bcdce0ef0302ac'
SECURITY = {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
            'capabilities': {'drop': ['ALL']}, 'seccompProfile': {'type': 'RuntimeDefault'}}
AFFINITY = {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {'nodeSelectorTerms': [
    {'matchExpressions': [{'key': 'ailab.io/relay-worker-pool', 'operator': 'DoesNotExist'},
                          {'key': 'kubernetes.io/arch', 'operator': 'In', 'values': ['amd64']}]}]}}}


class Refusal(ValueError):
    pass


def refuse(reason='binding-invalid'):
    raise Refusal(reason)


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        refuse('field-set-invalid')


def sha(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) and value != '0' * 64


def parse(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                refuse('duplicate-json-key')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: refuse('nonfinite-number'))
    except json.JSONDecodeError:
        refuse('invalid-json')


def pinned(pin, max_bytes=65536):
    fields(pin, ['path', 'sha256'])
    path = pin['path']
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path or not sha(pin['sha256']):
        refuse('file-pin-invalid')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        refuse('file-open-refused')
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid not in (0, os.getuid()) or st.st_mode & 0o022 or st.st_size > max_bytes:
            refuse('file-owner-type-or-size-refused')
        if os.path.realpath('/proc/self/fd/' + str(fd)) != path:
            refuse('file-path-not-canonical')
        raw = os.read(fd, st.st_size + 1)
        if len(raw) != st.st_size or hashlib.sha256(raw).hexdigest() != pin['sha256']:
            refuse('file-sha256-mismatch')
        return raw.decode('utf-8')
    finally:
        os.close(fd)


def metadata(name, ns, source):
    return {'name': name, 'namespace': ns, 'annotations': {'relay.chifor.me/source-commit': source},
            'labels': {'app.kubernetes.io/part-of': NAME, 'velero.io/exclude-from-backup': 'true'}}


def cm(name, ns, data, source):
    return {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': metadata(name, ns, source),
            'immutable': True, 'data': data}


def common(config, keys):
    fields(config, ['version', 'relaySourceCommit'] + keys)
    commit = config['relaySourceCommit']
    if config['version'] != 1 or not isinstance(commit, str) or not re.fullmatch('[a-f0-9]{40}', commit) or commit == '0' * 40:
        refuse('source-pin-invalid')
    return commit


def initialize(config):
    return database_job(config, 'initialize', list(range(1, 9)))


def upgrade(config):
    previous = config.get('fromVersion') if isinstance(config, dict) else None
    if type(previous) is not int or previous not in (5, 6, 7):
        refuse('upgrade-version-invalid')
    return database_job(config, 'upgrade', list(range(previous + 1, 9)))


def database_job(config, action, numbers):
    keys = ['schema' + str(n).zfill(3) for n in numbers]
    commit = common(config, keys + (['fromVersion'] if action == 'upgrade' else []))
    pins = [config[key] for key in keys]
    sql = {str(n).zfill(3) + '.sql': pinned(pin, 262144) for n, pin in zip(numbers, pins)}
    if any(len(re.findall(r'^BEGIN;$', raw, re.M)) != 1 or
           len(re.findall(r'^COMMIT;$', raw, re.M)) != 1 for raw in sql.values()):
        refuse('schema-transaction-invalid')
    filename = action + '.sh'
    script = (ROOT / 'initialize' / filename).read_text()
    suffix = hashlib.sha256(''.join(pin['sha256'] for pin in pins).encode()).hexdigest()[:16]
    schema_name, script_name = NAME + '-schema-' + suffix, NAME + '-' + action + '-' + hashlib.sha256(script.encode()).hexdigest()[:16]
    env = [{'name': 'PGDATA', 'value': '/var/lib/postgresql/data/pgdata'}] + [
        {'name': 'SCHEMA_' + str(n).zfill(3) + '_SHA256', 'value': pin['sha256']} for n, pin in zip(numbers, pins)]
    if action == 'upgrade':
        env.append({'name': 'FROM_SCHEMA_VERSION', 'value': str(config['fromVersion'])})
    mounts = [{'name': n, 'mountPath': p} for n, p in [('database', '/var/lib/postgresql/data'), ('schema', '/schema'),
             ('script', '/initialize'), ('run', '/var/run/postgresql'), ('tmp', '/tmp')]]
    job_name = NAME + ('-initialize-once' if action == 'initialize' else '-upgrade-' + str(config['fromVersion']) + '-to-8')
    job = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': metadata(job_name, DB_NS, commit),
           'spec': {'suspend': True, 'backoffLimit': 0, 'activeDeadlineSeconds': 600,
                    'template': {'metadata': {'labels': {'app': NAME + '-' + action}}, 'spec': {
                        'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                        'terminationGracePeriodSeconds': 120, 'affinity': AFFINITY,
                        'tolerations': [{'key': 'dedicated', 'operator': 'Equal', 'value': 'agent', 'effect': 'NoSchedule'}],
                        'securityContext': {'runAsNonRoot': True, 'runAsUser': 70, 'runAsGroup': 70,
                                            'fsGroup': 70, 'fsGroupChangePolicy': 'OnRootMismatch',
                                            'seccompProfile': {'type': 'RuntimeDefault'}},
                        'containers': [{'name': action, 'image': PG_IMAGE,
                                        'command': ['/bin/sh', '/initialize/' + filename], 'env': env,
                                        'securityContext': SECURITY, 'volumeMounts': mounts,
                                        'resources': {'requests': {'cpu': '250m', 'memory': '512Mi'},
                                                      'limits': {'cpu': '1', 'memory': '512Mi'}}}],
                        'volumes': [{'name': 'database', 'persistentVolumeClaim': {'claimName': NAME + '-postgres-data'}},
                                    {'name': 'schema', 'configMap': {'name': schema_name, 'defaultMode': 292}},
                                    {'name': 'script', 'configMap': {'name': script_name, 'defaultMode': 292}},
                                    {'name': 'run', 'emptyDir': {'medium': 'Memory', 'sizeLimit': '16Mi'}},
                                    {'name': 'tmp', 'emptyDir': {'sizeLimit': '32Mi'}}]}}}}
    if action == 'upgrade':
        # Never ask kubelet to repair group ownership/mode on retained data.
        # Scratch uses emptyDir, not a PVC: kubelet setupDir uses 0777 and
        # the memory volume uses tmpfs defaults, writable by UID 70 without
        # fsGroup. A root/chown init container would violate Restricted PSA.
        security = job['spec']['template']['spec']['securityContext']
        security.pop('fsGroup')
        security.pop('fsGroupChangePolicy')
    return [cm(schema_name, DB_NS, sql, commit), cm(script_name, DB_NS, {filename: script}, commit), job]


def service(config):
    commit = common(config, ['relayImage', 'config', 'policy', 'authorization', 'witness', 'databaseCa', 'tlsCertificate', 'clients'])
    image = config['relayImage']
    if not isinstance(image, str) or not re.fullmatch(r'[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}', image) or image.endswith('0' * 64):
        refuse('image-pin-invalid')
    public = {name: pinned(config[key]) for name, key in [('config.json', 'config'), ('policy.json', 'policy'),
        ('authorization.json', 'authorization'), ('witness.json', 'witness'), ('db-ca.pem', 'databaseCa'),
        ('tls.crt', 'tlsCertificate')]}
    c = parse(public['config.json'])
    fields(c, ['version', 'policy', 'authority', 'witness', 'pins', 'replyPrivateKeyFile', 'databaseCa',
               'maxClockSkewMs', 'sources', 'tls', 'listen'])
    if c['version'] != 1 or c['listen'] != {'host': '0.0.0.0', 'port': 8443} or type(c['listen']['port']) is not int:
        refuse('listener-invalid')
    for key, file in [('policy', 'policy.json'), ('authority', 'authorization.json'), ('witness', 'witness.json'),
                      ('databaseCa', 'db-ca.pem')]:
        pin = config['authorization' if key == 'authority' else key]
        if c[key] != {'path': PUBLIC + file, 'sha256': pin['sha256']}:
            refuse('public-pin-mismatch')
    if c['tls'] != {'certificate': {'path': PUBLIC + 'tls.crt', 'sha256': config['tlsCertificate']['sha256']},
                    'privateKeyFile': PUBLIC + 'tls-key.pem'} or c['replyPrivateKeyFile'] != PUBLIC + 'reply-key.pem':
        refuse('private-path-mismatch')
    fields(c['pins'], ['origin', 'caSha256', 'replyPublicKeyHex'])
    if c['pins']['origin'] != ORIGIN or any(not sha(c['pins'][key]) for key in ['caSha256', 'replyPublicKeyHex']):
        refuse('origin-or-key-pin-invalid')
    if type(c['maxClockSkewMs']) is not int or not 0 <= c['maxClockSkewMs'] <= 5000:
        refuse('clock-skew-invalid')
    sources = c['sources']
    if not isinstance(sources, list) or not 2 <= len(sources) <= 8:
        refuse('sources-invalid')
    secret_names = ['reply-key.pem', 'tls-key.pem']
    source_ids = set()
    transitions = 0
    for i, source in enumerate(sources):
        fields(source, ['sourceId', 'readConnectionStringFile'], ['transitionConnectionStringFile'])
        try:
            valid_id = str(uuid.UUID(source['sourceId'])) == source['sourceId']
        except (ValueError, TypeError, AttributeError):
            valid_id = False
        if not valid_id or source['sourceId'] in source_ids:
            refuse('source-id-invalid')
        source_ids.add(source['sourceId'])
        read = 'source-' + str(i) + '-read-dsn'
        if source['readConnectionStringFile'] != PUBLIC + read:
            refuse('source-path-invalid')
        secret_names.append(read)
        if 'transitionConnectionStringFile' in source:
            transition = 'source-' + str(i) + '-transition-dsn'
            if source['transitionConnectionStringFile'] != PUBLIC + transition:
                refuse('source-path-invalid')
            secret_names.append(transition)
            transitions += 1
    if transitions == 0:
        refuse('transition-source-required')
    clients = config['clients']
    if not isinstance(clients, list) or not 1 <= len(clients) <= 8:
        refuse('clients-invalid')
    peers = []
    for client in clients:
        fields(client, ['namespace', 'podLabels'])
        ns, labels = client['namespace'], client['podLabels']
        if not isinstance(ns, str) or not re.fullmatch('[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', ns) or ns in (SERVICE_NS, DB_NS):
            refuse('client-namespace-invalid')
        if not isinstance(labels, dict) or not 1 <= len(labels) <= 4 or any(
            not re.fullmatch('[a-zA-Z0-9._/-]{1,63}', k) or not isinstance(v, str) or
            not re.fullmatch('[a-zA-Z0-9._-]{1,63}', v) for k, v in labels.items()):
            refuse('client-labels-invalid')
        peers.append({'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': ns}},
                      'podSelector': {'matchLabels': labels}})
    cm_name = NAME + '-public-' + config['config']['sha256'][:16]
    tls_name = NAME + '-tls-' + config['tlsCertificate']['sha256'][:32]
    mounts = [{'name': 'public', 'mountPath': PUBLIC + file, 'subPath': file, 'readOnly': True} for file in public]
    mounts += [{'name': 'runtime', 'mountPath': PUBLIC + file, 'subPath': file, 'readOnly': True}
               for file in secret_names if file != 'tls-key.pem']
    mounts.append({'name': 'tls', 'mountPath': PUBLIC + 'tls-key.pem', 'subPath': 'tls.key', 'readOnly': True})
    probe = ['node', '-e', "const s=require('node:net').connect({host:'127.0.0.1',port:8443});s.setTimeout(2000);s.on('connect',()=>{s.destroy();process.exit(0)});s.on('timeout',()=>process.exit(1));s.on('error',()=>process.exit(1));"]
    deploy = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': metadata(NAME, SERVICE_NS, commit),
              'spec': {'replicas': 0, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': {'app': NAME}},
                       'template': {'metadata': {'labels': {'app': NAME}}, 'spec': {
                           'automountServiceAccountToken': False, 'enableServiceLinks': False,
                           'hostNetwork': False, 'hostPID': False, 'hostIPC': False,
                           'securityContext': {'runAsUser': 0, 'runAsGroup': 0, 'seccompProfile': {'type': 'RuntimeDefault'}},
                           'affinity': AFFINITY, 'containers': [{'name': 'cleanup', 'image': image,
                               'command': ['node', '/app/dist/scripts/cleanup-journal-service.js', PUBLIC + 'config.json', config['config']['sha256']],
                               'securityContext': SECURITY, 'ports': [{'name': 'https', 'containerPort': 8443}],
                               'readinessProbe': {'exec': {'command': probe}, 'timeoutSeconds': 3, 'periodSeconds': 5},
                               'livenessProbe': {'exec': {'command': probe}, 'timeoutSeconds': 3, 'periodSeconds': 10},
                               'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'},
                                             'limits': {'cpu': '500m', 'memory': '256Mi'}}, 'volumeMounts': mounts}],
                           'volumes': [{'name': 'public', 'configMap': {'name': cm_name, 'defaultMode': 292}},
                                       {'name': 'runtime', 'secret': {'secretName': NAME + '-runtime', 'defaultMode': 256,
                                           'items': [{'key': f, 'path': f} for f in secret_names if f != 'tls-key.pem']}},
                                       {'name': 'tls', 'secret': {'secretName': tls_name, 'defaultMode': 256,
                                           'items': [{'key': 'tls.key', 'path': 'tls.key'}]}}]}}}}
    svc = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': metadata(NAME, SERVICE_NS, commit),
           'spec': {'selector': {'app': NAME}, 'ports': [{'name': 'https', 'port': 8443, 'targetPort': 'https'}]}}
    policy = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
              'metadata': metadata('cleanup-clients-and-database', SERVICE_NS, commit),
              'spec': {'podSelector': {'matchLabels': {'app': NAME}}, 'policyTypes': ['Ingress', 'Egress'],
                       'ingress': [{'from': peers, 'ports': [{'protocol': 'TCP', 'port': 8443}]}],
                       'egress': [{'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': DB_NS}},
                                           'podSelector': {'matchLabels': {'app': NAME + '-postgres'}}}],
                                   'ports': [{'protocol': 'TCP', 'port': 5432}]},
                                  {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                                           'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
                                   'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]}]}}
    return [cm(cm_name, SERVICE_NS, public, commit), svc, deploy, policy]


def presence(config):
    commit = common(config, ['relayImage', 'tlsCertificate', 'clientCa', 'operatorRootHex',
                             'clientCertSha256', 'clients'])
    image = config['relayImage']
    if not isinstance(image, str) or not re.fullmatch(r'[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}', image) or image.endswith('0' * 64):
        refuse('image-pin-invalid')
    if any(not sha(config[key]) for key in ('operatorRootHex', 'clientCertSha256')):
        refuse('presence-pin-invalid')
    clients = config['clients']
    if not isinstance(clients, list) or not 1 <= len(clients) <= 8:
        refuse('clients-invalid')
    peers = []
    for client in clients:
        fields(client, ['namespace', 'podLabels'])
        ns, labels = client['namespace'], client['podLabels']
        if not isinstance(ns, str) or not re.fullmatch('[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', ns) or ns in (SERVICE_NS, DB_NS):
            refuse('client-namespace-invalid')
        if not isinstance(labels, dict) or not 1 <= len(labels) <= 4 or any(
            not re.fullmatch('[a-zA-Z0-9._/-]{1,63}', k) or not isinstance(v, str) or
            not re.fullmatch('[a-zA-Z0-9._-]{1,63}', v) for k, v in labels.items()):
            refuse('client-labels-invalid')
        peers.append({'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': ns}},
                      'podSelector': {'matchLabels': labels}})
    public = {'tls.crt': pinned(config['tlsCertificate']), 'client-ca.pem': pinned(config['clientCa'])}
    name = 'relay-runtime-presence-source'
    base = '/run/relay-presence/'
    public_binding = hashlib.sha256((config['tlsCertificate']['sha256'] + config['clientCa']['sha256']).encode()).hexdigest()
    cm_name = name + '-public-' + public_binding[:16]
    tls_secret = name + '-tls-' + config['tlsCertificate']['sha256'][:32]
    env = [{'name': k, 'value': v} for k, v in [
        ('RELAY_RUNTIME_PRESENCE_HOST', '0.0.0.0'), ('RELAY_RUNTIME_PRESENCE_PORT', '8443'),
        ('RELAY_RUNTIME_PRESENCE_DB_URL_FILE', base + 'observer-dsn'),
        ('RELAY_RUNTIME_PRESENCE_TLS_CERT_FILE', base + 'tls.crt'),
        ('RELAY_RUNTIME_PRESENCE_TLS_KEY_FILE', base + 'tls-key.pem'),
        ('RELAY_RUNTIME_PRESENCE_TLS_CA_FILE', base + 'client-ca.pem'),
        ('RELAY_RUNTIME_PRESENCE_OPERATOR_ROOT_HEX', config['operatorRootHex']),
        ('RELAY_RUNTIME_PRESENCE_CLIENT_CERT_SHA256', config['clientCertSha256'])]]
    mounts = [{'name': 'public', 'mountPath': base + f, 'subPath': f, 'readOnly': True} for f in public]
    mounts += [{'name': 'runtime', 'mountPath': base + 'observer-dsn', 'subPath': 'observer-dsn', 'readOnly': True},
               {'name': 'tls', 'mountPath': base + 'tls-key.pem', 'subPath': 'tls.key', 'readOnly': True}]
    probe = ['node', '-e', "const s=require('node:net').connect({host:'127.0.0.1',port:8443});s.setTimeout(2000);s.on('connect',()=>{s.destroy();process.exit(0)});s.on('timeout',()=>process.exit(1));s.on('error',()=>process.exit(1));"]
    deploy = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': metadata(name, SERVICE_NS, commit),
              'spec': {'replicas': 0, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': {'app': name}},
                       'template': {'metadata': {'labels': {'app': name}}, 'spec': {
                           'automountServiceAccountToken': False, 'enableServiceLinks': False,
                           'hostNetwork': False, 'hostPID': False, 'hostIPC': False,
                           'securityContext': {'runAsUser': 0, 'runAsGroup': 0, 'seccompProfile': {'type': 'RuntimeDefault'}},
                           'affinity': AFFINITY, 'containers': [{'name': 'presence', 'image': image,
                               'command': ['node', '/app/dist/scripts/serve-runtime-presence-source.js'],
                               'env': env, 'securityContext': SECURITY,
                               'ports': [{'name': 'https', 'containerPort': 8443}],
                               'readinessProbe': {'exec': {'command': probe}, 'timeoutSeconds': 3, 'periodSeconds': 5},
                               'livenessProbe': {'exec': {'command': probe}, 'timeoutSeconds': 3, 'periodSeconds': 10},
                               'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'},
                                             'limits': {'cpu': '500m', 'memory': '256Mi'}}, 'volumeMounts': mounts}],
                           'volumes': [{'name': 'public', 'configMap': {'name': cm_name, 'defaultMode': 292}},
                                       {'name': 'runtime', 'secret': {'secretName': name + '-runtime', 'defaultMode': 256,
                                           'items': [{'key': 'observer-dsn', 'path': 'observer-dsn'}]}},
                                       {'name': 'tls', 'secret': {'secretName': tls_secret, 'defaultMode': 256,
                                           'items': [{'key': 'tls.key', 'path': 'tls.key'}]}}]}}}}
    svc = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': metadata(name, SERVICE_NS, commit),
           'spec': {'selector': {'app': name}, 'ports': [{'name': 'https', 'port': 8443, 'targetPort': 'https'}]}}
    policy = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
              'metadata': metadata('runtime-presence-clients-and-database', SERVICE_NS, commit),
              'spec': {'podSelector': {'matchLabels': {'app': name}}, 'policyTypes': ['Ingress', 'Egress'],
                       'ingress': [{'from': peers, 'ports': [{'protocol': 'TCP', 'port': 8443}]}],
                       'egress': [{'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': DB_NS}},
                                           'podSelector': {'matchLabels': {'app': NAME + '-postgres'}}}],
                                   'ports': [{'protocol': 'TCP', 'port': 5432}]},
                                  {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                                           'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
                                   'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]}]}}
    return [cm(cm_name, SERVICE_NS, public, commit), svc, deploy, policy]


def main(args):
    if len(args) != 3 or args[0] not in ('initialize', 'upgrade', 'service', 'presence') or not sha(args[2]):
        refuse('usage-invalid')
    config = parse(pinned({'path': args[1], 'sha256': args[2]}))
    items = {'initialize': initialize, 'upgrade': upgrade, 'service': service, 'presence': presence}[args[0]](config)
    print(json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': items}, separators=(',', ':')))


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except (Refusal, OSError, UnicodeError, KeyError, TypeError, ValueError):
        print('provenance-render-refused', file=sys.stderr)
        sys.exit(78)
