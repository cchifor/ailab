# Gitea SQLite → Postgres Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate Gitea's database from SQLite (on the RWO `/data` LUN) to a `gitea` database on the shared `infra-pg` CNPG Postgres, eliminating the SQLite single-writer "database is locked" contention, with zero data loss and a safe rollback.

**Architecture:** Gitea stays a single replica with git repos on the existing `/data` RWO PVC; only the DB moves. Add `gitea` as an `infra-pg` tenant (managed role + CNPG `Database` CR + matching SOPS password in both `databases` and `gitea` namespaces), migrate the data with a one-shot `pgloader` Job during a short maintenance window, then repoint the Gitea HelmRelease at `infra-pg-rw`.

**Tech Stack:** Kubernetes (Talos), Flux GitOps (reconciles `main` from in-cluster Gitea), CloudNativePG **1.24.1** (operator, `infra-pg` PG17), SOPS+age secrets, Helm (gitea chart 12.6.0), pgloader.

> **CNPG 1.24.1 is < 1.25, so the `postgresql.cnpg.io/v1 Database` CRD DOES NOT EXIST on this estate.**
> Never add a `kind: Database` resource to `kubernetes/apps/databases/`. It fails server-side dry-run,
> which fails the apply of the **entire** `databases` Kustomization — not just that one resource. This
> exact mistake (`00dd55a`, 2026-07-16) wedged `databases` for 5 days (27 commits unapplied) and
> cascaded into `apps` / `agentforge-tenants` / `edge-connector`. Databases on the already-bootstrapped
> `infra-pg` are created by a one-shot superuser bootstrap SQL artifact that is deliberately **kept out
> of `kustomization.yaml`**, so a failure can never wedge the layer.

## Global Constraints

- kubectl context: **`--context admin@ai`** (the ailab cluster) for every cluster command.
- Forge: push/PR/merge on **Gitea** (`git.chifor.me/cchifor/ailab`), squash-merge; Flux reconciles `main`. Never `gh`.
- SOPS: k8s Secrets encrypt `data|stringData` to age recipient **`age1nfa6hhdz9egnje3nwa2k0gpk5nr29nyvu74eprk20m7ql4fhw4esrlmt5g`**; the age private key is at **`kubernetes/infra/_out/age.agekey`** (gitignored — so it does NOT exist in a fresh git worktree; reference the main checkout's copy by absolute path). DB credential Secrets are `type: kubernetes.io/basic-auth`, labeled `cnpg.io/reload: "true"`.
- The **same gitea DB password** must appear in two Secrets: `infra-pg-gitea` (`databases` ns, for the CNPG managed role) and `gitea-db` (`gitea` ns, for the Gitea pod + pgloader).
- Connection target: **`infra-pg-rw.databases.svc.cluster.local:5432`**, database `gitea`, user `gitea`, `sslmode=disable` (in-cluster).
- Gitea image: `docker.gitea.com/gitea:1.26.1-rootless`. Gitea `/data` PVC: `gitea-shared-storage` (gitea ns).
- **Rollback invariant:** nothing in this plan writes to or deletes `/data/gitea.db`. pgloader only reads it. Rollback = revert the repoint commit.
- Work on branch `feat/gitea-postgres-migration` (already created off `main`). SOPS/kustomization changes may span two PRs (tenant, then repoint) — keep them separate.

---

### Task 1: Provision the `gitea` tenant on infra-pg

**Files:**
- Create: `kubernetes/apps/databases/infra-pg-gitea.sops.yaml` (databases-ns role password)
- Create: `kubernetes/apps/apps/gitea/gitea-db.sops.yaml` (gitea-ns copy of the same password)
- Create: `kubernetes/apps/databases/gitea-db-bootstrap.yaml` (superuser one-shot creating the `gitea` DB — **NOT** registered in `kustomization.yaml`)
- Modify: `kubernetes/apps/databases/infra-pg.yaml` (add `gitea` to `spec.managed.roles`)
- Modify: `kubernetes/apps/databases/kustomization.yaml` (add the two databases-ns files)
- Modify: `kubernetes/apps/apps/gitea/kustomization.yaml` (add `gitea-db.sops.yaml`)

**Interfaces:**
- Produces: DB `gitea` owned by role `gitea` on `infra-pg`; Secret `gitea-db` (keys `username`,`password`) in the `gitea` ns for Tasks 3 & 4.

- [ ] **Step 1: Generate one shared password**

```bash
PW=$(openssl rand -base64 24 | tr -d '/+=' | head -c 32); echo "$PW"   # keep this value for both secrets
```

- [ ] **Step 2: Write + encrypt the databases-ns role secret**

Create `kubernetes/apps/databases/infra-pg-gitea.sops.yaml` (plaintext first):

```yaml
# Credentials for the gitea database on infra-pg (managed role). The SAME password is duplicated into
# kubernetes/apps/apps/gitea/gitea-db.sops.yaml (gitea ns) for the Gitea pod + the pgloader migration.
apiVersion: v1
kind: Secret
metadata:
  name: infra-pg-gitea
  namespace: databases
  labels:
    cnpg.io/reload: "true"
type: kubernetes.io/basic-auth
stringData:
  username: gitea
  password: REPLACE_WITH_PW
```

Then encrypt in place:
```bash
# --git-common-dir resolves to the MAIN checkout's .git even from a linked worktree, so this works
# from either, on any machine, without hardcoding a checkout location or a shell's path syntax.
export SOPS_AGE_KEY_FILE="$(cd "$(git rev-parse --git-common-dir)/.." && pwd -P)/kubernetes/infra/_out/age.agekey"
sops --encrypt --in-place kubernetes/apps/databases/infra-pg-gitea.sops.yaml
```

- [ ] **Step 3: Write + encrypt the gitea-ns copy (same password)**

Create `kubernetes/apps/apps/gitea/gitea-db.sops.yaml`:

```yaml
# Gitea's Postgres DB password (must MATCH infra-pg-gitea in the databases ns). Consumed by the Gitea
# pod (GITEA__database__PASSWD) and the one-shot pgloader migration Job.
apiVersion: v1
kind: Secret
metadata:
  name: gitea-db
  namespace: gitea
type: kubernetes.io/basic-auth
stringData:
  username: gitea
  password: REPLACE_WITH_PW
```
```bash
sops --encrypt --in-place kubernetes/apps/apps/gitea/gitea-db.sops.yaml
```

- [ ] **Step 4: Add the `gitea` managed role to infra-pg**

In `kubernetes/apps/databases/infra-pg.yaml`, append to `spec.managed.roles` (match the existing entries' shape):

```yaml
      - name: gitea
        ensure: present
        login: true
        inherit: true
        connectionLimit: -1
        passwordSecret:
          name: infra-pg-gitea
```

- [ ] **Step 5: Create the `gitea` DB bootstrap one-shot**

`managed.roles` (Step 4) creates the **ROLE** but never a **DATABASE**, and `postInitSQL` in
`infra-pg.yaml` runs ONLY at first cluster bootstrap — so on the already-bootstrapped `infra-pg` the DB
must come from a superuser one-shot. Create `kubernetes/apps/databases/gitea-db-bootstrap.yaml`,
mirroring `litellm-db-bootstrap.yaml` exactly:

```yaml
# Gitea DB bootstrap (SQLite->Postgres migration, 2026-07-21) — OPERATOR-RUN ONE-SHOT, deliberately
# NOT listed in kustomization.yaml so Flux never applies or health-gates it: a failing Job under the
# `databases` Kustomization would wedge that layer and everything that dependsOn it.
#
# A declarative `postgresql.cnpg.io/v1 Database` CR is NOT usable here — that CRD ships in CNPG >= 1.25
# and the estate operator is 1.24.1. See the note in kustomization.yaml.
#
# Run ONCE as SUPERUSER via peer-auth inside the CNPG pod (no password, no superuser Secret needed):
#   kubectl --context admin@ai -n databases exec -i infra-pg-1 -c postgres -- \
#     psql -U postgres -v ON_ERROR_STOP=1 -f - < <(kubectl --context admin@ai -n databases \
#       get cm gitea-db-bootstrap -o jsonpath='{.data.bootstrap\.sql}')
apiVersion: v1
kind: ConfigMap
metadata:
  name: gitea-db-bootstrap
  namespace: databases
data:
  bootstrap.sql: |
    -- Idempotent: CREATE ROLE/DATABASE cannot run inside a DO block, so use psql \gexec.
    -- The role's PASSWORD is set afterward by managed.roles (infra-pg.yaml) from infra-pg-gitea —
    -- the role created here only needs to EXIST. Gitea owns the DB (it runs its own migrations).
    SELECT 'CREATE ROLE gitea LOGIN'
      WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'gitea')\gexec
    SELECT 'CREATE DATABASE gitea OWNER gitea'
      WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'gitea')\gexec
```

Apply the ConfigMap (`kubectl apply -f`) and run the SQL as above. Note `infra-pg-superuser` does **not**
exist on this estate, so the Job-based variant in `litellm-db-bootstrap.yaml` would no-op — use the
peer-auth path.

- [ ] **Step 6: Register in both kustomizations**

In `kubernetes/apps/databases/kustomization.yaml` `resources:` add **only** the secret:
```yaml
  - infra-pg-gitea.sops.yaml # basic-auth creds for the gitea DB (managed role)
```
**Do NOT register `gitea-db-bootstrap.yaml`** — it is an operator-run one-shot, kept out of the
kustomization on purpose (see the CNPG >= 1.25 note already in that file).
In `kubernetes/apps/apps/gitea/kustomization.yaml` `resources:` add:
```yaml
  - gitea-db.sops.yaml # gitea-ns copy of the infra-pg gitea DB password
```

- [ ] **Step 7: Commit, push, PR, merge**

```bash
git add kubernetes/apps/databases/infra-pg-gitea.sops.yaml kubernetes/apps/databases/gitea-db-bootstrap.yaml \
        kubernetes/apps/databases/infra-pg.yaml kubernetes/apps/databases/kustomization.yaml \
        kubernetes/apps/apps/gitea/gitea-db.sops.yaml kubernetes/apps/apps/gitea/kustomization.yaml
git commit -m "feat(gitea): provision gitea DB tenant on infra-pg (SQLite->PG migration, part 1)"
GIT_TERMINAL_PROMPT=0 git push -u gitea feat/gitea-postgres-migration
# create PR via Gitea API (gitea_admin creds from GCM), squash-merge — see repo forge pattern
```

- [ ] **Step 8: Verify the DB + role exist (this is the task's test)**

```bash
kubectl --context admin@ai -n databases exec infra-pg-1 -c postgres -- \
  psql -U postgres -At -c "SELECT datname FROM pg_database WHERE datname='gitea'; SELECT rolname,rolcanlogin FROM pg_roles WHERE rolname='gitea';"
```
Expected: prints `gitea` (database) and `gitea|t` (role, can-login).

**Hard precondition, not a fallback:** the DB exists ONLY because Step 5's bootstrap SQL was run. There
is no declarative path to it on this operator, and no state in which the DB is merely "missing" while
everything else is fine — if a `kind: Database` resource were present, the whole `databases`
Kustomization would have stopped applying and every dependent layer would already be degrading. So if
this check fails, do **not** proceed: re-run Step 5, and confirm `kubectl -n flux-system get kustomization
databases` is `Ready=True` before continuing. Then confirm login:
```bash
kubectl --context admin@ai -n databases exec infra-pg-1 -c postgres -- \
  env PGPASSWORD="$PW" psql "host=infra-pg-rw dbname=gitea user=gitea sslmode=disable" -At -c "SELECT 1;"
```
Expected: `1`.

---

### Task 2: Author the one-shot pgloader migration Job

**Files:**
- Create: `kubernetes/apps/apps/gitea/pgloader-migration.yaml` (ConfigMap load-file + Job; **NOT** added to any kustomization — imperative one-shot, mirroring `litellm-db-bootstrap.yaml`)

**Interfaces:**
- Consumes: Secret `gitea-db` (gitea ns), PVC `gitea-shared-storage`.
- Produces: schema + data loaded into the `gitea` DB.

- [ ] **Step 1: Write the pgloader load file + Job**

Create `kubernetes/apps/apps/gitea/pgloader-migration.yaml`:

```yaml
# One-shot SQLite->Postgres migration for Gitea. OPERATOR-RUN, deliberately NOT in kustomization.yaml
# (a failing Job must never wedge the Flux `apps` layer). Run ONLY during the cutover window with Gitea
# scaled to 0. pgloader reads /data/gitea.db (read-only intent) and loads the gitea DB on infra-pg.
apiVersion: v1
kind: ConfigMap
metadata:
  name: gitea-pgloader
  namespace: gitea
data:
  gitea.load: |
    LOAD DATABASE
      FROM sqlite:///data/gitea.db
      INTO postgresql://gitea@infra-pg-rw.databases.svc.cluster.local:5432/gitea
    WITH include drop, create tables, create indexes, reset sequences,
         quote identifiers, batch rows = 5000, prefetch rows = 5000
    SET work_mem to '128MB', maintenance_work_mem to '512MB'
    ;
---
apiVersion: batch/v1
kind: Job
metadata:
  name: gitea-pgloader
  namespace: gitea
spec:
  backoffLimit: 1
  ttlSecondsAfterFinished: 86400
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: pgloader
          image: dimitri/pgloader:latest
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -eu
              export PGPASSWORD="$DB_PASSWORD"
              pgloader --verbose /cfg/gitea.load
          env:
            - name: DB_PASSWORD
              valueFrom:
                secretKeyRef: { name: gitea-db, key: password }
          volumeMounts:
            - { name: data, mountPath: /data, readOnly: true }
            - { name: cfg, mountPath: /cfg, readOnly: true }
      volumes:
        - name: data
          persistentVolumeClaim: { claimName: gitea-shared-storage, readOnly: true }
        - name: cfg
          configMap: { name: gitea-pgloader }
```

Note: pgloader picks up `PGPASSWORD` for the target DSN (the DSN omits the password deliberately). `include drop` makes re-runs idempotent (drops+recreates the target tables).

- [ ] **Step 2: Commit (do not apply yet)**

```bash
git add kubernetes/apps/apps/gitea/pgloader-migration.yaml
git commit -m "chore(gitea): one-shot pgloader SQLite->PG migration Job (operator-run, part 2)"
GIT_TERMINAL_PROMPT=0 git push
```

---

### Task 3: Cutover — migrate the data (maintenance window starts)

**Interfaces:**
- Consumes: Task 1 DB/role, Task 2 Job manifest.
- Produces: Gitea data present in the `gitea` Postgres DB, verified against SQLite.

- [ ] **Step 1: Freeze Gitea (stop writes for a consistent SQLite)**

```bash
CTX="--context admin@ai"
# Prevent Flux from scaling gitea back up mid-migration:
kubectl $CTX -n gitea patch helmrelease gitea --type merge -p '{"spec":{"suspend":true}}'
kubectl $CTX -n gitea scale deploy gitea --replicas=0
kubectl $CTX -n gitea rollout status deploy gitea --timeout=120s || true
kubectl $CTX -n gitea get pods -l app.kubernetes.io/name=gitea    # expect: no running gitea pod
```

- [ ] **Step 2: Capture pre-migration SQLite row counts (baseline for verification)**

Run a short read-only hostPath pod (Gitea is stopped, so read the file directly) — reuse the session's pattern: a `python:3.12-slim` pod on the PVC's node, `sqlite3`-free via Python, printing counts for `user`, `repository`, `issue`, `pull_request`, `action_run`, `action_task`, `access_token`. Record the numbers.

- [ ] **Step 3: Run the pgloader Job**

```bash
kubectl $CTX -n gitea delete job gitea-pgloader --ignore-not-found
kubectl $CTX apply -f kubernetes/apps/apps/gitea/pgloader-migration.yaml
kubectl $CTX -n gitea wait --for=condition=complete job/gitea-pgloader --timeout=1800s \
  || kubectl $CTX -n gitea logs job/gitea-pgloader --tail=80
kubectl $CTX -n gitea logs job/gitea-pgloader | tail -40   # pgloader summary: rows read == rows loaded, 0 errors
```
Expected: pgloader summary shows every table `read == imported`, `0` errors, sequences reset.

- [ ] **Step 4: Verify counts match (this is the task's test)**

```bash
for t in user repository issue pull_request action_run action_task access_token; do
  echo -n "$t: "
  kubectl $CTX -n databases exec infra-pg-1 -c postgres -- psql -U postgres -At -d gitea -c "SELECT count(*) FROM \"$t\";"
done
```
Expected: each count equals the Step 2 SQLite baseline. If any mismatch, STOP — do not proceed to repoint; investigate or roll back (Gitea is still safe on SQLite: unsuspend HR + scale to 1).

---

### Task 4: Repoint Gitea at Postgres

**Files:**
- Modify: `kubernetes/apps/apps/gitea/gitea.yaml` (database block + password env; remove SQLite tuning)

**Interfaces:**
- Consumes: verified `gitea` DB (Task 3), Secret `gitea-db`.
- Produces: Gitea running on Postgres.

- [ ] **Step 1: Edit the HelmRelease database config**

In `kubernetes/apps/apps/gitea/gitea.yaml`, replace the `database:` block:
```yaml
        database:
          DB_TYPE: postgres
          HOST: infra-pg-rw.databases.svc.cluster.local:5432
          NAME: gitea
          USER: gitea
          SSL_MODE: disable
```
(Delete `SQLITE_JOURNAL_MODE` and `SQLITE_TIMEOUT`.) Add a password env to the existing `additionalConfigFromEnvs:` list:
```yaml
        - name: GITEA__database__PASSWD
          valueFrom:
            secretKeyRef:
              name: gitea-db
              key: password
```

- [ ] **Step 2: Commit + PR + merge**

```bash
git add kubernetes/apps/apps/gitea/gitea.yaml
git commit -m "feat(gitea): switch DB_TYPE to postgres on infra-pg (SQLite->PG migration, part 3)"
GIT_TERMINAL_PROMPT=0 git push
# open PR, squash-merge to main via the Gitea API
```

- [ ] **Step 3: Resume Gitea onto Postgres**

```bash
CTX="--context admin@ai"
kubectl $CTX -n gitea patch helmrelease gitea --type merge -p '{"spec":{"suspend":false}}'
# force reconcile so it doesn't wait the 1h interval:
kubectl $CTX -n flux-system annotate --overwrite gitrepository/flux-system "reconcile.fluxcd.io/requestedAt=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
kubectl $CTX -n gitea rollout status deploy gitea --timeout=300s
```

- [ ] **Step 4: Verify Gitea is on Postgres (task test)**

```bash
POD=$(kubectl $CTX -n gitea get pod -l app.kubernetes.io/name=gitea --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
MSYS_NO_PATHCONV=1 kubectl $CTX -n gitea exec "$POD" -- sh -c 'grep -A2 "^\[database\]" /data/gitea/conf/app.ini'
kubectl $CTX -n gitea logs "$POD" --tail=50 | grep -iE 'error|locked|panic' || echo "no DB errors"
```
Expected: `DB_TYPE = postgres` (or `[database] DB_TYPE=postgres`); Gitea Ready; no `database is locked`.

---

### Task 5: Functional verification & finalize

- [ ] **Step 1: End-to-end functional checks**

- OIDC login at `https://git.chifor.me` (Authelia) succeeds.
- `git clone` + a test `push` over HTTPS and SSH succeed.
- An existing repo's issues/PRs render; open + comment on a test issue.
- Trigger a Gitea Actions workflow; it runs green and logs stream (DBFS-in-PG → S3).
- `/metrics` returns gitea_* metrics (bearer token).

- [ ] **Step 2: Confirm no lock/contention regressions**

```bash
kubectl --context admin@ai -n gitea logs deploy/gitea --since=15m | grep -ci 'database is locked'   # expect 0
```

- [ ] **Step 3: Record rollback window + cleanup**

- Leave `/data/gitea.db` in place for ≥1 week (rollback = `git revert` the Task 4 commit → Gitea back on SQLite).
- After the grace period (separate follow-up, NOT this plan): delete the SQLite file, shrink/replace `/data`, and remove `pgloader-migration.yaml`.

---

## Self-Review

- **Spec coverage:** infra-pg tenant (T1) ✓, pgloader migration (T2–T3) ✓, HelmRelease repoint (T4) ✓, cutover sequence (T3–T4) ✓, rollback (Global Constraints + T5) ✓, verification/success criteria (T3 counts + T5 functional) ✓. Gap check: the spec's "gitea-ns password availability" is satisfied by `gitea-db` (T1). The spec's former open question — "verify whether the `Database` CRD reconciles here" — was **resolved at implementation time (2026-07-21) and is closed**: the CRD ships in CNPG >= 1.25, the estate operator is 1.24.1, so there is no declarative path and T1 Step 5's bootstrap one-shot is the only one. It is a precondition, not a fallback.
- **Placeholders:** `REPLACE_WITH_PW` is a deliberate operator substitution (generated in T1 S1), not a plan gap. Row-count baseline (T3 S2) is captured live because the exact numbers aren't knowable pre-run. No TODO/TBD/"handle errors" left.
- **Consistency:** DB name `gitea`, user `gitea`, host `infra-pg-rw.databases.svc.cluster.local:5432`, secrets `infra-pg-gitea`(databases)/`gitea-db`(gitea) used identically across T1–T4.
