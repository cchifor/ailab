#!/usr/bin/env bash
# Operator-workstation only. Never run in Actions or with replacement/empty state.
set -euo pipefail
if [[ $# != 1 || ( "${1:-}" != --apply-access && "${1:-}" != --publish-dns && "${1:-}" != --apply-e2e-access ) ]]; then
  echo "Usage: TRUESWARM_ADMIN_CHECKOUT=/path/to/trueswarm-admin $0 --apply-access|--publish-dns|--apply-e2e-access" >&2
  echo "Uses existing workstation state/credentials. Access mode never publishes DNS. Publish mode requires the existing gate and matching GitOps audience, and permits only the dedicated DNS record." >&2
  echo "E2E mode (ADR 0035) adds only the dev-worker service token + its non_identity policy to the LIVE, published gate, then seeds the token into af/dev-workers/common via devworker-seeds.sops.yaml (left uncommitted for an ailab PR)." >&2
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
if [[ "$1" == --apply-e2e-access ]]; then
  # The adopted live installation keeps publication true (see the runbook); this mode must never
  # plan the DNS record away, and the guard below insists the record is present and unchanged.
  export TF_VAR_publish_trueswarm_admin=true
  export TF_VAR_enable_trueswarm_admin_e2e=true
  seeds="$ailab_root/kubernetes/apps/infrastructure/security/openbao/devworker-seeds.sops.yaml"
  sync="$ailab_root/kubernetes/apps/trueswarm-e2e-tokens/token-sync.yaml"
  [[ -z "$(git -C "$ailab_root" status --porcelain -- "$seeds" "$sync")" ]] || { echo "Commit or discard local changes to the seed file and token-sync.yaml first." >&2; exit 1; }
fi
tofu -chdir="$cloudflare_root" plan -input=false -out="$plan_dir/access.plan"
tofu -chdir="$cloudflare_root" show -json "$plan_dir/access.plan" > "$plan_dir/access.json"
python3 - "$plan_dir/access.json" "$1" "$admin_checkout/deploy/ailab/workloads.yaml" <<'PYGUARD'
import hashlib,json,pathlib,re,sys
plan=json.load(open(sys.argv[1]))
publishing=sys.argv[2]=='--publish-dns'
e2e=sys.argv[2]=='--apply-e2e-access'
gates={f'cloudflare_zero_trust_access_{kind}.trueswarm_admin[0]' for kind in ('identity_provider','policy','application')}
APP='cloudflare_zero_trust_access_application.trueswarm_admin[0]'
E2E={'cloudflare_zero_trust_access_service_token.trueswarm_admin_e2e[0]','cloudflare_zero_trust_access_policy.trueswarm_admin_e2e[0]'}
DNS='cloudflare_dns_record.trueswarm_admin[0]'
allowed={DNS} if publishing else (E2E|{APP}) if e2e else gates
changes=plan.get('resource_changes',[])
unexpected=[]
for resource in changes:
    actions=resource['change']['actions']
    if actions==['no-op'] or resource.get('mode')=='data':continue
    if resource['address'] not in allowed or 'delete' in actions:unexpected.append(resource['address'])
if unexpected:raise SystemExit('Refusing unrelated changes/deletions: '+', '.join(unexpected))
def gitops_audience_matches(audience):
    text=pathlib.Path(sys.argv[3]).read_text()
    audiences=re.findall(r'(?m)^  ACCESS_AUDIENCE: (.+)$',text)
    return len(audiences)==1 and audiences[0] in (audience,json.dumps(audience))
if e2e:
    def unknown(x):
        return x is True or (isinstance(x,dict) and any(unknown(v) for v in x.values())) or (isinstance(x,list) and any(unknown(v) for v in x))
    def blank(v):
        return v in (None,'',False) or v==[] or v=={}
    by={r['address']:r['change'] for r in changes}
    stay=(gates-{APP})|{DNS}
    if any(a not in by or by[a]['actions']!=['no-op'] for a in stay):
        raise SystemExit('E2E mode requires the existing IdP, human policy and published DNS record, all unchanged')
    if APP not in by or by[APP]['actions'] not in (['update'],['no-op']):
        raise SystemExit('E2E mode may only update the existing Access application in place')
    app=by[APP]
    before,after,later=app.get('before') or {},app.get('after') or {},app.get('after_unknown') or {}
    audience=after.get('aud','')
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,256}',audience) or not gitops_audience_matches(audience):
        raise SystemExit('The Access application audience does not match private GitOps; refusing to touch a different gate')
    # Everything except the policy list must be identical AND known after the update: allowed_idps,
    # session settings, domain, auto-redirect... A computed timestamp may legitimately go unknown.
    drift=sorted(k for k in set(before)|set(after)|set(later) if k not in ('policies','updated_at')
                 and (unknown(later.get(k)) or before.get(k)!=after.get(k)))
    if drift:
        raise SystemExit('E2E mode may change only the application policy list; the plan also changes: '+', '.join(drift))
    human_id=(by['cloudflare_zero_trust_access_policy.trueswarm_admin[0]'].get('before') or {}).get('id')
    bp,ap,up=before.get('policies') or [],after.get('policies') or [],later.get('policies') or []
    up=up+[None]*(2-len(up))
    if not human_id or not bp or bp[0].get('id')!=human_id or bp[0].get('precedence')!=1:
        raise SystemExit('The live application does not have the human MFA policy at precedence 1')
    if len(ap)!=2 or ap[0]!=bp[0] or unknown(up[0]):
        raise SystemExit('The human MFA policy must stay attached at precedence 1, unchanged')
    E2EP='cloudflare_zero_trust_access_policy.trueswarm_admin_e2e[0]'
    TOKEN='cloudflare_zero_trust_access_service_token.trueswarm_admin_e2e[0]'
    if E2EP not in by or TOKEN not in by:
        raise SystemExit('E2E mode expects the e2e service token and its policy in the plan')
    added,added_later=ap[1],(up[1] or {})
    if added.get('precedence')!=2 or any(not blank(v) for k,v in added.items() if k not in ('id','precedence')) \
            or any(unknown(v) for k,v in added_later.items() if k!='id'):
        raise SystemExit('The added application policy must be a bare reference (id, precedence 2), not an inline rule')
    policy=by[E2EP]
    policy_id=(policy.get('after') or {}).get('id')
    if added.get('id') is not None:
        if added['id']!=policy_id:
            raise SystemExit('The added application policy is not the planned e2e policy')
    elif not (added_later.get('id') is True and policy['actions']==['create']):
        raise SystemExit('The added application policy id is unknown but no e2e policy is being created')
    pafter,plater=policy.get('after') or {},policy.get('after_unknown') or {}
    include=pafter.get('include') or []
    token_id=(by[TOKEN].get('after') or {}).get('id')
    if pafter.get('decision')!='non_identity' or len(include)!=1 or set(include[0])-{'service_token'} \
            or any(not blank(pafter.get(k)) for k in ('exclude','require')):
        raise SystemExit('The e2e policy must be non_identity and include only the e2e service token')
    ref=(include[0].get('service_token') or {}).get('token_id')
    ref_later=((plater.get('include') or [{}])[0].get('service_token') or {}).get('token_id')
    if not ((ref is not None and ref==token_id) or (ref is None and ref_later is True and by[TOKEN]['actions']==['create'])):
        raise SystemExit('The e2e policy must reference the planned e2e service token')
    print('Plan guard passed: only the dev-worker e2e service token, its policy and the application policy list change.')
elif publishing:
    existing={r['address']:r['change'] for r in changes if r['address'] in gates}
    if set(existing)!=gates or any(change['actions']!=['no-op'] for change in existing.values()):
        raise SystemExit('Publication requires all three existing, unchanged Access resources in the plan')
    audience=existing['cloudflare_zero_trust_access_application.trueswarm_admin[0]']['after'].get('aud','')
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,256}',audience):
        raise SystemExit('The existing Access application has no valid audience')
    text=pathlib.Path(sys.argv[3]).read_text()
    if not gitops_audience_matches(audience):
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
if [[ "$1" == --apply-e2e-access ]]; then
  # Seed the service token where every dev worker can read it (af/dev-workers/common, via the
  # seed-wins devworker-seeds file) and give its client ID to the token sync, which writes it into
  # the admin tokens file. The secret never touches argv or stdout: it reaches Python through the
  # environment, and the plaintext seed document exists only in the umask-077 plan directory.
  TS_E2E_CLIENT_ID="$(tofu -chdir="$cloudflare_root" output -raw trueswarm_admin_e2e_access_client_id)"
  TS_E2E_CLIENT_SECRET="$(tofu -chdir="$cloudflare_root" output -raw trueswarm_admin_e2e_access_client_secret)"
  export TS_E2E_CLIENT_ID TS_E2E_CLIENT_SECRET
  sops decrypt "$seeds" > "$plan_dir/seeds.yaml"
  python3 - "$plan_dir/seeds.yaml" "$sync" <<'PYSEED'
import json,os,pathlib,re,sys
import yaml
seeds,sync=pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2])
cid,secret=os.environ['TS_E2E_CLIENT_ID'],os.environ['TS_E2E_CLIENT_SECRET']
if not re.fullmatch(r'[A-Za-z0-9._-]{8,200}',cid) or len(secret)<16:raise SystemExit('Cloudflare returned an unusable service token')
text=seeds.read_text()
common=json.loads(yaml.safe_load(text)['stringData']['common.json'])
common.update(trueswarm_admin_access_client_id=cid,trueswarm_admin_access_client_secret=secret)
# Replace only the common.json scalar (single line or block) so every comment in the file survives.
lines=text.split('\n')
start=next(i for i,l in enumerate(lines) if re.match(r'^\s+common\.json:',l))
indent=len(lines[start])-len(lines[start].lstrip())
end=start+1
while end<len(lines) and (not lines[end].strip() or len(lines[end])-len(lines[end].lstrip())>indent):end+=1
lines[start:end]=[' '*indent+'common.json: '+json.dumps(json.dumps(common,separators=(',',':')))]
open(seeds,'w',encoding='utf-8',newline='\n').write('\n'.join(lines))
body,count=re.subn(r'\{ name: ADMIN_ACCESS_CLIENT_IDS, value: "[^"]*" \}','{ name: ADMIN_ACCESS_CLIENT_IDS, value: "%s" }'%cid,sync.read_text())
if count!=2:raise SystemExit('Expected ADMIN_ACCESS_CLIENT_IDS in the CronJob and the bootstrap Job')
open(sync,'w',encoding='utf-8',newline='\n').write(body)
PYSEED
  sops encrypt --filename-override "$seeds" --input-type yaml --output-type yaml "$plan_dir/seeds.yaml" > "$plan_dir/seeds.enc"
  mv "$plan_dir/seeds.enc" "$seeds"
  unset TS_E2E_CLIENT_SECRET
  echo "E2E service token applied and seeded (af/dev-workers/common.trueswarm_admin_access_client_{id,secret}); ADMIN_ACCESS_CLIENT_IDS set in token-sync.yaml."
  echo "Open an ailab PR with exactly these two files: $seeds $sync"
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
# newline='\n': Windows Python would otherwise CRLF the whole file (a 400-line diff).
path.write_text(content,newline='\n')
print('Stored the non-secret Access audience in the private deployment.')
PY
if ! git -C "$admin_checkout" diff --quiet -- deploy/ailab/workloads.yaml; then
  git -C "$admin_checkout" add deploy/ailab/workloads.yaml
  git -C "$admin_checkout" commit -m 'Bind administration login to the provisioned Cloudflare Access application'
  git -C "$admin_checkout" push origin HEAD:main
fi
echo "Access gate provisioned and audience handed to GitOps. DNS is still unpublished."
