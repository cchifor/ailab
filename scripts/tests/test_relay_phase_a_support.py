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
import subprocess
import sys
import tempfile
import threading
import time
import unittest

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
