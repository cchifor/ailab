#!/usr/bin/env python3
"""Source gates for the first Relay researcher installation and its hub upgrade components.

docs/runbooks/relay-researcher-installation.md and relay-researcher-hub-upgrade.md. Everything here
is staged and inert until the operator's inclusion PRs; these tests pin what those PRs may include
and refuse an inclusion that still carries a placeholder. The rendered shapes are proven by
scripts/tests/test-relay-researcher-render.sh (docker, manifests.yaml).
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import importlib.util
import ipaddress
import json
import re
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TREE = ROOT / "kubernetes/apps/relay-researcher"
CLUSTER = ROOT / "kubernetes/apps/clusters/ai"
COMPONENTS = ROOT / "kubernetes/components"
RELAY_OVERLAY = ROOT / "kubernetes/apps/apps/relay/kustomization.yaml"
TEMPLATE = ROOT / "docs/relay-workers/researcher-installation-definition.json"
CONTROL, SLOT = "relay-infra-control", "relay-researcher-1"
MOZILLA_SHA256 = "427c064216761a1e72a8b30f0d954d08cb6e4435a82091fb865afd20c8497285"
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


def relay_components() -> list[str]:
    return yaml.safe_load(RELAY_OVERLAY.read_text(encoding="utf-8")).get("components", [])


class InclusionGate(unittest.TestCase):
    """Nothing is live on merge; an inclusion with a placeholder is refused."""

    def test_staged_flux_kustomizations(self):
        inst = docs(TREE / "flux/relay-researcher-installation.yaml")[0]
        agent = docs(TREE / "flux/relay-researcher-agent.yaml")[0]
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
        for path in sorted((TREE / "installation").glob("*.yaml")) + sorted((TREE / "agent").glob("*.yaml")):
            text = path.read_text(encoding="utf-8")
            for value in pins().values():
                self.assertNotIn(value, text, f"{path} holds a literal pin")

    def test_hub_components_are_included_only_together(self):
        comps = {c.rsplit("/", 1)[-1] for c in relay_components()}
        if "relay-infrastructure-installations" in comps or "relay-restore-fencing" in comps:
            self.assertTrue({"relay-infrastructure-installations", "relay-restore-fencing"} <= comps,
                            "installations need the recovery generation and vice versa (step H3)")
        if "relay-worker-profiles" in comps:
            self.assertIn("relay-infrastructure-installations", comps)
            self.assertIn("relay-worker-budget-client", comps)
            body = (COMPONENTS / "relay-worker-profiles/profiles.json").read_bytes()
            self.assertNotIn(b"OPERATOR-FILLS", body)
            patch = docs(COMPONENTS / "relay-worker-profiles/deployment.yaml")[0]
            env = {e["name"]: e["value"] for e in patch["spec"]["template"]["spec"]["containers"][0]["env"]}
            self.assertEqual(env["RELAY_WORKER_PROFILES_SHA256"], hashlib.sha256(body).hexdigest())
        if "relay-agent-control-plane" in comps:
            self.assertTrue({"relay-router-connection", "relay-worker-profiles"} <= comps,
                            "the control plane comes with the Router connection, last (step H6)")


class InstallationStage(unittest.TestCase):
    """The objects equal what Relay's prepare-pod-installation.ts renders from the template."""

    def setUp(self):
        self.d = template()
        self.objects = by_key([o for f in sorted((TREE / "installation").glob("*.yaml"))
                               if f.name != "kustomization.yaml" for o in docs(f)])

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

    def test_allow_list_is_dns_plus_the_two_public_edge_addresses(self):
        egress = self.objects[("NetworkPolicy", SLOT, "relay-researcher-egress")]
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

    def test_egress_artifact_digest(self):
        egress = self.objects[("NetworkPolicy", SLOT, "relay-researcher-egress")]
        # The measurement normalizes empty collections and defaulted protocols away; the committed
        # object has neither, so Relay's digest equals the plain canonical hash.
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
        walk(egress["spec"]["egress"])
        for rule in egress["spec"]["egress"]:
            for port in rule["ports"]:
                self.assertIn("protocol", port)
        digest = bundle_module().measurement_digest(egress)
        self.assertEqual(self.d["executionEgressArtifacts"],
                         [{"namespace": SLOT, "name": "relay-researcher-egress", "sha256": digest}])
        self.assertEqual(digest, "aa11c2299af12acc886696480b5800ac0b568a0b6e2813609d4ed2f7eea38405")


class AgentStage(unittest.TestCase):
    """The agent mirror equals the chart's enrollment-only render (proven against the pinned chart
    by relay-researcher-bundle.py check --chart) and carries only the template's values."""

    def setUp(self):
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
        self.assertEqual(roles[("Role", SLOT, "relay-infrastructure-agent-workloads")]["rules"], [
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "create", "delete"]}])
        self.assertEqual(roles[("ClusterRole", None, "relay-infrastructure-agent-relay-infra-control-cluster-uid")]["rules"], [
            {"apiGroups": [""], "resources": ["namespaces"], "resourceNames": ["kube-system"], "verbs": ["get"]}])
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
        for resource, _ in granted:
            self.assertNotIn(resource, {"secrets", "pods/exec", "pods/attach", "pods/portforward", "pods/log",
                                        "persistentvolumeclaims", "roles", "rolebindings", "*"})
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
        self.assertEqual(vols["hub-ca"]["configMap"]["name"], self.d["hub"]["caConfigMap"])
        token = vols["kubernetes"]["projected"]["sources"][0]["serviceAccountToken"]
        self.assertEqual(token, {"path": "token", "expirationSeconds": 3600})

    def test_network_policy_is_the_template_control_network(self):
        net = self.d["controlNetwork"]
        np = self.objects[("NetworkPolicy", CONTROL, "relay-infrastructure-agent")]["spec"]
        self.assertEqual(np["podSelector"], {"matchLabels": {k: LABELS[k] for k in ("app.kubernetes.io/name", "app.kubernetes.io/instance")}})
        self.assertEqual(np["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(np["ingress"], [])
        dns, api, hub = np["egress"]
        self.assertEqual(dns["to"], [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": net["dnsNamespace"]}},
                                      "podSelector": {"matchLabels": net["dnsPodLabels"]}}])
        self.assertEqual([p["ipBlock"]["cidr"] for p in api["to"]], net["apiServerCidrs"])
        self.assertEqual([p["port"] for p in api["ports"]], net["apiServerPorts"])
        self.assertEqual([p["ipBlock"]["cidr"] for p in hub["to"]], net["hubCidrs"])
        self.assertEqual(hub["ports"], [{"protocol": "TCP", "port": 443}])
        cnp = self.objects[("CiliumNetworkPolicy", CONTROL, "relay-infrastructure-agent-api-egress")]["spec"]
        self.assertEqual(cnp["egress"], [{"toEntities": ["kube-apiserver"],
                                          "toPorts": [{"ports": [{"port": str(p), "protocol": "TCP"} for p in net["apiServerPorts"]]}]}])

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
        self.assertIs(config["policy"]["bootstrapSecrets"], False)
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


class BundleCheck(unittest.TestCase):
    """scripts/relay-researcher-bundle.py accepts a bundle shaped like the tool's for this installation
    and refuses every reviewed difference. The bundle here is synthetic test input, never committed."""

    IID = "0f0e0d0c-0b0a-4908-8706-050403020100"

    def make_bundle(self, mutate=None) -> Path:
        b = bundle_module()
        d = template()
        d["sourceCommit"] = "a" * 40
        d["agentImage"] = "registry.chifor.me/relay/infrastructure-agent@sha256:" + "b" * 64
        d["identity"]["installationId"] = self.IID
        d["identity"]["installationPublicKey"] = "A" * 43
        d["installationPolicy"]["allowedImageDigests"] = ["c" * 64]
        d["installationPolicy"]["allowedHarnessAdapters"] = ["codex"]
        d["installationPolicy"]["allowedEgressDestinations"] = []
        d["binding"] = {"synthetic": True}
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
                         "clusterUid": d["identity"]["clusterUid"], "installationPublicKey": "A" * 43, "fenceObjectUid": "",
                         "installationKeySecret": d["references"]["installationKeySecret"],
                         "enrollmentTokenSecret": d["references"]["enrollmentTokenSecret"]},
            "policy": {"installation": d["installationPolicy"], "podTiers": [], "allowedRuntimeDigests": [],
                       "allowedTemplates": [], "retainedStorageClass": None, "persistentVolumeClaims": False,
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
        config = json.loads((TREE / "agent/config.json").read_text(encoding="utf-8"))
        config["identity"]["installationId"] = self.IID
        config["identity"]["installationPublicKey"] = "A" * 43
        config["policy"].update({"allowedRuntimeDigests": ["d" * 64], "allowedTemplates": [],
                                 "installation": d["installationPolicy"], "podTiers": []})
        files = {
            "00-definition.json": d,
            **{k: {"apiVersion": "v1", "kind": "List", "items": v} for k, v in lists.items()},
            "30-agent-values.json": values, "31-agent-config.json": config,
            "40-planned-pod-envelope.json": {"status": "planned-not-compiled"},
            "report.json": {"status": "enrollment-only-worker-creation-disabled", "workloadCreationEnabled": False},
        }
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

    def run_check(self, bundle):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return bundle_module().main(["check", str(bundle)])

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
        for mutate in (quota, ceiling, unfilled, gate, egress, hub, secrets, label, config):
            with self.subTest(mutate.__name__):
                self.assertEqual(self.run_check(self.make_bundle(mutate)), 1)

    def test_refuses_a_tampered_file(self):
        bundle = self.make_bundle()
        path = bundle / "20-containment.json"
        path.write_bytes(path.read_bytes().replace(b'"pods":"0"', b'"pods":"9"'))
        self.assertEqual(self.run_check(bundle), 1)


if __name__ == "__main__":
    unittest.main()
