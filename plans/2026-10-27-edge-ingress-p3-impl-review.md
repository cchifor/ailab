# Implementation review — edge-ingress P3 — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- TLS pinning, read-only DDNS handling, and field-preserving writes are present.
- DHCP ownership is not bound to `gateway-state.yaml`, and range changes do not check live leases.
- Owned-forward identity and collision handling can affect or expose the wrong controller target.
- The WAN canary gate is skipped for updates, can be bypassed outside the plan hash, and only checks a generic HTTP 200.
- The OpenBao provisioning matrix contains a literal `\n`, so the estate provisioning job fails before seeding UniFi.

## Findings

### DHCP network ownership is outside the state file

**Location:** scripts/edge/gateway.py:326; kubernetes/edge/gateway-state.yaml:8

**Severity:** blocker

<!-- codex: The DHCP PUT targets `lan["network_id"]` from mutable `gateway.yaml`, while the committed state records no DHCP network ID. A stale or changed configuration can therefore mutate a network object that is not state-owned; record the Home network ID/type in state and bind it to the controller fingerprint before allowing updates. -->

### An owned forward ID can be repurposed

**Location:** scripts/edge/gateway.py:406

**Severity:** blocker

<!-- codex: The owned branch trusts only the stored ID and never verifies that the current object still has the recorded/configured name. If the UI renames or reuses that ID, reconciliation can update or delete an unrelated forward; require an exact name/type match and stop for explicit forget/adopt recovery. -->

### The WAN gate is skipped when an owned forward is updated

**Location:** scripts/edge/gateway.py:519

**Severity:** blocker

<!-- codex: `vip_answers()` runs only for `action == "create"`, so a manually disabled or retargeted owned forward is updated to an enabled VIP forward without proving the canary works. The probe also accepts any CA-valid service returning HTTP 200 for the canary Host; gate every transition that exposes 443 and verify a unique canary response marker. -->

### `--skip-vip-check` is not bound to the plan hash

**Location:** scripts/edge/gateway.py:447

**Severity:** blocker

<!-- codex: The hash options include `only`, `adopt`, and `forget`, but omit `--skip-vip-check`. A reviewed normal plan hash can therefore be applied with the canary gate disabled; include this safety-affecting option in the hash or remove the bypass from ordinary apply. -->

### Estate provisioning is broken by a literal escape

**Location:** kubernetes/apps/infrastructure/security/openbao/estate-provision-job.yaml:105

**Severity:** blocker

<!-- codex: Inside the shell's double-quoted `MATRIX`, `\n` remains literal, so the loop receives a `\n` token and looks for a seed named `\n.json`; the Job exits before seeding `unifi`. Put `unifi:api_token` on a real continued line and add a rendered-shell test. -->

### Collision checks are bypassed once an object is owned

**Location:** scripts/edge/gateway.py:361

**Severity:** important

<!-- codex: An owned DNS record skips duplicate-key checks, and an owned forward ignores `same_name` and WAN-443 `conflicts`; a later unowned duplicate or 443 forward can remain while apply proceeds. Recheck the full inventory immediately before each relevant mutation and abort on every collision, including when an owned object already exists. -->

### DHCP changes can shrink beneath live leases

**Location:** scripts/edge/gateway.py:334

**Severity:** important

<!-- codex: Planning checks only that the VIP is outside the desired range and never inspects active leases or reservations before shrinking `.50` to `.52` or any future range. Query leases and refuse unsafe shrinkage, or require an explicit maintenance acknowledgement, with a live-lease fixture test. -->

### Operation sorting does not implement the required precedence

**Location:** scripts/edge/gateway.py:445

**Severity:** important

<!-- codex: `action_rank` is applied only after resource rank and key, so a DNS create can precede a delete for another key, and a DHCP update precedes a forward delete during disable. Use explicit phases that satisfy forget, adopt, delete, update, create while preserving the documented forward-disable ordering, and test mixed operations. -->

### Intent records omit the desired fields

**Location:** scripts/edge/gateway.py:531

**Severity:** important

<!-- codex: The journal stores only kind, key, and timestamp although the plan requires desired fields. Recovery cannot establish that the matching object is the interrupted create; persist the intended fields and controller binding and compare them during adoption. -->

### Classic list pagination is not handled

**Location:** scripts/edge/gateway.py:128

**Severity:** important

<!-- codex: `_classic_list()` accepts any `data` list without checking pagination metadata, so a first page can make an owned object appear absent and schedule its deletion. Consume all pages or reject truncated responses, and add a paginated-response test. -->

## Diff stat

 .gitea/workflows/manifests.yaml                    |   2 +-
 docs/runbooks/openbao-estate-credentials.md        |   2 +
 docs/runbooks/openbao-recovery.md                  |   2 +-
 justfile                                           |  14 +-
 .../security/openbao/estate-provision-job.yaml     |   2 +-
 .../security/openbao/estate-seeds.sops.yaml        |  48 +-
 kubernetes/edge/gateway-state.yaml                 |  10 +
 kubernetes/edge/gateway.yaml                       |  52 ++
 scripts/edge/gateway.py                            | 609 +++++++++++++++++++++
 scripts/tests/test_edge_gateway.py                 | 356 ++++++++++
 10 files changed, 1071 insertions(+), 26 deletions(-)