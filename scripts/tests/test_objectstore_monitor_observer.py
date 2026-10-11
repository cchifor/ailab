"""Bound the temporary monitor observer to named resources and GET-only proxying."""
from datetime import date
from copy import deepcopy
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
NAME = "objectstore-monitor-observer-dw2"
RULES = [
    {"apiGroups": ["apps"], "resources": ["deployments"],
     "resourceNames": ["objectstore-tls-probe"], "verbs": ["get"]},
    {"apiGroups": [""], "resources": ["services"],
     "resourceNames": ["objectstore-tls-probe", "kube-prometheus-stack-prometheus"],
     "verbs": ["get"]},
    {"apiGroups": ["monitoring.coreos.com"], "resources": ["probes", "prometheusrules"],
     "resourceNames": ["objectstore-tls"], "verbs": ["get"]},
    {"apiGroups": ["monitoring.coreos.com"], "resources": ["prometheuses"],
     "resourceNames": ["kube-prometheus-stack-prometheus"], "verbs": ["get"]},
    {"apiGroups": ["networking.k8s.io"], "resources": ["networkpolicies"],
     "resourceNames": ["objectstore-tls-probe"], "verbs": ["get"]},
    {"apiGroups": [""], "resources": ["services/proxy"],
     "resourceNames": ["http:kube-prometheus-stack-prometheus:9090"], "verbs": ["get"]},
]


class MonitorObserverTests(unittest.TestCase):
    def setUp(self):
        self.all_documents = list(yaml.safe_load_all(
            (ROOT / "kubernetes/apps/infrastructure/platform-access/rbac.yaml").read_text()))
        self.documents = [doc for doc in self.all_documents
            if doc and doc.get("metadata", {}).get("name") == NAME]

    def test_only_named_gets_and_get_proxy_in_monitoring(self):
        self.assertEqual(len(self.documents), 2)
        roles = [doc for doc in self.documents if doc["kind"] == "Role"]
        self.assertEqual(len(roles), 1)
        self.assertEqual(roles[0]["rules"], RULES)
        for doc in self.documents:
            self.assertEqual(doc["metadata"]["namespace"], "monitoring")
            annotations = doc["metadata"]["annotations"]
            self.assertEqual(annotations["platform-access.chifor.me/removal-issue"],
                             "https://git.chifor.me/cchifor/ailab/issues/1247")

    def test_removal_deadline_is_consistent_and_not_expired(self):
        deadlines = {doc["metadata"]["annotations"]["platform-access.chifor.me/remove-by"]
                     for doc in self.documents}
        self.assertEqual(deadlines, {"2026-10-18"})
        self.assertLessEqual(date.today(), date.fromisoformat(next(iter(deadlines))),
                             "Remove expired observer grant (ailab#1247)")

    def assert_worker_scope(self):
        bindings = [doc for doc in self.all_documents
                    if doc and doc.get("kind") == "RoleBinding"
                    and doc.get("roleRef", {}).get("kind") == "Role"
                    and doc.get("roleRef", {}).get("name") == NAME]
        self.assertEqual(len(bindings), 1)
        self.assertEqual([subject for doc in bindings for subject in doc["subjects"]], [{
            "kind": "ServiceAccount", "name": "platform-dw2", "namespace": "platform-access",
        }])
        self.assertEqual(bindings[0]["roleRef"], {
            "kind": "Role", "name": NAME, "apiGroup": "rbac.authorization.k8s.io",
        })

    def test_only_current_worker_gets_narrow_role(self):
        self.assert_worker_scope()

    def test_differently_named_binding_cannot_grant_another_worker_access(self):
        alias = deepcopy(next(doc for doc in self.documents if doc["kind"] == "RoleBinding"))
        alias["metadata"]["name"] = "extra-monitor-observer"
        alias["subjects"][0]["name"] = "platform-dw3"
        self.all_documents.append(alias)
        with self.assertRaises(AssertionError):
            self.assert_worker_scope()


if __name__ == "__main__":
    unittest.main()
