"""Reduce one Velero resource-list artifact to counts; never output names or URLs."""
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import ssl
import time
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

BACKUP = 'velero-weekly-20261011030034'
BACKUP_UID = '84590917-87e3-4dc9-b642-a1638e792214'
REQUEST = 'objectstore-inventory-20261011'
RESULT = 'objectstore-backup-inventory-20261011'
RESULT_NAMESPACE = 'platform-edge'
EXPECTED_ITEMS = 6198
REMOVE_BY = datetime(2026, 10, 18, tzinfo=timezone.utc)
MAX_COMPRESSED = 4 * 1024 * 1024
MAX_EXPANDED = 16 * 1024 * 1024
SA = Path('/var/run/secrets/kubernetes.io/serviceaccount')
CA = '/trust/ca.pem'
RESOURCE_TYPE = re.compile(r'(?:[a-z0-9.-]+/)?v[0-9]+(?:(?:alpha|beta)[0-9]+)?/[A-Za-z][A-Za-z0-9]*')


class InventoryUnavailable(Exception):
    """Fixed public reason: never include raw payloads or transport exceptions."""

    def __init__(self):
        super().__init__('inventory unavailable')


def require(condition):
    if not condition:
        raise InventoryUnavailable()


def stamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(result.tzinfo is not None)
        return result
    except (AttributeError, TypeError, ValueError):
        raise InventoryUnavailable() from None


def validate_backup(value, now):
    meta, status = value.get('metadata', {}), value.get('status', {})
    require(meta.get('name') == BACKUP and meta.get('namespace') == 'velero'
            and meta.get('uid') == BACKUP_UID and not meta.get('deletionTimestamp'))
    require(status.get('phase') == 'Completed' and stamp(status.get('expiration')) > now)
    spec = value.get('spec', {})
    require(spec.get('snapshotVolumes') is False and spec.get('snapshotMoveData') is False)
    # Velero's async finalizer can add objects without rewriting the resource list.
    # These int,omitempty fields are absent when zero; no other shape qualifies.
    for key in ('backupItemOperationsAttempted', 'backupItemOperationsCompleted',
                'backupItemOperationsFailed', 'errors'):
        counter = status.get(key, 0)
        require(type(counter) is int and counter == 0)
    progress = status.get('progress', {})
    count = progress.get('itemsBackedUp')
    require(type(count) is int and count > 0 and progress.get('totalItems') == count)
    return count


def validate_request(value, now):
    meta, status = value.get('metadata', {}), value.get('status', {})
    require(meta.get('name') == REQUEST and meta.get('namespace') == 'velero'
            and not meta.get('deletionTimestamp'))
    require(value.get('spec', {}).get('target') == {'kind': 'BackupResourceList', 'name': BACKUP})
    require(status.get('phase') == 'Processed')
    remaining = (stamp(status.get('expiration')) - now).total_seconds()
    require(0 < remaining <= 600)
    url = status.get('downloadURL')
    require(isinstance(url, str) and not any(c.isspace() for c in url))
    try:
        parsed = urlsplit(url)
        require(parsed.scheme == 'https' and parsed.netloc == '192.168.1.225:7070'
                and parsed.path == f'/velero/backups/{BACKUP}/{BACKUP}-resource-list.json.gz'
                and not parsed.fragment and bool(parsed.query))
    except ValueError:
        raise InventoryUnavailable() from None
    return url


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def json_document(raw):
    try:
        return json.loads(raw, object_pairs_hook=unique_object)
    except (UnicodeError, ValueError, RecursionError):
        raise InventoryUnavailable() from None


def reduce_inventory(compressed, expected, now):
    require(len(compressed) <= MAX_COMPRESSED)
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(MAX_EXPANDED + 1)
        require(len(raw) <= MAX_EXPANDED)
        resources = json_document(raw)
    except (OSError, EOFError):
        raise InventoryUnavailable() from None
    require(isinstance(resources, dict) and bool(resources))
    counts = {}
    for kind, names in resources.items():
        require(RESOURCE_TYPE.fullmatch(kind) is not None and isinstance(names, list))
        require(all(isinstance(name, str) and bool(name) for name in names))
        require(len(set(names)) == len(names))
        counts[kind] = len(names)
    count = sum(counts.values())
    require(count == expected)
    return {'status': 'available', 'backup': BACKUP, 'backup_uid': BACKUP_UID,
            'acquired_at': now.isoformat(), 'artifact_sha256': hashlib.sha256(compressed).hexdigest(),
            'item_count': count, 'expected_item_count': expected,
            'data_upload_count': sum(n for kind, n in counts.items()
                                     if kind.startswith('velero.io/') and kind.endswith('/DataUpload')),
            'counts': dict(sorted(counts.items()))}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InventoryUnavailable()


def fetch(url, *, context, limit, method='GET', data=None, headers=None):
    # Ignore proxy environment; neither the bearer token nor signed URL may go elsewhere.
    opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=context))
    request = Request(url, data=data, method=method, headers=headers or {})
    with opener.open(request, timeout=20) as response:
        require(response.status == 200)
        body = response.read(limit + 1)
        require(len(body) <= limit)
        return body


def api(path, *, method='GET', data=None):
    host = os.environ['KUBERNETES_SERVICE_HOST']
    port = os.environ['KUBERNETES_SERVICE_PORT_HTTPS']
    # The standard service environment is numeric; avoid DNS and URL injection.
    import ipaddress
    ipaddress.ip_address(host)
    require(port.isdecimal() and 1 <= int(port) <= 65535)
    authority = f'[{host}]' if ':' in host else host
    context = ssl.create_default_context(cafile=str(SA / 'ca.crt'))
    headers = {'Authorization': 'Bearer ' + (SA / 'token').read_text().strip(),
               'Content-Type': 'application/merge-patch+json' if method == 'PATCH' else 'application/json'}
    raw = fetch(f'https://{authority}:{port}{path}', context=context, limit=1024 * 1024,
                method=method, data=None if data is None else json.dumps(data).encode(), headers=headers)
    return json_document(raw)


def acquire():
    now = datetime.now(timezone.utc)
    require(now < REMOVE_BY)
    backup = api(f'/apis/velero.io/v1/namespaces/velero/backups/{BACKUP}')
    count = validate_backup(backup, now)
    require(count == EXPECTED_ITEMS)
    # Flux creates the fixed-name DownloadRequest. The Job cannot create another one.
    deadline = time.monotonic() + 120
    while True:
        try:
            request = api(f'/apis/velero.io/v1/namespaces/velero/downloadrequests/{REQUEST}')
        except HTTPError as error:
            if error.code != 404:
                raise
            request = {}
        if request.get('status', {}).get('phase') == 'Processed':
            break
        require(time.monotonic() < deadline)
        time.sleep(2)
    url = validate_request(request, datetime.now(timezone.utc))
    raw = fetch(url, context=ssl.create_default_context(cafile=CA), limit=MAX_COMPRESSED)
    now = datetime.now(timezone.utc)
    validate_request(request, now)
    # Check identity again so expiration/deletion during acquisition is not a success.
    require(validate_backup(api(f'/apis/velero.io/v1/namespaces/velero/backups/{BACKUP}'), now) == count)
    return reduce_inventory(raw, count, now)


def publish(receipt):
    api(f'/api/v1/namespaces/{RESULT_NAMESPACE}/configmaps/{RESULT}', method='PATCH',
        data={'data': {'receipt.json': json.dumps(receipt, sort_keys=True)}})


def main():
    try:
        receipt = acquire()
    except Exception:
        # urllib/SSL/JSON exceptions can embed the signed URL or resource names.
        receipt = {'status': 'unavailable', 'backup': BACKUP, 'backup_uid': BACKUP_UID,
                   'checked_at': datetime.now(timezone.utc).isoformat()}
    try:
        publish(receipt)
    except Exception:
        print('inventory receipt unavailable', flush=True)
        return 1
    print('inventory receipt ' + receipt['status'], flush=True)
    return 0 if receipt['status'] == 'available' else 1


if __name__ == '__main__':
    raise SystemExit(main())
