# Implementation review ? agentforge (ailab companion) ? round 1

<!-- codex-impl-review-status: complete -->

## Findings

### Monitoring is live while AgentForge is not deployable
**Location:** ansible/group_vars/dev_workers.yml:12; ansible/secrets/dev-worker.sops.yaml; kubernetes/apps/infrastructure/monitoring/agentforge-rules.yaml:18
**Severity:** blocker
**Resolution (9c4726c):** ACCEPTED — the agentforge scrape target and rules are now commented out in monitoring/kustomization.yaml with an explicit note; rollout Phase 0 (runbook) enables them together with dev_worker_enable_agentforge and the real SOPS secrets.

### Alert rules reference metrics AgentForge does not export
**Location:** kubernetes/apps/infrastructure/monitoring/agentforge-rules.yaml:34
**Severity:** important
**Resolution (9c4726c + app a22e225):** ACCEPTED — ForgeIssueStuck replaced by ForgeReconcileStalled on forge_last_reconcile_timestamp; the app now exports that gauge plus forge_needs_human_pending (reconciler-stamped via on_reconcile), so both remaining alerts reference real series.

### Bot PATs are written to a same-UID-readable file
**Location:** ansible/roles/dev_worker/tasks/agentforge.yml:53
**Severity:** important
**Resolution (9c4726c):** ACCEPTED — /etc/agentforge/agentforge.env is root:root 0600; systemd reads EnvironmentFile as the manager before dropping to User=, so same-UID agent/test children cannot read the PATs off disk.

### Release pin is used as a root filesystem path without validation
**Location:** ansible/roles/dev_worker/files/agentforge-update.sh:71
**Severity:** important
**Resolution (9c4726c):** ACCEPTED — the updater now rejects pins that are not plain release ids (^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$, no '..') and malformed sha256 values before any filesystem mutation.

## Diff stat
 .sops.yaml                                         |   2 +-
 CLAUDE.md                                          |   2 +-
 ansible/dev-workers.yml                            |  21 ++-
 ansible/group_vars/dev_workers.yml                 |   1 +
 ansible/roles/dev_worker/defaults/main.yml         |  24 ++++
 .../roles/dev_worker/files/agentforge-update.sh    | 145 +++++++++++++++++++
 ansible/roles/dev_worker/handlers/main.yml         |   9 ++
 ansible/roles/dev_worker/tasks/agentforge.yml      | 117 ++++++++++++++++
 ansible/roles/dev_worker/tasks/firewall.yml        |  16 +++
 ansible/roles/dev_worker/tasks/main.yml            |   5 +
 .../templates/agentforge-memory-cap.conf.j2        |  10 ++
 .../templates/agentforge-update.service.j2         |  17 +++
 .../templates/agentforge-update.timer.j2           |  16 +++
 .../roles/dev_worker/templates/agentforge.env.j2   |  16 +++
 .../dev_worker/templates/agentforge.service.j2     |  29 ++++
 ansible/secrets/dev-worker.sops.yaml.example       |  13 ++
 .../0018-agentforge-autonomous-dev-agents.md       | 140 +++++++++++++++++++
 docs/runbooks/agentforge.md                        | 125 +++++++++++++++++
 kubernetes/apps/apps/ai/kustomization.yaml         |   2 +
 .../apps/apps/ai/litellm-local-secret.sops.yaml    |  31 +++++
 kubernetes/apps/apps/ai/litellm-local.yaml         | 155 +++++++++++++++++++++
 kubernetes/apps/apps/gitea/gitea.yaml              |   4 +
 .../monitoring/agentforge-rules.yaml               |  81 +++++++++++
 .../apps/infrastructure/monitoring/agentforge.yaml |  55 ++++++++
 .../monitoring/dev-workers-node.yaml               |   2 +-
 .../infrastructure/monitoring/kustomization.yaml   |   4 +-
 26 files changed, 1033 insertions(+), 9 deletions(-)