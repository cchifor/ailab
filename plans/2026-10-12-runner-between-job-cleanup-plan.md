# Gitea runner: cleanup between jobs, and cache integrity

## Context

The `gitea_runner` pool runs act_runner 0.6.1 in **host mode**. Every job on a runner shares one Unix user (`runner`) and one `$HOME`, so it also shares `~/.cache/uv`, `~/.npm`, mise, zig and cargo. Jobs also share `/tmp`, one Docker daemon and one systemd cgroup (`gitea-act-runner.service`, MemoryMax 10G). act_runner has **no per-job hook**. The only job-boundary signal it emits is the journal line `task <N> repo is <owner>/<repo> …`, logged when a task starts. Nothing is logged at the end of a task. Jobs are short (median 14 s, p90 31 s), and about 96% of the gaps between jobs are under 10 s. So any check of the form "is the runner idle right now?" cannot reliably tell one job from the next (the existing cleanup needs 3 consecutive idle polls just to reject the false idle between steps).

A fleet audit on 2026-10-12 (20 runners) found these gaps:

1. **Processes left by a job are never killed.** On cloud-ci-13, a `vite preview --port 4786 --strictPort` from a job had been running for 57 h (PPID 1, still in the runner cgroup). Any later job that needs port 4786 fails, and the process counts against the 10G cgroup cap. The reclaim script's idle check (`pgrep -P <MainPID>`) cannot see re-parented processes.
2. **Leftover Compose volumes and networks are never removed.** `gitea-runner-cleanup.sh` never prunes volumes. Unused `platform_*` volumes and anonymous volumes pile up. Every repo's workspace folder is named `hostexecutor`, so **Compose's default project name is `hostexecutor` for every repo**. That is how a volume named `hostexecutor_relay-db` ended up on cloud-ci-8: one repo's leftovers fall into the next repo's project namespace.
3. **`/tmp` grows to 9–22 GB per runner.** On ci-runner-6, `pytest-of-runner` is 10.5 GB and `node-compile-cache` 8 GB. Ubuntu's `tmp.conf` only ages `/tmp` entries out after **30 days**.
4. **Shared caches are never verified, so one corrupted entry repeats on every job.**
   - The uv cache on cloud-ci-5 held a corrupted `cryptography-50.0.2` `_rust.abi3.so`, so every kartra-ai job that installed it segfaulted (PR #21/#22).
   - 14 npm `_cacache` entries on cloud-ci-7 no longer matched their own sha512.
   - uv **hardlinks** packages into venvs from the shared cache. The corrupted `.so` had 3 extra links inside `/tmp/pytest-of-runner/…/.venv`, so any in-place write by a test or tool corrupts the cache for every later job.
   - The cause of the cloud3 corruption itself is out of scope here (a KSM-off experiment is running).
5. **On ailab runners**, the dormant GitHub agent's `ExecStartPre=runner-reclaim.sh` force-removes **every** container and runs `volume prune -af` without checking for a live Gitea job. This is latent: it fires only when the agent restarts, but then it kills a running Gitea job's stack.
6. **A runner whose filesystem has died raises no alert.** cloud-ci-5 and cloud-ci-7 hit an ext4 shutdown (every read returned EIO, sshd reset connections) and nothing fired. node_exporter kept running from memory.

Out of scope:
- Finding the root cause of the cloud3 corruption.
- Dirty action clones under `work/<sha>`. Checked: the only change is act's own deletion of `.gitignore`, which is harmless.
- Replacing act_runner or moving to ephemeral VMs.

## Approach

Two PRs from this one plan. PR 1 covers items 1–3 and 5 (cleanup between jobs). PR 2 covers items 4 and 6 (cache integrity and filesystem health).

### PR 1-A — job-boundary leftover reaper (new phase of `gitea-runner-reclaim.sh`)

**Boundary.** Let `B` be the wall-clock time of the newest `task <N> repo is` line in `journalctl -u gitea-act-runner`. Read it incrementally with a journal cursor stored in `/var/lib/gitea-runner-reclaim/state`, so each 5 s tick reads only new lines. With `capacity: 1`, act_runner takes a task only after the previous one finished, so at `B` the previous job is over.

**Leftover criterion (race-free, needs no idle detection).** An object is a leftover if and only if:
- it was **created before `B − margin`** (margin 2 s), and
- it is not act_runner itself, and not a descendant of its MainPID.

The current job's processes, containers and volumes are all created after `B`, so they are never touched, even mid-job. That includes a server it backgrounds for later steps.

**Start time, not ancestry, is the rule that protects the current job.** Measured on 2026-10-12 on cloud-ci-10, during a live kartra-ai job:
- The job's Playwright `node` and `chrome-headless` processes, started 439 s earlier, were in the runner cgroup.
- They were **not** descendants of act_runner's MainPID: their parent chain broke at a parent that had already exited.
- So "kill non-descendants" on its own would kill a live job's browser. The descendant exclusion is only a second guard.

**Edge-triggered.** The reap runs once each time `B` advances (once per job start), plus a periodic re-pass every 15 min. Docker is therefore queried once per job, not every 5 s.

**Stale-idle rule.** If no new task has started for longer than the job timeout + 10 min (3h10m), the last job is certainly over, so the boundary becomes `now − 10 s`. Leftovers from the last job of the day still get reaped.

**Fresh-start guard.** Just before acting, re-read the newest task line. If it changed, recompute `B` and take a fresh snapshot.

**What gets reaped:**

a. **Processes.** Read every PID in the service cgroup, recursively, from `/sys/fs/cgroup/system.slice/gitea-act-runner.service`. Each process's start time is `btime + starttime/CLK_TCK`, from `/proc/stat` and `/proc/<pid>/stat`. Keep MainPID and its descendants; reap whatever started before the boundary.
   - Send SIGTERM, wait up to 10 s, then SIGKILL.
   - Cap at 64 processes per pass, and log each one (pid, age, cmdline trimmed to 120 characters).
   - The GitHub agent runs in a different cgroup, so it is never seen.

b. **Containers.** A container is reaped when it was created before `B`, is not on the keep-list, and is attributable to this pool.
   - Keep-list: image or name matches `INFRA_EXCLUDE_RE` (`buildkit|buildx`), or the container has a label matching `GITEA_REAP_KEEP_LABEL_RE` (default `^(io\.dsh\.conductor\.image-pin|ci\.keep)$`). That covers the deliberate `dsh-conductor-image-pin` on ci-runner-1.
   - Attribution: on hosts with no peer runner (`peer_services=none`, the cloud runners), every container counts. On hosts with peers (ailab), a container counts only if its Compose `working_dir` label or one of its bind-mount sources is under `WORKDIR_PARENT`. Anything else is left to the existing 4 h age reaper.
   - Removal: `docker rm -f`.

c. **Compose networks and volumes.** Remove those labelled `com.docker.compose.project` that were created before `B` and that no container references. Also remove anonymous dangling volumes (64-hex names, no labels) created before `B`. Named volumes without Compose labels are kept, because they may be deliberate caches.

**Fail-safe.** No boundary found (journal unreadable, or the message format changed on an act_runner upgrade) means no reaping. The existing reclaim behaviour is unchanged. The beacon exports `gitea_runner_reap_boundary_age_seconds` and `gitea_runner_reap_last_{processes,containers,volumes}`. A new reap-specific `GITEA_REAP_ENABLED` switch defaults to on.

### PR 1-B — bounded `/tmp`

`/tmp` is not cleaned per job, because pytest reuses its `pytest-of-<user>` parent and deleting during a job would race. Instead, use `systemd-tmpfiles` ageing, which checks atime, mtime and ctime per entry, so anything in use stays fresh:
- `/etc/tmpfiles.d/gitea-runner-tmp.conf`:
  - `e /tmp/pytest-of-runner - - - 6h`
  - `e /tmp/node-compile-cache - - - 1d`
  - `e /tmp - - - 2d`
- A drop-in for `systemd-tmpfiles-clean.timer` with `OnUnitActiveSec=1h`.
- 6 h is more than the 3 h job timeout, so nothing a live job touched can be aged out.

### PR 1-C — keep jobs from writing into the shared uv cache

Set `UV_LINK_MODE=copy` in act_runner's `runner.envs` (new role var `gitea_runner_uv_link_mode`, default `copy`). A job or test that writes into a venv file then changes its own copy, not the cache that every later job hardlinks from.
- Cost: venv installs copy instead of hardlinking. Measure job durations for forge, kartra-ai and platform on one canary runner over 24 h before rolling out to the fleet.
- If the cost turns out large, fall back to `clone`, which behaves as copy on ext4, so the decision stays the same.

### PR 1-D — GitHub reclaim guard (`github_runner/templates/runner-reclaim.sh.j2`)

Before removing containers and volumes, check whether the Gitea daemon is busy: `gitea-act-runner` MainPID has children, or the newest task line is under 2 min old. If busy, skip the container and volume removal, log it, and still do the workspace and `~/.docker` repair. The agent is dormant, so this only guards restarts.

### PR 2-A — nightly cache integrity check (`gitea-runner-cache-verify.sh`, new)

**Schedule.** A timer with `OnCalendar=*-*-* 04:30`, `RandomizedDelaySec=2h`, `Persistent=true` and `OnBootSec=45min`. The cloud hosts are powered off at night, so the persistent and boot triggers make sure it still runs. Runs under `nice -n 19` and `ionice -c3`.

**Checks:**
- npm: `~/.npm/_cacache/content-v2/{sha512,sha1}/aa/bb/<rest>`, where each file's digest must equal its path.
- uv: `~/.cache/uv/archive-v0/<id>/` against every `*.dist-info/RECORD` sha256 line, covering all files, not just `.so`. Archives without a RECORD are skipped and counted.

**Purge.**
- npm: delete the bad content file.
- uv: delete the archive folder, plus every `wheels-v*/**.http|.msgpack` pointer whose bytes contain that archive id.
- Purging happens **only inside a confirmed idle gap**, using the same `any_job_running` / `idle_confirmed` logic as the cleanup script (one wait budget of up to 10 min). Otherwise it is deferred to the next run and the metric still reports the problem.

**Beacon** (`gitea_runner_cache_verify.prom`):
- `gitea_runner_cache_verify_last_run_seconds`
- `gitea_runner_cache_checked{cache}`
- `gitea_runner_cache_corrupt{cache}`
- `gitea_runner_cache_purged{cache}`
- `gitea_runner_cache_verify_duration_seconds`

### PR 2-B — alerts

In `ci-runners-rules.yaml` (ailab VMs) and `ci-runners-cloud-rules.yaml` (cloud, gated on the host being up like the existing rules):
- `CIRunnerCacheCorrupt`: `gitea_runner_cache_corrupt > 0`, warning.
- `CIRunnerCacheVerifyStale`: last run more than 3 d ago while up.
- `CIRunnerFilesystemFailed`: `node_textfile_scrape_error == 1`, or the reclaim beacon older than 5 min while node_exporter is up. This catches the "pingable, node_exporter up, root fs dead" state.

Add promtool test cases for each rule.

### Docs

- `docs/runbooks/ci-runners.md` §7: replace the stale timings (the runbook says 30 s / 48h-80%-1h / "never -af"; the code does 5 s / 24h-75% with a 4 h floor / `-af` at 88%), and document the reaper, the tmpfiles ageing, `UV_LINK_MODE` and the verify job.
- §9: the cloud specifics, including how to read a dead root filesystem (`/sys/fs/ext4/sda1/errors_count` via `pvesh … agent/file-read`).

## Critical files

- `ansible/roles/gitea_runner/files/gitea-runner-reclaim.sh`: the reaper phase, journal cursor state, new beacon fields.
- `ansible/roles/gitea_runner/templates/gitea-runner-reclaim.env.j2`, `defaults/main.yml`: knobs (`gitea_runner_reap_enabled`, `_reap_margin_sec`, `_reap_keep_label_re`, `_reap_max_procs`, `_tmp_ages`, `_uv_link_mode`, `_cache_verify_*`).
- `ansible/roles/gitea_runner/tasks/main.yml`: tmpfiles drop-in, tmpfiles-clean timer drop-in, verify script, service and timer, state dir.
- `ansible/roles/gitea_runner/templates/config.yaml.j2`: `runner.envs: UV_LINK_MODE`.
- `ansible/roles/gitea_runner/files/gitea-runner-cache-verify.sh` (new) and `templates/gitea-runner-cache-verify.{service,timer,env}.j2` (new).
- `ansible/roles/gitea_runner/tests/test-reclaim.sh` (extend) and `tests/test-cache-verify.sh` (new).
- `.gitea/workflows/runner-scripts.yaml` and `justfile`: run the new test.
- `ansible/roles/github_runner/templates/runner-reclaim.sh.j2`: the Gitea-busy guard.
- `kubernetes/apps/infrastructure/monitoring/ci-runners-rules.yaml`, `ci-runners-cloud-rules.yaml` and `ci-runners-cloud-rules.test.yaml`: alerts and tests.
- `docs/runbooks/ci-runners.md`: §7 and §9.

## Verification

**Unit (CI, `runner-scripts` workflow).** The paths for `/proc`, `/sys/fs/cgroup`, `journalctl` and `docker` become overridable via env, the same way `test-reclaim.sh` already mocks `systemctl`/`docker`. Fixtures:
- Processes:
  - A process started before the boundary and not a descendant is killed.
  - A process started after the boundary (a job's own backgrounded server) is kept.
  - MainPID descendants are kept.
- Containers:
  - The `buildx` and `image-pin` containers are kept.
  - On a host with peers, only workdir-attributed containers are reaped.
- Compose volumes are removed only when unused **and** created before the boundary. A named non-Compose volume is kept.
- Failure modes:
  - No task line, or an unparsable one: nothing is reaped, and the beacon reports `boundary_age = -1`.
  - The boundary changes between snapshot and act: the pass is recomputed.
- The single-pass `/proc` scan test still holds, with no per-workspace fan-out.
- `test-cache-verify.sh`:
  - A corrupt npm file and a RECORD-mismatched uv file are both detected.
  - Purge is skipped while busy and runs when idle.
  - The metrics are correct.
  - Pointer files for a purged archive are removed.

**Lint.** `shellcheck` on the new and changed scripts; `promtool test rules` on the rule files.

**Canary.** Roll out to `cloud-ci-9` first (cloud2, the host with no corruption) with `just cloud-runners --limit cloud-ci-9`. For 24 h, watch the `reap` log lines (`journalctl -t gitea-runner-reclaim | grep reap`), the job failure rate on cloud-ci-9 against the pool (the same task→status method used in the audit), job durations under `UV_LINK_MODE=copy`, the `/tmp` size, and one manual `systemctl start gitea-runner-cache-verify` with its `.prom`. Then roll out to all cloud runners, then the ailab pool (`just gitea-runners`).

**Live acceptance.** The cloud-ci-13 orphan `vite preview` (or its equivalent at that point) is gone after the next job starts. No job failures are attributable to reaping. `/tmp` stays under about 2 GB after 24 h. The verify beacon is present on every runner.

## Risks (likelihood × impact, with mitigation)

1. **A job relies on a leftover from a previous job** (low × medium). That is a hidden cross-job dependency, and surfacing it is intended. Mitigation: the keep label (`ci.keep=true`) and a per-host kill switch (`GITEA_REAP_ENABLED=0` in the env file).
2. **act_runner changes its log text** (medium on an upgrade × low). No boundary means no reaping, which fails safe. The boundary-age metric shows it, and a test pins the regex.
3. **Clock mismatch between the journal and `/proc` start times** (low × high). Both come from the same host realtime clock: convert starttime with `btime` and use the 2 s margin. Tests cover a process started 1 s after the boundary.
4. **`UV_LINK_MODE=copy` slows jobs** (medium × low). Measure on the canary before the fleet rollout. The role var allows a quick revert.
5. **tmpfiles ages out a long job's file** (very low × medium). The 6 h minimum is twice the job timeout, and atime, mtime and ctime all have to be stale.
6. **Docker load from the reaper** (low × low). It is edge-triggered once per job start, plus every 15 min.

<!-- codex-review-status: pending -->
