# env-pool: stop a frozen Kata guest from taking talos-env-node-1 down

## Codex Review

<!-- codex-review-status: complete -->

**Security and correctness issues block implementation; substantial revisions needed before deployment.**

- **P1 credential & process-matching issues:** The reaper (T2) would grant every dev-worker credential access to host processes via hostPID+privileged; literal `comm` matching (15-char truncation) misses the cloud-hypervisor and containerd-shim. Requires namespace isolation and verified process identity chain.
- **P1 teardown assumption gap:** Killing cloud-hypervisor is plausible but not a completion guarantee; stage 2 (shim kill) is recovery, not proof of cleanup. Existing Kata wait/watch and containerd cleanup implementations have failure modes and races not addressed here.
- **P1 exec readiness risk:** Switching to `docker version` exec probes risks stale-Ready state when exec errors occur; Kubernetes 1.31 probe worker discards some CRI errors without counting failures. Eight-thousand+ execs/day/container expose leaks and timeouts.
- **P2 API connectivity & T5 timing gaps:** NetworkPolicy blocks the reaper's API path with no tested exemption; T5's age-based rotation has adoption races and permits 44-hour-old members (vs. proposed 20 hours). Verification steps cannot distinguish healthy behavior from undetected failure.
- **Positive:** SIGTERM trap design (sleep + wait) is sound; T4 runbook is justified; grace-period comment correction is correct. T1–T4 framework is viable with fixes; T5 should defer until T2 is validated.

## Context

**Incident 2026-09-20 09:29–12:38 UTC — `talos-env-node-1` NotReady, testpool at 0 warm
capacity for 3 h 10 m.** Recovered by a Proxmox `qm reset 4401` (both `talosctl reboot` and
`reboot --mode powercycle` hang in `stopAllPods` on the same un-killable containers).

The node did not die; the **kubelet wedged** (healthz deadline-exceeded, no log line, no lease
renewal from 09:29:04) behind containerd CRI calls that never return. The chain, each link
evidenced in the session that produced this plan:

1. **A long-lived env pod freezes.** `env-std-pool-k6h5m` (66 h old, Ready, idle at 0.03 cores)
   went NotReady at 09:27:48 — the `control` container's TCP ready-port stopped answering.
   `env-std-pool-xgssp` (24 h old) did the same on 09-17 15:35:29. Both readiness signals depend
   on re-exec'ing `nc` from the container rootfs every ~2 s (`( while true; do nc -l -p 9099 …;
   done ) &`), and the rootfs plus the `work`/`dsock` emptyDirs are served to the guest by a
   **single-threaded virtiofsd** (`--thread-pool-size=1`, `cache=auto`). A virtiofs stall is the
   one component whose failure explains every symptom below; it is not yet proven.
2. **The warm-pool GC turns a blip into a deletion.** agent-sandbox v1.0.2
   `sandboxwarmpool_controller.go:519-548` deletes any member with `!Ready && age > 15m`
   (`--sandbox-warm-pool-readiness-grace-period=15m`). It does not look at how long the member
   had been Ready; the kustomization comment that introduced the flag describes it as a fill-time
   window, which is wrong. The same rule killed the recovery's first replacement (`fsm72`,
   Pending 171 m during the outage) 34 s after it scheduled, before it could become Ready.
3. **Teardown of a frozen guest never completes.** `StopContainer`/`StopPodSandbox` for the
   deleted pod hang forever (`wait container … to be killed` never returns: the kata-agent accepts
   the signal, the guest processes never exit — consistent with D-state on a stalled virtiofs).
   kubelet retried every ~4.5 min for **3 days** (`kubelet_runtime_operations_errors_total`
   `stop_podsandbox` ≈ 26 / 2 h since 09-17 16:00). Each rotation leaks a Cloud Hypervisor VM
   (~800 MiB RSS, 60 GiB iSCSI LUN); `xgssp`'s zombie guest additionally burned 1.1 vCPU for 3 days.
   Teardown of a **fresh, idle** env (tested 12:42:24 today) completes in <10 s — the hang needs
   the frozen state, it is not deterministic.
4. **Two hung teardowns plus one sandbox create wedged the kubelet** (09:27:48 second hang,
   09:28:59 `mzjlz` create, 09:29:04 last kubelet log line). `ListContainerStats` had been timing
   out since ≤03:11 (`KubeletInstanceUnreachable` fired then — 6 h of unused warning).

Nothing alerted on the 3-day-old stuck teardown, and no runbook covers this node.

## Approach

Prevent the cascade at every link, in one PR on `fix/env-pool-frozen-guest-outage`, Flux-applied
under `kubernetes/apps/infrastructure/testpool`. Root-causing the guest freeze itself is a
follow-up (it needs a Kata debug configuration, which needs the env-node's machine config under
tofu management — see "Out of scope"). The prevention does not depend on knowing the freeze's cause.

### T1 — Readiness means "a lease can run docker here" (link 1 → link 2)

`kubernetes/apps/infrastructure/testpool/sandboxtemplate-std.yaml`, container `control`:

- Replace the fork-per-probe listener with one persistent process:
  `nc -lk -p 9099 -s 0.0.0.0 </dev/null >/dev/null 2>&1 &` (netcat-openbsd `-k`). No exec every
  2 s, no ~200 ms accept gap for a probe to land in.
- Keep `startupProbe` on `tcpSocket: 9099` (fast, 1 s period, gates on dockerd having answered once).
- `readinessProbe` becomes `exec: ["sh","-c","docker version >/dev/null"]`,
  `periodSeconds: 10`, `timeoutSeconds: 5`, `failureThreshold: 6`. A member is NotReady only after
  ≥60 s of a lease-equivalent check failing (exec through the kata-agent + dockerd over the
  socket). A 6 s blip can no longer trigger the GC; a genuinely frozen guest still does — which is
  what the GC is for, once T2 makes the resulting teardown bounded. The template's F5 note ("no
  exec probes") was about `dind`, which is un-exec-able under cgroup-v2 subtree control; `control`
  is the container every lease already execs into.
  <!-- codex: Exec readiness probe risks stale-Ready state on CRI errors (Kubernetes 1.31 probe worker discards some errors without counting failures); 8000+ execs/day/container expose timeout and leak risks. Test stalled exec, timeout cleanup, and readiness transitions under load. -->
- Handle SIGTERM in both entrypoints so a graceful stop takes ~1 s instead of the full 30 s grace
  then SIGKILL (today PID 1 is `sh -c` with no trap, so every stop is a SIGKILL after 30 s):
  `dind`: start dockerd with `& DPID=$!`, `trap 'kill -TERM $DPID; wait $DPID; exit 0' TERM`;
  `control`: `trap 'exit 0' TERM`. Both must end with `sleep infinity & wait $!` — a foreground
  `sleep infinity` never returns, so a trap set before it never fires.
  <!-- codex: SIGTERM trap design with backgrounded sleep and shell wait is sound, but existing listener does not show the claimed 200ms accept gap (sleep 0.2 only runs on nc failure). Test TERM during init and steady state in both images. -->
- Rewrite the header comment (lines 8–14) to state the new readiness contract.

Template hash change → the warm pool replaces its member on reconcile (expected, one rotation).

### T2 — `env-reaper`: no teardown outlives 3 minutes (link 3 → link 4)

New `kubernetes/apps/infrastructure/testpool/env-reaper.yaml` (+ kustomization entry): a
DaemonSet on `ailab.io/env-pool=true` nodes (tolerates `dedicated=env:NoSchedule`), `hostPID:
true`, `privileged: true`, ServiceAccount `env-reaper` with a Role limited to `pods` `get/list` in
`testpool`. Image: a pinned `kubectl`+`jq` image (`docker.io/alpine/k8s`, digest resolved at
implementation). Loop every 60 s:

1. `kubectl get pods -n testpool -o json` → pods with `deletionTimestamp` older than
   `REAP_AFTER_SECONDS` (default 180) scheduled on this node.
2. For each pod UID, find host processes whose `/proc/<pid>/cgroup` contains `pod<uid>` and whose
   `comm` is `cloud-hypervisor` or `virtiofsd` → `kill -9`. (`sandbox_cgroup_only=false`, so the
   VMM sits in `/kubepods/…/pod<uid>/kata_<sandbox>`; the CH process's cgroup was read from
   `persist.json` during the incident.) The kata shim observes VMM death and completes the
   pending `StopContainer` — this is the assumption V3 validates.
   <!-- codex: [P1] Moving to infrastructure namespace with cross-namespace RoleBinding is required; dev-worker credentials exec any testpool pod, granting them access to this reaper's hostPID privileges. Process identity must use cgroup + executable + socket paths, not literal comm (16/23 chars vs 15-char truncation); verify chain from pod UID → sandbox → process, excluding transient cleanup commands and other runtimes. -->
   <!-- codex: [P1] Killing cloud-hypervisor is plausible but not completion guarantee (Kata wait/watch has failure modes, races, and I/O waiter paths). Stage 2 is best-effort recovery, not proof of cleanup. Validate stages separately against deployed Kata/containerd versions, including concurrent cleanup, containerd restart, and residual resources (processes, mounts, network, CRI records, PVs/LUNs). -->
3. If the same pod is still Terminating after `2 × REAP_AFTER_SECONDS`, kill its
   `containerd-shim-kata-v2` too (containerd treats a dead shim's tasks as exited; the standard
   crashed-shim path).
4. Log one line per action (`reaped <comm> pid=<n> pod=<name> stage=<1|2>`); alloy ships it.

Idempotent (nothing to kill → nothing logged), pool-agnostic (matches any `testpool` pod), and
safe on a healthy node: a pod is only touched after it has been Terminating for 3 minutes, which
a working teardown never reaches (T1 makes graceful stops take ~1 s; today's worst case is 30 s).

<!-- codex: [P2] NetworkPolicy (testpool-rules.yaml:20) excludes service/pod CIDRs and API VIP with no cluster-DNS allowance. T2 needs an explicitly permitted and tested API path. Add request timeout, iteration deadline, bounded retries, and failure reporting to kubectl loop. Specify behavior on API/network stall or force-deleted pod disappearing; include resource requests, priority, and heartbeat metrics to distinguish healthy idle from broken reaper. Timing promise: 3-4 min before stage 1, 6-7 min before stage 2, plus cleanup delays; the incident's second hang was ~76 seconds before kubelet wedge. -->

### T3 — Alerts that name the precursors (link 3, link 4, six hours earlier)

`kubernetes/apps/infrastructure/monitoring/testpool-rules.yaml`:

- `TestpoolEnvTeardownStuck` — `(time() - kube_pod_deletion_timestamp{namespace="testpool"}) > 300`
  for 5 m, warning; critical variant at 20 m (the reaper should have acted by then).
- `EnvNodeRuntimeStopErrors` — `sum by (node) (increase(kubelet_runtime_operations_errors_total{
  node=~"talos-env-node-.*", operation_type=~"stop_container|stop_podsandbox"}[15m])) > 0`
  for 30 m, warning. This exact series was non-zero for 3 days.
  <!-- codex: kubelet_runtime_operations_errors_total presence, node label, and operation values must be verified against rendered scrape targets and relabeling. Missing series makes the selector silently empty; an operation that never returns produces no new error increment. -->
- `EnvNodeKubeletDegraded` — the env node's kubelet `/metrics/resource` scrape target down for
  10 m (the signal that fired as `KubeletInstanceUnreachable` at 03:11; check that rule's
  definition first — reuse by severity/label if it already carries `node`, otherwise add).
- Update `TestpoolNoWarmCapacity`'s description to point at the runbook (T4).

### T4 — Runbook + correct the wrong comment

- New `docs/runbooks/env-pool.md`: what the node is (vmid 4401 on ai-node2, `.37`, unmanaged
  tofu state — see Out of scope), symptom table, the **recovery sequence that actually works**
  (`talosctl reboot` and `--mode powercycle` both hang in `stopAllPods`; `ssh root@192.168.0.3 qm
  reset 4401`; then force-delete the orphaned `Unknown`/`Terminating` pods; expect the GC to kill
  one Pending replacement before the pool settles), the pre-reset evidence checklist
  (`talosctl logs cri|kubelet|syslogd`, `dmesg`, `processes`, `read /run/vc/sbs/<id>/persist.json`)
  and the dashboards/alerts from T3.
  <!-- codex: Runbook should document authorized operator credentials and management-network access, verify VM 4401 on ai-node2, bound evidence collection, and confirm reset before force deletion. hostPID exposes Talos VM processes but does not grant Proxmox SSH access. -->
- `kubernetes/apps/infrastructure/agent-sandbox/kustomization.yaml`: rewrite the grace-period
  comment to what the flag does (any member observed NotReady after 15 m of age is replaced,
  regardless of prior readiness; the unschedulable hold does not reset the clock).
  <!-- codex: Grace-period comment correction is justified; controller is v1.0.2 (despite stale v1.0.0 filenames) and uses creation age with unschedulable hold for GC. -->
- `docs/network-plan.md` `.37` row: link the runbook.

### T5 — Daily rotation of unclaimed members (hedge, cheap)

Both frozen envs were ≥24 h old; a fresh env tears down cleanly. Until the freeze is root-caused,
keep members young: CronJob `env-rotate` (04:00 UTC, `kubectl` image from T2, SA with `sandboxes`
`list/delete` in `testpool`) deletes pool-owned Sandboxes (label
`agents.x-k8s.io/warm-pool-sandbox`) older than 20 h, one per run, only when the pool reports
`readyReplicas == replicas` and no `SandboxClaim` exists. Cost: one ~2-min warm gap per day at
04:00. With T2 in place the rotation is safe even if a teardown hangs. Evidence is n=2; the task
is flagged as a hedge and can be dropped if codex or the operator judge it not worth a daily gap.
<!-- codex: [P2] Daily evaluation permits ~44-hour-old members (skipped runs extend this); "no claim" check is not atomic with deletion (adoption race). Permissions cannot read WarmPool status or list SandboxClaims. Two incidents do not establish age as cause or quantify benefit. Defer until T2 validated; if retained, add timeZone, deadline/concurrency controls, failure alerts, removal criterion, and UID/resourceVersion preconditions. -->

### T6 — Upstream issue (draft only; filing needs operator OK)

kubernetes-sigs/agent-sandbox: (a) the stuck-member GC deletes a member that was Ready for days on
a single NotReady observation — propose measuring the grace period from the Ready condition's
`lastTransitionTime`; (b) a member held as unschedulable past the grace period is deleted the
moment it schedules (`fsm72`, 34 s after scheduling) — the hold should reset the clock. Draft text
goes in the PR description; the operator files it.

### Out of scope (follow-up plan)

- Root cause of the guest freeze: needs `[runtime] enable_debug` + `[agent.kata] enable_debug` in a
  custom `configuration.toml` (pod annotations cannot raise the shim log level; today no agent or
  virtiofsd line reaches the host log), i.e. a `/etc/cri/conf.d/20-customization.part` runtime
  with its own `ConfigPath` via Talos `machine.files` — which requires `talos-env-node-1`'s machine
  config under tofu (`kubernetes/infra/env-pool` state was never moved out of the spike's
  scratchpad clone). Same follow-up: decide virtiofs `cache`/thread-pool, and whether a second env
  node is worth the RAM to remove the pool's single point of failure.
- The kubelet 1.31 deadlock under hung CRI calls is upstream; T2 removes its precondition.

## Critical files

| Path | Role |
|---|---|
| `kubernetes/apps/infrastructure/testpool/sandboxtemplate-std.yaml` | T1: persistent ready-port, exec readiness, SIGTERM traps, header comment |
| `kubernetes/apps/infrastructure/testpool/env-reaper.yaml` (new) | T2: DaemonSet + SA/Role/RoleBinding + reaper script (ConfigMap) |
| `kubernetes/apps/infrastructure/testpool/env-rotate.yaml` (new) | T5: CronJob + SA/Role/RoleBinding |
| `kubernetes/apps/infrastructure/testpool/kustomization.yaml` | register the two new files |
| `kubernetes/apps/infrastructure/monitoring/testpool-rules.yaml` | T3: three new alerts, one description update |
| `kubernetes/apps/infrastructure/agent-sandbox/kustomization.yaml` | T4: correct the grace-period comment |
| `docs/runbooks/env-pool.md` (new) | T4: recovery + evidence checklist |
| `docs/network-plan.md` | T4: `.37` row links the runbook |
| `kubernetes/apps/infrastructure/testpool/README.md` | T1/T2/T5: readiness contract, reaper, rotation |

## Verification

- **V1 (T1, Flux):** after reconcile, `kubectl -n testpool get sandboxes` shows the old member
  replaced and the new one `Ready=True` within ~3 min; `kubectl -n testpool describe pod
  env-std-pool-<new>` shows the exec readiness probe passing and no `Unhealthy` events over 30 min.
  `hack/` lease smoke test (or `tep` from a dev-worker) runs `docker version` inside a lease.
  <!-- codex: 30 minutes covers roughly 180 successful probes, but does not establish slow exec/resource leakage, timeout cleanup, stale-Ready after errors, or failure under load. Add process/FD/memory trends and injected failure cases. -->
- **V2 (T1, graceful stop):** `kubectl -n testpool delete sandbox <member>` → pod gone in <5 s
  (was ≥30 s: SIGKILL after grace). Check `talosctl logs cri` shows `StopContainer … with timeout
  30` followed by an exit *before* any `Kill container` line.
- **V3 (T2, the load-bearing assumption):** with a hack copy of the DaemonSet running at
  `REAP_AFTER_SECONDS=5`, delete a fresh member: the reaper must log a stage-1 reap and the pod
  must disappear within 60 s of the reap; `talosctl processes` shows no `cloud-hypervisor` for the
  old sandbox; `kubelet_runtime_operations_errors_total{operation_type=~"stop_.*"}` stays flat
  afterwards. Then remove the hack copy; the Flux-owned reaper runs at 180 s. If the shim does
  *not* complete on VMM death, stage 2 (shim kill) is validated the same way and stage 1's delay
  is folded into it.
  <!-- codex: Flat error counters do not prove reaper ran; first demonstrate controlled stall and pending stop, then exercise each stage and verify complete cleanup (processes, mounts, CRI records, PVs/LUNs) and replacement readiness. Does not validate already-hung StopContainer recovery. -->
- **V4 (T3):** `promtool check rules`; each expression returns data against
  `http://192.168.0.41:30090` (confirm `kube_pod_deletion_timestamp` is exported); the three alerts
  are `inactive` after V3 settles.
  <!-- codex: Needs firing/recovery and missing-series fixtures. Healthy expressions legitimately return no series. The earlier 50-cycle spike used runc attachment pods, not Kata; does not validate forced-teardown cleanup. -->
- **V5 (T5):** `kubectl create job --from=cronjob/env-rotate` on a Ready pool: exactly one member
  deleted, replacement Ready within ~3 min, second run is a no-op (member <20 h).
- **V6 (soak):** 24 h with no `EnvNodeRuntimeStopErrors`, no `TestpoolEnvTeardownStuck`, pool
  `1/1`, node `Ready`.
  <!-- codex: One day does not establish protection against recurrence at 24/66 hours or on separate days (3 days apart). Observe multiple cycles beyond those ages (preferably week+) as evidence, not proof. Daily rotation would censor the age experiment. -->
- Docs: `docs/runbooks/env-pool.md` exercised against the steps that were actually run today.

<!-- codex-review-status: complete -->
