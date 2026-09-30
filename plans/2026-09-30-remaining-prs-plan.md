# Resolve the four open ailab PRs (#973, #981, #971, #835) and the #975 NAS deploy

## Context

This follows `plans/2026-09-29-held-prs-plan.md` (branch `plan/held-prs-2026-09-29`, codex-reviewed).
That plan's track A became #973, track B became #971, and track C became issue #972. Earlier today the
PR queue was triaged: #975, #977, #978 and #982 were merged after their review findings were fixed,
and their rollouts were verified (all four OpenBao provision Jobs succeeded on 2.6.3, cri-log-relay is
Ready on talosctl v1.11.6, and the pg-sync bootstrap Job succeeded on postgres 16.15). Four PRs
remain. Each is held with `no-automerge`, which the reviewbot honours, so none of them merges without
a human decision. State as of 2026-09-30 19:20Z. This is a snapshot, so every execution gate below
refreshes the evidence it depends on.

**#973: `fix(litellm)`: read chatgpt-chat usage off the wire; LiteLLM v1.101.0 → v1.103.0.**
- Head `603a4257`, mergeable, CI green on all 7 checks, and both reviewer bots APPROVED this head.
- The last finding (50752) is fixed. v1.103.0's own parser raises `APIConnectionError` during the drain
  on a malformed `*_tokens_details`, and the handler now converts both that and the post-drain mapping
  failure into its own 502. New contract cases: `e-outdetailsusage`, `e-indetailsusage`,
  `e-usage-mapping-guard`. The contract passes in-image on v1.103.0 (31 cases) and on v1.101.0, and
  the proxy startup smoke is OK.
- What the tests prove: the NON-streaming usage fix, against a mocked upstream. They do not prove
  production auth, DB compatibility, ingress, or whole-gateway upgrade safety. Streaming usage stays
  ESTIMATED by design (ADR 0027): the streaming contract records completion 9 against the fixture's
  upstream 17. Only non-streaming guarantees upstream-exact counts.
- **Breaking-change review for v1.101 → v1.103, done before the merge (2026-09-30):**
  - *Budget re-check on router fallback targets* (BerriAI/litellm#41379).
    - Main gateway: it has `fallbacks` and one global `max_budget: 50 # USD` / `30d`. A fallback
      target is now checked against the same global budget, so this only changes behaviour when that
      budget is already exhausted, and then the primary is refused too. Inert in normal operation.
    - `litellm-local`: no `fallbacks` at all, and one virtual key with a budget. Inert.
  - *config.yaml wins over DB-stored settings* (#41779 and follow-ups). `LiteLLM_Config` has 0 rows,
    and both proxies are configured only by their mounted config.yaml (`STORE_MODEL_IN_DB=False`).
    Inert.
- **DB migrations, reviewed:**
  - Only `litellm-local` has a `DATABASE_URL`. The main gateway is stateless.
  - v1.103.0 ships 18 Prisma migrations beyond v1.101.0, taken from `litellm_proxy_extras/migrations`
    in both images. All are additive: ADD COLUMN, CREATE TABLE, CREATE INDEX.
  - None adds a `NOT NULL` column without a default.
  - The one index swap (`scope_jwt_key_mapping_by_issuer`: drops 2 indexes, adds `jwt_issuer TEXT
    NOT NULL DEFAULT ''` + a unique index) is on `LiteLLM_JWTKeyMapping`, which has 0 rows.
  - v1.101.0 therefore runs on the migrated schema, so an image rollback is DB-safe. The migrations
    are not undone, and don't need to be.
  - The DB is 13 MB.
- Rollout:
  - `litellm`: 2 replicas, `RollingUpdate maxUnavailable 0 / maxSurge 1`.
  - `litellm-local`: 1 replica, which rounds to surge 1 / unavailable 0.
  - The image, `checksum/config` and `checksum/chatgpt-chat` all change.
  - Readiness probes only hit `/health/liveliness`, so a pod that is Ready but broken for requests
    can replace a good one. Functional checks per pod are therefore required (step 1.4).
  - No preStop / extended grace is configured and main allows 900 s requests. A long stream in
    flight at the switch can be cut. Accepted at a quiet hour; step 1.1 checks for in-flight work first.
- Load (LiteLLM container CPU, 7 days): busiest 07–12Z and 18–19Z, but tiny (max 0.045 cores), and
  quiet 20Z–06Z. CPU is a weak signal for an I/O-bound proxy, so step 1.1 also checks live request
  activity in the pods' logs just before merging.

**#981: Renovate, LiteLLM v1.101.0 → v1.103.1 (`no-automerge` by the renovate.json LiteLLM rule).**
- `litellm-route-contract` fails because v1.103.x with the OLD handler reproduces the regression #973
  fixes. That shows incompatibility with the old handler. It says nothing yet about v1.103.1 with
  #973's handler.
- reviewer-claude findings: 51202 (a stale version in the `litellm-local.yaml` pin comment) and 51203
  (the v1.102/1.103 breaking changes).

**#971: `feat(llm-router)`: enable the admin-bridge plugin (held for the owner's explicit go).**
- It adds a verifier key ConfigMap, env `ADMIN_BRIDGE_VERIFY_KEY` and a read-only mount. The plugin
  code already ships in the router artifacts, so this PR ENABLES a public, signed-assertion endpoint
  (`POST /register/admin-bridge`) on the Recreate singleton.
- No intended caller exists (verified live 19:20Z): `trueswarm-admin/admin` mounts no bridge key,
  and there is no ROUTER/BRIDGE env and no router key in `admin-config`. The endpoint would still be
  reachable by anyone.
- The residual risk the PR names: no `jti`/nonce store, so replay is possible within ≤60 s.
- It conflicts in `router.yaml` (six release rolls since the branch point). Its artifact proof is
  from `router-0.1.0-20260929-subs`, which is no longer live.

**#835: env-node-2 (parked).**
- Gated on issue #972, which is still open. golden-v2 has never been built, and
  `SandboxWarmPool env-std-pool` is at `replicas: 0` (#880).
- The IP is `.39`, a reservation for an unbuilt VM (main #991). `.38` is the live cloud-win-1.
- It still conflicts in `testpool/sandboxtemplate-std.yaml` (`replicas: 2` here, `0` on main).

**#975 follow-up: the versitygw supervisor on the QNAP NAS is not Flux-managed.**
- Only the alert-rule change shipped (Flux). The NAS still runs the OLD watchdog.
- The PR says: "Deploy (after merge, operator go only — not done by this PR)".

## Approach

Ordering: #973 tonight, alone. The NAS deploy (5) never shares a window with a gateway change,
because the recorded USB incident also took Gitea down, which is the gateway's GitOps recovery path.

### 1. #973: merge from 20:00Z, verify each pod, keep a prepared rollback

1. **Pre-merge gate (refresh everything):**
   - The head is still `603a4257`, CI is green on it, both approvals are on it, and it is mergeable.
   - Record the full pre-merge image digests, `checksum/config` / `checksum/chatgpt-chat`, the
     ReplicaSet revisions and the main sha.
   - Check the last 10 min of both LiteLLM Deployments' logs for in-flight or recent requests; if
     there is an active long stream, wait.
   - Baseline requests: run the step 1.4 smoke calls once BEFORE the merge, to have a reference
     (content, finish reason, latency).
   - `pg_dump -Fc litellm` from infra-pg to local storage as the DB restore point, and verify it with
     `pg_restore -l`.
2. **Merge:** squash-merge via the API with `head_commit_id: 603a4257…` so a changed head is refused.
   Record the squash sha. The `no-automerge` label only stops the bot; this is the human decision.
3. **Rollout watch (deadline 10 min from Flux applying the sha):**
   - The `apps` Kustomization (wait: true, 5-min timeout) shows the squash sha applied and is Ready.
   - Both Deployments reach updated == available == desired, with no `ProgressDeadlineExceeded`.
   - Every pod is on `v1.103.0@sha256:bd089afd…`, and the endpoints contain only new pods.
   - Keep the rollout events and any terminated-pod state, so a replaced crash still shows.
   - The handler's import-time seam check did not fail pod start (logs).
   - `_prisma_migrations` shows the 18 new rows finished (litellm-local).
4. **Functional checks, run against EACH new pod (port-forward per pod) and once through
   `https://api.chifor.me`:**
   - Main gateway, `gpt-5.6-sol`, non-streaming, in the real consumer's shape (strict
     `response_format: json_schema` + reasoning params). Checks: valid JSON content, a sane
     `finish_reason`, latency within 2× the baseline, and usage present with
     `total = prompt + completion`. Provenance is proven by the contract; this proves the deployed
     route works.
   - Main gateway, `gpt-5.6-sol`, streaming with `stream_options.include_usage`. Checks: content, a
     terminal finish reason, a usage chunk (estimated by design), `[DONE]`, and no in-band error after
     HTTP 200.
   - Main gateway, one non-chatgpt route, plus one Responses API call (`/v1/responses`).
   - `litellm-local`: one local-model completion. Also `GET /key/info` for the existing virtual key
     (master key, read-only), which proves the DB-backed key store reads after the migrations.
5. **Rollback triggers (act immediately):**
   - any functional check failing reproducibly
   - a crash-loop or a rollout past its deadline
   - broken SSE or an empty/malformed 200
   - an auth/budget 4xx on a call that passed in the baseline
   - a latency regression over 2× the baseline
   A single upstream-provider 5xx that doesn't reproduce is not a trigger. Re-run the check.
6. **Rollback procedure (prepared before merging, owner = this operator session):**
   - **Normal path:** `git revert <squash-sha>` as a PR, then squash-merge it via the API. That
     restores all three image refs, the handler bytes and the checksums together. Then
     `flux reconcile kustomization apps --with-source` and re-run step 4.
   - **Emergency path, if Gitea/Flux is unavailable:**
     1. `flux suspend kustomization apps`.
     2. `kustomize build kubernetes/apps/apps/ai` at the recorded pre-merge sha, filtered to the
        LiteLLM ConfigMaps + Deployments, then `kubectl apply`. That restores config, handler and
        image together; ReplicaSet `rollout undo` alone would pair old pods with the in-place-updated
        ConfigMaps.
     3. Once Git is reachable, land the revert and `flux resume`.
   - The DB needs no action (additive migrations; the dump is only for disaster).
7. **Observation:** start the 15-minute window after both rollouts complete. Run step 4 again at its
   end, and check restarts, memory and Flux status. Re-check once more after the 07Z load start
   (restarts, logs, a smoke call). Only then is #981 eligible.

### 2. #981: keep it open, let Renovate regenerate it, and review the patch delta only

1. After #973 is on main, **fix the version drift at the source.** In a small PR to main, make the
   `litellm-local.yaml` pin comment version-agnostic ("SAME pin as litellm.yaml; bump together").
   That way a Renovate bump can never stale it, and nobody commits on the Renovate branch, whose
   maintenance a human commit would stop. This answers 51202.
2. Renovate's CronJob runs every 4 h (concurrency Forbid). After the next successful run, check that
   #981 is rebased. If not, tick its rebase checkbox, and investigate a conflict or manual-commit
   detection if it stays stuck.
3. On the regenerated head, verify:
   - It contains #973's handler + tests, still targets v1.103.1, and all three image refs agree
     (tag@digest).
   - All CI is green, including the contract and the startup smoke, and there are fresh approvals
     from both bots.
4. Answer 51203 by linking section 1's breaking-change + migration review (v1.101 → v1.103.0), plus a
   fresh review of the v1.103.0 → v1.103.1 changelog and migration delta, done the same way.
5. Merge in a later quiet window, after #973's observation, with step 1's full procedure. Its rollback
   reverts ITS squash commit to the verified #973/v1.103.0 baseline, which keeps the handler fix.

### 3. #971: hold. Post the evidence and the exact merge preconditions

- Reason: enabling a public endpoint that no intended caller uses adds exposure (including the
  replay window) for no benefit. The PR itself offers "hold it and merge alongside the Admin change".
  Rebasing now is wasted work, because the router release rolls daily.
- Comment on #971 with today's evidence and these preconditions, ALL required:
  1. **Admin side deployed and verified.** The caller is tolerant of, or disabled against, the router
     endpoint until the router side is verified. The key fingerprint freshly matched
     (public ConfigMap ↔ Admin's private key).
  2. **Rebase and validate.** Rebase onto the current `router.yaml` keeping the live release image.
     Re-run manifest CI and bot review on the resolved head, and bind the artifact evidence to that
     exact release.
  3. **Test the selected artifact's authorization:**
     - the plugin is present and registered
     - unsigned, tampered, wrong-key/alg, wrong iss/aud, expired/future, missing-claim and
       body/path/action-mismatch assertions are all rejected
     - operator-role restrictions hold
     - Admin derives the role from server-side authz
  4. **Exposure review:** the ingress path, direct-origin restriction, size/rate limits, audit logs
     without assertions, and how key rotation/revocation reaches the verifier.
  5. **The owner's explicit go.** It is required regardless of any nonce store. It must either
     accept the replay window, with the duplicable actions listed, or require atomic replay
     rejection first.
  6. **A Recreate maintenance window.** Preflight the ConfigMap/mount. After the roll, a real
     Admin-signed request works and existing API-key traffic still works. Rollback = remove the env
     + mount while keeping the release.
- Keep `no-automerge`. Owner: the #971 author and the platform owner. Next check: when trueswarm-admin
  ships the bridge caller.

### 4. #835: no change. It stays parked behind #972

- #972's gate is the whole list in that issue, not just golden-v2:
  - storage cleanup
  - source-volume retention
  - registry-pull recovery (#865/#879)
  - a measured lease/release/refill cycle
  - demand evidence
  - the ai-node3 G3b memory headroom
  - `.39` and vmid 4402 re-checked at execution
- An infra-only split is possible but has no demonstrated need.
- Owner: the #972 assignee. Next check: when #972 closes.
<!-- codex: The older `2026-09-20-env-pool-root-cause-followup-plan.md` still contains executable `.38` instructions and monitoring references, which must be superseded before resuming provisioning. -->
<!-- opus-pushback: `plans/` are dated historical records that CLAUDE.md says not to rewrite, and #835's own branch (env-pool variables.tf, runbook, dashboard selector) plus main's IPAM now say `.39`. Execution follows #835's branch, not the 09-20 plan. The right guard is a note on #972/#835 that the 09-20 plan's `.38` is superseded, not an edit to the old plan. -->

### 5. #975 NAS deploy: ESCALATED. It needs the operator's explicit go, in its own window

The merged PR requires "operator go only"; merge and review do not grant it. When the go is given,
use this procedure (never in the same window as a gateway change):
1. **Workspace.** In Git Bash, `cd` into the ops checkout `C:\Users\chifo\work\home\ailab`. Confirm
   the branch is `main` tracking `gitea/main`, then `git pull --ff-only`. Stop on any checkout
   conflict. Confirm HEAD contains #975, and that `.env` + Python/Paramiko resolve without printing
   credentials.
2. **Pin the NAS host key first.** `scripts/qnap-ssh.py` uses Paramiko `AutoAddPolicy`, so compare
   the NAS host key fingerprint against a trusted record (QNAP UI / console) before sending
   credentials, and fix the helper to load known hosts. That fix is its own PR.
3. **Capture state for recovery:**
   - the deployed watchdog script (and its md5)
   - `/etc/config/crontab` and the loaded root crontab
   - the current status file and any lock / maintenance state
   Check that no other installer or cron editor is running, and that the USB is mounted and healthy.
4. **`DRY_RUN=1 bash scripts/qnap-versitygw-install.sh`** and review EVERY effect:
   - the script copy
   - the cron reconcile, which removes every line containing `versitygw`, so list and preserve any
     unrelated ones
   - the on-USB watchdog retirement
   - old-log preservation/truncation
   - that apply runs the supervisor once, which may restart an unresponsive gateway
5. **Apply**, then require:
   - a status line with a post-install timestamp (`healthy`, or `restarted` followed by a fresh
     `healthy` on the next scheduled run)
   - the lock state inspected, never deleted while live
   - both crontabs correct, with unrelated lines intact, and the scheduler running
   - the deployed md5 == the repo's
   Then a successful scheduled `versitygw-probe` PUT/GET/verify/DELETE cycle (the CronJob's last
   success time updates), and the loaded alert rules plus source metrics evaluated. Do not start an
   ad-hoc overlapping probe.
6. **Residual:** the running gateway's stdout stays on USB until its next restart (as documented).
   Note it, and verify at a separately justified restart.
7. **Recovery:** a forward fix that keeps the mount guard. As a last resort, restore the captured
   script and crontab. Never re-install the pre-#975 script, which has the tmpfs bug and no mount
   guard. `.env` has to be provided explicitly to any other worktree. Truncated logs and gateway
   restarts can't be undone.

## Critical files

- `kubernetes/apps/apps/ai/litellm-local.yaml`: the version-agnostic pin comment (step 2.1, a small PR to main).
- #971, #835: comments only. No branch edits.
- `scripts/qnap-ssh.py`: host-key pinning (step 5.2, its own PR, only when the NAS deploy is approved).
- `scripts/qnap-versitygw-install.sh` / `scripts/qnap-versitygw-watchdog.sh`: deployed, not edited (step 5).

## Verification

- **1:** the `apps` Kustomization is applied at the squash sha and Ready. Both Deployments reach
  updated == available == desired on v1.103.0, with the endpoints on new pods only. The 18 migrations
  are applied. Each pod passes:
  - non-streaming usage present and additive
  - streaming delivery with an (estimated) usage chunk and `[DONE]`
  - a non-chatgpt route and `/v1/responses`
  - a local model and `/key/info`
  After the 15-min observation, the checks are re-run with no restarts. They are re-checked after 07Z.
- **2:** the version-agnostic comment is on main. The regenerated #981 head contains #973's handler,
  its three refs agree, all CI is green, fresh approvals are in, and 51202/51203 are answered. It is
  merged in its own window with step 1's procedure.
- **3:** the #971 comment is posted with the six preconditions, owner and next check. The label is unchanged.
- **4:** a note is posted on #835/#972 that the 09-20 plan's `.38` is superseded, with owner and next check.
- **5:** not executed without the operator's go. When executed: the step 5.5 acceptance, all of it.

<!-- codex-review-status: complete -->
