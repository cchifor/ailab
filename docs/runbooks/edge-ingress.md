# Runbook — edge ingress (`*.chifor.me`)

Standard: `docs/standards/edge-ingress.md`. Decision: ADR 0040. Plan and rollout phases:
`plans/2026-10-27-edge-ingress-plan.md`.

The rollout lands in phases P1-P9. A section marked **(from Pn)** describes tooling that phase
delivers; until then the step does not exist yet.

## Where things are

| What | Where |
|---|---|
| Host list, modes, auth, backends | `kubernetes/edge/hostnames.yaml` |
| Generated proxy config | `kubernetes/apps/apps/edge/proxy/Caddyfile` (never edit by hand: `just edge-render`) |
| Proxy workload | `kubernetes/apps/apps/edge/proxy/` (namespace `edge`, DaemonSet `edge-proxy`) |
| VIP | 192.168.0.51 (MetalLB pool `edge`, `kubernetes/apps/infrastructure/metallb-config/`) |
| Gateway desired state / owned IDs | `kubernetes/edge/gateway.yaml` / `kubernetes/edge/gateway-state.yaml` |
| Cloudflare DNS | `kubernetes/infra/cloudflare` (local state in the operator's checkout) |
| Tunnel connector | `kubernetes/apps/apps/edge/cloudflared.yaml` |
| UniFi controller | Cloud Key Gen2 Plus, `https://192.168.1.152`, site `default` |

## Credentials

- **UniFi API key**: `UNIFI_API_KEY`, else the OpenBao ceremony
  (`docs/runbooks/openbao-estate-credentials.md`, path `af/estate/unifi`, field `api_token`). Offline,
  `sops -d` the estate seeds (from P3). Never print it.
- **Cloudflare token**: as for the module (`kubernetes/infra/cloudflare/README.md`).

## Check the edge (from P2)

```bash
just edge-smoke                      # every registry host through the VIP, against its `smoke` outcome
kubectl --context admin@ai -n edge get ds edge-proxy
kubectl --context admin@ai -n edge get svc edge-proxy -o jsonpath='{.status.loadBalancer.ingress[0].ip}'   # must be 192.168.0.51
kubectl --context admin@ai -n edge logs ds/edge-proxy -c caddy --tail=50
```

To find which node announces the VIP, check `ip neigh show 192.168.0.51` on a LAN host and match
the MAC to a control-plane VM, or read the speaker logs in `metallb-system`.

## Add a hostname (complete from P5)

Until P5, cloudflared still routes each host from its own rule in `cloudflared.yaml`, and the DNS
record comes from the Cloudflare module only from P4. A host added before then needs those two by
the old procedure (`docs/runbooks/internet-exposure.md`) as well, or only the LAN VIP serves it.
Steps 1-3 work from P2; step 4 needs the gateway reconciler (P3) and the registry-driven DNS (P4).

1. Add the entry to `hostnames.yaml`, with every security field and a `smoke` outcome, and start it
   at `edge: tunnel` and `lan_dns: false`.
2. In the SAME PR, if the backend has an ingress NetworkPolicy, admit the `edge-proxy` pods (namespace
   `edge`, `app.kubernetes.io/name: edge-proxy`), or the proxy cannot reach it and step 4 fails.
3. `just edge-render` (it rewrites the Caddyfile and `kubernetes/edge/dns-records.json`), commit both,
   open the PR. CI validates the schema and parity, checks both files are current, and runs the runtime
   fixture. Merge, and wait for Flux to reconcile the policy and the proxy.
4. `just edge-smoke --host <name>` from the LAN. Only when it passes: set `lan_dns: true`, run
   `just edge-gateway-plan`, then `just edge-gateway-apply --plan-hash <hash>`, then `tofu apply` for
   its DNS record.

## Switch a host between tunnel and direct (from P4)

**To direct** (planned):
1. Set `edge: direct`, `just edge-render`, commit both files, merge. Flux updates the proxy, so WAN 443
   now serves the host.
2. `tofu apply` from a checkout at that merge: the DNS record becomes a DNS-only CNAME to the apex.
3. Check from outside: an external HTTP check of `https://<host>.chifor.me`. The proxy's access log
   shows the probe's public address. Cloudflare flattens the CNAME, so resolvers answer with the
   apex's A record, not a CNAME: that is expected.

**Before the first host goes direct (P6)**: Caddy labels its per-host metrics with whatever `Host`
arrives over TLS (`per_host` on a TLS server observes every host; only plain-HTTP servers fall back
to `_other`), so on the WAN a scanner could grow Caddy's in-memory series, and Prometheus, without
bound. Caddy 2.11.7 has no producer-side bound for TLS hosts short of dropping `per_host`, so choose
one first: drop `per_host` (EdgeUpstreamErrors then reads the whole proxy, not one host), or keep it
and drop unknown-host samples at scrape time with a `metricRelabelings` `action: keep` on
`source_labels: [host]`, on the Caddy endpoint (`source=caddy`) only. Its regex must also match the
EMPTY value, because a series without a `host` label (Caddy's process and Go metrics) reads as "" and
would otherwise be dropped too: `^(|router\.chifor\.me|git\.chifor\.me|...)$`, every registry host. The
second protects Prometheus only, and Caddy still holds every series (restarts reset it). Do not relabel unknown hosts to `_other`: relabeling does not aggregate, it makes
duplicate series within one scrape.

**To tunnel** (planned): run `tofu apply` first with the host set to `tunnel`, wait 300 s (the apex
TTL), then merge the proxy change. In an emergency do both at once and accept up to 300 s of 403
for clients holding the old answer.

**Everything to the tunnel**: set `force_tunnel: true`, commit, `tofu apply`, merge.

## DNS records and their state (from P4)

`kubernetes/infra/cloudflare/dns.tf` creates exactly the records in `kubernetes/edge/dns-records.json`,
which `just edge-render` generates from the validated registry. Its plan FAILS (lifecycle preconditions)
when the file was not rendered from the committed registry (a hash of it), names another zone or tunnel,
or holds a record of no valid shape. Fix the registry and re-render; never edit the JSON.

**Applying.** A plain `tofu plan` of this module is not clean: the live Trueswarm admin gate was applied by
`scripts/trueswarm-admin-access.sh` with inputs `terraform.tfvars` does not hold. Export them first:
`TF_VAR_enable_trueswarm_admin=true`, `TF_VAR_enable_trueswarm_admin_e2e=true`,
`TF_VAR_publish_trueswarm_admin=false` (keeps that script's own pending DNS import out of an edge apply)
and `TF_VAR_trueswarm_admin_access_client_secret` (the trueswarm-admin checkout's
`deploy/foundation/admin-access-identity.sops.yaml`, `stringData.OIDC_CLIENT_SECRET`; or the value the
operator state already holds for the identity provider; never print it). The plan must then touch only
`cloudflare_dns_record.edge[...]`.

**P4's first apply** moves the 27 records (`moved.tf`: `tunnel["x"]` becomes `edge["x"]`), imports
`relay` and creates `edge-canary`: 0 to change, 0 to destroy. Anything else: stop. Then delete
`relay-import.tf` in the next PR.

**Forward only.** Once the operator state holds `edge[...]` addresses, never plan or apply from a checkout
older than the P4 merge: it would destroy every `edge[...]` record and try to create `tunnel[...]` ones
on the same names. `tofu state list | grep -c 'dns_record.tunnel\['` is 0 on migrated state. To roll the
module back to pre-P4 code, back up the state, move the addresses back, then use the old checkout
(it has no `edge-canary`, so its plan deletes that record, which is fine):

```bash
cp terraform.tfstate terraform.tfstate.pre-rollback
for a in $(tofu state list | grep '^cloudflare_dns_record\.edge\['); do
  tofu state mv "$a" "cloudflare_dns_record.tunnel${a#cloudflare_dns_record.edge}"
done
```

**An interrupted replacement.** A CNAME/A switch deletes the record, then creates the new one. If the
apply stops in between:
- the record is gone remotely and from state: the next plan shows one create; apply it;
- the new record exists remotely but the apply did not save it: the create fails with "record already
  exists"; import it (`tofu import 'cloudflare_dns_record.edge["<host>"]' <zone_id>/<record_id>`, the id
  from the Cloudflare API), then plan clean.
No import block may target a record that is already managed: one would re-import a deleted id here and
block every plan, which is why the adopted records' blocks were deleted.

## Rehearse a mode change on edge-canary (P4)

Before the first real host changes mode (P6), run every DNS transition on `edge-canary` (a fixed answer,
nothing behind it) and record the evidence on the P4 PR. Each step is a registry edit on a scratch branch,
`just edge-render`, and a targeted apply from that checkout, with the inputs from "Applying" above:

```bash
tofu plan  '-target=cloudflare_dns_record.edge["edge-canary"]'
tofu apply '-target=cloudflare_dns_record.edge["edge-canary"]'
for i in $(seq 90); do date +%T; dig +short edge-canary.chifor.me @1.1.1.1; dig +short edge-canary.chifor.me @192.168.0.1; sleep 1; done
```

1. tunnel to direct (`edge: direct`): an in-place update, proxied CNAME to a DNS-only CNAME (answered
   flattened, as the apex's A record).
2. direct to private (`exposure: private`, drop `edge`): a replacement, CNAME to A. Measure the NXDOMAIN
   window on both resolvers.
3. private to tunnel: a replacement, A to CNAME.
4. Interrupt a replacement once (Ctrl-C after the delete) and recover as in "An interrupted replacement".

Record the provider and tofu versions, each plan's summary, each window and what each resolver answered.
The canary ends in tunnel mode, as on main; discard the scratch branch.

## The gateway (from P3)

```bash
just edge-gateway-plan                         # prints the diff and a plan hash; never mutates
just edge-gateway-apply --plan-hash <hash>     # aborts if anything changed since the plan
just edge-gateway-plan --only dhcp             # first step on a rebuild ...
just edge-gateway-apply --only dhcp --plan-hash <hash>
```

- **Owned objects**: listed in `gateway-state.yaml` (commit it after every apply). The reconciler
  never touches anything else: the Nextcloud forwards, the `*.lan` records and the other networks.
- **"collision" abort**: an unowned object has a key the registry wants. Inspect it in the UniFi UI;
  if it should be managed, run `--adopt <key>`.
- **"intent without ID" abort**: a create was interrupted. The run lists the matching objects; adopt
  the right one with `--adopt <key>`, or delete it in the UI and re-run.
- **Network-config changes** (the DHCP range) run in a declared window: UniFi has no conditional
  update, so a concurrent UI edit could be overwritten.

## Rollback

| Situation | Action |
|---|---|
| One host misbehaves direct | Switch it to tunnel (above) |
| All direct hosts | `force_tunnel: true` + `tofu apply` + merge |
| The proxy or MetalLB is broken (after P5) | (1) `force_tunnel` + `tofu apply`; (2) `git revert` of the P5 commit, which takes cloudflared back to per-host backends (app policies still admit cloudflared until P9); (3) set `lan_dns: false` for the affected hosts, then `just edge-gateway-plan` and `just edge-gateway-apply --plan-hash <hash>`; (4) wait out the caches (300 s public, 60 s local) |
| Stop serving the WAN at once | `port_forward.enabled: false` in `gateway.yaml`, then `just edge-gateway-plan` and `just edge-gateway-apply --plan-hash <hash>` |

Every apply, emergency or not, takes the hash of the plan it carries out; `--only` narrows both the plan
and the apply to one object type and is part of the hash.

**When git.chifor.me is behind the broken proxy**: push through a port-forward. It reaches the
cluster API through the Talos VIP, not the proxy:

```bash
kubectl --context admin@ai -n gitea port-forward svc/gitea-http 3000:3000
git push http://localhost:3000/cchifor/ailab.git HEAD:<branch>
```

Gitea mirrors to GitHub, where Flux reads.

## Recovery access without the proxy

From the LAN or the VPN:
- **Proxmox**: `https://192.168.0.2:8006` (also .3, .4).
- **QNAP**: `https://192.168.1.225`.
- **Dev-worker terminals**: `https://192.168.0.8` to `.11`.

These logins do not depend on the cluster. The OpenBao UI has no cluster-independent path; use
`kubectl port-forward` to `svc/openbao-ui`.

## Drain one proxy node (from P2)

```bash
kubectl --context admin@ai -n edge exec <edge-proxy-pod> -c caddy -- touch /config/drain   # readiness fails; MetalLB moves the VIP
kubectl --context admin@ai -n edge exec <edge-proxy-pod> -c caddy -- rm /config/drain
```

## Certificates (from P2)

`edge/edge-wildcard` (`*.chifor.me`) renews through cert-manager. The `cert-reloader` sidecar
reloads Caddy within 5 minutes of a valid new pair. Alerts:
- `EdgeCertReloadFailing`: check the sidecar's logs; a mismatched pair is kept out on purpose.
- `EdgeCertExpiringSoon`: the served certificate is under 14 days.

## Inventory notes

- **Gateway**: UniFi UXG (`UXGA6AA`). WAN1 is PPPoE; `Internet 2` is configured but not connected,
  so there is no WAN failover. DDNS for `chifor.me` and `chifor.net` runs on the gateway.
- **WAN forwards**:
  - 9443 -> 192.168.0.160:443 and 80 -> 192.168.0.160:80 (Nextcloud, not ours);
  - 443 -> 192.168.0.51:443 (`ailab-edge-https`, from P3).
- **Removed 2026-10-27**: the dead 443 -> .250 (`k8s`) and 22 -> .68 (`git`) forwards. The DHCP pool
  then started at .50 (cloud-ci-11's static address) and now starts at .52.
