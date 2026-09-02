# c-p0-09-broker-clusterip-pool — codex implementation review

## Round 1 — model gpt-5.6-sol — branch feat/c-p0-09-broker-clusterip-pool — base origin/main — 2026-09-02T14:49Z

<!-- codex-impl-review-status: complete -->
## Verdict
changes_requested

## Findings
**Location** docs/runbooks/agentforge-platform-activation.md:498 — **Severity** medium — **Issue** The binding backfill requirement is not fulfilled when the documented `UPDATE` affects zero rows. The instructions permit proceeding while `claude-max-3` remains absent from `subscription_accounts`, even though `allocate()` consults only `subscription_accounts.cluster_ip`; therefore the allocator cannot account for that hand-added seat’s address as required. — **Fix** Make a populated database row a required completion condition, using the supported create/adoption/upsert procedure, and verify its `cluster_ip` matches the pinned Service before enabling the pool.

**Location** kubernetes/apps/apps/agentforge/deployment.yaml:189 — **Severity** low — **Issue** The binding specification requires the literal operator check `kubectl get svc -A -o wide | grep ' 10\\.96\\.0\\.'`; the implementation substitutes a `/26`-scoped extended regex. Although the replacement avoids expected `.1` and `.10` matches, it changes an explicitly DECIDED check without a judge override. — **Fix** Obtain an explicit spec/judge resolution authorizing the scoped check, or restore the required command and reconcile its expected existing matches in the binding criteria.

## Checked
- **Deployment setting — pass:** `AFP_BROKER_CLUSTERIP_POOL` is placed after `AFP_OPENBAO_CA_PATH` with value `10.96.0.192/26`.
- **Deployment rationale — pass:** The comment identifies the `10.96.0.0/12` Service CIDR, KEP-3070 `/24` static band, three pinned addresses, boot validation, and runbook.
- **Exact operator check — fail:** The comment uses a scoped `grep -E` rather than the binding literal command.
- **Day-2 documentation — fail:** The setting and static-band rationale are documented, but the permitted zero-row backfill does not ensure `subscription_accounts.cluster_ip` protects the legacy seat.
- **Pool invariant tests — pass:** Tests parse the live manifest value and verify IPv4 parsing, private space, prefix length, Service-CIDR containment, and exclusion of all three pinned addresses.
- **Tests-first — pass:** Commit `e77f461` adds deliberately failing behavioral tests before production commit `da98029`.
- **Credential/token containment — pass:** The PR adds no credential-processing, response, logging, or metrics paths and introduces none of the prohibited token forms.
- **Wire compatibility and mock parity — pass:** No API/DTO, data-testid, mock-server, or broker-stub behavior is changed.
- **Repository rules/scope — pass:** No `AGENTS.md` or `TESTING.md` exists in the inspected repository tree; the additional network-plan edit directly reconciles the new allocator exception, and no unrelated refactor appears.
- **Diff/base correctness — pass:** `origin/main...HEAD` is empty because merge commit `687400e` already contains the branch; the historical PR diff was reviewed against that merge’s first parent, comprising four related files.
- **Verification plausibility — pass:** `python3 -m unittest scripts.tests.test_cp_env -v` passes all six tests; full discovery was attempted but unrelated tempfile-based tests could not run in the read-only environment.
## Round 2 — model gpt-5.6-sol — branch feat/c-p0-09-broker-clusterip-pool — base origin/main — 2026-09-02T14:54Z

<!-- codex-impl-review-status: complete -->
## Verdict
changes_requested

## Findings
**Location** docs/runbooks/agentforge-platform-activation.md:498 — **Severity** medium — **Issue** The binding backfill requirement remains unfulfilled when the documented `UPDATE` affects zero rows: the procedure permits continuing without a `subscription_accounts` row and postpones the update indefinitely, so `allocate()` cannot account for the hand-added seat through `subscription_accounts.cluster_ip`. — **Fix** Require creation/adoption of the `claude-max-3` database row, populate its live ClusterIP, and verify the stored value matches the pinned Service before enabling the pool.

**Location** kubernetes/apps/apps/agentforge/deployment.yaml:188 — **Severity** low — **Issue** The binding specification requires the literal operator check `kubectl get svc -A -o wide | grep ' 10\\.96\\.0\\.'`; the implementation substitutes a `/26`-scoped expression. The added explanation is operationally reasonable but is not a judge resolution overriding the DECIDED requirement. — **Fix** Restore the specified command or obtain an explicit spec/judge resolution authorizing the scoped check.

## Checked
- **Deployment setting — pass:** `AFP_BROKER_CLUSTERIP_POOL` follows `AFP_OPENBAO_CA_PATH` and is set to `10.96.0.192/26`.
- **Deployment rationale — pass:** Comments identify the Service CIDR, KEP-3070 static band, three pinned addresses, boot-time validation, and runbook.
- **Exact operator check — fail:** The required literal grep is replaced by a `/26`-specific `grep -E`.
- **Day-2 documentation — fail:** The setting and rationale are documented, but a zero-row backfill is allowed and therefore does not guarantee allocator inventory coverage.
- **Pool invariant tests — pass:** Six focused tests pass; they parse the live manifest value and verify IPv4, private space, prefix length, CIDR containment, pinned-address exclusion, and fail-closed lookup.
- **Tests-first — pass:** `e77f461` adds behavioral manifest tests before production commit `da98029`; the pre-production tree lacks the required env setting.
- **Credential/token containment — pass:** No credential-processing, logging, response, metric, or client path changes were introduced, and no prohibited token forms appear.
- **Wire compatibility and parity — pass:** No API/DTO fields, frozen data-testids, mock server, or broker-stub behavior changed.
- **Repository rules and scope — pass:** No `AGENTS.md` or `TESTING.md` exists in the inspected tree; changes remain limited to this allocator configuration and its documentation/tests.
- **Merge-base diff — pass:** `origin/main...HEAD` contains only the follow-up deployment comment and conventional review artefact because PR #469 already merged the earlier implementation; base-movement omissions were not treated as findings.
- **Verification plausibility — pass:** The focused suite passes. Full discovery reached 157 tests but 73 unrelated fixture tests could not create temporary directories under the read-only review environment; this does not contradict the claimed writable CI run.