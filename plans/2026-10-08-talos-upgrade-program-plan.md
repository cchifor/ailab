# Talos 1.11.2 → 1.14.2 upgrade program (Kubernetes 1.31.4 → 1.33.13)

## Review decisions (round 1: Codex + Opus stand-in for Fable)

Fable was out of credits; the owner chose an Opus stand-in, which verified its claims against the live
cluster. Both reviewers said **do not execute unchanged**. Everything below is accepted and folded in.

**Corrections to the draft:**

- Upgrades use talosctl's `--image`, not `machine.install.image` (that is read only at first install).
- agent-node-3 runs the kata/gVisor schematic.
- bpg 0.116 `import_from` does not force a VM replacement.
- `talos_machine_secrets` replaces only when the version is LOWERED.
- Bumping `talos_version` also changes the config contract: a 1.14 contract fails validation and
  enables sandboxd. Pin the contract separately.
- AppArmor gates are gone in 1.33, while `SidecarContainers` stays (locked true).
- The QNAP tag bump needs `reconcileStrategy: Revision`.
- CNPG primary isolation needs BOTH the API and the peers to be lost.

**Reordering and new gates:**

- Order: QNAP + Cilium (all the way to 1.18.14) before Talos 1.12's kernel 6.18; Kyverno/CNPG after
  P3; Kubernetes after the add-ons; Talos 1.14 last, with a 24 h soak before the third CP.
- `upgrade-k8s` runs with exactly one `-n` and `--dry-run` first.
- Add etcd :2383 to the firewall before 1.14.
- Per-node install images come from each node's live schematic.
- Single-instance CNPG PDBs, CP capacity, CI-fleet and night-cronjob quiescing.
- Consistent backups for every Postgres cluster.
- Cause-specific recovery with deadlines.
- tofu hygiene: provider locks, the untracked `node-labels.tf`, a no-destroy gate.

**Round 2 (Codex + Opus stand-in).** Both signed off conditionally; all accepted:
- Pin the contract at exactly `v1.11.2`, the value already in state. `v1.11` means v1.11.0, which is
  lower, so the provider would replace the PKI.
- Talos never uncordons a node that kubectl cordoned, so there is an explicit uncordon step, and the
  strive-pg 3/3 gate comes after it.
- The headroom gate is runbook 1e (reserve on the RETURNING node) plus proof of survivor placement.
  trident-controller and CoreDNS are in the moved set.
- The etcd backup gate is a real restore rehearsal into an isolated etcd. pg dumps are per database
  plus globals, restore-tested.
- The quiet-window go/no-go gate is absolute and timed.
- Per-node schematic mapping in IaC.
- Tonight is cp1 then cp2 (cp3 waits for trueswarm-admin #152). P1 is a patch release, so the worker
  soak is exempt.

**Owner decisions (2026-10-08):**

- Delete the drill restores `trueswarm-recovery/restored-592a76d3` and `restored-671d5224`.
- Remove `admin-restore-application-20260926` from trueswarm-admin's qualification Kustomization
  (`chifor` merges).
- Round 2 reviewers: Codex and the Opus stand-in.
- First CP window tonight (P1) only if every P0 gate is green.

## Context

**Goal and scope.**

- The owner asked for the newest Talos: v1.14.2 (2026-09-29), via the full program.
- Talos 1.14 accepts Kubernetes ≥1.32.0 in code, but its support matrix starts at 1.33. We stop on
  Kubernetes 1.33.13. 1.33 is EOL, so it is a bounded endpoint with a dated exit plan
  (Follow-ups), not a long-term state.
- **Realistic duration:** about 20 windows over 4-6 weeks.

**State** (2026-10-08; refresh live before every phase):

- **Talos nodes.** 7 nodes on Talos v1.11.2 and Kubernetes v1.31.4, all platform nocloud and
  booted via SeaBIOS/GRUB:
  - CPs .41-.43 (VIP .40);
  - agent nodes .47-.49;
  - env node .37 (tofu env-pool, staged apply).
- **Schematics are per node:**
  - `53513e54…` (qemu-guest-agent, iscsi-tools, util-linux-tools): cp1-3, agent-node-1, agent-node-2.
  - `0839748e…` (adds kata-containers, gvisor): agent-node-3 and the env node.
  - **The real extension trap is talosctl's default `--image`.** For 1.11-1.13 clients it is the
    plain ghcr installer, with no extensions. The 1.14.2 client defaults to
    `metal-installer/376567…`: `customization: {}` and the wrong platform. Either default drops
    iscsi-tools, which breaks every Trident iSCSI PV.
  - `factory.talos.dev/nocloud-installer/<schematic>:<ver>` resolves for both schematics at all four
    targets. `ghcr installer:v1.14.2` returns 404.
- **Add-ons:**
  - Cilium 1.16.5 (helm rev 1, hand-installed from `kubernetes/infra/bootstrap/cilium-values.yaml`;
    cluster-name `default`; one cilium-operator replica, on the env node). Cilium 1.16 and 1.17 are
    EOL; 1.18 supports Kubernetes 1.30-1.33.
  - Kyverno 1.13.4 (chart 3.3.6) and CNPG 1.24.1 (chart 0.22.1), both owner-protected in
    cchifor/platform.
  - QNAP CSI v1.6.0. Its chart is 2.0.0 at both tags, and the HelmChart uses the ChartVersion
    strategy.
  - Flux 2.8.8 (documented floor: Kubernetes 1.33, so already below it).
- **Postgres:** 8 CNPG clusters. Three are single-instance drill restores, being removed per the owner
  decision.
- **CP capacity:** CP requests are at 77-83% CPU and memory. A drained CP's pods only half fit on the
  other two. cnpg-operator, the cert-manager and ESO webhooks (failurePolicy Fail) and Flux all sit on
  cp1, at default priority.
- **etcd:** 3.6.4, with 7 leader changes in 24 h on shared QLC disks. The 250/5000 timing is merged
  but not rolled.
- **Kyverno admission:** 2 replicas plus a PDB (platform #2137).
- **tofu:**
  - Every module requests talos `~> 0.12` and proxmox `~> 0.116`, but the ops checkout's locks are
    0.11.0 and 0.114.0.
  - The agent-nodes state contains label/taint resources from an UNTRACKED `node-labels.tf` in the
    ops checkout. A plan from a clean checkout would destroy them.
  - agent-nodes still uses `apply_mode` auto.
  - `var.talos_version` doubles as the config/secrets contract.

**Talos and Kubernetes facts:**

- **Talos path:** adjacent minors at their latest patch: 1.11.6 → 1.12.12 → 1.13.11 → 1.14.2.
  Talos 1.14 refuses nodes older than 1.12.
- **Drain behaviour:**
  - Nodes on ≤1.12 cordon and drain themselves on upgrade, but only for 5 min. Eviction errors are
    logged, then pods are stopped, so PDBs are not honoured beyond that.
  - The 1.13 client flow writes the new image BEFORE draining (PDB-respecting). A failed drain
    returns early: the node stays cordoned and is armed to boot the new version on its next reboot.
- **Kubernetes support:** Talos 1.13 supports 1.31-1.36; Talos 1.14 supports 1.33-1.37 (doc).
- **`upgrade-k8s`:**
  - one minor at a time;
  - needs exactly one `-n <cp>`;
  - with a 1.13 client it prunes bootstrap manifests.
- **Talos 1.12:**
  - kernel 6.18; early 1.12 kernels hit Cilium-triggered BPF verifier bugs (talos#12726,
    cilium#44216);
  - hardening: ptrace_scope 2, kptr_restrict 2, unprivileged userfaultfd 0;
  - kata extension 3.26.0 (3.32.0 in 1.13/1.14). The env node ships a verbatim 3.20.0
    `configuration.toml` (kata_debug).
- **Talos 1.14:**
  - etcd 3.7.1. A mixed 3.6/3.7 cluster stays on 3.6; once all members are upgraded, reverting needs
    the etcd downgrade workflow or a snapshot restore.
  - TLS 1.3 minimum for the etcd and kube-apiserver serving endpoints.
  - A new etcd gRPC-gateway listener on :2383 (our metrics stay on :2381; the firewall rule covers
    only 2379-2380).
  - A fresh 1.14-contract config emits `SecurityProfileConfig workloadIsolation: true`.
- **Add-on targets:**
  - Cilium 1.16.5 → 1.16.19 → 1.17.18 → 1.18.14;
  - QNAP v1.6.2 (chart bound ≤1.35);
  - Kyverno 1.14.5 → 1.15.3 → 1.16.4 (chart 3.6.4);
  - CNPG 1.25.4 → 1.26.3 → 1.27.4.

## Approach

**Global rules for every window:**

- **Tools:**
  - Always use full-path talosctl. The client must match the node's running version:
    `kubernetes/infra/_out/talosctl-<ver>.exe`, checksum-verified (1112, 1116, 11212, 11311, 1142).
  - Always pass `--talosconfig _out/talosconfig` and `kubectl --context admin@ai`.
  - Every Talos upgrade goes through `scripts/talos-upgrade-node.sh` (P0.6), never a hand-typed
    image.
- **CP windows** start after the 01:00Z talos-backup and the 02:00Z Velero backup are CONFIRMED
  finished, not at a fixed time. They leave 60 min of recovery reserve before CI ramps up (about
  06:00Z).
- **Quiesce first:**
  - pause new CI on all three hosts and wait for active jobs to finish (gitea runner daemons stopped
    gracefully);
  - suspend `e2e-runner-agentic-qwen` (03:17Z), `rclone-offsite` (04:00Z) and, on Sundays,
    `k6-weekly-soak` (02:00Z) and Velero's Sunday 03:00Z schedule, via their reconcilers, BEFORE
    their start times;
  - wait for any already-created Jobs or Backups to finish; suspension does not stop them;
  - resume all of them and confirm one successful run afterwards.
- **Pacing:**
  - one node at a time;
  - at most 2 CPs per night;
  - each Talos minor is a completed cluster phase with a worker canary and a ≥24 h soak before its CPs;
  - one component change per window;
  - never a CNPG hop and a CP reboot in the same window.
- **No tofu apply** of `infra`, `agent-nodes` or `env-pool` during the program, except the P0 guard
  changes and the per-phase version reconciliation (P8 rules). Every plan must show 0 destroy,
  0 replace and no unrelated diff.

**Per-node CP procedure:**

1. **Pre-checks:**
   - etcd 3/3, one leader and term, applied index caught up, no alarms;
   - per-member WAL p99 below 100 ms over the last 15 min;
   - API writes OK through the VIP and through every apiserver;
   - no critical alerts;
   - the backup checkpoint for this phase is taken.
2. **Leadership:** if the target leads, `forfeit-leadership` (sent to the target only). Then all
   members must agree that a caught-up member other than the target leads (prefer not cp1, whose
   disk stalls most). Re-check immediately before the drain. Stop if it is wrong. The wrapper
   enforces this too: it refuses a CP unless every other CP answers, etcd membership equals the CP
   list with no learners or errors, one leader that is not the target, one term, and applied indexes
   within 1000 entries.
3. **Workload moves:**
   - cordon the target;
   - move every CNPG primary off it with `kubectl cnpg promote <cluster> <caught-up replica>`
     (plugin v1.24.1, matching the operator), and verify the writable service and replication;
   - roll the platform controllers off the cordoned target with `kubectl rollout restart`, then
     verify each new pod is Ready on a survivor: cnpg-operator, the cert-manager and ESO webhooks,
     ESO cert-controller, the Flux controllers, trident-controller and CoreDNS;
   - kyverno admission must have a Ready replica on another node;
   - apply the headroom gate:
     - runbook 1e: the RETURNING node keeps ≥500m CPU and 2Gi free for its strive-pg instance, which
       is intentionally Pending while the node is cordoned (required anti-affinity);
     - separately, prove the Tier-A replicas and the moved controllers fit on the survivors,
       including affinity, taints and PV constraints;
     - the accepted Tier-B local-path singletons (dsh and text-embeddings on cp1, dsh-conductor on
       cp3) are listed as expected downtime;
   - pre-drain with `kubectl drain --ignore-daemonsets --delete-emptydir-data --timeout=15m`
     (PDB-respecting). A blocked eviction is diagnosed, never forced;
   - after the drain, re-check the Ready endpoints, the CNPG writable services and replication
     before running the wrapper.


4. **Upgrade:** `scripts/talos-upgrade-node.sh <ip> <ver>`. The wrapper:
   - reads the node's live schematic;
   - resolves `factory.talos.dev/nocloud-installer/<schematic>:<ver>` and verifies the manifest
     exists;
   - runs the matching client with `--wait`;
   - refuses if the schematic is unknown or the platform is not nocloud.
5. **Post-checks (gate):**
   - **Node:** boot ID changed; `talosctl version` = target; `get extensions` lists the schematic's
     extensions; iscsid and qemu-guest-agent healthy; Ready; no DiskPressure.
   - **Then uncordon explicitly** (`kubectl uncordon`). Talos on ≤1.12 never uncordons a node that
     kubectl cordoned; the 1.13 client uncordons by itself once the node is Ready, before these gates.
     Every check below runs after the uncordon.
   - **etcd:** 3/3, the flags as expected (`talosctl processes`).
   - **Postgres:**
     - infra-pg 2/2, strive-pg 3/3 and every trueswarm cluster streaming;
     - infra-pg slot `wal_status` reserved (cap 1 GB, no WAL archive: a replica left Pending loses
       its slot);
     - application writes OK (Gitea, Authelia).
   - **Platform:** kyverno admission 2/2; no pod stuck ContainerCreating on a volume; Cilium health
     OK.
   - Then 30 min with no new critical alerts, watched from outside the drained node.
6. **Recovery by cause, with deadlines** (measured on the canary, then fixed):
   - boot/etcd rejoin: 10 min;
   - volume/DB recovery: 15 min (allow for the documented ~8-minute Trident reattach).
   - Quorum loss, storage I/O errors or failed application writes: stop at once and repair forward.
     `talosctl rollback` only for a node that will not come up healthy on the new OS, using the
     client of the version it is running. Never after etcd 3.7 is cluster-wide.
   - Never restart a survivor, remove a member or force quorum while one CP is absent.

**Worker procedure:**

- Pause new work on the pool (agent/env tenants), wait for active jobs and leases, then pre-drain.
- Upgrade through the wrapper, then the same node gates.
- For agent-node-3 and the env node, also run the runtime tests (P2).
- agent-node-1 is the canary for each Talos target.

### P0. Preconditions (today; gates for tonight's P1)

1. **Drill restores out** (owner decision):
   - delete CNPG Clusters `trueswarm-recovery/restored-592a76d3` and `restored-671d5224` (their PVs
     are Retain; record the PV names for NAS cleanup);
   - open a trueswarm-admin PR removing `admin-restore-application-20260926` from the qualification
     Kustomization (`chifor` merges).
2. **Backups:**
   - an off-host `talosctl etcd snapshot`, plus confirming the latest `talos-backup` succeeded;
   - a RESTORE REHEARSAL: retrieve and age-decrypt the latest talos-backup object, plus the off-host
     snapshot. Restore one into an isolated data directory, start a matching etcd 3.6 that cannot
     reach the live cluster, and read representative keys (namespaces, a Secret, a CNPG Cluster);
   - a `pg_dump -Fc` of EVERY non-template database in every CNPG cluster, plus `pg_dumpall
     --globals-only` (the roles), to off-cluster storage. Each dump must pass `pg_restore -l`. Then a
     test-restore of the infra-pg and strive-pg sets (all strive databases) into a scratch Postgres
     17;
   - a Velero databases kopia repo verify;
   - an off-cluster copy of the Talos secrets bundle, the per-node machine configs and the tofu
     states.


3. **Baseline (24 h, recorded):** per-member WAL p99 and fsyncs over 1.024 s and over 4.096 s,
   backend commit, failed proposals, leader changes, host IO PSI, API write success, pods not Ready,
   Trident attach latency, Cilium drops.
   - **Go/no-go,** absolute and repeated before each CP:
     - 15 continuous quiesced minutes with ZERO new WAL fsyncs over 1.024 s on every member;
     - no unexpected leader changes and no failed proposals;
     - API writes OK;
     - missing or stale telemetry fails the gate.
     If the gate fails, the CP phases wait for the PLP-drive work (etcd-leader-churn plan step C).
     Afterwards, compare the busy soak with a comparable busy baseline.


4. **Capacity and placement:**
   - PriorityClass `platform-critical` on cnpg-operator, the cert-manager/ESO webhooks and the Flux
     controllers (ailab and platform HelmRelease values);
   - kyverno admission spread required across nodes;
   - an inventory of the accepted Tier-B local-path singletons as expected per-window downtime;
   - until PriorityClass lands (before P2), the P1 mitigation is the per-node controller roll-off in
     step 3 of the CP procedure.
5. **IaC guards** (one ailab PR, plan-verified, no apply except where stated):
   - A new `talos_config_contract = "v1.11.2"` variable (exactly the value in state) feeds
     `talos_machine_secrets` (plus
     `lifecycle.prevent_destroy`) and the `talos_machine_configuration` contract in all three modules.
     `talos_version` drives only the install image and the raw-image stage path.


   - Explicit `machine.install.image` from a committed PER-NODE schematic map (agent-node-3 and the
     env node use `0839748e…`, everything else `53513e54…`). The same map drives the raw-image
     import paths, which must be distinct per schematic. It is housekeeping for future installs and
     rebuilds; the wrapper is the upgrade guard.
   - This IaC PR may trail P1 (P1 applies nothing through tofu), but it must be merged and
     plan-verified before P2.


   - `ignore_changes` on the disk `import_from` (noise suppression) and `reboot_after_update = false`
     on the CP and agent VMs.
   - agent-nodes and env-pool apply modes explicit: agent-nodes `no_reboot`; env-pool stays `staged`.
   - Commit the untracked `node-labels.tf` (agent-node taints/labels) after coordinating with the
     session that owns the ops checkout.
   - Run `tofu init -upgrade` as its own reviewed step in all three modules. The locks move to
     talos 0.12.x and proxmox 0.116.x, and full plans are inspected: 0 destroy, 0 replace, the
     rendered machine config diffed against the live `get machineconfig`.
6. **`scripts/talos-upgrade-node.sh`** (with a test using stubbed talosctl and curl): it resolves the
   live schematic, the client binary and the image as described in step 4 of the CP procedure,
   with `--dry-run` support.

### P1. Talos 1.11.2 → 1.11.6, plus etcd election-timeout 5000

- **Workers:** the agent-node-1 canary, then agent-node-2, agent-node-3 (runtime tests) and the env
  node (today, after P0.6).
- 1.11.6 is a patch release (kernel 6.12.62, etcd 3.6.5), so it is explicitly EXEMPT from the
  worker-canary 24 h soak: workers today, CPs tonight.
- **CPs tonight** (cp1, then cp2), if the P0 tonight-gates are green: the wrapper, its test and
  dry-runs; the etcd snapshot, talos-backup and restore rehearsal; config and state copies; pg dumps
  with `pg_restore -l`; the baseline; fleet quiesce; the CNPG plugin; the agent-node-1 canary passed.
  1. Forfeit leadership so that cp3 leads.
  2. `talosctl patch machineconfig --mode=no-reboot` with election-timeout 5000 on the target.
     Confirm it is accepted and persisted (`get machineconfig`).
  3. Run the CP procedure. The reboot activates the new flags; verify with `processes`.
  4. cp2 starts only if cp1 has passed every gate by about 04:00Z.
  5. cp3 follows on a later night, after trueswarm-admin #152 is merged and its qualification
     Kustomization is resumed and pruned. Its drill DB has a 0-disruption PDB.
- **Soak** 24 h with the baseline comparison before P2.

### P4a. QNAP CSI and Cilium, before the 1.12 kernel (Kubernetes still 1.31)

1. **QNAP v1.6.2:**
   - set `spec.chart.spec.reconcileStrategy: Revision` on the `qnap-trident` HelmRelease and bump the
     GitRepository tag;
   - verify the artifact revision, then the operator, controller and node images at v1.6.2;
   - on disposable volumes: provision, write known data, attach, expand, move to another node and
     remount, snapshot/restore, delete;
   - keep the Trident attacher-timeout policy and the `iscsi-recovery-tmo` workaround until the new
     driver is shown to make them unnecessary.
2. **Cilium 1.16.5 → 1.16.19 → 1.17.18 → 1.18.14, one hop per window.** For each hop:
   - export the live user values and manifest; reconcile them with the repo values file and commit;
   - render and diff the target chart; set `upgradeCompatibility` to the previous minor; no
     `--reuse-values`;
   - run the pre-flight against KubePrism localhost:7445;
   - `helm upgrade --version <v>` and wait for the full agent/operator rollout;
   - `cilium status` and the connectivity test, Hubble, a policy allow/deny probe, and DNS;
   - record the Helm revision for rollback.
   - **Before 1.18,** check the identity-relevant label config (the 1.18 identity-churn warning only
     applies to custom label settings).
   - Record the pinned version in the values-file header and the runbook.

### P2. Talos → 1.12.12 and P3. Talos → 1.13.11

- Each is a full cluster phase: canary, workers, CPs (2 per night), then a 24 h soak.
- Keep the A/B recovery point: never start the next minor until the current one has soaked.
- **Env node (before P2):**
  - refresh the env-pool `kata_debug` `configuration.toml` against the target kata extension (3.26.0
    for 1.12.12, 3.32.0 for 1.13.11), or set `kata_debug=false`;
  - staged apply, then compare active against staged config before the reboot.
- **After each phase** on agent-node-3 and the env node: the `kata-env` handler, gVisor, nested KVM,
  PVC I/O, networking and sandbox teardown; one real AgentForge/env job; review the userspace OOM
  handler defaults.
- **P3:** the 1.12.12 client drives P3, so the legacy self-drain path still applies.

### P4b. Kyverno and CNPG (Kubernetes still 1.31; after P3)

- **Kyverno 1.13.4 → 1.14.5 → 1.15.3 → 1.16.4.** Platform PRs (owner merges), one hop per window:
  - record chart and app versions;
  - CRD upgrade behaviour;
  - preserve 2 replicas, the PDB and the spread;
  - set ValidatingAdmissionPolicy generation explicitly in 1.15;
  - after each hop: an allowed and a denied request, the existing mutations still applied (including
    `trident-attacher-timeout`), and the webhook endpoints and CA bundles.
- **CNPG 1.24.1 → 1.25.4 → 1.26.3 → 1.27.4.** Platform PRs (owner merges), one hop per window:
  - consider `ENABLE_INSTANCE_MANAGER_INPLACE_UPDATES=true` first, so the hops do not each roll all
    clusters (1.26 forces one restart anyway);
  - stagger the rollouts;
  - gate per cluster on replication lag, the infra-pg slot, service recovery and application writes;
  - for 1.27, validate peer instance-manager reachability (the network policies) before any later CP
    reboot.
- **Abort:** keep the prior chart, values and CRDs; a Helm or Git revert does not reverse CRD or
  database state.

### P5. Kubernetes 1.31.4 → 1.32.13 and P6. → 1.33.13 (client 1.13.11, quiet window each)

1. Review first: removed APIs (flowcontrol v1beta3 is gone in 1.32), admission policies, dormant
   CronJobs and restore manifests, and feature gates (no `SidecarContainers=false`, no AppArmor
   gates).
2. Take a backup checkpoint.
3. Run `talosctl-11311.exe -n <one healthy cp> upgrade-k8s --to <v> --with-docs=false
   --with-examples=false --dry-run`. Inspect the bootstrap manifest changes and pruning, then run it
   for real.
4. Verify:
   - every apiserver, the controller-manager and the scheduler;
   - CoreDNS, service routing and network-policy behaviour;
   - admission;
   - representative application transactions;
   - all kubelets at the version.
5. Commit the `kubernetes_version` bump in all three modules. Before any apply, diff the rendered
   config against live; for env-pool, make sure its staged config cannot restore an older kubelet.
6. **Partial failure:** record the components that completed, repair, and re-run the same target. No
   downgrade.
7. **P6 extra:** before it, confirm QNAP v1.6.2 and Kyverno/CNPG at their targets.

### P7. Talos 1.13.11 → 1.14.2 (client 1.13.11)

1. Before P7:
   - add 2383 to the etcd ingress `NetworkRuleConfig` (same source set as 2379-2380, CP-only) and
     black-box test it from a non-CP host after the first 1.14 CP;
   - keep sandboxd off (the config contract stays pinned, see P8).
2. Workers first. Then cp1 and the follower; soak ≥24 h (the etcd cluster version stays 3.6 while
   mixed); then the third CP (the point of no return).
3. After the first 1.14 CP:
   - positively verify TLS 1.3 client operations (kubectl, Flux, the Prometheus scrape of that
     apiserver) and the etcd member's health;
   - run a manual talos-backup job and decrypt its snapshot.
4. **If the 1.13 drain fails:** the node may already be armed to boot 1.14. Diagnose (PDB, finalizer,
   storage). Do not reboot it unplanned. Re-run the same upgrade once fixed; never `--force`.
5. After the third CP: confirm each member's etcd image/version and the cluster version.
6. Snapshot immediately before the third CP; the RPO is that snapshot.

### P8. Reconcile and record (after each phase, final at the end)

- Keep `talos_config_contract` at v1.11.2 (it never moves in this program; schema and default
  migration is in the dated follow-up). Bump `talos_version` and
  `kubernetes_version` to the achieved versions after each phase.
- Before any apply: 0 destroy/replace, and the rendered config diffed against live.
- env-pool stays `staged` and is activated at its announced window. Stage the final nocloud raw image
  per schematic on the Proxmox hosts, for future node creation.
- New `docs/runbooks/talos-upgrade.md`: the procedures above, the wrapper, the client matrix,
  recovery by cause, and the cronjob/CI quiescing list. Also update node-maintenance.md's client
  references.

### Follow-ups (owned, dated)

- **Kubernetes 1.34+** plan by 2026-11-15 (1.33 is EOL), together with CNPG 1.28 → 1.29/1.30,
  Cilium 1.19+, Kyverno ≥1.17 and the ClusterPolicy deprecation, Flux 2.9 and metrics-server 0.8.

## Critical files

- **ailab:**
  - `kubernetes/infra/{talos.tf,vms.tf,image.tf,variables.tf,versions.tf,machine-config/*.tftpl}`
  - `kubernetes/infra/agent-nodes/{main.tf,talos.tf,variables.tf,node-labels.tf}`
  - `kubernetes/infra/env-pool/*`
  - `kubernetes/infra/bootstrap/cilium-values.yaml`
  - `kubernetes/apps/infrastructure/{sources,storage}/qnap-csi.yaml`
  - the etcd firewall template
  - new `scripts/talos-upgrade-node.sh` and its test
  - new `docs/runbooks/talos-upgrade.md`
  - `docs/runbooks/node-maintenance.md`
- **cchifor/platform** (owner merges): the kyverno and cnpg-operator HelmReleases.
- **trueswarm-admin:** the qualification Kustomization.
- **Ops checkout:** `kubernetes/infra/_out/` (clients, talosconfig).

## Verification

- **Per Talos node:** the full gate in the CP procedure's post-checks. On agent-node-3 and the env
  node, add the runtime tests.
- **Per add-on hop:**
  - Cilium: connectivity, policy and DNS;
  - QNAP: the disposable-volume suite;
  - Kyverno: allow/deny and mutation;
  - CNPG: per-cluster lag, slot and writes.
- **Per Kubernetes minor:** the P5 verification list, and the next scheduled backup jobs succeed.
- **End state:**
  - 7 nodes on Talos v1.14.2, Kubernetes v1.33.13, etcd 3.7.x 3/3;
  - three clean tofu plans with the contract pinned;
  - the runbook merged;
  - an after-upgrade soak covering normal CI and a full backup cycle (snapshot decrypts, database
    dumps restore);
  - write availability and latency tails no worse than the P0 baseline (not just the
    election count).

<!-- codex-review-status: finalized -->