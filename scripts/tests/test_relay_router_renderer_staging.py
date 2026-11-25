"""The Pi canary's renderer staging inputs on dev-worker-1 (docs/runbooks/inference-canary-v0113/renderer-inputs.json)
and the dedicated reader identities' SOPS file (ansible/secrets/relay-router.sops.yaml).

The SOPS rule must precede the generic ansible/secrets rule, or role_id/secret_id would be committed in PLAINTEXT
(that rule encrypts only github_app_private_key); every committed credential leaf, for every host, must be ENC[...]."""
import json
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INPUTS = ROOT / "docs/runbooks/inference-canary-v0113/renderer-inputs.json"
SECRETS = ROOT / "ansible/secrets/relay-router.sops.yaml"
SHA = r"[0-9a-f]{64}"


def first_rule(rules, path):
    for r in rules:
        if re.search(r["path_regex"], path):
            return r
    raise AssertionError("no .sops.yaml creation rule matches %s" % path)


class RendererStaging(unittest.TestCase):
    def test_sops_rule_precedes_the_generic_ansible_rule_and_covers_the_pair(self):
        rules = yaml.safe_load((ROOT / ".sops.yaml").read_text(encoding="utf-8"))["creation_rules"]
        first = first_rule(rules, "ansible/secrets/relay-router.sops.yaml")
        self.assertEqual(first["path_regex"], r"ansible[/\\]secrets[/\\]relay-router\.sops\.ya?ml$")
        for key in ("relay_router_credentials", "role_id", "secret_id"):
            self.assertRegex(key, first["encrypted_regex"])
        # Windows paths too: the rule must win there as well.
        self.assertIs(first_rule(rules, "ansible\\secrets\\relay-router.sops.yaml"), first)

    def test_committed_reader_identities_are_encrypted(self):
        doc = yaml.safe_load(SECRETS.read_text(encoding="utf-8"))
        self.assertIn("sops", doc)
        self.assertEqual(doc["sops"]["encrypted_regex"], "^(relay_router_credentials|role_id|secret_id)$")
        creds = doc["relay_router_credentials"]
        self.assertIn("dev-worker-1", creds)
        # Every host present (the next per-host pair is covered without editing this test).
        for host, pair in creds.items():
            self.assertEqual(set(pair), {"role_id", "secret_id"}, host)
            for key, value in pair.items():
                self.assertTrue(str(value).startswith("ENC["), "%s.%s is not encrypted" % (host, key))

    def test_inputs_stage_without_starting_and_match_the_reader_and_bindings(self):
        d = json.loads(INPUTS.read_text(encoding="utf-8"))
        self.assertIs(d["relay_router_enabled"], True)
        self.assertIs(d["relay_router_manage_services"], False, "staging must not start the renderer services")
        host_vars = yaml.safe_load((ROOT / "ansible/host_vars/dev-worker-1.yml").read_text(encoding="utf-8"))
        self.assertEqual(d["relay_router_host_id"], host_vars["relay_connector_host_id"])
        self.assertEqual((d["relay_router_bao_mount"], d["relay_router_bao_prefix"]), ("af", "relay-router/escrow"))
        # The reader policy the provision Job owns is exactly this tenant/host subtree, read only (whitespace-tolerant).
        job = (ROOT / "kubernetes/apps/infrastructure/security/openbao/relay-router-provision-job.yaml").read_text(encoding="utf-8")
        path = "af/data/relay-router/escrow/%s/%s/*" % (d["relay_router_tenant_id"], d["relay_router_host_id"])
        self.assertRegex(job, r'path\s*"%s"\s*\{\s*capabilities\s*=\s*\[\s*"read"\s*\]\s*\}' % re.escape(path))
        bindings = json.loads((INPUTS.parent / "renderer-bindings.json").read_text(encoding="utf-8"))["relay_router_bindings"]
        for b in bindings:
            self.assertEqual((b["tenantId"], b["hostId"], b["executionIdentity"]),
                             (d["relay_router_tenant_id"], d["relay_router_host_id"], d["relay_router_user"]))
        self.assertEqual(d["relay_router_secrets_file"], "/repo/ansible/secrets/relay-router.sops.yaml")

    def test_binary_pins_come_from_their_single_recorded_source(self):
        d = json.loads(INPUTS.read_text(encoding="utf-8"))
        # bao: the qualified binary pin stated in relay-router-workers.md.
        workers = (ROOT / "docs/runbooks/relay-router-workers.md").read_text(encoding="utf-8")
        bao = re.search(r"binary used in qualification has SHA\s*`(%s)`" % SHA, workers).group(1)
        self.assertEqual(d["relay_router_bao_sha256"], bao)
        # renderer: the pin recorded with its provenance (the release's holder archive) in the canary README.
        readme = (INPUTS.parent / "README.md").read_text(encoding="utf-8")
        renderer = re.search(r"relay-credential-renderer-linux-amd64`.*?SHA-256 `(%s)`" % SHA, readme, re.S).group(1)
        self.assertEqual(d["relay_router_renderer_sha256"], renderer)


if __name__ == "__main__":
    unittest.main()
