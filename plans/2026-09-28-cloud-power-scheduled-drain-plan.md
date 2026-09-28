# Cloud-power OFF becomes a scheduled, drained power-off

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
`PATCH /orgs/{org}/actions/runners/{id} {"disabled": true}` (org OWNER, token category
organization); `services/actions/task.go:PickTask` returns no task for `runner.IsDisabled` (and
`FetchTask` reloads the runner from the DB first, so a disable racing a poll is honoured), while an
already-assigned task keeps running and reporting. `GET /orgs/{org}/actions/jobs?status=in_progress`
(org reader; `in_progress` maps to `StatusRunning`) returns running jobs with `runner_name` and
`total_count`. The runner object carries `busy`, but `busy` is only `LastActive < 10 s` and was seen
flapping to `false` mid-job (runbook §7, 2026-09-16), so it is a SECONDARY signal: the job list is
authoritative and the two are unioned. So "stop taking new jobs, let the current ones finish" is
done at the forge, with the guest fully up — no dependence on shutdown ordering or drain caps.

<!-- codex: Reloading before PickTask does not establish an atomic barrier against a FetchTask that read the runner before PATCH committed. Verify how an assignment racing disable becomes visible, including any assigned-but-not-yet-in_progress interval, before treating two empty polls as sufficient. -->
<!-- opus-pushback: In 1.26.1 FetchTask re-reads the runner immediately before PickTask, and PickTask creates the task and flips the job to StatusRunning in ONE transaction (CreateTaskForRunner), so there is no assigned-but-not-running state; the residual is a millisecond window whose result is a running job, which two clear polls 20 s apart observe. -->

## Approach

### State machine in `cloud-power` (MODE=api only)

```
idle --OFF+confirm--> draining --(no cloud job in flight on 2 polls >= 20 s apart)--> powering_off --(every host that accepted
  |                    |   |                                                            |       shutdown is down AND each owned
  |                    |   +- deadline (3 h 15 min) -> stalled                          |       runner is offline)--> release --> idle
  |                    +- CANCEL / ON -> releasing -> idle                              +- every shutdown call failed -> stalled
  +- Gitea/state error, no cloud runner found -> refuse, nothing changed                +- 30 min, a host still up -> stalled
stalled --CANCEL / ON--> releasing -> idle          (runners stay paused while stalled; the page shows why)
```

All operator actions (schedule, cancel, wake-cancel) and every worker tick run under ONE lock, so
OFF/CANCEL/ON and the worker are serialised; `/api/status` reads an immutable snapshot published
after each transition, so a status poll never blocks on a slow Gitea/PVE call.

1. **Schedule (`POST /api/shutdown`, same confirm token + guest-signature re-check as today).**
   List org runners (fully paginated, response shape validated: every runner must carry `id`,
   `name`, `disabled`, `busy`, `status` or the call is refused). Cloud runners = `name` starting
   `CLOUD_RUNNER_PREFIX` (`cloud-ci-`). **Zero cloud runners -> refuse** (a wrong prefix/org would
   otherwise silently skip the drain; the PVE UI / `cluster-power.sh` remain for a deliberate
   no-drain shutdown). **Persist first**: the state (schema `v: 1`) records the runners this schedule
   will own = the cloud runners that are currently enabled; runners already disabled are recorded
   as `skipped` and are never re-enabled by us. Only then PATCH each owned runner `disabled: true`.
   Any PATCH failure (including a timeout, which may have succeeded) -> `releasing` (re-enable every
   owned runner, retried each tick until done) and the request answers 502.
   The confirmation authorises "power off the cloud cluster once CI has drained", whatever guests are
   running at that later moment: the preflight lists what is running now, and the page says so.
2. **Draining (worker, every `DRAIN_POLL_SEC`=20 s).** Each tick: list runners; **any cloud runner
   that is enabled** (re-enabled by someone mid-drain, or newly registered) is paused and added to the
   owned set, and the clear count resets. In-flight work = in_progress org jobs whose `runner_name`
   has the cloud prefix (ALL cloud runners, owned or skipped — an operator-disabled runner can still
   be finishing a job) **union** cloud runners with `busy: true`. Drained = empty on
   `DRAIN_CLEAR_POLLS`=2 consecutive ticks; the count resets on any API error, any in-flight work and
   any re-pause, and lives in memory, so a restart re-observes from zero. Any Gitea error -> not
   drained. Then power off (step 4).
3. **Deadline -> stalled, not a forced power-off.** `DRAIN_MAX_SEC` default 11700 s (3 h 15 min,
   act_runner's 3 h job timeout + margin). Reaching it means something is wrong (Gitea unreachable,
   a job the forge still shows as running); powering off anyway would break the "finish existing
   jobs" promise, so the schedule goes `stalled` and the page shows why. This also defuses a STALE
   schedule: a pod that was down all night and resumes a `draining` state past its deadline does not
   power the hosts off in the morning.
4. **Powering off.** Persist `powering_off` (+`shutdown_sent: false`) BEFORE calling the existing
   `shutdown_all()`, then persist its per-node results. A resume that finds `shutdown_sent: false`
   re-sends only if the transition is younger than `STALE_SHUTDOWN_SEC`=600 s, otherwise -> `stalled`
   (never replays an old shutdown against hosts that may have woken since). Every node call failed ->
   `stalled`. `pve-guests` still stops the (now idle) VMs and the LXCs, and the hosts' RTC hook arms
   the wake alarm exactly as today.
5. **Releasing the runners after the power-off.** A runner is re-enabled only when BOTH (a) every
   node that answered `shutdown requested` has been observed down (TCP :8006 refused/timeout) on two
   consecutive ticks, and (b) Gitea reports the runner `offline` (no poll for > 1 min). (a) alone
   could be pveproxy stopping before the VMs; (b) alone could be a network blip on a running VM;
   together the VM is gone. Then `idle` with outcome `off`, so the pool is whole again whenever the
   hosts wake (RTC 08:00 or ON) with no morning coupling. A host still up `OFFLINE_WAIT_SEC`=1800 s
   after the shutdown call -> `stalled` with the runners still paused (it may yet go down; re-enabling
   could hand a job to a VM about to die). The operator resolves a stall with CANCEL.
6. **Cancel / ON.** `POST /api/shutdown/cancel` (new) in `draining` or `stalled` persists
   `releasing` BEFORE re-enabling; refused in `powering_off` (the hosts are going down). `POST
   /api/wake` also cancels a `draining`/`stalled` schedule first; in `powering_off` it still forwards
   the packets but the page says the hosts are still shutting down (WoL on a running host is a no-op).
   Direct calls to the hostNetwork wol service and the RTC alarm do not touch the schedule — by then
   it has either finished or is `stalled` (deadline), never pending an unexpected shutdown.
7. **Persistence.** The schedule lives in a runtime-created ConfigMap `cloud-power-state` (not in git,
   so Flux prune leaves it), written with resourceVersion. A 409 marks the state unloaded and the
   next tick RE-READS and re-evaluates it (no merge-and-retry of a stale body). An unreadable or
   unknown-version state fails closed: phase `unknown`, OFF refused, nothing mutated, the page shows
   the error. Transitions persist before they act, so a restart repeats at most an idempotent call.
8. **Single worker.** One replica, `strategy: Recreate`.

   <!-- codex: Recreate orders deployment rollouts but is not a general singleton guarantee during pod replacement, forced deletion, or node partition. resourceVersion protects writes rather than external side effects, so define ownership/fencing for workers and stop stale workers from acting after ownership changes. -->
   <!-- opus-pushback: A lease/fencing layer is disproportionate for a single-replica dashboard controller: the side effects are idempotent PATCHes of the same value and a node shutdown gated on a persisted transition, and a second worker's first write after any external action hits 409 and forces it to re-read. The forced-deletion/partition double-worker case is accepted and documented rather than engineered away. -->

### UI (the iframe page, h-32)
Preflight shows the in-flight cloud CI jobs **and** the running guests. After confirm the summary
line shows `OFF scheduled HH:MM - waiting for N CI job(s)`, the scrollable body lists
`runner job (age)`, the paused runners and the deadline, with a **CANCEL OFF** button (OFF disabled
while scheduled). `powering_off`, `stalled` (with the reason and CANCEL) and `releasing` each have a
line; the last outcome is shown for 12 h. Gitea-supplied strings (job/runner names) are rendered with
`textContent` only; every fetch checks `r.ok` before claiming success. The snapshot carries
`last_tick`; the page shows `controller not polling` if it is older than 3 polls (the liveness probe
stays a plain healthz, so a Gitea outage does not restart-loop the pod).

### Access
- **Gitea PAT** for the org owner `chifor`, scope `write:organization` (runner list/PATCH need owner;
  org jobs need reader; both are in the organization token category — `routers/api/v1/api.go`).
  This is broader than "pause runners" (it can administer the org) and there is no narrower Gitea
  scope; it is minted once in the gitea pod, SOPS-encrypted into a separate
  `cloud-power/secret-gitea.sops.yaml` (`cloud-power-gitea`, key `token`), mounted only into the api
  pod, and never printed. Rotation/revocation is in the runbook (UI -> Settings -> Applications).
  `GITEA_URL` = in-cluster `http://gitea-http.gitea.svc.cluster.local:3000`. The client refuses to
  follow redirects (urllib would carry the Authorization header to the redirect target).

  <!-- codex: This URL sends the owner PAT over plaintext HTTP; avoiding Cloudflare does not itself protect the bearer token. Verify the cluster transport trust/encryption assumption or use verified TLS, and prevent the client from forwarding credentials through unexpected redirects. -->
  <!-- opus-pushback: Redirects: accepted (no-redirect opener). Plaintext: the same in-cluster HTTP path already carries the ci-rerun-watchdog's write:repository PAT and every Gitea runner/agent token in the estate; pod-to-pod traffic stays on the cluster network, and the alternative (git.chifor.me) adds a Cloudflare round trip and an external dependency to a power-off path. -->

- **Kubernetes**: ServiceAccount `cloud-power` + Role: `create` configmaps (cannot be
  resourceName-scoped — namespace-wide create is the accepted limitation) and `get`,`update` on
  `cloud-power-state` only (no patch/list/watch/delete). Token automounted on the api pod only; the
  hostNetwork wol pod keeps `automountServiceAccountToken: false` and no Gitea token.
- **CSRF**: POSTs are refused when `Sec-Fetch-Site` is `cross-site`/`same-site` or an `Origin`
  header is present and is not the dashboard origin (`ALLOWED_ORIGINS`, default
  `https://home.chifor.me`). Cancel and ON now have side effects without a confirm token.
- **Who may press it**: unchanged — any Authelia-authenticated dashboard user, behind oauth2-proxy;
  NetworkPolicy still admits only oauth2-proxy to the api pod; egress unchanged (unrestricted).

  <!-- codex: The Homepage proxy currently admits any Authelia-authenticated user, while cloud-power uses forwarded identity only for logging. Confirm that this remains the intended operator boundary for scheduling/cancelling power actions, and test denial of direct non-proxy access to the new routes. -->
  <!-- opus-pushback: That boundary is the existing, deliberately chosen one for the OFF button (ailab #389/#390) and this change does not widen it; narrowing it is a separate decision. The non-proxy denial is already enforced by the NetworkPolicy and is re-checked in the live verification below. -->

### Unchanged on purpose
The in-guest 10-min drain, `startup down=720` and cloudlab `cluster-power.sh` stay as the backstop
for the paths that do not go through the button (PVE UI, `poweroff`, the CLI). Moving the CLI onto
the same Gitea-level drain is a cloudlab follow-up, noted in the ADR.

### Operating notes (runbook)
- An operator who quarantines an owned runner mid-drain must do it AFTER cancelling or after the
  schedule finishes: the drain re-pauses and then re-enables every runner it owns.
- **Rollback** to the previous image: first make sure `/api/status` shows `idle` (CANCEL any
  schedule); if the state was lost with runners paused, re-enable them by hand (`PATCH
  .../runners/{id} {"disabled": false}` for each `cloud-ci-*` not deliberately quarantined).
- A `stalled` schedule keeps the cloud runners paused until someone presses CANCEL; the page says so.

## Critical files
- `kubernetes/apps/apps/cloud-power/app.py` — Gitea client, KubeState, Scheduler + worker,
  `/api/shutdown` (schedule), `/api/shutdown/cancel`, `/api/status.schedule`, CSRF check, page UI.
- `kubernetes/apps/apps/cloud-power/deployment.yaml` — api pod: SA, automount, Gitea env + secret,
  `Recreate`, knobs (`CLOUD_RUNNER_PREFIX`, `DRAIN_POLL_SEC`, `DRAIN_MAX_SEC`, `OFFLINE_WAIT_SEC`).
- `kubernetes/apps/apps/cloud-power/rbac.yaml` (new), `secret-gitea.sops.yaml` (new),
  `kustomization.yaml`.
- `scripts/tests/test_cloud_power.py` (new) — run by the existing `broker-inventory` workflow
  (`unittest discover -s scripts/tests`).
- `docs/decisions/0032-opportunistic-cloud-ci-runners.md` (amendment), `docs/runbooks/ci-runners.md`
  (drain section + operating notes).

## Verification
1. Unit tests (transport-level fake Gitea with real pagination, in-memory store with resourceVersion
   + injectable write failures, recorded `shutdown_all`, fake host probe, controllable clock):
   pause set/skip set; persist-before-pause; failed pause rolls back and retries a failed re-enable;
   zero cloud runners refused; malformed runner/job objects refused; job on page 3 seen; busy-only
   and job-only in-flight both hold; operator-disabled runner's job holds; Gitea error resets the
   clear count; mid-drain re-enable and new runner are paused and owned; deadline -> stalled;
   release needs host down AND runner offline; host never down -> stalled with runners paused; all
   shutdown calls failed -> stalled; cancel from draining/stalled, refused in powering_off; restart
   mid-drain resumes; restart with unsent shutdown resends when fresh and stalls when stale; 409 ->
   reload; unreadable/unknown-version state -> OFF refused; concurrent schedule from two threads ->
   exactly one wins; CSRF check.

   <!-- codex: Add fault injection before and after each state write and external mutation, including accepted-but-timed-out requests, failed rollback, partial restoration, and crashes during shutdown dispatch. Exercise concurrent OFF/CANCEL/ON, multi-page responses, pre-disabled busy runners, identity changes, and stale schedules after a wake using a controllable clock. -->
   <!-- opus-pushback: The list above covers each crash boundary the design relies on (persist-then-act means every boundary resumes into a phase already under test) plus the concrete cases named; an exhaustive before/after matrix per call would multiply tests without exercising new code paths. -->

2. `kubectl kustomize kubernetes/apps/apps/cloud-power` renders; manifests CI passes.
3. Live, after Flux applies (compatibility baseline: Gitea 1.26.1, act_runner 0.6.1):
   - `kubectl auth can-i` as `system:serviceaccount:cloud-power:cloud-power`: get/update
     `cloud-power-state` yes; update `cloud-power-app-*`, delete, list configmaps no.
   - `/api/status` through the dashboard shows `schedule.phase: idle` and a fresh `last_tick`; the
     state ConfigMap exists; the wol pod still answers only `POST /api/wake` and has no SA token.
   - The deployed PAT, from the api pod: runners list 200, org jobs 200 (a private repo's running job
     is visible), and a reversible `disabled: true` -> read back -> `disabled: false` on the
     quarantined, offline `cloud-ci-6` (no capacity impact).
   - A pod in another namespace still cannot reach the api Service (NetworkPolicy).
4. Live drain test at the next real OFF with a job in flight on a cloud runner (ideally one > 10 min,
   e.g. `gatekeeper`): Gitea shows the runners `disabled` at once, no new task lands on them, ailab
   runners keep taking jobs, the job finishes green, hosts power off only afterwards, runners are
   re-enabled once their hosts are down, the guest journal shows act_runner's stop with **no**
   `cancelled in progress jobs`, and the watchdog records no rerun. A daytime schedule-then-cancel
   exercises the pause/re-enable half with brief (seconds) capacity impact; check every owned runner
   ends `disabled: false`.

<!-- codex-review-status: complete -->
