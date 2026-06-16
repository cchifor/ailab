# Cloudflare (Zero Trust) — IaC module

Manages the **`chifor.me` DNS CNAMEs** and (optionally) **Zero Trust Access** apps/policies/service-tokens
for the lab, via the `cloudflare/cloudflare` v5 provider. A sibling root module to `../` (Talos) and
`../ai-lxc/`, with its own local, gitignored state.

## Ownership boundary (important)

The cloudflared tunnel is **locally-managed**: its ingress/route table lives in git at
`kubernetes/apps/apps/edge/cloudflared.yaml` (Flux-reconciled). A tunnel is *either* locally- *or*
remotely-managed — you can't mix them. So:

| Concern | Owner |
|---|---|
| Tunnel object + credentials | cloudflared CLI + SOPS/Flux |
| Tunnel **ingress/routes** | git + Flux (`cloudflared.yaml`) |
| DNS CNAMEs → `*.cfargotunnel.com` | **this module** (`dns.tf`) |
| Access apps / policies / IdP / service tokens | **this module** (`access.tf`, opt-in) |

This module **must not** create `cloudflare_zero_trust_tunnel_cloudflared` or its `_config` — `tunnel_id`
is a plain variable used only to build CNAME targets.

## Usage

```bash
export CLOUDFLARE_API_TOKEN="<scoped DNS+Access token>"   # provider reads this automatically
cp terraform.tfvars.example terraform.tfvars              # fill account_id + zone_id (gitignored)
terraform init
terraform plan      # adopt-existing records are imported (imports.tf); only status is created
terraform apply
```

Token scopes (least privilege): `Access: Apps and Policies` (Account, Edit), `Access: Service Tokens`
(Account, Edit), `DNS` (Zone=chifor.me, Edit). **No** Cloudflare Tunnel scope.

## DNS

`dns.tf` manages a proxied CNAME per `tunnel_hostnames` entry. Records already created by
`cloudflared tunnel route dns` (home/sso/chat/grafana/api) are adopted via `imports.tf`; the only record
this **creates** is `status.chifor.me` (previously 530-ing for lack of a DNS record).

## Access (opt-in)

`access.tf.example` holds ready-to-activate Access scaffolding. Per the per-host policy, only
`status` (interactive Allow) and `api` (Service Token, non-interactive) are sensible candidates;
`sso` must never be gated, and `home`/`chat`/`grafana` stay with Authelia. See
`docs/runbooks/cloudflare-access-apps.md`.
