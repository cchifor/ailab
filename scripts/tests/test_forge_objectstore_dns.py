"""CoreDNS adoption may add only one exact private-name rewrite to the Talos baseline."""

from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
DNS = ROOT / "kubernetes/apps/infrastructure/core-dns"
REWRITE = """    rewrite stop {
        name exact forge-objects.taild43998.ts.net objectstore-forge-tls.strive-ailab.svc.cluster.local
        answer auto
    }

"""


class ForgeObjectstoreDns(unittest.TestCase):
    def test_only_exact_rewrite_is_added_to_authoritative_talos_baseline(self):
        config = yaml.safe_load((DNS / "configmap.yaml").read_text())
        corefile = config["data"]["Corefile"]
        self.assertEqual(corefile.count(REWRITE), 1)
        baseline = (ROOT / "scripts/tests/fixtures/talos-v1.14.2.Corefile").read_text()
        self.assertEqual(corefile.replace(REWRITE, ""), baseline)
        self.assertEqual(config["kind"], "ConfigMap")
        self.assertEqual(config["metadata"]["name"], "coredns")
        self.assertEqual(config["metadata"]["namespace"], "kube-system")
        self.assertEqual(set(config["data"]), {"Corefile"})

    def test_critical_bootstrap_config_cannot_be_pruned(self):
        config = yaml.safe_load((DNS / "configmap.yaml").read_text())
        self.assertEqual(
            config["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"],
            "disabled",
        )
        self.assertNotIn(
            "config.k8s.io/owning-inventory", config["metadata"]["annotations"]
        )

    def test_wired_through_existing_infrastructure_owner(self):
        infra = yaml.safe_load((DNS.parent / "kustomization.yaml").read_text())
        self.assertIn("core-dns", infra["resources"])
        child = yaml.safe_load((DNS / "kustomization.yaml").read_text())
        self.assertEqual(child["resources"], ["configmap.yaml"])


if __name__ == "__main__":
    unittest.main()
