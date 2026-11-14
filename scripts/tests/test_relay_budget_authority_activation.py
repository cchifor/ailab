#!/usr/bin/env python3
"""The Relay worker budget ledger's activation overlay keeps the runbook's gates.

    python3 -m unittest scripts.tests.test_relay_budget_authority_activation

docs/runbooks/relay-worker-budget-authority.md is the contract. These checks pin what the source
can prove: the install-order preconditions in the Flux Kustomization, the zero/one replica rule,
the operator-only initialization Jobs staying out of every Flux inventory, the Velero exclusion,
the preserved application egress baselines, the OpenBao/ESO delivery (no secret in git, policies
matching the ExternalSecrets), and the consumer components (plugin disabled, mount contract,
no inclusion with a placeholder deployment digest). Nothing here touches a cluster.
"""

from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / "kubernetes"
CLUSTER = K8S / "apps/clusters/ai"
BASE = K8S / "optional/relay-budget-authority"
OVERLAY = K8S / "apps/relay-budget-authority/ledger"
CREDENTIALS = K8S / "apps/relay-budget-authority/credentials"
PROVISION = K8S / "apps/infrastructure/security/openbao/relay-budget-provision-job.yaml"
RELAY_COMPONENT = K8S / "components/relay-worker-budget-client"
ROUTER_COMPONENT = K8S / "components/llm-router-worker-budget-reader"
LEDGER_HOST = "relay-budget-postgres.relay-budget-authority.svc.cluster.local"

# The documented OpenBao contract: KV-v2 mount `af`, one document per (path, field).
OPENBAO = {
    "relay": {"login": "relay_budget_allocator", "password": "relay-budget/allocator",
              "store_role": "af-app-relay-budget", "sa": "relay-budget-eso",
              "secret": "relay-worker-budget"},
    "llm-router": {"login": "relay_budget_router", "password": "relay-budget/router",
                   "store_role": "af-app-router-budget", "sa": "router-budget-eso",
                   "secret": "llm-router-worker-budget"},
}
SHARED = {"relay-budget/ca": "certificate", "relay-budget/witness": "witness"}
FILES = {"dsn", "ca.crt", "witness.json"}


def docs(path):
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def one(path, kind, name=None):
    found = [d for d in docs(path) if d["kind"] == kind
             and (name is None or d["metadata"]["name"] == name)]
    if len(found) != 1:
        raise AssertionError(f"expected one {kind} {name or ''} in {path}, found {len(found)}")
    return found[0]


def flux_kustomizations():
    out = {}
    for path in CLUSTER.rglob("*.yaml"):
        for d in docs(path):
            if d.get("kind") == "Kustomization" and d.get("apiVersion", "").startswith(
                    "kustomize.toolkit.fluxcd.io"):
                out[d["metadata"]["name"]] = d
    return out


def provision_script():
    cm = one(PROVISION, "ConfigMap")
    return cm["data"]["provision.sh"]


def normalise(policy):
    lines = [line for line in policy.splitlines() if not re.match(r"^\s*#", line)]
    return re.sub(r"\s", "", "".join(lines))


class FluxInstallOrder(unittest.TestCase):
    def test_ledger_kustomization_encodes_the_runbook_preconditions(self):
        ks = flux_kustomizations()["relay-budget-authority"]["spec"]
        self.assertEqual(ks["path"], "./kubernetes/apps/relay-budget-authority/ledger")
        self.assertIs(ks["wait"], True, "the PVC and Certificate must gate readiness")
        self.assertIs(ks["prune"], True)
        self.assertNotIn("suspend", ks)
        deps = {d["name"] for d in ks["dependsOn"]}
        # StorageClass, ClusterIssuer, the app egress baselines, the Velero exclusion: all first.
        self.assertTrue({"qnap-storage", "cert-manager-config", "apps", "backup"} <= deps, deps)
        self.assertNotIn("decryption", ks, "nothing in the ledger overlay is a secret")

    def test_credentials_kustomization_never_gates_a_layer(self):
        ks = flux_kustomizations()["relay-budget-credentials"]["spec"]
        self.assertEqual(ks["path"], "./kubernetes/apps/relay-budget-authority/credentials")
        self.assertIs(ks["wait"], False)
        self.assertIn("external-secrets", {d["name"] for d in ks["dependsOn"]})
        for name, k in flux_kustomizations().items():
            for dep in k["spec"].get("dependsOn", []):
                self.assertNotEqual(dep["name"], "relay-budget-credentials", name)

    def test_no_flux_path_reaches_the_operator_only_jobs(self):
        for name, k in flux_kustomizations().items():
            self.assertNotIn("relay-budget-authority/initialize", k["spec"].get("path", ""), name)
        for path in (K8S / "apps").rglob("kustomization.yaml"):
            k = yaml.safe_load(path.read_text()) or {}
            for entry in k.get("resources", []) + k.get("components", []):
                self.assertNotIn("relay-budget-authority/initialize", entry, path)


class LedgerOverlay(unittest.TestCase):
    def test_overlay_uses_the_reviewed_base_and_only_sets_replicas(self):
        k = yaml.safe_load((OVERLAY / "kustomization.yaml").read_text())
        self.assertEqual(k["resources"], ["../../../optional/relay-budget-authority"])
        self.assertEqual(k["patches"], [{"path": "replicas.yaml"}])
        for key in ("components", "configMapGenerator", "secretGenerator", "images", "namespace"):
            self.assertNotIn(key, k)
        patch = yaml.safe_load((OVERLAY / "replicas.yaml").read_text())
        self.assertEqual((patch["kind"], patch["metadata"]["name"]),
                         ("StatefulSet", "relay-budget-postgres"))
        self.assertEqual(set(patch["spec"]), {"replicas"})
        # Zero until the initialization Job succeeded; then ONE, never more (runbook step 4).
        self.assertIn(patch["spec"]["replicas"], (0, 1))
        for path in OVERLAY.iterdir():
            for d in docs(path):
                self.assertNotEqual(d.get("kind"), "Job", path)

    def test_base_keeps_storage_retention_ca_and_backup_guards(self):
        base = yaml.safe_load((BASE / "kustomization.yaml").read_text())
        self.assertNotIn("initialize", base["resources"])
        self.assertEqual(base["labels"][0]["pairs"]["velero.io/exclude-from-backup"], "true")
        self.assertIs(base["labels"][0]["includeTemplates"], True)
        server = one(BASE / "postgres.yaml", "StatefulSet")
        self.assertEqual(server["spec"]["replicas"], 0)
        self.assertEqual(server["spec"]["updateStrategy"]["type"], "OnDelete")
        pvc = one(BASE / "storage.yaml", "PersistentVolumeClaim")
        self.assertEqual(pvc["spec"]["storageClassName"], "qnap-iscsi")
        self.assertEqual(pvc["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"], "disabled")
        ns = one(BASE / "namespace.yaml", "Namespace")
        self.assertEqual(ns["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"], "disabled")
        sc = one(K8S / "apps/qnap-storage/storageclass.yaml", "StorageClass", "qnap-iscsi")
        self.assertEqual(sc["reclaimPolicy"], "Retain")
        cert = one(BASE / "certificate.yaml", "Certificate")
        self.assertEqual(cert["spec"]["issuerRef"], {"name": "ailab-ca", "kind": "ClusterIssuer"})
        self.assertIn(LEDGER_HOST, cert["spec"]["dnsNames"])
        self.assertEqual(cert["spec"]["secretTemplate"]["labels"]["velero.io/exclude-from-backup"], "true")

    def test_both_velero_schedules_exclude_the_ledger(self):
        hr = one(K8S / "apps/backup/velero/helmrelease.yaml", "HelmRelease")
        schedules = hr["spec"]["values"]["schedules"]
        self.assertEqual(set(schedules), {"daily", "weekly"})
        for name, schedule in schedules.items():
            self.assertIn("relay-budget-authority", schedule["template"]["excludedNamespaces"], name)


class EgressBaseline(unittest.TestCase):
    """The base's ledger-egress policies are additive ONLY over the apps' reviewed baselines."""

    def baseline(self, path, name, app):
        policy = one(path, "NetworkPolicy", name)
        self.assertEqual(policy["spec"]["podSelector"], {"matchLabels": {"app": app}})
        self.assertIn("Egress", policy["spec"]["policyTypes"])
        ports = [p.get("port") for rule in policy["spec"]["egress"] for p in rule.get("ports", [])]
        self.assertIn(53, ports, f"{name}: DNS egress missing from the baseline")
        self.assertIn(443, ports, f"{name}: upstream HTTPS egress missing from the baseline")
        return policy

    def test_application_baselines_exist_and_select_the_same_pods(self):
        relay = K8S / "apps/apps/relay"
        router = K8S / "apps/apps/llm-router"
        self.assertIn("network-policy.yaml", yaml.safe_load((relay / "kustomization.yaml").read_text())["resources"])
        self.assertIn("router.yaml", yaml.safe_load((router / "kustomization.yaml").read_text())["resources"])
        self.baseline(relay / "network-policy.yaml", "relay", "relay")
        self.baseline(router / "router.yaml", "llm-router", "llm-router")
        additive = {d["metadata"]["namespace"]: d for d in docs(BASE / "network-policy.yaml")
                    if d["metadata"]["namespace"] in ("relay", "llm-router")}
        self.assertEqual(additive["relay"]["spec"]["podSelector"], {"matchLabels": {"app": "relay"}})
        self.assertEqual(additive["llm-router"]["spec"]["podSelector"], {"matchLabels": {"app": "llm-router"}})
        for policy in additive.values():
            self.assertEqual(policy["spec"]["policyTypes"], ["Egress"])
            self.assertEqual([p["port"] for p in policy["spec"]["egress"][0]["ports"]], [5432])


class CredentialDelivery(unittest.TestCase):
    def test_no_secret_material_and_exact_documents(self):
        k = yaml.safe_load((CREDENTIALS / "kustomization.yaml").read_text())
        self.assertEqual(k["resources"], ["relay.yaml", "llm-router.yaml"])
        for namespace, want in OPENBAO.items():
            path = CREDENTIALS / f"{namespace}.yaml"
            kinds = [d["kind"] for d in docs(path)]
            self.assertNotIn("Secret", kinds, path)
            sa = one(path, "ServiceAccount")
            self.assertEqual((sa["metadata"]["name"], sa["metadata"]["namespace"]), (want["sa"], namespace))
            self.assertIs(sa["automountServiceAccountToken"], False)
            store = one(path, "SecretStore")["spec"]["provider"]["vault"]
            self.assertEqual((store["path"], store["version"]), ("af", "v2"))
            self.assertEqual(store["auth"]["kubernetes"]["role"], want["store_role"])
            self.assertEqual(store["auth"]["kubernetes"]["serviceAccountRef"]["name"], want["sa"])
            es = one(path, "ExternalSecret")["spec"]
            refs = {d["remoteRef"]["key"]: d["remoteRef"]["property"] for d in es["data"]}
            self.assertEqual(refs, {want["password"]: "password", **SHARED})
            target = es["target"]
            self.assertEqual(target["name"], want["secret"])
            template = target["template"]
            self.assertEqual(template["mergePolicy"], "Replace")
            self.assertEqual(set(template["data"]), FILES)
            self.assertEqual(template["metadata"]["labels"]["velero.io/exclude-from-backup"], "true")
            dsn = template["data"]["dsn"]
            self.assertEqual(
                dsn, f"postgresql://{want['login']}:{{{{ .password | urlquery }}}}@{LEDGER_HOST}:5432/relay_budget")
            self.assertNotIn("?", dsn, "no query options: TLS verification cannot be weakened")

    def test_provision_policies_grant_exactly_what_the_stores_read(self):
        script = provision_script()
        self.assertNotRegex(script, r"bao kv (put|patch|delete|destroy|undelete)",
                            "the KV documents are the ceremony's, never this Job's")
        heredocs = dict(re.findall(r"bao policy write (\S+) - <<'EOF'\n(.*?)\nEOF", script, re.S))
        self.assertEqual(set(heredocs), {"af-app-relay-budget", "af-app-router-budget"})
        for namespace, want in OPENBAO.items():
            policy = heredocs[want["store_role"]]
            grants = re.findall(r'path "([^"]+)" \{ capabilities = \["read"\] \}', policy)
            self.assertEqual(len(grants), len(policy.strip().splitlines()), policy)
            self.assertEqual(set(grants), {f"af/data/{p}" for p in (want["password"], *SHARED)})
            label = "RELAY" if namespace == "relay" else "ROUTER"
            desired = re.search(rf"^\s*{label}_DESIRED='([^']*)'", script, re.M).group(1)
            self.assertEqual(normalise(policy), desired)
            self.assertIn(f"write_role {want['store_role']} {want['sa']} {namespace}", script)
        job = one(PROVISION, "Job")
        self.assertEqual(job["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/force"], "enabled")
        openbao = yaml.safe_load((PROVISION.parent / "kustomization.yaml").read_text())
        self.assertIn(PROVISION.name, openbao["resources"])


class ConsumerComponents(unittest.TestCase):
    def component(self, directory, deployment, container, secret, prefix):
        k = yaml.safe_load((directory / "kustomization.yaml").read_text())
        self.assertEqual(k["kind"], "Component")
        self.assertEqual(k["patches"], [{"path": "deployment.yaml"}])
        for key in ("resources", "components", "configMapGenerator", "secretGenerator"):
            self.assertNotIn(key, k)
        patch = yaml.safe_load((directory / "deployment.yaml").read_text())
        self.assertEqual((patch["kind"], patch["metadata"]["name"]), ("Deployment", deployment))
        spec = patch["spec"]["template"]["spec"]
        self.assertEqual(set(spec), {"containers", "volumes"})
        [c] = spec["containers"]
        self.assertEqual(c["name"], container)
        self.assertEqual(set(c), {"name", "env", "volumeMounts"})
        env = {e["name"]: e["value"] for e in c["env"]}
        mounts = {m["mountPath"]: m for m in c["volumeMounts"]}
        files = {f"{prefix}_DSN_FILE": "dsn", f"{prefix}_CA_FILE": "ca.crt",
                 f"{prefix}_WITNESS_FILE": "witness.json"}
        for name, key in files.items():
            path = env[name]
            # Canonical absolute path, no symlinked ancestor: /run, never /var/run.
            self.assertTrue(path.startswith("/run/"), path)
            self.assertEqual(mounts[path]["subPath"], key)
            self.assertIs(mounts[path]["readOnly"], True)
            self.assertEqual(mounts[path]["name"], "worker-budget")
        self.assertEqual(len(mounts), 3)
        [volume] = spec["volumes"]
        self.assertEqual(volume["name"], "worker-budget")
        self.assertEqual(volume["secret"]["secretName"], secret)
        self.assertEqual(volume["secret"]["defaultMode"] & 0o022, 0, "group/world-writable files are refused")
        self.assertNotIn("optional", volume["secret"])
        return env

    def test_relay_client_component(self):
        env = self.component(RELAY_COMPONENT, "relay", "relay", "relay-worker-budget", "RELAY_WORKER_BUDGET")
        self.assertEqual(set(env), {"RELAY_WORKER_BUDGET_DSN_FILE", "RELAY_WORKER_BUDGET_CA_FILE",
                                    "RELAY_WORKER_BUDGET_WITNESS_FILE"})

    def test_router_reader_component_never_enables_the_plugin(self):
        env = self.component(ROUTER_COMPONENT, "llm-router", "router", "llm-router-worker-budget",
                             "ROUTER_WORKER_BUDGET")
        self.assertEqual(set(env) - {"ROUTER_WORKER_BUDGET_DEPLOYMENT_DIGEST"},
                         {"ROUTER_WORKER_BUDGET_DSN_FILE", "ROUTER_WORKER_BUDGET_CA_FILE",
                          "ROUTER_WORKER_BUDGET_WITNESS_FILE"})
        # The env above is the whole patch: no ROUTER_PLUGIN_CONFIG, and the only mounts are the three
        # files under /run, so /data/plugins.yml (the admin API's enable state) is untouched.
        self.assertNotIn("ROUTER_PLUGIN_CONFIG", env)

    def test_router_component_is_not_live_with_a_placeholder_digest(self):
        router = yaml.safe_load((K8S / "apps/apps/llm-router/kustomization.yaml").read_text())
        patch = yaml.safe_load((ROUTER_COMPONENT / "deployment.yaml").read_text())
        [c] = patch["spec"]["template"]["spec"]["containers"]
        digest = {e["name"]: e["value"] for e in c["env"]}["ROUTER_WORKER_BUDGET_DEPLOYMENT_DIGEST"]
        if "../../../components/llm-router-worker-budget-reader" in router.get("components", []):
            self.assertRegex(digest, r"^[a-f0-9]{64}$",
                             "set the reviewed Relay router deployment digest before including this")
        else:
            self.assertNotRegex(digest, r"^[a-f0-9]{64}$",
                                "a real digest belongs in the same PR that includes the component")

    def test_component_secrets_are_the_ones_eso_renders(self):
        for namespace, want in OPENBAO.items():
            es = one(CREDENTIALS / f"{namespace}.yaml", "ExternalSecret")
            directory = RELAY_COMPONENT if namespace == "relay" else ROUTER_COMPONENT
            patch = yaml.safe_load((directory / "deployment.yaml").read_text())
            self.assertEqual(patch["metadata"]["namespace"], namespace)
            [volume] = patch["spec"]["template"]["spec"]["volumes"]
            self.assertEqual(volume["secret"]["secretName"], es["spec"]["target"]["name"])
            subpaths = {m["subPath"] for m in patch["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]}
            self.assertEqual(subpaths, set(es["spec"]["target"]["template"]["data"]))


if __name__ == "__main__":
    unittest.main()
