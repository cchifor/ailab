"""Confine restore planning observation to four named objects and one worker."""
from copy import deepcopy
from datetime import date
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
NAME = "objectstore-restore-observer-dw2"
SUBJECT = {"kind": "ServiceAccount", "name": "platform-dw2", "namespace": "platform-access"}


class RestoreObserverTests(unittest.TestCase):
    def setUp(self):
        self.all_documents = [doc for doc in yaml.safe_load_all(
            (ROOT / "kubernetes/apps/infrastructure/platform-access/rbac.yaml").read_text()) if doc]
        self.documents = [doc for doc in self.all_documents if doc["metadata"]["name"] == NAME]

    def test_exact_named_gets_only(self):
        self.assertEqual(len(self.documents), 4)
        by_kind = {doc["kind"]: doc for doc in self.documents}
        self.assertEqual(set(by_kind), {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"})
        self.assertEqual(by_kind["Role"]["metadata"]["namespace"], "velero")
        self.assertEqual(by_kind["Role"]["rules"], [
            {"apiGroups": ["apps"], "resources": ["deployments"],
             "resourceNames": ["velero"], "verbs": ["get"]},
            {"apiGroups": ["apps"], "resources": ["daemonsets"],
             "resourceNames": ["node-agent"], "verbs": ["get"]},
        ])
        self.assertEqual(by_kind["ClusterRole"]["rules"], [
            {"apiGroups": ["storage.k8s.io"], "resources": ["storageclasses"],
             "resourceNames": ["nfs-csi"], "verbs": ["get"]},
            {"apiGroups": [""], "resources": ["persistentvolumes"],
             "resourceNames": ["pvc-73726d9c-d190-4de9-8057-f17bc8996a3b"], "verbs": ["get"]},
        ])
        self.assertNotIn("aggregationRule", by_kind["ClusterRole"])
        self.assertNotIn("namespace", by_kind["ClusterRole"]["metadata"])
        self.assertNotIn("namespace", by_kind["ClusterRoleBinding"]["metadata"])

    def assert_worker_scope(self):
        bindings = [doc for doc in self.all_documents
                    if doc.get("kind") in {"RoleBinding", "ClusterRoleBinding"}
                    and doc.get("roleRef", {}).get("name") == NAME]
        self.assertEqual(len(bindings), 2)
        for binding in bindings:
            self.assertEqual(binding["subjects"], [SUBJECT])
            cluster = binding["kind"] == "ClusterRoleBinding"
            self.assertEqual(binding["roleRef"], {
                "apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole" if cluster else "Role",
                "name": NAME,
            })
            if not cluster:
                self.assertEqual(binding["metadata"]["namespace"], "velero")

    def test_only_current_worker_receives_grants(self):
        self.assert_worker_scope()

    def test_aliased_bindings_cannot_expand_access(self):
        for kind in ("RoleBinding", "ClusterRoleBinding"):
            with self.subTest(kind=kind):
                binding = deepcopy(next(doc for doc in self.documents if doc["kind"] == kind))
                binding["metadata"]["name"] = "unexpected-grant"
                binding["subjects"][0]["name"] = "platform-dw3"
                self.all_documents.append(binding)
                with self.assertRaises(AssertionError):
                    self.assert_worker_scope()
                self.all_documents.pop()

    def test_explicit_unexpired_removal_on_every_document(self):
        self.assertEqual(len(self.documents), 4)
        for doc in self.documents:
            annotations = doc["metadata"]["annotations"]
            self.assertEqual(annotations["platform-access.chifor.me/removal-issue"],
                             "https://git.chifor.me/cchifor/ailab/issues/1242")
            self.assertEqual(annotations["platform-access.chifor.me/remove-by"], "2026-10-18")
            self.assertLessEqual(date.today(), date.fromisoformat("2026-10-18"),
                                 "Remove expired restore observer (ailab#1242)")


if __name__ == "__main__":
    unittest.main()
