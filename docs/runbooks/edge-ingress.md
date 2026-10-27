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

**Every registry edit is followed by `just edge-render`**, and the registry, the Caddyfile and
`kubernetes/edge/dns-records.json` are committed together: from P4 the DNS plan refuses a registry it
was not rendered from (and CI refuses stale generated files).

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
4. `just edge-smoke --host <name>` from the LAN. Only when it passes: set `lan_dns: true`,
   `just edge-render`, commit and merge; then `just edge-gateway-plan`,
   `just edge-gateway-apply --plan-hash <hash>`, and `tofu apply` for its DNS record from a checkout at
   that merge.

## Switch a host between tunnel and direct (from P4)

**To direct** (planned):
1. Set `edge: direct`, `just edge-render`, commit both files, merge. Flux updates the proxy, so WAN 443
   now serves the host.
2. `tofu apply` from a checkout at that merge: the DNS record becomes a DNS-only CNAME to the apex.
3. Check from outside: an external HTTP check of `https://<host>.chifor.me`. The proxy's access log
   shows the probe's public address. Resolvers answer with the CNAME and the apex's A record
   (Cloudflare flattens only the apex by default): that is expected. The apex A record must stay
   DNS-only (grey cloud); proxied, every direct host would resolve to Cloudflare and fail there.

**Per-host metrics are off** (since 2026-10-27, before WAN 443 opened): on a TLS server Caddy labels
its per-host metrics with whatever `Host` arrives, so from the WAN a scanner could grow Caddy's memory and
Prometheus without bound, and Caddy 2.11.7 has no producer-side bound short of dropping `per_host`.
EdgeUpstreamErrors reads per listener (`server`: direct, tunnel, redirect); the access log names the hosts
(`kubectl -n edge logs -l app.kubernetes.io/name=edge-proxy -c caddy`). The cost: one low-traffic host failing
100 % pages only if it carries over a fifth of its listener's requests (Gatus probes only a few edge URLs:
agentforge, Keycloak's realms, the registry). Follow-up: a bounded per-host availability signal, probes of the
registry's hosts through the VIP. Do not bring `per_host` back without
a bound; a scrape-time relabel would protect Prometheus only (and must not map unknown hosts to `_other`:
relabeling does not aggregate, it makes duplicate series within one scrape).

**To tunnel** (planned): on a branch, set the host to `tunnel` and `just edge-render`; run `tofu apply`
from that branch first, wait 300 s (the apex TTL), then merge (the proxy change). In an emergency do
both at once and accept up to 300 s of 403 for clients holding the old answer.

**Everything to the tunnel**: set `force_tunnel: true`, `just edge-render`, commit, `tofu apply` from
that commit, merge.

## DNS records and their state (from P4)

`kubernetes/infra/cloudflare/dns.tf` creates exactly the records in `kubernetes/edge/dns-records.json`,
which `just edge-render` generates from the validated registry. Plan and apply with `just edge-dns-plan`
and `just edge-dns-apply` (arguments pass through): they run `render.py --check` first, so the file is
the renderer's own output, not just self-consistent. Its plan FAILS (preconditions on the
output `edge_dns_artifact` for an untargeted plan, and on each record, which a `-target` plan keeps) when the file was not rendered
from the committed registry (a hash of it), when its records are not the ones rendered for that registry
(a hash over both: a record edited, added or dropped by hand), when its hostnames are not exactly the
registry's hosts, when it names another zone or tunnel, or when a record has no valid shape. Fix the
registry and re-render; never edit the JSON.

Any registry edit moves both hashes, even one that changes no record (a `lan_dns` flip, a comment):
`registry_sha256` is over the registry text, and `binding_sha256` over that text plus the records. That
is the whole file; the LAN records live on the gateway (`gateway.py`), not here. `render.py --check`
(CI, every push) is what proves the committed file current.

**Applying.** A plain `tofu plan` of this module is not clean: the live Trueswarm admin gate was applied by
`scripts/trueswarm-admin-access.sh` with inputs `terraform.tfvars` does not hold. Export them first:
`TF_VAR_enable_trueswarm_admin=true`, `TF_VAR_enable_trueswarm_admin_e2e=true`,
`TF_VAR_publish_trueswarm_admin=false` (keeps that script's own pending DNS import out of an edge apply)
and `TF_VAR_trueswarm_admin_access_client_secret` (the trueswarm-admin checkout's
`deploy/foundation/admin-access-identity.sops.yaml`, `stringData.OIDC_CLIENT_SECRET`; or the value the
operator state already holds for the identity provider; never print it). The plan must then touch only
`cloudflare_dns_record.edge[...]`.

**Before P4's first apply** a single-record `-target` is refused (the moves are pending; tofu asks to target
`cloudflare_dns_record.edge` and `cloudflare_dns_record.tunnel` whole), so the first apply is untargeted.

**P4's first apply** moved the 27 records (`moved.tf`: `tunnel["x"]` became `edge["x"]`), imported
`relay` and created `edge-canary` (2026-10-27, state serial 48 to 49: "1 imported, 1 added, 0 changed,
0 destroyed"; the plan after it: no changes). `relay-import.tf` went in the next PR, so no import block
is left in the module. Adopting a record created outside Terraform again: one import block, applied,
then deleted in the next PR.

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
`just edge-render`, and a targeted apply from that checkout, with the inputs from "Applying" above.

Start the sampler in a second terminal BEFORE each apply, and keep it running until both resolvers have
shown the new answer for longer than the record's TTL. It logs each resolver's status (NOERROR, NXDOMAIN)
with the answer, so a gap is visible as such and not as an empty line:

```bash
while true; do for r in 1.1.1.1 192.168.0.1; do
  printf '%s %-11s ' "$(date +%T)" "$r"
  dig +noall +comments +answer edge-canary.chifor.me @"$r" | grep -E 'status:|IN' | sed 's/.*status: \([A-Z]*\).*/\1/' | tr '\n' ' '
  echo
done; sleep 1; done | tee -a canary-dns.log
```

```bash
tofu plan  '-target=cloudflare_dns_record.edge["edge-canary"]'
tofu apply '-target=cloudflare_dns_record.edge["edge-canary"]'
```

1. tunnel to direct (`edge: direct`): expected an in-place update, proxied CNAME to a DNS-only CNAME
   (answered as the CNAME plus the apex's A record).
2. direct to private (`exposure: private`, drop `edge`): expected a replacement, CNAME to A (provider
   5.24.0 planned the type change as `must be replaced` on 2026-10-27). Measure the NXDOMAIN window on
   both resolvers. Record what each plan actually says (`update in-place` or `must be replaced`).
3. private to tunnel: a replacement, A to CNAME.
4. Interrupt a replacement. One Ctrl-C lets tofu finish its in-flight call and save state, so it can only
   leave the first case of "An interrupted replacement" (the record deleted, the create never sent);
   rehearse that. The second case (created remotely, not saved) needs a hard stop during the create (a
   second Ctrl-C, or killing tofu); rehearse it too if you can time it, and record which case you got.

Record the provider and tofu versions, each plan's summary, each window and what each resolver answered.
A resolver that kept serving its cached answer through the replacement hides the gap: record that as
"not observed (cached until <time>)", never as a zero-length gap.
The canary ends in tunnel mode, as on main; discard the scratch branch.

## The gateway (from P3)

```bash
just edge-gateway-plan                         # prints the diff and a plan hash; never mutates
just edge-gateway-apply --plan-hash <hash>     # aborts if anything changed since the plan
just edge-gateway-plan --only dhcp             # first step on a rebuild ...
just edge-gateway-apply --only dhcp --plan-hash <hash>
```

- **Owned objects**: listed in `gateway-state.yaml` (commit it after every apply): the LAN network
  whose DHCP range the reconciler sets, its DNS records and its WAN forward. It never touches anything
  else: the Nextcloud forwards, the `*.lan` records, the other networks. On a controller the state
  does not know yet, take the network over first: `just edge-gateway-plan --adopt Home`, then apply
  that plan (bookkeeping only; the range changes only if it differs). Until then a range difference
  aborts.
- **Local records**: each `lan_dns` host is a CNAME to `edge-vip.lan`, whose one A record is the VIP
  (`gateway.yaml` `local_dns.vip_name`). Check a host with `nslookup -type=A <host> 192.168.0.1` (the
  alias, then 192.168.0.51) and `-type=AAAA` (a name, no addresses). An A record per host would let the
  resolver forward AAAA upstream, and a dual-stack client would take Cloudflare's IPv6.
  After every apply, run `just edge-gateway-plan` again and expect `no changes`: a controller that stores
  a CNAME value normalised (a trailing dot, another case) would otherwise show as an update on every run.
- **An owned ID with another identity aborts**: a record whose key, a forward whose name, or a
  network whose name no longer matches the state (renamed, or the ID reused). Inspect it in the UI;
  `--forget <recorded name>` drops it, then `--adopt` if it is in fact ours.
- **"collision" abort**: an unowned object has a key the registry wants. Inspect it in the UniFi UI;
  if it should be managed, run `--adopt <key>`. A second record with our key, a second forward with
  our name, or any other enabled forward on WAN 443 aborts even when ours exists, and every check runs
  again from a fresh read right before each change.
- **"interrupted create" abort**: a create's outcome is unknown. `--adopt <key>` takes the object only
  if it holds the fields the create sent (the state records them); otherwise delete it in the UI and
  `--forget <key>`.
- **The DHCP range** is changed only after a check that no active client holds an address the change
  gives back (other than its fixed IP); the check runs at plan time and again right before the write.
  Move or reserve those clients, or re-plan with `--ack-dhcp-leases` (it is part of the plan hash).
  Network-config changes still run in a declared window: UniFi has no conditional update, so a
  concurrent UI edit could be overwritten.
- **The WAN forward** (create, or an update that enables it) first needs the VIP to serve
  edge-canary's own answer with a valid certificate. `--skip-vip-check` overrides that, is part of the
  plan hash and prints a warning in the plan.

## Rollback

| Situation | Action |
|---|---|
| One host misbehaves direct | Switch it to tunnel (above) |
| All direct hosts | `force_tunnel: true` + `just edge-render` + `tofu apply` + merge |
| The proxy or MetalLB is broken (after P5) | (1) `force_tunnel` + `just edge-render` + `tofu apply`; (2) `git revert` of the P5 commit, which takes cloudflared back to per-host backends (app policies still admit cloudflared until P9); (3) set `lan_dns: false` for the affected hosts + `just edge-render`, then `just edge-gateway-plan` and `just edge-gateway-apply --plan-hash <hash>`; (4) wait out the caches (300 s public, 60 s local) |
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
