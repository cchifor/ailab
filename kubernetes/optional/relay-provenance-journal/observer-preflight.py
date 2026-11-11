#!/usr/bin/env python3
"""Read-only, outside-node Talos observer preflight. No cluster writes or secrets."""
import argparse
import json
import os
import re
import selectors
import subprocess
import time


DNS = re.compile(r'[a-z0-9]([-a-z0-9.]*[a-z0-9])?\Z')
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
VERSION = re.compile(r'v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?')
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
            process.kill()
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


def collect(node_name, namespace, pod_name, kubectl, talosctl):
    node = read_object([kubectl, 'get', 'node', node_name, '-o', 'json'], 'Node')
    pod = read_object([kubectl, 'get', 'pod', pod_name, '-n', namespace, '-o', 'json'], 'Pod')
    whoami = read_object([kubectl, 'auth', 'whoami', '-o', 'json'], 'SelfSubjectReview')
    talos_version = bounded([talosctl, '--nodes', node_name, 'version', '--short']) if talosctl else None
    version = VERSION.search(talos_version.decode('ascii', errors='ignore')) if talos_version else None
    node_meta = node.get('metadata', {}) if isinstance(node, dict) else {}
    node_status = node.get('status', {}) if isinstance(node, dict) else {}
    node_info = node_status.get('nodeInfo', {}) if isinstance(node_status, dict) else {}
    pod_meta = pod.get('metadata', {}) if isinstance(pod, dict) else {}
    pod_spec = pod.get('spec', {}) if isinstance(pod, dict) else {}
    pod_status = pod.get('status', {}) if isinstance(pod, dict) else {}
    identity = whoami.get('status', {}).get('userInfo', {}).get('username') if whoami else None
    if not isinstance(identity, str) or len(identity) > 256 or not re.fullmatch(r'[A-Za-z0-9:@._/-]+', identity):
        identity = UNKNOWN
    node_uid = uid(node_meta.get('uid')) if node_meta.get('name') == node_name else UNKNOWN
    pod_uid = uid(pod_meta.get('uid')) if (pod_meta.get('name') == pod_name and
                                             pod_meta.get('namespace') == namespace) else UNKNOWN
    linked = (node_uid != UNKNOWN and pod_uid != UNKNOWN and
              pod_spec.get('nodeName') == node_name and pod_status.get('phase') == 'Running')
    runtime = node_info.get('containerRuntimeVersion')
    if not isinstance(runtime, str) or not re.fullmatch(r'containerd://[A-Za-z0-9.+_-]{1,64}', runtime):
        runtime = UNKNOWN
    # API objects can contain annotations, environment, and tokens. Emit only
    # this fixed, bounded allowlist; discard the original objects immediately.
    report = {
        'version': 1,
        'kind': 'relay-runtime-observer-preflight',
        'executionVehicle': 'operator-workstation-read-only-api',
        'credentialIdentity': identity,
        'nodeName': node_name,
        'nodeUid': node_uid,
        'podNamespace': namespace,
        'podName': pod_name,
        'podUid': pod_uid,
        'podRunningOnNode': linked,
        'talosVersion': version.group(0) if version else UNKNOWN,
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
    parser.add_argument('--talosctl')
    args = parser.parse_args()
    for value in (args.node, args.namespace, args.pod):
        if len(value) > 253 or not DNS.fullmatch(value):
            parser.error('node, namespace and pod must be DNS names')
    for path in (args.kubectl, args.talosctl):
        if path is not None and (not os.path.isabs(path) or not os.path.isfile(path)):
            parser.error('tool paths must be absolute regular files')
    print(json.dumps(collect(args.node, args.namespace, args.pod, args.kubectl, args.talosctl),
                     sort_keys=True, separators=(',', ':')))


if __name__ == '__main__':
    main()
