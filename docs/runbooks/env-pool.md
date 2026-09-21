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
sandbox. The readiness contract in `sandboxtemplate-std.yaml` + `ready-watchdog.yaml` (an in-guest
watchdog holds the ready-port open only while virtio-fs and dockerd answer; the kubelet probes it
over TCP, ~30 s of a closed port = NotReady) is what keeps a 6 s blip from triggering the GC, and
what makes a real stall visible: `kubectl -n testpool logs <pod> -c control` names the check that
failed. The root cause of the guest freeze itself is still open; the node now runs with Kata
debug evidence switched on (next section) so that the next stall is captured, not just contained.

## Kata debug evidence (2026-09-21, plan T2)

What is on, where it lands, and how to read it. Everything here is delivered by tofu from
`kubernetes/infra/env-pool/machine-config/` (`var.kata_debug = true`), so the node's state is the
repo's state.

- **containerd at `[debug] level = "debug"`** (`cri-20-customization.part` → `/etc/cri/conf.d/20-customization.part`).
  This is what makes the kata shim emit anything at all: containerd passes `-debug` to shims only
  at its own debug/trace level, and the shim logs at Warn otherwise. The same part points the
  `kata` runtime (handler behind `RuntimeClass kata-env`) at the pool's own Kata config.
- **Kata config** = `/var/etc/kata-containers/configuration.toml`, a verbatim copy of the
  extension's file (`kata/configuration.toml`, sha256
  `38d1e30b0dc4ad59742bf807ddbc9363deb354e94a27c06883ca6f5d8074a4c6` for kata-containers 3.20.0 /
  schematic `0839748e…` / Talos v1.11.2), plus the drop-in `config.d/10-debug.toml`:
  `[hypervisor.clh] enable_debug` (cloud-hypervisor `-v` **and the guest serial console** → shim
  log lines tagged `vmconsole` — a guest kernel hung-task trace is the evidence a virtio-fs stall
  leaves), `[agent.kata] enable_debug` (the chatty one), `[runtime] enable_debug`.
- **Where it lands:** the node's containerd log, which on Talos is a **~1 MiB in-memory ring**
  (`talosctl logs cri`), minutes at debug rates. The durable copy is the
  `monitoring/cri-log-relay` Deployment (`kubernetes/apps/infrastructure/monitoring/cri-log-relay.yaml`),
  which streams `talosctl logs cri -f` with a read-only `os:reader` talosconfig into its own stdout
  → Alloy → Loki (168 h). If that pod is not Running, nothing is being kept.
- **Reading a stall.** The join key is the sandbox id: the reaper prints it in every `reap …
  sandbox=<id>` and `evidence … sandbox=<id>` line (kube-system), and the shim/agent/console lines
  carry the same id. In Grafana → Explore (Loki):
  ```logql
  {namespace="monitoring", app="cri-log-relay"} |= "<sandbox-id>"
  {namespace="monitoring", app="cri-log-relay"} |~ "vmconsole|hung_task|blocked for more than"
  {namespace="kube-system", app="env-reaper"} |~ "reap stage=|evidence"
  {namespace="testpool", container="control"} |= "ready-watchdog:"
  ```
  `evidence` lines (`state=`, `wchan=`, `threads=`, `tstates=`, and `evidence-stack … stack=`) are
  what the kernel knew about the VMM/virtiofsd right before the reaper killed them. The relay
  re-emits its last 200 lines on every reconnect (after the node reboots): dedupe by timestamp.
- **Privacy.** Shim debug records include exec commands, environment and mount details of leases.
  Raw exports stay under `kubernetes/infra/_out/` or in Loki; this repository is mirrored publicly,
  so plans/PRs carry redacted excerpts only.
- **Volume tiers.** Measured at rollout (V2 in the plan) — idle and during one lease. Tier 2 = set
  `[agent.kata] enable_debug = false` in `10-debug.toml` (keeps the guest console); there is no
  tier 3 short of dropping `[debug] level`, which silences the guest entirely. Either change is a
  machine-config change (below) and starts a new soak epoch.
- **Before any env-node image or extension upgrade:** re-verify the base file —
  `MSYS_NO_PATHCONV=1 kubernetes/infra/_out/talosctl-1112.exe -e 192.168.0.41 -n 192.168.0.37 read /usr/local/share/kata-containers/configuration.toml | sha256sum`
  must equal the hash above. If it differs, refresh `kata/configuration.toml` from the new
  extension FIRST; otherwise the node would run an old base under a new Kata.

## Changing the env node's machine config (tofu, staged, then an explicit reboot)

Running env nodes apply in **`staged`** mode (`env_nodes[*].apply_mode`, `variables.tf`): tofu
writes the new config to the node's STATE partition and never reboots or live-edits the pool's
node. From Windows (`~/.tofubin/tofu.exe`; from a worktree add
`-backend-config="path=C:/Users/chifo/work/home/ailab/kubernetes/infra/env-pool/terraform.tfstate"` to `init`):

1. Back up the state: `Copy-Item kubernetes/infra/env-pool/terraform.tfstate kubernetes/infra/_out/env-pool.tfstate.<utc-stamp>`.
2. `tofu -chdir=kubernetes/infra/env-pool plan -out=../_out/env-pool.tfplan` — expect exactly the
   in-place change on `talos_machine_configuration_apply.worker["env-node-1"]` you intended and
   nothing on the VM, label or taint. To see the exact node-side diff, extract the rendered config
   from the plan JSON (`tofu show -json …` → `resource_changes[].change.after.machine_configuration`,
   into `_out/`, it holds cluster secrets) and run
   `talosctl-1112.exe … apply-config --dry-run --mode=auto -f <file>`.
3. `tofu -chdir=kubernetes/infra/env-pool apply ../_out/env-pool.tfplan` — stages only.
   `talosctl … read /system/state/config.yaml | grep -c <new path>` shows the staged content;
   `talosctl … get mc -o yaml` still shows the running one.
4. **Reboot in an announced window** (the pool has one warm member; the gap is ~3 min and any
   lease started in it is lost). Pre-checks, all must hold right before the command:
   `kubectl -n testpool get sandboxclaims` empty; no `Terminating` pods on the node; node `Ready`;
   `kubectl -n testpool exec <member> -c control -- true` answers (a frozen guest hangs the reboot —
   reap it first); `cri-log-relay` Running. Then
   `kubernetes/infra/_out/talosctl-1112.exe -e 192.168.0.41 -n 192.168.0.37 reboot --wait`.
   If it sits in `stopAllPods` for > 5 min, `qm reset 4401` from ai-node2 (Recovery, above).
5. Verify: `talosctl … read /etc/cri/conf.d/cri.toml` shows the intended `kata` runtime table;
   `talosctl … ls -l /var/etc/kata-containers/config.d` lists exactly the drop-ins in the repo (each
   is an explicit `machine.files` entry — a file that only exists in the repo ships nothing, and
   `op: create` rewrites every entry on every boot, so nothing stale survives); the pool returns to
   `1/1`; a lease runs `docker version`; the relay shows shim `level=debug`, `kata-agent` and
   `vmconsole` lines for the new sandbox; `tofu plan` = `No changes.`
6. **Rollback** = `kata_debug = false` (or the previous drop-in content) → steps 1–5. The files under
   `/var/etc/kata-containers/` persist but are inert once the CRI part no longer references them.

Every reboot, rollback or tier change **starts a new soak epoch** (next section).

## Soak check-ins (plan T3)

`scripts/env-pool-soak.py` is the read-only check-in report: Prometheus (`:30090`) and Loki
(`:30310`) over an explicit UTC window, raw export under `kubernetes/infra/_out/soak/`, one
markdown block for the plan's soak record with a verdict — `OK`, `RECURRENCE-CONTAINED`,
`PREVENTION-FAILED` or `INCOMPLETE` (a failed endpoint, missing node, truncated page or a window
past Loki's 168 h can never read as a clean soak). Run it from the MAIN checkout at day 1, day 3
(past 66 h) and day 7 of each epoch, from the last checkpoint:

```sh
python scripts/env-pool-soak.py --checkpoint kubernetes/infra/_out/soak/checkpoint.json
```

`PREVENTION-FAILED` at any check-in → the Recovery section, immediately. The queries it runs
(`PROM_RANGE_QUERIES`, `PROM_INSTANT_QUERIES`, `LOKI_QUERIES` at the top of the script) are the
reproducible definition of every number in the report: node readiness and kubelet `/metrics`
target, `node_boot_time_seconds` (epochs), per-member `max_over_time` age and container restarts,
`ALERTS{alertname=~"Testpool.*|EnvNode.*"}`, `increase(kubelet_runtime_operations_errors_total{…stop_.*})`,
the reaper's `reap`/`evidence`/`heartbeat` lines, the watchdog's closures and the relay's counts
per reaped sandbox.

### Fault injection (validating the reaper, or reproducing a hung teardown on purpose)

The only thing on Talos that can signal a host process is a hostPID pod, so the **production
reaper pod itself** is the tool (`kubectl -n kube-system exec` into it). Do **not** run a second copy
with a shorter `REAP_AFTER_SECONDS` next to the production DaemonSet: it would reap first,
invalidate the production timing and truncate the evidence window. (The pre-merge validation of
2026-09-20 used a 5 s hack copy only because the production reaper did not exist yet.) Scope the
injection to one identified idle member — record its pod UID and sandbox id — and re-validate each
PID immediately before signalling. Freezing the current member's VMM:

```sh
POD=$(kubectl -n kube-system get pod -l app.kubernetes.io/name=env-reaper -o name)
kubectl -n kube-system exec $POD -- sh -c 'for cg in /proc/[0-9]*/cgroup; do grep -q "/kata_" "$cg" 2>/dev/null && p=${cg#/proc/} && p=${p%/cgroup} && [ "$(readlink /proc/$p/exe)" = /usr/local/bin/cloud-hypervisor ] && echo $p; done'
kubectl -n kube-system exec $POD -- kill -STOP <pid>          # a frozen VMM: Kata's monitor declares it dead in ~13 s
kubectl -n testpool delete sandbox <member>                    # ...and the shim completes the stop by itself (<30 s)
```

That is NOT the incident's hang. The faithful one freezes the sandbox's **virtiofsd** instead (two
processes, `exe=/usr/local/libexec/virtiofsd`, cgroup `/kata_overhead/<sandbox-id>`): guest
processes block in D-state on the rootfs while the agent keeps answering — `kubectl exec` into the
member hangs outright, the delete leaves the pod `Terminating` with `stop_*` errors climbing, and
only the reaper's stage 1 ends it (validated twice on 2026-09-20, 8.5 min hang → gone within 30 s
of the reap, zero residuals).

Watch `kubectl -n kube-system logs -f $POD` for `reap stage=1 …`; the pod must be gone within 60 s
of that line, `talosctl processes` must show no process for the sandbox, `/run/vc/sbs/<id>` and
`/run/kata-containers/shared/sandboxes/<id>` must be gone, the PV `Released` then deleted, and the
replacement `Ready`, and — with debug evidence on — `evidence` lines for each killed PID plus the
sandbox's shim/agent/`vmconsole` lines in the relay stream. Stage 2 alone (`REAP_STAGE1=0`) can
only be exercised by editing the production DaemonSet's env in a maintenance window and reverting
it; it remains untested, as do a containerd restart mid-reap and two simultaneous hangs. Holding a
freeze ≥ 5 min before the reaper acts is the firing/recovery fixture for `TestpoolEnvTeardownStuck`
and `EnvNodeRuntimeStopErrors` — that needs the production threshold raised for the window, again
by editing and reverting the DaemonSet, never a parallel copy.

## Related

- Alerts: `kubernetes/apps/infrastructure/monitoring/testpool-rules.yaml`, `env-node-rules.yaml`
  (fixtures beside them).
- Dashboard: "AI Lab Fleet" (Grafana home) → rows "Estate Health" and "Test Env Pool".
- IPAM: `docs/network-plan.md` (`.37`). Sizing/history: `kubernetes/infra/env-pool/SPIKE-REPORT.md`.
- General node loss / volume detach behaviour: `docs/runbooks/node-maintenance.md`.
- Debug evidence pipeline: `kubernetes/apps/infrastructure/monitoring/cri-log-relay.yaml`; soak
  report: `scripts/env-pool-soak.py`; the follow-up plan and its soak record:
  `plans/2026-09-20-env-pool-root-cause-followup-plan.md`.
