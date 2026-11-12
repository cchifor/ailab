#!/usr/bin/env python3
"""Render inactive provenance resources from reviewed, pinned public inputs."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
NAME = 'relay-provenance-journal'
DB_NS = NAME + '-db'
SERVICE_NS = NAME
PUBLIC = '/run/relay-provenance/'
ORIGIN = 'https://' + NAME + '.' + SERVICE_NS + '.svc:8443'
PG_IMAGE = 'docker.io/library/postgres:17.11-alpine@sha256:e5d2edd28b1f8d40a0a6c0c9cdc545b19b56a0d5d88f94bc11a97d970d4700d0'
# Keep renderer pins consistent with initialize.sh and the matching upgrade script.
REVIEWED_SCHEMA_SHA256 = {
    'schema010': '86fd1612f0d9aac62902825d7e4fb290ac6c2c030f89d3e4a764a23230a125e1',
    'schema011': '094e6639b07d497401f86ab6af5ccd14813238d2beb1d538bcd1ac7f105caebd',
    # The VM stage replays the reviewed 008 repair before the separately named
    # KubeVirt 011–014 chain. Numeric 011 alone is not a global schema version.
    'schema008Repair': '9e7b48a1f624c83e421bd187354318f589f788229908bc283d4e4e0abf07f95c',
    'vmSchema011': '02a3d6459458304f69ded3c3601d2b3df53ea84ca5cd59b3c4df99f94d66993d',
    'vmSchema012': 'cabffdf93c9759110d31480481d90b4cf9996514d1fb217080e2350ed8155c3a',
    'vmSchema013': '4d0397e70d144104d0a991b2f4475b9fa8d5d52ed03f1a63de8eb6023d679fcd',
    'vmSchema014': '17721e0021c88d76bf68157fbe6f28bb1ac44119fd92ecb291e001644d6eddf0',
}
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
    return database_job(config, 'initialize', list(range(1, 12)))


def upgrade(config):
    previous = config.get('fromVersion') if isinstance(config, dict) else None
    if type(previous) is not int or previous not in (5, 6, 7):
        refuse('upgrade-version-invalid')
    return database_job(config, 'upgrade', list(range(previous + 1, 9)))


def upgrade9(config):
    previous = config.get('fromVersion') if isinstance(config, dict) else None
    if type(previous) is not int or previous != 8:
        refuse('upgrade-version-invalid')
    return database_job(config, 'upgrade9', [9])


def upgrade10(config):
    previous = config.get('fromVersion') if isinstance(config, dict) else None
    if type(previous) is not int or previous != 9:
        refuse('upgrade-version-invalid')
    return database_job(config, 'upgrade10', [10])


def upgrade11(config):
    previous = config.get('fromVersion') if isinstance(config, dict) else None
    if type(previous) is not int or previous != 10:
        refuse('upgrade-version-invalid')
    return database_job(config, 'upgrade11', [11])


def upgrade_vm(config):
    previous = config.get('fromVersion') if isinstance(config, dict) else None
    if type(previous) is not int or previous != 11:
        refuse('upgrade-version-invalid')
    return database_job(config, 'upgradeVm', [8, 11, 12, 13, 14])


def database_job(config, action, numbers):
    vm = action == 'upgradeVm'
    keys = (['schema008Repair'] + ['vmSchema' + str(n).zfill(3) for n in numbers[1:]]) if vm else [
        'schema' + str(n).zfill(3) for n in numbers]
    filenames = (['008-repair.sql'] + ['vm' + str(n).zfill(3) + '.sql' for n in numbers[1:]]) if vm else [
        str(n).zfill(3) + '.sql' for n in numbers]
    env_names = (['SCHEMA_008_REPAIR_SHA256'] + ['VM_SCHEMA_' + str(n).zfill(3) + '_SHA256' for n in numbers[1:]]) if vm else [
        'SCHEMA_' + str(n).zfill(3) + '_SHA256' for n in numbers]
    is_upgrade = action in ('upgrade', 'upgrade9', 'upgrade10', 'upgrade11', 'upgradeVm')
    commit = common(config, keys + (['fromVersion'] if is_upgrade else []))
    pins = [config[key] for key in keys]
    for key, digest in REVIEWED_SCHEMA_SHA256.items():
        if key in keys and config[key]['sha256'] != digest:
            refuse(key + '-pin-mismatch')
    sql = {name: pinned(pin, 262144) for name, pin in zip(filenames, pins)}
    if any(len(re.findall(r'^BEGIN;$', raw, re.M)) != 1 or
           len(re.findall(r'^COMMIT;$', raw, re.M)) != 1 for raw in sql.values()):
        refuse('schema-transaction-invalid')
    filename = 'upgrade-vm.sh' if vm else action + '.sh'
    script = (ROOT / 'initialize' / filename).read_text()
    suffix = hashlib.sha256(''.join(pin['sha256'] for pin in pins).encode()).hexdigest()[:16]
    schema_name = NAME + '-schema-' + suffix
    script_name = NAME + '-' + ('upgrade-vm' if vm else action) + '-' + hashlib.sha256(script.encode()).hexdigest()[:16]
    env = [{'name': 'PGDATA', 'value': '/var/lib/postgresql/data/pgdata'}] + [
        {'name': name, 'value': pin['sha256']} for name, pin in zip(env_names, pins)]
    if is_upgrade:
        env.append({'name': 'FROM_SCHEMA_VERSION', 'value': str(config['fromVersion'])})
    mounts = [{'name': n, 'mountPath': p} for n, p in [('database', '/var/lib/postgresql/data'), ('schema', '/schema'),
             ('script', '/initialize'), ('run', '/var/run/postgresql'), ('tmp', '/tmp')]]
    job_name = NAME + ('-initialize-once' if action == 'initialize' else
                       '-upgrade-11-to-vm15' if vm else
                       '-upgrade-8-to-9' if action == 'upgrade9' else
                       '-upgrade-9-to-10' if action == 'upgrade10' else
                       '-upgrade-10-to-11' if action == 'upgrade11' else
                       '-upgrade-' + str(config['fromVersion']) + '-to-8')
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
    if is_upgrade:
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


def backing(config):
    commit = common(config, ['backingImage', 'publicDirectory', 'configSha256', 'privateSecret',
                             'archiveClaim', 'clients', 'kubernetesCidrs', 'providerCidrs', 'guestCidrs'])
    image = config['backingImage']
    if not isinstance(image, str) or not re.fullmatch(r'[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}', image) or image.endswith('0' * 64):
        refuse('image-pin-invalid')
    public_dir = config['publicDirectory']
    if not isinstance(public_dir, str) or not os.path.isabs(public_dir) or os.path.normpath(public_dir) != public_dir:
        refuse('public-directory-invalid')
    if any(not isinstance(config[key], str) or not re.fullmatch('[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', config[key])
           for key in ('privateSecret', 'archiveClaim')) or not sha(config['configSha256']):
        refuse('resource-reference-invalid')
    name = 'relay-vm-original-backing'
    base = '/run/relay-vm-backing/'
    public_path, private_path = base + 'public/', base + 'private/'
    public = {'config.json': pinned({'path': public_dir + '/config.json', 'sha256': config['configSha256']})}
    c = parse(public['config.json'])
    fields(c, ['version', 'policy', 'backingSource', 'issuerKeyFile', 'issuerKeyOwnerUid',
               'readerPins', 'storageClass', 'kubernetes', 'provider', 'guest', 'archive', 'service'])
    if c['version'] != 1:
        refuse('backing-config-version-invalid')
    def public_file(pin, filename):
        fields(pin, ['path', 'sha256'])
        if pin['path'] != public_path + filename:
            refuse('backing-public-path-invalid')
        public[filename] = pinned({'path': public_dir + '/' + filename, 'sha256': pin['sha256']})
    for pin, filename in [(c['policy'], 'policy.json'), (c['readerPins'], 'reader-pins.json'),
                          (c['storageClass'], 'storage-class.json'),
                          (c['backingSource']['ca'], 'source-ca.pem'),
                          (c['kubernetes']['ca'], 'kubernetes-ca.pem'),
                          (c['provider']['ca'], 'provider-ca.pem'),
                          (c['provider']['clientCert'], 'provider-client.crt'),
                          (c['guest']['ca'], 'guest-ca.pem'),
                          (c['service']['tlsCert'], 'tls.crt'),
                          (c['service']['clientCa'], 'client-ca.pem')]:
        public_file(pin, filename)
    policy = parse(public['policy.json'])
    if policy.get('version') != 3 or policy.get('bindingMode') != 'WaitForFirstConsumer' or not isinstance(policy.get('namespace'), dict):
        refuse('vm-policy-invalid')
    namespace = policy['namespace'].get('name')
    if not isinstance(namespace, str) or not re.fullmatch('[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', namespace):
        refuse('vm-namespace-invalid')
    sources = policy.get('sources')
    if not isinstance(sources, list) or len([s for s in sources if isinstance(s, dict) and s.get('kind') == 'backing'
                                             and s.get('id') == c['backingSource'].get('sourceId')]) != 1:
        refuse('backing-source-mismatch')
    for key in ('issuerKeyOwnerUid',):
        if c[key] != 65532:
            refuse('private-owner-invalid')
    for field in ('backingSource', 'provider', 'service'):
        if c[field]['keyOwnerUid' if field == 'backingSource' else 'clientKeyOwnerUid' if field == 'provider' else 'tlsKeyOwnerUid'] != 65532:
            refuse('private-owner-invalid')
    private_names = ['source-key.pem', 'issuer-key.pem', 'provider-client-key.pem', 'tls-key.pem']
    if (c['backingSource']['keyFile'] != private_path + private_names[0] or
        c['issuerKeyFile'] != private_path + private_names[1] or
        c['provider']['clientKeyFile'] != private_path + private_names[2] or
        c['service']['tlsKeyFile'] != private_path + private_names[3] or
        c['archive'] != {'directory': '/var/lib/relay-vm-backing/archive', 'trustedUid': 65532} or
        c['kubernetes']['tokenFile'] != '/var/run/secrets/kubernetes.io/serviceaccount/token' or
        c['service']['host'] != '0.0.0.0' or c['service']['port'] != 8443):
        refuse('backing-runtime-path-invalid')
    if c['backingSource']['pins']['origin'] != ORIGIN or c['backingSource']['pins']['caSha256'] != c['backingSource']['ca']['sha256']:
        refuse('backing-source-pin-invalid')
    def endpoint_port(origin):
        try:
            url = urlsplit(origin)
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.path or url.query or url.fragment or origin != f'https://{url.netloc}':
                refuse('backing-origin-invalid')
            return url.port or 443
        except ValueError:
            refuse('backing-origin-invalid')
    ports = [endpoint_port(c[field]['origin']) for field in ('kubernetes', 'provider', 'guest')]
    def networks(key):
        raw = config[key]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 8 or len(set(raw)) != len(raw):
            refuse('egress-cidrs-invalid')
        try:
            canonical = [str(ipaddress.ip_network(value, strict=True)) for value in raw]
        except (ValueError, TypeError):
            refuse('egress-cidrs-invalid')
        if raw != canonical or any(x in ('0.0.0.0/0', '::/0') for x in raw):
            refuse('egress-cidrs-invalid')
        return raw
    cidrs = [networks(key) for key in ('kubernetesCidrs', 'providerCidrs', 'guestCidrs')]
    clients = config['clients']
    if not isinstance(clients, list) or not 1 <= len(clients) <= 8:
        refuse('clients-invalid')
    peers = []
    for client in clients:
        fields(client, ['namespace', 'podLabels'])
        ns, labels = client['namespace'], client['podLabels']
        if not isinstance(ns, str) or not re.fullmatch('[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', ns) or not isinstance(labels, dict) or not 1 <= len(labels) <= 4 or any(
            not re.fullmatch('[a-zA-Z0-9._/-]{1,63}', k) or not isinstance(v, str) or not re.fullmatch('[a-zA-Z0-9._-]{1,63}', v) for k, v in labels.items()):
            refuse('clients-invalid')
        peers.append({'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': ns}},
                      'podSelector': {'matchLabels': labels}})
    public_name = name + '-public-' + config['configSha256'][:16]
    sa = {'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': metadata(name, SERVICE_NS, commit),
          'automountServiceAccountToken': False}
    cluster_role = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRole', 'metadata': {'name': name},
                    'rules': [{'apiGroups': [''], 'resources': ['namespaces', 'persistentvolumes'], 'verbs': ['get']},
                              {'apiGroups': ['storage.k8s.io'], 'resources': ['storageclasses'], 'verbs': ['get']},
                              {'apiGroups': ['storage.k8s.io'], 'resources': ['volumeattachments'], 'verbs': ['get', 'list']}]}
    cluster_binding = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRoleBinding', 'metadata': {'name': name},
                       'subjects': [{'kind': 'ServiceAccount', 'name': name, 'namespace': SERVICE_NS}],
                       'roleRef': {'kind': 'ClusterRole', 'name': name, 'apiGroup': 'rbac.authorization.k8s.io'}}
    role = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role', 'metadata': metadata(name, namespace, commit),
            'rules': [{'apiGroups': [''], 'resources': ['persistentvolumeclaims'], 'verbs': ['get']},
                      {'apiGroups': [''], 'resources': ['pods'], 'verbs': ['get', 'list']},
                      {'apiGroups': ['kubevirt.io'], 'resources': ['virtualmachines'], 'verbs': ['get']},
                      {'apiGroups': ['kubevirt.io'], 'resources': ['virtualmachineinstances'], 'verbs': ['get', 'list']}]}
    role_binding = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding', 'metadata': metadata(name, namespace, commit),
                    'subjects': [{'kind': 'ServiceAccount', 'name': name, 'namespace': SERVICE_NS}],
                    'roleRef': {'kind': 'Role', 'name': name, 'apiGroup': 'rbac.authorization.k8s.io'}}
    materialize = ("const f=require('node:fs'),b='/run/relay-vm-backing/private',s='/projected/';"
                   "for(const n of ['source-key.pem','issuer-key.pem','provider-client-key.pem','tls-key.pem']){"
                   "const v=f.readFileSync(s+n);if(v.length<1||v.length>65536)process.exit(78);"
                   "const h=f.openSync(b+'/'+n,f.constants.O_WRONLY|f.constants.O_CREAT|f.constants.O_EXCL|f.constants.O_NOFOLLOW,0o600);"
                   "try{f.writeSync(h,v);f.fsyncSync(h)}finally{f.closeSync(h)};f.chmodSync(b+'/'+n,0o600);f.chownSync(b+'/'+n,65532,65532)}"
                   "f.chmodSync(b,0o700);f.chownSync(b,65532,65532)")
    mounts = [{'name': 'public', 'mountPath': public_path + filename, 'subPath': filename, 'readOnly': True} for filename in public]
    mounts += [{'name': 'private', 'mountPath': private_path, 'readOnly': True},
               {'name': 'kube-token', 'mountPath': '/var/run/secrets/kubernetes.io/serviceaccount', 'readOnly': True},
               {'name': 'archive', 'mountPath': '/var/lib/relay-vm-backing/archive'}]
    deploy = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': metadata(name, SERVICE_NS, commit),
              'spec': {'replicas': 0, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': {'app': name}},
                       'template': {'metadata': {'labels': {'app': name}}, 'spec': {
                           'serviceAccountName': name, 'automountServiceAccountToken': False,
                           'enableServiceLinks': False, 'hostNetwork': False, 'hostPID': False, 'hostIPC': False,
                           'securityContext': {'runAsNonRoot': True, 'runAsUser': 65532, 'runAsGroup': 65532,
                                               'seccompProfile': {'type': 'RuntimeDefault'}}, 'affinity': AFFINITY,
                           'initContainers': [{'name': 'materialize-private', 'image': image,
                               'command': ['node', '-e', materialize],
                               'securityContext': {'runAsUser': 0, 'runAsNonRoot': False, 'allowPrivilegeEscalation': False,
                                   'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL'], 'add': ['CHOWN']},
                                   'seccompProfile': {'type': 'RuntimeDefault'}},
                               'volumeMounts': [{'name': 'projected-private', 'mountPath': '/projected', 'readOnly': True},
                                                {'name': 'private', 'mountPath': private_path}]}],
                           'containers': [{'name': 'backing', 'image': image,
                               'command': ['node', 'dist/scripts/vm-original-backing-service.js', public_path + 'config.json', config['configSha256']],
                               'securityContext': SECURITY, 'ports': [{'name': 'https', 'containerPort': 8443}],
                               'readinessProbe': {'tcpSocket': {'port': 8443}, 'periodSeconds': 5},
                               'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'},
                                             'limits': {'cpu': '1', 'memory': '512Mi'}}, 'volumeMounts': mounts}],
                           'volumes': [{'name': 'public', 'configMap': {'name': public_name, 'defaultMode': 292}},
                                       {'name': 'projected-private', 'secret': {'secretName': config['privateSecret'],
                                          'defaultMode': 256, 'items': [{'key': n, 'path': n} for n in private_names]}},
                                       {'name': 'private', 'emptyDir': {'medium': 'Memory', 'sizeLimit': '1Mi'}},
                                       {'name': 'kube-token', 'projected': {'sources': [{'serviceAccountToken': {'path': 'token', 'expirationSeconds': 3600}}]}},
                                       {'name': 'archive', 'persistentVolumeClaim': {'claimName': config['archiveClaim']}}]}}}}
    svc = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': metadata(name, SERVICE_NS, commit),
           'spec': {'selector': {'app': name}, 'ports': [{'name': 'https', 'port': 8443, 'targetPort': 'https'}]}}
    egress = [{'to': [{'podSelector': {'matchLabels': {'app': NAME}}}], 'ports': [{'protocol': 'TCP', 'port': 8443}]}]
    egress += [{'to': [{'ipBlock': {'cidr': cidr}} for cidr in group], 'ports': [{'protocol': 'TCP', 'port': port}]}
               for group, port in zip(cidrs, ports)]
    egress.append({'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                           'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
                   'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]})
    network = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
               'metadata': metadata(name + '-peers', SERVICE_NS, commit),
               'spec': {'podSelector': {'matchLabels': {'app': name}}, 'policyTypes': ['Ingress', 'Egress'],
                        'ingress': [{'from': peers, 'ports': [{'protocol': 'TCP', 'port': 8443}]}], 'egress': egress}}
    source_ingress = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
                      'metadata': metadata(name + '-journal-access', SERVICE_NS, commit),
                      'spec': {'podSelector': {'matchLabels': {'app': NAME}}, 'policyTypes': ['Ingress'],
                               'ingress': [{'from': [{'podSelector': {'matchLabels': {'app': name}}}],
                                            'ports': [{'protocol': 'TCP', 'port': 8443}]}]}}
    return [cm(public_name, SERVICE_NS, public, commit), sa, cluster_role, cluster_binding,
            role, role_binding, svc, deploy, network, source_ingress]


def main(args):
    if len(args) != 3 or args[0] not in ('initialize', 'upgrade', 'upgrade9', 'upgrade10', 'upgrade11', 'upgradeVm', 'service', 'presence', 'backing') or not sha(args[2]):
        refuse('usage-invalid')
    config = parse(pinned({'path': args[1], 'sha256': args[2]}))
    items = {'initialize': initialize, 'upgrade': upgrade, 'upgrade9': upgrade9, 'upgrade10': upgrade10, 'upgrade11': upgrade11,
             'upgradeVm': upgrade_vm,
             'service': service, 'presence': presence, 'backing': backing}[args[0]](config)
    print(json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': items}, separators=(',', ':')))


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except (Refusal, OSError, UnicodeError, KeyError, TypeError, ValueError):
        print('provenance-render-refused', file=sys.stderr)
        sys.exit(78)
