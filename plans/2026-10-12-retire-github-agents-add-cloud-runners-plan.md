# Retire the dormant GitHub agents; add three cloud runners (cloud-ci-6 back, cloud-ci-14, cloud-ci-15)

## Context

Operator request (2026-10-12): "remove github runners and add 3 more runners on cloud1 and cloud2".
The operator chose to scan and retire cloud1's bad RAM first, and to reuse cloud-ci-6's address.

**Measured facts:**

- **GitHub agents.** `actions.runner.cchifor-platform.service` (role `github_runner`, ADR 0013) runs on
  all 8 ailab runners (ci-runner-1..6, 9, 10).
  - ADR 0017 disabled Actions on the GitHub mirror, so the agents never get a job: 0 jobs in their
    journals since 09-30 / 10-02. Each is a listener refreshing its token every ~50 min, using
    15-61 MB RSS and 2-3.4 GB of disk.
  - The GitHub App key `/etc/runner/app.pem` is `runner:runner 0400`. Every Gitea CI job runs as
    `runner` (host mode), so every job can read it.
  - The role documents the App as **Administration: read/write** on `cchifor/platform`.
  - The agent's `runner-reclaim.sh` (an ExecStartPre) force-removes Docker state. #1284 guards it,
    but it stays a hazard.
  - The Gitea cleanup treats the agent as a peer runner (`gitea_runner_cleanup_peer_services`), and
    the cache purge checks for `Runner.Worker`.
  - The daily converge runs only dev-workers.yml and reviewers.yml. ci-runner-3's agent, which was
    retired by hand on 07-09, came back on the next manual `just runners`.
- **Demand.** Over 3 days (1,388 runs, all on the single label `self-hosted-hv`), the median wait was
  0 min, but 26 % of runs waited more than 5 min, mostly at 15:00-20:00 EEST, while the cloud pool is
  up.
  - forge, relay and llm-router had p90 waits of 60-82 min, and some forge jobs waited 2-4.5 h.
  - The window includes the 10-12 cloud-ci-5/7 outage, so the numbers are somewhat inflated.
- **Headroom (7-day minimum of MemAvailable):**
  - ailab hosts: ai-node1/2/3 at 13.4 / 7.7 / 29.4 GiB, reserved for the LLMs. There is no room for
    VMs there.
  - cloud1: 72.6 GiB free of 125.7, CPU 10 % busy, 32 threads.
  - cloud2: 35.2 GiB free of 125.6, CPU 5 % busy, 128 threads.
  - Each cloud runner VM is 8 vCPU, 24 GiB (balloon floor 10), 200 GB. Measured use is at most
    10.7 GiB and 4.4 vCPU at p99 (ADR 0032).
- **Addresses.** `.37` and `.39` are the only free static IPs (docs/network-plan.md). `.29` belongs to
  cloud-ci-6 (cloud1, vmid 6106), quarantined since 09-24.
- **cloud1 RAM.** #848: memtester inside cloud-ci-6 logged 1,038 bit flips in one small range, and
  `memtester 48G` on the bare host failed "Stuck Address" within a minute. The board has non-ECC DIMMs
  from two kits at XMP 3000. Nothing has been repaired since.
  - The same day, cloud3's bad page was located, retired and made durable (cloudlab#40,
    `host/retired-pages.tsv`). Any additional cloud1 frame can be retired the same way.

## Approach

Three workstreams. WS1 is independent. WS2 gates WS3.

### WS1: retire the GitHub agents (ailab PR)

1. **Role.** In `github_runner`, add `tasks/retire.yml`, run when `not github_runner_agent_enabled`.
   Idempotent, `state: absent` everywhere:
   - stop and disable `actions.runner.cchifor-platform.service`;
   - remove its unit and drop-ins (`10-job-started-hook.conf`, `20-reclaim.conf`);
   - remove `/home/runner/ephemeral-runner.sh`, the job-started hook script and
     `/usr/local/bin/runner-reclaim.sh`;
   - remove `/etc/runner/` (the App key) and `/home/runner/actions-runner/`;
   - then `daemon-reload`. Tag it `github_runner_retire`.
   - On the cloud runners (already agent-disabled) this is a no-op.
2. **Config.**
   - `group_vars/github_runners.yml`: `github_runner_agent_enabled: false`, with a comment on how to
     re-enable it (mint a new App key, flip the flag, run `just runners`).
   - `gitea_runner_cleanup_peer_services` defaults to `"none"`.
   - Make the playbooks' SOPS pre_task for `github-runner.sops.yaml` conditional on the agent being
     enabled, so the secret can be deleted.
3. **GitHub side, BEFORE removing the key.**
   - Use the App (JWT from the key) to list the self-hosted runners on `cchifor/platform` and delete
     every registration.
   - Then the operator revokes the App private key (or uninstalls `ailab-ci-runners`) in the GitHub
     UI. Treat the key as exposed: it was readable by CI jobs for months.
   - A follow-up deletes `ansible/secrets/github-runner.sops.yaml` once the key is revoked.
4. **Rollout.**
   - Run `just runners --tags github_runner_retire` from a WSL-native clone of merged main (Ansible
     over `/mnt/c` is broken). Use `--check --diff` first.
   - The act_runner is not touched, so in-flight Gitea jobs are unaffected.
5. **Docs.**
   - ADR 0013: status "agents retired 2026-10-12".
   - ADR 0017: a note.
   - Runbook §(GitHub agent reclaim): rewritten, plus re-enable steps.
   - Inventory comments.

### WS2: cloud1 RAM gate (cloudlab table PR)

1. **Host scan on cloud1.**
   - `findbad-host.py` runs as root on cloud1 (from the 10-12 `findbad.py`, but on the host, so
     `/proc/self/pagemap` gives the physical frame directly). It mlocks **40 GiB**, runs 12 patterns ×
     2 passes and prints every bad 4 KiB physical address.
   - Why 40 GiB: used is ~53 GiB, and +40 keeps the host under 80 %, below pvestatd's autoballoon
     threshold, so cloud-ci-1/2 are not squeezed mid-job.
   - **Abort** if MemAvailable drops below 15 GiB. Runtime ~10-15 min.
   - A second round re-mlocks to reach frames the first did not get. Coverage is partial by nature.
2. **Retire.**
   - Add each bad address to `host/retired-pages.tsv`, then run provision 3b on cloud1.
   - Verify that `cloudlab_bad_pages_verified == listed` and that CloudHostBadPageInService is quiet.
3. **Stop rule.** If the scan finds more than ~64 bad pages, or bad pages spread across many regions,
   page retirement is the wrong tool. Stop, report, and do NOT place runners on cloud1. That is the
   DIMM replacement / offline memtest86+ path, which needs the operator.

### WS3: capacity (cloudlab tofu PR + ailab PR)

1. **cloudlab `kubernetes/infra/ci-runners/variables.tf`:**
   - `cloud-ci-6` `started = true` (the comment records that the gate was met: WS2's date and pages);
   - `cloud-ci-14 = { cloud1, 6114, .37 }`;
   - `cloud-ci-15 = { cloud2, 6115, .39 }`;
   - datastore per host chosen from measured thin-pool usage (`pvesm status` / `lvs`), never a pool
     that would exceed ~80 %.
   - Apply with Windows `tofu.exe`, from the worktree, against the existing local state in the main
     cloudlab checkout (`-state=`/`-var-file=`). `plan` first, and the plan must show only the 2
     creates plus cloud-ci-6 started.
2. **Acceptance gate per VM, before it can take a job:**
   - **cloud-ci-6:** pause its Gitea registration (`disabled: true`) BEFORE starting the VM, because
     act_runner is already installed and would take jobs at boot.
   - **cloud-ci-14/15:** scan BEFORE running the role, while they are still bare cloud-init VMs.
   - The scan: in-guest `findbad.py` over ~80 % of the VM's RAM. 0 bad pages is required; otherwise
     stop the VM, translate the address on the host, retire it, and rescan.
3. **ailab PR:**
   - inventory: uncomment cloud-ci-6, add cloud-ci-14/15;
   - `ci-runners-cloud.yaml`: scrape targets;
   - `docs/network-plan.md`: `.37`/`.39` allocated, no free static IPs left;
   - ADR 0032 amendment: cloud pool 12 → 15, daytime pool 20 → 23, with keep-or-revert gates.
   - Then `just cloud-runners -l cloud-ci-6,cloud-ci-14,cloud-ci-15` from the WSL clone.
   - Finally unpause each runner in Gitea after its scan passes.
4. **Not changed:** the cloud-power drain, which discovers enabled `cloud-ci-*` runners by name, and
   `check-ci-runners.py`, which checks the 24/7 pool only. Both to be verified, not assumed.

## Risks

- **cloud2 memory.** A 4th runner (≤ 10.7 GiB real) takes cloud2's 7-day low from 35.2 to an
  expected ~24 GiB. That is below ADR 0032's keep gate (30 GiB) and near pvestatd's autoballoon
  point (80 % used ≈ 25 GiB free).
  - **Keep-or-revert:** if MemAvailable falls below 25 GiB, or any cloud2 runner balloon inflates,
    over 2 busy days, quarantine cloud-ci-15 (`started = false`) or move it to cloud1.
- **cloud1 CPU.** 4 runners × 4.4 vCPU at p99 ≈ 18 of 32 threads, plus cloud-llm workloads.
  - **Keep-or-revert:** host CPU p99 < 50 %.
- **cloud1 RAM coverage is partial.** The host scan cannot test frames owned by running guests or the
  kernel. Residual detection comes from the per-VM acceptance scan, the nightly cache verify's
  `CloudCIRunnerCacheReadFlaps`, and segfault counts.
- **WS1 removes a fallback.** GitHub Actions can no longer be switched on instantly (ADR 0017's
  re-enable path). Re-enabling needs a new App key plus `just runners`; documented.
- **tofu state lives outside the worktree.** A wrong `-state` path would plan a full re-create. The
  `plan` output is the guard: only the expected 2 creates and 1 update are accepted.

## Verification

- **WS1:**
  - no `actions.runner*` unit, `/etc/runner` or `actions-runner` on any of the 8 hosts;
  - GitHub API: 0 runners on `cchifor/platform`;
  - Gitea runners: no job interrupted, act_runner untouched;
  - the cleanup's next tick logs no peer.
- **WS2:** the scan log; each listed page `verified`; HardwareCorrupted on cloud1 = 4 kB × pages;
  the alert quiet.
- **WS3:**
  - each VM's acceptance scan is clean;
  - the runners are online and unpaused in Gitea, and their first jobs pass;
  - beacons (reclaim, cleanup, reap, cache-verify) and the scrape are up, and alerts are quiet;
  - after 2 busy days: queue waits re-measured, and the cloud1/cloud2 keep-or-revert gates checked.

## Rollback

- **WS1:** set `github_runner_agent_enabled: true`, mint a new App key into SOPS, run `just runners`.
- **WS2:** delete the tsv line and reboot. That is not advised; the page is bad.
- **WS3:** `started = false` per VM (the quarantine switch), pause in Gitea, revert the inventory and
  scrape entries.

<!-- codex-review-status: pending -->
