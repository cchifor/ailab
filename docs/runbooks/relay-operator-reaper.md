# The relay-operator reaper

The JIT automation identity (`relay-operator@ailab.invalid`; `docs/runbooks/relay.md`, "Operator automation identity")
holds Relay admin only while `scripts/relay-operator.py session` or `call` runs. Both revoke it in a `finally`. That
`finally` does not run if the orchestrator is SIGKILLed or the workstation is lost. In that case the membership would
stand, so this reaper removes it independently (plan: `plans/2026-11-27-relay-operator-session-plan.md`, section 3).

## How it works

- **The CronJob** `relay/relay-operator-reaper` runs every 5 min (`concurrencyPolicy: Forbid`, 120 s deadline). It has
  three steps:
  1. `lease-take` (python, `operator-reaper/lease.py take`) takes the fenced Lease `relay-operator-jit`, but ONLY when
     it is absent, released or expired. Every write is conditioned on `resourceVersion`. A live holder (a running
     `session` or `call`) is never touched: the step logs `skip` and the Job does nothing.
  2. `reap` (postgres) runs `SELECT * FROM ops.relay_operator_reap()` only if step 1 took the Lease, and logs the
     deleted session and membership counts. It is normally `0 0`.
  3. `lease-release` releases the Lease if the reaper still holds it. It holds the Lease for at most 120 s.
- **The database objects** are created by the idempotent Job `relay-operator-reaper-setup`. Flux force-replaces it on a
  spec change (`kustomize.toolkit.fluxcd.io/force: "enabled"`). It creates:
  - schema `ops`, owned by `relay_owner`;
  - `ops.relay_operator_reap()`: SECURITY DEFINER as `relay_owner`, a fixed `search_path`, lock and statement
    timeouts, and the JIT subject as a literal. It deletes only that identity's `sessions` and `memberships` rows;
  - the login role `relay_operator_reaper`. It can only EXECUTE that function: it cannot read or write any table and
    cannot create objects. This was verified against a disposable Postgres 17.

  The role's password is the SOPS Secret `relay-operator-reaper`.
- **RBAC.** The ServiceAccount `relay-operator-reaper` may get and update the Lease `relay-operator-jit`, and create
  leases (create cannot be restricted by name).
- **Network.** `relay-postgres-operator-reaper` additively admits the reaper and its setup Job to relay-postgres:5432.
  The existing `relay-postgres` policy is unchanged.

## Alerts

- `RelayOperatorReaperStale`: no successful run for more than 20 min.
- `RelayOperatorReaperFailing`: a failed reaper Job.
- `RelayOperatorReaperSetupFailed`: the database setup Job failed.

The reaper is a best-effort backstop, bounded by the scheduler, the API server and the database. While it alerts, check
by hand: `python scripts/relay-operator.py status` must report `0 memberships, 0 sessions`, and `revoke` clears it.

## Check it once (a gate before any automated phase A run)

1. `kubectl --context admin@ai -n relay logs job/relay-operator-reaper-setup` shows `reaper objects ready: 1`.
2. A normal tick logs `{"event":"reap","sessions_deleted":0,"memberships_deleted":0}`, or `skip` while a session holds
   the Lease.
3. The crash drill:
   1. Start `relay-operator.py session` with the smoke bundle and kill the process (`taskkill /F`) after `granted`.
   2. Within one Lease end plus one schedule, the reaper logs `memberships_deleted: 1`.
   3. `relay-operator.py status` then reports `0 memberships, 0 sessions`.
   4. Remove the leftover containers and volumes `relay-proxy-<run>`, `relay-runner-<run>` and
      `relay-session-<run>`.

## Roll back

Remove `operator-reaper.yaml`, `relay-operator-reaper.sops.yaml` and the `configMapGenerator` entry from
`kubernetes/apps/apps/relay/kustomization.yaml`. Then, as the postgres superuser:

```sql
DROP FUNCTION ops.relay_operator_reap();
DROP SCHEMA ops;
DROP ROLE relay_operator_reaper;
```
