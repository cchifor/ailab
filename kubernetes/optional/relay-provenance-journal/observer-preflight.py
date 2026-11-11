#!/usr/bin/env python3
"""Read-only, outside-node Talos observer preflight. No cluster writes or secrets."""
import argparse
import json
import os
import re
import selectors
import subprocess
import time


DNS_LABEL = re.compile(r'[a-z0-9]([-a-z0-9]*[a-z0-9])?\Z')
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
TALOS_IMAGE = re.compile(r'Talos \((v[0-9]+\.[0-9]+\.[0-9]+)\)\Z')
UNKNOWN = 'unknown'


def bounded(command):
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
    except OSError:
        return None
    chunks = []
    size = 0
    deadline = time.monotonic() + 8
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    return None
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    break
                size += len(chunk)
                if size > 262144:
                    return None
                chunks.append(chunk)
        if process.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
            return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait()
    return b''.join(chunks)


def read_object(command, kind):
    raw = bounded(command)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) and value.get('kind') == kind else None


def uid(value):
    return value if isinstance(value, str) and UUID.fullmatch(value) else UNKNOWN


def obj(value):
    return value if isinstance(value, dict) else {}


def dns_subdomain(value):
    return (len(value) <= 253 and all(0 < len(label) <= 63 and DNS_LABEL.fullmatch(label)
                                      for label in value.split('.')))


def collect(node_name, namespace, pod_name, kubectl, kubeconfig, context):
    base = [kubectl, '--kubeconfig', kubeconfig, '--context', context, '--request-timeout=8s']
    node = read_object(base + ['get', 'node', node_name, '-o', 'json'], 'Node')
    pod = read_object(base + ['get', 'pod', pod_name, '-n', namespace, '-o', 'json'], 'Pod')
    node_meta = obj(obj(node).get('metadata'))
    node_status = obj(obj(node).get('status'))
    node_info = obj(node_status.get('nodeInfo'))
    pod_meta = obj(obj(pod).get('metadata'))
    pod_spec = obj(obj(pod).get('spec'))
    pod_status = obj(obj(pod).get('status'))
    node_uid = uid(node_meta.get('uid')) if node_meta.get('name') == node_name else UNKNOWN
    pod_uid = uid(pod_meta.get('uid')) if (pod_meta.get('name') == pod_name and
                                             pod_meta.get('namespace') == namespace) else UNKNOWN
    statuses = pod_status.get('containerStatuses')
    containers_running = (isinstance(statuses, list) and len(statuses) == 2 and
                          {item.get('name') for item in statuses if isinstance(item, dict)} ==
                          {'supervisor', 'harness'} and
                          all(isinstance(item, dict) and item.get('ready') is True and
                              item.get('restartCount') == 0 and
                              isinstance(obj(item.get('state')).get('running'), dict)
                              for item in statuses))
    linked = (node_uid != UNKNOWN and pod_uid != UNKNOWN and
              pod_meta.get('deletionTimestamp') is None and
              pod_spec.get('nodeName') == node_name and pod_status.get('phase') == 'Running' and
              containers_running)
    runtime = node_info.get('containerRuntimeVersion')
    if not isinstance(runtime, str) or not re.fullmatch(r'containerd://[A-Za-z0-9.+_-]{1,64}', runtime):
        runtime = UNKNOWN
    image = node_info.get('osImage')
    version = TALOS_IMAGE.fullmatch(image) if isinstance(image, str) else None
    # API objects can contain annotations, environment, and tokens. Emit only
    # this fixed, bounded allowlist; discard the original objects immediately.
    report = {
        'version': 1,
        'kind': 'relay-runtime-observer-preflight',
        'executionVehicle': 'operator-workstation-read-only-api',
        'credentialIdentity': UNKNOWN,
        'kubernetesContext': context,
        'nodeName': node_name,
        'nodeUid': node_uid,
        'podNamespace': namespace,
        'podName': pod_name,
        'podUid': pod_uid,
        'podRunningOnNode': linked,
        'talosVersion': version.group(1) if version else UNKNOWN,
        'containerRuntimeVersion': runtime,
        'criSocketPath': UNKNOWN,
        'criInstance': UNKNOWN,
        'verboseInspectSchema': UNKNOWN,
        'cgroupVersionAndDriver': UNKNOWN,
        'hostPidAndProcAccess': UNKNOWN,
        'hostInitTimeNamespace': UNKNOWN,
        'pidfdAndPtraceAccess': UNKNOWN,
        'checkpointFeatureAndRbac': UNKNOWN,
        'signingKeyOwnerName': UNKNOWN,
        'offNodeLedgerOwnerName': UNKNOWN,
        'readyForObserver': False,
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--pod', required=True)
    parser.add_argument('--kubectl', required=True)
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--context', required=True)
    args = parser.parse_args()
    for value in (args.node, args.pod):
        if not dns_subdomain(value):
            parser.error('node and pod must be DNS subdomains')
    if len(args.namespace) > 63 or not DNS_LABEL.fullmatch(args.namespace):
        parser.error('namespace must be a DNS label')
    if len(args.context) > 253 or not re.fullmatch(r'[A-Za-z0-9@._:/-]+', args.context):
        parser.error('context must be a bounded name')
    if not os.path.isabs(args.kubectl) or not os.path.isfile(args.kubectl) or not os.access(args.kubectl, os.X_OK):
        parser.error('kubectl must be an absolute executable file')
    if not os.path.isabs(args.kubeconfig) or not os.path.isfile(args.kubeconfig):
        parser.error('kubeconfig must be an absolute regular file')
    print(json.dumps(collect(args.node, args.namespace, args.pod, args.kubectl,
                             args.kubeconfig, args.context),
                     sort_keys=True, separators=(',', ':')))


if __name__ == '__main__':
    main()
