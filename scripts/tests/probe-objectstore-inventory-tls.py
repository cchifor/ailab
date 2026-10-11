"""Explicit, read-only TLS acceptance probe; no HTTP or backup data is requested.

Run in the inventory Job's digest-pinned Python image with the repository mounted
at /repo. The three TLS handshakes use the existing endpoint, never credentials.
"""
import hashlib
import json
from pathlib import Path
import socket
import ssl
import sys

ROOT = Path(__file__).resolve().parents[2]
CA = ROOT / 'kubernetes/apps/backup/objectstore-inventory/ca.pem'
HOST = '192.168.1.225'
PORT = 7070
FINGERPRINT = '678b7e6a983e28dcc8c939e92abee0411eae606aad1bc7dc7e4e076aefeddf10'


def connect(context, hostname):
    with socket.create_connection((HOST, PORT), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=hostname) as connection:
            return connection.version(), hashlib.sha256(
                connection.getpeercert(binary_form=True)).hexdigest()


def must_reject(context, hostname):
    try:
        connect(context, hostname)
    except ssl.SSLCertVerificationError:
        return
    raise RuntimeError('TLS negative control unexpectedly accepted the peer')


def main():
    context = ssl.create_default_context(cafile=str(CA))
    if not (context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            and context.verify_flags & ssl.VERIFY_X509_STRICT):
        raise RuntimeError('Run with the pinned runtime and unmodified strict TLS defaults')
    version, peer = connect(context, HOST)
    if peer != FINGERPRINT:
        raise RuntimeError('Endpoint certificate differs from independently pinned trust')
    must_reject(ssl.create_default_context(cafile=str(CA)), 'mismatch.invalid')
    must_reject(ssl.create_default_context(), HOST)
    print(json.dumps({
        'python': sys.version.split()[0], 'openssl': ssl.OPENSSL_VERSION,
        'strict': True, 'hostname_check': True, 'certificate_required': True,
        'handshake': 'passed', 'tls': version, 'peer_sha256': peer,
        'wrong_hostname': 'rejected', 'untrusted_chain': 'rejected',
        'http_requests': 0,
    }, indent=2))


if __name__ == '__main__':
    main()
