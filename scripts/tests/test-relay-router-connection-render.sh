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
# Every render starts from the live overlay with this app's two components (the connection component
# and the budget-ledger consumer) removed, so the checks hold whether or not the live overlay already
# includes either one (hub step H4a includes the Relay budget client, budget runbook step 8 the Router
# reader, hub step H6 the Relay connection). The live overlay itself must then render exactly like the
# matching combination ("plain", "with", "budget" or "both"), compared order-insensitively for env,
# volumes and mounts, so what is checked is what Flux applies.
# Requires docker. Exit non-zero on the first broken expectation.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KUSTOMIZE_IMAGE="$(sed -n 's/^KUSTOMIZE_IMAGE="\(.*\)"$/\1/p' "$REPO_ROOT/scripts/manifest-lint.sh")"
[ -n "$KUSTOMIZE_IMAGE" ] || { echo "cannot find KUSTOMIZE_IMAGE in scripts/manifest-lint.sh" >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
# A copy of kubernetes/ in which each app overlay lists neither its connection component nor its budget
# component. Every render below starts from it; nothing else in the copy differs from the repo.
mkdir -p "$WORK/stripped"
cp -r "$REPO_ROOT/kubernetes" "$WORK/stripped/kubernetes"

for app in relay llm-router; do
  case "$app" in
    relay) component=relay-router-connection; budget=relay-worker-budget-client ;;
    llm-router) component=llm-router-relay-keys-trust; budget=llm-router-worker-budget-reader ;;
  esac
  # which of the two the live overlay includes: plain, with, budget or both
  live=$("$PY" - "$WORK/stripped/kubernetes/apps/apps/$app/kustomization.yaml" "$component" "$budget" <<'STRIP'
import sys, yaml
path, component, budget = sys.argv[1:4]
k = yaml.safe_load(open(path, encoding="utf-8"))
entries = k.get("components") or []
name = lambda e: e.rstrip("/").split("/")[-1]
found = {n: sum(1 for e in entries if name(e) == n) for n in (component, budget)}
assert all(v <= 1 for v in found.values()), f"{path}: a component is listed more than once: {found}"
kept = [e for e in entries if name(e) not in (component, budget)]
if len(kept) != len(entries):
    if kept:
        k["components"] = kept
    else:
        del k["components"]
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(k, f, sort_keys=False)
print({(0, 0): "plain", (1, 0): "with", (0, 1): "budget", (1, 1): "both"}[(found[component], found[budget])])
STRIP
)
  echo "$live" > "$WORK/live-$app"
  echo "$app: the live overlay renders as '$live'"
  base="../../stripped/kubernetes/apps/apps/$app"
  mkdir -p "$WORK/plain/$app" "$WORK/with/$app" "$WORK/budget/$app" "$WORK/both/$app" "$WORK/live/$app"
  printf 'resources: [%s]\n' "$base" > "$WORK/plain/$app/kustomization.yaml"
  printf 'resources: [%s]\ncomponents: [../../repo/kubernetes/components/%s]\n' \
    "$base" "$component" > "$WORK/with/$app/kustomization.yaml"
  printf 'resources: [%s]\ncomponents: [../../repo/kubernetes/components/%s]\n' \
    "$base" "$budget" > "$WORK/budget/$app/kustomization.yaml"
  printf 'resources: [%s]\ncomponents: [../../repo/kubernetes/components/%s, ../../repo/kubernetes/components/%s]\n' \
    "$base" "$budget" "$component" > "$WORK/both/$app/kustomization.yaml"
  printf 'resources: [../../repo/kubernetes/apps/apps/%s]\n' "$app" > "$WORK/live/$app/kustomization.yaml"
done
chmod -R a+rX "$WORK"

render() { # <plain|with|budget|both|live> <app>
  docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -v "$WORK/stripped:/stripped:ro" \
    -v "$WORK/$1:/$1:ro" -w "/$1" "$KUSTOMIZE_IMAGE" build "$2" > "$WORK/$1-$2.yaml"
}
for app in relay llm-router; do
  render plain "$app"
  render with "$app"
  render budget "$app"
  render both "$app"
  render live "$app"
done

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
    # The live overlay renders exactly like the combination it includes (env, volumes and mounts
    # compared as sets: component order in the overlay may order them differently).
    def normal(objs):
        out = copy.deepcopy(objs)
        for d in out.values():
            if d["kind"] != "Deployment":
                continue
            ps = d["spec"]["template"]["spec"]
            ps["volumes"] = sorted(ps.get("volumes", []), key=lambda v: v["name"])
            for cc in ps["containers"]:
                cc["env"] = sorted(cc.get("env", []), key=lambda e: e["name"])
                cc["volumeMounts"] = sorted(cc.get("volumeMounts", []), key=lambda m: m["mountPath"])
        return out
    live_as = open(f"{work}/live-{app}", encoding="utf-8").read().strip()
    assert normal(load(f"{work}/live-{app}.yaml")) == normal(load(f"{work}/{live_as}-{app}.yaml")), \
        f"{app}: the live overlay does not render like '{live_as}'"
    print(f"{app}: {len(added_env)} env, {len(mounts)} read-only mounts, {len(vols)} volumes, "
          f"{len(extra)} added objects; nothing else changed; combines with the budget component "
          f"({len(own)} + {len(budgets)} env, no collision)")
PY
echo "test-relay-router-connection-render: OK"
