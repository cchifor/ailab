# Gitea DB migration: SQLite → CNPG Postgres (infra-pg tenant)

**Date:** 2026-07-21 · **Status:** ✅ DONE 2026-07-22 (ailab PR #79) · **Scope:** ailab repo

> ## Outcome (as-built, 2026-07-22)
>
> Gitea is live on Postgres (`infra-pg`). ~26 min downtime. Verified: 1/1 on Postgres, `healthz`
> `database:ping` pass, `doctor` db-consistency OK, forge serves migrated repos, all Flux green.
>
> **The migration method changed from what this design first specified.** The original plan let
> **pgloader create the target schema**. That was **attempted 2026-07-21 and FAILED**: SQLite is
> untyped, so pgloader inferred `bigint` for columns Gitea's XORM structs define as `boolean`/`smallint`,
> and Gitea crashed on startup with `pq: invalid input syntax for type bigint: "false"`. Rolled back with
> zero data loss (pgloader only *reads* `gitea.db`).
>
> **The method that shipped is schema-first + data-only:**
> 1. **Gitea builds the schema itself** — `gitea migrate` against the empty `gitea` DB creates all ~112
>    tables with the correct XORM types (`boolean`, `smallint`, `bytea`).
> 2. **pgloader loads DATA ONLY** — `WITH data only, truncate` into that schema. No type inference, and
>    **no CAST rules needed** (Postgres `COPY` coerces SQLite `0/1` straight into the real `boolean`
>    columns). Gitea has no DB-level FKs/triggers, so no `disable triggers`.
> 3. **Reset sequences** with an explicit `setval` pass (pgloader's `reset sequences` is unreliable in
>    data-only mode).
>
> Proven end-to-end in an **isolated CSI-snapshot-clone rehearsal** (the live Gitea PVC is RWO and can't
> be second-mounted, so: VolumeSnapshot → clone PVC → load → dry-boot) with the live Gitea untouched,
> *before* the production window. `docs/…/plans/2026-07-21-gitea-postgres-migration.md` Task 1 shipped
> as-planned; **Tasks 2–5's pgloader-create-schema details are SUPERSEDED by the above** and retained
> only as a record of the original plan.

## Problem

Gitea runs on **SQLite** on the RWO `qnap-iscsi` `/data` LUN. SQLite is a single global writer;
under AgentForge's polling load (planner/dispatcher/reaper read loops + issue/comment/Actions writes)
writers get starved → recurring `database is locked` 500s (mitigated, not cured, by the WAL-mode +
10s busy-timeout added 2026-07-20). The DB also drives block churn on the thin LUN. Moving the DB to
Postgres (real MVCC) eliminates the lock-contention class.

## Decisions (approved)

- **Scope: DB-only.** Migrate SQLite→Postgres; keep Gitea **single-replica** on the existing RWO
  `/data` PVC (git repos, LFS, avatars stay there). Full multi-replica HA (RWX `/data` + Redis) is a
  separate, larger effort — explicitly **out of scope**.
- **Target: shared `infra-pg`** (CNPG, PG17, `databases` ns) as a new tenant, alongside
  grafana/authelia/openwebui/agentforge. Reuses existing HA/backup/operator; one ailab PR set.
- **Method: `pgloader`** (standard SQLite→PG tool; Gitea's XORM schema is engine-agnostic). **As-built
  correction (see Outcome):** pgloader must run **`data only`** into a schema **Gitea builds itself**
  (`gitea migrate`) — letting pgloader create the schema produces wrong column types and Gitea won't boot.
- **Cutover: one maintenance window**, ~10–15 min downtime. Default = migrate the whole DB. **As-built:**
  actual downtime was **~26 min** (the ~5 GB DB is mostly `dbfs_data` Actions-log BLOBs; the data-only
  `COPY` runs ~22 min). Pruning the stuck Actions logs first would shorten a future run.

## Non-goals

- No multi-replica Gitea / RWX storage / external session-cache-queue (future).
- No change to Actions log/artifact S3 offload or `LOG_RETENTION_DAYS` (orthogonal; already fixed).
- No immediate `/data` PVC shrink (deferred follow-up once SQLite is retired).

## Architecture

```
BEFORE                                     AFTER
Gitea (1 replica)                          Gitea (1 replica — unchanged count)
 ├─ DB → SQLite on /data (RWO LUN)    →      ├─ DB → gitea DB on infra-pg (PG17, HA)
 └─ /data: repos, LFS, avatars, DBFS         └─ /data: repos, LFS, avatars (RWO, unchanged)
```
Only the database moves. DBFS (Actions live-log buffer) becomes Postgres tables instead of SQLite.

## Changes (all in ailab repo)

**1. infra-pg gitea tenant** (`kubernetes/apps/databases/`), mirroring the litellm convention:
- Add `gitea` role to `infra-pg.yaml` `spec.managed.roles` → `passwordSecret: infra-pg-gitea`.
- `infra-pg-gitea.sops.yaml` — SOPS-encrypted password secret.
- `gitea-db-bootstrap.yaml` — idempotent superuser one-shot (`\gexec` CREATE ROLE/DATABASE) creating
  the `gitea` DB, **kept out of `kustomization.yaml`** so a failure can never wedge the layer.
  **Resolved at impl time (2026-07-21):** a declarative CNPG `Database` CR is NOT available here — that
  CRD ships in CNPG **>= 1.25** and the estate operator is **1.24.1**. The earlier assumption that 1.24
  served it was wrong, and acting on it (`00dd55a`) wedged the `databases` Kustomization for 5 days and
  cascaded into `apps` / `agentforge-tenants` / `edge-connector`. The bootstrap one-shot is the
  supported path, not a fallback.

**2. Data migration** — one-shot **pgloader** Job in the **`gitea` namespace** (PVCs are namespace-scoped,
so the Job must live where the `/data` PVC is):
- Mounts the **stopped** Gitea `gitea-shared-storage` PVC to read `/data/gitea.db`.
- Connects to `infra-pg-rw.databases.svc:5432` (cross-namespace DNS) and loads the `gitea` DB.
  pgloader handles schema + data + sequences. The `gitea` DB password must be available in the `gitea`
  ns for the Job — provide it via a small gitea-ns SOPS secret (or a superuser DSN) at impl time.
- Post-load: verify table list + key row counts match SQLite (e.g. `user`, `repository`, `issue`,
  `action_run`, `action_task`); reset sequences if pgloader didn't (it does by default).

**3. Gitea repoint** (`kubernetes/apps/apps/gitea/gitea.yaml` HelmRelease):
- `config.database`: `DB_TYPE=postgres`, `HOST=infra-pg-rw.databases.svc:5432`, `NAME=gitea`,
  `USER=gitea`, `SSL_MODE=disable` (in-cluster), password via `GITEA__database__PASSWD` env from the
  `infra-pg-gitea` secret (same `additionalConfigFromEnvs` pattern already used for S3/metrics).
- Remove `SQLITE_JOURNAL_MODE` / `SQLITE_TIMEOUT` (SQLite-only).
- Keep `persistence` `/data` (git repos). `strategy: Recreate` stays (still RWO `/data`).

## Cutover sequence (one window)

1. **Land tenant PR** (part 1) → Flux creates `gitea` DB + role on infra-pg. Verify both exist.
2. **Scale Gitea to 0** (`kubectl -n gitea scale deploy gitea --replicas=0`) → consistent SQLite.
3. **Run pgloader Job** → verify counts match.
4. **Land repoint PR** (part 3) → Flux upgrades Gitea onto Postgres; pod starts (scale back to 1).
5. **Verify** (success criteria below).

## Rollback

pgloader only **reads** `gitea.db`; the SQLite file is untouched. On any failure: `git revert` the
repoint PR → Gitea restarts on SQLite. **Zero data loss.** SQLite file retained ≥1 week before any
`/data` reclaim.

## Success criteria / verification

- Gitea pod healthy on Postgres; `[database] DB_TYPE=postgres` in effective app.ini.
- OIDC (Authelia) login works; `git clone`/`push` over HTTPS + SSH work.
- Issues/PRs render; an Actions workflow runs green end-to-end (logs stream via DBFS-in-PG → S3).
- `/metrics` serves; no `database is locked` and no DB errors in logs under load.
- Row counts for core tables match pre-migration SQLite.

## Risks & mitigations

- **pgloader schema quirks** (Gitea has ~250 tables): dry-run counts + a rollback path. If pgloader
  chokes on a table, `gitea dump`→fresh-restore is the fallback.
- **Large DB slows cutover** (~5 GB, mostly Actions logs): acceptable in-window; optional pre-prune of
  stuck DBFS logs if we want it faster (declined by default).
- **infra-pg blast radius**: gitea becomes a co-tenant; its ~5 GB (retention-bounded) load is light,
  but a shared-cluster incident now also affects Gitea. Accepted (infra-pg is HA).

## Follow-ups (out of scope)

- Reclaim/shrink `/data` once SQLite is retired (thin LUN won't UNMAP — likely a fresh smaller PVC).
- Revisit multi-replica Gitea HA (RWX `/data` + Redis) if warranted.
