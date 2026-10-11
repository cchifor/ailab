# Implementation review — worker-identities (dev_worker role) — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- The implementation largely respects the declared scope. Gate-off credential templates and Gitea credential instructions render byte-identically to main; commit identity is independent of the gate, and no host enables cut-over here.
- The SOPS credential consumer and encrypted token field are removed. The normal preflight paths keep token values out of output; leaving `/user` verification to the operator is an explicit simplification.
- The main concerns are incomplete failure recovery, interrupted-converge handling, and an audit that does not resolve effective identities in repositories and worktrees.
- Phase 1 item 2 remains detection rather than demonstrated cleanup. Removal of the existing Codex override, the deployed no-reply domain, and real-agent commit attribution still need verified acceptance.
- Read-only checks passed: render comparisons, Ansible argument parsing for 42 shell/command tasks, rendered two-user audit shell syntax, and `git diff --check`. The Linux fixture suite, full Ansible convergence, and live acceptance were not run.

## Findings

### Restart failures bypass the advertised rollback
**Location:** `ansible/roles/dev_worker/tasks/openbao.yml:302`, `ansible/roles/dev_worker/tasks/openbao.yml:351`, `ansible/roles/dev_worker/tasks/openbao.yml:478`
**Severity:** important
<!-- codex: Template replacement, credential deletion, service start, and the flushed restart handler occur outside the verification block, so a failure there can leave the new template installed and credentials missing without invoking rescue; rescue itself restores templates and restarts without removing a newly rendered wrong-login file or verifying recovery. Perform the restart as an explicit guarded transition task, cover preceding mutations with recovery, and remove mismatched credentials and verify the restored login before claiming rollback. -->

### An interrupted switch is not recovered idempotently
**Location:** `ansible/roles/dev_worker/tasks/openbao.yml:273`, `ansible/roles/dev_worker/tasks/openbao.yml:294`, `ansible/roles/dev_worker/tasks/openbao.yml:485`
**Severity:** important
<!-- codex: If a run stops after installing the new template and deleting credentials but before restarting, the next run sees an unchanged template and absent files, so neither task queues a restart; when verification fails, that run also has no template backup to restore, and rescue can start the new renderer while leaving the old username pin. Treat absent destinations as requiring a render/restart independently of the deletion task's changed result, and preserve the last successful transition state across interrupted runs. -->

### The audit misses effective repository and worktree identities
**Location:** `ansible/roles/dev_worker/tasks/git_identity.yml:73`, `ansible/roles/dev_worker/tasks/git_identity.yml:88`
**Severity:** important
<!-- codex: The only git-var checks run outside repositories, while discovery accepts only .git directories and reads config plus worktrees/*/config.worktree: it misses the main checkout's .git/config.worktree, checkouts backed by external Git directories, and the effective values selected by conditional includes. Discover checkouts and their registered worktrees, then run both identity queries as the owning dev_worker_users entry in each checkout, using its applicable global/XDG configuration and reporting inaccessible or excluded repositories explicitly. -->

### The existing Codex override is not located or removed
**Location:** `ansible/roles/dev_worker/tasks/git_identity.yml:27`, `ansible/roles/dev_worker/tests/test-gitea-identity.sh:403`
**Severity:** important
<!-- codex: The task explicitly leaves every higher-priority override untouched, and the source scan only establishes that today's role does not export identity variables; neither establishes the source or removal of the existing Codex <codex@chifor.me> override required by Phase 1 item 2. Record its actual source and verified removal, add narrowly scoped cleanup wherever the role owns it, and retain acceptance through the next real agent-made commit. -->

### Acceptance checks authenticate the vault token instead of the rendered credential
**Location:** `docs/runbooks/openbao-dev-workers.md:629`, `docs/runbooks/openbao-dev-workers.md:662`, `docs/runbooks/openbao-dev-workers.md:665`
**Severity:** important
<!-- codex: Step 4 reuses the GET /user command that fetches dev-worker-N's token directly from OpenBao, so it does not establish the identity of the token actually rendered into ~/.git-credentials; after rollback it still authenticates the slot bot even though the expected result is dev-worker-bot. Add a verification command that reads the rendered credential in memory, authenticates with that exact token without exposing it in arguments or output, and checks the expected account after both transitions. -->

### Tests do not exercise convergence, recovery, or check mode
**Location:** `ansible/roles/dev_worker/tests/test-gitea-identity.sh:189`, `ansible/roles/dev_worker/tests/test-gitea-identity.sh:223`, `ansible/roles/dev_worker/tests/test-gitea-identity.sh:340`
**Severity:** important
<!-- codex: The tests execute useful shell fragments, but rollback is modeled by manually replacing fixture contents, ordering is checked by flattening task names, and identity writes are inspected as YAML rather than executed through Ansible; consequently they cannot catch the recovery failures above or establish module idempotence and --check behavior. Add isolated convergence coverage for false→true→false, restart/render failures and interrupted retries, repeated runs and --check, multiple users with both global-config locations, and the real cred helper's host-name-to-flat-document resolution instead of only the simplified cred stub. -->

### False positives can hide actionable audit results
**Location:** `ansible/roles/dev_worker/tasks/git_identity.yml:53`, `ansible/roles/dev_worker/tasks/git_identity.yml:114`
**Severity:** nit
<!-- codex: Every matching local/XDG setting or include is presented as an override even when it agrees with the desired identity, is overridden itself, or contains no identity setting; the first-60-lines cap can then hide actual mismatches and live-process findings, while --check also reports identities that its skipped writes would correct. Separate configuration inventory from demonstrated effective overrides, distinguish predicted check-mode corrections, and make every actionable finding available rather than replacing the remainder with a count. -->

### New shell and template interpolations need quoting or validation
**Location:** `ansible/roles/dev_worker/tasks/git_identity.yml:50`, `ansible/roles/dev_worker/tasks/openbao.yml:70`, `ansible/roles/dev_worker/templates/git-credentials.ctmpl.j2:9`
**Severity:** nit
<!-- codex: Identity values, hostnames, and paths are inserted directly into shell source, and the credential document name is inserted into a quoted consul-template string without escaping; the current inventory names are safe, but double quotes around Jinja substitutions do not prevent shell expansion of substituted content. Apply shell quoting to complete substituted arguments and validate slot/login identifiers, or encode the complete consul-template string literal, with regression cases for quotes and shell metacharacters. -->

### Remaining runbooks still depend on the deleted SOPS token
**Location:** `docs/runbooks/openbao-dev-workers.md:343`, `docs/runbooks/owner-credentials.md:17`, `docs/runbooks/s2s-identity.md:103`
**Severity:** nit
<!-- codex: The seed/recovery recipe still indexes dev_worker_gitea_token from the now-modified SOPS document, the owner-credentials table still names that file as the source, and the identity ceremony still directs operators to maintain the second copy. Update these operational references alongside removal so recovery does not fail with a missing key or instruct an operator to recreate the retired credential path. -->

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