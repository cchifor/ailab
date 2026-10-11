# Implementation review — worker-identities (consumers + docs) — round 2

<!-- codex-impl-review-status: pending -->

## Summary

- The original canonical-login cases, whole-body marker idempotence, rotation fingerprint checks, explicit Job rerun, and stateless-reviewer handoff are addressed.
- Dropped the SOPS fallback concern: companion commits `7921e5fd` and `39ad1722` remove the consumer and encrypted key, and these docs record that dependency.
- Dropped the flat host-document test concern: companion commit `bbb1f979` covers the doubled-path miss, exact fallback, and credential delivery without printing it.
- Four important findings remain: duplicate YAML keys, break-glass merge permissions, failed revocation checks, and re-slot ordering.
- The current slot checker, six allowlist tests, original parser regression cases, in-memory marker checks, and shell syntax checks passed. Tests requiring temporary files were blocked by the read-only environment.

## Findings

### Alternate YAML key syntax still bypasses duplicate detection
**Location:** scripts/check-slot-enumerations.py:96; scripts/tests/test_reviewbot.py:2385
**Severity:** important
<!-- codex: Appending the explicit YAML key `? dev_worker_gitea_users\n: []` or the escaped quoted key `"dev_worker_gitea_\u0075sers": []` still makes the decoded YAML list empty while both guards accept the original four users, as reproduced against the current file. Reject duplicate decoded mapping keys, or reject unsupported top-level YAML syntax throughout the file, and add both cases to the checker and reviewer-parser regression tests. -->

### Break-glass still requires explicit owner admission to the merge whitelist
**Location:** docs/runbooks/relay-reviewer-onboarding.md:106
**Severity:** important
<!-- codex: Gitea 1.26.1's `CheckPullMergeable` checks `IsUserAllowedToMerge` before considering `force_merge`, so changing only `block_admin_merge_override` still rejects `chifor`; disabling the approval whitelist cannot fix this, while disabling the merge whitelist unnecessarily admits every repository writer. Temporarily add only `chifor` to `merge_whitelist_usernames` alongside allowing admin override, preserve the approval whitelist, verify the exact-head merge procedure, and require restoration/read-back and token cleanup even when the merge fails. -->

### Failed OpenBao enumeration can be mistaken for completed revocation
**Location:** docs/runbooks/s2s-identity.md:363
**Severity:** important
<!-- codex: Unlike the provision Job's checked `list_or_empty` assignments, these `for a in $(bao list ... | ids)` loops do not propagate enumeration failures to the parent shell, and failed token lookups are treated as nonmatches; repeating the loops during an outage therefore cannot prove that the previous holder's renewable tokens are gone. Check enumeration, parsing, lookup and revocation results explicitly, distinguish documented empty/not-found responses from operational errors, and abort before publishing replacements, with regression coverage for denied and unavailable OpenBao responses. -->

### Re-slotting enables the replacement before retiring the old PATs
**Location:** docs/runbooks/s2s-identity.md:372
**Severity:** important
<!-- codex: Step 3 enables the replacement VM before step 4 deletes the previous holder's PATs, leaving both machines able to write under the same identity and reversing the finalized plan's requirement to prove old credentials fail before enabling the replacement; its reference to steps 4–8 also places the new machine's AppRole verification before minting its SecretID. Bootstrap the replacement's SecretID with workloads held, publish and verify its new credentials, revoke all old PATs and prove retained old values return 401, then enable the replacement. -->

## Diff stat

```text
 CLAUDE.md                                          |   5 +-
 ansible/group_vars/dev_workers.yml                 |  13 +
 ansible/roles/pr_reviewer/defaults/main.yml        |  16 +-
 ansible/roles/pr_reviewer/files/reviewbot.py       |  23 +-
 .../0020-dev-worker-openbao-credentials.md         |   3 +
 .../0021-agent-credential-plane-and-helm-testns.md |   3 +-
 docs/runbooks/openbao-dev-workers.md               |  30 ++-
 docs/runbooks/owner-credentials.md                 |  11 +-
 docs/runbooks/relay-reviewer-onboarding.md         |  75 +++++-
 docs/runbooks/s2s-identity.md                      | 267 ++++++++++++++++++++-
 ...0-11-worker-identities-impl-review-consumers.md |  63 +++++
 scripts/check-slot-enumerations.py                 |  53 +++-
 scripts/gitea-api.sh                               |  60 ++++-
 scripts/tests/test-check-slot-enumerations.sh      |  67 +++++-
 scripts/tests/test_gitea_api_sh.py                 | 141 ++++++++++-
 scripts/tests/test_reviewbot.py                    | 251 ++++++++++++++++++-
 16 files changed, 1052 insertions(+), 29 deletions(-)
```