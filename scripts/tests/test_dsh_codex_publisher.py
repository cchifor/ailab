"""The reviewer owns refresh; only the selected, unexpired access token may leave it."""
import base64
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('publisher', ROOT / 'ansible/roles/dsh_codex_publisher/files/publish.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def auth(email='realjaysage@gmail.com', expires=1000):
    payload = base64.urlsafe_b64encode(json.dumps({
        'https://api.openai.com/profile': {'email': email}, 'exp': expires,
    }).encode()).decode().rstrip('=')
    return {'tokens': {'access_token': 'header.' + payload + '.signature',
                       'refresh_token': 'never-publish-this', 'id_token': 'also-not-published'}}


class Projection(unittest.TestCase):
    def test_access_only(self):
        fields = publisher.project(auth(), 'realjaysage@gmail.com', 100)
        self.assertEqual(set(fields), {'DSH_CODEX_ACCESS_TOKEN', 'DSH_CODEX_ACCOUNT_EMAIL', 'DSH_CODEX_EXPIRES_AT'})
        self.assertNotIn('never-publish-this', json.dumps(fields))
        self.assertNotIn('also-not-published', json.dumps(fields))

    def test_wrong_account_is_refused(self):
        with self.assertRaises(ValueError):
            publisher.project(auth('other@example.com'), 'realjaysage@gmail.com', 100)

    def test_expired_and_nearly_expired_are_refused(self):
        for expiry in [50, 100, 400]:
            with self.subTest(expiry=expiry), self.assertRaises(ValueError):
                publisher.project(auth(expires=expiry), 'realjaysage@gmail.com', 100)

    def test_cas_patch_and_noop_then_rotation(self):
        calls = []
        version = 8

        def response(req, **kwargs):
            nonlocal version
            data = json.loads(req.data) if req.data else None
            calls.append((req.method, req.full_url, data))
            if req.full_url.endswith('/auth/approle/login'):
                result = {'auth': {'client_token': 'scoped-token'}}
            elif req.full_url.endswith('/metadata/dsh/credentials'):
                result = {'data': {'current_version': version, 'versions': {
                    str(version): {'destroyed': False, 'deletion_time': ''}}}}
            else:
                self.assertEqual(req.method, 'PATCH')
                self.assertEqual(data['options']['cas'], version)
                self.assertEqual(set(data['data']), {'DSH_CODEX_ACCESS_TOKEN', 'DSH_CODEX_ACCOUNT_EMAIL', 'DSH_CODEX_EXPIRES_AT'})
                version += 1
                result = {'data': {'version': version}}
            return io.BytesIO(json.dumps(result).encode())

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(publisher.ssl, 'create_default_context'), \
                patch.object(publisher.urllib.request, 'urlopen', side_effect=response):
            state = Path(directory) / 'state.json'
            config = {'address': 'https://vault.example'}
            fields = publisher.project(auth(), 'realjaysage@gmail.com', 100)
            publisher.publish(config, {}, fields, state)
            publisher.publish(config, {}, fields, state)
            rotated = publisher.project(auth(expires=2000), 'realjaysage@gmail.com', 100)
            publisher.publish(config, {}, rotated, state)
        writes = [call for call in calls if call[0] == 'PATCH']
        self.assertEqual(len(writes), 2)
        self.assertFalse(any(method == 'GET' and '/data/' in url for method, url, _ in calls))


if __name__ == '__main__':
    unittest.main()
