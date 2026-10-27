# Implementation review — edge-ingress P3 — round 2

<!-- codex-impl-review-status: pending -->

## Summary

- Not ready to merge: one blocker, four important findings and one nit remain.
- Six round-1 resolutions are complete; four have remaining correctness gaps.
- The phase table covers every emitted operation, including DHCP adopt/forget. Forget preserves matching owned objects; combined forget/adopt operations execute in the correct order. Legacy state without `dhcp_network` loads safely, and intents without fields cannot be silently adopted.
- Accepted the author’s test results and live-controller facts. Additional verification used in-memory fixtures and mocked sockets; the worktree is unchanged.
- The supplied evidence establishes read-only planning. P3’s live rollout acceptance—NAT loopback and preservation of Home, Nextcloud and the 60 `*.lan` records—still needs recorded evidence.

## Round-1 verification

- **DHCP network ownership is outside the state file — resolved.** Controller-bound state now records the network ID/name; changing an unowned network requires adoption, and a different owned network must be explicitly forgotten.
- **An owned forward ID can be repurposed — incomplete.** Planning checks recorded identities, but apply can overwrite a renamed object observed by its final reread. Forward recovery also selects `--forget` using the desired name instead of the recorded name.
- **The WAN gate is skipped when an owned forward is updated — incomplete.** Enabled creates and updates now run the gate with CA and hostname verification, but the response parser can accept an incomplete response or a marker outside the actual body.
- **`--skip-vip-check` is not bound to the plan hash — resolved.** Both override flags are hashed, and apply reads them from `plan["options"]`.
- **Estate provisioning is broken by a literal escape — resolved.** The matrix contains a real continuation. The regression test evaluates shell tokenization and is included by the existing CI test discovery.
- **Collision checks are bypassed once an object is owned — incomplete.** Owned-object checks now cover duplicate keys/names, port ranges and another forward’s `both` interface; disabled forwards occupy no port but still collide by name. However, apply ignores collisions visible in its final reread.
- **DHCP changes can shrink beneath live leases — incomplete.** Inclusive range subtraction and fixed-IP exemptions work, and checks run during planning and apply. Missing network IDs silently bypass protection; IPv6 addresses cause an uncaught exception.
- **Operation sorting does not implement the required precedence — resolved.** Explicit phases implement withdrawal-before-addition ordering; adopt/forget are handled separately for every kind, so emitted DHCP operations do not cause a phase-table `KeyError`.
- **Intent records omit the desired fields — resolved.** Creates persist their fields before the API call, and recovery checks those fields. Legacy intents without fields fail closed and retain the delete/forget recovery path.
- **Classic list pagination is not handled — resolved.** The verified complete envelope is accepted; additional pagination metadata is rejected before planning.

## Findings

### The final update snapshot bypasses freshness and collision checks

**Location:** scripts/edge/gateway.py:715  
**Severity:** blocker
<!-- codex: round-2: If a forward is renamed or another WAN 443 forward appears during vip_answers(), the final inventory reread observes that change, but apply checks neither the returned object's digest nor that inventory's collisions before issuing the PUT; both cases were reproduced, and the renamed forward was overwritten. Validate the final object against op["before"] and rerun collisions immediately before mutation, moving create's collision check after the canary gate as well, with regression cases that change the controller during the gate. -->

### The canary gate does not parse HTTP response framing

**Location:** scripts/edge/gateway.py:632  
**Severity:** important
<!-- codex: round-2: The gate accepts a response that closes before its declared Content-Length and even an empty chunked body whose trailer contains the marker, while rejecting a valid chunked body when the marker spans chunks. Parse the response with an HTTP parser over the verified TLS socket, require a complete bounded body and exact status 200, and inspect only the decoded body; HTTP/2 support is unnecessary here because the probe does not negotiate it. -->

### A client without a network ID bypasses lease protection

**Location:** scripts/edge/gateway.py:356  
**Severity:** important
<!-- codex: round-2: A client row containing ip=192.168.0.51 but no network_id is silently discarded, so shrinking the pool from .50 to .52 reports no conflict and permits reclaiming the occupied address without --ack-dhcp-leases. Reject incomplete client rows or conservatively check unknown-network addresses against the removed portion of the IPv4 range, with coverage at both planning and apply time. -->

### IPv6 client addresses crash the IPv4 lease check

**Location:** scripts/edge/gateway.py:362  
**Severity:** nit
<!-- codex: round-2: An IPv6 address on a client with the matching network_id reaches the comparison against IPv4 pool bounds and raises an uncaught TypeError, including when --ack-dhcp-leases is supplied. Check address families before comparison and exclude IPv6 addresses from this IPv4 DHCP calculation, with a mixed-family regression case. -->

### Forward recovery uses the wrong forget key

**Location:** scripts/edge/gateway.py:504  
**Severity:** important
<!-- codex: round-2: After the controller forward and gateway.yaml are renamed from OLD to NEW while state still records OLD, the documented --forget OLD --adopt NEW recovery remains blocked because the forget branch tests the desired NEW name; the error nevertheless instructs the operator to forget OLD. Select and remove stale ownership using its recorded name, preserving the identity-change guard, and test this recovery through apply. -->

### Apply omits the required Nextcloud preservation assertion

**Location:** scripts/edge/gateway.py:734  
**Severity:** important
<!-- codex: round-2: The finalized P3 plan requires both Nextcloud forwards to be asserted unchanged after every apply, but verification examines only each mutated object; an in-memory apply still reported success when a Nextcloud target changed during creation of the edge forward. Retain a baseline of the protected Nextcloud objects and compare their IDs and non-secret fields after apply, failing loudly on disappearance or drift and adding a regression for that case. -->