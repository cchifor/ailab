# Complete Forge gate on ci-runner-9

Reserve the existing, always-on `ci-runner-9` VM for a single complete Forge CI job. This is
owner-operated infrastructure. The infrastructure PR requires owner review; keep the dependent
Forge workflow in draft until the ordered rollout below has been observed. Source checks do not
certify the live gate.

The dependent [Forge routing PR319](https://git.chifor.me/cchifor/forge/pulls/319) preserves the single
complete test invocation and must remain draft until this runbook's live prerequisites are met.

## Scope and budget

| Setting | Dedicated worker | Ordinary workers |
|---|---|---|
| Host | `ci-runner-9`, `192.168.0.31`, VM4109 on `ai-node1` | Other seven always-on ailab workers |
| Sole routing label | `forge-complete:host` | Existing `self-hosted-hv:host` labels |
| Capacity | 1 | 1 |
| Complete job timeout | 10h / 600min | 3h |
| Graceful shutdown / systemd stop deadline | 600min / 601min | 10min / 11min |
| Minimum prune window / running-container age | 11h / 11h | Existing 4h / 4h |
| Gitea endless-task ceiling | 12h, global | Same ceiling; daemon still limits jobs to 3h |

The ten-hour daemon deadline includes checkout, bootstrap, the entire unchanged `scripts/test.sh`
invocation and evidence upload. `act_runner` enforces its supported `runner.timeout`; Gitea 1.26
does not enforce workflow `timeout-minutes`. The planning measurement is approximately seven hours
for a complete Forge run, leaving about three hours for variability and surrounding steps. A
timeout fails the gate. Do not split the suite, omit providers or browsers, retry tests, raise
capacity, or substitute diagnostic results for the complete gate.

`inventory/hosts.yml` and `kubernetes/infra/runners/variables.tf` already declare this VM. It has no
`ci-runner-1` conductor-image-pin exception and stays on its physical node. Reserving it reduces the
ordinary always-on pool from eight to seven. The cloud pool is unchanged; its hosts can power off
overnight. No VM is created, resized or moved, and no CPU, memory or task cap is raised.

The declared shape is eight vCPUs, a 24 GiB ceiling, a 10 GiB balloon floor and a 200 GB disk. The
runner service remains capped at 10 GiB; Docker workloads also consume guest memory outside that
cgroup. Inventory notes describe historical physical-node memory pressure. The owner must compare
actual guest/node headroom, disk high-water usage and maintenance plans against the complete Forge
workload. Longer cleanup windows retain more data. Critical disk pressure can still interrupt an
in-flight pull through containerd GC despite the age floor.

## Owner rollout

1. Confirm live runner/Gitea versions, effective timeouts and cleanup settings through approved
   operator access. The repository pins act_runner 0.6.1 and Gitea chart 12.6.0; source declarations
   do not verify live versions or overrides. Record selected non-secret fields only. Confirm that
   ci-runner-9 can be reserved and that losing one ordinary slot is acceptable.
2. Schedule the single-replica Gitea `Recreate` rollout and worker conversion. Keep the Forge
   workflow on existing routing until this infrastructure is qualified. Pause scheduling to
   ci-runner-9 in BOTH Gitea and legacy GitHub, observe both queues with no active jobs, and inspect
   the host for surviving task processes and containers. Missing `Runner.Worker` does not prove
   Gitea idle: host-mode Gitea jobs use different processes.
3. With both queues drained, stop `gitea-act-runner.service` and
   `actions.runner.cchifor-platform.service`. Verify both existing units are stopped and no
   `act_runner` or `Runner.Worker` process remains. Keep scheduling paused through conversion.
   Do not stop a live job to satisfy this step. The old daemon still has its old drain budget;
   changing a file does not change an already running daemon's deadline.
4. Merge the owner-reviewed ailab change. Wait for the GitHub mirror and Flux to reconcile the
   Gitea HelmRelease, the Recreate rollout to finish and the forge to recover. Verify intended
   `ENDLESS_TASK_TIMEOUT=12h` and the effective runtime setting. Leave heartbeat/zombie detection
   unchanged. Do not infer the effective setting from the HelmRelease alone.
5. Apply `ansible/gitea-runners.yml` **only to ci-runner-9** through the normal approved SOPS/Ansible
   workflow (`--limit ci-runner-9`). The `gitea_runner_exclusive_host` guard runs before any runner
   configuration/binary change and fails closed if either existing service is not stopped or a
   runner process survives. It then stops and disables any installed legacy GitHub unit.
   `github_runner_agent_enabled: false` alone only skips installation and would leave an existing
   service and its Docker reclaim hook active. Preserve its role-managed regular unit file under
   `/etc/systemd/system`; masking would conflict with that file. Keep the peer-service cleanup
   probe; the stopped peer is inactive. Do not run a fleet-wide restart.
6. Verify runner timeout 10h, shutdown 600m, capacity 1, sole label `forge-complete:host`, systemd
   `KillMode=mixed`/`TimeoutStopSec=601min`, both cleanup age fields 39600 seconds, and pressure/critical
   retention 11h. Check that GitHub stays stopped and disabled. Verify advertised labels
   through Gitea after daemon declaration; preserve its registration/name. Do not delete `.runner`,
   re-register it or print its credential fields.
7. Resume only the dedicated Gitea registration. Run `scripts/check-ci-runners.py` with approved
   read-only operator credentials. It checks the dedicated host and advertised API label, rejects
   that label on another worker, and retains the ordinary monitored-worker checks. Targeted SSH
   probes do not narrow the API gate. Confirm every other ordinary worker retains its 3h timeout
   and label. Freeze unattended runner, Docker, VM and physical-node maintenance for qualification.
8. Only after these observations, activate Forge's separate change selecting `forge-complete`.
   Run one complete same-head `scripts/test.sh` job with zero test retries. Record exact commit,
   runner identity, duration, all required results and uploaded evidence. Missing, timed-out,
   skipped or stale evidence fails qualification. Independent review and required CI still apply.

Every subsequent Ansible maintenance run on this dedicated host must repeat scheduling pause,
observed drain and stopping both services before the guard. Its 600-minute graceful shutdown is a
recovery bound, not permission to interrupt live validation or force a VM reboot.

## Rollback

1. Pause dedicated scheduling and hold the dependent Forge workflow in draft. Let any full job
   finish, or have the owner explicitly abort it and record a failed result. Observe no active
   Gitea/GitHub jobs or task processes before stopping the dedicated daemon.
2. Revert the reviewed ailab change and restore ordinary configuration through a host-limited
   Ansible apply. Verify timeout 3h, drain 10m/11min, cleanup age/floor 4h and ordinary host label.
   Do not lower cleanup windows while long-job resources are in use. GitHub stays disabled until the
   owner deliberately restores it through its normal role; git revert does not undo systemd
   state. Do not enable competing pools or run their reclaim hooks over retained evidence.
3. After no dedicated task remains, revert/reconcile the server ceiling in a scheduled Gitea
   Recreate window. Verify the effective restored owner value, ordinary scheduling and monitor.
   Preserve small diagnostic evidence; retire rebuildable artifacts through established cleanup.

## Validation before promotion

Run the full `scripts/tests/test_*.py` suite, both mocked runner cleanup/reclaim suites and the Gitea
manifest render. `test_forge_complete_gate.py` renders existing templates, checks budget/cleanup
relationships and exercises stopped-service predicates. `test_check_ci_runners.py` covers exact
per-host routing and API advertisement mismatches. Live headroom, settings, drain, retirement,
scheduling and a successful complete Forge run require the observations above.
