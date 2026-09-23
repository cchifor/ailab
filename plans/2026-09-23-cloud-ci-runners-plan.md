# Opportunistic Gitea CI runners on cloudlab (`cloud-ci-N`) — drain on shutdown, bounded auto-rerun

## Context

The Gitea Actions pool is wait-bound: 8 always-on `ci-runner-N` VMs on the ailab cluster serve every
`cchifor` repo through ONE label (`self-hosted-hv:host`, capacity 1 each), and PR checks queue FIFO
behind platform CI for minutes during the day (measured 2026-09-15: median wait 226 s, p90 1030 s;
job execution itself median 14 s, p90 31 s). The ailab hosts have no RAM left for more runners.
The cloudlab Proxmox cluster (cloud1 `.20`, cloud2 `.21`, cloud3 `.22`) is idle on CPU/RAM during
the day and is powered off by a human at night (Homepage OFF button or `cluster-power.sh down`),
waking on an RTC alarm at 08:00.

Goal: add runners on cloud1/cloud2/cloud3 that join the pool automatically when the hosts are up
and leave it when they are off, without disturbing ailab CI, and make sure a job caught on a cloud
runner at power-off is completed or re-run so the always-on ailab runners finish the validation.

This plan supersedes the draft `plans/2026-09-15-cloud-ci-runners-plan.md` (committed alongside for its §3 refutations) for the cloud
runner scope. Its GitHub-agent decommission (§6) and label rename (§7) are **out of scope** here;
the new runners register under the current label so they serve jobs from day one.

## Decisions taken with the user (2026-09-23)

| Decision | Choice |
|---|---|
| Guest type | QEMU VMs, same contract as ailab's runners (role applies unmodified) |
| cloud2 | User believed SVM was enabled; **live probe says NO** (`grep -c ' svm ' /proc/cpuinfo` = 0, no `/dev/kvm`, PVE 9.2.10 / kernel 6.14.11-9-pve). cloud2 VMs are a gated final phase; the gate is the probe passing |
| Retry policy | Cooperative drain for planned shutdowns + a bounded auto-rerun watchdog for the residual, **shadow mode first** |
| Ownership | cloudlab repo: tofu module + power scripts. ailab repo: ansible group/playbook, IPAM, monitoring, watchdog |

## Live-verified facts this plan rests on (2026-09-23, read-only probes)

- cloud1: `svm` present, `/dev/kvm` present, 125 GiB RAM / 121 available, 32 threads, LXC 5102 only.
- cloud3: KVM ok (per 2026-09-15), 251 GiB / 244 available, 64 threads, LXC 5101 only (`cloud-llm-3`, 160 GiB dedicated).
- pve-cluster storage: `local` (dir), `local-lvm`, `local-nvme` only — **no `import` datastore**. Every host already mounts `192.168.1.225:/pve-nfs` at `/mnt/qnap-nfs`, the SAME export ailab's `qnap-nfs` storage uses, so `import/noble-server-cloudimg-amd64-20260616.qcow2` is already on the share.
- Workstation `~/.ssh/id_ed25519` is **not** in root's `authorized_keys` on the cloud hosts. bpg's `ssh {}` block "defaults to the password used for the Proxmox API connection when using username/password authentication" — the existing cloudlab modules already use password auth, so the disk import works without a key.
- PVE node shutdown runs `pve-guests.service` → `pvesh create /nodes/localhost/stopall` with `TimeoutSec=infinity`; `stopall` uses `int($d->{down} // $param->{timeout} // 180)` per guest (`/usr/share/perl5/PVE/API2/Nodes.pm:2411`), `force-stop` default 1. So a per-VM `startup` `down=` value makes BOTH the OFF button and a plain `poweroff` wait for the runner to drain. In bpg the attribute is **`startup { down_delay = … }`** (the provider name for PVE's `down`).
- ailab `gitea_runner` role already drains: `shutdown_timeout: 10m`, `KillMode=mixed`, `TimeoutStopSec=11min` (ailab#745). The unit is `After=network-online.target docker.service`, so at shutdown systemd stops the runner **before** docker and the network. `cluster-power.sh` still hard-kills guests at 120 s and powers off 5 s later regardless (`cloudlab/scripts/cluster-power.sh:42-43,57`), and its transport (`timeout 90` around `node-ssh.py`, which itself has a 180 s silence timeout) cannot carry a 12-minute drain.
- Gitea 1.26.1 (chart 12.6.0; source release/v1.26): runner `status` is `offline` once `LastOnline` > 1 min old; zombie reaper fails an orphaned task after 10 min and **never re-queues**; `POST /repos/{o}/{r}/actions/runs/{id}/rerun-failed-jobs` → 201, requires `run.Status.IsDone()` (else 400), reruns jobs with status Failure OR Cancelled **and always their `needs` dependents**; job objects expose `runner_id`, `runner_name` (looked up live from `task.RunnerID` — a deleted runner yields empty), `run_attempt`, `conclusion`, `completed_at`; run objects expose `path`, `event`, `head_sha`, `head_branch`, `status`, `conclusion`, `completed_at` (no `updated_at`, no `ref`). **`head_branch` is EMPTY for `pull_request` runs** (their ref is `refs/pull/N/head`), so PR runs must be resolved through the PR API. `GET …/actions/runs` filters on `status`, `branch`, `event`, `head_sha`; every list is paginated (`page`, `limit`).
- Org runners list `GET /orgs/cchifor/actions/runners` needs `read:admin` + `read:organization`.
- In-cluster Gitea: headless Service `gitea-http.gitea.svc.cluster.local:3000`, pods `app.kubernetes.io/name=gitea`, `app.kubernetes.io/instance=gitea`; Prometheus pods `app.kubernetes.io/name=prometheus`, `app.kubernetes.io/instance=kube-prometheus-stack-prometheus`.
- Wake path: `cloud-rtc-wake.service` has `DefaultDependencies=no` without `Conflicts=shutdown.target`, so its `ExecStop` never runs at a real shutdown; only `cluster-power.sh down` arms the alarm today. The hook (`cloud-arm-rtc-wake.sh:45`) clears and recomputes the alarm from `CLOUD_WAKE_AT` in `/etc/default/cloud-power`.

## Architecture

**Join / leave is free.** act_runner registration is persistent (`.runner`, non-ephemeral). Gitea
only hands a task to a runner that polls and marks a runner `offline` 1 min after its last poll. A
cloud runner VM with `on_boot` + `Restart=always` is online a minute after the host boots and
offline a minute after it stops — nothing registers or deregisters at runtime, and **cloud runner
registrations are never deleted while a job of theirs may be within the watchdog's lookback**
(the API resolves `runner_name`/`runner_id` from the live runner row).

**Planned shutdown drains (three layers, all needed):**
1. In-guest (live): act_runner stops polling at SIGTERM and waits ≤ 10 min for the running job; systemd SIGKILLs at 11 min. Runner stops before docker/network.
2. Hypervisor: `startup { down_delay = 720 }` on every `cloud-ci-*` VM (> the guest's 660 s stop budget) so `pve-guests` stopall (OFF button, PVE UI, `poweroff`) waits for layer 1.
3. Script: `cluster-power.sh down` runs the drain+poweroff sequence **detached on the host** and polls; it never depends on a long SSH session.

**Residual (hard power loss, drain deadline exceeded, VM crash): the `ci-rerun-watchdog`** — an
always-on ailab in-cluster service that re-runs a run's failed jobs once when the failure is
correlated with the loss of the cloud runner it ran on and the run is still the current one for
its PR/branch. The re-queued jobs go to **any online runner with the label**; at the nightly
power-off that is necessarily an ailab runner, and if a single cloud host dies while the others are
up the job may land on another cloud runner (explicitly acceptable — it is up).

**Sizing — worst case is dedicated memory (balloons fully inflated), not the floor:**

| Host | Phase-1 VMs | vmid | IP | Worst case / physical | Spare |
|---|---|---|---|---|---|
| cloud1 | `cloud-ci-1`, `cloud-ci-2` | 6101, 6102 | `.32`, `.33` | 32 (exec-1) + 2×24 + 5 host = 85 / 125 | 40 GiB |
| cloud3 | `cloud-ci-3`, `cloud-ci-4` | 6103, 6104 | `.34`, `.35` | 160 (llm-3) + 2×24 + 5 = 213 / 251 | 38 GiB (page cache for model loads) |
| cloud3 5th (`cloud-ci-5`, 6105, `.30`) | gated | | | 237 / 251 | 14 GiB — **only after the V-sizing measurement** |
| cloud2 | `cloud-ci-6`, `cloud-ci-7` | 6106, 6107 | free pool then (`.5`–`.7`) | 48 (exec-2) + 2×24 + 5 = 101 / 125 | 24 GiB; a 3rd would reach 125 = full |

Phase 1 ships **4 runners (pool 8 → 12)**; the 5th and cloud2's pair are gated. Per VM identical
to ailab: 8 vCPU `host`, 24 GiB dedicated / 10 GiB floor, 200 GB thin on `local-lvm` (794 GiB
pools at ≤ 3 % used — check thin-pool **data and metadata** headroom with `lvs` before and after),
Ubuntu 24.04 cloud image from the shared export, cloud-init user `ubuntu` + the ailab control key,
`on_boot`, tags `["vm","ci","gitea-runner","cloud"]`. `.32`–`.34` were pencilled for the unbuilt
Talos GPU workers in cloudlab's spec; that spec is "NOT YET BUILT" and re-reserves elsewhere —
record that in the IPAM row.

---

## Phase 0 — cloudlab prerequisites (no runner code yet)

Repo `C:\Users\chifo\work\home\cloudlab` (Gitea `cchifor/cloudlab`, branch off `main`).

1. **Import datastore** — `scripts/converge-storage.sh`: after the `local-nvme` block, an idempotent
   `pvesm add nfs qnap-nfs --server 192.168.1.225 --export /pve-nfs --content import` (guarded by
   `pvesm status | grep -q '^qnap-nfs '`). **`--content import` ONLY** — never `images`/`rootdir`:
   this is the other estate's export. PVE mounts it a second time under `/mnt/pve/qnap-nfs`; the
   existing `/mnt/qnap-nfs` host mount is untouched. Cluster-wide, so run once; verify
   `pvesm list qnap-nfs --content import` shows the noble volid from every node.
2. **`cluster-power.sh down` honours the drain** — restructure, not just a bigger `--timeout`:
   - The remote body becomes `/usr/local/sbin/cloud-drain-poweroff` (installed by
     `provision-host.sh`), started as a transient unit: `systemd-run --unit=cloud-drain-poweroff
     /usr/local/sbin/cloud-drain-poweroff`. It: `qm shutdown $id --timeout 720` per VM,
     `pct shutdown $id --timeout 120` per CT, polls until no guest is `running` (cap 780 s, log
     stragglers + elapsed time to the journal), re-arms WoL, then `/sbin/poweroff`. The SSH call
     returns immediately (fits the 90 s / 180 s transport budgets).
   - The workstation side polls `qm list`/`pct list` and the host's reachability every 30 s with
     short SSH calls, prints per-host state, and exits non-zero if any host is still up after the
     total deadline (guests sequential worst case + margin = 15 min).
   - **Wake time:** `down HH:MM` writes `CLOUD_WAKE_AT=HH:MM` into `/etc/default/cloud-power`
     before the poweroff and the `ExecStop` hook arms the alarm — ONE authoritative mechanism, so
     the fixed hook (item 3) cannot overwrite a custom time. Verify a non-default time survives
     the shutdown and the boot (then restore 08:00).
3. **Wake path fix**: `Conflicts=shutdown.target` in `host/systemd/cloud-rtc-wake.service`, re-run
   `just provision-all`. (Dead hook since at least 2026-09-19; needed so the OFF button also arms
   the alarm — "auto-subscribe in the morning".)
4. `just power-down [HH:MM]` / `just power-status` recipes.

Exit: `pvesm status` lists `qnap-nfs` with `import`; V2 passes; `systemctl show cloud-rtc-wake -p Conflicts` includes `shutdown.target`; custom wake time survives.

## Phase 1 — ailab side (inert scaffolding; nothing scrapes or alerts yet)

Repo `ailab`, branch off `main`, push to `gitea`, PR + reviewbot.

1. **IPAM** — `docs/network-plan.md`: rows for `.32 .33 .34 .35` (`cloud-ci-1..4`, vmids 6101–6104,
   cluster `pve`, host), `.30` reserved for `cloud-ci-5` (gated), the Talos-GPU-worker note.
   **Must merge before the cloudlab apply.**
2. **`github_runner` role: `github_runner_agent_enabled` (default `true`)** guarding the App
   credentials assert (`tasks/main.yml:8`) and everything from "Download runner agent" (`:207`) to
   "Enable + start the runner service" (`:321`); the playbook's `github-runner.sops.yaml` pre_task
   gated the same way. Base toolchain (apt, guest agent, swap, Docker + buildx + compose,
   daemon.json, Node, uv, k6, `runner` user, `~/.docker` chown) stays. Fix the stale `:3` header.
   Existing runners unchanged: prove with `--check --diff -l ci-runner-1` → zero changes.
3. **Cleanup peer seam** — `gitea_runner_cleanup_peer_services` is a space-separated string and
   the script falls back through `${VAR:-…}`, so neither `[]` nor `""` means "no peers". Add
   `gitea_runner_cleanup_peers_enabled` (default `true`); when false the env template renders
   `GITEA_CLEANUP_PEER_SERVICES=none` and `gitea-runner-cleanup.sh` treats `none` as an empty
   list. Cover it in `tests/test-cleanup.sh` (keep role defaults literal — section M compares
   them verbatim).
4. **Inventory** — `inventory/hosts.yml`: group `cloud_gitea_runners` (NOT a child of
   `github_runners`/`gitea_runners`, so `just runners`/`just gitea-runners`/preflight never touch
   it) with `cloud-ci-1..4`, `ansible_host`, `ansible_user: ubuntu`, `ansible_become`.
   `group_vars/cloud_gitea_runners.yml`: `github_runner_enabled: true`,
   `github_runner_agent_enabled: false`, `gitea_runner_enabled: true`, `gitea_runner_label:
   self-hosted-hv`, `gitea_runner_cleanup_peers_enabled: false`, `node_exporter_extra_args`
   textfile dir (as `github_runners.yml:17`).
5. **Playbook** — `ansible/cloud-runners.yml`: hosts `cloud_gitea_runners`; pre_task loads only
   `secrets/gitea-runner.sops.yaml`; roles `host_time`, `node_exporter`, `github_runner`,
   `gitea_runner`. Recipes `just cloud-runners *args` / `just ping-cloud-runners` (argument
   forwarding so `--check`, `-l`, `--syntax-check` work).
6. **Preflight** — `scripts/check-ci-runners.py`: never add `cloud-ci-*` to `DEFAULT_RUNNERS`;
   add `--include-cloud` (informational online/offline/busy) and make the extra-runner warning
   ignore `^cloud-ci-\d+$`. Unit test in `scripts/tests/`.
7. **Monitoring objects without targets** — `kubernetes/apps/infrastructure/monitoring/ci-runners-cloud.yaml`:
   Services `ci-runner-cloud1/2/3` (label `cloud_host: cloudN`), their Endpoints with **empty
   `subsets`** (addresses land in Phase 2), one ServiceMonitor selecting
   `app.kubernetes.io/name: ci-runner-cloud`, relabelling `job=ci-runner-cloud`, `cloud_host` from
   the Service label, `nodename` from the endpoint hostname. `ci-runners-cloud-rules.yaml` +
   `.test.yaml` (promtool via `scripts/rules-lint.sh`):
   - `CloudCIRunnerDownWhileHostUp`: `up{job="ci-runner-cloud"} == 0 and on (cloud_host) (max by (cloud_host) (label_replace(up{job="cloud-node"}, "cloud_host", "$1", "nodename", "(.*)")) == 1)` for 10m — tests: fires with host up, silent with host down, silent with no target series.
   - `CloudCIRunnerDiskCritical`, `CloudCIRunnerCleanupStale`, gated the same way.
   With no addresses there are no `up` series, so nothing fires before the VMs exist.
8. **Docs** — runbook §9 "Cloud opportunistic runners"; ADR `0032-opportunistic-cloud-ci-runners.md`
   (VMs not LXC, same label, drain layers, bounded rerun with its narrowed guarantees, cloud2 gate);
   CLAUDE.md inventory row.

Exit: PR merged; `ansible-inventory --graph` shows the group; `just cloud-runners --syntax-check`
passes; `just runners --check -l ci-runner-1` shows zero changes; promtool tests green.

## Phase 2 — cloudlab tofu module + registration + activation

1. **`cloudlab/kubernetes/infra/ci-runners/`** cloned from ailab `kubernetes/infra/runners/`, deltas:
   - `providers.tf`: `username`/`password` auth (copy of `executor-lxc/providers.tf`), `ssh { agent = false; username = var.pve_ssh_username }`.
   - `versions.tf`: `bpg/proxmox ~> 0.113`; commit `.terraform.lock.hcl`.
   - **No `download_file` resource**: `import_from = "qnap-nfs:import/noble-server-cloudimg-amd64-20260616.qcow2"` (a managed download with `overwrite=false` ERRORS on an existing unmanaged file, and adopting it would put ailab's image under this state's lifecycle).
   - **`agent { enabled = false }`** at create (as ailab; `ignore_changes` keeps it) — the provider would otherwise wait for an agent the role installs later. No `guest-agent.tf` copy: nothing here needs the QEMU agent (ACPI shutdown → systemd → drain). Enable later by hand only if wanted.
   - **`startup { order = 3, down_delay = 720 }`**; verify with `qm config <vmid> | grep startup` → `down=720`.
   - drop `pool_id`; `network_prefix = 23`; keep `lifecycle { ignore_changes = [initialization, agent, disk[0].import_from] }`; `on_boot = true`; tags as above.
   - `runner_nodes`: the 4 entries; `cloud-ci-5` and cloud2's pair as commented blocks headed by their gates (V-sizing; SVM probe).
   - Header: why 61xx (no VMs on `pve`; distinct from ailab's 41xx), why /23, why no download resource.
2. `just ci-runners-plan` / `just ci-runners-apply` (Windows `~/.tofubin/tofu.exe`).
3. Apply → 4 VMs. `lvs` on cloud1/cloud3 for thin-pool data/metadata after.
4. From ailab: `just ping-cloud-runners` → `just cloud-runners` → registers `cloud-ci-1..4`
   (`self-hosted-hv:host`, capacity 1, non-ephemeral). Then `just cloud-runners --check` → zero changes.
5. Org runners API: all four `online`, one label each.
6. **Activation PR (ailab)**: add the four addresses to the `ci-runner-cloudN` Endpoints; before
   merging prove the alert join returns a **non-empty** vector against live Prometheus with one
   runner's daemon stopped and its host up (`port-forward svc/kube-prometheus-stack-prometheus
   9090`), then silent again.

Exit: V1–V6 and V-sizing pass.

## Phase 3 — `ci-rerun-watchdog` (ailab, in-cluster)

Always-on, Flux-managed, stdlib-only Python (the `cloud-power` shape: `app.py` from a hash-rolled
`configMapGenerator`, SOPS Secret, NetworkPolicy). Each gate answers a named refutation from the
09-15 plan §3.1 or a codex round-1 finding.

**Observations the watchdog keeps itself (every 60 s):** `runner_seen{runner_id} = {name,
last_online_ts, last_offline_ts}` from the org runners list (all pages). A runner whose status just
flipped to `offline` gets `lost_at = now`. This is the correlation signal — the API exposes no
`last_online`.

**Selection (every scan, fail closed on any API error or incomplete pagination):**

```
cloud = {runner_id: name | name =~ ^cloud-ci-\d+$}         # from the FULL runners list; unknown ids are never eligible
for repo in REPOS:
  for run in GET /repos/{repo}/actions/runs?status=failure   # paginate until completed_at < now - LOOKBACK (2 h)
    G1  run.status == completed AND run.conclusion == failure          # a run-level `cancelled` is NEVER a candidate
    G2  run.completed_at within LOOKBACK
    G3  (repo, run.id) has no tombstone (live or shadow ledger)        # one watchdog rerun per run within retention (90 d)
    G5  jobs = ALL pages of /runs/{id}/jobs
        every job with conclusion ∈ {failure, cancelled} must have runner_id ∈ cloud
        AND runner_seen[runner_id].lost_at is set AND job.completed_at ≥ lost_at − 3 min   # failure correlated with runner loss:
            zombie reap lands ~10 min AFTER loss; drain-deadline cancel ~0–1 min BEFORE; an hours-old genuine failure is excluded
        (a cancelled job that is NOT a lost-cloud job → skip: rerun-failed-jobs would replay it)
    G6  current-head gate, by event:
        pull_request → PR = the open PR whose head.sha == run.head_sha (GET /repos/{repo}/pulls?state=open, all pages); none → skip (closed or superseded)
        push / schedule / workflow_dispatch → GET /branches/{head_branch}.commit.id == run.head_sha; 404 → skip
        empty head_branch and no PR match (AGit, detached) → skip
    G7  no other run with the same `path` for the same sha/PR is non-terminal (waiting, blocked, queued, in_progress, running)
        and no run with the same `path` on the same PR/branch has id > run.id
    caps: MAX_RERUNS_PER_SCAN=3, MAX_RERUNS_PER_DAY=20 (live and shadow ledgers count separately); kill switch `disabled`
    DRY_RUN → record in the SHADOW ledger (same reservation/cap/restart semantics), log `would-rerun`, no POST
    live   → ledger.reserve(repo, run.id, sha, now); save                  # persist BEFORE the POST
             re-check G6+G7 immediately before the POST                    # shrink the race window
             POST /runs/{id}/rerun-failed-jobs → 201 confirmed | 400 rejected(not done) | other/timeout → uncertain
    post-check (next scan): a run for a NEWER sha of the same PR/branch that turned `cancelled` within 2 min of our POST
             → `collateral_cancel` (error metric + alert) and, once, rerun THAT newest run — it is the current head, so it is safe
    uncertain reservations: reconcile next scan — run no longer `failure` → confirmed; unchanged → one retry (max 2), then
             `unresolved` gauge + alert. Never claim "exactly one successful rerun".
```

What the gates buy: G5's correlation removes the "every failed PR of the day is rerun at 21:00"
population and the wedged-job-reaped-by-ENDLESS_TASK_TIMEOUT case (its runner is not lost). G6+G7
mean the concurrency group `<wf>-<ref>` has nothing live to cancel and nothing arriving; the
residual race between check and POST is seconds wide, re-checked, and compensated by the
post-check. `rerun-failed-jobs` replays failed/cancelled jobs and their dependents, so a cancelled
gate (`ci-gate`) is restored; G5's "no other cancelled jobs" keeps the replay set to the lost work.

**Ledger/state:** ConfigMap `ci-rerun-watchdog-state` in the app namespace, **created by the pod at
runtime, not in git** (Flux `prune: true` removes only objects in its inventory labels; survives
restarts; `kubectl patch`-able). Keys: `disabled`, `runner_seen` (JSON), `live` and `shadow`
ledgers (`{repo, run_id, sha, reserved_at, outcome}`), tombstones kept 90 d. `PUT` +
`resourceVersion` (409 → re-read, retry once; failure → skip this scan). RBAC: SA + Role
(`configmaps` create; get/update/patch scoped by `resourceNames`), `agentforge/rbac-flux.yaml`
shape; `automountServiceAccountToken: true` (the one deviation from cloud-power). Deleting the
namespace loses the ledger — bounded: one extra rerun per run inside the 2 h lookback, under caps.

**Repos:** `REPOS` env, default = `pr_reviewer_repos` (`ansible/roles/pr_reviewer/defaults/main.yml:28-37`:
ailab, agentforge, platform, agentforge-platform, dsh-team-conductor). Each scan also reads
`GET /orgs/cchifor/repos` and exports `ci_rerun_watchdog_unlisted_repos` so the gap is visible.

**Token:** in the gitea pod, ONCE:
`gitea admin user generate-access-token --raw -u chifor -t ci-rerun-watchdog --scopes read:admin,read:organization,write:repository`.
Secret `kubernetes/apps/apps/ci-rerun-watchdog/secret.sops.yaml` (generic `.sops.yaml` rule).

**Runtime:** namespace `ci-rerun-watchdog` (PSS restricted), Deployment `replicas: 1`,
`strategy: Recreate` (single ledger writer), `mirror.gcr.io/library/python:3.14-slim`, non-root,
read-only rootfs, `cpu 10m / mem 32Mi` (limit 128Mi). `GITEA_URL=http://gitea-http.gitea.svc.cluster.local:3000`,
`User-Agent: git/2.47.0` kept. Env: `GITEA_ORG`, `REPOS`, `SCAN_INTERVAL=60`,
`LOOKBACK_SECONDS=7200`, `LOSS_CORRELATION_SECONDS=180`, `MAX_RERUNS_PER_SCAN=3`,
`MAX_RERUNS_PER_DAY=20`, `DRY_RUN=true`, `STATE_CONFIGMAP`, `PORT=8128`. `/healthz` + `/metrics`
(hand-rendered like `reviewbot.py write_metrics`): `ci_rerun_watchdog_reruns_total{repo,mode}`,
`_candidates{repo}` (unique per scan), `_skipped_total{repo,reason}`,
`_last_scan_timestamp_seconds` (only on a fully successful scan), `_errors_total{stage}`,
`_collateral_cancel_total`, `_unresolved`, `_dry_run`, `_disabled`, `_reruns_last_24h{mode}`,
`_unlisted_repos`. Service + ServiceMonitor. NetworkPolicy: default-deny; ingress Prometheus
:8128; egress kube-dns, gitea pods :3000, API server (`dsh/networkpolicy.yaml` shape). **No
internet egress.**

**Alerts** (`prometheusrule.yaml` + `.test.yaml`, both directions): `CIRerunWatchdogScanStale`
(> 600 s, 5m), `CIRerunWatchdogMissing` (`absent`, 15m), `CIRerunWatchdogRerunStorm`
(`sum(increase(reruns_total{mode="live"}[1h])) > 5`, critical), `CIRerunWatchdogErrors`
(`increase(errors_total[30m]) > 3`, 10m), `CIRerunWatchdogCollateralCancel` (any, critical),
`CIRerunWatchdogUnresolved` (> 0 for 15m).

**Logs:** one grep-able line per decision (`rerun …`/`would-rerun …`/`skip … reason=…`). No PR comment.

**Tests:** `scripts/tests/test_ci_rerun_watchdog.py` (stdlib unittest, `importlib` load, injected
`api`/`state` fakes) covering: PR run resolved via the PR API (open PR head match), PR closed,
branch moved, branch gone, superseded, non-terminal sibling run, run-level cancelled never selected,
unrelated cancelled job in the run → skip, mixed `ci-runner-N` failure → skip, runner online →
skip, runner never observed → skip, hours-old genuine failure + later shutdown → skip, zombie-reap
timing (+10 min) → candidate, drain-cancel timing (−1 min) → candidate, page-two failure found,
pagination error → zero reruns, unknown runner_id → skip, dedupe across scans (live and shadow),
tombstone survives after lookback, both caps, `disabled`, DRY_RUN issues no POST but reserves in
the shadow ledger, reserve-before-POST ordering, 400 → rejected, timeout → uncertain → reconciled,
collateral-cancel detection. Runs in the existing `broker-inventory.yaml` unittest discovery;
`manifests.yaml` already builds every `kubernetes/apps/apps/*` kustomization.

**Files:** `kubernetes/apps/apps/ci-rerun-watchdog/{namespace,deployment,rbac,networkpolicy,servicemonitor,prometheusrule,prometheusrule.test,secret.sops,kustomization}.yaml` + `app.py`;
`kubernetes/apps/apps/kustomization.yaml` entry; `scripts/tests/test_ci_rerun_watchdog.py`; runbook §9 kill switch.

**Rollout:** ship `DRY_RUN=true`; shadow week reading `reruns_total{mode="dry_run"}`,
`_candidates` and `_skipped_total` by reason, hand-verifying every `would-rerun`; V7; PR flipping
`DRY_RUN=false`; V8. Instant stop without a PR:
`kubectl --context admin@ai -n ci-rerun-watchdog patch cm ci-rerun-watchdog-state -p '{"data":{"disabled":"true"}}'`.

**Stated limits:** latency ≈ 10 min zombie timeout + ≤ 60 s for hard power loss (near-immediate
for a drain-deadline cancel); a genuine failure inside the 3-min loss-correlation window is re-run
once; the rerun lands on any online labelled runner (ailab at full power-off); the check→POST race
is shrunk and compensated, not eliminated; `run_attempt` semantics are unverified and unused.

## Phase 4 — gated additions

- **`cloud-ci-5` on cloud3**: only after V-sizing shows ≥ 30 GiB MemAvailable on cloud3 with
  two heavy platform jobs running concurrently while `cloud-llm-3` has its default model loaded, and
  model-load latency within the runbook baseline.
- **cloud2**: gate = `grep -c ' svm ' /proc/cpuinfo` > 0 **and** `test -c /dev/kvm` on cloud2.
  The 2026-09-23 probe shows it off → BIOS visit (TRX40 "SVM Mode" under Advanced → CPU
  Configuration; confirm the save before power-off). Then: two IPs from the free pool (IPAM row
  first), uncomment the cloud2 block, apply, `just cloud-runners -l cloud-ci-6,cloud-ci-7`,
  Endpoints addresses. Pool → 14 (15 with `cloud-ci-5`).

---

## Verification (end to end)

V0 **Baseline first**: 7 days before Phase 2 — p50/p90 queue wait and jobs/day via a kept
`scripts/ci-queue-stats.py`, zero-log-line failures/week.

V1 Drain in-guest: a scratch workflow that runs `docker run --rm alpine sleep 300` **and** resolves
a DNS name (exercises docker + network ordering), on a cloud runner; `systemctl stop
gitea-act-runner` → job completes green.

V1b **Drain deadline exceeded**: same with `sleep 900`; graceful stop → record the job's and run's
actual `status`/`conclusion` (expected `failure`, "Early termination"). If it is `cancelled`, G1/G5
are adjusted to admit a cancelled job **only** when it is a lost-cloud job (G6 still excludes
routine cancel-in-progress, which implies a newer head) — decided on evidence, not assumed.

V2 Drain via `cluster-power.sh down` on ONE host with V1's job in flight → host waits, job green,
poweroff after; run it before the fix too, to prove the test has teeth.

V3 Drain via the Homepage OFF button with a job in flight → same (proves `down_delay` is honoured
by `pve-guests`).

V-sizing Two heavy platform e2e jobs pinned on the two cloud3 runners while `cloud-llm-3` has its
model loaded: MemAvailable, swap, `lvs` thin data/metadata, model-load latency vs baseline.

V4 Attach: power-cycle a cloud host → `cloud-ci-*` `online` in the org API within ~2 min, unattended.

V5 Carry: push a branch → a job lands on `cloud-ci-N` (`runner_name`) and goes green.

V6 Detach: power the cluster off → runners `offline`, ailab CI keeps flowing, no alert fires
(meaningful only after the rules' fires-when-host-up test passed).

V7 Residual + watchdog (shadow): `sleep 1200` job on a cloud runner, `qm stop` the VM (hard) →
Gitea fails the task ~10 min later → `would-rerun run=<id>` with all gates satisfied, shadow ledger
entry, no POST. Repeat with (a) a superseding push to the same PR → `skip:superseded`; (b) a PR
opened 3 h earlier that failed genuinely on a cloud runner, then a normal shutdown → `skip`
(loss-correlation); (c) a `pull_request`-event run AND a push-event run, both recovered.

V8 Watchdog live (after the shadow week): V7(c) with `DRY_RUN=false` → 201, jobs re-queued, picked
by an ailab runner, check green, the reviewbot merges the fixture PR. Also a partial-cloud failure
(stop only cloud1) → rerun lands on any online runner; documented as acceptable.

V9 Steady state: a week against V0 — lower p90 wait, zero manual reruns, no nightly pages, no
`collateral_cancel`.

## Rollback

| To undo | Steps |
|---|---|
| One cloud runner | `systemctl stop` (drains), `tofu destroy -target`, wait > LOOKBACK, then `DELETE /api/v1/orgs/cchifor/actions/runners/<id>` (else it lingers offline), drop its Endpoints address + IPAM row |
| The tier | above ×N, remove `ci-runners-cloud*.yaml`, the inventory group, playbook, release the IPs |
| Watchdog | `DRY_RUN=true` PR, or the `disabled` patch (≤ 60 s), or scale to 0 |
| Phase 0 script/storage | revert the commit; `pvesm remove qnap-nfs` (import-only storage, no volumes) |

## Review trail — codex round 1 (gpt-6-astra, xhigh, 2026-09-23, native seat; LiteLLM route 429'd twice)

Verdict was "request changes before Phase 2". Disposition of the 19 findings:

**Accepted (17):** (1) `startup.down_delay`, 720 > guest 660 · (2) agent disabled at create, no guest-agent workaround needed · (3) reference the volid, no `download_file` · (4) detached drain unit + workstation polling; transport budgets untouched · (5) PR runs resolved via the PR API (`head_branch` is empty for `pull_request`) · (6) all non-terminal statuses count as live, re-check before POST, post-check + compensation for collateral cancel · (7) V1b measures the real drain-deadline conclusion; docker+DNS in the fixture · (8) loss-correlation on the watchdog's own runner observations, lookback 2 h · (9) cancelled jobs in the run must all be lost-cloud jobs (fixtures added) · (10) full pagination, classify by `runner_id`, unknown ids never eligible · (11) reserved/confirmed/rejected/uncertain outcomes, reconciliation, `unresolved` alert · (12) 90-day tombstones, guarantee narrowed to "once per run within retention" · (13) shadow ledger with identical semantics · (15) one authoritative wake-time mechanism · (16) sizing corrected (cloud3 worst case 237 GiB → 4 runners in phase 1, 5th gated on measurement; cloud2 two not three) · (17) Endpoints ship empty, addresses land with the VMs · (18) explicit `none` peer sentinel + test · (19) recipe args, realistic exit criteria.

**Narrowed rather than adopted (2):** (14) "recovery must run on ailab" — enforcing that needs a second label/routing change across every workflow; the requirement is met at the nightly power-off (all cloud runners are down) and a single-host failure landing on another live cloud runner is acceptable and now stated + tested. (9's allowlist) — an explicit workflow allowlist was not adopted; the "no other cancelled/failed jobs" rule bounds the replay set without a second config surface.

Round 2 is deferred to the PR stage: the `reviewer-codex` persona reviews every PR on open and on
each push (seat-independent), and the plan's remaining judgment calls (14, 9) are recorded here.

<!-- codex-review-status: finalized -->
