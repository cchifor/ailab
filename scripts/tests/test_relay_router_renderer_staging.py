"""The Pi canary's renderer staging inputs on dev-worker-1 (docs/runbooks/inference-canary-v0113/renderer-inputs.json)
and the dedicated reader identity's SOPS file (ansible/secrets/relay-router.sops.yaml).

The SOPS rule must precede the generic ansible/secrets rule, or role_id/secret_id would be committed in PLAINTEXT
(that rule encrypts only github_app_private_key); every committed credential leaf must be ENC[...]."""
import json
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INPUTS = ROOT / "docs/runbooks/inference-canary-v0113/renderer-inputs.json"
SECRETS = ROOT / "ansible/secrets/relay-router.sops.yaml"


def rule_index(rules, path):
    for i, r in enumerate(rules):
        if re.search(r["path_regex"], path):
            return i
    return -1


class RendererStaging(unittest.TestCase):
    def test_sops_rule_precedes_the_generic_ansible_rule_and_covers_the_pair(self):
        rules = yaml.safe_load((ROOT / ".sops.yaml").read_text(encoding="utf-8"))["creation_rules"]
        first = rules[rule_index(rules, "ansible/secrets/relay-router.sops.yaml")]
        self.assertEqual(first["path_regex"], r"ansible[/\\]secrets[/\\]relay-router\.sops\.ya?ml$")
        for key in ("relay_router_credentials", "role_id", "secret_id"):
            self.assertRegex(key, first["encrypted_regex"])
        # Windows paths too: the rule must win there as well.
        self.assertIs(rules[rule_index(rules, "ansible\\secrets\\relay-router.sops.yaml")], first)

    def test_committed_reader_identity_is_encrypted(self):
        doc = yaml.safe_load(SECRETS.read_text(encoding="utf-8"))
        self.assertIn("sops", doc)
        creds = doc["relay_router_credentials"]
        self.assertEqual(set(creds), {"dev-worker-1"})
        self.assertEqual(set(creds["dev-worker-1"]), {"role_id", "secret_id"})
        for key, value in creds["dev-worker-1"].items():
            self.assertTrue(str(value).startswith("ENC["), "%s is not encrypted" % key)
        self.assertEqual(doc["sops"]["encrypted_regex"], "^(relay_router_credentials|role_id|secret_id)$")

    def test_inputs_stage_without_starting_and_match_the_reader_and_bindings(self):
        d = json.loads(INPUTS.read_text(encoding="utf-8"))
        self.assertIs(d["relay_router_enabled"], True)
        self.assertIs(d["relay_router_manage_services"], False, "staging must not start the renderer services")
        host_vars = yaml.safe_load((ROOT / "ansible/host_vars/dev-worker-1.yml").read_text(encoding="utf-8"))
        self.assertEqual(d["relay_router_host_id"], host_vars["relay_connector_host_id"])
        self.assertEqual((d["relay_router_bao_mount"], d["relay_router_bao_prefix"]), ("af", "relay-router/escrow"))
        # The reader policy the provision Job owns is exactly this tenant/host subtree.
        job = (ROOT / "kubernetes/apps/infrastructure/security/openbao/relay-router-provision-job.yaml").read_text(encoding="utf-8")
        self.assertIn('path "af/data/relay-router/escrow/%s/%s/*" { capabilities = ["read"] }'
                      % (d["relay_router_tenant_id"], d["relay_router_host_id"]), job)
        bindings = json.loads((INPUTS.parent / "renderer-bindings.json").read_text(encoding="utf-8"))["relay_router_bindings"]
        for b in bindings:
            self.assertEqual((b["tenantId"], b["hostId"], b["executionIdentity"]),
                             (d["relay_router_tenant_id"], d["relay_router_host_id"], d["relay_router_user"]))
        for k in ("relay_router_renderer_sha256", "relay_router_bao_sha256"):
            self.assertRegex(d[k], r"^[0-9a-f]{64}$")
        # Pins from the runbook: OpenBao 2.5.5's linux x86-64 binary.
        self.assertEqual(d["relay_router_bao_sha256"], "4e7e04cc5d13043b9d67850d875242a42fae5c5f01aaff226a6ebba77ccc27bb")
        self.assertEqual(d["relay_router_secrets_file"], "/repo/ansible/secrets/relay-router.sops.yaml")
        self.assertNotIn("secret", json.dumps({k: v for k, v in d.items() if k != "relay_router_secrets_file"}).lower())


if __name__ == "__main__":
    unittest.main()
