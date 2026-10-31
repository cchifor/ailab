# Implementation review — playground-leftovers (PR-A) — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- Storage paths, pre-binding, reaper mount, and canary identity follow PR-A. The workspace refusal prevents selecting platform-dev; retention uses non-recursive deletion confined to the canary directory.
- Structural comparison confirms all five CNPs preserve their other rules. The new canary rule has the intended five selectors and TCP port 8700.
- PR-B1/B2 are correctly deferred. Findings concern incomplete verification evidence, a mutation that does not exercise the probe, and swallowed retention errors; two inherited verifier defects are identified explicitly.
- Bash syntax, inline hashes, YAML parsing, CNP comparison, and three static tests passed. Windows skipped ten preflight tests; additional in-memory Bash checks reproduced the failure cases below. Kustomize encountered filesystem access denial; live verification was not performed.

## Findings

### Unresolved image settings silently disappear from discovery
**Location:** scripts/verify-sandbox-boundary.sh:77
**Severity:** important
<!-- codex: The JSONPath reads only `.value`, then this filter discards empty results, so an existing AF_SANDBOX_IMAGE supplied through valueFrom—or explicitly empty—is silently ignored; reproduced cases either selected the stale committed fallback or accepted another orchestrator's digest without checking agreement. Preserve whether each orchestrator declares the setting, reject unresolved/empty values with exit 2, and add regression cases distinguishing absent discovery from an unreadable image configuration. -->

### Broker recovery can hide an outage during the deny probe
**Location:** scripts/verify-sandbox-boundary.sh:291
**Severity:** important
<!-- codex: Comparing only the first Ready pod's name/IP before and after cannot establish the claimed stability: the selected broker can restart or become unready during curl, then recover with the same name/IP, allowing downtime to count as ingress isolation. Recheck the specifically selected pod, retain its UID, restart counts and Ready transition timestamp, and fail inconclusive on changes, with a regression test for an outage followed by recovery. -->

### The documented mutation never tests a reachable deny target
**Location:** scripts/verify-sandbox-boundary.sh:104
**Severity:** important
<!-- codex: AF_CANARY_DENY_BROKER=broker-openai-codex resolves the same first pod as the positive control, so this branch increments fail and clears DENY_IP before the network probe runs; the planned mutation therefore fails even if the actual denied-broker assertion is broken. Preserve the same-IP guard, but arrange the mutation to select another Ready replica of the admitted broker and assert that reaching it produces the denied-broker BREACH, using a behavioral test beyond PREFLIGHT_ONLY. -->

### Deny evidence is matched as a substring
**Location:** scripts/verify-sandbox-boundary.sh:312
**Severity:** important
<!-- codex: The plan requires the exact `ok (blocked): denied-broker-ip` line, but has() performs an unanchored regex search; substituting `ok (blocked): denied-broker-ipv6` in otherwise passing logs reproduced GREEN 24/24 without the required evidence. Use a fixed, whole-line comparison for this assertion and test missing, prefixed and suffixed evidence instead of checking only that the assertion text exists in the script. -->

### Retention succeeds when directory enumeration fails
**Location:** kubernetes/apps/infrastructure/agentforge-sandbox/platform-dev-nfs-provisioner-job.yaml:123
**Severity:** important
<!-- codex: With `set -eu` and no pipefail, the pipeline's status comes from the while loop, so a failing find followed by an empty loop still prints “sandbox NFS subdirs ready” and completes successfully; this was reproduced with an injected enumeration failure. Enable pipefail in the pinned BusyBox shell or explicitly check enumeration before processing, and test that find failures fail the Job while non-empty directories remain preserved. -->

### RBAC lookup errors still count as absence of bindings
**Location:** scripts/verify-sandbox-boundary.sh:132
**Severity:** important
<!-- codex: This inherited path remains capable of GREEN without evidence: a failed kubectl lookup feeds empty input to grep, whose count becomes zero while `|| true` suppresses the failure; an injected Forbidden response reproduced all eight static checks passing. Check kubectl success separately before counting subjects, remove the unused rb lookup above it, and add an API-error regression case to the verifier tests. -->

### Unreadable process environment still counts as clean
**Location:** scripts/verify-sandbox-boundary.sh:235
**Severity:** important
<!-- codex: This inherited check treats every nonzero grep status as “no credentials,” including a permission/read error or missing grep; injecting status 2 reproduced PROC1 CLEAN, BOUNDARY OK and exit 0 without inspecting the environment. Distinguish grep's no-match status from execution/read failures, mark the latter inconclusive, and mirror the correction in egress-canary.yaml with a behavioral regression test. -->

### Documentation describes later storage deletion as completed
**Location:** docs/runbooks/sandbox-boundary-canary.md:47
**Severity:** nit
<!-- codex: This new text and the harness header say playground storage is already deleted, although PR-A correctly keeps both PV/PVC pairs for verification and rollback. Describe them as retained pending PR-B1/B2 so the operational documentation matches this PR's actual state. -->

## Diff stat

```text
 docs/decisions/0019-agentforge-v2-control-plane.md |  11 +-
 docs/runbooks/sandbox-boundary-canary.md           |  33 ++--
 .../broker-anthropic-claude-max-3.yaml             |  17 +-
 .../broker-anthropic-claude-max-4.yaml             |  21 +--
 .../agentforge-broker/broker-anthropic-max1.yaml   |  21 +--
 .../agentforge-broker/broker-anthropic-max2.yaml   |  21 +--
 .../agentforge-broker/broker-openai-codex.yaml     |  22 +--
 .../boundary-canary-workspace-pvc.yaml             |  64 +++++++
 .../agentforge-sandbox/egress-canary.yaml          |  23 ++-
 .../agentforge-sandbox/kustomization.yaml          |   8 +-
 .../platform-dev-nfs-provisioner-job.yaml          |  24 ++-
 .../platform-dev-reaper-storage.yaml               |  66 ++++++++
 .../agentforge-sandbox/reaper-deployment.yaml      |  21 ++-
 .../agentforge-sandbox/test-egress-canary.yaml     |   6 +-
 plans/2026-10-31-playground-leftovers-plan.md      |   6 +-
 scripts/tests/test_verify_sandbox_boundary.py      | 185 +++++++++++++++++++++
 scripts/verify-sandbox-boundary.sh                 | 125 ++++++++++----
 17 files changed, 521 insertions(+), 153 deletions(-)
```