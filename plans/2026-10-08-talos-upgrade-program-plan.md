# Talos 1.11.2 → 1.14.2 upgrade program (Kubernetes 1.31.4 → 1.33.13)

## Codex Review

- The adjacent-minor Talos path, explicit factory images, separate env schematic, and one-node gates are sound; Kubernetes 1.33 before Talos 1.14 follows the published support matrix.
- **Do not execute unchanged.** The shared-disk etcd stalls require stronger pre-drain and survivor-health gates, and the blanket A/B rollback instruction is unsafe across an etcd cluster-version transition.
- Correct the Proxmox replacement claim, QNAP Flux chart reconciliation, feature-gate statement, CNPG isolation semantics, and final IaC apply procedure.
- Prefer P0 → P1 and stability soak → QNAP/Cilium preparation → P2/P3 → Kyverno/CNPG → P5/P6 → P7, with separate component windows and verified recovery points.
- Missing essentials include backup-window coordination, consistent backups for every database cluster, VIP/KubePrism failover checks, env runtime-config validation, and explicit abort/recovery deadlines; Kubernetes 1.33 remains a temporary EOL endpoint.

## Context

**Goal.** The owner asked (2026-10-08) for the newest Talos. That is v1.14.2, released 2026-09-29. The
owner chose the full program over stopping at 1.13.11, which is the newest release that still supports
Kubernetes 1.31. Control-plane (CP) reboots happen in CI-quiet night windows.

**Current state (verified 2026-10-08).**

<!-- codex: This review checked repository configuration and upstream documentation, not the live cluster or the platform repository; retain the dated inventory as prior evidence and refresh actual versions, schematics, pending configs, and workload placement before execution. -->

- **Nodes:** 7 Talos v1.11.2 nodes, all on platform `nocloud` and Kubernetes v1.31.4:
  - CPs: cp1/cp2/cp3, 192.168.0.41-43, VIP .40;
  - agent nodes: .47-.49 (tofu `infra/agent-nodes`);
  - env node: talos-env-node-1, .37 (tofu `infra/env-pool`, staged apply).
- **Image schematics:**
  - CPs and agent nodes run `53513e54…`: qemu-guest-agent, iscsi-tools, util-linux-tools.
  - The env node runs `0839748e…`: the same three plus kata-containers and gvisor.
- **The extension trap.** No template sets `machine.install.image`, so the config falls back to the
  provider default: today `ghcr.io/siderolabs/installer:v1.13.0` (no extensions). With provider 0.12
  it becomes `factory…/metal-installer/376567988…` (`customization: {}`, also none). An upgrade that
  relies on the config's image would DROP iscsi-tools, and every Trident iSCSI PV (CNPG included)
  would break.
  <!-- codex: The extension-loss risk is real, but `talosctl upgrade` defaults its image from the CLI version, not from `machine.install.image`; fixing the template does not replace explicitly passing the correct factory image on every upgrade. Provider-generated defaults also depend on the installed provider, so inspect rendered configuration rather than treating the quoted default as universal ([upgrade guide](https://docs.siderolabs.com/talos/v1.14/configure-your-talos-cluster/lifecycle-management/upgrading-talos)). -->
- **Add-ons:**
  - Cilium 1.16.5, hand-installed from `kubernetes/infra/bootstrap/cilium-values.yaml`, chart not
    pinned anywhere;
  - Kyverno 1.13.4 (chart 3.3.6) and CNPG operator 1.24.1 (chart 0.22.1), both owner-protected in
    cchifor/platform;
  - QNAP CSI/Trident v1.6.0 (ailab Flux GitRepository tag);
  - Flux 2.8.8, kube-prometheus-stack 86.2.3, cert-manager 1.20.2, ESO 2.7.0, Velero 1.18.1,
    KEDA 2.20.1, metrics-server 0.7.2, snapshot-controller 8.6.0, agent-sandbox 1.0.5.
- **etcd:** 3.6.4. Leader elections recur because the CP disks share consumer QLC NVMe with CI
  runners (plans/2026-10-07-etcd-leader-churn-plan.md). Its step B (election-timeout 5000) is merged
  but not yet rolled.
- **kyverno admission:** now 2 replicas plus a PDB (platform #2137, live 2026-10-08).

**Hard constraints from the research** (sources: siderolabs release and support-matrix pages, the
upgrade guide, `pkg/machinery/compatibility`, the component compatibility matrices).

- **Talos path.** Talos upgrades go through adjacent minors at their latest patch:
  1.11.6 → 1.12.12 → 1.13.11 → 1.14.2. Talos 1.14 refuses nodes older than 1.12.
  `ghcr.io/siderolabs/installer` is not published for 1.14, so always pass the factory installer.
  <!-- codex: Adjacent minors at their latest patches are the recommended tested path, not the exact installer enforcement boundary; retain this conservative sequence despite the installer accepting some skipped-minor upgrades ([supported paths](https://docs.siderolabs.com/talos/v1.13/configure-your-talos-cluster/lifecycle-management/upgrading-talos)). -->
- **Kubernetes support:** Talos 1.13 supports 1.31-1.36 and Talos 1.14 supports 1.33-1.37.
  Kubernetes moves one minor per `upgrade-k8s`: 1.31.4 → 1.32.13 → 1.33.13. Both 1.32 and 1.33 are
  EOL and are transit points only.
  <!-- codex: Distinguish published support from acceptance checks: the [1.14 matrix](https://docs.siderolabs.com/talos/v1.14/getting-started/support-matrix) starts at Kubernetes 1.33, while the [v1.14.2 compatibility constant](https://github.com/siderolabs/talos/blob/v1.14.2/pkg/machinery/compatibility/talos114/talos114.go) permits 1.32. Keep 1.33 before P7; there is no corresponding requirement to move Kubernetes before P2 or P3. -->
- **Add-ons blocking Kubernetes 1.32/1.33:**
  - Cilium 1.16 is tested on Kubernetes ≤1.30; 1.17.18 covers ≤1.32 and 1.18.14 covers 1.30-1.33.
    One minor at a time, with pre-flight.
  - Kyverno 1.13 supports ≤1.31; 1.16.4 (chart 3.6.4) supports 1.31-1.34.
  - CNPG 1.24 supports ≤1.31; 1.27.4 supports 1.31-1.33. Go in order 1.25 → 1.26 → 1.27; each hop
    rolls every Postgres cluster.
  - The QNAP v1.6.0 chart has `kubeVersion <= 1.32`, so the HelmRelease fails on 1.33. v1.6.2 has
    `<= 1.35`.
    <!-- codex: The chart bounds are confirmed, but crossing the bound does not itself stop an already-running driver; it blocks chart installation/upgrade when Helm checks compatibility. Complete and verify the actual driver upgrade before 1.33 rather than relying on an existing Ready HelmRelease ([v1.6.0 chart](https://github.com/qnap-dev/QNAP-CSI-PlugIn/blob/v1.6.0/Helm/trident/Chart.yaml), [v1.6.2 chart](https://github.com/qnap-dev/QNAP-CSI-PlugIn/blob/v1.6.2/Helm/trident/Chart.yaml)). -->
- **Version-specific changes:**
  - Talos 1.12: kernel 6.18 and stricter hardening sysctls.
  - Talos 1.13: talosctl performs the drain for nodes on ≥1.13; `--stage/--force/--preserve` are
    deprecated.
    <!-- codex: This describes the new lifecycle path when both client and serving node support it; P3 initiated with the 1.12 client still uses the legacy path. In the new path, installation happens before client-side draining and reboot, so a drain failure can leave new boot assets installed but not booted ([v1.13.11 implementation](https://github.com/siderolabs/talos/blob/v1.13.11/cmd/talosctl/cmd/talos/upgrade.go)). -->
  - Talos 1.14: etcd 3.7.1 (effectively one-way); TLS 1.3 minimum for etcd and kube-apiserver; etcd's
    default metrics port moves to 2383 (we pin `listen-metrics-urls :2381` explicitly).
    <!-- codex: Also account for 1.14 configuration-apply changes: the CLI removes reboot mode and applies configuration without automatically rebooting, while some changes still need an explicit reboot. Neither an accepted apply nor a no-reboot setting proves every requested change is active ([1.14 changes](https://docs.siderolabs.com/talos/v1.14/getting-started/what%27s-new-in-talos)). -->
- **tofu hazards:**
  - `var.talos_version` feeds the CP and agent VM disk `import_from`, and `ignore_changes` covers only
    `initialization`. A version bump plans a disk/VM REPLACE (env-pool already ignores
    `disk[0].import_from`).
    <!-- codex: The replacement claim is wrong for bpg 0.116: `import_from` has `ForceNew: false`, and existing disks are not re-imported. Keep the narrow ignore rule to suppress irrelevant drift, but inspect the actual provider-locked plan for unrelated disk changes and VM power cycles ([schema](https://github.com/bpg/terraform-provider-proxmox/blob/v0.116.0/proxmoxtf/resource/vm/disk/schema.go), [update implementation](https://github.com/bpg/terraform-provider-proxmox/blob/v0.116.0/proxmoxtf/resource/vm/disk/disk.go)). -->
  - `var.talos_version` also feeds `talos_machine_secrets`.
    <!-- codex: In provider 0.12, increasing this contract updates metadata without regenerating cluster secrets; decreasing it requests replacement, making a blanket variable revert during rollback dangerous. Preserve the existing PKI and decouple its generation contract from the OS version if needed ([provider implementation](https://github.com/siderolabs/terraform-provider-talos/blob/v0.12.0/pkg/talos/talos_machine_secrets_resource.go)). -->
  - `var.kubernetes_version` must follow each `upgrade-k8s`, or a later apply pushes 1.31.4 images
    back.
    <!-- codex: Changing either version variable regenerates desired machine configuration; it is not an imperative Talos/Kubernetes upgrade, and a later configuration apply can replace live image settings. Freeze unrelated applies during each transition and compare the complete rendered configuration with live and staged configuration before reconciliation. -->

## Approach

Every step is gated and one node at a time. Workers can go any time. CPs go 02:30-05:00Z: after the
01:00Z etcd snapshot and the 02:00Z Velero backup, before CI ramps up. Each phase must be green before
the next one starts; phases may span several nights.

<!-- codex: The safest practical adjustment is P0 → P1 plus a stability soak → P4's QNAP/Cilium steps → P2/P3 → P4's Kyverno/CNPG steps → P5/P6 → P7. This prepares networking/storage before new kernels while avoiding unnecessary CNPG operator rollouts before the earlier CP reboot rounds; keep each component change in its own window. -->
<!-- codex: “Workers can go any time” is unsafe for occupied agent/env pools and misleading on shared Proxmox disks: pause new work, finish active jobs/leases, and check host I/O before each worker upgrade. Use one ordinary worker as the first canary for each Talos target, and treat the env node as a separate runtime canary. -->
<!-- codex: 02:30 does not prove the 02:00 backup has finished, and the repository also schedules Sunday Velero at 03:00Z and off-site rclone at 04:00Z. Wait for backup/data-transfer completion, coordinate temporary schedule pauses through their reconciler, and reserve recovery time before the window ends; resume schedules and verify a successful run afterward. -->

### P0. Preconditions (day 1)

1. **talosctl clients.** Download the Windows binaries for v1.11.6, v1.12.12, v1.13.11 and v1.14.2
   from the GitHub release. Verify each against the release `sha256sum.txt`. Store them as
   `kubernetes/infra/_out/talosctl-<ver>.exe` (gitignored). The rule: use the client that matches the
   version the cluster is running.
   <!-- codex: Retain and verify the 1.11.2 binary required by P1, invoke every versioned binary by full path, and explicitly select this cluster's talosconfig and Kubernetes context. Match the serving node's version during mixed-version operation and switch to the new client for post-upgrade inspection; the runbook documents an unsafe system talosctl and a different default Kubernetes context. -->
2. **IaC guards** (ailab PR, merged and plan-verified before any tofu apply):
   - Explicit `machine.install.image =
     factory.talos.dev/nocloud-installer/<schematic>:<talos_version>` in the CP and worker templates,
     using `talos_image_factory_schematic.this.id` = 53513e54…. The env-pool template gets its own
     0839748e… schematic.
     <!-- codex: The nocloud installer name is appropriate; record full per-node schematic IDs, verify the selected platform, and resolve both schematics for every target version before a window. A schematic preserves extension selection, not extension versions or availability, and the worker modules need an explicit variable/output input rather than a reference to a resource in another module ([Image Factory](https://docs.siderolabs.com/talos/v1.13/learn-more/image-factory), [nocloud installer example](https://factory.talos.dev/?arch=amd64&cmdline-set=true&extensions=-&extensions=siderolabs%2Fiscsi-tools&extensions=siderolabs%2Futil-linux-tools&platform=nocloud&target=cloud&version=1.10.6)). -->
   - `lifecycle.ignore_changes` gains the disk `import_from` on the CP VMs (`vms.tf`) and the agent
     VMs (`agent-nodes/main.tf`).
   - `tofu plan` in all three modules must show NO replacement of `talos_machine_secrets`,
     `proxmox_virtual_environment_vm` or disks. The only change allowed is in-place config: install
     image, etcd election-timeout and `apply_mode`.
     <!-- codex: Resolve and record exact provider versions first: all modules request Talos `~> 0.12`, but this worktree only has the env-pool lockfile, so a provider change must not sneak into an operational apply. Back up the authoritative states and inspect protected rendered-config diffs, since sensitive-value placeholders can hide material changes. -->
     <!-- codex: Add explicit `no_reboot` for agent-node configuration applies and consider `reboot_after_update = false` on CP/agent VMs, matching the existing env VM guard. “In-place” and “no reboot” still permit disruptive service restarts, so reject unrelated hardware diffs and apply any service-affecting configuration to one node with health gates. -->
   - No module's machine config may carry `SidecarContainers` or `AppArmor` feature gates (they
     break 1.33).
     <!-- codex: This is partly wrong: `AppArmor` is absent in Kubernetes 1.33, but `SidecarContainers` remains registered and locked true at GA, so `SidecarContainers=true` is not itself a startup-breaking setting. Remove obsolete overrides after inspecting both generated and live configurations, and specifically reject `SidecarContainers=false` ([1.33.13 feature gates](https://github.com/kubernetes/kubernetes/blob/v1.33.13/pkg/features/kube_features.go)). -->
3. **Backups verified, not assumed:**
   - a fresh off-host `talosctl etcd snapshot`;
   - the latest `talos-backup` run succeeded;
   - the Velero `databases` kopia repo passes a verify (the 10-06 maintenance logged "failed to
     rewrite 14 contents");
   - a test-restore of the latest infra-pg dump into a scratch namespace;
   - the CNPG clusters (infra-pg, strive-pg, trueswarm*) are healthy and streaming.
   <!-- codex: This is insufficient for the database rollout: `velero/volume-policy.yaml` explicitly says strive/trueswarm still rely on filesystem copies of running PostgreSQL data, which are not a demonstrated consistent recovery point. Obtain and test a consistent logical or native backup for each affected cluster before its first disruption; restoring infra-pg alone does not cover them. -->
   <!-- codex: Verify an encrypted snapshot can actually be retrieved, decrypted, inspected, and restored in an isolated recovery rehearsal, with the existing Talos secrets bundle and required keys available off-cluster. Save per-node configurations, provider states, and previous installer references securely, and take a new recovery checkpoint before each Kubernetes minor and the etcd 3.7 transition. -->
4. **Baseline** for 24 h:
   - per-member WAL p99 and leader changes;
   - API error rate;
   - pods not Ready;
   - Trident volume attach latency;
   - Cilium drops and health.
   Record it in the PR.
   <!-- codex: An already-bad baseline is not a go criterion: also track multi-second fsync tails, backend commit latency, pending/failed proposals, host I/O pressure, and API write success during the actual quiet period. After P1, require stability under the intended load suppression before more CP rolls; if stalls persist while quiet, defer the program for the existing storage-remediation work. -->

### P1. Talos 1.11.2 → 1.11.6, plus etcd election-timeout 5000 (client 1.11.2)

- **Workers first (day):** agent-node-1..3, then the env node. For each:
  1. `talosctl upgrade -n <ip> --image factory.talos.dev/nocloud-installer/<schematic>:v1.11.6`.
     Talos below 1.13 cordons and drains itself.
     <!-- codex: Add explicit endpoint/context selection and `--wait`, then verify that the boot ID and running version changed before evaluating Ready; an asynchronous acknowledgement or a pre-existing Ready condition is not completion. Pre-check eviction blockers and use a completed, eviction-respecting pre-drain where necessary rather than treating the legacy shutdown drain as a guaranteed application-safety gate. -->
  2. Wait for Ready and uncordon. `talosctl get extensions` must list iscsi-tools (and kata/gvisor on
     the env node).
     <!-- codex: Talos normally uncordons after a successful legacy upgrade; investigate a remaining cordon before manually clearing it. Verify CNI, CSI and runtime services first so a node that is merely Ready does not immediately receive production work with broken mounts or sandbox handlers. -->
  3. Check the node's Trident iSCSI sessions and that its pods are Running.
- **CPs (night):**
  <!-- codex: Before each CP, inspect all PDBs and pod placement, ensure Kyverno replicas occupy different surviving failure domains, and verify spare schedulable capacity despite agent/env taints and local-path PV constraints. Cordon the target, move any CNPG primary to a caught-up survivor under operator control, and verify the writable service and replication before proceeding; do not bypass PDBs to meet the window. -->
  1. On each CP first, `talosctl patch machineconfig --mode=no-reboot` with election-timeout 5000;
     the upgrade's reboot activates it.
     <!-- codex: This matches the repository's observed etcd behavior, but `no-reboot` is not staged mode: it applies supported live configuration changes and rejects changes requiring reboot on these older servers. Confirm the patch is accepted and persisted, then verify the running etcd flags after reboot; a rejected patch will not be activated later. -->
  2. Order: cp1 first (cp2+cp3, the healthiest pair, hold quorum), then the follower, then the leader.
  3. For the leader: `forfeit-leadership` to the leader only, then all members must agree a caught-up
     member other than cp1 and other than the target leads. Stop if not.
     <!-- codex: Re-evaluate leadership immediately before every CP drain and reboot, not only the nominal final leader step; forfeiting leadership does not permanently exclude cp1 from future elections. If the observed leader or survivor health violates the rule, stop and reassess rather than repeatedly inducing elections. -->
  4. Stop node1's runner daemons (graceful drain) whenever cp1 is one of the two survivors.
     <!-- codex: Quiesce competing writes on both survivor hosts before draining the target, not only node1's runners: the repository records stalls on cp2 and cp3 too. Pausing new CI assignments is insufficient until active jobs and other heavy writers have finished, and stopping runners does not cure QLC device-internal stalls. -->
  5. **Gate per node** (node-maintenance.md):
     - etcd 3/3, one leader and term, applied index caught up, no alarms;
       <!-- codex: Require this before and after each CP and continuously watch both survivors while the target is absent; a three-member cluster with one member down has no remaining failure tolerance. If either survivor stalls or API writes fail, halt further changes and prioritize restoring the absent member, without restarting a survivor, removing members, or forcing quorum checks. -->
     - 10 min of successful API writes;
       <!-- codex: Check writes through VIP .40 and healthy individual API servers, verify VIP ownership/ARP convergence, and inspect KubePrism's healthy upstreams while each CP is absent. Cilium uses localhost:7445, but external administration uses the VIP; neither path alone proves the other survives, and Talos management endpoints should be CP addresses rather than the VIP. -->
     - nodes Ready and uncordoned;
     - infra-pg 2/2 and strive-pg 3/3 streaming on separate CPs;
       <!-- codex: Include every trueswarm cluster and application reconnection/write checks, not only operator status. Strive's required anti-affinity can leave its displaced instance Pending until the CP returns, so this is a post-return gate and must pass before the next CP; allow the documented roughly eight-minute storage recovery and preserve its CPU/memory headroom. -->
     - kyverno admission 2/2 Ready;
     - no pod stuck ContainerCreating on a volume;
     - `talosctl processes` shows `--election-timeout=5000`;
     - extensions present.
- **Abort and rollback:** `talosctl rollback -n <ip>` (A/B partition) if a node fails its gate.
  Never touch a second node while one is unhealthy.
  <!-- codex: A blanket rollback for any failed gate is unsafe: first distinguish drain failure, boot failure, config error, storage failure, and quorum loss, then select recovery for that cause. A/B retains only the immediately previous OS boot assets and does not restore Kubernetes or PV data; across minors require compatible saved configuration and etcd state, and use the client matching the node's actual running version. -->
  <!-- codex: Define explicit escalation deadlines before execution, such as a ten-minute boot/etcd rejoin budget and a fifteen-minute volume/database recovery budget, adjusted from canary measurements. Any quorum loss, storage corruption/I/O error, or failed application write is an immediate stop; exceeding a deadline means diagnosis and recovery, not automatic rollback or beginning the next node. -->

### P2. Talos → 1.12.12 (client 1.11.6) and P3. Talos → 1.13.11 (client 1.12.12)

- Same procedure, with the image tag at the target version.
  <!-- codex: Make P2 and P3 separate completed cluster phases with their own canary and soak; do not carry a single worker through both minors while the rest remain on 1.11. Retain a viable previous-version recovery point before overwriting the next A/B slot. -->
- **After P2:**
  - kernel 6.18;
  - hardening sysctls: verify Cilium health, Trident iSCSI and a kata RuntimeClass smoke pod on the
    env node (the warm pool is paused, so run one explicit test);
    <!-- codex: The env node's `kata_debug=true` installs a complete Kata 3.20.0 configuration copied from Talos 1.11.2, including runtime-specific paths/settings; rebase that file against each target extension before booting it, as `env-pool.md` requires. Test the actual `kata-env` handler, gVisor, nested KVM, PVC I/O, networking and sandbox teardown at every minor, since listing extensions or starting a generic pod does not validate this configuration. -->
  - the `.machine.network`/`registries` deprecation warnings are expected.
    <!-- codex: Keep working legacy configuration during the OS rollout; do not combine it with wholesale network/registry document migration. Compare active and persisted/staged env configuration before every reboot so unrelated pending file changes cannot enter the canary unnoticed. -->
- **After P3:** talosctl ≥1.13 performs the drain itself for later upgrades. The GRUB
  `extraKernelArgs` bug doesn't apply (none set).

### P4. Add-ons ready for Kubernetes 1.32 (Kubernetes still 1.31)

1. **QNAP CSI v1.6.0 → v1.6.2** (ailab Flux GitRepository tag).
   <!-- codex: Changing only the Git tag is insufficient: both tags declare chart version `2.0.0`, and the HelmRelease omits `spec.chart.spec.reconcileStrategy: Revision`. Set that strategy and verify the new Git artifact, Helm chart revision, operator and actual Trident controller/node images before proceeding ([Flux reconciliation rules](https://fluxcd.io/flux/components/source/helmcharts/#reconcile-strategy)). -->
   - Do it early: it adds a data-safety check before formatting, and 1.33 needs it.
   - Verify a new PVC provisions, attaches and expands.
     <!-- codex: Also write known data, remount it after a node move/reboot, and verify persistence, detach, snapshot/restore and cleanup on disposable volumes. Check existing volumes, storage-fabric routes, the Trident attacher-timeout policy, and `iscsi-recovery-tmo` behavior; retain the timeout workaround until the new driver is demonstrated to set correct integer values. -->
2. **Cilium 1.16.5 → 1.16.19 → 1.17.18.**
   - Manual `helm upgrade --version <v>` with the repo values. Pin the version in
     `kubernetes/infra/bootstrap/cilium-values.yaml`'s header and in a runbook.
     <!-- codex: Export the live release's user values and manifest, reconcile them with this file, and render/diff each target chart before upgrading; the file's claim of Flux day-2 ownership contradicts the stated manual installation. Preserve kube-proxy replacement, KubePrism localhost:7445 and Talos security/cgroup settings, use the documented `upgradeCompatibility` setting, and avoid `--reuse-values` across minors ([Cilium upgrade guide](https://docs.cilium.io/en/v1.17/operations/upgrade/)). -->
   - Run the Cilium pre-flight first. One minor per quiet window.
     <!-- codex: Make preflight use the same KubePrism API endpoint because kube-proxy is disabled, and wait for the entire agent/operator rollout before the next change. Record the previous Helm revision and supported adjacent-version rollback procedure; a header comment alone does not enforce the executable chart version. -->
   - Verify `cilium status`/connectivity, Hubble, and no drop spike.
   - Check that `cluster.name` meets 1.17's ≤32 chars of `[a-z0-9-]`.
3. **Kyverno 1.13.4 → 1.14.5 → 1.15.3 → 1.16.4.**
   - Platform PRs (owner merges), one hop each. Verify admission and webhook health after every hop.
     <!-- codex: Record chart versions separately from application versions for every Kyverno and CNPG hop, including how the platform HelmRelease upgrades CRDs. Preserve admission replicas/PDB/spread settings, test both allowed and denied requests plus existing mutation policies, and verify webhook service endpoints and CA bundles before any Kubernetes change. -->
   - 1.15 turns on ValidatingAdmissionPolicy generation by default. Set it explicitly to the current
     behaviour.
4. **CNPG 1.24.1 → 1.25.4 → 1.26.3 → 1.27.4.**
   - Platform PRs (owner merges), one hop per window. Each hop rolls and switches over every cluster;
     watch infra-pg (Gitea/Authelia) closely.
     <!-- codex: Sequential upgrades are the recommended conservative route, but “each hop switches over every cluster” depends on live instance-manager and primary-update settings; 1.26 specifically forces a restart even with in-place updates enabled. Inventory those settings and await actual rollout completion, including supervised actions, without adding gratuitous switchovers solely to satisfy the checklist ([CNPG upgrade behavior](https://cloudnative-pg.io/docs/1.27/installation_upgrade/)). -->
     <!-- codex: CNPG can roll different clusters concurrently, multiplying QNAP attachment and API load even though each individual cluster rolls serially. Use controlled rollout delays where needed, preserve PostgreSQL major versions, and gate on replication lag, read/write service recovery and application reconnection for every cluster. -->
   - 1.27's liveness isolation check can shut down a primary that loses the API server. Do not run a
     CNPG hop and a CP reboot in the same window.
     <!-- codex: The isolation description is incomplete: failure requires losing both the Kubernetes API and every peer instance-manager REST endpoint, not API loss alone. Validate peer reachability/network policy and appropriate probe timeouts before the later P7 reboots, and do not disable this split-brain protection merely because etcd has churn ([CNPG primary isolation](https://github.com/cloudnative-pg/cloudnative-pg/blob/release-1.27/docs/src/instance_manager.md)). -->
   <!-- codex: Define add-on-specific abort/recovery actions before these hops: preserve prior charts, values and CRDs, halt further upgrades on failed admission/network/storage/database checks, and coordinate any Git revert with Flux. Helm rollback does not automatically reverse CRD schema changes or database state. -->

### P5. Kubernetes 1.31.4 → 1.32.13 (client 1.13.11)

- Run `talosctl upgrade-k8s --to 1.32.13 --with-docs=false --with-examples=false`.
  <!-- codex: As written this may fail because the generated talosconfig selects all three CP nodes: `upgrade-k8s` requires exactly one `-n <healthy-cp-ip>` and upgrades the whole cluster, not just that node. Specify the versioned executable and talosconfig/endpoints, run the same command with `--dry-run` first, and retain default image pre-pulling; the two comment-generation flags are valid ([v1.13.11 command](https://github.com/siderolabs/talos/blob/v1.13.11/cmd/talosctl/cmd/talos/upgrade-k8s.go)). -->
  <!-- codex: This patches control-plane components and all kubelets without a Talos OS reboot or a PDB-protected node drain, and kubelet updates can still disrupt workloads; schedule it in a quiet window with all CPs healthy. Inspect bootstrap manifest changes and 1.13 inventory pruning, preserve disabled kube-proxy/CNI-none, and verify CoreDNS afterward ([Kubernetes upgrade behavior](https://docs.siderolabs.com/kubernetes-guides/advanced-guides/upgrading-kubernetes)). -->
  <!-- codex: Review removed APIs, admission policies, client compatibility and feature gates before the dry run, including infrequently used CronJobs and restore manifests. A successful dry run and post-upgrade deprecated-API metric cannot prove those dormant consumers are compatible. -->
- Then bump `kubernetes_version` in `infra`, `infra/agent-nodes` and `infra/env-pool`. Each
  `tofu plan` must show config-only changes.
  <!-- codex: Commit the successful version promptly, but do not blindly apply the regenerated full configs: compare every component image and retained customization with the live result. For the staged env module, ensure its next boot cannot restore an older kubelet/configuration; keep unrelated applies frozen until live, persisted and desired versions agree. -->
- Verify: all control-plane static pods are on 1.32.13, every kubelet reports 1.32.13, and no
  deprecated-API errors.
  <!-- codex: Add a checkpoint for partial failure: record completed components, diagnose the blocker, and resume the same target upgrade only after health recovers. Kubernetes downgrade is not the routine abort path; never attempt it by lowering tofu variables or by Talos A/B rollback, and document full-cluster recovery plus its data-loss boundary if forward repair fails. -->

### P6. Add-ons for 1.33, then Kubernetes 1.32.13 → 1.33.13

1. Cilium 1.17.18 → 1.18.14. The new service-account identity label causes identity churn: quiet
   window, and watch drops.
   <!-- codex: The documented identity-churn warning is conditional on custom identity-relevant label configuration; inspect live values before asserting it applies or changing label selection. Reuse the full preflight, values review, connectivity tests and rollback preparation from the earlier hop ([1.18 upgrade notes](https://docs.cilium.io/en/v1.18/operations/upgrade/)). -->
2. Run `upgrade-k8s --to 1.33.13` (same flags) and bump the tofu `kubernetes_version` again.
   <!-- codex: Require a completed Cilium rollout and soak before this command, then repeat all P5 backup, dry-run, single-CP targeting, reconciliation and failure-handling steps. Verify actual QNAP 1.6.2 and compatible Kyverno/CNPG versions before crossing the Kubernetes minor boundary. -->

### P7. Talos 1.13.11 → 1.14.2 (client 1.13.11)

- Take a fresh off-host etcd snapshot right before the first CP. etcd 3.7.1 is one-way once the
  cluster version moves.
  <!-- codex: “One-way” is an operational precaution, not an absolute etcd limitation: a mixed 3.6/3.7 cluster remains on the older protocol, while a fully upgraded cluster needs the supported downgrade workflow or a pre-upgrade snapshot restore. Do not use plain Talos rollback after the cluster version advances; establish a stop checkpoint before the final CP and a rehearsed Talos-compatible recovery procedure with an explicit RPO ([etcd 3.7 upgrade/rollback rules](https://etcd.io/docs/v3.7/upgrades/upgrade_3_7/)). -->
  <!-- codex: Confirm each member's actual etcd image/version and cluster version rather than assuming the Talos tag alone upgrades etcd; an explicit machine-config image override can retain an older version. Never restore an old snapshot or VM disk into one member of the still-running production quorum as an attempted cluster rollback. -->
- Verify the TLS 1.3 minimum before the first CP: talos-backup, the Prometheus apiserver/kubelet
  scrape, Flux and the ESO webhooks all negotiate TLS 1.3. Check with `openssl s_client -tls1_2`
  against a 1.14 node.
  <!-- codex: This test is insufficient and mistimed: a 1.14 worker has no kube-apiserver, and TLS 1.2 rejection alone proves neither TLS 1.3 success nor application compatibility. Test on an isolated 1.14 CP beforehand, then positively verify authenticated TLS 1.3 and real client operations against the first upgraded production CP before proceeding. -->
  <!-- codex: Test the correct connection directions: the changed minimum covers etcd and kube-apiserver serving endpoints, whereas ESO webhook serving TLS and kubelet scraping are separate paths. Talos-backup uses the Talos API, so prove its pinned beta image can perform and upload a snapshot after the upgrade instead of treating an OpenSSL probe as validation. -->
  <!-- codex: Retaining metrics on :2381 does not remove the new :2383 HTTP/gateway listener; the current CP firewall contains only 2379-2380. Inspect the bound listener and extend the existing source restrictions to 2383 where reachable, then verify restrictions from a non-CP host and confirm metrics still scrape on 2381 ([1.14 etcd changes](https://docs.siderolabs.com/talos/v1.14/getting-started/what%27s-new-in-talos)). -->
- Same per-node gates. talosctl ≥1.13 drains (`--drain` default, 5 m timeout). If a drain fails, the
  node may be left cordoned: re-run, never use `--force`.
  <!-- codex: Diagnose failed eviction/PDB, finalizer, storage or runtime shutdown before retrying, and allow a deliberate timeout suited to this cluster's measured attach/switchover times. Because new boot assets may already be installed, inspect running versus pending upgrade state and avoid an unrelated reboot; keep the client process and API connectivity alive through drain, reboot and uncordon. -->
- Keep workload isolation (sandboxd) off.
  <!-- codex: An OS upgrade preserves the absent/disabled setting, but generating a fresh 1.14 configuration can introduce `SecurityProfileConfig` with isolation enabled. Explicitly preserve the intended setting during P8 and defer enabling isolation to a separate CSI/Kata-tested change ([1.14 upgrade configuration changes](https://docs.siderolabs.com/talos/v1.14/configure-your-talos-cluster/lifecycle-management/upgrading-talos)). -->

### P8. Reconcile IaC and record

- `talos_version = "v1.14.2"` in all three modules; the import_from guards prevent VM replacement.
  <!-- codex: Reconcile version intent at each completed phase, or explicitly maintain an apply freeze until this point; leaving desired state on 1.11 through a multi-night program makes an unrelated apply hazardous. For future node creation, stage and verify the final nocloud raw images with the correct per-pool schematic on the Proxmox hosts, since ignoring existing import paths does not supply a new-node image. -->
- `tofu plan` is a no-op apart from expected attributes. Apply with `apply_mode = "no_reboot"`.
  <!-- codex: A clean plan does not establish live convergence: provider 0.12's configuration-apply `Read` does not refresh machine configuration from the node. Compare live and persisted/staged configurations against the complete rendered result, including Kubernetes/etcd images, CNI/proxy settings, network routes, new document kinds and defaults, before any apply ([provider implementation](https://github.com/siderolabs/terraform-provider-talos/blob/v0.12.0/pkg/talos/talos_machine_configuration_apply_resource.go)). -->
  <!-- codex: Applying `no_reboot` indiscriminately is wrong for the env pool's established staged-only contract and can still restart services elsewhere. Preserve staged mode for env changes, reconcile active configuration deliberately at its announced window, and handle any service-affecting CP changes serially; document all remaining activation requirements instead of calling the result complete. -->
- New `docs/runbooks/talos-upgrade.md`: the per-node procedure, gates, client-matching, install-image
  rule and rollback.
- Follow-ups out of scope:
  - Kubernetes 1.34+ (1.33 is EOL);
    <!-- codex: Calling 1.33 a transit point conflicts with leaving the next supported release indefinitely out of scope. Preserve this program's bounded target, but assign an owner and dated exit plan for the EOL Kubernetes/add-on endpoint rather than presenting it as a supported long-term production state. -->
  - CNPG 1.28 → 1.29/1.30, Cilium 1.19+, Kyverno ≥1.17 and the ClusterPolicy deprecation;
  - Flux 2.9 (needs 1.34), metrics-server 0.8.

## Critical files

- ailab:
  - `kubernetes/infra/{vms.tf,talos.tf,image.tf,variables.tf,machine-config/controlplane.yaml.tftpl}`
  - `kubernetes/infra/agent-nodes/{main.tf,talos.tf,variables.tf}`
  - `kubernetes/infra/env-pool/*`
  - `kubernetes/infra/bootstrap/cilium-values.yaml`
  - `kubernetes/apps/infrastructure/sources/qnap-csi.yaml`
  - new `docs/runbooks/talos-upgrade.md`
  <!-- codex: Also include the agent worker template, provider constraints/lockfiles, `storage/qnap-csi.yaml` for Revision reconciliation, and the backup schedules/recovery evidence in the implementation review. Update stale maintenance commands to select the appropriate versioned client without copying unrelated Proxmox host-reboot or AI-LXC maintenance into this Talos-only program. -->
- cchifor/platform (owner merges):
  - `deploy/components/kyverno/helmrelease.yaml`
  - `deploy/components/cnpg-operator/helmrelease.yaml`
- Ops checkout `kubernetes/infra/_out/`: talosctl binaries and talosconfig (not in git).

## Verification

- **Per node, every Talos step:**
  - `talosctl version` shows the target;
  - `get extensions` shows iscsi-tools (and kata/gvisor on the env node);
    <!-- codex: Record full schematic and extension versions and verify iscsid/qemu-guest-agent service health, actual PVC read/write/remount behavior, and runtime teardown on env nodes. Check boot logs, storage/network errors, free EPHEMERAL space and absence of DiskPressure; extension registration alone does not demonstrate functionality. -->
  - the full gate above;
  - no new critical alerts for 30 min.
    <!-- codex: Monitor API and application writes from outside the node being drained because Prometheus/Alertmanager can themselves move or lose state during these CP drains. Budget the thirty-minute observation plus recovery reserve into each window, with known singleton downtime distinguished from unexpected Tier-A failures. -->
- **Per add-on hop:** the component's own health (Cilium connectivity test, Kyverno admission of a
  test object, CNPG cluster healthy + streaming + a switchover observed, QNAP PVC
  provision/attach/expand).
- **Per Kubernetes step:** all nodes at the version; `kubectl get --raw /readyz?verbose`;
  `apiserver_requested_deprecated_apis` reviewed; the Flux Kustomizations and HelmReleases all Ready.
  <!-- codex: Check every API-server endpoint, controller-manager/scheduler health, DNS, service routing, network-policy allow/deny behavior, admission, and representative application transactions after each minor. Verify the next scheduled jobs and backup/restore path too, since node version and Ready conditions miss failures in dormant workloads. -->
- **End state:**
  - 7 nodes on Talos v1.14.2 and Kubernetes v1.33.13;
  - etcd 3.7.x, 3/3;
  - all three tofu plans clean;
  - runbook merged;
  - leader-change rate no worse than the P0 baseline.
    <!-- codex: Require an after-upgrade soak covering normal CI and a complete backup cycle, with successful snapshots, decryptability, database backups and no unexplained API/storage failures. Compare latency tails and write availability as well as election counts: increasing the election timeout can hide elections while leaving the underlying disk stalls and degraded availability unchanged. -->

<!-- codex-review-status: complete -->