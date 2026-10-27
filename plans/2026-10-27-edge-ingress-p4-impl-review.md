# Implementation review — edge-ingress P4 — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- The forward migration covers all 28 legacy default keys. The supplied result—27 no-op moves, the no-op relay import, one canary create, zero changes and destroys—is consistent with the diff. Retargeted imports retain their IDs; no import conditions changed.
- For valid inputs, effective-mode precedence matches `render.py`, including private overriding `force_tunnel`. Record types, targets, proxy flags and TTLs match the plan. Cloudflare automatically [flattens same-zone CNAMEs](https://developers.cloudflare.com/dns/cname-flattening/set-up-cname-flattening/), so direct-mode verification must allow flattened A answers.
- Access applications remain unchanged during DNS-only mode flips. CNAME/A replacement therefore does not itself remove a gate, and `create_before_destroy` would encounter Cloudflare’s record-name collision restriction. Future gate removal/replacement still needs separate sequencing; direct publication depends on Authelia already being deployed for Access-gated hosts.
- Important gaps remain in blocking validation, interrupted replacement recovery, downgrade guidance, and P4 verification. The current tunnel-only plan does not exercise these cases.
- All 24 selected renderer/schema/parity tests passed, as did `git diff --check`. No executable consumers of the removed variable or output were found; operational documentation still references the old interface.

## Findings

### Registry identity mismatches do not stop apply
**Location:** kubernetes/infra/cloudflare/dns.tf:70
**Severity:** important
<!-- codex: OpenTofu check assertions produce warnings rather than blocking execution, so a mismatched registry tunnel_id can still retarget every tunnel record despite this purported guard (https://opentofu.org/docs/language/checks/). Replace this with a blocking resource precondition and add negative tests proving zone/tunnel mismatches prevent DNS changes. -->

### Invalid registry values silently become public DNS
**Location:** kubernetes/infra/cloudflare/dns.tf:35
**Severity:** important
<!-- codex: An unknown mode such as edge: tunnnel falls through to a DNS-only CNAME to the apex, while the normal renderer load rejects it; Terraform also never checks the access_gated/direct/Authelia invariant or validates exposure before treating everything except literal private as public. Make registry validation a blocking prerequisite of DNS planning, covering unknown values and security combinations, so a local tofu apply cannot publish a registry that the proxy refuses to load. -->

### Retained import IDs obstruct interrupted replacement recovery
**Location:** kubernetes/infra/cloudflare/imports.tf:5; kubernetes/infra/cloudflare/router-import.tf:3; kubernetes/infra/cloudflare/relay-import.tf:2; kubernetes/infra/cloudflare/muse-import.tf:2
**Severity:** important
<!-- codex: If a CNAME-to-A replacement deletes an imported record and creation fails or is interrupted, its destination can be absent from state and the next plan reactivates the import against the now-deleted historical ID, preventing ordinary recovery; import blocks are inert only while their destination remains tracked (https://opentofu.org/docs/language/import/). Separate completed adoption from ongoing mode management without dropping the pending relay adoption, and test/document recovery both when no replacement exists and when creation succeeded remotely but its new ID was not saved. -->

### Older checkouts cannot safely consume migrated state
**Location:** kubernetes/infra/cloudflare/moved.tf:3
**Severity:** important
<!-- codex: These moves work only forward: applying pre-P4 configuration against migrated state plans destruction of edge instances while creating or importing tunnel instances, and retained imports do not reverse that migration; a state serial greater than 48 also does not prove a particular checkout has migrated. Document the minimum compatible checkout and authoritative-state handoff, retain the moves, and provide an explicit reverse-address migration procedure before any rollback to pre-P4 code. -->

### The claimed Terraform/Python parity test does not exist
**Location:** kubernetes/infra/cloudflare/dns.tf:10
**Severity:** important
<!-- codex: test_edge_render.py tests Python effective_mode() and proxy/backend parity, but never reads or evaluates dns.tf, and the CI workflow adds no Terraform comparison, so changing Terraform precedence or DNS tuples would leave those tests green. Add a CI test that evaluates the actual HCL across defaults, pins, force_tunnel, private hosts and rejected inputs, compares it with the renderer, and asserts type/content/proxied/TTL for every valid mode. -->

### P4 replacement rehearsal is neither documented nor currently schema-valid
**Location:** kubernetes/infra/cloudflare/dns.tf:13; docs/runbooks/edge-ingress.md:53
**Severity:** important
<!-- codex: The comment says replacement was rehearsed, but the supplied verification covers only tunnel records and the runbook contains no tunnel→direct→private→tunnel or interrupted-apply evidence; the existing fixed-response canary also cannot validate as private because render.py requires auth:none for respond backends and forbids auth:none on private hosts. Provide a valid harmless rehearsal fixture, perform and record the required transitions and interruption recovery with the provider version, observed DNS/cache behavior and authentication checks, and remove the completed-rehearsal claim until that evidence exists. -->

### Operational instructions still target the removed resource
**Location:** docs/runbooks/keycloak.md:96; kubernetes/infra/cloudflare/README.md:42
**Severity:** nit
<!-- codex: The Keycloak publication commands still target cloudflare_dns_record.tunnel["id"], and the module README directs operators to the removed tunnel_hostnames variable, so those instructions no longer describe the supported configuration. Update active commands to edge["id"], explain registry ownership and the renamed output, and include the migration prerequisites for targeted operations. -->

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