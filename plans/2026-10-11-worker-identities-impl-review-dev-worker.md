# Implementation review — worker-identities (dev_worker role) — round 1

<!-- codex-impl-review-status: pending -->

## Findings

### Restart failures bypass the advertised rollback
**Location:** `ansible/roles/dev_worker/tasks/openbao.yml:302`, `ansible/roles/dev_worker/tasks/openbao.yml:351`, `ansible/roles/dev_worker/tasks/openbao.yml:478`
**Severity:** important

### An interrupted switch is not recovered idempotently
**Location:** `ansible/roles/dev_worker/tasks/openbao.yml:273`, `ansible/roles/dev_worker/tasks/openbao.yml:294`, `ansible/roles/dev_worker/tasks/openbao.yml:485`
**Severity:** important

### The audit misses effective repository and worktree identities
**Location:** `ansible/roles/dev_worker/tasks/git_identity.yml:73`, `ansible/roles/dev_worker/tasks/git_identity.yml:88`
**Severity:** important

### The existing Codex override is not located or removed
**Location:** `ansible/roles/dev_worker/tasks/git_identity.yml:27`, `ansible/roles/dev_worker/tests/test-gitea-identity.sh:403`
**Severity:** important
**Disposition:** source located by a read-only survey of the live workers (2026-10-11): no global,
profile, environment or codex-config override exists; the overrides are REPOSITORY-level
`.git/config` `user.name`/`user.email`, set ad hoc by agents while git had no global identity —
dev-worker-1 `/workspace/c4/platform` (Constantin Chifor <chifor@gmail.com>); dev-worker-3
`/workspace/c4/{addcol,platform,p0-grid,embedder-fix,recall-gate}` (Constantin <chifor@gmail.com>),
`/workspace/c4/{ailab,forge,muse-stream}` (Codex <codex@chifor.me>), `/workspace/c4/agentforge`
(gitea_admin <gitea@local.domain>); dev-worker-4 `/workspace/c4/{trueswarm-admin,trueswarm,ailab}`
(Codex <codex@chifor.me>), `/home/c4/platform` (Constantin Chifor <chifor@gmail.com>).
`git_identity.yml` now removes them (repository config and `config.worktree`, as the owning user,
except under `dev_worker_git_identity_keep_paths`; `--check` reports "would correct"), and the agent
instructions forbid setting an identity. Verified removal on the hosts and the next real
agent-made commit remain the acceptance.

### Acceptance checks authenticate the vault token instead of the rendered credential
**Location:** `docs/runbooks/openbao-dev-workers.md:629`, `docs/runbooks/openbao-dev-workers.md:662`, `docs/runbooks/openbao-dev-workers.md:665`
**Severity:** important

### Tests do not exercise convergence, recovery, or check mode
**Location:** `ansible/roles/dev_worker/tests/test-gitea-identity.sh:189`, `ansible/roles/dev_worker/tests/test-gitea-identity.sh:223`, `ansible/roles/dev_worker/tests/test-gitea-identity.sh:340`
**Severity:** important

### False positives can hide actionable audit results
**Location:** `ansible/roles/dev_worker/tasks/git_identity.yml:53`, `ansible/roles/dev_worker/tasks/git_identity.yml:114`
**Severity:** nit

### New shell and template interpolations need quoting or validation
**Location:** `ansible/roles/dev_worker/tasks/git_identity.yml:50`, `ansible/roles/dev_worker/tasks/openbao.yml:70`, `ansible/roles/dev_worker/templates/git-credentials.ctmpl.j2:9`
**Severity:** nit

### Remaining runbooks still depend on the deleted SOPS token
**Location:** `docs/runbooks/openbao-dev-workers.md:343`, `docs/runbooks/owner-credentials.md:17`, `docs/runbooks/s2s-identity.md:103`
**Severity:** nit
**Disposition:** `openbao-dev-workers.md` fixed on this branch; `owner-credentials.md` and
`s2s-identity.md` are fixed on the companion consumers branch.

## Diff stat

```text
 .gitea/workflows/dev-worker-scripts.yaml           |  15 +
 ansible/dev-workers.yml                            |  10 +-
 ansible/roles/dev_worker/defaults/main.yml         |  41 +-
 .../dev_worker/files/git-credential-store-readonly |  18 +-
 .../dev_worker/tasks/git_credential_helper.yml     |   6 +-
 ansible/roles/dev_worker/tasks/git_forge.yml       |  56 +--
 ansible/roles/dev_worker/tasks/git_identity.yml    | 118 ++++++
 ansible/roles/dev_worker/tasks/main.yml            |   8 +-
 ansible/roles/dev_worker/tasks/openbao.yml         | 119 +++++-
 .../roles/dev_worker/tasks/pin_gitea_username.yml  |   8 +-
 .../dev_worker/templates/git-credentials.ctmpl.j2  |   9 +-
 .../tests/test-git-credential-readonly.sh          |  10 +-
 .../roles/dev_worker/tests/test-gitea-identity.sh  | 443 +++++++++++++++++++++
 ansible/secrets/dev-worker.sops.yaml               |  18 +-
 ansible/secrets/dev-worker.sops.yaml.example       |   4 +-
 docs/runbooks/dev-workers.md                       |  24 +-
 docs/runbooks/openbao-dev-workers.md               |  69 +++-
 17 files changed, 862 insertions(+), 114 deletions(-)
```