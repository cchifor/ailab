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
(Account, Edit), `Access: Organizations, Identity Providers, and Groups` (Account, Edit — required by the
Authelia IdP in `access.tf`), `DNS` (Zone=chifor.me, Edit). **No** Cloudflare Tunnel scope.

> The Organizations/IdPs/Groups scope is easy to omit: **read** on it is enough to `plan` the identity
> provider, so a token missing it produces a clean plan and then fails at `apply`.

## DNS

`dns.tf` manages one record per host of the edge registry (ADR 0040): it creates exactly the records in
`kubernetes/edge/dns-records.json`, which `just edge-render` generates from `kubernetes/edge/hostnames.yaml`
after validating it. Add, remove or switch a host in the registry, never here; the plan refuses a stale or
hand-edited file. The output `edge_records` lists them.

Before P4 (2026-10-27) the resource was `cloudflare_dns_record.tunnel`, fed by `var.tunnel_hostnames`;
`moved.tf` carries the state across, forward only. Records first created outside Terraform were adopted by
import blocks, each deleted once adopted (the last, `relay-import.tf`, after P4's apply on 2026-10-27). Read
`docs/runbooks/edge-ingress.md`, "DNS records and their state", before running an older checkout, a
targeted apply or a rollback against this state.

## Access (opt-in)

`access.tf.example` holds ready-to-activate Access scaffolding. Per the per-host policy, only
`status` (interactive Allow) and `api` (Service Token, non-interactive) are sensible candidates;
`sso` must never be gated, and `home`/`chat`/`grafana` stay with Authelia. See
`docs/runbooks/cloudflare-access-apps.md`.
