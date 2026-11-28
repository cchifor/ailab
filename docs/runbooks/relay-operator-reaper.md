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
  2. `reap` (postgres) runs `SELECT * FROM ops.relay_operator_reap(<fence>)` only if step 1 took the Lease. The
     fence is the reaper's own Lease expires-at, and the function deletes nothing unless at least 30 s of the
     hold remain on the database's clock. A reaper that stalled past its hold fails closed, instead of revoking
     a successor's fresh grant. The step logs the deleted session and membership counts, normally `0 0`.
  3. `lease-release` releases the Lease if the reaper still holds it.

  The hold, `HOLD_S` = 180 s, outlasts anything the Job can still run: `activeDeadlineSeconds` 120 s, plus
  `terminationGracePeriodSeconds` 30 s, plus the fence's 30 s. Only `lease-take` and `lease-release` mount the
  API token, a projected volume; the pod has no automounted token. The `reap` container, which holds the
  database password, cannot touch the Lease.
- **The database objects** are created by `python scripts/relay-operator-reaper-setup.py`, which is idempotent. It
  applies the reviewed `operator-reaper/setup.sql` as the postgres superuser over relay-postgres' LOCAL socket
  (`kubectl exec`). There is no setup Job: pg_hba rejects the superuser over the network, deliberately, and no runtime
  role may hold CREATEROLE. The role password is read from the Secret in-process and goes only to psql's stdin,
  never to argv or a log; statement logging is off for that session. Run the script:
  - once after the reaper is deployed;
  - whenever `setup.sql` changes;
  - **after ANY relay-postgres restore or re-initialization** (roles are cluster globals and are easily lost).

  Without the objects, the reaper's `reap` step fails ("password authentication failed" or "function ... does not
  exist") and `RelayOperatorReaperFailing` fires. The script creates:
  - schema `ops`, owned by `relay_owner`;
  - `ops.relay_operator_reap(fence bigint)`: SECURITY DEFINER as `relay_owner`, a fixed `search_path`, lock and
    statement timeouts, the Lease fence, and the JIT subject as a literal. It deletes only that identity's
    `sessions` and `memberships` rows;
  - the login role `relay_operator_reaper`. It can only EXECUTE that function: it cannot read or write any table and
    cannot create objects. This was verified against a disposable Postgres 17.

  The role's password is the SOPS Secret `relay-operator-reaper`. The setup reads it from the environment
  (`\getenv`), never from psql's argv.
- **RBAC.** The ServiceAccount `relay-operator-reaper` may get and update the Lease `relay-operator-jit`, and create
  leases (create cannot be restricted by name).
- **Network.** `relay-postgres-operator-reaper` additively admits the reaper's pods to relay-postgres:5432.
  The existing `relay-postgres` policy is unchanged.

## Alerts

- `RelayOperatorReaperStale`: no successful run for more than 20 min, or no successful run ever (`absent`: a fresh
  install that never ran, a deleted CronJob, a missing scrape).
- `RelayOperatorReaperFailing`: a failed reaper Job.

The reaper is a best-effort backstop, bounded by the scheduler, the API server and the database. While it alerts, check
by hand: `python scripts/relay-operator.py status` must report `0 memberships, 0 sessions`, and `revoke` clears it.

## Check it once (a gate before any automated phase A run)

1. `python scripts/relay-operator-reaper-setup.py` prints `reaper objects ready: 1`.
2. A normal tick logs `{"event":"reap","sessions_deleted":0,"memberships_deleted":0}`, or `skip` while a session holds
   the Lease.
3. The crash drill:
   1. Start `relay-operator.py session` with the smoke bundle and kill the process (`taskkill /F`) after `granted`.
   2. Within one Lease end plus one schedule, the reaper logs `memberships_deleted: 1`.
   3. `relay-operator.py status` then reports `0 memberships, 0 sessions`.
   4. Remove the leftover containers and volumes `relay-proxy-<run>`, `relay-runner-<run>` and
      `relay-session-<run>`.

## Roll back

Remove the `operator-reaper` entry from `kubernetes/apps/apps/relay/kustomization.yaml` (everything lives in
`kubernetes/apps/apps/relay/operator-reaper/`, its own kustomization). Then, as the postgres superuser:

```sql
DROP FUNCTION ops.relay_operator_reap(bigint);
DROP SCHEMA ops;
DROP ROLE relay_operator_reaper;
```
