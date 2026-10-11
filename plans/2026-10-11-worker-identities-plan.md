# One Gitea identity per worker: attributable PRs, comments, merges and commits

## Context

On git.chifor.me it is often impossible to tell **which worker** did something:

- **All four dev workers share `dev-worker-bot`.** `af/dev-workers/common` `gitea_pat` is rendered into
  `~/.git-credentials` on every slot (`ansible/roles/dev_worker/templates/git-credentials.ctmpl.j2:8`,
  `dev_worker_gitea_user: dev-worker-bot` in `roles/dev_worker/defaults/main.yml:30`). Since
  2026-10-07 12:00Z `dev-worker-bot` wrote 136 comments, and none of them says which slot wrote it.
- **Commit authorship is unmanaged.** No task sets `user.name`/`user.email` on the dev workers.
  Commits on recent `dev-worker-bot` PRs are authored as `Constantin Chifor
  <constantin.chifor@strive.us>` (133), `Codex <codex@chifor.me>` (124), `Ubuntu
  <c4@dev-worker-4.lan>` (107) and `Ubuntu <c4@dev-worker-2.lan>` (9). Worker work therefore reads
  as the owner's own work in git history. Only the commits where git fell back to the hostname
  reveal a slot, and only by accident.
- **Two reviewed repositories record no review and no merger at all.** `cchifor/relay` and
  `cchifor/llm-router` have an unprotected `main`. Their last ten merged PRs each (relay #108–#116,
  #96; llm-router #87–#96) have **zero reviews**, and the API reports `merged_by: null`. The bots
  authored and merged them (`dev-worker-bot`, `workstation-bot`). Both repos are in
  `pr_reviewer_repos` ("reviewer-owned merges", operator request 2026-10-10), but the reviewer
  accounts are not collaborators there yet, and the activation in
  `docs/runbooks/relay-reviewer-onboarding.md` has not been done.
- **The owner gate is self-attested.** On platform, `OWNER_ACK_USERS` lists `dev-worker-bot` next to
  `chifor`. Under the owner's instruction, the worker posts `approve-pin <head>` itself
  (`platform/docs/runbooks/owner-ack.md`, "Accepted residual risk"). Which slot did it is invisible.

This was always meant to be done. ADR 0020 (lines 162–164) left per-worker PATs "deliberately out of scope… Splitting the
PAT is a follow-up", and its follow-up list (:230–231) calls this "the change that makes a worker compromise actually
contained". ADR 0021 (:183–185) still lists it. The s2s plan's D1 (`plans/2026-10-06-s2s-identity-openbao-plan.md:173`)
split the owner from the automation, but it created **one** shared bot and did not revisit the per-worker split.

**Goal.** Every write to Gitea is attributable to the machine that holds the credential, and every
commit to the worker that made it. Gitea writes are PR open, push, comment, review, merge and
package publish. Where several agent runs share one machine, the run is attributable through a
marker the run writes.

**Principle: one identity per credential holder.** A Gitea account is the unit of attribution
*and* of revocation. It maps to a machine or slot that holds its own token. It does not map to an
agent session: sessions on one machine share that machine's credential store, so separate accounts
there would be a label without a boundary. Sessions are attributed in-band, by trailers and
markers.

## Target identity map

| Worker | Today | Target |
| --- | --- | --- |
| dev-worker-1..4 (slots, `inventory_hostname`) | `dev-worker-bot`, shared | **`dev-worker-1-bot` … `dev-worker-4-bot`**, one per slot (new) |
| Owner workstation (Windows, many Claude sessions) | `workstation-bot` | Unchanged. Sessions are attributed by the existing `Claude-Session` commit trailer and a session marker in PR/comment bodies |
| Reviewer VMs | `reviewer-claude`, `reviewer-codex`, one per kind | Unchanged. Stateless reviewers (#1127) require one account per kind, because markers, pending reviews and approvals are per account. Each review marker gains `instance=<host>` |
| dsh | `dsh` | Unchanged account. Its commit author becomes consistent (today there are three variants) |
| AgentForge (`conductor-bot`, `planner-bot`, …) | Per role | Unchanged. They already write an `af:run` marker, which is the precedent for the session marker |
| renovate / org-ops / release / actions-retention | Single-purpose | Unchanged |
| homelab Incus claude-workers | `homelab-agent`, wired in uncommitted homelab work. **The account does not exist** | Not created now. When that work lands, each worker gets `claude-worker-<n>-bot` through this plan's enrolment procedure (§ Phase 2), never a shared account |

Out of scope: owner credential cleanup (B6: the `cchifor` org tokens and the 39 non-allowlisted
`gitea_admin` tokens), `agentforge-ci-bot`'s undocumented token home, and the AgentForge bots. They
are already separate identities.

## Approach

### Phase 0 — Close the merge-attribution hole on relay and llm-router (owner activation)

Per-slot identities do not help if a bot can still merge its own unreviewed PR with no merger
recorded. Run the existing `docs/runbooks/relay-reviewer-onboarding.md` activation:

- add `reviewer-claude` and `reviewer-codex` as write collaborators on `relay` and `llm-router`;
- protect `main` with 2 required approvals and approval and merge whitelists naming only the two
  reviewer users, as on `forge` and `dsh-acp-plus`;
- confirm the reviewbot sees both repos, which means the org webhook is present.

After this, every merge on these repos goes through the reviewer API path, so `merged_by` is
recorded. This phase is independent of the rest and can land first. It is listed here because
"who merged" is part of the goal.

### Phase 1 — Commit identity and in-band run markers (no new accounts)

1. **dev_worker role: per-host git identity.** A new task sets, in each agent user's global git
   config:
   - `user.name = {{ inventory_hostname }}-bot`
   - `user.email = {{ inventory_hostname }}-bot@noreply.git.chifor.me`

   That is Gitea's no-reply domain; it is already used for `dev-worker-bot` merge commits. Once
   the account exists (Phase 2) and carries that email, Gitea links each commit to the slot's
   user. Until then the name alone already says which slot made the commit. The task also asserts
   that no `GIT_AUTHOR_*`/`GIT_COMMITTER_*` override is exported in the agents' profiles.
2. **Locate and remove the `Codex <codex@chifor.me>` author override** (124 commits). It is set
   neither by the ailab repo nor by the dev_worker role. Candidates are the codex CLI's own config
   on the workers (`~/.codex/config.toml`) and a wrapper environment. The agent is recorded as a
   trailer instead (item 3).
3. **Trailers and markers.** The managed agent-instruction block in `roles/dev_worker/tasks/openbao.yml:490+`
   already reaches `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`. Extend it to require:
   - commit trailers `Worker: <slot>`, `Agent: claude|codex` and `Run: <session or run id>`;
   - a final line `<!-- worker: <slot> agent: <agent> run: <id> -->` in every PR body, comment and
     review the agent writes. This mirrors AgentForge's `af:run` marker.

   On the workstation, PR bodies already carry the session URL, and commits carry `Claude-Session`.
   `scripts/gitea-api.sh` appends the same `<!-- worker: workstation … run: <id> -->` marker to
   comment and review bodies when a session id is in the environment.
4. **dsh commit identity.** One author, `dsh <dsh@noreply.git.chifor.me>`, set where the dsh image
   or pod configures git. Today it is `DSH Operator`, `DSH` and `dsh agent`.
5. **Reviewbot marker.** `<!-- review-bot:v1 persona=… head=… verdict=… instance=<host> -->`.
   Parsers ignore unknown keys; this needs to be confirmed in `reviewbot.py`'s marker parser and in
   `cchifor/reviewer`.

Phase 1 alone fixes the worst symptom: worker commits no longer carry the owner's name.

### Phase 2 — Create the per-slot Gitea accounts and tokens (operator ceremony)

Follow the procedure that created `dev-worker-bot` on 2026-10-06 (`docs/runbooks/s2s-identity.md:56–113`):
in-pod `gitea admin user create` + `gitea admin user generate-access-token --raw`, with the same
flags as `dev-worker-bot` (non-admin, not restricted, same visibility). This plan writes the
procedure once, as a reusable **enrolment procedure** in `docs/runbooks/s2s-identity.md`
("enrol a worker identity"), so the homelab claude-workers and any future worker follow it:

1. Create the user `dev-worker-N-bot` with the email `dev-worker-N-bot@noreply.git.chifor.me` and a
   random password that is never stored. Log in only by token.
2. **Parity, not new grants.** Add each account to team `automation` (write on all repos, never admin),
   the only grant `dev-worker-bot` holds, plus its explicit collaborator grant on `dsh-acp-plus`.
   Do not add any whitelist entry: every branch-protection whitelist names users, and none names
   `dev-worker-bot`.
3. **Tokens per slot, with the same three scopes as today** (`docs/runbooks/openbao-dev-workers.md:69–118`).
   The token names are `dw<N>-git-<date>`, `dw<N>-repo-<date>` and `dw<N>-pkg-<date>`:
   - `gitea_pat`: `write:repository,write:issue,read:organization,read:user`
   - `gitea_repo_pat`: `write:organization,write:repository,read:user`
   - `gitea_package_pat`: `write:package,read:repository,read:user`

   The package token stops being "shared on purpose" (`openbao-dev-workers.md:116`). The reason
   for sharing it was that the publishing agent is not pinned to a worker, and every slot now has
   its own token. Whether `gitea_repo_pat`'s `write:organization` is still needed is decision D4.
4. **Storage: the slot's own OpenBao subtree, `af/dev-workers/dev-worker-N/gitea`**, with fields
   `gitea_pat`, `gitea_repo_pat` and `gitea_package_pat`.
   - The per-worker AppRole already reads `af/dev-workers/<host>/*`
     (`kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml`).
   - `cred` resolves a name against the host subtree first (`roles/dev_worker/files/cred:11–16`),
     so `cred exec gitea gitea_pat …` works with no policy change.
   - The value goes from the pod's `--raw` output straight into `bao kv put` through a pipe. It is
     never written to a terminal, a file or a transcript.
   - The SOPS seed for reseeding is `devworker-seeds.sops.yaml`, extended per slot the same way it
     seeds `common` today. Delete the duplicate `dev_worker_gitea_token` in
     `ansible/secrets/dev-worker.sops.yaml`; `owner-credentials.md:17` wrongly calls that file the
     source.
5. **Verify each account** from outside the dev worker before any consumer uses it:
   - `GET /repos/cchifor/ailab` with each token answers 200;
   - a token without a scope is refused for that scope, e.g. `gitea_pat` on a package read answers
     403;
   - team membership is listed.

### Phase 3 — Teach every name-keyed consumer the new logins (lands before any slot switches)

- **reviewbot.** Add `dev-worker-1-bot` … `dev-worker-4-bot` to `pr_reviewer_merge_authors`
  (`ansible/roles/pr_reviewer/defaults/main.yml:244`) and **keep `dev-worker-bot`** until its last
  open PR is closed: PR authorship cannot be changed, and its open PRs (#1206, #1207 and platform
  #2219/#2221 at the time of writing) still need merge eligibility. Converge both reviewer hosts and
  check `journalctl -u reviewbot` for the loaded list. Make the same change in `cchifor/reviewer`'s
  `policy.json`, where `merge_authors_by_repo` **replaces** the global list for its repo
  (`src/reviewer/engine.py:847`).
- **platform `OWNER_ACK_USERS`** (repository variable; decision D3). Recommended: list the four slot
  bots in place of `dev-worker-bot`. That is parity with today's instruction, and each
  acknowledgement now names its slot. Removing `dev-worker-bot` waits until Phase 5.
- **`scripts/check-slot-enumerations.py`.** Add the Gitea login list as one more enumeration that
  must agree with the slot list. A future slot then cannot get an AppRole without a Gitea identity,
  or the other way round.
- **Docs.** Update:
  - `docs/runbooks/openbao-dev-workers.md` (token table);
  - `docs/runbooks/owner-credentials.md` (fix line 17);
  - `docs/runbooks/s2s-identity.md` (rotation procedure per slot, plus the enrolment procedure);
  - `docs/runbooks/relay-reviewer-onboarding.md:50`;
  - the ADR 0020 and ADR 0021 follow-up lists (mark this item done);
  - `CLAUDE.md` (identity summary).
- **Tests.** Update `roles/dev_worker/tests/test-cred-helper.sh` (the `gitea` name resolves under the
  host subtree), `roles/dev_worker/tests/test-git-credential-readonly.sh`, and a new test asserting
  that the rendered credential line and the git identity carry `<host>-bot`.

### Phase 4 — Switch slots one at a time (canary first)

1. **dev_worker role changes:**
   - `dev_worker_gitea_user: "{{ inventory_hostname }}-bot"`.
   - `git-credentials.ctmpl.j2` reads `af/data/dev-workers/{{ inventory_hostname }}/gitea`.
   - `pin_gitea_username.yml` is unchanged; it pins whatever the variable says.
   - The agent instruction block names `cred … gitea …` instead of `cred … common …`.

   The `openbao-agent.hcl.j2` guard against `<no value>` (:50) applies unchanged.
2. **Canary:** the slot with no running agent job (check `who`/agent processes first). Converge with
   `--limit dev-worker-N --tags openbao,git`. Then, from the slot:
   - `git ls-remote` succeeds;
   - a commit pushed to a scratch branch shows the `-bot` author;
   - a PR opened against `cchifor/primes-lab` (the spike repo) and a comment on it show the author
     `dev-worker-N-bot`;
   - `cred exec gitea gitea_package_pat …` lists packages;
   - the reviewbot merges a trivial PR from this author (eligibility proven end to end).
3. **Running agents** are not disturbed. `dev-worker-bot`'s tokens stay valid through Phase 4, so a
   session that cached the old token in its environment keeps working until it restarts. git
   re-reads `~/.git-credentials` on every operation.
4. **Roll the remaining three slots**, one converge each, with the same checks after each one.

### Phase 5 — Retire `dev-worker-bot`

Do this once all four slots run on their own identity **and** no open PR is authored by
`dev-worker-bot` (or the owner closes the stragglers):

1. Remove `dev-worker-bot` from `pr_reviewer_merge_authors`, from `OWNER_ACK_USERS`, from team
   `automation` and from the `dsh-acp-plus` grant.
2. Delete its three tokens, using the `gitea_admin` basic-auth API, because the token API refuses
   token auth. Set `prohibit_login`. **Keep the account**: deleting it would orphan the PR, comment
   and commit history.
3. Remove the `gitea_*` fields from `af/dev-workers/common` and from the SOPS seed, and leave
   `proxmox_ssh_key` there.
4. **Drift alarm.** Extend `gitea-credential-inventory.sql` (it currently watches only `chifor`,
   `cchifor`, `gitea_admin` and admins):
   - any token on `dev-worker-bot` alerts;
   - every `dev-worker-N-bot` must hold exactly its three named tokens;
   - a `dev-worker-N-bot` that is not in the slot list alerts.

### Slot lifecycle (re-slot, retire)

The identity belongs to the **slot**, like the AppRole. On a re-slot like 2026-09-23, the token
follows the slot's OpenBao path. A retired slot's account is disabled and its tokens are deleted;
the account is never deleted. `RETIRED_SLOTS` in the provision Job gains this step in the runbook.

## Decisions for the owner

| # | Question | Recommendation |
| --- | --- | --- |
| D1 | Naming | `dev-worker-N-bot`. It matches the slot name used by AppRoles and ansible, and reads plainly in the Gitea UI |
| D2 | Commit author for automation | The worker's own `-bot` identity. The owner's name is used only for commits the owner makes by hand |
| D3 | `OWNER_ACK_USERS` | Parity: the four slot bots replace `dev-worker-bot`. Narrowing it, e.g. back to `chifor` only, is a separate owner decision about the self-attested gate |
| D4 | `gitea_repo_pat` (`write:organization`) | Keep it per slot for parity in this plan. Check its last use in the token audit and drop it in a follow-up if unused |
| D5 | Phase 0 timing | First, independent of the rest. It is the only phase that changes what gets reviewed |

## Risks

- **A PR opened by `dev-worker-bot` loses merge eligibility if the name is dropped too early.**
  Phase 5 waits for its open PRs to close.
- **Missing a consumer that keys on the login.** Found so far:
  - reviewbot, `cchifor/reviewer`, `OWNER_ACK_USERS`;
  - platform `build.yml:28` and `release-build-access.yml:13` (owner and reviewer logins only, unaffected);
  - `scripts/gitea-api.sh` (`workstation-bot`, unaffected).

  Phase 3 lands before Phase 4 so a missed one fails on the canary, not on four slots.
- **Token handling during the ceremony.** `--raw` prints the token. It must be piped and never
  echoed, as in the 2026-10-06 creation.
- **More accounts to audit:** four more non-admin users. The Phase 5 inventory alarm makes drift
  visible.
- **This does not close the self-attested owner gate** (owner-ack). It makes each acknowledgement
  name its slot. Closing the gate is D3's follow-up.

## Critical files

- `ansible/roles/dev_worker/defaults/main.yml`: `dev_worker_gitea_user` becomes per host.
- `ansible/roles/dev_worker/templates/git-credentials.ctmpl.j2`: reads the slot's `gitea` path.
- `ansible/roles/dev_worker/tasks/` (new `git_identity.yml`) and `tasks/openbao.yml`: instruction
  block, trailers and markers.
- `ansible/roles/dev_worker/tests/*`: cred helper and credential-line tests.
- `ansible/roles/pr_reviewer/defaults/main.yml`, `files/reviewbot.py` (marker `instance=`),
  `scripts/tests/test_reviewbot.py`.
- `kubernetes/apps/infrastructure/security/openbao/` seed path for `af/dev-workers/<host>/gitea`.
- `scripts/check-slot-enumerations.py`, `scripts/gitea-api.sh`.
- `docs/runbooks/{s2s-identity,openbao-dev-workers,owner-credentials,relay-reviewer-onboarding}.md`,
  ADR 0020/0021 follow-up lists, `CLAUDE.md`.
- Outside ailab:
  - platform repository variable `OWNER_ACK_USERS`;
  - `cchifor/reviewer` `policy.json`;
  - relay and llm-router branch protection (Phase 0);
  - Gitea itself: the accounts and tokens.

## Verification

1. **Unit and CI:** dev_worker role tests, reviewbot tests (merge-author list, marker with
   `instance=`), `check-slot-enumerations.py` with the new list, `ansible-lint`.
2. **Accounts (Phase 2):** for each token, one allowed call returns 200 and one out-of-scope call
   returns 403. Team membership and the absence of admin are read back with `gitea_admin`.
3. **Canary (Phase 4):** the end-to-end checks in Phase 4 step 2, ending with a reviewbot merge of a
   PR authored by the slot bot.
4. **Fleet:** for each slot, the next real PR, comment and commit show `dev-worker-N-bot` as author,
   carry the `worker:` marker and the trailers, and Gitea links the commit to the user.
5. **Phase 0:** a test PR on relay cannot be merged by `workstation-bot`, the reviewbot merges it,
   and `merged_by` names the reviewer.
6. **Retirement (Phase 5):** `dev-worker-bot` holds no token and cannot log in, and the inventory
   alarm fires on a planted test token on a scratch user, then is cleared.

<!-- codex-review-status: pending -->
