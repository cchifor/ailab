# Plan — Application HA: tolerate one node down (issue #100)

Dated planning record (historical — don't rewrite). Decision record: **ADR 0016**. Operations:
`docs/runbooks/node-maintenance.md`. Cross-repo spec:
`docs/superpowers/specs/2026-07-06-strive-platform-ha-tier1-spec.md`.

## Problem

The 2026-07-06 carve→GTT reboots (one node at a time, etcd 3/3 throughout) still took the strive
platform down: critical stateful workloads are single instances on RWO storage — nothing to serve
while their node is away, plus a ~6-min iSCSI force-detach on hard loss. The `strive-pg-primary` PDB
(allowed-disruptions 0 over a 1-instance CNPG cluster) additionally blocked every drain until the
5-min timeout hard-killed the DB.

## Key findings (exploration, 2026-07-06)

- Every stateful workload in `kubernetes/apps/**` is `replicas: 1` + `Recreate` on `qnap-iscsi` RWO
  (or nfs-csi). Zero PDBs / topologySpread / anti-affinity in the repo; only cloudflared runs ×2.
- Authelia: SQLite on RWO **and in-process sessions** → cannot scale without a DB + session store.
  It fronts SSO for Grafana, Open WebUI, Homepage, zot, Gitea.
- Grafana: implicit SQLite on RWO (no `[database]` config), `Recreate`.
- CNPG operator (1.24.1) already runs cluster-wide, installed by the external `cchifor/platform`
  repo; strive-pg / valkey / the whole strive data tier live in that repo (Tier 1 = cross-repo).
- Live bug found: 4 strive worker Deployments point at `valkey-primary`, a Service that doesn't
  exist (chart 1.0.3 master-era vs ≥2.x values schema — the `primary:` values block is silently
  ignored). Valkey is passwordless (`auth.enabled: false`).
- `local-path`: zero PVCs (Tier-3 audit clean). No SPOF/singleton alerting existed.
- Headroom: requests ~36 % CPU / ~30 % mem; cp1 near-idle; memory *limits* overcommitted on cp2/cp3.

## Decisions (owner-approved)

1. ailab implements Tiers 2–4; `cchifor/platform` gets a precise spec for Tier 1 (strive-pg ×3,
   valkey fix/audit).
2. One shared 2-instance CNPG cluster **`infra-pg`** (new `databases` ns/layer) hosts the `grafana` +
   `authelia` databases; consumes the platform-installed operator (documented coupling).
3. Authelia sessions → new dedicated ephemeral **`auth-valkey`** (no PVC; a bounce = one re-login).
4. Patterns: PDB `maxUnavailable: 1` + `AlwaysAllow` only where replicas ≥ 2; spread
   `ScheduleAnyway` (DoNotSchedule wedges surge rollouts during maintenance); 60 s tolerations only
   for PVC-free pods; CNPG `*-primary` PDBs excluded from drain-blocker alerting (always 0 by
   design); infra-pg anti-affinity `required` (2 pods / 3 nodes), strive-pg stays `preferred`
   (3 pods / 3 nodes); **no Talos/controller-manager tuning** (rejected — ADR 0016).

## Delivery (4 squash-merge PRs + cross-repo spec)

- **PR A — guardrails + docs:** cloudflared PDB/spread/60s-tolerations; `ha-rules.yaml`
  (CriticalWorkloadSingleReplica [staged regex], PodDisruptionBudgetBlockingDrains, unaccepted-
  singleton inventory recording rule); `node-maintenance.md` runbook; ADR 0016; the platform Tier-1
  spec; README row; this record.
- **PR B — databases layer:** Flux Kustomization `databases` (dedicated — CRD-bootstrap race
  self-heals, qnap-storage precedent; healthCheckExprs on the Cluster Ready condition); ns +
  limitrange + 2 SOPS basic-auth secrets; `infra-pg` Cluster (×2, pg 17.10, 5 Gi qnap-iscsi each,
  required anti-affinity, initdb grafana + managed role/postInitSQL authelia);
  `InfraPostgresSingleInstance` alert.
- **PR C — Grafana:** drop persistence/Recreate → `replicas: 2`, `[database]` → infra-pg-rw,
  password via `envValueFrom` (SOPS duplicate), PDB/spread/tolerations via chart values; fresh DB
  accepted (dashboards are ConfigMap-provisioned; OIDC users recreate; old PV kept ~1 week for
  rollback).
- **PR D — Authelia:** `auth-valkey` (valkey/valkey official image, ephemeral); config →
  `storage.postgres` + `session.redis`; ×2, PVC deleted; `identifiers export/import` preserves OIDC
  `sub` (Gitea account continuity); ADR 0012 amendment; ha-rules regex extension.
- **Platform repo (owner):** fix 4 `valkey-primary` URLs → `strive-pg instances: 3` → acceptance
  drain test (write-probe ≤ ~10 s gap on primary-node drain, 0 on replica-node drain) → valkey
  values tidy + bitnamilegacy exit follow-up.

## Acceptance (from #100)

Draining/rebooting any one node: strive DB + infra-pg fail over in seconds; Grafana/SSO/tunnel stay
up; every remaining singleton is on the ADR 0016 Tier-B accepted list; nothing sticks permanently on
node loss (out-of-service taint procedure bounds hard-loss recovery).
