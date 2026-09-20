# Runbook — test-env-pool node (`talos-env-node-1`)

The single Talos **worker** that hosts the leasable Kata DinD environments (`testpool`,
`kubernetes/apps/infrastructure/testpool/`): VM **4401 on ai-node2**, `192.168.0.37`, 16 GiB fixed /
8 vCPU, label `ailab.io/env-pool=true`, taint `dedicated=env:NoSchedule`. Tofu module
`kubernetes/infra/env-pool/` — but **its state was never moved out of the spike's scratchpad clone**
(`SPIKE-REPORT.md`, "State / handover"), so today the VM is tracked in no state; `docs/network-plan.md`
is its only record. Nothing but DaemonSets and env pods run here; losing the node loses warm
capacity, nothing else.

Incident that produced this runbook: 2026-09-20, `plans/2026-09-20-env-pool-frozen-guest-outage-plan.md`
(the full evidence chain). Short form: a long-lived Kata guest freezes → its teardown never completes
→ a second hung teardown plus a sandbox create wedges the kubelet → node `NotReady`, pool at 0.

## Prerequisites

- **talosctl:** `kubernetes/infra/_out/talosctl-1112.exe` (v1.11.2 — the system `talosctl` v1.6.2 is
  fine for read-only calls but unsafe for config writes) with
  `TALOSCONFIG=kubernetes/infra/_out/talosconfig`, endpoint any CP (`-e 192.168.0.41`), node
  `-n 192.168.0.37`.
- **kubectl:** `KUBECONFIG=kubernetes/infra/_out/kubeconfig` (context `admin@ai`; the default context
  is a DIFFERENT cluster).
- **Proxmox:** root SSH to ai-node2 (`ssh root@192.168.0.3`, inventory key `~/.ssh/id_ed25519`) or
  `python scripts/node-ssh.py 192.168.0.3 "<cmd>"`. **Verify the vmid before any `qm` command:**
  `qm list | grep talos-env`.
- All of this needs the mgmt LAN (`192.168.0.0/24`). None of it is reachable from a dev-worker
  lease, and `hostPID` on the env node does not grant Proxmox access.

## Symptoms → what they mean

| You see | It means | Do |
|---|---|---|
| `TestpoolEnvTeardownStuck` (testpool pod `Terminating` > 5 m) | a Kata teardown is hung; the env-reaper should have killed the VMM ~150 s after the delete | `kubectl -n kube-system logs ds/env-reaper` — expect `reap stage=1 …` lines; if none, the reaper is not seeing the pod (RBAC? node?) |
| `TestpoolEnvTeardownStuckCritical` (> 20 m) | the reaper is dead, wedged, or its kills did not unstick the shim | **Reset the node now** (below) — the next hung teardown wedges the kubelet |
| `EnvNodeRuntimeStopErrors` | `stop_container`/`stop_podsandbox` failing for 30 m | same as above; `talosctl logs cri \| grep StopPodSandbox` shows the sandbox id |
| `EnvNodeKubeletMetricsBlocked` (kubelet `/metrics` target down 10 m) | containerd's CRI stats calls are hanging — the precursor of the wedge (6 h of lead on 2026-09-20) | if the node is still `Ready`: find the hung sandbox (`talosctl logs cri`), let the reaper act or reset during a quiet moment; if `NotReady`: reset |
| node `NotReady`, Talos `apid` answers, `talosctl services` shows `kubelet Running/Fail: healthz … deadline exceeded`, no new kubelet log lines | the kubelet is wedged (Go-level, process in `S`), not the VM | reset; a `kubelet` restart re-wedges on the same stuck shims |
| `TestpoolNoWarmCapacity` with the node `Ready` | pool not refilling for another reason | `kubectl -n testpool get sandboxes,pvc`; golden snapshot? PVC Pending? |
| "Estate Health" row on the AI Lab Fleet dashboard: a red bar in the node-readiness timeline for `talos-env-node-1` | the outage window, at a glance | this runbook |

## Recovery: the sequence that works (2026-09-20, ~5 minutes)

Both graceful paths hang on the un-killable containers — do not wait on them:

- `talosctl … reboot` sat in `phase cleanup (1/10): stopAllPods … shutting down kubelet gracefully`
  for 5 min.
- `talosctl … reboot --mode powercycle` cancelled that sequence and started the SAME cleanup phase.

1. **(≤ 5 min) Evidence, if the outage is already total.** Do not extend it collecting more:
   ```sh
   T="kubernetes/infra/_out/talosctl-1112.exe -e 192.168.0.41 -n 192.168.0.37"
   $T services; $T processes | grep -E "cloud-hypervisor|virtiofsd|containerd-shim-kata"
   $T logs cri > cri.log; $T logs kubelet > kubelet.log; $T logs syslogd > syslogd.log; $T dmesg > dmesg.log
   $T ls /run/vc/sbs; $T read /run/vc/sbs/<sandbox-id>/persist.json > persist.json   # MSYS_NO_PATHCONV=1 in Git Bash
   kubectl -n testpool get pods -o wide; kubectl -n kube-system logs ds/env-reaper --tail=200
   ```
2. **Hard reset from Proxmox.**
   ```sh
   ssh root@192.168.0.3 'qm list | grep talos-env && qm reset 4401'
   kubectl get node talos-env-node-1 -w        # Ready ~40 s after the reset
   ```
3. **Only after the node is `Ready`:** force-delete the orphaned pod objects (the fresh kubelet has no
   record of them; they hold the PVC finalizers):
   ```sh
   kubectl -n testpool get pods
   kubectl -n testpool delete pod <Unknown/Terminating env pods> --force --grace-period=0
   ```
4. **Expect one extra rotation.** A replacement that sat `Pending` during the outage is older than
   the pool's 15 m readiness grace, so the warm-pool GC deletes it ~30 s after it finally schedules
   and creates another; the pool settles `1/1` about 3 min after step 3.
5. **Verify.** `kubectl -n testpool get sandboxes,swp` (`Ready=True`, `1/1`);
   `kubectl get pv | grep Released` (Trident reclaims them within a minute);
   `talosctl … services | grep kubelet` (`Running/OK`); the alerts above resolve.

## Why this happens, and what bounds it now

`kubernetes/apps/infrastructure/testpool/env-reaper.yaml` (DaemonSet in `kube-system`) is the
guarantee that hung teardowns never accumulate: a `testpool` pod `Terminating` longer than 120 s
past its `deletionTimestamp` gets its Cloud Hypervisor VM and virtiofsd SIGKILLed from the host
(stage 1), and the kata shim 120 s later if the teardown still has not completed (stage 2). A
process is signalled only when its cgroup, executable and cmdline all tie it to the deleted pod's
sandbox. The readiness contract in `sandboxtemplate-std.yaml` (an exec of `docker version` through
the kata-agent, 60 s of sustained failure) is what keeps a 6 s blip from triggering the GC in the
first place. The root cause of the guest freeze itself is still open — see the plan's "Out of
scope": Kata debug logging needs the machine config under tofu.

### Fault injection (validating the reaper, or reproducing a hung teardown on purpose)

The only thing on Talos that can signal a host process is a hostPID pod, so the reaper itself is
the tool. From `testpool/hack/`, apply a copy of the DaemonSet renamed `env-reaper-hack` with
`REAP_AFTER_SECONDS=5` (never through Flux), then from that pod freeze the current member's VMM:

```sh
POD=$(kubectl -n kube-system get pod -l app.kubernetes.io/name=env-reaper-hack -o name)
kubectl -n kube-system exec $POD -- sh -c 'for cg in /proc/[0-9]*/cgroup; do grep -q "/kata_" "$cg" 2>/dev/null && p=${cg#/proc/} && p=${p%/cgroup} && [ "$(readlink /proc/$p/exe)" = /usr/local/bin/cloud-hypervisor ] && echo $p; done'
kubectl -n kube-system exec $POD -- kill -STOP <pid>          # the guest is now frozen for real
kubectl -n testpool delete sandbox <member>                    # StopContainer hangs: Terminating, stop_* errors
```

Watch `kubectl -n kube-system logs -f $POD` for `reap stage=1 …`; the pod must be gone within 60 s
of that line, `talosctl processes` must show no process for the sandbox, `/run/vc/sbs/<id>` and
`/run/kata-containers/shared/sandboxes/<id>` must be gone, the PV `Released` then deleted, and the
replacement `Ready`. Set `REAP_STAGE1=0` on the hack copy to exercise stage 2 alone. Delete the
hack DaemonSet afterwards. Holding the freeze ≥ 5 min before letting the reaper act is also the
firing/recovery fixture for `TestpoolEnvTeardownStuck` and `EnvNodeRuntimeStopErrors`.

## Related

- Alerts: `kubernetes/apps/infrastructure/monitoring/testpool-rules.yaml`, `env-node-rules.yaml`
  (fixtures beside them).
- Dashboard: "AI Lab Fleet" (Grafana home) → rows "Estate Health" and "Test Env Pool".
- IPAM: `docs/network-plan.md` (`.37`). Sizing/history: `kubernetes/infra/env-pool/SPIKE-REPORT.md`.
- General node loss / volume detach behaviour: `docs/runbooks/node-maintenance.md`.
