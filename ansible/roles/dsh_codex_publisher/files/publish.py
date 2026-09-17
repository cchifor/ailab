#!/usr/bin/env python3
"""Project one reviewer's access token into DSH's existing KV document. Never refresh OAuth."""
import base64
import hashlib
import json
import os
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.request

CONFIG_DIR = Path('/etc/dsh-codex-publisher')
STATE = Path('/var/lib/dsh-codex-publisher/state.json')


def project(auth, email, now):
    token = auth['tokens']['access_token']
    claims = json.loads(base64.urlsafe_b64decode(token.split('.')[1] + '==='))
    if claims.get('https://api.openai.com/profile', {}).get('email') != email:
        raise ValueError('reviewer account does not match configured identity')
    expires = int(claims['exp'])
    if expires <= now + 300:
        raise ValueError('reviewer access token is expired or expires within five minutes')
    # An explicit allowlist: neither refresh_token nor the auth.json document is published.
    return {'DSH_CODEX_ACCESS_TOKEN': token, 'DSH_CODEX_ACCOUNT_EMAIL': email,
            'DSH_CODEX_EXPIRES_AT': str(expires)}


def publish(config, approle, fields, state_path=STATE):
    tls = ssl.create_default_context(cafile=str(CONFIG_DIR / 'ca.crt'))

    def request(path, method='GET', data=None, token=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Vault-Token'] = token
        if method == 'PATCH':
            headers['Content-Type'] = 'application/merge-patch+json'
        req = urllib.request.Request(config['address'] + '/v1/' + path,
                                     data=None if data is None else json.dumps(data).encode(),
                                     headers=headers, method=method)
        with urllib.request.urlopen(req, context=tls, timeout=20) as response:
            return json.load(response)

    # Short-lived AppRole token; the persistent secret-id is root-readable only.
    login = request('auth/approle/login', 'POST', approle)
    token = login['auth']['client_token']
    metadata = request('af/metadata/dsh/credentials', token=token)['data']
    version = metadata['current_version']
    current = metadata['versions'][str(version)]
    if current['destroyed'] or current['deletion_time']:
        raise ValueError('DSH credential document is deleted; operator recovery required')
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state == {'digest': digest, 'version': version}:
        print('DSH Codex projection unchanged')
        return
    result = request('af/data/dsh/credentials', 'PATCH',
                     {'options': {'cas': version}, 'data': fields}, token)
    state = {'digest': digest, 'version': result['data']['version']}
    temp = state_path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as file:
        json.dump(state, file)
    os.replace(temp, state_path)
    print('DSH Codex projection updated; unrelated fields preserved')


def main():
    config = json.loads((CONFIG_DIR / 'config.json').read_text())
    auth = json.loads(Path(config['auth_path']).read_text())
    fields = project(auth, config['email'], time.time())
    approle = json.loads((CONFIG_DIR / 'approle.json').read_text())
    publish(config, approle, fields)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never include HTTP response bodies, auth documents or credential-bearing locals.
        detail = (' HTTP ' + str(error.code)) if isinstance(error, urllib.error.HTTPError) else ''
        print('DSH Codex projection failed: ' + type(error).__name__ + detail, flush=True)
        raise SystemExit(1)
