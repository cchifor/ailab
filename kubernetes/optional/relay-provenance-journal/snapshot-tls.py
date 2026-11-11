#!/usr/bin/env python3
"""Operator-only secret-to-secret pipe. Never log or persist private material."""
import base64
import hashlib
import json
import os
import stat
import re
import subprocess
import sys

NAME = 'relay-provenance-journal'
NAMESPACE = 'relay-provenance-journal'


def snapshot(source, certificate_sha256):
    if not re.fullmatch('[a-f0-9]{64}', certificate_sha256):
        raise ValueError('certificate-pin-invalid')
    if source.get('kind') != 'Secret' or source.get('type') != 'kubernetes.io/tls' or source.get('metadata', {}).get('name') != NAME + '-tls' or source.get('metadata', {}).get('namespace') != NAMESPACE:
        raise ValueError('certificate-source-refused')
    data = source.get('data', {})
    cert, key = (base64.b64decode(data[name], validate=True) for name in ['tls.crt', 'tls.key'])
    if not 1 <= len(cert) <= 65536 or not 1 <= len(key) <= 65536 or hashlib.sha256(cert).hexdigest() != certificate_sha256:
        raise ValueError('certificate-pin-mismatch')
    def public(command, value):
        result = subprocess.run(command, input=value, capture_output=True, timeout=5, check=False)
        if result.returncode or len(result.stdout) > 16384:
            raise ValueError('certificate-key-pair-invalid')
        return result.stdout.strip()
    if public(['openssl', 'x509', '-pubkey', '-noout'], cert) != public(['openssl', 'pkey', '-pubout'], key):
        raise ValueError('certificate-key-pair-mismatch')
    return {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'kubernetes.io/tls', 'immutable': True,
        'metadata': {'name': NAME + '-tls-' + certificate_sha256[:32], 'namespace': NAMESPACE,
            'labels': {'app.kubernetes.io/part-of': NAME, 'velero.io/exclude-from-backup': 'true'},
            'annotations': {'relay.chifor.me/tls-certificate-sha256': certificate_sha256}},
        'data': {'tls.crt': data['tls.crt'], 'tls.key': data['tls.key']}}


def main():
    if len(sys.argv) != 2:
        raise ValueError('usage')
    if not stat.S_ISFIFO(os.fstat(sys.stdout.fileno()).st_mode):
        raise ValueError('secret-consumer-pipe-required')
    raw = sys.stdin.buffer.read(262145)
    if len(raw) > 262144:
        raise ValueError('secret-input-too-large')
    # stdout MUST be piped straight into kubectl create; never a file or log.
    result = snapshot(json.loads(raw), sys.argv[1])
    sys.stdout.write(json.dumps(result) + '\n')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Input and OpenSSL errors can contain private data. Do not echo them.
        print('TLS snapshot refused: check source Secret, certificate pin and matching key. No private material logged.', file=sys.stderr)
        sys.exit(1)
