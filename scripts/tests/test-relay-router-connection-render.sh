#!/bin/bash
# Renders the live Relay and Router overlays with and without the Router connection components
# (kubernetes/components/relay-router-connection, llm-router-relay-keys-trust) under the SAME
# digest-pinned kustomize image as scripts/manifest-lint.sh, and proves each component:
#   * applies to the current Deployment (a renamed container or Deployment fails here, not at the
#     operator's inclusion PR);
#   * changes nothing but that Deployment's one container env/volumeMounts and its added volumes,
#     plus, for Relay, exactly two added objects: the generated connection ConfigMap (whose content is
#     connections.json byte for byte, and whose hashed name the volume follows) and the additive
#     OpenBao egress NetworkPolicy. Every other object, container and pre-existing env entry is the
#     plain render, so ROUTER_PLUGIN_CONFIG and RELAY_AGENT_CONTROL_PLANE are untouched;
#   * also builds together with the budget-ledger consumer component of the same app (the inclusion
#     the runbook orders) with no volume, mount or env collision: every env entry of the plain render,
#     of the budget component and of this component is present exactly once with its own value (env
#     is a strategic-merge list keyed by name, so a shared name would be silently overwritten);
#   * (Relay) its egress NetworkPolicy selects the live Deployment's pods: its podSelector labels are
#     a subset of the rendered pod template labels, or the in-cluster baoOrigin would be unreachable.
# The components are NOT part of any Flux path, which is why manifest-lint.sh does not build them.
# Requires docker. Exit non-zero on the first broken expectation.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KUSTOMIZE_IMAGE="$(sed -n 's/^KUSTOMIZE_IMAGE="\(.*\)"$/\1/p' "$REPO_ROOT/scripts/manifest-lint.sh")"
[ -n "$KUSTOMIZE_IMAGE" ] || { echo "cannot find KUSTOMIZE_IMAGE in scripts/manifest-lint.sh" >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

for app in relay llm-router; do
  case "$app" in
    relay) component=relay-router-connection; budget=relay-worker-budget-client ;;
    llm-router) component=llm-router-relay-keys-trust; budget=llm-router-worker-budget-reader ;;
  esac
  mkdir -p "$WORK/plain/$app" "$WORK/with/$app" "$WORK/budget/$app" "$WORK/both/$app"
  printf 'resources: [../../repo/kubernetes/apps/apps/%s]\n' "$app" > "$WORK/plain/$app/kustomization.yaml"
  printf 'resources: [../../repo/kubernetes/apps/apps/%s]\ncomponents: [../../repo/kubernetes/components/%s]\n' \
    "$app" "$component" > "$WORK/with/$app/kustomization.yaml"
  printf 'resources: [../../repo/kubernetes/apps/apps/%s]\ncomponents: [../../repo/kubernetes/components/%s]\n' \
    "$app" "$budget" > "$WORK/budget/$app/kustomization.yaml"
  printf 'resources: [../../repo/kubernetes/apps/apps/%s]\ncomponents: [../../repo/kubernetes/components/%s, ../../repo/kubernetes/components/%s]\n' \
    "$app" "$budget" "$component" > "$WORK/both/$app/kustomization.yaml"
done
chmod -R a+rX "$WORK"

render() { # <plain|with|budget|both> <app>
  docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -v "$WORK/$1:/$1:ro" -w "/$1" \
    "$KUSTOMIZE_IMAGE" build "$2" > "$WORK/$1-$2.yaml"
}
for app in relay llm-router; do
  render plain "$app"
  render with "$app"
  render budget "$app"
  render both "$app"
done

PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
"$PY" - "$WORK" "$REPO_ROOT/kubernetes/components/relay-router-connection/connections.json" <<'PY'
import copy, sys, yaml
work, record = sys.argv[1:3]
# app: (deployment, container, env names, volume names, added objects (kind, name prefix))
CASES = {
    "relay": ("relay", "relay", {"RELAY_ROUTER_CONNECTIONS_FILE", "NODE_EXTRA_CA_CERTS"},
              {"router-connection", "router-signers", "router-escrow-token", "router-openbao-ca"},
              {("ConfigMap", "relay-router-connection-"), ("NetworkPolicy", "relay-router-openbao-egress")}),
    "llm-router": ("llm-router", "router", {"RELAY_KEYS_TRUST_FILE"}, {"relay-keys-trust"}, set()),
}
def load(path):
    return {(d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"]): d
            for d in yaml.safe_load_all(open(path, encoding="utf-8")) if d}
for app, (deployment, container, env_names, volumes, added) in CASES.items():
    plain, with_ = load(f"{work}/plain-{app}.yaml"), load(f"{work}/with-{app}.yaml")
    key = ("Deployment", app, deployment)
    extra = set(with_) - set(plain)
    assert not set(plain) - set(with_), f"{app}: the component removed an object"
    assert len(extra) == len(added), f"{app}: unexpected objects {extra}"
    for kind, prefix in added:
        hits = [k for k in extra if k[0] == kind and k[2].startswith(prefix) and k[1] == app]
        assert len(hits) == 1, f"{app}: expected one added {kind} {prefix}*, got {extra}"
    for k in plain:
        if k != key:
            assert plain[k] == with_[k], f"{app}: the component changed {k}"
    before, after = plain[key], copy.deepcopy(with_[key])
    spec = after["spec"]["template"]["spec"]
    [c] = [x for x in spec["containers"] if x["name"] == container]
    added_env = [e for e in c["env"] if e["name"] in env_names]
    assert {e["name"] for e in added_env} == env_names and all("value" in e for e in added_env), added_env
    assert not {e["name"] for e in before["spec"]["template"]["spec"]["containers"][[x["name"] for x in before["spec"]["template"]["spec"]["containers"]].index(container)].get("env", [])} & env_names
    c["env"] = [e for e in c["env"] if e["name"] not in env_names]
    mounts = [m for m in c["volumeMounts"] if m["name"] in volumes]
    assert mounts and all(m["readOnly"] and m["mountPath"].startswith("/run/") for m in mounts), mounts
    c["volumeMounts"] = [m for m in c["volumeMounts"] if m["name"] not in volumes]
    vols = {v["name"]: v for v in spec["volumes"] if v["name"] in volumes}
    assert set(vols) == volumes, vols
    spec["volumes"] = [v for v in spec["volumes"] if v["name"] not in volumes]
    assert before == after, f"{app}: the component changed more than its env, mounts and volumes"
    if app == "relay":
        [cm_key] = [k for k in extra if k[0] == "ConfigMap"]
        cm = with_[cm_key]
        assert vols["router-connection"]["configMap"]["name"] == cm_key[2], "volume does not follow the hashed ConfigMap"
        assert cm["data"]["connections.json"] == open(record, encoding="utf-8").read(), "rendered record differs"
        env = {e["name"]: e["value"] for e in added_env}
        assert env["RELAY_ROUTER_CONNECTIONS_FILE"] == "/run/relay-router/connections.json"
        assert "RELAY_AGENT_CONTROL_PLANE" not in {e["name"] for e in c["env"]}
    if app == "relay":
        [np_key] = [k for k in extra if k[0] == "NetworkPolicy"]
        selector = with_[np_key]["spec"]["podSelector"]["matchLabels"]
        pod_labels = with_[key]["spec"]["template"]["metadata"]["labels"]
        assert selector.items() <= pod_labels.items(), f"egress policy {selector} selects no Relay pod {pod_labels}"
    # Together with the budget component: no volume/mount collision, and no env name shared (a shared
    # name would be silently replaced by the strategic merge): the combined env is exactly the plain
    # env plus each component's own additions, each with its own value.
    def container_of(render):
        return [x for x in load(f"{work}/{render}-{app}.yaml")[key]["spec"]["template"]["spec"]["containers"]
                if x["name"] == container][0]
    def env_of(render):
        return [(e["name"], e.get("value", e.get("valueFrom"))) for e in container_of(render).get("env", [])]
    plain_env, with_env, budget_env, both_env = (env_of(r) for r in ("plain", "with", "budget", "both"))
    own = [e for e in with_env if e not in plain_env]
    budgets = [e for e in budget_env if e not in plain_env]
    assert own and budgets, f"{app}: a component added no env"
    assert not {n for n, _ in own} & {n for n, _ in budgets}, f"{app}: env name shared with the budget component"
    names = [n for n, _ in both_env]
    assert len(names) == len(set(names)), f"{app}: duplicate env {names}"
    assert sorted(both_env, key=str) == sorted(plain_env + own + budgets, key=str), f"{app}: combined env {both_env}"
    both = load(f"{work}/both-{app}.yaml")[key]["spec"]["template"]["spec"]
    names = [v["name"] for v in both["volumes"]]
    assert len(names) == len(set(names)), f"{app}: volume collision {names}"
    [bc] = [x for x in both["containers"] if x["name"] == container]
    paths = [m["mountPath"] for m in bc["volumeMounts"]]
    assert len(paths) == len(set(paths)), f"{app}: mount collision {paths}"
    print(f"{app}: {len(added_env)} env, {len(mounts)} read-only mounts, {len(vols)} volumes, "
          f"{len(extra)} added objects; nothing else changed; combines with the budget component "
          f"({len(own)} + {len(budgets)} env, no collision)")
PY
echo "test-relay-router-connection-render: OK"
