# Review round 1 — runner-between-job-cleanup plan

The reviewer was an independent Claude Plan agent. The Codex seat had no quota left: `usage_limit_reached`, resets 2026-10-18 06:20 UTC. The reviewer read the plan at `58706ceb` against the code it changes.

The original review is pasted below, verbatim apart from line wrapping. Each finding then has a classification: **ACCEPT** (folded into the plan), **PUSHBACK** (I disagree, with the reason) or **FACT** (checked on live hosts). The revised plan is `2026-10-12-runner-between-job-cleanup-plan.md`.

## Summary as given

- The direction is right. The task-start line is the only race-free job boundary act_runner offers. "Created before B" is sound for objects that no later job takes over by name.
- There are two ways to kill a live job:
  - the stale-idle `now − 10 s` boundary (permanent after a log-format change or a lost line, and briefly true every cloud morning);
  - the B-based 15-min re-pass, which hits anything a live job took over by name (Compose `up` reusing the shared `hostexecutor` project, `docker start X`, reused daemons).

  Use `max(B, now − job_timeout − margin)`, drop the re-pass, assert capacity == 1, and ship log-only first.
- Mechanics that don't match the real stack:
  - Docker ≥ 23 labels anonymous volumes.
  - `journalctl -u` includes `logger` lines from job processes.
  - A new `e /tmp` line collides with the vendor `D /tmp`.
  - relatime defeats the atime argument.
  - The reclaim env carries no INFRA_EXCLUDE_RE or peer settings.
- 1-C and 2-A build more than needed. Use `npm cache verify` and `uv cache clean`, and run the verifier as `runner`. Handle the other hardlinks of a corrupt inode. 1-D's busy check is too weak: skip the docker block on Gitea hosts, or retire the dormant agents.
- Alerts: on 6.8 ext4 shutdown the textfile series disappear; key on `scrape_error`. Find out why CloudCIRunnerCleanupBeaconMissing did not fire. Keep the CIRunner*/CloudCIRunner* split, gate staleness on boot time, and add `ci-runners-rules.test.yaml`.

## Findings and classification

| # | Finding (condensed) | Class | Resolution |
|---|---|---|---|
| 1 | The `hostexecutor` Compose collision needs a per-repo fix (`name:` or `COMPOSE_PROJECT_NAME: ${{ github.event.repository.name }}`). The reaper only mitigates it. | ACCEPT | Follow-up issues for platform, kartra-ai and relay. Listed under Follow-ups. |
| 2 | Verify the 30-day `/tmp` claim; Debian ships `D /tmp … -`. | FACT | Ubuntu 24.04 ships `D /tmp 1777 root root 30d` (seen on cloud-ci-9 and ci-runner-6). The claim stands. |
| 3 | Three hardlinks show the inode is shared, not that anything wrote to it, so 1-C is speculative. Collect evidence first. | ACCEPT | 1-C is deferred. The verifier records nlink, mtime and ctime against the archive dir for each bad file. |
| 4 | Find out whether the dead-fs window made the series absent or left them stale. | FACT | `node_textfile_scrape_error` went to 1 within about a minute on both VMs. The series count was unchanged. BeaconMissing went pending and was cleared by the reset about 4 min later. `node_filesystem_readonly` stayed 0. Fleet baseline today: 0 of 40 series at 1. |
| 5 | Split the PRs by risk. | ACCEPT | Three PRs: low-risk hygiene; the reaper in log-only mode; cache verification and alerts. |
| 6 | `journalctl -u` includes job `logger` lines, so B can be forged. | ACCEPT | Match `_PID=<MainPID>` and `_COMM=act_runner`, read `-o json`, and anchor the regex. |
| 7 | Safety rests on capacity 1. | ACCEPT | Ansible assert, plus the script refuses to reap if `config.yaml` capacity ≠ 1. |
| 8 | The journald receive time lags the log call, which pushes B in the unsafe direction. | ACCEPT | B = the earlier of the logrus `time=` (floored) and the journal monotonic stamp, both converted to boot time. Margin 5 s. |
| 9 | Store B and the boot id apart from the cursor; `state_get` rejects cursor characters. | ACCEPT | Separate key=value state with its own parser. |
| 10 | Take-over race: a new job can take over a pre-B container or daemon by name before the edge pass runs. | ACCEPT | Log-only first, plus an "adopted" counter (a would-reap object still in use on a later tick). Measure task-line → first `compose up`. Skip `StartedAt ≥ B`. |
| 11 | A B-based 15-min re-pass kills taken-over stacks mid-job. | ACCEPT | Re-passes use only the timeout boundary. |
| 12 | The stale-idle `now − 10 s` rule can kill live jobs. | ACCEPT | Removed. The boundary is always `max(B, now − job_timeout − 10 min)` (the timeout term only matters when B is old or missing). |
| 13 | PID reuse, the per-PID 10 s wait against TimeoutStartSec=120, zombies. | ACCEPT | Re-check starttime before each signal, use one shared wait, skip state Z. |
| 14 | The reap must run outside the `daemon_busy` early return, and be skipped in ExecStartPre. | ACCEPT | The reap phase sits before the busy gate. ExecStartPre passes `--no-reap`. |
| 15 | Skip containers with StartedAt ≥ B. | ACCEPT | Done. |
| 16 | The reclaim env lacks INFRA_EXCLUDE_RE and the peer vars, and the pin-exemption test must cover them. | ACCEPT | Render the same role vars, and extend `scripts/tests/test_dsh_validator_pin.py`. |
| 17 | The keep label should be a key match and apply to volumes and networks; reject `..` in working_dir. | ACCEPT | Done. |
| 18 | Select anonymous volumes by the `com.docker.volume.anonymous` label; compute references after the container pass. | ACCEPT + FACT | Label confirmed on Docker 29.8.1. |
| 19 | This reverses cleanup.sh's "no volume prune" safety model; check for lease failures on the canary. | ACCEPT | Update the header and runbook, and add a canary check. |
| 20 | Default the reaper to log-only. | ACCEPT | `gitea_runner_reap_mode: log` by default; `enforce` is a later one-line change. |
| 21 | relatime weakens the atime argument; argue only from creation/modification time. | ACCEPT | Rationale reworded. |
| 22 | pytest's retention count, not age, sizes `pytest-of-runner`. Use `-o tmp_path_retention_count=1` via PYTEST_ADDOPTS. | PUSHBACK (partial) | Accepted that the 6 h line frees little on busy runners, and the acceptance criterion is changed. Not a global PYTEST_ADDOPTS: on a repo with older pytest the option raises PytestConfigWarning, which fails under `-W error`. It is also a `config.yaml` change, so a fleet-wide drain. Retention becomes a per-repo follow-up. |
| 23 | An `e /tmp` line in a new file collides with the vendor `D /tmp`. | ACCEPT | Override `/etc/tmpfiles.d/tmp.conf` by name with `D /tmp 1777 root root 2d`. |
| 24 | runner.envs can be overridden; use `/etc/uv/uv.toml`; verify. | ACCEPT (deferred with 1-C) | Recorded for when 1-C ships. |
| 25 | `clone` is not a fallback; copy turns shared /tmp inodes into real bytes; `chmod a-w` on the archive is cheaper. | ACCEPT (deferred with 1-C) | `chmod a-w` becomes the preferred 1-C if the evidence shows in-place writes. |
| 26 | The 1-D busy check is too weak, and buildx/prune also need guarding. | ACCEPT | Skip the GitHub reclaim's whole docker block on hosts where `gitea-act-runner.service` is enabled. Retiring the agents is noted as a follow-up. |
| 27 | runner-scripts.yaml has no github_runner path, and `test_dsh_validator_pin.py` extracts "up to the first top-level fi". | ACCEPT | Add the path filter, and keep the guard inside the extracted block (or update the extraction). |
| 28 | The verify job's I/O lands in working hours; ionice is ignored under the `none` scheduler. | ACCEPT | CPUQuota=100%, IOWeight=10, IOReadBandwidthMax on `/`. |
| 29 | Use `npm cache verify` (it fixes index-v5), idle-gated. | ACCEPT (hybrid) | A Python walker detects and records evidence (read-only, as runner). Purge runs `npm cache verify` as runner, inside a race-free window. |
| 30 | Write the verifier in Python, as runner, with O_NOFOLLOW; skip `..` and lines without a hash. | ACCEPT | Done. |
| 31 | Purge uv with `uv cache clean <pkg>`, not by grepping pointers. | PUSHBACK | The host's uv is 0.5.11; jobs write the cache with newer setup-uv versions. An older `cache clean` may not know the newer buckets. Removing the archive dir plus every non-archive file that names its id is version-agnostic. The canary purges a deliberately corrupted copy and confirms uv re-fetches it. |
| 32 | Purging the cache leaves hardlinked corrupt inodes in tool envs and venvs. | ACCEPT | When nlink > 1, find the other links under known roots and remove each one's venv root (nearest `pyvenv.cfg`) or pytest session dir. |
| 33 | Idle gaps cannot be detected reliably; queue the purge and apply it at ExecStartPre; don't make a third copy of the idle logic. | ACCEPT | A purge queue file. It is applied by reclaim ExecStartPre (daemon down) and by gitea-runner-cleanup.sh inside its existing `idle_confirmed` window. No new copy of the logic. |
| 34 | Separate found from unpurged. | ACCEPT | `…_corrupt_found` and `…_corrupt_pending`. Alerts key on pending; found is a warning signal that points at the host. |
| 35 | Keep the CIRunner*/CloudCIRunner* and job-selector split. | ACCEPT | Done. |
| 36 | A weekend power-off makes staleness alerts fire on Monday. | ACCEPT | Gate on `time() - node_boot_time_seconds > 4h`. |
| 37 | The dead-fs alert should key on `scrape_error`. | ACCEPT + FACT | `scrape_error == 1 for 3m`. Confirmed live that it rises within about a minute. |
| 38 | Make the dead-fs alert critical; the runbook step is to disable the runner in Gitea from outside. | ACCEPT | Done. |
| 39 | Add `ci-runners-rules.test.yaml`. | ACCEPT | Done. |
| 40 | Critical-files omissions. | ACCEPT | List updated. |
| 41 | Tests run on live runners as `runner`: every signal must go through a mockable kill, journalctl must be mocked, and no real PID may be signalled. | ACCEPT | A `REAP_KILL` indirection, a mocked journalctl, plus an assertion. |
| 42 | Missing test cases (take-over, stale lines, capacity, forged line, anonymous label, env drift). | ACCEPT | Added. |
| 43 | The control node cannot run Ansible. | FACT | Runbook §7 l.195: the pool is installed by hand over SSH. The rollout steps are written out manually (mirroring the role) unless the WSL converge path works. Verify at deploy time. |
| 44 | cloud-ci-13's orphan won't survive a power-off; use a synthetic acceptance test. | ACCEPT | Root moves a planted `sleep` into the service cgroup and plants a labelled container and volume. The would-reap / reap log must name them. |
| 45 | Add risks: capacity, forged lines, take-over, a slow docker daemon stretching ticks. | ACCEPT | Done. |

## Round 2 (same reviewer, on the two pushbacks and the revised plan)

- **#22 dropped:** the per-repo follow-up stands. The reviewer's idea for later: let the reaper's edge pass remove `pytest-of-runner/pytest-N` dirs created before E.
- **#31 dropped:** the version-agnostic uv purge stands, with the canary re-download check.

New defects in the revision, all ACCEPTED and folded into the finalized plan:
1. The adoption probe missed Compose taking over a running container. It now uses `docker events --since B` for the would-reap ids.
2. The task-line regex must come from a captured real line and handle the tz offset. Enforce also requires `boundary_age ≥ 0`.
3. The `tmp.conf` override must keep every other vendor line. It is now rendered from the vendor file.
4. The runner-writable purge queue is applied as root. Hardened: strict id regex, the applier builds the paths, deletes run via `runuser`, walks are bounded, and everything is under a timeout.
5. Unbounded work and a GC race in the purge appliers. npm now deletes only the listed files (no `npm cache verify`), everything has a hard timeout, and ExecStartPre does deletion only.
6. A missing verify beacon went unalerted, and `info` still pages. Added `CacheVerifyBeaconMissing` and dropped the `CacheCorruptFound` alert (dashboard series only).

Plan finalized after round 2. Per the two-round cap, no further rounds.
