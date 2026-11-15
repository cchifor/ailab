#!/bin/bash
# Renders the staged Relay researcher internal TLS terminators (kubernetes/apps/relay-internal-tls) under
# the SAME digest-pinned kustomize image as scripts/manifest-lint.sh and validates the render with its
# kubeconform. The tree is not a Flux path until its qualification-stage inclusion, which is why
# manifest-lint.sh does not build it today. Proves the build itself: exactly the expected objects, the
# generated Caddy ConfigMaps equal byte for byte to hub.Caddyfile / router.Caddyfile / cert-reloader.sh,
# each Deployment mounting its own hashed ConfigMap and its own Certificate's Secret, and the two
# Services pinned to 10.96.0.40 / 10.96.0.41. The source-level contract is
# scripts/tests/test_relay_internal_tls.py; what Caddy does with the config is
# scripts/tests/relay_internal_tls_runtime.py. Requires docker.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KUSTOMIZE_IMAGE="$(sed -n 's/^KUSTOMIZE_IMAGE="\(.*\)"$/\1/p' "$REPO_ROOT/scripts/manifest-lint.sh")"
KUBECONFORM_IMAGE="$(sed -n 's/^KUBECONFORM_IMAGE="\(.*\)"$/\1/p' "$REPO_ROOT/scripts/manifest-lint.sh")"
[ -n "$KUSTOMIZE_IMAGE" ] && [ -n "$KUBECONFORM_IMAGE" ] || { echo "cannot find the pinned images in scripts/manifest-lint.sh" >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
chmod a+rwx "$WORK"

docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w / \
  "$KUSTOMIZE_IMAGE" build /repo/kubernetes/apps/relay-internal-tls > "$WORK/out.yaml"
chmod a+r "$WORK/out.yaml"

docker run --rm -v "$WORK:/out:ro" "$KUBECONFORM_IMAGE" \
  -strict -summary -ignore-missing-schemas \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  /out/out.yaml

PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
"$PY" - "$WORK/out.yaml" "$REPO_ROOT" <<'PY'
import sys, yaml
out, repo = sys.argv[1:3]
tree = f"{repo}/kubernetes/apps/relay-internal-tls"
objs = {(d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"]): d
        for d in yaml.safe_load_all(open(out, encoding="utf-8")) if d}
NS = "relay-internal-tls"
cms = {k[2]: v for k, v in objs.items() if k[0] == "ConfigMap"}
static = {k for k in objs if k[0] != "ConfigMap"}
expected = {
    ("Namespace", None, NS),
    ("NetworkPolicy", NS, "relay-default-deny"),
    ("NetworkPolicy", NS, "relay-hub-tls"), ("NetworkPolicy", NS, "relay-router-tls"),
    ("NetworkPolicy", "relay", "relay-internal-tls-ingress"),
    ("NetworkPolicy", "llm-router", "llm-router-edge-internal-tls-ingress"),
    ("Certificate", NS, "relay-hub-tls"), ("Certificate", NS, "relay-router-tls"),
    ("Deployment", NS, "relay-hub-tls"), ("Deployment", NS, "relay-router-tls"),
    ("PodDisruptionBudget", NS, "relay-hub-tls"), ("PodDisruptionBudget", NS, "relay-router-tls"),
    ("Service", NS, "relay-hub-tls"), ("Service", NS, "relay-router-tls"),
}
assert static == expected, f"objects {sorted(static ^ expected)}"
assert len(cms) == 2, sorted(cms)
reloader = open(f"{tree}/cert-reloader.sh", encoding="utf-8").read()
for name, ip, caddyfile in (("relay-hub-tls", "10.96.0.40", "hub.Caddyfile"),
                            ("relay-router-tls", "10.96.0.41", "router.Caddyfile")):
    [cm] = [v for k, v in cms.items() if k.startswith(f"{name}-caddy-")]
    assert cm["metadata"]["namespace"] == NS
    assert cm["data"] == {"Caddyfile": open(f"{tree}/{caddyfile}", encoding="utf-8").read(),
                          "cert-reloader.sh": reloader}, f"{name}: ConfigMap content differs from the files"
    pod = objs[("Deployment", NS, name)]["spec"]["template"]["spec"]
    vols = {v["name"]: v for v in pod["volumes"]}
    assert vols["caddyfile"]["configMap"]["name"] == cm["metadata"]["name"], name
    cert = objs[("Certificate", NS, name)]["spec"]
    assert vols["certs"]["secret"]["secretName"] == cert["secretName"], name
    svc = objs[("Service", NS, name)]["spec"]
    assert svc["clusterIP"] == ip and svc["clusterIPs"] == [ip], (name, svc["clusterIP"])
    assert svc["selector"] == objs[("Deployment", NS, name)]["spec"]["selector"]["matchLabels"], name
    print(f"{name}: Service {ip}:443, ConfigMap {cm['metadata']['name']}, Secret {cert['secretName']}")
print(f"relay-internal-tls: {len(objs)} objects")
PY
echo "test-relay-internal-tls-render: OK"
