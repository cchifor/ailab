"""Fixed cleanup-only observations. No Phase A preparation or mutation dispatch.

The factory remains unavailable until a reviewed deployment qualification is
compiled. The Reader class is an offline test seam, not an authority validator:
the broker must obtain its plan from the actual pure finalizer validator and
compare decision bytes with the independently authorized operator ledger.
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import threading
import time

from relay_phase_a_observer import Host, Evidence, EvidenceFailure

QUALIFIED_FINALIZATION_OBSERVER = None
TENANT = '00000000-0000-4000-8000-000000000001'
HOST_ID = 'b3b1be1a-275c-4a2e-b515-428b39c43171'
CONNECTION = 'babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8'
PROFILES = {
    'codex': ('1d9a7d6e-4b26-49fd-9053-7b0a22e1baae', 'a0f57684-747f-4c3e-8d90-e684b7262590'),
    'qwen': ('f25c187b-1e50-4c7c-860e-52947bfdf39e', 'd2871eec-8f5a-4afe-a917-014f0ae8e14d'),
    'claude': ('fdb56ca5-908b-40ee-94b3-aac735dd8622', 'a8f6211c-d7e2-4ffd-824d-b158e349a8d4'),
}
SCOPE = dict(tenantId=TENANT, hostId=HOST_ID, connectionId=CONNECTION, executionIdentity='c4',
             bindingIds=[p[1] for p in PROFILES.values()], profileIds=[p[0] for p in PROFILES.values()])
ROOT = Path(__file__).parent / 'relay-finalization'
SHA = re.compile(r'[a-f0-9]{64}')
UUID = re.compile(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}')
KEY = re.compile(r'key_[a-f0-9]{12,32}')
MAX_BYTES = 2 * 1024 * 1024


class Refused(Exception):
    """Fixed diagnostic only; external exception text never enters evidence."""


class PrivateEvidence(Evidence):
    """Exclusive evidence file in the broker's already-private staging directory.
    Never chmod an existing operator directory or append to another run's file."""
    def __init__(self, path):
        path=Path(path).absolute()
        need(path.parent.resolve()==path.parent and not path.is_symlink(), 'evidence-path')
        if os.name=='nt':
            result=subprocess.run(['icacls',str(path.parent)],capture_output=True,timeout=5)
            need(result.returncode==0, 'evidence-acl')
            text=result.stdout.decode(errors='strict')
            grants=[line.strip().removeprefix(str(path.parent)).strip() for line in text.splitlines() if ':(' in line]
            user=os.environ.get('USERNAME','')
            need(user and len(grants)==1 and
                 (grants[0].startswith(user+':') or ('\\'+user+':') in grants[0]) and '(I)' not in grants[0], 'evidence-acl')
        else:
            info=path.parent.stat()
            need(info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o700, 'evidence-private')
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        self.f=os.fdopen(fd,'wb',buffering=0)
        self.lock=threading.Lock()
        if os.name!='nt':
            parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(parent)
            finally:os.close(parent)


def need(ok, reason='evidence-unavailable'):
    if not ok:
        raise Refused(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def integer(v, minimum=0):
    return isinstance(v, int) and not isinstance(v, bool) and minimum <= v <= 9007199254740991


def now_ms():
    return time.time_ns() // 1000000


def utc(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def epoch(v):
    need(isinstance(v, str) and len(v) < 50, 'time-invalid')
    try:
        return int(datetime.datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp() * 1000)
    except (ValueError, OverflowError):
        raise Refused('time-invalid') from None


def strict_json(raw):
    need(isinstance(raw, bytes) and len(raw) <= MAX_BYTES, 'frame-overflow')
    def pairs(items):
        obj = {}
        for k, v in items:
            need(k not in obj, 'duplicate-key')
            obj[k] = v
        return obj
    try:
        return json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=pairs,
                          parse_constant=lambda _: need(False, 'nonfinite-json'))
    except (ValueError, UnicodeError, RecursionError):
        raise Refused('json-invalid') from None


class Budget:
    def __init__(self, deadline, cancelled):
        need(integer(deadline) and now_ms() < deadline <= now_ms() + 120000, 'deadline-invalid')
        self.deadline, self.cancelled = deadline, cancelled
        self.end = time.monotonic() + (deadline - now_ms()) / 1000

    def left(self):
        need(not self.cancelled.is_set(), 'cancelled')
        left = min(self.end - time.monotonic(), (self.deadline - now_ms()) / 1000)
        need(left > 0, 'deadline')
        return left

    def wait_until(self, when):
        while now_ms() < when:
            self.cancelled.wait(min(.05, self.left(), (when - now_ms()) / 1000))
        self.left()


def run_bounded(argv, stdin, budget, cap=MAX_BYTES):
    """Own one child; cap BOTH pipes, cancel/kill/join, never infer remote success.

    Fixed remote commands also enforce their own deadline. Killing kubectl/ssh
    cannot prove remote termination; that remains a recorded unresolved read.
    """
    budget.left()
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=os.name != 'nt')
    outputs = [bytearray(), bytearray()]
    overflow = threading.Event()
    input_failed = threading.Event()
    output_failed = threading.Event()
    def read(pipe, result):
        try:
            while chunk := pipe.read(65536):
                if len(result) + len(chunk) > cap:
                    overflow.set()
                    return
                result.extend(chunk)
        except OSError:
            output_failed.set()
    def write():
        try:
            child.stdin.write(stdin)
            child.stdin.close()
        except (OSError, ValueError):
            input_failed.set()
    def check_pipes():
        need(not input_failed.is_set(), 'reader-stdin-failed')
        need(not output_failed.is_set(), 'reader-output-failed')
        need(not overflow.is_set(), 'reader-output-overflow')
    threads = [threading.Thread(target=read, args=(child.stdout, outputs[0]), daemon=True),
               threading.Thread(target=read, args=(child.stderr, outputs[1]), daemon=True),
               threading.Thread(target=write, daemon=True)]
    for t in threads:
        t.start()
    try:
        while child.poll() is None or any(t.is_alive() for t in threads):
            budget.left()
            check_pipes()
            time.sleep(.01)
        check_pipes()
        need(child.returncode == 0, 'reader-failed')
        budget.left()
        return bytes(outputs[0])
    finally:
        if child.poll() is None or any(t.is_alive() for t in threads):
            try:
                if os.name != 'nt':
                    os.killpg(child.pid, signal.SIGKILL)
                else:
                    child.kill()
            except ProcessLookupError:
                pass
        child.wait(timeout=5)
        for pipe in (child.stdin, child.stdout, child.stderr):
            try:
                pipe.close()
            except OSError:
                pass
        for t in threads:
            t.join(2)
        need(not any(t.is_alive() for t in threads), 'reader-unresolved')


def validate_plan(plan, decision_raw, authorization_sha256):
    # A hash is only an identity. The broker supplies authorization_sha256 from
    # its retained authorized ledger, NOT from a request or the staged manifest.
    keys = {'version', 'purpose', 'scope', 'reportPins', 'decisionSha256', 'cleanupDeadline',
            'launches', 'markers', 'originalIdle', 'sources'}
    need(isinstance(plan, dict) and set(plan) == keys and plan['version'] == 1 and
         plan['purpose'] == 'canary-finalization' and plan['scope'] == SCOPE, 'plan-scope')
    pins = plan['reportPins']
    need(isinstance(pins, dict) and set(pins) in ({'a', 'b', 'bInputs', 'c'}, {'a', 'b', 'bInputs', 'c', 'd'}) and
         all(isinstance(v, str) and SHA.fullmatch(v) for v in pins.values()), 'report-pins')
    count = 11 if 'd' in pins else 10
    need(len(plan['launches']) == count and len(plan['markers']) == count - 6, 'report-inventory')
    for field in ('launchId', 'sessionId'):
        vals = [x[field] for x in plan['launches']]
        need(len(set(vals)) == count and all(isinstance(v, str) and UUID.fullmatch(v) for v in vals), 'launch-identity')
    for l in plan['launches']:
        need(set(l) == {'launchId','sessionId','profileId','bindingId','credentialId','generation','launchedAt'} and
             (l['profileId'], l['bindingId']) in PROFILES.values() and UUID.fullmatch(l['credentialId']) and
             integer(l['generation'], 1) and l['generation'] < 128, 'launch-scope')
        epoch(l['launchedAt'])
    need(integer(plan['originalIdle'], 1000) and plan['originalIdle'] <= 300000, 'idle-setting')
    need(integer(plan['cleanupDeadline']) and now_ms() < plan['cleanupDeadline'] <= now_ms() + 900000, 'cleanup-deadline')
    need(isinstance(authorization_sha256, str) and SHA.fullmatch(authorization_sha256) and
         sha(decision_raw) == authorization_sha256 == plan['decisionSha256'], 'decision-identity')
    d = strict_json(decision_raw)
    need(isinstance(d,dict) and set(d) == {'version','purpose','runId','reportPins','cleanupDeadline',
         'qualificationSha256','bindingIds','noFurtherUse'} and isinstance(d['runId'],str) and UUID.fullmatch(d['runId']) and
         d == {k: plan[k] for k in ('version','purpose','reportPins','cleanupDeadline')} |
              {'runId':d['runId'], 'qualificationSha256':sha(canonical(plan['sources']).encode()),
               'bindingIds':sorted(SCOPE['bindingIds']), 'noFurtherUse': True}, 'authorized-decision-scope')
    return copy.deepcopy(plan)


def fresh(ms, context):
    need(integer(ms) and context['notBefore'] <= ms <= now_ms() + 5000 and now_ms() - ms <= 120000,
         'evidence-stale')


def hub_projection(raw, plan, context):
    need(isinstance(raw, dict) and raw.get('version') == 1 and raw.get('schemaVersion') == 93 and
         isinstance(raw.get('databaseIdentity'), str) and 0 < len(raw['databaseIdentity']) <= 200, 'hub-identity')
    fresh(raw['observedMs'], context)
    caps = {'launches':1000, 'sessions':1000, 'bindings':3, 'credentials':384, 'installations':1000}
    for name, cap in caps.items():
        need(isinstance(raw.get(name), list) and integer(raw['counts'].get(name)) and
             raw['counts'][name] == len(raw[name]) <= cap, 'inventory-overflow')
    expected = {x['launchId']: x for x in plan['launches']}
    sessions = {x['sessionId']: x for x in plan['launches']}
    need(len(raw['launches']) == len(expected) and len(raw['sessions']) == len(sessions), 'unlisted-work')
    need({x['id'] for x in raw['launches']} == set(expected) and
         {x['id'] for x in raw['sessions']} == set(sessions), 'unlisted-work')
    need(len(raw['installations']) == len(expected) and
         {i['launch_id'] for i in raw['installations']} == set(expected), 'unlisted-installation')
    for i in raw['installations']:
        l = expected[i['launch_id']]
        need(all(i[k] == l[n] for k,n in [('session_id','sessionId'),('binding_id','bindingId'),
                 ('credential_id','credentialId'),('generation','generation')]), 'installation-link')
    for l in raw['launches']:
        e = expected[l['id']]
        need(l['session_id'] == e['sessionId'] and l['profile_id'] == e['profileId'] and
             l['profile_revision'] == 1 and l['connector_id'] == HOST_ID and l['state'] == 'open' and
             l['credential_file'] == '/home/c4/.local/state/relay/router-managed/' + e['bindingId'] + '/credential.json',
             'launch-db-scope')
    for s in raw['sessions']:
        need(s['state'] == 'closed' and s['connector_id'] == HOST_ID and
             s['profile_id'] == sessions[s['id']]['profileId'] and s['rebinding'] is False and
             all(integer(s[k]) and s[k] == 0 for k in ('pending_commands','pending_permissions','control_leases')),
             'session-work')
    need(raw['unresolvedOperations'] == 0 and integer(raw['unresolvedOperations']), 'credential-operation')
    need(len(raw['bindings']) == 3 and {x['id'] for x in raw['bindings']} == set(SCOPE['bindingIds']), 'binding-inventory')
    credentials = raw['credentials']
    need(len({c['id'] for c in credentials}) == len(credentials), 'credential-duplicate')
    for b in raw['bindings']:
        need(b['connection_id'] == CONNECTION and b['host_id'] == HOST_ID and b['execution_identity'] == 'c4' and
             integer(b['active_consumers']) and b['active_consumers'] == 0, 'binding-consumers')
        cs = sorted([c for c in credentials if c['binding_id'] == b['id']], key=lambda c: c['generation'])
        need(integer(b['generation'], 1) and b['generation'] < 128 and
             [c['generation'] for c in cs] == list(range(1, b['generation'] + 1)), 'generation-gap')
        for c in cs:
            need(UUID.fullmatch(c['id']) and KEY.fullmatch(c['router_key_id']) and c['namespace'] == 'default' and
                 c['state'] in ('ready','revoked'), 'credential-unresolved')
    for l in expected.values():
        need(sum(c['id'] == l['credentialId'] and c['generation'] == l['generation'] and
                 c['binding_id'] == l['bindingId'] for c in credentials) == 1, 'credential-report-link')
    return credentials


class Reader:
    def __init__(self, *, plan, decision_raw, authorization_sha256, remote, evidence):
        self.plan = validate_plan(plan, decision_raw, authorization_sha256)
        self.remote, self.ev = remote, evidence

    def observe(self, context, cancelled):
        p = self.plan
        need(isinstance(context, dict) and set(context) == {'version','stage','reportPins','decisionSha256','sources',
             'scope','launches','markers','originalIdle','notBefore','deadline'}, 'context-shape')
        need(context['stage'] in ('preflight', 'renderer') and context['version'] == 1, 'stage')
        for name in ('reportPins','decisionSha256','sources','scope','launches','markers','originalIdle'):
            need(context[name] == p[name], 'context-expanded')
        need(integer(context['notBefore']) and context['notBefore'] < context['deadline'] <= p['cleanupDeadline'], 'stage-deadline')
        budget = Budget(context['deadline'], cancelled)
        budget.wait_until(context['notBefore'])
        before = self.remote.identities(budget)
        need(before['hubSource'] == p['sources']['hubSource'] and before['routerSource'] == p['sources']['routerSource'],
             'source-mismatch')
        h = self.remote.hub(budget)
        credentials = hub_projection(h, p, context)
        r = self.remote.router(credentials, budget)
        fresh(r['observedMs'], context)
        need(r['source'] == p['sources']['routerSource'] and len(r['keys']) == len(credentials) and
             {x['keyId'] for x in r['keys']} == {c['router_key_id'] for c in credentials}, 'meter-inventory')
        bindings = []
        for binding in SCOPE['bindingIds']:
            keys = [c['router_key_id'] for c in credentials if c['binding_id'] == binding]
            rows = [x for x in r['keys'] if x['keyId'] in keys]
            for x in rows:
                need(x['coveredFrom'] == x['keyCreatedAt'] and all(integer(x[k]) and x[k] == 0
                     for k in ('openRequests','staleOpenRequests','unattributed')), 'meter-unresolved')
                need(x['fenceState'] in ('closed','not-applicable') and
                     (binding != PROFILES['claude'][1] or x['fenceState'] == 'closed'), 'fence-unresolved')
            bindings.append(dict(bindingId=binding, activeConsumers=0, openRequests=0, staleOpenRequests=0,
                                 unattributed=0, fenceState='closed' if any(x['fenceState']=='closed' for x in rows)
                                 else 'not-applicable'))
        need(r['settings']['warmSessions'] is False and r['settings']['warmIdleMs'] == p['originalIdle'] and
             SHA.fullmatch(r['settings']['revision']) and r['processes'] == [] and r['processEvidence']['complete'] is True,
             'router-not-cold')
        v = {k: copy.deepcopy(p[k]) for k in ('reportPins','decisionSha256','scope')}
        v.update(version=1, stage=context['stage'], hubSource=before['hubSource'], routerSource=before['routerSource'],
                 noFurtherUse=True, unlistedLaunches=0, unlistedSessions=0, unresolvedOperations=0,
                 sessions=[dict(sessionId=x['sessionId'], state='closed', pendingCommands=0, pendingPermissions=0)
                           for x in p['launches']], bindings=bindings,
                 router=dict(warmSessions=False, warmIdleMs=p['originalIdle'], settingsRevision=r['settings']['revision'], processes=[]))
        f = None
        if context['stage'] == 'renderer':
            need(all(c['state']=='revoked' and c['cleanup_pending'] is False and c['revoke_requested'] is True
                     for c in credentials), 'renderer-credentials-not-revoked')
            f = self.remote.files(p['markers'], budget)
            fresh(f['observedMs'], context)
            need(set(f['managedFiles']) == set(SCOPE['bindingIds']) and all(x is True for x in f['managedFiles'].values()),
                 'managed-file-present')
            need(len(h['installations']) == len(p['launches']) and
                 {i['launch_id'] for i in h['installations']} == {l['launchId'] for l in p['launches']}, 'renderer-inventory')
            v['launches'] = []
            for l in p['launches']:
                i = next(x for x in h['installations'] if x['launch_id'] == l['launchId'])
                ref, receipt = i['local_reference'], i['cleanup_receipt']
                need(all(i[k] == l[n] for k,n in [('session_id','sessionId'),('binding_id','bindingId'),
                     ('credential_id','credentialId'),('generation','generation')]) and UUID.fullmatch(i['command_id']), 'renderer-link')
                need(isinstance(receipt, dict) and set(receipt) == {'version','commandId','reference','status'} and
                     receipt['version'] == 1 and receipt['commandId'] == i['command_id'] and receipt['reference'] == ref and
                     ref['credentialId'] == l['credentialId'] and ref['generation'] == l['generation'] and
                     receipt['status'] in ('absent','not-current'), 'renderer-receipt')
                need(set(ref)=={'credentialId','generation','secretVersion','keyId','creationOperationId'} and
                     integer(ref['secretVersion'],1) and UUID.fullmatch(ref['creationOperationId']) and
                     any(c['id']==ref['credentialId'] and c['router_key_id']==ref['keyId'] for c in credentials),
                     'renderer-reference')
                need(epoch(l['launchedAt']) - 5000 <= epoch(i['cleaned_at']) <= h['observedMs'] + 5000, 'renderer-time')
                # Preserve not-current verbatim. The finalizer owns acceptance policy.
                v['launches'].append(dict(l, commandId=i['command_id'], status=receipt['status'],
                                          fileAbsent=True, cleanedAt=i['cleaned_at'],reference=copy.deepcopy(ref)))
            need(len(f['markers']) == len(p['markers']), 'marker-inventory')
            for old, current in zip(p['markers'], f['markers']):
                fresh(epoch(current['observedUtc']), context)
                need({k:v for k,v in old.items() if k!='observedUtc'} ==
                     {k:v for k,v in current.items() if k!='observedUtc'}, 'marker-changed')
            v['markers'] = f['markers']
        after = self.remote.identities(budget)
        need(before == after, 'source-replaced')
        budget.left()
        v['observedUtc'] = utc(now_ms())
        v['provenance'] = dict(querySha256=sha((ROOT/'hub.sql').read_bytes()),
                              projectionSha256=sha(canonical(dict(hub=h, router=r, files=f, identities=before)).encode()),
                              databaseIdentity=h['databaseIdentity'])
        self.ev.write(event='finalization-observation', value=v, readEvidence=dict(hub=h,router=r,files=f,identities=before))
        budget.left()
        return copy.deepcopy(v)


class FinalizationHost(Host):
    """Only inherited gate framing/liveness; no Phase A admission or collectors."""
    def __init__(self, *, gate, reader, evidence, on_fault, lease_ok):
        super().__init__(gate=gate, collectors={}, cfg=None, evidence=evidence, on_fault=on_fault, lease_ok=lease_ok)
        self.reader = reader

    def handle(self, seq, frame, recv_mono=None):
        try:
            need(set(frame) == {'seq','gateNow','request'} and frame['seq'] == seq, 'gate-frame')
            req = frame['request']
            need(set(req) == {'v','call','kind','deadline','context'} and req['v'] == 1 and req['call'] == 'checkpoint' and
                 req['kind'] == 'finalization' and req['deadline'] == req['context']['deadline'], 'gate-request')
            need(integer(frame['gateNow']) and abs(now_ms()-frame['gateNow']) <= 5000, 'clock-domain')
            need(self.lease_ok is not None and self.lease_ok(), 'authority-ended')
            with self.lock:
                self.inflight = (seq, time.monotonic() + min(120, (req['deadline']-now_ms())/1000))
            value = self.reader.observe(req['context'], self.cancelled)
            need(not self.cancelled.is_set() and self.lease_ok(), 'authority-ended')
            self._reply(seq, dict(v=1, ok=True, value=value))
        except EvidenceFailure:
            self.fault('finalization-evidence-failed', evidence=True)
            self._reply(seq, dict(v=1, ok=False, code='failed'))
        except Exception:
            try:
                self.ev.write(event='finalization-refused', seq=seq)
            except EvidenceFailure:
                self.fault('finalization-evidence-failed', evidence=True)
                self._reply(seq, dict(v=1, ok=False, code='failed'))
                return
            self._reply(seq, dict(v=1, ok=False, code='refused'))
            self.fault('finalization-observer-refused')

    def _reader(self):
        # Same gate frames as Host, with strict UTF-8/duplicate-key/size checks.
        # Phase A's inherited admission and clock translation are not invoked.
        while not self.stopping.is_set():
            line = self.gate.stdout.readline(524289)
            if not line:
                if not self.stopping.is_set():self.fault('gate-eof')
                return
            need(len(line) <= 524288 and line.endswith(b'\n'), 'gate-frame-overflow')
            frame = strict_json(line)
            if frame == {'ready':True}:
                self.ready.set()
            elif isinstance(frame,dict) and set(frame)=={'seq','cancel'} and frame['cancel'] is True:
                self.cancelled.set()
                self.ev.write(event='finalization-cancelled', seq=frame['seq'])
            else:
                self._accept(frame)

    def drain(self, timeout):
        # Join the active reader process before claiming drain. The gate itself
        # is broker-owned and may keep the framing thread blocked until teardown.
        return super().drain(timeout)


def create_finalization_host(*, gate, plan, decision_raw, authorization_sha256, config, evidence, on_fault, lease_ok):
    need(QUALIFIED_FINALIZATION_OBSERVER is not None, 'observer-not-qualified')
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location('finalization_remote', ROOT/'host.py')
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    remote = module.Remote(config, QUALIFIED_FINALIZATION_OBSERVER)
    if isinstance(evidence, (str, Path)):
        evidence = PrivateEvidence(str(evidence))
    reader = Reader(plan=plan, decision_raw=decision_raw, authorization_sha256=authorization_sha256,
                    remote=remote, evidence=evidence)
    return FinalizationHost(gate=gate, reader=reader, evidence=evidence, on_fault=on_fault, lease_ok=lease_ok)
