"""Keep the temporary recovery observer read-only and confined to one slot."""
from pathlib import Path
from datetime import date
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
NAME = "objectstore-backup-observer-dw2"


class BackupObserverTests(unittest.TestCase):
    def setUp(self):
        source = ROOT / "kubernetes/apps/infrastructure/platform-access/rbac.yaml"
        self.documents = [doc for doc in yaml.safe_load_all(source.read_text())
                          if doc and doc.get("metadata", {}).get("name") == NAME]

    def test_only_backup_status_reads_in_velero(self):
        self.assertEqual(len(self.documents), 2)
        roles = [doc for doc in self.documents if doc["kind"] == "Role"]
        self.assertEqual(len(roles), 1)
        self.assertEqual(roles[0]["rules"], [{
            "apiGroups": ["velero.io"],
            "resources": ["backups", "podvolumebackups", "restores", "podvolumerestores"],
            "verbs": ["get", "list"],
        }])
        for doc in self.documents:
            self.assertEqual(doc["metadata"]["namespace"], "velero")
            annotations = doc["metadata"]["annotations"]
            self.assertEqual(annotations["platform-access.chifor.me/remove-by"], "2026-10-18")
            self.assertLessEqual(
                date.today(), date.fromisoformat(annotations["platform-access.chifor.me/remove-by"]),
                "temporary observer past remove-by; delete the RBAC documents (ailab#1242)",
            )
            self.assertEqual(annotations["platform-access.chifor.me/removal-issue"],
                             "https://git.chifor.me/cchifor/ailab/issues/1242")

    def test_binding_grants_only_the_current_worker_the_narrow_role(self):
        bindings = [doc for doc in self.documents if doc["kind"] == "RoleBinding"]
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["subjects"], [{
            "kind": "ServiceAccount", "name": "platform-dw2", "namespace": "platform-access",
        }])
        self.assertEqual(bindings[0]["roleRef"], {
            "kind": "Role", "name": NAME, "apiGroup": "rbac.authorization.k8s.io",
        })


if __name__ == "__main__":
    unittest.main()
