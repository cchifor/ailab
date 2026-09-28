# Cloud-power OFF becomes a scheduled, drained power-off

## Codex Review

- Disabling runners at the forge addresses the measured failure while preserving the existing guest-side shutdown backstop and API/WoL separation.
- The drain must also observe already-disabled runners; neither the configured job timeout nor Gitea’s `offline` status provides the shutdown guarantees claimed here.
- Persist intent before external mutations, retain failed cleanup work, and serialize HTTP requests with worker transitions.
- Add pagination, crash-boundary, concurrency, API-contract, access-control, and rollback verification.
- Keep the implementation small: reuse the ConfigMap transport, but defer forced shutdown and unattended runner restoration if their safety conditions cannot be established.

## Context

The Homepage OFF button (`kubernetes/apps/apps/cloud-power/`, `home.chifor.me/cloud-power/`) powers
off the cloudlab GPU hosts (cloud1/2/3) that also carry the opportunistic Gitea CI runners
`cloud-ci-1..5` (ADR 0032). Operator request (2026-09-28): *"the turning off should be scheduled, the
runners should not take new jobs, and after they are finishing the existing ones, the machine should
be turned off."*

**Today's OFF path** — preflight (list running guests, 30 s confirm token) -> confirm ->
`POST /nodes/{n}/status command=shutdown` on every up node, immediately. The drain happens *inside*
the host shutdown: `pve-guests` stopall gives each `cloud-ci-*` VM `startup down=720`, the guest's
systemd stops `gitea-act-runner` (`KillMode=mixed`), act_runner stops polling and waits
`shutdown_timeout: 10m` for its in-flight job, then **cancels it**.

**Measured failure.** The only real OFF since the runners went live (2026-09-24 23:54) cancelled a
job on cloud-ci-3: `shutdown initiated, waiting 10m0s` at 23:54:30 -> `cancelled in progress jobs
during shutdown` at 00:04:31 (guest journal, boot -1). The guest-side ordering held (docker, DNS and
sudo stayed up — it was purely the cap). Job-duration sample from the org jobs API (n=1758):
p50 4 s, p90 136 s, **p99 774 s, p99.9 1674 s, max 3002 s**; `gatekeeper` routinely runs ~13 min.
A fixed 10-minute window structurally cancels the long tail, and raising it inside a host shutdown
runs into `poweroff.target`'s 30-min `JobTimeoutSec` (then `poweroff-force`), leaves the LLM LXCs
already dead while runners drain, and cannot be seen or cancelled from the dashboard once issued.

**The primitive that fixes it.** Gitea 1.26.1 (live version, swagger) has
`PATCH /orgs/{org}/actions/runners/{id} {"disabled": true}`; `services/actions/task.go:PickTask`
returns no task for `runner.IsDisabled` (and `FetchTask` reloads the runner from the DB first, so a
disable racing a poll is honoured), while an already-assigned task keeps running and reporting.
`GET /orgs/{org}/actions/jobs?status=in_progress` returns running jobs with `runner_name`, and the
runner object carries `busy` (`LastActive` < 10 s; act_runner's reporter calls `UpdateTask` every
second while a task runs). So "stop taking new jobs, let the current ones finish" can be done at the
forge, with the guest fully up — no dependence on shutdown ordering or drain caps.

<!-- codex: Reloading before PickTask does not establish an atomic barrier against a FetchTask that read the runner before PATCH committed. Verify how an assignment racing disable becomes visible, including any assigned-but-not-yet-in_progress interval, before treating two empty polls as sufficient. -->
<!-- codex: LastActive is a heartbeat heuristic, not execution state; docs/runbooks/ci-runners.md already records busy=false during a running job. Retain the union check, but verify its coverage during delayed reporting, cancellation, and final artifact/log uploads rather than assuming one-second reports are guaranteed. -->

## Approach

### State machine in `cloud-power` (MODE=api only)

```
idle --OFF+confirm--> draining --(no cloud job in flight, 2 consecutive polls)--> powering_off --(every paused runner offline)--> idle
  |                     |                                                           |
  |                     +- CANCEL / ON -> re-enable runners -> idle                 +- 30 min and a runner still online -> re-enable, record error -> idle
  |                     +- deadline (job timeout 3h + 15 min) -> powering_off (logged "deadline")
  +- Gitea unreachable / a disable fails -> roll back the disables, refuse (HTTP 502), stay idle
```

<!-- codex: ThreadingHTTPServer permits concurrent OFF, CANCEL, and ON requests alongside the worker; the current lock protects only confirmation tokens. Serialize schedule mutations and the shutdown commitment, reject a second active schedule server-side, and invalidate stale worker observations when cancellation wins. -->

1. **Schedule (`POST /api/shutdown`, same confirm token + guest-signature re-check as today).**
   List org runners, select `name` starting `CLOUD_RUNNER_PREFIX` (`cloud-ci-`) that are **not
   already disabled** (an operator-disabled runner is left alone and never re-enabled by us), PATCH
   each `disabled: true`, and persist the state *before* answering. If any PATCH fails, re-enable the
   ones already paused and refuse. No cloud runner registered -> schedule anyway (nothing to drain;
   the next tick powers off).

   <!-- codex: Already-disabled runners can still have in-flight jobs, which this recorded set would ignore. Maintain separate sets for all cloud runners whose work must drain and runners whose disabled state this schedule owns. -->
   <!-- codex: Persisting after the PATCH sequence leaves a crash window that strands runners disabled with no recovery record. Persist the intended runner IDs and original states before mutations, including the runner whose PATCH times out because that mutation may have succeeded. -->
   <!-- codex: Rollback and re-enable calls can also fail; returning to idle then loses the outstanding cleanup. Retain durable restoration work and expose a recovery/error state until each owned runner is reconciled, including failures during cancel and normal completion. -->
   <!-- codex: An empty match can mean a wrong organization or prefix, not an empty cloud pool. Validate the target configuration and make the no-runner shutdown policy explicit instead of silently treating unexpected disappearance as an all-clear. -->
   <!-- codex: A guest signature checked at scheduling time can be hours stale when shutdown occurs. Specify whether confirmation authorizes newly started guests too; otherwise recheck before dispatch and stop for renewed confirmation when the set changes. -->

2. **Draining (worker thread, every `DRAIN_POLL_SEC`=20 s).** Re-assert `disabled` on the recorded
   runners (idempotent: PATCH only when the API shows it enabled — someone re-enabling in the UI
   mid-drain must not silently reopen the gate). Collect in-flight work = in_progress org jobs whose
   `runner_name` is a recorded runner **union** recorded runners with `busy: true`. Drained = empty on
   two consecutive polls. Any Gitea error -> not drained (keep waiting). Then call the existing
   `shutdown_all()`; `pve-guests` still stops the (now idle) VMs and the LXCs, and the hosts' RTC
   hook arms the wake alarm exactly as today.

   <!-- codex: Both runner and job enumeration need complete pagination and response validation; a failed later page or missing required field must never become an empty workload. Check the org jobs endpoint's actual pagination contract rather than assuming the watchdog's other endpoints have the same response shape. -->
   <!-- codex: A fixed recorded set misses new or re-registered runners during the drain, and deletion can remove the runner_name association documented in ADR 0032. Detect membership/identity changes and reconcile or stop safely; preserve IDs for mutations and define how ambiguous names are handled. -->
   <!-- codex: Reset the clear-poll count after any API error, observed work, or runner re-disable, and obtain fresh observations after restart. Require the polls to be separated by the configured interval so a retry or restart cannot manufacture two consecutive clear observations. -->

3. **Deadline.** `DRAIN_MAX_SEC` default 11700 s (3 h 15 min) = act_runner's `runner.timeout` 3 h +
   margin. Past it, no job that was running at schedule time can still be alive (Gitea times it out),
   so powering off cannot cut a legitimate job; it only guards against a stuck Gitea/API.

   <!-- codex: The supporting Ansible template sets act_runner's local runner.timeout, not a verified Gitea-enforced upper bound on live execution, and effective configuration or cleanup can differ. Treat expiry as a visible stalled/error condition; automatic forced shutdown during API uncertainty does not satisfy the promise to finish existing jobs and should be a separately defined policy. -->

4. **Powering off.** Re-enable each recorded runner once the API reports it `offline` (stopped
   polling >= 1 min — its VM is going down, so it cannot pick anything up), so the pool is whole again
   when the hosts wake (RTC 08:00 or ON) with no morning coupling. If all node shutdown calls failed,
   re-enable immediately; if a runner is still online `OFFLINE_WAIT_SEC`=1800 s after the shutdown
   call, re-enable it anyway (its host evidently did not go down) and record the error.

   <!-- codex: Offline means stale contact, not a stopped VM: a transient network failure or daemon restart can produce it while the host remains up. Re-enabling then allows new work if polling resumes before shutdown finishes; require corroborating shutdown evidence or retain the pause. -->
   <!-- codex: Still online after 30 minutes does not prove the host abandoned shutdown; it may be blocked earlier in its stop sequence. Re-enabling on that timer can admit a job immediately before a delayed power-off, so leave this outcome unresolved rather than inferring safety from elapsed time. -->
   <!-- codex: shutdown_all() currently labels a failed TCP probe “already off” and reports request exceptions without distinguishing a rejected shutdown from an accepted request whose reply was lost. Preserve uncertain outcomes and avoid immediately restoring runners or blindly retrying power commands on that evidence. -->
   <!-- codex: Completion must account for all three host outcomes, including a host with only LXCs or no recorded runner. Every paused runner being offline, or the paused set being empty, cannot establish that the requested cluster shutdown succeeded. -->
   <!-- codex: A smaller MVP could retain pauses until explicit ON/resume if reliable shutdown completion cannot be established. Deferring unattended RTC restoration would simplify recovery, but its required morning operator action must be documented as a deliberate tradeoff. -->

5. **Cancel / ON.** `POST /api/shutdown/cancel` (new) and `POST /api/wake` (existing) in `draining`
   re-enable the recorded runners and clear the schedule. In `powering_off` cancel is refused (the
   hosts are already shutting down); ON still forwards the wake.

   <!-- codex: A WoL packet sent while a machine is still shutting down can be ineffective, leaving it off despite a successful HTTP response. Define this as a best-effort request with clear feedback, or persist a pending wake to execute after shutdown completes. -->
   <!-- codex: The initial disabled snapshot preserves existing operator decisions but cannot detect an operator quarantining an already-owned runner mid-drain. Document coordination for maintenance during an active schedule so later restoration does not silently undo that intent. -->

6. **Persistence.** The schedule lives in a runtime-created ConfigMap `cloud-power-state` (the
   `ci-rerun-watchdog` pattern: not in git so Flux prune leaves it; resourceVersion-checked writes).
   On start the worker reads it and resumes `draining`/`powering_off`, so a pod restart mid-drain
   (Renovate bump, node drain) neither loses the schedule nor strands runners disabled. A write
   failure at schedule time rolls back the disables and refuses.

   <!-- codex: Define the durable ordering around powering_off and each PVE call, including crashes after a request succeeds but before its outcome is saved. Recovery must distinguish unissued from uncertain requests and must not replay an old shutdown against hosts that have since awakened. -->
   <!-- codex: Reuse KubeState's transport, not the watchdog State.save() conflict merge, which retries a stale body after importing only its operator keys. A schedule conflict must reload and re-evaluate the complete current state before any further side effect. -->
   <!-- codex: Specify a versioned state schema and fail closed on unreadable or unsupported state; the watchdog's permissive JSON loader resets malformed keys to empty. Distinguish first initialization from state loss and document recovery when ownership records are unavailable. -->

7. **Single worker.** The api Deployment switches to `strategy: Recreate` so a rollout never runs two
   workers; writes are resourceVersion-guarded anyway.

   <!-- codex: Recreate orders deployment rollouts but is not a general singleton guarantee during pod replacement, forced deletion, or node partition. resourceVersion protects writes rather than external side effects, so define ownership/fencing for workers and stop stale workers from acting after ownership changes. -->

### UI (the iframe page)
Preflight shows running guests **and** the in-flight cloud CI jobs. After confirm the status line
shows `OFF scheduled — waiting for N job(s): cloud-ci-3 gatekeeper (12 min) …`, the schedule age and
the deadline, and a **CANCEL** button (OFF disabled while scheduled). `powering_off` shows
`powering off — runners paused until hosts are down`. The last outcome (done / error) is shown once.
`/api/status` grows a `schedule` object; the page keeps its 15 s refresh.

<!-- codex: Expose the last successful worker poll, current API/state error, unresolved restoration, and durable last outcome so a stalled controller is distinguishable from a long job. The current health endpoint always returns success, so add worker-health verification without making a transient Gitea outage trigger a restart loop. -->
<!-- codex: Job and runner names introduce externally controlled strings into this page. Render them with textContent or equivalent escaping, and check HTTP response status before displaying successful cancel/wake feedback. -->

### Access
- New Gitea PAT for an org owner (`chifor`), scopes `read:organization,write:organization` (runner
  list/PATCH + org jobs), minted once in the gitea pod, SOPS-encrypted into
  `cloud-power/secret.sops.yaml` as `gitea_token` beside the PVE token (never printed).
  `GITEA_URL` = in-cluster `http://gitea-http.gitea.svc.cluster.local:3000` (no Cloudflare hop).

  <!-- codex: An owner PAT with write:organization grants substantially more authority than runner pause/resume, and its scope is not restricted by CLOUD_RUNNER_PREFIX. Prefer a dedicated identity limited to the target organization where supported, and document the unavoidable authority plus rotation/revocation procedure. -->
  <!-- codex: This URL sends the owner PAT over plaintext HTTP; avoiding Cloudflare does not itself protect the bearer token. Verify the cluster transport trust/encryption assumption or use verified TLS, and prevent the client from forwarding credentials through unexpected redirects. -->

- ServiceAccount `cloud-power` + Role (create configmaps; get/update/patch on `cloud-power-state`
  only), token automounted on the api pod only; the hostNetwork wol pod keeps no token.

  <!-- codex: ConfigMap create permission is namespace-wide because RBAC cannot restrict create by resourceNames; keep that limitation explicit and omit patch if reusing KubeState's GET/POST/PUT implementation. Verify the account cannot update the generated application ConfigMap or delete the state object, since state integrity controls recovery and power actions. -->

- NetworkPolicy: unchanged — the api pod's egress is already unrestricted (documented in
  `networkpolicy.yaml`); gitea has no ingress policy. Ingress stays oauth2-proxy-only.

  <!-- codex: The Homepage proxy currently admits any Authelia-authenticated user, while cloud-power uses forwarded identity only for logging. Confirm that this remains the intended operator boundary for scheduling/cancelling power actions, and test denial of direct non-proxy access to the new routes. -->
  <!-- codex: Proxy authentication does not establish CSRF protection for upstream application POSTs. Add or verify Origin/CSRF enforcement for cancel and API wake, which now re-enable runners and cancel a pending shutdown without a confirmation token. -->

### Unchanged on purpose
The in-guest 10-min drain, `startup down=720` and cloudlab `cluster-power.sh` stay as the backstop
for the paths that do not go through the button (PVE UI, `poweroff`, the CLI). Moving the CLI onto
the same Gitea-level drain is a cloudlab follow-up, noted in the ADR.

<!-- codex: Document that direct calls to the hostNetwork WoL service and RTC wake do not pass through API ON and therefore cannot cancel a persisted schedule. Recovery after an outage spanning a wake event needs an explicit stale-schedule policy to avoid an unexpected daytime shutdown. -->

## Critical files
- `kubernetes/apps/apps/cloud-power/app.py` — Gitea client, KubeState, schedule state machine +
  worker, `/api/shutdown` (schedule), `/api/shutdown/cancel`, `/api/status.schedule`, page UI.
- `kubernetes/apps/apps/cloud-power/deployment.yaml` — api pod: SA, automount, Gitea env + secret
  key, `Recreate`, knobs (`CLOUD_RUNNER_PREFIX`, `DRAIN_POLL_SEC`, `DRAIN_MAX_SEC`, `OFFLINE_WAIT_SEC`).
- `kubernetes/apps/apps/cloud-power/rbac.yaml` (new) + `kustomization.yaml`.
- `kubernetes/apps/apps/cloud-power/secret.sops.yaml` — add `gitea_token`.
- `scripts/tests/test_cloud_power.py` (new) — unit tests, run by the existing `broker-inventory`
  workflow (`unittest discover -s scripts/tests`).
- `docs/decisions/0032-opportunistic-cloud-ci-runners.md` (amendment), `docs/runbooks/ci-runners.md`
  (drain section).

<!-- codex: Add a rollback procedure that settles or explicitly transfers runner-restoration work before reverting to the old binary, which ignores this ConfigMap. Runtime creation avoids ordinary Flux inventory pruning but does not protect state against namespace deletion or restore, so preserve the ownership record through rollback and document recovery from its loss. -->

## Verification
1. Unit tests (fake Gitea transport, in-memory state store, fake `shutdown_all`): schedule pauses only
   enabled cloud runners; failed PATCH rolls back; draining waits while a job is in progress or a
   runner is busy, needs two clear polls, keeps waiting on Gitea errors, re-disables a runner
   re-enabled mid-drain; deadline forces power-off; powering_off re-enables only offline runners,
   times out with an error; cancel/wake re-enable; restart resumes from persisted state; an
   operator-disabled runner is never touched.

   <!-- codex: Add fault injection before and after each state write and external mutation, including accepted-but-timed-out requests, failed rollback, partial restoration, and crashes during shutdown dispatch. Exercise concurrent OFF/CANCEL/ON, multi-page responses, pre-disabled busy runners, identity changes, and stale schedules after a wake using a controllable clock. -->

2. `kubectl kustomize kubernetes/apps/apps/cloud-power` renders; the existing manifests CI passes.

   <!-- codex: Rendering does not verify real ConfigMap conflict behavior or projected-token access and rotation under UID 65532. Add focused in-cluster checks for state read/create/update and forbidden operations, and verify MODE=wol starts without either credential and rejects all drain/status-control routes. -->

3. Live, after Flux applies: `/api/status` answers through the dashboard with `schedule: idle`;
   the state CM is created; token scope check = `GET /orgs/cchifor/actions/runners` 200.

   <!-- codex: A runner-list 200 does not validate PATCH permission or org-job visibility, especially for private repositories. Verify the exact deployed PAT against the jobs endpoint and a reversible disable/readback/restore on a controlled runner. -->
   <!-- codex: Record Gitea 1.26.1 and act_runner 0.6.1 as the tested compatibility baseline, including disabled polling, busy/status fields, and job-state transitions. Repeat that contract check on upgrades and refuse scheduling when required capabilities or response fields are absent. -->

4. Live drain test at the next real OFF with a job in flight on a cloud runner: runner shows
   `disabled` in Gitea immediately, no new task lands on it, the job finishes green, hosts power off
   only afterwards, runners are re-enabled once offline, and the guest journal shows act_runner's
   stop with **no** `cancelled in progress jobs`. (Schedule-then-cancel is a zero-risk dry test
   of the pause/re-enable half during the day.)

   <!-- codex: Use a controlled job longer than ten minutes, including a quiet execution period and final artifact/log upload, so the test exercises the original failure and heartbeat assumptions. Also verify restart during draining, the next RTC/ON wake, continued dispatch to ailab runners, and watchdog behavior after the planned shutdown. -->
   <!-- codex: Schedule-then-cancel mutates live capacity and can strand runners if restoration fails, so it is not a zero-risk dry run. Verify every owned runner's final disabled state and retain a tested recovery procedure before performing it. -->

<!-- codex-review-status: complete -->
