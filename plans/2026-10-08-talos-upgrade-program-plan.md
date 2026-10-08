# Talos 1.11.2 → 1.14.2 upgrade program (Kubernetes 1.31.4 → 1.33.13)

## Context

**Goal.** The owner asked (2026-10-08) for the newest Talos. That is v1.14.2, released 2026-09-29. The
owner chose the full program over stopping at 1.13.11, which is the newest release that still supports
Kubernetes 1.31. Control-plane (CP) reboots happen in CI-quiet night windows.

**Current state (verified 2026-10-08).**

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
- **Kubernetes support:** Talos 1.13 supports 1.31-1.36 and Talos 1.14 supports 1.33-1.37.
  Kubernetes moves one minor per `upgrade-k8s`: 1.31.4 → 1.32.13 → 1.33.13. Both 1.32 and 1.33 are
  EOL and are transit points only.
- **Add-ons blocking Kubernetes 1.32/1.33:**
  - Cilium 1.16 is tested on Kubernetes ≤1.30; 1.17.18 covers ≤1.32 and 1.18.14 covers 1.30-1.33.
    One minor at a time, with pre-flight.
  - Kyverno 1.13 supports ≤1.31; 1.16.4 (chart 3.6.4) supports 1.31-1.34.
  - CNPG 1.24 supports ≤1.31; 1.27.4 supports 1.31-1.33. Go in order 1.25 → 1.26 → 1.27; each hop
    rolls every Postgres cluster.
  - The QNAP v1.6.0 chart has `kubeVersion <= 1.32`, so the HelmRelease fails on 1.33. v1.6.2 has
    `<= 1.35`.
- **Version-specific changes:**
  - Talos 1.12: kernel 6.18 and stricter hardening sysctls.
  - Talos 1.13: talosctl performs the drain for nodes on ≥1.13; `--stage/--force/--preserve` are
    deprecated.
  - Talos 1.14: etcd 3.7.1 (effectively one-way); TLS 1.3 minimum for etcd and kube-apiserver; etcd's
    default metrics port moves to 2383 (we pin `listen-metrics-urls :2381` explicitly).
- **tofu hazards:**
  - `var.talos_version` feeds the CP and agent VM disk `import_from`, and `ignore_changes` covers only
    `initialization`. A version bump plans a disk/VM REPLACE (env-pool already ignores
    `disk[0].import_from`).
  - `var.talos_version` also feeds `talos_machine_secrets`.
  - `var.kubernetes_version` must follow each `upgrade-k8s`, or a later apply pushes 1.31.4 images
    back.

## Approach

Every step is gated and one node at a time. Workers can go any time. CPs go 02:30-05:00Z: after the
01:00Z etcd snapshot and the 02:00Z Velero backup, before CI ramps up. Each phase must be green before
the next one starts; phases may span several nights.

### P0. Preconditions (day 1)

1. **talosctl clients.** Download the Windows binaries for v1.11.6, v1.12.12, v1.13.11 and v1.14.2
   from the GitHub release. Verify each against the release `sha256sum.txt`. Store them as
   `kubernetes/infra/_out/talosctl-<ver>.exe` (gitignored). The rule: use the client that matches the
   version the cluster is running.
2. **IaC guards** (ailab PR, merged and plan-verified before any tofu apply):
   - Explicit `machine.install.image =
     factory.talos.dev/nocloud-installer/<schematic>:<talos_version>` in the CP and worker templates,
     using `talos_image_factory_schematic.this.id` = 53513e54…. The env-pool template gets its own
     0839748e… schematic.
   - `lifecycle.ignore_changes` gains the disk `import_from` on the CP VMs (`vms.tf`) and the agent
     VMs (`agent-nodes/main.tf`).
   - `tofu plan` in all three modules must show NO replacement of `talos_machine_secrets`,
     `proxmox_virtual_environment_vm` or disks. The only change allowed is in-place config: install
     image, etcd election-timeout and `apply_mode`.
   - No module's machine config may carry `SidecarContainers` or `AppArmor` feature gates (they
     break 1.33).
3. **Backups verified, not assumed:**
   - a fresh off-host `talosctl etcd snapshot`;
   - the latest `talos-backup` run succeeded;
   - the Velero `databases` kopia repo passes a verify (the 10-06 maintenance logged "failed to
     rewrite 14 contents");
   - a test-restore of the latest infra-pg dump into a scratch namespace;
   - the CNPG clusters (infra-pg, strive-pg, trueswarm*) are healthy and streaming.
4. **Baseline** for 24 h:
   - per-member WAL p99 and leader changes;
   - API error rate;
   - pods not Ready;
   - Trident volume attach latency;
   - Cilium drops and health.
   Record it in the PR.

### P1. Talos 1.11.2 → 1.11.6, plus etcd election-timeout 5000 (client 1.11.2)

- **Workers first (day):** agent-node-1..3, then the env node. For each:
  1. `talosctl upgrade -n <ip> --image factory.talos.dev/nocloud-installer/<schematic>:v1.11.6`.
     Talos below 1.13 cordons and drains itself.
  2. Wait for Ready and uncordon. `talosctl get extensions` must list iscsi-tools (and kata/gvisor on
     the env node).
  3. Check the node's Trident iSCSI sessions and that its pods are Running.
- **CPs (night):**
  1. On each CP first, `talosctl patch machineconfig --mode=no-reboot` with election-timeout 5000;
     the upgrade's reboot activates it.
  2. Order: cp1 first (cp2+cp3, the healthiest pair, hold quorum), then the follower, then the leader.
  3. For the leader: `forfeit-leadership` to the leader only, then all members must agree a caught-up
     member other than cp1 and other than the target leads. Stop if not.
  4. Stop node1's runner daemons (graceful drain) whenever cp1 is one of the two survivors.
  5. **Gate per node** (node-maintenance.md):
     - etcd 3/3, one leader and term, applied index caught up, no alarms;
     - 10 min of successful API writes;
     - nodes Ready and uncordoned;
     - infra-pg 2/2 and strive-pg 3/3 streaming on separate CPs;
     - kyverno admission 2/2 Ready;
     - no pod stuck ContainerCreating on a volume;
     - `talosctl processes` shows `--election-timeout=5000`;
     - extensions present.
- **Abort and rollback:** `talosctl rollback -n <ip>` (A/B partition) if a node fails its gate.
  Never touch a second node while one is unhealthy.

### P2. Talos → 1.12.12 (client 1.11.6) and P3. Talos → 1.13.11 (client 1.12.12)

- Same procedure, with the image tag at the target version.
- **After P2:**
  - kernel 6.18;
  - hardening sysctls: verify Cilium health, Trident iSCSI and a kata RuntimeClass smoke pod on the
    env node (the warm pool is paused, so run one explicit test);
  - the `.machine.network`/`registries` deprecation warnings are expected.
- **After P3:** talosctl ≥1.13 performs the drain itself for later upgrades. The GRUB
  `extraKernelArgs` bug doesn't apply (none set).

### P4. Add-ons ready for Kubernetes 1.32 (Kubernetes still 1.31)

1. **QNAP CSI v1.6.0 → v1.6.2** (ailab Flux GitRepository tag).
   - Do it early: it adds a data-safety check before formatting, and 1.33 needs it.
   - Verify a new PVC provisions, attaches and expands.
2. **Cilium 1.16.5 → 1.16.19 → 1.17.18.**
   - Manual `helm upgrade --version <v>` with the repo values. Pin the version in
     `kubernetes/infra/bootstrap/cilium-values.yaml`'s header and in a runbook.
   - Run the Cilium pre-flight first. One minor per quiet window.
   - Verify `cilium status`/connectivity, Hubble, and no drop spike.
   - Check that `cluster.name` meets 1.17's ≤32 chars of `[a-z0-9-]`.
3. **Kyverno 1.13.4 → 1.14.5 → 1.15.3 → 1.16.4.**
   - Platform PRs (owner merges), one hop each. Verify admission and webhook health after every hop.
   - 1.15 turns on ValidatingAdmissionPolicy generation by default. Set it explicitly to the current
     behaviour.
4. **CNPG 1.24.1 → 1.25.4 → 1.26.3 → 1.27.4.**
   - Platform PRs (owner merges), one hop per window. Each hop rolls and switches over every cluster;
     watch infra-pg (Gitea/Authelia) closely.
   - 1.27's liveness isolation check can shut down a primary that loses the API server. Do not run a
     CNPG hop and a CP reboot in the same window.

### P5. Kubernetes 1.31.4 → 1.32.13 (client 1.13.11)

- Run `talosctl upgrade-k8s --to 1.32.13 --with-docs=false --with-examples=false`.
- Then bump `kubernetes_version` in `infra`, `infra/agent-nodes` and `infra/env-pool`. Each
  `tofu plan` must show config-only changes.
- Verify: all control-plane static pods are on 1.32.13, every kubelet reports 1.32.13, and no
  deprecated-API errors.

### P6. Add-ons for 1.33, then Kubernetes 1.32.13 → 1.33.13

1. Cilium 1.17.18 → 1.18.14. The new service-account identity label causes identity churn: quiet
   window, and watch drops.
2. Run `upgrade-k8s --to 1.33.13` (same flags) and bump the tofu `kubernetes_version` again.

### P7. Talos 1.13.11 → 1.14.2 (client 1.13.11)

- Take a fresh off-host etcd snapshot right before the first CP. etcd 3.7.1 is one-way once the
  cluster version moves.
- Verify the TLS 1.3 minimum before the first CP: talos-backup, the Prometheus apiserver/kubelet
  scrape, Flux and the ESO webhooks all negotiate TLS 1.3. Check with `openssl s_client -tls1_2`
  against a 1.14 node.
- Same per-node gates. talosctl ≥1.13 drains (`--drain` default, 5 m timeout). If a drain fails, the
  node may be left cordoned: re-run, never use `--force`.
- Keep workload isolation (sandboxd) off.

### P8. Reconcile IaC and record

- `talos_version = "v1.14.2"` in all three modules; the import_from guards prevent VM replacement.
- `tofu plan` is a no-op apart from expected attributes. Apply with `apply_mode = "no_reboot"`.
- New `docs/runbooks/talos-upgrade.md`: the per-node procedure, gates, client-matching, install-image
  rule and rollback.
- Follow-ups out of scope:
  - Kubernetes 1.34+ (1.33 is EOL);
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
- cchifor/platform (owner merges):
  - `deploy/components/kyverno/helmrelease.yaml`
  - `deploy/components/cnpg-operator/helmrelease.yaml`
- Ops checkout `kubernetes/infra/_out/`: talosctl binaries and talosconfig (not in git).

## Verification

- **Per node, every Talos step:**
  - `talosctl version` shows the target;
  - `get extensions` shows iscsi-tools (and kata/gvisor on the env node);
  - the full gate above;
  - no new critical alerts for 30 min.
- **Per add-on hop:** the component's own health (Cilium connectivity test, Kyverno admission of a
  test object, CNPG cluster healthy + streaming + a switchover observed, QNAP PVC
  provision/attach/expand).
- **Per Kubernetes step:** all nodes at the version; `kubectl get --raw /readyz?verbose`;
  `apiserver_requested_deprecated_apis` reviewed; the Flux Kustomizations and HelmReleases all Ready.
- **End state:**
  - 7 nodes on Talos v1.14.2 and Kubernetes v1.33.13;
  - etcd 3.7.x, 3/3;
  - all three tofu plans clean;
  - runbook merged;
  - leader-change rate no worse than the P0 baseline.

<!-- codex-review-status: pending -->
