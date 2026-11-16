#!/usr/bin/env python3
"""S-A per-run generator for the Relay researcher qualification campaign (owner decision O6).

One signed candidate run needs one mechanical ailab PR (docs/runbooks/relay-candidate-run-generator.md).
This script writes exactly the run-bound values of that PR, from the signed run.json and the hub
document Relay's scripts/compose-candidate-hub-document.ts composed for it, and nothing else:

  hub   kubernetes/components/relay-worker-candidate/run/ (whole files):
          document.json        the CandidateHubDocument, byte for byte;
          kustomization.yaml   its ConfigMap relay-worker-candidate-<runDigest[:16]> (immutable);
          deployment.yaml      RELAY_WORKER_CANDIDATE_SHA256 = sha256(document.json), the ConfigMap
                               name, and the session-key Secret relay-worker-candidate-key-<runDigest[:16]>
                               (operator-created from the run's permit key, never in git);
  agent kubernetes/apps/relay-researcher/agent/config.json: candidateRuntime (trustSha256,
          trustConfigMap, pinConfigMap; bindingSha256 = the frozen I4 binding, holderSecret and the
          file paths fixed), the rest of the file unchanged;
        kubernetes/apps/relay-researcher/agent/deployment.yaml: the `candidate` volume's ConfigMap,
          relay-candidate-control-<runDigest[:16]>, the only line that changes.

The agent half needs the one-time candidate structure in the agent tree (installation runbook,
"Beyond enrollment" item 3): config.json as imported at I5 (canonical JSON) with
policy.bootstrapSecrets true, and the chart's four `candidate` mounts and `candidate` volume
(Relay deploy/infrastructure-agent/templates/deployment.yaml with candidateRuntime.configMap set), the
volume in block style with `configMap:` then `name:` on the next line. Without it the generator
refuses (`agent-...`): no candidate run can launch without those values.

  generate --run RUN.json --run-sha256 SHA --document DOCUMENT.json --document-sha256 SHA
           [--journal-ca CA.pem --operator-out NEW_DIR] [--repo DIR]
      Refuses inconsistent inputs (each refusal prints `Refusal: <code>` and exits 2), writes the files
      above and prints a JSON summary naming the run digest. With --operator-out it also writes the
      three public ConfigMaps the operator creates with kubectl (never in git): the agent's control
      ConfigMap (binding.json, config.json = the CandidateAgentTrust, ca.pem, network-policy.json) in
      relay-infra-control, and the slot's trust (config.json) and pin (sha256) ConfigMaps in
      relay-researcher-1. trustSha256 is the SHA-256 of those exact trust bytes, which are
      canonical JSON plus a final newline, as Relay's candidate policy preparation measures them.
  reset [--repo DIR]
      Writes the placeholder run/ back (the component's removal PR before the canary). The agent's
      candidate values leave with the agent's own change to the canary runtime.
  check [--run-digest DIGEST] [--base GIT_REF] [--repo DIR]
      Regenerates every generated value from the committed run/document.json (it carries the signed
      run) and compares it with the committed files: exit 0 when they are equal, 1 with the differing
      paths otherwise. With --run-digest, the committed run must be that run. The placeholder state
      (no run yet) must equal the generator's placeholder exactly. Without --base this is
      self-consistency (what CI runs): the agent files' other content is taken as committed.
      With --base (the PR's base, e.g. origin/main) it is the S-A merge gate on the committed HEAD
      (uncommitted changes are ignored): the generator runs on the BASE revision's files with HEAD's
      run, and HEAD must equal that output in every generated file, as regular files, and change
      nothing else. The first run's PR (base run/ still the
      placeholder) is H7's inclusion PR: its generated values are checked for self-consistency and
      every other changed path is listed under reviewByHand. The gate refuses a branch that does
      not contain the base (base-not-in-branch: merge origin/main into it first) and an expired run
      (run-expired), and reports hubReadbackDeadlinePassed past t_s + 60 min.

Deterministic: the same inputs give the same bytes. Offline: no cluster, hub, Router or journal
access, and it never reads a private key. The run's issuer signature is verified by Relay's composer
and again by the hub at startup; this script checks consistency, not cryptography. Stdlib + PyYAML.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = Path("kubernetes/components/relay-worker-candidate")
RUN_DIR = COMPONENT / "run"
AGENT = Path("kubernetes/apps/relay-researcher/agent")
INSTALLATION = Path("kubernetes/apps/relay-researcher/installation")
BINDING = Path("docs/relay-workers/researcher-i4/binding.json")
QUALIFICATION_EGRESS = INSTALLATION / "qualification-egress.yaml"
RELAY_MANIFEST = Path("kubernetes/apps/apps/relay/relay.yaml")

# Frozen for the campaign (owner decision I4, ailab #1782): docs/relay-workers/researcher-i4/summary.json.
BINDING_SHA256 = "fbf5cdb3559885683512dd4bb6c91a3f981e18f6571d6bcef001f0945ce6ae39"
HUB_ORIGIN = "https://relay.chifor.me"
TENANT_ID = "00000000-0000-4000-8000-000000000001"
HUB_NAMESPACE = "relay"
CONTROL_NAMESPACE = "relay-infra-control"
EXECUTION_NAMESPACE = "relay-researcher-1"
# kubernetes/optional/relay-candidate-journal/render.py ORIGIN (the Service at 10.96.0.42).
JOURNAL_ORIGIN = "https://relay-candidate-journal.relay-candidate-journal.svc:8443"
# One holder Secret (holder-key.pem, ca.pem) in the slot for the whole campaign (campaign runbook §3.5).
HOLDER_SECRET = "relay-candidate-holder"
SUPERVISOR_UID = 2000
MAX_DOCUMENT_BYTES = 65536  # Relay readCandidateFile, which the hub applies to the document
MAX_RUN_BYTES = 65536
PLACEHOLDER = "OPERATOR-FILLS"
RUN_DOMAIN = "relay.candidate-run/v1\n"
HUB_READBACK_DEADLINE_MS = 60 * 60 * 1000  # owner decision O6: abort before the grant past t_s + 60 min

# Relay deploy/infrastructure-agent/templates/configmap.yaml: candidateRuntime's fixed paths.
AGENT_PATHS = {
    "bindingFile": "/run/relay/candidate-binding.json",
    "trustFile": "/run/relay/candidate-trust.json",
    "journalCaFile": "/run/relay/candidate-ca.pem",
    "networkPolicyFile": "/run/relay/candidate-network-policy.json",
}
# The same chart's deployment.yaml with candidateRuntime.configMap set.
CONTROL_KEYS = ["binding.json", "config.json", "ca.pem", "network-policy.json"]
AGENT_MOUNTS = [
    {"name": "candidate", "mountPath": AGENT_PATHS["bindingFile"], "subPath": "binding.json", "readOnly": True},
    {"name": "candidate", "mountPath": AGENT_PATHS["trustFile"], "subPath": "config.json", "readOnly": True},
    {"name": "candidate", "mountPath": AGENT_PATHS["journalCaFile"], "subPath": "ca.pem", "readOnly": True},
    {"name": "candidate", "mountPath": AGENT_PATHS["networkPolicyFile"], "subPath": "network-policy.json",
     "readOnly": True},
]
AGENT_VOLUME_LINE = re.compile(r"^(?P<lead>[ \t]*- name: candidate\n[ \t]+configMap:\n[ \t]+name: )(?P<name>\S+)$",
                               re.M)
GENERATED = "relay-candidate-run.py"


class Refusal(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def refuse(code: str):
    raise Refusal(code)


def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def canonical(value) -> str:
    """Relay canonicalJson: sorted keys, no whitespace, JSON.stringify strings and numbers. Equal to it
    for these documents, which the checks below require to be in this form already."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def strict_json(text: str, code: str):
    def pairs(items):
        keys = [k for k, _ in items]
        if len(keys) != len(set(keys)):
            refuse(code)
        return dict(items)

    def constant(_):
        refuse(code)

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except Refusal:
        raise
    except ValueError:
        refuse(code)


def read_pinned(path: str, pin: str, limit: int, code: str) -> bytes:
    """A regular file (never followed through a link) of at most `limit` bytes whose SHA-256 is `pin`."""
    if not re.fullmatch(r"[0-9a-f]{64}", pin or ""):
        refuse(f"{code}-pin")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        refuse(f"{code}-unreadable")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            refuse(f"{code}-untrusted")
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        refuse(f"{code}-untrusted")
    if sha256(data) != pin:
        refuse(f"{code}-pin")
    return data


def utf8(data: bytes, code: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        refuse(code)


def run_digest(run: dict) -> str:
    """Relay candidateDigest: sha256("relay.candidate-run/v1\\n" + canonicalJson(run without signature))."""
    unsigned = {k: v for k, v in run.items() if k != "signature"}
    return sha256(RUN_DOMAIN + canonical(unsigned))


def measured_policy(policy: dict) -> dict:
    """Relay candidateNetworkPolicy: the measured form of a NetworkPolicy (apiVersion, kind, name and
    namespace, and the spec without empty omitempty collections, a missing port protocol as TCP)."""
    try:
        spec = json.loads(json.dumps(policy["spec"]))
        types = spec["policyTypes"]
        if (policy["apiVersion"] != "networking.k8s.io/v1" or policy["kind"] != "NetworkPolicy"
                or not isinstance(types, list) or not 1 <= len(types) <= 2 or len(set(types)) != len(types)
                or any(t not in ("Ingress", "Egress") for t in types)):
            refuse("egress-policy")
        metadata = {"name": policy["metadata"]["name"], "namespace": policy["metadata"]["namespace"]}
    except (KeyError, TypeError):
        refuse("egress-policy")

    def omit_empty(obj, field, array):
        value = obj.get(field) if isinstance(obj, dict) else None
        if isinstance(value, list if array else dict) and len(value) == 0:
            del obj[field]

    def selector(value):
        if isinstance(value, dict):
            omit_empty(value, "matchLabels", False)
            omit_empty(value, "matchExpressions", True)

    selector(spec.get("podSelector"))
    for direction, peers in (("ingress", "from"), ("egress", "to")):
        omit_empty(spec, direction, True)
        for rule in spec.get(direction, []) if isinstance(spec.get(direction), list) else []:
            omit_empty(rule, "ports", True)
            omit_empty(rule, peers, True)
            for port in rule.get("ports", []) if isinstance(rule, dict) else []:
                if isinstance(port, dict) and "protocol" not in port:
                    port["protocol"] = "TCP"
            for peer in rule.get(peers, []) if isinstance(rule, dict) else []:
                if isinstance(peer, dict):
                    selector(peer.get("podSelector"))
                    selector(peer.get("namespaceSelector"))
                    if isinstance(peer.get("ipBlock"), dict):
                        omit_empty(peer["ipBlock"], "except", True)
    return {"apiVersion": policy["apiVersion"], "kind": policy["kind"], "metadata": metadata, "spec": spec}


def names(digest: str) -> dict:
    d16 = digest[:16]
    return {
        "hubConfigMap": f"relay-worker-candidate-{d16}",
        "hubSessionKeySecret": f"relay-worker-candidate-key-{d16}",
        "controlConfigMap": f"relay-candidate-control-{d16}",
        "trustConfigMap": f"relay-candidate-trust-{d16}",
        "pinConfigMap": f"relay-candidate-pin-{d16}",
    }


def load_yaml_docs(path: Path) -> list:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def hub_image(repo: Path) -> str:
    [deployment] = [d for d in load_yaml_docs(repo / RELAY_MANIFEST) if d.get("kind") == "Deployment"]
    [relay] = [c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "relay"]
    return relay["image"]


def selected_egress(repo: Path) -> str:
    kust = yaml.safe_load((repo / INSTALLATION / "kustomization.yaml").read_text(encoding="utf-8"))
    chosen = [r for r in kust.get("resources", []) if r in ("execution-egress.yaml", "qualification-egress.yaml")]
    return chosen[0] if len(chosen) == 1 else ""


# --- the run and its hub document ------------------------------------------------------------------

def validate(repo: Path, run_text: str, document_bytes: bytes) -> dict:
    """Every consistency check between the signed run, its hub document and this repository."""
    run = strict_json(run_text, "run-json")
    if run_text != canonical(run):
        refuse("run-not-canonical")  # sign-candidate-run.ts writes canonical JSON
    try:
        if run["version"] != 1 or run["kind"] != "relay.candidate-run":
            refuse("run-schema")
        p = run["payload"]
        issuer = run["issuerKeyId"]
        control = [n for n in p["namespaces"] if n["purpose"] == "control"]
        execution = [n for n in p["namespaces"] if n["purpose"] == "execution"]
        ledger = p["ledger"]
        for key in ("runId", "issuedAt", "notBefore", "expiresAt", "hub", "router", "image", "installationId",
                    "installationPolicyDigest", "sourceCommit", "roleTemplateDigests", "egressPolicySha256",
                    "clusterUid", "runtimeDigest", "profileTemplateDigests"):
            p[key]
        ledger["clientPublicKeys"]["agent"], ledger["origin"], ledger["caSha256"], ledger["replyPublicKey"]
    except (KeyError, TypeError):
        refuse("run-schema")
    if len(control) != 1 or len(execution) != 1:
        refuse("run-schema")
    digest = run_digest(run)

    if len(document_bytes) > MAX_DOCUMENT_BYTES:
        refuse("document-too-large")
    document_text = utf8(document_bytes, "document-json")
    document = strict_json(document_text, "document-json")
    if document_text != canonical(document):
        refuse("document-not-compact")  # compose-candidate-hub-document.ts writes canonical JSON
    if not isinstance(document, dict) or set(document) != {
            "version", "run", "issuerKeys", "destination", "pool", "binding", "egressPolicy", "profiles"}:
        refuse("document-schema")
    if document["version"] != 1:
        refuse("document-schema")
    if document["run"] != run_text:
        refuse("document-run-mismatch")
    keys = document["issuerKeys"]
    if (not isinstance(keys, list) or not 1 <= len(keys) <= 8
            or any(not isinstance(k, dict) or set(k) != {"id", "publicKey"} for k in keys)
            or len({k["id"] for k in keys}) != len(keys) or issuer not in {k["id"] for k in keys}):
        refuse("document-issuer-keys")
    if document["destination"] != {
            "hub": p["hub"], "router": p["router"],
            "journal": {"origin": ledger["origin"], "caSha256": ledger["caSha256"],
                        "replyPublicKey": ledger["replyPublicKey"]}}:
        refuse("document-destination")

    binding_bytes = (repo / BINDING).read_bytes()
    if sha256(binding_bytes) != BINDING_SHA256:
        refuse("binding-not-frozen")
    binding = json.loads(binding_bytes)
    if document["binding"] != binding:
        refuse("document-binding")
    manifest = binding["runtime"]["manifest"]
    mismatches = [name for name, ok in [
        ("installationId", binding["installationId"] == p["installationId"]),
        ("installationPolicyDigest", binding["policyDigest"] == p["installationPolicyDigest"]),
        ("image", binding["runtime"]["image"] == p["image"]),
        ("sourceCommit", manifest["sourceCommit"] == p["sourceCommit"]),
        ("roleTemplateDigests", all(t["digest"] in p["roleTemplateDigests"] for t in binding["templates"])),
        ("hubOrigin", p["hub"]["origin"] == HUB_ORIGIN and manifest["supervisorConfig"]["hubOrigin"] == HUB_ORIGIN),
        ("tenantId", p["hub"]["tenantId"] == TENANT_ID),
        ("hubImage", p["hub"]["image"] == hub_image(repo)),
        ("journalOrigin", ledger["origin"] == JOURNAL_ORIGIN),
        ("namespaces", control[0]["name"] == CONTROL_NAMESPACE and execution[0]["name"] == EXECUTION_NAMESPACE),
    ] if not ok]
    if mismatches:
        refuse("run-mismatch:" + ",".join(mismatches))

    if selected_egress(repo) != "qualification-egress.yaml":
        refuse("egress-stage-not-qualification")
    [installed] = load_yaml_docs(repo / QUALIFICATION_EGRESS)
    policy = measured_policy(installed)
    if sha256(canonical(policy)) != p["egressPolicySha256"]:
        refuse("run-egress-policy")
    if document["egressPolicy"] != policy:
        refuse("document-egress-policy")

    pool = document["pool"]
    try:
        target = pool["target"]
        if (target["installationId"] != p["installationId"] or target["controlNamespace"] != CONTROL_NAMESPACE
                or target["executionSlots"] != [EXECUTION_NAMESPACE] or pool["capacity"]["instances"] != 1
                or pool["templates"] != binding["templates"]):
            refuse("document-pool")
    except (KeyError, TypeError):
        refuse("document-pool")
    templates = manifest["holderProfiles"].get("workerTemplates", [])
    profiles = document["profiles"]
    if (not isinstance(profiles, list) or not 1 <= len(profiles) <= 4
            or any(e.get("template") not in templates or e.get("templateDigest") not in p["roleTemplateDigests"]
                   for e in profiles)):
        refuse("document-profiles")

    trust = {
        "version": 1,
        "issuerKeys": {k["id"]: k["publicKey"] for k in keys},
        "journal": document["destination"]["journal"],
        "run": run,
        "supervisorUid": SUPERVISOR_UID,
    }
    trust_bytes = (canonical(trust) + "\n").encode("utf-8")
    return {
        "run": run,
        "payload": p,
        "runDigest": digest,
        "runText": run_text,
        "document": document_bytes,
        "documentSha256": sha256(document_bytes),
        "names": names(digest),
        "binding": binding_bytes,
        "policy": policy,
        "trust": trust_bytes,
        "trustSha256": sha256(trust_bytes),
    }


# --- generated hub files ---------------------------------------------------------------------------

def hub_files(v: dict) -> dict:
    n = v["names"]
    header = (f"# GENERATED by scripts/{GENERATED} for candidate run {v['runDigest']}\n"
              f"# (runId {v['payload']['runId']}). Do not edit: regenerate it, and confirm with\n"
              f"#   python3 scripts/{GENERATED} check --base origin/main --run-digest {v['runDigest']}\n")
    kustomization = header + (
        "apiVersion: kustomize.config.k8s.io/v1alpha1\n"
        "kind: Component\n"
        "configMapGenerator:\n"
        f"- name: {n['hubConfigMap']}\n"
        f"  namespace: {HUB_NAMESPACE}\n"
        "  files:\n"
        "  - document.json\n"
        "  options:\n"
        "    disableNameSuffixHash: true\n"
        "    immutable: true\n"
        "    annotations:\n"
        f"      relay.chifor.me/candidate-run-digest: {v['runDigest']}\n"
        "patches:\n"
        "- path: deployment.yaml\n")
    deployment = header + (
        f"# The session-key Secret is operator-created in {HUB_NAMESPACE} from the run's permit key\n"
        "# (key session-key.pem), never in git.\n"
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: relay\n"
        f"  namespace: {HUB_NAMESPACE}\n"
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        "      - name: relay\n"
        "        env:\n"
        "        - name: RELAY_WORKER_CANDIDATE_SHA256\n"
        f"          value: {v['documentSha256']}\n"
        "      volumes:\n"
        "      - name: worker-candidate-document\n"
        "        configMap:\n"
        f"          name: {n['hubConfigMap']}\n"
        "      - name: worker-candidate-key-source\n"
        "        secret:\n"
        f"          secretName: {n['hubSessionKeySecret']}\n")
    return {
        RUN_DIR / "document.json": v["document"],
        RUN_DIR / "kustomization.yaml": kustomization.encode("utf-8"),
        RUN_DIR / "deployment.yaml": deployment.encode("utf-8"),
    }


def placeholder_files() -> dict:
    header = (f"# PLACEHOLDER: no candidate run yet. scripts/{GENERATED} generate replaces the three files of\n"
              "# this directory for each signed run (docs/runbooks/relay-candidate-run-generator.md);\n"
              "# scripts/tests/test_relay_candidate_run.py refuses including the component while they are.\n")
    document = {PLACEHOLDER: "the CandidateHubDocument that Relay scripts/compose-candidate-hub-document.ts "
                             "composes for the signed run; Relay's schema refuses this placeholder"}
    kustomization = header + (
        "apiVersion: kustomize.config.k8s.io/v1alpha1\n"
        "kind: Component\n"
        "configMapGenerator:\n"
        "- name: relay-worker-candidate-unsigned\n"
        f"  namespace: {HUB_NAMESPACE}\n"
        "  files:\n"
        "  - document.json\n"
        "  options:\n"
        "    disableNameSuffixHash: true\n"
        "    immutable: true\n"
        "patches:\n"
        "- path: deployment.yaml\n")
    deployment = header + (
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: relay\n"
        f"  namespace: {HUB_NAMESPACE}\n"
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        "      - name: relay\n"
        "        env:\n"
        "        - name: RELAY_WORKER_CANDIDATE_SHA256\n"
        f"          value: {PLACEHOLDER}-sha256-of-run-document-json\n"
        "      volumes:\n"
        "      - name: worker-candidate-document\n"
        "        configMap:\n"
        "          name: relay-worker-candidate-unsigned\n"
        "      - name: worker-candidate-key-source\n"
        "        secret:\n"
        "          secretName: relay-worker-candidate-key-unsigned\n")
    return {
        RUN_DIR / "document.json": (canonical(document) + "\n").encode("utf-8"),
        RUN_DIR / "kustomization.yaml": kustomization.encode("utf-8"),
        RUN_DIR / "deployment.yaml": deployment.encode("utf-8"),
    }


def is_placeholder(repo: Path) -> bool:
    path = repo / RUN_DIR / "document.json"
    return PLACEHOLDER.encode() in path.read_bytes()


# --- the agent half --------------------------------------------------------------------------------

def local_policy_digest(installation: dict) -> str:
    """Relay agent localPolicyDigest: contentDigest("relay.infrastructure/v1/installation-policy", policy)."""
    return sha256(b"relay.infrastructure/v1/installation-policy\0" + canonical(installation).encode("utf-8"))


def agent_config(repo: Path, v: dict) -> bytes:
    path = repo / AGENT / "config.json"
    text = path.read_text(encoding="utf-8")
    config = strict_json(text, "agent-config-json")
    newline = "\n" if text.endswith("\n") else ""
    if text != canonical(config) + newline:
        refuse("agent-config-not-imported")  # I5 commits Relay's canonical 31-agent-config.json
    p = v["payload"]
    try:
        identity, policy = config["identity"], config["policy"]
        if policy["bootstrapSecrets"] is not True:
            refuse("agent-bootstrap-secrets-off")
        if any(k in config for k in ("podRuntime", "ordinaryCanaryRuntime", "ordinaryNamespacePins",
                                      "provenancePreparation", "provenanceExecution")):
            refuse("agent-mode-conflict")
        binding = json.loads(v["binding"])
        mismatches = [name for name, ok in [
            ("clusterUid", identity["clusterUid"] == p["clusterUid"]),
            ("installationId", identity["installationId"] == p["installationId"]),
            ("tenantId", identity["tenantId"] == p["hub"]["tenantId"]),
            ("hubOrigin", config["hub"]["origin"] == p["hub"]["origin"]),
            ("controlNamespace", identity["controlNamespace"] == CONTROL_NAMESPACE),
            ("executionSlots", policy["executionSlots"] == [EXECUTION_NAMESPACE]),
            ("installationPublicKey", identity["installationPublicKey"] == p["ledger"]["clientPublicKeys"]["agent"]),
            ("policyDigest", local_policy_digest(policy["installation"]) == binding["policyDigest"]),
            ("allowedRuntimeDigests", p["runtimeDigest"] in policy["allowedRuntimeDigests"]),
            ("allowedTemplates", all(any(a == {"templateId": t["definition"]["templateId"],
                                               "revision": t["definition"]["revision"], "digest": t["digest"]}
                                         for a in policy["allowedTemplates"]) for t in binding["templates"])),
        ] if not ok]
    except (KeyError, TypeError):
        refuse("agent-config-schema")
    if mismatches:
        refuse("agent-mismatch:" + ",".join(mismatches))
    previous = config.get("candidateRuntime")
    if previous is not None and (
            not isinstance(previous, dict)
            or {k: previous.get(k) for k in AGENT_PATHS} != AGENT_PATHS
            or previous.get("bindingSha256") != BINDING_SHA256
            or previous.get("holderSecret") != HOLDER_SECRET):
        refuse("agent-candidate-runtime-fixed-fields")  # the binding and the holder Secret are frozen
    config["candidateRuntime"] = {
        **AGENT_PATHS,
        "bindingSha256": BINDING_SHA256,
        "trustSha256": v["trustSha256"],
        "trustConfigMap": v["names"]["trustConfigMap"],
        "pinConfigMap": v["names"]["pinConfigMap"],
        "holderSecret": HOLDER_SECRET,
    }
    return (canonical(config) + newline).encode("utf-8")


def agent_candidate(deployment: dict) -> tuple[list, list]:
    spec = deployment["spec"]["template"]["spec"]
    [agent] = [c for c in spec["containers"] if c["name"] == "agent"]
    mounts = [m for m in agent.get("volumeMounts", []) if m.get("name") == "candidate"]
    volumes = [v for v in spec.get("volumes", []) if v.get("name") == "candidate"]
    return mounts, volumes


def agent_deployment(repo: Path, v: dict) -> bytes:
    path = repo / AGENT / "deployment.yaml"
    text = path.read_text(encoding="utf-8")
    try:
        [before] = [d for d in load_yaml_docs(path) if d.get("kind") == "Deployment"]
        mounts, volumes = agent_candidate(before)
    except (yaml.YAMLError, ValueError, KeyError, TypeError, AttributeError):
        refuse("agent-deployment-yaml")
    if mounts != AGENT_MOUNTS:
        refuse("agent-candidate-mounts")
    if len(volumes) != 1 or set(volumes[0]) != {"name", "configMap"} or {
            k: volumes[0]["configMap"].get(k) for k in ("defaultMode", "optional", "items")} != {
            "defaultMode": 0o444, "optional": False, "items": [{"key": k, "path": k} for k in CONTROL_KEYS]} or set(
            volumes[0]["configMap"]) != {"name", "defaultMode", "optional", "items"}:
        refuse("agent-candidate-volume")
    matches = list(AGENT_VOLUME_LINE.finditer(text))
    if len(matches) != 1 or matches[0]["name"] != volumes[0]["configMap"]["name"]:
        refuse("agent-candidate-volume-layout")
    name = v["names"]["controlConfigMap"]
    out = text[:matches[0].start("name")] + name + text[matches[0].end("name"):]
    [after] = [d for d in yaml.safe_load_all(out) if d and d.get("kind") == "Deployment"]
    expected = json.loads(json.dumps(before))
    agent_candidate(expected)[1][0]["configMap"]["name"] = name
    if after != expected:
        refuse("agent-candidate-volume-layout")
    return out.encode("utf-8")


def generated_files(repo: Path, v: dict) -> dict:
    return {**hub_files(v),
            AGENT / "config.json": agent_config(repo, v),
            AGENT / "deployment.yaml": agent_deployment(repo, v)}


# --- operator-local ConfigMaps (public, never in git) ----------------------------------------------

def operator_objects(v: dict, journal_ca: bytes) -> dict:
    if sha256(journal_ca) != v["payload"]["ledger"]["caSha256"]:
        refuse("journal-ca-mismatch")
    ca = utf8(journal_ca, "journal-ca-mismatch")
    n, annotations = v["names"], {"relay.chifor.me/candidate-run-digest": v["runDigest"]}

    def configmap(name, namespace, data):
        return {"apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": namespace, "annotations": annotations,
                             "labels": {"app.kubernetes.io/part-of": "relay-researcher"}},
                "immutable": True, "data": data}

    trust = v["trust"].decode("utf-8")
    return {
        "control-configmap.json": configmap(n["controlConfigMap"], CONTROL_NAMESPACE, {
            "binding.json": v["binding"].decode("utf-8"),
            "config.json": trust,
            "ca.pem": ca,
            "network-policy.json": canonical(v["policy"]) + "\n",
        }),
        "slot-trust-configmap.json": configmap(n["trustConfigMap"], EXECUTION_NAMESPACE, {"config.json": trust}),
        "slot-pin-configmap.json": configmap(n["pinConfigMap"], EXECUTION_NAMESPACE, {"sha256": v["trustSha256"]}),
    }


def iso_ms(text: str) -> int:
    return round(datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp() * 1000)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def summary(v: dict, files: dict) -> dict:
    p = v["payload"]
    return {
        "kind": "ailab.relay-candidate-run-generated",
        "grantsQualification": False,
        "runId": p["runId"],
        "runDigest": v["runDigest"],
        "runSha256": sha256(v["runText"]),
        "issuedAt": iso(p["issuedAt"]),
        "expiresAt": iso(p["expiresAt"]),
        "abortBeforeGrantIfHubNotReadBackBy": iso(p["issuedAt"] + HUB_READBACK_DEADLINE_MS),
        "document": {"sha256": v["documentSha256"], "bytes": len(v["document"]), "maxBytes": MAX_DOCUMENT_BYTES},
        "names": v["names"],
        "holderSecret": HOLDER_SECRET,
        "bindingSha256": BINDING_SHA256,
        "trustSha256": v["trustSha256"],
        "files": {str(path): sha256(data) for path, data in sorted(files.items(), key=lambda kv: str(kv[0]))},
    }


def generate(repo: Path, run_path: str, run_pin: str, document_path: str, document_pin: str,
             journal_ca: str | None = None, operator_out: str | None = None, now_ms: int | None = None) -> dict:
    if (journal_ca is None) != (operator_out is None):
        refuse("usage")
    run_text = utf8(read_pinned(run_path, run_pin, MAX_RUN_BYTES, "run"), "run-json")
    # Read up to 1 MiB so that an oversized document gets its own refusal (document-too-large).
    document = read_pinned(document_path, document_pin, 1024 * 1024, "document")
    v = validate(repo, run_text, document)
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if now < v["payload"]["notBefore"]:
        refuse("run-not-yet-valid")
    if now >= v["payload"]["expiresAt"]:
        refuse("run-expired")
    files = generated_files(repo, v)  # every refusal happens before anything is written
    objects = None
    if operator_out is not None:
        objects = operator_objects(v, read_pinned(journal_ca, v["payload"]["ledger"]["caSha256"], 256 * 1024,
                                                  "journal-ca"))
        out = Path(operator_out)
        try:
            out.mkdir(mode=0o755)  # a NEW directory: never overwrite an operator file
        except OSError:
            refuse("operator-out-exists")
        for name, obj in objects.items():
            (out / name).write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for path, data in files.items():
        (repo / path).write_bytes(data)
    result = summary(v, files)
    if objects is not None:
        result["operatorFiles"] = {name: str(Path(operator_out) / name) for name in objects}
    return result


def check(repo: Path, expected_digest: str | None = None) -> tuple[dict, list]:
    if is_placeholder(repo):
        if expected_digest:
            return {"runDigest": None}, ["no run recorded: run/ is the placeholder"]
        expected = placeholder_files()
        diff = [str(path) for path, data in expected.items() if (repo / path).read_bytes() != data]
        return {"kind": "ailab.relay-candidate-run-check", "runDigest": None, "state": "placeholder"}, diff
    document = (repo / RUN_DIR / "document.json").read_bytes()
    run_text = strict_json(utf8(document, "document-json"), "document-json").get("run")
    if not isinstance(run_text, str):
        refuse("document-schema")
    v = validate(repo, run_text, document)
    diff = []
    try:
        expected = generated_files(repo, v)
    except Refusal as refusal:
        expected = hub_files(v)
        diff.append(f"agent tree: {refusal.code}")
    diff += [str(path) for path, data in expected.items() if (repo / path).read_bytes() != data]
    if expected_digest and expected_digest != v["runDigest"]:
        diff.append(f"run digest {v['runDigest']} is not the expected {expected_digest}")
    result = summary(v, expected)
    result["kind"] = "ailab.relay-candidate-run-check"
    return result, diff


GENERATOR_INPUTS = (COMPONENT, AGENT, INSTALLATION, BINDING, RELAY_MANIFEST)


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if result.returncode:
        refuse("git")
    return result.stdout


def extract(repo: Path, rev: str, into: Path) -> None:
    """The generator's inputs at `rev`, read from the tree object itself (never `git archive`, which
    applies export attributes), as files; a symlink, submodule or other entry is refused."""
    listing = git(repo, "ls-tree", "-r", "-z", rev, "--", *map(str, GENERATOR_INPUTS)).decode()
    for entry in filter(None, listing.split("\0")):
        meta, path = entry.split("\t", 1)
        mode, kind, obj = meta.split()
        if kind != "blob" or mode not in ("100644", "100755") or path.startswith("/") or ".." in Path(path).parts:
            refuse("git-entry")
        target = into / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(git(repo, "cat-file", "blob", obj))


def blob(repo: Path, rev: str, path: str) -> bytes | None:
    """A regular, non-executable file's bytes at `rev`; None when absent; refused for any other entry."""
    entry = git(repo, "ls-tree", "-z", rev, "--", path).decode()
    if not entry:
        return None
    mode, kind, obj = entry.split("\t", 1)[0].split()
    if (mode, kind) != ("100644", "blob"):
        refuse("git-entry")
    return git(repo, "cat-file", "blob", obj)


def check_against_base(repo: Path, base: str, expected_digest: str | None = None,
                       now_ms: int | None = None) -> tuple[dict, list]:
    """The S-A merge gate: the committed HEAD against `base`. Uncommitted changes are ignored: what
    merges is HEAD."""
    for rev in (base, "HEAD"):
        git(repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}")
    # The generator runs against the base tip (a hub image bump there must count), so the branch must
    # contain it; otherwise every change on the base would show as "not generated".
    if subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", base, "HEAD"],
                      capture_output=True).returncode:
        refuse("base-not-in-branch")  # merge the base (origin/main) into the branch, then re-run
    now = int(time.time() * 1000) if now_ms is None else now_ms
    changed = {p for p in git(repo, "diff", "--name-only", "-z", base, "HEAD", "--").decode().split("\0") if p}
    run_files = {str(RUN_DIR / name) for name in ("document.json", "kustomization.yaml", "deployment.yaml")}
    with tempfile.TemporaryDirectory() as tmp:
        base_tree, head_tree = Path(tmp) / "base", Path(tmp) / "head"
        base_tree.mkdir()
        head_tree.mkdir()
        extract(repo, base, base_tree)
        extract(repo, "HEAD", head_tree)
        if is_placeholder(head_tree):
            # Not a per-run PR (staging, or the removal before the canary): run/ must be the placeholder.
            result, diff = check(head_tree, expected_digest)
            result.update(base=base, reviewByHand=sorted(changed))
            return result, diff
        if not (base_tree / RUN_DIR / "document.json").is_file() or is_placeholder(base_tree):
            # H7's inclusion PR (the first run): its run-bound values are checked for self-consistency;
            # every other change, including the rest of the agent files, is reviewed by hand.
            result, diff = check(head_tree, expected_digest)
            if now >= iso_ms(result["expiresAt"]):
                refuse("run-expired")
            result.update(base=base, firstRun=True, reviewByHand=sorted(changed - run_files),
                          hubReadbackDeadlinePassed=now > iso_ms(result["issuedAt"]) + HUB_READBACK_DEADLINE_MS)
            return result, diff
        document = blob(repo, "HEAD", str(RUN_DIR / "document.json"))
        run_text = strict_json(utf8(document, "document-json"), "document-json").get("run")
        if not isinstance(run_text, str):
            refuse("document-schema")
        v = validate(base_tree, run_text, document)
        expected = generated_files(base_tree, v)
    if now >= v["payload"]["expiresAt"]:
        refuse("run-expired")  # merging it would only roll the hub to log worker_candidate_unavailable
    diff = [f"not the generator output for the base: {path}" for path, data in expected.items()
            if blob(repo, "HEAD", str(path)) != data]
    diff += sorted(f"not generated: {p}" for p in changed - {str(path) for path in expected})
    if expected_digest and expected_digest != v["runDigest"]:
        diff.append(f"run digest {v['runDigest']} is not the expected {expected_digest}")
    result = summary(v, expected)
    result.update(kind="ailab.relay-candidate-run-check", base=base, firstRun=False,
                  hubReadbackDeadlinePassed=now > v["payload"]["issuedAt"] + HUB_READBACK_DEADLINE_MS)
    return result, diff


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", default=str(ROOT))
    sub = parser.add_subparsers(dest="command", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--run", required=True)
    g.add_argument("--run-sha256", required=True)
    g.add_argument("--document", required=True)
    g.add_argument("--document-sha256", required=True)
    g.add_argument("--journal-ca")
    g.add_argument("--operator-out")
    c = sub.add_parser("check")
    c.add_argument("--run-digest")
    c.add_argument("--base")
    sub.add_parser("reset")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        if args.command == "generate":
            print(json.dumps(generate(repo, args.run, args.run_sha256, args.document, args.document_sha256,
                                      args.journal_ca, args.operator_out), indent=2))
            return 0
        if args.command == "reset":
            for path, data in placeholder_files().items():
                (repo / path).write_bytes(data)
            print(json.dumps({"kind": "ailab.relay-candidate-run-reset", "files": {
                str(path): sha256(data) for path, data in placeholder_files().items()}}, indent=2))
            return 0
        result, diff = (check_against_base(repo, args.base, args.run_digest) if args.base
                        else check(repo, args.run_digest))
        result["equal"] = not diff
        result["differences"] = diff
        print(json.dumps(result, indent=2))
        return 0 if not diff else 1
    except Refusal as refusal:
        print(f"Refusal: {refusal.code}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
