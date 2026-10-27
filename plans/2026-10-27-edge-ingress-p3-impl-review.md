# Implementation review — edge-ingress P3 — round 1

<!-- codex-impl-review-status: complete -->

## Findings

All ten findings were accepted; none is contested. Fixes: 5755d7a3 (the reconciler, its tests, the
state file, the runbook) and eb9ec154 (the provisioning matrix).

### DHCP network ownership is outside the state file
**Location:** scripts/edge/gateway.py:326; kubernetes/edge/gateway-state.yaml:8
**Severity:** blocker
**Resolution (accepted):** `gateway-state.yaml` gains `dhcp_network` ({id, name}), inside the file's
existing controller binding (URL, site, certificate fingerprint). The range is changed only on an owned
network: `--adopt Home` takes it (name and subnet verified), a difference on an unowned one aborts, and
a state that owns another network than gateway.yaml names aborts until `--forget <owned name>`. Live:
`plan --adopt Home` plans the adoption alone (the range already matches).

### An owned forward ID can be repurposed
**Location:** scripts/edge/gateway.py:406
**Severity:** blocker
**Resolution (accepted):** an owned forward must still carry its recorded name (equal to gateway.yaml's);
otherwise the run stops with no operation for it, and `--forget` then `--adopt` is the recovery. The
same identity rule now covers DNS records (key) and the network (name), and `--forget` is allowed when
an owned object is gone or has changed identity.

### The WAN gate is skipped when an owned forward is updated
**Location:** scripts/edge/gateway.py:519
**Severity:** blocker
**Resolution (accepted):** the gate runs for every forward create or update whose result is enabled. It
verifies the certificate for edge-canary (CA chain and name) and requires a 200 whose body carries the
canary's fixed answer (`edge-canary ok peer=`, derived from the registry), not any 200 (`canary_ok`).
Tests: an update is blocked when the VIP fails the gate and proceeds when it passes; a 200 from another
site and a 403 carrying the marker both fail.

### `--skip-vip-check` is not bound to the plan hash
**Location:** scripts/edge/gateway.py:447
**Severity:** blocker
**Resolution (accepted):** the option is a plan option inside the hash (apply reads it from the plan,
not the command line), and a plan with it prints a warning. The new `--ack-dhcp-leases` is bound the
same way.

### Estate provisioning is broken by a literal escape
**Location:** kubernetes/apps/infrastructure/security/openbao/estate-provision-job.yaml:105
**Severity:** blocker
**Resolution (accepted):** a real line continuation. `test_estate_provision_matrix` evaluates the
assignment with bash and checks every token is one `seed:field` (it fails on the broken file with the
stray `\n` token), plus a negative case.

### Collision checks are bypassed once an object is owned
**Location:** scripts/edge/gateway.py:361
**Severity:** important
**Resolution (accepted):** `collisions()` (a duplicate DNS key, a second forward with our name, any
other enabled forward covering the WAN port) runs whether or not ours exists, at plan time and again
from a fresh read right before each create or update. Tests: an owned record with a duplicate, an owned
forward with a new 443 neighbour (a port range), and a duplicate appearing between plan and apply.

### DHCP changes can shrink beneath live leases
**Location:** scripts/edge/gateway.py:334
**Severity:** important
**Resolution (accepted):** `lease_conflicts()` reads the active clients (`stat/sta`, seen live with
`ip`, `network_id`, `use_fixedip`, `fixed_ip`) and refuses a change that takes back an address an
active client on that network holds other than as its fixed IP, unless `--ack-dhcp-leases`. Clients are
volatile, so they stay out of the hash; the check runs at plan time and again before the write. Tests:
a refused and an acknowledged shrink, a fixed IP and another network not counting, and a client
arriving after the plan stopping the write.

### Operation sorting does not implement the required precedence
**Location:** scripts/edge/gateway.py:445
**Severity:** important
**Resolution (accepted):** explicit phases (forget, adopt, forward delete, DNS delete, DHCP update, DNS
update, DNS create, forward create/update) sort before kind and key. Test: a mixed plan (forward and one
record withdrawn, the range moved, another record added) comes out in that order.

### Intent records omit the desired fields
**Location:** scripts/edge/gateway.py:531
**Severity:** important
**Resolution (accepted):** an intent records the fields the create sends (the controller binding is the
state file's own). Adopting under an intent requires an object holding those fields; otherwise the plan
refuses and the operator deletes it and forgets the intent. Tests: the recorded fields, and a refused
adoption of a record with the key but other fields.

### Classic list pagination is not handled
**Location:** scripts/edge/gateway.py:128
**Severity:** important
**Resolution (accepted):** the classic API answered every list live with `{"meta": {"rc": "ok"},
"data": [...]}` and no paging metadata. `_classic_list` now refuses any other metadata key (a count, an
offset, a cursor) rather than reading a possible page. Test: three paged shapes refused, the real shape
accepted.

## Diff stat

```text
 .gitea/workflows/manifests.yaml                    |   2 +-
 docs/runbooks/openbao-estate-credentials.md        |   2 +
 docs/runbooks/openbao-recovery.md                  |   2 +-
 justfile                                           |   14 +-
 .../security/openbao/estate-provision-job.yaml     |   2 +-
 .../security/openbao/estate-seeds.sops.yaml        |  48 +-
 kubernetes/edge/gateway-state.yaml                 |  10 +
 kubernetes/edge/gateway.yaml                       |  52 ++
 scripts/edge/gateway.py                            | 609 +++++++++++++++++++++
 scripts/tests/test_edge_gateway.py                 | 356 ++++++++++
 10 files changed, 1071 insertions(+), 26 deletions(-)
```
