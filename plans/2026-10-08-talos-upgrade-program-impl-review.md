# Implementation review — talos-upgrade-program — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- The wrapper correctly constructs per-node Factory images with the live schematic, passes `--wait`, never passes `--force`, and enforces basic refusal gates (downgrade, skip-minor, no-op, missing schema, wrong platform, HTTP 404).
- Five important issues affect operational safety: Factory redirects rejected as 404; probe failures passing guards due to missing `set -e` and error checks; unconditional CP selection without reachability validation; version-matching by filename alone risking collisions; JSON parsing too brittle for whitespace/field-order variations.
- Test coverage is nominal: stubs do not assert request arguments, probe network conditions, or simulate failover scenarios. Bash syntax passes, but live HTTP behavior is unverified.

## Findings

### Factory redirects are rejected as missing images

**Location:** `scripts/talos-upgrade-node.sh:69`  
**Severity:** important
<!-- codex: Image Factory documents a 307 redirect flow for manifest requests, but this probe neither follows redirects nor handles backing-registry authentication, so it can reject an available image. Use registry-aware manifest inspection or implement that flow, and test redirects ending in both success and failure; see https://github.com/siderolabs/image-factory/blob/main/docs/api.md#oci-registry-frontend-api. -->

### Failed probes can pass their guards

**Location:** `scripts/talos-upgrade-node.sh:47`, `:62`, `:65`, `:69`  
**Severity:** important
<!-- codex: The script enables pipefail without errexit or explicit status checks, so valid-looking output from a failed talosctl command—or curl reporting HTTP 200 before a transfer error—can satisfy the following guard. Check command success before parsing complete output, retain useful diagnostics, and distinguish transport errors and HTTP 5xx from an actual 404. -->

### CP selection does not establish a reachable survivor

**Location:** `scripts/talos-upgrade-node.sh:76`  
**Severity:** important
<!-- codex: The first non-target CP is selected unconditionally: upgrading .41 still selects an unavailable .42 when .43 is reachable, while TALOS_CP_IPS containing only the target leaves the endpoint override empty and permits routing through that target. Validate and document the override, probe candidates with bounded timeouts, and explicitly refuse a CP upgrade when no reachable non-target endpoint exists. -->

### Client filenames do not prove version identity

**Location:** `scripts/talos-upgrade-node.sh:43`, `:59`  
**Severity:** important
<!-- codex: Both v1.11.12 and v1.1.112 map to talosctl-1112.exe without checking the binary's actual version; additionally, sort -V ranks suffix 11311 above 1142, contradicting the "newest client" claim. Verify candidates with version --client, compare semantic version components, and use the verified matching client for subsequent metadata reads as well as the upgrade. -->

### JSON extraction depends on formatting and field order

**Location:** `scripts/talos-upgrade-node.sh:62`, `:65`  
**Severity:** important
<!-- codex: The schematic regex requires adjacent name-then-version fields with space-only whitespace, rejects valid reordered or tab-separated JSON, and accepts matching fragments without validating their structural location or uniqueness; the platform parser likewise searches arbitrary matching text. Parse the complete JSON resource stream structurally, select spec.metadata.name == "schematic", and require exactly one valid 64-character hexadecimal schematic plus the expected spec.platform. -->

### Stubs cannot detect important integration failures

**Location:** `scripts/tests/test-talos-upgrade-node.sh:32`, `:47`  
**Severity:** important
<!-- codex: The curl stub returns 200 regardless of URL, headers, or method, and the talosctl stub ignores endpoint availability and binary-version identity, allowing incorrect implementations of those requirements to pass. Assert request arguments and absence of --force, then cover redirects, timeouts, partial-output failures, CP3/failover/no-survivor cases, client collisions, later upgrade phases, and TALOS_OUT-unset worktree resolution. -->

### Printed commands lose argument boundaries

**Location:** `scripts/talos-upgrade-node.sh:86`  
**Severity:** nit
<!-- codex: Joining cmd with ${cmd[*]} produces an ambiguous command when the Windows checkout path contains spaces, although actual array-based execution remains correctly quoted. Print shell-escaped arguments with printf '%q ' and identify dry-run output as a planned command. -->

## Summary continued

The implementation is focused and well commented, but needs changes before operational sign-off.

- **Core behavior:** It constructs the correct per-node `nocloud-installer` reference, passes `--wait`, never passes `--force`, and rejects ordinary downgrade, skipped-minor, no-op, missing-schematic, wrong-platform, and HTTP-404 cases.
- **Version selection:** Version comparisons use separate components. For running `v1.11.2`, upgrade selection uses exactly `talosctl-1112.exe`, never the similarly prefixed `talosctl-11120.exe`; the unresolved ambiguity is different versions producing the *same* filename.
- **Security:** NODE/TARGET character restrictions, the hexadecimal schematic restriction, and quoted array execution prevent straightforward shell or URL injection. Logs expose the talosconfig **path**, not its contents or credentials. NODE validation checks IPv4 shape rather than valid octet ranges.
- **Node isolation:** Every explicit talosctl invocation supplies one `-n`. Multiple configured endpoints do not themselves request multiple nodes, although `get extensions` legitimately returns multiple resource objects.
- **Paths and dry-run:** Explicit `TALOS_OUT` is quoted correctly, and the common-directory fallback resolves to the main checkout in this worktree layout. Dry-run skips upgrade and creates no local temporary files, but still performs network probes and prints logs.
- **Coverage and scope:** Tests cover both schematic families, CP1/CP2 routing, and nominal refusal cases. P0.5's IaC mapping is separate work; this wrapper should not update IaC. Pre-drain and operational gates also remain external by design.

Both files passed Bash syntax checks, and the commit passed `git diff --check`. The test suite was not executed because this environment permits no filesystem writes; the direct factory probe could not connect, so current live HTTP behavior remains unverified.

## Diff stat

 scripts/talos-upgrade-node.sh            |  91 ++++++++++++++++++++++
 scripts/tests/test-talos-upgrade-node.sh | 127 +++++++++++++++++++++++++++++++
 2 files changed, 218 insertions(+)
