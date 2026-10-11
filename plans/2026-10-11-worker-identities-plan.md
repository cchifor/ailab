# One Gitea identity per worker: attributable PRs, comments, merges and commits

## Context

On git.chifor.me it is often impossible to tell **which worker** did something:

- **All four dev workers share `dev-worker-bot`.** `af/dev-workers/common` `gitea_pat` is rendered into
  `~/.git-credentials` on every slot (`ansible/roles/dev_worker/templates/git-credentials.ctmpl.j2:8`,
  `dev_worker_gitea_user: dev-worker-bot` in `roles/dev_worker/defaults/main.yml:30`). Since
  2026-10-07 12:00Z `dev-worker-bot` wrote 136 comments, and none of them says which slot wrote it.
- **Commit authorship is unmanaged.** No task sets `user.name`/`user.email` on the dev workers.
  Commits on recent `dev-worker-bot` PRs are authored as:
  - `Constantin Chifor <constantin.chifor@strive.us>` (133)
  - `Codex <codex@chifor.me>` (124)
  - `Ubuntu <c4@dev-worker-4.lan>` (107)
  - `Ubuntu <c4@dev-worker-2.lan>` (9)

  Worker work therefore reads as the owner's own work in git history. A slot shows only when git
  fell back to the hostname, and only by accident.
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

This was always meant to be done:
- ADR 0020 (lines 162–164) left per-worker PATs "deliberately out of scope… Splitting the PAT is a
  follow-up". Its follow-up list (:230–231) calls this "the change that makes a worker compromise
  actually contained".
- ADR 0021 (:183–185) still lists it.
- The s2s plan's D1 (`plans/2026-10-06-s2s-identity-openbao-plan.md:173`) split the owner from the
  automation, but it created **one** shared bot and did not revisit the per-worker split.

**Goal.** Every write to Gitea is attributable to the credential that made it, and every commit to
the worker that made it. Gitea writes are PR open, push, comment, review, merge and package publish.
Where several agent runs share one machine, the run is attributable through a marker the run
writes.

**Principle: one identity per credential holder.** A Gitea account is the unit of attribution
*and* of revocation. It maps to a machine or slot that holds its own token. It does not map to an
agent session: sessions on one machine share that machine's credential store, so separate accounts
there would be a label without a boundary. Sessions are attributed in-band, by trailers and
markers.

**What this does not give.**
- **A token is a bearer credential.** Gitea attributes a write to the token that made it, not to
  the machine where it ran. A copied token impersonates its slot until it is revoked.
- **Commit identities and run markers are self-asserted.** They are attribution for a cooperative
  worker, not proof.
- **Per-slot revocation shrinks incident response**: a revocation removes one slot, not four.
  It does **not** narrow what each slot may do. Every slot keeps team `automation`'s write on all
  repositories, and the shared hypervisor-root credential in `common` (`proxmox_ssh_key`,
  `docs/runbooks/openbao-dev-workers.md`) stays shared.
- Narrowing the grants is a separate follow-up.

## Target identity map

| Worker | Today | Target |
| --- | --- | --- |
| dev-worker-1..4 (slots, `inventory_hostname`) | `dev-worker-bot`, shared | **`dev-worker-1-bot` … `dev-worker-4-bot`**, one per slot (new) |
| Owner workstation (Windows, many Claude sessions) | `workstation-bot` | Unchanged. Sessions are attributed by the existing `Claude-Session` commit trailer and a run marker in comment and review bodies |
| Reviewer VMs | `reviewer-claude`, `reviewer-codex`, one per kind | Unchanged. Stateless reviewers (#1127) require one account per kind, because markers, pending reviews and approvals are per account. The instance is named in an informational line next to the unchanged canonical marker |
| dsh | `dsh` | Unchanged account. Its commit author becomes consistent (today there are three variants) |
| AgentForge (`conductor-bot`, `planner-bot`, …) | Per role | Unchanged. They already write an `af:run` marker, which is the precedent for the run marker |
| renovate / org-ops / release / actions-retention | Single-purpose | Unchanged |
| homelab Incus claude-workers | `homelab-agent`, wired in uncommitted homelab work. **The account does not exist** | Not created now. When that work lands, each worker gets `claude-worker-<n>-bot` through this plan's enrolment procedure (Phase 2), never a shared account |

Out of scope:
- owner credential cleanup (B6: the `cchifor` org tokens and the 39 non-allowlisted `gitea_admin`
  tokens);
- `agentforge-ci-bot`'s undocumented token home;
- the AgentForge bots, which are already separate identities;
- narrowing the `automation` team grant.

## Approach

### Phase 0 — Close the merge-attribution hole on relay and llm-router (owner activation)

Per-slot identities do not help if a bot can still merge its own unreviewed PR with no merger
recorded. Run the existing `docs/runbooks/relay-reviewer-onboarding.md` activation and **read back
the complete rule set** on both repositories, matching `forge` and `dsh-acp-plus`:

- **Collaborators:** `reviewer-claude` and `reviewer-codex` as write collaborators.
- **Protection on `main`:**
  - direct push and force push disabled for everyone;
  - 2 required approvals, with the approval whitelist naming only the two reviewer users;
  - merge whitelist naming only the two reviewer users;
  - stale approvals dismissed on push;
  - the repository's own CI as required status checks;
  - admin merge override blocked.
- **Delivery, verified separately:**
  - the deployed `pr_reviewer_repos` list on both reviewer hosts contains both repos (read
    `/etc/reviewbot/config.json`);
  - both reviewer tokens can read each repo;
  - the org webhook shows **successful deliveries** for a test PR event.

  Reviews appearing is not proof: the reconciler also polls admitted repos every 300 s.
- **Negative test:**
  - `workstation-bot` and a dev-worker token cannot push to `main` or merge a test PR;
  - the reviewbot then merges that PR, and `merged_by` names the reviewer.

This phase is independent of the rest and can land first. It is listed here because "who merged"
is part of the goal.

### Phase 1 — Commit identity and in-band run markers (no new accounts)

1. **dev_worker role: per-host git identity.** A new `tasks/git_identity.yml` sets, in each agent
   user's global git config:
   - `user.name = {{ inventory_hostname }}-bot`
   - `user.email = {{ inventory_hostname }}-bot@<NO_REPLY_ADDRESS>`

   Global config is only the bottom layer, so the task also **audits the effective identity**:
   - On each agent user's existing checkouts and worktrees, run `git var GIT_AUTHOR_IDENT` and
     `git var GIT_COMMITTER_IDENT`, which cover repo and worktree config and `includeIf`.
   - In the agents' profiles and launch units, check for any `GIT_AUTHOR_*` /
     `GIT_COMMITTER_*` / `EMAIL` export.

   It reports every override and removes the ones the role owns. Running jobs keep their inherited
   environment until they restart, so acceptance is a **real agent-made commit** after the next job
   start. A scratch repository is not enough.

   **Gitea linking is verified, not inferred.** Read the deployed `[service] NO_REPLY_ADDRESS` from
   the Gitea config. 1.26.1 resolves both `<login>@<NO_REPLY_ADDRESS>` and
   `<id>+<login>@<NO_REPLY_ADDRESS>` to the user even without a matching stored email. Acceptance:
   after Phase 2, the commit API's `author.id` for a slot commit equals that slot user's ID.
2. **Locate and remove the `Codex <codex@chifor.me>` author override** (124 commits). Neither the
   ailab repo nor the dev_worker role sets it. Candidates are the codex CLI's own config on the
   workers (`~/.codex/config.toml`) and a wrapper environment. The audit in item 1 finds it. The
   agent is recorded as a trailer instead (item 3).
3. **Trailers and markers for agents.** The managed agent-instruction block in
   `roles/dev_worker/tasks/openbao.yml:490+` already reaches `~/.claude/CLAUDE.md` and
   `~/.codex/AGENTS.md`. Extend it to require:
   - commit trailers `Worker: <slot>`, `Agent: claude|codex` and `Run: <id>`;
   - a final line `<!-- worker: <slot> agent: <agent> run: <id> -->` in every PR body, comment and
     review the agent writes. This mirrors AgentForge's `af:run` marker.

   The run ID comes from the launcher:
   - Claude Code's session ID, or Codex's thread ID;
   - when no ID is available, the marker carries `run: unknown`. An ID is never invented.
4. **Workstation marker in `scripts/gitea-api.sh`.** Today it forwards the body file verbatim.
   Change it as follows:
   - Insert the marker **only** for comment and review POST/PATCH endpoints (`issues/*/comments`,
     `pulls/*/reviews`).
   - Parse the JSON payload and append `\n<!-- worker: workstation run: <id> -->` to its `body`
     field.
   - Make the insertion idempotent: no second marker if one is already present.
   - Leave every other endpoint's payload byte-identical.
   - The run ID comes from `AILAB_RUN_ID`, which a Claude Code `SessionStart` hook exports. If the
     harness cannot export it, the marker is omitted, never guessed.
   - The existing `EXPECTED_USER=workstation-bot` credential guard is untouched.
   - Tests go in `scripts/tests/test_gitea_api.py`.
5. **dsh commit identity.** One author, `dsh <dsh@<NO_REPLY_ADDRESS>>`, set where the dsh image or
   pod configures git. Today it is `DSH Operator`, `DSH` and `dsh agent`.
6. **Reviewer instance.** Both readers parse the canonical marker with a strict regex
   (`ansible/roles/pr_reviewer/files/reviewbot.py:43`, `cchifor/reviewer` `src/reviewer/review.py:25`),
   so the canonical `<!-- review-bot:v1 … -->` marker stays **byte-for-byte unchanged**. Instead, a
   separate informational line `<!-- review-bot-instance: <host> -->` is written **before** it, so
   the canonical marker remains the review's last marker. Tests:
   - a review with and without the new line is recognised identically;
   - the existing author check and last-marker check still pass;
   - mixed old and new reviews deduplicate correctly.

Phase 1 alone fixes the worst symptom: worker commits no longer carry the owner's name.

### Phase 2 — Create the per-slot Gitea accounts and tokens (operator ceremony)

The ceremony follows the procedure that created `dev-worker-bot` on 2026-10-06
(`docs/runbooks/s2s-identity.md:56–113`). This plan writes it once, as a reusable **enrolment
procedure** in that runbook ("enrol a worker identity"). The homelab claude-workers and any future
worker then follow the same steps.

**Who writes what:**
- The **operator** runs the ceremony from the workstation, with cluster-admin `kubectl exec` into
  the Gitea pod and the SOPS age key.
- The **`openbao-devworker-provision` Job** writes OpenBao, from the SOPS seed.
- Neither the workers' read-only AppRoles nor `k8stoken-sync` gains any write. No policy changes.

1. **Create the user**: `dev-worker-N-bot`, email `dev-worker-N-bot@<NO_REPLY_ADDRESS>`.
   - Flags: `--random-password --must-change-password=false`, non-admin, not restricted, and the
     same visibility as `dev-worker-bot`.
   - `--random-password` **prints the password**. The command runs with its stdout discarded and
     its exit status checked: the password is never used, and the account logs in only by token.
   - Before any token is distributed, read back with `gitea_admin`: `is_active=true`,
     `must_change_password=false`, `prohibit_login=false`, `is_admin=false`, `restricted=false`.
     The API refuses PAT authentication for a must-change-password or prohibit-login account.
     `prohibit_login` is set **only at retirement**.
2. **Parity, not new grants.**
   - Add each account to team `automation` (write on all repos, never admin). That is the only
     team `dev-worker-bot` is in.
   - Add the same explicit collaborator grant on `dsh-acp-plus`.
   - Add no whitelist entry: every branch-protection whitelist names users, and none names
     `dev-worker-bot`.
3. **Tokens per slot, with the same three scopes as today** (`docs/runbooks/openbao-dev-workers.md:69–118`).
   The token names are `dw<N>-git-<date>`, `dw<N>-repo-<date>` and `dw<N>-pkg-<date>`:
   - `gitea_pat`: `write:repository,write:issue,read:organization,read:user`
   - `gitea_repo_pat`: `write:organization,write:repository,read:user`
   - `gitea_package_pat`: `write:package,read:repository,read:user`

   The package token stops being "shared on purpose" (`openbao-dev-workers.md:116`). The reason
   for sharing it was that the publishing agent is not pinned to a worker, and every slot now has
   its own token. D4 decides whether `gitea_repo_pat` keeps `write:organization`.
4. **Storage: the slot's existing flat document `af/dev-workers/dev-worker-N`**, with fields
   `gitea_pat`, `gitea_repo_pat` and `gitea_package_pat`. This needs no Job, policy or `cred`
   change:
   - **Policy.** The worker policy already grants `read` on exactly `af/data/dev-workers/${host}`
     (`devworker-provision-job.yaml:89`).
   - **Seeding.** The provision Job already seeds `dev-worker-N.json` → `af/dev-workers/dev-worker-N`
     with seed-wins `kv patch`, and creates the document with `put -cas=0`. Fields that
     `k8stoken-sync` writes into the same document survive (`:319–352`). The keys
     `dev-worker-1..4.json` already exist in `devworker-seeds.sops.yaml`.
   - **Agent access.** `cred` resolves `<name>` against the host subtree, then the shared one, so
     agents use `cred exec {{ inventory_hostname }} gitea_pat …`. The instruction block is rendered
     per host. `cred get common gitea_pat` keeps returning the shared token until Phase 5, which is
     exactly the transition behaviour wanted.

   **Ceremony for one slot. All three tokens go into one seed edit, so there is never a partial
   document:**
   1. Mint the three tokens in-pod with `--raw`. Pipe each value straight into the SOPS seed edit
      (`sops set --value-stdin` on the seed key's JSON field; confirm the flag on the installed
      sops, otherwise decrypt to a RAM-backed temp and re-encrypt). A value never reaches argv,
      a terminal, a transcript or a persistent file.
   2. Commit the seed. Flux recreates the Job, and the Job patches the document.
   3. **If a token was minted but not durably stored** (the seed edit failed), delete that token
      by ID straight away and mint it again. Never leave an unrecorded live token.
   4. Delete the duplicate `dev_worker_gitea_token` in `ansible/secrets/dev-worker.sops.yaml`.
      `owner-credentials.md:17` wrongly calls that file the source.
5. **Verify each account before any consumer uses it.** For each of the twelve tokens:
   - authenticated `GET /user` returns the expected slot login and user ID, with `is_admin=false`.
     A repository read alone cannot tell a slot token from the old shared one;
   - one in-scope call returns 200, and one out-of-scope call returns 403 (for example,
     `gitea_pat` on a package read).

   Then read back team membership and collaborator grants with `gitea_admin`.
6. **AppRole isolation, server-side.** On each worker, using its own AppRole token:
   - it reads its own document's three fields;
   - reading a sibling's document is **denied**;
   - writing its own document is **denied**.

   The `cred` unit test stubs `bao` and cannot prove this.

### Phase 3 — Teach every name-keyed consumer the new logins (lands before any slot switches)

- **Legacy reviewbot.**
  - Add `dev-worker-1-bot` … `dev-worker-4-bot` to `pr_reviewer_merge_authors`
    (`ansible/roles/pr_reviewer/defaults/main.yml:244`).
  - **Keep `dev-worker-bot`**: PR authorship cannot be changed, and its open PRs need merge
    eligibility until they close.
  - Converge both reviewer hosts. The startup log does not print the list, so read back only
    `merge_authors` from `/etc/reviewbot/config.json` and confirm the service restarted after the
    write.
- **Stateless reviewer (`cchifor/reviewer`).** It loads `policy.json` from its configured
  `coord_repo`/`coord_ref`, which need not be the source repository.
  - Identify the deployed location from its config.
  - Update the global list there, and **every** `merge_authors_by_repo` override. An override
    **replaces** the global list for its repo (`src/reviewer/engine.py:847`).
- **platform `OWNER_ACK_USERS`** (repository variable; decision D3). Recommended: list the four slot
  bots in place of `dev-worker-bot`. That is parity with today's instruction, and each
  acknowledgement now names its slot. Removing `dev-worker-bot` waits until Phase 5.
- **`scripts/check-slot-enumerations.py`.** A new committed enumeration,
  `dev_worker_gitea_users` in `ansible/group_vars/dev_workers.yml`, joins the slot lists the check
  already parses. The check fails closed when that source is missing or malformed, and the
  retired slots are excluded as they are elsewhere.
  - This proves only that the repository's declarations agree. Whether the accounts, tokens and
    grants exist live is the Phase 2 readback, a separate enrolment gate.
- **Docs.** Update:
  - `docs/runbooks/openbao-dev-workers.md` (token table);
  - `docs/runbooks/owner-credentials.md` (fix line 17);
  - `docs/runbooks/s2s-identity.md` (rotation per slot, plus the enrolment procedure);
  - `docs/runbooks/relay-reviewer-onboarding.md:50`;
  - the ADR 0020 and ADR 0021 follow-up lists;
  - `CLAUDE.md` (identity summary).
- **Tests.**
  - **Merge-author transition cases:**
    - `dev-worker-bot` and every slot bot are eligible;
    - a legacy-authored PR that a slot bot later pushed to is still eligible, because eligibility
      keys on the PR author;
    - an unrelated author is denied.

    Cover both the legacy reviewbot's additive repo policy and the stateless reviewer's replacing
    policy. The legacy author is removed only in the retirement case.
  - **`check-slot-enumerations`** with the new list.
  - **`roles/dev_worker/tests/test-cred-helper.sh`**: the host name resolves to the host document.
  - **`test-git-credential-readonly.sh`.**
  - **A new test:** the rendered credential line and the git identity carry `<host>-bot` when the
    host is cut over, and stay `dev-worker-bot` when it is not.

### Phase 4 — Switch slots one at a time (canary first)

1. **A per-host cut-over gate.** This follows ADR 0021's precedent,
   `dev_worker_openbao_kubeconfig_cutover`.
   - A new `dev_worker_gitea_identity_cutover` defaults to `false`, and is set `true` per slot in
     `host_vars/` as each slot switches.
   - While it is `false`, a full converge renders exactly what it renders today, so an unrelated
     converge cannot switch a slot.
   - When `true`, three things change **together**:
     - `dev_worker_gitea_user` becomes `{{ inventory_hostname }}-bot`;
     - `git-credentials.ctmpl.j2` reads `af/data/dev-workers/{{ inventory_hostname }}`;
     - the instruction block names `cred … {{ inventory_hostname }} …` instead of `cred … common …`.
   - The gate stays until Phase 5. The role then drops the legacy branch.
2. **Preflight on the slot, before flipping the gate.** The `<no value>` guard in
   `openbao-agent.hcl.j2:50` stops the whole agent on a missing field. It is failure detection, not
   a cut-over gate, so this preflight is the gate. Check that:
   - `hostname -s` equals `inventory_hostname`;
   - all three fields are non-empty, read with the slot's **own** AppRole token;
   - `GET /user` with the fetched `gitea_pat` returns `dev-worker-N-bot`.

   Any failure leaves the gate `false`.
3. **Drain the slot.** The render (`openbao.yml`) runs before `pin_gitea_username.yml`. Between the
   two, the rendered credential line and the pinned username disagree, and git finds no credential.
   - No agent job or interactive session may run on the slot during its converge. Check `who` and
     the agent processes, and hold new jobs.
   - Other slots are unaffected: they switch on separate days.
4. **Rollback, defined and rehearsed on the canary.** Set the gate back to `false` and converge.
   That re-renders the template, the credential and the pinned username as one change. It must
   *also*:
   - remove the rendered `~/.git-credentials` before re-rendering;
   - restore the pinned username.

   `openbao.yml:412` today restores only `agent.hcl` and a kubeconfig marker. The role's git health
   check only requires a non-empty file, so an old credential can pass while the new template is
   broken. Acceptance therefore reads the **login in the rendered line** and calls `GET /user` with
   it, both after the cut-over and after the rehearsed rollback.
5. **Canary.** Flip the gate on one drained slot and converge with `--limit dev-worker-N`. Then
   check from the slot:
   - `git ls-remote` succeeds;
   - a commit pushed to a scratch branch shows the `-bot` author;
   - a disposable package publish to `cchifor` and its removal succeed with
     `cred exec dev-worker-N gitea_package_pat …`;
   - a scratch org-repository creation with `gitea_repo_pat` succeeds, and the operator deletes
     the repository afterwards;
   - the slot cannot do an admin operation, cannot push to a protected `main`, and cannot merge
     past protection;
   - **reviewbot merge path:** a small real docs PR in **ailab**, which is admitted to
     `pr_reviewer_repos`, opened by the slot bot, is reviewed and merged by the reviewbot. In the
     same window, an open `dev-worker-bot`-authored PR stays merge-eligible. `cchifor/primes-lab`
     is not admitted, so it cannot prove this.
6. **Roll the remaining three slots** on separate days, each with preflight, drain and the same
   checks.

### Phase 5 — Retire `dev-worker-bot`

**Revocation and authorship retire separately.**
- **Tokens** can go as soon as nothing *uses* them. Every slot is cut over, every session started
  before its slot's cut-over has ended, and no script, package login or Docker login still reads
  `cred … common gitea_*` (check `~/.docker/config.json`, npm and pip configs, and running
  processes on each slot).
- **The merge-author entries** go only when no open PR is *authored* by `dev-worker-bot`.

1. **Revoke the tokens once their consumers are drained.**
   - Enumerate `dev-worker-bot`'s current token IDs with the `gitea_admin` basic-auth API, rather
     than assuming three.
   - Delete each one.
   - Read back an empty list.
   - Prove that each retained old value is refused (401) from a slot.
   - Only then set `prohibit_login`; set earlier, it could mask an incomplete revocation.
   - **Keep the account**: deleting it would orphan the PR, comment and commit history.
2. **Strip the shared fields.** Remove `gitea_pat`, `gitea_repo_pat` and `gitea_package_pat`
   (**only** these) from `common.json` in the seed, and add a `MOVED_FIELDS` line
   `common gitea_pat gitea_repo_pat gitea_package_pat`. The Job's null-patch then deletes the live
   copies on every run, so seed-wins neither keeps nor resurrects them.
   - `common` also holds `proxmox_ssh_key`, the `strive_test_*`, `litellm_diag_*` and `registry_*`
     credentials. All of them stay.
3. **Remove the remaining grants.** Re-enumerate open PRs authored by `dev-worker-bot` across every
   reviewed repository, paginated. When none remain (or the owner closes the stragglers), remove
   `dev-worker-bot` from:
   - `pr_reviewer_merge_authors`;
   - the stateless reviewer's deployed `policy.json` and every repo override;
   - `OWNER_ACK_USERS`;
   - team `automation`;
   - the `dsh-acp-plus` grant.
4. **Drift alarm, the whole pipeline.** The pipeline is the SQL views in
   `gitea-credential-inventory.sql` (today they watch only `chifor`, `cchifor`, `gitea_admin` and
   admins), applied by the operator; the exporter queries in `infra-pg-gitea-credential-queries.yaml`;
   and the alerts in `gitea-credential-rules.yaml`, with rule-test fixtures.
   - **Alert:** `dev-worker-bot` holds any token.
   - **Alert:** an *active* `dev-worker-N-bot` that is not in the slot list holds a token, a team
     grant, or `prohibit_login=false`. A disabled, credential-free account of a retired slot is the
     expected end state and stays silent.
   - **Alert:** a slot bot holds tokens other than its expected names. A **rotation window** allows
     at most two generations of each name for at most 24 h, so mint-before-revoke rotation does not
     page.
   - **Alert:** an expected slot account or its metric is **missing**.
   - Verify as `pg_monitor` that the exported aggregates contain no token material. Then plant a
     test token on a fixture account that the new query actually selects (a disabled
     `dev-worker-9-bot`), watch the alert fire, remove the token, and watch it clear.

### Slot lifecycle (re-slot, retire)

The account **name** belongs to the slot, like the AppRole. The **tokens do not survive a change of
machine**. When a different VM takes a slot (as on 2026-09-23):
1. mint new tokens for the slot;
2. revoke the previous holder's AppRole SecretIDs and tokens, as in ADR 0020's re-slot amendment;
3. delete the old PATs;
4. verify that the old credentials fail before the new machine is enabled.

A retired slot's account is disabled, its tokens are deleted, and the account is never deleted.
`RETIRED_SLOTS` in the provision Job gains this step in the runbook.

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
  Phase 5 removes the authorship entries only after a paginated re-enumeration finds none open.
- **Missing a consumer that keys on the login.** Found so far:
  - reviewbot, `cchifor/reviewer`, `OWNER_ACK_USERS`;
  - platform `build.yml:28` and `release-build-access.yml:13` (owner and reviewer logins only, unaffected);
  - `scripts/gitea-api.sh` (`workstation-bot`, unaffected).

  Phase 3 lands before Phase 4, so a missed one fails on the canary, not on four slots.
- **Secrets in the ceremony.** Both `--random-password` and `--raw` print a value. The password is
  discarded, and the tokens go straight into the seed edit.
- **A cut-over under a running agent breaks its git** for the window between render and pin.
  Phase 4 drains the slot first.
- **More accounts to audit:** four more non-admin users. The Phase 5 alarm covers active, retired
  and missing accounts.
- **This does not close the self-attested owner gate** (owner-ack). It makes each acknowledgement
  name its slot. Closing the gate is D3's follow-up.

## Critical files

- **`ansible/roles/dev_worker/`:**
  - `defaults/main.yml`: the `dev_worker_gitea_identity_cutover` gate and the per-host user;
  - `templates/git-credentials.ctmpl.j2`: the host document when cut over;
  - `tasks/git_identity.yml` (new) and `tasks/openbao.yml`: instruction block, trailers and
    markers, rollback;
  - `tests/*`.
- **`ansible/host_vars/dev-worker-N.yml`:** the per-slot gate.
- **`ansible/group_vars/dev_workers.yml`:** `dev_worker_gitea_users`.
- **`ansible/roles/pr_reviewer/`:**
  - `defaults/main.yml` (`merge_authors`);
  - `files/reviewbot.py` (the informational instance line);
  - `scripts/tests/test_reviewbot.py`.
- **`kubernetes/apps/infrastructure/security/openbao/`:**
  - `devworker-seeds.sops.yaml`: `dev-worker-N.json` gains three fields; `common.json` loses three
    in Phase 5;
  - `devworker-provision-job.yaml`: the `MOVED_FIELDS` line in Phase 5.
- **Gitea credential alarm:** `gitea-credential-inventory.sql`,
  `infra-pg-gitea-credential-queries.yaml`, `gitea-credential-rules.yaml` and their rule tests.
- **Scripts:** `scripts/check-slot-enumerations.py`, `scripts/gitea-api.sh` and their tests.
- **Docs:** `docs/runbooks/{s2s-identity,openbao-dev-workers,owner-credentials,relay-reviewer-onboarding}.md`,
  the ADR 0020/0021 follow-up lists, `CLAUDE.md`.
- **Outside ailab:**
  - the platform repository variable `OWNER_ACK_USERS`;
  - the stateless reviewer's deployed `policy.json`;
  - relay and llm-router protection and collaborators (Phase 0);
  - Gitea accounts and tokens.

## Verification

1. **Unit and CI:**
   - **dev_worker role tests:** the gate on and off, credential line, git identity, rollback.
   - **reviewbot tests:** merge-author transition cases, and the informational instance line next
     to an unchanged marker.
   - **`scripts/tests/test-check-slot-enumerations.sh`.**
   - **`scripts/tests/test_gitea_api.py`:** marker insertion only on comment and review endpoints,
     idempotent, other payloads byte-identical, credential guard intact.
   - **`scripts/tests/test-devworker-provision.sh`:**
     - empty-vault seeding of a host document with the gitea fields;
     - a second seed run;
     - a rotation that persists;
     - a retired host not recreated;
     - `k8stoken-sync` and `common` fields preserved;
     - the Phase 5 `MOVED_FIELDS` strip of exactly three fields.
   - **The full suite:** `ansible-lint` and the Gitea credential rule tests.
2. **Accounts (Phase 2):**
   - each token's `GET /user` identity, plus one 200 and one 403;
   - account flags and grants read back with `gitea_admin`;
   - AppRole isolation (own document read, sibling read denied, own write denied).
3. **Canary (Phase 4):**
   - preflight;
   - the end-to-end checks in Phase 4 step 5, including package publish and removal, scratch
     repository creation, the denied admin and protected-branch operations, and a reviewbot merge
     of a slot-authored ailab PR next to an eligible legacy PR;
   - the rehearsed rollback, verified by the rendered login and `GET /user`.
4. **Fleet:** for each slot:
   - the next real agent commit, PR and comment show `dev-worker-N-bot` as author;
   - they carry the trailers and the `worker:` marker;
   - the commit API's `author.id` is the slot user's ID.
5. **Phase 0:**
   - the full protection read-back on both repositories;
   - the webhook delivery log;
   - the worker push and merge are refused;
   - the reviewbot merges, and `merged_by` names the reviewer.
6. **Retirement (Phase 5):**
   - `dev-worker-bot` has an empty token list, every old value is refused, and the account has
     `prohibit_login=true`;
   - `common` has no gitea fields, and every other field is present;
   - the alarm fires on the planted fixture token, then clears;
   - the alarm stays silent for the retired fixture account.

<!-- codex-review-status: complete -->
