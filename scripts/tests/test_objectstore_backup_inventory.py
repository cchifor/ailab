"""A resource-list receipt must never contain names or bearer capabilities."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import ssl
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / 'kubernetes/apps/backup/objectstore-inventory/inventory.py'
SPEC = importlib.util.spec_from_file_location('objectstore_inventory', PATH)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)
NOW = datetime(2026, 10, 11, 13, tzinfo=timezone.utc)
SECRET = 'private-secret-name-never-emit'
URL = f'https://192.168.1.225:7070/velero/backups/{mod.BACKUP}/{mod.BACKUP}-resource-list.json.gz?X-Amz-Signature=never-emit-token'


def backup():
    return {'metadata': {'name': mod.BACKUP, 'namespace': 'velero', 'uid': mod.BACKUP_UID},
            'spec': {'snapshotVolumes': False, 'snapshotMoveData': False},
            'status': {'phase': 'Completed', 'expiration': '2026-11-10T03:00:34Z',
                       'progress': {'itemsBackedUp': 2, 'totalItems': 2}}}


def download():
    return {'metadata': {'name': mod.REQUEST, 'namespace': 'velero'},
            'spec': {'target': {'kind': 'BackupResourceList', 'name': mod.BACKUP}},
            'status': {'phase': 'Processed', 'expiration': (NOW + timedelta(minutes=5)).isoformat(),
                       'downloadURL': URL}}


class InventoryTests(unittest.TestCase):
    def receipt(self, resources, expected=2):
        return mod.reduce_inventory(gzip.compress(json.dumps(resources).encode()), expected, NOW)

    def test_only_counts_digest_and_pinned_identity_are_emitted(self):
        resources = {'v1/Secret': ['strive-ailab/' + SECRET], 'v1/Pod': ['strive-ailab/objectstore']}
        result = self.receipt(resources)
        wire = json.dumps(result)
        self.assertNotIn(SECRET, wire)
        self.assertNotIn('strive-ailab/objectstore', wire)
        self.assertEqual(result['counts'], {'v1/Pod': 1, 'v1/Secret': 1})
        self.assertEqual(result['data_upload_count'], 0)
        self.assertEqual(result['item_count'], 2)
        self.assertEqual(result['backup_uid'], mod.BACKUP_UID)
        self.assertRegex(result['artifact_sha256'], r'^[a-f0-9]{64}$')
        self.assertEqual(result['status'], 'available')

    def test_data_uploads_are_reported_across_versions(self):
        result = self.receipt({'velero.io/v2alpha1/DataUpload': ['a/b'], 'velero.io/v1/DataUpload': ['a/c']})
        self.assertEqual(result['data_upload_count'], 2)

    def test_mismatch_does_not_claim_verified_inventory(self):
        with self.assertRaises(mod.InventoryUnavailable):
            self.receipt({'v1/Pod': ['a/b']})

    def test_malformed_or_duplicate_payload_is_unavailable(self):
        for body in [b'not json', b'[]', b'{"v1/Pod":[],"v1/Pod":[]}', b'{"v1/Pod":{}}',
                     b'{"v1/Pod":[null]}', b'{"v1/Pod":["a/b","a/b"]}', b'{"secret-name":["a/b"]}']:
            with self.subTest(body=body), self.assertRaises(mod.InventoryUnavailable):
                mod.reduce_inventory(gzip.compress(body), 2, NOW)

    def test_truncated_gzip_and_decompression_overflow_are_unavailable(self):
        raw = gzip.compress(b'[]')
        for body in [raw[:-5], b'plain text']:
            with self.subTest(body=body), self.assertRaises(mod.InventoryUnavailable):
                mod.reduce_inventory(body, 2, NOW)
        with patch.object(mod, 'MAX_EXPANDED', 8), self.assertRaises(mod.InventoryUnavailable):
            self.receipt({'v1/Pod': ['a/b']})

    def test_pin_and_expiry_are_checked_before_url_use(self):
        self.assertEqual(mod.validate_backup(backup(), NOW), 2)
        self.assertEqual(mod.validate_request(download(), NOW), URL)
        mutations = [lambda b: b['metadata'].update(uid='other'),
                     lambda b: b['metadata'].update(name='other'),
                     lambda b: b['status'].update(phase='PartiallyFailed'),
                     lambda b: b['status'].update(expiration=NOW.isoformat()),
                     lambda b: b['status']['progress'].update(itemsBackedUp=True),
                     lambda b: b['status']['progress'].update(totalItems=3)]
        for change in mutations:
            b = backup(); change(b)
            with self.assertRaises(mod.InventoryUnavailable):
                mod.validate_backup(b, NOW)
        mutations = [lambda d: d['metadata'].update(name='other'),
                     lambda d: d['spec']['target'].update(kind='BackupContents'),
                     lambda d: d['spec']['target'].update(name='other'),
                     lambda d: d['status'].update(phase='New'),
                     lambda d: d['status'].update(expiration=NOW.isoformat()),
                     lambda d: d['status'].update(expiration=(NOW+timedelta(hours=2)).isoformat())]
        for change in mutations:
            d = download(); change(d)
            with self.assertRaises(mod.InventoryUnavailable):
                mod.validate_request(d, NOW)

    def test_only_completed_filesystem_backups_without_async_finalization_qualify(self):
        value = backup()
        value['spec'] = {'snapshotVolumes': False, 'snapshotMoveData': False}
        for key in ['backupItemOperationsAttempted', 'backupItemOperationsCompleted', 'backupItemOperationsFailed', 'errors']:
            for invalid in [True, -1, 1, '0', None]:
                candidate = deepcopy(value)
                candidate['status'][key] = invalid
                with self.subTest(key=key, invalid=invalid), self.assertRaises(mod.InventoryUnavailable):
                    mod.validate_backup(candidate, NOW)
        for key in ['snapshotVolumes', 'snapshotMoveData']:
            for invalid in [True, None, 'false', 0]:
                candidate = deepcopy(value)
                candidate['spec'][key] = invalid
                with self.subTest(key=key, invalid=invalid), self.assertRaises(mod.InventoryUnavailable):
                    mod.validate_backup(candidate, NOW)

    def test_only_exact_resource_list_endpoint_is_allowed(self):
        for url in [URL.replace('https:', 'http:'), URL.replace('192.168.1.225', 'example.com'),
                    URL.replace(':7070', ':443'), URL.replace('resource-list.json.gz', 'backup.tar.gz'),
                    URL.replace('/velero/', '/other/'), URL.replace('https://', 'https://user@'),
                    URL + '#fragment', URL.replace('/backups/', '/x/../backups/')]:
            d = download(); d['status']['downloadURL'] = url
            with self.subTest(url=url), self.assertRaises(mod.InventoryUnavailable):
                mod.validate_request(d, NOW)

    def test_acquisition_separates_api_auth_from_backup_fetch_and_rechecks_counts(self):
        raw = gzip.compress(json.dumps({'v1/Pod': ['a/b'], 'v1/Secret': [SECRET]}).encode())
        with patch.object(mod, 'EXPECTED_ITEMS', 2), patch.object(mod, 'REMOVE_BY', datetime.max.replace(tzinfo=timezone.utc)), \
             patch.object(mod, 'validate_request', return_value=URL), patch.object(mod, 'api', side_effect=[backup(), download(), backup()]) as api, \
             patch.object(mod, 'fetch', return_value=raw) as fetch, patch.object(mod.ssl, 'create_default_context') as context:
            context.return_value = ssl.create_default_context()
            result = mod.acquire()
            self.assertEqual(result['item_count'], 2)
            self.assertEqual(api.call_count, 3)
            self.assertEqual(fetch.call_args.args, (URL,))
            self.assertNotIn('headers', fetch.call_args.kwargs)
            context.assert_called_with(cafile=mod.CA)
        changed = backup()
        changed['status']['progress'] = {'itemsBackedUp': 3, 'totalItems': 3}
        with patch.object(mod, 'EXPECTED_ITEMS', 2), patch.object(mod, 'REMOVE_BY', datetime.max.replace(tzinfo=timezone.utc)), \
             patch.object(mod, 'validate_request', return_value=URL), patch.object(mod, 'api', side_effect=[backup(), download(), changed]), \
             patch.object(mod, 'fetch', return_value=raw), patch.object(mod.ssl, 'create_default_context'), \
             self.assertRaises(mod.InventoryUnavailable):
            mod.acquire()

    def test_request_creation_race_waits_for_only_the_named_request(self):
        raw = gzip.compress(json.dumps({'v1/Pod': ['a/b'], 'v1/Secret': [SECRET]}).encode())
        with patch.object(mod, 'EXPECTED_ITEMS', 2), patch.object(mod, 'REMOVE_BY', datetime.max.replace(tzinfo=timezone.utc)), \
             patch.object(mod, 'validate_request', return_value=URL), patch.object(mod, 'api', side_effect=[backup(), HTTPError('api', 404, 'not found', {}, None), download(), backup()]) as api, \
             patch.object(mod, 'fetch', return_value=raw), patch.object(mod.ssl, 'create_default_context'), \
             patch.object(mod.time, 'sleep') as sleep:
            self.assertEqual(mod.acquire()['item_count'], 2)
            self.assertEqual(api.call_count, 4)
            sleep.assert_called_once_with(2)

    def test_redirects_are_refused_without_echoing_the_signed_url(self):
        with self.assertRaises(mod.InventoryUnavailable) as err:
            mod.NoRedirect().redirect_request(None, None, 302, 'moved', {}, URL)
        self.assertNotIn('never-emit-token', str(err.exception))

    def test_transport_errors_publish_only_a_generic_failure(self):
        for err in [ssl.SSLCertVerificationError(URL), HTTPError(URL, 500, URL, {}, io.BytesIO())]:
            with patch.object(mod, 'acquire', side_effect=err), patch.object(mod, 'publish') as publish, \
                 patch('sys.stdout', new_callable=io.StringIO) as stdout:
                self.assertEqual(mod.main(), 1)
                receipt = publish.call_args.args[0]
                self.assertEqual(receipt['status'], 'unavailable')
                self.assertNotIn('data_upload_count', receipt)
                self.assertNotIn('never-emit-token', json.dumps(receipt) + stdout.getvalue())


class ManifestTests(unittest.TestCase):
    def setUp(self):
        import yaml
        self.directory = PATH.parent
        self.docs = list(yaml.safe_load_all((self.directory / 'resources.yaml').read_text()))

    def one(self, kind, namespace=None):
        values = [d for d in self.docs if d['kind'] == kind and (namespace is None or d['metadata']['namespace'] == namespace)]
        self.assertEqual(len(values), 1)
        return values[0]

    def test_job_identity_is_the_only_recipient_of_exact_gets(self):
        role = self.one('Role', 'velero')
        self.assertEqual(role['rules'], [
            {'apiGroups': ['velero.io'], 'resources': ['backups'], 'resourceNames': [mod.BACKUP], 'verbs': ['get']},
            {'apiGroups': ['velero.io'], 'resources': ['downloadrequests'], 'resourceNames': [mod.REQUEST], 'verbs': ['get']},
        ])
        self.assertEqual(self.one('Role', 'platform-edge')['rules'], [
            {'apiGroups': [''], 'resources': ['configmaps'], 'resourceNames': [mod.RESULT], 'verbs': ['patch']},
        ])
        for binding in [d for d in self.docs if d['kind'] == 'RoleBinding']:
            self.assertEqual(binding['subjects'], [{'kind': 'ServiceAccount', 'name': mod.REQUEST, 'namespace': 'velero'}])
        self.assertEqual(self.one('DownloadRequest')['spec'], {'target': {'kind': 'BackupResourceList', 'name': mod.BACKUP}})

    def test_job_is_restricted_without_backup_credentials_or_data_volumes(self):
        job = self.one('Job')
        self.assertEqual(job['metadata']['annotations']['kustomize.toolkit.fluxcd.io/force'], 'enabled')
        self.assertEqual(job['spec']['backoffLimit'], 0)
        self.assertLessEqual(job['spec']['activeDeadlineSeconds'], 300)
        self.assertNotIn('ttlSecondsAfterFinished', job['spec'])
        pod = job['spec']['template']['spec']
        self.assertEqual(pod['serviceAccountName'], mod.REQUEST)
        self.assertEqual(pod['restartPolicy'], 'Never')
        self.assertEqual(pod['securityContext']['runAsNonRoot'], True)
        self.assertEqual(len(pod['containers']), 1)
        container = pod['containers'][0]
        self.assertIn('@sha256:', container['image'])
        self.assertFalse(container['securityContext']['allowPrivilegeEscalation'])
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
        self.assertEqual(container['securityContext']['capabilities']['drop'], ['ALL'])
        self.assertTrue(all(set(v) == {'name', 'configMap'} for v in pod['volumes']))
        self.assertNotIn('envFrom', container)
        self.assertEqual(container['command'], ['python3', '-B', '/script/inventory.py'])

    def test_network_only_admits_backup_host_and_api_server(self):
        spec = self.one('CiliumNetworkPolicy')['spec']
        self.assertEqual(spec['ingress'], [])
        self.assertEqual(spec['egress'], [
            {'toEntities': ['kube-apiserver'], 'toPorts': [{'ports': [{'port': '443', 'protocol': 'TCP'}, {'port': '6443', 'protocol': 'TCP'}]}]},
            {'toCIDR': ['192.168.1.225/32'], 'toPorts': [{'ports': [{'port': '7070', 'protocol': 'TCP'}]}]},
        ])
        selector = spec['endpointSelector']['matchLabels']
        self.assertEqual(selector, self.one('Job')['spec']['template']['metadata']['labels'])

    def test_public_trust_matches_the_existing_committed_backup_trust(self):
        import yaml
        original = yaml.safe_load((ROOT / 'kubernetes/apps/backup/talos-backup/versitygw-ca.yaml').read_text())['data']['ca.pem']
        self.assertEqual((self.directory / 'ca.pem').read_text(), original)
        self.assertEqual(hashlib.sha256(ssl.PEM_cert_to_DER_cert(original)).hexdigest(),
                         '678b7e6a983e28dcc8c939e92abee0411eae606aad1bc7dc7e4e076aefeddf10')

    def test_job_generated_script_and_trust_are_content_addressed(self):
        import yaml
        cfg = yaml.safe_load((self.directory / 'kustomization.yaml').read_text())
        self.assertEqual(cfg['resources'], ['resources.yaml'])
        generators = cfg['configMapGenerator']
        self.assertEqual([g['files'] for g in generators], [['inventory.py'], ['ca.pem']])
        self.assertTrue(all(g['namespace'] == 'velero' for g in generators))
        self.assertFalse(cfg.get('generatorOptions', {}).get('disableNameSuffixHash', False))


if __name__ == '__main__':
    unittest.main()
