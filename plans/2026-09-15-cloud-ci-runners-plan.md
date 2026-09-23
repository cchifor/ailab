# Gitea-only CI: decommission the GitHub/Hyper-V pool, add opportunistic runners on cloudlab

**Date:** 2026-09-15 · **Status:** PROPOSED (nothing executed)
**Repos:** `ailab` (roles, inventory, monitoring, docs) + `cloudlab` (VM provisioning, power)
**Supersedes** the first draft of this file, which assumed a drain-plus-watchdog design that the
research below refutes.

---

## 1. Why

The Gitea Actions pool is **saturated**. Verified live 2026-09-15: 9 online runners, **8-9 busy** at
every sample. Measured queue wait across 130 recent jobs: **median 226 s, p90 1030 s**, 56 of 130
waiting over five minutes. Job execution itself is fast — **median 14 s, p90 31 s, max 127 s** — so
the pool is wait-bound, not compute-bound.

The ailab hosts cannot take another runner: all three PVE hosts sit at **100-105 GiB used of 124
GiB**, so every runner VM is pinned at its 10 GiB balloon floor and sees ~9.7 GiB. `ci-runner-8` was
*retired* on 2026-09-12 to give RAM back to a model.

cloudlab is idle on CPU/RAM and already powers off nightly:

| Host | CPU | RAM total / avail | Existing guest | Verdict |
|---|---|---|---|---|
| cloud1 `.20` | 3950X, 32T | 125.70 / 121.81 GiB | LXC 5102 `cloud-exec-1` (32 GiB) | 2 runners |
| cloud2 `.21` | 3990X, **128T** | 125.62 / 106.13 GiB | LXC 5103 `cloud-exec-2` (**48 GiB live**) | **BLOCKED — see §5** |
| cloud3 `.22` | 3970X, 64T | 251.55 / 244.75 GiB | LXC 5101 `cloud-llm-3` | 3 runners |

---

## 2. Scope change: this is a Gitea-only estate

The previous draft inherited GitHub scaffolding. That is now explicitly out. Two corrections to the
repo's own documentation came out of this research and both matter:

> **The GitHub Actions agent is LIVE on all nine runner VMs.** `actions.runner.cchifor-platform.service`
> is `enabled` **and** `active` on `.14 .15 .16 .17 .18 .19 .29 .31 .23`, each holding an ESTABLISHED
> TLS session to GitHub and refreshing its OAuth token every ~50 min.
>
> `CLAUDE.md` ("GitHub Actions dormant"), `docs/runbooks/ci-runners.md` §7 and `inventory/hosts.yml`
> (both claiming ci-runner-3's agent was "RETIRED 2026-07-09") and `ci-runners-rules.yaml`'s header
> are all **false**. This is a decommission of nine live agents, not a paperwork exercise.

The saving grace: they are connected but **idle**. Zero `Worker_*.log` files across all nine; the
newest artefact in every `_work` tree is a runner self-update from 2026-09-02. Nothing is executing,
so removal is safe from a workload standpoint.

**`self-hosted-hv` is itself the Hyper-V remnant** — the label. Renaming it is §7, and it is the
single most dangerous change in this plan.

---

## 3. Fault tolerance — drain, not abandon-and-retry

### 3.1 The requirement, and why the obvious implementation is wrong

The stated goal was: *stop the cloud machines anytime; in-flight tasks are abandoned and **retried**
by the always-on ailab runners.* I designed that watchdog, then had three independent agents attack
it against the Gitea v1.26.1 source and the live estate. **All three refuted it.** The findings are
specific enough to act on:

1. **The premise is wrong — a graceful stop produces no zombie at all.** act_runner v0.6.1's
   `daemon.go` does `ctx, _ := context.WithTimeout(ctx, cfg.Runner.ShutdownTimeout); poller.Shutdown(ctx)`.
   `shutdown_timeout` is **unset in the live config and has no compiled default**, so it is `0`, the
   context is already expired, and the job is cancelled at once. `reporter.Close()` then reports
   `RESULT_FAILURE` ("Early termination") **with retry**. So today a stop yields a *promptly
   reported failure*, indistinguishable from a real one — never the 10-minute zombie fingerprint the
   watchdog keys on. The watchdog would not fire on the scenario it exists for.

2. **Auto-rerun can cancel live CI on `main`.** All three required ailab workflows carry
   `concurrency: {group: <wf>-${{ github.ref }}, cancel-in-progress: true}`
   (`manifests.yaml:40-42`, `rules-lint.yaml:39-41`, `broker-inventory.yaml:47-49`). A rerun
   re-enters the old run into a live group. Either the rerun is cancelled on arrival — permanently
   red, silently, while the watchdog's metric says "rerun issued" — or it cancels the in-flight run
   on the current head. This fires today: runs 34579/34580/34581 on `refs/heads/main` are `cancelled`
   with `started_at 1970-01-01`.

3. **Per-job rerun cannot restore platform's gate.** `prepareRunRerun` refuses with HTTP 400 unless
   `run.Status.IsDone()`, and a job rerun sets the run back to waiting — so two dead jobs in one run
   can never both be rerun. platform's `ci-gate` (`needs: [backend, postgres, frontend, contract,
   e2e]`, sole required check) treats `cancelled` as a hard failure and would re-read the other dead
   job's stale `failure`.

4. **Temporal detection sits inside the noise floor of genuine failures.** Over the 275 most recent
   failed jobs, **21 of 237 have >60 s** between last step boundary and completion and **14 exceed
   540 s** — e.g. 223194 `ci-green` (901 s), 194842 `build-push` (934 s). Any gap-based rule
   misclassifies real test failures.

5. **Lowering `ZOMBIE_TASK_TIMEOUT` to shorten detection is estate-dangerous.** It is **global**:
   `stopTasks()` reaps every task whose `action_task.updated` is stale, and that column is bumped
   only by a gRPC write landing in `infra-pg`. This estate has a documented infra-pg stall RCA
   (2026-08-05). A >2 min stall would mass-reap every live job on all nine busy runners.

Supporting corrections: `/api/v1/admin/*` is gated by `reqSiteAdmin()`, not merely the `read:admin`
scope; `ENDLESS_TASK_TIMEOUT` (3 h) reaps produce an **identical** artefact to a zombie reap, so a
wedged job would be rerun repeatedly; `cancelled` must never be a rerun candidate (38,737 such jobs
exist, because `cancel-in-progress` is routine); and Flux (`prune: true`, 10 min) would drift-correct
any ConfigMap holding watchdog state.

### 3.2 What to do instead

**Make the shutdown cooperative.** Set `shutdown_timeout` and act_runner *waits for the running job
instead of cancelling it.* Given median job duration of **14 s** and p90 of **31 s**, a drain
completes in seconds. That delivers the requirement more directly than any retry could:

> Stop the cloud machines whenever you like. The runner finishes its current job — typically within
> seconds — then exits. Nothing is abandoned, so nothing needs retrying.

Three layers, in order of how much they carry:

**D1 — cooperative drain (the whole answer for graceful stops).**
- `gitea_runner_shutdown_timeout: 10m` rendered into `config.yaml`; `TimeoutStopSec=11min` in the
  unit (today it is 5 min with `KillMode=control-group`).
- PVE guest budget to match: `qm set <vmid> --startup down=720`.
- **`cluster-power.sh` must be fixed in the same change.** Today `:42-43` give each guest
  `--timeout 120` and `:57` powers the host off **5 s later regardless of whether guests stopped**.
  A 10-minute in-guest grace is a lie until that is raised and the final `poweroff` is made
  conditional on the guests actually being down.
- This is a **live bug on the ailab pool too** — any `systemctl stop`, reboot or maintenance today
  cancels the running job. Fix it estate-wide first; it stands alone.

**D2 — drain before shutdown (bounds the wait).**
`cloudlab/scripts/ci-drain.sh` stops each cloud runner's daemon when idle, then `qm shutdown`. Gate
on the **Gitea API `busy` field** for the authoritative signal, *not* the `gitea-runner-cleanup.sh`
idle-gate — that one treats ~3 s of quiet as idle, which is right for pruning and wrong for
shutdown. Note `busy` is a 10-second liveness window on job-progress RPCs, not a job-ownership flag;
confirm with the process-tree check as a secondary signal.

**D3 — detect and ALERT on the residual, do not auto-rerun.**
Hard power loss still orphans a job. Ship a Prometheus alert that flags a job failed on a cloud
runner with the zombie fingerprint so a human reruns it. Alert-only is the honest boundary: a
component that can silently re-run CI is not worth 3-5 at-risk 14-second jobs per power-off.

**Revisit auto-rerun only if measured loss justifies it** (suggested threshold: >5 abandoned tasks
per week against the V0 baseline) — and only with a staleness gate (newer run for the same
workflow+ref, head_sha still current, PR still open), `rerun-failed-jobs` rather than per-job rerun,
Lease-based leader election, runtime-created state, and `{failure}` as the sole candidate conclusion.
Every one of those is a finding above, not speculation.

*Variant considered and rejected:* gating reruns on `runner_id` / runner name, so only jobs from a
known cloud runner are eligible. It sounds like it should narrow the blast radius, and it does — but
it addresses neither decisive refutation. A graceful stop still produces an ordinary failure rather
than a zombie (1), so there is nothing to match on; and `cancel-in-progress` still makes the rerun
itself hazardous (2). It also fails open in a plausible nightly case: a destroyed or deregistered
cloud runner no longer appears in the runners listing at all, so `runner_name` resolves to nothing
and the gate cannot classify the job either way.

---

## 4. Cloud runner specification

Deliberately identical to the proven ailab contract so `ansible/roles/gitea_runner` applies
unmodified.

### 4.1 Per-runner

| Property | Value | Why |
|---|---|---|
| Guest type | QEMU VM (not LXC) | host-mode CI Docker is root-equivalent; an LXC escape lands on a host running 4× RTX 3090 + vLLM. Also the role's cgroup caps and cleanup scripts are validated on Ubuntu VMs |
| Image | `qnap-nfs:import/noble-server-cloudimg-amd64-20260616.qcow2` | **already present** on the QNAP export and visible from every cloud host — byte-identical to ailab's runners |
| vCPU | 8 (1 socket) | matches ailab |
| RAM | 24 GiB ceiling / **10 GiB balloon floor** | matches ailab; floor prevents the platform#620 OOM class |
| Root disk | 200 GB thin on `local-lvm` | build cache is the known filler; pools are 794 GiB at 0.6-2.7 % used |
| act_runner | v0.6.1, **`capacity: 1`** | act_runner reuses one workspace per repo-hash; two concurrent same-repo jobs collide. Scale by VM count, never capacity |
| Registration | **NON-ephemeral** | an ephemeral runner self-deletes on reap (`UpdateTask` → `DeleteEphemeralRunner`), destroying the audit trail |
| `onboot` | `1` | rejoins automatically after the 08:00 RTC wake |
| `shutdown_timeout` | `10m` (§3.2 D1) | cooperative drain |

### 4.2 Fleet

| Host | vmid | Name | IP | Count | Floor budget | Worst case (balloons full) |
|---|---|---|---|---|---|---|
| cloud1 | 6101-6102 | `cloud-ci-1/2` | `.32` `.33` | **2** | 32 + 20 + 5 = 57 / 125.70 GiB | 85 GiB → 40.7 spare |
| cloud3 | 6103-6105 | `cloud-ci-3/4/5` | `.34` `.35` `.30` | **3** | 5 + ~20 + 30 = 55 / 251.55 GiB | 97 GiB → ~154 for page cache |
| cloud2 | *(reserved)* | `cloud-ci-6/7/8` | *(none free)* | **0 — blocked** | — | see §5 |

**Phase 1 ships 5 runners**, taking the pool 9 → 14 (**+56 %**). cloud2's three are a commented,
undeclared reserve in `variables.tf` — the same pattern ailab used for `ci-runner-6`, with the guard
keyed to the SVM fix.

Naming is `cloud-ci-N`, matching cloudlab's `cloud-exec-N` / `cloud-llm-3`, **not** `ci-runner-N`.
vmids come from a visually distinct `61xx` block (the `pve` cluster has no VMs at all today). Both
choices exist because this LAN has already produced two cross-cluster identity incidents.

### 4.3 cloud3 page-cache caveat

cloud3 shows **147 GiB of buff/cache**, which llama-swap relies on for on-demand model loading. That
cache is *reclaimable*, so runner VMs cannot OOM the LLM — the cost is **cold-load latency**, since
an evicted cache means re-reading 72-84 GiB. Three runners still leave ~154 GiB for cache, more than
the largest model. Measure model-load latency against the runbook baseline before adding a fourth.

---

## 5. Blockers found (all verified live)

| # | Blocker | Fix | Severity |
|---|---|---|---|
| **B1** | **cloud2 has AMD-V/SVM disabled in BIOS — no QEMU VM can start there.** Confirmed five ways: no `svm` flag in `/proc/cpuinfo` (cloud1/3 have it), no `kvm` module, no `/dev/kvm`, no `Virtualization:` line from lscpu, and PVE reports `"hvm":""` vs `"hvm":"1"` on the others | **Physical visit** — there is no BMC/IPMI on any cloud node. Enable `SVM Mode`; the TRX40 board defaults it off and the box was rebuilt 2026-09-04 | **HIGH** — costs the best CI host in either estate (112 free threads) |
| **B2** | No datastore in the `pve` cluster has the `import` content type | `pvesm add nfs qnap-nfs --server 192.168.1.225 --export /pve-nfs --content import` — **`import` ONLY**, never `images`/`rootdir`, since this is the other estate's shared export | MEDIUM (cluster-wide storage edit) |
| **B3** | Root SSH **key** auth from the workstation fails on all three cloud hosts; bpg needs node SSH for the disk import | `ssh { password = … }` (how the existing cloudlab modules already work), or install the workstation key into `/etc/pve/priv/authorized_keys` | MEDIUM — silent until apply time |
| **B4** | `executor-lxc` has real state drift: `ct_memory_mib` defaults to 32768 but LXC 5103 is live at **48 GiB** | Reconcile **before** anyone runs tofu on this cluster | **HIGH** — an unrelated apply would live-shrink 48→32 GiB under a running vLLM TP=4 engine |

### Port deltas from ailab's `runners` module

- `network_prefix` **23**, not 24 — the cloud LAN is `192.168.0.0/23`. Shipping /24 gives guests a
  wrong netmask and half the segment becomes unreachable.
- Drop `pool_id = "ailab"` — the `pve` cluster has no resource pools.
- Pin `bpg/proxmox ~> 0.113` (ailab's proven version), not cloudlab's `~> 0.109`.
- Keep `lifecycle { ignore_changes = [initialization, agent, disk[0].import_from] }` verbatim.
- Using the `import_from` volid directly removes the `download_file` resource entirely.

---

## 6. GitHub / Hyper-V decommission

**The role is not deletable — it must be split.** `ansible/roles/github_runner` (11 files, 703 lines)
installs the *entire base toolchain*: apt base, qemu-guest-agent, the 8 GiB swapfile, Docker +
buildx + pinned Compose 2.31.0, `daemon.json` (including the `registry.chifor.me` pull-through mirror
and BuildKit GC caps), Node 20, `uv`, `k6`, and the `runner` user. `gitea_runner` installs none of it
and hard-fails its `docker --version` assert without it.

→ Extract sections 1-4 + `swap.yml` + `daemon.json.j2` into a new **`runner_common`** role (ADR 0017
already names this follow-up). Delete only the GitHub-specific sections 5-7.

### The five traps

| Trap | Consequence if done naively |
|---|---|
| `inventory/hosts.yml`: the `github_runners` group is the **only** place any runner's `ansible_host` is declared; `gitea_runners` entries are bare `ci-runner-N: {}` | Deleting the group breaks `just gitea-runners` outright — ansible falls back to DNS. **Rename in place, never delete** |
| `group_vars/github_runners.yml` carries `node_exporter_extra_args: --collector.textfile.directory=…`, and `ansible/runners.yml` is the **only** playbook applying `node_exporter` + `host_time` to these VMs | Deleting either blinds **every** gitea beacon alert — the exact absent-series trap those rules exist to close. The key must be **moved into the renamed group's `group_vars` in the same commit**, not merely left behind — a rename that lands without it opens a window where the collector is unflagged |
| `runner-reclaim.sh` (github_runner) is the **only** code that chown-heals `/home/runner/.docker` | Removing it drops the buildx-permission self-heal that ADR 0013 documents. **Move it into `runner_common` and call it from there** — do *not* copy it into `gitea_runner`, which would create two divergent copies of a script both tiers need. Repoint `CIRunnerDockerConfigRootOwned` at a new gitea beacon |
| Stale `runner_health.prom` files on all nine hosts (frozen since Sep 11; ci-runner-1 still serves a 4-day-old `runner_docker_build_cache_bytes 26020000000`) | A textfile `.prom` is never GC'd — Prometheus serves dead gauges forever. **Delete the files** during agent removal |
| `actions/checkout@v7` and other `uses:` resolve from **github.com**, which is why `gitea_runner` ships `/etc/gai.conf` IPv4-precedence and the resilient-DNS drop-in | **Explicitly out of scope.** This is not a GitHub-Actions remnant; a Gitea-only estate still needs it. Removing it reintroduces the documented IPv6 hang |

The peer-coordination knobs (`gitea_runner_cleanup_peer_services`, `_peer_job_process_re`) are **safe
to remove in either order** — an empty list runs zero loop iterations, and a pointer to a removed
unit reads `inactive` and is skipped. Removing them is a net *improvement*: the `pgrep -f
"Runner\.Worker"` probe is host-global and can be self-matched by any process with that string in its
cmdline, which is the 2026-08-08 starvation failure mode.

### Order of operations (the traps interlock)

The five traps are not independent, and doing them in the wrong order opens windows where the
playbooks do not run at all. Per runner VM, idle-gated, **one at a time**:

1. **Split first, while everything still works.** Create `runner_common` (base toolchain + swap +
   `daemon.json` + `runner-reclaim.sh`), rename the `github_runners` group **in place**, and move
   `node_exporter_extra_args` into the renamed group's `group_vars`. Apply and confirm
   `just gitea-runners` still runs. Nothing has been removed yet, so this step is reversible.
2. **Port the beacon.** Emit the `~/.docker` heal as a `gitea_runner_*` gauge and repoint
   `CIRunnerDockerConfigRootOwned`. Confirm the new series exists in Prometheus **before** the old
   writer goes away.
3. **Converge the runner generations.** Re-apply the role so runners 1-5 gain `TimeoutStartSec=10min`
   and the single-pass reclaim script — they currently lack the pair that fixed the 2026-07-28
   quadratic-reclaim outage, so this is a latent fleet-wide exposure being closed on the way past.
4. **Stop + disable the agent**, gated on **both** tiers being idle — `pgrep -P` against the MainPID
   of `gitea-act-runner.service` *and* of `actions.runner.cchifor-platform.service`. (The GitHub side
   is provably idle — zero Worker logs fleet-wide — so this is belt-and-braces, but it costs nothing.)
5. **Delete the stale `runner_health.prom`** — only now, after step 2 has a replacement series.
6. **Remove the GitHub-only role sections**, the peer knobs and their tests.

**Credentials:** deleting `github-runner.sops.yaml` does **not** revoke anything. Revoke the App key
on github.com and purge OpenBao `af/estate/github` first, delete the file second.

**ADRs are appended, never rewritten** (0013 gets a superseded-by note; 0017 flips PROPOSED →
ACCEPTED+DEPLOYED, two months stale). `plans/` is historical and stays untouched per CLAUDE.md.

---

## 7. Label rename (`self-hosted-hv` → neutral)

**Good news:** every `runs-on:` in all 9 repos resolves through `vars.RUNNER_LABEL` /
`vars.STATIC_RUNNER_LABEL` — **zero hardcoded `runs-on` anywhere** (50 workflow files: platform 74,
agentforge 18, agentforge-platform 16, ailab 7, cloudlab 1). Both variables are **org-scoped only**;
no repo shadows them. So the consumer cut is a single atomic two-variable flip.

**Better news:** act_runner **re-declares its label set from `config.yaml` on every restart** — no
re-registration, no `.runner` deletion. (This corrects an earlier assumption that labels are frozen
at registration.)

**The danger:** branch protection is keyed on *workflow/job name strings*, not labels — and **Gitea
queues a job with no matching runner indefinitely rather than failing it.** Flip the org vars before
every runner carries the new label and required checks never report at all: PRs become unmergeable
with a silent pending and no red signal.

| Phase | Action | Reversible |
|---|---|---|
| L1 | Dual-label all runners (`self-hosted-hv:host` **+** `<new>:host`), serial, each gated on `busy=false`. Verify all report both via the org API | yes, invisible to CI |
| L2 | Flip `RUNNER_LABEL` + `STATIC_RUNNER_LABEL` together. Re-check the runners API immediately before | yes |
| L3 | Migrate the 4 fallback literals, `check-ci-runners.py` + tests, role defaults, the gated ScaledJob, docs | inert while L2 holds |
| L4 | Drop the old label; delete the stale offline `ci-runner-8` registration (id 18) | **point of no return** |

New cloud runners register under the **new name from day one** — never inherit `self-hosted-hv`.

> ### L0 — the blocking gate (do this before L1, not as an afterthought)
>
> The entire dual-label path rests on one unproven assumption: **that Gitea matches `runs-on: X`
> against a runner whose label set is a *superset* of `{X}`.** It is standard semantics and
> consistent with everything observed, but it was not empirically tested — testing it requires
> queueing a job, which is a write.
>
> If it is false (Gitea does strict set equality), then at L2 every newly-queued job matches
> **nothing**, and because Gitea queues an unmatched job indefinitely rather than failing it, every
> required check across platform, agentforge and agentforge-platform silently stops reporting. PRs
> become unmergeable with no red signal — only a pending that never resolves.
>
> **Procedure:** dual-label exactly **one** idle runner → confirm both labels via the org runners API
> → push a scratch branch and queue a throwaway job against the **old** label → assert it still lands
> on that runner → queue one against the **new** label → assert the same.
> **Abort condition:** if either job does not route, revert that runner's `config.yaml`, restart, and
> stop. Do not proceed to L1.

Naming note: on GitHub Actions `self-hosted` is a reserved implicit label; on Gitea act_runner adds
no implicit labels. `self-hosted-ci` or `ailab-host` keeps the ability to distinguish *this pool*
from *any* self-hosted runner.

---

## 8. Regressions to avoid

1. **Nightly `NodeExporterDown`.** Do **not** add cloud runners to the `ci-runner-node` Endpoints —
   that file already carries a comment about a dead address firing forever. Use a separate
   `ci-runner-cloud` job whose alerts are gated on the cloud host being up (cloudlab already ships
   `job="cloud-node"` to the same Prometheus). **Prove the join matches live series** — a join on a
   label that does not exist on both sides silently yields an empty vector and the alert never fires,
   which is the worse failure.
2. **`just ci-runners-preflight` must keep passing.** `check-ci-runners.py` asserts every name in
   `DEFAULT_RUNNERS` is online. Do **not** add cloud runners there. Separately, `DEFAULT_RUNNERS`
   still lists only 5 of the 9 live runners — fix that now, independently.
3. **`EXPECTED_LABEL` is a fail-closed gate.** It is a *membership* test, so both labels pass during
   the dual-label window — but flip it before the runners carry the new label and the AgentForge
   Stage-0/4 image build is blocked.
4. **Do not change `kubernetes/infra/runners` VM `tags` casually.** `tags` is not in
   `lifecycle.ignore_changes`, so retagging `github-runner` → `ci-runner` is a real in-place update
   across all live runner VMs.
5. **Converge the two runner generations.** Runners 1-5 (2026-07-09) lack `TimeoutStartSec=10min` and
   carry the older 8847-byte reclaim script; 6/7/9/10 (2026-08-25) have both. That pair is the fix
   for the 2026-07-28 quadratic-reclaim outage that took the whole fleet offline, so 1-5 plausibly
   still carry that exposure.
6. **Busy/idle is transient.** Measured 9/9 busy at 15:00 and 7/9 at 15:52 — the two idle VMs had
   been busy 50 minutes earlier. Never plan around a fixed idle set; re-probe
   `pgrep -P $(systemctl show -p MainPID --value gitea-act-runner.service)` immediately before acting
   and abort on non-empty.
7. **cloudlab README drift:** cloud3's `local-nvme` is **1.72 TiB**, not the documented 2.58 TiB.

---

## 9. Phases

Each phase has an **exit criterion**. A phase that has not met it is not "landed", and a half-landed
phase is the ambiguous state rollback handles worst — so do not start the next one.

**Phase 0 — cooperative drain + hygiene (both repos, no new hardware; independent of everything else).**
Three things, and the first two are one change, not two:
- (a) `gitea_runner_shutdown_timeout: 10m` + `TimeoutStopSec=11min` in the role.
- (b) `cluster-power.sh`: raise the guest `--timeout` past the in-guest grace **and** make the final
  `poweroff` conditional on the guests actually being down. **(a) without (b) is inert** — the script
  hard-kills at ~125 s regardless — so they land together or the fix is cosmetic.
- (c) Independent hygiene: fix `DEFAULT_RUNNERS` in `check-ci-runners.py`, which still lists only 5
  of the 9 live runners and is therefore blind to four of them today. Do this now, not after Phase 3.

Roll the role change the non-disruptive way (install files; the unit picks them up on its next
natural restart — **never restart a busy daemon**).
*Fixes a live bug; worth doing even if the cloud tier is never built.*
**Exit:** V1 and V2 pass; preflight green against all nine runners.

**Phase 1 — GitHub/HV decommission.** Follow §6's six-step order exactly, per VM, idle-gated on
**both** tiers. Then revoke the App key, purge the OpenBao path, and do the documentation sweep.
**Exit:** all nine agents stopped and disabled; `runner_health.prom` gone from Prometheus; every
gitea beacon alert still returning series; `just gitea-runners` and the renamed base playbook both
run clean; V9 passes.

**Phase 2 — label rename.** **L0 first** (§7 — blocking), then L1→L4.
**Exit per step:** L1 — all runners report both labels via the org API. L2 — a test PR gets green
required checks on every affected repo. L4 — no queued or running job anywhere still resolves to the
old label. L4 is the point of no return.

**Phase 3 — cloud runners.** Clear B2/B3/B4, then: `cloudlab/kubernetes/infra/ci-runners/` (5 VMs),
`just ci-runners-plan/apply` recipes, ailab inventory group + IPAM allocation, `ci-drain.sh` wired
into `cluster-power.sh`, monitoring with host-gated alerts.

> **Landing order is not optional.** ailab's IPAM entry and inventory group are inert on their own and
> must land **before** cloudlab's apply. Reversed, live VMs sit on addresses the registry still shows
> free — the exact mechanism behind the 2026-09-03 collisions.

**Exit (Phase 3):** V3, V5, V6, V7, V8 pass; a week of V10 steady state.

**Phase 4 — cloud2.** After the physical BIOS visit: 3 more runners (pool → 17). Needs an IPAM
decision first; `.26`-`.35` is exhausted, so they must come from `.5`-`.7`, `.38`, `.39`, `.50`.

> A note on a tempting-but-wrong gate: cloud2's runners live on **cloud2** and have no bearing on
> cloud3's page cache. The cloud3 measurement in §4.3 gates a **fourth runner on cloud3**, nothing
> else. And page cache is reclaimable in any case — the risk there is cold-load latency, never an
> OOM.

---

## 10. Verification

**V0 — baseline, before anything changes.** Record p90/median queue wait, jobs/day, job distribution
across runners, and the weekly count of zero-log-line failures. Without this, "measurable drop in
queue wait" in V9 is unfalsifiable.

1. **Drain on `systemctl stop`** (D1, guest half). Start an ~8-minute job, stop the unit.
   *Pass:* the job **completes**. *Fail:* reds with zero log lines. **This fails today.**
2. **Drain via the real power path** (D1, host half). Same job, via `cluster-power.sh down`.
   Only meaningful **after** the script fix — run it before the fix as well, to confirm it fails, so
   the test is proven to have teeth.
3. **`ci-drain.sh` in isolation** (D2). With a job in flight, run the script alone.
   *Pass:* it waits, the job completes, *then* the VM stops. *Fail:* it stops a busy runner — which
   means the idle signal is wrong.
4. **Label matching** (L0, §7). Blocking gate before any org-variable flip.
5. **Attach.** Power-cycle a cloud node → runner `online` within ~2 min of POST, unattended.
6. **Carry.** Push a branch → a job lands on `cloud-ci-N` and goes green.
7. **Alert join has teeth.** Before Phase 3 ships, run the §8.1 expression against live Prometheus
   and assert it returns a **non-empty** vector while a runner is down and its host is up. An
   expression that never matches is silent in both directions.
8. **Detach.** Power the cluster off → runners `offline`, ailab CI unaffected, **no alert fires**
   (meaningful only because V7 proved the rule can fire).
9. **Decommission.** Nine agents stopped, `runner_health.prom` gone from Prometheus, every gitea
   beacon alert still has series, `just ci-runners-preflight` green against all **nine** runners.
10. **Steady state.** A week against the V0 baseline: no nightly pages, zero zombie tasks, a drop in
    p90 queue wait.

---

## 11. Rollback

| To undo | Steps |
|---|---|
| A cloud runner | drain, `tofu destroy -target`, then **delete the registration** (`DELETE /api/v1/orgs/cchifor/actions/runners/<id>`) — otherwise it lingers offline forever, as `ci-runner-8` does today |
| The cloud tier | all of the above ×5, remove the `ci-runner-cloud` monitoring objects, revert the inventory group, release `.30`/`.32`-`.35` in `docs/network-plan.md` |
| Phase 0 | revert `config.yaml.j2` + `TimeoutStopSec`; re-run the role. Safe at any time — the old behaviour is the current buggy one |
| The label rename | re-add the old label to every runner and flip the org vars back — **only possible before L4** |
| The GitHub decommission | reinstalling agents requires a new App key (the old one is revoked). Treat as one-way; the safety net is that the agents are provably idle |

**Mid-phase rollback** is the dangerous case — a phase abandoned halfway leaves a state neither
playbook expects. The §6 ordering is built so the reversible work comes first: steps 1-3 (split,
beacon port, generation convergence) are additive and revert cleanly by reverting the commit and
re-applying. From step 4 (agent stop) onward, **finish the VM you started** rather than leaving the
fleet in two states — a runner with its agent stopped but the group un-renamed is the one combination
where neither `just gitea-runners` nor the base playbook is guaranteed to run.

---

## 12. Open questions

1. ~~Ansible or scripted SSH?~~ **RESOLVED 2026-09-15 — and `CLAUDE.md` is stale.** It claims
   "WSL has no internet, so Ansible-over-`/mnt/c` and tofu provider downloads fail there." Measured
   from WSL (Ubuntu 24.04.3): `pypi:200 github:200 galaxy:200 gitea:200`, and TCP 22 to
   `192.168.0.14` open. `pip 24.0` and `pipx` are present; `ansible-playbook` is simply **not
   installed**. `ansible.cfg` lives at `ansible/ansible.cfg` — the world-writable `/mnt/c` case where
   it is silently ignored unless `ANSIBLE_CONFIG` is set explicitly.
   → Install Ansible in WSL, export `ANSIBLE_CONFIG`, prove with `--check`. Use the real IaC path for
   Phase 1 rather than hand-SSH; hand-installation is how the runner-generation drift in §8.5 arose
   in the first place. **Correct `CLAUDE.md` and the runbook in the same change.** cloudlab still has
   no Ansible of its own.
2. **Who owns cloud-guest config?** Cheapest is a `cloud_gitea_runners` group in ailab's inventory
   running the existing roles against cloud guests. That is the mirror image of the two-estate
   boundary rule, and ailab's inventory already carries a scar comment about `just gitea-runners`
   having been pointed at cloud1/2/3.
3. **New label name** — `self-hosted-ci`, `ailab-host`, or plain `self-hosted`?
4. **When is the cloud2 BIOS visit?** It gates 60 % of the available opportunistic CPU.
5. Auto-rerun stays deferred (§3.2). Revisit only with measured loss.
