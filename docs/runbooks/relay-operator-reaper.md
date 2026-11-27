# The relay-operator reaper

The JIT automation identity (`relay-operator@ailab.invalid`; `docs/runbooks/relay.md`, "Operator automation identity")
holds Relay admin only while `scripts/relay-operator.py session` or `call` runs. Both revoke it in a `finally`. That
`finally` does not run if the orchestrator is SIGKILLed or the workstation is lost. In that case the membership would
stand, so this reaper removes it independently (plan: `plans/2026-11-27-relay-operator-session-plan.md`, section 3).

## How it works

- **The CronJob** `relay/relay-operator-reaper` runs every 5 min (`concurrencyPolicy: Forbid`, 120 s deadline,
  30 s termination grace). It has one init container and two cooperating containers:
  1. `lease-take` (python, `operator-reaper/lease.py take`) takes the fenced Lease `relay-operator-jit`, but ONLY when
     it is absent, released or expired. Every write is conditioned on `resourceVersion`. A live holder (a running
     `session` or `call`) is never touched: the step logs `skip` and the Job does nothing.
  2. `reap` (postgres) requests a permit when it is ready. `lease-guard` (Python) freshly reads the Lease and
     requires the exact acquired holder, resourceVersion and original expiry, with at least 45 s remaining.
     It writes that expiry atomically to the private shared volume. No renewal or reacquisition occurs.
     The token-free database container calls `ops.relay_operator_reap(expiry)` with a 10 s connection timeout.
     On the database clock, the function requires at least 30 s remaining before deleting and checks expiry
     again afterward; either refusal rolls the whole statement back. The role has a 20 s statement timeout
     and a 25 s PostgreSQL 17 transaction timeout. It logs deleted counts, normally `0 0`.
  3. Only a successful terminal SQL reply writes completion. `lease-guard` then releases its own Lease.
     If the SQL reply is failed or lost, no early release is claimed: the original 180 s Lease expires.
     Handshake waits are bounded; Job cancellation is a cleanup backstop, not the database execution fence.
- **The database objects** are created by the idempotent Job `relay-operator-reaper-setup`. Flux force-replaces it on a
  spec change (`kustomize.toolkit.fluxcd.io/force: "enabled"`). It creates:
  - schema `ops`, owned by `relay_owner`;
  - `ops.relay_operator_reap(bigint)`: SECURITY DEFINER as `relay_owner`, a fixed `search_path`, lock and statement
    timeouts, and the JIT subject as a literal. It deletes only that identity's `sessions` and `memberships` rows;
  - the login role `relay_operator_reaper`. It can only EXECUTE that function: it cannot read or write any table and
    cannot create objects. This was verified against a disposable Postgres 17.

  The role's password is the SOPS Secret `relay-operator-reaper`; setup reads it with psql `\getenv`, without
  putting it in psql arguments. Setup removes the former zero-argument function so it cannot bypass the deadline.
- **RBAC.** The ServiceAccount `relay-operator-reaper` may get and update the Lease `relay-operator-jit`, and create
  leases (create cannot be restricted by name). Pod token automount is disabled. A projected token and CA are
  mounted only in `lease-take` and `lease-guard`; neither has the database password. `reap` has no API token.
  The shared memory-backed volume uses the pod's group 65534; the containers run without root or added capabilities.
- **Network.** `relay-postgres-operator-reaper` additively admits the reaper and its setup Job to relay-postgres:5432.
  The existing `relay-postgres` policy is unchanged.

## Alerts

- `RelayOperatorReaperStale`: no successful run for more than 20 min, using creation time until the first success.
- `RelayOperatorReaperUnobserved`: both CronJob creation and last-success telemetry are absent for 10 min.
- `RelayOperatorReaperFailing`: a failed reaper Job.
- `RelayOperatorReaperSetupFailed`: the database setup Job failed.

The reaper is a best-effort backstop, bounded by the scheduler, the API server and the database. While it alerts, check
by hand: `python scripts/relay-operator.py status` must report `0 memberships, 0 sessions`, and `revoke` clears it.

The Lease annotation is a client-interpreted epoch timestamp, not a Kubernetes server-enforced lock. The existing
broker accepts the 180 s hold without a protocol change. Correct overlap exclusion still assumes synchronized broker,
node and database clocks and no privileged early Lease replacement. The SQL checks close stale-start and bounded
execution gaps; they do not create an atomic transaction spanning Kubernetes and PostgreSQL. Monitor clock health.

## Offline qualification

Run the focused module with `python3 -m unittest scripts.tests.test_relay_operator_reaper` on Linux. It exercises
the actual Lease module and the manifest's shell with a synthetic API/psql boundary, including delayed ownership,
changed revisions, malformed expiry, one-shot permits, failed SQL replies and credential separation.
`python scripts/tests/integration/test_relay_operator_reaper_postgres.py` requires the cached deployment image and
Docker; it creates a private network-disabled PostgreSQL 17 instance, tests setup twice, retired-entry refusal,
late dispatch, exact-subject deletion, restricted privileges and timeout rollback. Missing Docker/image fails.
`scripts/rules-lint.sh` discovers the single-document `operator-reaper-rules.yaml` and its real PromQL fixtures.
The child `operator-reaper/kustomization.yaml` keeps its generator independent of optional Relay components.
These tests use synthetic data and prove no deployment, real Lease ownership, or live cleanup.

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

Remove `operator-reaper.yaml`, `operator-reaper-rules.yaml`, `relay-operator-reaper.sops.yaml` and `operator-reaper/` from
`kubernetes/apps/apps/relay/kustomization.yaml`. Then, as the postgres superuser:

```sql
DROP FUNCTION ops.relay_operator_reap(bigint);
DROP SCHEMA ops;
DROP ROLE relay_operator_reaper;
```
