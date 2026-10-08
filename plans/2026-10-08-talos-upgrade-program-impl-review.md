# Implementation review — talos-upgrade-program — round 2

<!-- codex-impl-review-status: complete -->

## Findings

### Failed client-identity probes are still accepted

**Location:** scripts/talos-upgrade-node.sh:119  
**Severity:** important
Resolved: the probe's exit status gates registration (`cout=... || continue`). Tests: the only v1.11.2 client exiting 7 after a valid tag is a refusal; a healthy second v1.11.2 client is used instead. 79/79.

## Diff stat

 scripts/talos-upgrade-node.sh            | 190 ++++++++++++++++++++++++++++
 scripts/tests/test-talos-upgrade-node.sh | 208 +++++++++++++++++++++++++++++++
 2 files changed, 398 insertions(+)
