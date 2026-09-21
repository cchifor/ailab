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
platform_managed tms sentinel`), plus `app`'s own bootstrap DB. Services connect as `app`; the
workflow service also uses per-tenant RLS roles (`tenant_<12hex>`, `workflow_app/admin`). The
`strive-pg-superuser` Secret (CNPG-generated, `enableSuperuserAccess: true`) is consumed by the
platform's own init Job, which re-runs every ~10–20 min (TTL 600 + 10 min reconcile) and only ever
adds privileges to `app`.

Fleet state that constrains the design: PR-C1 (#809) retired slot 6; **PR-C2 will retire the
dev-worker-3 VM and re-slot ex-dw4→3, ex-dw5→4**, ending at four slots. Every per-slot enumeration
in this change must therefore be a one-line edit per slot, in the same files PR-C2 already touches
or in files it can extend, and retired slots must have a codified revocation path.

## Decisions (to be recorded in ADR 0028)

1. **Reach through the API server, not the LAN.** A per-slot ServiceAccount + read-only RBAC in the
   platform namespaces, minted into a third sync-owned kubeconfig (`platform_kubeconfig`). Every
   ClusterIP (Postgres, Valkey, Hatchet, Keycloak admin, any service's internal API) is reached with
   `kubectl port-forward` over the authenticated, audited API server path the workers already use for
   tep/helmtest. Rejected: a `strive-pg-ro-lan` NodePort (a second LAN exposure of production data,
   SNAT-blind to the client like `openbao-lan`, and it generalises badly — one NodePort per
   component); a Cloudflare tunnel (third party in the path, ADR 0020's reasoning).
2. **Observe-only RBAC, no secret material, no exec, no writes.** ClusterRole
   `dev-worker-platform-observer` bound by RoleBinding in `strive-ailab`, `strive-sandboxes-ailab` and
   `platform-edge`: get/list/watch on workloads, pods/log, services/endpoints, configmaps, events,
   PVCs, CNPG/Traefik/Cilium/Flux-HelmRelease/ExternalSecret/PodMonitor CRs, `kubectl top`; `create`
   (+`get`) on `pods/portforward`. **Excluded:** `secrets` (any verb), `pods/exec`, `pods/attach`,
   `pods/ephemeralcontainers`, `serviceaccounts/token`, every mutating verb. Exec is excluded because
   it is equivalent to reading every Secret mounted or env-injected into any pod. Verified before
   granting: the `strive` HelmRelease has empty values and `hatchet`'s only name Secrets; the
   ConfigMap keys in the platform namespaces are checked during implementation (values never
   printed). Escalation is a git change: a purpose-named Role + RoleBinding PR for one slot, with a
   removal date in the PR body — no standing escalation object exists.
3. **Database access is a per-slot Postgres role, rotated daily by the cluster, published as
   sync-owned KV — never seeded.** New CronJob `openbao-platform-pg-sync` (ns `strive-ailab`, so it
   can mount `strive-pg-superuser`; image `ghcr.io/cloudnative-pg/postgresql:16.9` — the operand
   image, which carries python3.9 + psycopg2, verified) upserts `dw<N>_platform_ro` for every live
   slot: `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS CONNECTION LIMIT 5`, a fresh
   `PASSWORD` every run, `VALID UNTIL now()+7d` (a missed run costs nothing for a week; a leaked
   password dies within a week), `GRANT pg_monitor`, and per-database explicit read grants
   (`CONNECT`, `USAGE ON SCHEMA public`, `SELECT ON ALL TABLES/SEQUENCES IN SCHEMA public`, plus
   `ALTER DEFAULT PRIVILEGES FOR ROLE app|postgres IN SCHEMA public GRANT SELECT` so tables created
   by future migrations are readable). Role-level `default_transaction_read_only = on`,
   `statement_timeout = 5min`, `idle_in_transaction_session_timeout = 60s`. Rejected:
   `pg_read_all_data` (cluster-wide: it cannot exclude a database without revoking PUBLIC's CONNECT,
   which the per-tenant RLS roles rely on); a SOPS-seeded password (seed-wins would re-apply an
   expired credential daily, and a password in git is the pattern ADR 0021 removed); the OpenBao
   `database` secrets engine (the canonical Vault answer — dynamic per-lease users — but it needs a
   new engine + a privileged PG login held by the vault, a `bao agent` template rollout on every
   worker, and `cred` cannot read non-KV paths; recorded as the future step if per-lease revocation
   is ever needed).
   The database allowlist is an env var on the CronJob, default = all platform databases **except
   `keycloak`** (the identity store: credential hashes, sessions; nothing an app-debugging agent
   needs that the Keycloak admin API does not expose better). RLS-protected tables (workflow,
   platform_managed) read as empty for a role with no policy and `NOBYPASSRLS` — documented, not
   worked around.
   Publish per slot in ONE KV patch: `platform_pg_user`, `platform_pg_password`,
   `platform_pg_valid_until`. Order: all `ALTER/GRANT` in one transaction on the primary, commit,
   verify by logging in as each new role (`SELECT 1`, `transaction_read_only = on`), then publish.
   A publish failure leaves the previous KV value (now invalid) until the next run or the bootstrap
   Job's retry — the same failure class the k8stoken sync documents.
   Retired slots: `RETIRED_SLOTS` env → for each database `DROP OWNED BY dw<N>_platform_ro` (drops
   grants and default-privilege entries), then `DROP ROLE IF EXISTS` — idempotent, resumable, the
   same mechanism the provision script uses for AppRoles, and where PR-C2 adds `dw5`/`dw6`.
4. **Worker side stays on the ADR 0021 rails.** The bao agent renders `~/.platform/kubeconfig` and
   `~/.platform/pgpass` (libpq `*:*:*:dw<N>_platform_ro:<password>`, 0600, user-owned) — but a
   stanza for a field that does not exist yet exits the agent (`error_on_missing_key` +
   `exit_on_retry_failure`), so the stanzas are emitted only when a live pre-flight under this host's
   own AppRole finds all three `platform_*` fields; a host whose KV lacks them gets no stanza, no dead
   agent, and picks them up on the next `-t openbao` run. A `platform` helper (`platform kubectl …`,
   `platform pf <svc> <local:remote>`, `platform psql [-d db] [--rw] [psql args]` — starts a
   port-forward to `strive-pg-ro` on a free local port, runs psql with `PGPASSFILE`, tears the forward
   down) removes the background-process footgun from the agents' hands and defaults to the replicas,
   where the server itself refuses writes. `postgresql-client` joins the base packages. The managed
   CLAUDE.md block gains a "Platform (Strive) access" section: what it can see, what it cannot
   (secrets/exec/writes), the RLS and replica-cancellation caveats, and "say so instead of looking for
   a wider credential".
5. **Tiering (ADR 0021 §5) widens Tier A to production read.** A worker credential can now read
   every platform database except keycloak and every non-Secret object in the platform namespaces.
   Accepted on the estate's stated posture (single-operator; the same workers already hold the Gitea
   PAT and the hypervisors' root key) with two properties the earlier posture lacked: **attribution**
   (a distinct SA and a distinct PG role per slot — `auth.log`-equivalent in the API audit log and
   `pg_stat_activity.usename`) and **revocation of one slot without touching the others** (drop the
   RoleBinding subject + `RETIRED_SLOTS`).

## Approach

### A. Cluster: `kubernetes/apps/infrastructure/platform-access/` (new Flux Kustomization `platform-access`)

- `namespace.yaml` — ns `platform-access` (PSA restricted labels like `testpool`). Holds only SAs.
  Keeps ailab-owned identities out of the platform-owned namespaces; the platform's `prune: true`
  Kustomizations only prune their own labelled objects, but a separate namespace makes the ownership
  legible.
- `rbac.yaml` — ClusterRole `dev-worker-platform-observer` (rules in Decision 2, with a header
  comment per exclusion); SAs `platform-dw1..5`; RoleBindings `dev-worker-platform-observer` in
  `strive-ailab`, `strive-sandboxes-ailab`, `platform-edge` listing the five SA subjects; Role +
  RoleBinding `openbao-k8stoken-sync` in `platform-access` granting `serviceaccounts/token create`
  with `resourceNames: [platform-dw1..5]` to `openbao/openbao-k8stoken-sync` (the mint Role lives
  next to the SAs it names, not in `k8stoken-sync.yaml`, so PR-C2's slot edit for this plane is one
  file). The slot list appears exactly three times in this file (SAs, mint `resourceNames`,
  RoleBinding subjects) and is called out in a comment.
- `pg-sync.yaml` — SA `openbao-platform-pg-sync` (ns `strive-ailab`, `automountServiceAccountToken:
  true` for the OpenBao k8s-auth login; no k8s RBAC at all), ConfigMap `openbao-platform-pg-sync-script`
  (`sync.py`, python3.9-compatible, psycopg2 + urllib; the OpenBao login/probe/patch/cas-create logic
  copied from `k8stoken-sync.yaml`'s `sync.py` with a comment naming the origin), CronJob
  `openbao-platform-pg-sync` (`47 3 * * *` UTC, `concurrencyPolicy: Forbid`, restricted
  securityContext, uid 65532, `readOnlyRootFilesystem`, emptyDir `/tmp` + `HOME=/tmp`, env
  `PGHOST=strive-pg-rw.strive-ailab.svc.cluster.local`, `PGSSLMODE=require`, PGUSER/PGPASSWORD from
  `strive-pg-superuser`, `LIVE_SLOTS="1 2 3 4 5"`, `RETIRED_SLOTS="6"`, `PLATFORM_DATABASES` (12,
  no keycloak), `PG_PASSWORD_VALID_SECONDS=604800`, `BAO_ADDR` = the headless per-pod name,
  `BAO_ROLE=platform-pg-sync`), and a bootstrap Job with the `kustomize.toolkit.fluxcd.io/force`
  annotation + `ttlSecondsAfterFinished: 3600` (first run on apply; Flux re-applies the reaped Job —
  the retry path when the OpenBao role does not exist yet, exactly as `openbao-k8stoken-sync-bootstrap`).
  Logs print role names, database names, field names, `valid_until` — never a password.
- `kustomization.yaml`; `kubernetes/apps/clusters/ai/platform-access.yaml` — Flux Kustomization,
  `dependsOn: [platform, openbao]` (the platform namespaces and the `openbao` namespace's sync SA
  must exist), `wait: false`, `prune: true`, no decryption (nothing is SOPS here — that is the point).

### B. Cluster: two existing files, minimal PR-C2-shaped edits

- `security/openbao/k8stoken-sync.yaml` — inside the existing `for _n in (1, 2, 3, 4, 5):` loop add
  `TARGETS.append((f"dev-worker-{_n}", "platform_kubeconfig", "platform-access", f"platform-dw{_n}", "strive-ailab"))`;
  the tuple gains a fifth element (context namespace), `kubeconfig()` takes it, and the two existing
  appends pass their own namespace twice. Header comment: the mint Role for this target class lives in
  `platform-access/rbac.yaml`; a target whose SA is missing fails the whole run before anything is
  written (unchanged, deliberate). The "validated N/N" line goes from 10/10 to 15/15.
- `security/openbao/devworker-provision-job.yaml` — after the `k8stoken-sync` k8s-auth role, add
  role `platform-pg-sync` bound to `openbao-platform-pg-sync`@`strive-ailab` with
  `token_policies=k8stoken-sync` (the same per-slot write grant, so PR-C2's narrowing of that policy
  covers both writers; a comment says so). The existing `test-devworker-provision.sh` stub must accept
  the extra `bao write auth/kubernetes/role/…` call — checked, extended if it asserts an exact call
  list.

### C. Workers: `ansible/roles/dev_worker`

- `tasks/packages.yml` — `postgresql-client`.
- `files/platform` (+ `tasks/openbao.yml` install task, mode 0755) — POSIX sh, `set -eu`;
  `kubectl` sub-command = `kubectl --kubeconfig ~/.platform/kubeconfig -n strive-ailab "$@"`;
  `pf <svc> <local:remote>` = foreground port-forward; `psql` = pick a free port, background
  `port-forward svc/strive-pg-ro` (or `-rw` with `--rw`), wait for readiness (`pg_isready`), exec
  `psql -h 127.0.0.1 -p <port> -U dw<N>_platform_ro -d <db>` with `PGPASSFILE=~/.platform/pgpass`,
  trap-kill the forward. Slot number derived from `hostname -s` (`dev-worker-N`), the same
  load-bearing fact `cred` relies on.
- `tasks/openbao.yml` — (i) create `~/.platform` (0700) per user, alongside `.tep/.helmtest`;
  (ii) lay down `platform-kubeconfig.ctmpl` and `platform-pgpass.ctmpl`; (iii) new pre-flight task,
  runs on every `-t openbao` run (not gated on the ADR 0021 marker — a different question: "are the
  fields there", not "who owns the file"): `cred get $host platform_kubeconfig|platform_pg_user|
  platform_pg_password` under this host's AppRole → fact `dev_worker_platform_fields_present`;
  (iv) `openbao-agent.hcl.j2` emits the two `platform` stanzas only when that fact is true;
  (v) the health block, when the stanzas were emitted: both files rendered 0600 user-owned;
  `kubectl --kubeconfig ~/.platform/kubeconfig auth can-i list pods -n strive-ailab` = yes;
  `auth can-i get secrets -n strive-ailab` = **no** (the negative check is the one that proves the
  ClusterRole is the one we think); pgpass has the expected user field. No live DB login from the
  playbook (it would need a port-forward inside ansible); the sync already proved the login in-cluster.
- CLAUDE.md managed block — "Platform (Strive) access" section, emitted under the same fact.
- `tests/test-platform-helper.sh` — exercised in `dev-worker-scripts.yaml` as a named step: usage
  exits 2; `kubectl` composes the right argv against a stub `kubectl` on PATH; `psql` refuses when
  `~/.platform/pgpass` is absent; slot parsing from a stubbed hostname.

### D. Docs

- `docs/decisions/0028-dev-worker-platform-access.md` — the five decisions above, alternatives,
  consequences (Tier A widening, per-slot attribution/revocation, the sync-owned recovery ordering
  gains two fields, PR-C2 interplay), follow-ups (database secrets engine; per-database allowlist
  changes are a one-line env edit; reviewer VMs deliberately excluded).
- `docs/runbooks/dev-worker-platform-access.md` — how-to for agents and operators: `platform` usage,
  what is readable, RLS/replica caveats, escalation PR shape, rotation, retiring a slot, failure modes
  (sync red → fields stale → agent restart loop only if a field is deleted), verification commands.
- `docs/runbooks/openbao-dev-workers.md` — KV layout: the three `platform_pg_*` + `platform_kubeconfig`
  fields under the sync-owned paragraph; component map row for the pg-sync; § Adding a worker /
  retiring a slot gains the `platform-access/rbac.yaml` + `LIVE_SLOTS/RETIRED_SLOTS` edits.
- `docs/runbooks/openbao-recovery.md` — the sync-owned path class lists the new fields and the second
  sync CronJob (recovery ordering: vault + auth roles → both syncs → AppRole re-mint → agents).
- `docs/runbooks/dev-workers.md` — one pointer.
- `CLAUDE.md` (repo) — one line under "Reaching hosts/guests": workers reach the platform via
  `platform …`, see the runbook.

## Critical files

| Path | Role |
|---|---|
| `kubernetes/apps/infrastructure/platform-access/{namespace,rbac,pg-sync,kustomization}.yaml` | NEW: SAs, ClusterRole + bindings, mint Role, pg-sync SA/ConfigMap/CronJob/bootstrap Job |
| `kubernetes/apps/clusters/ai/platform-access.yaml` | NEW: Flux Kustomization (`dependsOn: platform, openbao`) |
| `kubernetes/apps/infrastructure/security/openbao/k8stoken-sync.yaml` | third target class per slot; context-namespace tuple element |
| `kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml` | k8s-auth role `platform-pg-sync` |
| `scripts/tests/test-devworker-provision.sh` | stub accepts the new role write (if it enumerates) |
| `ansible/roles/dev_worker/files/platform` (+ tests) | helper CLI |
| `ansible/roles/dev_worker/templates/{openbao-agent.hcl.j2,platform-kubeconfig.ctmpl.j2,platform-pgpass.ctmpl.j2}` | agent stanzas + render templates |
| `ansible/roles/dev_worker/tasks/{openbao,packages}.yml` | pre-flight fact, dirs, templates, helper install, health checks, CLAUDE.md block, package |
| `.gitea/workflows/dev-worker-scripts.yaml` | named test step |
| `docs/decisions/0028-…`, `docs/runbooks/dev-worker-platform-access.md`, `openbao-dev-workers.md`, `openbao-recovery.md`, `dev-workers.md`, `CLAUDE.md` | records |

## Verification

Pre-merge (CI + local):
- `scripts/manifest-lint.sh` (kustomize build + kubeconform, dockerised) locally and in the
  `manifests` workflow; `kubectl --context admin@ai apply --dry-run=server -k
  kubernetes/apps/infrastructure/platform-access` (schema + admission, creates nothing).
- `python3.9`-compat check of `sync.py` (`python -W error -c "import ast; ast.parse(...)"` plus no
  3.10+ syntax) and a unit-style dry run of its SQL builder with psycopg2's `mogrify` semantics
  (`sql.Identifier` for role/db names — no string-formatted identifiers).
- `bash scripts/tests/test-devworker-provision.sh` (docker), `bash
  ansible/roles/dev_worker/tests/test-cred-helper.sh`, new `test-platform-helper.sh`.
- WSL: `ANSIBLE_CONFIG=… ansible-playbook dev-workers.yml --syntax-check` and `--check -t openbao
  --limit dev-worker-3` (the pre-flight fact must come out **false** before the sync has run, and the
  rendered agent.hcl diff must show no platform stanza).
- Codex plan review (Phase A) and implementation review (Phase B), ≤2 rounds each.

Post-merge rollout (in this order; each step has a stop condition):
1. Flux: `platform-access` Kustomization Ready; `kubectl -n strive-ailab get sa,cronjob,job | grep
   platform-pg-sync`; `kubectl -n openbao logs job/openbao-devworker-provision` shows the
   `platform-pg-sync` role write.
2. `kubectl -n strive-ailab logs job/openbao-platform-pg-sync-bootstrap` → "published 5/5 slots";
   `psql` in the primary pod: `\du dw*` shows five roles with `valid until` ≈ +7d and no superuser.
3. `kubectl -n openbao logs job/openbao-k8stoken-sync-bootstrap` → "validated 15/15 fields".
4. From dev-worker-3 as `c4`: `cred get dev-worker-3 platform_pg_user` prints `dw3_platform_ro`;
   `cred get dev-worker-3 platform_kubeconfig | wc -c` > 1000.
5. Ansible `-t openbao --limit dev-worker-3` (WSL) → pre-flight true, stanzas emitted, health block
   passes including the `can-i get secrets` = no check; then the remaining live workers.
6. On dev-worker-3: `platform kubectl get pods` lists the platform; `platform kubectl get secrets` is
   `Forbidden`; `platform psql -d airlock -c 'select count(*) from app_table_drafts'` returns;
   `platform psql -d airlock -c 'create table t()'` fails read-only; `platform psql -d keycloak -c
   'select 1'` fails (no CONNECT grant needed to fail: no USAGE/SELECT — `\dt` empty, any table read is
   permission denied).
7. Hand the dev-worker-3 agent: `~/.platform/kubeconfig`, `platform psql -d airlock`, the three KV
   field names, and `app_user_tables_max_columns = 16` (live pod, no override).

<!-- codex-review-status: pending -->
