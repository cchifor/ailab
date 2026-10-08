# Implementation review — talos-upgrade-program — round 2

<!-- codex-impl-review-status: pending -->

## Summary

- Checked all six resolutions against `61490318`; survivor selection, client identity, structural parsing, argument quoting, and test coverage substantially improve the wrapper.
- Server/resource probes and curl now reject failures correctly, but the new client-identity probe still ignores its exit status.
- A regression test for failed client identification is missing.
- Bash syntax and diff checks pass, as do targeted Git Bash/Windows Python parsing checks. The full WSL suite was not rerun in this read-only session.

## Findings

### Failed client-identity probes are still accepted

**Location:** scripts/talos-upgrade-node.sh:119  
**Severity:** important
<!-- codex: The `version --client` assignment ignores its exit status: a probe printing `Tag: v1.11.2` and exiting 7 still registers that binary in `TAG`, making it eligible for the upgrade (reproduced), while the test stub always exits 0 for this branch. Capture the complete output and require successful completion before parsing/registering the client, then add a valid-output/nonzero-exit regression test proving that binary cannot reach `upgrade`. -->

## Diff stat

 scripts/talos-upgrade-node.sh            | 190 ++++++++++++++++++++++++++++
 scripts/tests/test-talos-upgrade-node.sh | 208 +++++++++++++++++++++++++++++++
 2 files changed, 398 insertions(+)
