# Implementation review — worker-identities (consumers + docs) — round 1

<!-- codex-impl-review-status: pending -->

## Findings

### Re-slotting must revoke issued OpenBao tokens before publishing replacements
**Location:** docs/runbooks/s2s-identity.md:311  
**Severity:** important  

### Rotation verification can pass while the old tokens remain active
**Location:** docs/runbooks/s2s-identity.md:286  
**Severity:** important  

### Lowering approvals does not recover merges during a reviewer outage
**Location:** docs/runbooks/relay-reviewer-onboarding.md:97  
**Severity:** important  

### The slot checker can accept a different list from Ansible
**Location:** scripts/check-slot-enumerations.py:89  
**Severity:** important  

### Editing text after an existing marker creates a second marker
**Location:** scripts/gitea-api.sh:63  
**Severity:** important  

### Phase 3 leaves the shared SOPS credential fallback intact
**Location:** docs/runbooks/owner-credentials.md:17  
**Severity:** important  
<!-- codex: The documentation acknowledges the second shared PAT copy, but this branch leaves dev_worker_gitea_token and its git_forge.yml consumer unchanged despite Phase 3 explicitly requiring removal or conversion to per-slot credentials after verification. Complete that cleanup, including its examples and loading references, or record an explicit plan amendment and prerequisite follow-up; otherwise disabling OpenBao can reinstall the shared identity, and retirement leaves a fallback that renders a revoked credential. -->
<!-- opus-pushback: The SOPS path and key are removed on the companion branch feat/worker-identities-dev-worker (7921e5fd, 39ad1722); this branch's docs now say so. -->

### The stateless-reviewer transition has no recorded handoff
**Location:** 8f83223c  
**Severity:** important  

### The planned flat host-document credential test is missing
**Location:** ansible/roles/dev_worker/tests/test-cred-helper.sh:135  
**Severity:** nit  
<!-- codex: The existing tests exercise common and a nested personal document, but never the newly documented `cred exec <hostname> gitea_pat` lookup of the flat per-slot document. Add the planned fixture proving that the doubled host-path probe misses, the fallback reads exactly af/dev-workers/<hostname>, and exec passes that slot's value without exposing it. -->
<!-- opus-pushback: Belongs to the dev_worker role branch; the flat host-document cred test is added there. -->

## Diff stat

```text
 CLAUDE.md                                          |   5 +-
 ansible/group_vars/dev_workers.yml                 |  13 +
 ansible/roles/pr_reviewer/defaults/main.yml        |  16 +-
 ansible/roles/pr_reviewer/files/reviewbot.py       |  23 +-
 .../0020-dev-worker-openbao-credentials.md         |   3 +
 .../0021-agent-credential-plane-and-helm-testns.md |   3 +-
 docs/runbooks/openbao-dev-workers.md               |  30 +-
 docs/runbooks/owner-credentials.md                 |  11 +-
 docs/runbooks/relay-reviewer-onboarding.md         |  60 ++-
 docs/runbooks/s2s-identity.md                      | 200 ++++++++
 plans/2026-10-11-worker-identities-plan.md         | 558 +++++++++++++++++++++
 scripts/check-slot-enumerations.py                 |  43 +-
 scripts/gitea-api.sh                               |  60 ++-
 scripts/tests/test-check-slot-enumerations.sh      |  36 +-
 scripts/tests/test_gitea_api_sh.py                 | 129 ++++-
 scripts/tests/test_reviewbot.py                    | 243 ++++++++-
 16 files changed, 1407 insertions(+), 26 deletions(-)
```