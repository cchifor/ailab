# Implementation review — playground-leftovers (PR-A) — round 1

<!-- codex-impl-review-status: finalized -->

## Findings

### Unresolved image settings silently disappear from discovery
**Location:** scripts/verify-sandbox-boundary.sh:77
**Severity:** important
**Resolution:** ACCEPTED, fixed in def965db: every declared AF_SANDBOX_IMAGE is marked `declared:` and must be a literal digest; `valueFrom`/empty/tag exit 2 (test `a_declared_image_without_a_literal_digest_is_fatal`).

### Broker recovery can hide an outage during the deny probe
**Location:** scripts/verify-sandbox-boundary.sh:291
**Severity:** important
**Resolution:** ACCEPTED, fixed in def965db: the picked pod is re-read by name; its fingerprint (uid, restart counts, Ready transition time) must be unchanged, else inconclusive (tests for restarted / flapped / gone; mutation-checked).

### The documented mutation never tests a reachable deny target
**Location:** scripts/verify-sandbox-boundary.sh:104
**Severity:** important
**Resolution:** ACCEPTED, fixed in def965db: the deny target excludes the positive pod's IP, so `AF_CANARY_DENY_BROKER=broker-openai-codex` picks codex's other replica and the live mutation run exercises a reachable target (verified live: picks 10.244.4.79 vs positive 10.244.5.132).

### Deny evidence is matched as a substring
**Location:** scripts/verify-sandbox-boundary.sh:312
**Severity:** important
**Resolution:** ACCEPTED, fixed in def965db: `has_line` (`grep -qxF`), with missing/prefixed/suffixed variants tested through the new AF_CANARY_FAKE_RUN seam (mutation-checked).

### Retention succeeds when directory enumeration fails
**Location:** kubernetes/apps/infrastructure/agentforge-sandbox/platform-dev-nfs-provisioner-job.yaml:123
**Severity:** important
**Resolution:** ACCEPTED, fixed in def965db: `find` runs in an assignment under `set -e`; verified under a POSIX sh with a failing `find` (rc=1) and with old-empty / new / non-empty dirs.

### RBAC lookup errors still count as absence of bindings
**Location:** scripts/verify-sandbox-boundary.sh:132
**Severity:** important
**Resolution:** ACCEPTED, fixed in def965db: a failed list is a FAIL; the unused lookup is removed; anchored subject match (tests: error, bound).

### Unreadable process environment still counts as clean
**Location:** scripts/verify-sandbox-boundary.sh:235
**Severity:** important
**Resolution:** ACCEPTED, fixed in def965db: grep rc 1 = CLEAN, rc 0 = BREACH, else INCONCLUSIVE, in the harness and egress-canary.yaml.

### Documentation describes later storage deletion as completed
**Location:** docs/runbooks/sandbox-boundary-canary.md:47
**Severity:** nit
**Resolution:** ACCEPTED, fixed in def965db: the runbook and harness header say retained until PR-B1.

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