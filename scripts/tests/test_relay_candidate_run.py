#!/usr/bin/env python3
"""scripts/relay-candidate-run.py, the S-A per-run generator (owner decision O6).

The committed state must equal the generator's output for the committed run (the placeholder until the
first run), so a per-run PR whose diff is not exactly the generator's fails CI here. The fixture tests
run the generator on a temporary copy of the repository with a synthetic run over the REAL frozen
binding (no key, no signature: the generator checks consistency, Relay's composer and hub check
cryptography), and the one-time agent candidate structure the generator expects."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
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
# The agent config as step I5 imports it (ailab #1788: Relay prepare-pod-installation.ts's canonical
# 31-agent-config.json), with the one-time candidate value policy.bootstrapSecrets true.
AGENT_CONFIG = {
    "hub": {"caFile": "/run/relay/hub-ca/ca.crt", "origin": "https://relay.chifor.me"},
    "identity": {"clusterUid": "faf3a521-bd08-4d6d-ba5c-9dc7da903819", "controlNamespace": "relay-infra-control",
                 "enrollmentTokenFile": "/run/relay/enrollment/token",
                 "installationId": "b380ff7f-0716-478d-8b01-3d39f30e750b",
                 "installationKeyFile": "/run/relay/installation/installation.pem",
                 "installationPublicKey": "6D2Cma_Oa4KiaDtOnpmiiztuBhEKObNtEqFNJl-FBas",
                 "tenantId": "00000000-0000-4000-8000-000000000001"},
    "kubernetes": {"caFile": "/run/relay/kubernetes/ca.crt", "origin": "https://kubernetes.default.svc:443",
                   "tokenFile": "/run/relay/kubernetes/token"},
    "leadership": {"leaseDurationSeconds": 15, "renewDeadlineSeconds": 10, "retryPeriodSeconds": 2},
    "policy": {"allowedRuntimeDigests": ["d758093f66f080a69a28fc5f8162184f9f284be981e8f9b0f0c719290af46407"],
               "allowedTemplates": [{"digest": "abffc97e804e8069d289d58b945d9623afc9fda2400036d69d51df42fea9f195",
                                     "revision": 1, "templateId": "4da6f919-d660-472c-b503-4d8849fa60df"}],
               "bootstrapSecrets": True, "executionSlots": ["relay-researcher-1"],
               "installation": {"allowedEgressDestinations": [], "allowedHarnessAdapters": ["codex"],
                                "allowedImageDigests": [
                                    "39aed234fe31d6c819fa472dea3422f305031d5d5a0716962c3547c960cc9b6d"],
                                "allowedRegistries": ["registry.chifor.me"], "maxLeaseSeconds": 900,
                                "maxLifetimeSeconds": 28800,
                                "resourceCeiling": {"cacheGiB": 0, "maxProcesses": 512, "memoryMiB": 4096,
                                                    "milliCpu": 2000, "retainedGiB": 0, "scratchMiB": 10240}},
               "podTiers": [{"binding": "relay-gvisor", "runtimeClassName": "relay-gvisor", "tier": "sandboxed-pod"}],
               "retainedStorageClass": None},
    "version": 1,
}
# The chart's candidate mounts and volume (Relay deploy/infrastructure-agent, candidateRuntime.configMap set).
AGENT_MOUNT_LINES = "".join(
    f"        - {{name: candidate, mountPath: {m['mountPath']}, subPath: {m['subPath']}, readOnly: true}}\n"
    for m in gen.AGENT_MOUNTS)
AGENT_VOLUME = (
    "      - name: candidate\n"
    "        configMap:\n"
    "          name: relay-candidate-control-unset\n"
    "          defaultMode: 0444\n"
    "          optional: false\n"
    "          items:\n" + "".join(f"          - {{key: {k}, path: {k}}}\n" for k in gen.CONTROL_KEYS))
KEY = {name: (name * 43)[:43] for name in ("I", "R", "H", "P")}
CA = b"-----BEGIN CERTIFICATE-----\nc3ludGhldGljIGpvdXJuYWwgQ0E=\n-----END CERTIFICATE-----\n"


def canonical(value) -> str:
    return gen.canonical(value)


def candidate_tree(root: Path) -> None:
    """The repository after I5, Q4 and the one-time agent candidate PR, in a temporary copy."""
    for rel in (gen.COMPONENT, gen.AGENT, gen.INSTALLATION):
        shutil.copytree(ROOT / rel, root / rel)
    for rel in (gen.BINDING, gen.RELAY_MANIFEST):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, root / rel)
    kust = root / gen.INSTALLATION / "kustomization.yaml"
    text = kust.read_text(encoding="utf-8")
    if "- qualification-egress.yaml" not in text:
        kust.write_text(text.replace("- execution-egress.yaml", "- qualification-egress.yaml"), encoding="utf-8")
    (root / gen.AGENT / "config.json").write_text(canonical(AGENT_CONFIG) + "\n", encoding="utf-8")
    deployment = root / gen.AGENT / "deployment.yaml"
    [parsed] = [d for d in gen.load_yaml_docs(deployment) if d.get("kind") == "Deployment"]
    if gen.agent_candidate(parsed) != ([], []):
        return  # the one-time structure is committed already: keep it as it is
    text = deployment.read_text(encoding="utf-8")
    mount = "        - {name: config, mountPath: /etc/relay-agent, readOnly: true}\n"
    volume = "      - name: config\n        configMap:\n          name: relay-infrastructure-agent-config\n"
    assert text.count(mount) == 1 and text.count(volume) == 1
    deployment.write_text(text.replace(mount, mount + AGENT_MOUNT_LINES).replace(volume, AGENT_VOLUME + volume),
                          encoding="utf-8")


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

    def test_generates_exactly_the_run_bound_values_and_checks_them(self):
        c = campaign(self.root, self.now)
        before = self.snapshot()
        previous_control = control_name(self.root)
        out = self.generate(c)
        digest = gen.run_digest(c["run"])
        d16 = digest[:16]
        self.assertEqual(out["runDigest"], digest)
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
            gen.AGENT / "config.json", gen.AGENT / "deployment.yaml")))
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
        # Agent config: only candidateRuntime is new; bindingSha256 is the frozen binding.
        config = json.loads(after[str(gen.AGENT / "config.json")])
        self.assertEqual({k: v for k, v in config.items() if k != "candidateRuntime"}, AGENT_CONFIG)
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
        # Agent deployment: one line, the control ConfigMap.
        old = before[str(gen.AGENT / "deployment.yaml")].decode().splitlines()
        new = after[str(gen.AGENT / "deployment.yaml")].decode().splitlines()
        self.assertEqual([(a, b) for a, b in zip(old, new) if a != b],
                         [(f"          name: {previous_control}", f"          name: relay-candidate-control-{d16}")])
        result, diff = gen.check(self.root, digest)
        self.assertEqual(diff, [])
        self.assertEqual(result["runDigest"], digest)
        self.assertEqual(gen.check(self.root, "0" * 64)[1], [f"run digest {digest} is not the expected {'0' * 64}"])

    def test_deterministic_and_idempotent(self):
        c = campaign(self.root, self.now)
        self.generate(c)
        first = self.snapshot()
        self.generate(c)
        self.assertEqual(self.snapshot(), first)
        other = Path(self.tmp.name) / "other"
        candidate_tree(other)
        gen.generate(other, *self.files(c, "o"), now_ms=self.now)
        self.assertEqual({k: v for k, v in first.items()},
                         {str(p.relative_to(other)): p.read_bytes() for p in sorted(other.rglob("*")) if p.is_file()})

    def test_next_run_replaces_only_run_bound_values(self):
        self.generate(campaign(self.root, self.now))
        first = self.snapshot()

        def next_run(run, _document):
            run["payload"]["runId"] = "3d4e5f6a-7b8c-4d9e-8f0a-1b2c3d4e5f6a"
            run["payload"]["leaseSeconds"] = 120
        second = campaign(self.root, self.now, next_run)
        out = self.generate(second, tag="s")
        after = self.snapshot()
        self.assertEqual(sorted(k for k in after if after[k] != first[k]), sorted(out["files"]))
        a = json.loads(first[str(gen.AGENT / "config.json")])["candidateRuntime"]
        b = json.loads(after[str(gen.AGENT / "config.json")])["candidateRuntime"]
        self.assertEqual({k for k in a if a[k] != b[k]}, {"trustSha256", "trustConfigMap", "pinConfigMap"})
        self.assertEqual(gen.check(self.root, out["runDigest"])[1], [])

    def test_hand_edits_fail_the_check(self):
        """run/ is generator-owned byte for byte; in the agent tree the generator owns candidateRuntime
        and the candidate volume's ConfigMap, and everything else there stays reviewed content."""
        out = self.generate(campaign(self.root, self.now))
        d16 = out["runDigest"][:16]
        edits = {
            str(gen.RUN_DIR / "kustomization.yaml"): lambda b: b + b"# reviewed by hand\n",
            str(gen.RUN_DIR / "deployment.yaml"): lambda b: b.replace(b"relay-worker-candidate-key-", b"relay-worker-key-"),
            str(gen.RUN_DIR / "document.json"): lambda b: b[:-1] + b" }",
            str(gen.AGENT / "config.json"): lambda b: b.replace(out["trustSha256"].encode(), b"0" * 64),
            str(gen.AGENT / "deployment.yaml"): lambda b: b.replace(f"relay-candidate-control-{d16}".encode(),
                                                                   b"relay-candidate-control-previous"),
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

    def test_refuses_without_the_one_time_structure(self):
        c = campaign(self.root, self.now)
        egress = self.root / gen.INSTALLATION / "kustomization.yaml"
        original = egress.read_text()
        egress.write_text(original.replace("- qualification-egress.yaml", "- execution-egress.yaml"))
        self.assertEqual(self.refusal(c), "egress-stage-not-qualification")
        egress.write_text(original)
        config = self.root / gen.AGENT / "config.json"
        for mutate, code in [
            (lambda cfg: cfg["policy"].update(bootstrapSecrets=False), "agent-bootstrap-secrets-off"),
            (lambda cfg: cfg["identity"].update(installationPublicKey=KEY["H"]), "agent-mismatch:installationPublicKey"),
            (lambda cfg: cfg["policy"].update(executionSlots=["relay-researcher-2"]), "agent-mismatch:executionSlots"),
            (lambda cfg: cfg["policy"]["installation"].update(maxLeaseSeconds=600), "agent-mismatch:policyDigest"),
            (lambda cfg: cfg.update(podRuntime={}), "agent-mode-conflict"),
            (lambda cfg: cfg.update(candidateRuntime={**gen.AGENT_PATHS, "bindingSha256": gen.BINDING_SHA256,
                                                      "holderSecret": "another-holder"}),
             "agent-candidate-runtime-fixed-fields"),
            (lambda cfg: cfg.update(candidateRuntime={**gen.AGENT_PATHS, "bindingSha256": "0" * 64,
                                                      "holderSecret": gen.HOLDER_SECRET}),
             "agent-candidate-runtime-fixed-fields"),
        ]:
            cfg = copy.deepcopy(AGENT_CONFIG)
            mutate(cfg)
            config.write_text(canonical(cfg) + "\n")
            self.assertEqual(self.refusal(c), code)
        # The placeholder config of main before I5 is not an imported config.
        config.write_text(json.dumps(AGENT_CONFIG, indent=2) + "\n")
        self.assertEqual(self.refusal(c), "agent-config-not-imported")
        config.write_text(canonical(AGENT_CONFIG) + "\n")
        # Structural refusals, built from whatever candidate structure the fixture carries (the
        # committed one once it exists): the parsed Deployment, changed and dumped without comments.
        deployment = self.root / gen.AGENT / "deployment.yaml"
        text = deployment.read_text()
        [parsed] = [d for d in yaml.safe_load_all(text) if d]

        def dumped(mutate):
            d = copy.deepcopy(parsed)
            mutate(d["spec"]["template"]["spec"])
            return yaml.safe_dump(d, default_flow_style=False, sort_keys=False)

        def drop_mounts(spec):
            [agent] = [c for c in spec["containers"] if c["name"] == "agent"]
            agent["volumeMounts"] = [m for m in agent["volumeMounts"] if m["name"] != "candidate"]

        def configmap_first(spec):
            spec["volumes"] = [{"configMap": v["configMap"], "name": v["name"]} if v["name"] == "candidate" else v
                               for v in spec["volumes"]]

        def drop_optional(spec):
            [v] = [v for v in spec["volumes"] if v["name"] == "candidate"]
            del v["configMap"]["optional"]
        for body, code in [(dumped(drop_mounts), "agent-candidate-mounts"),
                           (dumped(drop_optional), "agent-candidate-volume"),
                           # The same volume with `configMap:` before `name:`: equal when parsed, but not
                           # the block layout whose name line is replaced.
                           (dumped(configmap_first), "agent-candidate-volume-layout"),
                           (text.replace("      - name: candidate\n", "      - name: candidate\n  - broken: [\n", 1),
                            "agent-deployment-yaml")]:
            deployment.write_text(body)
            self.assertEqual(self.refusal(c), code)
        deployment.write_text(text)
        self.generate(c)

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
        deployment = self.root / gen.AGENT / "deployment.yaml"
        original = deployment.read_text()
        deployment.write_text(original.replace("talos-agent-node-3", "talos-agent-node-2"))
        commit("R1")
        # The first run is H7's inclusion PR: run-bound values self-consistent, the rest by hand,
        # including any other change to the partly generated agent files.
        with self.assertRaises(gen.Refusal):
            gen.check_against_base(self.root, "HEAD~1", now_ms=self.now + 7_200_000)
        result, diff = gen.check_against_base(self.root, "HEAD~1", first["runDigest"])
        self.assertEqual(diff, [])
        self.assertEqual((result["firstRun"], result["reviewByHand"]),
                         (True, sorted([str(gen.AGENT / "config.json"), str(gen.AGENT / "deployment.yaml"),
                                        "kubernetes/apps/apps/relay-inclusion.txt"])))
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
        self.assertIs(gen.check_against_base(self.root, "r1", now_ms=self.now + 3_600_001)[0]
                      ["hubReadbackDeadlinePassed"], True)
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
                lambda f: f.write_text(f.read_text().replace("talos-agent-node-2", "talos-agent-node-1"))}
        self.assertEqual(gen.check(self.root, second["runDigest"])[1], [])
        amended(node, [f"not the generator output for the base: {gen.AGENT / 'deployment.yaml'}"])
        amended(node, [f"not the generator output for the base: {gen.AGENT / 'deployment.yaml'}"],
                restore_in_worktree=True)
        amended({str(gen.AGENT / "config.json"):
                 lambda f: f.write_text(f.read_text().replace("/run/relay/hub-ca/ca.crt", "/run/relay/hub-ca/x.crt"))},
                [f"not the generator output for the base: {gen.AGENT / 'config.json'}"])
        amended({"kubernetes/apps/apps/relay-inclusion.txt": lambda f: f.write_text("changed\n")},
                ["not generated: kubernetes/apps/apps/relay-inclusion.txt"])

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
        run, run_pin, document, document_pin = self.files(c)
        base = [sys.executable, str(SCRIPT), "--repo", str(self.root)]
        ok = subprocess.run(base + ["generate", "--run", run, "--run-sha256", run_pin, "--document", document,
                                    "--document-sha256", document_pin], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        summary = json.loads(ok.stdout)
        self.assertEqual(summary["runDigest"], gen.run_digest(c["run"]))
        self.assertEqual(summary["abortBeforeGrantIfHubNotReadBackBy"],
                         gen.iso(c["run"]["payload"]["issuedAt"] + 3_600_000))
        good = subprocess.run(base + ["check", "--run-digest", summary["runDigest"]], capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stdout)
        self.assertTrue(json.loads(good.stdout)["equal"])
        bad = subprocess.run(base + ["check", "--run-digest", "0" * 64], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        # The removal before the canary: run/ back to the placeholder, which then checks as such.
        reset = subprocess.run(base + ["reset"], capture_output=True, text=True)
        self.assertEqual(reset.returncode, 0, reset.stderr)
        for path, data in gen.placeholder_files().items():
            self.assertEqual((self.root / path).read_bytes(), data)
        placeholder = subprocess.run(base + ["check"], capture_output=True, text=True)
        self.assertEqual((placeholder.returncode, json.loads(placeholder.stdout)["state"]), (0, "placeholder"))
        refused = subprocess.run(base + ["generate", "--run", run, "--run-sha256", "0" * 64, "--document", document,
                                         "--document-sha256", document_pin], capture_output=True, text=True)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(refused.stderr.splitlines()[0], "Refusal: run-pin")


if __name__ == "__main__":
    unittest.main()
