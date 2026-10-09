#!/usr/bin/env python3
"""Qualify rendered AppRole/policy config against an in-memory TLS OpenBao.

Run with a built Relay renderer via RELAY_RENDERER_BIN and pinned BAO_BIN.
Requires Jinja2. All identities/material are disposable; no estate access.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / 'ansible/roles/relay_router_renderer/templates'
BAO = os.environ.get('BAO_BIN', '/usr/local/bin/bao')
RENDERER = os.environ['RELAY_RENDERER_BIN']
assert Path(BAO).is_absolute() and Path(RENDERER).is_absolute()
assert 'OpenBao v2.5.5 ' in subprocess.check_output([BAO, 'version'], text=True)
environment = Environment(loader=FileSystemLoader(TEMPLATES), undefined=StrictUndefined)
environment.filters['to_json'] = json.dumps
environment.filters['to_nice_json'] = lambda value: json.dumps(value, indent=4)


def put(path, data):
    path.write_text(data)
    path.chmod(0o600)


def eventually(fn, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = fn()
            if value:
                return value
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    raise AssertionError('Disposable fixture did not become ready')


with tempfile.TemporaryDirectory(prefix='relay-bao-qualification-') as temporary:
    root = Path(temporary)
    for name in ['tls', 'config', 'config/auth', 'sink', 'rendered']:
        (root / name).mkdir(mode=0o700)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    origin = f'https://127.0.0.1:{port}'
    root_token = 'fixture-root-' + uuid.uuid4().hex
    processes = []
    logs = []
    # Minimal environment: never inherit a real token, proxy, CLI config or token helper.
    process_env = {'PATH': os.defpath, 'TMPDIR': str(root), 'GOMAXPROCS': '2'}

    def start(args, name):
        log = (root / name).open('wb')
        logs.append(log)
        process = subprocess.Popen(args, stdout=log, stderr=log, env=process_env)
        processes.append(process)
        return process

    try:
        start([BAO, 'server', '-dev', '-dev-tls', '-dev-no-store-token',
               '-dev-tls-cert-dir=' + str(root / 'tls'), '-dev-listen-address=127.0.0.1:' + str(port),
               '-dev-root-token-id=' + root_token], 'server.log')
        ca = root / 'tls/vault-ca.pem'
        eventually(lambda: ca.exists())
        context = ssl.create_default_context(cafile=str(ca))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))

        def api(method, path, body=None, token=root_token, status=200):
            headers = {'X-Vault-Token': token, 'Content-Type': 'application/json'}
            request = urllib.request.Request(origin + '/v1/' + path,
                data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
            try:
                with opener.open(request, timeout=3) as response:
                    code, data = response.status, response.read()
            except urllib.error.HTTPError as error:
                code, data = error.code, error.read()
            assert code == status, f'Fixture API status mismatch: expected {status}, got {code}'
            return json.loads(data) if data else None

        eventually(lambda: api('GET', 'sys/health'))
        tenant, host, credential, operation, binding, connection = [str(uuid.uuid4()) for _ in range(6)]
        values = dict(relay_router_bao_mount='secret', relay_router_bao_prefix='relay/router',
                      relay_router_tenant_id=tenant, relay_router_host_id=host,
                      relay_router_config_dir=str(root / 'config'), relay_router_uid=str(os.getuid()),
                      relay_router_bao_origin=origin)
        policy = environment.get_template('reader-policy.hcl.j2').render(**values)
        api('PUT', 'sys/policies/acl/relay-reader', dict(policy=policy), status=204)
        api('POST', 'sys/auth/approle', dict(type='approle'), status=204)
        api('POST', 'auth/approle/role/relay-reader', dict(token_policies=['relay-reader'], token_period='10s',
            secret_id_ttl='10m', secret_id_num_uses=0), status=204)
        role_id = api('GET', 'auth/approle/role/relay-reader/role-id')['data']['role_id']
        secret_id = api('POST', 'auth/approle/role/relay-reader/secret-id', {})['data']['secret_id']
        put(root / 'config/auth/role-id', role_id + '\n')
        put(root / 'config/auth/secret-id', secret_id + '\n')
        shutil.copyfile(ca, root / 'config/ca.pem')
        (root / 'config/ca.pem').chmod(0o600)
        sink = root / 'sink/token'
        # Only substitute the systemd-created runtime directory with our disposable directory.
        agent = environment.get_template('agent.hcl.j2').render(**values).replace(
            f'/run/user/{os.getuid()}/relay-router-auth/token', str(sink))
        put(root / 'config/auth/agent.hcl', agent)
        start([BAO, 'agent', '-config=' + str(root / 'config/auth/agent.hcl')], 'agent.log')
        reader = eventually(lambda: sink.read_text().strip())
        assert sink.stat().st_mode & 0o777 == 0o600
        assert sink.stat().st_uid == os.getuid()
        own = f'secret/data/relay/router/{tenant}/{host}/{credential}'
        material = 'lrk_fixture_canary_' + uuid.uuid4().hex
        api('POST', own, dict(data=dict(token=material, router_key_id='fixture-key', operation_id=operation)))
        api('POST', own, dict(data=dict(token='lrk_different_latest_' + uuid.uuid4().hex,
                                       router_key_id='different-key', operation_id=str(uuid.uuid4()))))
        assert api('GET', own + '?version=1', token=reader)['data']['data']['token'] == material
        for method, path, data in [
            ('GET', own.replace(host, str(uuid.uuid4())), None),
            ('GET', own.replace(tenant, str(uuid.uuid4())), None),
            ('LIST', own.replace('/data/', '/metadata/'), None),
            ('GET', own.replace('/data/', '/metadata/'), None),
            ('POST', own, dict(data=dict(token='forbidden-write'))),
            ('POST', own.replace('/data/', '/delete/'), dict(versions=[1])),
            ('POST', own.replace('/data/', '/destroy/'), dict(versions=[1])),
        ]:
            api(method, path, data, token=reader, status=403)

        reference = dict(credentialId=credential, generation=1, secretVersion=1,
                         keyId='fixture-key', creationOperationId=operation)
        destination = root / 'rendered/credential.json'
        config = dict(version=1, baoOrigin=origin, baoMount='secret', baoPrefix='relay/router',
            baoTokenFile=str(sink), baoCaFile=str(root / 'config/ca.pem'),
            router=dict(tenantId=tenant, hostId=host, executionIdentity='fixture', connectionId=connection,
                bindingId=binding, origin='https://router.example', credentialFile=str(destination),
                protocol='openai-chat', selection=dict(kind='route', alias='fixture')))
        put(root / 'config/renderer.json', json.dumps(config))

        def render(action='render', success=True):
            put(root / 'config/request.json', json.dumps(dict(version=1, action=action, reference=reference,
                                                            notAfterUnixMillis=int(time.time()*1000)+60000)))
            result = subprocess.run([RENDERER, '--config', str(root / 'config/renderer.json'), '--request',
                str(root / 'config/request.json')], capture_output=True, text=True, env=process_env, timeout=15)
            assert (result.returncode == 0) == success, 'Renderer outcome mismatch'
            for secret in [reader, material, secret_id, root_token]:
                assert secret not in result.stdout + result.stderr
            return result

        render()
        original = destination.read_bytes()
        assert json.loads(original)['token'] == material  # exact v1, even though v2 is latest
        assert destination.stat().st_mode & 0o777 == 0o600
        # Confirm the real agent renews a periodic token across its original TTL.
        time.sleep(11)
        api('GET', own + '?version=1', token=reader)
        assert sink.read_text().strip() == reader
        # Deleted v1 must not fall forward to latest v2 or overwrite a valid local file.
        api('POST', own.replace('/data/', '/delete/'), dict(versions=[1]), status=204)
        render(success=False)
        assert destination.read_bytes() == original
        # Cleanup is independent of authentication and CA availability.
        (root / 'config/ca.pem').unlink()
        sink.unlink()
        render('remove')
        assert not destination.exists()
        print('PASS: private TLS, AppRole renewal, scoped denials, exact-version rendering and offline cleanup')
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in logs:
            log.close()
