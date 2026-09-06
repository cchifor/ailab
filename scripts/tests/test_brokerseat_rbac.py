#!/usr/bin/env python3
"""Unit tests for kubernetes/apps/infrastructure/agentforge-broker/brokerseat-rbac.yaml (plan A1, D7).

FROZEN VERB TABLES. The two Roles grant exactly the verbs their code paths issue and the safety of
the design rests on what they do NOT grant: the provisioner has no delete (teardown is k8s GC under
the CP's Foreground delete), no watch, no list on any child kind, and nothing on secrets/configmaps/
pods; the control plane has create/get/list/delete on the CR and ONE read (services get) for the
teardown witness — no update/patch, no status, no finalizers. Each table is compared by EQUALITY,
so an added rule, an added verb (`delete`, `watch`, `*`) or a widened resource turns a test red.
The bindings are pinned to the two ServiceAccounts as they are actually declared elsewhere in the
tree (cross-file), so a renamed SA cannot leave a dangling grant.

stdlib-only, read from the real tree (see _brokerseat_support.py). Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
from __future__ import annotations

import unittest

import _brokerseat_support as sup

PROVISIONER_ROLE = "agentforge-provisioner-brokerseats"
CP_ROLE = "af-cp-brokerseats"
BROKER_NS = "agentforge-broker"

PROVISIONER_EXPECTED: dict[tuple[str, str], frozenset[str]] = {
    ("agentforge.io", "brokerseats"): frozenset({"get", "list", "patch"}),
    ("agentforge.io", "brokerseats/status"): frozenset({"patch"}),
    ("agentforge.io", "brokerseats/finalizers"): frozenset({"update"}),
    ("apps", "deployments"): frozenset({"get", "create", "update"}),
    ("", "services"): frozenset({"get", "create", "update"}),
    ("policy", "poddisruptionbudgets"): frozenset({"get", "create", "update"}),
    ("external-secrets.io", "externalsecrets"): frozenset({"get", "create", "update"}),
    ("cilium.io", "ciliumnetworkpolicies"): frozenset({"get", "create", "update"}),
    # barrier pod evidence (E2c): the one inventory read beyond the CR
    ("", "pods"): frozenset({"list"}),
    ("", "endpoints"): frozenset({"get"}),
}

CP_EXPECTED: dict[tuple[str, str], frozenset[str]] = {
    ("agentforge.io", "brokerseats"): frozenset({"create", "get", "list", "delete"}),
    ("", "services"): frozenset({"get"}),
}

FORBIDDEN_VERBS = {"delete", "deletecollection", "watch", "*", "escalate", "bind", "impersonate"}
FORBIDDEN_RESOURCES = {"secrets", "configmaps", "pods/exec", "pods/log", "pods/attach",
                       "pods/portforward", "serviceaccounts", "roles", "rolebindings", "*"}
# `pods` is allowed with exactly one verb (list — metadata/phase/deletionTimestamp for the barrier's
# pod evidence); any other verb on pods is a widening.
POD_ALLOWED_VERBS = frozenset({"list"})


def _docs_by_kind() -> dict[tuple[str, str], str]:
    out = {}
    for d in sup.docs(sup.RBAC_FILE):
        out[(sup.kind(d) or "", sup.name(d) or "")] = d
    return out


class ProvisionerRole(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = _docs_by_kind()[("Role", PROVISIONER_ROLE)]
        self.rules = sup.role_rules(self.doc)

    def test_provisioner_role_verbs_exact_no_delete_no_watch_no_secrets(self) -> None:
        self.assertEqual(self.rules, PROVISIONER_EXPECTED)
        for key, verbs in self.rules.items():
            self.assertTrue(verbs.isdisjoint(FORBIDDEN_VERBS), (key, verbs))
            self.assertNotIn(key[1], FORBIDDEN_RESOURCES, key)
            self.assertNotEqual(key[0], "*", key)
        self.assertEqual(self.rules[("", "pods")], POD_ALLOWED_VERBS)
        # `list` only on the CR itself and on pods (evidence): children are addressed by derived name.
        for (group, resource), verbs in self.rules.items():
            if (group, resource) not in {("agentforge.io", "brokerseats"), ("", "pods")}:
                self.assertNotIn("list", verbs, (group, resource))
        # the ONLY writes on the CR are the finalizer (patch) + status (patch) + finalizers subresource.
        self.assertNotIn("create", self.rules[("agentforge.io", "brokerseats")])
        self.assertNotIn("update", self.rules[("agentforge.io", "brokerseats")])

    def test_role_lives_in_the_broker_namespace(self) -> None:
        self.assertEqual(sup.namespace(self.doc), BROKER_NS)


class ControlPlaneRole(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = _docs_by_kind()[("Role", CP_ROLE)]
        self.rules = sup.role_rules(self.doc)

    def test_cp_role_is_cr_only_no_status_no_patch(self) -> None:
        self.assertEqual(self.rules, CP_EXPECTED)
        cr = self.rules[("agentforge.io", "brokerseats")]
        for verb in ("update", "patch", "watch", "deletecollection", "*"):
            self.assertNotIn(verb, cr)
        for sub in ("brokerseats/status", "brokerseats/finalizers"):
            self.assertNotIn(("agentforge.io", sub), self.rules)
        # nothing writable on any child kind — `services: get` is the single read-only witness.
        for (group, resource), verbs in self.rules.items():
            if group != "agentforge.io":
                self.assertEqual(verbs, frozenset({"get"}), (group, resource))
        for key in PROVISIONER_EXPECTED:
            if key[0] != "agentforge.io" and key != ("", "services"):
                self.assertNotIn(key, self.rules, key)

    def test_role_lives_in_the_broker_namespace(self) -> None:
        self.assertEqual(sup.namespace(self.doc), BROKER_NS)


class Bindings(unittest.TestCase):
    def setUp(self) -> None:
        self.docs = _docs_by_kind()

    def _binding(self, role: str) -> str:
        return self.docs[("RoleBinding", role)]

    def test_provisioner_binding_is_cross_namespace_to_the_openbao_sa(self) -> None:
        doc = self._binding(PROVISIONER_ROLE)
        self.assertEqual(sup.namespace(doc), BROKER_NS)
        self.assertEqual(sup.role_ref(doc), ("Role", PROVISIONER_ROLE))
        self.assertEqual(sup.subjects(doc), [("ServiceAccount",) + sup.PROVISIONER_SA])

    def test_cp_binding_is_cross_namespace_to_the_agentforge_sa(self) -> None:
        doc = self._binding(CP_ROLE)
        self.assertEqual(sup.namespace(doc), BROKER_NS)
        self.assertEqual(sup.role_ref(doc), ("Role", CP_ROLE))
        self.assertEqual(sup.subjects(doc), [("ServiceAccount",) + sup.CP_SA])

    def test_subjects_are_service_accounts_declared_in_the_tree(self) -> None:
        provisioner_sas = sup.service_account_docs(
            sup.REPO / "kubernetes/apps/infrastructure/security/openbao/unseal-rbac.yaml")
        cp_sas = sup.service_account_docs(sup.REPO / "kubernetes/apps/apps/agentforge/serviceaccount-service.yaml")
        self.assertIn(sup.PROVISIONER_SA, provisioner_sas)
        self.assertIn(sup.CP_SA, cp_sas)

    def test_file_holds_exactly_two_namespaced_pairs(self) -> None:
        kinds = sorted(k for k, _ in self.docs)
        self.assertEqual(kinds, ["Role", "Role", "RoleBinding", "RoleBinding"])
        for (k, _), d in self.docs.items():
            self.assertEqual(sup.namespace(d), BROKER_NS, k)


if __name__ == "__main__":
    unittest.main()
