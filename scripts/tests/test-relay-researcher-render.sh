#!/bin/bash
# Renders the first Relay researcher installation (kubernetes/apps/relay-researcher) under the SAME
# digest-pinned kustomize image as scripts/manifest-lint.sh, then validates the renders with its
# kubeconform. Neither tree is a Flux path today, which is why manifest-lint.sh does not build them.
# Proves:
#   * installation/ builds: exactly the two namespaces (restricted PSA, retention), their quotas,
#     limits and default-deny, the slot's one allow-list and the deny gate, with the pins component's
#     values substituted (placeholders today) and no pins ConfigMap in the output;
#   * BOTH egress stages build (owner decision G3): the tree as committed, and a copy of it with the
#     other stage selected, each rendering exactly that stage's relay-researcher-egress and nothing
#     else different;
#   * agent/ builds: the agent objects, the hashed config ConfigMap equal byte for byte to
#     agent/config.json, the immutable hashed trust ConfigMap equal byte for byte to the pinned Mozilla
#     bundle (sha256 427c0642...), the Deployment's volumes following both hashed names, and the image
#     and source annotation coming from the pins.
# The hub upgrade components have their own render test (test-relay-researcher-hub-render.sh).
# Requires docker. Exit non-zero on the first broken expectation.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KUSTOMIZE_IMAGE="$(sed -n 's/^KUSTOMIZE_IMAGE="\(.*\)"$/\1/p' "$REPO_ROOT/scripts/manifest-lint.sh")"
KUBECONFORM_IMAGE="$(sed -n 's/^KUBECONFORM_IMAGE="\(.*\)"$/\1/p' "$REPO_ROOT/scripts/manifest-lint.sh")"
[ -n "$KUSTOMIZE_IMAGE" ] && [ -n "$KUBECONFORM_IMAGE" ] || { echo "cannot find the pinned images in scripts/manifest-lint.sh" >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/out"
build() { # <dir-in-container> <output>
  docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w / "$KUSTOMIZE_IMAGE" build "$1" > "$2"
}
build /repo/kubernetes/apps/relay-researcher/installation "$WORK/out/installation.yaml"
build /repo/kubernetes/apps/relay-researcher/agent "$WORK/out/agent.yaml"
# The other egress stage: a copy of the tree with the one stage line switched.
ALT="$WORK/alt/relay-researcher"
mkdir -p "$WORK/alt"
cp -r "$REPO_ROOT/kubernetes/apps/relay-researcher" "$ALT"
if grep -qx -- '- execution-egress.yaml' "$ALT/installation/kustomization.yaml"; then
  sed -i 's#^- execution-egress.yaml$#- qualification-egress.yaml#' "$ALT/installation/kustomization.yaml"
else
  sed -i 's#^- qualification-egress.yaml$#- execution-egress.yaml#' "$ALT/installation/kustomization.yaml"
fi
chmod -R a+rX "$WORK"
docker run --rm --network none -v "$WORK/alt:/alt:ro" -w / "$KUSTOMIZE_IMAGE" build /alt/relay-researcher/installation \
  > "$WORK/out/installation-other-stage.yaml"
chmod -R a+rX "$WORK"

docker run --rm -v "$WORK/out:/out:ro" "$KUBECONFORM_IMAGE" \
  -strict -summary -ignore-missing-schemas \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  -skip Secret \
  /out/installation.yaml /out/installation-other-stage.yaml /out/agent.yaml

PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
"$PY" - "$WORK/out" "$REPO_ROOT" <<'PY'
import copy, hashlib, json, sys, yaml
out, repo = sys.argv[1:3]
tree = f"{repo}/kubernetes/apps/relay-researcher"

def load(name):
    return {(d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"]): d
            for d in yaml.safe_load_all(open(f"{out}/{name}", encoding="utf-8")) if d}

pins = yaml.safe_load(open(f"{tree}/pins/pins.yaml", encoding="utf-8"))["data"]
iid = pins["installationId"]

# --- installation stage
inst = load("installation.yaml")
gate = f"relay-enrollment-{iid}"
expected = {
    ("Namespace", None, "relay-infra-control"), ("Namespace", None, "relay-researcher-1"),
    ("ResourceQuota", "relay-infra-control", "relay-enrollment-quota"),
    ("ResourceQuota", "relay-researcher-1", "relay-enrollment-quota"),
    ("LimitRange", "relay-infra-control", "relay-enrollment-limits"),
    ("LimitRange", "relay-researcher-1", "relay-enrollment-limits"),
    ("NetworkPolicy", "relay-infra-control", "relay-default-deny"),
    ("NetworkPolicy", "relay-researcher-1", "relay-default-deny"),
    ("NetworkPolicy", "relay-researcher-1", "relay-researcher-egress"),
    ("ValidatingAdmissionPolicy", None, gate), ("ValidatingAdmissionPolicyBinding", None, gate),
}
assert set(inst) == expected, f"installation objects {sorted(set(inst) ^ expected)}"
for ns in ("relay-infra-control", "relay-researcher-1"):
    labels = inst[("Namespace", None, ns)]["metadata"]["labels"]
    assert labels["relay.chifor.me/installation-id"] == iid, labels
    for mode in ("enforce", "warn", "audit"):
        assert labels[f"pod-security.kubernetes.io/{mode}"] == "restricted"
assert inst[("ValidatingAdmissionPolicyBinding", None, gate)]["spec"]["policyName"] == gate
assert inst[("ResourceQuota", "relay-researcher-1", "relay-enrollment-quota")]["spec"]["hard"]["pods"] == "0"
print(f"installation: {len(inst)} objects, gate {gate}, slot Pod quota 0")

# --- the two egress stages: each build renders exactly its stage's policy, nothing else differs
other = load("installation-other-stage.yaml")
assert set(other) == set(inst), sorted(set(other) ^ set(inst))
key = ("NetworkPolicy", "relay-researcher-1", "relay-researcher-egress")
for k in inst:
    if k != key:
        assert inst[k] == other[k], f"switching the egress stage changed {k}"
stages = {}
for stage, name in (("enrollment", "execution-egress.yaml"), ("qualification", "qualification-egress.yaml")):
    (doc,) = [d for d in yaml.safe_load_all(open(f"{tree}/installation/{name}", encoding="utf-8")) if d]
    stages[stage] = doc
rendered = sorted([inst[key], other[key]], key=lambda d: json.dumps(d, sort_keys=True))
assert rendered == sorted(stages.values(), key=lambda d: json.dumps(d, sort_keys=True)), "a stage rendered differently"
assert "ipBlock" not in json.dumps(stages["qualification"]) and "kube-dns" not in json.dumps(stages["qualification"])
print("egress stages: enrollment and qualification both render, and only relay-researcher-egress differs")

# --- agent stage
agent = load("agent.yaml")
ns = "relay-infra-control"
[cfg_key] = [k for k in agent if k[0] == "ConfigMap" and k[2].startswith("relay-infrastructure-agent-config-")]
[ca_key] = [k for k in agent if k[0] == "ConfigMap" and k[2].startswith("relay-hub-ca-")]
assert agent[cfg_key]["data"]["config.json"] == open(f"{tree}/agent/config.json", encoding="utf-8").read()
ca = agent[ca_key]
assert ca.get("immutable") is True
assert hashlib.sha256(ca["data"]["ca.crt"].encode("utf-8")).hexdigest() == \
    "427c064216761a1e72a8b30f0d954d08cb6e4435a82091fb865afd20c8497285", "trust bundle bytes changed"
dep = agent[("Deployment", ns, "relay-infrastructure-agent")]
pod = dep["spec"]["template"]
vols = {v["name"]: v for v in pod["spec"]["volumes"]}
assert vols["config"]["configMap"]["name"] == cfg_key[2] and vols["hub-ca"]["configMap"]["name"] == ca_key[2]
assert pod["spec"]["containers"][0]["image"] == pins["agentImage"]
assert pod["metadata"]["annotations"]["chifor.me/source-commit"] == pins["sourceCommit"]
assert not [k for k in agent if k[0] == "ConfigMap" and k[2] == "relay-researcher-pins"]
assert {k[0] for k in agent} == {"ServiceAccount", "Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding",
                                 "ConfigMap", "Deployment", "NetworkPolicy", "CiliumNetworkPolicy", "PrometheusRule"}
print(f"agent: {len(agent)} objects; config and trust follow their hashed names")
PY
echo "test-relay-researcher-render: OK"
