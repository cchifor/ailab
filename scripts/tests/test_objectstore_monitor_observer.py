"""Bound the temporary monitor observer to named resources and GET-only proxying."""
from datetime import date
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
        self.documents = [doc for doc in yaml.safe_load_all(
            (ROOT / "kubernetes/apps/infrastructure/platform-access/rbac.yaml").read_text())
            if doc and doc.get("metadata", {}).get("name") == NAME]

    def test_only_named_gets_and_get_proxy_in_monitoring(self):
        self.assertEqual(len(self.documents), 2)
        roles = [doc for doc in self.documents if doc["kind"] == "Role"]
        self.assertEqual(len(roles), 1)
        self.assertEqual(roles[0]["rules"], RULES)
        for doc in self.documents:
            self.assertEqual(doc["metadata"]["namespace"], "monitoring")
            annotations = doc["metadata"]["annotations"]
            self.assertEqual(annotations["platform-access.chifor.me/remove-by"], "2026-10-18")
            self.assertLessEqual(date.today(), date(2026, 10, 18),
                                 "Remove expired observer grant (ailab#1247)")
            self.assertEqual(annotations["platform-access.chifor.me/removal-issue"],
                             "https://git.chifor.me/cchifor/ailab/issues/1247")

    def test_only_current_worker_gets_narrow_role(self):
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
