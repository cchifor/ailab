"""Exercise the real apply helper with isolated Git repositories and fake cloud tools."""
import hashlib
import json
import os
from pathlib import Path
import shutil
from subprocess import run as run_process
# Preserve the real runner: test_reviewbot assigns subprocess.run process-wide.
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
AUDIENCE = 'a' * 64
CLIENT_SECRET = 'fixture-only-never-log-this-secret'
TUNNEL = 'd2452442-efae-4056-ac82-a5c348033971'
CF_CLIENT_ID = 'abc123def456.access'
CF_SECRET = 'fixture-cf-service-token-secret-never-log'
SEEDS = 'kubernetes/apps/infrastructure/security/openbao/devworker-seeds.sops.yaml'
SYNC = 'kubernetes/apps/trueswarm-e2e-tokens/token-sync.yaml'


class AccessApplyHelper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        scripts = self.root / 'ailab/scripts'
        scripts.mkdir(parents=True)
        self.script = scripts / 'trueswarm-admin-access.sh'
        shutil.copyfile(ROOT / 'scripts/trueswarm-admin-access.sh', self.script)
        self.cf = self.root / 'ailab/kubernetes/infra/cloudflare'
        self.cf.mkdir(parents=True)
        (self.cf / 'terraform.tfstate').write_text('{"version":4,"resources":[{}]}')
        self.admin = self.root / 'admin'
        self.admin.mkdir()
        self.origin = self.root / 'origin.git'
        self.git('init', '--bare', str(self.origin), cwd=self.root)
        self.git('init', '-b', 'main')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        self.workloads = self.admin / 'deploy/ailab/workloads.yaml'
        self.workloads.parent.mkdir(parents=True)
        self.workloads.write_text('data:\n  ACCESS_AUDIENCE: awaiting-access\nmetadata:\n  annotations:\n    trueswarm.chifor.me/access-config: awaiting-access\n')
        self.git('add', '.')
        self.git('commit', '-m', 'fixture')
        self.git('remote', 'add', 'origin', str(self.origin))
        self.git('push', '-u', 'origin', 'main')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.calls = self.root / 'calls'
        self.plan = self.root / 'plan.json'
        self.plan.write_text(json.dumps({'resource_changes': [self.resource('application')]}))
        self.stub('sops', '''#!/usr/bin/env python3
import sys,pathlib
args=sys.argv[1:]
if '--extract' in args:print('''+repr(CLIENT_SECRET)+''')
elif args[0] in ('decrypt','encrypt'):print(pathlib.Path(args[-1]).read_text(),end='')
else:raise SystemExit('Unexpected sops call')
''')
        self.stub('tofu', '''#!/usr/bin/env python3
import os,sys,pathlib
args=sys.argv[1:]
cmd=args[1]
with open(os.environ['FIXTURE_CALLS'],'a') as out:out.write(cmd+'\\n')
if cmd=='plan':
 with open(os.environ['FIXTURE_CALLS']+'.plan-args','w') as out:out.write(' '.join(args))
assert os.environ['TF_VAR_enable_trueswarm_admin']=='true'
assert os.environ['TF_VAR_publish_trueswarm_admin']==os.environ.get('FIXTURE_PUBLISH','false')
if cmd=='plan':
 pathlib.Path(next(a.split('=',1)[1] for a in args if a.startswith('-out='))).write_text('sensitive-plan-fixture')
elif cmd=='show':print(pathlib.Path(os.environ['FIXTURE_PLAN']).read_text())
elif cmd=='output' and args[-1]=='trueswarm_admin_e2e_access_client_id':print(os.environ['FIXTURE_CF_CLIENT_ID'],end='')
elif cmd=='output' and args[-1]=='trueswarm_admin_e2e_access_client_secret':print(os.environ['FIXTURE_CF_SECRET'],end='')
elif cmd=='output':print(os.environ['FIXTURE_AUDIENCE'],end='')
elif cmd!='apply':raise SystemExit('Unexpected command')
''')
        self.env = {k:v for k,v in os.environ.items() if k not in ('CI','GITHUB_ACTIONS','GITEA_ACTIONS') and not k.startswith('GIT_')}
        self.env.update(PATH=str(self.bin)+':'+os.environ['PATH'], TRUESWARM_ADMIN_CHECKOUT=str(self.admin), CLOUDFLARE_API_TOKEN='fixture-token', FIXTURE_CALLS=str(self.calls), FIXTURE_PLAN=str(self.plan), FIXTURE_AUDIENCE=AUDIENCE, FIXTURE_CF_CLIENT_ID=CF_CLIENT_ID, FIXTURE_CF_SECRET=CF_SECRET, TMPDIR=str(self.root))

    def git(self, *args, cwd=None):
        return run_process(['git', *args], cwd=cwd or self.admin, check=True, text=True, capture_output=True).stdout.strip()

    def stub(self, name, body):
        p = self.bin / name
        p.write_text(body)
        p.chmod(0o755)

    def resource(self, kind, actions=None, address=None):
        return {'address': address or f'cloudflare_zero_trust_access_{kind}.trueswarm_admin[0]', 'mode':'managed', 'change':{'actions':actions or ['create']}}

    def run_helper(self, mode="--apply-access"):
        result = run_process(['bash', str(self.script), mode], env=self.env, text=True, capture_output=True)
        self.assertNotIn(CLIENT_SECRET, result.stdout+result.stderr)
        self.assertNotIn(CF_SECRET, result.stdout+result.stderr)
        self.assertFalse(list(self.root.glob('tmp.*/access.plan')), 'sensitive temporary plan was left behind')
        return result

    def assert_refused_before_apply(self, mode="--apply-access"):
        r = self.run_helper(mode)
        self.assertNotEqual(r.returncode, 0, r.stdout+r.stderr)
        self.assertNotIn('apply', self.calls.read_text().splitlines() if self.calls.exists() else [])

    def test_applies_access_then_commits_and_pushes_audience(self):
        r = self.run_helper()
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        self.assertIn(AUDIENCE, self.workloads.read_text())
        self.assertEqual(self.git('rev-parse','HEAD'), self.git('rev-parse','refs/heads/main',cwd=self.origin))
        self.assertEqual(self.calls.read_text().splitlines(), ['plan','show','apply','output'])

    def test_audience_commit_changes_only_the_two_bound_lines(self):
        self.assertEqual(self.run_helper().returncode, 0)
        self.assertNotIn(b'\r', self.workloads.read_bytes())
        self.assertEqual(self.git('diff','--numstat','HEAD~1','HEAD').split()[:2], ['2','2'])

    def test_rerun_does_not_create_an_empty_commit(self):
        self.assertEqual(self.run_helper().returncode, 0)
        previous = self.git('rev-parse','HEAD')
        self.assertEqual(self.run_helper().returncode, 0)
        self.assertEqual(previous, self.git('rev-parse','HEAD'))

    def test_rejects_ci_before_any_cloud_operation(self):
        self.env['GITEA_ACTIONS']='true'
        self.assert_refused_before_apply()
        self.assertFalse(self.calls.exists())

    def test_rejects_missing_existing_state(self):
        (self.cf/'terraform.tfstate').unlink()
        self.assert_refused_before_apply()

    def test_rejects_missing_token(self):
        del self.env['CLOUDFLARE_API_TOKEN']
        self.assert_refused_before_apply()

    def test_rejects_dirty_private_checkout(self):
        self.workloads.write_text(self.workloads.read_text()+'# local edit\n')
        self.assert_refused_before_apply()

    def test_rejects_non_main_private_checkout(self):
        self.git('checkout','-b','unrelated')
        self.assert_refused_before_apply()

    def test_rejects_dns_publication(self):
        self.plan.write_text(json.dumps({'resource_changes':[self.resource('application',address='cloudflare_dns_record.trueswarm_admin[0]')]}))
        self.assert_refused_before_apply()

    def test_rejects_unrelated_access_changes(self):
        self.plan.write_text(json.dumps({'resource_changes':[self.resource('application',address='cloudflare_zero_trust_access_application.other')]}))
        self.assert_refused_before_apply()

    def test_rejects_deletion_and_replacement(self):
        for actions in [['delete'],['delete','create']]:
            with self.subTest(actions=actions):
                self.plan.write_text(json.dumps({'resource_changes':[self.resource('application',actions=actions)]}))
                self.assert_refused_before_apply()

    def test_invalid_audience_never_changes_git(self):
        self.env['FIXTURE_AUDIENCE']='invalid'
        previous=self.git('rev-parse','HEAD')
        self.assertNotEqual(self.run_helper().returncode, 0)
        self.assertEqual(previous,self.git('rev-parse','HEAD'))
        self.assertEqual('',self.git('status','--porcelain'))

    def publication_fixture(self):
        self.env['FIXTURE_PUBLISH']='true'
        self.workloads.write_text('data:\n  ACCESS_AUDIENCE: "'+AUDIENCE+'"\nmetadata:\n  annotations:\n    trueswarm.chifor.me/access-config: '+hashlib.sha256(AUDIENCE.encode()).hexdigest()[:16]+'\n')
        self.workloads.write_text(self.workloads.read_text()+'tunnel: '+TUNNEL+'\n')
        self.git('add', '.')
        self.git('commit', '-m', 'audience is deployed')
        self.git('push', 'origin', 'main')
        resources=[self.resource(kind,actions=['no-op']) for kind in ['identity_provider','policy','application']]
        resources[-1]['change']['after']={'aud':AUDIENCE}
        resources.append(self.resource('application', address='cloudflare_dns_record.trueswarm_admin[0]'))
        resources[-1]['change']['after']={'name':'trueswarm-admin.chifor.me','type':'CNAME','proxied':True,'content':TUNNEL+'.cfargotunnel.com'}
        return resources

    def test_publication_only_applies_dns_and_does_not_modify_git(self):
        resources=self.publication_fixture()
        self.plan.write_text(json.dumps({'resource_changes':resources}))
        before=self.git('rev-parse','HEAD')
        r=self.run_helper('--publish-dns')
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertEqual(self.calls.read_text().splitlines(),['plan','show','apply'])
        self.assertEqual(before,self.git('rev-parse','HEAD'))
        self.assertEqual('',self.git('status','--porcelain'))

    def test_publication_rejects_access_changes(self):
        resources=self.publication_fixture()
        resources[1]['change']['actions']=['update']
        self.plan.write_text(json.dumps({'resource_changes':resources}))
        self.assert_refused_before_apply('--publish-dns')

    def test_publication_rejects_missing_access_resources(self):
        resources=self.publication_fixture()
        self.plan.write_text(json.dumps({'resource_changes':resources[1:]}))
        self.assert_refused_before_apply('--publish-dns')

    def test_publication_rejects_a_different_audience(self):
        resources=self.publication_fixture()
        resources[2]['change']['after']['aud']='b'*64
        self.plan.write_text(json.dumps({'resource_changes':resources}))
        self.assert_refused_before_apply('--publish-dns')

    def test_publication_rejects_a_missing_audience(self):
        resources=self.publication_fixture()
        resources[2]['change']['after']={}
        self.plan.write_text(json.dumps({'resource_changes':resources}))
        self.assert_refused_before_apply('--publish-dns')

    def test_publication_rejects_an_outdated_rollout_marker(self):
        resources=self.publication_fixture()
        self.workloads.write_text(self.workloads.read_text().replace(hashlib.sha256(AUDIENCE.encode()).hexdigest()[:16],'awaiting-access'))
        self.git('add','.')
        self.git('commit','-m','stale rollout marker')
        self.git('push','origin','main')
        self.plan.write_text(json.dumps({'resource_changes':resources}))
        self.assert_refused_before_apply('--publish-dns')

    def test_publication_rejects_dns_deletion(self):
        resources=self.publication_fixture()
        resources[-1]['change']['actions']=['delete']
        self.plan.write_text(json.dumps({'resource_changes':resources}))
        self.assert_refused_before_apply('--publish-dns')

    def test_publication_rejects_unrelated_dns_changes(self):
        resources=self.publication_fixture()
        resources.append(self.resource('application',address='cloudflare_dns_record.other'))
        self.plan.write_text(json.dumps({'resource_changes':resources}))
        self.assert_refused_before_apply('--publish-dns')

    def test_publication_rejects_a_plan_without_the_dns_record(self):
        resources=self.publication_fixture()
        self.plan.write_text(json.dumps({'resource_changes':resources[:-1]}))
        self.assert_refused_before_apply('--publish-dns')

    def test_publication_rejects_wrong_dns_routing(self):
        resources=self.publication_fixture()
        for field,value in [('name','another.chifor.me'),('proxied',False),('content','another.cfargotunnel.com')]:
            with self.subTest(field=field):
                changed=json.loads(json.dumps(resources))
                changed[-1]['change']['after'][field]=value
                self.plan.write_text(json.dumps({'resource_changes':changed}))
                self.assert_refused_before_apply('--publish-dns')

    def test_publication_accepts_existing_unchanged_dns(self):
        resources=self.publication_fixture()
        resources[-1]['change']['actions']=['no-op']
        self.plan.write_text(json.dumps({'resource_changes':resources}))
        r=self.run_helper('--publish-dns')
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)

    # ---- --apply-e2e-access (ADR 0035) -------------------------------------------------------------
    def e2e_fixture(self):
        """The live, published gate plus the e2e additions; and an ailab git tree holding the seed file
        (plaintext here: the sops stub's "crypto" is the identity) and token-sync.yaml."""
        if getattr(self, '_e2e_ready', False):
            # Repeat call inside a subTest loop: same plan, git fixtures already in place.
            resources = [self.resource(kind, actions=['no-op']) for kind in ['identity_provider', 'policy', 'application']]
            resources.append(self.resource('application', address='cloudflare_dns_record.trueswarm_admin[0]'))
        else:
            resources = self.publication_fixture()
        resources[-1]['change']['actions'] = ['no-op']
        # The shape a real provider-5 plan has: full before/after for the application, the new policy's
        # id unknown until apply, the human policy's id known on its no-op change.
        resources[1]['change']['before'] = resources[1]['change']['after'] = {'id': 'human-policy-id', 'decision': 'allow'}
        resources.pop()  # targeted plan: the DNS record is not in it
        common = {'aud': AUDIENCE, 'domain': 'trueswarm-admin.chifor.me', 'allowed_idps': ['idp-id'],
                  'session_duration': '1h', 'auto_redirect_to_identity': True}
        human = {'id': 'human-policy-id', 'precedence': 1}
        app = resources[2]['change']
        app['actions'] = ['update']
        app['before'] = dict(common, policies=[dict(human)])
        app['after'] = dict(common, policies=[dict(human), {'precedence': 2}])
        app['after_unknown'] = {'policies': [{}, {'id': True}], 'allowed_idps': [False],
                                'destinations': [{}], 'self_hosted_domains': [False]}
        token = self.resource('x', address='cloudflare_zero_trust_access_service_token.trueswarm_admin_e2e[0]')
        token['change']['after'], token['change']['after_unknown'] = {'name': 'trueswarm-admin-e2e'}, {'id': True, 'client_id': True}
        policy = self.resource('x', address='cloudflare_zero_trust_access_policy.trueswarm_admin_e2e[0]')
        policy['change']['after'] = {'decision': 'non_identity', 'require': None, 'exclude': None, 'include': [
            dict({k: None for k in ('everyone', 'email', 'any_valid_service_token', 'group', 'ip', 'login_method')},
                 service_token={})]}
        policy['change']['after_unknown'] = {'id': True, 'include': [{'service_token': {'token_id': True}}]}
        resources += [token, policy]
        ailab = self.root / 'ailab'
        if getattr(self, '_e2e_ready', False):
            self.plan.write_text(json.dumps({'resource_changes': resources}))
            return resources
        self._e2e_ready = True
        (ailab / SEEDS).parent.mkdir(parents=True, exist_ok=True)
        (ailab / SEEDS).write_text('# header comment — that must survive\napiVersion: v1\nkind: Secret\nstringData:\n    common.json: \'{"gitea_pat":"pat-fixture"}\'\n    other.json: \'{"k":"v"}\'\n', encoding='utf-8')
        (ailab / SYNC).parent.mkdir(parents=True, exist_ok=True)
        (ailab / SYNC).write_text('a:\n  - { name: ADMIN_ACCESS_CLIENT_IDS, value: "" }\nb:\n  - { name: ADMIN_ACCESS_CLIENT_IDS, value: "" }\n')
        self.git('init', '-b', 'main', cwd=ailab)
        self.git('-c', 'user.email=f@x', '-c', 'user.name=f', 'add', '.', cwd=ailab)
        self.git('-c', 'user.email=f@x', '-c', 'user.name=f', 'commit', '-m', 'fixture', cwd=ailab)
        self.plan.write_text(json.dumps({'resource_changes': resources}))
        return resources

    def test_e2e_applies_then_seeds_common_and_the_sync(self):
        self.e2e_fixture()
        r = self.run_helper('--apply-e2e-access')
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), ['plan', 'show', 'apply', 'output', 'output'])
        import yaml
        seeds = (self.root / 'ailab' / SEEDS).read_text(encoding='utf-8')
        self.assertTrue(seeds.startswith('# header comment — that must survive\n'), seeds[:60])
        doc = yaml.safe_load(seeds)
        common = json.loads(doc['stringData']['common.json'])
        self.assertEqual(common, {'gitea_pat': 'pat-fixture', 'trueswarm_admin_access_client_id': CF_CLIENT_ID,
                                  'trueswarm_admin_access_client_secret': CF_SECRET})
        self.assertEqual(doc['stringData']['other.json'], '{"k":"v"}')
        self.assertEqual((self.root / 'ailab' / SYNC).read_text().count('value: "%s"' % CF_CLIENT_ID), 2)
        self.assertFalse(list(self.root.glob('tmp.*/seeds.yaml')), 'plaintext seed document was left behind')

    def test_e2e_never_commits_or_touches_the_private_checkout(self):
        self.e2e_fixture()
        before = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.run_helper('--apply-e2e-access').returncode, 0)
        self.assertEqual(before, self.git('rev-parse', 'HEAD'))
        self.assertEqual('', self.git('status', '--porcelain'))

    def test_e2e_rejects_changes_to_the_human_gate(self):
        for index in (0, 1):  # identity provider, human policy
            with self.subTest(index=index):
                resources = self.e2e_fixture()
                resources[index]['change']['actions'] = ['update']
                self.plan.write_text(json.dumps({'resource_changes': resources}))
                self.assert_refused_before_apply('--apply-e2e-access')

    def with_dns(self, change):
        resources = self.e2e_fixture()
        resources.append({'address': 'cloudflare_dns_record.trueswarm_admin[0]', 'mode': 'managed', 'change': change})
        self.plan.write_text(json.dumps({'resource_changes': resources}))

    def test_e2e_accepts_the_dns_record_untouched(self):
        self.with_dns({'actions': ['no-op']})
        self.assertEqual(self.run_helper('--apply-e2e-access').returncode, 0)

    def test_e2e_refuses_a_changed_dns_record(self):
        self.with_dns({'actions': ['update']})
        self.assert_refused_before_apply('--apply-e2e-access')

    def test_e2e_refuses_importing_the_dns_record(self):
        self.with_dns({'actions': ['no-op'], 'importing': {'id': 'zone/record'}})
        self.assert_refused_before_apply('--apply-e2e-access')

    def test_e2e_plans_only_its_own_resources(self):
        self.e2e_fixture()
        self.assertEqual(self.run_helper('--apply-e2e-access').returncode, 0)
        args = (self.root / 'calls.plan-args').read_text().split()
        self.assertEqual(sorted(a for a in args if a.startswith('-target=')), [
            '-target=cloudflare_zero_trust_access_application.trueswarm_admin',
            '-target=cloudflare_zero_trust_access_identity_provider.trueswarm_admin',
            '-target=cloudflare_zero_trust_access_policy.trueswarm_admin',
            '-target=cloudflare_zero_trust_access_policy.trueswarm_admin_e2e',
            '-target=cloudflare_zero_trust_access_service_token.trueswarm_admin_e2e'])

    def test_other_modes_do_not_target(self):
        self.assertEqual(self.run_helper().returncode, 0)
        self.assertNotIn('-target', (self.root / 'calls.plan-args').read_text())

    def test_an_unrelated_import_is_a_change_in_every_mode(self):
        resources = self.e2e_fixture()
        resources.append({'address': 'cloudflare_dns_record.tunnel["relay"]', 'mode': 'managed',
                          'change': {'actions': ['no-op'], 'importing': {'id': 'zone/record'}}})
        self.plan.write_text(json.dumps({'resource_changes': resources}))
        self.assert_refused_before_apply('--apply-e2e-access')

    def test_e2e_rejects_creating_or_replacing_the_application(self):
        for actions in (['create'], ['delete', 'create']):
            with self.subTest(actions=actions):
                resources = self.e2e_fixture()
                resources[2]['change']['actions'] = actions
                self.plan.write_text(json.dumps({'resource_changes': resources}))
                self.assert_refused_before_apply('--apply-e2e-access')

    def test_e2e_rejects_a_different_audience(self):
        resources = self.e2e_fixture()
        resources[2]['change']['after']['aud'] = 'b' * 64
        self.plan.write_text(json.dumps({'resource_changes': resources}))
        self.assert_refused_before_apply('--apply-e2e-access')

    def test_e2e_requires_the_human_policy_first(self):
        resources = self.e2e_fixture()
        resources[2]['change']['after']['policies'] = [{'precedence': 2}, {'precedence': 1}]
        self.plan.write_text(json.dumps({'resource_changes': resources}))
        self.assert_refused_before_apply('--apply-e2e-access')

    def test_e2e_rejects_unrelated_resources_and_deleting_its_own(self):
        resources = self.e2e_fixture()
        resources.append(self.resource('x', address='cloudflare_zero_trust_access_service_token.api[0]'))
        self.plan.write_text(json.dumps({'resource_changes': resources}))
        self.assert_refused_before_apply('--apply-e2e-access')
        resources = self.e2e_fixture()
        resources[-1]['change']['actions'] = ['delete']
        self.plan.write_text(json.dumps({'resource_changes': resources}))
        self.assert_refused_before_apply('--apply-e2e-access')

    def test_e2e_refuses_local_edits_to_the_files_it_writes(self):
        self.e2e_fixture()
        sync = self.root / 'ailab' / SYNC
        sync.write_text(sync.read_text() + '# local edit\n')
        self.assert_refused_before_apply('--apply-e2e-access')
        self.assertFalse(self.calls.exists())

    def test_e2e_rejects_any_other_application_field_change(self):
        for field, value in [('allowed_idps', ['idp-id', 'otp']), ('session_duration', '24h'),
                             ('auto_redirect_to_identity', False), ('domain', 'other.chifor.me')]:
            with self.subTest(field=field):
                resources = self.e2e_fixture()
                resources[2]['change']['after'][field] = value
                self.plan.write_text(json.dumps({'resource_changes': resources}))
                self.assert_refused_before_apply('--apply-e2e-access')

    def test_e2e_rejects_an_application_field_going_unknown(self):
        resources = self.e2e_fixture()
        resources[2]['change']['after_unknown']['allowed_idps'] = True
        self.plan.write_text(json.dumps({'resource_changes': resources}))
        self.assert_refused_before_apply('--apply-e2e-access')

    def test_e2e_rejects_a_substituted_human_policy(self):
        resources = self.e2e_fixture()
        resources[2]['change']['after']['policies'][0]['id'] = 'someone-elses-policy'
        self.plan.write_text(json.dumps({'resource_changes': resources}))
        self.assert_refused_before_apply('--apply-e2e-access')

    def test_e2e_rejects_an_added_policy_that_is_not_the_planned_one(self):
        resources = self.e2e_fixture()
        resources[2]['change']['after']['policies'][1]['id'] = 'an-existing-permissive-policy'
        resources[2]['change']['after_unknown'] = {'policies': [{}, {}]}
        self.plan.write_text(json.dumps({'resource_changes': resources}))
        self.assert_refused_before_apply('--apply-e2e-access')

    def test_e2e_rejects_an_inline_rule_in_the_policy_list(self):
        resources = self.e2e_fixture()
        resources[2]['change']['after']['policies'][1]['include'] = [{'everyone': {}}]
        self.plan.write_text(json.dumps({'resource_changes': resources}))
        self.assert_refused_before_apply('--apply-e2e-access')

    def test_e2e_rejects_a_widened_or_identity_e2e_policy(self):
        for change in ({'decision': 'allow'}, {'include': [{'everyone': {}}]},
                       {'include': [{'everyone': {}, 'service_token': {}}]},
                       {'include': [{'any_valid_service_token': {}, 'service_token': {}}]},
                       {'require': [{'email': {'email': 'x@y.z'}}]},
                       {'include': [{'service_token': {}}, {'email': {'email': 'x@y.z'}}]}):
            with self.subTest(change=change):
                resources = self.e2e_fixture()
                resources[-1]['change']['after'].update(change)
                self.plan.write_text(json.dumps({'resource_changes': resources}))
                self.assert_refused_before_apply('--apply-e2e-access')

    def test_e2e_rejects_an_e2e_policy_pointing_at_another_token(self):
        resources = self.e2e_fixture()
        resources[-1]['change']['after']['include'] = [{'service_token': {'token_id': 'api-token-id'}}]
        resources[-1]['change']['after_unknown'] = {'id': True}
        self.plan.write_text(json.dumps({'resource_changes': resources}))
        self.assert_refused_before_apply('--apply-e2e-access')

    def test_e2e_rerun_with_everything_in_place_is_accepted(self):
        resources = self.e2e_fixture()
        app = resources[2]['change']
        app['actions'] = ['no-op']
        app['after']['policies'][1]['id'] = 'e2e-policy-id'
        app['before']['policies'].append({'id': 'e2e-policy-id', 'precedence': 2})
        app['after_unknown'] = {}
        resources[-2]['change'] = {'actions': ['no-op'], 'before': {'id': 'tok'}, 'after': {'id': 'tok'}}
        resources[-1]['change'] = {'actions': ['no-op'], 'before': {}, 'after': {
            'id': 'e2e-policy-id', 'decision': 'non_identity', 'include': [{'service_token': {'token_id': 'tok'}}]}}
        self.plan.write_text(json.dumps({'resource_changes': resources}))
        r = self.run_helper('--apply-e2e-access')
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)



class HelperEncoding(unittest.TestCase):
    def test_every_embedded_read_is_utf8(self):
        """The helper runs on the Windows operator workstation, where a bare read_text() decodes with
        cp1252: the first live --apply-e2e-access turned the seed file's em dashes into mojibake
        (2026-10-04). Every read in the helper's embedded Python must name its encoding."""
        import re
        helper = (ROOT / 'scripts/trueswarm-admin-access.sh').read_text(encoding='utf-8')
        self.assertIn("read_text(encoding='utf-8')", helper)
        self.assertEqual(re.findall(r"read_text\((?!encoding='utf-8')[^)]*\)", helper), [])
