# Implementation review — worker-identities (consumers + docs) — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- The reviewer changes are focused: all three writers use the instance helper, canonical markers remain unchanged, and the config template correctly renders all nine merge authors.
- Changes are needed for fail-closed slot parsing, marker idempotence, and the rotation, re-slot and outage procedures.
- The documented token homes, omitted `dsh-acp-plus` ADMIN grant and read-only WSL token match the supplied live facts.
- Phase 3’s SOPS fallback cleanup and stateless-reviewer handoff remain unaccounted for; several planned regression cases are missing.
- Validation passed for the current slot enumeration, six allowlist tests, in-memory config rendering, shell syntax and diff whitespace. Tests requiring temporary files were blocked by the read-only environment; no tracked files were changed.

## Findings

### Re-slotting must revoke issued OpenBao tokens before publishing replacements
**Location:** docs/runbooks/s2s-identity.md:311  
**Severity:** important  
<!-- codex: Applying the publish-first rotation procedure to a replacement VM lets the previous holder read the replacement PATs, and destroying its AppRole SecretIDs does not revoke already-issued renewable OpenBao tokens. Separate re-slotting from routine rotation: revoke the previous holder's SecretIDs and issued tokens, verify that its vault access fails, and only then publish replacement PATs and enable the new machine. -->

### Rotation verification can pass while the old tokens remain active
**Location:** docs/runbooks/s2s-identity.md:286  
**Severity:** important  
<!-- codex: A seed-only merge does not recreate an already-completed provision Job until its daily TTL cleanup or an explicit operator deletion, and the rotation check at lines 308–310 cannot distinguish old and new tokens because both return the same GET /user identity. Make the manual rerun or daily wait explicit, verify that the new token values reached OpenBao and the rendered git credential without exposing them, and only then revoke the old tokens. -->

### Lowering approvals does not recover merges during a reviewer outage
**Location:** docs/runbooks/relay-reviewer-onboarding.md:97  
**Severity:** important  
<!-- codex: Setting required_approvals to zero leaves reviewbot's independent requirement for both personas' clean verdicts intact, while the reviewer-only merge whitelist and blocked admin override still prevent the owner from merging directly. Document and verify a complete temporary owner merge procedure, including the necessary narrowly scoped protection changes, the actual merge and restoration, instead of presenting approval relaxation alone as recovery. -->

### The slot checker can accept a different list from Ansible
**Location:** scripts/check-slot-enumerations.py:89  
**Severity:** important  
<!-- codex: Appending a quoted duplicate key, `"dev_worker_gitea_users": []`, makes the YAML value empty while both this checker and the reviewbot allowlist test still accept the original four entries; the entry regex also accepts dev-worker-04-bot and dev-worker-4-bot#typo as slot 4 even though those are different login strings. Validate duplicate keys regardless of quoting and require canonical login spellings with YAML-correct comment boundaries, adding failure fixtures for these reproduced cases. -->

### Editing text after an existing marker creates a second marker
**Location:** scripts/gitea-api.sh:63  
**Severity:** important  
<!-- codex: Idempotence checks only the final nonblank line, so PATCHing a previously marked comment after appending prose produces two worker markers, potentially naming different runs, contrary to the plan's no-second-marker requirement. Recognize an existing attribution marker throughout the body and preserve the original payload when present, with regression tests for markers followed by additional text and markers written inline. -->

### Phase 3 leaves the shared SOPS credential fallback intact
**Location:** docs/runbooks/owner-credentials.md:17  
**Severity:** important  
<!-- codex: The documentation acknowledges the second shared PAT copy, but this branch leaves dev_worker_gitea_token and its git_forge.yml consumer unchanged despite Phase 3 explicitly requiring removal or conversion to per-slot credentials after verification. Complete that cleanup, including its examples and loading references, or record an explicit plan amendment and prerequisite follow-up; otherwise disabling OpenBao can reinstall the shared identity, and retirement leaves a fallback that renders a revoked credential. -->

### The stateless-reviewer transition has no recorded handoff
**Location:** 8f83223c  
**Severity:** important  
<!-- codex: The consumer update and transition tests cover only the legacy reviewbot; neither the branch nor the supplied completion record identifies the stateless reviewer's deployed coord_repo/coord_ref or accounts for updating its replacing merge_authors_by_repo overrides as Phase 3 requires. Link the companion policy change and transition tests, or explicitly record verified non-deployment and defer this work behind a deployment gate, so the legacy allowlist change is not mistaken for completion of every consumer. -->

### The planned flat host-document credential test is missing
**Location:** ansible/roles/dev_worker/tests/test-cred-helper.sh:135  
**Severity:** nit  
<!-- codex: The existing tests exercise common and a nested personal document, but never the newly documented `cred exec <hostname> gitea_pat` lookup of the flat per-slot document. Add the planned fixture proving that the doubled host-path probe misses, the fallback reads exactly af/dev-workers/<hostname>, and exec passes that slot's value without exposing it. -->

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