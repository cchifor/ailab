# Relay and llm-router reviewer onboarding

**Status: activated 2026-10-11** (worker-identities plan, Phase 0). What was applied is in
§ "Activation record"; recovery when the protection stops every merge is in § "Break-glass". The
account below is the state before activation.

The operator requested review, feedback repair and reviewer-owned merges for the Relay
control-plane stack and its llm-router token-management dependency. Both repositories
are absent from the central reviewer allowlist at AILab main `903066b`. The allowlist
admits both webhook and reconciliation jobs; adding only these two entries uses the
existing two-persona review and merge policy.

At 2026-10-10 08:26 UTC, Relay PRs #14–59 were open, with no posted reviews: nine current
heads passed CI and 37 remained pending. Router PR #91 also had no reviews. This is
source/API evidence, not proof of the live reviewer configuration. The implementation
worker cannot read live configuration (SSH public-key denial on both reviewer hosts)
or administrator-only collaborator/protection endpoints (HTTP 403). The access and
activation steps below therefore remain operator work after this PR is reviewed.
The readable `/branches/main` API reports `protected: false`, zero required approvals
and no required status checks for both repositories. The reviewer application still
checks its own merge gates, but those are not forge-enforced branch protections.

## Activation

1. Confirm `reviewer-claude` and `reviewer-codex` can read and review both
   `cchifor/relay` and `cchifor/llm-router`, with the write access required for normal
   review and merge APIs. Check repository protection with the operator identity.
   Review the currently absent main protections and establish the operator-approved
   checks, current-head approval requirements, stale-approval dismissal and merge
   restrictions before activation; this PR does not select or apply those settings. Do not grant the implementation worker merge
   overrides. Resolve missing prerequisites through the normal operator procedure.
2. Let the reviewers merge this AILab PR. From `ansible/`, the operator then applies
   `ansible-playbook reviewers.yml -t reviewbot` using the normal inventory and
   encrypted variables. This PR does not deploy or change permissions/protection.
3. Read only the `repos` field of `/etc/reviewbot/config.json` on both reviewer hosts
   and confirm both names are present. Avoid printing the complete config: it
   contains credentials. Check that both services are healthy and that the existing
   organization webhooks deliver the required PR events. Reconciliation also polls
   admitted repositories every 300 seconds and reads all pages.
4. Observe fresh `reviewer-claude` and `reviewer-codex` verdicts on the current heads
   of [Relay PR #14](https://git.chifor.me/cchifor/relay/pulls/14) and
   [llm-router PR #91](https://git.chifor.me/cchifor/llm-router/pulls/91). Monitor the
   entire Relay stack, including later result pages. Repair findings and preserve
   dependency order as predecessors merge; do not merge from the implementation
   worker. Record reviewer-owned merge SHAs and required CI outcomes in the PRs.
5. Investigate `ReviewbotReconcileRepoFailing`, missing verdicts or failed CI before
   declaring onboarding complete. A source merge or a queued CI run alone does not
   establish that the requested review loop is active.

Existing merge gates remain both personas clean at the current head, successful CI,
an allowed author and absence of `no-automerge`. Forge-enforced branch protection is
separate from these application checks and is the operator prerequisite in step 1;
it was absent on the two target main branches until the 2026-10-11 activation below.
`dev-worker-bot` and `workstation-bot` are already allowed authors. This change adds no
persona, author exception, review-policy exception or protection change. Since
2026-10-11 the per-slot `dev-worker-1-bot` … `dev-worker-4-bot` are allowed authors too
(`pr_reviewer_merge_authors`, `plans/2026-10-11-worker-identities-plan.md`).

## Activation record (2026-10-11)

Applied by the operator on 2026-10-11. Both repositories now carry the same `main`
protection as `cchifor/forge`:

| Setting | `cchifor/relay` | `cchifor/llm-router` |
| --- | --- | --- |
| Push and force push | disabled for everyone | disabled for everyone |
| Required approvals | 2, approval whitelist `reviewer-claude`, `reviewer-codex` | same |
| Merge whitelist | `reviewer-claude`, `reviewer-codex` | same |
| Stale approvals | dismissed on push | dismissed on push |
| Outdated branch | merge blocked (`block_on_outdated_branch`) | merge blocked |
| Admin merge override | blocked | blocked |
| Required status context | `Verify / verify (pull_request)` | `CI / verify (pull_request)` |

- `reviewer-claude` and `reviewer-codex` are write collaborators on both repositories.
- `reviewers.yml` was converged at 10:40Z, so the deployed reviewbot admits
  `cchifor/relay`, `cchifor/llm-router` and `cchifor/dsh-acp-plus`.
- **Negative test (2026-10-11):** `workstation-bot`, in team `automation` with the same
  grants as the dev-worker bots, reads `user_can_push=false` and `user_can_merge=false`
  on `main` in both repositories.
- **Webhook delivery and reviews (2026-10-11):** the deployed reviewbot logged webhook
  deliveries for relay (`hook:pull_request` relay#123 at 11:02Z, `hook:pull_request_sync`
  relay#118–#122 at 11:03Z), not just reconciler polls, and posted reviews:
  `reviewer-codex` clean on relay#118–#122, `reviewer-claude` findings on relay#123 at
  11:17Z.
- **Still pending when this was written:** the first reviewer-owned relay merge, the one
  whose `merged_by` names a reviewer. Add it here when it lands.

## Break-glass: when the protection stops every merge

With exactly two whitelisted approvers, two required approvals and one required context,
one reviewer-host outage or a renamed CI context stops every merge on these repositories.
Lowering `required_approvals` alone recovers nothing: the reviewbot still merges only on
both personas' clean verdicts, the merge whitelist names only the two reviewers, and the
admin override is blocked, so nobody else can merge either. Relaxing only
`block_admin_merge_override` is not enough: Gitea 1.26.1's `CheckPullMergeable` asks whether
the user may merge (the merge whitelist) BEFORE it considers `force_merge`, so `chifor`
is still refused; and disabling the merge whitelist would admit every repository writer.

Recovery is therefore an **owner merge of one specific PR at its exact head**, with a
one-shot `chifor` token under the same discipline as `scripts/gitea-owner-merge.sh` (that
script cannot do it: its gate requires both reviewer approvals at the head). The rule is
opened by exactly two fields, `chifor` added to `merge_whitelist_usernames` and
`block_admin_merge_override: false`; the approvals whitelist and everything else stay.

1. Open an issue in the affected repository: the reason, the time (UTC), the PR and its full
   head SHA. Check that head's CI yourself: a forced merge bypasses the required context.
2. Run the script below in WSL bash, as a FILE (`bash break-glass.sh`), not pasted into an
   interactive shell: its `EXIT` trap is what restores the rule, reads it back and deletes the
   token, also when the merge or anything before it fails. `K`, `GP`, `DB` and `sql` are the
   shared setup of `s2s-identity.md` § "Enrol a worker identity".

   ```bash
   set -u
   REPO=relay PR=<n> HEAD=<full head sha>
   API=https://git.chifor.me/api/v1/repos/cchifor/$REPO; W=/dev/shm/break-glass; rm -rf $W; mkdir -m 700 $W
   NAME=owner-op-$REPO-$PR-breakglass-$(date -u +%Y%m%d%H%M%S); T=""
   g() { printf 'Authorization: token %s\n' "$T" | curl -q -sS --proto =https --max-redirs 0 -H @- "$@"; }
   finish() {                                     # ALWAYS: rule back, read back, token gone
     if [ -s $W/restore.json ]; then
       g -X PATCH -H 'Content-Type: application/json' --data-binary @$W/restore.json \
         -o /dev/null -w 'restore: HTTP %{http_code}\n' "$API/branch_protections/main"
       g -o $W/after.json "$API/branch_protections/main"
       python3 -c 'import json,sys; a,b=(json.load(open(f)) for f in sys.argv[1:])
   d=sorted(k for k in set(a)|set(b) if k!="updated_at" and a.get(k)!=b.get(k))
   print("read-back:", "identical to the record" if not d else "DIFFERS in %s - fix it now" % d)' \
         $W/rule.json $W/after.json
     fi
     printf "delete from access_token where uid=(select id from \"user\" where name='chifor') and name='%s';\n" \
       "$NAME" | sql
     g -o /dev/null -w 'token after delete: HTTP %{http_code} (must be 401)\n' https://git.chifor.me/api/v1/user
     T=""
   }
   trap finish EXIT
   T=$($K -n gitea exec "$GP" -c gitea -- gitea admin user generate-access-token --username chifor \
     --token-name "$NAME" --scopes write:repository --raw 2>/dev/null | tr -d '\r\n')
   [[ $T =~ ^[0-9a-f]{40}$ ]] || { echo "mint failed"; exit 1; }
   g -f -o $W/rule.json "$API/branch_protections/main" || exit 1         # the record
   python3 - $W <<'PY' || exit 1
   import json, sys
   w = sys.argv[1]; r = json.load(open(w + "/rule.json"))
   keep = {"merge_whitelist_usernames": r["merge_whitelist_usernames"],
           "block_admin_merge_override": r["block_admin_merge_override"]}
   json.dump(keep, open(w + "/restore.json", "w"))
   json.dump({"merge_whitelist_usernames": keep["merge_whitelist_usernames"] + ["chifor"],
              "block_admin_merge_override": False}, open(w + "/open.json", "w"))
   PY
   g -f -X PATCH -H 'Content-Type: application/json' --data-binary @$W/open.json \
     -o /dev/null -w 'open: HTTP %{http_code}\n' "$API/branch_protections/main" || exit 1
   printf '{"Do":"merge","head_commit_id":"%s","force_merge":true}' "$HEAD" > $W/merge.json
   g -X POST -H 'Content-Type: application/json' --data-binary @$W/merge.json \
     -w 'merge: HTTP %{http_code}\n' "$API/pulls/$PR/merge"
   ```

3. Expect `merge: HTTP 200`, `read-back: identical to the record` and `token after delete:
   HTTP 401`. Anything else is the incident now: fix the rule by hand with a fresh one-shot
   token and read it back against the table above. Record the result (merge SHA or the
   refusal, and the read-back) in the issue, then close it.

For a renamed CI context, the fix is a `PATCH` of `status_check_contexts` under the same
token discipline, recorded and read back the same way.

`gitea_admin` stays the last resort, as everywhere else
([`owner-credentials.md`](owner-credentials.md) § Break-glass).

## Verification and rollback

Run `python3 -m unittest discover -s scripts/tests -p test_reviewbot.py`. The shipped
allowlist contract now includes both repositories; existing admission, pagination
and merge-gate tests continue to apply.

To stop future reviews, remove the two entries through a reviewed PR and have the
operator converge both hosts. Remove any repository access only after the allowlist
change is active, so the reconcilers do not poll inaccessible repositories. Leave
existing reviews and CI evidence intact.
