# Runbook: dev-worker access to the Strive platform (`platform`, ADR 0028)

How a dev-worker agent observes the running Strive platform and reads its databases, and the
operator ceremonies around it: **what is reachable**, **rollout**, **rotation**, **retiring a slot**,
**escalation**, **failure modes**. Design and rejected alternatives: **ADR 0028**
(`docs/decisions/0028-dev-worker-platform-access.md`). The credential plumbing this rides on:
`docs/runbooks/openbao-dev-workers.md` (ADR 0020/0021). The vault itself:
`docs/runbooks/openbao-recovery.md`.

**Context for every cluster command below:** `kubectl --context admin@ai` (the default context
flip-flops — always pass it explicitly), repo root as CWD.

## What this is

Two credentials per live dev-worker slot, both minted **by the cluster** and published into the
slot's own OpenBao path as **sync-owned** fields (never seeded, never in git):

| Field | Written by | Rendered to | Is |
|---|---|---|---|
| `platform_kubeconfig` | `openbao-k8stoken-sync` (ns `openbao`, daily 03:17 UTC) | `~/.platform/kubeconfig` | a bound token for SA `platform-access/platform-dw<N>`, context namespace `strive-ailab` |
| `platform_pg_user` · `platform_pg_password` · `platform_pg_valid_until` | `openbao-platform-pg-sync` (ns `strive-ailab`, daily 03:47 UTC) | `~/.platform/pgpass` | the Postgres login `dw<N>_platform_ro` on `strive-pg` |

The agents use `/usr/local/bin/platform`; the files refresh themselves and must never be edited,
copied or pasted anywhere.

```sh
platform kubectl get pods                    # namespace strive-ailab
platform kubectl logs deploy/airlock --tail 100
platform kubectl get cluster strive-pg -o wide
platform psql -d airlock -c '\dt'            # replicas (strive-pg-ro), read-only
platform psql -d airlock --tenant <tenant_id> -c 'select count(*) from app_table_drafts'
platform psql --rw -d workflow -c '...'      # the primary; still read-only
platform pf valkey-master 16379:6379         # any ClusterIP, foreground, Ctrl-C to stop
platform env                                 # slot, postgres role, file paths (no secrets)
```

### What is reachable

- **Namespaces `strive-ailab`, `strive-sandboxes-ailab`, `platform-edge`**: workloads, `pods/log`,
  services, endpoints, configmaps, events, PVCs, PDBs, HPAs, NetworkPolicies, Ingresses, the
  CNPG / Traefik / Cilium / Flux HelmRelease / ExternalSecret / Prometheus / KEDA / Kyverno /
  agent-sandbox objects, `kubectl top pods`, and `port-forward`.
- **Every platform database except `keycloak`**: `airlock profile knowledge mcp workflow hatchet
  integration notification digest platform_managed tms sentinel`, SELECT only, plus `pg_monitor`
  (`pg_stat_activity`, `pg_stat_statements`, …).

### What is not, and why (do not work around these — ask)

1. **Secrets and exec are denied.** `get secrets`, `pods/exec`, `pods/attach` all fail, as does every
   write through the Kubernetes API. Exec is denied *because* it reads every mounted file and env
   var — the same thing as a Secret read.
2. **`port-forward` is a TCP path, and it is only as read-only as the service behind it.** The
   Kubernetes API cannot scope a forward to one Service, so `platform pf` reaches any pod port in
   these namespaces. Postgres is safe by privilege (below) and anything fronted by gatekeeper still
   authenticates you — but **Valkey runs with `ALLOW_EMPTY_PASSWORD=yes`**, so a forward to it could
   issue writes (`SET`, `FLUSHALL`). Treat every non-Postgres endpoint as read-only by discipline:
   inspect, do not mutate. If you need to change cache or queue state to test something, say so —
   that is a platform PR or an operator action, not something to do down a debugging tunnel.
3. **Postgres is SELECT-only by privilege.** The role owns nothing; an `INSERT` fails
   `permission denied` even after `SET default_transaction_read_only = off`, on the primary too.
4. **RLS: most `airlock` and `workflow` tables read EMPTY without `--tenant`.** 25 of 27 airlock
   tables and 14 of 15 workflow tables have `TO public` policies keyed on
   `current_setting('app.tenant_id')`. Zero rows and no error is almost always a missing tenant. A
   tenant id comes from the App (`apps.tenant_id`), the persona's token claim, or
   `platform_managed`'s `platform_data_<tenant>` schema names.
5. **`keycloak` connects but reads nothing** (`permission denied`) — by design; use the Keycloak
   admin API for identity questions.
6. **A tenant-created table is readable only after the next sync run** (the default privileges cover
   the migrator `app` and `postgres`, never a tenant role — a default ACL for a role would block
   `DROP ROLE`).
7. **Long scans: prefer `--rw`.** A replica can cancel a query that conflicts with recovery
   (`max_standby_streaming_delay`); the primary will not, and is equally read-only.

## Component map

| Component | Lives in | Does |
|---|---|---|
| ns `platform-access` + SAs `platform-dw<N>` | `kubernetes/apps/infrastructure/platform-access/{namespace,rbac}.yaml` | the per-slot identities; the namespace holds nothing else |
| ClusterRole `dev-worker-platform-observer` + 3 RoleBindings | `.../platform-access/rbac.yaml` | observe-only rights, **bound by RoleBinding only** (nothing cluster-wide) |
| Role/RoleBinding `openbao-k8stoken-sync` in `platform-access` | `.../platform-access/rbac.yaml` | lets the sync mint tokens for exactly these SAs |
| CronJob + bootstrap Job `openbao-platform-pg-sync` (+ script ConfigMap, CA ConfigMap) | `.../platform-access/pg-sync.yaml` | maintains `dw<N>_platform_ro`, publishes the three KV fields |
| k8s-auth role `platform-pg-sync` | `.../security/openbao/devworker-provision-job.yaml` | the sync's vault login (shares the `k8stoken-sync` KV-write policy) |
| third target class per slot | `.../security/openbao/k8stoken-sync.yaml` | mints `platform_kubeconfig` |
| `platform` helper, two `.ctmpl`s, agent stanzas, health checks, CLAUDE.md block | `ansible/roles/dev_worker/{files,templates,tasks}` | the worker side |
| `scripts/check-slot-enumerations.py` | CI (`manifests` workflow) | fails when the repo's slot lists disagree |
| `scripts/tests/test-platform-pg-sync.sh`, `ansible/roles/dev_worker/tests/test-platform-helper.sh` | CI | the sync under its own image; the helper against stubs |

## Rollout (first time, or after a rebuild)

Each step has a stop condition — do not continue past a failing one.

### 1. Merge, then let Flux apply

```bash
kubectl --context admin@ai -n flux-system get kustomization platform-access
#   Ready=True. It has no dependsOn: a missing platform namespace is a retried apply, not a wedge.
kubectl --context admin@ai -n platform-access get sa
#   platform-dw1..N
```

### 2. Give the provision Job its new k8s-auth role, then run the pg-sync

The `platform-pg-sync` role is created by the daily `openbao-devworker-provision` Job. To get it now
rather than within a day (the Rotation step in `openbao-dev-workers.md`):

```bash
kubectl --context admin@ai -n openbao delete job openbao-devworker-provision
kubectl --context admin@ai -n flux-system annotate kustomization openbao \
  reconcile.fluxcd.io/requestedAt="$(date +%s)" --overwrite
kubectl --context admin@ai -n openbao wait --for=condition=complete job/openbao-devworker-provision --timeout=5m
kubectl --context admin@ai -n openbao logs job/openbao-devworker-provision | grep platform-pg-sync
```

Then the sync itself (its bootstrap Job runs on apply; if it ran before the role existed, it failed,
was reaped, and Flux re-applied it — check the newest one):

```bash
kubectl --context admin@ai -n strive-ailab logs job/openbao-platform-pg-sync-bootstrap | tail -5
#   MUST end: published N/N slots (rotated N, kept 0)        <- STOP if it does not
```

### 3. Verify in Postgres and in KV (without printing anything)

```bash
kubectl --context admin@ai -n strive-ailab exec strive-pg-9 -c postgres -- \
  psql -U postgres -tAc \
  "select rolname, rolcanlogin, rolsuper, rolbypassrls, rolvaliduntil from pg_roles where rolname like 'dw%_platform_ro' order by 1"
#   one row per live slot: t | f | f | ~ now()+14d

# Validity per slot — a -field read, NEVER `-format=json` (that prints the password; see the
# secret-leak lesson in the estate runbooks).
BAO_TOKEN=... bao kv get -mount=af -field=platform_pg_valid_until dev-workers/dev-worker-1
```

### 4. The kubeconfig field

```bash
kubectl --context admin@ai -n openbao delete job openbao-k8stoken-sync-bootstrap  # optional: run it now
kubectl --context admin@ai -n openbao logs job/openbao-k8stoken-sync-bootstrap | tail -3
#   validated 15/15 fields   (3 fields x 5 live slots)
```

### 5. Roll the workers (one at a time)

From WSL (`ANSIBLE_CONFIG` must be explicit — `/mnt/c` is world-writable and ansible.cfg is dropped):

```bash
export ANSIBLE_CONFIG=/mnt/c/Users/chifo/work/home/ailab/ansible/ansible.cfg
cd /mnt/c/Users/chifo/work/home/ailab/ansible
ansible-playbook dev-workers.yml -t openbao --limit dev-worker-3
```

The play fails closed if the vault is unreachable; it prints a `debug` message and renders no
platform stanzas if the fields are simply not published yet. On success the health block has already
proven, on that host: both files 0600 and user-owned, the pgpass naming this worker's role,
`psql`/`pg_isready` present, and six **SelfSubjectAccessReviews** answered by the API server —
`list pods`, `create pods/portforward` and `apps: get deployments` allowed; `get secrets`,
`create pods/exec` and `apps: delete deployments` denied.

> Those are SSARs, not `kubectl auth can-i`, and the difference is not cosmetic. The workers carry
> kubectl 1.30, whose `can-i` resolves a **subresource** through the RESTMapper — and this identity
> cannot do discovery, so it answered a confident `no` for `pods/portforward` while the API server
> allowed it (measured 2026-09-21: it failed the first rollout of this very runbook, and the
> rollback then worked exactly as designed). The same false `no` would have made the NEGATIVE
> checks pass vacuously, which is the half that matters. So they are POSTed with
> `kubectl create --raw /apis/authorization.k8s.io/v1/selfsubjectaccessreviews` — a fixed path that
> needs no discovery at all — and kubectl's stderr is kept, so an expired token reads differently
> from a denial.
>
> **And name the API group.** `resourceAttributes.group` defaults to core, so asking about
> `deployments` without `group: apps` asks about a resource that does not exist: always denied,
> always "passing". That is the same vacuum one layer down, and it is why `apps: get deployments`
> is in the list — a positive control that fails loudly if the group is ever wrong, instead of
> letting its paired `delete` assertion pass quietly.

`-t openbao` is self-sufficient on purpose: `postgresql-client` is installed by the openbao-tagged
tasks as well as by `packages.yml`, because this tag-limited run is the documented path to an
EXISTING worker and would otherwise deliver the helper without the client it shells out to.

### 6. Prove it from the worker

```sh
platform env
platform kubectl get pods | head
platform kubectl get secrets            # MUST be Forbidden
platform psql -d airlock -c 'select count(*) from app_table_drafts'         # 0 rows without --tenant
platform psql -d airlock --tenant <id> -c 'select count(*) from app_table_drafts'
# TWO -c requests on purpose: psql runs each one in its own implicit transaction, and
# `default_transaction_read_only` takes effect from the NEXT transaction — both statements in one
# -c would fail with "read-only transaction" and prove nothing about privileges.
platform psql --rw -d airlock -c 'set default_transaction_read_only = off' -c 'insert into outbox default values'
#   MUST fail: permission denied for table outbox  (privilege, not the read-only setting)
```

## Rotation

**The Postgres password** rotates itself: 14-day validity, replaced when under 7 days remain, when
the published password stops working, or when the fields are missing. Nothing to do. To force one
(a suspected leak):

```bash
kubectl --context admin@ai -n strive-ailab create job --from=cronjob/openbao-platform-pg-sync \
  pg-sync-force-$(date +%s) --dry-run=client -o yaml \
  | python3 -c 'import sys,yaml;d=yaml.safe_load(sys.stdin);c=d["spec"]["template"]["spec"]["containers"][0];c["env"].append({"name":"FORCE_ROTATE","value":"1"});print(yaml.safe_dump(d))' \
  | kubectl --context admin@ai apply -f -
```

then watch for `rotated … (FORCE_ROTATE)` and restart the agents only if a worker reports auth
failures (the agent re-renders on its own poll).

**The kubeconfig** re-mints daily with a 30-day token; the floor check in `openbao-k8stoken-sync`
fails the Job rather than publishing anything short-lived.

**The ClusterRole** applies immediately on merge — RBAC is evaluated per request, so no restart
anywhere.

## Retiring or adding a slot — the checklist

`scripts/check-slot-enumerations.py` is the gate (CI, and run it locally). Every file below must name
the same live set, and retired slots must appear in both `RETIRED_SLOTS` lists:

| # | File | What to edit |
|---|---|---|
| 1 | `kubernetes/apps/infrastructure/platform-access/rbac.yaml` | the SA objects, the mint Role's `resourceNames`, and **all three** RoleBinding subject lists |
| 2 | `kubernetes/apps/infrastructure/platform-access/pg-sync.yaml` | `LIVE_SLOTS` and `RETIRED_SLOTS` — **in both the CronJob and the bootstrap Job** |
| 3 | `kubernetes/apps/infrastructure/security/openbao/k8stoken-sync.yaml` | the `for _n in (…)` loop |
| 4 | `kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml` | `for host in …` and `RETIRED_SLOTS` |
| 5 | `kubernetes/apps/infrastructure/testpool/tep-access.yaml` | the `tep-dw<N>` SAs (+ token Secret) |
| 6 | `kubernetes/apps/infrastructure/helmtest/namespaces.yaml` (+ `rbac.yaml`, `networkpolicy.yaml`) | the per-slot namespace tree |
| 7 | `kubernetes/infra/dev-workers/variables.tf` | the map key |
| 8 | `inventory/hosts.yml` | the host |

```bash
python3 scripts/check-slot-enumerations.py   # prints every enumeration and its set; exits 1 on a DIFF
```

A retired slot's Postgres role is dropped by the next pg-sync run (`DROP OWNED BY` in every database,
then `DROP ROLE IF EXISTS`); its AppRole, tokens and KV subtree are handled by the provision Job's
own `RETIRED_SLOTS` step. Both are idempotent — a re-run reports the same converged state.

## Escalation (when observe-only is genuinely not enough)

Open a PR adding a **purpose-named** Role + RoleBinding for **one slot**, in
`platform-access/rbac.yaml`, with the reason and a removal date in the PR body. Do not widen
`dev-worker-platform-observer`, and do not add `secrets`, `pods/exec` or a write verb to it — that
ClusterRole is bound to every worker at once, and the ansible health check asserts those exact
denials on every playbook run (it would go red, which is the point).

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Playbook: "af/dev-workers/… has no platform_* fields yet" | the syncs have not published for this slot (new slot, wiped vault, failed run) | check both Jobs; re-run `-t openbao` after they succeed. **Not an error** — the agent simply renders no platform files |
| Playbook fails at "Check that the vault is reachable" | sealed vault, network, dead sink token | fix the vault (`openbao-recovery.md`); the play stops on purpose rather than stripping working access |
| `platform: no kubeconfig at …` / `no pgpass at …` | the field exists but the agent has not rendered it (or the stanzas are not emitted yet) | `systemctl status openbao-agent`; re-run `-t openbao` |
| `openbao-agent` restart-looping right after a run | a rendered field disappeared from KV (`error_on_missing_key`) | check the syncs; the agent recovers once the field is back. This is why the pre-flight reads all three fields |
| `platform psql`: "no password supplied" / auth failed | the published password expired (both syncs down > 14 d) or the slot/hostname mismatch | run the pg-sync; `platform env` must name this host's role |
| `platform psql`: "FATAL: too many connections for role" | more than 10 concurrent sessions for this slot | close sessions; `pg_stat_activity` (readable via `pg_monitor`) shows them. The sync also refuses to rotate while a slot is at its limit (it would commit a password it could not then prove), so a run may exit with "capacity or transport" until the sessions drain — that is the safe outcome, not a fault |
| A query on a replica is cancelled mid-scan | `max_standby_streaming_delay` | re-run with `--rw` |
| `KubeJobFailed` on `openbao-platform-pg-sync` | see the Job's log — OpenBao login (role missing / sealed), Postgres unreachable, or a proof failure | nothing expires for 14 days; fix and let the next run converge |
