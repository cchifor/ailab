# Resolve the four open ailab PRs (#973, #981, #971, #835) and the #975 NAS deploy

## Context

This follows `plans/2026-09-29-held-prs-plan.md` (branch `plan/held-prs-2026-09-29`, codex-reviewed).
That plan's track A became #973, track B became #971, and track C became issue #972. Earlier today the
PR queue was triaged: #975, #977, #978 and #982 were merged after their review findings were fixed,
and their rollouts were verified (all four OpenBao provision Jobs succeeded on 2.6.3, cri-log-relay is
Ready on talosctl v1.11.6, and the pg-sync bootstrap Job succeeded on postgres 16.15). Four PRs
remain. Each is held with `no-automerge`, which the reviewbot honours, so none of them merges without
a human decision. State as of 2026-09-30 19:20–19:50Z. This is a snapshot, so every execution gate
below refreshes the evidence it depends on.

**GitOps path (verified 19:50Z; CLAUDE.md is stale on this).**
- The `flux-system` GitRepository sources ailab from the **GitHub push-mirror**
  (`https://github.com/cchifor/ailab.git`, interval 1m), not from in-cluster Gitea.
- Gitea's push mirror to GitHub has `sync_on_commit: true`, with an 8 h fallback interval.
- The `apps` Kustomization: interval 10m, timeout 5m, `wait: true`.
- At 19:50Z, Gitea main, GitHub main and `apps.lastAppliedRevision` were all `201ee114`.
- So a Gitea outage blocks *publishing* a revert, but Flux keeps reconciling the last mirrored state.

**#973: `fix(litellm)`: read chatgpt-chat usage off the wire; LiteLLM v1.101.0 → v1.103.0.**
- Head `603a4257`, mergeable, CI green on all 7 checks, and both reviewer bots APPROVED this head.
- Finding 50752 is fixed. v1.103.0's parser raises `APIConnectionError` during the drain on a
  malformed `*_tokens_details`, and the handler converts both that and the post-drain mapping failure
  into its own 502. New contract cases: `e-outdetailsusage`, `e-indetailsusage`,
  `e-usage-mapping-guard`. The contract passes in-image on v1.103.0 (31 cases) and on v1.101.0, and
  the startup smoke is OK.
- What the tests prove: the NON-streaming usage fix, against a mocked upstream. They do not prove
  production auth, DB compatibility, ingress, or whole-gateway upgrade safety. Streaming usage stays
  ESTIMATED by design (ADR 0027): the streaming contract records completion 9 against the fixture's
  upstream 17.
- **Breaking-change review, v1.101 → v1.103:**
  - *Budget re-check on router fallback targets.* The main gateway has `fallbacks` and one global
    `max_budget: 50` USD / 30d, so this only changes behaviour once that budget is exhausted, and
    then the primary is refused too. `litellm-local` has no `fallbacks`. Inert.
  - *config.yaml wins over DB settings.* `LiteLLM_Config` has 0 rows, and both proxies are configured
    only by their mounted config.yaml. Inert.
- **DB migrations:**
  - Only `litellm-local` has a `DATABASE_URL` (DB `litellm`, 13 MB).
  - v1.103.0 adds 18 Prisma migrations. All are additive (ADD COLUMN / CREATE TABLE / CREATE INDEX),
    and none is `NOT NULL` without a default.
  - The one index swap is on `LiteLLM_JWTKeyMapping`, which has 0 rows.
  - So v1.101.0 runs on the migrated schema, and an image rollback is DB-safe.
- **Rollout:**
  - `litellm`: 2 replicas, maxUnavailable 0 / maxSurge 1, ConfigMap `litellm-config` (config + handler).
  - `litellm-local`: 1 replica, which rounds to surge 1 / unavailable 0, ConfigMap `litellm-local-config`.
  - Readiness only hits `/health/liveliness`, so functional checks run per pod.
  - No preStop hook and default grace, while main allows 900 s requests: **a long stream in flight
    at the switch can be cut. This is accepted explicitly.** Recent-traffic evidence lowers the
    odds; it does not prove the pods have drained.
- **Load:** LiteLLM CPU over 7 days is busiest at 07–12Z and 18–19Z (max 0.045 cores) and quiet from
  20Z to 06Z. It's a weak signal, so it's supplemented by the pods' recent request logs.
- **Pre-merge prep, done 19:35–19:45Z:**
  - Rollback reference recorded: main/Flux `201ee114`, both images
    `v1.101.0@sha256:d295634e09c6…`, checksums `chatgpt-chat dfd6112a7bfc` / `config abd1f60bae7a`
    (main) and `config 422b24ab77f8` (local), ReplicaSet revisions 90 (main) and 21 (local).
  - DB dump `_out/litellm-db-20260930-pre-v1.103.0.dump` (264 KB, `pg_restore -l` OK, 78 table-data entries).
  - Smoke baseline `smoke-baseline-v1.101.0.json`: **14/14 pass**, 1.3–2.5 s. The `*-cloud` routes
    are excluded because cloudlab is powered off at night.

**#981: Renovate, LiteLLM v1.101.0 → v1.103.1 (`no-automerge` by the renovate.json LiteLLM rule).**
- The contract fails because v1.103.x with the OLD handler reproduces the regression #973 fixes. That
  says nothing yet about v1.103.1 with #973's handler.
- reviewer-claude findings: 51202 (a stale pin-comment version in `litellm-local.yaml`) and 51203
  (the v1.102/1.103 breaking changes).

**#971: `feat(llm-router)`: enable the admin-bridge plugin (held for the owner's explicit go).**
- It ENABLES a public, signed-assertion endpoint (`POST /register/admin-bridge`) on the Recreate
  singleton. The plugin code already ships in the router artifacts.
- No intended caller exists (verified 19:20Z): `trueswarm-admin/admin` has no bridge key, no
  ROUTER/BRIDGE env, and no router key in `admin-config`. The endpoint would still be reachable by
  anyone. The replay window is ≤60 s (no nonce store).
- It conflicts in `router.yaml` (six release rolls since the branch point). Its artifact proof is from
  a release that is no longer live.

**#835: env-node-2 (parked).** It is gated on issue #972, which is open: golden-v2 has never been
built and the pool is at `replicas: 0`. The IP is `.39`, a reservation (main #991). It conflicts in
`sandboxtemplate-std.yaml`.

**#975 follow-up: the NAS versitygw supervisor is not Flux-managed.** Only the alert rule shipped.
The NAS still runs the OLD watchdog. The PR says: "Deploy (after merge, operator go only — not done by this PR)".

## Approach

Ordering: #973 tonight, alone. The NAS deploy (5) never shares a window with a gateway change: the
recorded USB incident also took Gitea down, which blocks publishing a gateway revert.
All cluster commands use `kubectl --context admin@ai` (the default context is a different cluster),
with `-n flux-system` for Flux objects and `-n ai` for LiteLLM.

### 1. #973: merge from 20:00Z, verify each pod, keep a prepared rollback

1. **Pre-merge gate (refresh everything):**
   - The head is still `603a4257`, CI is green on it, both approvals are on it, and it is mergeable.
   - The rollback baseline is still live: Gitea main, GitHub main and `apps.lastAppliedRevision` all
     equal the recorded `201ee114`. Otherwise re-record the baseline and re-render the recovery bundle.
   - The pods' last 10 min of request logs, read as recent-traffic evidence only.
   - **Recovery bundle rendered and saved before the merge:**
     - `kustomize build kubernetes/apps/apps/ai` at `201ee114`, allowlisted to exactly
       `ConfigMap/ai/litellm-config`, `ConfigMap/ai/litellm-local-config`,
       `Deployment/ai/litellm` and `Deployment/ai/litellm-local`. No Secrets and no other resources.
     - The apply command is written next to it.
2. **Merge:** squash-merge via the API with `head_commit_id: 603a4257…`, so a changed head is refused.
   Record the squash sha.
3. **Source + rollout watch:**
   - **Source (bounded: 10 min from the merge):**
     - GitHub `main` == the squash sha. If not, trigger `POST …/push_mirrors-sync`.
     - The `flux-system` GitRepository artifact carries the squash sha.
     - `apps` starts applying it (Kustomization events / attempted revision), with
       `flux reconcile kustomization apps -n flux-system --with-source` as a nudge.
     - Past 10 min without the source reaching Flux: stop and investigate the mirror or Flux, with
       nothing live changed yet.
   - **Rollout (bounded: 10 min from `apps` applying the sha):**
     - `apps` shows `lastAppliedRevision` == the squash sha and is Ready.
     - Both Deployments reach updated == available == desired, with no `ProgressDeadlineExceeded`.
     - Every pod is on `v1.103.0@sha256:bd089afd…`, and the endpoints contain only new pods.
     - Keep the rollout events and any terminated-pod state.
     - The handler's import-time seam check passed (logs).
     - `_prisma_migrations` has the 18 new rows finished.
4. **Functional checks. Every call has an overall deadline of 120 s, including SSE completion.**
   - **Main gateway (each of the 2 new pods via port-forward, and once via `https://api.chifor.me`):**
     - `gpt-5.6-sol` non-streaming in the consumer's shape (strict `json_schema` +
       `reasoning_effort`). Checks: valid JSON content, a sane `finish_reason`, and usage present with
       `total = prompt + completion`.
     - `gpt-5.6-sol` streaming with `include_usage`. Checks: content, a terminal finish reason, a
       usage chunk (estimated by design), `[DONE]`, and no in-band error.
     - `/v1/responses` returns output.
     - One ailab-served non-chatgpt route (`qwen3.8-27b-ailab`).
   - **Local gateway (the new pod via port-forward, and once via the LAN path
     `litellm-lan` NodePort 30400):**
     - One `qwen3.8-27b-ailab` completion.
     - A **temporary restricted virtual key**, created with the master key:
       `models=[qwen3.8-27b-ailab]`, a tiny `max_budget`, `duration=1h`.
     - The allowed model answers, and a disallowed model is refused.
     - `/key/info` shows its spend increasing after the accounting flush.
     - The key is deleted afterwards.
     - The existing tenant key is only read via `/key/info`.
5. **Rollback triggers (act immediately):**
   - a functional check failing reproducibly
   - a crash-loop or a rollout past its deadline
   - broken SSE or an empty/malformed 200
   - an auth/budget 4xx on a call that passed in the baseline
   For latency alone, re-run the same bounded call twice under comparable conditions, and roll back
   only on a consistent regression over 2× the baseline. A single non-reproducing upstream 5xx is not
   a trigger.
6. **Rollback procedure (prepared before merging, owner = this operator session):**
   - **Normal path (deadline 20 min from the decision):**
     1. `git revert <squash-sha>` as a PR, squash-merged via the API. That restores the three image
        refs, the handler bytes and the checksums together.
     2. Confirm GitHub main == the revert sha, and the GitRepository artifact carries it.
     3. `flux reconcile kustomization apps -n flux-system --with-source`.
     4. Verify the restored rollout and endpoint membership, then re-run step 4.
     If the CI/review queue or source convergence would exceed 20 min, switch to the emergency path.
   - **Emergency path** (Gitea down, or the normal path past its deadline):
     1. `flux suspend kustomization apps -n flux-system`, and confirm it is suspended.
     2. `kubectl --context admin@ai apply` the saved recovery bundle: the ConfigMaps first, then the
        Deployments. That restores config, handler and image together; ReplicaSet `rollout undo`
        alone would pair old pods with the in-place-updated ConfigMaps.
     3. Verify the rollout, the endpoints and step 4.
     4. Keep `apps` SUSPENDED until the revert is merged, mirrored to GitHub, and present in the
        GitRepository artifact. Only then `flux resume kustomization apps -n flux-system`.
        Resuming earlier would re-apply the failed version from the cached source.
   - The DB needs no action (additive migrations; the dump is only for disaster).
7. **Observation:**
   - Start the 15-min window after both rollouts complete. At its end, run a short smoke: main
     non-streaming + streaming, a local completion, and one temp-key call. Also check restarts,
     memory and Flux status. Re-run the full step-4 suite only if pods changed or an anomaly appeared.
   - Re-check after the 07Z load start: restarts, logs, one smoke call.
   - Only then is #981 eligible.

### 2. #981: keep it open, let Renovate regenerate it, and review the patch delta only

1. After #973 is observed, **fix the version drift at the source** in a small PR to main. It is
   scheduled as its own local-pod roll, because it changes `checksum/config`:
   - make the `litellm-local.yaml` pin comment version-agnostic ("SAME pin as litellm.yaml; bump together")
   - remove the image digest embedded in the mirrored-route description inside `litellm-local.yaml`'s
     `config.yaml`, which a Renovate image bump would leave stale
   - regenerate `checksum/config`
   No human commits on the Renovate branch. This answers 51202.
2. Renovate's CronJob runs every 4 h (concurrency Forbid). After the next successful run, confirm
   #981 is rebased. If not, tick its rebase checkbox, and investigate a conflict or manual-commit
   detection if it stays stuck.
3. On the regenerated head, verify:
   - It contains #973's handler + tests and still targets v1.103.1, and all three image refs agree
     (tag@digest).
   - All CI is green, including the contract and the startup smoke.
   - Fresh approvals are in from both bots.
4. Answer 51203 by linking section 1's review (v1.101 → v1.103.0), plus a fresh review of the
   v1.103.0 → v1.103.1 changelog and migration delta, done the same way.
5. Merge in a later quiet window with section 1's procedure, **re-parameterised**: #981's full head
   sha, its target digest, a freshly recorded rollback baseline and recovery bundle (the
   #973/v1.103.0 state), its actual migration delta, and a fresh smoke baseline. The rollback reverts
   #981's own squash commit, which keeps the handler fix.

### 3. #971: hold. Post the evidence and the exact merge preconditions

- Reason: enabling a public endpoint that no intended caller uses adds exposure (including the
  replay window) for no benefit. The PR itself offers "hold it and merge alongside the Admin change".
  Rebasing now is wasted work, because the router release rolls daily.
- Comment on #971 with today's evidence and these preconditions, ALL required:
  1. **Admin side deployed and verified.** The caller is disabled or tolerant until the router is
     verified. The key fingerprint freshly matched (public ConfigMap ↔ Admin's private key).
  2. **Rebase and validate.** Rebase onto the current `router.yaml` keeping the live release. Re-run
     manifest CI and bot review on the resolved head, and bind the artifact evidence to that exact release.
  3. **Test the selected artifact's authorization:**
     - the plugin is present and registered
     - unsigned, tampered, wrong-key/alg, wrong iss/aud, expired/future, missing-claim and
       body/path/action-mismatch assertions are all rejected
     - operator-role restrictions hold
     - Admin derives the role from server-side authz
  4. **Exposure review:** the ingress path, direct-origin restriction, size/rate limits, audit logs
     without assertions, and key rotation/revocation reaching the verifier.
  5. **The owner's explicit go**, regardless of any nonce store. It either accepts the replay window
     (with the duplicable actions listed) or requires atomic replay rejection first.
  6. **A Recreate maintenance window.** Preflight the ConfigMap/mount. After the roll, a real
     Admin-signed request and existing API-key traffic both work. Rollback = remove the env + mount
     while keeping the release.
- Keep `no-automerge`. Owner: the #971 author and the platform owner. Next check: when trueswarm-admin
  ships the bridge caller.

### 4. #835: no change. It stays parked behind #972

- #972's gate is the whole list in that issue:
  - storage cleanup
  - source-volume retention
  - registry-pull recovery (#865/#879)
  - a measured lease/release/refill cycle
  - demand evidence
  - the ai-node3 G3b memory headroom
  - `.39` / vmid 4402 re-checked at execution
- An infra-only split is possible but has no demonstrated need.
- Post a note on #835 and #972 that the 2026-09-20 plan's `.38` references are superseded by `.39`.
  Historical plans are not rewritten.
- Owner: the #972 assignee. Next check: when #972 closes.

### 5. #975 NAS deploy: ESCALATED. It needs the operator's explicit go, in its own window

The merged PR requires "operator go only"; merge and review do not grant it. When the go is given
(never in the same window as a gateway change):
1. **Workspace.** In Git Bash, `cd` into the ops checkout `C:\Users\chifo\work\home\ailab`. Confirm
   the branch is `main` tracking `gitea/main`, then `git pull --ff-only`, stopping on any conflict.
   Confirm HEAD contains #975, and that `.env` + Python/Paramiko resolve (no credentials printed).
2. **Pin the NAS host key first.** `scripts/qnap-ssh.py` uses Paramiko `AutoAddPolicy`. Compare the
   fingerprint against a trusted record (QNAP UI/console) before sending credentials, and fix the
   helper to load known hosts in its own PR.
3. **Capture state:**
   - the deployed script + md5, for forensics only
   - `/etc/config/crontab` and the loaded root crontab
   - the status file and any lock/maintenance state
   Check that no other installer or cron editor is running, and that the USB is mounted and healthy.
4. **`DRY_RUN=1 bash scripts/qnap-versitygw-install.sh`** and review EVERY effect:
   - the script copy
   - the cron reconcile, which removes every `versitygw` line, so preserve unrelated ones
   - the on-USB watchdog retirement
   - old-log preservation/truncation
   - that apply runs the supervisor once, which may restart an unresponsive gateway
5. **Apply**, then require:
   - a status with a post-install timestamp (`healthy`, or `restarted` then a fresh `healthy`)
   - the lock state inspected, never deleted while live
   - both crontabs correct, with unrelated lines intact, and the scheduler running
   - the deployed md5 == the repo's
   - a successful scheduled `versitygw-probe` cycle (the CronJob's last success time updates) and the
     alert rules evaluated, with no ad-hoc overlapping probe
6. **Residual:** the running gateway's stdout stays on USB until its next restart. Note it, and
   verify at a separately justified restart.
7. **Recovery: forward fix only.** A corrected script that keeps the mount guard, installed the same
   way. The captured old script is never reinstalled: it is the vulnerable pre-#975 version. The
   captured crontab is used only to restore unrelated cron lines if the reconcile damaged them.
   `.env` must be provided explicitly to any other worktree. Truncated logs and gateway restarts
   can't be undone.

## Critical files

- `kubernetes/apps/apps/ai/litellm-local.yaml`: the version-agnostic pin comment + the digest removed from the route description + `checksum/config` (step 2.1, a small PR to main).
- #971, #835, #972: comments only. No branch edits.
- `scripts/qnap-ssh.py`: host-key pinning (step 5.2, its own PR, only with the NAS go).
- `scripts/qnap-versitygw-install.sh` / `scripts/qnap-versitygw-watchdog.sh`: deployed, not edited (step 5).

## Verification

- **1, main gateway (2 pods + `api.chifor.me`):** `apps` is applied at the squash sha and Ready. Both
  pods are on v1.103.0 and are the only endpoints. Non-streaming usage is present and additive;
  streaming delivers with an (estimated) usage chunk and `[DONE]`; `/v1/responses` and the ailab
  route answer.
- **1, local gateway (1 pod + LAN NodePort 30400):** on v1.103.0, with the 18 migrations applied. A
  local completion answers. The temp restricted key passes the allowed model, is refused the
  disallowed one, and its spend increments; it is then deleted. The existing key reads.
- **1, observation:** the 15-min short smoke is green with no restarts; re-checked after 07Z.
- **2:** the source-fix PR is on main. The regenerated #981 head contains #973's handler, its three
  refs agree, all CI is green, fresh approvals are in, and 51202/51203 are answered. It is merged
  with the re-parameterised section 1.
- **3:** the #971 comment is posted with the six preconditions, owner and next check. The label is unchanged.
- **4:** the `.38`-superseded note is posted on #835/#972, with owner and next check.
- **5:** not executed without the operator's go. When executed: all of step 5.5.

<!-- codex-review-status: finalized -->
