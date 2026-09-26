"""Exercise the real apply helper with isolated Git repositories and fake cloud tools."""
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
        self.stub('sops', '#!/usr/bin/env python3\nprint('+repr(CLIENT_SECRET)+')\n')
        self.stub('tofu', '''#!/usr/bin/env python3
import os,sys,pathlib
args=sys.argv[1:]
cmd=args[1]
with open(os.environ['FIXTURE_CALLS'],'a') as out:out.write(cmd+'\\n')
assert os.environ['TF_VAR_enable_trueswarm_admin']=='true'
assert os.environ['TF_VAR_publish_trueswarm_admin']=='false'
if cmd=='plan':
 pathlib.Path(next(a.split('=',1)[1] for a in args if a.startswith('-out='))).write_text('sensitive-plan-fixture')
elif cmd=='show':print(pathlib.Path(os.environ['FIXTURE_PLAN']).read_text())
elif cmd=='output':print(os.environ['FIXTURE_AUDIENCE'],end='')
elif cmd!='apply':raise SystemExit('Unexpected command')
''')
        self.env = {k:v for k,v in os.environ.items() if k not in ('CI','GITHUB_ACTIONS','GITEA_ACTIONS') and not k.startswith('GIT_')}
        self.env.update(PATH=str(self.bin)+':'+os.environ['PATH'], TRUESWARM_ADMIN_CHECKOUT=str(self.admin), CLOUDFLARE_API_TOKEN='fixture-token', FIXTURE_CALLS=str(self.calls), FIXTURE_PLAN=str(self.plan), FIXTURE_AUDIENCE=AUDIENCE, TMPDIR=str(self.root))

    def git(self, *args, cwd=None):
        return run_process(['git', *args], cwd=cwd or self.admin, check=True, text=True, capture_output=True).stdout.strip()

    def stub(self, name, body):
        p = self.bin / name
        p.write_text(body)
        p.chmod(0o755)

    def resource(self, kind, actions=None, address=None):
        return {'address': address or f'cloudflare_zero_trust_access_{kind}.trueswarm_admin[0]', 'mode':'managed', 'change':{'actions':actions or ['create']}}

    def run_helper(self):
        result = run_process(['bash', str(self.script), '--apply-access'], env=self.env, text=True, capture_output=True)
        self.assertNotIn(CLIENT_SECRET, result.stdout+result.stderr)
        self.assertFalse(list(self.root.glob('tmp.*/access.plan')), 'sensitive temporary plan was left behind')
        return result

    def assert_refused_before_apply(self):
        r = self.run_helper()
        self.assertNotEqual(r.returncode, 0, r.stdout+r.stderr)
        self.assertNotIn('apply', self.calls.read_text().splitlines() if self.calls.exists() else [])

    def test_applies_access_then_commits_and_pushes_audience(self):
        r = self.run_helper()
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        self.assertIn(AUDIENCE, self.workloads.read_text())
        self.assertEqual(self.git('rev-parse','HEAD'), self.git('rev-parse','refs/heads/main',cwd=self.origin))
        self.assertEqual(self.calls.read_text().splitlines(), ['plan','show','apply','output'])

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
