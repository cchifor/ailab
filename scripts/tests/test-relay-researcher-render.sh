#!/bin/bash
# Renders the first Relay researcher installation (kubernetes/apps/relay-researcher) and the hub
# upgrade components onto the live Relay overlay under the SAME digest-pinned kustomize image as
# scripts/manifest-lint.sh, then validates the renders with its kubeconform. None of these paths is a
# Flux path today, which is why manifest-lint.sh does not build them. Proves:
#   * installation/ builds: exactly the two namespaces (restricted PSA, retention), their quotas,
#     limits and default-deny, the slot's one allow-list and the deny gate, with the pins component's
#     values substituted (placeholders today) and no pins ConfigMap in the output;
#   * agent/ builds: the agent objects, the hashed config ConfigMap equal byte for byte to
#     agent/config.json, the immutable hashed trust ConfigMap equal byte for byte to the pinned Mozilla
#     bundle (sha256 427c0642...), the Deployment's volumes following both hashed names, and the image
#     and source annotation coming from the pins;
#   * each hub component (relay-infrastructure-installations with relay-restore-fencing,
#     relay-worker-profiles with relay-worker-budget-client, relay-agent-control-plane) applies to the
#     current Relay Deployment and changes nothing but the relay container's env, mounts and added
#     volumes plus its own generated ConfigMap;
#   * the whole H3-H6 set together (those five plus relay-router-connection, relay-control-monitoring
#     and relay-worker-monitoring) builds with no env, volume or mount collision.
# Requires docker. Exit non-zero on the first broken expectation.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KUSTOMIZE_IMAGE="$(sed -n 's/^KUSTOMIZE_IMAGE="\(.*\)"$/\1/p' "$REPO_ROOT/scripts/manifest-lint.sh")"
KUBECONFORM_IMAGE="$(sed -n 's/^KUBECONFORM_IMAGE="\(.*\)"$/\1/p' "$REPO_ROOT/scripts/manifest-lint.sh")"
[ -n "$KUSTOMIZE_IMAGE" ] && [ -n "$KUBECONFORM_IMAGE" ] || { echo "cannot find the pinned images in scripts/manifest-lint.sh" >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# name -> components layered on the live relay overlay
declare -A CASES=(
  [plain]=""
  [installations]="relay-restore-fencing relay-infrastructure-installations"
  [profiles]="relay-worker-budget-client relay-worker-profiles"
  [controlplane]="relay-agent-control-plane"
  [all]="relay-restore-fencing relay-infrastructure-installations relay-worker-budget-client relay-worker-profiles relay-router-connection relay-agent-control-plane relay-control-monitoring relay-worker-monitoring"
)
for case in "${!CASES[@]}"; do
  mkdir -p "$WORK/$case"
  {
    printf 'resources: [../../repo/kubernetes/apps/apps/relay]\n'
    if [ -n "${CASES[$case]}" ]; then
      printf 'components:\n'
      for c in ${CASES[$case]}; do printf -- '- ../../repo/kubernetes/components/%s\n' "$c"; done
    fi
  } > "$WORK/$case/kustomization.yaml"
done
chmod -R a+rX "$WORK"

build() { # <dir-in-container> <output>
  docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -v "$WORK:/work:ro" -w / \
    "$KUSTOMIZE_IMAGE" build "$1" > "$2"
}
mkdir -p "$WORK/out"
build /repo/kubernetes/apps/relay-researcher/installation "$WORK/out/installation.yaml"
build /repo/kubernetes/apps/relay-researcher/agent "$WORK/out/agent.yaml"
for case in "${!CASES[@]}"; do
  build "/work/$case" "$WORK/out/relay-$case.yaml"
done
chmod -R a+rX "$WORK/out"

docker run --rm -v "$WORK/out:/out:ro" "$KUBECONFORM_IMAGE" \
  -strict -summary -ignore-missing-schemas \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  -skip Secret \
  /out/installation.yaml /out/agent.yaml /out/relay-all.yaml

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

# --- hub components on the live Relay Deployment
CASES = {  # case -> (env names, volume names, added (kind, name prefix))
    "installations": ({"RELAY_INFRASTRUCTURE_INSTALLATIONS", "RELAY_RECOVERY_GENERATION_FILE"},
                      {"restore-generation"}, set()),
    "profiles": ({"RELAY_WORKER_PROFILES_FILE", "RELAY_WORKER_PROFILES_SHA256", "RELAY_WORKER_BUDGET_DSN_FILE",
                  "RELAY_WORKER_BUDGET_CA_FILE", "RELAY_WORKER_BUDGET_WITNESS_FILE"},
                 {"worker-profiles", "worker-budget"}, {("ConfigMap", "relay-worker-profiles-")}),
    "controlplane": ({"RELAY_AGENT_CONTROL_PLANE"}, set(), set()),
}
key = ("Deployment", "relay", "relay")
plain = load("relay-plain.yaml")
def relay_container(doc):
    [c] = [c for c in doc["spec"]["template"]["spec"]["containers"] if c["name"] == "relay"]
    return c
for case, (env_names, volumes, added) in CASES.items():
    with_ = load(f"relay-{case}.yaml")
    extra = set(with_) - set(plain)
    assert not set(plain) - set(with_), f"{case}: removed an object"
    assert len(extra) == len(added), f"{case}: unexpected objects {extra}"
    for kind, prefix in added:
        assert [k for k in extra if k[0] == kind and k[2].startswith(prefix)], f"{case}: no {kind} {prefix}*"
    for k in plain:
        if k != key:
            assert plain[k] == with_[k], f"{case}: changed {k}"
    after = copy.deepcopy(with_[key])
    c = relay_container(after)
    got = {e["name"] for e in c["env"]} - {e["name"] for e in relay_container(plain[key]).get("env", [])}
    assert got == env_names, f"{case}: env {got}"
    c["env"] = [e for e in c["env"] if e["name"] not in env_names]
    mounts = [m for m in c.get("volumeMounts", []) if m["name"] in volumes]
    assert all(m.get("readOnly") and m["mountPath"].startswith("/run/") for m in mounts), mounts
    assert {m["name"] for m in mounts} == volumes, mounts
    c["volumeMounts"] = [m for m in c["volumeMounts"] if m["name"] not in volumes]
    spec = after["spec"]["template"]["spec"]
    assert {v["name"] for v in spec["volumes"]} - {v["name"] for v in plain[key]["spec"]["template"]["spec"]["volumes"]} == volumes
    spec["volumes"] = [v for v in spec["volumes"] if v["name"] not in volumes]
    assert after == plain[key], f"{case}: changed more than the relay container's env, mounts and volumes"
    if case == "installations":
        env = {e["name"]: e.get("value") for e in relay_container(with_[key])["env"]}
        assert env["RELAY_INFRASTRUCTURE_INSTALLATIONS"] == "1"
        assert env["RELAY_RECOVERY_GENERATION_FILE"] == "/run/relay-restore/generation.json"
    if case == "profiles":
        [cm] = [with_[k] for k in extra]
        assert cm["data"]["profiles.json"] == open(f"{repo}/kubernetes/components/relay-worker-profiles/profiles.json",
                                                   encoding="utf-8").read()
        v = {x["name"]: x for x in with_[key]["spec"]["template"]["spec"]["volumes"]}
        assert v["worker-profiles"]["configMap"]["name"] == cm["metadata"]["name"]
    print(f"{case}: {sorted(env_names)}; nothing else in the Deployment changed")

allr = load("relay-all.yaml")[key]["spec"]["template"]["spec"]
c = relay_container({"spec": {"template": {"spec": allr}}})
names = [e["name"] for e in c["env"]]
assert len(names) == len(set(names)), f"duplicate env {names}"
for n in ("RELAY_INFRASTRUCTURE_INSTALLATIONS", "RELAY_RECOVERY_GENERATION_FILE", "RELAY_WORKER_PROFILES_FILE",
          "RELAY_WORKER_BUDGET_DSN_FILE", "RELAY_ROUTER_CONNECTIONS_FILE", "RELAY_AGENT_CONTROL_PLANE",
          "RELAY_CONTROL_METRICS_FILE", "RELAY_AGENT_COMMUNICATION"):
    assert n in names, f"all: {n} missing"
vnames = [v["name"] for v in allr["volumes"]]
assert len(vnames) == len(set(vnames)), f"volume collision {vnames}"
paths = [m["mountPath"] for m in c["volumeMounts"]]
assert len(paths) == len(set(paths)), f"mount collision {paths}"
print(f"all: {len(names)} env, {len(vnames)} volumes, {len(paths)} mounts, no collision")
PY
echo "test-relay-researcher-render: OK"
