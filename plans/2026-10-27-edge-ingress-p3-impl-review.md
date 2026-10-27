# Implementation review — edge-ingress P3 — round 2

<!-- codex-impl-review-status: finalized -->

## Round-1 verification

- **DHCP network ownership is outside the state file — resolved.**
- **An owned forward ID can be repurposed — incomplete in round 2; resolved below** (the final read before the write; forget by the recorded name).
- **The WAN gate is skipped when an owned forward is updated — incomplete in round 2; resolved below** (HTTP framing).
- **`--skip-vip-check` is not bound to the plan hash — resolved.**
- **Estate provisioning is broken by a literal escape — resolved.**
- **Collision checks are bypassed once an object is owned — incomplete in round 2; resolved below** (re-checked in the final read).
- **DHCP changes can shrink beneath live leases — incomplete in round 2; resolved below** (rows without a network ID; IPv6).
- **Operation sorting does not implement the required precedence — resolved.**
- **Intent records omit the desired fields — resolved.**
- **Classic list pagination is not handled — resolved.**

## Findings

All six round-2 findings were accepted and fixed (8b386736); none is contested. Each new test fails on the
round-1 code.

### The final update snapshot bypasses freshness and collision checks
**Location:** scripts/edge/gateway.py:715
**Severity:** blocker
**Resolution (accepted):** the gates (canary, leases) run first; then one final read right before the
mutation re-checks the object against the plan's digest and re-runs the collision checks, for creates
and updates alike, and the update writes from that read. Tests: a forward renamed and a 443 forward
added while the canary gate runs each stop the write (no PUT, no POST, no intent left).

### The canary gate does not parse HTTP response framing
**Location:** scripts/edge/gateway.py:632
**Severity:** important
**Resolution (accepted):** `canary_ok` parses the raw answer with `http.client.HTTPResponse`: exactly 200,
a complete body (Content-Length or chunked), the marker in the decoded body, at most 64 KiB. Tests: a
truncated Content-Length body, a marker only in a chunked trailer or a header, garbage and an oversized
answer fail; a marker split across chunks passes.

### A client without a network ID bypasses lease protection
**Location:** scripts/edge/gateway.py:356
**Severity:** important
**Resolution (accepted):** a row that names no network counts (conservatively); only rows naming another
network are skipped. Test included.

### IPv6 client addresses crash the IPv4 lease check
**Location:** scripts/edge/gateway.py:362
**Severity:** nit
**Resolution (accepted):** addresses of another family than the range are skipped. Test: IPv6 clients,
with and without a network ID, with and without `--ack-dhcp-leases`.

### Forward recovery uses the wrong forget key
**Location:** scripts/edge/gateway.py:504
**Severity:** important
**Resolution (accepted):** the forget matches the recorded name, and is allowed when the object is gone,
renamed, or renamed in gateway.yaml. Test: after OLD becomes NEW in the controller and gateway.yaml,
`--forget OLD --adopt NEW` plans and applies, and the next plan is clean.

### Apply omits the required Nextcloud preservation assertion
**Location:** scripts/edge/gateway.py:734
**Severity:** important
**Resolution (accepted):** apply snapshots every listed object the reconciler neither owns nor is about to
touch (the Nextcloud forwards, the `*.lan` records, the other networks) and, after the operations, fails
loudly naming any that changed or vanished. Test: Nextcloud retargeted during the edge forward's create.

## Deferred to live verification

After merge: adopt the Home network (`plan --adopt Home`, bookkeeping only) and commit the state; the
local DNS records for smoke-passed hosts; then the WAN 443 forward (`port_forward.enabled: true`) with
NAT loopback and client-source evidence, and the Home network, Nextcloud forwards and the 60 `*.lan`
records shown unchanged, recorded on the P3 PR.
