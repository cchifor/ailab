# Gitea runner: cleanup between jobs, and cache integrity

Round-1 review and per-finding resolutions: `2026-10-12-runner-between-job-cleanup-review-r1.md`. The Codex seat was out of quota (it resets 2026-10-18), so an independent Claude Plan agent reviewed this plan instead.

## Context

The `gitea_runner` pool runs act_runner 0.6.1 in **host mode** with `capacity: 1`. Every job on a runner shares the following:
- one Unix user (`runner`) and one `$HOME`, so also `~/.cache/uv`, `~/.npm`, mise, zig and cargo;
- `/tmp`;
- one Docker daemon (29.x, containerd store);
- one systemd cgroup (`gitea-act-runner.service`, MemoryMax 10G).

act_runner has **no per-job hook**. The only job boundary it emits is the `task <N> repo is <owner>/<repo> …` line, logged by act_runner itself when a task starts. Nothing is logged at the end. Jobs are short (median 14 s), and about 96% of the gaps between jobs are under 10 s. So "is the runner idle right now?" cannot reliably tell one job from the next.

The fleet audit on 2026-10-12 found:

1. **Processes left by a job are never killed.** On cloud-ci-13, a job's `vite preview --port 4786 --strictPort` had been running for 57 h (PPID 1, still in the runner cgroup). It blocks that port for later jobs and counts against the 10G cap.
2. **Compose volumes and networks are never removed** (`gitea-runner-cleanup.sh` never prunes volumes). Unused `platform_*` and anonymous volumes pile up. Every workspace folder is named `hostexecutor`, so Compose's default project name is **`hostexecutor` for every repo**. That is how cloud-ci-8 ended up with a `hostexecutor_relay-db` volume: repos land in each other's namespace.
3. **`/tmp` holds 9–22 GB per runner.** On ci-runner-6, `pytest-of-runner` is 10.5 GB and `node-compile-cache` 8 GB. Ubuntu 24.04 ships `D /tmp 1777 root root 30d`, so entries are only aged out after 30 days. The cloud runners also lose `/tmp` at boot on nightly power-off. The 24/7 ailab pool does not.
4. **Shared caches are never verified, so one corrupt entry repeats on every job.**
   - A corrupt `cryptography-50.0.2` `.so` in cloud-ci-5's uv cache segfaulted every kartra-ai job that installed it.
   - 14 npm `_cacache` entries on cloud-ci-7 failed their own sha512.
   - uv hardlinks installs from the cache, and that `.so` also had 3 hardlinks inside `/tmp/pytest-of-runner` venvs. That proves the inode was shared, not that anything wrote to it. So "jobs write through hardlinks" stays a hypothesis until the verifier records evidence.
5. **On ailab runners** the dormant GitHub agent's `ExecStartPre=runner-reclaim.sh` force-removes every container and prunes images, volumes and buildx builders. It has no Gitea check. It is latent: it fires on any agent restart.
6. **A runner whose root filesystem has died gets no fast alert.**
   - cloud-ci-5 and cloud-ci-7 hit an ext4 shutdown at 09:55–09:56Z. Every read returned EIO, while node_exporter kept serving.
   - `node_textfile_scrape_error` went to 1 within about 1 min. `node_filesystem_readonly` stayed 0.
   - `CloudCIRunnerCleanupBeaconMissing` went only *pending* (its `for` is 30 min). A reset cleared it about 4 min later.
   - So nothing would have paged for half an hour, while a dead runner keeps polling Gitea and failing every job it is given.

Out of scope:
- The cloud3 corruption itself. A KSM-off experiment is running.
- Action clones. The only change in them is act's own deletion of `.gitignore`, which is harmless.
- Replacing act_runner or moving to ephemeral VMs.
- Retiring the dormant GitHub agents (follow-up).

## Approach — three PRs, ordered by risk

### PR 1 (low risk): bounded `/tmp`, GitHub reclaim guard, runbook refresh

**1-B /tmp.**
- Override the vendor file by name with `/etc/tmpfiles.d/tmp.conf`, containing `D /tmp 1777 root root 2d`. That keeps the boot wipe and changes only the age.
- `/etc/tmpfiles.d/gitea-runner-tmp.conf` adds `e /tmp/pytest-of-runner - - - 6h` and `e /tmp/node-compile-cache - - - 1d`. These are different paths, so no duplicate-line collision.
- A `systemd-tmpfiles-clean.timer` drop-in sets `OnUnitActiveSec=1h`.
- Why this is safe: anything a live job created or modified is under 3 h old (the job timeout), and the smallest age here is 6 h. Under relatime a file a job only *reads* can look older. That only affects caches (node-compile-cache), where losing an entry costs a recompile.
- Verify with `systemd-tmpfiles --cat-config` (no "Duplicate line" warning) and `systemd-tmpfiles --clean` exiting 0.
- On busy runners pytest's retention count (3 sessions), not age, sets the size of `pytest-of-runner`. Per-repo `tmp_path_retention_count` is a follow-up. It is not set globally, because PytestConfigWarning on older pytest plus `-W error` would fail jobs.

**1-D GitHub reclaim guard** (`github_runner/templates/runner-reclaim.sh.j2`).
- When `gitea-act-runner.service` is enabled on the host, skip the **whole** docker block: `rm -f` of containers, `volume prune -af`, image/builder prune, and `buildx rm --all-inactive`. Log the skip.
- The workspace and `~/.docker` repair still run.
- Docker hygiene on those hosts belongs to the Gitea cleanup. This is race-free and needs no busy detection.
- Add `ansible/roles/github_runner/**` to the `runner-scripts.yaml` path filter. Keep the guard inside the block that `scripts/tests/test_dsh_validator_pin.py::TestGithubReclaimKeepList` extracts, or update that extraction, and add a test that the guard exists.

**Docs.**
- `docs/runbooks/ci-runners.md` §7: real timings (5 s reclaim; cleanup 24 h / 75% / 4 h floor / `-af` at 88%) and the `/tmp` policy.
- §9: reading a dead root fs (`/sys/fs/ext4/sda1/errors_count` via `pvesh … agent/file-read`; recovery is `qm reset`, since the on-disk fsck was clean), and the runbook action of disabling the runner in Gitea.

### PR 2 (medium risk, ships in log-only mode): job-boundary leftover reaper

This is a new phase of `gitea-runner-reclaim.sh`. It runs on the existing 5 s timer, **before** the `daemon_busy` early return. It is skipped under ExecStartPre (`--no-reap`): with `KillMode=mixed` the cgroup is already empty at start, and keeping docker calls off the start path avoids repeating the 2026-07-28 outage.

**Preconditions (otherwise skip and report `reap_state=refused`):**
- `gitea_runner_capacity == 1`: an Ansible assert while reaping is enabled, plus the script parses `config.yaml`.
- The act_runner MainPID is numeric and > 0.

**Boundary `B`.**
- Read new journal entries since a stored cursor: `journalctl _SYSTEMD_UNIT=<svc> _PID=<MainPID> -o json --after-cursor=…`.
- Accept only messages from `_COMM=act_runner` whose `MESSAGE` matches the anchored `^time="([^"]+)" level=info msg="task [0-9]+ repo is [A-Za-z0-9._/-]+ `. That ignores `logger` lines from job processes, which journald attributes to the same unit.
- For each accepted line, compute two boot-relative estimates and take the **earlier** one:
  - logrus `time=` (floored to the second, act_runner's own clock at the log call), converted with the current `btime`;
  - `__MONOTONIC_TIMESTAMP` (journald receive time, with the same `_BOOT_ID`).

  Then subtract a margin of 5 s. Receive lag only ever pushes B later, which is the unsafe direction, so taking the earlier estimate and the margin guard against it.
- State lives in `/var/lib/gitea-runner-reclaim/reap.state`: `boot_id`, `b_boot_ns`, `cursor` (parsed with its own reader, since cursors contain `;=`), `last_edge_b`, `last_periodic`. A new boot id resets B.

**Effective boundary.** `E = max(B, now − (job_timeout + 10 min))`. If B is missing, `E = now − (job_timeout + 10 min)`. No job can be older than its timeout, so `E` never reaches into a live job. There is no other fallback.

**When it acts.**
- **Edge pass**: once, when B advances. It uses `E`, so at a job start that is that job's B.
- **Periodic pass**: every 15 min, using only the timeout term, `now − (job_timeout + 10 min)`. It never uses B, so an object a long job took over can never be hit mid-job.
- Just before acting, the edge pass re-reads the journal. If B moved again, it recomputes.

**What counts as a leftover** (created before `E`, and not protected):

a. **Processes.**
   - Take the PIDs in the service cgroup, recursively. Skip MainPID, its descendants and zombies.
   - start = `btime + starttime/CLK_TCK`; a process is a candidate if start < E.
   - Live evidence (cloud-ci-10, 2026-10-12): a running job's Playwright `node`/`chrome-headless` were **not** descendants of MainPID. Start time is the rule that protects them; ancestry is only a second guard.
   - Re-read `/proc/<pid>/stat` starttime right before SIGTERM, and again before SIGKILL (guards against PID reuse).
   - One shared 10 s wait for the whole batch, capped at 64 processes per pass.
   - Every signal goes through the `REAP_KILL` indirection, so tests can mock it.

b. **Containers.**
   - Candidate when `Created < E` **and** `State.StartedAt < E`. A container started or restarted by the current job, or brought back by a restart policy, is kept.
   - Never reaped: image or name matching `INFRA_EXCLUDE_RE`, or a label **key** matching `REAP_KEEP_LABEL_RE` (default `^(io\.dsh\.conductor\.image-pin|ci\.keep)$`).
   - Attribution: on hosts with `peer_services=none` (cloud), all candidates count. On hosts with peers, a container counts only if its Compose `working_dir` label or a bind-mount source resolves under `WORKDIR_PARENT`. Values containing `..` are rejected.
   - The reclaim env renders the **same** role vars as cleanup (`gitea_runner_cleanup_infra_exclude_re`, `_peer_services`), so ci-runner-1's pin exemption and the cloud `none` sentinel apply. `test_dsh_validator_pin.py` gets a case covering the reclaim env too.

c. **Networks and volumes.**
   - Computed **after** the container pass.
   - Compose-labelled networks and volumes, and anonymous volumes (label `com.docker.volume.anonymous`, confirmed on 29.8.1), created before `E` and referenced by no container (running or stopped). The keep-label key also applies here.
   - Named non-Compose volumes are kept, as possible deliberate caches.
   - This reverses cleanup.sh's "never prune volumes" model, so update that header and the runbook.

**Modes.**
- `gitea_runner_reap_mode`: `off | log | enforce`, default **`log`**. Log mode writes `would-reap <kind> <id> age=… reason=…`.
- **Adoption probe** (log mode): on each later edge, check whether objects reported as would-reap are still in use by the current job. A process still alive and holding a listening port the job connected to is hard to prove, so the probe tracks the evidence that matters: a container, network or volume reported earlier that is now attached to a running container started ≥ B. A non-zero `gitea_runner_reap_adopted_total` blocks the switch to enforce.
- Measure task line → first `docker compose up` per repo from the journal and logs, to size the take-over window.

**Beacon:**
- `gitea_runner_reap_boundary_age_seconds` (−1 when missing)
- `gitea_runner_reap_mode`
- `gitea_runner_reap_last_candidates{kind}`
- `gitea_runner_reap_last_reaped{kind}`
- `gitea_runner_reap_adopted_total`
- `gitea_runner_reap_refused`

**Switching to enforce** is a one-line role var change in a follow-up PR. It happens after two or more CI days of log data with zero adoption and no would-reap object that later failed a job.

### PR 3 (medium): cache integrity verifier, purge queue, alerts

**Verifier.** `gitea-runner-cache-verify.py` is a single Python process run as `User=runner`.
- Opens files with `O_NOFOLLOW` and hashes regular files only. Skips `..` and RECORD lines with no hash.
- npm: `content-v2/sha512` and `content-v2/sha1` digests must equal the path.
- uv: `archive-v0/<id>` is checked against every `*.dist-info/RECORD` sha256 (urlsafe-b64). Archives without a RECORD are counted, not failed.
- For each bad file it records evidence: `nlink`; `mtime`/`ctime` against the archive dir's ctime (a later write means modified in place); size and path.

**Purge queue.** Bad entries are appended to `/var/lib/gitea-runner-cache/purge.queue` (owned by runner). The queue is applied only inside race-free windows:
- reclaim ExecStartPre (daemon down; the cloud runners boot daily);
- `gitea-runner-cleanup.sh`, inside its **existing** `idle_confirmed` window. One call, no new copy of the idle logic.

Purge actions:
- npm: run `npm cache verify` as runner. It rehashes, removes bad content and rewrites index-v5.
- uv: remove the `archive-v0/<id>` dir, plus every non-archive file under the uv cache whose bytes name `<id>`. This does not depend on the uv version: the host's uv 0.5.11 is older than the setup-uv versions that write the cache, so `uv cache clean` could miss newer buckets.
- If `nlink > 1`, `find` the other links under `~/.local/share/uv`, `/tmp/pytest-of-runner` and `~/act-runner/work` with `-xdev -samefile`, and remove each one's venv root (nearest `pyvenv.cfg`) or pytest session dir.

**Schedule and limits.**
- Timer: `OnCalendar=04:30`, `RandomizedDelaySec=2h`, `Persistent=true`, `OnBootSec=45min`.
- Unit: `CPUQuota=100%`, `IOWeight=10`, `IOReadBandwidthMax=/ 50M`, `Nice=19`.

**Beacon** (`gitea_runner_cache_verify.prom`):
- `…_last_run_seconds`
- `…_duration_seconds`
- `…_checked{cache}`
- `…_corrupt_found{cache}`
- `…_corrupt_pending{cache}`
- `…_corrupt_inplace_evidence{cache}`

**Alerts.** Keep the existing split: `CIRunner*` with `job="ci-runner-node"` in `ci-runners-rules.yaml`, and `CloudCIRunner*` with `job="ci-runner-cloud"` plus `and on (cloud_host) cloud_host:up == 1` in the cloud file.

| Alert | Condition | Severity |
|---|---|---|
| `…CacheCorruptPending` | `corrupt_pending > 0` for 30m | warning |
| `…CacheCorruptFound` | `corrupt_found > 0` (points at the host) | info |
| `…CacheVerifyStale` | last run > 3d and `time() - node_boot_time_seconds > 4h` | warning |
| `…RootFilesystemFailed` | `node_textfile_scrape_error == 1` for 3m | **critical**; the runbook says disable the runner in Gitea from outside |

Add `ci-runners-rules.test.yaml` (new; rules-lint auto-discovers siblings) and extend `ci-runners-cloud-rules.test.yaml`.

### Deferred: 1-C uv in-place-write protection

Ship this only if the verifier's `corrupt_inplace_evidence` is ever non-zero. If it ships, the preferred form is `chmod a-w` on archive files: hardlinks are kept, and in-place writes fail loudly with EACCES. The other option is `UV_LINK_MODE=copy` via `/etc/uv/uv.toml` (no daemon restart). Copy mode was rejected for now: unproven benefit, real disk cost under `/tmp`, and a fleet-wide drain.

### Follow-ups (issues, not this change)

- Compose project names in platform, kartra-ai and relay: `name:` in their compose files, or `COMPOSE_PROJECT_NAME: ${{ github.event.repository.name }}`.
- Per-repo pytest `tmp_path_retention_count`.
- Retire the dormant GitHub agents.

## Critical files

- PR 1:
  - `ansible/roles/gitea_runner/{tasks/main.yml,defaults/main.yml}` and new templates `tmpfiles-tmp.conf.j2`, `tmpfiles-gitea-runner.conf.j2`, `tmpfiles-clean.timer.override.j2`
  - `ansible/roles/github_runner/templates/runner-reclaim.sh.j2`
  - `scripts/tests/test_dsh_validator_pin.py`
  - `.gitea/workflows/runner-scripts.yaml`
  - `docs/runbooks/ci-runners.md`
- PR 2:
  - `ansible/roles/gitea_runner/files/gitea-runner-reclaim.sh`
  - `templates/gitea-runner-reclaim.env.j2`, `templates/gitea-act-runner.service.j2` (`--no-reap` on ExecStartPre)
  - `defaults/main.yml`, `tasks/main.yml` (state dir, capacity assert)
  - `files/gitea-runner-cleanup.sh` (safety-model header)
  - `tests/test-reclaim.sh`
  - `scripts/tests/test_dsh_validator_pin.py`
  - runbook
- PR 3:
  - `files/gitea-runner-cache-verify.py` (new)
  - `templates/gitea-runner-cache-verify.{service,timer}.j2` (new)
  - `files/gitea-runner-cleanup.sh` (purge-queue call in the idle window)
  - `gitea-runner-reclaim.sh` (purge queue under ExecStartPre)
  - `tests/test-cache-verify.sh` (new), `.gitea/workflows/runner-scripts.yaml`, `justfile`
  - `kubernetes/apps/infrastructure/monitoring/ci-runners-rules.yaml`, `ci-runners-rules.test.yaml` (new), `ci-runners-cloud-rules.yaml`, `ci-runners-cloud-rules.test.yaml`
  - runbook

## Verification

**Unit tests (`runner-scripts` CI, which runs on the live runners as `runner`).** Every external dependency is injectable via env: `/proc` root, cgroup root, `journalctl`, `docker`, `REAP_KILL`, `config.yaml` path, `btime`. A guard test asserts that no real PID was signalled.

Reaper cases:
- A pre-E non-descendant is killed (log mode: logged only).
- A post-B background process is kept.
- Descendants and zombies are kept.
- PID reuse: starttime changes between snapshot and signal, so it is skipped.
- A taken-over container (`Created < B`, `StartedAt ≥ B`) is kept.
- `buildx`, the pin label and `ci.keep` are kept.
- With peers, only workdir-attributed containers count; a working_dir containing `..` is rejected.
- Compose and anonymous (label) volumes are reaped only when unreferenced and pre-E; a named non-Compose volume is kept.
- A `logger`-forged task line from another PID is ignored.
- An unparsable or missing line never reaps anything newer than the timeout boundary.
- Capacity ≠ 1 is refused.
- B moving mid-pass triggers a recompute.
- Env-var drift check (as in test-cleanup.sh [M]).
- The existing single-pass `/proc` test still holds.

`test-cache-verify.sh`:
- A corrupt npm file and a RECORD mismatch are found.
- In-place evidence is flagged when ctime is newer than the archive.
- The queue is written; purge happens only through the queue appliers; pointer files are removed; a hardlinked venv root is removed.
- O_NOFOLLOW: a symlink to a FIFO does not hang.

`test_dsh_validator_pin.py`: the reclaim env exemption and the GitHub reclaim guard. `promtool test rules` covers all new rules. `shellcheck` on the changed scripts.

**Rollout.**
- Runbook §7 says this control node cannot run Ansible: the pool is installed by hand over SSH, mirroring the role. Confirm at deploy time whether the WSL converge path can run `cloud-runners.yml --limit`. If not, the steps are written out per PR (files, units, `daemon-reload`, timers), mirroring `tasks/main.yml`.
- **Canary:** cloud-ci-9 (cloud2) first, for one CI day, then all cloud runners, then the ailab pool.

**Synthetic acceptance on the canary (PR 2, log mode, then once briefly in enforce on the canary only).**
- As root, start `sleep 99999` as runner and move it into the service cgroup with `echo $pid > …/cgroup.procs`.
- `docker run -d --label com.docker.compose.project=reaptest alpine sleep 99999`.
- `docker volume create --label com.docker.compose.project=reaptest reaptest_v`.
- At the next job start, the would-reap log (or, in enforce, the reap log) must name all three, and the job running at that moment must succeed.

**Live checks per PR:**
- PR 1: `/tmp` on the 24/7 pool no longer grows (`du` daily). No GitHub reclaim docker actions in the journal on hosts running Gitea.
- PR 2: reap beacon present; adoption counter 0; failure rate on cloud-ci-9 within the pool range (the task→status method from the audit); no `lease does not exist` failures that line up with reaper actions.
- PR 3: one manual `systemctl start gitea-runner-cache-verify` per runner, `.prom` present. Purge on the canary: copy one uv archive, flip a byte, wait for the queue to apply, then `uv pip install` that package in a temp venv and confirm it re-downloads.

## Risks (likelihood × impact, with mitigation)

1. **A new job takes over a pre-B object by name before the edge pass** (medium × medium). Ships log-only with an adoption counter; StartedAt ≥ B is kept; periodic passes use only the timeout boundary; Compose-name follow-ups shrink the window.
2. **Capacity raised above 1** (low × high). Ansible assert plus script refusal.
3. **A forged or accidental task line** (low × high). `_PID`/`_COMM` match and anchored regex.
4. **act_runner log format changes** (medium on an upgrade × low). No B means only the timeout boundary applies, which is safe. Beacon `boundary_age = -1`.
5. **Clock skew** (low × high). Both estimates are boot-relative, the earlier one is taken, and the margin is 5 s.
6. **A slow docker daemon stretches the reclaim tick** (low × medium). Docker calls only on the edge and periodic passes, each with `timeout 15`.
7. **tmpfiles removes a cache file mid-job** (low × low). Caches only. Job-created files are under 3 h old against a 6 h minimum.
8. **The verifier's I/O during the CI day** (medium × low). CPUQuota, IOWeight and bandwidth caps.

<!-- codex-review-status: complete -->
