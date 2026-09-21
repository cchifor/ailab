# Implementation review — retire-dev-workers-3-6 (PR-C1) — round 1

<!-- codex-impl-review-status: finalized -->

## Findings

### Shell script robustness: grepping JSON for role_name
**Location:** kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml:193
**Severity:** nit

### Variable comment references obsolete vmid range
**Location:** kubernetes/infra/dev-workers/variables.tf:146
**Severity:** nit

### Cloudflare variable description outdated
**Location:** kubernetes/infra/cloudflare/variables.tf:18
**Severity:** nit

### RETIRED_SLOTS revocation logic and idempotency
**Location:** kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml:172-215
**Severity:** none (verified correct)

### Kyverno exclude for namespace cleanup
**Location:** kubernetes/apps/infrastructure/helmtest/kyverno-protect-reserved.yaml:81-87, 121-127
**Severity:** none (verified correct)

### Config-revision bump for cloudflared rollout
**Location:** kubernetes/apps/apps/edge/cloudflared.yaml:171
**Severity:** none (verified correct)

### Plan text vs. implementation order
**Location:** Plan line 109 vs. implementation line 172
**Severity:** none (clarification only)

### Secrets properly removed from SOPS files
**Location:** ansible/secrets/dev-worker.sops.yaml, ansible/secrets/tep-tokens.sops.yaml
**Severity:** none (verified correct)

### Helmtest and tep objects complete removal
**Location:** kubernetes/apps/infrastructure/helmtest/{namespaces,rbac,networkpolicy}.yaml, kubernetes/apps/infrastructure/testpool/tep-access.yaml
**Severity:** none (verified correct)

### k8stoken-sync target list and RBAC updates
**Location:** kubernetes/apps/infrastructure/security/openbao/k8stoken-sync.yaml:89, 183-197
**Severity:** none (verified correct)

### Scripts: fleet-converge and oom-protect
**Location:** scripts/fleet-converge-daily.sh, scripts/oom-protect-guests.sh
**Severity:** none (verified correct)

### All live references to dw6 marked as retired
**Location:** Throughout codebase
**Severity:** none (verified via git grep)

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
