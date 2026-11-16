#!/usr/bin/env python3
"""Join Relay's generated installation bundle to the reviewed ailab researcher installation.

The first Relay researcher installation (docs/runbooks/relay-researcher-installation.md) is reviewed
in git BEFORE Relay has approved it, so two things cannot be in git yet: the installation ID Relay
assigns at approval and the release-derived parts of the agent configuration. Relay's own offline
tool, scripts/prepare-pod-installation.ts, renders the namespaces, containment, deny gate and agent
configuration from one definition. This script proves that the tool's bundle for the approved
installation is exactly what was reviewed here, and then copies the only two things it may copy.

  check  <bundle-dir> [--chart <relay-checkout>]
         Read-only. Verifies the bundle's own file hashes; the definition against the reviewed
         template (docs/relay-workers/researcher-installation-definition.json): every value that is
         not an OPERATOR-FILLS leaf must be identical, every OPERATOR-FILLS leaf must be filled; the
         tool's 10-namespaces, 20-containment and 21-admission-deny objects against
         kubernetes/apps/relay-researcher/installation with the bundle's installation ID; the
         execution egress artifact digest against the egress stage the installation tree includes
         (execution-egress.yaml or qualification-egress.yaml, exactly one); the binding's G3 fields
         (hostAliases equal to the pinned internal TLS and journal Service addresses, the hub CA and the
         templates' Router/communication CA pins equal to docs/relay-workers/researcher-runtime-ca.crt),
         required in both stages because the binding never changes between them; 30-agent-values.json and
         31-agent-config.json against the committed agent objects and config, and the generated
         agent policy against the definition it came from (installationPolicy verbatim, podTiers and
         allowedTemplates from the binding, the same policy in values and config, report.json's
         identity; placements on relay-worker-1 with relay-gvisor's overhead, every key of its
         scheduling.nodeSelector and exactly its tolerations, read from relay-workers.tf); pins already filled against the definition. With --chart it also renders that checkout's
         deploy/infrastructure-agent with 30-agent-values.json (helm template, no cluster) and
         compares every rendered object with the committed mirror. Exit 1 on the first mismatch.
  import <bundle-dir> --chart <relay-checkout>
         Runs check WITH the chart render (required here), then writes the bundle's installationId, agentImage and sourceCommit into
         kubernetes/apps/relay-researcher/pins/pins.yaml and copies 31-agent-config.json byte for
         byte to kubernetes/apps/relay-researcher/agent/config.json. It writes nothing else, creates
         no Kubernetes object and contacts nothing. Review and commit the diff.

Stdlib + PyYAML. Never reads a secret: the bundle is public by construction (no key, no token).
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TREE = ROOT / "kubernetes/apps/relay-researcher"
TEMPLATE = ROOT / "docs/relay-workers/researcher-installation-definition.json"
EGRESS_STAGES = {"execution-egress.yaml": "enrollment", "qualification-egress.yaml": "qualification"}
# Owner decision G3 (relay#277 comment 122379): the qualification stage's workers reach these three
# pinned Services through measured hostAliases, and trust only the ailab root CA for them.
INTERNAL_TLS_TERMINATORS = ROOT / "kubernetes/apps/relay-internal-tls/terminators.yaml"
JOURNAL_RENDER = ROOT / "kubernetes/optional/relay-candidate-journal/render.py"
RUNTIME_CA = ROOT / "docs/relay-workers/researcher-runtime-ca.crt"
RELAY_WORKERS_TF = ROOT / "kubernetes/infra/agent-nodes/relay-workers.tf"
HUB_CA_PATH = "/etc/relay/hub-ca.pem"
ROUTER_ORIGIN = "https://router.chifor.me"
PLACEHOLDER = "OPERATOR-FILLS"
BUNDLE_FILES = (
    "00-definition.json",
    "10-namespaces.json",
    "20-containment.json",
    "21-admission-deny.json",
    "30-agent-values.json",
    "31-agent-config.json",
    "40-planned-pod-envelope.json",
    "report.json",
)
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
AGENT_IMAGE = re.compile(r"^registry\.chifor\.me/relay/infrastructure-agent@sha256:[0-9a-f]{64}$")
CHART_RELEASE = "relay-infrastructure-agent"


class Mismatch(Exception):
    pass


def fail(message: str) -> None:
    raise Mismatch(message)


def load_yaml_docs(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def is_placeholder(value) -> bool:
    return isinstance(value, str) and value.startswith(PLACEHOLDER)


def template() -> dict:
    data = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    data.pop("_comment", None)
    return data


def pins() -> dict:
    (doc,) = load_yaml_docs(TREE / "pins/pins.yaml")
    return doc["data"]


def pins_filled(p: dict) -> bool:
    return bool(
        UUID.match(p["installationId"]) and COMMIT.match(p["sourceCommit"]) and AGENT_IMAGE.match(p["agentImage"])
    )


def measurement_digest(policy: dict) -> str:
    """Relay candidateMeasurementDigest(candidateNetworkPolicy(policy)) for a policy written without
    empty collections or defaulted protocols (the committed allow-list; the test asserts that)."""
    measured = {
        "apiVersion": policy["apiVersion"],
        "kind": policy["kind"],
        "metadata": {"name": policy["metadata"]["name"], "namespace": policy["metadata"]["namespace"]},
        "spec": policy["spec"],
    }
    canonical = json.dumps(measured, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compare_definition(expected, actual, path: str = "") -> None:
    """Every reviewed leaf equal; every OPERATOR-FILLS leaf filled. Post-enrollment regeneration may
    add identity.fenceObjectUid and drop references.enrollmentTokenSecret, and nothing else."""
    if is_placeholder(expected):
        if actual is None or is_placeholder(actual) or (isinstance(actual, str) and PLACEHOLDER in actual):
            fail(f"definition {path or '/'} is still a placeholder")
        # Every remaining OPERATOR-FILLS leaf needs content (allowedEgressDestinations, which may
        # legitimately be empty, is a reviewed [] in the template, not a placeholder).
        if actual in ("", [], {}):
            fail(f"definition {path} is empty")
        return
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            fail(f"definition {path} is not an object")
        optional_extra = {"/identity": {"fenceObjectUid"}}.get(path, set())
        optional_missing = {"/references": {"enrollmentTokenSecret"}}.get(path, set())
        extra = set(actual) - set(expected) - optional_extra
        missing = set(expected) - set(actual) - optional_missing
        if extra or missing:
            fail(f"definition {path or '/'} keys differ: extra {sorted(extra)}, missing {sorted(missing)}")
        for key in expected:
            if key in actual:
                compare_definition(expected[key], actual[key], f"{path}/{key}")
        if "fenceObjectUid" in actual and path == "/identity" and not UUID.match(str(actual["fenceObjectUid"])):
            fail("definition /identity/fenceObjectUid is not a UUID")
        return
    if expected != actual:
        fail(f"definition {path} differs from the reviewed template: {actual!r} != {expected!r}")


def egress_stage() -> tuple[str, Path]:
    """The one egress stage kubernetes/apps/relay-researcher/installation includes."""
    resources = yaml.safe_load((TREE / "installation/kustomization.yaml").read_text(encoding="utf-8"))["resources"]
    stages = [r for r in resources if r in EGRESS_STAGES]
    if len(stages) != 1:
        fail(f"the installation tree must include exactly one egress stage, found {stages}")
    return EGRESS_STAGES[stages[0]], TREE / "installation" / stages[0]


def internal_tls_addresses() -> dict:
    """The pinned ClusterIPs, read from the manifests that create them (never a second copy here)."""
    services = {d["metadata"]["name"]: d["spec"].get("clusterIP")
                for d in load_yaml_docs(INTERNAL_TLS_TERMINATORS) if d["kind"] == "Service"}
    journal = re.findall(r"^CLUSTER_IP = '([0-9.]+)'$", JOURNAL_RENDER.read_text(encoding="utf-8"), re.M)
    if len(journal) != 1:
        fail("kubernetes/optional/relay-candidate-journal/render.py has no single CLUSTER_IP")
    return {"hub": services.get("relay-hub-tls"), "router": services.get("relay-router-tls"),
            "candidateJournal": journal[0]}


def runtime_class_scheduling() -> tuple[dict, list[dict]]:
    """relay-gvisor's scheduling.nodeSelector and tolerations, read from the RuntimeClass manifest
    (kubernetes/infra/agent-nodes/relay-workers.tf, never a second copy here). RuntimeClass admission
    merges both into every Pod of the class, and Relay pins the placement's selector exactly (VAP CEL
    equality and the executor's readback), so a placement must already carry every selector key and
    exactly the class's tolerations (Relay docs/infrastructure-candidate-policy.md)."""
    text = RELAY_WORKERS_TF.read_text(encoding="utf-8")
    block = re.search(r'resource "kubernetes_manifest" "relay_gvisor" \{(.*?)\n\}', text, re.S)
    scheduling = re.search(r"scheduling = \{\s*nodeSelector = \{(.*?)\}\s*tolerations = \[(.*?)\]",
                           block.group(1) if block else "", re.S)
    if not scheduling:
        fail("relay-workers.tf has no relay-gvisor RuntimeClass scheduling block")
    selector = dict(re.findall(r'"([^"]+)"\s*=\s*"([^"]*)"', scheduling.group(1)))
    tolerations = []
    for body in re.findall(r"\{(.*?)\}", scheduling.group(2), re.S):
        # Every attribute, a number (tolerationSeconds) included: one left out would be merged into the
        # Pod yet missing from the comparison.
        toleration = {}
        for attribute in filter(None, (a.strip() for a in re.split(r"[\n,]", body))):
            match = re.fullmatch(r'(\w+)\s*=\s*(?:"([^"]*)"|(\d+))', attribute)
            if not match or match.group(1) in toleration:
                fail(f"relay-workers.tf relay-gvisor toleration attribute not understood: {attribute!r}")
            toleration[match.group(1)] = match.group(2) if match.group(2) is not None else int(match.group(3))
        tolerations.append(toleration)
    if not selector or not tolerations:
        fail("relay-workers.tf relay-gvisor scheduling has no nodeSelector or tolerations")
    return selector, tolerations


def check_internal_tls(definition: dict, stage: str) -> None:
    """G3 in the release's binding: exactly the pinned addresses, the hub origin and CA of the runtime,
    and every worker template's Router and communication clients pinned to the same CA. Required in
    BOTH stages: the binding is never changed between them (qualification receipts bind the exact
    binding), so an enrollment-stage bundle without these fields could never reach the qualification
    stage; it is refused at step I4, not discovered at Q4."""
    binding = definition["binding"]
    if not isinstance(binding, dict):
        fail("the definition's binding is not an object")
    aliases = binding.get("hostAliases")
    if aliases is None:
        fail(f"the binding has no hostAliases: the {stage}-stage binding must already carry G3 "
             "(the qualification stage reuses it unchanged and its slot has no DNS)")
    if aliases != internal_tls_addresses():
        fail(f"binding hostAliases {aliases!r} are not the pinned internal TLS and journal addresses")
    ca_sha = hashlib.sha256(RUNTIME_CA.read_bytes()).hexdigest()
    runtime = binding.get("runtime")
    manifest = runtime.get("manifest") if isinstance(runtime, dict) else None
    if not isinstance(manifest, dict):
        fail("the binding has hostAliases but no runtime manifest")
    supervisor = manifest.get("supervisorConfig", {})
    artifacts = manifest.get("artifacts", [])
    hub_ca = [a for a in artifacts if a.get("role") == "hub-ca"]
    if (supervisor.get("hubOrigin") != definition["hub"]["origin"] or supervisor.get("hubCaPath") != HUB_CA_PATH
            or supervisor.get("hubCaSha256") != ca_sha or len(hub_ca) != 1 or hub_ca[0].get("sha256") != ca_sha
            or hub_ca[0].get("installPath") != HUB_CA_PATH):
        fail("the runtime's hub origin or hub CA is not relay.chifor.me with the ailab root CA "
             "(docs/relay-workers/researcher-runtime-ca.crt)")
    pinned = {a.get("installPath") for a in artifacts if a.get("sha256") == ca_sha}
    templates = manifest.get("holderProfiles", {}).get("workerTemplates") or []
    if not templates:
        fail("the runtime has hostAliases but no worker templates")
    for t in templates:
        holder = t.get("definition", {}).get("holder", {})
        for client, origin in (("router", ROUTER_ORIGIN), ("communication", definition["hub"]["origin"])):
            policy = holder.get(client) or {}
            if policy.get("origin") != origin or policy.get("caSha256") != ca_sha or policy.get("caFile") not in pinned:
                fail(f"a worker template's holder.{client} is not {origin} pinned to a packaged copy of the ailab root CA")


def with_installation_id(docs: list[dict], installation_id: str) -> list[dict]:
    """What ../pins does at build time, for the objects of the installation tree."""
    out = []
    for doc in copy.deepcopy(docs):
        kind = doc["kind"]
        if kind == "Namespace":
            doc["metadata"]["labels"]["relay.chifor.me/installation-id"] = installation_id
        elif kind == "ValidatingAdmissionPolicy":
            doc["metadata"]["name"] = f"relay-enrollment-{installation_id.lower()}"
        elif kind == "ValidatingAdmissionPolicyBinding":
            doc["metadata"]["name"] = f"relay-enrollment-{installation_id.lower()}"
            doc["spec"]["policyName"] = f"relay-enrollment-{installation_id.lower()}"
        out.append(doc)
    return out


def key(doc: dict) -> tuple:
    return (doc["kind"], doc["metadata"].get("namespace"), doc["metadata"]["name"])


def compare_objects(label: str, generated: list[dict], committed: list[dict]) -> None:
    gen, com = {key(d): d for d in generated}, {key(d): d for d in committed}
    if len(gen) != len(generated) or len(com) != len(committed):
        fail(f"{label}: duplicate object names")
    if set(gen) != set(com):
        fail(f"{label}: objects differ: only generated {sorted(set(gen) - set(com))}, "
             f"only committed {sorted(set(com) - set(gen))}")
    for k in gen:
        if gen[k] != com[k]:
            fail(f"{label}: {k} differs from the committed object")


def tool_list(bundle: Path, name: str) -> list[dict]:
    doc = json.loads((bundle / name).read_text(encoding="utf-8"))
    if doc.get("apiVersion") != "v1" or doc.get("kind") != "List" or not isinstance(doc.get("items"), list):
        fail(f"{name} is not a v1 List")
    return doc["items"]


def committed_agent() -> dict:
    docs = {}
    for name in ("rbac.yaml", "deployment.yaml", "network-policy.yaml"):
        for doc in load_yaml_docs(TREE / "agent" / name):
            docs[key(doc)] = doc
    return docs


def check_values(values: dict, definition: dict, agent: dict) -> None:
    ns = definition["identity"]["controlNamespace"]
    deployment = agent[("Deployment", ns, "relay-infrastructure-agent")]
    pod = deployment["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    volumes = {v["name"]: v for v in pod["volumes"]}
    expect = {
        "image.reference": (values["image"]["reference"], definition["agentImage"]),
        "controlNamespace": (values["controlNamespace"], ns),
        "executionSlots": (values["executionSlots"], definition["executionSlots"]),
        "replicas": (values["replicas"], deployment["spec"]["replicas"]),
        "hub": (values["hub"], {"origin": definition["hub"]["origin"], "caConfigMap": "relay-hub-ca"}),
        "kubernetes": (values["kubernetes"], {"origin": definition["kubernetesOrigin"]}),
        "identity.installationKeySecret": (values["identity"]["installationKeySecret"],
                                           volumes["installation-key"]["secret"]["secretName"]),
        "identity.enrollmentTokenSecret": (values["identity"].get("enrollmentTokenSecret", ""),
                                           volumes.get("enrollment-token", {}).get("secret", {}).get("secretName", "")),
        "policy.bootstrapSecrets": (values["policy"]["bootstrapSecrets"], False),
        "policy.persistentVolumeClaims": (values["policy"]["persistentVolumeClaims"], False),
        "podRuntime.configMap": (values["podRuntime"]["configMap"], ""),
        "candidateRuntime.configMap": (values["candidateRuntime"]["configMap"], ""),
        "serviceAccountTokenSeconds": (
            values["serviceAccountTokenSeconds"],
            volumes["kubernetes"]["projected"]["sources"][0]["serviceAccountToken"]["expirationSeconds"]),
        "resources": (values["resources"], container["resources"]),
        "nodeSelector": (values["nodeSelector"], pod["nodeSelector"]),
        "tolerations": (values["tolerations"], pod["tolerations"]),
        "networkPolicy.enabled": (values["networkPolicy"]["enabled"], True),
        "networkPolicy.apiServerCidrs": (values["networkPolicy"]["apiServerCidrs"],
                                         definition["controlNetwork"]["apiServerCidrs"]),
        "networkPolicy.hubCidrs": (values["networkPolicy"]["hubCidrs"], definition["controlNetwork"]["hubCidrs"]),
    }
    for name, (got, want) in expect.items():
        if got != want:
            fail(f"30-agent-values.json {name} = {got!r}, the committed agent has {want!r}")
    if values.get("ordinaryNamespacePins") not in (None,):
        fail("30-agent-values.json sets ordinaryNamespacePins; enrollment-only values do not")


def check_config(config: dict, definition: dict) -> None:
    committed = json.loads((TREE / "agent/config.json").read_text(encoding="utf-8"))

    def walk(expected, actual, path):
        if is_placeholder(expected):
            if is_placeholder(actual):
                fail(f"31-agent-config.json {path} is a placeholder")
            return
        if isinstance(expected, dict):
            if not isinstance(actual, dict):
                fail(f"31-agent-config.json {path} is not an object")
            allowed_extra = {"/identity": {"fenceObjectUid"}}.get(path, set())
            allowed_missing = {"/identity": {"enrollmentTokenFile"}}.get(path, set())
            if (set(actual) - set(expected) - allowed_extra) or (set(expected) - set(actual) - allowed_missing):
                fail(f"31-agent-config.json {path or '/'} keys differ from the committed config")
            for k in expected:
                if k in actual:
                    walk(expected[k], actual[k], f"{path}/{k}")
            return
        if expected != actual:
            fail(f"31-agent-config.json {path} = {actual!r}, committed {expected!r}")

    walk(committed, config, "")
    identity = config["identity"]
    for field in ("installationId", "installationPublicKey", "tenantId", "clusterUid", "controlNamespace"):
        if identity[field] != definition["identity"][field]:
            fail(f"31-agent-config.json identity.{field} differs from the definition")
    if identity.get("fenceObjectUid") != definition["identity"].get("fenceObjectUid"):
        fail("31-agent-config.json identity.fenceObjectUid differs from the definition")


def check_policy(config: dict, values: dict, definition: dict, report: dict) -> None:
    """The release-derived agent policy is the one leaf the committed config cannot pin, so join it
    to the definition it came from. The tool writes the same policy into 31-agent-config.json and
    30-agent-values.json, takes `installation` verbatim from the definition, and maps podTiers and
    allowedTemplates from the binding's placements and templates; report.json repeats the identity."""
    policy, binding = config["policy"], definition["binding"]
    if not isinstance(binding, dict):
        fail("the definition's binding is not an object")
    if policy["installation"] != definition["installationPolicy"]:
        fail("31-agent-config.json policy.installation differs from the definition's installationPolicy")
    for field in ("installation", "podTiers", "allowedRuntimeDigests", "allowedTemplates", "retainedStorageClass",
                  "bootstrapSecrets"):
        if values["policy"][field] != policy[field]:
            fail(f"30-agent-values.json policy.{field} differs from 31-agent-config.json")
    tiers = [{"tier": p.get("tier"), "binding": p.get("binding"), "runtimeClassName": p.get("runtimeClassName")}
             for p in binding.get("placements", [])]
    if not tiers or policy["podTiers"] != tiers:
        fail("policy.podTiers is not the binding's placements")
    if {t["runtimeClassName"] for t in tiers} != {"relay-gvisor"}:
        fail("every researcher placement must use RuntimeClass relay-gvisor")
    class_selector, class_tolerations = runtime_class_scheduling()
    for p in binding["placements"]:
        if p.get("overhead") != {"milliCpu": 80, "memoryMiB": 24}:
            fail("a placement's overhead is not relay-gvisor's 80m / 24Mi (the slot quota assumes it)")
        selector = p.get("nodeSelector", {})
        if selector.get("kubernetes.io/hostname") != "relay-worker-1" or \
                selector.get("ailab.io/relay-worker-pool") != "true":
            fail("a placement does not pin kubernetes.io/hostname relay-worker-1 on the relay worker pool")
        if any(selector.get(k) != v for k, v in class_selector.items()):
            fail(f"a placement's nodeSelector lacks relay-gvisor's scheduling.nodeSelector {class_selector}: "
                 "RuntimeClass admission would add it and Relay's exact selector pin would refuse the Pod")
        if p.get("tolerations") != class_tolerations:
            fail(f"a placement's tolerations are not relay-gvisor's scheduling.tolerations {class_tolerations}")
    templates = [{"templateId": t.get("definition", {}).get("templateId"),
                  "revision": t.get("definition", {}).get("revision"), "digest": t.get("digest")}
                 for t in binding.get("templates", [])]
    if not templates or policy["allowedTemplates"] != templates:
        fail("policy.allowedTemplates is not the binding's templates")
    digests = policy["allowedRuntimeDigests"]
    if not (isinstance(digests, list) and len(digests) == 1 and re.match(r"^[0-9a-f]{64}$", str(digests[0]))):
        fail("policy.allowedRuntimeDigests is not exactly one runtime digest")
    if binding.get("installationId") != definition["identity"]["installationId"]:
        fail("the binding names another installation")
    image = binding.get("runtime", {}).get("image", "")
    if not re.match(r"^registry\.chifor\.me/[a-z0-9/._-]+@sha256:[0-9a-f]{64}$", image) or \
            image.rsplit(":", 1)[1] not in definition["installationPolicy"]["allowedImageDigests"]:
        fail("the binding's runtime image is not a digest-pinned registry.chifor.me image allowed by the policy")
    ident = report.get("identity", {})
    expected = {"sourceCommit": definition["sourceCommit"], "agentImage": definition["agentImage"],
                "installationId": definition["identity"]["installationId"],
                "clusterUid": definition["identity"]["clusterUid"], "runtimeImage": image,
                "runtimeDigest": digests[0], "policyDigest": binding.get("policyDigest")}
    for field, want in expected.items():
        if ident.get(field) != want:
            fail(f"report.json identity.{field} differs from the definition")
    for field in ("tenantId", "installationId", "clusterUid", "installationPublicKey"):
        if values["identity"][field] != definition["identity"][field]:
            fail(f"30-agent-values.json identity.{field} differs from the definition")
    if values["identity"]["fenceObjectUid"] != definition["identity"].get("fenceObjectUid", ""):
        fail("30-agent-values.json identity.fenceObjectUid differs from the definition")


def chart_compare(chart_root: Path, bundle: Path, definition: dict, agent: dict) -> None:
    chart = chart_root / "deploy/infrastructure-agent"
    if not (chart / "Chart.yaml").is_file():
        fail(f"{chart} is not the Relay infrastructure-agent chart")
    head = subprocess.run(["git", "-C", str(chart_root), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    if head != definition["sourceCommit"]:
        fail(f"--chart checkout is at {head}, the definition pins {definition['sourceCommit']}")
    dirty = subprocess.run(["git", "-C", str(chart_root), "status", "--porcelain", "--", "deploy/infrastructure-agent"],
                           check=True, capture_output=True, text=True).stdout.strip()
    if dirty:
        fail("--chart checkout has local changes under deploy/infrastructure-agent")
    ns = definition["identity"]["controlNamespace"]
    rendered = subprocess.run(
        ["helm", "template", CHART_RELEASE, str(chart), "--namespace", ns, "-f", str(bundle / "30-agent-values.json")],
        check=True, capture_output=True, text=True).stdout
    docs = {key(d): d for d in yaml.safe_load_all(rendered) if d}
    config_key = ("ConfigMap", ns, "relay-infrastructure-agent-config")
    chart_config = json.loads(docs.pop(config_key)["data"]["config.json"])
    if chart_config != json.loads((bundle / "31-agent-config.json").read_text(encoding="utf-8")):
        fail("the chart's rendered config.json differs from 31-agent-config.json")
    committed = copy.deepcopy(agent)
    committed.pop(("CiliumNetworkPolicy", ns, "relay-infrastructure-agent-api-egress"))  # ailab addition
    deployment = committed[("Deployment", ns, "relay-infrastructure-agent")]
    deployment["spec"]["template"]["spec"]["containers"][0]["image"] = definition["agentImage"]
    # The two documented differences: config rollout by hashed ConfigMap name instead of the
    # config-digest annotation, and ailab's source-commit annotation.
    deployment["spec"]["template"]["metadata"].pop("annotations")
    chart_deployment = docs[("Deployment", ns, "relay-infrastructure-agent")]
    chart_deployment["spec"]["template"]["metadata"].pop("annotations", None)
    compare_objects("chart render", list(docs.values()), list(committed.values()))


def check(bundle: Path, chart: Path | None) -> dict:
    manifest = json.loads((bundle / "bundle-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("kind") != "relay-installation-preparation-bundle" or manifest.get("workloadCreationEnabled"):
        fail("bundle-manifest.json is not an enrollment-only installation bundle")
    if sorted(manifest["files"]) != sorted(BUNDLE_FILES):
        fail(f"bundle files differ: {sorted(manifest['files'])}")
    for name, digest in manifest["files"].items():
        if hashlib.sha256((bundle / name).read_bytes()).hexdigest() != digest:
            fail(f"{name} does not match bundle-manifest.json")
    report = json.loads((bundle / "report.json").read_text(encoding="utf-8"))
    if report.get("status") != "enrollment-only-worker-creation-disabled" or report.get("workloadCreationEnabled"):
        fail("report.json is not enrollment-only")

    definition = json.loads((bundle / "00-definition.json").read_text(encoding="utf-8"))
    compare_definition(template(), definition)
    installation_id = definition["identity"]["installationId"]
    if not UUID.match(installation_id):
        fail("the definition's installationId is not a lowercase UUID")
    if not COMMIT.match(definition["sourceCommit"]) or not AGENT_IMAGE.match(definition["agentImage"]):
        fail("the definition's sourceCommit or agentImage is not a reviewed pin shape")
    p = pins()
    if pins_filled(p) and (p["installationId"], p["agentImage"], p["sourceCommit"]) != (
            installation_id, definition["agentImage"], definition["sourceCommit"]):
        fail("kubernetes/apps/relay-researcher/pins/pins.yaml differs from the bundle's definition")

    installation = TREE / "installation"
    compare_objects("10-namespaces.json", tool_list(bundle, "10-namespaces.json"),
                    with_installation_id(load_yaml_docs(installation / "namespaces.yaml"), installation_id))
    compare_objects("20-containment.json", tool_list(bundle, "20-containment.json"),
                    load_yaml_docs(installation / "containment.yaml"))
    compare_objects("21-admission-deny.json", tool_list(bundle, "21-admission-deny.json"),
                    with_installation_id(load_yaml_docs(installation / "admission-deny.yaml"), installation_id))
    stage, egress_path = egress_stage()
    (egress,) = load_yaml_docs(egress_path)
    artifacts = [{"namespace": egress["metadata"]["namespace"], "name": egress["metadata"]["name"],
                  "sha256": measurement_digest(egress)}]
    if definition["executionEgressArtifacts"] != artifacts:
        fail(f"executionEgressArtifacts does not name the included {stage}-stage allow-list and its digest")
    check_internal_tls(definition, stage)

    agent = committed_agent()
    values = json.loads((bundle / "30-agent-values.json").read_text(encoding="utf-8"))
    config = json.loads((bundle / "31-agent-config.json").read_text(encoding="utf-8"))
    check_values(values, definition, agent)
    check_config(config, definition)
    check_policy(config, values, definition, report)
    if chart is not None:
        chart_compare(chart, bundle, definition, agent)
    return definition


def write_pins(definition: dict) -> None:
    path = TREE / "pins/pins.yaml"
    text = path.read_text(encoding="utf-8")
    for field, value in (("installationId", definition["identity"]["installationId"]),
                         ("agentImage", definition["agentImage"]),
                         ("sourceCommit", definition["sourceCommit"])):
        # Quoted: an all-digit commit would otherwise read back as a YAML integer.
        text, n = re.subn(rf"^(  {field}: ).*$", lambda m: m.group(1) + json.dumps(value), text, flags=re.M)
        if n != 1:
            fail(f"pins.yaml has no single {field} line")
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "import"):
        sp = sub.add_parser(name)
        sp.add_argument("bundle", type=Path)
        # import never runs without the chart render: it is the only step that writes.
        sp.add_argument("--chart", type=Path, required=name == "import",
                        help="exact pinned Relay checkout to render the chart from")
    args = parser.parse_args(argv)
    try:
        definition = check(args.bundle, args.chart)
        if args.command == "import":
            write_pins(definition)
            target = TREE / "agent/config.json"
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
                tmp.write((args.bundle / "31-agent-config.json").read_bytes())
            shutil.move(tmp.name, target)
            print("imported: pins/pins.yaml and agent/config.json; review and commit the diff")
        else:
            print("bundle matches the reviewed installation"
                  + ("" if pins_filled(pins()) else "; pins are not filled yet (run import)"))
        return 0
    except Mismatch as error:
        print(f"relay-researcher-bundle: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
