# Reserved short-job lane

Status: prepared; requires a drained host apply and a routing canary. Merging
this Ansible change does not apply it. Do not set consumer routing variables
before verifying the live runner.

Reserve existing `ci-runner-4` (`192.168.0.17`) for `ci-short:host`, capacity 1.
Its host variables override the normal `self-hosted-hv` label. Do not give it
both labels: long jobs would occupy the reserved slot. No VM/IP allocation,
capacity increase, cache deletion, or cleanup-policy change is involved.
This removes one heavy slot (5% of the currently usable 20-runner daytime pool;
12.5% of the eight always-on runners when cloud capacity is offline).

Only short discovery/admission/final-gate jobs should use `SHORT_RUNNER_LABEL`.
Keep `STATIC_RUNNER_LABEL` and `RUNNER_LABEL` unchanged: static E2E checks and
consumer tests can take minutes, so moving every static job to one runner can
create a new bottleneck. Start with Platform only; do not set an org-wide
variable during qualification.

## Apply from the existing infrastructure control host

1. Record the Gitea runner ID, enabled state and labels for `ci-runner-4`.
   Disable that runner in the forge (`PATCH /api/v1/admin/actions/runners/<id>`
   with `{"disabled":true}`), preserving all other runners and any previously
   disabled state. This prevents new work; do not stop the daemon yet.
2. Wait for its assigned jobs to finish. Check running jobs at the forge and
   on the host: obtain `MainPID` with `systemctl show -p MainPID --value
   gitea-act-runner.service`, then inspect `pgrep -P <pid>`. Require an active,
   nonzero daemon PID and no children on two observations 20 seconds apart.
   The API `busy` flag alone is unreliable. Do not restart a running job:
   the daemon's ten-minute shutdown allowance is shorter than Python/E2E.
3. Apply only this host with the existing role/playbook:
   `ansible-playbook -i inventory/hosts.yml ansible/gitea-runners.yml --limit ci-runner-4`.
   Use the estate's configured SOPS/SSH credentials. Where Ansible is
   unavailable, follow the existing `ci-runners.md` SSH installation procedure:
   change only the `runner.labels` entry in the existing config to
   `ci-short:host`, retain capacity 1, and restart the drained daemon.
4. Verify the daemon is healthy and its runtime config says `ci-short:host`.
   Verify the same runner ID advertises **only** `ci-short` at Gitea, then
   re-enable it if it was enabled before. A config/restart updates labels;
   do not delete `.runner`, re-register it, or edit its secret state.
5. Run a small checkout/shell canary against `ci-short`. Confirm the job's
   runner ID, host execution, exit-status propagation and timeout behavior.
   Only after it passes, set Platform's repository variable
   `SHORT_RUNNER_LABEL=ci-short`; keep all other repositories unchanged.

## Qualification and rollback

Capture a contemporaneous before/after cohort using `scripts/ci-queue-stats.py`.
Record dependencies separately from runner wait, unknown readiness, cancelled
work, hourly arrivals, cloud online count and host pressure. Compare short-job
p50/p90 wait, short-runner utilization, and heavy Python/E2E p90 wait. Include
nighttime capacity; do not call the reserve accepted from a quiet daytime run.
Use at least 20 complete Platform PR workflow cohorts before expanding routing.
Rollback if a reserved job cannot start/execute, checks disappear, or matched
heavy-job p90 wait regresses more than 10% without an explained fleet change.
Hold expansion if confidence is insufficient.

Rollback routing first: remove the Platform `SHORT_RUNNER_LABEL` override so
new runs use their original expressions. Already queued jobs retain their old
label: let them complete on the reserve before removing its label. Then disable,
drain, restore `self-hosted-hv:host` via the same host variable/role and restart;
verify labels before re-enabling. Preserve all failed/cancelled observations.

Gitea's [runner label documentation](https://docs.gitea.com/1.26/usage/actions/act-runner/#labels)
explains the distinction between the scheduling label and execution mapping.
The existing [runner runbook](ci-runners.md) owns drain and restart procedures.
