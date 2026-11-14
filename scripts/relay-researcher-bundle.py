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
         execution egress artifact digest against the committed allow-list; 30-agent-values.json and
         31-agent-config.json against the committed agent objects and config; pins already filled
         against the definition. With --chart it also renders that checkout's
         deploy/infrastructure-agent with 30-agent-values.json (helm template, no cluster) and
         compares every rendered object with the committed mirror. Exit 1 on the first mismatch.
  import <bundle-dir>
         Runs check, then writes the bundle's installationId, agentImage and sourceCommit into
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
        "identity.enrollmentTokenSecret": (values["identity"]["enrollmentTokenSecret"],
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
    (egress,) = load_yaml_docs(installation / "execution-egress.yaml")
    artifacts = [{"namespace": egress["metadata"]["namespace"], "name": egress["metadata"]["name"],
                  "sha256": measurement_digest(egress)}]
    if definition["executionEgressArtifacts"] != artifacts:
        fail("executionEgressArtifacts does not name the committed allow-list and its digest")

    agent = committed_agent()
    check_values(json.loads((bundle / "30-agent-values.json").read_text(encoding="utf-8")), definition, agent)
    check_config(json.loads((bundle / "31-agent-config.json").read_text(encoding="utf-8")), definition)
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
        if name == "check":
            sp.add_argument("--chart", type=Path, help="exact pinned Relay checkout to render the chart from")
    args = parser.parse_args(argv)
    try:
        definition = check(args.bundle, getattr(args, "chart", None))
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
