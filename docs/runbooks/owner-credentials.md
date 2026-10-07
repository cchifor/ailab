# Runbook: owner credentials on the Gitea forge (`git.chifor.me`)

Who holds which Gitea credential, how owner-only operations run without a standing owner credential, how to
get in when the normal paths fail, and which alerts watch for credentials piling up again. Written for step B8
of the 2026-10-07 auth-hardening plan (`plans/2026-10-07-auth-hardening-and-ops-plan.md`, Item 2).

Target state: the workstation holds ONE non-admin routine credential (`workstation-bot`); the org account
`cchifor` holds no token; the owner `chifor` holds no standing token and no OAuth grant; owner operations mint a
token for one operation and delete it at once; break-glass is documented and tested; the alerts below fire on
any re-accumulation. Until B6 (below) the old credentials still exist and four alerts are silenced.

## Identities

| Identity | What it is | Credential and where it lives | Can |
| --- | --- | --- | --- |
| `workstation-bot` (uid 90) | The owner workstation's routine identity (B1, D10). Non-admin, team `automation` (id 37, write, never admin). | Token `ws-20261007` (`write:repository,write:issue,read:organization,read:user`) in the workstation's git credential helper. Used through `scripts/gitea-api.sh`, which refuses any identity but a non-admin `workstation-bot`. | Push branches, open and comment on PRs in every org repo. It is a merge author (`pr_reviewer_merge_authors`), so its PRs automerge on both bots' approval. It cannot push `main`, merge owner-protected files (405 "Changed protected files"), or call admin APIs (403). |
| `dev-worker-bot` | The dev workers' identity (2026-10-06, D1). Non-admin, team `automation`. | Per-worker token from `ansible/secrets/dev-worker.sops.yaml` (`dev_worker_gitea_token`), installed by the `dev_worker` role. | As `workstation-bot`. It is also in platform's `OWNER_ACK_USERS` (the owner's 2026-10-05 delegation), so it may post `approve-pin` under the timing and hold rules in platform `docs/runbooks/owner-ack.md` § "When the automation acknowledges, and when it holds". |
| `org-ops-bot` | Org automation. Non-admin member of the org Owners team. | Three tokens: `org-ops-watchdog-20261006` (read), `org-ops-cloud-power-20261006` (`write:organization`), `org-ops-hookcheck-20261006` (`read:organization`). See `s2s-identity.md` § D2. | Org metadata; `write:organization` can also manage teams (accepted residual). |
| `release-bot` | The forge repo's release publisher. Team `release` (`cchifor/forge` only). | Token `forge-release-20261006` as the forge repo secret `FORGE_RELEASE_TOKEN`. | Read code and releases, write packages, in `cchifor/forge`. |
| `renovate-bot` | Renovate. | Gitea PAT (`write:repository,write:issue,read:user`) as `RENOVATE_TOKEN` in Secret `renovate/renovate-credentials` (SOPS). | Open PRs; a merge author, same gate as everyone. |
| `gitea_admin` | **Break-glass** site admin. | Password login. The password is in Secret `gitea/gitea-admin` (`kubernetes/apps/apps/gitea/gitea-admin.sops.yaml`). Two allowlisted tokens held by automation: `af-ci-scaler-2941` (`read:admin`) and `flux-ailab-read` (`read:repository`). 39 other tokens are still there until B6. | Everything. |
| `chifor` | **The owner**, site admin. Web login at git.chifor.me through Authelia. | **No standing token** (target). Owner operations use a token minted for one operation (next section). Until B6 it still has `cc-admin-20260913` and a Git Credential Manager OAuth grant. | Everything; the only merger of owner-protected files. |
| `cchifor` | The organization account (a converted user, uid 2). | Should hold **nothing**. Holds 26 tokens until B6, and stays in `pr_reviewer_merge_authors` until B7. | Acts with the org's identity. |

## Owner operations are automated (ruling R13)

The plan's default for owner actions (D11 (a): the owner acts in the web UI) was superseded on 2026-10-07 by the
owner's instruction: "The overall process must be automated, do not get me involved unless there is a complex
issue." Owner-only operations therefore run from the workstation's session, with a token for `chifor` minted
inside the Gitea pod through cluster-admin `kubectl exec`, used for one operation and deleted from the database
at once. What bounds it: both reviewer bots must approve the exact head, the gate below must pass before anything
is minted, the token lives for seconds, and `GiteaOwnerTokenStanding` fires if a `chifor` token survives 4 hours.

### Which operations

- **Merging a PR that touches owner-protected files.** The bots approve it, but their merge (and
  `workstation-bot`'s) is refused with HTTP 405 "Changed protected files".
- **Posting `approve-pin <head sha>`** on a platform PR whose `CI / owner-ack` check is red, then re-running it.
  `dev-worker-bot` may also acknowledge (see the identities table); the owner automation does it when the
  owner-ack rules in platform `docs/runbooks/owner-ack.md` say the worker must hold.

### The procedure: `scripts/gitea-owner-merge.sh`

```bash
# from an ailab checkout on the workstation (kubectl context admin@ai, workstation-bot in the credential helper)
scripts/gitea-owner-merge.sh platform 2138 8f9c07439418a66197025f5685a51b948ef191af             # dry run
scripts/gitea-owner-merge.sh platform 2138 8f9c07439418a66197025f5685a51b948ef191af --execute
scripts/gitea-owner-merge.sh platform 2149 8a8daed88aa05197de65bfd2d97af00aa740f250 --approve-pin --execute
```

Give the PR's full 40-hex head sha, the one the bots approved: the script never merges anything else.

**It is a dry run unless `--execute` is given** (`--dry-run` says so explicitly). A dry run makes one pass of the
gate below, prints what it would do (mint, `approve-pin`, which run it would re-run, merge), and exits: no
kubectl call, no token, no comment, no re-run, no merge, no waiting. Always run the dry run first, and add
`--execute` only when the operation is due: inside its rollout window, with the `rollout-freeze` issue open
when platform `owner-ack.md` requires one.

1. **Gate, with no owner credential** (routine reads as `workstation-bot` through `scripts/gitea-api.sh`):
   - the PR is open, not merged, not a draft or `WIP`, targets `main`, and its head is still the given sha;
   - `reviewer-claude`'s and `reviewer-codex`'s latest non-dismissed review at that head is `APPROVED`;
   - every pattern in `scripts/owner-ops/required-contexts-<repo>.txt` matches a context that passed (success or
     skipped) in the **combined** status (`/commits/<sha>/status`, read page by page; the raw `/statuses` list
     pages unreliably past 250 entries), and no context at the head failed or is pending. `force_merge`
     bypasses every branch-protection check, so this gate is the only check left;
   - the live protection of `main` (`GET /repos/cchifor/<repo>/branches/main`, readable without admin)
     requires no context the file lacks. A repo without a contexts file is refused.

   While contexts are pending or missing, it waits (default 120 polls of 30 s) and re-runs the whole gate on
   every poll, still with no credential.
2. **Owner operation**, with `--execute` only. Only then is a token minted (`owner-op-<repo>-<pr>-<purpose>-<utc>`, scopes
   `write:issue,write:repository`). It lives in a shell variable and reaches curl only on stdin
   (`curl -q --proto =https --max-redirs 0 -H @-`), so it is never in a process argument or the output.
   - Merge: a plain merge first; `force_merge` only after Gitea's "Changed protected files" refusal. Any other
     refusal stops (exit 4).
   - `--approve-pin`: if `CI / owner-ack` failed and nothing else did (`CI / ci-gate` aggregates it), token 1
     posts `approve-pin <head>`, re-runs the failed jobs of the owner-ack run, and is deleted. The script then
     waits with no token until every context passes; token 2 does the merge. If owner-ack fails again after
     the re-run, it stops. If owner-ack is already green, no pin is posted.
3. **Revocation.** A trap on EXIT, INT, TERM and HUP deletes the token by name on the infra-pg primary
   (found by label, not by pod name). The script then proves the token is dead: it is gone from the database,
   and its next use answers HTTP 401. It also prints the names of the tokens `chifor` still holds.

What an `--approve-pin` run prints. This is platform #2149 on 2026-10-07; that run was unintended (see the
incident below), but the output shows the real flow:

```
gate passed except owner-ack (run 71276): reviewer-claude=APPROVED reviewer-codex=APPROVED; required: CI / ci-gate*=failed, ... owner-ack=failed; 75 contexts
owner token owner-op-platform-2149-pin-20261007172108 minted
approve-pin 8a8daed88aa05197de65bfd2d97af00aa740f250 posted
re-ran the failed jobs of run 71276 -> HTTP 201
owner token owner-op-platform-2149-pin-20261007172108 deleted: next use -> HTTP 401; chifor tokens now: cc-admin-20260913
gate passed: ... CI / ci-gate*=ok, ...; owner-ack=ok; 75 contexts
owner token owner-op-platform-2149-merge-20261007172148 minted
plain merge -> HTTP 200
owner token owner-op-platform-2149-merge-20261007172148 deleted: next use -> HTTP 401; chifor tokens now: cc-admin-20260913
MERGED cchifor/platform#2149 as f6b487ccc9b7dc7edf35133f8a59443df2c0ddc0
```

| Exit | Meaning | Do |
| --- | --- | --- |
| 0 | Merged; the merge commit is printed. In a dry run: the gate passes now. | Verify the rollout the change drives. |
| 2 | Bad input, or no contexts file for the repo. | Fix the call, or add the file (below). |
| 3 | Refused by the gate. Nothing (more) was minted. | Read the reason: head moved, a bot did not approve, a check failed or never went green, or the contexts file is stale. Fix that, then run again. |
| 4 | The owner operation failed (mint, pin, re-run or merge refused). The token was deleted. | Read the HTTP status and body it printed. |
| 5 | **The deletion was not confirmed.** | Delete the token now: on the infra-pg primary, `delete from access_token where name='<name>';` in database `gitea`. Then list the `chifor` tokens again. `GiteaOwnerTokenStanding` is silenced until B6 (see Alerts), so nothing else will flag it. |
| 6 | Dry run only: checks are still pending or missing. | Wait, or let an `--execute` run do the waiting. |

Rules:

- **Never run the script against the live forge to test it.** A "read-only" check is the dry run and nothing
  else. Its no-mint guarantee is covered by `scripts/tests/test_gitea_owner_merge.py`. Those tests run against
  fakes, and they abort before the script starts unless `kubectl`, `curl` and `git` resolve to the fakes.

- Never hold an owner token across a wait. Never mint one by hand for an operation this script covers.
- Run one invocation per PR at a time. Stopping a task does not always kill its child processes: after you stop
  one, check for leftover `gitea-owner-merge.sh` processes and kill them. On 2026-10-07 a leftover loop minted
  a second token for an already merged PR. The gate now refuses merged PRs.
- Around rollout windows, follow platform `owner-ack.md` (a `rollout-freeze` issue before a window's first
  merge). One gatekeeper-rolling change per day.
- A contexts file lists the required status contexts of `main`, one glob per line (`#` comments). Update it in
  an ailab PR when the protection changes; the drift check refuses until you do. `ailab` requires none today
  (`enable_status_check=false`), so its file holds only comments.

### Incident 2026-10-07: platform #2149 merged by a "read-only" check

While writing this script, the agent ran an early version against live platform PRs. It intended a read-only
check, with a fake `kubectl` prepended to `PATH` to stop any mint. The fake's directory was written as
`C:/Users/...`, and bash split that `PATH` entry at the drive colon. The real `kubectl` ran, and that version had
no dry-run mode. Against #2149 (the AG1a gatekeeper pin, approved by both bots, planned for the 03:07Z window on
2026-10-08) with `--approve-pin`, the script did exactly what it is built for:
- 17:21:08Z: minted a token, posted `approve-pin`, re-ran run 71276, deleted the token (401);
- 17:21:52Z: minted a second token and merged #2149 as `f6b487cc`, then deleted it (401).

No token was left behind. The controller accepted the merge (ruling R15: it rolls at once, outside its window,
and the controller verifies AG1a itself). Changes made because of it:
- the dry run is the default, and the owner operation needs `--execute`;
- the tests prove that `kubectl`, `curl` and `git` resolve to fakes before every run, and abort otherwise;
- the rule above: the script is never "tested" against the live forge.

**Other owner operations** (branch-protection edits, the re-apply in `s2s-identity.md` § "Re-apply
procedure") follow the same pattern: gate first, then mint, use once, delete, and prove the 401. That block
still reads `$OWNER_TOKEN` from the environment; rewriting it onto this pattern is open B8 work.

## Break-glass (B0)

Use break-glass when the normal paths are broken: Authelia or the owner's login is down, or the owner
automation cannot mint. Tested 2026-10-07: basic-auth `GET /api/v1/user` as `gitea_admin` answered 200 with
`is_admin=true`.

1. **`gitea_admin` password.** Read it from the Secret into a variable, and send it only on curl's stdin. Print
   only the status.

   ```bash
   P=$(kubectl --context admin@ai -n gitea get secret gitea-admin -o jsonpath='{.data.password}' | base64 -d)
   printf 'Authorization: Basic %s\n' "$(printf 'gitea_admin:%s' "$P" | base64 -w0)" \
     | curl -q -s -o /dev/null -w '%{http_code}\n' -H @- https://git.chifor.me/api/v1/user   # expect 200
   unset P
   ```

   The token API (`DELETE /api/v1/users/{username}/tokens/{id}`) needs basic auth like this; a token cannot
   delete tokens.
2. **Cluster-admin `kubectl exec` into the Gitea pod.** Mint a token there with
   `gitea admin user generate-access-token --username <user> --token-name <name> --scopes <smallest> --raw`, as
   `gitea-owner-merge.sh` does. Delete it after use (`delete from access_token where name='<name>'` on the
   infra-pg primary, database `gitea`), and check that its next use answers 401. A token minted for
   `gitea_admin` fires `GiteaAdminTokenNotAllowlisted`, which is intended.

If Gitea itself is down, Flux still bootstraps from the GitHub mirror (see the forge paragraph in `CLAUDE.md`).

## Alerts (B5)

Rules: `kubernetes/apps/infrastructure/monitoring/gitea-credential-rules.yaml`, with fixtures in
`gitea-credential-rules.test.yaml` (`scripts/rules-lint.sh`). They read counts and ages only, never a token
value, hash or name. The source is the custom CNPG exporter queries
(`kubernetes/apps/databases/infra-pg-gitea-credential-queries.yaml`) over the aggregate views in
`kubernetes/apps/databases/gitea-credential-inventory.sql`. Those views are applied by hand on the primary,
because the exporter runs as `pg_monitor`, which cannot read Gitea's tables.

| Alert | Fires when | Do |
| --- | --- | --- |
| `GiteaOrgAccountHasTokens` | `cchifor` holds any token. | Move the consumer to a bot identity, then delete the token. |
| `GiteaOwnerTokenStanding` | A `chifor` token is older than 4 h. | Find it by id and name (never `token_hash`) and delete it. An owner-operation token outlives its run only if the deletion failed (exit 5). |
| `GiteaAdminTokenNotAllowlisted` | `gitea_admin` holds a token outside its two documented consumers (critical). | Delete it. To rotate a documented token, update its id in `gitea-credential-inventory.sql` and re-run that file in the same PR. |
| `GiteaOwnerOAuthGrant` | `chifor` has an OAuth2 grant. | Revoke it (Settings, Applications). |
| `GiteaAdminUserCountChanged` | The number of site admins is not 2 (critical). | Find out who gained or lost the flag. |
| `GiteaCredentialInventoryMissing` | An inventory series is absent for 30 min, per family and per owner. | Check the views, `cnpg_collector_last_collection_error` and the exporter target. |

**Silenced until 2026-10-14T12:00Z**, one silence per (alertname, owner): `GiteaOrgAccountHasTokens`/`cchifor`,
`GiteaOwnerTokenStanding`/`chifor`, `GiteaAdminTokenNotAllowlisted`/`gitea_admin`, `GiteaOwnerOAuthGrant`/`chifor`.
These cover the credentials B6 removes. The `chifor` silence also hides any other standing `chifor` token,
because the alert has no per-token label. Until B6, the token list that `gitea-owner-merge.sh` prints after
each run is the only check for a token left behind (ruling R7).

## Still to do: B6 and B7 (after the 7-day observation that ends 2026-10-14)

- **B6, observe then revoke.** Every token slated for deletion is checked daily for an `updated_unix` change:
  `cchifor`'s 26, `chifor`'s `cc-admin-20260913` and `gitea_admin`'s 39. First confirm that a use moves
  `updated_unix`. A token that moved needs its consumer found before revocation. Then:
  - take a fresh (id, name, scopes) snapshot to `_out/`;
  - delete `gitea_admin`'s 39 and `cchifor`'s 26 through the API with `gitea_admin`'s basic auth (break-glass
    path 1);
  - delete `cc-admin-20260913` and revoke the Git Credential Manager OAuth grant in the UI;
  - check that the local copies answer 401 (`~/.gitea_tok`, `~/.gitea_cred_tmp`), then delete them;
  - remove the four silences. The alerts must resolve.
- **B7.** Remove `cchifor` from `pr_reviewer_merge_authors` (keep `chifor`) and converge the reviewers.
- **Rest of B8.** Update `s2s-identity.md` (its token count, 26, and the closed items) and rewrite its
  branch-protection re-apply block onto the owner-operation pattern. Update the spec's F-30 and the stale
  auto-memory note about the trueswarm merge identity.
