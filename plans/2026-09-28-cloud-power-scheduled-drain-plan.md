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
`PATCH /orgs/{org}/actions/runners/{id} {"disabled": true}`; `services/actions/task.go:PickTask`
returns no task for `runner.IsDisabled` (and `FetchTask` reloads the runner from the DB first, so a
disable racing a poll is honoured), while an already-assigned task keeps running and reporting.
`GET /orgs/{org}/actions/jobs?status=in_progress` returns running jobs with `runner_name`, and the
runner object carries `busy` (`LastActive` < 10 s; act_runner's reporter calls `UpdateTask` every
second while a task runs). So "stop taking new jobs, let the current ones finish" can be done at the
forge, with the guest fully up — no dependence on shutdown ordering or drain caps.

## Approach

### State machine in `cloud-power` (MODE=api only)

```
idle --OFF+confirm--> draining --(no cloud job in flight, 2 consecutive polls)--> powering_off --(every paused runner offline)--> idle
  |                     |                                                           |
  |                     +- CANCEL / ON -> re-enable runners -> idle                 +- 30 min and a runner still online -> re-enable, record error -> idle
  |                     +- deadline (job timeout 3h + 15 min) -> powering_off (logged "deadline")
  +- Gitea unreachable / a disable fails -> roll back the disables, refuse (HTTP 502), stay idle
```

1. **Schedule (`POST /api/shutdown`, same confirm token + guest-signature re-check as today).**
   List org runners, select `name` starting `CLOUD_RUNNER_PREFIX` (`cloud-ci-`) that are **not
   already disabled** (an operator-disabled runner is left alone and never re-enabled by us), PATCH
   each `disabled: true`, and persist the state *before* answering. If any PATCH fails, re-enable the
   ones already paused and refuse. No cloud runner registered -> schedule anyway (nothing to drain;
   the next tick powers off).
2. **Draining (worker thread, every `DRAIN_POLL_SEC`=20 s).** Re-assert `disabled` on the recorded
   runners (idempotent: PATCH only when the API shows it enabled — someone re-enabling in the UI
   mid-drain must not silently reopen the gate). Collect in-flight work = in_progress org jobs whose
   `runner_name` is a recorded runner **union** recorded runners with `busy: true`. Drained = empty on
   two consecutive polls. Any Gitea error -> not drained (keep waiting). Then call the existing
   `shutdown_all()`; `pve-guests` still stops the (now idle) VMs and the LXCs, and the hosts' RTC
   hook arms the wake alarm exactly as today.
3. **Deadline.** `DRAIN_MAX_SEC` default 11700 s (3 h 15 min) = act_runner's `runner.timeout` 3 h +
   margin. Past it, no job that was running at schedule time can still be alive (Gitea times it out),
   so powering off cannot cut a legitimate job; it only guards against a stuck Gitea/API.
4. **Powering off.** Re-enable each recorded runner once the API reports it `offline` (stopped
   polling >= 1 min — its VM is going down, so it cannot pick anything up), so the pool is whole again
   when the hosts wake (RTC 08:00 or ON) with no morning coupling. If all node shutdown calls failed,
   re-enable immediately; if a runner is still online `OFFLINE_WAIT_SEC`=1800 s after the shutdown
   call, re-enable it anyway (its host evidently did not go down) and record the error.
5. **Cancel / ON.** `POST /api/shutdown/cancel` (new) and `POST /api/wake` (existing) in `draining`
   re-enable the recorded runners and clear the schedule. In `powering_off` cancel is refused (the
   hosts are already shutting down); ON still forwards the wake.
6. **Persistence.** The schedule lives in a runtime-created ConfigMap `cloud-power-state` (the
   `ci-rerun-watchdog` pattern: not in git so Flux prune leaves it; resourceVersion-checked writes).
   On start the worker reads it and resumes `draining`/`powering_off`, so a pod restart mid-drain
   (Renovate bump, node drain) neither loses the schedule nor strands runners disabled. A write
   failure at schedule time rolls back the disables and refuses.
7. **Single worker.** The api Deployment switches to `strategy: Recreate` so a rollout never runs two
   workers; writes are resourceVersion-guarded anyway.

### UI (the iframe page)
Preflight shows running guests **and** the in-flight cloud CI jobs. After confirm the status line
shows `OFF scheduled — waiting for N job(s): cloud-ci-3 gatekeeper (12 min) …`, the schedule age and
the deadline, and a **CANCEL** button (OFF disabled while scheduled). `powering_off` shows
`powering off — runners paused until hosts are down`. The last outcome (done / error) is shown once.
`/api/status` grows a `schedule` object; the page keeps its 15 s refresh.

### Access
- New Gitea PAT for an org owner (`chifor`), scopes `read:organization,write:organization` (runner
  list/PATCH + org jobs), minted once in the gitea pod, SOPS-encrypted into
  `cloud-power/secret.sops.yaml` as `gitea_token` beside the PVE token (never printed).
  `GITEA_URL` = in-cluster `http://gitea-http.gitea.svc.cluster.local:3000` (no Cloudflare hop).
- ServiceAccount `cloud-power` + Role (create configmaps; get/update/patch on `cloud-power-state`
  only), token automounted on the api pod only; the hostNetwork wol pod keeps no token.
- NetworkPolicy: unchanged — the api pod's egress is already unrestricted (documented in
  `networkpolicy.yaml`); gitea has no ingress policy. Ingress stays oauth2-proxy-only.

### Unchanged on purpose
The in-guest 10-min drain, `startup down=720` and cloudlab `cluster-power.sh` stay as the backstop
for the paths that do not go through the button (PVE UI, `poweroff`, the CLI). Moving the CLI onto
the same Gitea-level drain is a cloudlab follow-up, noted in the ADR.

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

## Verification
1. Unit tests (fake Gitea transport, in-memory state store, fake `shutdown_all`): schedule pauses only
   enabled cloud runners; failed PATCH rolls back; draining waits while a job is in progress or a
   runner is busy, needs two clear polls, keeps waiting on Gitea errors, re-disables a runner
   re-enabled mid-drain; deadline forces power-off; powering_off re-enables only offline runners,
   times out with an error; cancel/wake re-enable; restart resumes from persisted state; an
   operator-disabled runner is never touched.
2. `kubectl kustomize kubernetes/apps/apps/cloud-power` renders; the existing manifests CI passes.
3. Live, after Flux applies: `/api/status` answers through the dashboard with `schedule: idle`;
   the state CM is created; token scope check = `GET /orgs/cchifor/actions/runners` 200.
4. Live drain test at the next real OFF with a job in flight on a cloud runner: runner shows
   `disabled` in Gitea immediately, no new task lands on it, the job finishes green, hosts power off
   only afterwards, runners are re-enabled once offline, and the guest journal shows act_runner's
   stop with **no** `cancelled in progress jobs`. (Schedule-then-cancel is a zero-risk dry test
   of the pause/re-enable half during the day.)

<!-- codex-review-status: pending -->
