#!/bin/bash
# Renders the Relay hub upgrade components (docs/runbooks/relay-researcher-hub-upgrade.md) onto the
# live Relay overlay under the SAME digest-pinned kustomize image as scripts/manifest-lint.sh, and
# validates the combined render with its kubeconform. None of the components is included by a Flux
# path, which is why manifest-lint.sh does not build them. Proves:
#   * each hub component (relay-infrastructure-installations with relay-restore-fencing,
#     relay-worker-profiles with relay-worker-budget-client, relay-agent-control-plane) applies to the
#     current Relay Deployment and changes nothing but the relay container's env, mounts and added
#     volumes plus its own generated ConfigMap;
#   * the whole H3-H6 set together (those five plus relay-router-connection, relay-control-monitoring
#     and relay-worker-monitoring) builds with no env, volume or mount collision, and still sets no
#     RELAY_ENVIRONMENTS, Gitea issuer or notification file (owner decision G2).
#   * the live overlay, which includes some of these components from H3 on, renders exactly as the
#     overlay WITHOUT them plus the ones it includes, in its order. Every case above is layered on that
#     overlay without them (its kustomization minus those lines, mounted over the real one), so each
#     still proves exactly what its own components change.
# Requires docker. Exit non-zero on the first broken expectation.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KUSTOMIZE_IMAGE="$(sed -n 's/^KUSTOMIZE_IMAGE="\(.*\)"$/\1/p' "$REPO_ROOT/scripts/manifest-lint.sh")"
KUBECONFORM_IMAGE="$(sed -n 's/^KUBECONFORM_IMAGE="\(.*\)"$/\1/p' "$REPO_ROOT/scripts/manifest-lint.sh")"
[ -n "$KUSTOMIZE_IMAGE" ] && [ -n "$KUBECONFORM_IMAGE" ] || { echo "cannot find the pinned images in scripts/manifest-lint.sh" >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

OVERLAY=kubernetes/apps/apps/relay
HUB_COMPONENTS="relay-restore-fencing relay-infrastructure-installations relay-worker-budget-client relay-worker-profiles relay-router-connection relay-agent-control-plane relay-control-monitoring relay-worker-monitoring"
LIVE_HUB=()
: > "$WORK/pre-kustomization.yaml"
while IFS= read -r line || [ -n "$line" ]; do
  for c in $HUB_COMPONENTS; do
    if [ "$line" = "- ../../../components/$c" ]; then LIVE_HUB+=("$c"); continue 2; fi
  done
  printf '%s\n' "$line" >> "$WORK/pre-kustomization.yaml"
done < "$REPO_ROOT/$OVERLAY/kustomization.yaml"
echo "live overlay includes: ${LIVE_HUB[*]:-(no hub component)}"

# name -> components layered on the relay overlay without its hub components
declare -A CASES=(
  [plain]=""
  [live-expected]="${LIVE_HUB[*]:-}"
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

build() { # <dir-in-container> <output> [docker args]
  local dir="$1" output="$2"; shift 2
  docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -v "$WORK:/work:ro" "$@" -w / \
    "$KUSTOMIZE_IMAGE" build "$dir" > "$output"
}
mkdir -p "$WORK/out"
for case in "${!CASES[@]}"; do
  build "/work/$case" "$WORK/out/relay-$case.yaml" -v "$WORK/pre-kustomization.yaml:/repo/$OVERLAY/kustomization.yaml:ro"
done
build "/repo/$OVERLAY" "$WORK/out/relay-live.yaml"
chmod -R a+rX "$WORK/out"

docker run --rm -v "$WORK/out:/out:ro" "$KUBECONFORM_IMAGE" \
  -strict -summary -ignore-missing-schemas \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  -skip Secret \
  /out/relay-all.yaml

PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
"$PY" - "$WORK/out" "$REPO_ROOT" <<'PY'
import copy, hashlib, json, sys, yaml
out, repo = sys.argv[1:3]

def load(name):
    return {(d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"]): d
            for d in yaml.safe_load_all(open(f"{out}/{name}", encoding="utf-8")) if d}


# --- the live overlay is the overlay without its hub components plus exactly the ones it includes
live, expected = load("relay-live.yaml"), load("relay-live-expected.yaml")
assert live == expected, "the live overlay differs from its hub components layered on the overlay without them"
print("live: equals the overlay without its hub components plus the ones it includes")

# --- hub components on the Relay Deployment without them
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
# Owner decision G2: the control plane comes without environments, Gitea issuers or notifications.
for n in ("RELAY_ENVIRONMENTS", "RELAY_GITEA_ISSUERS_FILE", "RELAY_NOTIFICATION_DESTINATIONS_FILE"):
    assert n not in names, f"all: {n} must stay unset with the control plane"
vnames = [v["name"] for v in allr["volumes"]]
assert len(vnames) == len(set(vnames)), f"volume collision {vnames}"
paths = [m["mountPath"] for m in c["volumeMounts"]]
assert len(paths) == len(set(paths)), f"mount collision {paths}"
print(f"all: {len(names)} env, {len(vnames)} volumes, {len(paths)} mounts, no collision")
PY
echo "test-relay-researcher-hub-render: OK"
