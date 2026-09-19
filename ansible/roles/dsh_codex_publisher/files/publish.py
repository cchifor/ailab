#!/usr/bin/env python3
"""Project reviewer Codex access tokens into their consumers' KV documents. Never refresh OAuth.

One PROJECTION per reviewer seat: the seat's `~/.codex/auth.json` (the OAuth owner -- the Codex CLI
running as that seat is the only thing that ever refreshes it) is read, its access token and the
identity claims in that token are PATCHed into one KV-v2 document under a field prefix, and the
refresh token, the id token and the auth document itself never leave the host. Two projections
exist today (host_vars/reviewer-2.yml): seat b -> af/dsh/credentials as DSH_CODEX_*, read by dsh's
native openai-codex provider; seat d -> af/litellm/chatgpt as CHATGPT_*, rendered by ESO into the
auth file LiteLLM's `chatgpt/` provider reads (ADR 0026).

Projections are independent, and each one logs in on its own: the AppRole token lives 60 s, so a
token is never carried from one projection into the next. A seat whose auth file does not exist yet
is a logged skip ONLY when the projection is `optional` (a staged seat, not logged in); for a required
projection absence is a failure like any other, because "the service ran green while the token aged"
is the silent failure this file exists to prevent. Any other failure -- unreadable or malformed file,
wrong account, expiring token, vault error -- is logged without the secret, and the run exits 1 once
every projection has had its turn.

Per-projection freshness is exported through the node_exporter textfile collector (config `textfile`),
beside reviewbot's own metrics, so a reviewer login that refreshes fine while the publisher, ESO or
kubelet fails downstream is a distinct, alertable signal (CodexProjectionStale):
    dsh_codex_projection_ok{document,email}                        1 published/unchanged, 0 otherwise
    dsh_codex_projection_optional{document}                        1 when absence is tolerated
    dsh_codex_projection_token_expires_at_seconds{document}        exp of the last token published
    dsh_codex_projection_last_success_timestamp_seconds{document}  last run that published or confirmed
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import ssl
import tempfile
import time
import urllib.error
import urllib.request

CONFIG_DIR = Path('/etc/dsh-codex-publisher')
STATE = Path('/var/lib/dsh-codex-publisher/state.json')
PROFILE_CLAIM = 'https://api.openai.com/profile'
AUTH_CLAIM = 'https://api.openai.com/auth'
HTTP_TIMEOUT_S = 20   # x3 operations x N projections must fit the unit's TimeoutStartSec


class SeatAbsent(Exception):
    """The seat's auth file is not there: provisioned but not logged in yet."""


def project(auth, email, now, prefix='DSH_CODEX'):
    """The four fields one projection publishes, or a ValueError naming why it must not."""
    token = auth['tokens']['access_token']
    claims = json.loads(base64.urlsafe_b64decode(token.split('.')[1] + '==='))
    if claims.get(PROFILE_CLAIM, {}).get('email') != email:
        raise ValueError('reviewer account does not match configured identity')
    expires = int(claims['exp'])
    if expires <= now + 300:
        raise ValueError('reviewer access token is expired or expires within five minutes')
    # The ChatGPT account id is a JWT claim, not a secret: LiteLLM's auth file wants it beside the
    # token (it derives it from the token otherwise, then tries to WRITE the file to cache it --
    # which a read-only Secret mount refuses on every request).
    account_id = claims.get(AUTH_CLAIM, {}).get('chatgpt_account_id') or auth['tokens'].get('account_id')
    if not account_id:
        raise ValueError('reviewer access token carries no ChatGPT account id')
    # An explicit allowlist: neither refresh_token nor id_token nor the auth document is published.
    return {prefix + '_ACCESS_TOKEN': token, prefix + '_ACCOUNT_ID': str(account_id),
            prefix + '_ACCOUNT_EMAIL': email, prefix + '_EXPIRES_AT': str(expires)}


def _request(config, tls, path, method='GET', data=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-Vault-Token'] = token
    if method == 'PATCH':
        headers['Content-Type'] = 'application/merge-patch+json'
    req = urllib.request.Request(config['address'] + '/v1/' + path,
                                 data=None if data is None else json.dumps(data).encode(),
                                 headers=headers, method=method)
    with urllib.request.urlopen(req, context=tls, timeout=HTTP_TIMEOUT_S) as response:
        return json.load(response)


def login(config, approle):
    """A short-lived AppRole token (ttl 60s); the persistent secret-id is root-readable only."""
    tls = ssl.create_default_context(cafile=str(CONFIG_DIR / 'ca.crt'))
    return tls, _request(config, tls, 'auth/approle/login', 'POST', approle)['auth']['client_token']


def _read_state(state_path):
    if not state_path.exists():
        return {}
    state = json.loads(state_path.read_text())
    # The single-projection state was a bare {digest, version}; it belonged to af/dsh/credentials
    # and costs at most one redundant PATCH to forget.
    return state if all(isinstance(value, dict) for value in state.values()) else {}


def _write_atomic(path, text, mode=0o600):
    """Write `text` to `path` through a private temp file and one rename.

    This runs as root, and the textfile's directory is the node_exporter collector dir, which is
    GROUP-writable (0775, group = the reviewbot user) so reviewbot can write its own metrics
    there. A predictable temp name opened with O_CREAT|O_TRUNC follows a symlink somebody in that
    group pre-positioned, and a pathname chmod follows it again (codex impl-review round 1). So:
    a fresh, unpredictable name (mkstemp: O_CREAT|O_EXCL|O_NOFOLLOW, 0600), the mode set on the
    DESCRIPTOR (fchmod -- explicit because the unit's UMask=0077 would otherwise leave a 0600 file
    node_exporter cannot read), then rename, which is atomic for readers. A leftover temp from a
    crash would never be touched again (mkstemp names are fresh every run), so earlier orphans of
    THIS target are swept before writing (#792 review)."""
    for orphan in path.parent.glob(path.name + '.*.tmp'):
        try:
            orphan.unlink()
        except OSError:
            pass
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as file:
            os.fchmod(fd, mode)
            file.write(text)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def publish(config, session, fields, kv_path='dsh/credentials', state_path=STATE):
    """CAS-PATCH `fields` into af/<kv_path>; a no-op when neither the fields nor the version moved."""
    tls, token = session
    metadata = _request(config, tls, 'af/metadata/' + kv_path, token=token)['data']
    version = metadata['current_version']
    current = metadata['versions'][str(version)]
    if current['destroyed'] or current['deletion_time']:
        raise ValueError('credential document af/' + kv_path + ' is deleted; operator recovery required')
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
    state = _read_state(state_path)
    entry = state.get(kv_path) or {}
    # ONLY the digest and the version decide "unchanged". The same entry also carries the
    # freshness fields _carry_last_success stamps every run (last_success, expires_at); comparing
    # the whole entry made every run a mismatch and wrote a new KV version per minute (codex
    # impl-review round 1) -- 1,440 versions a day of an identical token, eating the retained
    # history the metadata read depends on.
    if entry.get('digest') == digest and entry.get('version') == version:
        print('projection af/' + kv_path + ' unchanged')
        return
    result = _request(config, tls, 'af/data/' + kv_path, 'PATCH',
                      {'options': {'cas': version}, 'data': fields}, token)
    entry.update({'digest': digest, 'version': result['data']['version']})
    state[kv_path] = entry
    _write_atomic(state_path, json.dumps(state))
    print('projection af/' + kv_path + ' updated; unrelated fields preserved')


def _label(text):
    # The exposition format escapes backslash, double quote AND newline in label values; an
    # unescaped newline (a folded-scalar typo in host_vars) would make node_exporter reject the
    # WHOLE textfile, surfacing only as CodexPublisherMetricsMissing 30 min later (#792 review).
    return str(text).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def render_metrics(results, now):
    """The textfile body for one run; `results` is a list of dicts from run()."""
    lines = ['# HELP dsh_codex_projection_ok 1 when the projection was published or confirmed unchanged this run.',
             '# TYPE dsh_codex_projection_ok gauge']
    for r in results:
        lines.append('dsh_codex_projection_ok{document="%s",email="%s"} %d'
                     % (_label(r['document']), _label(r['email']), 1 if r['ok'] else 0))
    lines += ['# HELP dsh_codex_projection_optional 1 when an absent auth file is tolerated (a staged seat).',
              '# TYPE dsh_codex_projection_optional gauge']
    for r in results:
        lines.append('dsh_codex_projection_optional{document="%s"} %d' % (_label(r['document']), 1 if r['optional'] else 0))
    lines += ['# HELP dsh_codex_projection_token_expires_at_seconds exp claim of the last token this projection published.',
              '# TYPE dsh_codex_projection_token_expires_at_seconds gauge']
    for r in results:
        if r.get('expires_at') is not None:
            lines.append('dsh_codex_projection_token_expires_at_seconds{document="%s"} %d' % (_label(r['document']), r['expires_at']))
    lines += ['# HELP dsh_codex_projection_last_success_timestamp_seconds last run that published or confirmed this projection.',
              '# TYPE dsh_codex_projection_last_success_timestamp_seconds gauge']
    for r in results:
        if r.get('last_success') is not None:
            lines.append('dsh_codex_projection_last_success_timestamp_seconds{document="%s"} %d' % (_label(r['document']), r['last_success']))
    lines.append('dsh_codex_publisher_last_run_timestamp_seconds %d' % now)
    return '\n'.join(lines) + '\n'


def run(config, approle, now, state_path=STATE, read_auth=None, textfile=None):
    """Every projection in turn; 0 when all published or (optionally) skipped, 1 when any failed."""
    read_auth = read_auth or (lambda path: json.loads(Path(path).read_text()))
    failures = 0
    results = []
    for projection in config['projections']:
        document, email = projection['kv_path'], projection['email']
        optional = bool(projection.get('optional', False))
        label = email + ' -> af/' + document
        result = {'document': document, 'email': email, 'optional': optional, 'ok': False,
                  'expires_at': None, 'last_success': None}
        try:
            try:
                auth = read_auth(projection['auth_path'])
            except FileNotFoundError:
                raise SeatAbsent()
            fields = project(auth, email, now, projection.get('prefix', 'DSH_CODEX'))
            publish(config, login(config, approle), fields, document, state_path)
            # Recorded only AFTER the vault accepted it (or confirmed it unchanged): the metric is
            # the exp of the token the CONSUMERS have, so a run that read a freshly renewed token
            # and then failed to publish it must keep reporting the previous, still-served expiry.
            result['expires_at'] = int(fields[projection.get('prefix', 'DSH_CODEX') + '_EXPIRES_AT'])
            result['ok'] = True
            result['last_success'] = int(now)
        except SeatAbsent:
            if optional:
                print('projection ' + label + ' skipped: auth file absent (seat not logged in yet)')
            else:
                print('projection ' + label + ' failed: auth file absent and the projection is required', flush=True)
                failures += 1
        except Exception as error:
            # Never include HTTP response bodies, auth documents or credential-bearing locals.
            detail = (' HTTP ' + str(error.code)) if isinstance(error, urllib.error.HTTPError) else ''
            print('projection ' + label + ' failed: ' + type(error).__name__ + detail, flush=True)
            failures += 1
        results.append(result)
    _carry_last_success(results, state_path)
    if textfile:
        _write_atomic(Path(textfile), render_metrics(results, now), 0o644)
    return 1 if failures else 0


def _carry_last_success(results, state_path):
    """Remember the last success per document in the state file, so a failing run still reports
    WHEN the document was last good rather than dropping the series."""
    try:
        state = _read_state(state_path)
    except (OSError, ValueError):
        state = {}
    for r in results:
        entry = state.setdefault(r['document'], {})
        if r['ok']:
            entry['last_success'] = r['last_success']
            entry['expires_at'] = r['expires_at']
        else:
            r['last_success'] = entry.get('last_success')
            if r['expires_at'] is None:
                r['expires_at'] = entry.get('expires_at')
    try:
        _write_atomic(state_path, json.dumps(state))
    except OSError:
        pass


def main():
    config = json.loads((CONFIG_DIR / 'config.json').read_text())
    approle = json.loads((CONFIG_DIR / 'approle.json').read_text())
    raise SystemExit(run(config, approle, time.time(), textfile=config.get('textfile')))


if __name__ == '__main__':
    main()
