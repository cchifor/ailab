# Complete bounded PR reviews

The ordinary reviewer reads one PR diff and refuses excessive input. The optional
complete-snapshot lane handles one large PR without opening a PR for each file group,
increasing the model input limit or treating incomplete review as approval. It is
**off by default**. This change does not enable it for Forge or deploy either reviewer.

## Evidence and boundaries

The service pins repository identities, base/head refs and full SHAs, a unique merge-base,
the review implementation, coordinated model profiles and a policy epoch. It fetches
exact commits into an owned temporary bare repository, without checkout, hooks,
external diff or textconv. Only trusted HTTPS forge origins receive the service's own
fetch credential. Fetch credentials are transient, absent from persisted git config,
logs, review receipts and model input. Temporary objects are deleted on exit; fetches
have time, output, individual-file and total object limits.

Every unified-diff byte must belong to one complete file section. Documentation and
lockfiles are included. A section larger than 400,000 bytes, malformed/truncated hunk,
unreadable path, binary material or non-UTF8 content fails closed. The initial release
does not split a single file into partial hunks or invent approval for binary changes.
The aggregate snapshot is limited to 32 MiB and 128 parts; each model diff stays within
the existing 400,000-byte ceiling. PR metadata, model results and final integration
input have separate limits. An overflow is a refusal, never prefix truncation.

SQLite stores the complete immutable manifest and ordered per-persona results. One
part is processed per worker claim, retaining the established timeout, rate-limit,
retry and quarantine policies. Completed matching parts survive restarts. Neither
persona can use the other's receipts. A required final integration pass receives every
changed path, ordered part digest and prior part result; it cannot erase a prior
blocking finding. Incomplete, corrupted, missing or conflicting receipts cannot
aggregate. The public review includes the snapshot digest and exact source; all
findings remain in the local receipt even when the public presentation limit is hit.

Before either persona reviews parts, **both services must post an authenticated ready
review for the same implementation, policy and immutable snapshot**. This is the
runtime guard against a half-deployed rollout. Ready reviews are comments, never
approvals. Each persona publishes one final approval only after every part and final
integration completes cleanly. The merge check accepts only the matching current
snapshot pair and repeats the check immediately before its merge request. Existing CI,
author, no-automerge and branch-protection checks remain mandatory. The legacy approval
upgrade path is disabled for opted-in repositories; old clean or skipped markers cannot
satisfy the new gate.

## Reviewed rollout

1. Review and merge this infrastructure change through the normal ailab PR process.
   Changes to the reviewer's own workflow, guarded CI paths or merge policy require
   explicit human examination; the automation must not approve or merge its own rollout.
   Run `python3 -B -m unittest scripts.tests.test_review_chunks
   scripts.tests.test_reviewbot scripts.tests.test_reviewbot_router` as one command.
   These are local stdlib tests with stub APIs/models; they are not a live reviewer
   acceptance claim. The existing broker-inventory workflow also discovers them.
2. Keep `pr_reviewer_chunk_review_repos: []`. Converge both reviewer hosts with the
   normal `ansible-playbook reviewers.yml -t reviewbot` workflow. Verify all three
   deployed Python files match the reviewed source on both hosts, and normal small-PR
   review remains healthy. No credential needs to be printed, copied or borrowed.
3. Prepare the central opt-in as a reviewed configuration change. Set only
   `pr_reviewer_chunk_review_repos: [cchifor/forge]` for the consolidated Forge delivery,
   a nonempty `pr_reviewer_chunk_review_policy_epoch`, and identical
   `pr_reviewer_chunk_review_profiles` on both hosts. The latter contains exactly
   `claude` and `codex` entries, each with that persona's effective public
   `llm_kind`, `llm_model`, `llm_fallback_model`, `llm_effort` and `llm_models` values.
   The local entry must equal the running config; otherwise review refuses. Retain the
   existing two personas and per-call limit. Changing any profile or epoch invalidates
   existing snapshot receipts. Do not put credentials or credential-file contents in
   this mapping.
4. Converge both hosts again, then confirm both ready reviews reference the same full
   base/head, snapshot digest, merge-base and policy. If only one is ready, fix the
   deployment/configuration mismatch; do not invent the missing review. A fresh eligible
   head enters the normal queue. Draft PRs stay unreviewed until made ready through their
   existing authorized workflow.
5. If this exact head previously received a **legacy skipped** review, run on each
   reviewer host using its own normal service config:

   ```text
   reviewbot.py /etc/reviewbot/config.json --restart-skipped cchifor/forge 304 EXPECTED_BASE_SHA EXPECTED_HEAD_SHA
   ```

   Use the full current expected SHAs. This only requeues a terminal skipped local job
   corroborated by that persona's latest authenticated skipped review. It preserves
   attempts, reviews and receipts. It rejects real approvals/findings, moved source,
   active work and every ambiguous publication state. It never impersonates the peer.
6. Observe each part and final integration completing, then both final current-source
   reviews and the protected Forge quality result. Source completion, local tests or
   a ready handshake are not final acceptance. Let the reviewers own the normal merge.

## Failure, recovery and rollback

If base, head, target or policy changes, old parts cannot qualify the new snapshot.
Head changes use ordinary coalescing; completed v2 jobs with base-only changes are
requeued against the new snapshot. Legacy completed jobs are never silently revived.
An interrupted model call resumes through the normal retry budget. A service crash
while posting quarantines the job as an ambiguous POST; inspect the actual remote
review before the existing operator recovery procedure. Never clear that quarantine
with the skipped-review command or a database edit.

The receipt database is service-owned state, not an external approval API. Protect and
back it up with existing reviewer state. Snapshots are intentionally retained for
audit; they are at most 32 MiB each plus bounded results, but repeated revised large PRs
accumulate. Monitor the state filesystem. Do not delete live or ambiguous-job receipts;
any future retention tool must account for their jobs explicitly.

Rollback removes the repository opt-in from both hosts and preserves the receipt
database. V2 reviews have no legacy clean marker, so the old reviewer cannot inherit
their approval verdict. A too-large PR returns to an explicit review-size refusal.
Keep branch protection intact. Changing approval identities, bypassing protected status
or manually stamping a clean marker is not a recovery path.

The first live rollout must also measure immutable object-fetch sizes, actual prompt
and output sizes, part durations, retries and total completion time. The model sees
bounded diffs and a final integration summary; unresolved dependencies across parts
must remain findings until reviewed. A successful planner dry run does not establish
that the full application or the live reviewer deployment is qualified.
