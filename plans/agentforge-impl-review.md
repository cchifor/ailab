# Implementation review ? agentforge (ailab companion) ? round 1

<!-- codex-impl-review-status: pending -->

## Summary
- The core infra shape mostly matches the plan: Gitea webhook allowlist syntax is correct for Gitea 1.26, litellm-local is structurally local-only, and the NodePort selects the local deployment.
- The biggest rollout gap is that monitoring/rules are applied while AgentForge is not enabled and the real dev-worker SOPS file is absent.
- Monitoring rules drift from the app's metric contract: two alerts use metrics AgentForge does not export.
- The updater implements most of the atomic contract, but it still trusts the release pin as a filesystem path; the env-file ownership also weakens the planned secrets hygiene.

## Findings

### Monitoring is live while AgentForge is not deployable
**Location:** ansible/group_vars/dev_workers.yml:12; ansible/secrets/dev-worker.sops.yaml; kubernetes/apps/infrastructure/monitoring/agentforge-rules.yaml:18
**Severity:** blocker
<!-- codex: The plan's Deployment section calls for the go-live toggle and real re-encrypted dev-worker SOPS secrets, but this diff only adds a commented toggle and the example file. At the same time Flux will apply the ServiceMonitor and critical down alert, so Prometheus will scrape closed ports and page for all six workers until a separate manual enable/secrets step happens; either make this a real go-live change or keep the scrape/rules disabled or muted until the rollout step that enables the service. -->

### Alert rules reference metrics AgentForge does not export
**Location:** kubernetes/apps/infrastructure/monitoring/agentforge-rules.yaml:34
**Severity:** important
<!-- codex: forge_issue_state_seconds and forge_needs_human_pending are not in the app metric contract, so ForgeIssueStuck and ForgeNeedsHumanPending will never fire. Rework these alerts around exported metrics or add the missing gauges in the app; also consider using forge_worker_up for app-level worker readiness instead of scrape-only up. -->

### Bot PATs are written to a same-UID-readable file
**Location:** ansible/roles/dev_worker/tasks/agentforge.yml:53
**Severity:** important
<!-- codex: /etc/agentforge/agentforge.env is installed as owner/group dev_worker_agent_user with mode 0600, but v1 runs agent subprocesses and repo test_cmd under that same UID. Prompt-injected code can read the bot PATs, webhook secret, and litellm key from disk, contradicting the plan's "bot PATs live only in the orchestrator process" hygiene; make the env file root-owned 0600 so systemd and the root updater can read it while same-UID children cannot. -->

### Release pin is used as a root filesystem path without validation
**Location:** ansible/roles/dev_worker/files/agentforge-update.sh:71
**Severity:** important
<!-- codex: pinned comes from the config repo and is concatenated into $RELEASES_DIR/$pinned before rm -rf, mkdir, tar, and chown -R run as root. Reject pins that are not simple release IDs and verify the resolved destination stays under /opt/agentforge/releases before any filesystem mutation. -->

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