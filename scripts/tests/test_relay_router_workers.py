#!/usr/bin/env python3
"""Run the real role twice against disposable local paths, never worker services.

Requires ansible-core, PyYAML and systemd-analyze. Set TMPDIR under /workspace.
No real credentials, systemd changes, sudo, network or inventory are used.
"""
import hashlib
import grp
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import tempfile
import unittest
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "ansible/roles/relay_router_renderer"
SECRET = "relay_fixture_secret_id_never_log"
TOKEN = "relay_fixture_connector_token_never_log"


class WorkerRole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="relay-router-iac-")
        cls.root = Path(cls.temporary.name)
        user = pwd.getpwuid(os.getuid())
        home = cls.root / "home"
        home.mkdir(mode=0o700)
        connector = home / "connector.json"
        host, tenant, binding = [str(uuid.uuid4()) for _ in range(3)]
        connector.write_text(json.dumps(dict(id=host, url="https://relay.example", phase="approved", token=TOKEN)))
        connector.chmod(0o600)
        artifact = cls.root / "fixture-bin"
        # Pin real executable bytes; the role stages them but never executes them.
        artifact.write_bytes(Path("/usr/bin/true").read_bytes())
        artifact.chmod(0o755)
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        cls.variables = dict(
            relay_router_enabled=True,
            relay_router_manage_services=False,
            relay_router_user=user.pw_name,
            relay_router_group=grp.getgrgid(user.pw_gid).gr_name,
            relay_router_home=str(home),
            relay_router_artifact_dir=str(cls.root / "artifacts"),
            relay_router_artifact_owner=user.pw_name,
            relay_router_artifact_group=grp.getgrgid(user.pw_gid).gr_name,
            relay_router_renderer_source=str(artifact), relay_router_renderer_sha256=digest,
            relay_router_bao_source=str(artifact), relay_router_bao_sha256=digest,
            relay_router_origin="https://relay.example", relay_router_tenant_id=tenant,
            relay_router_host_id=host, relay_router_connector_state_file=str(connector),
            relay_router_role_id="relay_fixture_role_id", relay_router_secret_id=SECRET,
            relay_router_bindings=[dict(tenantId=tenant, hostId=host, executionIdentity=user.pw_name,
                connectionId=str(uuid.uuid4()), bindingId=binding, origin="https://router.example",
                credentialFile=str(home / ".local/state/relay/router-managed" / binding / "credential.json"),
                protocol="codex-responses", selection=dict(kind="model", model="qualified-fixture"))],
        )
        cls.play = cls.root / "play.yml"
        cls.play.write_text(yaml.safe_dump([dict(hosts="localhost", gather_facts=False,
            roles=[dict(role="relay_router_renderer")])]))
        cfg = cls.root / "ansible.cfg"
        cfg.write_text("[defaults]\nroles_path=" + str(ROOT / "ansible/roles") + "\nretry_files_enabled=False\n")
        cls.env = {**os.environ, "ANSIBLE_CONFIG": str(cfg), "ANSIBLE_LOCAL_TEMP": str(cls.root / "ansible-tmp"),
                   "ANSIBLE_NOCOLOR": "1"}

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_role(self, updates=None, success=True):
        values = {**self.variables, **(updates or {})}
        extra = self.root / "vars.json"
        extra.write_text(json.dumps(values))
        extra.chmod(0o600)
        result = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(self.play),
            "--diff", "-e", "@" + str(extra)], env=self.env, text=True, capture_output=True, timeout=120)
        output = result.stdout + result.stderr
        self.assertNotIn(SECRET, output)
        self.assertNotIn(TOKEN, output)
        self.assertEqual(result.returncode == 0, success, output[-5000:])
        return output

    def test_01_disabled_role_has_no_side_effects(self):
        output = self.run_role(dict(relay_router_enabled=False, relay_router_renderer_sha256="invalid"))
        self.assertRegex(output, r"changed=0\s")
        self.assertFalse((self.root / "artifacts").exists())

    def test_02_bad_digest_rejected_before_installation(self):
        self.run_role(dict(relay_router_renderer_sha256="a" * 64), success=False)
        self.assertFalse((self.root / "artifacts").exists())

    def test_03_install_and_rerun_are_private_and_idempotent(self):
        self.run_role()
        config_dir = Path(self.variables['relay_router_home']) / ".config/relay/router-managed"
        binding = self.variables["relay_router_bindings"][0]
        config = json.loads((config_dir / (binding['bindingId'] + '.json')).read_text())
        self.assertEqual(config['router'], binding)
        self.assertEqual(config['baoTokenFile'], f"/run/user/{os.getuid()}/relay-router-auth/token")
        self.assertEqual(config['baoCaFile'], str(config_dir / "ca.pem"))
        self.assertNotIn(TOKEN, json.dumps(config))
        self.assertNotIn(SECRET, json.dumps(config))
        for f in config_dir.rglob('*'):
            self.assertEqual(f.stat().st_uid, os.getuid())
            self.assertEqual(f.stat().st_mode & 0o777, 0o700 if f.is_dir() else 0o600)
        self.assertEqual(config_dir.stat().st_mode & 0o777, 0o700)
        self.assertFalse(Path(binding['credentialFile']).exists())  # only the renderer writes credentials
        self.assertRegex(self.run_role(), r"changed=0\s")
        units = list((Path(self.variables['relay_router_home']) / '.config/systemd/user').glob('*.service'))
        self.assertEqual(len(units), 2)
        # Offline unit parsing. No user manager is contacted and no unit is installed.
        runtime = self.root / 'systemd-runtime'
        runtime.mkdir(mode=0o700)
        result = subprocess.run(['systemd-analyze', '--user', 'verify', *map(str, units)],
            text=True, capture_output=True, timeout=15,
            env={**os.environ, 'XDG_RUNTIME_DIR': str(runtime)})
        self.assertEqual(result.returncode, 0, result.stderr)
        auth, render = [(p.name, p.read_text()) for p in sorted(units)]
        self.assertIn('RuntimeDirectoryMode=0700', auth[1])
        self.assertIn('Wants=network-online.target relay-router-auth.service', render[1])
        self.assertNotIn('Requires=relay-router-auth.service', render[1])
        self.assertIn('ReadWritePaths=' + str(Path(binding['credentialFile']).parent), render[1])
        self.assertNotIn('--qualification', render[1])
        policy = (config_dir / 'reader-policy.hcl').read_text()
        self.assertIn(f"/{binding['tenantId']}/{binding['hostId']}/*", policy)
        self.assertEqual(re.findall(r'capabilities = \[(.*?)\]', policy), ['"read"'])

    def test_04_cross_identity_unsafe_path_and_route_rejected(self):
        for update in [dict(hostId=str(uuid.uuid4())), dict(executionIdentity='another-user'),
                       dict(credentialFile='/tmp/elsewhere'), dict(selection=dict(kind='route', alias='agent'))]:
            with self.subTest(update=update):
                b = {**self.variables['relay_router_bindings'][0], **update}
                self.run_role(dict(relay_router_bindings=[b]), success=False)
        self.run_role(dict(relay_router_config_dir=str(self.root) + '/bad%unit'), success=False)

    def test_05_reject_orphaned_binding(self):
        binding = {**self.variables['relay_router_bindings'][0], 'bindingId': str(uuid.uuid4())}
        binding['credentialFile'] = str(Path(self.variables['relay_router_home']) / '.local/state/relay/router-managed' / binding['bindingId'] / 'credential.json')
        self.run_role(dict(relay_router_bindings=[binding]), success=False)

    def test_06_unapproved_or_shared_connector_is_rejected(self):
        state = Path(self.variables['relay_router_connector_state_file'])
        original = state.read_bytes()
        try:
            state.chmod(0o640)
            self.run_role(success=False)
            state.chmod(0o600)
            value = json.loads(original)
            value['phase'] = 'pending'
            state.write_text(json.dumps(value))
            self.run_role(success=False)
        finally:
            state.write_bytes(original)
            state.chmod(0o600)


if __name__ == '__main__':
    unittest.main()
