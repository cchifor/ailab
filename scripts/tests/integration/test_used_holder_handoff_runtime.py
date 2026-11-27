#!/usr/bin/env python3
"""Opt-in real holder/published Pi handoff qualification, entirely offline.

Requires Linux UID1000, a network-none container, an already built holder,
the verified published Pi2604 closure, and a Relay checkout with cached tsx.
No installation/build fallback. Systemd status alone is a simulated seam;
this does not qualify Ansible activation, owner authority, or a live router.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[3]
SOURCE = '2604c41b69c5d9c50a3324545a51f0bfa38435a0'
MANIFEST = 'd0bf7f9d44a2ae992bb1fbd87f7f2043c7d582ad563078bd13d713ebcd211a0d'
TOKEN = 'lrk_pi_synthetic_qualification_only'
SIDECAR = r"""
import {pathToFileURL} from 'node:url';
import {createInterface} from 'node:readline';
const source = process.env.RELAY_SOURCE;
const {verifyPiRuntime} = await import(pathToFileURL(source+'/packages/pi-launcher/integrity.ts'));
const manifest = await verifyPiRuntime(process.env.PUBLISHED_PI_ROOT, process.env.PI_MANIFEST);
if (manifest.sourceCommit !== process.env.PI_SOURCE) throw Error('Published source mismatch');
const {piServices} = await import(pathToFileURL(source+'/tests/fixtures/pi-native.ts'));
const f = await piServices();
console.log(JSON.stringify({origin:f.origin, verified:true}));
for await (const line of createInterface({input:process.stdin})) {
  const q=JSON.parse(line);
  if (q.type==='queue') f.outputs.push({content:q.content});
  else if(q.type!=='snapshot') throw Error('Unknown fixture control');
  console.log(JSON.stringify({requests:f.requests.length,calls:f.calls.length,failures:f.failures}));
}
await f.close();
"""


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    path.chmod(mode)


def identifier():
    return str(uuid.uuid4())


@unittest.skipUnless(os.environ.get('RELAY_USED_HANDOFF_RUNTIME') == '1',
                     'Explicit published-runtime qualification required')
class UsedHolderRuntime(unittest.TestCase):
    def test_used_state_is_preserved_and_new_holder_requires_fresh_delivery(self):
        self.assertEqual(os.getuid(), 1000)
        # Bound the whole disposable fixture, including closure verification.
        old_handler = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(
            TimeoutError('Handoff qualification exceeded 360 seconds')))
        signal.alarm(360)
        self.addCleanup(lambda: signal.signal(signal.SIGALRM, old_handler))
        self.addCleanup(lambda: signal.alarm(0))
        home = Path(tempfile.mkdtemp(prefix='uh-', dir='/tmp'))
        # Preserve bounded synthetic process diagnostics before tmpfs disappears.
        self.addCleanup(lambda: self.save_diagnostics(home))
        self.holders = []
        self.addCleanup(self.stop_all)
        relay = Path(os.environ['RELAY_SOURCE'])
        runtime = Path(os.environ['PUBLISHED_PI_ROOT'])
        binary = Path(os.environ['RELAY_HOLDER_BINARY'])
        self.assertEqual(digest(binary), os.environ['RELAY_HOLDER_SHA256'])
        self.assertEqual(digest(runtime / 'manifest.json'), MANIFEST)
        manifest = json.loads((runtime / 'manifest.json').read_text())
        self.assertEqual(manifest['sourceCommit'], SOURCE)
        # Directory/symlink manifest entries have no file digest. Required
        # executable artifacts below still require an explicit SHA entry.
        entries = {e['path']: e['sha256'] for e in manifest['entries'] if 'sha256' in e}
        helper_path = ROOT / 'scripts/prepare-used-holder-handoff.py'
        spec = importlib.util.spec_from_file_location('used_handoff', helper_path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        sidecar = home / 'fixture.mjs'
        write(sidecar, SIDECAR)
        stderr = open(home / 'fixture.stderr', 'w')
        self.addCleanup(stderr.close)
        self.service = subprocess.Popen(
            [os.environ.get('RELAY_FIXTURE_NODE', 'node'), '--import',
             str(relay / 'node_modules/tsx/dist/loader.mjs'), str(sidecar)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            env={**os.environ, 'PI_MANIFEST': MANIFEST, 'PI_SOURCE': SOURCE},
            text=True, bufsize=1)
        self.addCleanup(self.stop_service)
        service = self.read_service(150)
        self.assertTrue(service['verified'])
        state = home / '.local/state/relay-holder'
        state.mkdir(parents=True, mode=0o700)
        state.chmod(0o700)
        for name in ('profile-homes', 'workspaces'):
            (state / name).mkdir(mode=0o700)
        # Connector is not run here; its private lock inode is a fixture input.
        # The helper must still acquire it rather than bypassing either lock.
        write(state / 'connector-control.lock', '')
        profile_file = home / helper.FILES['profiles'][0]
        profiles, references = [], {}
        for profile_id in helper.PROFILES:
            credential_home = state / 'profile-homes' / profile_id
            workspace = state / 'workspaces' / profile_id
            for path in (credential_home, workspace):
                path.mkdir(parents=True, mode=0o700)
                path.chmod(0o700)
            write(workspace / 'acceptance-fixed.effects', 'unchanged-marker\n')
            authority = dict(tenantId=identifier(), hostId=identifier(),
                             executionIdentity='qualification', connectionId=identifier(),
                             bindingId=identifier())
            credential = home / '.local/state/relay/router-managed' / authority['bindingId'] / 'credential.json'
            reference = dict(credentialId=identifier(), generation=1, secretVersion=1,
                             keyId='synthetic-old-' + profile_id, creationOperationId=identifier())
            references[profile_id] = reference
            write(credential, dict(version=1, **authority, reference=reference, token=TOKEN))
            artifacts = {str(runtime / p): entries[p] for p in
                         ('relay-pi.mjs', 'pi-worker.mjs', 'relay-pi-policy.mjs',
                          'relay-agent-tools.mjs', 'bin/pi', 'bin/npm')}
            artifacts[str(runtime / 'manifest.json')] = MANIFEST
            profiles.append(dict(version=1, id=profile_id, revision=1,
                executable=str(runtime / 'node'), executableSha256=entries['node'],
                artifacts=artifacts, args=[], environment={}, executionUid=1000,
                credentialHome=str(credential_home), roots=[str(workspace)], maxRequestSeconds=30,
                qualification='protocol-fixture', native=dict(kind='pi', runtimeDirectory=str(runtime),
                    contextWindow=65536, maxTokens=1024),
                router=dict(**authority, origin=service['origin'], credentialFile=str(credential),
                    protocol='openai-chat', selection=dict(kind='model', model='qualification')),
                communication=dict(executable=str(runtime / 'node'),
                    args=[str(runtime / 'relay-agent-tools.mjs')], origin=service['origin'])))
        write(profile_file, profiles)
        write(home / helper.FILES['service'][0], dict(version=1, binary=str(binary),
              binarySha256=digest(binary), stateDirectory=str(state), profilesFile=str(profile_file)))
        for name, flag in zip(helper.UNITS, ('--state-dir', '--holder-dir')):
            write(home / helper.FILES[name][0], '[Service]\nKillMode=control-group\nExecStart=' +
                  str(binary) + ' --qualification ' + flag + ' ' + str(state) +
                  ' --profiles ' + str(profile_file) + '\n', 0o644)
        hello = self.start_holder(binary, state, profile_file, home / 'old.stderr')
        self.assertTrue(hello['nativeStateReady'])
        self.assertEqual(len(hello['profiles']), 3)
        profile = profiles[0]
        profile_digest = next(p['digest'] for p in hello['profiles'] if p['id'] == profile['id'])
        source_session = identifier()
        self.install(state, profile, profile_digest, source_session, references[profile['id']], service['origin'])
        self.assertEqual(self.command(state, self.open_action(profile, profile_digest, source_session,
                         references[profile['id']]))['status'], 'completed')
        self.turn(state, source_session, 'Old retained synthetic answer')
        self.assertEqual(self.command(state, dict(type='close', sessionId=source_session, generation=1))['status'], 'completed')
        deadline = time.monotonic() + 30
        while self.ipc(state, dict(type='hello'))['sessions']:
            if time.monotonic() >= deadline:
                raise TimeoutError('Old holder did not drain after Close')
            time.sleep(.05)
        paused = self.ipc(state, dict(type='set-admission', operationId=identifier(),
                                    expectedRevision=hello['admission']['revision'], paused=True))
        seal = identifier()
        self.ipc(state, dict(type='set-maintenance-seal', operationId=seal,
            expectedRevision=paused['revision'], expectedIncarnation=hello['incarnation'], sealId=seal, sealed=True))
        self.stop(self.holders[-1])
        journal = state / 'journal.jsonl'
        first = json.loads(journal.read_text().splitlines()[0])
        proof = dict(sha256=digest(journal), journalId=first['incarnation'],
                     incarnation=hello['incarnation'], sealId=seal)
        saved = {name: helper.tree_snapshot(state / name, 1000) for name in
                 ('native-homes', 'tool-bindings', 'profile-homes', 'workspaces')}
        self.assertGreater(saved['native-homes']['entries'], 1)
        request = dict(version=1, operationId=identifier(), journal=proof,
                       expectedFiles={key: digest(home / rel) for key, (rel, _) in helper.FILES.items()})

        def stopped_status(name):
            fragment = home / helper.FILES[name][0]
            present = fragment.exists()
            return dict(Id=name, ActiveState='inactive', SubState='dead', MainPID='0',
                        ControlGroup='', UnitFileState='disabled' if present else '',
                        FragmentPath=str(fragment) if present else '', DropInPaths='',
                        LoadState='loaded' if present else 'not-found')

        prepared = helper.prepare(request, home=home, uid=1000, status=stopped_status)
        new = Path(prepared['newState'])
        self.assertEqual(list(new.iterdir()), [])
        self.assertEqual(digest(profile_file), request['expectedFiles']['profiles'])
        fresh = self.start_holder(binary, new, profile_file, home / 'new.stderr')
        self.assertTrue(fresh['nativeStateReady'])
        self.assertEqual(fresh['sessions'], [])
        self.assertNotEqual(fresh['incarnation'], hello['incarnation'])
        fresh_journal = json.loads((new / 'journal.jsonl').read_text().splitlines()[0])
        self.assertNotEqual(fresh_journal['incarnation'], first['incarnation'])
        # Hello enumerates a HashMap; stable identity/digest is the contract,
        # not iteration order across independent holder processes.
        self.assertEqual(sorted(fresh['profiles'], key=lambda p: p['id']),
                         sorted(hello['profiles'], key=lambda p: p['id']))
        with self.assertRaisesRegex(RuntimeError, 'Holder rejected request'):
            self.command(new, self.open_action(profile, profile_digest, identifier(),
                         references[profile['id']]))
        self.assertEqual(self.fixture(dict(type='snapshot'))['requests'], 1)
        ref = dict(credentialId=identifier(), generation=2, secretVersion=1,
                   keyId='synthetic-new', creationOperationId=identifier())
        authority = {k: profile['router'][k] for k in
                     ('tenantId', 'hostId', 'executionIdentity', 'connectionId', 'bindingId')}
        write(Path(profile['router']['credentialFile']), dict(version=1, **authority, reference=ref, token=TOKEN))
        attempted = identifier()
        self.install(new, profile, profile_digest, attempted, ref, service['origin'])
        before = self.fixture(dict(type='snapshot'))['requests']
        action = self.open_action(profile, profile_digest, attempted, ref)
        action['resumeFrom'] = source_session
        with self.assertRaisesRegex(RuntimeError, 'Holder rejected request'):
            self.command(new, action)
        self.assertEqual(self.fixture(dict(type='snapshot'))['requests'], before)
        self.assertEqual(self.ipc(new, dict(type='hello'))['sessions'], [])
        fresh_session = identifier()
        self.install(new, profile, profile_digest, fresh_session, ref, service['origin'])
        opened = self.command(new, self.open_action(profile, profile_digest, fresh_session, ref))
        self.assertEqual(opened['status'], 'completed')
        self.assertEqual(opened['result']['nativeRecoveryVersion'], 2)
        self.turn(new, fresh_session, 'Fresh independently admitted synthetic answer')
        self.assertEqual(self.command(new, dict(type='close', sessionId=fresh_session, generation=1))['status'], 'completed')
        self.stop(self.holders[-1])
        self.assertEqual(digest(journal), proof['sha256'])
        for name, snapshot in saved.items():
            self.assertEqual(helper.tree_snapshot(state / name, 1000), snapshot, name)
        self.assertEqual(digest(profile_file), request['expectedFiles']['profiles'])
        final = self.fixture(dict(type='snapshot'))
        self.assertEqual(final, dict(requests=2, calls=0, failures=[]))
        evidence = dict(result='PASS', publishedPiSource=SOURCE, publishedPiManifest=MANIFEST,
            holderSha256=digest(binary), helperSha256=digest(helper_path), profileSha256=digest(profile_file),
            oldJournalSha256=proof['sha256'], preserved=saved, providerRequests=2,
            oldJournalId=first['incarnation'], newJournalId=fresh_journal['incarnation'],
            oldRecoveryProviderRequests=0, communicationCalls=0,
            limits=['Synthetic provider and systemd-status seam; no actual role/service activation.',
                    'Connector is not launched; its private lock inode is an explicit fixture input.',
                    'Fresh synthetic credential delivery; not live owner/managed-key authority.',
                    'Old root retained, not resumed; whole-holder recovery remains fenced.'])
        if os.environ.get('RELAY_HANDOFF_EVIDENCE'):
            write(Path(os.environ['RELAY_HANDOFF_EVIDENCE']), evidence)

    def read_service(self, timeout=15):
        with selectors.DefaultSelector() as selector:
            selector.register(self.service.stdout, selectors.EVENT_READ)
            if not selector.select(timeout):
                raise TimeoutError('Synthetic sidecar response timeout')
        line = self.service.stdout.readline()
        if not line:
            raise RuntimeError('Synthetic sidecar exited; inspect fixture.stderr')
        return json.loads(line)

    def fixture(self, value):
        self.service.stdin.write(json.dumps(value) + '\n')
        self.service.stdin.flush()
        return self.read_service()

    def ipc(self, state, action):
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(15)
            connection.connect(str(state / 'control.sock'))
            connection.sendall((json.dumps(dict(version=1, action=action)) + '\n').encode())
            data = b''
            while b'\n' not in data:
                part = connection.recv(65536)
                if not part or len(data) > 4 * 1024 * 1024:
                    raise RuntimeError('Invalid IPC response')
                data += part
        result = json.loads(data.split(b'\n')[0])
        if not result['ok']:
            raise RuntimeError('Holder rejected request')
        return result['result']

    def command(self, state, action):
        command = identifier()
        self.ipc(state, dict(**action, commandId=command))
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            result = self.ipc(state, dict(type='status', commandId=command))
            if result['status'] != 'accepted':
                return result
            time.sleep(.05)
        raise TimeoutError('Holder command timeout')

    def start_holder(self, binary, state, profiles, log):
        stream = open(log, 'w')
        self.addCleanup(stream.close)
        process = subprocess.Popen([str(binary), '--qualification', '--state-dir', str(state),
                                    '--profiles', str(profiles)], stdout=stream, stderr=stream)
        self.holders.append(process)
        started = time.monotonic()
        # Three profiles each verify the pinned executable/artifacts before IPC
        # exists; allow three single-profile startup budgets, still within the
        # unchanged 360-second whole-test bound.
        deadline = started + 90
        observed = started
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Holder exited; inspect private stderr')
            if time.monotonic() - observed >= 30:
                self.startup_observation(process, log, started, 'validating')
                observed = time.monotonic()
            try:
                hello = self.ipc(state, dict(type='hello'))
                self.startup_observation(process, log, started, 'ready')
                return hello
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(.05)
        self.startup_observation(process, log, started, 'timeout')
        raise TimeoutError('Holder startup timeout')

    @staticmethod
    def startup_observation(process, log, started, outcome):
        # Only synthetic process CPU/state, never argv/environment/credentials.
        fields = Path('/proc', str(process.pid), 'stat').read_text().rsplit(')', 1)[1].split()
        value = dict(outcome=outcome, elapsedSeconds=time.monotonic() - started,
                     state=fields[0], userTicks=int(fields[11]), systemTicks=int(fields[12]))
        path = log.with_suffix('.startup.json')
        previous = json.loads(path.read_text()) if path.exists() else []
        write(path, [*previous, value])

    def install(self, state, profile, digest_value, session, reference, origin):
        identity = dict(sessionId=session, profileId=profile['id'], profileDigest=digest_value)
        binding = dict(version=1, origin=origin, token='rmt_' + identifier() + '.' + identifier() + '.' + 'a' * 43,
                       expiresAt=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + 300)))
        for action in (dict(type='install-communication', **identity, binding=binding),
                       dict(type='install-router', **identity, reference=reference,
                            notAfterUnixMillis=int(time.time() * 1000) + 60000)):
            self.assertEqual(self.command(state, action)['status'], 'completed')

    @staticmethod
    def open_action(profile, digest_value, session, reference):
        return dict(type='open', sessionId=session, profileId=profile['id'], profileDigest=digest_value,
                    cwd=profile['roots'][0], routerReference=reference,
                    notAfterUnixMillis=int(time.time() * 1000) + 60000)

    def turn(self, state, session, answer):
        self.fixture(dict(type='queue', content=answer))
        result = self.command(state, dict(type='prompt', sessionId=session, generation=1,
            promptOrigin='human', text='Complete this synthetic fixture turn without tools',
            notAfterUnixMillis=int(time.time() * 1000) + 60000))
        self.assertEqual(result['status'], 'completed')

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise

    def stop_all(self):
        for process in reversed(self.holders):
            self.stop(process)

    def stop_service(self):
        self.stop(self.service)

    @staticmethod
    def save_diagnostics(home):
        target = os.environ.get('RELAY_HANDOFF_EVIDENCE')
        if target:
            for name in ('fixture.stderr', 'old.stderr', 'new.stderr',
                         'old.startup.json', 'new.startup.json'):
                path = home / name
                if path.is_file():
                    with path.open('rb') as stream:
                        data = stream.read(1024 * 1024)
                    Path(target).with_name(name).write_bytes(data)


if __name__ == '__main__':
    unittest.main()
