# Resolve the four open ailab PRs (#973, #981, #971, #835) and the #975 NAS deploy

## Codex Review

- The dependency ordering is sound: land #973 before reconsidering #981, and retain the #971 and #835 holds.
- #973’s contracts support the non-streaming usage fix, but streaming usage remains estimated; the verification section incorrectly promises real usage for both.
- Gateway readiness and low CPU do not establish safe rollout or rollback. Review breaking changes and database migrations before #973, and add bounded rollout, representative request, and recovery checks.
- The NAS installer has useful mount and checksum guards, but changes more than advertised, accepts unverified SSH host keys, and can finish successfully without a fresh healthy result. Its rollback needs correction.
- Preserve explicit operator approval for NAS deployment and bridge enablement, separate the NAS work from gateway recovery, and assign owners and follow-up times to deferred work.

## Context

This follows `plans/2026-09-29-held-prs-plan.md` (branch `plan/held-prs-2026-09-29`, codex-reviewed).
That plan's track A became #973, track B became #971, and track C became issue #972. Earlier today the
PR queue was triaged: #975, #977, #978 and #982 were merged after their review findings were fixed,
and their rollouts were verified (all four OpenBao provision Jobs succeeded on 2.6.3, cri-log-relay is
Ready on talosctl v1.11.6, and the pg-sync bootstrap Job succeeded on postgres 16.15). Four PRs
remain. Each is held with `no-automerge`, which the reviewbot honours, so none of them merges without
a human decision. State as of 2026-09-30 19:20Z:
<!-- codex: This review checked repository code and locally available PR refs; it did not independently refresh live cluster, CI, approval, or external-repository state. Treat the timestamped observations as a snapshot and refresh the relevant evidence at each execution gate. -->

**#973: `fix(litellm)`: read chatgpt-chat usage off the wire; LiteLLM v1.101.0 → v1.103.0.**
- Head `603a4257`, mergeable, CI green on all 7 checks, and both reviewer bots APPROVED this head.
- The last finding (50752, an unguarded usage mapping) is fixed. The gap was wider than reported:
  v1.103.0's own parser raises `APIConnectionError` during the drain on a malformed `*_tokens_details`,
  before the post-drain mapping. The handler now converts both paths into its own 502. New contract
  cases: `e-outdetailsusage`, `e-indetailsusage`, `e-usage-mapping-guard`. The contract passes in the
  image on v1.103.0 (31 cases) and on v1.101.0. The proxy startup smoke is OK.
  <!-- codex: These are substantive tests of the non-streaming fix: they exercise real parsing, exact fixture counts, specific refusals, and transport closure; the direct mapping test covers the path the stricter parser prevents reaching. The mocked upstream and network-disabled startup smoke do not establish production authentication, database compatibility, ingress behavior, or whole-gateway upgrade safety. -->
- Rollout blast radius:
  - `litellm`: 2 replicas, `RollingUpdate maxUnavailable 0 / maxSurge 1`.
  - `litellm-local`: 1 replica, 25%/25%, which rounds to surge 1 / unavailable 0.
  - Both roll with zero downtime. The image, `checksum/config` and `checksum/chatgpt-chat` all change.
  <!-- codex: The rounding is correct, but zero downtime is not guaranteed: both readiness probes check only `/health/liveliness`, so a request-broken pod can become Ready and replace working replicas, including the sole old local pod. Check functional readiness and capacity for both simultaneous surge pods, including memory and database connections; preferred anti-affinity does not guarantee placement on separate nodes. -->
  <!-- codex: Neither Deployment specifies a drain hook or an extended termination grace period, while the main gateway permits 900-second requests. Existing streams can outlive Kubernetes' default 30-second grace period, so verify the image's shutdown behavior and arrange draining or explicitly accept interrupted requests. -->
- Load, measured as LiteLLM container CPU over the last 7 days:
  - busiest 07–12Z and 18–19Z, but tiny even then (max 0.045 cores)
  - quiet from 20Z to 06Z
  - no `litellm_*` Prometheus metrics exist, and `LiteLLM_SpendLogs` holds 2 rows for the week, so
    CPU is the only load signal.
  <!-- codex: CPU is weak evidence for an I/O-bound proxy: long upstream calls and open streams may consume little CPU, and sparse spend logs do not establish low traffic. Supplement the window decision with available ingress/client request evidence, active work, and memory pressure; 20:00Z is 23:00 in Bucharest, so confirm operator coverage through verification and recovery. -->
- The PR description's post-merge checks:
  - both Deployments reach Ready
  - a smoke completion through `api.chifor.me`, including one non-streaming `gpt-5.6-sol` call, returns usage

**#981: Renovate, LiteLLM v1.101.0 → v1.103.1 (`no-automerge` by the renovate.json LiteLLM rule).**
- `litellm-route-contract` fails, correctly. v1.103.x with the OLD handler reproduces the estimated-usage
  regression #973 fixes.
  <!-- codex: This failure establishes incompatibility with the old handler, not compatibility between v1.103.1 and #973's handler. Keeping the PR for regeneration is reasonable; compatibility requires a fresh contract and startup run on its rebased contents and exact target digest. -->
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
  <!-- codex: This supports holding enablement because no intended caller is ready; it does not mean the public endpoint would receive no requests. Absence of a matching PR is also weaker evidence than runtime wiring, so identify the Admin owner and an explicit readiness signal. -->
- Residual risk the PR itself names: no `jti`/nonce store, so a signed request can be replayed within its ≤60 s window.
  <!-- codex: The previous plan establishes that the plugin code already ships in router artifacts; the unmerged change enables it rather than introducing the code. Leaving it disabled is reasonable provided the current artifact rejects bridge requests without the verifier configuration; replay acceptance needs assessment against the actual privileged actions and duplicate side effects. -->
- It conflicts in `router.yaml`: main rolled the router release six times since the branch point
  (#974 … `router-0.1.0-20260930-sysmerge`). Its pre-merge proof that the plugin is in the artifact
  and registered was taken on `router-0.1.0-20260929-subs`, which is no longer live.

**#835: env-node-2 (parked).**
- Gated on issue #972 (env-pool restore: golden-v2 + resume), which is still open. golden-v2 has
  never been built, and `SandboxWarmPool env-std-pool` is at `replicas: 0` (#880).
- Its IP moved today from `.38` (now cloud-win-1, live) to `.39`, which main reserves (#991).
  <!-- codex: The allocation is recorded in main's `docs/network-plan.md`, and the PR's Terraform default uses `.39`; this is a reservation for an unbuilt VM, not a completed live IP migration. The older `2026-09-20-env-pool-root-cause-followup-plan.md` still contains executable `.38` instructions and monitoring references, which must be superseded before resuming provisioning. -->
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
  <!-- codex: The initial mount-table check prevents the known already-unmounted case, but does not establish disk health or prevent a later disconnect. The installer itself performs synchronous directory checks, `df`, and log reads on the USB, even during dry-run, so run only against healthy storage and inspect remote process state before retrying after an SSH timeout. -->

## Approach

<!-- codex: The dependency ordering is sensible, but #975 is not operationally isolated: the recorded USB incident also took Gitea down, potentially removing the merge/Flux recovery path needed for #973. Complete NAS verification before starting the gateway window, or defer NAS work until gateway observation is finished; retain enough operator time for either recovery. -->

### 1. #973: merge in tonight's quiet window, then verify and keep a rollback ready

1. From 20:00Z, re-check that the head is still `603a4257`, CI is green, both approvals are on that
   head, and it is mergeable. Record the pre-merge image (`v1.101.0@sha256:d295634e…`) and ReplicaSet
   revisions as the rollback reference.
   <!-- codex: Before authorizing this bump, resolve the v1.102/v1.103 breaking-change review described under #981; waiting until that PR would review the changes after exposure. Also record the full image digests, current configuration/handler hashes, and baseline request results so recovery has an unambiguous target. -->
   <!-- codex: `litellm-local.yaml` enables database schema migrations at startup, and the old and new local replicas share that database during rolling deployment. Review the migration delta, establish old/new schema compatibility, and verify a recoverable backup of the LiteLLM database before merging; handler compatibility with v1.101.0 does not prove database downgrade safety. -->
2. Squash-merge via the API. The `no-automerge` label only stops the bot, and this is the human decision.
   <!-- codex: Make the API merge conditional on the reviewed head SHA and capture the resulting squash commit SHA. A changed head or intervening relevant main change requires renewed checks rather than relying on the earlier mergeability snapshot. -->
3. Wait for Flux, then verify:
   <!-- codex: Specify a bounded rollout deadline and monitor from the start, including Pending/image-pull/init failures, readiness flapping, and `ProgressDeadlineExceeded`; Kubernetes does not automatically roll back a stalled Deployment. The shared `apps` Kustomization has `wait: true` and a five-minute timeout, so verify its applied revision and Ready condition as well as both Deployments. -->
   - Both Deployments `rolloutStatus` complete, every pod on `v1.103.0@sha256:bd089afd…`, 0 restarts,
     and the handler's import-time seam check did not fail pod start.
     <!-- codex: Check desired/updated/available replica counts, endpoint membership, and the expected mounted configuration and handler on each new main-gateway replica. A single load-balanced smoke request can miss a faulty replica, and zero current restarts can hide an already-replaced failed pod, so retain rollout events and terminated-pod evidence. -->
   - A non-streaming `gpt-5.6-sol` completion through `https://api.chifor.me/v1/chat/completions`
     returns non-zero usage whose `total_tokens` equals prompt + completion.
     <!-- codex: Use the real consumer's request shape, including strict JSON schema and reasoning parameters, and validate usable content, finish reason, and bounded latency. Non-zero arithmetic alone also passes fabricated estimates; provenance is established by the fixture contract, while this smoke establishes that the deployed route still works. -->
   - A streaming completion ends with a usage chunk.
     <!-- codex: Send `stream_options: {"include_usage": true}` and validate content, terminal finish reason, usage, and the final SSE `[DONE]`, including client-visible stream errors after HTTP 200. The contract explicitly documents estimated streaming usage, so this check must not claim upstream-exact counts. -->
   - One call to a non-chatgpt route (a local model through `litellm-local`) still answers.
     <!-- codex: Exercise the main gateway's non-chatgpt routing too, since the local gateway has different routing settings; include the existing Responses API route because the handler changes a shared provider transformation. For `litellm-local`, use an existing restricted virtual key and verify model restrictions and spend accounting, with budget/fallback rejection covered by controlled tests rather than exhausting production quotas. -->
4. Rollback trigger: any smoke failure, a crash-looping pod, or 5xx in the logs within 15 minutes.
   <!-- codex: Define immediate rollback for reproducible upgrade failures and a baseline-relative threshold for incidental upstream errors; a single unrelated provider 5xx is not necessarily an upgrade regression. Include timeouts, increased latency, auth/budget 4xx, empty or malformed HTTP-200 responses, and broken SSE streams, which a 5xx-only log scan misses. -->
   Rollback means a `git revert` PR of the merge commit (image + handler return together), merged
   immediately. No `kubectl rollout undo`, because Flux would re-apply main.
   <!-- codex: Reverting the resulting squash commit is a real GitOps rollback, but “merged immediately” assumes available Gitea, CI/review access, conflict-free main, and successful Flux reconciliation. Prepare the exact revert procedure and recovery owner, restore all three image refs plus configuration/handler checksums, explicitly reconcile the revert, and repeat the functional checks; image rollback does not undo database migrations. -->
   <!-- codex: ReplicaSet revisions alone are insufficient because the ConfigMaps are updated in place, so an old ReplicaSet restart can read new configuration or handler bytes. Keep a documented emergency recovery path for unavailable Git/Flux, with narrowly scoped reconciliation control and subsequent Git convergence, rather than improvising `rollout undo`. -->

### 2. #981: let Renovate rebase it onto the new baseline, then treat it as a normal patch bump

1. After #973 is on main, Renovate rebases #981 to v1.103.0 → v1.103.1. Do not hand-edit before
   then, because that would stop Renovate from maintaining the branch. If Renovate has not rebased it
   by the next run, ask for a rebase through the PR's rebase checkbox.
   <!-- codex: Retaining the PR preserves useful review context, whereas closing it can cause Renovate to suppress that version proposal. Rebase timing is not guaranteed: the CronJob runs every four hours with concurrency forbidden, so inspect the next successful run after #973, request regeneration if necessary, and investigate conflicts or manual-commit detection if it remains stuck. -->
2. When it is rebased:
   - The contract must be green: it runs with #973's handler on v1.103.1.
     <!-- codex: Verify that the branch actually contains #973's handler and tests, still targets v1.103.1, and updates all three image references consistently; Renovate may retarget a later release while the PR waits. Require all relevant CI, startup smoke, and fresh approvals on the final head, not merely the formerly failing contract check. -->
   - Answer 51202 by fixing the stale comment. By then it is a `v1.103.0` comment that becomes `v1.103.1`.
     <!-- codex: A human commit after rebase can still stop subsequent Renovate maintenance, contradicting the earlier rationale for avoiding edits. Choose an explicit ownership approach, such as fixing version-specific commentary on main and requesting regeneration, or accepting manual maintenance of the final branch; inspect embedded digest comments too and regenerate checksums if ConfigMap contents change. -->
   - Answer 51203 with the v1.103.0 → v1.103.1 changelog delta only. The v1.102/1.103 breaking
     changes are already live via #973. Evidence that config.yaml-over-DB precedence doesn't matter
     here: the proxy is configured by the mounted config.yaml, and no DB-managed settings are in use
     (check `LiteLLM_Config` rows).
     <!-- codex: Limit this review to the patch delta only after linking the completed pre-#973 assessment of the earlier breaking changes; “already live” is not evidence of compatibility. A mounted config and `STORE_MODEL_IN_DB=False` do not establish that DB-managed settings are absent, so inspect the relevant `LiteLLM_Config` entries and effective settings without exposing secrets, and separately assess existing budgets and fallback behavior. -->
3. Merge in a later quiet window with the same smoke checks as step 1. Not tonight: one gateway change per window.
   <!-- codex: The later window is sensible if #973 has passed observation, including representative traffic, rather than merely surviving until the next date. Define #981's rollback as reverting its own squash commit to the verified #973/v1.103.0 baseline, with a fresh migration review, so recovery does not inadvertently remove the handler fix. -->

### 3. #971: do not merge now; keep it held until the Admin side ships

- Reason: opening an externally reachable endpoint that nothing calls adds attack surface, including
  the 60 s replay window, with no benefit. The PR already offers "hold it and merge alongside the
  Admin change". Rebasing now is also wasted work: the router release rolls daily, so a rebase would
  be stale by the time Admin is ready.
- Action: comment on #971 with today's evidence (the Admin side is still unwired, the conflict, the
  stale artifact proof). List the exact merge preconditions:
  1. the trueswarm-admin change mounting `admin-router-bridge` and configuring the router URL is merged
     <!-- codex: “Merged” is insufficient: require deployed, verified Admin wiring and a freshly matched public/private key fingerprint. Ship the Admin caller disabled or tolerant of the unavailable endpoint, then enable it after router verification, avoiding an Admin-first outage caused by this ordering. -->
  2. rebase onto current `router.yaml`, keeping the live release image
     <!-- codex: Re-run manifest validation and review on the resolved head, preserving current release selection and security settings, then bind artifact evidence to that exact release through merge. A daily release cadence justifies checking near execution, but does not replace checking whether relevant content changed. -->
  3. re-verify on the then-live artifact that `packages/plugins/admin-bridge` is present and registered
     <!-- codex: Presence and registration do not verify authorization: test unsigned, tampered, wrong-key/algorithm, wrong issuer/audience, expired/future-dated, missing-claim, and mismatched body/path/action assertions against the selected artifact. Verify administrator/operator action restrictions and that Admin derives signing authority from authenticated server-side permissions rather than caller-supplied roles. -->
     <!-- codex: Check the public ingress path and direct-origin restrictions, request-size/rate limits, and audit logging that excludes assertions and secrets; a signed endpoint remains exposed to unauthenticated resource-exhaustion attempts. Record how key rotation/revocation reaches a running verifier and invalidates the old signing key. -->
  4. the owner's go on the replay-window risk, or a nonce store added in the router first
     <!-- codex: The owner's explicit go remains required for enabling this public endpoint even if a nonce store is added; the current “or” silently drops that existing gate. If replay is accepted, document which actions can be duplicated and why their impact is acceptable; otherwise require atomic replay rejection with retention and restart behavior defined for the assertion lifetime. -->
  <!-- codex: Add a coordinated Recreate maintenance window, preflight the ConfigMap/key mount, and verify a real Admin-signed request plus existing router API-key traffic after rollout. Predefine rollback by removing bridge enablement while preserving the current release, since a bad key or startup failure takes down the singleton and unrelated router functionality. -->
- Keep `no-automerge`.

### 4. #835: no change. It stays parked behind #972 (gate unmet)

- The IP fix and the `.39` reservation are already done. Nothing else is actionable until golden-v2
  exists and the pool is back at 1.
  <!-- codex: Holding the combined PR is correct, but golden-v2 plus one replica is only part of the recorded gate: retain storage cleanup, source-volume retention, registry-pull recovery, a measured lease/release/refill cycle, demand evidence, and the ai-node3 G3b memory-headroom requirement. Preserve the staged infrastructure-before-pool-expansion ordering and recheck the reserved address and VM ID at execution. -->
  <!-- codex: An infrastructure-only node-preparation split could technically proceed without golden-v2 if independently justified and capacity-safe, while keeping the pool paused; it would not unblock the broken storage/image path. With no demonstrated need for that extra work, parking this PR is the proportionate decision rather than bypassing #972 or merely resolving the replicas conflict. -->

### 5. #975: deploy the supervisor to the NAS now (dry run first)

<!-- codex: Replace the unconditional “now” execution gate with the operator go explicitly required by the merged PR, unless that authorization is already recorded for this deployment. The completed code review and merge authorize neither an inferred NAS maintenance window nor an assumption that the operator is available. -->

- Reason: the NAS still runs the version with the tmpfs-litter bug. The fix is merged, tested
  (91/91, promtool OK) and reviewed by both bots. The installer is idempotent: it copies and
  md5-verifies the script, reconciles one cron line, and runs it once. It also now refuses when the
  USB isn't mounted.
  <!-- codex: The temporary-file checksum check followed by rename is useful protection against a corrupt copy, but the 91 tests chiefly cover watchdog behavior and installer input guards, not a real NAS install transaction. Idempotence does not provide transaction rollback or concurrency control, so save the deployed script and durable/live crontabs and ensure no competing installer or cron editor is running. -->
  <!-- codex: Cron reconciliation removes every line containing `versitygw`, not just a uniquely identified watchdog entry, and restarts the NAS-wide cron daemon while ignoring restart failure. Review all matched lines, preserve unrelated jobs, and verify both `/etc/config/crontab` and the loaded root crontab plus scheduler operation after apply. -->
  <!-- codex: `scripts/qnap-ssh.py` uses Paramiko `AutoAddPolicy` without loading trusted host keys, so even dry-run sends NAS credentials to an unauthenticated SSH host. Pin and verify the NAS host key through the deployment helper; the MD5 comparison checks transferred bytes but does not establish host authenticity. -->
1. `git -C C:/Users/chifo/work/home/ailab pull --ff-only gitea main`. It has only untracked files,
   which do not block a fast-forward.
   <!-- codex: Untracked files can block a fast-forward when incoming tracked paths would overwrite them, and pulling updates whichever branch is currently checked out. Verify branch/upstream and the resulting commit contains #975, preserve `.env`, and stop on checkout conflicts rather than assuming the working tree is deployable. -->
2. `DRY_RUN=1 bash scripts/qnap-versitygw-install.sh`: read the preview, which should show only the
   script copy + cron reconcile.
   <!-- codex: The preview also includes retirement of an on-USB watchdog and possible old-log preservation/truncation; apply invokes the supervisor, which may restart an unresponsive gateway. Review those effects explicitly, including the fact that failed tail preservation is ignored before truncation, rather than accepting a two-operation summary. -->
   <!-- codex: Specify the supported Bash environment and execute from the verified ops checkout: the inline environment assignment is not PowerShell syntax, and the preceding `git -C` command does not change the shell's working directory. Confirm that this environment resolves the intended Python/Paramiko installation and checkout-local `.env` without printing credentials. -->
3. Apply, and expect `healthy  http=403, disk=ok`. Then confirm the cron line and that
   `VersitygwProbeFailed` / `VersitygwProbeStale` are not firing.
   <!-- codex: HTTP 403 is the expected unauthenticated S3-root response, and the actual status file includes a timestamp and tab-separated fields. However, the watchdog treats any non-000 HTTP code as “answering” and disables TLS verification for this local probe, so even “healthy” does not prove authenticated S3 or certificate validity. -->
   <!-- codex: The installer catches watchdog failure and can exit successfully after printing an old status; an existing lock can also make the watchdog exit without writing a new one. Require a post-install status timestamp, inspect maintenance/lock state without deleting live locks, and observe a subsequent scheduled run; `restarted` can be a legitimate first result before a later fresh `healthy`. -->
   <!-- codex: Confirm a successful post-deploy scheduled `versitygw-probe` PUT/GET/content-verify/DELETE cycle and its updated CronJob success timestamp, then check the loaded alert rules and their source metrics after evaluation. Alert absence immediately after apply can reflect old success or pending timers, and ad-hoc overlapping probes would contend for the probe's fixed object path. -->
   <!-- codex: Installing the supervisor does not move an already-running gateway's stdout file descriptor off USB; the installer documents that this occurs on the next gateway restart. Record that residual risk and verify the destination at a separately justified restart, rather than claiming the installation completed log relocation. -->
4. Rollback: re-run the installer from the previous main commit (`git worktree` at the pre-#975 sha).
   <!-- codex: This reinstalls the known tmpfs bug and removes the installer's mount guard, while “previous main” need not match the script actually deployed before this operation. Pin a verified recovery artifact and restore the captured script/cron state if necessary, prefer a forward fix retaining the mount guard, and keep the already-deployed Flux alert correction. -->
   <!-- codex: A new worktree does not inherit the untracked `.env`, so this rollback command is incomplete even when the chosen revision is correct. Specify secure credential availability and post-recovery checks, and recognize that rerunning the installer cannot undo truncated logs, gateway restarts, or other completed side effects. -->

## Critical files

- None on this branch beyond this plan. Actions happen on PR branches, via the Gitea API, and on the NAS.
- #981 branch `renovate/ghcr.io-berriai-litellm-1.x`: `kubernetes/apps/apps/ai/litellm-local.yaml` comment (step 2).
- `scripts/qnap-versitygw-install.sh` / `scripts/qnap-versitygw-watchdog.sh`: deployed, not edited (step 5).

## Verification

- **1:** both LiteLLM Deployments are Ready on v1.103.0 with 0 restarts, non-streaming and streaming
  chatgpt-chat smoke calls return real usage, a local-model call answers, and there are no 5xx in
  15 min of logs.
  <!-- codex: Correct “non-streaming and streaming ... real usage”: at `603a4257`, the streaming contract explicitly records an estimated completion count of 9 against the upstream fixture's 17, and the wire-usage tap is used only by non-streaming completion. Exact upstream input/output counts are the non-streaming guarantee; streaming verification establishes successful delivery and the documented usage behavior. -->
  <!-- codex: Start the observation interval after both rollouts complete and exercise a known set of representative requests during it, covering each new replica and relevant client path. Check latency, stream completion, memory/restart trends, Flux status, and DB-backed key/accounting behavior, then assign follow-up through the next normal load period; fifteen quiet minutes with no 5xx is only an initial smoke result. -->
- **2:** #981's contract is green after Renovate's rebase, both findings are answered, and it merges
  in its own window with the same smoke checks.
- **3:** the #971 comment is posted, and the label and hold are unchanged.
- **4:** no action.
  <!-- codex: For both held PRs, record an owner and a next check tied to Admin readiness or #972 progress, preserving the labels and dependency links. This keeps “hold” and “parked” as tracked decisions without expanding tonight's scope into bridge implementation or pool restoration. -->
- **5:** the installer verify prints `healthy  http=403, disk=ok`, the NAS cron line points at the new
  script (md5 matches the repo), and no versitygw alert is firing.
  <!-- codex: Acceptance additionally requires the approved deployed revision, a fresh supervisor status, an observed cron execution, and a successful post-deploy authenticated S3 probe with current metrics. Confirm unrelated cron jobs remain intact and the corrected Flux alert rules remain loaded; neither installer exit zero nor a historical healthy line establishes this. -->

<!-- codex-review-status: complete -->