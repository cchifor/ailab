#!/usr/bin/env bash
# Operator-workstation only. Never run in Actions or with replacement/empty state.
set -euo pipefail
if [[ $# != 1 || ( "${1:-}" != --apply-access && "${1:-}" != --publish-dns ) ]]; then
  echo "Usage: TRUESWARM_ADMIN_CHECKOUT=/path/to/trueswarm-admin $0 --apply-access|--publish-dns" >&2
  echo "Uses existing workstation state/credentials. Access mode never publishes DNS. Publish mode requires the existing gate and matching GitOps audience, and permits only the dedicated DNS record." >&2
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
if [[ "$1" == --publish-dns ]]; then
  export TF_VAR_publish_trueswarm_admin=true
fi
tofu -chdir="$cloudflare_root" plan -input=false -out="$plan_dir/access.plan"
tofu -chdir="$cloudflare_root" show -json "$plan_dir/access.plan" > "$plan_dir/access.json"
python3 - "$plan_dir/access.json" "$1" "$admin_checkout/deploy/ailab/workloads.yaml" <<'PYGUARD'
import hashlib,json,pathlib,re,sys
plan=json.load(open(sys.argv[1]))
publishing=sys.argv[2]=='--publish-dns'
gates={f'cloudflare_zero_trust_access_{kind}.trueswarm_admin[0]' for kind in ('identity_provider','policy','application')}
allowed={'cloudflare_dns_record.trueswarm_admin[0]'} if publishing else gates
changes=plan.get('resource_changes',[])
unexpected=[]
for resource in changes:
    actions=resource['change']['actions']
    if actions==['no-op'] or resource.get('mode')=='data':continue
    if resource['address'] not in allowed or 'delete' in actions:unexpected.append(resource['address'])
if unexpected:raise SystemExit('Refusing unrelated changes/deletions: '+', '.join(unexpected))
if publishing:
    existing={r['address']:r['change'] for r in changes if r['address'] in gates}
    if set(existing)!=gates or any(change['actions']!=['no-op'] for change in existing.values()):
        raise SystemExit('Publication requires all three existing, unchanged Access resources in the plan')
    audience=existing['cloudflare_zero_trust_access_application.trueswarm_admin[0]']['after'].get('aud','')
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,256}',audience):
        raise SystemExit('The existing Access application has no valid audience')
    text=pathlib.Path(sys.argv[3]).read_text()
    audiences=re.findall(r'(?m)^  ACCESS_AUDIENCE: (.+)$',text)
    if len(audiences)!=1 or audiences[0] not in (audience,json.dumps(audience)):
        raise SystemExit('Private GitOps audience does not match the existing Access application')
    markers=re.findall(r'(?m)^\s+trueswarm\.chifor\.me/access-config: (.+)$',text)
    if markers!=[hashlib.sha256(audience.encode()).hexdigest()[:16]]:
        raise SystemExit('Private GitOps audience rollout marker is missing or stale')
    dns=[r['change'] for r in changes if r['address']=='cloudflare_dns_record.trueswarm_admin[0]']
    tunnels=re.findall(r'\btunnel: ([0-9a-f-]{36})',text)
    if len(dns)!=1 or len(tunnels)!=1:
        raise SystemExit('Publication requires the DNS record in the plan and one tunnel in private GitOps')
    record=dns[0].get('after') or {}
    if (record.get('name')!='trueswarm-admin.chifor.me' or record.get('type')!='CNAME'
            or record.get('proxied') is not True or record.get('content')!=tunnels[0]+'.cfargotunnel.com'):
        raise SystemExit('Publication requires the proxied administrator CNAME to its deployed tunnel')
    print('Plan guard passed: only the dedicated DNS record changes; Access and its GitOps audience remain unchanged.')
else:
    print('Plan guard passed: only the dedicated Trueswarm Access resources change; no DNS publication.')
PYGUARD
tofu -chdir="$cloudflare_root" apply -input=false "$plan_dir/access.plan"
if [[ "$1" == --publish-dns ]]; then
  echo "DNS published behind the existing Access gate. Complete authenticated browser qualification before declaring the admin console ready. Recovery qualification is unchanged."
  exit 0
fi
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
