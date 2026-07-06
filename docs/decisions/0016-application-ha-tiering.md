# ADR 0016 — Application availability tiering (survive one node down)

**Status:** accepted · **Date:** 2026-07-06 · **Relates to:** ADR 0007 (storage classes), ADR 0009
(colocation + RAM budget + PriorityClasses), ADR 0012 (Authelia — amended by this ADR)

## Context

The 2026-07-06 carve→GTT host reboots (one node at a time, etcd 3/3 throughout) still took the strive
platform down (issue #100). Kubernetes tolerated every node loss; the *workload topology* did not:

- Nearly every stateful workload runs as a **single replica on RWO storage** — no replica to serve
  while its node is away, and on a *hard* node loss the RWO iSCSI LUN takes ~6 min to force-detach
  (`maxWaitForUnmountDuration`, hard-coded) before the pod can even reschedule. StatefulSet pods on an
  unreachable node additionally stick in `Terminating` until human action.
- The audit found **2 PDBs cluster-wide** — one of which (`strive-pg-primary`, allowed-disruptions 0
  over a 1-instance cluster) *blocked* every drain for the full 5-min timeout and got the DB
  hard-killed each time. Zero topologySpreadConstraints; the sole multi-replica app (cloudflared ×2)
  had neither PDB nor spread.
- ADR 0009's finite RAM budget (~84 GiB stateful across 3 nodes, limits already overcommitted on
  cp2/cp3) makes HA-everything unaffordable. Availability must be a *deliberate, per-workload* choice.

## Decision

Every workload is assigned to a tier; the tier dictates its manifests. Node maintenance follows
`docs/runbooks/node-maintenance.md`.

### 1. Tier A — must survive any single node loss (zero downtime)

| Workload | Mechanism |
|---|---|
| `strive-pg` (platform repo) | CNPG `instances: 3`, async streaming, auto-switchover/failover (spec: `docs/superpowers/specs/2026-07-06-strive-platform-ha-tier1-spec.md`) |
| `infra-pg` (databases ns) | CNPG `instances: 2`, **required** hostname anti-affinity, operator-managed PDBs |
| `authelia` ×2 (auth) | storage → infra-pg Postgres, sessions → auth-valkey; PDB + spread |
| `grafana` ×2 (monitoring) | DB → infra-pg Postgres (drops SQLite-on-RWO); PDB + spread |
| `cloudflared` ×2 (edge) | stateless; PDB + spread + 60 s tolerations |

Manifest pattern for every Tier-A Deployment: **PDB `maxUnavailable: 1` +
`unhealthyPodEvictionPolicy: AlwaysAllow`** (a crash-looping pod must never wedge a drain), hostname
**topologySpread with `whenUnsatisfiable: ScheduleAnyway`**, and — *only for PVC-free pods* —
**`tolerationSeconds: 60`** on the not-ready/unreachable NoExecute taints.

`auth-valkey` (Authelia's session store) is deliberately **accepted-ephemeral**: 1 replica, no PVC,
platform-critical priority, 60 s tolerations. A bounce costs one SSO re-login; a node loss reschedules
it in ~100 s. Sentinel-grade session HA is not worth the moving parts here.

### 2. Tier B — accepted singletons (a node event moves them once; blips documented in the runbook)

| Workload | Why accepted |
|---|---|
| prometheus / alertmanager / loki / tempo | HA monitoring (2× TSDB + Thanos dedup, scalable loki/tempo) costs more RAM than the observability gap during rare maintenance; alertmanager is ephemeral (silences lost — noted) |
| gitea / vaultwarden / ntfy | SQLite-on-RWO by design; app-level HA would mean architecture changes (external DB / clustering) for tools that tolerate a 1–3 min blip |
| open-webui / litellm / homepage / gatus / headlamp / oauth2-proxy / trivy / renovate | low criticality; stateless ones reschedule in seconds anyway |
| valkey-master (strive, platform repo) | cache/rate-limit store; airlock has an explicit in-memory fallback — see the Tier-1 spec |

The Tier-B list is **encoded as the negative regexes** in
`kubernetes/apps/infrastructure/monitoring/ha-rules.yaml` (`ailab:workload_singletons:unaccepted`):
a new, unlisted singleton surfaces in that inventory until it is replicated or added here + there.

### 3. Storage & scheduling rules

- **No `local-path` for anything that must reschedule** (currently zero users — keep it that way;
  it remains for scratch/interim use only).
- **App-level replication over RWO reattach** as the HA mechanism for anything that must fail over
  fast (Postgres streaming beats volume migration; the 6-min detach wall is immovable).
- **PDB only when replicas ≥ 2.** A PDB on a singleton is a drain-blocker, not protection.
- **`ScheduleAnyway`, not `DoNotSchedule`**, for spread: with 2 replicas on 3 nodes and one node
  drained, a hard constraint deadlocks surge rollouts (maxSurge 1 needs a 3rd hostname domain);
  `minDomains` only makes DoNotSchedule stricter. The scheduler spreads correctly in steady state
  either way, and `CriticalWorkloadSingleReplica` catches sustained co-location degradation.
- **60 s tolerations only for PVC-free pods.** For RWO pods, faster eviction just reaches the 6-min
  detach wall sooner, and a 60 s trigger on a *transient* node blip starts a detach/reattach race
  against a node that may still hold the LUN mounted — an ext4-on-iSCSI corruption risk the 300 s
  default buffers. The sanctioned fast path for a *confirmed-dead* node is the human-driven
  `node.kubernetes.io/out-of-service` taint (runbook), which also unsticks Terminating STS pods.

### 4. Guardrails (alerting)

`ha-rules.yaml`: `CriticalWorkloadSingleReplica` (Tier-A workload below 2 available for 10 m,
critical), `PodDisruptionBudgetBlockingDrains` (any PDB at allowed-disruptions 0 for 1 h, warning —
CNPG `*-primary` PDBs excluded: always 0 by design, the operator switches over ahead of drains), and
the `ailab:workload_singletons:unaccepted` inventory recording rule.

## Consequences

- Draining any one node: Tier A rides through (strive-pg/infra-pg switch over in seconds); Tier B
  blips per the runbook table; drains complete inside talosctl's 5-min window (no PDB hangs).
- Cost: +~1.3 GiB memory requests (infra-pg ×2 @512 Mi req=lim, auth-valkey 64 Mi) + ~10 Gi
  qnap-iscsi + one more Postgres to keep patched — inside the ADR 0009 budget (cp1 was near-idle).
- New coupling, documented: the CNPG **operator** (cnpg-system) is installed by the external
  `cchifor/platform` repo; ailab's `databases` Flux Kustomization consumes its CRDs (fail-retry until
  present on a fresh bootstrap). If the platform repo ever leaves this cluster, the operator install
  moves into `kubernetes/apps/infrastructure/`.
- Grafana/Authelia lose their SQLite fragility class entirely (the 2026-07 grafana UID-mismatch
  crash-loop cannot recur on Postgres).

## Alternatives rejected

- **HA-everything** — RAM budget (ADR 0009) and operational surface; most Tier-B apps tolerate a
  1–3 min move.
- **Talos/controller-manager tuning** (`node-monitor-grace-period`, attach-detach flags) — cluster-wide
  blast radius on a lab where CP VMs share hosts with GPU LXCs (a false NotReady evicts a healthy
  node's pods); `--pod-eviction-timeout` is defunct (taint-based eviction owns this, tuned per-workload
  via tolerationSeconds); the 6-min force-detach isn't configurable at all. `controlplane.yaml.tftpl`
  stays untouched.
- **DoNotSchedule spread (+ minDomains)** — wedges rollouts during maintenance; see §3.
- **RWO→NFS migration for HA** — SQLite/TSDB on NFS is unsafe (locking), per existing file comments.
- **Descheduler for post-maintenance rebalance** — new operator vs ADR 0009's built-ins-only stance;
  the runbook's one manual pod-delete covers it.
