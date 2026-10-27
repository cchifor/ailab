# Implementation review — edge-ingress P4 — round 1

<!-- codex-impl-review-status: complete -->

## Findings

All seven findings were accepted; none is contested. Fixes: 371d9b24 (generated records behind
blocking preconditions) and 2a9bb1f3 (import blocks, migration and recovery docs, stale references).

### Registry identity mismatches do not stop apply
**Location:** kubernetes/infra/cloudflare/dns.tf:70
**Severity:** important
**Resolution (accepted):** the `check` block is gone. Three `lifecycle` preconditions on
`cloudflare_dns_record.edge` fail the plan: registry hash, zone and tunnel identity, record shape.
Exercised with tofu against a copy of the operator state: pointing `tunnel_id` elsewhere fails the
plan with "names another zone or tunnel".

### Invalid registry values silently become public DNS
**Location:** kubernetes/infra/cloudflare/dns.tf:35
**Severity:** important
**Resolution (accepted):** dns.tf no longer interprets the registry. It creates the records in
`kubernetes/edge/dns-records.json`, which `render.py` writes only from a registry that passed
`validate()` (unknown modes, exposure values and the access_gated/direct/Authelia rule included). The
file carries the sha256 of the registry it came from (CRs removed); a registry edit without
`just edge-render`, or a hand edit of the file, fails the plan (both exercised). The shape
precondition also refuses a record that is not exactly a tunnel, direct or private shape, so a
private record cannot point anywhere but the VIP.

### Retained import IDs obstruct interrupted replacement recovery
**Location:** kubernetes/infra/cloudflare/imports.tf:5; router-import.tf:3; relay-import.tf:2; muse-import.tf:2
**Severity:** important
**Resolution (accepted):** imports.tf, router-import.tf and muse-import.tf are deleted (their records
were adopted long ago). relay-import.tf, the one pending adoption, stays for P4's first apply; its
comment and the runbook say to delete it in the next PR. The runbook documents recovery from an
interrupted replacement: absent everywhere (plan shows a create), or created remotely but unsaved
(import by the new id).

### Older checkouts cannot safely consume migrated state
**Location:** kubernetes/infra/cloudflare/moved.tf:3
**Severity:** important
**Resolution (accepted):** the "state serial > 48" claim is removed. moved.tf and the runbook state that
the moves are forward only, with a check (`tofu state list | grep -c 'dns_record.tunnel\['` is 0 on
migrated state) and a reverse `tofu state mv` loop, after a state backup, for any rollback to pre-P4
code. The moves stay at least until P9.

### The claimed Terraform/Python parity test does not exist
**Location:** kubernetes/infra/cloudflare/dns.tf:10
**Severity:** important
**Resolution (accepted):** removed the need for one rather than adding an HCL evaluator to CI: the mode
rule now has a single implementation (`render.effective_mode()`), and DNS consumes its output.
`test_edge_render.DnsRecords` asserts type/content/proxied/TTL per mode, every host once, mode
agreement with the proxy across edge_default and force_tunnel, force_tunnel never publishing a private
host, the CR-insensitive digest, the committed file being current, and that dns.tf reads the file
behind three preconditions with no `edge_default`/`force_tunnel` logic or `check` block of its own.
That the HCL hash equals Python's is proven by the passing plan plus the failing tamper cases.

### P4 replacement rehearsal is neither documented nor currently schema-valid
**Location:** kubernetes/infra/cloudflare/dns.tf:13; docs/runbooks/edge-ingress.md:53
**Severity:** important
**Resolution (accepted):** dns.tf no longer claims a rehearsal. `validate()` now admits `auth: none`
on a private host only when its backend is a fixed `respond`, so edge-canary is valid in every mode
(tested: it renders the admin-tier 403 before its answer; a private upstream with auth none is still
refused). The runbook's "Rehearse a mode change on edge-canary" lists the transitions
(tunnel to direct, direct to private, private to tunnel, one interrupted replacement), the resolver
sampling and the evidence to record. The rehearsal itself runs after P4's first apply and its
evidence goes on the P4 PR (see "Deferred to live verification").

### Operational instructions still target the removed resource
**Location:** docs/runbooks/keycloak.md:96; kubernetes/infra/cloudflare/README.md:42
**Severity:** nit
**Resolution (accepted):** the Keycloak commands target `cloudflare_dns_record.edge["id"]`, the
README's DNS section describes the registry, the generated file, `edge_records`, the forward-only
moves and the remaining import, and the trueswarm e2e note names `edge["relay"]`.

## Deferred to live verification

P4's first apply (expected: 27 moves, the relay import, the edge-canary create, 0 changes, 0 destroys,
with the inputs in the runbook's "Applying"), the deletion of relay-import.tf after it, and the
edge-canary rehearsal with its recorded evidence.

## Diff stat

```text
 kubernetes/infra/cloudflare/dns.tf           |  79 ++++++++++++------
 kubernetes/infra/cloudflare/imports.tf       |  12 +--
 kubernetes/infra/cloudflare/moved.tf         | 118 +++++++++++++++++++++++++++
 kubernetes/infra/cloudflare/muse-import.tf   |   2 +-
 kubernetes/infra/cloudflare/outputs.tf       |   6 +-
 kubernetes/infra/cloudflare/relay-import.tf  |   2 +-
 kubernetes/infra/cloudflare/router-import.tf |   2 +-
 kubernetes/infra/cloudflare/variables.tf     |   8 +-
 8 files changed, 186 insertions(+), 43 deletions(-)
```
