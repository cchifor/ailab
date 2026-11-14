#!/usr/bin/env python3
"""The Relay Router connection record, its Router trust mapping and their OpenBao delivery.

    python3 -m unittest scripts.tests.test_relay_router_connection

docs/runbooks/relay-router-connection.md is the contract. These checks pin what the source can prove:
the fixed connection record and its deployment digest (recomputed here exactly as Relay's
deploymentDigest does), the Router trust mapping agreeing with the record, the record's files agreeing
with the Relay component's mounts, the OpenBao policies agreeing with the record and with the
ExternalSecrets, no secret material in git, and neither component being included. Nothing here
touches a cluster.
"""

from pathlib import Path
import hashlib
import json
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / "kubernetes"
CLUSTER = K8S / "apps/clusters/ai"
RELAY_COMPONENT = K8S / "components/relay-router-connection"
ROUTER_COMPONENT = K8S / "components/llm-router-relay-keys-trust"
CREDENTIALS = K8S / "apps/relay-router-connection/credentials"
PROVISION = K8S / "apps/infrastructure/security/openbao/relay-router-provision-job.yaml"
OPENBAO_TLS = K8S / "apps/infrastructure/security/openbao/tls.yaml"
RUNBOOK = ROOT / "docs/runbooks/relay-router-connection.md"

# The reviewed record (relay#277 comments 120817, 120818, 120903; generated 2026-11-14).
CONNECTION_ID = "babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8"
TENANT = "00000000-0000-4000-8000-000000000001"
DIGEST = "547353ac649b454646cad921598d8e231c5e9db41c8e99b028351969e1a261ad"
RECORD = {
    "id": CONNECTION_ID,
    "name": "ailab llm-router",
    "tenants": [TENANT],
    "origin": "https://router.chifor.me",
    "issuer": "relay.chifor.me",
    "kid": "relay-admin-2026-11",
    "signingKeyFile": "/run/relay-router/admin-key.pem",
    "reconcilerKid": "relay-reconciler-2026-11",
    "reconcilerKeyFile": "/run/relay-router/reconciler-key.pem",
    "baoOrigin": "https://openbao.openbao.svc.cluster.local:8200",
    "baoTokenFile": "/run/relay-router-escrow/token",
    "baoMount": "af",
    "baoPrefix": "relay-router/escrow",
}
PLACEHOLDER = "operator-sets-8x(I+8192)"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ROUTER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")  # relay-keys `id`, Relay `issuer`

# OpenBao documents (KV-v2 mount `af`) -> (reader policy = k8s-auth role, ServiceAccount, namespace).
READERS = {
    "relay": {"role": "af-app-relay-router", "sa": "relay-router-eso", "secret": "relay-router-files",
              "refs": {"relay-router/relay/admin-signer": {"private_key"},
                       "relay-router/relay/reconciler-signer": {"private_key"},
                       "relay-router/relay/escrow-token": {"token"}},
              "files": {"admin-key.pem", "reconciler-key.pem", "escrow-token"}},
    "llm-router": {"role": "af-app-router-relay-keys", "sa": "router-relay-keys-eso",
                   "secret": "llm-router-relay-trust",
                   "refs": {"relay-router/router/trust": {"admin_public_key", "reconciler_public_key"}},
                   "files": {"trust.json"}},
}


def docs(path):
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def one(path, kind, name=None):
    found = [d for d in docs(path) if d["kind"] == kind
             and (name is None or d["metadata"]["name"] == name)]
    if len(found) != 1:
        raise AssertionError(f"expected one {kind} {name or ''} in {path}, found {len(found)}")
    return found[0]


def record():
    entries = json.loads((RELAY_COMPONENT / "connections.json").read_text(encoding="utf-8"))
    if not isinstance(entries, list) or len(entries) != 1:
        raise AssertionError("connections.json must be a one-entry array")
    return entries[0]


def canonical_json(value):
    """Relay transport.ts canonicalJson: keys sorted at every level, JSON.stringify spacing."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def deployment_digest(d):
    """Relay service.ts deploymentDigest."""
    identity = {k: d[k] for k in ("id", "origin", "issuer", "baoOrigin", "baoMount", "baoPrefix")}
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def trust_document(public_admin="ADMIN-PEM\n", public_reconciler="RECONCILER-PEM\n"):
    """The Router ExternalSecret's trust.json template, rendered the way ESO's `toJson` does."""
    es = one(CREDENTIALS / "llm-router.yaml", "ExternalSecret")
    template = es["spec"]["target"]["template"]["data"]["trust.json"]
    rendered = (template.replace("{{ .adminPublicKey | toJson }}", json.dumps(public_admin))
                .replace("{{ .reconcilerPublicKey | toJson }}", json.dumps(public_reconciler)))
    if "{{" in rendered:
        raise AssertionError(f"unexpected template expression left in trust.json: {rendered}")
    return json.loads(rendered)


def including(directory):
    out = []
    for path in K8S.rglob("kustomization.yaml"):
        if path.parent == directory:
            continue
        k = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for entry in k.get("components", []) + k.get("resources", []):
            if (path.parent / entry).resolve() == directory.resolve():
                out.append(path)
    return out


def flux_kustomizations():
    out = {}
    for path in CLUSTER.rglob("*.yaml"):
        for d in docs(path):
            if d.get("kind") == "Kustomization" and d.get("apiVersion", "").startswith(
                    "kustomize.toolkit.fluxcd.io"):
                out[d["metadata"]["name"]] = d
    return out


def normalise(policy):
    lines = [line for line in policy.splitlines() if not re.match(r"^\s*#", line)]
    return re.sub(r"\s", "", "".join(lines))


def provision_script():
    return one(PROVISION, "ConfigMap")["data"]["provision.sh"]


class ConnectionRecord(unittest.TestCase):
    def test_record_is_the_reviewed_one_and_valid_for_relay(self):
        d = record()
        self.assertEqual(d, RECORD)
        self.assertRegex(d["id"], UUID)
        for t in d["tenants"]:
            self.assertRegex(t, UUID)
        self.assertRegex(d["issuer"], ROUTER_ID)
        for kid in (d["kid"], d["reconcilerKid"]):
            self.assertRegex(kid, ROUTER_ID)
        self.assertNotEqual(d["kid"], d["reconcilerKid"], "separate admin and reconciler signers")
        self.assertRegex(d["baoMount"], r"^[a-zA-Z0-9_-]+$")
        self.assertRegex(d["baoPrefix"], r"^[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*$")
        # Relay parses both origins to URL.origin and hashes the PARSED value: the file must already
        # hold the canonical origin (https, no path, no trailing slash) or the digest below is wrong.
        for origin in (d["origin"], d["baoOrigin"]):
            self.assertRegex(origin, r"^https://[a-z0-9.-]+(:[0-9]+)?$")
        self.assertEqual(d["origin"], "https://router.chifor.me", "llm-router.chifor.me does not resolve")

    def test_deployment_digest_is_recomputed_and_recorded(self):
        self.assertEqual(deployment_digest(record()), DIGEST)
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn(DIGEST, text)
        self.assertIn(CONNECTION_ID, text)
        comment = (RELAY_COMPONENT / "kustomization.yaml").read_text(encoding="utf-8")
        self.assertIn(DIGEST, comment)
        self.assertIn(CONNECTION_ID, comment)

    def test_openbao_origin_is_a_name_on_the_openbao_certificate(self):
        host = record()["baoOrigin"].split("://", 1)[1].split(":", 1)[0]
        cert = one(OPENBAO_TLS, "Certificate")
        self.assertIn(host, cert["spec"]["dnsNames"])


class RelayComponent(unittest.TestCase):
    def setUp(self):
        self.k = yaml.safe_load((RELAY_COMPONENT / "kustomization.yaml").read_text(encoding="utf-8"))
        self.patch = yaml.safe_load((RELAY_COMPONENT / "deployment.yaml").read_text(encoding="utf-8"))
        self.spec = self.patch["spec"]["template"]["spec"]
        [self.c] = self.spec["containers"]
        self.mounts = {m["mountPath"]: m for m in self.c["volumeMounts"]}
        self.volumes = {v["name"]: v for v in self.spec["volumes"]}

    def test_component_shape(self):
        self.assertEqual(self.k["kind"], "Component")
        self.assertEqual(self.k["patches"], [{"path": "deployment.yaml"}])
        self.assertEqual(self.k["resources"], ["network-policy.yaml"])
        [gen] = self.k["configMapGenerator"]
        self.assertEqual((gen["name"], gen["namespace"], gen["files"]),
                         ("relay-router-connection", "relay", ["connections.json"]))
        self.assertNotIn("secretGenerator", self.k)
        self.assertEqual((self.patch["kind"], self.patch["metadata"]["name"],
                          self.patch["metadata"]["namespace"]), ("Deployment", "relay", "relay"))
        self.assertEqual(set(self.spec), {"containers", "volumes"})
        self.assertEqual(self.c["name"], "relay")
        self.assertEqual(set(self.c), {"name", "env", "volumeMounts"})

    def test_env_is_the_record_and_the_openbao_ca_only(self):
        env = {e["name"]: e["value"] for e in self.c["env"]}
        self.assertEqual(set(env), {"RELAY_ROUTER_CONNECTIONS_FILE", "NODE_EXTRA_CA_CERTS"})
        self.assertNotIn("RELAY_AGENT_CONTROL_PLANE", env, "the control plane is the release's decision")
        record_mount = self.mounts[env["RELAY_ROUTER_CONNECTIONS_FILE"]]
        self.assertEqual(record_mount["subPath"], "connections.json")
        self.assertEqual(self.volumes[record_mount["name"]]["configMap"]["name"], "relay-router-connection")
        ca = self.mounts[env["NODE_EXTRA_CA_CERTS"]]
        volume = self.volumes[ca["name"]]["secret"]
        self.assertEqual(volume["secretName"], "relay-router-openbao-tls")
        self.assertEqual(volume["items"], [{"key": "ca.crt", "path": "ca.crt"}], "never the leaf key")
        self.assertEqual(ca["subPath"], "ca.crt")

    def test_record_files_are_exactly_the_mounted_secret_keys(self):
        d = record()
        for field, key in (("signingKeyFile", "admin-key.pem"), ("reconcilerKeyFile", "reconciler-key.pem")):
            m = self.mounts[d[field]]
            self.assertEqual(m["subPath"], key)
            volume = self.volumes[m["name"]]["secret"]
            self.assertEqual(volume["secretName"], READERS["relay"]["secret"])
            self.assertIn({"key": key, "path": key}, volume["items"])
        # The token is a DIRECTORY mount (rotation without restart), its one item named `token`.
        directory, name = d["baoTokenFile"].rsplit("/", 1)
        m = self.mounts[directory]
        self.assertNotIn("subPath", m)
        volume = self.volumes[m["name"]]["secret"]
        self.assertEqual(volume["secretName"], READERS["relay"]["secret"])
        self.assertEqual(volume["items"], [{"key": "escrow-token", "path": name}])
        for m in self.mounts.values():
            self.assertIs(m["readOnly"], True)
            self.assertTrue(m["mountPath"].startswith("/run/"), "never /var/run (a symlink)")
        for v in self.volumes.values():
            source = v.get("secret") or v.get("configMap")
            self.assertEqual(source["defaultMode"] & 0o022, 0, "no group/world-writable files")
            self.assertNotIn("optional", source)
        self.assertEqual(len(self.mounts), 5)

    def test_egress_opens_only_openbao_8200(self):
        policy = one(RELAY_COMPONENT / "network-policy.yaml", "NetworkPolicy")
        self.assertEqual(policy["metadata"]["namespace"], "relay")
        self.assertEqual(policy["spec"]["podSelector"], {"matchLabels": {"app": "relay"}})
        self.assertEqual(policy["spec"]["policyTypes"], ["Egress"])
        [rule] = policy["spec"]["egress"]
        self.assertEqual(rule["ports"], [{"protocol": "TCP", "port": 8200}])
        [peer] = rule["to"]
        self.assertEqual(peer["namespaceSelector"], {"matchLabels": {"kubernetes.io/metadata.name": "openbao"}})
        self.assertEqual(peer["podSelector"],
                         {"matchLabels": {"app.kubernetes.io/name": "openbao", "component": "server"}})
        self.assertEqual(int(record()["baoOrigin"].rsplit(":", 1)[1]), 8200)
        # It must select the live Relay pods, or the in-cluster baoOrigin stays unreachable.
        relay = one(K8S / "apps/apps/relay/relay.yaml", "Deployment", "relay")
        pod_labels = relay["spec"]["template"]["metadata"]["labels"]
        selector = policy["spec"]["podSelector"]["matchLabels"]
        self.assertLessEqual(selector.items(), pod_labels.items(), pod_labels)


class RouterTrust(unittest.TestCase):
    def test_trust_maps_the_record_issuer_tenant_and_kids(self):
        d, trust = record(), trust_document()
        self.assertEqual(trust["version"], 1)
        [issuer] = trust["issuers"]
        self.assertEqual(set(issuer), {"issuer", "keys", "tenants"})
        self.assertEqual(issuer["issuer"], d["issuer"])
        keys = {k["kid"]: k for k in issuer["keys"]}
        self.assertEqual(set(keys), {d["kid"], d["reconcilerKid"]})
        self.assertEqual(keys[d["kid"]]["roles"], ["admin"])
        self.assertEqual(keys[d["reconcilerKid"]]["roles"], ["reconciler"])
        self.assertEqual(keys[d["kid"]]["publicKey"], "ADMIN-PEM\n")
        self.assertEqual(keys[d["reconcilerKid"]]["publicKey"], "RECONCILER-PEM\n")
        for k in keys.values():
            self.assertEqual(set(k), {"kid", "publicKey", "roles"})
        self.assertEqual([t["tenant"] for t in issuer["tenants"]], d["tenants"])

    def test_tenant_ceiling(self):
        [ceiling] = trust_document()["issuers"][0]["tenants"]
        self.assertEqual(set(ceiling), {"tenant", "namespace", "routes", "access", "maxLifetimeSeconds",
                                        "maxKeys", "workerBudgets"})
        self.assertEqual(ceiling["namespace"], "default")
        # Selection {kind: model, model: gpt-6-luna}, no route: the requested model IS the route alias.
        self.assertEqual(ceiling["routes"], ["gpt-6-luna"])
        self.assertEqual(ceiling["access"], ["api"], "the worker budget refuses subscription transports")
        self.assertTrue(60 <= ceiling["maxLifetimeSeconds"] <= 366 * 86400)
        self.assertTrue(1 <= ceiling["maxKeys"] <= 1000)
        budgets = ceiling["workerBudgets"]
        self.assertEqual(set(budgets), {"maxTokens", "maxAuthoritySeconds", "maxRequests"})
        self.assertEqual(budgets["maxAuthoritySeconds"], 120)  # relay#277 comment 120818 D
        self.assertEqual(budgets["maxRequests"], 8)
        tokens = budgets["maxTokens"]
        if tokens != PLACEHOLDER:
            self.assertIsInstance(tokens, int)
            # 8 x (I + 8192), with I the provider-enforced input bound, at most the 1,000,000 cap.
            self.assertEqual(tokens % 8, 0)
            self.assertTrue(8 * (1 + 8192) <= tokens <= 1_000_000, tokens)

    def test_component_shape(self):
        k = yaml.safe_load((ROUTER_COMPONENT / "kustomization.yaml").read_text(encoding="utf-8"))
        self.assertEqual(k["kind"], "Component")
        self.assertEqual(k["patches"], [{"path": "deployment.yaml"}])
        for key in ("resources", "components", "configMapGenerator", "secretGenerator"):
            self.assertNotIn(key, k)
        patch = yaml.safe_load((ROUTER_COMPONENT / "deployment.yaml").read_text(encoding="utf-8"))
        self.assertEqual((patch["kind"], patch["metadata"]["name"], patch["metadata"]["namespace"]),
                         ("Deployment", "llm-router", "llm-router"))
        spec = patch["spec"]["template"]["spec"]
        self.assertEqual(set(spec), {"containers", "volumes"})
        [c] = spec["containers"]
        self.assertEqual(c["name"], "router")
        self.assertEqual(set(c), {"name", "env", "volumeMounts"})
        env = {e["name"]: e["value"] for e in c["env"]}
        self.assertEqual(set(env), {"RELAY_KEYS_TRUST_FILE"}, "never ROUTER_PLUGIN_CONFIG")
        [m] = c["volumeMounts"]
        self.assertEqual((m["mountPath"], m["subPath"], m["readOnly"]), (env["RELAY_KEYS_TRUST_FILE"], "trust.json", True))
        self.assertTrue(m["mountPath"].startswith("/run/"))
        [v] = spec["volumes"]
        self.assertEqual((v["name"], v["secret"]["secretName"]), (m["name"], READERS["llm-router"]["secret"]))
        self.assertEqual(v["secret"]["defaultMode"] & 0o022, 0)
        self.assertNotIn("optional", v["secret"])


class CredentialDelivery(unittest.TestCase):
    def test_flux_kustomization_never_gates_a_layer(self):
        ks = flux_kustomizations()["relay-router-credentials"]["spec"]
        self.assertEqual(ks["path"], "./kubernetes/apps/relay-router-connection/credentials")
        self.assertIs(ks["wait"], False)
        self.assertIs(ks["prune"], True)
        self.assertNotIn("decryption", ks, "nothing in this tree is a secret")
        deps = {d["name"] for d in ks["dependsOn"]}
        self.assertTrue({"external-secrets", "cert-manager-config"} <= deps, deps)
        self.assertTrue(deps <= set(flux_kustomizations()), deps)
        for name, k in flux_kustomizations().items():
            for dep in k["spec"].get("dependsOn", []):
                self.assertNotEqual(dep["name"], "relay-router-credentials", name)
            self.assertNotIn("components/relay-router-connection", k["spec"].get("path", ""), name)
            self.assertNotIn("components/llm-router-relay-keys-trust", k["spec"].get("path", ""), name)

    def test_no_secret_material_and_exact_documents(self):
        k = yaml.safe_load((CREDENTIALS / "kustomization.yaml").read_text(encoding="utf-8"))
        self.assertEqual(k["resources"], ["relay.yaml", "llm-router.yaml"])
        for directory in (CREDENTIALS, RELAY_COMPONENT, ROUTER_COMPONENT):
            for path in directory.iterdir():
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("PRIVATE KEY-----", text, path)
                self.assertNotIn("PUBLIC KEY-----", text, path)
                self.assertNotRegex(text, r"\b(hvs|s)\.[A-Za-z0-9]{20,}", path)
                if path.suffix == ".yaml":
                    self.assertNotIn("Secret", [d["kind"] for d in docs(path)], path)
        for namespace, want in READERS.items():
            path = CREDENTIALS / f"{namespace}.yaml"
            sa = one(path, "ServiceAccount")
            self.assertEqual((sa["metadata"]["name"], sa["metadata"]["namespace"]), (want["sa"], namespace))
            self.assertIs(sa["automountServiceAccountToken"], False)
            store = one(path, "SecretStore")["spec"]["provider"]["vault"]
            self.assertEqual((store["path"], store["version"]), (record()["baoMount"], "v2"))
            self.assertEqual(store["auth"]["kubernetes"]["role"], want["role"])
            self.assertEqual(store["auth"]["kubernetes"]["serviceAccountRef"]["name"], want["sa"])
            es = one(path, "ExternalSecret")["spec"]
            refs = {}
            for d in es["data"]:
                refs.setdefault(d["remoteRef"]["key"], set()).add(d["remoteRef"]["property"])
            self.assertEqual(refs, want["refs"])
            target = es["target"]
            self.assertEqual(target["name"], want["secret"])
            self.assertEqual(target["template"]["mergePolicy"], "Replace")
            self.assertEqual(set(target["template"]["data"]), want["files"])
            self.assertEqual(target["template"]["metadata"]["labels"]["velero.io/exclude-from-backup"], "true")
            # No connection document may sit inside the escrow prefix Relay writes.
            for key in refs:
                self.assertFalse(key.startswith(record()["baoPrefix"] + "/"), key)


class OpenBaoProvision(unittest.TestCase):
    def setUp(self):
        self.script = provision_script()
        self.heredocs = dict(re.findall(r"bao policy write (\S+) - <<'EOF'\n(.*?)\nEOF", self.script, re.S))

    def test_job_creates_no_document_and_no_token(self):
        self.assertNotRegex(self.script, r"bao kv (put|patch|delete|destroy|undelete|get)")
        self.assertNotRegex(self.script, r"bao token create")
        self.assertNotRegex(self.script, r"bao write af/")
        job = one(PROVISION, "Job")
        self.assertEqual(job["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/force"], "enabled")
        openbao = yaml.safe_load((PROVISION.parent / "kustomization.yaml").read_text(encoding="utf-8"))
        self.assertIn(PROVISION.name, openbao["resources"])

    def test_escrow_writer_is_exactly_the_record_prefix_and_tenant(self):
        self.assertEqual(set(self.heredocs), {"relay-router-escrow", "af-app-relay-router", "af-app-router-relay-keys"})
        d = record()
        base = f"{d['baoPrefix']}/{TENANT}/+/+"
        want = (f'path "{d["baoMount"]}/data/{base}" {{ capabilities = ["create", "read", "update"] }}\n'
                f'path "{d["baoMount"]}/destroy/{base}" {{ capabilities = ["update"] }}')
        self.assertEqual(self.heredocs["relay-router-escrow"].strip(), want.strip())
        desired = re.search(r"^\s*ESCROW_DESIRED='([^']*)'", self.script, re.M).group(1)
        self.assertEqual(normalise(self.heredocs["relay-router-escrow"]), desired)
        self.assertEqual(d["tenants"], [TENANT], "a second tenant needs its own reviewed grant")

    def test_reader_policies_grant_exactly_what_the_stores_read(self):
        for label, (namespace, want) in {"RELAY": ("relay", READERS["relay"]),
                                         "ROUTER": ("llm-router", READERS["llm-router"])}.items():
            policy = self.heredocs[want["role"]]
            grants = re.findall(r'path "([^"]+)" \{ capabilities = \["read"\] \}', policy)
            self.assertEqual(len(grants), len(policy.strip().splitlines()), policy)
            self.assertEqual(set(grants), {f"af/data/{key}" for key in want["refs"]})
            desired = re.search(rf"^\s*{label}_DESIRED='([^']*)'", self.script, re.M).group(1)
            self.assertEqual(normalise(policy), desired)
            self.assertIn(f"write_role {want['role']} {want['sa']} {namespace}", self.script)

    def test_token_role_carries_only_the_escrow_policy(self):
        self.assertIn('allowed_policies="relay-router-escrow"', self.script)
        for field in ("orphan=true", "renewable=false", "token_no_default_policy=true",
                      "token_explicit_max_ttl=720h", "token_period=0", 'allowed_policies_glob=""'):
            self.assertIn(field, self.script)


class InclusionGate(unittest.TestCase):
    def test_relay_component_is_not_included(self):
        # The LAST step of the runbook order. The inclusion PR changes this test deliberately, together
        # with test_relay_v060_activation_gate.py, once the budget authority is ready=true and the
        # researcher release that reads these settings is deployed.
        self.assertEqual(including(RELAY_COMPONENT), [],
                         "the Relay connection is included last (docs/runbooks/relay-router-connection.md)")

    def test_router_component_is_not_live_with_the_token_placeholder(self):
        tokens = trust_document()["issuers"][0]["tenants"][0]["workerBudgets"]["maxTokens"]
        if including(ROUTER_COMPONENT):
            self.assertNotEqual(tokens, PLACEHOLDER, "set workerBudgets.maxTokens before including the trust")
            self.assertIsInstance(tokens, int)


if __name__ == "__main__":
    unittest.main()
