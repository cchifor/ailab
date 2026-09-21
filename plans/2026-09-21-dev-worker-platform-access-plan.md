# Dev-worker platform access plane: observe, reach and read the Strive platform from every worker

Repo `ailab` · branch `feat/dev-worker-platform-access` off `gitea/main` `0ce2137f` (PR-C1 merged).
Decision record to be written as **ADR 0028**; operator how-to as
`docs/runbooks/dev-worker-platform-access.md`.

## Context

A codex agent on dev-worker-3 investigating an Airlock App-table draft bug asked for "a short-lived,
read-only production Airlock database credential" plus the effective `app_user_tables_max_columns`
(verified live in the `airlock` pod: **16**, the code default — no `APP__AIRLOCK__…` override on
ailab). The one-off answer would have been a seeded password for one worker and one database. The
operator's direction (2026-09-21) is broader: **worker agents need standing access to the platform's
runtime components for debugging, analysis, investigation and enhancement work**, and it must be
generic — every worker, every component, self-maintaining, revocable, IaC.

What exists today (ADR 0020/0021): each live dev-worker slot has its own OpenBao AppRole; a
root-owned `bao agent` renders file-shaped credentials and feeds `cred`; the
`openbao-k8stoken-sync` CronJob mints per-slot bound SA tokens with the TokenRequest API and
publishes rendered kubeconfigs into `af/dev-workers/dev-worker-<N>` as **sync-owned** fields
(`tep_kubeconfig`, `helmtest_kubeconfig`). Nothing reaches the platform: `strive-pg-{rw,ro,r}` and
every platform Service are ClusterIPs, and no worker credential can see namespace `strive-ailab`.

The platform's data plane (platform repo, Flux Kustomization `platform-cnpg`): CNPG cluster
`strive-pg` (PG 16.9, 3 instances, primary `strive-pg-9`), 13 logical databases all `OWNER app`
(`airlock profile keycloak knowledge mcp workflow hatchet integration notification digest
platform_managed tms sentinel`), plus `app`'s own bootstrap DB. Every service connects as `app`
(Keycloak included: `KC_DB_USERNAME=app`); the workflow service's `db-admin-url` connects as
`postgres`; the integration service mints per-tenant `tenant_<12hex>` LOGIN roles that own
`platform_data_<tenant>` schemas in `platform_managed`. **Row-level security, verified live:** 25 of
27 `airlock` tables and 14 of 15 `workflow` tables carry RLS policies, all `TO public`, all keyed on
`tenant_id = current_setting('app.tenant_id', true)` — i.e. any role reads a tenant's rows by setting
one session GUC, and reads nothing without it; several tables are `FORCE ROW LEVEL SECURITY` (the
owner is scoped too). The `strive-pg-superuser` Secret (CNPG-generated, `enableSuperuserAccess:
true`) is consumed by the platform's own init Job, which re-runs every ~10–20 min (TTL 600 + 10 min
reconcile) and only ever adds privileges to `app`.

Fleet state that constrains the design: PR-C1 (#809) retired slot 6; **PR-C2 will retire the
dev-worker-3 VM and re-slot ex-dw4→3, ex-dw5→4**, ending at four slots. Every per-slot enumeration
in this change must therefore be a one-line edit per slot, in the same files PR-C2 already touches
or in files it can extend, and retired slots must have a codified revocation path.

## Decisions (to be recorded in ADR 0028)

1. **Reach through the API server, not the LAN.** A per-slot ServiceAccount + read-only RBAC in the
   platform namespaces, minted into a third sync-owned kubeconfig (`platform_kubeconfig`). Every
   ClusterIP (Postgres, Valkey, Hatchet, Keycloak admin, any service's internal API) is reached with
   `kubectl port-forward` over the authenticated, audited API server path the workers already use for
   tep/helmtest. One `port-forward` process is one SPDY/WebSocket session that multiplexes every TCP
   connection to that port; `platform psql` opens one per psql session and a persistent forward is
   one `platform pf` away — connection churn is not a design concern at debugging volumes. Rejected:
   a `strive-pg-ro-lan` NodePort (a second LAN exposure of production data, SNAT-blind to the client
   like `openbao-lan`, and it generalises badly — one NodePort per component); a Cloudflare tunnel
   (third party in the path, ADR 0020's reasoning).
2. **Observe-only RBAC, no secret material, no exec, no writes.** ClusterRole
   `dev-worker-platform-observer` bound by RoleBinding in `strive-ailab`, `strive-sandboxes-ailab` and
   `platform-edge`: get/list/watch on workloads, pods/log, services/endpoints, configmaps, events,
   PVCs, CNPG/Traefik/Cilium/Flux-HelmRelease/ExternalSecret/PodMonitor CRs, `kubectl top`; `create`
   (+`get`) on `pods/portforward`. **Excluded:** `secrets` (any verb), `pods/exec`, `pods/attach`,
   `pods/ephemeralcontainers`, `serviceaccounts/token`, every mutating verb. Exec is excluded because
   it is equivalent to reading every Secret mounted or env-injected into any pod. **The boundary this
   draws is worker-side vs cluster-side, not "Secrets are never read":** the pg-sync CronJob mounts
   `strive-pg-superuser` exactly as the platform's own init Job already does every ten minutes — a
   Flux-owned, operator-reviewed, in-cluster workload whose code is in git, the same trust position as
   every other seeder in `security/openbao/`. A worker is an agent-controlled machine outside the
   cluster; what it can read is the thing being bounded. Verified before granting: the `strive`
   HelmRelease has empty values and `hatchet`'s only name Secrets; every ConfigMap key in the three
   namespaces was checked (2026-09-21, names only — `keycloak-realm/realm.json` carries no
   `secret`/`clientSecret`/`password`/`value` literals, `valkey.conf` no `requirepass`). Escalation
   is a git change: a purpose-named Role + RoleBinding PR for one slot, with a removal date in the PR
   body — no standing escalation object exists.
3. **Database access is a per-slot Postgres role, maintained by the cluster, published as
   sync-owned KV — never seeded.** New CronJob `openbao-platform-pg-sync` (ns `strive-ailab`, so it
   can mount `strive-pg-superuser`; image `ghcr.io/cloudnative-pg/postgresql:16.9` — the operand
   image, which carries python3.9 + psycopg2, verified) upserts `dw<N>_platform_ro` for every live
   slot: `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION INHERIT CONNECTION
   LIMIT 10` (interactive psql sessions are one connection each; ten per worker is headroom, not a
   budget, and `pg_stat_activity` shows the count if it is ever hit), `GRANT pg_monitor`, and
   per-database explicit read grants: `CONNECT`; for every user schema present at run time (`public`,
   and the `platform_data_*` tenant schemas in `platform_managed`) `USAGE` + `SELECT ON ALL
   TABLES/SEQUENCES IN SCHEMA`; and `ALTER DEFAULT PRIVILEGES FOR ROLE app GRANT SELECT ON
   TABLES/SEQUENCES` plus the same `FOR ROLE postgres` — **database-wide, not `IN SCHEMA`**: `app`
   owns every database and runs every alembic migration, `postgres` is what the platform's init Job
   and the workflow admin DSN connect as, and a per-schema default ACL is a dependency of that schema
   that would make the integration provisioner's per-tenant `DROP SCHEMA` fail. Tables created by a
   tenant role become readable on the next run (daily lag; accepted and documented — no default ACL is
   ever attached to a tenant role, whose `DROP ROLE` must stay possible). Role-level
   `default_transaction_read_only = on`, `statement_timeout = 5min`,
   `idle_in_transaction_session_timeout = 60s`.
   **Read-only is enforced by privilege, not by a setting:** the role owns nothing and holds only
   `SELECT`, so an `INSERT` on the primary fails with `permission denied` even after `SET
   transaction_read_only = off`; the GUC default is a courtesy that turns accidental writes into a
   clearer error, and the `-ro` replicas add the server-side refusal on top. The sync proves this on
   every run (below) and the rollout proves it against `-rw` by hand.
   **RLS is honoured, not bypassed:** with `NOBYPASSRLS` and the platform's `TO public` policies, a
   worker reads a tenant's airlock/workflow rows by setting `app.tenant_id` for the session — `platform
   psql --tenant <id>` passes it as a libpq startup option (`options=-c app.tenant_id=<id>`), which is
   exactly how the services scope themselves. Without `--tenant` those tables read as empty; the
   runbook says so and says where a tenant id comes from (the App's `tenant_id`, the persona's token
   claim).
   Rejected: `pg_read_all_data` (cluster-wide: it cannot exclude a database without revoking PUBLIC's
   `CONNECT`, which the per-tenant roles rely on); a SOPS-seeded password (seed-wins would re-apply
   an expired credential daily, and a password in git is the pattern ADR 0021 removed); the OpenBao
   `database` secrets engine (the canonical Vault answer — dynamic per-lease users — but it needs a
   new engine + a privileged PG login held by the vault, a `bao agent` template rollout on every
   worker, and `cred` cannot read non-KV paths; recorded as the future step if per-lease revocation
   is ever needed).
   The database allowlist is an env var on the CronJob, default = all platform databases **except
   `keycloak`** (the identity store: credential hashes, confidential-client secrets in clear,
   sessions; nothing an app-debugging agent needs that the Keycloak admin API does not expose
   better). Exclusion means *no grant*: PUBLIC's `CONNECT` is untouched, so `\c keycloak` succeeds and
   every table read is `permission denied` — the sync refuses to start if `keycloak` is ever listed.
   **Rotation is when-needed, not forced daily.** Validity is 14 days (`VALID UNTIL now()+14d`,
   `PG_PASSWORD_VALID_SECONDS=1209600`) and a run rotates a slot's password only when: the KV fields
   are absent/unreadable, or the published `platform_pg_valid_until` has less than
   `PG_ROTATE_BEFORE_SECONDS` (7d) left, or logging in with the published password fails (a role
   dropped or altered out of band), or `FORCE_ROTATE=1`. Grants and role attributes are re-asserted on
   every run regardless. Effective cadence: a new password roughly weekly, a leaked one dead within
   14 days, a week of missed runs before anything expires, and — the property that made this the
   design rather than a nicety — **re-running the Job is a no-op**, so the bootstrap Job's
   TTL-reap-and-re-apply retry loop (below) cannot rotate a live credential hourly, and a manual
   `kubectl create job --from=cronjob/…` is always safe. Failed runs alert through the stack's
   existing `KubeJobFailed` (15 m, kube-state-metrics; verified present) — no new rule.
   Publish per slot in ONE KV patch: `platform_pg_user`, `platform_pg_password`,
   `platform_pg_valid_until`. Order: log in to OpenBao first (a run that cannot publish must not
   rotate); all role changes in one transaction on the primary (`pg_is_in_recovery()` must be false —
   the sync refuses a replica); per-database grants; then, **before publishing anything**, log in as
   each rotated role over the same primary service and prove `current_user`,
   `transaction_read_only = on`, a `pg_stat_activity` read (pg_monitor) and a refused `CREATE TABLE`
   (SQLSTATE 25006/42501); only then patch KV. A publish failure leaves the previous KV value (now
   invalid) until the retry — the same failure class the k8stoken sync documents.
   Retired slots: `RETIRED_SLOTS` env → for each database `DROP OWNED BY dw<N>_platform_ro` (drops
   grants and default-privilege entries; the role owns nothing else), then `DROP ROLE IF EXISTS` —
   idempotent, resumable, the same mechanism the provision script uses for AppRoles, and where PR-C2
   adds slot 5.
4. **Worker side stays on the ADR 0021 rails.** The bao agent renders `~/.platform/kubeconfig` and
   `~/.platform/pgpass` (libpq `*:*:*:dw<N>_platform_ro:<password>`, 0600, user-owned). A stanza for
   a field that does not exist yet exits the agent (`error_on_missing_key` + `exit_on_retry_failure`),
   so the stanzas are emitted only when a pre-flight on this host, under its own AppRole, **reads all
   three `platform_*` fields** (`cred get`, values discarded) — not a login probe. The pre-flight
   distinguishes *absent* from *unreachable*: `cred list` failing (sealed vault, network, bad token)
   **fails the play** rather than silently dropping the stanzas, so a transient outage can neither
   crash the agent nor strip the platform files from a host that had them; only a genuinely missing
   field yields "no stanza". The result is a `set_fact` **without `cacheable`**, which is play-scoped
   — `ansible.cfg` enables jsonfile fact caching for *gathered* facts only, so nothing about this
   decision survives a run (the ADR 0021 marker is durable precisely because that question, file
   ownership between two writers, needed to be). A `platform` helper (`platform kubectl …`,
   `platform pf <svc> <local:remote>`, `platform psql [-d db] [--tenant id] [--rw] [psql args]` —
   picks a free local port, starts a port-forward to `strive-pg-ro`, waits for `pg_isready`, runs psql
   with `PGPASSFILE`, tears the forward down) removes the background-process footgun from the agents'
   hands and defaults to the replicas, where the server itself refuses writes.
   `postgresql-client` joins the base packages. The managed CLAUDE.md block gains a "Platform
   (Strive) access" section: what it can see, what it cannot (secrets/exec/writes), the tenant-GUC
   and replica-cancellation caveats, and "say so instead of looking for a wider credential".
5. **Tiering (ADR 0021 §5) widens Tier A to production read.** A worker credential can now read
   every platform database except keycloak (tenant-scoped where the platform scopes) and every
   non-Secret object in the platform namespaces. Accepted on the estate's stated posture
   (single-operator; the same workers already hold the Gitea PAT and the hypervisors' root key) with
   two properties the earlier posture lacked: **attribution** (a distinct SA and a distinct PG role
   per slot — the API audit log and `pg_stat_activity.usename` name the machine) and **revocation of
   one slot without touching the others** (drop the RoleBinding subject + `RETIRED_SLOTS`). The
   reviewer VMs are **deliberately excluded** — they review diffs, hold no `cred`, and have no
   per-host KV subtree — and every slot-bearing file says so in a comment so nobody "completes" the
   list.

## Approach

### A. Cluster: `kubernetes/apps/infrastructure/platform-access/` (new Flux Kustomization `platform-access`)

- `namespace.yaml` — ns `platform-access` (PSA restricted labels). Holds only SAs. Keeps ailab-owned
  identities out of the platform-owned namespaces; the platform's `prune: true` Kustomizations only
  prune their own labelled objects, but a separate namespace makes the ownership legible.
- `rbac.yaml` — ClusterRole `dev-worker-platform-observer` (rules in Decision 2, with a header
  comment per exclusion); SAs `platform-dw1..5`; RoleBindings `dev-worker-platform-observer` in
  `strive-ailab`, `strive-sandboxes-ailab`, `platform-edge` listing the five SA subjects; Role +
  RoleBinding `openbao-k8stoken-sync` in `platform-access` granting `serviceaccounts/token create`
  with `resourceNames: [platform-dw1..5]` to `openbao/openbao-k8stoken-sync` (the mint Role lives
  next to the SAs it names, not in `k8stoken-sync.yaml`, so PR-C2's slot edit for this plane is one
  file). The slot list appears five times in this file (SAs, mint `resourceNames`, three
  RoleBinding subject lists) and is called out in a header comment.
- `pg-sync.yaml` — SA `openbao-platform-pg-sync` (ns `strive-ailab`, `automountServiceAccountToken:
  true` for the OpenBao k8s-auth login; no k8s RBAC at all), ConfigMap `openbao-platform-pg-sync-script`
  (`sync.py`, python3.9-compatible, psycopg2 + urllib; the OpenBao login/probe/patch/cas-create logic
  copied from `k8stoken-sync.yaml`'s `sync.py` with a comment naming the origin), CronJob
  `openbao-platform-pg-sync` (`47 3 * * *` UTC, `concurrencyPolicy: Forbid`, restricted
  securityContext, uid 65532, `readOnlyRootFilesystem`, emptyDir `/tmp` + `HOME=/tmp`, env
  `PGHOST=strive-pg-rw.strive-ailab.svc.cluster.local`, `PGSSLMODE=require`, PGUSER/PGPASSWORD from
  `strive-pg-superuser`, `LIVE_SLOTS="1 2 3 4 5"`, `RETIRED_SLOTS="6"`, `PLATFORM_DATABASES` (12,
  no keycloak), `PG_PASSWORD_VALID_SECONDS=1209600`, `PG_ROTATE_BEFORE_SECONDS=604800`, `BAO_ADDR` =
  the headless per-pod name, `BAO_ROLE=platform-pg-sync`), and a bootstrap Job with the
  `kustomize.toolkit.fluxcd.io/force` annotation + `ttlSecondsAfterFinished: 3600`. The bootstrap Job
  is the first run on apply and the retry path when the OpenBao role does not exist yet (Flux
  re-applies the reaped Job); **agents read KV, never the Job**, so its presence or absence between
  reaps changes nothing for a worker — and because a re-run is a no-op unless rotation is due
  (Decision 3), the recurring re-apply is harmless rather than an hourly rotation. Logs print role
  names, database names, field names, `valid_until`, `rotated`/`kept` — never a password.
- `kustomization.yaml`; `kubernetes/apps/clusters/ai/platform-access.yaml` — Flux Kustomization,
  `dependsOn: [platform, openbao]` (the platform namespaces and the `openbao` namespace's sync SA
  must exist), `wait: false`, `prune: true`, no decryption (nothing is SOPS here — that is the point).

### B. Cluster: two existing files, minimal PR-C2-shaped edits

- `security/openbao/k8stoken-sync.yaml` — inside the existing `for _n in (1, 2, 3, 4, 5):` loop add
  `TARGETS.append((f"dev-worker-{_n}", "platform_kubeconfig", "platform-access", f"platform-dw{_n}", "strive-ailab"))`.
  The tuple gains a fifth element (the kubeconfig's context namespace, distinct from the SA's
  namespace for the first time); `kubeconfig()` takes it, and **all three target classes are updated
  in the same edit** — the two existing appends pass their namespace twice, so no 4-tuple remains
  anywhere and there is nothing for PR-C2 to catch up on. Header comment: the mint Role for this
  target class lives in `platform-access/rbac.yaml`; a target whose SA is missing fails the whole run
  before anything is written (unchanged, deliberate). The "validated N/N" line goes from 10/10 to 15/15.
- `security/openbao/devworker-provision-job.yaml` — after the `k8stoken-sync` k8s-auth role, add
  role `platform-pg-sync` bound to `openbao-platform-pg-sync`@`strive-ailab` with
  `token_policies=k8stoken-sync`. **That policy is a KV write grant** — `read/create/update/patch`
  on `af/data/dev-workers/dev-worker-<N>` for live slots and nothing else; SA-token minting is
  Kubernetes RBAC and never an OpenBao policy — so it is exactly and only what a second sync-owned
  writer to the same per-slot documents needs, and PR-C2's narrowing of it to the surviving slots is
  correct for both writers. A second, identical policy would double the slot list the provision
  script's `RETIRED_SLOTS` step has to narrow; the comment on the role says "shared on purpose".
  `test-devworker-provision.sh` already accepts any `bao write` (its stub's `write *` arm exits 0);
  one `expect` line for the new role write is added so the test asserts it exists.

### C. Workers: `ansible/roles/dev_worker`

- `tasks/packages.yml` — `postgresql-client`.
- `files/platform` (+ `tasks/openbao.yml` install task, mode 0755) — POSIX sh, `set -eu`;
  `kubectl` sub-command = `kubectl --kubeconfig ~/.platform/kubeconfig -n strive-ailab "$@"`;
  `pf <svc> <local:remote>` = foreground port-forward; `psql` = pick a free port, background
  `port-forward svc/strive-pg-ro` (or `-rw` with `--rw`), wait for `pg_isready`, exec
  `psql -h 127.0.0.1 -p <port> -U dw<N>_platform_ro -d <db>` with `PGPASSFILE=~/.platform/pgpass`
  and, with `--tenant`, `PGOPTIONS="-c app.tenant_id=<id>"`; trap-kill the forward. Slot number
  derived from `hostname -s` (`dev-worker-N`), the same load-bearing fact `cred` relies on.
- `tasks/openbao.yml` — (i) create `~/.platform` (0700) per user, alongside `.tep/.helmtest`;
  (ii) lay down `platform-kubeconfig.ctmpl` and `platform-pgpass.ctmpl`; (iii) the pre-flight of
  Decision 4 (`cred list` must succeed or the play fails; then `cred get $host <field> >/dev/null`
  for the three fields → fact `dev_worker_platform_fields_present`); (iv) `openbao-agent.hcl.j2`
  emits the two `platform` stanzas only when that fact is true; (v) the health block, when the
  stanzas were emitted: both files rendered 0600 user-owned; `kubectl --kubeconfig
  ~/.platform/kubeconfig auth can-i list pods -n strive-ailab` = yes; `auth can-i get secrets -n
  strive-ailab` = **no** and `auth can-i create pods/exec -n strive-ailab` = **no** (the negative
  checks are what prove the ClusterRole is the one we think); pgpass has the expected user field. No
  live DB login from the playbook (it would need a port-forward inside ansible); the sync proved the
  login in-cluster, and the rollout proves the worker-side path by hand.
- CLAUDE.md managed block — "Platform (Strive) access" section, emitted under the same fact.
- `tests/test-platform-helper.sh` — exercised in `dev-worker-scripts.yaml` as a named step: usage
  exits 2; `kubectl` composes the right argv against a stub `kubectl` on PATH; `psql` refuses when
  `~/.platform/pgpass` is absent; `--tenant` lands in `PGOPTIONS`; slot parsing from a stubbed
  hostname.

### D. Tests and guards

- `scripts/tests/test-platform-pg-sync.sh` — runs `sync.py` **under the CronJob's own image**
  (`ghcr.io/cloudnative-pg/postgresql:16.9`, the same pattern as `test-devworker-provision.sh`)
  against a throwaway `initdb` Postgres and a stub OpenBao (python `http.server` recording every
  request): first run creates roles + grants + publishes; second run keeps (no password change, KV
  untouched); a run with `valid_until` inside the rotate window rotates; `FORCE_ROTATE=1` rotates; a
  role dropped by hand is recreated; a table named `"user"` and a schema named `"select"` are granted
  (keyword identifiers); an RLS table reads empty without the GUC and its rows with it; on the primary
  an `INSERT` is refused, and still refused after `SET transaction_read_only = off`; a database left
  out of the allowlist is not readable; `keycloak` in the allowlist aborts before any change; a retired
  slot's grants and role are gone after the run and the run converges when re-run; no log line and
  no stub-received body other than the KV patch contains a password (the stub's canary is a marker
  the password must never appear next to). Wired into `manifests.yaml` as a named step.
- `scripts/check-slot-enumerations.py` — the cross-file consistency check codex asked for: parses
  the live slot set from `k8stoken-sync.yaml` (`for _n in (…)`), `devworker-provision-job.yaml`
  (`for host in …` and `RETIRED_SLOTS`), `platform-access/rbac.yaml` (SAs, mint `resourceNames`,
  RoleBinding subjects), `platform-access/pg-sync.yaml` (`LIVE_SLOTS`, `RETIRED_SLOTS`),
  `testpool/tep-access.yaml` (SAs) and `kubernetes/infra/dev-workers/variables.tf` (map keys), and
  fails when any two disagree on live or retired slots. Wired into `manifests.yaml`; the retire
  checklist in the runbook points at it as the thing that tells you what you missed.

### E. Docs

- `docs/decisions/0028-dev-worker-platform-access.md` — the five decisions above, alternatives,
  consequences (Tier A widening, per-slot attribution/revocation, the sync-owned recovery ordering
  gains a second sync and four fields, the when-needed rotation contract, PR-C2 interplay),
  follow-ups (database secrets engine; per-database allowlist is a one-line env edit; reviewer VMs
  deliberately excluded).
- `docs/runbooks/dev-worker-platform-access.md` — how-to for agents and operators: `platform` usage,
  what is readable, tenant-GUC and replica-cancellation caveats (`max_standby_streaming_delay`
  cancels long replica queries — use `--rw` for a long scan, it is still read-only), escalation PR
  shape, rotation contract, **the retire-a-slot checklist enumerating every file** (with
  `check-slot-enumerations.py` as the gate), failure modes (sync red → `KubeJobFailed` → fields
  stale until the next successful run; an agent restart loop only if a field is *deleted*),
  verification commands.
- `docs/runbooks/openbao-dev-workers.md` — KV layout: `platform_kubeconfig` + the three
  `platform_pg_*` fields under the sync-owned paragraph; component map rows for the pg-sync and the
  platform-access layer; § Adding a worker / retiring a slot points at the checklist above.
- `docs/runbooks/openbao-recovery.md` — the sync-owned path class lists the new fields and the second
  sync CronJob (recovery ordering: vault + auth roles → both syncs → AppRole re-mint → agents).
- `docs/runbooks/dev-workers.md` — one pointer. `CLAUDE.md` (repo) — one line under "Reaching
  hosts/guests": workers reach the platform via `platform …`, see the runbook.

## Critical files

| Path | Role |
|---|---|
| `kubernetes/apps/infrastructure/platform-access/{namespace,rbac,pg-sync,kustomization}.yaml` | NEW: SAs, ClusterRole + bindings, mint Role, pg-sync SA/ConfigMap/CronJob/bootstrap Job |
| `kubernetes/apps/clusters/ai/platform-access.yaml` | NEW: Flux Kustomization (`dependsOn: platform, openbao`) |
| `kubernetes/apps/infrastructure/security/openbao/k8stoken-sync.yaml` | third target class per slot; context-namespace tuple element (all classes updated) |
| `kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml` | k8s-auth role `platform-pg-sync` (shared KV-write policy) |
| `scripts/tests/test-devworker-provision.sh` | `expect` for the new role write |
| `scripts/tests/test-platform-pg-sync.sh`, `scripts/check-slot-enumerations.py`, `.gitea/workflows/manifests.yaml` | NEW: sync integration test under the operand image; slot consistency gate; CI steps |
| `ansible/roles/dev_worker/files/platform` (+ `tests/test-platform-helper.sh`, `.gitea/workflows/dev-worker-scripts.yaml`) | helper CLI + test + CI step |
| `ansible/roles/dev_worker/templates/{openbao-agent.hcl.j2,platform-kubeconfig.ctmpl.j2,platform-pgpass.ctmpl.j2}` | agent stanzas + render templates |
| `ansible/roles/dev_worker/tasks/{openbao,packages}.yml` | pre-flight fact, dirs, templates, helper install, health checks, CLAUDE.md block, package |
| `docs/decisions/0028-…`, `docs/runbooks/dev-worker-platform-access.md`, `openbao-dev-workers.md`, `openbao-recovery.md`, `dev-workers.md`, `CLAUDE.md` | records |

## Verification

Pre-merge (CI + local):
- `scripts/manifest-lint.sh` (kustomize build + kubeconform, dockerised) locally (Windows needs the
  `pwd -W` + `MSYS_NO_PATHCONV=1` wrapper; the runner does not) and in the `manifests` workflow;
  `kubectl --context admin@ai apply --dry-run=server -k kubernetes/apps/infrastructure/platform-access`
  (schema + admission, creates nothing).
- `scripts/tests/test-platform-pg-sync.sh` (docker; the integration matrix in § D) and
  `scripts/check-slot-enumerations.py` — both locally and as `manifests.yaml` steps.
- `bash scripts/tests/test-devworker-provision.sh` (docker), `bash
  ansible/roles/dev_worker/tests/test-cred-helper.sh`, new `test-platform-helper.sh`.
- WSL: `ANSIBLE_CONFIG=… ansible-playbook dev-workers.yml --syntax-check`, then `--check -t openbao
  --limit dev-worker-3`. The check run must be **consistent with the live KV**, whichever state it
  is in: before the syncs have run (the pre-merge case by construction — nothing is applied yet) the
  pre-flight fact is false and the agent.hcl diff shows no platform stanza; after they have run
  (post-merge, before the real rollout) it is true and the diff shows exactly the two stanzas.
- Codex plan review (Phase A) and implementation review (Phase B), ≤2 rounds each.

Post-merge rollout (in this order; each step has a stop condition):
1. Flux: `platform-access` Kustomization Ready; `kubectl -n strive-ailab get sa,cronjob,job | grep
   platform-pg-sync`; force the daily provision Job (Rotation step 4 in `openbao-dev-workers.md`)
   and confirm its log shows the `platform-pg-sync` role write; then `kubectl -n strive-ailab logs
   job/openbao-platform-pg-sync-bootstrap | tail -5` must show `published 5/5 slots` — **stop here
   if it does not**.
2. `psql` in the primary pod: `\du dw*` shows five roles, none superuser; structured check of the
   published validity **without a JSON dump** (a `-format=json | jq` would print the password —
   [[secret-leaks-via-debug-output]]): `bao kv get -mount=af -field=platform_pg_valid_until
   dev-workers/dev-worker-1` parses and is ≥ now()+13d.
3. `kubectl -n openbao logs job/openbao-k8stoken-sync-bootstrap` → "validated 15/15 fields".
4. From dev-worker-3 as `c4`: `cred get dev-worker-3 platform_pg_user` prints `dw3_platform_ro`;
   `cred get dev-worker-3 platform_kubeconfig | wc -c` > 1000.
5. Ansible `-t openbao --limit dev-worker-3` (WSL) → pre-flight true, stanzas emitted, health block
   passes including the `can-i get secrets` / `can-i create pods/exec` = no checks; then the
   remaining live workers.
6. On dev-worker-3: `platform kubectl get pods` lists the platform; `platform kubectl get secrets` is
   `Forbidden`; `platform psql -d airlock -c 'select count(*) from app_table_drafts'` returns 0 rows
   without `--tenant` and the tenant's rows with it; **against the primary**: `platform psql --rw -d
   airlock -c "set transaction_read_only = off; insert into outbox default values"` fails
   `permission denied` (privilege, not setting); `platform psql -d keycloak -c 'select 1 from
   user_entity limit 1'` fails `permission denied`.
7. Hand the dev-worker-3 agent: `~/.platform/kubeconfig`, `platform psql -d airlock --tenant …`, the
   four KV field names, and `app_user_tables_max_columns = 16` (live pod, no override).

<!-- codex: Pre-flight task sets a fact that persists across runs. If a subsequent run fails (OpenBao unreachable), the fact remains true and agent.hcl regenerates with stanzas for missing KV fields, crashing the agent with error_on_missing_key. The health check validates RBAC but not KV presence. Add a secondary check: in the pre-flight task, only set the fact true if a real read of all three KV fields succeeds, not just a probe of the OpenBao login. This matches ADR 0021's mitigation for the same hazard. -->
<!-- opus-pushback: Verified against ansible/ansible.cfg: jsonfile fact caching applies to gathered facts; a `set_fact` without `cacheable: true` is play-scoped, so nothing persists between runs. The actionable half is taken anyway (Decision 4 / §C: the pre-flight reads all three fields and fails the play when the vault is unreachable instead of dropping stanzas). -->

<!-- codex: Bootstrap Job TTL 3600s but Flux' default interval is 600s. If the Job finishes at time T, Flux reaps it at T+3600s, then reconciles at T+3610s. If an agent re-login happens at T+3605s, the Job is gone; if it happens at T+3620s, it's back. Is this a problem for in-flight credentials? The sync-owned recovery ordering (docs/runbooks/openbao-recovery.md) assumes the bootstrap completes before agents re-login, but TTL doesn't enforce that. Should the Job have a longer TTL or an active re-apply interval? -->
<!-- opus-pushback: A worker never reads the Job — it reads the KV the Job wrote, which outlives the Job. The real hazard in the reap-and-re-apply loop was different: with forced daily rotation, every re-apply would have rotated the live password (an hourly login-failure window on every worker). Decision 3 now rotates only when due, so re-runs are no-ops and the loop stays as the ordering-race retry it is meant to be. -->

<!-- codex: token_policies=k8stoken-sync is the EXISTING policy (k8stoken-sync, k8stoken-sync) from the two existing targets. This means pg-sync inherits the per-slot mint grant intended only for k8stoken-sync. Is that correct, or should pg-sync have its own narrower policy granting only KV patch on af/dev-workers? If k8stoken-sync is narrowed in PR-C2, pg-sync may lose the grant it needs. Clarify the policy ownership. -->
<!-- opus-pushback: The `k8stoken-sync` OpenBao policy contains no mint grant — minting is Kubernetes RBAC (`serviceaccounts/token create`, per namespace). The policy is precisely "read/create/update/patch on af/data/dev-workers/dev-worker-<N> for live slots", which is the whole of what pg-sync needs; PR-C2's narrowing to the surviving slots is the correct narrowing for both writers. A duplicate policy would double the list the provision script's RETIRED_SLOTS step has to keep in step. Kept shared, said so on the role (§B). -->

<!-- codex-review-status: complete -->
