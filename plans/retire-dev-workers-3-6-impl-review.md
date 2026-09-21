# Implementation review — retire-dev-workers-3-6 (PR-C1) — round 1

<!-- codex-impl-review-status: pending -->

## Summary

The implementation is high-quality and follows the plan closely. The new RETIRED_SLOTS revocation block correctly handles AppRole cleanup with good idempotency properties, and all cluster/infrastructure changes are complete and consistent. All helmet-test-dw6, tep-dw6, and cloudflare objects are properly removed. Documentation is thorough with dated retirement comments. Three minor issues: a descriptive comment in variables.tf should be clearer about the vmid range change, the cloudflare variable description should mention dw6 retirement, and the shell script uses simple grep-based JSON parsing which, while functional, is fragile.

## Findings

### Shell script robustness: grepping JSON for role_name
**Location:** kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml:193
**Severity:** nit
<!-- codex: The pattern `grep -q "\"role_name\": *\"${host}\""` relies on exact JSON formatting from `bao token lookup`. This works but is brittle if bao formatting changes (e.g., whitespace, quote style, or field order). A more robust approach would use a JSON parser (e.g., `jq`) or accept that the pattern is sufficient since it mirrors patterns used elsewhere in the codebase. No action needed unless bao output format has been known to vary. -->

### Variable comment references obsolete vmid range
**Location:** kubernetes/infra/dev-workers/variables.tf:146
**Severity:** nit
<!-- codex: The comment "vmids 42xx band (4201-4206) don't collide" should be updated to "(4201-4205)" since 4206 is now retired. The sentence reads oddly after dev-worker-6 is gone: "4201-4206" is factually stale as of this PR's apply. Recommend: change to "vmids 42xx band (4201-4205; 4206 retired 2026-09-2x) don't collide" for consistency with other retirement notes. -->

### Cloudflare variable description outdated
**Location:** kubernetes/infra/cloudflare/variables.tf:18
**Severity:** nit
<!-- codex: The variable description mentions "NEW ones (status, dw1-dw6, agentforge, openbao, search)" but dw6 is retired as of this PR. The variable itself correctly lists only dw1-dw5, so the description is misleading to future readers. Recommend: update to "…dw1-dw5 (dw6 retired 2026-09-2x)…" to match the code and other documented retirement notices. -->

### RETIRED_SLOTS revocation logic and idempotency
**Location:** kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml:172-215
**Severity:** none (verified correct)
<!-- codex: The revocation block correctly implements the plan: (1) k8stoken-sync policy narrowed first, preventing re-creation of retired KV paths; (2) AppRole SecretID accessors destroyed by accessor ID; (3) issued token accessors revoked by role_name lookup (correctly NOT by path prefix, which would revoke all workers); (4) role and policy deleted; (5) KV metadata and descendants deleted; (6) idempotent handling of already-absent objects. The order (policy narrowing → revocation → seed skipping) matches the plan. No issues found. -->

### Kyverno exclude for namespace cleanup
**Location:** kubernetes/apps/infrastructure/helmtest/kyverno-protect-reserved.yaml:81-87, 121-127
**Severity:** none (verified correct)
<!-- codex: The exclusion of `namespace-controller` and `generic-garbage-collector` ServiceAccounts is correctly implemented twice (one per guard rule). The rationale is sound: these controllers act only inside a namespace already being deleted by cluster-admin (never a normal operation), so the guard never protected against their cleanup actions. Implementation matches plan exactly. -->

### Config-revision bump for cloudflared rollout
**Location:** kubernetes/apps/apps/edge/cloudflared.yaml:171
**Severity:** none (verified correct)
<!-- codex: The `chifor.me/config-revision` annotation was bumped from "2026-09-12-searxng" to "2026-09-21-retire-dw6", ensuring running connectors roll out to pick up the ingress list change (removal of dw6.chifor.me entry). This is required and correctly implemented per the plan. -->

### Plan text vs. implementation order
**Location:** Plan line 109 vs. implementation line 172
**Severity:** none (clarification only)
<!-- codex: The plan text says the retired step runs "before the upsert loop" but the implementation places it after the k8stoken-sync policy narrowing and before the seed loop. However, the plan's own detailed rationale (lines 114-117) explains: "fence first…then the provision run narrows the sync policy *before* it deletes the retired KV (the script order)". The implementation correctly follows this detailed order, not the abbreviated summary. The plan summary on line 109 should say "after policy narrowing, before seeds" for future clarity, but the implementation is correct. -->

### Secrets properly removed from SOPS files
**Location:** ansible/secrets/dev-worker.sops.yaml, ansible/secrets/tep-tokens.sops.yaml
**Severity:** none (verified correct)
<!-- codex: Both SOPS files had dev-worker-6 entries removed and their MACs/lastmodified timestamps updated (Talos 2026-09-21T10:15:04Z). Files decrypt cleanly. No stale encrypted material left behind. -->

### Helmtest and tep objects complete removal
**Location:** kubernetes/apps/infrastructure/helmtest/{namespaces,rbac,networkpolicy}.yaml, kubernetes/apps/infrastructure/testpool/tep-access.yaml
**Severity:** none (verified correct)
<!-- codex: helmtest-dw6 namespace, RBAC objects, and NetworkPolicy removed entirely from YAML (not just excluded from for_each). tep-dw6 ServiceAccount, RoleBinding, and token Secret completely removed from tep-access.yaml. Plan's pruning requirement is met. -->

### k8stoken-sync target list and RBAC updates
**Location:** kubernetes/apps/infrastructure/security/openbao/k8stoken-sync.yaml:89, 183-197
**Severity:** none (verified correct)
<!-- codex: resourceNames list narrowed from range(1,7) to explicit tuple (1,2,3,4,5), and helmtest-dw6 RBAC Role/RoleBinding removed entirely. The Python ConfigMap script updated to match. Sync will report "10 fields written/validated" instead of 12 after this PR's apply (1 tep + 1 helmtest per slot × 5 slots = 10). -->

### Scripts: fleet-converge and oom-protect
**Location:** scripts/fleet-converge-daily.sh, scripts/oom-protect-guests.sh
**Severity:** none (verified correct)
<!-- codex: The special dw6 herdr takeover case (--skip-tags herdr) is removed from fleet-converge-daily.sh with a dated comment explaining why. oom-protect-guests.sh removed vmid 4206 from the OOM victim list and updated the comment. No stale code left. -->

### All live references to dw6 marked as retired
**Location:** Throughout codebase
**Severity:** none (verified via git grep)
<!-- codex: Spot check of `git grep -n -e dw6 -e dev-worker-6 -e '192\.168\.0\.13' -e 4206` shows all remaining references are (1) documentation/comments marked "retired 2026-09-2x", (2) ADR amendments noting the retirement, (3) runbook history sections, (4) plan file references. No "live" configuration left treating dw6 as active. One reference in env-pool/SPIKE-REPORT.md is historical context (testing from dw6 pre-retirement), appropriate to leave. -->

## Diff stat
```
 CLAUDE.md                                          |  6 +--
 README.md                                          |  2 +-
 ansible/host_vars/dev-worker-6.yml                 | 10 ----
 ansible/roles/dev_worker/tasks/tep.yml             |  2 +-
 ansible/secrets/dev-worker.sops.yaml               |  7 +--
 ansible/secrets/tep-tokens.sops.yaml               |  5 +-
 .../0018-agentforge-autonomous-dev-agents.md       |  6 +++
 .../0020-dev-worker-openbao-credentials.md         |  5 ++
 docs/network-plan.md                               |  9 ++--
 docs/runbooks/agentforge.md                        |  8 +--
 docs/runbooks/cloudflare-access-apps.md            |  2 +-
 docs/runbooks/dev-workers.md                       | 41 +++++++++------
 docs/runbooks/openbao-dev-workers.md               |  6 ++-
 docs/runbooks/passkeys.md                          |  2 +-
 inventory/hosts.yml                                |  7 +--
 kubernetes/apps/apps/edge/cloudflared.yaml         |  9 ++--
 kubernetes/apps/apps/homepage/configmap.yaml       |  4 --
 kubernetes/apps/backup/velero/helmrelease.yaml     |  4 +-
 .../helmtest/kyverno-protect-reserved.yaml         | 24 +++++++++
 .../apps/infrastructure/helmtest/namespaces.yaml   | 55 --------------------
 .../infrastructure/helmtest/networkpolicy.yaml     | 45 -----------------
 kubernetes/apps/infrastructure/helmtest/rbac.yaml  | 52 -------------------
 .../apps/infrastructure/monitoring/agentforge.yaml |  1 -
 .../monitoring/dev-workers-node.yaml               |  1 -
 .../security/openbao/devworker-provision-job.yaml  | 59 ++++++++++++++++++++--
 .../security/openbao/k8stoken-sync.yaml            | 27 +++-------
 .../apps/infrastructure/testpool/tep-access.yaml   | 13 -----
 kubernetes/infra/cloudflare/access.tf              |  4 +-
 kubernetes/infra/cloudflare/variables.tf           |  2 +-
 kubernetes/infra/dev-workers/main.tf               |  3 +-
 kubernetes/infra/dev-workers/variables.tf          | 32 ++++++++----
 scripts/fleet-converge-daily.sh                    |  6 +--
 scripts/oom-protect-guests.sh                      |  5 +-
 scripts/tep-render-kubeconfigs.py                  |  5 +-
 34 files changed, 194 insertions(+), 275 deletions(-)
```
