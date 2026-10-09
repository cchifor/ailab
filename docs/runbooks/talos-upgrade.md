# Talos / Kubernetes upgrade runbook

How the cluster went from Talos v1.11.2 / Kubernetes v1.31.4 to **Talos v1.14.2 / Kubernetes v1.33.13**
on 2026-10-08/09, written so the next upgrade is routine. The plan and its review trail are in
`plans/2026-10-08-talos-upgrade-program-plan.md` (and `-impl-review.md`).

## Rules that protect the cluster (never skip)

- **Every Talos upgrade goes through `scripts/talos-upgrade-node.sh <ip> <vX.Y.Z>`**, never a hand-typed
  image. talosctl's default `--image` drops the node's system extensions. Losing `iscsi-tools` breaks
  every Trident PV, and losing kata/gvisor breaks the env pool. The wrapper:
  - builds `factory.talos.dev/nocloud-installer/<the node's OWN live schematic>:<target>` and checks
    the factory manifest;
  - picks the `_out/talosctl-*.exe` whose `version --client` IS the running version;
  - allows only a patch step or the next minor;
  - for a control plane, requires every other CP to answer and etcd to be 3/3 healthy with the
    target NOT leading, and upgrades through a survivor endpoint.
- **Adjacent minors only**, for Talos and for Kubernetes (`talosctl upgrade-k8s`, one minor per run,
  exactly one `-n <cp>`). Check the support matrix first: Talos 1.14 supports Kubernetes 1.33+.
- **One control plane at a time.** Between CPs: etcd 3/3 (`talosctl etcd status`) and every CNPG
  cluster at `readyInstances == instances`.
- **If the target CP leads etcd**, forfeit first: `talosctl -n <cp> etcd forfeit-leadership`.
- **Never `--force`**, and never restart a survivor while a CP is absent.
- **Take an etcd snapshot** (`talosctl -n <survivor> etcd snapshot _out/etcd-snapshots/<name>.db`)
  before each phase. The last CP of a minor that moves etcd (1.14 -> etcd 3.7) is one-way, so its
  snapshot is the RPO.

## Per-node cycle (what worked)

1. `kubectl cordon <node>`. CNPG 1.24+ **switches a primary off a cordoned node by itself** (event
   `SwitchingOver`) within ~2 s.
2. **Wait until every CNPG cluster is full BEFORE draining.** On 2026-10-09, draining a CP while its
   demoted ex-primary was still rejoining left `strive-pg-11` with an unreadable PGDATA
   (`pg_controldata` exit 1). The fix was `kubectl cnpg destroy strive-pg 11` and a re-clone.
3. **2-instance CNPG clusters with only preferred anti-affinity** (`trueswarm-admin-pg`) can have
   BOTH instances on one CP. CNPG then cannot switch over and the primary PDB blocks the drain:
   delete the replica pod so it reschedules elsewhere, then CNPG switches over.
4. `kubectl drain --ignore-daemonsets --delete-emptydir-data --timeout=10m`. Add `--force` only if
   the owner accepts losing the controller-less airlock sandbox pods in `strive-sandboxes-ailab`.
   They are emptyDir-only and airlock marks them failed rather than recreating them. Talos's own
   drain evicts them anyway.
5. Run the wrapper. A `talosctl upgrade` exit 1 with `grpc: the client connection is closing` was
   **client-side** (cp3, 1.12->1.13): the node still upgraded. Check `talosctl version` /
   `get machinestatus` before any retry.
6. Gates: version, extensions plus the schematic (`get extensions`), Ready, then an **explicit
   `kubectl uncordon`** (Talos <= 1.12 never uncordons a kubectl-cordoned node), etcd 3/3, CNPG full.

Workers have no CNPG instances and can go in parallel. CPs never can.

## Things that bit us (and the fix)

- **Gitea is a single replica.** Every CP drain moves it, so PR CI checkouts during a drain fail
  with 502. Re-run CI after the drain (an empty commit, or `POST .../actions/runs/<run>/jobs/<job>/rerun`
  for a job whose final status was lost).
- **A platform owner ack is a merge.** The reviewer bots merge an approved, green PR as soon as the
  `approve-pin` lands. Ack only when no node operation is in flight. Hop 1 auto-merged during a cordon
  and stranded a CNPG replica.
- **Flux `GitRepository` with `ref.commit` does a FULL clone.** On the 287 MB QNAP-CSI repo that
  crash-looped source-controller (liveness kill, exit 137) and stalled every Flux source for ~45 min
  (#1170, fixed by #1172). Pin by tag.
- **CP capacity:** the three CPs must hold one CP's drain, so each is 10 vCPU / 32 GiB (#1169).
- **Talos 1.14** adds an etcd listener on **:2383**, a TLS 1.3 minimum, and etcd 3.7. The etcd
  ingress rule (`machine-config/controlplane.yaml.tftpl`) covers 2379-2380 **and 2383**. A
  strategic-merge patch of `NetworkRuleConfig.portSelector.ports` REPLACES the list, so always
  patch both ranges together.
- **env pool kata:** the `kata_debug` verbatim 3.20 `configuration.toml` does not match the newer kata
  extension. `kata_debug` defaults to false. Re-enable only with a config copied from the current
  extension.

## Tofu after an upgrade (never apply blindly)

- `talos_version` / `kubernetes_version` = what runs. Bump `talos_version` in **all three** modules (infra,
  agent-nodes, env-pool) in the same change as a Talos upgrade, and `kubernetes_version` right after each
  `upgrade-k8s` hop. `machine.install.image` (the reinstall/reset image) is derived from `talos_version` plus
  each node's schematic (`image.tf` for the CPs, `talos-schematics.yaml` for kata/gvisor), so a lagging
  variable would make a reset install an older Talos. A node whose extensions change needs its schematic
  updated in `talos-schematics.yaml` (or `agent-nodes/talos.tf` `install_schematics`) in the same change. `talos_config_contract` stays `v1.11.2` (the
  value in state; it feeds `talos_machine_secrets` and the rendered config). Moving it is its own
  reviewed migration.
- VM disks ignore `disk[0].import_from` (create-only), so a version bump is not a disk diff.
- Before any apply: the plan must show 0 destroy / 0 replace, and the rendered machine config must be
  diffed against live (`talosctl get machineconfig`).

## Addons in the same program

- Cilium (hand-installed Helm, values in `kubernetes/infra/bootstrap/cilium-values.yaml`). One minor
  per hop: `helm --kube-context admin@ai upgrade cilium cilium/cilium --version <v> -f <values> --set upgradeCompatibility=1.16` (the INITIALLY installed version, not the previous minor; see the header of cilium-values.yaml),
  then the agent/operator rollout, `cilium-dbg status` on every agent, and a DNS + service smoke.
- Kyverno / CNPG: platform PRs (`deploy/components/{kyverno,cnpg-operator}/helmrelease.yaml`), one
  minor per hop, chart-to-app mapping checked with `helm show chart`. Every CNPG hop rolls every
  instance (switchovers).
- QNAP CSI: the `qnap-csi` GitRepository tag plus `reconcileStrategy: Revision` (the chart version
  string does not change between tags).
