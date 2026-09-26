#!/usr/bin/env bash
# Operator-workstation only. Never run in Actions or with replacement/empty state.
set -euo pipefail
if [[ "${1:-}" != --apply-access ]]; then
  echo "Usage: TRUESWARM_ADMIN_CHECKOUT=/path/to/trueswarm-admin $0 --apply-access" >&2
  echo "Uses existing Cloudflare state/token, applies only the dedicated Access gate, and commits its public audience to private GitOps. DNS stays unpublished." >&2
  exit 2
fi
if [[ -n "${GITHUB_ACTIONS:-}${GITEA_ACTIONS:-}${CI:-}" ]]; then
  echo "Estate Cloudflare operations are restricted to the operator workstation." >&2
  exit 1
fi
ailab_root="$(cd "$(dirname "$0")/.." && pwd)"
admin_checkout="${TRUESWARM_ADMIN_CHECKOUT:?Set the private trueswarm-admin checkout path}"
cloudflare_root="$ailab_root/kubernetes/infra/cloudflare"
for tool in tofu sops python3 git; do
  command -v "$tool" >/dev/null || { echo "Required executable not found: $tool" >&2; exit 1; }
done
[[ -s "$cloudflare_root/terraform.tfstate" ]] || { echo "Existing workstation state is required; refusing a fresh state." >&2; exit 1; }
[[ -n "${CLOUDFLARE_API_TOKEN:-}" ]] || { echo "Load the existing workstation DNS/Access token into CLOUDFLARE_API_TOKEN first." >&2; exit 1; }
[[ -z "$(git -C "$admin_checkout" status --porcelain)" ]] || { echo "The private admin checkout must be clean." >&2; exit 1; }
[[ "$(git -C "$admin_checkout" branch --show-current)" == main ]] || { echo "Use the private admin main branch." >&2; exit 1; }
git -C "$admin_checkout" pull --ff-only
umask 077
plan_dir="$(mktemp -d)"
trap 'unset TF_VAR_trueswarm_admin_access_client_secret; rm -rf "$plan_dir"' EXIT
export TF_VAR_trueswarm_admin_access_client_secret
TF_VAR_trueswarm_admin_access_client_secret="$(sops decrypt \
  --extract '["stringData"]["OIDC_CLIENT_SECRET"]' \
  "$admin_checkout/deploy/foundation/admin-access-identity.sops.yaml")"
export TF_VAR_enable_trueswarm_admin=true
export TF_VAR_publish_trueswarm_admin=false
tofu -chdir="$cloudflare_root" plan -input=false -out="$plan_dir/access.plan"
tofu -chdir="$cloudflare_root" show -json "$plan_dir/access.plan" > "$plan_dir/access.json"
python3 - "$plan_dir/access.json" <<'PY'
import json,sys
plan=json.load(open(sys.argv[1]))
allowed={f'cloudflare_zero_trust_access_{kind}.trueswarm_admin[0]' for kind in ('identity_provider','policy','application')}
unexpected=[]
for resource in plan.get('resource_changes',[]):
    actions=resource['change']['actions']
    if actions==['no-op'] or resource.get('mode')=='data':continue
    if resource['address'] not in allowed or 'delete' in actions:unexpected.append(resource['address'])
if unexpected:raise SystemExit('Refusing unrelated changes/deletions: '+', '.join(unexpected))
print('Plan guard passed: only the dedicated Trueswarm Access resources change; no DNS publication.')
PY
tofu -chdir="$cloudflare_root" apply -input=false "$plan_dir/access.plan"
tofu -chdir="$cloudflare_root" output -raw trueswarm_admin_access_audience > "$plan_dir/audience"
python3 - "$admin_checkout/deploy/ailab/workloads.yaml" "$plan_dir/audience" <<'PY'
import hashlib,json,pathlib,re,sys
path=pathlib.Path(sys.argv[1]);audience=pathlib.Path(sys.argv[2]).read_text().strip()
if not re.fullmatch(r'[A-Za-z0-9_-]{20,256}',audience):raise SystemExit('Cloudflare returned an invalid audience')
content,count=re.subn(r'(?m)^(  ACCESS_AUDIENCE: ).+$',lambda m:m[1]+json.dumps(audience),path.read_text())
if count!=1:raise SystemExit('Expected exactly one private deployment audience setting')
content,count=re.subn(r'(?m)^(\s+trueswarm\.chifor\.me/access-config: ).+$',lambda m:m[1]+hashlib.sha256(audience.encode()).hexdigest()[:16],content)
if count!=1:raise SystemExit('Expected exactly one admin rollout marker')
path.write_text(content)
print('Stored the non-secret Access audience in the private deployment.')
PY
if ! git -C "$admin_checkout" diff --quiet -- deploy/ailab/workloads.yaml; then
  git -C "$admin_checkout" add deploy/ailab/workloads.yaml
  git -C "$admin_checkout" commit -m 'Bind administration login to the provisioned Cloudflare Access application'
  git -C "$admin_checkout" push origin HEAD:main
fi
echo "Access gate provisioned and audience handed to GitOps. DNS is still unpublished."
