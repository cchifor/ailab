"""Offline support qualification. All authority records below are synthetic.

Native and SQLite reads use the production readers on actual private files. SQL is
qualified separately on PostgreSQL17 using --sql-fixture / --sql-readbacks. No
host discovery, SSH, kubectl, credential files or live providers are used here.
"""
import copy
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import relay_phase_a_collectors as C
import relay_phase_a_observer as O
from scripts.tests import test_relay_phase_a_observer as T

spec = importlib.util.spec_from_file_location('support_dw', SCRIPTS / 'relay-phase-a/remote/dw.py')
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)
u, sha = T.u, O.sha256_bytes
STAMP = '2026-11-28T00:00:00.000Z'
KEY = 'key_' + 'a' * 32
NATIVE = {'conversation': 'conversation-1', 'branch': 'branch-1', 'nativeSession': u(11),
          'parent': 'turn-parent', 'pending': 'turn-pending'}
NOTICE = ('Router refused changed session configuration (SESSION_CONFIGURATION_CHANGED). '
          'Explicit reconciliation is required. No provider or model fallback was used.')
PROBE = ('After any required scoped Relay message read, reply exactly EFFORT_HIGH_PROBE. '
         'Do not use other tools, access files or modify anything.')


def encode(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode()


def private(path, data):
    path.write_bytes(data)
    path.chmod(0o600)


class NativeFixture:
    def __init__(self, root, native=None):
        self.root = Path(root)
        self.state, self.workspace = self.root / 'state', self.root / 'workspace'
        self.home = self.state / 'native-homes' / u(10)
        self.resume = self.state / 'native-homes' / u(12)
        for p in (self.state, self.workspace, self.state / 'native-homes', self.home, self.resume):
            p.mkdir(mode=0o700)
        self.native = copy.deepcopy(native or NATIVE)
        self.h = T.FakeHost(None)
        self.run = T.bound_run(self.h)
        self.run.native_id = u(11)
        self.h.cfg.raw = copy.deepcopy(self.h.cfg.raw)
        self.h.cfg.raw['holder']['stateDir'] = str(self.state)
        self.h.cfg.raw['holder']['newJournalId'] = u(7)
        self.h.cfg.raw['profiles']['codex']['cwd'] = str(self.workspace)
        self.h.state.holder = {'incarnation': u(7), 'journal': u(7), 'mainPid': 42}
        self.h.state.renewed = {'codex': {'key': KEY}}
        self.run.approved_marker_sha = 'e' * 64
        self.run.confirmed = [{'permission': u(21), 'option': 'rej', 'kind': 'reject_once', 'step': 'denied'},
                              {'permission': u(22), 'option': 'acc', 'kind': 'allow_once', 'step': 'effects'}]
        self.entries = []
        self.add('custom', customType='relay-inference-session-v1', data=self.native)
        for index, (step, error) in enumerate((('denied', True), ('effects', False))):
            self.add('message', message={'role': 'assistant', 'timestamp': 1, 'stopReason': 'toolUse', 'content': [
                {'type': 'toolCall', 'id': 'call-' + str(index), 'name': 'bash',
                 'arguments': {'command': self.h.marker_command('codex', self.run.run_id, step)}}]})
            self.add('message', message={'role': 'toolResult', 'timestamp': 2, 'toolCallId': 'call-' + str(index),
                                        'toolName': 'bash', 'isError': error, 'content': []})
        self.header = {'type': 'session', 'version': 3, 'id': u(11), 'timestamp': STAMP, 'cwd': str(self.workspace)}
        self.history = self.home / 'history.jsonl'
        self.records = []
        self.append({'kind': 'holder-started'})
        self.append({'kind': 'session-opened', 'sessionId': u(10), 'value': self.opened()})
        self.persist()

    def add(self, kind, **fields):
        self.entries.append({'type': kind, 'id': f'{len(self.entries)+1:08x}',
                             'parentId': self.entries[-1]['id'] if self.entries else None,
                             'timestamp': STAMP, **fields})

    def opened(self):
        return {'profileId': T.CODEX['id'], 'profileDigest': 'f' * 64, 'nativeSessionId': u(11), 'generation': 1}

    def append(self, event):
        self.records.append({'version': 1, 'sequence': len(self.records) + 1, 'incarnation': u(7), 'event': event})

    def persist(self):
        self.source = b'\n'.join(encode(x) for x in [self.header, *self.entries]) + b'\n'
        private(self.history, self.source)
        ws = D.private_dir_identity(str(self.workspace))
        home = D.private_dir_identity(str(self.home))
        self.retained = {'version': 1, 'admission': 'blocked', 'containment': 'owned-process-group-only',
                         'home': {k: int(home[k]) for k in ('device', 'inode', 'uid')},
                         'metadata': {'sessionId': u(10), 'nativeSessionId': u(11), 'profileDigest': 'f' * 64,
                                      'workspace': ws, 'outcome': 'closed',
                                      'history': {'path': str(self.history), 'sha256': sha(self.source)}}}
        data = encode(self.retained)
        private(self.home / 'relay-retained-state.json', data)
        self.records = self.records[:2]
        self.append({'kind': 'native-state-retained', 'sessionId': u(10), 'value': {'receiptSha256': sha(data)}})
        self.append({'kind': 'session-ended', 'sessionId': u(10)})
        self.save_journal()

    def save_journal(self):
        private(self.state / 'journal.jsonl', b'\n'.join(encode(r) for r in self.records) + b'\n')

    def imported(self):
        desc = {'version': 2, 'admission': 'holder-owned', 'ownerSessionId': u(12), 'sourceSessionId': u(10),
                'nativeSessionId': u(11), 'workspace': D.private_dir_identity(str(self.workspace)),
                'nativeHome': D.private_dir_identity(str(self.home)), 'runtimeManifestSha256': 'd' * 64,
                'retainedReceiptSha256': sha((self.home / 'relay-retained-state.json').read_bytes())}
        private(self.resume / 'holder-recovery.json', encode({**desc, 'historyFile': str(self.history),
                                                             'historySha256': sha(self.source)}))
        private(self.resume / 'native-recovery.json', encode({**desc, 'sourceSha256': sha(self.source),
                                                             'importedSha256': sha(self.source)}))
        # Real Pi session_start can append a coordination receipt to the imported history.
        suffix = {'type': 'custom', 'id': '00000009', 'parentId': self.entries[-1]['id'], 'timestamp': STAMP,
                  'customType': 'relay-inference-session-v1', 'data': self.native}
        private(self.resume / 'resumed-session.jsonl', self.source + encode(suffix) + b'\n')
        self.append({'kind': 'session-opened', 'sessionId': u(12), 'value': self.opened()})
        self.save_journal()

    def read(self, session=u(10)):
        return D.native_checkpoint(str(self.state), session, u(11), T.CODEX['id'], str(self.workspace), u(7), u(7))


SQLITE = r'''
const { DatabaseSync } = require('node:sqlite');
const { createHash } = require('node:crypto');
const { branchRecord } = require(process.argv[1]);
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
if (input.record !== undefined) {
  const db = new DatabaseSync(input.file);
  db.exec('PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS kv(ns TEXT,key TEXT,value TEXT,PRIMARY KEY(ns,key))');
  const key = createHash('sha256').update(JSON.stringify(input.scope)).digest('hex');
  db.prepare('INSERT OR REPLACE INTO kv VALUES(?,?,?)').run('inference-session-v1',key,JSON.stringify(input.record));
  // This unrelated row must never appear in the returned metadata.
  db.prepare('INSERT OR REPLACE INTO kv VALUES(?,?,?)').run('other','secret','DO-NOT-EXPORT');
  db.close();
}
process.stdout.write(JSON.stringify(branchRecord(input.file,...input.scope)));
'''


def sqlite_read(path, native, record=None, principal='key:' + KEY):
    args = {'file': str(path), 'scope': ['default', principal, native['conversation'], native['branch']]}
    if record is not None:
        args['record'] = record
    p = subprocess.run(['node', '-e', SQLITE, str(SCRIPTS / 'relay-phase-a/remote/router.js')],
                       input=encode(args), capture_output=True, timeout=10)
    if p.returncode:
        raise ValueError('synthetic SQLite reader refused')
    return json.loads(p.stdout)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = NativeFixture(self.temp.name)

    def replace_import(self, entries, suffix=None):
        raw = b'\n'.join(encode(x) for x in [self.f.header, *entries]) + b'\n'
        descriptor = self.f.resume / 'native-recovery.json'
        data = json.loads(descriptor.read_bytes()); data['importedSha256'] = sha(raw)
        private(descriptor, encode(data))
        private(self.f.resume / 'resumed-session.jsonl', raw + (encode(suffix) + b'\n' if suffix else b''))

    def test_jsonl_unicode_separators_are_message_data(self):
        self.f.entries[1]['message']['content'].append({'type':'text','text':'first\u0085second\u2028third\u2029last'})
        self.f.persist()
        before = self.f.read()
        self.f.imported()
        after = self.f.read(u(12))
        self.assertEqual(before['shellCommands'], after['shellCommands'])
        self.assertEqual(after['native'], NATIVE)

    def test_imported_prefix_cannot_change_retained_conversation_or_tool_effects(self):
        self.f.imported()
        changes = [lambda e:e[1]['message']['content'][0]['arguments'].update(command='changed'),
                   lambda e:e[2]['message'].update(isError=False),
                   lambda e:e[1]['message']['content'].append({'type':'text','text':'invented'}),
                   lambda e:e[1]['message']['content'][0]['arguments'].update(extra=True)]
        for change in changes:
            entries=copy.deepcopy(self.f.entries);change(entries);self.replace_import(entries)
            with self.subTest(change=change),self.assertRaises(ValueError):self.f.read(u(12))

    def test_appended_receipt_cannot_change_retained_native_join_or_reparent_history(self):
        self.f.imported()
        suffix={'type':'custom','id':'00000009','parentId':self.f.entries[-1]['id'],
                'timestamp':STAMP,'customType':'relay-inference-session-v1','data':{**NATIVE,'pending':'changed'}}
        self.replace_import(self.f.entries,suffix)
        with self.assertRaises(ValueError):self.f.read(u(12))
        suffix['data']=NATIVE;suffix['parentId']=self.f.entries[0]['id']
        self.replace_import(self.f.entries,suffix)
        with self.assertRaises(ValueError):self.f.read(u(12))

    def test_source_defined_import_removals_and_parent_rewrite_are_accepted(self):
        self.f.entries[2]['message']['details']={'display':'omitted'}
        self.f.add('model_change',provider='old',modelId='old')
        self.f.add('thinking_level_change',thinkingLevel='high')
        self.f.add('message',message={'role':'system','content':'old policy'})
        self.f.add('custom',customType='relay-inference-session-v1',data=NATIVE)
        self.f.persist();self.f.imported()
        expected=copy.deepcopy(self.f.entries[:5]+self.f.entries[-1:])
        del expected[2]['message']['details']
        expected[-1]['parentId']=expected[-2]['id']
        self.replace_import(expected)
        self.assertEqual(self.f.read(u(12))['native'],NATIVE)

    def test_actual_private_retained_and_resumed_readers(self):
        before = self.f.read()
        self.f.imported()
        after = self.f.read(u(12))
        self.assertEqual(before['native'], after['native'])
        self.assertEqual(before['currentSha256'], after['sourceSha256'])
        self.assertNotEqual(after['importedSha256'], after['currentSha256'])
        self.assertEqual(after['sourceSessionId'], u(10))
        self.assertEqual(after['shellCommands'], before['shellCommands'])
        self.assertNotIn('command', json.dumps(after))

    def test_active_ancestry_excludes_abandoned_pending_branch(self):
        old = copy.deepcopy(self.f.entries[-1])
        self.f.add('custom', customType='relay-inference-session-v1', data={**NATIVE, 'branch': 'abandoned'})
        self.f.entries.append({**old, 'id': '00000007', 'parentId': old['id']})
        # Use a harmless model entry as the active leaf, avoiding a duplicate tool result.
        self.f.entries[-1] = {'type': 'model_change', 'id': '00000007', 'parentId': old['id'],
                             'timestamp': STAMP, 'provider': 'synthetic', 'modelId': 'synthetic'}
        self.f.persist()
        value = self.f.read()
        self.assertEqual(value['native'], NATIVE)
        self.assertLess(value['activeEntries'], value['entries'])

    def test_private_file_guards_and_hash_drift(self):
        path = self.f.history
        original = path.read_bytes()
        for mode in (0o644, 0o400):
            path.chmod(mode)
            with self.assertRaises(ValueError): self.f.read()
        path.chmod(0o600)
        other = path.with_name('link.jsonl')
        os.link(path, other)
        with self.assertRaises(ValueError): self.f.read()
        other.unlink()
        path.unlink()
        private(other, original)
        path.symlink_to(other)
        with self.assertRaises((ValueError, OSError)): self.f.read()
        path.unlink()
        private(path, original + b' ')
        with self.assertRaises(ValueError): self.f.read()

    def test_duplicate_json_overflow_timestamp_parent_and_tool_refuse(self):
        for data in (b'{"x":1,"x":2}', b'{"x":1e999}', b'{"x":NaN}'):
            with self.assertRaises(ValueError): D.checkpoint_json(data)
        for mutate in (lambda e: e[1].update(parentId='ffffffff'),
                       lambda e: e[1].update(timestamp='invalid'),
                       lambda e: e[3]['message'].update(toolCallId='unknown'),
                       lambda e: e[-1]['message'].update(isError=None)):
            entries = [copy.deepcopy(self.f.header), *copy.deepcopy(self.f.entries)]
            mutate(entries)
            with self.assertRaises(ValueError): D.native_ancestry(b'\n'.join(encode(e) for e in entries), u(11), str(self.f.workspace))

    def test_import_descriptor_and_appended_work_refuse(self):
        self.f.imported()
        p = self.f.resume / 'native-recovery.json'
        original = p.read_bytes()
        for field, value in (('ownerSessionId', u(99)), ('sourceSha256', '0'*64), ('importedSha256', '1'*64),
                             ('retainedReceiptSha256', '2'*64), ('nativeSessionId', u(99))):
            data = json.loads(original); data[field] = value; private(p, encode(data))
            with self.assertRaises(ValueError): self.f.read(u(12))
        private(p, original)
        path = self.f.resume / 'resumed-session.jsonl'
        with path.open('ab') as out: out.write(encode({'type': 'message'}) + b'\n')
        with self.assertRaises(ValueError): self.f.read(u(12))

    def test_actual_sqlite_exact_key_readonly_and_missing_row(self):
        path = self.f.root / 'router.db'
        record = {'epoch': 'epoch-1', 'operation': 'turn-pending', 'state': 'uncertain', 'updatedAt': C.now_ms()}
        value = sqlite_read(path, NATIVE, record)
        before = path.read_bytes()
        self.assertEqual(sqlite_read(path, NATIVE)['record'], record)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(value['querySha256'], sha(b'SELECT value FROM kv WHERE ns=? AND key=?'))
        self.assertEqual(value['rawRecordSha256'], sha(encode(record)))
        self.assertNotIn('DO-NOT-EXPORT', json.dumps(value))
        with self.assertRaises(ValueError): sqlite_read(path, {**NATIVE, 'branch': 'other'})
        with self.assertRaises(ValueError): sqlite_read(path, NATIVE, principal='key:' + 'key_' + 'b'*32)

    def test_future_router_record_refused(self):
        with self.assertRaises(ValueError):
            sqlite_read(self.f.root/'router.db', NATIVE,
                        {'epoch':'e','operation':'turn-pending','state':'complete','updatedAt':C.now_ms()+60000})

    def test_router_exact_stdin_entry_used_by_remote_adapter(self):
        path=self.f.root/'router.db'
        expected=sqlite_read(path,NATIVE,{'epoch':'e','operation':'turn-pending','state':'complete','updatedAt':C.now_ms()})
        code=(SCRIPTS/'relay-phase-a/remote/router.js').read_bytes()
        p=subprocess.run(['node','-',str(C.now_ms()+3000),'branch','default','key:'+KEY,
                          NATIVE['conversation'],NATIVE['branch']],input=code,capture_output=True,timeout=5,
                         env={'PATH':os.environ['PATH'],'ROUTER_DATABASE':str(path)})
        self.assertEqual(p.returncode,0)
        actual=json.loads(p.stdout)
        self.assertEqual(actual['record'],expected['record'])
        self.assertEqual(actual['rawRecordSha256'],expected['rawRecordSha256'])
        self.assertEqual(actual['storeSha256'],expected['storeSha256'])

    def test_actual_identity_and_branch_modes_share_the_sqlite_store_identity(self):
        path = self.f.root/'same-store.db'
        expected = sqlite_read(path, NATIVE, {'epoch':'e','operation':'turn-pending',
                                            'state':'complete','updatedAt':C.now_ms()})
        original, requests = path.read_bytes(), []
        class Config(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                requests.append((self.path, self.headers.get('Authorization')))
                body = encode({'capture': {'level': 'full', 'retentionDays': 7}})
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        try:
            server = http.server.HTTPServer(('127.0.0.1', 8787), Config)
        except OSError as error:
            raise AssertionError('isolated fixture requires free 127.0.0.1:8787; use the network-none qualification container') from error
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            source = (SCRIPTS/'relay-phase-a/remote/router.js').read_bytes()
            env = {'PATH':os.environ['PATH'], 'ROUTER_DATABASE':str(path),
                   'ROUTER_ADMIN_TOKEN':'synthetic-config-reader'}
            def invoke(*args):
                p = subprocess.run(['node','-',str(C.now_ms()+3000),*args], input=source,
                                   capture_output=True, timeout=5, env=env)
                self.assertEqual(p.returncode, 0, p.stderr)
                return json.loads(p.stdout)
            identity = invoke('identity')
            branch = invoke('branch', 'default', 'key:'+KEY, NATIVE['conversation'], NATIVE['branch'])
            self.assertEqual(identity['captureLevel'], 'full')
            self.assertEqual(identity['retentionDays'], 7)
            self.assertEqual(identity['storeSha256'], branch['storeSha256'])
            self.assertEqual(branch['storeSha256'], expected['storeSha256'])
            self.assertEqual(branch['record'], expected['record'])
            self.assertEqual(requests, [('/admin/v1/config', 'Bearer synthetic-config-reader')])
            self.assertEqual(path.read_bytes(), original)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)
            self.assertFalse(worker.is_alive())

    def test_missing_runtime_manifest_in_both_descriptors_refused(self):
        self.f.imported()
        for name in ('native-recovery.json','holder-recovery.json'):
            p=self.f.resume/name; value=json.loads(p.read_bytes()); del value['runtimeManifestSha256']
            private(p,encode(value))
        with self.assertRaises(ValueError): self.f.read(u(12))

    def test_native_entry_normalizes_nonobject_private_records(self):
        local = self.f.root / '.local/state/fixture'
        local.mkdir(mode=0o700, parents=True)
        f = NativeFixture(local)
        f.imported()
        # Only the allowed home root is redirected to this private synthetic tree.
        # The actual entry and file reader execute unchanged in a child process.
        entry = ('import importlib.util,sys; path,home,args=sys.argv[1],sys.argv[2],sys.argv[3:]; '
                 's=importlib.util.spec_from_file_location("entry",path); d=importlib.util.module_from_spec(s); '
                 's.loader.exec_module(d); d.HOME=home; sys.argv=[path,*args]; d.main()')
        argv = [sys.executable, '-c', entry, str(SCRIPTS/'relay-phase-a/remote/dw.py'), str(self.f.root),
                'native-checkpoint', str(f.state), u(12), u(11), T.CODEX['id'], str(f.workspace), u(7), u(7)]
        valid = subprocess.run(argv, capture_output=True, timeout=5)
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertEqual(json.loads(valid.stdout)['native'], NATIVE)
        journal = f.state/'journal.jsonl'
        wrong_opened = copy.deepcopy(f.records)
        wrong_opened[-1]['event']['value'] = []  # current resumed session's opening, not its predecessor
        history = f.resume/'resumed-session.jsonl'
        for label, path, data in (
                ('journal', journal, b'[]\n'),
                ('opened', journal, b'\n'.join(encode(r) for r in wrong_opened)+b'\n'),
                ('native-recovery', f.resume/'native-recovery.json', b'[]'),
                ('holder-recovery', f.resume/'holder-recovery.json', b'[]'),
                ('resumed-line', history, history.read_bytes()+b'[]\n')):
            with self.subTest(record=label):
                original = path.read_bytes()
                try:
                    private(path, data)
                    refused = subprocess.run(argv, capture_output=True, timeout=5)
                    self.assertEqual(refused.returncode, 3)
                    self.assertEqual(refused.stdout, b'')
                    self.assertEqual(refused.stderr, b'native checkpoint unavailable\n')
                finally:
                    private(path, original)


class JoinTests(unittest.TestCase):
    def test_all_authoritative_join_rows_and_refusals(self):
        r = {'epoch': 'e', 'operation': 'turn-pending', 'state': 'uncertain', 'updatedAt': 1}
        self.assertTrue(C.branch_decision(NATIVE, r))
        parent = {**r, 'operation': 'turn-parent', 'state': 'complete'}
        self.assertTrue(C.branch_decision(NATIVE, parent))
        n = {k:v for k,v in NATIVE.items() if k != 'pending'}
        self.assertFalse(C.branch_decision(n, parent))
        for record in ({**r, 'state': 'running'}, {**r, 'successor': 'new'}, {**r, 'operation': 'other'},
                       {**parent, 'state': 'uncertain'}, {**parent, 'epoch': None}):
            with self.assertRaises(O.Failed): C.branch_decision(n, record)


class ActualReaderRemote(T.FakeRemote):
    """Synthetic Hub/holder service; native files and SQLite are read for real."""
    def __init__(self, fixture):
        self.fixture = fixture
        self.db = fixture.root / 'router.db'
        self.branch = sqlite_read(self.db, fixture.native, {'epoch':'epoch-1', 'operation':'turn-pending',
                                  'state':'uncertain', 'updatedAt':C.now_ms()})
        holder = T.holder_rec()
        holder['hello']['journalId'] = u(7)
        holder['observedMs'] = C.now_ms()
        super().__init__({'holder':holder, 'sql:session-state.sql':T.sess('closed'),
            'sql:session-commands.sql':[{'table':'control','kind':'close','state':'completed','receipt_status':'completed'}],
            'sql:permission-decisions.sql':[{'id':u(21),'state':'resolved','option_id':'rej'},
                                            {'id':u(22),'state':'resolved','option_id':'acc'}],
            'marker:approved':{'state':'observed','phaseChecksPass':True,'observedUtc':C.utc(C.now_ms()),
                               'files':{'effects':{'sha256':'e'*64}}},
            'branch-identity':{'captureLevel':'metadata','captureSha256':'c'*64,'storeSha256':self.branch['storeSha256']}})
        fixture.h.state.router_pod = {'uid':T.POD['uid'],'restarts':0,'capture':'c'*64,'store':self.branch['storeSha256']}

    def dw(self,args,budget,obs,what,cap=60.0,deadline_arg=False):
        if args[0]=='native-checkpoint':
            assert deadline_arg is False  # this reader's typed CLI takes only the bound identities
            self.calls.append(what)
            return D.native_checkpoint(*args[1:])
        return super().dw(args,budget,obs,what,cap,deadline_arg=deadline_arg)

    def router(self,args,budget,obs,what,cap=60.0,pod=None):
        if args[0]=='branch':
            self.calls.append(what)
            return sqlite_read(self.db,self.fixture.native)
        return super().router(args,budget,obs,what,cap,pod)


class CollectorIntegration(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.f=NativeFixture(self.temp.name)
        self.remote=ActualReaderRemote(self.f); self.f.h.remote=self.remote

    def context(self):
        r=self.f.run
        return {'provider':'codex','runId':r.run_id,'sessionId':r.session_id,'generation':1,
                'holderId':u(7),'journalId':u(7)}

    def test_actual_c9_retains_original_reader_provenance(self):
        self.assertTrue(C.c_recover_history(self.f.h,{},self.context(),T.budget(),C.now_ms(),{}))
        saved=self.f.run.retained_branch
        self.assertEqual(saved['sessionId'],self.f.run.session_id)
        self.assertEqual(saved['native']['currentSha256'],sha(self.f.source))
        self.assertEqual(saved['router']['record'],self.remote.branch['record'])
        self.assertEqual(self.remote.calls.count('native-checkpoint'),1)
        self.assertEqual(self.remote.calls.count('branch-record'),1)


def sql_fixture():
    """Predecessor-only actual PG projection schema; synthetic metadata."""
    statements=['CREATE TABLE schema_migrations(version integer PRIMARY KEY); INSERT INTO schema_migrations SELECT generate_series(1,93);',
      'CREATE TABLE agent_control_commands(tenant_id uuid,id uuid,session_id uuid,operation_id uuid,generation integer,kind text,state text,receipt jsonb);']
    for i,kind in enumerate(('prompt','cancel','close')):
        receipt={'status':'completed','result':{'stopReason':'cancelled'} if kind=='prompt' else {'cancelSent':True}}
        q="'"+json.dumps(receipt).replace("'","''")+"'::jsonb"
        statements.append(f"INSERT INTO agent_control_commands VALUES('{u(1)}','{u(40+i)}','45724087-335a-4c55-ad0f-cb278afe608a','{u(50+i)}',1,'{kind}','completed',{q});")
    return '\n'.join(statements)

def predecessor_adapter(request, *, source_tuple=None):
    """Synthetic deployment transport plus unmodified PostgreSQL readback.
    The database timestamp is used verbatim; this adapter never refreshes it.
    """
    h=T.FakeHost(None)
    h.cfg.raw=copy.deepcopy(h.cfg.raw)
    if source_tuple is not None:  # synthetic unqualified deployment configuration, never a reader bypass
        h.cfg.raw['hub']['runSource'], h.cfg.raw['router']['source'], h.cfg.raw['holder']['qualified']['source'] = source_tuple
    h.cfg.raw['tenant']=u(1)  # synthetic database fixture tenant, never a live repin
    h.cfg.files['relay-phase-a/sql/predecessor-projection.sql']=(SCRIPTS/'relay-phase-a/sql/predecessor-projection.sql').read_bytes()
    h.predecessor_bytes=encode(request['report'])
    h.predecessor_sha256=sha(h.predecessor_bytes)
    h.predecessor_ok=T.H.ObserverHost.predecessor_ok.__get__(h)
    h.predecessor_canonical=T.H.ObserverHost.predecessor_canonical.__get__(h)
    class Adapter:
        def kubectl(self,ns,args,budget,obs,what):
            assert ns==h.cfg.raw['hub']['namespace']
            if what=='hub-deploy':
                return encode({'metadata':{'generation':1},'spec':{'replicas':1,'selector':{'matchLabels':{'app':'fixture'}}},
                               'status':{'observedGeneration':1,'updatedReplicas':1,'readyReplicas':1,'availableReplicas':1}})
            assert what=='hub-pods'
            return encode({'items':[{'metadata':{},'status':{'containerStatuses':[{'name':'relay','ready':True,
                'imageID':'fixture@'+h.cfg.raw['hub']['receipts'][0]['imageDigest']}]}}]})
        def sql(self,name,variables,budget,obs):
            assert name=='predecessor-projection.sql'
            assert variables=={'tenant':h.cfg.raw['tenant'],'session_id':'45724087-335a-4c55-ad0f-cb278afe608a',
                              'generation':1,'prompt_operation':u(50),'cancel_operation':u(51),'close_operation':u(52)}
            return [request['row']]
    h.remote=Adapter()
    obs={}
    value=C.c_predecessor(h,{},request['context'],T.budget(),C.now_ms(),obs)
    return {'value':value,'observations':obs}


class PredecessorProjectionTests(unittest.TestCase):
    """Invoke the actual collector before any Runner-side transformation."""
    def setUp(self):
        wanted=[{'operationId':u(50+i),'kind':kind} for i,kind in enumerate(('prompt','cancel','close'))]
        report={'operations':[{'body':row} for row in wanted]}
        context={'source':C.SUPPORT_HUB,'schemaVersion':93,'sessionId':C.SUPPORT_PREDECESSOR_SESSION,
                 'generation':1,'priorReportCanonicalSha256':sha(O.canonical(report).encode()),'commands':wanted}
        commands=[{'id':u(40+i),'operation_id':row['operationId'],'session_id':context['sessionId'],
                   'generation':1,'kind':row['kind'],'state':'completed','receipt':{'status':'completed',
                   'result':{'stopReason':'cancelled'} if row['kind']=='prompt' else {'cancelSent':True}}}
                  for i,row in enumerate(wanted)]
        self.request={'report':report,'context':context,'row':{'schemaVersions':list(range(1,94)),
            'observedMs':C.now_ms(),'databaseName':T.CFG['hubDb']['database'],'commands':commands}}

    def test_valid_projection_preserves_observed_time_and_both_hashes(self):
        result=predecessor_adapter(self.request)
        value=json.loads(result['value']);evidence=result['observations']['predecessor']
        self.assertEqual(value['commands'],self.request['row']['commands'])
        self.assertEqual(value['observedUtc'],C.utc(self.request['row']['observedMs']))
        self.assertEqual(evidence['rawSha256'],sha(result['value'].encode()))
        content={k:v for k,v in value.items() if k!='observedUtc'}
        self.assertEqual(evidence['contentSha256'],sha((C.CONTENT_DOMAIN+O.canonical(content)).encode()))

    def test_unqualified_source_or_schema_refuses(self):
        qualified=[C.SUPPORT_HUB,C.SUPPORT_ROUTER,C.SUPPORT_HOLDER]
        for i in range(3):
            wrong=qualified.copy();wrong[i]='0'*40
            with self.subTest(source=i),self.assertRaises(O.Unsupported):
                predecessor_adapter(self.request,source_tuple=wrong)
        self.request['row']['schemaVersions'].pop()
        with self.assertRaises(O.Unsupported):predecessor_adapter(self.request)

    def test_stale_or_future_database_timestamp_refuses(self):
        for observed in (C.now_ms()-121000,C.now_ms()+60000,None,True):
            self.request['row']['observedMs']=observed
            with self.subTest(observed=observed),self.assertRaises(O.Failed):predecessor_adapter(self.request)

    def test_missing_duplicate_foreign_or_reordered_commands_refuse(self):
        original=copy.deepcopy(self.request['row']['commands'])
        for mutate in (lambda rows:rows.pop(),lambda rows:rows.append(copy.deepcopy(rows[0])),
                       lambda rows:rows[1].update(id=rows[0]['id']),
                       lambda rows:rows[0].update(session_id=u(99)),
                       lambda rows:rows[0].update(operation_id=u(99)),
                       lambda rows:rows[0].update(generation=2),lambda rows:rows.reverse()):
            rows=copy.deepcopy(original);mutate(rows);self.request['row']['commands']=rows
            with self.subTest(rows=rows),self.assertRaises(O.Failed):predecessor_adapter(self.request)

    def test_unknown_receipts_or_nonterminal_command_refuse(self):
        original=copy.deepcopy(self.request['row']['commands'])
        for mutate in (lambda rows:rows[0].update(state='unknown'),
                       lambda rows:rows[0]['receipt'].update(status='accepted'),
                       lambda rows:rows[0]['receipt'].update(result={'stopReason':'refusal'}),
                       lambda rows:rows[1]['receipt'].update(result={'cancelSent':False}),
                       lambda rows:rows[2]['receipt'].update(extra=True)):
            rows=copy.deepcopy(original);mutate(rows);self.request['row']['commands']=rows
            with self.subTest(rows=rows),self.assertRaises(O.Failed):predecessor_adapter(self.request)

    def test_context_and_independent_report_identity_remain_required(self):
        original=copy.deepcopy(self.request)
        for field,value in [('source','0'*40),('schemaVersion',92),('generation',2),
                            ('sessionId',u(99)),('priorReportCanonicalSha256','0'*64)]:
            self.request=copy.deepcopy(original);self.request['context'][field]=value
            with self.subTest(field=field),self.assertRaises(O.Failed):predecessor_adapter(self.request)
        self.request=copy.deepcopy(original);self.request['report']['operations'].append({'body':{'kind':'close','operationId':u(99)}})
        with self.assertRaises(O.Failed):predecessor_adapter(self.request)


def load_tests(loader, standard_tests, pattern):
    # These are actual private-file readers with a fixed deployed UID, not a
    # policy parameter. Never patch that guard to suit the CI runner account.
    if sys.platform == 'linux' and os.getuid() == 1000:
        return standard_tests

    class IsolatedNativeReaders(unittest.TestCase):
        def runTest(self):
            docker = shutil.which('docker')
            self.assertIsNotNone(docker, 'Docker is required for UID1000 native observer tests; no skip')
            root = SCRIPTS.parent
            name = 'pi-observer-readers-' + uuid.uuid4().hex
            try:
                result = subprocess.run([
                    docker, 'run', '--rm', '--init', '--name', name,
                    '--network', 'none', '--read-only', '--user', '1000:1000',
                    '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--tmpfs', '/tmp:rw,mode=1777',
                    '--mount', f'type=bind,source={root},target=/repo,readonly',
                    '--workdir', '/repo', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                    'node@sha256:8fa96264f302f58bbb13aa7339c541547b56975766ce956c875da149452a3e40',
                    'timeout', '60s', 'python3', '-m', 'unittest', '-v',
                    'scripts.tests.test_relay_phase_a_support'],
                    cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=180)
                print(result.stdout + result.stderr)
                self.assertEqual(result.returncode, 0, 'isolated native observer suite failed')
            finally:
                cleanup = subprocess.run([docker, 'rm', '-f', name], capture_output=True,
                                         text=True, encoding='utf-8', timeout=30)
                self.assertTrue(cleanup.returncode == 0 or 'No such container' in cleanup.stderr,
                                'could not confirm removal of the owned native observer container')
    return unittest.TestSuite([IsolatedNativeReaders()])


if __name__ == '__main__':
    if sys.argv[1:] == ['--predecessor']:
        print(json.dumps(predecessor_adapter(json.load(sys.stdin))))
    elif sys.argv[1:] == ['--sql-fixture']:
        print(sql_fixture())
    elif len(sys.argv)==3 and sys.argv[1]=='--sql-readbacks':
        root=Path(sys.argv[2])
        p=json.loads((root/'predecessor.json').read_text())
        C.supported_schema(p)
        assert [c['kind'] for c in p['commands']]==['prompt','cancel','close']
        assert [c['receipt']['result'] for c in p['commands']]==[{'stopReason':'cancelled'},{'cancelSent':True},{'cancelSent':True}]
        wrong=json.loads((root/'foreign.json').read_text())
        assert wrong['commands']==[]
        print('actual PostgreSQL17 predecessor readbacks PASS')
    else:
        unittest.main()
