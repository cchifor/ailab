#!/usr/bin/env python3
"""Source gates for the control planes' kube-apiserver audit policy.

docs/runbooks/relay-researcher-r7-audit-policy.md (owner decision O7, relay#277 comment 125437). The
policy lives in kubernetes/infra/machine-config/controlplane.yaml.tftpl as cluster.apiServer.auditPolicy.
It adds exactly one class: Pod `create` in the researcher slot at RequestResponse, without its
RequestReceived stage (one ResponseComplete event per create; a handler panic is still logged).
Every other request keeps Talos's default (Metadata, every stage), and no Secret or token body is ever
logged. The evaluator below follows the API server's first-match rule semantics
(k8s.io/apiserver/pkg/audit/policy/checker.go).
"""
from __future__ import annotations

import itertools
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "kubernetes/infra/machine-config/controlplane.yaml.tftpl"
VARIABLES = ROOT / "kubernetes/infra/variables.tf"
NAMESPACES = ROOT / "kubernetes/apps/relay-researcher/installation/namespaces.yaml"
RUNBOOK = ROOT / "docs/runbooks/relay-researcher-r7-audit-policy.md"
SLOT, CONTROL = "relay-researcher-1", "relay-infra-control"
AGENT = f"system:serviceaccount:{CONTROL}:relay-infrastructure-agent"

# Talos v1alpha1 APIServerDefaultAuditPolicy: what config contract v1.11.2 generates into the field.
TALOS_DEFAULT = {"apiVersion": "audit.k8s.io/v1", "kind": "Policy", "rules": [{"level": "Metadata"}]}
R7_RULE = {
    "level": "RequestResponse",
    "verbs": ["create"],
    "namespaces": [SLOT],
    "resources": [{"group": "", "resources": ["pods"]}],
    "omitStages": ["RequestReceived"],
}
GUARD_RULE = {
    "level": "Metadata",
    "resources": [
        {"group": "", "resources": ["secrets", "serviceaccounts/token"]},
        {"group": "authentication.k8s.io", "resources": ["tokenreviews"]},
    ],
}
# Secret and token material, as (group, resource, subresource).
SENSITIVE = [("", "secrets", ""), ("", "serviceaccounts", "token"), ("authentication.k8s.io", "tokenreviews", "")]
BODY_LEVELS = {"Request", "RequestResponse"}
LEVELS = ["None", "Metadata", "Request", "RequestResponse"]
STAGES = {"RequestReceived", "ResponseStarted", "ResponseComplete", "Panic"}
RULE_KEYS = {"level", "users", "userGroups", "verbs", "resources", "namespaces", "nonResourceURLs",
             "omitStages", "omitManagedFields"}
GROUP_RESOURCE_KEYS = {"group", "resources", "resourceNames"}


class UniqueKeyLoader(yaml.SafeLoader):
    """Refuses a duplicated mapping key, as Talos's YAML decoder does; PyYAML would keep the last one."""

    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(k, deep=deep) for k, _ in node.value]
        duplicates = {k for k in keys if keys.count(k) > 1}
        if duplicates:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate keys {sorted(duplicates)}", node.start_mark)
        return super().construct_mapping(node, deep=deep)


def template_docs() -> list[dict]:
    """The template's documents, directives dropped and every interpolation replaced (tofu fills them)."""
    text = TEMPLATE.read_text(encoding="utf-8")
    text = "\n".join(line for line in text.splitlines() if not re.match(r"^\s*%\{.*\}\s*$", line))
    return [d for d in yaml.load_all(re.sub(r"\$\{[a-z_]+\}", "x", text), Loader=UniqueKeyLoader) if d]


def rendered_patch() -> dict:
    """The v1alpha1 patch: the template's first document."""
    return template_docs()[0]


def policy() -> dict:
    return rendered_patch()["cluster"]["apiServer"]["auditPolicy"]


def rule_matches(rule: dict, req: dict) -> bool:
    if rule.get("users") and req["user"] not in rule["users"]:
        return False
    if rule.get("userGroups") and not set(req["groups"]) & set(rule["userGroups"]):
        return False
    if rule.get("verbs") and req["verb"] not in rule["verbs"]:
        return False
    if rule.get("namespaces") or rule.get("resources"):
        if req.get("url"):
            return False
        if rule.get("namespaces") and req["namespace"] not in rule["namespaces"]:
            return False
        if not rule.get("resources"):
            return True
        resource, sub, name = req["resource"], req["subresource"], req["name"]
        combined = f"{resource}/{sub}" if sub else resource
        for gr in rule["resources"]:
            if gr.get("group", "") != req["group"]:
                continue
            if not gr.get("resources"):
                return True
            names_ok = not gr.get("resourceNames") or name in gr["resourceNames"]
            for res in gr["resources"]:
                if not names_ok:
                    continue
                if res in (combined, "*"):
                    return True
                if sub and res.startswith("*/") and res[2:] == sub:
                    return True
                if res.endswith("/*") and res[:-2] == resource:
                    return True
        return False
    if rule.get("nonResourceURLs"):
        url = req.get("url")
        return bool(url) and any(u == url or (u.endswith("*") and url.startswith(u[:-1])) for u in rule["nonResourceURLs"])
    return True


def evaluate(pol: dict, req: dict) -> tuple[str, frozenset]:
    """(level, omitted stages) of the first matching rule; None when nothing matches."""
    for rule in pol["rules"]:
        if rule_matches(rule, req):
            return rule["level"], frozenset(rule.get("omitStages", [])) | frozenset(pol.get("omitStages", []))
    return "None", frozenset(pol.get("omitStages", []))


def requests():
    verbs = ["get", "list", "watch", "create", "update", "patch", "delete", "deletecollection", "proxy"]
    namespaces = [SLOT, CONTROL, "relay-researcher-2", "kube-system", "default", ""]
    targets = [("", "pods", ""), ("", "pods", "binding"), ("", "pods", "eviction"), ("", "pods", "status"),
               ("", "pods", "ephemeralcontainers"), ("", "pods", "exec"), ("", "pods", "log"),
               ("", "configmaps", ""), ("", "serviceaccounts", ""), ("apps", "deployments", ""),
               ("batch", "jobs", ""), ("core", "pods", ""), ("authorization.k8s.io", "subjectaccessreviews", ""),
               *SENSITIVE]
    users = [(AGENT, ["system:serviceaccounts", "system:authenticated"]),
             ("admin", ["system:masters", "system:authenticated"]),
             ("system:kube-scheduler", ["system:authenticated"])]
    for verb, ns, (group, resource, sub), (user, groups) in itertools.product(verbs, namespaces, targets, users):
        # The checker runs before the body is decoded: a create (POST to the collection), list, watch or
        # deletecollection has no name.
        name = "" if verb in ("create", "list", "watch", "deletecollection") and not sub else "relay-x-g1"
        yield {"verb": verb, "namespace": ns, "group": group, "resource": resource, "subresource": sub,
               "name": name, "user": user, "groups": groups}
    for url, (user, groups) in itertools.product(["/healthz", "/livez", "/readyz", "/metrics", "/version", "/api"], users):
        yield {"verb": "get", "url": url, "user": user, "groups": groups}


def is_r7(req: dict) -> bool:
    return (not req.get("url") and req["verb"] == "create" and req["namespace"] == SLOT and req["group"] == ""
            and req["resource"] == "pods" and req["subresource"] == "")


class AuditPolicyTest(unittest.TestCase):
    def test_policy_shape(self):
        pol = policy()
        self.assertEqual(set(pol), {"apiVersion", "kind", "rules"}, "no policy-wide omitStages or omitManagedFields")
        self.assertEqual((pol["apiVersion"], pol["kind"]), ("audit.k8s.io/v1", "Policy"))
        self.assertEqual(pol["rules"], [GUARD_RULE, R7_RULE, TALOS_DEFAULT["rules"][0]])

    def test_rules_decode_strictly(self):
        # Talos decodes the policy strictly (FromUnstructuredWithValidation): an unknown key stops the
        # control plane's static pod rendering.
        for rule in policy()["rules"]:
            self.assertLessEqual(set(rule), RULE_KEYS)
            self.assertIn(rule["level"], LEVELS)
            self.assertLessEqual(set(rule.get("omitStages", [])), STAGES)
            for gr in rule.get("resources", []):
                self.assertLessEqual(set(gr), GROUP_RESOURCE_KEYS)

    def test_catch_all_is_the_talos_default_and_last(self):
        rules = policy()["rules"]
        self.assertEqual(rules[-1], {"level": "Metadata"})
        self.assertLess(rules.index(R7_RULE), len(rules) - 1, "the R7 rule must precede the catch-all")
        self.assertTrue(all(len(r) > 1 for r in rules[:-1]), "only the last rule may match everything")

    def test_only_the_r7_class_changes(self):
        pol, seen = policy(), 0
        for req in requests():
            if is_r7(req):
                seen += 1
                self.assertEqual(evaluate(pol, req), ("RequestResponse", frozenset({"RequestReceived"})), req)
            else:
                self.assertEqual(evaluate(pol, req), evaluate(TALOS_DEFAULT, req), req)
        self.assertGreater(seen, 0)

    def test_r7_rule_scope(self):
        pol = policy()
        base = {"verb": "create", "namespace": SLOT, "group": "", "resource": "pods", "subresource": "",
                "name": "", "user": AGENT, "groups": ["system:serviceaccounts"]}
        self.assertEqual(evaluate(pol, base)[0], "RequestResponse")
        for change in ({"verb": "update"}, {"verb": "patch"}, {"verb": "delete"}, {"verb": "get"},
                       {"namespace": "relay-researcher-2"}, {"namespace": CONTROL}, {"subresource": "binding"},
                       {"subresource": "eviction"}, {"subresource": "ephemeralcontainers"}, {"group": "apps"},
                       {"resource": "secrets"}, {"resource": "configmaps"}):
            self.assertEqual(evaluate(pol, {**base, **change}), ("Metadata", frozenset()), change)

    def test_secrets_and_tokens_never_get_a_body(self):
        pol = policy()
        self.assertEqual(pol["rules"][0], GUARD_RULE, "the Secret and token guard comes first")
        for req in requests():
            if not req.get("url") and (req["group"], req["resource"], req["subresource"]) in SENSITIVE:
                self.assertEqual(evaluate(pol, req), ("Metadata", frozenset()), req)

    def test_every_body_rule_is_narrow(self):
        sensitive = {f"{r}/{s}" if s else r for _, r, s in SENSITIVE}
        for rule in policy()["rules"]:
            if rule["level"] not in BODY_LEVELS:
                continue
            self.assertNotIn("nonResourceURLs", rule)
            self.assertTrue(rule.get("verbs") and "*" not in rule["verbs"], rule)
            self.assertTrue(rule.get("namespaces") and "" not in rule["namespaces"], rule)
            self.assertTrue(rule.get("resources"), rule)
            for gr in rule["resources"]:
                self.assertTrue(gr.get("resources"), rule)
                for res in gr["resources"]:
                    self.assertNotIn("*", res, rule)
                    self.assertNotIn(res, sensitive, rule)

    def test_one_api_server_block(self):
        # A second `apiServer:` (or any duplicated key) would pass a last-wins YAML load while Talos
        # refuses the config at apply time.
        self.assertEqual(len(template_docs()), 3)
        with self.assertRaises(yaml.constructor.ConstructorError):
            yaml.load("cluster:\n  apiServer: {}\n  apiServer: {}\n", Loader=UniqueKeyLoader)

    def test_slot_is_the_execution_namespace(self):
        roles = {d["metadata"]["name"]: d["metadata"]["labels"].get("relay.chifor.me/namespace-role")
                 for d in yaml.safe_load_all(NAMESPACES.read_text(encoding="utf-8")) if d}
        self.assertEqual(roles.get(SLOT), "execution")
        self.assertEqual(roles.get(CONTROL), "control")

    def test_legacy_field_under_contract_1_11_2(self):
        # Contract v1.11.2 generates cluster.apiServer.auditPolicy itself; a KubeAuditPolicyConfig
        # document beside it is refused ("audit policy config is already set in v1alpha1 config").
        # Moving the contract (P8) means moving this policy into that document in the same change.
        self.assertRegex(VARIABLES.read_text(encoding="utf-8"),
                         r'variable "talos_config_contract" \{[^}]*default\s*=\s*"v1\.11\.2"')
        self.assertNotIn("KubeAuditPolicyConfig", [d.get("kind") for d in template_docs()])
        self.assertIn("auditPolicy", rendered_patch()["cluster"]["apiServer"])

    def test_runbook_names_the_template_and_the_log(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        for needle in ("controlplane.yaml.tftpl", "/var/log/audit/kube/kube-apiserver.log",
                       "talos_machine_configuration_apply.cp", "etcd status", "createAudit"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
