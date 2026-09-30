# Resolve the four open ailab PRs (#973, #981, #971, #835) and the #975 NAS deploy

## Context

This follows `plans/2026-09-29-held-prs-plan.md` (branch `plan/held-prs-2026-09-29`, codex-reviewed).
That plan's track A became #973, track B became #971, and track C became issue #972. Earlier today the
PR queue was triaged: #975, #977, #978 and #982 were merged after their review findings were fixed,
and their rollouts were verified (all four OpenBao provision Jobs succeeded on 2.6.3, cri-log-relay is
Ready on talosctl v1.11.6, and the pg-sync bootstrap Job succeeded on postgres 16.15). Four PRs
remain. Each is held with `no-automerge`, which the reviewbot honours, so none of them merges without
a human decision. State as of 2026-09-30 19:20Z:

**#973: `fix(litellm)`: read chatgpt-chat usage off the wire; LiteLLM v1.101.0 → v1.103.0.**
- Head `603a4257`, mergeable, CI green on all 7 checks, and both reviewer bots APPROVED this head.
- The last finding (50752, an unguarded usage mapping) is fixed. The gap was wider than reported:
  v1.103.0's own parser raises `APIConnectionError` during the drain on a malformed `*_tokens_details`,
  before the post-drain mapping. The handler now converts both paths into its own 502. New contract
  cases: `e-outdetailsusage`, `e-indetailsusage`, `e-usage-mapping-guard`. The contract passes in the
  image on v1.103.0 (31 cases) and on v1.101.0. The proxy startup smoke is OK.
- Rollout blast radius:
  - `litellm`: 2 replicas, `RollingUpdate maxUnavailable 0 / maxSurge 1`.
  - `litellm-local`: 1 replica, 25%/25%, which rounds to surge 1 / unavailable 0.
  - Both roll with zero downtime. The image, `checksum/config` and `checksum/chatgpt-chat` all change.
- Load, measured as LiteLLM container CPU over the last 7 days:
  - busiest 07–12Z and 18–19Z, but tiny even then (max 0.045 cores)
  - quiet from 20Z to 06Z
  - no `litellm_*` Prometheus metrics exist, and `LiteLLM_SpendLogs` holds 2 rows for the week, so
    CPU is the only load signal.
- The PR description's post-merge checks:
  - both Deployments reach Ready
  - a smoke completion through `api.chifor.me`, including one non-streaming `gpt-5.6-sol` call, returns usage

**#981: Renovate, LiteLLM v1.101.0 → v1.103.1 (`no-automerge` by the renovate.json LiteLLM rule).**
- `litellm-route-contract` fails, correctly. v1.103.x with the OLD handler reproduces the estimated-usage
  regression #973 fixes.
- reviewer-claude findings:
  - 51202: the stale `v1.101.0` comment in `litellm-local.yaml`
  - 51203: the v1.102/1.103 breaking changes, namely the budget re-check on router fallbacks, and
    config.yaml winning over DB-stored settings
- A comment on the PR already explains the dependency on #973.

**#971: `feat(llm-router)`: enable the admin-bridge plugin (held for the owner's explicit go).**
- It adds a verifier public-key ConfigMap, env `ADMIN_BRIDGE_VERIFY_KEY` and a read-only mount. That
  turns on an externally reachable, signed-assertion endpoint (`POST /register/admin-bridge`) on the
  Recreate singleton.
- It is still unwired on the Admin side, verified live now: the `trueswarm-admin/admin` Deployment
  mounts no bridge key, has no ROUTER/BRIDGE env, and `admin-config` has no router key. No
  trueswarm-admin PR mentions the bridge. So nothing would call the endpoint.
- Residual risk the PR itself names: no `jti`/nonce store, so a signed request can be replayed within its ≤60 s window.
- It conflicts in `router.yaml`: main rolled the router release six times since the branch point
  (#974 … `router-0.1.0-20260930-sysmerge`). Its pre-merge proof that the plugin is in the artifact
  and registered was taken on `router-0.1.0-20260929-subs`, which is no longer live.

**#835: env-node-2 (parked).**
- Gated on issue #972 (env-pool restore: golden-v2 + resume), which is still open. golden-v2 has
  never been built, and `SandboxWarmPool env-std-pool` is at `replicas: 0` (#880).
- Its IP moved today from `.38` (now cloud-win-1, live) to `.39`, which main reserves (#991).
- It still conflicts in `testpool/sandboxtemplate-std.yaml`: `replicas: 2` here, `0` on main.

**#975 follow-up: the versitygw supervisor on the QNAP NAS is not Flux-managed.**
- #975 merged. Only its alert-rule change shipped, through Flux.
- The watchdog on the NAS is still the OLD script. That version probes and `mkdir -p`s on tmpfs when
  the USB is unmounted, which is the incident #975 fixes.
- The PR says: "Deploy (after merge, operator go only — not done by this PR)". Run from the ops
  checkout `C:\Users\chifo\work\home\ailab`, which is behind `gitea/main` and has `.env` with the QNAP creds:
  - `DRY_RUN=1 bash scripts/qnap-versitygw-install.sh`
  - then `bash scripts/qnap-versitygw-install.sh`
  - expected verify output: `healthy  http=403, disk=ok`
- The installer now refuses unless the USB is mounted.

## Approach

### 1. #973: merge in tonight's quiet window, then verify and keep a rollback ready

1. From 20:00Z, re-check that the head is still `603a4257`, CI is green, both approvals are on that
   head, and it is mergeable. Record the pre-merge image (`v1.101.0@sha256:d295634e…`) and ReplicaSet
   revisions as the rollback reference.
2. Squash-merge via the API. The `no-automerge` label only stops the bot, and this is the human decision.
3. Wait for Flux, then verify:
   - Both Deployments `rolloutStatus` complete, every pod on `v1.103.0@sha256:bd089afd…`, 0 restarts,
     and the handler's import-time seam check did not fail pod start.
   - A non-streaming `gpt-5.6-sol` completion through `https://api.chifor.me/v1/chat/completions`
     returns non-zero usage whose `total_tokens` equals prompt + completion.
   - A streaming completion ends with a usage chunk.
   - One call to a non-chatgpt route (a local model through `litellm-local`) still answers.
4. Rollback trigger: any smoke failure, a crash-looping pod, or 5xx in the logs within 15 minutes.
   Rollback means a `git revert` PR of the merge commit (image + handler return together), merged
   immediately. No `kubectl rollout undo`, because Flux would re-apply main.

### 2. #981: let Renovate rebase it onto the new baseline, then treat it as a normal patch bump

1. After #973 is on main, Renovate rebases #981 to v1.103.0 → v1.103.1. Do not hand-edit before
   then, because that would stop Renovate from maintaining the branch. If Renovate has not rebased it
   by the next run, ask for a rebase through the PR's rebase checkbox.
2. When it is rebased:
   - The contract must be green: it runs with #973's handler on v1.103.1.
   - Answer 51202 by fixing the stale comment. By then it is a `v1.103.0` comment that becomes `v1.103.1`.
   - Answer 51203 with the v1.103.0 → v1.103.1 changelog delta only. The v1.102/1.103 breaking
     changes are already live via #973. Evidence that config.yaml-over-DB precedence doesn't matter
     here: the proxy is configured by the mounted config.yaml, and no DB-managed settings are in use
     (check `LiteLLM_Config` rows).
3. Merge in a later quiet window with the same smoke checks as step 1. Not tonight: one gateway change per window.

### 3. #971: do not merge now; keep it held until the Admin side ships

- Reason: opening an externally reachable endpoint that nothing calls adds attack surface, including
  the 60 s replay window, with no benefit. The PR already offers "hold it and merge alongside the
  Admin change". Rebasing now is also wasted work: the router release rolls daily, so a rebase would
  be stale by the time Admin is ready.
- Action: comment on #971 with today's evidence (the Admin side is still unwired, the conflict, the
  stale artifact proof). List the exact merge preconditions:
  1. the trueswarm-admin change mounting `admin-router-bridge` and configuring the router URL is merged
  2. rebase onto current `router.yaml`, keeping the live release image
  3. re-verify on the then-live artifact that `packages/plugins/admin-bridge` is present and registered
  4. the owner's go on the replay-window risk, or a nonce store added in the router first
- Keep `no-automerge`.

### 4. #835: no change. It stays parked behind #972 (gate unmet)

- The IP fix and the `.39` reservation are already done. Nothing else is actionable until golden-v2
  exists and the pool is back at 1.

### 5. #975: deploy the supervisor to the NAS now (dry run first)

- Reason: the NAS still runs the version with the tmpfs-litter bug. The fix is merged, tested
  (91/91, promtool OK) and reviewed by both bots. The installer is idempotent: it copies and
  md5-verifies the script, reconciles one cron line, and runs it once. It also now refuses when the
  USB isn't mounted.
1. `git -C C:/Users/chifo/work/home/ailab pull --ff-only gitea main`. It has only untracked files,
   which do not block a fast-forward.
2. `DRY_RUN=1 bash scripts/qnap-versitygw-install.sh`: read the preview, which should show only the
   script copy + cron reconcile.
3. Apply, and expect `healthy  http=403, disk=ok`. Then confirm the cron line and that
   `VersitygwProbeFailed` / `VersitygwProbeStale` are not firing.
4. Rollback: re-run the installer from the previous main commit (`git worktree` at the pre-#975 sha).

## Critical files

- None on this branch beyond this plan. Actions happen on PR branches, via the Gitea API, and on the NAS.
- #981 branch `renovate/ghcr.io-berriai-litellm-1.x`: `kubernetes/apps/apps/ai/litellm-local.yaml` comment (step 2).
- `scripts/qnap-versitygw-install.sh` / `scripts/qnap-versitygw-watchdog.sh`: deployed, not edited (step 5).

## Verification

- **1:** both LiteLLM Deployments are Ready on v1.103.0 with 0 restarts, non-streaming and streaming
  chatgpt-chat smoke calls return real usage, a local-model call answers, and there are no 5xx in
  15 min of logs.
- **2:** #981's contract is green after Renovate's rebase, both findings are answered, and it merges
  in its own window with the same smoke checks.
- **3:** the #971 comment is posted, and the label and hold are unchanged.
- **4:** no action.
- **5:** the installer verify prints `healthy  http=403, disk=ok`, the NAS cron line points at the new
  script (md5 matches the repo), and no versitygw alert is firing.

<!-- codex-review-status: pending -->
