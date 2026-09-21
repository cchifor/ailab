# ADR 0028 — Read-only platform access for dev-worker agents: observe the cluster, read the databases

**Status:** PROPOSED (2026-09-21). Implementation on `feat/dev-worker-platform-access`; plan and
codex review trail in `plans/2026-09-21-dev-worker-platform-access-plan.md`.
**Relates to:** ADR 0021 (the agent credential plane and the sync-owned KV class this extends, and
whose Tier A this widens), 0020 (per-VM AppRole + agent-rendered files), 0019 (OpenBao as the estate
secret store), 0016 (the platform's application tiering), 0018 (the dev-worker agents that consume
this). Operator how-to: `docs/runbooks/dev-worker-platform-access.md`.

## Context

A codex agent on dev-worker-3, investigating an Airlock App-table draft bug, asked for "a
short-lived, read-only production Airlock database credential, restricted to this App's draft/table
metadata" and for the effective `app_user_tables_max_columns`. The literal answer was cheap (16 —
the code default; ailab sets no `APP__AIRLOCK__…` override) and the credential could have been one
seeded password for one worker and one database. The operator's direction was broader: **worker
agents need standing access to the platform's runtime components for debugging, analysis,
investigation and enhancement work** — generic, every worker, self-maintaining, revocable, IaC.

What the workers had (ADR 0020/0021): a per-slot AppRole, a root-owned `bao agent` rendering
file-shaped credentials, `cred` for ad-hoc reads, and two sync-owned kubeconfigs
(`tep_kubeconfig`, `helmtest_kubeconfig`) minted daily by `openbao-k8stoken-sync`. None of it
reaches the platform: `strive-pg-{rw,ro,r}` and every platform Service are ClusterIPs, and no worker
credential could see namespace `strive-ailab` at all.

What the platform is (platform repo, Flux `platform-cnpg`): CNPG `strive-pg` (PG 16.9, 3 instances),
13 logical databases all `OWNER app`. Every service connects as `app` — Keycloak included
(`KC_DB_USERNAME=app`); the workflow service's admin DSN connects as `postgres`; the integration
service mints per-tenant `tenant_<12hex>` LOGIN roles owning `platform_data_<tenant>` schemas in
`platform_managed`. **Row-level security, measured live 2026-09-21:** 25 of 27 `airlock` tables and
14 of 15 `workflow` tables carry policies, all `TO public`, all keyed on
`tenant_id = current_setting('app.tenant_id', true)`; several are `FORCE ROW LEVEL SECURITY`. So a
role reads a tenant's rows by setting one session GUC and reads *nothing* without it — the platform
already scopes by tenant, for every role, including owners.

## Decision

### 1. Reach the platform through the API server, not the LAN

Each live slot gets a ServiceAccount (`platform-access/platform-dw<N>`) and a third sync-owned
kubeconfig, `platform_kubeconfig`, minted by the existing `openbao-k8stoken-sync`. Every ClusterIP —
Postgres, Valkey, Hatchet, the Keycloak admin API, any service's internal endpoint — is reached with
`kubectl port-forward`, over the authenticated, audited path the workers already use for
tep/helmtest. One forward is one SPDY/WebSocket session multiplexing every TCP connection to that
port, so per-session forwards are not a cost worth engineering around.

**Rejected — a `strive-pg-ro-lan` NodePort.** It would be a second LAN exposure of production data,
source-IP-blind for the same Cilium SNAT reason `openbao-lan` documents, and it generalises badly:
one NodePort per component, each its own decision. **Rejected — a Cloudflare tunnel**: a third party
in the path, for the reason ADR 0020 already gives.

### 2. Observe-only RBAC: no Secrets, no exec, no writes

ClusterRole `dev-worker-platform-observer`, bound **by RoleBinding only** in `strive-ailab`,
`strive-sandboxes-ailab` and `platform-edge`: `get/list/watch` on workloads, `pods/log`, services,
endpoints/endpointslices, configmaps, events, PVCs, PDBs, HPAs, NetworkPolicies, Ingresses, and the
CNPG / Traefik / Cilium / Flux HelmRelease / ExternalSecret / Prometheus / KEDA / Kyverno /
agent-sandbox objects in those namespaces, plus `metrics.k8s.io` pods for `kubectl top`; `create` +
`get` on `pods/portforward`.

Absent, deliberately: **`secrets` (any verb)** — every platform credential lives in one;
**`pods/exec`, `pods/attach`, `pods/ephemeralcontainers`** — exec is a Secret read by another door
(every mounted file and env var in any container); **`serviceaccounts/token`**; and every mutating
verb.

**`pods/portforward` bounds only what the Kubernetes API can bound.** RBAC cannot scope a forward
to one Service, so a worker reaches any pod port in these namespaces. That is fine for Postgres
(read-only by privilege, Decision 3) and for anything that authenticates its own callers, but it
is NOT a general write boundary: **Valkey runs with `ALLOW_EMPTY_PASSWORD=yes`** (verified
2026-09-21), so a forward to it could issue `SET`/`FLUSHALL`. Accepted deliberately rather than
papered over — the cache is rebuildable and airlock carries an in-memory rate-limit fallback (ADR
0016); enumerating pod names in `resourceNames` breaks on every CNPG instance rename; and
service-side authz is a change in the platform repo this ADR does not get to assume. The runbook
and the agents' CLAUDE.md block say plainly that non-Postgres endpoints are read-only by
discipline, not by enforcement. If that stops being good enough, the fix is Valkey `requirepass`,
not a narrower rule here.

**The line this draws is worker-side vs cluster-side, not "Secrets are never read".** The pg-sync
CronJob below mounts `strive-pg-superuser` exactly as the platform's own `strive-pg-init-databases`
Job already does every ten minutes: a Flux-owned, reviewed, in-cluster workload whose code is in git.
A worker is an agent-driven machine outside the cluster; what *it* can read is what is being bounded.

Verified before granting (2026-09-21, names only, no values printed): the `strive` HelmRelease has
empty values and `hatchet`'s reference Secret names only; every ConfigMap key in the three namespaces
was checked — `keycloak-realm/realm.json` carries no `secret`/`clientSecret`/`password`/`value`
literals, `valkey.conf` no `requirepass`.

Escalation is a git change: a purpose-named Role + RoleBinding for one slot, with a removal date in
the PR body. No standing escalation object exists — ADR 0021 removed `claude-grant-write` for
exactly that reason and this does not reintroduce it.

### 3. Database access is a per-slot Postgres role, maintained by the cluster, published as sync-owned KV

`openbao-platform-pg-sync` (CronJob, ns `strive-ailab` — a Secret can only be mounted by a pod in its
own namespace; image `ghcr.io/cloudnative-pg/postgresql:16.9`, the operand image, which carries
python3 + psycopg2) maintains `dw<N>_platform_ro` per live slot:

- `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS NOREPLICATION CONNECTION LIMIT 10`,
  `GRANT pg_monitor`, and role-level `default_transaction_read_only = on`, `statement_timeout = 5min`,
  `idle_in_transaction_session_timeout = 60s`.
- Per database: `CONNECT`; for every user schema present at run time (`public`, and the
  `platform_data_*` tenant schemas) `USAGE` + `SELECT ON ALL TABLES/SEQUENCES`; and `ALTER DEFAULT
  PRIVILEGES FOR ROLE app` / `FOR ROLE postgres` `GRANT SELECT` — **database-wide, never `IN
  SCHEMA`, and never for a tenant role**: a per-schema default ACL is a dependency of that schema and
  a default ACL *for* a role blocks `DROP ROLE`, either of which would break the integration
  provisioner's per-tenant teardown. A table created by a tenant role becomes readable on the next
  daily run; the runbook says so.
- **Read-only is enforced by privilege, not by a setting.** The role owns nothing and holds only
  `SELECT`, so a write fails `permission denied` even after a session turns
  `default_transaction_read_only` off — on the primary as much as on a replica. The GUC default
  turns accidents into clearer errors; the `-ro` replicas add the server's own refusal. **Every
  run re-asserts the invariant rather than assuming it:** non-SELECT privileges are REVOKED on
  every table in every allowlisted database — and on every SEQUENCE, `USAGE` as well as `UPDATE`,
  because `USAGE` alone permits `nextval()`, which advances an application's id counter and is
  therefore a write (verified 2026-09-21; `information_schema.role_table_grants` does not cover
  sequences, so the audit reads `pg_class.relacl` for `relkind='S'` separately) — role memberships
  other than
  `pg_monitor` are revoked, and the catalog is then checked for any remaining non-SELECT grant —
  a grant made out of band is repaired, not merely undetected. The live proof runs for **every**
  slot before anything is published, kept ones included, inside a transaction that is always
  rolled back, so the check itself can never leave a probe table or row behind in production.
- **RLS is honoured, not bypassed.** With `NOBYPASSRLS` and the platform's `TO public` policies, a
  worker reads a tenant's rows by setting `app.tenant_id` (`platform psql --tenant <id>`, a libpq
  startup option) — the same mechanism the services use. Without it those tables read **empty**.
- **`keycloak` is excluded** (credential hashes, client secrets, sessions; nothing an app-debugging
  agent needs that the Keycloak admin API does not expose better). Exclusion means *no grant*:
  PUBLIC's `CONNECT` is untouched, so connecting succeeds and every table read is `permission denied`.
  The sync refuses to start if `keycloak` is ever added to `PLATFORM_DATABASES`.
- **Rotation is when-needed**, not forced: 14-day validity, and a run rotates a slot only when its KV
  fields are missing, the published validity has under 7 days left, the published password no longer
  logs in, or `FORCE_ROTATE=1`. Grants and attributes are re-asserted every run. Consequences: a
  password changes roughly weekly, a leaked one dies within 14 days, a week of missed runs expires
  nothing — and **a re-run is a no-op**, which is what makes the bootstrap Job's
  reap-and-re-apply retry loop safe and `kubectl create job --from=cronjob/…` always safe. A failed
  run surfaces through the stack's existing `KubeJobFailed` rule; no new alert.
- Ordering, fail-closed: OpenBao login first (a run that cannot publish must not rotate) → a
  **cluster-wide advisory lock** for the whole run (`concurrencyPolicy: Forbid` serialises the
  CronJob against itself but not against the bootstrap Job or a hand-made one, and two overlapping
  runs could otherwise publish a password the other had already replaced) → role changes in one
  transaction on the primary (`pg_is_in_recovery()` must be false) → **clean up, then grant**
  (load-bearing: `DROP OWNED BY` revokes privileges on SHARED objects too, so cleaning up after
  granting strips the database-level `CONNECT` just made — a real bug the integration test caught)
  → prove every live credential → only then patch KV, one patch per worker. Nothing logs a
  password.
- **The password never reaches the server in cleartext.** `ALTER ROLE … PASSWORD` is given a
  client-computed SCRAM-SHA-256 verifier, because every worker holds `pg_monitor` and
  `pg_stat_activity` shows other sessions' query text verbatim (measured 2026-09-21): a cleartext
  rotation statement would hand slot B's new password to slot A, and `pods/log` would expose it a
  second way. The sync's own session also disables statement logging.
- **A failed login triggers rotation only when the SERVER rejects it** (SQLSTATE 28xxx). A
  connection refused for capacity (`CONNECTION LIMIT` exhausted by the worker's own sessions) or
  for transport reasons exits the run without touching Postgres or KV — and that probe runs for
  every slot that has a published password, **including the ones already due for rotation**. That
  ordering is the point: a rotation commits the new password before it can be proven, so a proof
  that then fails for capacity would leave Postgres holding a password KV never received. (If it
  happens anyway — the proof can fail for other reasons — nothing unproven is published and the
  next run self-heals: the published password is rejected, so the slot rotates again.)
- Retiring a slot is `RETIRED_SLOTS`: `DROP OWNED BY` in every database, then `DROP ROLE IF EXISTS` —
  idempotent and resumable, the same shape `devworker-provision-job.yaml` uses for AppRoles.

**Rejected — `pg_read_all_data`**: cluster-wide, and it cannot exclude a database without revoking
PUBLIC's `CONNECT`, which the per-tenant roles rely on. **Rejected — a SOPS-seeded password**:
seed-wins would re-apply an expired credential daily, and a password in git is the shape ADR 0021
removed. **Deferred — OpenBao's `database` secrets engine** (the canonical Vault answer, dynamic
per-lease users): it needs a new engine, a privileged PG login held by the vault, a `bao agent`
template rollout, and `cred` cannot read non-KV paths. Revisit if per-lease revocation is ever
needed.

### 4. The worker side stays on the ADR 0021 rails, with a stricter gate

The agent renders `~/.platform/kubeconfig` and `~/.platform/pgpass`. Because a stanza for a field
that does not exist **exits the agent** (`error_on_missing_key` + `exit_on_retry_failure`) and would
take `~/.git-credentials` with it, the stanzas are emitted only when a pre-flight on this host, under
its own AppRole, **reads all three `platform_*` fields** (values discarded). The pre-flight
distinguishes *absent* from *unreachable*: a failing `cred list` (sealed vault, network, dead sink
token) **fails the play**, so a transient outage can neither crash the agent nor strip working
platform access from a host that had it.

The gate is a plain `set_fact` with **no `cacheable`** — play-scoped, re-derived every run. That is
the opposite choice from ADR 0021's durable marker, and deliberately: that marker answers "which of
two writers owns this file" and must survive a run; this one answers "is the vault publishing these
fields right now", which must not.

A `platform` helper (`kubectl`, `pf`, `psql` with `--tenant` and `--rw`, `env`) keeps the
port-forward lifecycle out of the agents' hands and defaults to the replicas. `postgresql-client`
joins the base packages. The managed CLAUDE.md block gains a "Platform (Strive) access" section
stating what is reachable, what is denied, the tenant-GUC and replica-cancellation caveats, and to
ask rather than hunt for a wider credential.

### 5. Tiering: Tier A widens to production read, with attribution and per-slot revocation

Under ADR 0021 §5, Tier A (agent-readable, `af/dev-workers/*`) held namespace-scoped k8s tokens and
a Gitea PAT. It now also holds a production **read** credential for the platform's databases (minus
keycloak, tenant-scoped where the platform scopes) and observe rights over three namespaces.

Accepted on the estate's stated posture — single-operator, and these same workers already hold the
shared Gitea PAT and the hypervisors' root key — with two properties that posture previously lacked:
**attribution** (a distinct ServiceAccount and a distinct Postgres role per slot: the API audit log
and `pg_stat_activity.usename` name the machine, unlike the shared `proxmox_ssh_key`) and
**per-slot revocation** (drop the RoleBinding subject; move the slot to `RETIRED_SLOTS`).

The **reviewer VMs are excluded**: they review diffs, hold no `cred`, and have no per-host KV
subtree. Every slot-bearing file says so, so nobody "completes" the list.

## Consequences

- **A worker can now read production data.** Bounded to SELECT, no keycloak, tenant-scoped by the
  platform's own RLS, and with no path to a Secret, an exec, or a write. The agents are told this in
  their CLAUDE.md block, including that an empty RLS table is a missing `--tenant`, not missing data.
- **A second sync-owned writer exists**, sharing the `k8stoken-sync` OpenBao policy (a per-slot KV
  write grant; minting is Kubernetes RBAC, not a vault capability). Post-wipe recovery gains two
  fields and one more Job — the ordering in `openbao-recovery.md` is unchanged in shape: vault + auth
  roles → both syncs → AppRole re-mint → restart agents.
- **The slot list grew from seven enumerations to twelve.** `scripts/check-slot-enumerations.py`
  (CI) fails when any of them disagree, and the retire-a-slot checklist in the runbook is its
  human-readable form. This is what keeps PR-C2 (retiring the dev-worker-3 VM, re-slotting 4→3, 5→4)
  from silently missing a file.
- **The pg-sync holds the CNPG superuser.** It is the third workload that does (the platform's init
  Job and the operator itself), it is Flux-owned, and it only ever creates/alters `dw<N>_platform_ro`
  and grants SELECT. Its logs print role and database names, never a password — asserted by the test.
- **A worker can write to a service that does not authenticate its callers.** `port-forward` is a
  TCP path (Decision 2) and Valkey has no password today; that is discipline in the runbook and
  the CLAUDE.md block, not enforcement. Revisit if the platform adds `requirepass`.
- **Two copies of the public ailab root CA now exist** (the ansible role's and the ConfigMap's,
  because a pod in `strive-ailab` cannot mount ns `openbao`'s TLS Secret). CI `cmp`s them.
- **A worker with no platform fields is normal, not broken.** A fresh slot, a wiped vault or a failed
  sync simply means no platform stanzas and a `debug` message on the next playbook run.

## Follow-ups

- **OpenBao's `database` secrets engine** for per-lease Postgres credentials, if revocation ever
  needs to be finer than per-slot.
- **A metric on the published validity** (`platform_pg_valid_until` per slot) rather than relying on
  `KubeJobFailed` plus the 14-day window.
- **The per-database allowlist is one env var** — adding a future platform database is a one-line
  change, and forgetting it is a `permission denied`, never silent wrong data.
- **`kubectl top` needs metrics-server**; if it is ever removed, that rule is dead weight, not an error.
