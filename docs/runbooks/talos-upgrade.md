# Talos / Kubernetes upgrade runbook

How the cluster went from Talos v1.11.2 / Kubernetes v1.31.4 to **Talos v1.14.2 / Kubernetes v1.33.13**
(2026-10-08/09), and then to **Kubernetes v1.35.9** (2026-10-09), written so the next upgrade is routine.
Plans and review trails: `plans/2026-10-08-talos-upgrade-program-plan.md` (Talos, k8s 1.31 -> 1.33) and
`plans/2026-10-09-k8s-1.35-and-barman-plugin-plan.md` (k8s 1.33 -> 1.35, Cilium 1.19, Kyverno 1.19,
CNPG 1.30, and the Barman plugin migration in [cnpg-barman-plugin.md](cnpg-barman-plugin.md)).
Where the cluster can go next, and what blocks it: issue #1197 ("Kubernetes 1.36/1.37 readiness").

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

## Kubernetes minor hop (what worked, 1.33 -> 1.34 -> 1.35)

No drains and no reboots. The control plane and the kubelets are split so that every kubelet can be
gated on application health (`talosctl upgrade-k8s` alone patches all kubelets in one go, with no pause).

1. **Check the matrix first.** Every component must support the target minor:
   - Talos;
   - Cilium (1.18 stops at 1.33; 1.19 covers 1.32-1.35);
   - CNPG (1.27 stops at 1.33);
   - Kyverno;
   - KEDA;
   - QNAP CSI (the chart's `kubeVersion` is `<= 1.35`).

   Upgrade the addons first. Then run `pluto detect-helm` / `detect-api-resources` /
   `detect-files --target-versions k8s=v<target>` against the cluster and all three repos. The
   `apiserver_requested_deprecated_apis` metric only covers the time since the last apiserver restart.
2. **Use a kubectl within one minor of every apiserver** for the whole hop (1.34 for 1.33 -> 1.35).
   Take an etcd snapshot.
3. **Dry-run:** `_out/talosctl-1142.exe -n 192.168.0.41 upgrade-k8s --to <v> --upgrade-kubelet=false --dry-run`.
   Read the manifest diff; for 1.34 and 1.35 it was only CoreDNS gaining an os/arch nodeAffinity.
4. **Control plane:** the same command without `--dry-run`. It rolls apiserver, controller-manager and
   scheduler one CP at a time, then the bootstrap manifests, in about 5-6 min.
   - The VIP `.40` refuses for about 40 s while the VIP holder's apiserver restarts.
   - Every apiserver is at the target before any kubelet changes, so the kubelet is never newer than
     the apiserver.
5. **Trident re-reconciles after every apiserver minor.** The QNAP Trident operator recreates
   trident-controller and all 7 trident-node pods (~3 min after the apiserver roll, in both hops).
   - During startup it logs transient `portal value cannot be empty` / `secret trident-csi not found`
     errors. They are benign.
   - Wait for `TridentOrchestrator` `Installed` **and** 5 quiet minutes in each trident-node log before
     starting the kubelet roll. The roll's pre-gate stops on any trident error in the last 5 min, and
     it did stop once in the 1.35 hop until the window passed.
6. **Kubelets, one node at a time:** env-node-1, agent-node-1/2/3, then cp1, cp2, cp3. Each restart
   takes 7-19 s to Ready.
   - **Pre-gate:**
     - all CNPG clusters writable;
     - replicas streaming with lag at most 1 MiB, slots active;
     - node Ready;
     - no trident-node errors in the last 5 min.
   - **Patch:**
     `talosctl -n <ip> -e 192.168.0.41 patch mc --mode=no-reboot --dry-run -p '{"machine":{"kubelet":{"image":"ghcr.io/siderolabs/kubelet:v<target>"}}}'`.
     The dry-run diff must change exactly the kubelet image line; then run it without `--dry-run`.
     - Use a **strategic-merge** patch. Talos refuses JSON6902 patches on multi-document configs, which
       all CPs have.
     - When reading the live config, select the MachineConfig with id `v1alpha1`. The CPs also expose a
       `persistent` one.
   - **Post-gate:**
     - kubeletVersion is the target and the node is Ready;
     - no pod on the node restarted or stuck;
     - no checkpoint or state errors in the kubelet log, ignoring the benign memory-manager
       "state checkpoint" and device-manager "checkpoint is not found" startup lines;
     - the CNPG checks pass again.
   - When cp3 hosted all 5 CNPG primaries, its kubelet restart dipped `readyInstances` for ~15 s while
     readiness re-probed. There were no restarts and writes stayed OK.
   - This in-place restart without a drain was a deliberate choice: the kubelet checkpoint state is
     unused here (no CPU, memory or topology manager policy, no device plugins, no DRA claims). Re-check
     that on all 7 nodes before relying on it.
7. **Post-hop gates:**
   - nodes and the 9 CP static pods at the target;
   - etcd 3/3, and per-CP `/readyz` on each CP's own IP (the VIP masks a dead CP);
   - CNPG health;
   - Flux Ready;
   - the Kyverno allow/deny smoke plus the `trident-attacher-timeout` mutation smoke;
   - `cilium-dbg status` on 7/7;
   - a QNAP PVC smoke (write on cp1, delete the pod, read back on cp2, then confirm the PV and the
     TridentVolume are gone);
   - a kata smoke on the env node.

   Then bump tofu `kubernetes_version` (see below).

**Partial failure.** Never retry blindly after a client disconnect.
1. **Stop** further changes at the first:
   - CP component failure;
   - unhealthy etcd member;
   - CNPG write, streaming or slot failure;
   - storage error;
   - 10 min without progress.
2. **Cancel.** Interrupt the `talosctl` client: it drives every step. Record the last component it
   completed, from its output.
3. **Inventory:**
   - static pod images;
   - kubelet versions;
   - `talosctl get machinestatus`.
4. **Resume only when all of these hold:**
   - etcd 3/3;
   - every CP ready on its own IP;
   - all CNPG clusters writable and streaming;
   - storage clean;
   - valid skew.

   Then re-run the **same** `--to`, which skips the components already done, or continue the kubelet
   roll.
5. **Escalate** if the cluster isn't healthy within 30 min.

Talos etcd recovery (`talosctl bootstrap --recover-from=<snapshot>`) is an unrehearsed last resort that
needs a separate owner decision. It restores control-plane metadata only.

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
- **CNPG operator hops roll every instance, and the in-place-restart clusters cost minutes.** Switchover
  clusters pause writes for seconds. `primaryUpdateMethod: restart` clusters behave differently:
  - Affected clusters: infra-pg (Gitea, Authelia, ...) and trueswarm-admin-pg.
  - Each operator hop costs them **~3.5 min**: CNPG's smart-shutdown timeout (~3 min, new connections
    refused, existing sessions finish), then ~30 s down.
  - Gitea answers 401/500 during it.
  - Measured at every hop on 2026-10-09.
- **CNPG 1.29.1 runs its metrics exporter as `cnpg_metrics_exporter`** (`pg_monitor`), not the superuser
  (CVE-2026-44477).
  - The 1.29.1 hop created the role on 4 of 5 clusters. strive-pg had none: collection errors, and its
    metrics and alerts went blind.
  - The upstream-documented recovery, on the primary: `CREATE ROLE cnpg_metrics_exporter LOGIN NOSUPERUSER
    INHERIT; GRANT pg_monitor TO cnpg_metrics_exporter;`.
  - After every CNPG hop, gate on `cnpg_collector_last_collection_error == 0` for every instance.
  - Custom queries must be readable by `pg_monitor`. infra-pg's Gitea credential views already were.
- **The Flux `platform` GitRepository pulls the GitHub mirror** (`ssh://git@github.com/cchifor/platform`,
  10 min interval), which lags the Gitea merge. After a platform merge, re-annotate
  `reconcile.fluxcd.io/requestedAt` on the GitRepository until its artifact revision moves, and only then
  nudge the Kustomization (`platform-cnpg-operator`, ...). One hop sat on the pre-merge revision for ~5 min.
- **Toggling a label on a platform PR re-triggers `Preview Environment / preview`** and cancels the run in
  flight, which then shows as a failure. Re-run it with `POST .../actions/runs/<run>/jobs/<job>/rerun`,
  which goes to the back of the queue; it cost ~25 min once. To hold a pipelined PR, set
  `no-automerge` **before** its CI starts, not mid-run.
- **CI capacity sets the pace of PR-driven hops.** On 2026-10-09 the `self-hosted-hv` pool sat at 19/19
  busy on 80+ min jobs for hours, and one CNPG hop waited ~80 min for a single queued E2E job. Hops that
  need no CI (`upgrade-k8s`, the kubelet roll, Cilium) can run between PR hops when every component
  supports the target.
- **talosctl clients must follow the Talos minor everywhere**, not just on the workstation. cri-log-relay
  drove 1.14 nodes with talosctl 1.11.6 until the Renovate bound was moved (#1182). Bump the
  `ghcr.io/siderolabs/talosctl` allowedVersions and the image in the same change as a Talos upgrade, and
  do the same for the `alpine/k8s` kubectl Jobs (window 1.34 for the 1.33 -> 1.35 program).

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
  diffed against live (`talosctl get machineconfig`, the `v1alpha1` document) for **every** node.
- A node with `apply_mode = "staged"` (env-node-1) takes a tofu apply into its `persistent` config only.
  The active `v1alpha1` keeps the old values until that node's next reboot. That is expected, so don't
  re-apply.

## Addons in the same program

- Cilium (hand-installed Helm, values in `kubernetes/infra/bootstrap/cilium-values.yaml`; live 1.19.8,
  Helm rev 5). First run the pre-flight check: `helm template ... --set preflight.enabled=true --set
  agent=false --set operator.enabled=false` with `k8sServiceHost/Port` (KubePrism), wait for 7/7 plus
  "All CCNPs and CNPs valid!", then delete it. One minor per hop: `helm --kube-context admin@ai upgrade cilium cilium/cilium --version <v> -f <values> --set upgradeCompatibility=1.16` (the INITIALLY installed version, not the previous minor; see the header of cilium-values.yaml),
  then the agent/operator rollout, `cilium-dbg status` on every agent, and a DNS + service smoke.
- Kyverno / CNPG: platform PRs (`deploy/components/{kyverno,cnpg-operator}/helmrelease.yaml`), one
  minor per hop, chart-to-app mapping checked with `helm show chart`. Every CNPG hop rolls every
  instance (switchovers). Live: Kyverno v1.19.1 (chart 3.9.1) and CNPG 1.30.1 (chart 0.29.1).
  - **Kyverno hop gates:** an Enforce allow/deny smoke, plus a server-side dry-run of `trident-controller`
    asserting that the `trident-attacher-timeout` mutation still sets `--timeout=600s`.
  - ClusterPolicy/ClusterCleanupPolicy are deprecated in 1.19 and removed in 1.20. Migrate them to CEL
    types before Kyverno 1.20.
- plugin-barman-cloud (CNPG-I Barman Cloud, 0.8.1 / v0.15.1) runs next to the operator in `cnpg-system`
  (platform `deploy/components/cnpg-operator/plugin-barman-cloud.yaml`). See
  [cnpg-barman-plugin.md](cnpg-barman-plugin.md).
- QNAP CSI: the `qnap-csi` GitRepository tag plus `reconcileStrategy: Revision` (the chart version
  string does not change between tags).
