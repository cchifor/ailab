"""The reviewer owns refresh; only a selected, unexpired access token and its identity claims leave it."""
import base64
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('publisher', ROOT / 'ansible/roles/dsh_codex_publisher/files/publish.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)

ACCOUNT = '9c8a8cfb-1147-4e67-8131-4f91174e5ee7'
FIELDS = ('_ACCESS_TOKEN', '_ACCOUNT_ID', '_ACCOUNT_EMAIL', '_EXPIRES_AT')


def auth(email='realjaysage@gmail.com', expires=1000, account_id=ACCOUNT):
    claims = {'https://api.openai.com/profile': {'email': email}, 'exp': expires}
    if account_id:
        claims['https://api.openai.com/auth'] = {'chatgpt_account_id': account_id}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return {'tokens': {'access_token': 'header.' + payload + '.signature',
                       'refresh_token': 'never-publish-this', 'id_token': 'also-not-published'}}


def config(optional_d=True):
    return {'address': 'https://vault.example', 'projections': [
        {'auth_path': '/home/codexrun2/.codex/auth.json', 'email': 'realjaysage@gmail.com',
         'kv_path': 'dsh/credentials', 'prefix': 'DSH_CODEX'},
        {'auth_path': '/home/codexrun4/.codex/auth.json', 'email': 'realjaynesage@gmail.com',
         'kv_path': 'litellm/chatgpt', 'prefix': 'CHATGPT', 'optional': optional_d},
    ]}


def both_seats(path):
    return auth('realjaynesage@gmail.com') if 'codexrun4' in path else auth()


class Projection(unittest.TestCase):
    def test_access_only(self):
        fields = publisher.project(auth(), 'realjaysage@gmail.com', 100)
        self.assertEqual(set(fields), {'DSH_CODEX' + f for f in FIELDS})
        self.assertEqual(fields['DSH_CODEX_ACCOUNT_ID'], ACCOUNT)
        self.assertEqual(fields['DSH_CODEX_EXPIRES_AT'], '1000')
        self.assertNotIn('never-publish-this', json.dumps(fields))
        self.assertNotIn('also-not-published', json.dumps(fields))

    def test_prefix_names_the_consumer(self):
        fields = publisher.project(auth('realjaynesage@gmail.com'), 'realjaynesage@gmail.com', 100, 'CHATGPT')
        self.assertEqual(set(fields), {'CHATGPT' + f for f in FIELDS})

    def test_wrong_account_is_refused(self):
        with self.assertRaises(ValueError):
            publisher.project(auth('other@example.com'), 'realjaysage@gmail.com', 100)

    def test_expired_and_nearly_expired_are_refused(self):
        for expiry in [50, 100, 400]:
            with self.subTest(expiry=expiry), self.assertRaises(ValueError):
                publisher.project(auth(expires=expiry), 'realjaysage@gmail.com', 100)

    def test_missing_account_id_is_refused(self):
        with self.assertRaises(ValueError):
            publisher.project(auth(account_id=None), 'realjaysage@gmail.com', 100)

    def test_account_id_falls_back_to_the_auth_document(self):
        document = auth(account_id=None)
        document['tokens']['account_id'] = ACCOUNT
        fields = publisher.project(document, 'realjaysage@gmail.com', 100)
        self.assertEqual(fields['DSH_CODEX_ACCOUNT_ID'], ACCOUNT)


class FakeVault:
    """Answers the three requests the publisher makes; records every call; versions per path."""

    def __init__(self, test, fail_path=None, fail_status=403):
        self.test, self.calls, self.versions = test, [], {}
        self.fail_path, self.fail_status = fail_path, fail_status

    def __call__(self, req, **kwargs):
        data = json.loads(req.data) if req.data else None
        self.calls.append((req.method, req.full_url, data))
        url = req.full_url
        if url.endswith('/auth/approle/login'):
            result = {'auth': {'client_token': 'scoped-token'}}
        elif '/af/metadata/' in url:
            path = url.split('/af/metadata/', 1)[1]
            if path == self.fail_path:
                raise urllib.error.HTTPError(url, self.fail_status, 'forbidden', {}, io.BytesIO(b'{"errors":["secret body"]}'))
            version = self.versions.setdefault(path, 8)
            result = {'data': {'current_version': version, 'versions': {
                str(version): {'destroyed': False, 'deletion_time': ''}}}}
        else:
            self.test.assertEqual(req.method, 'PATCH')
            path = url.split('/af/data/', 1)[1]
            self.test.assertEqual(data['options']['cas'], self.versions[path])
            self.versions[path] += 1
            result = {'data': {'version': self.versions[path]}}
        return io.BytesIO(json.dumps(result).encode())

    def patches(self, path=None):
        return [c for c in self.calls if c[0] == 'PATCH' and (path is None or c[1].endswith('/af/data/' + path))]

    def logins(self):
        return sum(1 for c in self.calls if c[1].endswith('/auth/approle/login'))


class Publishing(unittest.TestCase):
    def setUp(self):
        self.vault = FakeVault(self)
        self.config = {'address': 'https://vault.example'}
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state.json'
        self.textfile = Path(self.tmp.name) / 'dsh-codex-publisher.prom'
        self.patches = [patch.object(publisher.ssl, 'create_default_context'),
                        patch.object(publisher.urllib.request, 'urlopen', side_effect=lambda req, **kw: self.vault(req, **kw))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _run(self, cfg, read_auth, now=100):
        with patch('builtins.print') as out:
            rc = publisher.run(cfg, {}, now, self.state, read_auth, str(self.textfile))
        return rc, ' '.join(str(c.args[0]) for c in out.call_args_list)

    def test_cas_patch_and_noop_then_rotation(self):
        session = publisher.login(self.config, {})
        fields = publisher.project(auth(), 'realjaysage@gmail.com', 100)
        publisher.publish(self.config, session, fields, 'dsh/credentials', self.state)
        publisher.publish(self.config, session, fields, 'dsh/credentials', self.state)
        rotated = publisher.project(auth(expires=2000), 'realjaysage@gmail.com', 100)
        publisher.publish(self.config, session, rotated, 'dsh/credentials', self.state)
        self.assertEqual(len(self.vault.patches()), 2)
        self.assertFalse(any(m == 'GET' and '/data/' in u for m, u, _ in self.vault.calls))
        self.assertEqual(set(self.vault.patches()[0][2]['data']), {'DSH_CODEX' + f for f in FIELDS})

    def test_state_is_kept_per_document(self):
        session = publisher.login(self.config, {})
        fields = publisher.project(auth(), 'realjaysage@gmail.com', 100)
        publisher.publish(self.config, session, fields, 'dsh/credentials', self.state)
        publisher.publish(self.config, session, fields, 'litellm/chatgpt', self.state)
        publisher.publish(self.config, session, fields, 'litellm/chatgpt', self.state)
        self.assertEqual(len(self.vault.patches('dsh/credentials')), 1)
        self.assertEqual(len(self.vault.patches('litellm/chatgpt')), 1)
        self.assertEqual(set(json.loads(self.state.read_text())), {'dsh/credentials', 'litellm/chatgpt'})

    def test_legacy_single_projection_state_is_forgotten_not_misread(self):
        self.state.write_text(json.dumps({'digest': 'old', 'version': 8}))
        session = publisher.login(self.config, {})
        fields = publisher.project(auth(), 'realjaysage@gmail.com', 100)
        publisher.publish(self.config, session, fields, 'dsh/credentials', self.state)
        self.assertEqual(len(self.vault.patches()), 1)

    # ---- run(): projection isolation -------------------------------------------------------
    def test_both_projections_publish_with_one_login_each(self):
        rc, lines = self._run(config(), both_seats)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.vault.patches()), 2)
        self.assertEqual(self.vault.logins(), 2, 'a 60 s AppRole token is never carried across projections')
        self.assertEqual(set(self.vault.patches('litellm/chatgpt')[0][2]['data']), {'CHATGPT' + f for f in FIELDS})
        self.assertNotIn('header.', lines)

    def test_absent_optional_seat_is_skipped_and_the_other_publishes(self):
        def read_auth(path):
            if 'codexrun4' in path:
                raise FileNotFoundError(path)
            return auth()

        rc, lines = self._run(config(), read_auth)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.vault.patches('dsh/credentials')), 1)
        self.assertEqual(len(self.vault.patches('litellm/chatgpt')), 0)
        self.assertIn('realjaynesage@gmail.com -> af/litellm/chatgpt skipped', lines)

    def test_absent_required_seat_is_a_failure(self):
        def read_auth(path):
            if 'codexrun4' in path:
                raise FileNotFoundError(path)
            return auth()

        rc, lines = self._run(config(optional_d=False), read_auth)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.vault.patches('dsh/credentials')), 1)
        self.assertIn('af/litellm/chatgpt failed: auth file absent and the projection is required', lines)

    def test_wrong_account_on_one_seat_fails_that_projection_only(self):
        rc, lines = self._run(config(), lambda p: auth('other@example.com') if 'codexrun2' in p else auth('realjaynesage@gmail.com'))
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.vault.patches('dsh/credentials')), 0)
        self.assertEqual(len(self.vault.patches('litellm/chatgpt')), 1)
        self.assertIn('realjaysage@gmail.com -> af/dsh/credentials failed: ValueError', lines)
        self.assertNotIn('never-publish-this', lines)
        self.assertNotIn('header.', lines)

    def test_malformed_and_unreadable_files_fail_even_when_optional(self):
        cases = {'malformed': lambda p: (_ for _ in ()).throw(json.JSONDecodeError('x', 'y', 0)) if 'codexrun4' in p else auth(),
                 'permission': lambda p: (_ for _ in ()).throw(PermissionError(p)) if 'codexrun4' in p else auth()}
        for name, read_auth in cases.items():
            with self.subTest(name):
                self.vault = FakeVault(self)
                rc, lines = self._run(config(), read_auth)
                self.assertEqual(rc, 1)
                self.assertIn('af/litellm/chatgpt failed:', lines)
                self.assertEqual(len(self.vault.patches('dsh/credentials')), 1)

    def test_vault_error_on_one_document_is_logged_without_the_body(self):
        self.vault = FakeVault(self, fail_path='litellm/chatgpt', fail_status=403)
        rc, lines = self._run(config(), both_seats)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.vault.patches('dsh/credentials')), 1)
        self.assertIn('af/litellm/chatgpt failed: HTTPError HTTP 403', lines)
        self.assertNotIn('secret body', lines)

    # ---- metrics ---------------------------------------------------------------------------
    def test_textfile_reports_each_projection(self):
        rc, _ = self._run(config(), both_seats, now=100)
        self.assertEqual(rc, 0)
        text = self.textfile.read_text()
        self.assertIn('dsh_codex_projection_ok{document="dsh/credentials",email="realjaysage@gmail.com"} 1', text)
        self.assertIn('dsh_codex_projection_ok{document="litellm/chatgpt",email="realjaynesage@gmail.com"} 1', text)
        self.assertIn('dsh_codex_projection_optional{document="litellm/chatgpt"} 1', text)
        self.assertIn('dsh_codex_projection_optional{document="dsh/credentials"} 0', text)
        self.assertIn('dsh_codex_projection_token_expires_at_seconds{document="dsh/credentials"} 1000', text)
        self.assertIn('dsh_codex_projection_last_success_timestamp_seconds{document="litellm/chatgpt"} 100', text)
        self.assertIn('dsh_codex_publisher_last_run_timestamp_seconds 100', text)
        self.assertNotIn('header.', text)

    def test_textfile_keeps_last_success_and_expiry_across_a_failing_run(self):
        self._run(config(), both_seats, now=100)
        self.vault = FakeVault(self, fail_path='dsh/credentials', fail_status=500)
        rc, _ = self._run(config(), both_seats, now=200)
        self.assertEqual(rc, 1)
        text = self.textfile.read_text()
        self.assertIn('dsh_codex_projection_ok{document="dsh/credentials",email="realjaysage@gmail.com"} 0', text)
        self.assertIn('dsh_codex_projection_last_success_timestamp_seconds{document="dsh/credentials"} 100', text)
        self.assertIn('dsh_codex_projection_token_expires_at_seconds{document="dsh/credentials"} 1000', text)
        self.assertIn('dsh_codex_projection_last_success_timestamp_seconds{document="litellm/chatgpt"} 200', text)

    def test_three_identical_runs_patch_each_document_once(self):
        # THE full run() cycle, not publish() alone: the freshness fields _carry_last_success
        # stamps on the state entry must not turn every minute into a new KV version (codex
        # impl-review round 1 found six PATCHes where two were due).
        for now in (100, 160, 220):
            rc, _ = self._run(config(), both_seats, now=now)
            self.assertEqual(rc, 0)
        self.assertEqual(len(self.vault.patches('dsh/credentials')), 1)
        self.assertEqual(len(self.vault.patches('litellm/chatgpt')), 1)
        # ...and a genuinely rotated token still publishes.
        rc, _ = self._run(config(), lambda p: auth(expires=5000) if 'codexrun2' in p else auth('realjaynesage@gmail.com'), now=280)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.vault.patches('dsh/credentials')), 2)
        self.assertEqual(len(self.vault.patches('litellm/chatgpt')), 1)

    def test_atomic_write_uses_a_private_temp_and_leaves_nothing_behind(self):
        target = Path(self.tmp.name) / 'out.prom'
        publisher._write_atomic(target, 'x\n', 0o644)
        self.assertEqual(target.read_text(), 'x\n')
        leftovers = [p.name for p in Path(self.tmp.name).iterdir() if p.name.endswith('.tmp')]
        self.assertEqual(leftovers, [])
        if hasattr(publisher.os, 'fchmod') and publisher.os.name == 'posix':
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        # A pre-positioned file at the OLD predictable temp name is never opened, truncated or chmodded.
        decoy = Path(self.tmp.name) / 'out.prom.tmp'
        decoy.write_text('decoy')
        publisher._write_atomic(target, 'y\n', 0o644)
        self.assertEqual(decoy.read_text(), 'decoy')
        self.assertEqual(target.read_text(), 'y\n')

    def test_textfile_expiry_is_the_published_tokens_not_the_locally_renewed_one(self):
        # Run 1 publishes a token expiring at 1000. Run 2 reads a RENEWED token (2000) but the
        # vault refuses the PATCH: the consumers still hold the 1000 token, and the metric must
        # say so -- reporting 2000 would silence CodexProjectionStale on a projection that never
        # reached anyone (workflow verify, PR 2).
        self._run(config(), both_seats, now=100)
        self.vault = FakeVault(self, fail_path='dsh/credentials', fail_status=500)
        rc, _ = self._run(config(), lambda p: auth(expires=2000) if 'codexrun2' in p else auth('realjaynesage@gmail.com'), now=200)
        self.assertEqual(rc, 1)
        text = self.textfile.read_text()
        self.assertIn('dsh_codex_projection_token_expires_at_seconds{document="dsh/credentials"} 1000', text)
        self.assertNotIn('dsh_codex_projection_token_expires_at_seconds{document="dsh/credentials"} 2000', text)

    def test_textfile_for_a_skipped_optional_seat_reads_not_ok_but_optional(self):
        def read_auth(path):
            if 'codexrun4' in path:
                raise FileNotFoundError(path)
            return auth()

        self._run(config(), read_auth, now=100)
        text = self.textfile.read_text()
        self.assertIn('dsh_codex_projection_ok{document="litellm/chatgpt",email="realjaynesage@gmail.com"} 0', text)
        self.assertIn('dsh_codex_projection_optional{document="litellm/chatgpt"} 1', text)
        self.assertNotIn('dsh_codex_projection_last_success_timestamp_seconds{document="litellm/chatgpt"}', text)


if __name__ == '__main__':
    unittest.main()
