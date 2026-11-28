"""Offline fixed-reader tests. Real clock/files/children; synthetic scoped rows."""
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

SCRIPTS=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SCRIPTS))
import relay_finalization_observer as F
spec=importlib.util.spec_from_file_location('file_reader',SCRIPTS/'relay-finalization/host.py')
H=importlib.util.module_from_spec(spec)
spec.loader.exec_module(H)


def uid(n):
    return str(uuid.UUID(int=n))


def fixture(d=False):
    now=F.now_ms()
    profiles=list(F.PROFILES.values())
    launches=[]
    for i in range(11 if d else 10):
        p,b=profiles[i%3]
        launches.append(dict(launchId=uid(100+i),sessionId=uid(200+i),profileId=p,bindingId=b,
                             credentialId=uid(300+i%3),generation=1,launchedAt=F.utc(now-10000)))
    markers=[]
    for i,provider in enumerate(['codex','qwen','claude','claude']+(['claude'] if d else [])):
        run=uid(400+i); profile=F.PROFILES[provider][0]; phase='recovered' if i<3 else 'baseline'
        text='relay-pi-'+provider+'-'+run
        files={'denied':{'state':'absent'},'effects':{'state':'absent'}}
        if i<3:
            files['effects']=dict(state='present',bytes=len(text)+1,sha256=F.sha((text+'\n').encode()),
                                  exactlyOneMarker=True,markerOccurrences=1)
        markers.append(dict(version=1,observedUtc=F.utc(now),provider=provider,profileId=profile,runId=run,
            phase=phase,state='observed',phaseChecksPass=True,files=files,commands={k:
             "umask 077; printf '%s\\n' '"+text+"' >> '/home/c4/.local/state/relay-holder/workspaces/"+
             profile+'/acceptance-'+run+'.'+k+"'" for k in ('denied','effects')}))
    plan=dict(version=1,purpose='canary-finalization',scope=copy.deepcopy(F.SCOPE),
        reportPins={k:F.sha(k.encode()) for k in ['a','b','bInputs','c']+(['d'] if d else [])},
        cleanupDeadline=now+60000,launches=launches,markers=markers,originalIdle=10000,
        sources=dict(hubSource='a'*40,routerSource='b'*40,reportHubSources={k:'c'*40 for k in 'abc'},
                     reportRouterSources={k:'d'*40 for k in 'abc'},observerQualificationSha256='e'*64))
    decision={k:plan[k] for k in ('version','purpose','reportPins','cleanupDeadline')}
    decision.update(runId=uid(999),qualificationSha256=F.sha(F.canonical(plan['sources']).encode()),
                    bindingIds=sorted(F.SCOPE['bindingIds']),noFurtherUse=True)
    raw=F.canonical(decision).encode();plan['decisionSha256']=F.sha(raw)
    credentials=[dict(id=uid(300+i),binding_id=b,generation=1,router_key_id='key_'+str(i+1)*32,
       namespace='default',state='revoked',created_at=F.utc(now-20000),cleanup_pending=False,revoke_requested=True)
       for i,(_,b) in enumerate(profiles)]
    hub=dict(version=1,databaseIdentity='relay',schemaVersion=93,observedMs=now,unresolvedOperations=0,
       launches=[dict(id=l['launchId'],session_id=l['sessionId'],profile_id=l['profileId'],profile_revision=1,
         state='open',created_at=l['launchedAt'],connector_id=F.HOST_ID,
         credential_file='/home/c4/.local/state/relay/router-managed/'+l['bindingId']+'/credential.json') for l in launches],
       sessions=[dict(id=l['sessionId'],state='closed',connector_id=F.HOST_ID,profile_id=l['profileId'],generation=1,
                  rebinding=False,pending_commands=0,pending_permissions=0,control_leases=0) for l in launches],
       bindings=[dict(id=b,connection_id=F.CONNECTION,host_id=F.HOST_ID,execution_identity='c4',generation=1,
                      active_consumers=0) for _,b in profiles],credentials=credentials,installations=[])
    for i,l in enumerate(launches):
        ref=dict(credentialId=l['credentialId'],generation=1,secretVersion=1,keyId=credentials[i%3]['router_key_id'],
                 creationOperationId=uid(500+i%3))
        hub['installations'].append(dict(launch_id=l['launchId'],session_id=l['sessionId'],binding_id=l['bindingId'],
          credential_id=l['credentialId'],generation=1,command_id=uid(600+i),local_reference=ref,
          cleanup_receipt=dict(version=1,commandId=uid(600+i),reference=ref,status='absent'),cleaned_at=F.utc(now)))
    hub['counts']={k:len(hub[k]) for k in ('launches','sessions','bindings','credentials','installations')}
    router=dict(version=1,source='b'*40,observedMs=now,
       settings=dict(warmSessions=False,warmIdleMs=10000,revision='f'*64),processes=[],processEvidence={'complete':True},
       keys=[dict(keyId=c['router_key_id'],keyCreatedAt=1,coveredFrom=1,openRequests=0,staleOpenRequests=0,
              unattributed=0,fenceState='closed',tokenUsageUnknown=2) for c in credentials])
    files=dict(version=1,observedMs=now,managedFiles={b:True for _,b in profiles},markers=copy.deepcopy(markers))
    return plan,raw,hub,router,files


class Evidence:
    def __init__(self):self.records=[]
    def write(self,**kw):self.records.append(copy.deepcopy(kw))


class Remote:
    def __init__(self,h,r,f):self.h,self.r,self.f=h,r,f;self.calls=[]
    def identities(self,b):
        b.left();self.calls.append('identities');return dict(hubSource='a'*40,routerSource='b'*40)
    def hub(self,b):b.left();self.calls.append('hub');return copy.deepcopy(self.h)
    def router(self,c,b):b.left();self.calls.append('router');return copy.deepcopy(self.r)
    def files(self,m,b):b.left();self.calls.append('files');return copy.deepcopy(self.f)


class DecisionContract(unittest.TestCase):
    def test_broker_sorted_decision_preserves_scope_and_raw_bytes(self):
        plan, raw, *_ = fixture()
        original_scope = copy.deepcopy(plan['scope'])
        decision = F.strict_json(raw)
        # prepare_finalization in the broker requires this canonical order.
        decision['bindingIds'] = sorted(F.SCOPE['bindingIds'])
        raw = F.canonical(decision).encode()
        original_raw = bytes(raw)
        plan['decisionSha256'] = F.sha(raw)
        actual = F.validate_plan(plan, raw, F.sha(raw))
        self.assertEqual(actual, plan)
        self.assertEqual(actual['scope'], original_scope)
        self.assertNotEqual(original_scope['bindingIds'], decision['bindingIds'])
        self.assertEqual(raw, original_raw)

    def test_noncanonical_decision_order_refused_even_with_matching_hash(self):
        plan, raw, *_ = fixture()
        decision = F.strict_json(raw)
        decision['bindingIds'] = list(F.SCOPE['bindingIds'])
        self.assertNotEqual(decision['bindingIds'], sorted(decision['bindingIds']))
        raw = F.canonical(decision).encode()
        plan['decisionSha256'] = F.sha(raw)
        with self.assertRaises(F.Refused):
            F.validate_plan(plan, raw, F.sha(raw))


class Projection(unittest.TestCase):
    def setUp(self):
        self.p,self.d,h,r,f=fixture();self.remote=Remote(h,r,f);self.ev=Evidence()
        self.reader=F.Reader(plan=self.p,decision_raw=self.d,authorization_sha256=F.sha(self.d),remote=self.remote,evidence=self.ev)
    def context(self,stage='preflight'):
        return dict(version=1,stage=stage,**{k:copy.deepcopy(self.p[k]) for k in
            ('reportPins','decisionSha256','sources','scope','launches','markers','originalIdle')},
            notBefore=F.now_ms()-1000,deadline=F.now_ms()+30000)
    def test_exact_both_stages_with_unknown_tokens(self):
        for stage in ('preflight','renderer'):
            v=self.reader.observe(self.context(stage),threading.Event())
            self.assertEqual(v['stage'],stage);self.assertTrue(v['noFurtherUse'])
        self.assertEqual(len(self.ev.records),2)
    def test_explicit_d_only_eleven(self):
        p,d,h,r,f=fixture(True)
        reader=F.Reader(plan=p,decision_raw=d,authorization_sha256=F.sha(d),remote=Remote(h,r,f),evidence=self.ev)
        self.p=p;self.assertEqual(len(reader.observe(self.context('renderer'),threading.Event())['launches']),11)
    def test_public_null_before_remote_or_evidence(self):
        with self.assertRaisesRegex(F.Refused,'not-qualified'):
            F.create_finalization_host(gate=None,plan=None,decision_raw=None,authorization_sha256=None,
               config=None,evidence=None,on_fault=None,lease_ok=None)
    def test_decision_hash_is_independent(self):
        with self.assertRaises(F.Refused):F.validate_plan(self.p,self.d,'0'*64)
        forged=json.loads(self.d);forged['noFurtherUse']=False;raw=F.canonical(forged).encode()
        self.p['decisionSha256']=F.sha(raw)
        with self.assertRaises(F.Refused):F.validate_plan(self.p,raw,F.sha(raw))
    def test_request_cannot_expand_or_authorize(self):
        for name,value in [('scope',dict(F.SCOPE,hostId=uid(999))),('noFurtherUse',True),('originalIdle',5000)]:
            ctx=self.context();ctx[name]=value
            with self.subTest(name=name),self.assertRaises(F.Refused):self.reader.observe(ctx,threading.Event())
        self.assertEqual(self.remote.calls,[])
    def test_missing_extra_overflow_duplicate_and_pending_refuse(self):
        mutations=[lambda h:h['sessions'].pop(),lambda h:h['counts'].__setitem__('sessions',1001),
          lambda h:h['sessions'][0].__setitem__('pending_commands',1),
          lambda h:h['sessions'][0].__setitem__('pending_permissions',1),
          lambda h:h['sessions'][0].__setitem__('state','open'),
          lambda h:h['bindings'][0].__setitem__('active_consumers',1),
          lambda h:h['credentials'][0].__setitem__('generation',2),
          lambda h:h['credentials'][0].__setitem__('router_key_id',None),
          lambda h:h.__setitem__('unresolvedOperations',1),lambda h:h.__setitem__('schemaVersion',92)]
        original=copy.deepcopy(self.remote.h)
        for i,change in enumerate(mutations):
            self.remote.h=copy.deepcopy(original);change(self.remote.h)
            with self.subTest(i=i),self.assertRaises((F.Refused,TypeError)):self.reader.observe(self.context(),threading.Event())
    def test_accounting_absence_never_defaulted(self):
        for field,value in [('openRequests',1),('staleOpenRequests',1),('unattributed',1),('fenceState','unknown'),('coveredFrom',2)]:
            r=copy.deepcopy(self.remote.r);self.remote.r['keys'][0][field]=value
            with self.subTest(field=field),self.assertRaises(F.Refused):self.reader.observe(self.context(),threading.Event())
            self.remote.r=r
        self.remote.r['keys'].pop()
        with self.assertRaises(F.Refused):self.reader.observe(self.context(),threading.Event())
    def test_process_or_settings_unknown_refuses(self):
        for field,value in [('processes',[{'pid':1}]),('processEvidence',{'complete':False})]:
            old=self.remote.r[field];self.remote.r[field]=value
            with self.assertRaises(F.Refused):self.reader.observe(self.context(),threading.Event())
            self.remote.r[field]=old
        self.remote.r['settings']['warmSessions']=True
        with self.assertRaises(F.Refused):self.reader.observe(self.context(),threading.Event())
    def test_not_current_retained_and_fresh_absence_independent(self):
        self.remote.h['installations'][0]['cleanup_receipt']['status']='not-current'
        v=self.reader.observe(self.context('renderer'),threading.Event())
        self.assertEqual(v['launches'][0]['status'],'not-current')
        self.remote.f['managedFiles'][F.SCOPE['bindingIds'][0]]=False
        with self.assertRaises(F.Refused):self.reader.observe(self.context('renderer'),threading.Event())
    def test_renderer_exact_reference_and_revocation(self):
        for modify in [lambda:self.remote.h['installations'][0]['cleanup_receipt'].__setitem__('commandId',uid(999)),
                       lambda:self.remote.h['credentials'][0].__setitem__('state','ready'),
                       lambda:self.remote.f['markers'][0]['files']['effects'].__setitem__('bytes',999)]:
            h,r,f=copy.deepcopy((self.remote.h,self.remote.r,self.remote.f));modify()
            with self.assertRaises(F.Refused):self.reader.observe(self.context('renderer'),threading.Event())
            self.remote.h,self.remote.r,self.remote.f=h,r,f
    def test_abort_deadline_and_stale(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaises(F.Refused):self.reader.observe(self.context(),cancel)
        self.assertEqual(self.remote.calls,[])
        ctx=self.context();ctx['deadline']=F.now_ms()-1
        with self.assertRaises(F.Refused):self.reader.observe(ctx,threading.Event())
        self.remote.h['observedMs']=F.now_ms()-130000
        with self.assertRaises(F.Refused):self.reader.observe(self.context(),threading.Event())
    def test_source_replacement_and_failed_durable_write_no_success(self):
        calls=0
        def identities(b):
            nonlocal calls
            calls+=1
            return dict(hubSource='a'*40,routerSource=('b' if calls==1 else 'c')*40)
        self.remote.identities=identities
        with self.assertRaisesRegex(F.Refused,'source-replaced'):self.reader.observe(self.context(),threading.Event())
        self.assertEqual(self.ev.records,[])
        self.remote.identities=lambda b:dict(hubSource='a'*40,routerSource='b'*40)
        def fail(**kw):raise F.EvidenceFailure('synthetic write failure')
        self.ev.write=fail
        with self.assertRaises(F.EvidenceFailure):self.reader.observe(self.context(),threading.Event())


class RemoteSqlFraming(unittest.TestCase):
    def setUp(self):
        self.config=dict(version=1,kubeContext='admin@ai',hub={},router={},devWorker={},
            hubDb=dict(namespace='fixture',pod='postgres',container='db',user='reader',database='relay'))
        qualification=dict(configSha256=F.sha(F.canonical(self.config).encode()),files={
            name:F.sha((H.HERE/name).read_bytes()) for name in ('hub.sql','router.cjs','host.py')})
        self.remote=H.Remote(self.config,qualification)

    def read(self,raw):
        budget=F.Budget(F.now_ms()+10000,threading.Event())
        with patch.object(self.remote,'kubectl',return_value=raw) as call:
            value=self.remote.hub(budget)
        args=call.call_args.args
        self.assertEqual(args[0],'fixture')
        self.assertEqual(args[1],['exec','-i','postgres','-c','db','--','psql','-X','-qAt',
            '-v','ON_ERROR_STOP=1','-U','reader','-d','relay','-f','-'])
        self.assertIs(args[2],budget)
        self.assertEqual(args[3],(H.HERE/'hub.sql').read_bytes())
        return value

    def test_postgres_multiline_single_json_object(self):
        value=dict(databaseIdentity='relay',schemaVersion=93,rows=[dict(id=1),dict(id=2)])
        raw=(json.dumps(value,indent=2)+'\n').encode()
        self.assertGreater(raw.count(b'\n'),1)
        self.assertEqual(self.read(raw),value)

    def test_multiple_roots_duplicates_and_nonobjects_refused(self):
        valid=b'{"databaseIdentity":"relay"}\n'
        for raw in (valid+valid,valid+b'NOTICE\n',b'{"databaseIdentity":"relay","databaseIdentity":"relay"}\n',
                    b'[{"databaseIdentity":"relay"}]\n',b'null\n',b'{"databaseIdentity":"relay","n":NaN}\n'):
            with self.subTest(raw=raw),self.assertRaises(F.Refused):self.read(raw)

    def test_terminator_limit_and_database_identity_retained(self):
        for raw in (b'{"databaseIdentity":"relay"}',b'{"databaseIdentity":"foreign"}\n',
                    b'{"databaseIdentity":"relay","x":"'+b'x'*F.MAX_BYTES+b'"}\n'):
            with self.subTest(size=len(raw)),self.assertRaises(F.Refused):self.read(raw)


class RealFiles(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=Path(self.temp.name);self.plan,*_=fixture()
        for b in H.BINDINGS:(self.home/'.local/state/relay/router-managed'/b).mkdir(parents=True,mode=0o700)
        for m in self.plan['markers']:
            path=self.home/'.local/state/relay-holder/workspaces'/m['profileId'];path.mkdir(parents=True,exist_ok=True,mode=0o700)
            if m['phase']=='recovered':
                file=path/('acceptance-'+m['runId']+'.effects')
                file.write_text('relay-pi-'+m['provider']+'-'+m['runId']+'\n');file.chmod(0o600)
    def tearDown(self):self.temp.cleanup()
    def read(self):return H.file_snapshot(self.plan['markers'],home=str(self.home),deadline=F.now_ms()+10000)
    def test_private_files_actual_bytes_and_absence(self):
        v=self.read();self.assertTrue(all(v['managedFiles'].values()))
        for old,new in zip(self.plan['markers'],v['markers']):
            self.assertEqual(old['files'],new['files']);self.assertEqual(old['commands'],new['commands'])
    def test_credential_present_only_lstat_never_content(self):
        p=self.home/'.local/state/relay/router-managed'/H.BINDINGS[0]/'credential.json'
        os.mkfifo(p,0o600)  # Opening would block; lstat truthfully reports present.
        self.assertFalse(self.read()['managedFiles'][H.BINDINGS[0]])
    def test_missing_directory_and_symlink_refuse(self):
        p=self.home/'.local/state/relay/router-managed'/H.BINDINGS[0];p.rmdir()
        with self.assertRaises(FileNotFoundError):self.read()
        p.symlink_to(self.home,target_is_directory=True)
        with self.assertRaises(OSError):self.read()
    def test_marker_symlink_and_extra_effect_detected(self):
        m=self.plan['markers'][0];p=self.home/'.local/state/relay-holder/workspaces'/m['profileId']/('acceptance-'+m['runId']+'.effects')
        p.write_text('different');p.chmod(0o600)
        self.assertFalse(self.read()['markers'][0]['phaseChecksPass'])
        p.unlink();p.symlink_to('/etc/passwd')
        with self.assertRaises(OSError):self.read()


class SubprocessReader(unittest.TestCase):
    def test_private_exclusive_evidence_and_fsync(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'evidence.jsonl';e=F.PrivateEvidence(p)
            e.write(event='test');e.close()
            self.assertEqual(p.stat().st_mode&0o777,0o600)
            self.assertEqual(json.loads(p.read_text())['event'],'test')
            with self.assertRaises(FileExistsError):F.PrivateEvidence(p)
            os.chmod(d,0o755)
            with self.assertRaises(F.Refused):F.PrivateEvidence(Path(d)/'other')
            os.chmod(d,0o700)
    def test_real_child_success_and_failure_no_stderr_leak(self):
        b=F.Budget(F.now_ms()+10000,threading.Event())
        self.assertEqual(F.run_bounded([sys.executable,'-c','print("ok")'],b'',b),b'ok\n')
        with self.assertRaisesRegex(F.Refused,'reader-failed'):
            F.run_bounded([sys.executable,'-c','import sys;sys.stderr.write("secret");sys.exit(1)'],b'',b)
    def test_output_overflow_stops_actual_child(self):
        b=F.Budget(F.now_ms()+10000,threading.Event())
        with self.assertRaises(F.Refused):F.run_bounded([sys.executable,'-c','print("x"*100000)'],b'',b,100)
    def test_abort_joins_actual_child(self):
        cancel=threading.Event();timer=threading.Timer(.2,cancel.set);timer.start()
        try:
            with self.assertRaisesRegex(F.Refused,'cancelled'):
                F.run_bounded([sys.executable,'-c','import time;time.sleep(10)'],b'',F.Budget(F.now_ms()+5000,cancel))
        finally:timer.cancel()
    def test_native_json_strict(self):
        for raw in (b'{"a":1,"a":2}',b'{"a":NaN}',b'\xff'):
            with self.assertRaises(F.Refused):F.strict_json(raw)


def pg_fixture():
    """Actual queried schema93 columns, synthetic metadata rows; no migration or
    Hub lifecycle claim. Secret columns contain a sentinel that must not escape."""
    p,d,h,r,f=fixture()
    ddl='''
CREATE TABLE schema_migrations(version integer PRIMARY KEY);INSERT INTO schema_migrations VALUES(93);
CREATE TABLE agent_launch_profiles(tenant_id uuid,id uuid,revision integer,connector_id uuid,definition jsonb);
CREATE TABLE agent_session_launches(tenant_id uuid,id uuid,session_id uuid,profile_id uuid,profile_revision integer,state text,created_at timestamptz);
CREATE TABLE agent_sessions(tenant_id uuid,id uuid,state text,connector_id uuid,profile jsonb,generation integer,rebinding boolean);
CREATE TABLE agent_control_commands(tenant_id uuid,id uuid,session_id uuid,state text);
CREATE TABLE agent_commands(tenant_id uuid,id uuid,session_id uuid,delivery text);
CREATE TABLE permission_requests(tenant_id uuid,id uuid,session_id uuid,state text);
CREATE TABLE agent_control_leases(tenant_id uuid,session_id uuid,expires_at timestamptz);
CREATE TABLE router_credential_bindings(tenant_id uuid,id uuid,connection_id uuid,host_id uuid,execution_identity text,generation integer);
CREATE TABLE router_credentials(tenant_id uuid,id uuid,binding_id uuid,generation integer,router_key_id text,namespace text,state text,created_at timestamptz,cleanup_pending boolean,revoke_requested boolean,secret_reference jsonb);
CREATE TABLE router_credential_consumers(tenant_id uuid,id uuid,binding_id uuid,state text);
CREATE TABLE router_credential_operations(tenant_id uuid,id uuid,credential_id uuid,state text);
CREATE TABLE holder_router_installations(tenant_id uuid,launch_id uuid,session_id uuid,binding_id uuid,credential_id uuid,generation integer,command_id uuid,local_reference jsonb,cleanup_receipt jsonb,cleaned_at timestamptz);
'''
    def lit(v):
        if v is None:return 'NULL'
        if isinstance(v,bool):return 'true' if v else 'false'
        if isinstance(v,int):return str(v)
        if isinstance(v,dict):v=json.dumps(v)
        return "'"+v.replace("'","''")+"'"
    def insert(table,row):
        return 'INSERT INTO '+table+'('+','.join(row)+') VALUES('+','.join(map(lit,row.values()))+');\n'
    for profile,binding in F.PROFILES.values():
        ddl+=insert('agent_launch_profiles',dict(tenant_id=F.TENANT,id=profile,revision=1,connector_id=F.HOST_ID,
            definition={'holder':{'router':{'credentialFile':'/home/c4/.local/state/relay/router-managed/'+binding+'/credential.json'}}}))
    for l in h['launches']:
        ddl+=insert('agent_session_launches',dict(tenant_id=F.TENANT,**{k:l[k] for k in
          ('id','session_id','profile_id','profile_revision','state','created_at')}))
    for s in h['sessions']:
        ddl+=insert('agent_sessions',dict(tenant_id=F.TENANT,id=s['id'],state=s['state'],connector_id=s['connector_id'],
          profile={'id':s['profile_id']},generation=1,rebinding=False))
    for b in h['bindings']:
        ddl+=insert('router_credential_bindings',dict(tenant_id=F.TENANT,**{k:v for k,v in b.items() if k!='active_consumers'}))
    for c in h['credentials']:
        ddl+=insert('router_credentials',dict(tenant_id=F.TENANT,**c,secret_reference={'secret':'MUST_NOT_ESCAPE'}))
    for i in h['installations']:ddl+=insert('holder_router_installations',dict(tenant_id=F.TENANT,**i))
    # A foreign-tenant row cannot expand or mask this tenant's exact inventory.
    ddl+=insert('agent_sessions',dict(tenant_id=uid(999),id=uid(998),state='open',connector_id=F.HOST_ID,
           profile={'id':F.PROFILES['codex'][0]},generation=1,rebinding=False))
    return ddl,p


def qualify_postgres(output):
    """Explicit offline gate: cached image only; all argv/results retained."""
    out=Path(output);out.mkdir(parents=True,exist_ok=False)
    name='finalization-pg-'+uuid.uuid4().hex
    image='postgres@sha256:f9cf3e1f0066f0eb0c48707d864f0ac5941e9ff454a072077d97a4d278d07d73'
    log=[];started=False
    def command(args,data=None,check=True):
        p=subprocess.run(args,input=data,capture_output=True,timeout=30)
        log.append(dict(argv=args,returncode=p.returncode,stdout=p.stdout.decode(errors='replace'),stderr=p.stderr.decode(errors='replace')))
        if check and p.returncode:raise AssertionError('private PostgreSQL command failed')
        return p
    def sql(raw):
        return command(['docker','exec','-i',name,'psql','-X','-qAt','-v','ON_ERROR_STOP=1','-U','postgres','-d','relay','-f','-'],raw).stdout
    def query(label):
        raw=sql((F.ROOT/'hub.sql').read_bytes());(out/(label+'.json')).write_bytes(raw)
        assert b'MUST_NOT_ESCAPE' not in raw
        return F.strict_json(raw)
    try:
        command(['docker','image','inspect',image,'--format','{{.Id}}'])
        command(['docker','run','-d','--pull','never','--name',name,'--network','none','--user','70:70','--read-only',
          '--tmpfs','/var/lib/postgresql/data:rw,uid=70,gid=70,mode=0700',
          '--tmpfs','/var/run/postgresql:rw,uid=70,gid=70,mode=0700','--tmpfs','/tmp:rw,mode=1777',
          '-e','POSTGRES_HOST_AUTH_METHOD=trust','-e','POSTGRES_DB=relay',image]);started=True
        end=time.monotonic()+20
        while True:
            final=command(['docker','exec',name,'cat','/proc/1/comm'],check=False)
            ready=command(['docker','exec',name,'pg_isready','-U','postgres','-d','relay'],check=False)
            if final.stdout.strip()==b'postgres' and ready.returncode==0:break
            if time.monotonic()>=end:raise AssertionError('private PostgreSQL not ready')
            time.sleep(.1)
        command(['docker','exec',name,'psql','--version'])
        fixture_sql,plan=pg_fixture();(out/'schema-and-rows.sql').write_text(fixture_sql,encoding='utf-8');sql(fixture_sql.encode())
        ctx={'notBefore':F.now_ms()-120000}
        first=query('complete');F.hub_projection(first,plan,ctx)
        assert len(first['sessions'])==10 and first['counts']['sessions']==10
        sql(("INSERT INTO agent_control_commands VALUES('"+F.TENANT+"','"+uid(700)+"','"+uid(200)+"','unknown');").encode())
        bad=query('pending-command')
        try:F.hub_projection(bad,plan,ctx)
        except F.Refused:pass
        else:raise AssertionError('pending command accepted')
        sql(b'TRUNCATE agent_control_commands;')
        sql(("INSERT INTO agent_sessions VALUES('"+F.TENANT+"','"+uid(900)+"','closed','"+F.HOST_ID+
             "','{\"id\":\""+F.PROFILES['codex'][0]+"\"}',1,false);").encode())
        bad=query('unlisted-session')
        try:F.hub_projection(bad,plan,ctx)
        except F.Refused:pass
        else:raise AssertionError('unlisted session accepted')
        sql(("DELETE FROM agent_sessions WHERE id='"+uid(900)+"';").encode())
        sql(("INSERT INTO agent_sessions SELECT '"+F.TENANT+"'::uuid,md5(n::text)::uuid,'closed','"+F.HOST_ID+
          "'::uuid,'{\"id\":\""+F.PROFILES['codex'][0]+"\"}'::jsonb,1,false FROM generate_series(10000,11000)n;").encode())
        bad=query('overflow');assert bad['counts']['sessions']==1011 and len(bad['sessions'])==1001
        try:F.hub_projection(bad,plan,ctx)
        except F.Refused:pass
        else:raise AssertionError('truncated inventory accepted')
        # Actual SQL transaction rejects writes, even with this synthetic superuser.
        refused=command(['docker','exec','-i',name,'psql','-X','-qAt','-v','ON_ERROR_STOP=1','-U','postgres','-d','relay','-f','-'],
            b'BEGIN READ ONLY;DELETE FROM agent_sessions;COMMIT;',check=False)
        assert refused.returncode!=0
        print('PostgreSQL17 actual fixed SQL: complete/foreign-tenant/pending/unlisted/overflow/read-only PASS')
    finally:
        if started:command(['docker','rm','-f',name],check=False)
        (out/'commands.json').write_text(json.dumps(log,indent=2),encoding='utf-8')


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--postgres':qualify_postgres(sys.argv[2])
    else:unittest.main()
