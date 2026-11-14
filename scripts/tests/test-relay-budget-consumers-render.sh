#!/bin/bash
# Renders the live Relay and Router overlays with and without the budget-ledger consumer components
# (kubernetes/components/relay-worker-budget-client, llm-router-worker-budget-reader) under the SAME
# digest-pinned kustomize image as scripts/manifest-lint.sh, and proves each component:
#   * applies to the current Deployment (a renamed container or Deployment fails the build or the
#     checks below, instead of surfacing at the operator's inclusion PR);
#   * changes nothing but that Deployment's one container env/volumeMounts and one added volume:
#     every other rendered object, every other container and every pre-existing env entry is
#     byte-for-byte the plain render (so ROUTER_PLUGIN_CONFIG, the plugin's enable state, is untouched).
# The components are NOT part of any Flux path yet, which is why manifest-lint.sh does not build them.
# Requires docker. Exit non-zero on the first broken expectation.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KUSTOMIZE_IMAGE="$(sed -n 's/^KUSTOMIZE_IMAGE="\(.*\)"$/\1/p' "$REPO_ROOT/scripts/manifest-lint.sh")"
[ -n "$KUSTOMIZE_IMAGE" ] || { echo "cannot find KUSTOMIZE_IMAGE in scripts/manifest-lint.sh" >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

for app in relay llm-router; do
  case "$app" in
    relay) component=relay-worker-budget-client ;;
    llm-router) component=llm-router-worker-budget-reader ;;
  esac
  mkdir -p "$WORK/plain/$app" "$WORK/with/$app"
  printf 'resources: [../../repo/kubernetes/apps/apps/%s]\n' "$app" > "$WORK/plain/$app/kustomization.yaml"
  printf 'resources: [../../repo/kubernetes/apps/apps/%s]\ncomponents: [../../repo/kubernetes/components/%s]\n' \
    "$app" "$component" > "$WORK/with/$app/kustomization.yaml"
done
chmod -R a+rX "$WORK"

render() { # <plain|with> <app>
  docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -v "$WORK/$1:/$1:ro" -w "/$1" \
    "$KUSTOMIZE_IMAGE" build "$2" > "$WORK/$1-$2.yaml"
}
for app in relay llm-router; do
  render plain "$app"
  render with "$app"
done

PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
"$PY" - "$WORK" <<'PY'
import copy, sys, yaml
work = sys.argv[1]
CASES = {
    "relay": ("relay", "relay", "relay-worker-budget", "RELAY_WORKER_BUDGET", "/run/relay-worker-budget/"),
    "llm-router": ("llm-router", "router", "llm-router-worker-budget", "ROUTER_WORKER_BUDGET",
                   "/run/llm-router-worker-budget/"),
}
def load(path):
    return {(d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"]): d
            for d in yaml.safe_load_all(open(path, encoding="utf-8")) if d}
for app, (deployment, container, secret, prefix, root) in CASES.items():
    plain, with_ = load(f"{work}/plain-{app}.yaml"), load(f"{work}/with-{app}.yaml")
    key = ("Deployment", app, deployment)
    assert set(plain) == set(with_), f"{app}: the component added or removed an object"
    for k in plain:
        if k != key:
            assert plain[k] == with_[k], f"{app}: the component changed {k}"
    before, after = plain[key], copy.deepcopy(with_[key])
    spec = after["spec"]["template"]["spec"]
    [c] = [x for x in spec["containers"] if x["name"] == container]
    added_env = [e for e in c["env"] if e["name"].startswith(prefix + "_")]
    assert added_env and all("value" in e for e in added_env), f"{app}: env not applied"
    c["env"] = [e for e in c["env"] if not e["name"].startswith(prefix + "_")]
    mounts = [m for m in c["volumeMounts"] if m["name"] == "worker-budget"]
    assert len(mounts) == 3 and all(m["mountPath"].startswith(root) and m["readOnly"] for m in mounts), mounts
    c["volumeMounts"] = [m for m in c["volumeMounts"] if m["name"] != "worker-budget"]
    [vol] = [v for v in spec["volumes"] if v["name"] == "worker-budget"]
    assert vol["secret"]["secretName"] == secret, vol
    spec["volumes"] = [v for v in spec["volumes"] if v["name"] != "worker-budget"]
    assert before == after, f"{app}: the component changed more than its env, mounts and volume"
    print(f"{app}: {len(added_env)} env, 3 read-only subPath mounts, 1 Secret volume; nothing else changed")
PY
echo "test-relay-budget-consumers-render: OK"
