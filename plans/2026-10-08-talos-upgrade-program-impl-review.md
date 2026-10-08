# Implementation review — talos-upgrade-program — round 1

<!-- codex-impl-review-status: complete -->

## Findings

### Factory redirects are rejected as missing images

**Location:** `scripts/talos-upgrade-node.sh:69`  
**Severity:** important
<!-- codex: Image Factory documents a 307 redirect flow for manifest requests, but this probe neither follows redirects nor handles backing-registry authentication, so it can reject an available image. Use registry-aware manifest inspection or implement that flow, and test redirects ending in both success and failure; see https://github.com/siderolabs/image-factory/blob/main/docs/api.md#oci-registry-frontend-api. -->
<!-- opus-pushback: Measured live: factory.talos.dev answers the manifest HEAD itself (200 + Docker-Content-Digest for v1.11.6/v1.12.12, 404 for v1.99.0, num_redirects=0); the 307 flow in that doc is for blobs. And a redirect could only ever produce a refusal (non-200 = exit 1), never a wrong image, so the probe fails closed; following redirects adds no safety. -->

### Failed probes can pass their guards

**Location:** `scripts/talos-upgrade-node.sh:47`, `:62`, `:65`, `:69`  
**Severity:** important
Resolved: every probe's exit status is checked (`probe()`); curl rc and HTTP 5xx are reported separately from 404. Tests: fail.<cmd> and curl rc/503 cases.

### CP selection does not establish a reachable survivor

**Location:** `scripts/talos-upgrade-node.sh:76`  
**Severity:** important
Resolved: CP detected from the live machine type; first survivor that answers `version` within 20 s; refusal with no survivor, a CP missing from TALOS_CP_IPS, or a malformed list. Live-verified with an unreachable candidate.

### Client filenames do not prove version identity

**Location:** `scripts/talos-upgrade-node.sh:43`, `:59`  
**Severity:** important
Resolved: clients are identified by `version --client`; the running version is read with the newest by semver. (The example mapping was off: v1.11.12 and v1.1.112 both map to `11112`, not `1112`; the collision class is real.)

### JSON extraction depends on formatting and field order

**Location:** `scripts/talos-upgrade-node.sh:62`, `:65`  
**Severity:** important
Resolved: structural parse of the JSON stream (python3/python); exactly one 64-hex schematic, exactly one platform. Tests: pretty/reordered/tabs, two schematics, decoy string.

### Stubs cannot detect important integration failures

**Location:** `scripts/tests/test-talos-upgrade-node.sh:32`, `:47`  
**Severity:** important
Resolved: the stubs log and assert request arguments, per-binary identity, per-address reachability and post-output failures; 44 new assertions (75 total), including no --force and worktree resolution.

### Printed commands lose argument boundaries

**Location:** `scripts/talos-upgrade-node.sh:86`  
**Severity:** nit
Resolved: `printf '%q '`; dry-run says the command is planned. Test: TALOS_OUT with a space.

## Diff stat

 scripts/talos-upgrade-node.sh            |  91 ++++++++++++++++++++++
 scripts/tests/test-talos-upgrade-node.sh | 127 +++++++++++++++++++++++++++++++
 2 files changed, 218 insertions(+)
