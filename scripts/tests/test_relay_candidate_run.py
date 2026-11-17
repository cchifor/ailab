#!/usr/bin/env python3
"""scripts/relay-candidate-run.py, the S-A per-run generator (owner decision O6).

The committed state must equal the generator's output for the committed run (the placeholder and the
enrollment agent tree until the first run), so a per-run PR whose diff is not exactly the generator's
fails CI here. The fixture tests run the generator on a temporary copy of the repository with a
synthetic run over the REAL frozen binding (no key, no signature: the generator checks consistency,
Relay's composer and hub check cryptography). The first run writes the agent's one-time candidate
structure onto the enrollment tree; reset removes it again, byte for byte.

ChartRender compares that structure with Relay's chart (helm template of deploy/infrastructure-agent at
the pinned sourceCommit). It needs a checkout of the private Relay repository and helm, which CI has
neither of: it runs locally with RELAY_CHECKOUT=<checkout> and is skipped otherwise."""
from __future__ import annotations

import copy
import difflib
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/relay-candidate-run.py"
_spec = importlib.util.spec_from_file_location("relay_candidate_run", SCRIPT)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

RELAY = ROOT / "kubernetes/apps/apps/relay/kustomization.yaml"


def enrollment_config() -> dict:
    """agent/config.json as step I5 imports it (Relay prepare-pod-installation.ts's canonical
    31-agent-config.json; after I10 with the fence UID): main's config outside the campaign, and during it
    main's config without the campaign overlay."""
    config = json.loads((ROOT / gen.AGENT / "config.json").read_text(encoding="utf-8"))
    config["policy"]["bootstrapSecrets"] = False
    config.pop("candidateRuntime", None)
    return config


AGENT_CONFIG = enrollment_config()
KEY = {name: (name * 43)[:43] for name in ("I", "R", "H", "P")}
CA = b"-----BEGIN CERTIFICATE-----\nc3ludGhldGljIGpvdXJuYWwgQ0E=\n-----END CERTIFICATE-----\n"
NOTE = "relay-candidate-run.py one-time candidate structure (reset removes it): "


def one_time_text(control: str) -> dict:
    """The one-time structure as generate inserts it, written out here independently of the generator:
    per agent YAML file, (the line it follows, the inserted lines). It is Relay's chart
    deploy/infrastructure-agent with candidateRuntime set (ChartRender proves that against the chart)."""
    return {
        "deployment.yaml": [
            ("        - {name: config, mountPath: /etc/relay-agent, readOnly: true}\n",
             f"        # {NOTE}the run's control files.\n"
             "        - {name: candidate, mountPath: /run/relay/candidate-binding.json, subPath: binding.json, readOnly: true}\n"
             "        - {name: candidate, mountPath: /run/relay/candidate-trust.json, subPath: config.json, readOnly: true}\n"
             "        - {name: candidate, mountPath: /run/relay/candidate-ca.pem, subPath: ca.pem, readOnly: true}\n"
             "        - {name: candidate, mountPath: /run/relay/candidate-network-policy.json, subPath: network-policy.json, readOnly: true}\n"),
            ("      volumes:\n",
             f"      # {NOTE}the run's control ConfigMap.\n"
             "      - name: candidate\n"
             "        configMap:\n"
             f"          name: {control}\n"
             "          defaultMode: 0444\n"
             "          optional: false\n"
             "          items:\n"
             "          - {key: binding.json, path: binding.json}\n"
             "          - {key: config.json, path: config.json}\n"
             "          - {key: ca.pem, path: ca.pem}\n"
             "          - {key: network-policy.json, path: network-policy.json}\n"),
        ],
        "rbac.yaml": [
            ("  - kube-system\n",
             f"  # {NOTE}the namespaces the run pins.\n"
             "  - relay-infra-control\n"
             "  - relay-researcher-1\n"),
            ('  verbs: ["get", "create", "delete"]\n',
             f"# {NOTE}the slot egress policy the run\n"
             "# measures, and the bootstrap Secrets (policy.bootstrapSecrets).\n"
             '- apiGroups: ["networking.k8s.io"]\n'
             '  resources: ["networkpolicies"]\n'
             '  resourceNames: ["relay-researcher-egress"]\n'
             '  verbs: ["get"]\n'
             '- apiGroups: [""]\n'
             '  resources: ["secrets"]\n'
             '  verbs: ["get", "create", "delete"]\n'),
        ],
        "network-policy.yaml": [
            ("    - {protocol: TCP, port: 443}\n",
             f"  # {NOTE}the chart's journal\n"
             "  # rule, owner decision U9's values. Under Cilium it admits nothing: the third object below does.\n"
             "  - to:\n"
             '    - ipBlock: {cidr: "10.96.0.42/32"}\n'
             "    ports:\n"
             "    - {protocol: TCP, port: 8443}\n"),
        ],
    }


def by_key(docs) -> dict:
    return {(d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"]): d for d in docs if d}


def chart_candidate_additions(enrollment: dict, control: str) -> dict:
    """Relay's chart deploy/infrastructure-agent with candidateRuntime set, as additions to the objects of
    its enrollment-only render, written out here from the templates (deployment.yaml: the four mounts
    after the config mount and the volume first; rbac.yaml: the two namespaces' reads, the slot egress
    policy read and the bootstrap Secrets; networkpolicy.yaml: the journal rule with U9's values)."""
    out = copy.deepcopy(enrollment)
    ns, slot = "relay-infra-control", "relay-researcher-1"
    pod = out["deployment.yaml"][("Deployment", ns, "relay-infrastructure-agent")]["spec"]["template"]["spec"]
    [agent] = pod["containers"]
    assert agent["volumeMounts"][0]["name"] == "config"
    agent["volumeMounts"][1:1] = [
        {"name": "candidate", "mountPath": "/run/relay/candidate-binding.json", "subPath": "binding.json", "readOnly": True},
        {"name": "candidate", "mountPath": "/run/relay/candidate-trust.json", "subPath": "config.json", "readOnly": True},
        {"name": "candidate", "mountPath": "/run/relay/candidate-ca.pem", "subPath": "ca.pem", "readOnly": True},
        {"name": "candidate", "mountPath": "/run/relay/candidate-network-policy.json", "subPath": "network-policy.json",
         "readOnly": True}]
    pod["volumes"].insert(0, {"name": "candidate", "configMap": {
        "name": control, "defaultMode": 0o444, "optional": False,
        "items": [{"key": k, "path": k} for k in ("binding.json", "config.json", "ca.pem", "network-policy.json")]}})
    rbac = out["rbac.yaml"]
    [cluster] = rbac[("ClusterRole", None, "relay-infrastructure-agent-relay-infra-control-cluster-uid")]["rules"]
    assert cluster["resourceNames"] == ["kube-system"]
    cluster["resourceNames"] += [ns, slot]
    workloads = rbac[("Role", slot, "relay-infrastructure-agent-workloads")]["rules"]
    assert workloads == [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "create", "delete"]}]
    workloads += [{"apiGroups": ["networking.k8s.io"], "resources": ["networkpolicies"],
                   "resourceNames": ["relay-researcher-egress"], "verbs": ["get"]},
                  {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get", "create", "delete"]}]
    egress = out["network-policy.yaml"][("NetworkPolicy", ns, "relay-infrastructure-agent")]["spec"]["egress"]
    assert len(egress) == 3
    egress.append({"to": [{"ipBlock": {"cidr": "10.96.0.42/32"}}], "ports": [{"protocol": "TCP", "port": 8443}]})
    return out


def with_one_time_text(texts: dict, control: str) -> dict:
    """The agent YAML texts with one_time_text() inserted, each block after the one line it follows."""
    out = dict(texts)
    for name, edits in one_time_text(control).items():
        for line, block in edits:
            assert out[name].count(line) == 1, (name, line)
            out[name] = out[name].replace(line, line + block)
    return out


def canonical(value) -> str:
    return gen.canonical(value)


def candidate_tree(root: Path) -> None:
    """The repository after I5 and Q4, with no run and the enrollment agent tree, in a temporary copy:
    main, or during the campaign main as reset leaves it (Committed proves reset's output is main's)."""
    for rel in (gen.COMPONENT, gen.AGENT, gen.INSTALLATION):
        shutil.copytree(ROOT / rel, root / rel)
    for rel in (gen.BINDING, gen.RELAY_MANIFEST, gen.PINS):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, root / rel)
    kust = root / gen.INSTALLATION / "kustomization.yaml"
    text = kust.read_text(encoding="utf-8")
    if "- qualification-egress.yaml" not in text:
        kust.write_text(text.replace("- execution-egress.yaml", "- qualification-egress.yaml"), encoding="utf-8")
    files, _ = gen.reset_files(root)
    for path, data in files.items():
        (root / path).write_bytes(data)


def agent_texts(root: Path) -> dict:
    return {name: (root / gen.AGENT / name).read_text(encoding="utf-8") for name in gen.ONE_TIME_FILES}


def control_name(root: Path) -> str:
    [parsed] = [d for d in gen.load_yaml_docs(root / gen.AGENT / "deployment.yaml") if d.get("kind") == "Deployment"]
    return gen.agent_candidate(parsed)[1][0]["configMap"]["name"]


def campaign(root: Path, now_ms: int, patch=None) -> dict:
    """A synthetic signed-run shape over the real binding, and the hub document Relay's composer writes
    for it (canonical compact JSON)."""
    binding = json.loads((root / gen.BINDING).read_bytes())
    m = binding["runtime"]["manifest"]
    [template] = m["holderProfiles"]["workerTemplates"]
    [role] = binding["templates"]
    [installed] = gen.load_yaml_docs(root / gen.QUALIFICATION_EGRESS)
    policy = gen.measured_policy(installed)
    hub_ca = next(a["sha256"] for a in m["artifacts"] if a["role"] == "hub-ca")
    payload = {
        "runId": "0b75023f-bd9a-445b-9d67-5dd115686ad0", "owner": "Synthetic researcher run, no qualification",
        "issuedAt": now_ms, "notBefore": now_ms, "expiresAt": now_ms + 7_200_000,
        "cleanupDeadline": now_ms + 7_200_000 + 86_400_000,
        "externalEpoch": "1d2c3b4a-5f6e-4d8c-9b0a-112233445566",
        "hub": {"origin": "https://relay.chifor.me", "caSha256": hub_ca, "image": gen.hub_image(root),
                "databaseIdentity": "2e3f4a5b-6c7d-4e8f-9a0b-1c2d3e4f5a6b", "tenantId": gen.TENANT_ID},
        "router": {"origin": "https://router.chifor.me", "caSha256": template["definition"]["holder"]["router"]["caSha256"],
                   "image": "registry.chifor.me/llm-router@sha256:" + "2" * 64,
                   "databaseIdentity": "3f4a5b6c-7d8e-4f9a-8b1c-2d3e4f5a6b7c", "tokenPolicyDigests": ["3" * 64]},
        "ledger": {"origin": gen.JOURNAL_ORIGIN, "caSha256": hashlib.sha256(CA).hexdigest(),
                   "replyPublicKey": KEY["R"],
                   "clientPublicKeys": {"agent": AGENT_CONFIG["identity"]["installationPublicKey"], "holder": KEY["H"]},
                   "databaseIdentity": "4a5b6c7d-8e9f-4a0b-9c2d-3e4f5a6b7c8d",
                   "gateId": "5b6c7d8e-9f0a-4b1c-8d3e-4f5a6b7c8d9e", "witnessSha256": "5" * 64},
        "clusterUid": AGENT_CONFIG["identity"]["clusterUid"], "installationId": binding["installationId"],
        "installationPublicKeySha256": "6" * 64,
        "namespaces": [{"name": "relay-infra-control", "uid": "6c7d8e9f-0a1b-4c2d-9e4f-5a6b7c8d9e0f", "purpose": "control"},
                       {"name": "relay-researcher-1", "uid": "7d8e9f0a-1b2c-4d3e-8f5a-6b7c8d9e0f1a",
                        "purpose": "execution"}],
        "sourceCommit": m["sourceCommit"], "runtimeDigest": AGENT_CONFIG["policy"]["allowedRuntimeDigests"][0],
        "image": binding["runtime"]["image"], "profileTemplateDigests": ["8" * 64],
        "roleTemplateDigests": [role["digest"]], "installationPolicyDigest": binding["policyDigest"],
        "placementDigest": "9" * 64, "sessionProtocol": 1, "allowedScenarios": ["revocation-stops-worker"],
        "workers": [{"workerId": "8e9f0a1b-2c3d-4e4f-9a6b-7c8d9e0f1a2b", "grantId": "9f0a1b2c-3d4e-4f5a-8b7c-8d9e0f1a2b3c",
                     "launches": [{"environmentId": "0a1b2c3d-4e5f-4a6b-9c8d-9e0f1a2b3c4d", "generation": 1,
                                   "operationId": "1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e",
                                   "sessionId": "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f",
                                   "commandSha256": "a" * 64, "promptSha256": "b" * 64}]}],
        "maxActiveWorkers": 1, "maxGenerations": 1, "maxLifetimeSeconds": 900, "leaseSeconds": 90,
        "resources": {"cpuMillis": 2000, "memoryMiB": 4096, "diskMiB": 10240, "processes": 512},
        "egressPolicySha256": hashlib.sha256(canonical(policy).encode()).hexdigest(),
        "providerTokenCap": 1000000, "providerRequestCap": 8, "sessionPermitPublicKey": KEY["P"],
    }
    run = {"version": 1, "kind": "relay.candidate-run", "issuerKeyId": "campaign-issuer", "payload": payload,
           "signature": "A" * 86}
    pool = {"id": "0197f2ed-f131-4ee9-9373-29e4efa2a756", "revision": 1, "name": "relay-researcher",
            "provider": "kubernetes",
            "target": {"provider": "kubernetes", "installationId": binding["installationId"],
                       "controlNamespace": "relay-infra-control", "executionSlots": ["relay-researcher-1"],
                       "tiers": ["sandboxed-pod"], "policyDigest": binding["policyDigest"]},
            "installationPolicy": AGENT_CONFIG["policy"]["installation"],
            "tierBindings": [{"tier": "sandboxed-pod", "binding": "relay-gvisor"}],
            "runtimeDigest": payload["runtimeDigest"], "templates": binding["templates"], "repositories": [],
            "capacity": {"milliCpu": 2000, "memoryMiB": 4096, "scratchMiB": 10240, "retainedGiB": 0, "cacheGiB": 0,
                         "instances": 1},
            "qualification": "qualified-release",
            "inferencePolicies": [{"name": "relay-researcher",
                                   "connectionId": template["definition"]["holder"]["router"]["connectionId"],
                                   "grant": {"routes": ["approved"], "access": ["api"], "lifetimeSeconds": 900}}]}
    document = {"version": 1, "issuerKeys": [{"id": "campaign-issuer", "publicKey": KEY["I"]}],
                "destination": copy.deepcopy({
                    "hub": payload["hub"], "router": payload["router"],
                    "journal": {k: payload["ledger"][k] for k in ("origin", "caSha256", "replyPublicKey")}}),
                "pool": pool, "binding": binding, "egressPolicy": policy,
                "profiles": [{"templateDigest": role["digest"], "modelPolicy": "relay-researcher", "template": template}]}
    if patch:
        patch(run, document)
    run_text = canonical(run)
    document.setdefault("run", run_text)
    return {"run": run, "runText": run_text, "document": canonical(document).encode("utf-8")}


class Committed(unittest.TestCase):
    def test_committed_files_equal_the_generator_output(self):
        result, diff = gen.check(ROOT)
        self.assertEqual(diff, [], result)
        # No run: no one-time structure either. A run: exactly the structure its generation wrote.
        self.assertEqual(result["oneTimeStructure"], "absent" if gen.is_placeholder(ROOT) else "present")

    def test_the_agent_tree_is_the_enrollment_tree_or_the_runs(self):
        """Without a run, reset changes nothing on main, byte for byte; with one, generating that run onto
        reset's output reproduces main's agent files byte for byte (the first run's addition)."""
        reset, state = gen.reset_files(ROOT)
        if gen.is_placeholder(ROOT):
            self.assertEqual(state, "absent")
            self.assertEqual({str(p): (ROOT / p).read_bytes() for p in reset}, {str(p): d for p, d in reset.items()})
            return
        self.assertEqual(state, "removed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate_tree(root)
            for path in (gen.RUN_DIR / name for name in ("document.json", "kustomization.yaml", "deployment.yaml")):
                shutil.copy2(ROOT / path, root / path)
            files, one_time = gen.generated_files(root, gen.committed_run(root))
            self.assertEqual(one_time, "added")
            self.assertEqual({str(p): d for p, d in files.items()}, {str(p): (ROOT / p).read_bytes() for p in files})

    def test_placeholder_is_never_included(self):
        included = "../../../components/relay-worker-candidate" in (yaml.safe_load(RELAY.read_text()).get("components") or [])
        if gen.is_placeholder(ROOT):
            self.assertFalse(included, "the candidate component is included while run/ is the placeholder")

    def test_pinned_constants(self):
        summary = json.loads((ROOT / "docs/relay-workers/researcher-i4/summary.json").read_text())
        self.assertEqual(summary["files"]["binding.json"], gen.BINDING_SHA256)
        self.assertEqual(hashlib.sha256((ROOT / gen.BINDING).read_bytes()).hexdigest(), gen.BINDING_SHA256)
        render = importlib.util.spec_from_file_location(
            "journal_render", ROOT / "kubernetes/optional/relay-candidate-journal/render.py")
        journal = importlib.util.module_from_spec(render)
        render.loader.exec_module(journal)
        self.assertEqual(gen.JOURNAL_ORIGIN, journal.ORIGIN)
        # Owner decision U9 (ailab #1818): the chart's journal rule is the journal Service on its port.
        self.assertEqual((gen.JOURNAL_CIDRS, gen.JOURNAL_PORT), (("10.96.0.42/32",), 8443))
        self.assertEqual(gen.JOURNAL_CIDRS, (f"{journal.CLUSTER_IP}/32",))
        self.assertEqual(gen.JOURNAL_ORIGIN.rsplit(":", 1)[1], str(gen.JOURNAL_PORT))
        self.assertFalse(gen.HOLDER_SECRET.startswith("relay-bootstrap-"))
        for name in gen.names("f" * 64).values():
            self.assertRegex(name, r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
        # The recorded qualification-stage digest (installation runbook, Q4).
        [policy] = gen.load_yaml_docs(ROOT / gen.QUALIFICATION_EGRESS)
        self.assertEqual(hashlib.sha256(canonical(gen.measured_policy(policy)).encode()).hexdigest(),
                         "f6d3187c2fa3843ef54ed5d6026093c00a8fe487ab54263b7ce6db140d51282f")
        self.assertEqual(gen.local_policy_digest(AGENT_CONFIG["policy"]["installation"]),
                         json.loads((ROOT / gen.BINDING).read_bytes())["policyDigest"])


class Generator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        candidate_tree(self.root)
        self.now = int(time.time() * 1000)

    def tearDown(self):
        self.tmp.cleanup()

    def files(self, c: dict, tag: str = "r") -> tuple[str, str, str, str]:
        run = Path(self.tmp.name) / f"{tag}-run.json"
        document = Path(self.tmp.name) / f"{tag}-document.json"
        run.write_text(c["runText"], encoding="utf-8")
        document.write_bytes(c["document"])
        return (str(run), hashlib.sha256(c["runText"].encode()).hexdigest(),
                str(document), hashlib.sha256(c["document"]).hexdigest())

    def generate(self, c: dict, **kwargs):
        return gen.generate(self.root, *self.files(c, kwargs.pop("tag", "r")), now_ms=self.now, **kwargs)

    def snapshot(self) -> dict:
        return {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}

    def refusal(self, c: dict, **kwargs) -> str:
        before = self.snapshot()
        with self.assertRaises(gen.Refusal) as caught:
            self.generate(c, **kwargs)
        self.assertEqual(self.snapshot(), before, "a refusal wrote a file")
        return caught.exception.code

    def test_first_run_writes_the_run_values_and_the_one_time_structure(self):
        """R0 (and R1 again after R0's reset) on the enrollment tree: the five run-bound files, and the
        one-time structure in the four agent files, exactly as one_time_text() writes it out."""
        c = campaign(self.root, self.now)
        before = self.snapshot()
        agent_before = agent_texts(self.root)
        out = self.generate(c)
        digest = gen.run_digest(c["run"])
        d16 = digest[:16]
        self.assertEqual(out["runDigest"], digest)
        self.assertEqual(out["oneTimeStructure"], "added")
        self.assertEqual(out["names"], {
            "hubConfigMap": f"relay-worker-candidate-{d16}", "hubSessionKeySecret": f"relay-worker-candidate-key-{d16}",
            "controlConfigMap": f"relay-candidate-control-{d16}", "trustConfigMap": f"relay-candidate-trust-{d16}",
            "pinConfigMap": f"relay-candidate-pin-{d16}"})
        self.assertEqual(out["document"]["sha256"], hashlib.sha256(c["document"]).hexdigest())
        self.assertGreater(out["document"]["bytes"], 50_000)
        self.assertLessEqual(out["document"]["bytes"], 65_536)
        after = self.snapshot()
        changed = sorted(k for k in after if after[k] != before.get(k))
        self.assertEqual(changed, sorted(out["files"]))
        self.assertEqual(changed, sorted(str(p) for p in (
            gen.RUN_DIR / "document.json", gen.RUN_DIR / "kustomization.yaml", gen.RUN_DIR / "deployment.yaml",
            *(gen.AGENT / name for name in gen.ONE_TIME_FILES))))
        self.assertEqual(after[str(gen.RUN_DIR / "document.json")], c["document"])
        run_patch = yaml.safe_load(after[str(gen.RUN_DIR / "deployment.yaml")])
        spec = run_patch["spec"]["template"]["spec"]
        self.assertEqual(spec["containers"][0]["env"],
                         [{"name": "RELAY_WORKER_CANDIDATE_SHA256", "value": hashlib.sha256(c["document"]).hexdigest()}])
        self.assertEqual(spec["volumes"], [
            {"name": "worker-candidate-document", "configMap": {"name": f"relay-worker-candidate-{d16}"}},
            {"name": "worker-candidate-key-source", "secret": {"secretName": f"relay-worker-candidate-key-{d16}"}}])
        kust = yaml.safe_load(after[str(gen.RUN_DIR / "kustomization.yaml")])
        self.assertEqual(kust["configMapGenerator"][0]["name"], f"relay-worker-candidate-{d16}")
        # Agent config: the I5 import with bootstrapSecrets true and the run's candidateRuntime, nothing else.
        config = json.loads(after[str(gen.AGENT / "config.json")])
        imported = copy.deepcopy(AGENT_CONFIG)
        imported["policy"]["bootstrapSecrets"] = True
        self.assertEqual({k: v for k, v in config.items() if k != "candidateRuntime"}, imported)
        trust = (canonical({"version": 1, "issuerKeys": {"campaign-issuer": KEY["I"]},
                            "journal": {k: c["run"]["payload"]["ledger"][k] for k in ("caSha256", "origin", "replyPublicKey")},
                            "run": c["run"], "supervisorUid": 2000}) + "\n").encode()
        self.assertEqual(config["candidateRuntime"], {
            "bindingFile": "/run/relay/candidate-binding.json", "bindingSha256": gen.BINDING_SHA256,
            "trustFile": "/run/relay/candidate-trust.json", "trustSha256": hashlib.sha256(trust).hexdigest(),
            "journalCaFile": "/run/relay/candidate-ca.pem", "networkPolicyFile": "/run/relay/candidate-network-policy.json",
            "trustConfigMap": f"relay-candidate-trust-{d16}", "pinConfigMap": f"relay-candidate-pin-{d16}",
            "holderSecret": "relay-candidate-holder"})
        self.assertEqual(after[str(gen.AGENT / "config.json")].decode(), canonical(config) + "\n")
        # The agent YAML files: exactly the one-time text inserted, every other byte unchanged.
        expected = with_one_time_text(agent_before, f"relay-candidate-control-{d16}")
        for name in gen.ONE_TIME_FILES[1:]:
            self.assertEqual(after[str(gen.AGENT / name)].decode(), expected[name], name)
        # ... which parses to the enrollment objects plus exactly the chart's candidate additions.
        objects = {name: by_key(yaml.safe_load_all(after[str(gen.AGENT / name)])) for name in gen.ONE_TIME_FILES[1:]}
        enrollment = {name: by_key(yaml.safe_load_all(agent_before[name])) for name in gen.ONE_TIME_FILES[1:]}
        self.assertEqual(objects, chart_candidate_additions(enrollment, f"relay-candidate-control-{d16}"))
        result, diff = gen.check(self.root, digest)
        self.assertEqual(diff, [])
        self.assertEqual((result["runDigest"], result["oneTimeStructure"]), (digest, "present"))
        self.assertEqual(gen.check(self.root, "0" * 64)[1], [f"run digest {digest} is not the expected {'0' * 64}"])

    def test_reset_restores_the_enrollment_tree_byte_for_byte(self):
        before = self.snapshot()
        self.generate(campaign(self.root, self.now))
        files, state = gen.reset_files(self.root)
        self.assertEqual(state, "removed")
        for path, data in files.items():
            (self.root / path).write_bytes(data)
        self.assertEqual(self.snapshot(), before)
        result, diff = gen.check(self.root)
        self.assertEqual((diff, result["state"], result["oneTimeStructure"]), ([], "placeholder", "absent"))
        # Reset is idempotent: on the enrollment tree it writes run/ only, with the bytes already there.
        files, state = gen.reset_files(self.root)
        self.assertEqual((state, sorted(map(str, files))), ("absent", sorted(map(str, gen.placeholder_files()))))
        self.assertTrue(all((self.root / p).read_bytes() == d for p, d in files.items()))
        # Another runtime mode's agent tree (the ordinary activation, after the campaign) is not reset's to
        # change, as long as it carries no candidate-only object; one that does is refused.
        config = self.root / gen.AGENT / "config.json"
        ordinary = copy.deepcopy(AGENT_CONFIG)
        ordinary["policy"]["bootstrapSecrets"] = True
        ordinary["ordinaryNamespacePins"] = {"control": {"name": "relay-infra-control", "uid": "0" * 8},
                                             "execution": [{"name": "relay-researcher-1", "uid": "1" * 8}]}
        config.write_text(canonical(ordinary) + "\n")
        files, state = gen.reset_files(self.root)
        self.assertEqual((state, sorted(map(str, files))), ("other-mode", sorted(map(str, gen.placeholder_files()))))
        self.assertEqual(gen.check(self.root)[1], [])
        texts = agent_texts(self.root)
        for name in ("deployment.yaml", "network-policy.yaml"):
            path = self.root / gen.AGENT / name
            path.write_text(with_one_time_text(texts, "relay-candidate-control-" + "0" * 16)[name])
            with self.assertRaises(gen.Refusal) as caught:
                gen.reset_files(self.root)
            self.assertEqual(caught.exception.code, "agent-mode-conflict", name)
            self.assertEqual(gen.check(self.root)[1], ["agent tree: agent-mode-conflict"])
            path.write_text(texts[name])
        config.write_text(canonical({**ordinary, "candidateRuntime": {}}) + "\n")
        with self.assertRaises(gen.Refusal) as caught:
            gen.reset_files(self.root)
        self.assertEqual(caught.exception.code, "agent-mode-conflict")

    def test_deterministic_and_idempotent(self):
        c = campaign(self.root, self.now)
        self.generate(c)
        first = self.snapshot()
        self.assertEqual(self.generate(c)["oneTimeStructure"], "kept")
        self.assertEqual(self.snapshot(), first)
        other = Path(self.tmp.name) / "other"
        candidate_tree(other)
        gen.generate(other, *self.files(c, "o"), now_ms=self.now)
        self.assertEqual({k: v for k, v in first.items()},
                         {str(p.relative_to(other)): p.read_bytes() for p in sorted(other.rglob("*")) if p.is_file()})

    def test_next_run_replaces_only_run_bound_values(self):
        self.generate(campaign(self.root, self.now))
        first = self.snapshot()
        previous_control = control_name(self.root)

        def next_run(run, _document):
            run["payload"]["runId"] = "3d4e5f6a-7b8c-4d9e-8f0a-1b2c3d4e5f6a"
            run["payload"]["leaseSeconds"] = 120
        second = campaign(self.root, self.now, next_run)
        out = self.generate(second, tag="s")
        self.assertEqual(out["oneTimeStructure"], "kept")
        after = self.snapshot()
        self.assertEqual(sorted(k for k in after if after[k] != first[k]), sorted(str(p) for p in (
            gen.RUN_DIR / "document.json", gen.RUN_DIR / "kustomization.yaml", gen.RUN_DIR / "deployment.yaml",
            gen.AGENT / "config.json", gen.AGENT / "deployment.yaml")))
        a = json.loads(first[str(gen.AGENT / "config.json")])["candidateRuntime"]
        b = json.loads(after[str(gen.AGENT / "config.json")])["candidateRuntime"]
        self.assertEqual({k for k in a if a[k] != b[k]}, {"trustSha256", "trustConfigMap", "pinConfigMap"})
        # Agent deployment: one line, the control ConfigMap.
        old = first[str(gen.AGENT / "deployment.yaml")].decode().splitlines()
        new = after[str(gen.AGENT / "deployment.yaml")].decode().splitlines()
        self.assertEqual([(x, y) for x, y in zip(old, new) if x != y],
                         [(f"          name: {previous_control}",
                           f"          name: relay-candidate-control-{out['runDigest'][:16]}")])
        self.assertEqual(gen.check(self.root, out["runDigest"])[1], [])

    def test_hand_edits_fail_the_check(self):
        """run/ is generator-owned byte for byte; in the agent tree the generator owns the one-time
        structure, candidateRuntime and the candidate volume's ConfigMap, and everything else there stays
        reviewed content."""
        out = self.generate(campaign(self.root, self.now))
        d16 = out["runDigest"][:16]
        edits = {
            str(gen.RUN_DIR / "kustomization.yaml"): lambda b: b + b"# reviewed by hand\n",
            str(gen.RUN_DIR / "deployment.yaml"): lambda b: b.replace(b"relay-worker-candidate-key-", b"relay-worker-key-"),
            str(gen.RUN_DIR / "document.json"): lambda b: b[:-1] + b" }",
            str(gen.AGENT / "config.json"): lambda b: b.replace(out["trustSha256"].encode(), b"0" * 64),
            str(gen.AGENT / "deployment.yaml"): lambda b: b.replace(f"relay-candidate-control-{d16}".encode(),
                                                                   b"relay-candidate-control-" + b"0" * 16),
            str(gen.AGENT / "rbac.yaml"): lambda b: b.replace(b'  verbs: ["get", "create", "delete"]\n---',
                                                             b'  verbs: ["get", "create", "delete", "list"]\n---'),
            str(gen.AGENT / "network-policy.yaml"): lambda b: b.replace(b"port: 8443}\n---\napiVersion: cilium",
                                                                       b"port: 443}\n---\napiVersion: cilium"),
        }
        self.assertEqual(sorted(edits), sorted(out["files"]))
        for rel, edit in edits.items():
            path = self.root / rel
            original = path.read_bytes()
            path.write_bytes(edit(original))
            self.assertNotEqual(path.read_bytes(), original, rel)
            try:
                result, diff = gen.check(self.root)
                self.assertTrue(diff, rel)
            except gen.Refusal:
                pass  # an edited document is no longer the committed run's compact document: refused outright
            finally:
                path.write_bytes(original)
        self.assertEqual(gen.check(self.root)[1], [])

    def test_refuses_a_hand_edited_one_time_object(self):
        """Once written, the one-time structure is the generator's, byte for byte: any edit to one of its
        objects, or a structure in some files and not others, is refused by generate (nothing written),
        by reset (nothing written) and by check. Nothing is ever completed or repaired."""
        out = self.generate(campaign(self.root, self.now))
        control = f"relay-candidate-control-{out['runDigest'][:16]}"
        second = campaign(self.root, self.now, lambda run, _d: run["payload"].update(
            runId="3d4e5f6a-7b8c-4d9e-8f0a-1b2c3d4e5f6a"))
        D, R, N, C = (str(gen.AGENT / n) for n in ("deployment.yaml", "rbac.yaml", "network-policy.yaml", "config.json"))

        def configmap_first(text):
            [d] = [d for d in yaml.safe_load_all(text) if d]
            spec = d["spec"]["template"]["spec"]
            spec["volumes"] = [{"configMap": v["configMap"], "name": v["name"]} if v["name"] == "candidate" else v
                               for v in spec["volumes"]]
            return yaml.safe_dump(d, default_flow_style=False, sort_keys=False)

        def config_edit(change):
            def edit(text):
                cfg = json.loads(text)
                change(cfg)
                return canonical(cfg) + "\n"
            return edit
        cases = [
            (D, lambda t: t.replace("candidate-ca.pem, subPath", "candidate-ca.crt, subPath"), "agent-one-time-edited:deployment.yaml"),
            (D, lambda t: t.replace("          defaultMode: 0444\n          optional", "          defaultMode: 0440\n          optional"),
             "agent-one-time-edited:deployment.yaml"),
            (D, lambda t: t.replace("          optional: false\n", ""), "agent-one-time-edited:deployment.yaml"),
            (D, lambda t: t.replace("path: network-policy.json}\n", "path: network-policy.json}\n          - {key: x, path: x}\n"),
             "agent-one-time-edited:deployment.yaml"),
            (D, lambda t: t.replace(control, "relay-candidate-control-unset"), "agent-one-time-edited:deployment.yaml"),
            (D, lambda t: t.replace("(reset removes it): the run's control files.", "(edited by hand)."),
             "agent-one-time-edited:deployment.yaml"),
            # The same objects, parsed, in another layout (configMap before name, no comments).
            (D, configmap_first, "agent-one-time-edited:deployment.yaml"),
            (D, lambda t: t.replace("      - name: candidate\n", "      - name: candidate\n  - broken: [\n", 1),
             "agent-one-time-yaml:deployment.yaml"),
            (R, lambda t: t.replace('  verbs: ["get", "create", "delete"]\n---', '  verbs: ["get", "create", "delete", "list"]\n---'),
             "agent-one-time-edited:rbac.yaml"),
            (R, lambda t: t.replace("  - relay-researcher-1\n", ""), "agent-one-time-edited:rbac.yaml"),
            (R, lambda t: t.replace('resourceNames: ["relay-researcher-egress"]', 'resourceNames: ["relay-default-deny"]'),
             "agent-one-time-edited:rbac.yaml"),
            (N, lambda t: t.replace('{cidr: "10.96.0.42/32"}', '{cidr: "10.96.0.0/16"}'), "agent-one-time-edited:network-policy.yaml"),
            (N, lambda t: t.replace("port: 8443}\n---\napiVersion: cilium", "port: 443}\n---\napiVersion: cilium"),
             "agent-one-time-edited:network-policy.yaml"),
            (C, config_edit(lambda cfg: cfg["policy"].update(bootstrapSecrets=False)), "agent-one-time-edited:config.json"),
            (C, config_edit(lambda cfg: cfg["candidateRuntime"].update(holderSecret="another-holder")),
             "agent-candidate-runtime-fixed-fields"),
            (C, config_edit(lambda cfg: cfg["candidateRuntime"].update(bindingSha256="0" * 64)),
             "agent-candidate-runtime-fixed-fields"),
            (C, config_edit(lambda cfg: cfg["candidateRuntime"].update(extra="x")), "agent-one-time-edited:config.json"),
            (C, config_edit(lambda cfg: cfg["candidateRuntime"].update(trustConfigMap="relay-candidate-trust-" + "0" * 16)),
             "agent-one-time-edited:config.json"),
            (C, lambda t: json.dumps(json.loads(t), indent=2) + "\n", "agent-one-time-edited:config.json"),
            # Another runtime mode beside the structure: never taken as that mode's tree (reset would keep it).
            (C, config_edit(lambda cfg: cfg.update(podRuntime={})), "agent-mode-conflict"),
            (C, config_edit(lambda cfg: cfg.update(ordinaryNamespacePins={})), "agent-mode-conflict"),
            # Partial: one file without the structure while the others carry it.
            (R, lambda t: gen.remove_one_time_file(t, "rbac.yaml", control, "relay-researcher-egress"), "agent-one-time-partial"),
            (N, lambda t: gen.remove_one_time_file(t, "network-policy.yaml", control, "relay-researcher-egress"),
             "agent-one-time-partial"),
            (D, lambda t: gen.remove_one_time_file(t, "deployment.yaml", control, "relay-researcher-egress"),
             "agent-one-time-partial"),
            (C, lambda t: canonical(AGENT_CONFIG) + "\n", "agent-one-time-partial"),
        ]
        for rel, edit, code in cases:
            with self.subTest(rel=rel, code=code):
                path = self.root / rel
                original = path.read_text()
                path.write_text(edit(original))
                self.assertNotEqual(path.read_text(), original)
                try:
                    self.assertEqual(self.refusal(second, tag="s"), code)
                    before = self.snapshot()
                    with self.assertRaises(gen.Refusal) as caught:
                        gen.reset_files(self.root)
                    self.assertEqual(caught.exception.code, code)
                    self.assertEqual(self.snapshot(), before)
                    self.assertIn(f"agent tree: {code}", gen.check(self.root)[1])
                finally:
                    path.write_text(original)
        self.assertEqual(gen.check(self.root)[1], [])

    def test_operator_configmaps(self):
        c = campaign(self.root, self.now)
        ca = Path(self.tmp.name) / "journal-ca.pem"
        ca.write_bytes(CA)
        out_dir = Path(self.tmp.name) / "operator"
        out = self.generate(c, journal_ca=str(ca), operator_out=str(out_dir))
        objects = {name: json.loads((out_dir / name).read_text()) for name in sorted(out["operatorFiles"])}
        self.assertEqual(sorted(objects), ["control-configmap.json", "slot-pin-configmap.json", "slot-trust-configmap.json"])
        control, pin, trust = (objects[n] for n in sorted(objects))
        self.assertEqual((control["metadata"]["name"], control["metadata"]["namespace"]),
                         (out["names"]["controlConfigMap"], "relay-infra-control"))
        self.assertEqual((trust["metadata"]["name"], trust["metadata"]["namespace"]),
                         (out["names"]["trustConfigMap"], "relay-researcher-1"))
        self.assertEqual((pin["metadata"]["name"], pin["metadata"]["namespace"]),
                         (out["names"]["pinConfigMap"], "relay-researcher-1"))
        self.assertTrue(control["immutable"] and pin["immutable"] and trust["immutable"])
        self.assertEqual(sorted(control["data"]), sorted(gen.CONTROL_KEYS))
        self.assertEqual(control["data"]["binding.json"].encode(), (self.root / gen.BINDING).read_bytes())
        self.assertEqual(control["data"]["ca.pem"].encode(), CA)
        self.assertEqual(control["data"]["config.json"], trust["data"]["config.json"])
        self.assertEqual(hashlib.sha256(trust["data"]["config.json"].encode()).hexdigest(), out["trustSha256"])
        self.assertEqual(pin["data"], {"sha256": out["trustSha256"]})  # no newline: the supervisor compares exactly
        policy = json.loads(control["data"]["network-policy.json"])
        self.assertEqual(hashlib.sha256(canonical(gen.measured_policy(policy)).encode()).hexdigest(),
                         c["run"]["payload"]["egressPolicySha256"])
        # A new directory only, and the CA the run pins.
        self.assertEqual(self.refusal(c, journal_ca=str(ca), operator_out=str(out_dir)), "operator-out-exists")
        ca.write_bytes(CA + b"\n")
        self.assertEqual(self.refusal(c, journal_ca=str(ca), operator_out=str(Path(self.tmp.name) / "o2")),
                         "journal-ca-pin")
        self.assertEqual(self.refusal(c, journal_ca=str(ca)), "usage")

    def test_refuses_inconsistent_inputs(self):
        def case(patch):
            return campaign(self.root, self.now, patch)

        def set_run(run, path, value):
            target = run["payload"]
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value

        c = campaign(self.root, self.now)
        pretty = dict(c, runText=json.dumps(c["run"], indent=1))
        pretty["document"] = canonical({**json.loads(c["document"]), "run": pretty["runText"]}).encode()
        self.assertEqual(self.refusal(pretty), "run-not-canonical")
        self.assertEqual(self.refusal(dict(c, document=b"{ " + c["document"][1:])), "document-not-compact")
        self.assertEqual(self.refusal(case(lambda r, d: d.update(run=canonical({**r, "signature": "B" * 86})))),
                         "document-run-mismatch")
        big = json.loads(c["document"])
        big["pool"]["name"] = "x" * 9000
        self.assertEqual(self.refusal(dict(c, document=canonical(big).encode())), "document-too-large")
        self.assertEqual(self.refusal(case(lambda r, d: d["issuerKeys"][0].update(id="other"))), "document-issuer-keys")
        self.assertEqual(self.refusal(case(lambda r, d: d["destination"]["router"].update(image="x"))),
                         "document-destination")
        self.assertEqual(self.refusal(case(lambda r, d: d["binding"].update(version=2))), "document-binding")
        def other_hub(run, document):  # a run signed for another hub image than relay.yaml deploys
            set_run(run, ["hub", "image"], "registry.chifor.me/relay/control-plane@sha256:" + "1" * 64)
            document["destination"]["hub"] = copy.deepcopy(run["payload"]["hub"])
        self.assertEqual(self.refusal(case(other_hub)), "run-mismatch:hubImage")
        self.assertEqual(self.refusal(case(lambda r, d: r["payload"]["namespaces"][1].update(name="relay-researcher-2"))),
                         "run-mismatch:namespaces")
        self.assertEqual(self.refusal(case(lambda r, d: set_run(r, ["egressPolicySha256"], "f" * 64))), "run-egress-policy")
        self.assertEqual(self.refusal(case(lambda r, d: d["egressPolicy"]["spec"].update(policyTypes=["Ingress", "Egress"]))),
                         "document-egress-policy")
        self.assertEqual(self.refusal(case(lambda r, d: d["pool"]["target"].update(executionSlots=["relay-researcher-2"]))),
                         "document-pool")
        # Time: a run outside its window is never generated (the hub would refuse it at startup).
        self.now += 7_200_000
        self.assertEqual(self.refusal(c), "run-expired")
        self.now -= 7_200_001 + 7_200_000
        self.assertEqual(self.refusal(c), "run-not-yet-valid")
        self.now += 7_200_001
        # Wrong pins.
        run, run_pin, document, document_pin = self.files(c)
        with self.assertRaises(gen.Refusal) as caught:
            gen.generate(self.root, run, "0" * 64, document, document_pin, now_ms=self.now)
        self.assertEqual(caught.exception.code, "run-pin")

    def test_refuses_an_agent_tree_it_cannot_take(self):
        """On the enrollment tree: the qualification stage, an I5-imported config that agrees with the run,
        and no other runtime mode; a structure in only some files, or not exactly the generator's, is
        never completed."""
        c = campaign(self.root, self.now)
        egress = self.root / gen.INSTALLATION / "kustomization.yaml"
        original = egress.read_text()
        egress.write_text(original.replace("- qualification-egress.yaml", "- execution-egress.yaml"))
        self.assertEqual(self.refusal(c), "egress-stage-not-qualification")
        egress.write_text(original)
        config = self.root / gen.AGENT / "config.json"
        for mutate, code in [
            (lambda cfg: cfg["identity"].update(installationPublicKey=KEY["H"]), "agent-mismatch:installationPublicKey"),
            (lambda cfg: cfg["policy"].update(executionSlots=["relay-researcher-2"]), "agent-mismatch:executionSlots"),
            (lambda cfg: cfg["policy"]["installation"].update(maxLeaseSeconds=600), "agent-mismatch:policyDigest"),
            (lambda cfg: cfg.update(podRuntime={}), "agent-mode-conflict"),
            (lambda cfg: cfg.update(ordinaryNamespacePins={}), "agent-mode-conflict"),
            # The one-time config value without candidateRuntime: neither the import nor generate's output.
            (lambda cfg: cfg["policy"].update(bootstrapSecrets=True), "agent-one-time-edited:config.json"),
            (lambda cfg: cfg.update(candidateRuntime={**gen.AGENT_PATHS, "bindingSha256": gen.BINDING_SHA256,
                                                      "holderSecret": "another-holder"}),
             "agent-one-time-edited:config.json"),
            (lambda cfg: (cfg["policy"].update(bootstrapSecrets=True), cfg.update(candidateRuntime={
                **gen.AGENT_PATHS, "bindingSha256": "0" * 64, "holderSecret": gen.HOLDER_SECRET})),
             "agent-candidate-runtime-fixed-fields"),
        ]:
            cfg = copy.deepcopy(AGENT_CONFIG)
            mutate(cfg)
            config.write_text(canonical(cfg) + "\n")
            self.assertEqual(self.refusal(c), code)
        # A config that is not the I5 import (as main's placeholder was before I5).
        config.write_text(json.dumps(AGENT_CONFIG, indent=2) + "\n")
        self.assertEqual(self.refusal(c), "agent-config-not-imported")
        config.write_text(canonical(AGENT_CONFIG) + "\n")
        # Part of the structure, written by hand before the run: never completed.
        texts = agent_texts(self.root)
        written = with_one_time_text(texts, "relay-candidate-control-" + "0" * 16)
        for name, code in [("deployment.yaml", "agent-one-time-partial"), ("rbac.yaml", "agent-one-time-partial"),
                           ("network-policy.yaml", "agent-one-time-partial")]:
            (self.root / gen.AGENT / name).write_text(written[name])
            self.assertEqual(self.refusal(c), code, name)
            (self.root / gen.AGENT / name).write_text(texts[name])
        # Another layout of the enrollment file the structure goes into: refused, not guessed.
        deployment = self.root / gen.AGENT / "deployment.yaml"
        deployment.write_text(texts["deployment.yaml"].replace(
            "        - {name: config, mountPath: /etc/relay-agent, readOnly: true}\n",
            "        - name: config\n          mountPath: /etc/relay-agent\n          readOnly: true\n"))
        self.assertEqual(self.refusal(c), "agent-one-time-layout:deployment.yaml")
        deployment.write_text(texts["deployment.yaml"])
        self.generate(c)

    def test_any_trace_of_the_structure_is_not_its_absence(self):
        """A one-time object anywhere in the enrollment files (not only where generate puts it), or a
        candidateRuntime key of any value, means the structure is not absent: generate, reset and check
        refuse instead of adding a second copy, repairing it or leaving it behind."""
        c = campaign(self.root, self.now)
        texts = agent_texts(self.root)
        journal = '  - to:\n    - ipBlock: {cidr: "10.96.0.42/32"}\n    ports:\n    - {protocol: TCP, port: 8443}\n'
        dns_rule = ("  - to:\n    - namespaceSelector:\n        matchLabels:\n          kubernetes.io/metadata.name: kube-system\n"
                    "      podSelector:\n        matchLabels:\n          k8s-app: kube-dns\n    ports:\n"
                    "    - {protocol: UDP, port: 53}\n    - {protocol: TCP, port: 53}\n")
        self.assertEqual(texts["network-policy.yaml"].count(dns_rule), 1)
        cases = [
            # The journal rule in place of the DNS rule: still three rules.
            ("network-policy.yaml", lambda t: t.replace(dns_rule, ""), journal, "agent-one-time-partial"),
            # A rule on the journal's port, wherever it is.
            ("network-policy.yaml", lambda t: t.replace("    - {protocol: TCP, port: 443}\n",
                                                        "    - {protocol: TCP, port: 443}\n    - {protocol: TCP, port: 8443}\n"),
             None, "agent-one-time-partial"),
            # A candidate file mounted from a volume of another name.
            ("deployment.yaml", lambda t: t.replace(
                "        - {name: kubernetes,",
                "        - {name: other, mountPath: /run/relay/candidate-trust.json, subPath: config.json, readOnly: true}\n"
                "        - {name: kubernetes,"), None, "agent-one-time-edited:deployment.yaml"),
            ("config.json", lambda t: canonical({**AGENT_CONFIG, "candidateRuntime": None}) + "\n", None,
             "agent-one-time-edited:config.json"),
            ("config.json", lambda t: canonical({**AGENT_CONFIG, "candidateRuntime": {}}) + "\n", None,
             "agent-one-time-edited:config.json"),
        ]
        for name, edit, append, code in cases:
            with self.subTest(name=name, code=code):
                path = self.root / gen.AGENT / name
                body = edit(texts[name])
                if append:
                    body = body.replace("    - {protocol: TCP, port: 443}\n", "    - {protocol: TCP, port: 443}\n" + append)
                self.assertNotEqual(body, texts[name])
                path.write_text(body)
                try:
                    self.assertEqual(self.refusal(c), code)
                    with self.assertRaises(gen.Refusal) as caught:
                        gen.reset_files(self.root)
                    self.assertEqual(caught.exception.code, code)
                    self.assertEqual(gen.check(self.root)[1], [f"agent tree: {code}"])
                    # ... and beside another runtime mode, a conflict.
                    if name != "config.json":
                        config = self.root / gen.AGENT / "config.json"
                        config.write_text(canonical({**AGENT_CONFIG, "podRuntime": {}}) + "\n")
                        with self.assertRaises(gen.Refusal) as caught:
                            gen.reset_files(self.root)
                        self.assertEqual(caught.exception.code, "agent-mode-conflict")
                        config.write_text(texts["config.json"])
                finally:
                    path.write_text(texts[name])
        self.assertEqual(gen.check(self.root)[1], [])

    def test_base_check_is_the_merge_gate(self):
        """S-A: the committed HEAD must be exactly the generator's output on the PR's base."""
        def run_git(*args):
            subprocess.run(["git", "-C", str(self.root), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                            "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)

        def commit(message):
            run_git("add", "-A")
            run_git("commit", "-q", "--allow-empty", "-m", message)

        # The fixture tree as the repository, with the placeholder run/ as its base.
        for path, data in gen.placeholder_files().items():
            (self.root / path).write_bytes(data)
        run_git("init", "-q", "-b", "main")
        commit("base")
        first = self.generate(campaign(self.root, self.now))
        inclusion = self.root / "kubernetes/apps/apps/relay-inclusion.txt"
        inclusion.parent.mkdir(parents=True, exist_ok=True)
        inclusion.write_text("- ../../../components/relay-worker-candidate\n")
        commit("R1")
        # The first run is H7's inclusion PR: the generator on the base (the enrollment agent tree) with
        # HEAD's run, the agent's one-time structure included, is what HEAD must hold; only the other
        # paths it changes (the inclusion line) are reviewed by hand.
        with self.assertRaises(gen.Refusal):
            gen.check_against_base(self.root, "HEAD~1", now_ms=self.now + 7_200_000)
        result, diff = gen.check_against_base(self.root, "HEAD~1", first["runDigest"])
        self.assertEqual(diff, [])
        self.assertEqual((result["firstRun"], result["oneTimeStructure"], result["reviewByHand"]),
                         (True, "added", ["kubernetes/apps/apps/relay-inclusion.txt"]))
        self.assertEqual(sorted(result["files"]), sorted(first["files"]))
        # HEAD's own tree must still agree with its run: a first run's PR that also leaves the
        # qualification egress stage is refused, as the generator would refuse it.
        kust = self.root / gen.INSTALLATION / "kustomization.yaml"
        original = kust.read_text()
        kust.write_text(original.replace("- qualification-egress.yaml", "- execution-egress.yaml"))
        commit("R1 with the enrollment stage")
        with self.assertRaises(gen.Refusal) as caught:
            gen.check_against_base(self.root, "HEAD~2", first["runDigest"])
        self.assertEqual(caught.exception.code, "egress-stage-not-qualification")
        run_git("reset", "-q", "--hard", "HEAD~1")
        # Any other change to the agent files fails the gate, in the structure or beside it, as does a
        # first run's PR without the structure.
        # (HEAD's self-check names an edit inside the structure too.)
        deployment, rbac = self.root / gen.AGENT / "deployment.yaml", self.root / gen.AGENT / "rbac.yaml"
        for path, change, own in [
                (deployment, lambda t: t.replace("talos-agent-node-3", "talos-agent-node-2"), []),
                (rbac, lambda t: t.replace("  - relay-researcher-1\n", "  - relay-researcher-1\n  - default\n"),
                 ["HEAD's self-check: agent tree: agent-one-time-edited:rbac.yaml"]),
                (rbac, lambda t: gen.remove_one_time_file(t, "rbac.yaml", first["names"]["controlConfigMap"],
                                                          "relay-researcher-egress"),
                 ["HEAD's self-check: agent tree: agent-one-time-partial"])]:
            original = path.read_text()
            path.write_text(change(original))
            commit("R1 edited")
            self.assertEqual(gen.check_against_base(self.root, "HEAD~2", first["runDigest"])[1],
                             [f"not the generator output for the base: {path.relative_to(self.root)}", *own])
            run_git("reset", "-q", "--hard", "HEAD~1")
        run_git("branch", "-f", "r1")

        def next_run(run, _document):
            run["payload"]["runId"] = "3d4e5f6a-7b8c-4d9e-8f0a-1b2c3d4e5f6a"
        second = self.generate(campaign(self.root, self.now, next_run), tag="s")
        # Export attributes never change what the gate reads: it reads tree objects, not git archive.
        (self.root / ".gitattributes").write_text("kubernetes/** export-ignore\ndocs/** export-ignore\n")
        commit("R2")
        result, diff = gen.check_against_base(self.root, "r1", second["runDigest"])
        self.assertEqual(diff, ["not generated: .gitattributes"])
        (self.root / ".gitattributes").unlink()
        commit("R2 without attributes")
        result, diff = gen.check_against_base(self.root, "r1", second["runDigest"], now_ms=self.now)
        self.assertEqual(diff, [])
        self.assertEqual((result["firstRun"], result["hubReadbackDeadlinePassed"]), (False, False))
        # Past t_s + 60 min the gate fails: O6 says abort before the grant, never merge.
        late, late_diff = gen.check_against_base(self.root, "r1", now_ms=self.now + 3_600_001)
        self.assertIs(late["hubReadbackDeadlinePassed"], True)
        self.assertEqual(late_diff, [gen.DEADLINE_PASSED])
        # An expired run never passes the gate; plain check stays time-free (CI after expiry).
        with self.assertRaises(gen.Refusal) as caught:
            gen.check_against_base(self.root, "r1", now_ms=self.now + 7_200_000)
        self.assertEqual(caught.exception.code, "run-expired")
        self.assertEqual(gen.check(self.root)[1], [])
        # A base that moved on since the branch was cut: merge it first.
        moved = subprocess.run(["git", "-C", str(self.root), "-c", "user.name=test",
                                "-c", "user.email=test@example.invalid", "commit-tree", "r1^{tree}", "-p", "r1",
                                "-m", "main moved"], check=True, capture_output=True, text=True).stdout.strip()
        with self.assertRaises(gen.Refusal) as caught:
            gen.check_against_base(self.root, moved, second["runDigest"])
        self.assertEqual(caught.exception.code, "base-not-in-branch")
        self.assertEqual(gen.check_against_base(self.root, "r1", first["runDigest"])[1],
                         [f"run digest {second['runDigest']} is not the expected {first['runDigest']}"])

        def amended(edit, expected_diff, restore_in_worktree=False):
            run_git("reset", "-q", "--hard", "HEAD")
            before = {p: (self.root / p).read_bytes() for p in edit}
            for p, change in edit.items():
                change(self.root / p)
            commit("R2 amended")
            if restore_in_worktree:  # an uncommitted revert never hides a committed change
                for p, data in before.items():
                    (self.root / p).write_bytes(data)
            try:
                self.assertEqual(gen.check_against_base(self.root, "r1", second["runDigest"])[1], expected_diff)
            finally:
                run_git("reset", "-q", "--hard", "HEAD~1")

        # Anything beyond the generated values fails, even an edit the self-check cannot see.
        node = {str(gen.AGENT / "deployment.yaml"):
                lambda f: f.write_text(f.read_text().replace("talos-agent-node-3", "talos-agent-node-1"))}
        self.assertEqual(gen.check(self.root, second["runDigest"])[1], [])
        amended(node, [f"not the generator output for the base: {gen.AGENT / 'deployment.yaml'}"])
        amended(node, [f"not the generator output for the base: {gen.AGENT / 'deployment.yaml'}"],
                restore_in_worktree=True)
        amended({str(gen.AGENT / "config.json"):
                 lambda f: f.write_text(f.read_text().replace("/run/relay/hub-ca/ca.crt", "/run/relay/hub-ca/x.crt"))},
                [f"not the generator output for the base: {gen.AGENT / 'config.json'}"])
        amended({"kubernetes/apps/apps/relay-inclusion.txt": lambda f: f.write_text("changed\n")},
                ["not generated: kubernetes/apps/apps/relay-inclusion.txt"])
        # The one-time structure is generated in every later run's PR too: unchanged, byte for byte.
        amended({str(gen.AGENT / "network-policy.yaml"):
                 lambda f: f.write_text(f.read_text().replace('{cidr: "10.96.0.42/32"}', '{cidr: "10.96.0.0/16"}'))},
                [f"not the generator output for the base: {gen.AGENT / 'network-policy.yaml'}"])
        amended({str(gen.AGENT / "rbac.yaml"):
                 lambda f: f.write_text(f.read_text().replace("# measures, and the", "# measures and the"))},
                [f"not the generator output for the base: {gen.AGENT / 'rbac.yaml'}"])

        # A generated path committed as a symlink to the right bytes is refused.
        def symlink(f):
            target = Path(self.tmp.name) / "outside.yaml"
            target.write_bytes(f.read_bytes())
            f.unlink()
            f.symlink_to(target)
        run_git("reset", "-q", "--hard", "HEAD")
        symlink(self.root / gen.AGENT / "deployment.yaml")
        commit("R2 symlink")
        with self.assertRaises(gen.Refusal) as caught:
            gen.check_against_base(self.root, "r1", second["runDigest"])
        self.assertEqual(caught.exception.code, "git-entry")
        with self.assertRaises(gen.Refusal):
            gen.check_against_base(self.root, "no-such-ref")

    def test_cli(self):
        c = campaign(self.root, self.now)
        before = self.snapshot()
        run, run_pin, document, document_pin = self.files(c)
        base = [sys.executable, str(SCRIPT), "--repo", str(self.root)]
        ok = subprocess.run(base + ["generate", "--run", run, "--run-sha256", run_pin, "--document", document,
                                    "--document-sha256", document_pin], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        summary = json.loads(ok.stdout)
        self.assertEqual(summary["runDigest"], gen.run_digest(c["run"]))
        self.assertEqual(summary["abortBeforeGrantIfHubNotReadBackBy"],
                         gen.iso(c["run"]["payload"]["issuedAt"] + 3_600_000))
        self.assertEqual(summary["oneTimeStructure"], "added")
        good = subprocess.run(base + ["check", "--run-digest", summary["runDigest"]], capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stdout)
        self.assertTrue(json.loads(good.stdout)["equal"])
        bad = subprocess.run(base + ["check", "--run-digest", "0" * 64], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        # Reset refuses a hand-edited structure and writes nothing.
        rbac = self.root / gen.AGENT / "rbac.yaml"
        original = rbac.read_text()
        rbac.write_text(original.replace("  - relay-researcher-1\n", ""))
        edited = self.snapshot()
        refused = subprocess.run(base + ["reset"], capture_output=True, text=True)
        self.assertEqual((refused.returncode, refused.stderr.splitlines()[0]), (2, "Refusal: agent-one-time-edited:rbac.yaml"))
        self.assertEqual(self.snapshot(), edited)
        rbac.write_text(original)
        # R0's reset, and the removal before the canary: run/ back to the placeholder and the agent tree
        # back to the enrollment tree, byte for byte, which then checks as such.
        reset = subprocess.run(base + ["reset"], capture_output=True, text=True)
        self.assertEqual(reset.returncode, 0, reset.stderr)
        self.assertEqual(json.loads(reset.stdout)["oneTimeStructure"], "removed")
        self.assertEqual(self.snapshot(), before)
        placeholder = subprocess.run(base + ["check"], capture_output=True, text=True)
        self.assertEqual((placeholder.returncode, json.loads(placeholder.stdout)["state"]), (0, "placeholder"))
        # --chart compares the working tree, never with the merge gate.
        usage = subprocess.run(base + ["check", "--base", "HEAD", "--chart", str(self.root)], capture_output=True, text=True)
        self.assertEqual((usage.returncode, usage.stderr.splitlines()[0]), (2, "Refusal: usage"))
        refused = subprocess.run(base + ["generate", "--run", run, "--run-sha256", "0" * 64, "--document", document,
                                         "--document-sha256", document_pin], capture_output=True, text=True)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(refused.stderr.splitlines()[0], "Refusal: run-pin")


class R0State(unittest.TestCase):
    """R0's per-run PR on a copy of every tracked file: the generator's synthetic first run onto main, with
    the one-time structure. The I5 checks (test_relay_researcher_installation.py,
    test_relay_researcher_reproduction.py and this file's Committed) must pass in that state as they do on
    main, through their campaign paths; then reset must give main back, byte for byte. (R0's PR also carries
    H7's inclusion, which needs H6 first: test_relay_researcher_hub_upgrade.py's order, not exercised here.)"""

    def test_the_i5_checks_pass_in_r0_and_reset_restores_main(self):
        if not gen.is_placeholder(ROOT):
            self.skipTest("a run is committed on main: R0's state is behind us (Committed checks the run's)")
        listing = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True)
        if listing.returncode:
            self.skipTest("not a git checkout")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            main = {}
            for name in filter(None, listing.stdout.decode().split("\0")):
                source = ROOT / name
                if not source.is_file() or source.is_symlink():
                    continue
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, root / name)
                main[name] = source.read_bytes()
            now = int(time.time() * 1000)
            c = campaign(root, now)
            run, document = Path(tmp) / "run.json", Path(tmp) / "document.json"
            run.write_text(c["runText"], encoding="utf-8")
            document.write_bytes(c["document"])
            out = gen.generate(root, str(run), hashlib.sha256(c["runText"].encode()).hexdigest(), str(document),
                               hashlib.sha256(c["document"]).hexdigest(), now_ms=now)
            self.assertEqual(out["oneTimeStructure"], "added")
            # The copy's I5 tooling sees the campaign (so the suites below take their campaign paths).
            spec = importlib.util.spec_from_file_location("r0_bundle", root / "scripts/relay-researcher-bundle.py")
            bundle = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bundle)
            self.assertEqual(bundle.campaign_run()["runDigest"], out["runDigest"])
            done = subprocess.run([sys.executable, "-B", "-m", "unittest", "test_relay_researcher_installation",
                                   "test_relay_researcher_reproduction", "test_relay_candidate_run.Committed"],
                                  cwd=root / "scripts/tests", capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr[-6000:])
            self.assertRegex(done.stderr, r"\nOK")
            files, state = gen.reset_files(root)
            self.assertEqual(state, "removed")
            for path, data in files.items():
                (root / path).write_bytes(data)
            self.assertEqual({name: (root / name).read_bytes() for name in main}, main)


class ChartRender(unittest.TestCase):
    """The one-time structure is Relay's chart: check --chart renders deploy/infrastructure-agent at the
    pinned sourceCommit with helm, enrollment-only and with the candidate values, and compares every object
    and the config with the agent tree without and with the structure. LOCAL ONLY: it needs a checkout of
    the private Relay repository (RELAY_CHECKOUT) and helm, which CI has neither of."""

    def setUp(self):
        relay = os.environ.get("RELAY_CHECKOUT")
        if not relay or not shutil.which("helm"):
            self.skipTest("RELAY_CHECKOUT (a Relay checkout at pins.yaml's sourceCommit) and helm are local only")
        self.relay = Path(relay).resolve()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        candidate_tree(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_both_renders_equal_the_agent_tree(self):
        # Before any run: the enrollment tree, and what generate would add (stand-in names).
        result, diff = gen.chart_check(self.root, self.relay)
        self.assertEqual(diff, [])
        self.assertEqual((result["oneTimeStructure"], result["candidateSide"]), ("absent", "generated"))
        # R0 committed: the tree with the structure and the run's values, and reset's tree.
        now = int(time.time() * 1000)
        c = campaign(self.root, now)
        run, document = Path(self.tmp.name) / "run.json", Path(self.tmp.name) / "document.json"
        run.write_text(c["runText"], encoding="utf-8")
        document.write_bytes(c["document"])
        out = gen.generate(self.root, str(run), hashlib.sha256(c["runText"].encode()).hexdigest(), str(document),
                           hashlib.sha256(c["document"]).hexdigest(), now_ms=now)
        result, diff = gen.chart_check(self.root, self.relay)
        self.assertEqual(diff, [])
        self.assertEqual((result["oneTimeStructure"], result["candidateSide"], result["controlConfigMap"]),
                         ("present", "committed", out["names"]["controlConfigMap"]))
        cli = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.root), "check", "--run-digest",
                              out["runDigest"], "--chart", str(self.relay)], capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)

    def test_a_structure_the_chart_does_not_render_fails(self):
        """A generator whose blocks and objects agree with each other but not with the chart (one mount
        path) passes its own checks and fails only here."""
        mounts = copy.deepcopy(gen.AGENT_MOUNTS)
        try:
            gen.AGENT_MOUNTS[2]["mountPath"] = "/run/relay/candidate-ca.crt"
            result, diff = gen.chart_check(self.root, self.relay)
        finally:
            gen.AGENT_MOUNTS[:] = mounts
        self.assertEqual(diff, ["candidate: ['Deployment', 'relay-infra-control', 'relay-infrastructure-agent'] "
                                "differs from the chart render"])


if __name__ == "__main__":
    unittest.main()
