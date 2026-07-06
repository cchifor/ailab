# Tier-1 HA Change Spec — strive platform (`cchifor/platform`) on ailab

**Companion to:** [cchifor/ailab#100](https://github.com/cchifor/ailab/issues/100) — "Application HA:
seamlessly tolerate one node down" · **ailab-side decision record:** ADR 0016
**Scope:** the changes that live in the **external `cchifor/platform` repo** (Flux path
`deploy/gitops/flux/clusters/ailab`). Self-contained — apply there directly.
**Evidence basis:** 2026-07-06, live cluster `admin@ai` (ns `strive-ailab`), CNPG operator 1.24.1,
CNPG 1.24 docs + operator source (`pkg/specs/poddisruptionbudget.go`, release-1.24), Bitnami valkey
chart CHANGELOG. Anything not visible from ailab is flagged **`[verify in platform repo]`**.

## 0. Summary

| # | Change | Flux artifact | Risk | Downtime |
|---|---|---|---|---|
| 1 | Fix dangling `valkey-primary` URL in 4 worker Deployments | `platform-workers` | none | none (rolling) |
| 2 | CNPG `strive-pg`: `instances: 1 → 3` | `platform-cnpg` | low | zero (additive) |
| 3 | Valkey: audit → keep standalone ×1, align values schema, plan bitnamilegacy exit | `platform-valkey` | low | one pod bounce |
| 4 | (Tier 1.5, optional) `web` + `airlock` → 2 replicas + PDB | HelmRelease `strive` values | low | none |
| 5 | Acceptance drain test | — | — | ≤ ~10 s write blip, once |

Recommended order: **1 → 2 → 5 → 3 → 4.**

## 1. CNPG `strive-pg`: 1 → 3 instances

### 1.1 Why 3, not 2

CNPG operator PDB behaviour (verified in operator source, release-1.24):

- The **primary PDB** (`strive-pg-primary`, minAvailable 1) exists at every instance count — its
  allowed-disruptions is *always* 0. That's fine: on a drain the operator performs a **switchover
  ahead of the drain**, after which the pod on that node is a replica.
- The **replicas PDB** (`strive-pg`, minAvailable = instances − 2) is created **only at
  `instances ≥ 3`** (the code returns nil below 3).

| | `instances: 2` | `instances: 3` (recommended) |
|---|---|---|
| Drain of primary's node | works (switchover, then evict) | works (switchover + replicas PDB allows 1) |
| Redundancy DURING the window | **zero** — one instance left; a second failure = the 2026-07-06 outage again | primary + 1 replica still streaming |
| Sync replication viable later | no (lone replica down ⇒ writes stall) | yes (1-of-2 quorum) |
| Cost vs today | +25 Gi QNAP, +1 Gi mem req | +50 Gi QNAP (2×(20+5) Gi, thin), +2 Gi mem req, +500 m CPU req |

The point of #100 is surviving *any* one-node event without a scramble — and rolling maintenance is
exactly when a 2-instance cluster degrades back to a SPOF. Take 3.

### 1.2 The change (entire diff)

In the `strive-pg` Cluster manifest under `platform-cnpg` `[verify in platform repo: exact file]`:

```diff
 spec:
-  instances: 1
+  instances: 3
```

Everything else already in the live spec is multi-instance-ready and needs **no** edits:
`primaryUpdateStrategy: unsupervised` + `primaryUpdateMethod: switchover`, HA replication slots
(`replicationSlots.highAvailability`), `wal_keep_size: 512MB`, `max_replication_slots: 32`,
per-instance storage stanzas (20 Gi data + 5 Gi WAL each, `qnap-iscsi`), `enablePDB: true`.

### 1.3 Keep `podAntiAffinityType: preferred` (do NOT switch to required)

3 instances on 3 nodes with `required` looks perfect until a drain: the evicted instance would sit
**Pending** for the whole maintenance window (only 2 nodes available, both occupied). `preferred`
lets it double up temporarily → **3/3 running even mid-maintenance** (iSCSI reattaches anywhere,
~30–60 s). After the node returns, k8s won't rebalance on its own — delete the doubled-up **replica**
pod (never the primary) once; preference places it on the empty node.
(Contrast: ailab's `infra-pg` runs `required` — with only 2 instances on 3 nodes it never blocks.)

### 1.4 Stay async; do not enable sync replication on operator 1.24

- Async RPO > 0 applies only to *hard* primary loss (streaming lag on an idle LAN ≈ 0); graceful
  drains use switchover and lose nothing.
- CNPG 1.24's `.spec.postgresql.synchronous` is deliberately strict: with the required standby count
  unavailable, **all writes stall** — it turns partial failures into full outages on a homelab.
- If RPO=0 is wanted later: upgrade the operator (cnpg-system HelmRelease `cnpg-operator`,
  chart 0.22.1 → operator ≥ 1.25) and use `synchronous: { method: any, number: 1,
  dataDurability: preferred }` (sync when possible, auto-fallback to async). Follow-up, not now.

### 1.5 Timing expectations (for the runbook)

- **Graceful drain/reboot:** operator-driven switchover in seconds; `strive-pg-rw` endpoint flips
  atomically; apps see one connection reset. Expect a **~2–10 s write-path gap**.
- **Hard node loss:** detection is the long pole (~40 s node-monitor + readiness); promotion needs
  **no volume movement** (each instance owns its PVCs) → **~40 s–2 min** to a new primary,
  `failoverDelay: 0` already set. The old 6-min iSCSI force-detach is off the critical path entirely.

### 1.6 Rollout mechanics 1 → 3

Editing `instances` is the whole trigger. The operator adds replicas **one at a time**: PVC pair →
join job (`pg_basebackup` clone from the primary, WAL streamed via the pre-existing slots — no WAL
loss possible mid-join) → hot standby + HA slot. The primary is never restarted → zero downtime.
Minutes per replica for a few-GiB DB; worst case ~4–6 min per replica for a full 20 Gi at 1 GbE.

**Pre-checks:**

```bash
# actual DB size (clone time ∝ data, not PVC size)
kubectl --context admin@ai cnpg psql strive-pg -n strive-ailab -- \
  -tAc "SELECT pg_size_pretty(sum(pg_database_size(oid))) FROM pg_database;"
kubectl --context admin@ai cnpg psql strive-pg -n strive-ailab -- -tAc "SHOW max_wal_senders;"   # default 10, fine
kubectl --context admin@ai top nodes                                     # +1 Gi req lands on each of 2 nodes
# QNAP pool: >= ~60 Gi free for 2x(20+5) Gi thin LUNs
```

**Verification:**

```bash
kubectl --context admin@ai cnpg status strive-pg -n strive-ailab
#  -> 3 instances, "Cluster in healthy state", replication lag ~0, slots _cnpg_* active
kubectl --context admin@ai get pods -n strive-ailab -l cnpg.io/cluster=strive-pg -o wide   # one per node
kubectl --context admin@ai get pdb -n strive-ailab
#  -> strive-pg-primary (allowed 0, by design) AND strive-pg (minAvailable 1, allowed 1)
```

**Monitoring hook (small, recommended):** alert on CNPG replication-lag / inactive-slot metrics from
the already-enabled PodMonitor (an inactive slot from a long-dead replica = WAL bloat on the 5 Gi WAL
volume).

## 2. Valkey

### 2.1 The bug — 4 Deployments point at a Service that does not exist

Only `valkey-master` + `valkey-headless` Services exist. 13 of 17 strive workloads use the correct
`redis://valkey-master.strive-ailab.svc.cluster.local:6379` (incl. airlock's
`APP__AIRLOCK__RATE_LIMIT_REDIS_URL`); these four use `redis://valkey-primary...` (NXDOMAIN):

| Deployment | Env | Owner |
|---|---|---|
| `digest-worker` | `REDIS_URL` | `platform-workers` |
| `integration-worker` | `REDIS_URL` | `platform-workers` |
| `mcp-worker` | `REDIS_URL` | `platform-workers` |
| `workflow-worker` | `REDIS_URL` | `platform-workers` |

All four run 0-restarts despite it — the connection is evidently lazy/optional, i.e. **the bug is
silent**. Fix → `valkey-master`, then check worker logs for what has been quietly degraded
`[verify in platform repo / app code: what REDIS_URL drives in workers — queues vs cache]`.

### 2.2 Root cause: chart-version schema mismatch (not a half-reverted architecture flip)

- Bitnami valkey chart **2.0.0 renamed master→primary** everywhere (bitnami/charts #30024). The
  deployed HelmRelease pins **1.0.3** (master-era) with `architecture: standalone` → live Service is
  `valkey-master`.
- But the HelmRelease **values use the ≥2.x schema** (`primary:` block — persistence 4 Gi nfs-csi,
  256 Mi limit, hardened securityContext). Chart 1.0.3 expects `master:` and **silently ignores the
  whole block** — proven live: the pod runs chart-default resources and a default **8 Gi** PVC.
- The 4 worker manifests were likewise authored against the ≥2.x Service name.
  `[verify in platform repo: git history — was a 2.x upgrade attempted and rolled back?]`

**Reconciliation, both ways:**

- **Path 1 (recommended now):** stay on 1.0.3 naming — fix the 4 workers (2.1) and translate the
  values `primary:` → `master:` so the intended resources/securityContext/4 Gi actually apply (one pod
  bounce).
- **Path 2 (later, deliberate):** upgrade the chart to ≥2.x/4.x — Service becomes `valkey-primary`,
  which then breaks the **other 13** deployments' URLs; requires flipping the URL in the strive chart
  values too `[verify in platform repo]`. Bundle only with the image migration (2.5).

Also observed live: `auth.enabled: false` → `ALLOW_EMPTY_PASSWORD=yes` — valkey is **passwordless**
on the cluster network. Cluster-internal only, but make it an explicit accept-or-fix.

### 2.3 Audit before choosing (what does valkey actually hold?)

```bash
V="kubectl --context admin@ai exec -n strive-ailab valkey-master-0 -- valkey-cli"
$V INFO keyspace; $V INFO persistence; $V INFO clients
$V --scan --count 200 | head -50        # key shapes: rate-limit buckets? queues? sessions?
$V CLIENT LIST                          # source IPs -> which pods really connect
```

Keys with TTLs + tolerable loss ⇒ cache/rate-limit. Lists/streams consumed by workers ⇒ durable
state. Two strong priors point at cache-only: airlock sets `AIRLOCK_ALLOW_MEMORY_RATE_LIMIT=1`
(explicit in-memory fallback) and the **events bus is Postgres** (`events-bus-url` →
`strive-pg-rw`), not valkey.

### 2.4 Decision

- **Cache/rate-limit only (expected) → keep `standalone` ×1.** Optionally drop persistence (no PVC →
  instant reschedule; rate-limit counters reset, fine) and add 60 s not-ready/unreachable tolerations
  `[verify chart 1.0.3 exposes master.tolerations]`. Node loss ⇒ valkey back in ≤ ~2 min, airlock
  falls back to in-memory limits meanwhile.
- **Durable state found → do NOT reach for chart replication+sentinel:** replication without sentinel
  is read-replicas-only (no failover); with sentinel, plain `redis://` clients can't follow it without
  app changes. The right move is moving that state into **strive-pg** (now HA) — the platform already
  runs its events bus there.

### 2.5 bitnamilegacy exit (schedule, don't rush)

`docker.io/bitnamilegacy/valkey:8.0.1` is the frozen legacy namespace (no CVE fixes). End-state:
replace the chart with a ~50-line plain manifest on the official **`valkey/valkey:8.x`** image
(plain-manifest precedent already exists in `platform-workers`; the Bitnami chart can't just repoint
images — its scripts expect `/opt/bitnami`). Bundle with Path 2 naming if ever taken. ailab's new
`auth-valkey` (issue #100 PR D) already uses the official image — copy that manifest as the starting
point.

## 3. Tier 1.5 (optional): stateless hot-path replicas

`gatekeeper` already runs the target pattern (×2 + PDB, allowed-disruptions 1). Extend the same knob
to **`web`** and **`airlock`** (user-facing synchronous path; airlock already has probes +
maxUnavailable 0 rollouts) `[verify in platform repo: how gatekeeper's replicas+PDB are expressed in
chart values]`. Keep limits modest (cp2/cp3 limits are overcommitted). Note: two airlocks on the
in-memory fallback rate-limit independently (per-pod buckets) — acceptable; valkey-backed limits stay
global. Workers/async services stay ×1 (accepted, ADR 0016 Tier B).

## 4. Acceptance test — "zero downtime for the DB path"

Preconditions: §1 rolled out, `cnpg` kubectl plugin installed, cluster healthy 3/3.

```bash
# A) cluster watch
watch -n2 "kubectl --context admin@ai cnpg status strive-pg -n strive-ailab | head -30"

# B) continuous WRITE probe through -rw (a read probe would hide a primary outage)
PGPASS=$(kubectl --context admin@ai get secret strive-pg-app -n strive-ailab -o jsonpath='{.data.password}' | base64 -d)
kubectl --context admin@ai run db-probe -n strive-ailab --restart=Never \
  --image=ghcr.io/cloudnative-pg/postgresql:16.9 --env="PGPASSWORD=$PGPASS" --command -- bash -c '
  psql -h strive-pg-rw -U app -d app -c "CREATE TABLE IF NOT EXISTS ha_probe(ts timestamptz)";
  while true; do
    psql -h strive-pg-rw -U app -d app -tAc "INSERT INTO ha_probe VALUES (now()) RETURNING ts" >/dev/null 2>&1 \
      && echo "$(date +%T) WRITE-OK" || echo "$(date +%T) WRITE-FAIL"; sleep 1; done'
kubectl --context admin@ai logs -n strive-ailab db-probe -f

# C) app-path probe (airlock readiness; add the external URL if exposed)
kubectl --context admin@ai run app-probe -n strive-ailab --restart=Never --image=curlimages/curl --command -- sh -c \
  'while true; do echo "$(date +%T) airlock=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 \
   http://airlock.strive-ailab.svc.cluster.local:5100/api/v1/health/ready)"; sleep 1; done'

# Test 1 — drain the PRIMARY's node
PRIMARY_NODE=$(kubectl --context admin@ai get pod -n strive-ailab \
  -l cnpg.io/cluster=strive-pg,cnpg.io/instanceRole=primary -o jsonpath='{.items[0].spec.nodeName}')
kubectl --context admin@ai drain "$PRIMARY_NODE" --ignore-daemonsets --delete-emptydir-data --timeout=8m
# PASS: switchover within seconds; <= ~10 consecutive WRITE-FAILs; drain COMPLETES (no PDB hang);
#       evicted instance rejoins as replica (3/3 within ~2-3 min).

# Test 2 — drain a REPLICA-only node
# PASS: zero WRITE-FAILs; replicas PDB permits exactly one eviction; back to 3/3 after.

# wrap-up
kubectl --context admin@ai uncordon "$PRIMARY_NODE"   # and the replica node
kubectl --context admin@ai cnpg psql strive-pg -n strive-ailab -- -c "DROP TABLE ha_probe"
kubectl --context admin@ai delete pod db-probe app-probe -n strive-ailab
```

Repeat Test 1 via `talosctl shutdown` for the full end-to-end (its 5-min drain window is now ample).

## 5. Consolidated `[verify in platform repo]` list

1. Exact file paths under `deploy/gitops/flux/clusters/ailab/` for: strive-pg Cluster
   (platform-cnpg), the 4 worker Deployments (platform-workers), the valkey HelmRelease
   (platform-valkey), strive chart values (platform-app).
2. Whether the workers' `REDIS_URL` is one templated value or 4 copies.
3. Worker app code: what REDIS_URL drives (queues vs cache) + logs for silent redis failures.
4. Valkey HelmRelease git history (was a 2.x upgrade attempted/reverted?).
5. Chart 1.0.3 `master.tolerations` support (for the fast-reschedule tweak).
6. Where the strive chart sources `valkey-master` URLs (only needed for Path 2).
7. The app's external endpoint for an outside-in acceptance probe.
8. QNAP pool free space ≥ ~60 Gi before the CNPG scale-up.
