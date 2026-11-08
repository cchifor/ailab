"""Deterministic render of the pinned KubeVirt v1.9.1 operator + CR for the dedicated spike pool.

Input: the exact upstream downloads named in docs/relay-workers/runtime-pins.json
(kubevirt.spike_bundle.sources). Any hash mismatch, unknown resource, tag-only image or placement
outside `ailab.io/relay-worker-pool=true` + `dedicated=relay-worker:NoSchedule` raises BundleError.
"""
from __future__ import annotations

import copy
import hashlib
import pathlib
import re

import yaml

POOL_LABEL, POOL_VALUE = "ailab.io/relay-worker-pool", "true"
TOLERATION = {"key": "dedicated", "operator": "Equal", "value": "relay-worker", "effect": "NoSchedule"}
FORBIDDEN_NODE_LABELS = ("ailab.io/agent-pool", "node-role.kubernetes.io/control-plane",
                         "node-role.kubernetes.io/master")
DIGEST_IMAGE = re.compile(r"^[a-z0-9.\-]+(/[a-z0-9._\-]+)+@sha256:[0-9a-f]{64}$")
# (kind, name) exactly as shipped in kubevirt-operator.yaml v1.9.1. Anything else is refused.
EXPECTED = {("Namespace", "kubevirt"), ("CustomResourceDefinition", "kubevirts.kubevirt.io"),
            ("PriorityClass", "kubevirt-cluster-critical"), ("ClusterRole", "kubevirt.io:operator"),
            ("ServiceAccount", "kubevirt-operator"), ("Role", "kubevirt-operator"),
            ("RoleBinding", "kubevirt-operator-rolebinding"), ("ClusterRole", "kubevirt-operator"),
            ("ClusterRoleBinding", "kubevirt-operator"), ("Deployment", "virt-operator")}
# Every image env read by v1.9.1 pkg/virt-operator/util/config.go getConfig(); none may stay unset,
# or the operator derives `<registry>/<name>:<tag>` from KUBEVIRT_VERSION (a mutable tag).
REQUIRED_ENVS = ("VIRT_OPERATOR_IMAGE", "VIRT_API_IMAGE", "VIRT_CONTROLLER_IMAGE", "VIRT_HANDLER_IMAGE",
                 "VIRT_LAUNCHER_IMAGE", "VIRT_EXPORTPROXY_IMAGE", "VIRT_EXPORTSERVER_IMAGE",
                 "VIRT_SYNCHRONIZATIONCONTROLLER_IMAGE", "GS_IMAGE", "PR_HELPER_IMAGE",
                 "SIDECAR_SHIM_IMAGE", "VIRT_TEMPLATE_APISERVER_IMAGE", "VIRT_TEMPLATE_CONTROLLER_IMAGE")
# Beta gates (on by default in v1.9.1) that add components or features outside the spike's bounds.
DISABLED_GATES = ["DeclarativeHotplugVolumes", "ImageVolume", "Snapshot", "Template"]
SPIKE_LABEL = {"ailab.io/spike": "relay-kubevirt"}


class BundleError(ValueError):
    pass


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_sources(bundle_pins: dict, source_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    out = {}
    for src in bundle_pins["sources"]:
        path = source_dir / src["file"]
        if not path.is_file():
            raise BundleError(f"missing pinned source {src['file']}")
        got = sha256_file(path)
        if got != src["sha256"]:
            raise BundleError(f"sha256 mismatch for {src['file']}: {got} != pinned {src['sha256']}")
        out[src["file"]] = path
    return out


def component_env(bundle_pins: dict) -> list[dict]:
    comps = {c["env"]: c["image"] for c in bundle_pins["components"]}
    missing = [e for e in REQUIRED_ENVS if e not in comps]
    extra = sorted(set(comps) - set(REQUIRED_ENVS))
    if missing or extra:
        raise BundleError(f"component pins incomplete: missing={missing} unknown={extra}")
    for env, image in comps.items():
        if not DIGEST_IMAGE.match(image):
            raise BundleError(f"{env} is not digest-pinned: {image}")
    return [{"name": e, "value": comps[e]} for e in REQUIRED_ENVS]


def dedicated_placement() -> dict:
    """Shared by the operator pod and KubeVirt spec.infra/spec.workloads nodePlacement."""
    exprs = [{"key": POOL_LABEL, "operator": "In", "values": [POOL_VALUE]}]
    exprs += [{"key": k, "operator": "DoesNotExist"} for k in FORBIDDEN_NODE_LABELS]
    return {"nodeSelector": {"kubernetes.io/os": "linux", POOL_LABEL: POOL_VALUE},
            "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution":
                                          {"nodeSelectorTerms": [{"matchExpressions": exprs}]}}},
            "tolerations": [dict(TOLERATION)]}


def _patch_operator(dep: dict, env_pins: list[dict]) -> None:
    operator_image = env_pins[0]["value"]
    spec = dep["spec"]
    spec["replicas"] = 1  # one dedicated node: a second replica could only Pending or escape the pool
    pod = spec["template"]["spec"]
    containers = pod["containers"]
    if len(containers) != 1 or containers[0]["name"] != "virt-operator" or pod.get("initContainers"):
        raise BundleError("unexpected virt-operator container layout")
    c = containers[0]
    c["image"] = operator_image
    kept = [e for e in c.get("env", []) if e["name"] in ("WATCH_NAMESPACE", "KUBEVIRT_VERSION")]
    if [e["name"] for e in kept] != ["WATCH_NAMESPACE", "KUBEVIRT_VERSION"]:
        raise BundleError("upstream virt-operator env changed; re-review the source")
    c["env"] = env_pins + kept
    placement = dedicated_placement()
    pod["nodeSelector"] = placement["nodeSelector"]
    pod["tolerations"] = placement["tolerations"]
    anti = pod.get("affinity", {}).get("podAntiAffinity")
    pod["affinity"] = placement["affinity"] | ({"podAntiAffinity": anti} if anti else {})


def render_operator(text: str, env_pins: list[dict]) -> list[dict]:
    docs = [d for d in yaml.safe_load_all(text) if d]
    seen = {(d.get("kind"), d.get("metadata", {}).get("name")) for d in docs}
    if seen != EXPECTED or len(docs) != len(EXPECTED):
        raise BundleError(f"unsupported/missing operator resources: +{sorted(seen - EXPECTED)} "
                          f"-{sorted(EXPECTED - seen)}")
    out = []
    for d in docs:
        d = copy.deepcopy(d)
        if d["kind"] == "Namespace":
            # PSA privileged is upstream's requirement: virt-handler is a privileged DaemonSet.
            d["metadata"]["labels"] = d["metadata"].get("labels", {}) | SPIKE_LABEL
        if d["kind"] == "Deployment":
            _patch_operator(d, env_pins)
        out.append(d)
    return out


def render_cr(text: str) -> dict:
    docs = [d for d in yaml.safe_load_all(text) if d]
    if len(docs) != 1 or docs[0].get("kind") != "KubeVirt" or docs[0]["metadata"].get("namespace") != "kubevirt":
        raise BundleError("unexpected kubevirt-cr.yaml content")
    cr = copy.deepcopy(docs[0])
    spec = cr["spec"]
    dev = spec["configuration"].setdefault("developerConfiguration", {})
    if dev.get("featureGates") or "useEmulation" in dev:
        raise BundleError("upstream CR enables gates or emulation; re-review")
    dev.update({"featureGates": [], "disabledFeatureGates": list(DISABLED_GATES)})
    spec["configuration"]["evictionStrategy"] = "None"  # no live migration in the spike
    spec["infra"] = {"replicas": 1, "nodePlacement": dedicated_placement()}
    spec["workloads"] = {"nodePlacement": dedicated_placement()}
    cr["metadata"]["labels"] = SPIKE_LABEL | cr["metadata"].get("labels", {})
    return cr


def check_pod_placement(where: str, pod: dict) -> None:
    """Post-render guard: a pod/nodePlacement must select only the dedicated pool."""
    want = dedicated_placement()
    if pod.get("nodeSelector") != want["nodeSelector"] or pod.get("tolerations") != want["tolerations"]:
        raise BundleError(f"{where}: placement escapes the dedicated pool")
    if pod.get("affinity", {}).get("nodeAffinity") != want["affinity"]["nodeAffinity"]:
        raise BundleError(f"{where}: node affinity escapes the dedicated pool")


def check_rendered(operator_docs: list[dict], cr: dict) -> None:
    check_cr_schema(operator_docs, cr)
    for d in operator_docs:
        if d["kind"] == "Deployment":
            pod = d["spec"]["template"]["spec"]
            check_pod_placement("virt-operator", pod)
            if any(not DIGEST_IMAGE.match(c["image"]) for c in pod["containers"]):
                raise BundleError("virt-operator container image is not digest-pinned")
    for part in ("infra", "workloads"):
        check_pod_placement(f"KubeVirt spec.{part}", cr["spec"][part]["nodePlacement"])


def check_schema_paths(value, schema: dict, path: str) -> None:
    """Reject unknown rendered paths using the pinned structural CRD schema.

    This checks fields/types, not Kubernetes admission/CEL/defaulting. Arbitrary
    maps and preserve-unknown subtrees retain their declared CRD semantics.
    """
    if schema.get("x-kubernetes-preserve-unknown-fields"):
        return
    if isinstance(value, dict):
        if schema.get("type") != "object":
            raise BundleError(f"{path}: object not declared in KubeVirt CRD")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", False)
        for key, child in value.items():
            rule = props.get(key, extra)
            if rule is True:
                continue
            if not isinstance(rule, dict):
                raise BundleError(f"{path}.{key}: unknown field in KubeVirt CRD")
            check_schema_paths(child, rule, f"{path}.{key}")
    elif isinstance(value, list):
        if schema.get("type") != "array" or not isinstance(schema.get("items"), dict):
            raise BundleError(f"{path}: array not declared in KubeVirt CRD")
        for index, child in enumerate(value):
            check_schema_paths(child, schema["items"], f"{path}[{index}]")
    else:
        kind = schema.get("type")
        valid = {"string": isinstance(value, str), "boolean": isinstance(value, bool),
                 "integer": type(value) is int, "number": type(value) in (int, float)}
        if schema.get("x-kubernetes-int-or-string") and type(value) in (int, str):
            return
        if not valid.get(kind, False):
            raise BundleError(f"{path}: value type differs from KubeVirt CRD")
        if "enum" in schema and value not in schema["enum"]:
            raise BundleError(f"{path}: value not allowed by KubeVirt CRD")


def check_cr_schema(operator_docs: list[dict], cr: dict) -> None:
    crd = next((d for d in operator_docs if d.get("kind") == "CustomResourceDefinition"
                and d.get("metadata", {}).get("name") == "kubevirts.kubevirt.io"), {})
    version = cr.get("apiVersion", "").split("/")[-1]
    schemas = [v.get("schema", {}).get("openAPIV3Schema", {})
               for v in crd.get("spec", {}).get("versions", [])
               if v.get("name") == version and v.get("served")]
    if len(schemas) != 1 or "spec" not in schemas[0].get("properties", {}):
        raise BundleError("pinned KubeVirt CRD has no served spec schema")
    check_schema_paths(cr["spec"], schemas[0]["properties"]["spec"], "KubeVirt.spec")
