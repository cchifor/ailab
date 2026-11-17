#!/usr/bin/env python3
"""Source gates for the first Relay researcher installation.

docs/runbooks/relay-researcher-installation.md (the hub upgrade and its components are ailab #1767,
with their own test_relay_researcher_hub_upgrade.py). Everything here
is staged and inert until the operator's inclusion PRs; these tests pin what those PRs may include
and refuse an inclusion that still carries a placeholder. The rendered shapes are proven by
scripts/tests/test-relay-researcher-render.sh (docker, manifests.yaml).
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import importlib.util
import ipaddress
import json
import re
import tempfile
import urllib.parse
from unittest import mock
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TREE = ROOT / "kubernetes/apps/relay-researcher"
CLUSTER = ROOT / "kubernetes/apps/clusters/ai"
TEMPLATE = ROOT / "docs/relay-workers/researcher-installation-definition.json"
CONTROL, SLOT = "relay-infra-control", "relay-researcher-1"
MOZILLA_SHA256 = "427c064216761a1e72a8b30f0d954d08cb6e4435a82091fb865afd20c8497285"
# The slot's two egress stages (owner decision G3): exactly one is included; the definition pins its digest.
# One file per stage, side by side; the enrollment file is never edited in place.
EGRESS = {"enrollment": TREE / "installation/execution-egress.yaml",
          "qualification": TREE / "installation/qualification-egress.yaml"}
ENROLLMENT_EGRESS_SHA256 = "aa11c2299af12acc886696480b5800ac0b568a0b6e2813609d4ed2f7eea38405"
QUALIFICATION_EGRESS_SHA256 = "f6d3187c2fa3843ef54ed5d6026093c00a8fe487ab54263b7ce6db140d51282f"
# The ailab root CA the qualification-stage runtime pins (hub-ca.pem, Router and communication caFile).
RUNTIME_CA = ROOT / "docs/relay-workers/researcher-runtime-ca.crt"
RUNTIME_CA_SHA256 = "3d109e83f3e144daf1022cff5af22cbeda668db9bb1f4b25fc42f179254ad4c3"
INTERNAL_TLS = ROOT / "kubernetes/apps/relay-internal-tls"
JOURNAL_RENDER = ROOT / "kubernetes/optional/relay-candidate-journal/render.py"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
LABELS = {
    "app.kubernetes.io/name": "relay-infrastructure-agent",
    "app.kubernetes.io/instance": "relay-infrastructure-agent",
    "app.kubernetes.io/component": "infrastructure-agent",
}


def docs(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def by_key(items: list[dict]) -> dict:
    return {(d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"]): d for d in items}


def template() -> dict:
    data = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    data.pop("_comment")
    return data


def pins() -> dict:
    return docs(TREE / "pins/pins.yaml")[0]["data"]


def bundle_module():
    spec = importlib.util.spec_from_file_location("relay_researcher_bundle", ROOT / "scripts/relay-researcher-bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def gvisor_overhead() -> tuple[int, int]:
    text = (ROOT / "kubernetes/infra/agent-nodes/relay-workers.tf").read_text(encoding="utf-8")
    match = re.search(r'overhead\s*=\s*\{\s*podFixed\s*=\s*\{\s*cpu\s*=\s*"(\d+)m",\s*memory\s*=\s*"(\d+)Mi"', text)
    assert match, "relay-gvisor overhead not found in relay-workers.tf"
    return int(match.group(1)), int(match.group(2))


def flux_kustomizations_for_tree() -> dict:
    found = {}
    for manifest in sorted(CLUSTER.glob("*.yaml")):
        for doc in docs(manifest):
            if doc.get("kind") == "Kustomization" and "relay-researcher" in str(doc.get("spec", {}).get("path", "")):
                found[doc["spec"]["path"]] = doc
    return found


def installation_resources() -> list[str]:
    return yaml.safe_load((TREE / "installation/kustomization.yaml").read_text(encoding="utf-8"))["resources"]


def selected_egress() -> Path:
    stages = [r for r in installation_resources() if r in {p.name for p in EGRESS.values()}]
    assert len(stages) == 1, f"exactly one egress stage must be included, found {stages}"
    return TREE / "installation" / stages[0]


class InclusionGate(unittest.TestCase):
    """Nothing is live on merge; an inclusion with a placeholder is refused."""

    @staticmethod
    def flux_manifest(name: str) -> Path:
        """Steps I5 and I7 `git mv` each file from flux/ (staged) to clusters/ai (active): exactly one
        copy exists at any time, and it keeps the reviewed shape in either place."""
        copies = [p for p in (TREE / "flux" / name, CLUSTER / name) if p.is_file()]
        assert len(copies) == 1, f"{name}: expected exactly one of flux/ or clusters/ai, found {copies}"
        return copies[0]

    def test_staged_flux_kustomizations(self):
        inst = docs(self.flux_manifest("relay-researcher-installation.yaml"))[0]
        agent = docs(self.flux_manifest("relay-researcher-agent.yaml"))[0]
        for doc, path in ((inst, "./kubernetes/apps/relay-researcher/installation"),
                          (agent, "./kubernetes/apps/relay-researcher/agent")):
            self.assertEqual(doc["kind"], "Kustomization")
            self.assertEqual(doc["metadata"]["namespace"], "flux-system")
            self.assertEqual(doc["spec"]["path"], path)
            self.assertEqual(doc["spec"]["sourceRef"], {"kind": "GitRepository", "name": "flux-system"})
            self.assertIs(doc["spec"]["wait"], True)
        # The containment must survive a file removal; the agent tree may prune.
        self.assertIs(inst["spec"]["prune"], False)
        self.assertIs(agent["spec"]["prune"], True)
        self.assertEqual(agent["spec"]["dependsOn"], [{"name": "relay-researcher-installation"}])

    def test_tree_is_included_only_with_real_pins(self):
        included = flux_kustomizations_for_tree()
        p = pins()
        if not included:
            return
        self.assertTrue(UUID.match(p["installationId"]), "installation stage included without an installation ID")
        self.assertIn("./kubernetes/apps/relay-researcher/installation", included)
        self.assertIs(included["./kubernetes/apps/relay-researcher/installation"]["spec"]["prune"], False)
        if "./kubernetes/apps/relay-researcher/agent" in included:
            self.assertRegex(p["agentImage"], r"^registry\.chifor\.me/relay/infrastructure-agent@sha256:[0-9a-f]{64}$")
            self.assertRegex(p["sourceCommit"], r"^[0-9a-f]{40}$")
            text = (TREE / "agent/config.json").read_text(encoding="utf-8")
            self.assertNotIn("OPERATOR-FILLS", text)
            config = json.loads(text)
            self.assertEqual(config["identity"]["installationId"], p["installationId"])

    def test_pins_are_all_placeholders_or_all_real(self):
        p = pins()
        self.assertEqual(set(p), {"installationId", "agentImage", "sourceCommit"})
        placeholders = [k for k, v in p.items() if "operator-fills" in v]
        self.assertIn(len(placeholders), (0, 3), f"half-filled pins: {placeholders}")
        if not placeholders:
            self.assertTrue(UUID.match(p["installationId"]))

    def test_no_literal_copy_of_a_pin(self):
        # Every copy is derived by ../pins: the objects carry the marker, never a value.
        for path in sorted((TREE / "installation").rglob("*.yaml")) + sorted((TREE / "agent").glob("*.yaml")):
            text = path.read_text(encoding="utf-8")
            for value in pins().values():
                self.assertNotIn(value, text, f"{path} holds a literal pin")


class InstallationStage(unittest.TestCase):
    """The objects equal what Relay's prepare-pod-installation.ts renders from the template."""

    def setUp(self):
        self.d = template()
        # What the tree renders: its resources, so exactly the INCLUDED egress stage.
        self.objects = by_key([o for f in installation_resources() for o in docs(TREE / "installation" / f)])

    def test_namespaces(self):
        nss = {k[2]: v for k, v in self.objects.items() if k[0] == "Namespace"}
        self.assertEqual(set(nss), {CONTROL, SLOT})
        self.assertEqual(self.d["identity"]["controlNamespace"], CONTROL)
        self.assertEqual(self.d["executionSlots"], [SLOT])
        for name, ns in nss.items():
            self.assertEqual(ns["metadata"]["annotations"],
                             {"kustomize.toolkit.fluxcd.io/prune": "disabled", "helm.sh/resource-policy": "keep"})
            labels = ns["metadata"]["labels"]
            expected = {"relay.chifor.me/installation-id": "pinned-by-pins-component",
                        "relay.chifor.me/namespace-role": "control" if name == CONTROL else "execution"}
            for mode in ("enforce", "warn", "audit"):
                expected[f"pod-security.kubernetes.io/{mode}"] = "restricted"
                expected[f"pod-security.kubernetes.io/{mode}-version"] = f"v{self.d['kubernetesMinor']}"
            self.assertEqual(labels, expected)

    def test_containment_is_the_tool_derivation(self):
        r, ceiling = self.d["agentResources"], self.d["installationPolicy"]["resourceCeiling"]
        cpu_over, mem_over = gvisor_overhead()
        # The enrollment Secret reference is dropped after confirmed enrollment (runbook step I10),
        # which lowers the control Secret budget by one.
        secrets = len(set(self.d["references"].values())) + 3
        for ns in (CONTROL, SLOT):
            control = ns == CONTROL
            cpu = f"{r['milliCpu'] if control else ceiling['milliCpu'] + cpu_over}m"
            mem = f"{r['memoryMiB'] if control else ceiling['memoryMiB'] + mem_over}Mi"
            eph = f"{r['ephemeralMiB'] if control else ceiling['scratchMiB']}Mi"
            quota = self.objects[("ResourceQuota", ns, "relay-enrollment-quota")]["spec"]["hard"]
            self.assertEqual(quota, {
                "pods": "1" if control else "0",
                "requests.cpu": cpu, "limits.cpu": cpu, "requests.memory": mem, "limits.memory": mem,
                "requests.ephemeral-storage": eph, "limits.ephemeral-storage": eph,
                "persistentvolumeclaims": "0", "requests.storage": "0", "services": "0",
                "services.loadbalancers": "0", "services.nodeports": "0",
                "secrets": str(secrets) if control else "0", "configmaps": "8",
                "count/leases.coordination.k8s.io": "2" if control else "0"})
            limits = self.objects[("LimitRange", ns, "relay-enrollment-limits")]["spec"]["limits"]
            self.assertEqual(limits, [
                {"type": "Container", "max": {"cpu": cpu, "memory": mem, "ephemeral-storage": eph}},
                {"type": "Pod", "max": {"cpu": cpu, "memory": mem}}])
            self.assertEqual(self.objects[("NetworkPolicy", ns, "relay-default-deny")]["spec"],
                             {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": []})
        self.assertEqual(ceiling["retainedGiB"], 0)
        self.assertEqual(ceiling["cacheGiB"], 0)

    def test_gate_is_the_tool_gate(self):
        name = "relay-enrollment-pinned-by-pins-component"
        policy = self.objects[("ValidatingAdmissionPolicy", None, name)]["spec"]
        self.assertEqual(policy, {
            "failurePolicy": "Fail",
            "matchConstraints": {"resourceRules": [{
                "apiGroups": [""], "apiVersions": ["v1"], "operations": ["CREATE", "UPDATE"],
                "resources": ["pods", "pods/ephemeralcontainers"], "scope": "Namespaced"}]},
            "matchConditions": [{"name": "dedicated-execution-slots",
                                 "expression": f"request.namespace in {json.dumps(self.d['executionSlots'])}"}],
            "validations": [{"expression": "false", "reason": "Forbidden",
                             "message": "Relay enrollment-only installation: all worker Pod creation and updates are disabled."}],
        })
        binding = self.objects[("ValidatingAdmissionPolicyBinding", None, name)]["spec"]
        self.assertEqual(binding, {
            "policyName": name, "validationActions": ["Deny"],
            "matchResources": {"namespaceSelector": {"matchExpressions": [
                {"key": "kubernetes.io/metadata.name", "operator": "In", "values": self.d["executionSlots"]}]}}})

    def test_slot_has_exactly_default_deny_and_the_allow_list(self):
        slot_policies = sorted(k[2] for k in self.objects if k[0] == "NetworkPolicy" and k[1] == SLOT)
        self.assertEqual(slot_policies, ["relay-default-deny", "relay-researcher-egress"])
        # Nothing in the agent tree may add a policy to the slot either.
        for path in sorted((TREE / "agent").glob("*.yaml")):
            for doc in docs(path):
                if "NetworkPolicy" in str(doc.get("kind")):
                    self.assertNotEqual(doc["metadata"].get("namespace"), SLOT, path)

    def test_enrollment_allow_list_is_dns_plus_the_two_public_edge_addresses(self):
        (egress,) = docs(EGRESS["enrollment"])
        spec = egress["spec"]
        self.assertEqual(spec["podSelector"], {})
        self.assertEqual(spec["policyTypes"], ["Egress"])
        dns, edge = spec["egress"]
        self.assertEqual(dns, {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                                       "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                               "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]})
        self.assertEqual(edge["ports"], [{"protocol": "TCP", "port": 443}])
        cidrs = [peer["ipBlock"]["cidr"] for peer in edge["to"]]
        self.assertEqual(set(edge["to"][0]), {"ipBlock"})
        self.assertEqual(cidrs, self.d["controlNetwork"]["hubCidrs"])
        for cidr in cidrs:
            net = ipaddress.ip_network(cidr)
            self.assertEqual(net.prefixlen, 32, cidr)
            self.assertTrue(net.is_global, f"{cidr} is not a public address")
            self.assertNotIn("except", edge["to"][cidrs.index(cidr)]["ipBlock"])

    def test_cloudflare_allow_list_is_marked_enrollment_stage_only(self):
        # Owner decision G3: qualification moves to internal TLS terminators (pending series).
        text = EGRESS["enrollment"].read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# ENROLLMENT-STAGE ONLY."), "the header must say so first")

    def test_ceilings_admit_relay_researcher_baseline(self):
        # Relay packages/infrastructure-protocol/baselines.ts ROLE_BASELINES.researcher, which the
        # sealed researcher template is built from (owner decision G4). Relay's templatePolicyViolations
        # refuses resources above the ceiling, a lease above maxLeaseSeconds, a lifetime above
        # maxLifetimeSeconds and egress slugs outside allowedEgressDestinations.
        baseline = {"resources": {"milliCpu": 1000, "memoryMiB": 2048, "scratchMiB": 4096, "retainedGiB": 0,
                                  "cacheGiB": 0, "maxProcesses": 512},
                    "leaseSeconds": 900, "maxLifetimeSeconds": 8 * 3600, "idleStopSeconds": 1800,
                    "egressDestinations": []}
        policy = self.d["installationPolicy"]
        self.assertEqual(policy["resourceCeiling"], {"milliCpu": 2000, "memoryMiB": 4096, "scratchMiB": 10240,
                                                     "retainedGiB": 0, "cacheGiB": 0, "maxProcesses": 512})
        self.assertEqual((policy["maxLeaseSeconds"], policy["maxLifetimeSeconds"]), (900, 28800))
        self.assertEqual(policy["allowedEgressDestinations"], [])
        for key, value in baseline["resources"].items():
            self.assertLessEqual(value, policy["resourceCeiling"][key], key)
        self.assertLessEqual(baseline["leaseSeconds"], policy["maxLeaseSeconds"])
        self.assertLessEqual(baseline["maxLifetimeSeconds"], policy["maxLifetimeSeconds"])
        self.assertLessEqual(set(baseline["egressDestinations"]), set(policy["allowedEgressDestinations"]))

    def test_egress_artifact_digest(self):
        # The measurement normalizes empty collections and defaulted protocols away; neither stage has
        # either, so Relay's digest equals the plain canonical hash.
        def walk(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    if k != "podSelector":
                        self.assertNotEqual(v, {} if k != "spec" else None, k)
                    walk(v)
            if isinstance(value, list):
                self.assertTrue(value)
                for v in value:
                    walk(v)
        measure = bundle_module().measurement_digest
        for name, want in (("enrollment", ENROLLMENT_EGRESS_SHA256),
                           ("qualification", QUALIFICATION_EGRESS_SHA256)):
            (egress,) = docs(EGRESS[name])
            self.assertEqual((egress["metadata"]["name"], egress["metadata"]["namespace"]),
                             ("relay-researcher-egress", SLOT), name)
            walk(egress["spec"]["egress"])
            for rule in egress["spec"]["egress"]:
                for port in rule["ports"]:
                    self.assertIn("protocol", port)
            self.assertEqual(measure(egress), want, name)
        self.assertIn(QUALIFICATION_EGRESS_SHA256, EGRESS["qualification"].read_text(encoding="utf-8"))
        # The definition pins the INCLUDED stage, and the tree renders that one object.
        egress = self.objects[("NetworkPolicy", SLOT, "relay-researcher-egress")]
        (selected,) = docs(selected_egress())
        self.assertEqual(egress, selected)
        self.assertEqual(self.d["executionEgressArtifacts"],
                         [{"namespace": SLOT, "name": "relay-researcher-egress", "sha256": measure(selected)}])


def journal_render():
    spec = importlib.util.spec_from_file_location("relay_candidate_journal_render", JOURNAL_RENDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QualificationStage(unittest.TestCase):
    """Owner decision G3: the qualification-stage slot reaches the hub, the Router and the candidate
    journal through three distinct pinned endpoints, one rule each, and nothing else (no DNS)."""

    def setUp(self):
        (self.policy,) = docs(EGRESS["qualification"])
        tls = {(d["kind"], d["metadata"]["name"]): d for f in ("terminators.yaml", "network-policy.yaml")
               for d in docs(INTERNAL_TLS / f)}
        journal = journal_render()
        # endpoint -> (namespace, Pod labels, container port, Service ClusterIP)
        self.endpoints = {}
        for name in ("relay-hub-tls", "relay-router-tls"):
            dep, svc = tls[("Deployment", name)], tls[("Service", name)]
            (caddy,) = [c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == "caddy"]
            self.endpoints[name] = (dep["metadata"]["namespace"], dep["spec"]["template"]["metadata"]["labels"],
                                    caddy["ports"][0]["containerPort"], svc["spec"]["clusterIP"])
            self.assertEqual(svc["spec"]["ports"], [{"name": "https", "protocol": "TCP", "port": 443,
                                                     "targetPort": caddy["ports"][0]["containerPort"]}])
            ingress = tls[("NetworkPolicy", name)]["spec"]["ingress"]
            self.assertEqual(ingress[0]["from"], [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": SLOT}},
                                                   "podSelector": {"matchLabels": {"relay.infrastructure/managed": "true"}}}])
        self.endpoints["relay-candidate-journal"] = (journal.SERVICE_NS, {"app": journal.NAME}, 8443, journal.CLUSTER_IP)
        self.journal = journal

    def test_three_rules_in_order_no_dns_no_cidr(self):
        spec = self.policy["spec"]
        # Every Pod in the slot: Relay gives candidate Pods no label of their own, so rule 2 cannot be
        # narrowed to them (accepted, relay#277 comment 123028 condition 1; runbook steps Q2 and Q7).
        self.assertEqual(spec["podSelector"], {})
        self.assertEqual(spec["policyTypes"], ["Egress"])
        expected = []
        for name in ("relay-hub-tls", "relay-router-tls", "relay-candidate-journal"):
            ns, labels, port, _ = self.endpoints[name]
            selector = {"app.kubernetes.io/name": name} if name != "relay-candidate-journal" else {"app": name}
            self.assertTrue(all(labels.get(k) == v for k, v in selector.items()), name)
            expected.append({"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ns}},
                                     "podSelector": {"matchLabels": selector}}],
                             "ports": [{"protocol": "TCP", "port": port}]})
        self.assertEqual(spec["egress"], expected)
        text = json.dumps(spec)
        for forbidden in ("ipBlock", "kube-dns", "kube-system", '"port": 53'):
            self.assertNotIn(forbidden, text)

    def test_each_rule_admits_exactly_its_own_endpoint(self):
        # The hub-partition driver removes rule 0 and keeps rule 1: separable only if no rule's peer
        # matches another endpoint's Pods (Relay derives this from the live objects; here from source).
        def admits(rule, endpoint):
            ns, labels, port, _ = self.endpoints[endpoint]
            return any(p["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": ns}
                       and all(labels.get(k) == v for k, v in p["podSelector"]["matchLabels"].items())
                       for p in rule["to"]) and any(q["port"] == port for q in rule["ports"])
        names = list(self.endpoints)
        for i, rule in enumerate(self.policy["spec"]["egress"]):
            self.assertEqual([n for n in names if admits(rule, n)], [names[i]], f"rule {i}")

    def test_addresses_are_the_pinned_services_and_distinct(self):
        ips = {n: e[3] for n, e in self.endpoints.items()}
        self.assertEqual(ips, {"relay-hub-tls": "10.96.0.40", "relay-router-tls": "10.96.0.41",
                               "relay-candidate-journal": "10.96.0.42"})
        self.assertEqual(bundle_module().internal_tls_addresses(),
                         {"hub": "10.96.0.40", "router": "10.96.0.41", "candidateJournal": "10.96.0.42"})
        binding = template()["binding"]
        self.assertIn("hostAliases {hub: 10.96.0.40, router: 10.96.0.41, candidateJournal: 10.96.0.42}", binding)
        self.assertIn(RUNTIME_CA_SHA256, binding)
        self.assertEqual(self.journal.ORIGIN, "https://relay-candidate-journal.relay-candidate-journal.svc:8443")

    def test_journal_admits_only_this_slot_and_its_control_namespace(self):
        # relay#277 comment 123028 condition 2: the journal's client ingress is this installation's
        # execution slot (managed worker Pods) and its control namespace (the agent, itself a journal
        # client), nothing else; render.py refuses a run naming other namespaces.
        roles = {d["metadata"]["labels"]["relay.chifor.me/namespace-role"]: d["metadata"]["name"]
                 for d in docs(TREE / "installation/namespaces.yaml")}
        self.assertEqual(roles, {"control": CONTROL, "execution": SLOT})
        self.assertEqual(self.journal.CLIENT_NAMESPACES, roles)
        self.assertIn("{'relay.infrastructure/managed': 'true'}", JOURNAL_RENDER.read_text(encoding="utf-8"))

    def test_runtime_ca_is_the_ailab_root(self):
        body = RUNTIME_CA.read_bytes()
        self.assertEqual(body, (ROOT / "ansible/roles/openbao_agent/files/ailab-root-ca.crt").read_bytes())
        self.assertEqual(hashlib.sha256(body).hexdigest(), RUNTIME_CA_SHA256)
        # Relay's packager grammar for an operator-supplied CA: PEM certificates only.
        self.assertRegex(body.decode("ascii"), r"^-----BEGIN CERTIFICATE-----\n[A-Za-z0-9+/=\n]+-----END CERTIFICATE-----\n$")
        self.assertIn("researcher-runtime-ca.crt", (ROOT / ".gitea/workflows/manifests.yaml").read_text(encoding="utf-8"))


def campaign():
    """(the S-A generator, its validated committed run) during the qualification campaign, else None.
    The campaign's agent tree is the enrollment one plus exactly the chart's candidate values."""
    bundle = bundle_module()
    v = bundle.campaign_run()
    return None if v is None else (bundle.candidate_generator(), v)


class AgentStage(unittest.TestCase):
    """The agent mirror equals the chart's enrollment-only render (proven against the pinned chart
    by relay-researcher-bundle.py check --chart) and carries only the template's values; during the
    qualification campaign, plus exactly the chart's candidate values for the committed run."""

    def setUp(self):
        self.campaign = campaign()
        self.d = template()
        self.objects = by_key([o for f in ("rbac.yaml", "deployment.yaml", "network-policy.yaml")
                               for o in docs(TREE / "agent" / f)])

    def test_rbac_is_exactly_the_enrollment_chart_rbac(self):
        roles = {k: v for k, v in self.objects.items() if k[0] in ("Role", "ClusterRole")}
        self.assertEqual(set(roles), {("Role", CONTROL, "relay-infrastructure-agent-control"),
                                      ("Role", SLOT, "relay-infrastructure-agent-workloads"),
                                      ("ClusterRole", None, "relay-infrastructure-agent-relay-infra-control-cluster-uid")})
        self.assertEqual(roles[("Role", CONTROL, "relay-infrastructure-agent-control")]["rules"], [
            {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["relay-installation-fence"], "verbs": ["get", "update"]},
            {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["create"]},
            {"apiGroups": ["coordination.k8s.io"], "resources": ["leases"], "resourceNames": ["relay-infrastructure-agent"], "verbs": ["get", "update"]},
            {"apiGroups": ["coordination.k8s.io"], "resources": ["leases"], "verbs": ["create"]}])
        slot_rules = [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "create", "delete"]}]
        namespaces = ["kube-system"]
        if self.campaign:
            # Relay chart templates/rbac.yaml with candidateRuntime set and policy.bootstrapSecrets true:
            # the slot's egress policy read (the run measures it), the bootstrap Secrets, and the two
            # namespaces' reads (their UIDs are pinned by the run).
            (egress,) = docs(selected_egress())
            slot_rules += [{"apiGroups": ["networking.k8s.io"], "resources": ["networkpolicies"],
                            "resourceNames": [egress["metadata"]["name"]], "verbs": ["get"]},
                           {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get", "create", "delete"]}]
            namespaces += [CONTROL, SLOT]
        self.assertEqual(roles[("Role", SLOT, "relay-infrastructure-agent-workloads")]["rules"], slot_rules)
        self.assertEqual(roles[("ClusterRole", None, "relay-infrastructure-agent-relay-infra-control-cluster-uid")]["rules"], [
            {"apiGroups": [""], "resources": ["namespaces"], "resourceNames": namespaces, "verbs": ["get"]}])
        subject = [{"kind": "ServiceAccount", "name": "relay-infrastructure-agent", "namespace": CONTROL}]
        for k, v in self.objects.items():
            if k[0] in ("RoleBinding", "ClusterRoleBinding"):
                self.assertEqual(v["subjects"], subject)
                self.assertEqual(v["roleRef"]["name"], k[2])
            self.assertEqual(v["metadata"].get("labels", LABELS) if k[0] != "CiliumNetworkPolicy" else LABELS, LABELS)
        sa = self.objects[("ServiceAccount", CONTROL, "relay-infrastructure-agent")]
        self.assertIs(sa["automountServiceAccountToken"], False)
        granted = {(r, v) for role in roles.values() for rule in role["rules"]
                   for r in rule["resources"] for v in rule["verbs"]}
        # Secrets only in the slot and only during the campaign (bootstrapSecrets), pinned above.
        forbidden = {"secrets", "pods/exec", "pods/attach", "pods/portforward", "pods/log",
                     "persistentvolumeclaims", "roles", "rolebindings", "*"} - ({"secrets"} if self.campaign else set())
        for resource, _ in granted:
            self.assertNotIn(resource, forbidden)
        for _, verb in granted:
            self.assertIn(verb, {"get", "create", "update", "delete"})

    def test_deployment(self):
        dep = self.objects[("Deployment", CONTROL, "relay-infrastructure-agent")]
        self.assertEqual(dep["spec"]["replicas"], 1)
        self.assertEqual(dep["spec"]["strategy"], {"type": "Recreate"})
        pod = dep["spec"]["template"]["spec"]
        self.assertEqual(pod["nodeSelector"], self.d["controlPlacement"]["nodeSelector"])
        self.assertEqual(pod["tolerations"], self.d["controlPlacement"]["tolerations"])
        self.assertNotIn("ailab.io/relay-worker-pool", pod["nodeSelector"])
        self.assertNotEqual(pod["nodeSelector"]["kubernetes.io/hostname"], "relay-worker-1")
        for field in ("automountServiceAccountToken", "enableServiceLinks", "hostNetwork", "hostPID", "hostIPC"):
            self.assertIs(pod[field], False, field)
        self.assertEqual(pod["securityContext"], {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532,
                                                  "fsGroup": 65532, "seccompProfile": {"type": "RuntimeDefault"}})
        (c,) = pod["containers"]
        self.assertEqual(c["image"], "pinned-by-pins-component")
        self.assertEqual(c["securityContext"], {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                "privileged": False, "capabilities": {"drop": ["ALL"]}})
        r = self.d["agentResources"]
        want = {"cpu": f"{r['milliCpu']}m", "memory": f"{r['memoryMiB']}Mi", "ephemeral-storage": f"{r['ephemeralMiB']}Mi"}
        self.assertEqual(c["resources"], {"requests": want, "limits": want})
        self.assertNotIn("env", c)
        self.assertNotIn("envFrom", c)
        vols = {v["name"]: v for v in pod["volumes"]}
        mounts = {m["name"] for m in c["volumeMounts"]}
        self.assertEqual(vols["installation-key"]["secret"]["secretName"], self.d["references"]["installationKeySecret"])
        enrollment = self.d["references"].get("enrollmentTokenSecret")
        if enrollment:
            self.assertEqual(vols["enrollment-token"]["secret"]["secretName"], enrollment)
            self.assertIn("enrollment-token", mounts)
        else:
            self.assertNotIn("enrollment-token", vols)
            self.assertNotIn("enrollment-token", mounts)
        self.assertEqual(set(vols), mounts, "every volume is mounted, every mount has a volume")
        candidate_mounts = [m for m in c["volumeMounts"] if m["name"] == "candidate"]
        if self.campaign:
            # The chart's four candidate mounts and the volume of the committed run's control ConfigMap.
            gen, v = self.campaign
            self.assertEqual(candidate_mounts, gen.AGENT_MOUNTS)
            self.assertEqual(vols["candidate"], {"name": "candidate", "configMap": {
                "name": v["names"]["controlConfigMap"], "defaultMode": 0o444, "optional": False,
                "items": [{"key": k, "path": k} for k in gen.CONTROL_KEYS]}})
        else:
            self.assertEqual(candidate_mounts, [])
            self.assertNotIn("candidate", vols)
        self.assertEqual(vols["hub-ca"]["configMap"]["name"], self.d["hub"]["caConfigMap"])
        token = vols["kubernetes"]["projected"]["sources"][0]["serviceAccountToken"]
        self.assertEqual(token, {"path": "token", "expirationSeconds": 3600})

    def test_network_policy_is_the_template_control_network(self):
        net = self.d["controlNetwork"]
        np = self.objects[("NetworkPolicy", CONTROL, "relay-infrastructure-agent")]["spec"]
        self.assertEqual(np["podSelector"], {"matchLabels": {k: LABELS[k] for k in ("app.kubernetes.io/name", "app.kubernetes.io/instance")}})
        self.assertEqual(np["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(np["ingress"], [])
        dns, api, hub, *journal = np["egress"]
        if self.campaign:
            # The chart's journal egress rule: ipBlocks only, private and off the LAN, on the journal's
            # port (its CIDRs are the one-time PR's, U9).
            (rule,) = journal
            origin = urllib.parse.urlsplit(journal_render().ORIGIN)
            self.assertEqual(rule["ports"], [{"protocol": "TCP", "port": origin.port or 443}])
            self.assertTrue(rule["to"])
            rfc1918 = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
            for peer in rule["to"]:
                self.assertEqual(set(peer), {"ipBlock"})
                self.assertEqual(set(peer["ipBlock"]), {"cidr"})
                block = ipaddress.ip_network(peer["ipBlock"]["cidr"])
                self.assertTrue(any(block.subnet_of(n) for n in rfc1918), f"{block} is not RFC 1918")
                self.assertFalse(block.overlaps(ipaddress.ip_network("192.168.0.0/23")), f"{block} reaches the LAN")
        else:
            self.assertEqual(journal, [])
        self.assertEqual(dns["to"], [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": net["dnsNamespace"]}},
                                      "podSelector": {"matchLabels": net["dnsPodLabels"]}}])
        self.assertEqual([p["ipBlock"]["cidr"] for p in api["to"]], net["apiServerCidrs"])
        self.assertEqual([p["port"] for p in api["ports"]], net["apiServerPorts"])
        self.assertEqual([p["ipBlock"]["cidr"] for p in hub["to"]], net["hubCidrs"])
        self.assertEqual(hub["ports"], [{"protocol": "TCP", "port": 443}])
        cnp = self.objects[("CiliumNetworkPolicy", CONTROL, "relay-infrastructure-agent-api-egress")]["spec"]
        self.assertEqual(cnp["egress"], [{"toEntities": ["kube-apiserver"],
                                          "toPorts": [{"ports": [{"port": str(p), "protocol": "TCP"} for p in net["apiServerPorts"]]}]}])
        # ailab's journal addition (owner decision U9): an ipBlock never selects the journal Pod under
        # Cilium, so this selector rule is what admits agent -> journal. Egress only, the agent Pod only,
        # the journal service Pods only, TCP 8443 only: the same selector as the slot's qualification rule.
        journal_np = self.objects[("NetworkPolicy", CONTROL, "relay-infrastructure-agent-journal-egress")]
        self.assertEqual(journal_np["spec"], {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "relay-infrastructure-agent",
                                            "app.kubernetes.io/instance": "relay-infrastructure-agent"}},
            "policyTypes": ["Egress"],
            "egress": [{"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "relay-candidate-journal"}},
                                "podSelector": {"matchLabels": {"app": "relay-candidate-journal"}}}],
                        "ports": [{"protocol": "TCP", "port": 8443}]}]})
        qualification = docs(EGRESS["qualification"])[0]["spec"]["egress"][2]
        self.assertEqual(journal_np["spec"]["egress"][0], qualification,
                         "the agent's journal rule must match the slot's qualification journal rule")

    def test_config_matches_the_template(self):
        config = json.loads((TREE / "agent/config.json").read_text(encoding="utf-8"))
        d = self.d
        expected = {
            "hub": {"caFile": "/run/relay/hub-ca/ca.crt", "origin": d["hub"]["origin"]},
            "kubernetes": {"caFile": "/run/relay/kubernetes/ca.crt", "origin": d["kubernetesOrigin"],
                           "tokenFile": "/run/relay/kubernetes/token"},
            "leadership": {"leaseDurationSeconds": 15, "renewDeadlineSeconds": 10, "retryPeriodSeconds": 2},
            "version": 1,
        }
        for k, v in expected.items():
            self.assertEqual(config[k], v, k)
        identity = config["identity"]
        for k in ("tenantId", "clusterUid", "controlNamespace"):
            self.assertEqual(identity[k], d["identity"][k])
        self.assertEqual(identity["installationKeyFile"], "/run/relay/installation/installation.pem")
        if self.d["references"].get("enrollmentTokenSecret"):
            self.assertEqual(identity["enrollmentTokenFile"], "/run/relay/enrollment/token")
        else:
            self.assertNotIn("enrollmentTokenFile", identity)
        self.assertEqual(config["policy"]["executionSlots"], d["executionSlots"])
        # Off until the campaign; then on, with exactly the generator's candidateRuntime for the run.
        self.assertIs(config["policy"]["bootstrapSecrets"], self.campaign is not None)
        if self.campaign:
            gen, v = self.campaign
            self.assertEqual(config["candidateRuntime"], gen.candidate_runtime(v))
        else:
            self.assertNotIn("candidateRuntime", config)
        self.assertIsNone(config["policy"]["retainedStorageClass"])

    def test_trust_bundle_is_the_pinned_mozilla_bundle(self):
        body = (TREE / "agent/hub-ca/mozilla-ca-bundle.crt").read_bytes()
        self.assertEqual(hashlib.sha256(body).hexdigest(), MOZILLA_SHA256)
        self.assertIn(b"Certificate data from Mozilla as of: Thu Nov  5 04:12:01 2026 GMT", body)
        self.assertNotIn(b"\r", body)
        self.assertNotIn(b"PRIVATE KEY", body)
        kust = yaml.safe_load((TREE / "agent/kustomization.yaml").read_text(encoding="utf-8"))
        [ca] = [g for g in kust["configMapGenerator"] if g["name"] == "relay-hub-ca"]
        self.assertEqual(ca["files"], ["ca.crt=hub-ca/mozilla-ca-bundle.crt"])
        self.assertIs(ca["options"]["immutable"], True)

    def test_alert_rules_have_a_fixture(self):
        self.assertTrue((TREE / "agent/relay-researcher-rules.test.yaml").is_file())


def included_stage() -> str:
    """The egress stage the installation tree includes: enrollment until step Q4, qualification after."""
    return {p.name: stage for stage, p in EGRESS.items()}[selected_egress().name]


class BundleCheck(unittest.TestCase):
    """scripts/relay-researcher-bundle.py accepts a bundle shaped like the tool's for this installation
    and refuses every reviewed difference. The bundle here is synthetic test input, never committed.

    It follows the committed tree in every runbook state: before step I5 the pins and the release-derived
    parts of agent/config.json are placeholders and the bundle synthesizes them; once I5 commits the real
    pins and config (and after Q4 switches the egress stage) it carries exactly those, because check()
    compares a bundle with filled pins and config. Every other value stays synthetic, so each refusal
    below still comes from its own guard in every state (the accept tests prove the baseline passes)."""

    IID = "0f0e0d0c-0b0a-4908-8706-050403020100"
    SOURCE = "a" * 40
    AGENT = "registry.chifor.me/relay/infrastructure-agent@sha256:" + "b" * 64
    KEY = "A" * 43

    @staticmethod
    def committed(value, synthetic):
        """The committed value once the operator has filled it, else the synthetic stand-in."""
        return synthetic if bundle_module().is_placeholder(value) else copy.deepcopy(value)

    def setUp(self):
        p = pins()
        if bundle_module().pins_filled(p):
            self.IID, self.SOURCE, self.AGENT = p["installationId"], p["sourceCommit"], p["agentImage"]

    def make_bundle(self, mutate=None, stage=None) -> Path:
        """`stage`: the egress stage whose digest the definition pins; default the included one."""
        b = bundle_module()
        d = template()
        committed = json.loads((TREE / "agent/config.json").read_text(encoding="utf-8"))
        if campaign():
            # The tool's config is the I5 import: without the campaign's overlay, which check() applies.
            committed["policy"]["bootstrapSecrets"] = False
            committed.pop("candidateRuntime", None)
        key = self.committed(committed["identity"]["installationPublicKey"], self.KEY)
        # After step I10 the committed config carries the live fence UID; so does the definition then.
        fence = committed["identity"].get("fenceObjectUid")
        d["sourceCommit"] = self.SOURCE
        d["agentImage"] = self.AGENT
        d["identity"]["installationId"] = self.IID
        d["identity"]["installationPublicKey"] = key
        if fence:
            d["identity"]["fenceObjectUid"] = fence
        installation = copy.deepcopy(d["installationPolicy"])
        installation.update(allowedImageDigests=["c" * 64], allowedHarnessAdapters=["codex"],
                            allowedEgressDestinations=[])
        policy = {"installation": self.committed(committed["policy"]["installation"], installation),
                  "podTiers": self.committed(committed["policy"]["podTiers"], [
                      {"tier": "sandboxed-pod", "binding": "researcher", "runtimeClassName": "relay-gvisor"}]),
                  "allowedRuntimeDigests": self.committed(committed["policy"]["allowedRuntimeDigests"], ["d" * 64]),
                  "allowedTemplates": self.committed(committed["policy"]["allowedTemplates"], [
                      {"templateId": "researcher", "revision": 1, "digest": "e" * 64}])}
        d["installationPolicy"] = copy.deepcopy(policy["installation"])
        runtime_image = ("registry.chifor.me/relay/worker-runtime@sha256:"
                         + policy["installation"]["allowedImageDigests"][0])
        # The tool maps placements to podTiers and templates to allowedTemplates; invert that mapping.
        placements = [{**tier, "overhead": {"milliCpu": 80, "memoryMiB": 24},
                       # Every key of relay-gvisor's scheduling.nodeSelector (RuntimeClass admission merges it).
                       "nodeSelector": {"ailab.io/relay-worker-pool": "true", "ailab.io/runtime-gvisor": "true",
                                        "kubernetes.io/hostname": "relay-worker-1"},
                       "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "relay-worker",
                                        "effect": "NoSchedule"}]}
                      for tier in policy["podTiers"]]
        templates = [{"definition": {"templateId": t["templateId"], "revision": t["revision"]}, "digest": t["digest"]}
                     for t in policy["allowedTemplates"]]
        d["binding"] = {"installationId": self.IID, "policyDigest": "f" * 64, "runtime": {"image": runtime_image},
                        "placements": placements, "templates": templates}
        inst = TREE / "installation"
        lists = {
            "10-namespaces.json": b.with_installation_id(docs(inst / "namespaces.yaml"), self.IID),
            "20-containment.json": docs(inst / "containment.yaml"),
            "21-admission-deny.json": b.with_installation_id(docs(inst / "admission-deny.yaml"), self.IID),
        }
        r = d["agentResources"]
        res = {"cpu": f"{r['milliCpu']}m", "memory": f"{r['memoryMiB']}Mi", "ephemeral-storage": f"{r['ephemeralMiB']}Mi"}
        values = {
            "image": {"reference": d["agentImage"], "pullPolicy": "IfNotPresent"},
            "controlNamespace": CONTROL, "executionSlots": d["executionSlots"], "replicas": 1,
            "leadership": {"allowStandby": False, "leaseDurationSeconds": 15, "renewDeadlineSeconds": 10, "retryPeriodSeconds": 2},
            "hub": {"origin": d["hub"]["origin"], "caConfigMap": d["hub"]["caConfigMap"]},
            "kubernetes": {"origin": d["kubernetesOrigin"]},
            "identity": {"tenantId": d["identity"]["tenantId"], "installationId": self.IID,
                         "clusterUid": d["identity"]["clusterUid"], "installationPublicKey": key, "fenceObjectUid": fence or "",
                         "installationKeySecret": d["references"]["installationKeySecret"],
                         "enrollmentTokenSecret": d["references"].get("enrollmentTokenSecret", "")},
            "policy": {**copy.deepcopy(policy), "retainedStorageClass": None, "persistentVolumeClaims": False,
                       "bootstrapSecrets": False},
            "podRuntime": {"configMap": "", "bindingSha256": "", "qualificationSha256": ""},
            "candidateRuntime": {"configMap": "", "bindingSha256": "", "trustSha256": "", "trustConfigMap": "",
                                 "pinConfigMap": "", "holderSecret": "", "networkPolicyName": ""},
            "serviceAccountTokenSeconds": 3600, "resources": {"requests": res, "limits": res},
            "nodeSelector": d["controlPlacement"]["nodeSelector"], "tolerations": d["controlPlacement"]["tolerations"],
            "networkPolicy": {"enabled": True, "dnsSelector": {"namespace": "kube-system", "podLabels": {"k8s-app": "kube-dns"}},
                              "apiServerCidrs": d["controlNetwork"]["apiServerCidrs"],
                              "apiServerPorts": d["controlNetwork"]["apiServerPorts"],
                              "hubCidrs": d["controlNetwork"]["hubCidrs"], "hubPort": 443,
                              "candidateJournalCidrs": [], "candidateJournalPort": 443},
        }
        config = committed
        config["identity"]["installationId"] = self.IID
        config["identity"]["installationPublicKey"] = key
        config["policy"].update(copy.deepcopy(policy))
        files = {
            "00-definition.json": d,
            **{k: {"apiVersion": "v1", "kind": "List", "items": v} for k, v in lists.items()},
            "30-agent-values.json": values, "31-agent-config.json": config,
            "40-planned-pod-envelope.json": {"status": "planned-not-compiled"},
            "report.json": {"status": "enrollment-only-worker-creation-disabled", "workloadCreationEnabled": False,
                            "identity": {"sourceCommit": d["sourceCommit"], "agentImage": d["agentImage"],
                                         "runtimeImage": runtime_image,
                                         "runtimeDigest": policy["allowedRuntimeDigests"][0],
                                         "bindingDigest": "9" * 64, "installationId": self.IID,
                                         "clusterUid": d["identity"]["clusterUid"], "policyDigest": "f" * 64}},
        }
        # The reviewed shape carries G3 in both stages (the binding never changes between them).
        self.g3(files, stage or included_stage())
        if mutate:
            mutate(files)
        out = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in out.iterdir()] and out.rmdir())
        digests = {}
        for name, value in files.items():
            body = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
            (out / name).write_bytes(body)
            digests[name] = hashlib.sha256(body).hexdigest()
        (out / "bundle-manifest.json").write_text(json.dumps({
            "version": 1, "kind": "relay-installation-preparation-bundle",
            "status": "enrollment-only-worker-creation-disabled", "workloadCreationEnabled": False, "files": digests}))
        return out

    def run_check(self, bundle, stage=None, reason=None):
        """Exit code of `check`; with `reason`, a refusal must name it (not an earlier, unrelated check)."""
        module = bundle_module()
        err = io.StringIO()
        with contextlib.ExitStack() as patches:
            if stage is not None:
                # As after that stage's switch PR: the tree includes it and the template pins its digest.
                (egress,) = docs(EGRESS[stage])
                reviewed = module.template()
                reviewed["executionEgressArtifacts"][0]["sha256"] = module.measurement_digest(egress)
                patches.enter_context(mock.patch.object(module, "egress_stage", lambda: (stage, EGRESS[stage])))
                patches.enter_context(mock.patch.object(module, "template", lambda: copy.deepcopy(reviewed)))
            patches.enter_context(contextlib.redirect_stderr(err))
            patches.enter_context(contextlib.redirect_stdout(io.StringIO()))
            code = module.main(["check", str(bundle)])
        if reason is not None:
            self.assertEqual(code, 1)
            self.assertIn(reason, err.getvalue())
        return code

    def test_each_check_runs_on_its_own_module(self):
        # run_check patches the module it loads; that is isolated only while each call loads afresh.
        self.assertIsNot(bundle_module(), bundle_module())

    @staticmethod
    def g3(files, stage=None):
        """The release binding's G3 fields, as the qualification stage needs them; the definition pins
        `stage`'s egress digest (default: the included stage)."""
        d = files["00-definition.json"]
        pem = {"installPath": "/etc/relay/hub-ca.pem", "sha256": RUNTIME_CA_SHA256}
        d["binding"]["hostAliases"] = {"hub": "10.96.0.40", "router": "10.96.0.41", "candidateJournal": "10.96.0.42"}
        holder = {"router": {"origin": "https://router.chifor.me", "caFile": pem["installPath"], "caSha256": pem["sha256"]},
                  "communication": {"origin": "https://relay.chifor.me", "caFile": pem["installPath"],
                                    "caSha256": pem["sha256"]}}
        d["binding"]["runtime"]["manifest"] = {
            "supervisorConfig": {"hubOrigin": "https://relay.chifor.me", "hubCaPath": pem["installPath"],
                                 "hubCaSha256": pem["sha256"]},
            "artifacts": [{"role": "hub-ca", **pem}, {"role": "holder", "installPath": "/opt/relay/relay-holder",
                                                      "sha256": "1" * 64}],
            "holderProfiles": {"workerTemplates": [{"definition": {"holder": holder}}]}}
        BundleCheck.g3_pin(files, stage or included_stage())

    @staticmethod
    def g3_pin(files, stage):
        (egress,) = docs(EGRESS[stage])
        files["00-definition.json"]["executionEgressArtifacts"][0]["sha256"] = bundle_module().measurement_digest(egress)

    def test_accepts_the_g3_binding_in_both_stages(self):
        # The included stage as committed, then each stage as after its switch PR.
        self.assertEqual(self.run_check(self.make_bundle()), 0)
        for stage in ("enrollment", "qualification"):
            with self.subTest(stage=stage):
                self.assertEqual(self.run_check(self.make_bundle(stage=stage), stage), 0)

    def test_both_stages_require_the_g3_binding_and_their_own_pin(self):
        # The binding never changes between the stages: a binding without G3 is refused at enrollment
        # (step I4), not first discovered at the qualification switch (reviewer-codex, #1769).
        def no_aliases(files, stage):
            self.g3(files, stage)
            del files["00-definition.json"]["binding"]["hostAliases"]
        def null_aliases(files, stage):
            self.g3(files, stage)
            files["00-definition.json"]["binding"]["hostAliases"] = None
        def no_g3_at_all(files, stage):
            files["00-definition.json"]["binding"].pop("hostAliases")
            files["00-definition.json"]["binding"]["runtime"].pop("manifest")
            self.g3_pin(files, stage)
        for mutate in (no_aliases, null_aliases, no_g3_at_all):
            for stage in ("enrollment", "qualification"):
                with self.subTest(mutate.__name__, stage=stage):
                    self.run_check(self.make_bundle(lambda f, m=mutate, s=stage: m(f, s)), stage,
                                   reason="the binding has no hostAliases")
        # The enrollment pin with the qualification stage selected, and the reverse.
        self.run_check(self.make_bundle(stage="enrollment"), "qualification", reason="/executionEgressArtifacts differs")
        self.run_check(self.make_bundle(stage="qualification"), "enrollment", reason="/executionEgressArtifacts differs")

    def test_refuses_each_g3_difference(self):
        def m(files):
            return files["00-definition.json"]["binding"]["runtime"]["manifest"]
        def aliases_swapped(files):
            files["00-definition.json"]["binding"]["hostAliases"].update(hub="10.96.0.41", router="10.96.0.40")
        def alias_extra(files):
            files["00-definition.json"]["binding"]["hostAliases"]["git"] = "10.96.0.43"
        def no_journal_alias(files):
            del files["00-definition.json"]["binding"]["hostAliases"]["candidateJournal"]
        def hub_ca_mozilla(files):
            m(files)["supervisorConfig"]["hubCaSha256"] = MOZILLA_SHA256
            m(files)["artifacts"][0]["sha256"] = MOZILLA_SHA256
        def hub_origin(files):
            m(files)["supervisorConfig"]["hubOrigin"] = "https://relay.example"
        def router_unpinned(files):
            del m(files)["holderProfiles"]["workerTemplates"][0]["definition"]["holder"]["router"]["caSha256"]
        def router_ca_not_packaged(files):
            m(files)["holderProfiles"]["workerTemplates"][0]["definition"]["holder"]["router"]["caFile"] = "/tmp/ca.pem"
        def communication_other_ca(files):
            m(files)["holderProfiles"]["workerTemplates"][0]["definition"]["holder"]["communication"]["caSha256"] = "2" * 64
        def router_origin(files):
            m(files)["holderProfiles"]["workerTemplates"][0]["definition"]["holder"]["router"]["origin"] = "https://router.example"
        def no_templates(files):
            m(files)["holderProfiles"]["workerTemplates"] = []
        def null_runtime(files):
            # A clean refusal, not an AttributeError (reviewer-claude, #1769).
            files["00-definition.json"]["binding"]["runtime"] = None
        # Each refusal must come from its own guard, so a reordered or weakened check is caught.
        aliases, hub, router, communication = ("are not the pinned internal TLS and journal addresses",
                                               "hub origin or hub CA", "holder.router is not", "holder.communication is not")
        for mutate, reason in ((aliases_swapped, aliases), (alias_extra, aliases), (no_journal_alias, aliases),
                               (hub_ca_mozilla, hub), (hub_origin, hub), (router_unpinned, router),
                               (router_ca_not_packaged, router), (communication_other_ca, communication),
                               (router_origin, router), (no_templates, "no worker templates"),
                               (null_runtime, "hostAliases but no runtime manifest")):
            for stage in ("enrollment", "qualification"):
                with self.subTest(mutate.__name__, stage=stage):
                    def both(files, mutate=mutate, stage=stage):
                        self.g3(files, stage)
                        mutate(files)
                    self.run_check(self.make_bundle(both), stage, reason=reason)

    def test_accepts_the_reviewed_shape(self):
        self.assertEqual(self.run_check(self.make_bundle()), 0)

    def test_refuses_each_reviewed_difference(self):
        def quota(files):
            files["20-containment.json"]["items"][3]["spec"]["hard"]["pods"] = "1"
        def ceiling(files):
            files["00-definition.json"]["installationPolicy"]["resourceCeiling"]["milliCpu"] = 4000
        def unfilled(files):
            files["00-definition.json"]["binding"] = "OPERATOR-FILLS: still"
        def gate(files):
            files["21-admission-deny.json"]["items"][0]["spec"]["validations"][0]["expression"] = "true"
        def egress(files):
            files["00-definition.json"]["executionEgressArtifacts"][0]["sha256"] = "e" * 64
        def hub(files):
            files["30-agent-values.json"]["networkPolicy"]["hubCidrs"] = ["0.0.0.0/1"]
        def secrets(files):
            files["30-agent-values.json"]["policy"]["bootstrapSecrets"] = True
        def label(files):
            files["10-namespaces.json"]["items"][1]["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] = "baseline"
        def config(files):
            files["31-agent-config.json"]["hub"]["origin"] = "https://elsewhere.example"
        # The generated agent policy, which the committed config cannot pin (reviewer-codex, #1766):
        def config_ceiling(files):
            files["31-agent-config.json"]["policy"]["installation"]["resourceCeiling"]["milliCpu"] = 4000
            files["30-agent-values.json"]["policy"]["installation"]["resourceCeiling"]["milliCpu"] = 4000
        def config_registries(files):
            for f in ("31-agent-config.json", "30-agent-values.json"):
                files[f]["policy"]["installation"]["allowedRegistries"] = ["registry.chifor.me", "ghcr.io"]
        def values_tiers(files):
            files["30-agent-values.json"]["policy"]["podTiers"] = []
        def tiers_not_binding(files):
            for f in ("31-agent-config.json", "30-agent-values.json"):
                files[f]["policy"]["podTiers"][0]["runtimeClassName"] = None
        def templates_not_binding(files):
            for f in ("31-agent-config.json", "30-agent-values.json"):
                files[f]["policy"]["allowedTemplates"][0]["digest"] = "7" * 64
        def two_runtimes(files):
            for f in ("31-agent-config.json", "30-agent-values.json"):
                files[f]["policy"]["allowedRuntimeDigests"].append("8" * 64)
        def placement_off_node(files):
            files["00-definition.json"]["binding"]["placements"][0]["nodeSelector"]["kubernetes.io/hostname"] = "talos-agent-node-1"
        def placement_overhead(files):
            files["00-definition.json"]["binding"]["placements"][0]["overhead"]["milliCpu"] = 0
        def runtime_not_allowed(files):
            files["00-definition.json"]["binding"]["runtime"]["image"] = "registry.chifor.me/relay/worker-runtime@sha256:" + "6" * 64
        def report_policy(files):
            files["report.json"]["identity"]["policyDigest"] = "5" * 64
        def report_runtime(files):
            files["report.json"]["identity"]["runtimeDigest"] = "4" * 64
        def empty_fill(files):
            files["00-definition.json"]["installationPolicy"]["allowedHarnessAdapters"] = []
        # The same joins from the definition's side, so they stay proven once I5 has committed the policy.
        def definition_policy(files):
            files["00-definition.json"]["installationPolicy"]["allowedImageDigests"].append("9" * 64)
        def binding_tiers(files):
            files["00-definition.json"]["binding"]["placements"][0]["binding"] = "other-binding"
        def binding_templates(files):
            files["00-definition.json"]["binding"]["templates"][0]["digest"] = "7" * 64
        # Before I5 the tool's policy is checked only by its join to the definition; once I5 commits the
        # real config, the committed config pins it and refuses a different one first.
        committed = json.loads((TREE / "agent/config.json").read_text(encoding="utf-8"))
        filled = not bundle_module().is_placeholder(committed["policy"]["installation"])
        def pinned(leaf, join):
            return f"31-agent-config.json /policy/{leaf} = " if filled else join
        installation_join = "31-agent-config.json policy.installation differs from the definition's installationPolicy"
        for mutate, reason in (
                (quota, "20-containment.json: ('ResourceQuota', 'relay-researcher-1', 'relay-enrollment-quota') differs"),
                (ceiling, "definition /installationPolicy/resourceCeiling/milliCpu differs from the reviewed template"),
                (unfilled, "definition /binding is still a placeholder"),
                (gate, "21-admission-deny.json: ('ValidatingAdmissionPolicy', None, 'relay-enrollment-"),
                (egress, "definition /executionEgressArtifacts differs from the reviewed template"),
                (hub, "30-agent-values.json networkPolicy.hubCidrs = ['0.0.0.0/1']"),
                (secrets, "30-agent-values.json policy.bootstrapSecrets = True"),
                (label, "10-namespaces.json: ('Namespace', None, 'relay-researcher-1') differs"),
                (config, "31-agent-config.json /hub/origin = 'https://elsewhere.example'"),
                (config_ceiling, pinned("installation/resourceCeiling/milliCpu", installation_join)),
                (config_registries, pinned("installation/allowedRegistries", installation_join)),
                (values_tiers, "30-agent-values.json policy.podTiers differs from 31-agent-config.json"),
                (tiers_not_binding, pinned("podTiers", "policy.podTiers is not the binding's placements")),
                (templates_not_binding, pinned("allowedTemplates", "policy.allowedTemplates is not the binding's templates")),
                (two_runtimes, pinned("allowedRuntimeDigests", "policy.allowedRuntimeDigests is not exactly one runtime digest")),
                (definition_policy, installation_join),
                (binding_tiers, "policy.podTiers is not the binding's placements"),
                (binding_templates, "policy.allowedTemplates is not the binding's templates"),
                (placement_off_node, "a placement does not pin kubernetes.io/hostname relay-worker-1"),
                (placement_overhead, "a placement's overhead is not relay-gvisor's 80m / 24Mi"),
                (runtime_not_allowed, "the binding's runtime image is not a digest-pinned registry.chifor.me image"),
                (report_policy, "report.json identity.policyDigest differs from the definition"),
                (report_runtime, "report.json identity.runtimeDigest differs from the definition"),
                (empty_fill, "definition /installationPolicy/allowedHarnessAdapters is empty")):
            with self.subTest(mutate.__name__):
                self.run_check(self.make_bundle(mutate), reason=reason)

    def test_placement_carries_the_runtime_class_scheduling(self):
        # relay-gvisor's scheduling block, read from its manifest: RuntimeClass admission merges it into
        # every Pod, and Relay pins the placement's selector exactly (VAP CEL equality, executor readback).
        self.assertEqual(bundle_module().runtime_class_scheduling(), (
            {"ailab.io/relay-worker-pool": "true", "ailab.io/runtime-gvisor": "true"},
            [{"key": "dedicated", "operator": "Equal", "value": "relay-worker", "effect": "NoSchedule"}]))
        def placement(files):
            return files["00-definition.json"]["binding"]["placements"][0]
        def without_class_selector(files):
            del placement(files)["nodeSelector"]["ailab.io/runtime-gvisor"]
        def other_class_selector_value(files):
            placement(files)["nodeSelector"]["ailab.io/runtime-gvisor"] = "false"
        def other_toleration(files):
            placement(files)["tolerations"][0]["value"] = "agent"
        def extra_toleration(files):
            placement(files)["tolerations"].append({"key": "dedicated", "operator": "Equal", "value": "agent",
                                                    "effect": "NoSchedule"})
        selector, tolerations = "lacks relay-gvisor's scheduling.nodeSelector", "tolerations are not relay-gvisor's"
        # make_bundle follows the committed pins, config and egress stage, so these hold in every state.
        for mutate, reason in ((without_class_selector, selector), (other_class_selector_value, selector),
                               (other_toleration, tolerations), (extra_toleration, tolerations)):
            with self.subTest(mutate.__name__):
                self.run_check(self.make_bundle(mutate), reason=reason)

    def test_runtime_class_tolerations_keep_every_attribute(self):
        # A numeric attribute (tolerationSeconds) is read, not dropped, so the placement comparison sees
        # it; HCL comments are not configuration and change nothing; an attribute the reader does not
        # understand is refused rather than skipped.
        tf = ROOT / "kubernetes/infra/agent-nodes/relay-workers.tf"
        text = tf.read_text(encoding="utf-8")
        effect = '        effect   = "NoSchedule"\n'
        self.assertEqual(text.count(effect), 1)
        numeric, unchanged = "tolerations are not relay-gvisor's", "accepted"
        for replacement, expect in (
                (effect + '        tolerationSeconds = 300\n', numeric),
                ('        # the dedicated pool\n        effect   = "NoSchedule" # trailing, with a } brace\n'
                 '        // and a line comment\n        /* and a\n           block comment */\n', unchanged),
                (effect + '        tolerationSeconds = var.seconds\n', "toleration attribute not understood"),
                (effect + '        effect = "NoExecute"\n', "toleration attribute not understood")):
            with self.subTest(replacement.strip()), tempfile.TemporaryDirectory() as tmp:
                patched = Path(tmp) / "relay-workers.tf"
                patched.write_text(text.replace(effect, replacement), encoding="utf-8")
                module = bundle_module()
                with mock.patch.object(module, "RELAY_WORKERS_TF", patched), \
                        mock.patch.dict(globals(), {"bundle_module": lambda: module}):
                    if expect == numeric:
                        _, tolerations = module.runtime_class_scheduling()
                        self.assertEqual(tolerations[0]["tolerationSeconds"], 300)
                        # A placement without it no longer matches the class, so the bundle is refused.
                        self.run_check(self.make_bundle(), reason=numeric)
                    elif expect == unchanged:
                        self.assertEqual(module.runtime_class_scheduling()[1], [
                            {"key": "dedicated", "operator": "Equal", "value": "relay-worker", "effect": "NoSchedule"}])
                        self.assertEqual(self.run_check(self.make_bundle()), 0)
                    else:
                        with self.assertRaises(module.Mismatch) as refused:
                            module.runtime_class_scheduling()
                        self.assertIn(expect, str(refused.exception))

    def test_refuses_a_tampered_file(self):
        bundle = self.make_bundle()
        path = bundle / "20-containment.json"
        path.write_bytes(path.read_bytes().replace(b'"pods":"0"', b'"pods":"9"'))
        self.run_check(bundle, reason="20-containment.json does not match bundle-manifest.json")


if __name__ == "__main__":
    unittest.main()
