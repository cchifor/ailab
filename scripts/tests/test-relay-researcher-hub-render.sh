#!/bin/bash
# Renders the Relay hub upgrade components (docs/runbooks/relay-researcher-hub-upgrade.md) onto the
# live Relay overlay under the SAME digest-pinned kustomize image as scripts/manifest-lint.sh, and
# validates the combined render with its kubeconform. None of the components is included by a Flux
# path, which is why manifest-lint.sh does not build them. Proves:
#   * each hub step's components (H3: relay-infrastructure-installations with relay-restore-fencing;
#     H4a: relay-worker-budget-client alone; H4b: relay-worker-profiles alone, after the release
#     approval; H6: relay-agent-control-plane; H7: relay-worker-candidate) apply to the current Relay
#     Deployment and change nothing but the relay container's env, mounts and added volumes plus their
#     own generated ConfigMap, and for H7 its one init container (candidate-session-key, which alone
#     mounts the session-key Secret and copies it, as UID 1000, into the emptyDir the relay container
#     mounts read-only);
#   * the whole set together (H3, H4a, H6 and H4b: those five plus relay-router-connection,
#     relay-control-monitoring and relay-worker-monitoring), and the H7 set (H3, H4a, H6 and the
#     candidate files, which never share a rollout with H4b's registry), each build with no env,
#     volume or mount collision, and still set no RELAY_ENVIRONMENTS, Gitea issuer or notification file
#     (owner decision G2).
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
HUB_COMPONENTS="relay-restore-fencing relay-infrastructure-installations relay-worker-budget-client relay-worker-profiles relay-router-connection relay-agent-control-plane relay-control-monitoring relay-worker-monitoring relay-worker-candidate"
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
  [budget]="relay-worker-budget-client"
  [profiles]="relay-worker-profiles"
  [controlplane]="relay-agent-control-plane"
  [candidate]="relay-worker-candidate"
  [all]="relay-restore-fencing relay-infrastructure-installations relay-worker-budget-client relay-worker-profiles relay-router-connection relay-agent-control-plane relay-control-monitoring relay-worker-monitoring"
  [h7]="relay-restore-fencing relay-infrastructure-installations relay-worker-budget-client relay-router-connection relay-agent-control-plane relay-control-monitoring relay-worker-monitoring relay-worker-candidate"
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
  /out/relay-all.yaml /out/relay-h7.yaml

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
CASES = {  # case -> (env names, volume names, added (kind, name prefix), relay-mounted volumes, init containers)
    "installations": ({"RELAY_INFRASTRUCTURE_INSTALLATIONS", "RELAY_RECOVERY_GENERATION_FILE"},
                      {"restore-generation"}, set(), None, []),
    "budget": ({"RELAY_WORKER_BUDGET_DSN_FILE", "RELAY_WORKER_BUDGET_CA_FILE", "RELAY_WORKER_BUDGET_WITNESS_FILE"},
               {"worker-budget"}, set(), None, []),
    "profiles": ({"RELAY_WORKER_PROFILES_FILE", "RELAY_WORKER_PROFILES_SHA256"},
                 {"worker-profiles"}, {("ConfigMap", "relay-worker-profiles-")}, None, []),
    "controlplane": ({"RELAY_AGENT_CONTROL_PLANE"}, set(), set(), None, []),
    # The session-key Secret is mounted only by the init container, never by the relay container.
    "candidate": ({"RELAY_WORKER_CANDIDATE_FILE", "RELAY_WORKER_CANDIDATE_SHA256", "RELAY_WORKER_CANDIDATE_SESSION_KEY_FILE"},
                  {"worker-candidate-document", "worker-candidate-key-source", "worker-candidate-key"},
                  {("ConfigMap", "relay-worker-candidate-")}, {"worker-candidate-document", "worker-candidate-key"},
                  ["candidate-session-key"]),
}
key = ("Deployment", "relay", "relay")
plain = load("relay-plain.yaml")
def relay_container(doc):
    [c] = [c for c in doc["spec"]["template"]["spec"]["containers"] if c["name"] == "relay"]
    return c
for case, (env_names, volumes, added, relay_volumes, inits) in CASES.items():
    relay_volumes = volumes if relay_volumes is None else relay_volumes
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
    assert {m["name"] for m in mounts} == relay_volumes, mounts
    c["volumeMounts"] = [m for m in c["volumeMounts"] if m["name"] not in volumes]
    spec = after["spec"]["template"]["spec"]
    plain_inits = [i["name"] for i in plain[key]["spec"]["template"]["spec"]["initContainers"]]
    assert [i["name"] for i in spec["initContainers"]] == inits + plain_inits, spec["initContainers"]
    spec["initContainers"] = [i for i in spec["initContainers"] if i["name"] not in inits]
    assert {v["name"] for v in spec["volumes"]} - {v["name"] for v in plain[key]["spec"]["template"]["spec"]["volumes"]} == volumes
    spec["volumes"] = [v for v in spec["volumes"] if v["name"] not in volumes]
    assert after == plain[key], f"{case}: changed more than the relay container's env, mounts and volumes"
    if case == "installations":
        env = {e["name"]: e.get("value") for e in relay_container(with_[key])["env"]}
        assert env["RELAY_INFRASTRUCTURE_INSTALLATIONS"] == "1"
        assert env["RELAY_RECOVERY_GENERATION_FILE"] == "/run/relay-restore/generation.json"
    if case == "candidate":
        [cm] = [with_[k] for k in extra]
        run = f"{repo}/kubernetes/components/relay-worker-candidate/run"
        document = open(f"{run}/document.json", encoding="utf-8").read()
        assert cm["data"] == {"document.json": document} and cm.get("immutable") is True, cm["metadata"]
        pod = with_[key]["spec"]["template"]["spec"]
        v = {x["name"]: x for x in pod["volumes"]}
        assert v["worker-candidate-document"]["configMap"] == {
            "name": cm["metadata"]["name"], "defaultMode": 0o444,
            "items": [{"key": "document.json", "path": "document.json"}]}, v["worker-candidate-document"]
        [patch] = [d for d in yaml.safe_load_all(open(f"{run}/deployment.yaml", encoding="utf-8")) if d]
        [secret] = [x for x in patch["spec"]["template"]["spec"]["volumes"] if x["name"] == "worker-candidate-key-source"]
        assert v["worker-candidate-key-source"]["secret"] == {
            "secretName": secret["secret"]["secretName"], "defaultMode": 0o440,
            "items": [{"key": "session-key.pem", "path": "session-key.pem"}]}, v["worker-candidate-key-source"]
        assert v["worker-candidate-key"] == {"name": "worker-candidate-key",
                                             "emptyDir": {"medium": "Memory", "sizeLimit": "64Ki"}}
        env = {e["name"]: e.get("value") for e in relay_container(with_[key])["env"]}
        pin = env["RELAY_WORKER_CANDIDATE_SHA256"]
        assert pin == hashlib.sha256(document.encode("utf-8")).hexdigest() or (
            pin.startswith("OPERATOR-FILLS") and "OPERATOR-FILLS" in document), pin
        [init] = [i for i in pod["initContainers"] if i["name"] == "candidate-session-key"]
        assert init["volumeMounts"] == [
            {"name": "worker-candidate-key-source", "mountPath": "/run/relay-worker-candidate-key-source", "readOnly": True},
            {"name": "worker-candidate-key", "mountPath": "/run/relay-worker-candidate-key"}], init
        assert init["securityContext"]["readOnlyRootFilesystem"] is True and "runAsUser" not in init["securityContext"]
        # The Pod's own UID 1000 / fsGroup 1000 apply to the copy (the component's mount contract).
        sc = pod["securityContext"]
        assert (sc["runAsUser"], sc["runAsGroup"], sc["fsGroup"]) == (1000, 1000, 1000), sc
        for c2 in pod["containers"] + pod["initContainers"]:
            if c2["name"] != "candidate-session-key":
                assert all(m["name"] != "worker-candidate-key-source" for m in c2.get("volumeMounts", [])), c2["name"]
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

h7 = load("relay-h7.yaml")[key]["spec"]["template"]["spec"]
c = relay_container({"spec": {"template": {"spec": h7}}})
names = [e["name"] for e in c["env"]]
assert len(names) == len(set(names)), f"h7: duplicate env {names}"
for n in ("RELAY_WORKER_CANDIDATE_FILE", "RELAY_WORKER_CANDIDATE_SHA256", "RELAY_WORKER_CANDIDATE_SESSION_KEY_FILE",
          "RELAY_ROUTER_CONNECTIONS_FILE", "RELAY_AGENT_CONTROL_PLANE", "RELAY_WORKER_BUDGET_DSN_FILE"):
    assert n in names, f"h7: {n} missing"
for n in ("RELAY_ENVIRONMENTS", "RELAY_GITEA_ISSUERS_FILE", "RELAY_NOTIFICATION_DESTINATIONS_FILE",
          "RELAY_WORKER_PROFILES_FILE"):
    assert n not in names, f"h7: {n} must stay unset"
vnames = [v["name"] for v in h7["volumes"]]
assert len(vnames) == len(set(vnames)), f"h7: volume collision {vnames}"
paths = [m["mountPath"] for m in c["volumeMounts"]]
assert len(paths) == len(set(paths)), f"h7: mount collision {paths}"
print(f"h7: {len(names)} env, {len(vnames)} volumes, {len(paths)} mounts, no collision")
PY
echo "test-relay-researcher-hub-render: OK"
