# env-pool: stop a frozen Kata guest from taking talos-env-node-1 down

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
   on re-exec'ing `nc` from the container rootfs after every probe (`( while true; do nc -l -p
   9099 …; done ) &` — one connection per `nc`), and the rootfs plus the `work`/`dsock` emptyDirs
   are served to the guest by a **single-threaded virtiofsd** (`--thread-pool-size=1`,
   `cache=auto`). A virtiofs stall is the one component whose failure explains every symptom
   below; it is not yet proven.
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
   09:28:59 `mzjlz` create, 09:29:04 last kubelet log line). One hung teardown plus one create
   (09-17 15:35) had NOT wedged it — the node ran 3 more days in that state. The kubelet's whole
   `/metrics` handler had been blocked since ~02:56 (`KubeletInstanceUnreachable`, the chart rule
   `up{job="kubelet",metrics_path="/metrics"} == 0`, fired at 03:11 — 6 h of unused warning).

Nothing alerted on the 3-day-old stuck teardown, no runbook covers this node, and the default
Grafana dashboard ("AI Lab Fleet", the home dashboard) shows no node readiness, firing alerts or
Flux state anywhere — today's outage was visible only as "Envs Ready = 0" in its eighth row.

## Approach

Prevent the cascade at every link and make the next one visible, in one PR on
`fix/env-pool-frozen-guest-outage`, Flux-applied under `kubernetes/apps/infrastructure/{testpool,
monitoring}`. Root-causing the guest freeze itself is a follow-up (it needs a Kata debug
configuration, which needs the env-node's machine config under tofu management — see "Out of
scope"). The prevention does not depend on knowing the freeze's cause, and deliberately leaves
member age alone so the follow-up can still observe whether age matters.

### T1 — Readiness means "a lease can run docker here" (link 1 → link 2)

`kubernetes/apps/infrastructure/testpool/sandboxtemplate-std.yaml`, container `control`:

- Replace the fork-per-connection listener with one persistent process:
  `nc -lk -p 9099 -s 0.0.0.0 </dev/null >/dev/null 2>&1 &` (netcat-openbsd `-k`). Today every
  probe connection terminates `nc` and the loop re-execs it from the virtiofs rootfs; the window
  between exit and the next `listen()` is the exec latency, which is exactly what a stalled rootfs
  stretches. One long-lived listener has no such window.
- Keep `startupProbe` on `tcpSocket: 9099` (fast, 1 s period, gates on dockerd having answered once).
- `readinessProbe` becomes `exec: ["sh","-c","docker version >/dev/null"]`,
  `periodSeconds: 10`, `timeoutSeconds: 5`, `failureThreshold: 6`. A member is NotReady only after
  ≥60 s of a lease-equivalent check failing (exec through the kata-agent + dockerd over the
  socket). A 6 s blip can no longer trigger the GC; a genuinely frozen guest still does — which is
  what the GC is for, once T2 makes the resulting teardown bounded. The template's F5 note ("no
  exec probes") was about `dind`, which is un-exec-able under cgroup-v2 subtree control; `control`
  is the container every lease already execs into.
  - **Known gap, documented in the template:** kubelet 1.31's prober (`prober/worker.go` `doProbe`)
    discards a probe whose transport errored (`probe.Unknown` + error) without counting a failure,
    so a CRI `ExecSync` that fails fast with a non-timeout error leaves readiness stale. The two
    stalls this plan targets both surface as **timeouts** — a frozen agent never answers, and a
    stalled rootfs blocks `sh`/`docker` in the guest until `timeoutSeconds` — and
    `pkg/probe/exec` maps a timeout to `probe.Failure`, which IS counted. V1 injects the
    hang and proves the transition; the non-timeout path stays covered by T3's alerts, which do
    not depend on the probe.
  - Exec cost: one short-lived guest process per 10 s. V1 checks the guest's process and FD counts
    over 24 h so an agent-side exec leak would be caught before it matters.
- Handle SIGTERM in both entrypoints so a graceful stop takes ~1 s instead of the full 30 s grace
  then SIGKILL (today PID 1 is `sh -c` with no trap, so every stop is a SIGKILL after 30 s):
  `dind`: start dockerd with `& DPID=$!`, `trap 'kill -TERM $DPID; wait $DPID; exit 0' TERM`;
  `control`: `trap 'exit 0' TERM`. Both must end with `sleep infinity & wait $!` — a foreground
  `sleep infinity` never returns, so a trap set before it never fires. The trap is installed as
  the first statement so a TERM during the `until docker version` init loop also exits (the loop's
  `sleep 0.2` returns between iterations; V2 tests both phases, in both images — busybox `sh` in
  `dind`, dash in `control`).
- Rewrite the header comment (lines 8–14) to state the new readiness contract and the gap above.

Template hash change → the warm pool replaces its member on reconcile (expected, one rotation).

### T2 — `env-reaper`: no teardown outlives 2 minutes (link 3 → link 4)

What it prevents and what it does not: a single hung teardown did not wedge the kubelet; the
**accumulation** did (two hung + a create). The reaper cannot race a fresh hang (today's second
hang preceded the wedge by 76 s) — it guarantees that hangs never accumulate: `xgssp`'s zombie
would have been reaped on 09-17, and today's single hang would have been a 2-minute blip.

New `kubernetes/apps/infrastructure/testpool/env-reaper.yaml` (+ kustomization entry), but the
workload lives in **`kube-system`**, not `testpool`:

- `testpool`'s `env-egress` NetworkPolicy (`podSelector: {}`) blocks the API VIP and the service
  CIDR for every pod in the namespace, and the `tep-worker` Role grants `pods/exec` on every
  `testpool` pod — a hostPID+privileged pod there would be unreachable to the API *and* a root
  shell on the node for any dev-worker credential. `kube-system` has no NetworkPolicy, is
  privileged-PSA, already hosts the node-plumbing DaemonSets (cilium, csi-nfs-node), and no
  estate credential has exec there.
- DaemonSet `env-reaper` (namespace `kube-system`): `nodeSelector: ailab.io/env-pool=true`,
  tolerations `dedicated=env:NoSchedule` and `node.kubernetes.io/unreachable`,
  `priorityClassName: system-node-critical`, `hostPID: true`, `privileged: true`, resources
  `50m/64Mi` requests, `200m/128Mi` limits, ServiceAccount `env-reaper` in `kube-system`; a Role
  in `testpool` (`pods` `get/list`) bound to `system:serviceaccount:kube-system:env-reaper`. Image:
  a digest-pinned `kubectl`+`jq` image (`docker.io/alpine/k8s`, via the estate mirror). Script in a
  ConfigMap.
- Loop every 30 s, each iteration under `timeout 25`; `kubectl --request-timeout=10s`; an API
  failure logs one line and the iteration ends (no retries inside the iteration — the next one is
  30 s away); a heartbeat line every 10 min so the log shows liveness.
  1. `kubectl get pods -n testpool -o json` filtered to this node (`$NODE_NAME` from the downward
     API) → pods whose `deletionTimestamp` is older than `REAP_AFTER_SECONDS` (default **120**;
     a graceful stop takes ~1 s after T1 and 30 s before it, so 120 s is 4× the worst legitimate
     case).
  2. **Process identity chain**, not `comm` (which is truncated to 15 chars —
     `cloud-hyperviso`, `containerd-shim`): a host process is reaped only if
     `/proc/<pid>/cgroup` matches `/kubepods/[^/]+/pod<uid>/kata_<sandbox-id>` (this is how the
     VMM is placed with `sandbox_cgroup_only=false`; the sandbox id comes from that path, nothing
     else) AND `readlink /proc/<pid>/exe` is one of `/usr/local/bin/cloud-hypervisor`,
     `/usr/local/libexec/virtiofsd` AND `/proc/<pid>/cmdline` contains
     `/run/vc/vm/<sandbox-id>/` (CH) or `/run/kata-containers/shared/sandboxes/<sandbox-id>`
     (virtiofsd). Anything else under the pod cgroup — runc/gvisor tasks, transient `umount`s,
     probe execs — is never touched. Stage 1: `kill -9` those.
  3. Stage 2, if the same pod is still Terminating `REAP_AFTER_SECONDS` later: the shim,
     identified by `exe == /usr/local/bin/containerd-shim-kata-v2` AND cmdline `-id <sandbox-id>`.
     containerd's crashed-shim path marks the tasks exited and lets `StopPodSandbox` complete.
     Stage 2 is recovery, not proof of cleanup — V3 checks residuals after each stage.
  4. One log line per action (`reap stage=<1|2> pod=<name> uid=<uid> sandbox=<id> exe=<path>
     pid=<n>`); alloy ships it. A pod that disappears between the list and the kill is skipped
     (the `/proc` lookup simply finds nothing).

Idempotent (nothing matches → nothing happens), safe on a healthy node (a pod is only touched
after 2 minutes of Terminating, which a working teardown never reaches), and self-limiting (it
only ever signals processes it has tied to a deleted pod by cgroup + binary + sandbox id).

### T3 — Alerts that name the precursors (link 3, link 4, six hours earlier)

`kubernetes/apps/infrastructure/monitoring/testpool-rules.yaml`. Every series below was verified
present with these exact labels against the live Prometheus during the incident
(`kubelet_runtime_operations_errors_total{node="talos-env-node-1",operation_type="stop_podsandbox"}`,
`kube_pod_deletion_timestamp{namespace="testpool",pod=…}` for all three stuck pods).

- `TestpoolEnvTeardownStuck` — `(time() - kube_pod_deletion_timestamp{namespace="testpool"}) > 300`
  for 5 m, warning; `TestpoolEnvTeardownStuckCritical` at 20 m — this is the reaper's outcome
  alert: it can only fire if the reaper did not do its job (dead, wedged, or its kill did not
  unstick the shim).
- `EnvNodeRuntimeStopErrors` — `sum by (node) (increase(kubelet_runtime_operations_errors_total{
  node=~"talos-env-node-.*", operation_type=~"stop_container|stop_podsandbox"}[15m])) > 0`
  for 30 m, warning. This exact series was non-zero for 3 days. (A fully wedged kubelet stops
  incrementing it — that state is the next rule's.)
- `EnvNodeKubeletMetricsBlocked` — `up{job="kubelet",metrics_path="/metrics",node=~"talos-env-node-.*"} == 0`
  for 10 m, **critical**: on the env node a blocked `/metrics` handler means CRI stats calls are
  hanging, the precursor of the wedge. The chart's `KubeletInstanceUnreachable` (15 m, warning,
  generic text) stays; this one carries the runbook link and the severity the env node warrants.
- Update `TestpoolNoWarmCapacity`'s description to point at the runbook (T4).

### T4 — Runbook + correct the wrong comments

- New `docs/runbooks/env-pool.md`: **prerequisites** (talosconfig at `kubernetes/infra/_out/`,
  root SSH to ai-node2 with the inventory key, the LAN — nothing here is reachable from a
  dev-worker or via hostPID), what the node is (vmid 4401 on ai-node2, `.37`, tofu state never
  imported — see Out of scope; **verify with `qm list | grep talos-env` before any `qm`
  command**), symptom table, the **recovery sequence that actually works** (`talosctl reboot`
  and `--mode powercycle` both hang in `stopAllPods`; `qm reset 4401`; wait for `Ready`; only
  then force-delete the orphaned `Unknown`/`Terminating` pods; expect the GC to kill one Pending
  replacement before the pool settles), a **5-minute-capped** pre-reset evidence checklist
  (`talosctl logs cri|kubelet|syslogd`, `dmesg`, `processes`, `read /run/vc/sbs/<id>/persist.json`
  — the outage is already total, do not extend it collecting evidence) and the alerts/dashboard
  row from T3/T7.
- `kubernetes/apps/infrastructure/agent-sandbox/kustomization.yaml`: rewrite the grace-period
  comment to what the flag does (any member observed NotReady after 15 m of age is replaced,
  regardless of prior readiness; the unschedulable hold does not reset the clock), and correct
  the header's "PINNED at v1.0.0" claim — the vendored file ships
  `agent-sandbox-controller:v1.0.2` (the file name is stale; renaming it is not this PR's job).
- `docs/network-plan.md` `.37` row: link the runbook.

### T5 — removed

A daily age-based rotation was considered and dropped on review: n=2 does not establish age as
the cause, the rotation would censor the only experiment that could (V6), and its safe form
needs atomic claim checks and pool-status reads that the operator does not expose. It is listed
in the follow-up plan as an option if the soak shows age-correlated freezes.

### T6 — Upstream issue (draft only; filing needs operator OK)

kubernetes-sigs/agent-sandbox: (a) the stuck-member GC deletes a member that was Ready for days on
a single NotReady observation — propose measuring the grace period from the Ready condition's
`lastTransitionTime`; (b) a member held as unschedulable past the grace period is deleted the
moment it schedules (`fsm72`, 34 s after scheduling) — the hold should reset the clock. Draft text
goes in the PR description; the operator files it.

### T7 — Outages visible on the default Grafana dashboard

`scripts/gen-reporting-dashboard.py` (the generator; the ConfigMap
`kubernetes/apps/infrastructure/monitoring/reporting-dashboard.yaml` is its output and is never
edited by hand) gets a new **first** row, "Estate Health", above "Hypervisors"; every existing
row's `y` shifts down by the new row's height (introduce one offset constant rather than editing
95 hard-coded positions). Panels, all Prometheus:

- stat **Nodes NotReady** — `count(kube_node_status_condition{condition="Ready",status="true"} == 0) or vector(0)`;
  green at 0, red ≥1.
- stat **Critical alerts** — `count(ALERTS{alertstate="firing",severity="critical"}) or vector(0)`;
  stat **Warnings** — same for `warning` (31 are firing right now, so warnings get a neutral
  colour and no red threshold; criticals and the outage-class stats are the red ones).
- stat **Flux not Ready** — `count(gotk_resource_info{ready="False"}) or vector(0)`.
- stat **Pods stuck Terminating** — `count((time() - kube_pod_deletion_timestamp) > 300) or vector(0)`.
- stat **Hypervisors Up** and **Envs Ready** — the existing expressions, duplicated here so the
  top row answers "is the estate up" without scrolling.
- state-timeline **Node readiness** — one lane per k8s node, `kube_node_status_condition{
  condition="Ready",status="true"}`, green/red mapping via the existing `state_timeline` helper;
  an outage is a red bar of its exact duration on the default 6 h range.
- table **Firing alerts** — `ALERTS{alertstate="firing"}` with `alertname`, `severity`,
  `namespace`/`node`/`instance` and `ALERTS_FOR_STATE` (since) columns, sorted critical first
  (`qtable` helper).

Render check with `scripts/dashboard-preview.py check --row "Estate Health" --shot` (the Playwright
gate the generator already documents) before committing the regenerated ConfigMap. The `dataviz`
skill is loaded before writing the panels (thresholds, colour roles, stat-tile rules).

### Out of scope (follow-up plan)

- Root cause of the guest freeze: needs `[runtime] enable_debug` + `[agent.kata] enable_debug` in a
  custom `configuration.toml` (pod annotations cannot raise the shim log level; today no agent or
  virtiofsd line reaches the host log), i.e. a `/etc/cri/conf.d/20-customization.part` runtime
  with its own `ConfigPath` via Talos `machine.files` — which requires `talos-env-node-1`'s machine
  config under tofu (`kubernetes/infra/env-pool` state was never moved out of the spike's
  scratchpad clone). Same follow-up: decide virtiofs `cache`/thread-pool, whether a second env
  node is worth the RAM to remove the pool's single point of failure, and age-based rotation if
  V6 shows age matters.
- The kubelet 1.31 deadlock under hung CRI calls is upstream; T2 removes its precondition.
- containerd restarting mid-reap (only happens on Talos upgrade/reboot) and two members hanging
  in the same minute are not exercised; with `replicas: 1` the second cannot be staged.

## Critical files

| Path | Role |
|---|---|
| `kubernetes/apps/infrastructure/testpool/sandboxtemplate-std.yaml` | T1: persistent ready-port, exec readiness, SIGTERM traps, header comment |
| `kubernetes/apps/infrastructure/testpool/env-reaper.yaml` (new) | T2: DaemonSet in `kube-system` + SA + `testpool` Role/RoleBinding + script ConfigMap |
| `kubernetes/apps/infrastructure/testpool/kustomization.yaml` | register `env-reaper.yaml` |
| `kubernetes/apps/infrastructure/monitoring/testpool-rules.yaml` | T3: four new alerts, one description update |
| `kubernetes/apps/infrastructure/agent-sandbox/kustomization.yaml` | T4: grace-period comment, version claim |
| `docs/runbooks/env-pool.md` (new) | T4: prerequisites, recovery, capped evidence checklist |
| `docs/network-plan.md` | T4: `.37` row links the runbook |
| `kubernetes/apps/infrastructure/testpool/README.md` | T1/T2: readiness contract, reaper |
| `scripts/gen-reporting-dashboard.py` | T7: "Estate Health" row, row offset |
| `kubernetes/apps/infrastructure/monitoring/reporting-dashboard.yaml` | T7: regenerated output |

## Verification

Fault injection is done from a **hack copy** of the reaper DaemonSet (same image, hostPID,
`REAP_AFTER_SECONDS=5`, applied by hand from `testpool/hack/`, never by Flux) — it is the only
thing on Talos that can signal a host process. Every step below names what it proves and what it
cannot.

- **V1 (T1, readiness):** after Flux reconciles, the old member is replaced and the new one is
  `Ready=True` within ~3 min, exec probe passing, no `Unhealthy` events for 30 min; a lease from a
  dev-worker runs `docker version`. **Injected hang:** `kubectl exec` into `control`, replace
  `/run/dsock/docker.sock` with a Unix socket that accepts and never answers (`nc -lU`), and watch:
  `docker version` in the probe blocks → `timeoutSeconds` → `Unhealthy … probe failed: command
  timed out` events → `Ready=False` after 6 failures (~60 s) → Sandbox NotReady → GC deletes it →
  teardown completes normally (the guest is not frozen) → replacement Ready. Proves the
  timeout→failure→GC path end to end. **24 h trend:** `ps | wc -l` and `/proc/sys/fs/file-nr`
  inside `control` at hour 1 and hour 24 differ by <10 (no exec leak). Cannot prove the
  non-timeout discard path; T3 covers it.
- **V2 (T1, graceful stop):** delete a Ready member → pod gone in <5 s; `talosctl logs cri` shows
  `StopContainer … with timeout 30` followed by the exit *before* any `Kill container` line. Repeat
  on a member still in its init loop (delete within 20 s of creation) — same bound.
- **V3a (T2 stage 1, against a genuinely hung teardown):** from the hack reaper pod, `kill -STOP`
  the current member's cloud-hypervisor (found by the same identity chain). The guest is now
  frozen for real: exec probe times out, member goes NotReady, GC deletes it, `StopContainer`
  hangs (`Terminating` > 60 s, `stop_container` errors increment, `TestpoolEnvTeardownStuck`
  reaches pending/firing if held ≥5 min — hold it, this is V4's firing fixture). Then let stage 1
  fire (SIGKILL on a stopped process works). Pass: the pod is gone within 60 s of the reap line,
  and **residuals are gone**: no CH/virtiofsd/shim process for the sandbox in `talosctl
  processes`, `/run/vc/sbs/<id>` and `/run/kata-containers/shared/sandboxes/<id>` absent, the
  iSCSI session count back to baseline, the PV `Released` then deleted by Trident, the
  replacement Ready, `stop_*` error rate back to 0 and the alert resolves (V4's recovery fixture).
- **V3b (T2 stage 2):** same, with stage 1 disabled in the hack copy (`REAP_STAGE1=0`) so the shim
  kill is exercised alone; same residual checks. If stage 1 alone never completes cleanup, the
  Flux-owned reaper folds stage 1's delay into stage 2.
- **V4 (T3):** `scripts/rules-lint.sh` (promtool, the CI gate); each expression returns the env
  node's series on the live Prometheus; firing and recovery observed during V3a for
  `TestpoolEnvTeardownStuck` and `EnvNodeRuntimeStopErrors`; `EnvNodeKubeletMetricsBlocked` is
  checked against the 03:11 window with a range query (it cannot be reproduced safely).
- **V5 (T7):** `python scripts/gen-reporting-dashboard.py` is idempotent (second run: no diff);
  `dashboard-preview.py check --row "Estate Health"` renders; the state-timeline shows the red bar
  for `talos-env-node-1` 09:29–12:38 when the range covers today; the alerts table lists the live
  set critical-first. Existing rows unchanged except `y`.
- **V6 (soak, evidence not proof):** **7 days** with the pool at `1/1`, node `Ready`, none of the
  T3 alerts firing, and the member allowed to age past the 24 h and 66 h marks at which the two
  freezes occurred. A freeze during the soak is the *desired* outcome for the follow-up plan: it
  must show up as a 2-minute blip (reaper log line, `TestpoolEnvTeardownStuck` never firing) and
  the node must stay `Ready`.
- `scripts/manifest-lint.sh` (kustomize build + kubeconform, the CI gate) passes; docs exercised
  against the steps that were actually run today.



## Implementation notes (deviations from the finalized plan, with the evidence that forced them)

- **T1's exec readiness probe was REJECTED by V1's injected stall and replaced.** With the member's
  virtiofsd frozen (`kill -STOP` from the reaper pod — the faithful injection, see below) the pod
  stayed `Ready=True` for 7+ minutes: the first probe exec hung, every later one failed fast with
  `cannot enter container` (a non-timeout CRI error the kubelet 1.31 prober discards), and the only
  event was "Readiness probe **errored**". Codex's round-1 concern was right and the plan's "both
  stalls surface as timeouts" claim was wrong. Shipped instead: `ready-watchdog.yaml`, a stdlib
  Python process in `control` that holds :9099 open only while a virtio-fs write+fsync on `/work`
  and a dockerd `/_ping` each answer within 5 s (a hung check closes the port at once), with BOTH
  probes back on TCP — answered by the kubelet itself, no CRI path to discard. Verified: the hang
  path in isolation (port open → hung check → port closed, exit 1) and, live, a member built from
  the new template becoming Ready via the watchdog (startup 112 s) and going NotReady 43 s after a
  virtiofsd freeze.
- **SIGSTOP on cloud-hypervisor is NOT the incident's hang** (Codex asked). Kata's monitor pings
  the VMM API (10 s) and the agent; an unresponsive VMM is declared dead in ~13 s, the shim aborts
  every pending wait (`ttrpc: closed`) and containerd completes the stop — the pod was gone in
  <30 s. That IS the mechanism stage 1 relies on, demonstrated. The faithful injection is
  `kill -STOP` on the sandbox's **virtiofsd** processes: guest processes block in D-state on the
  rootfs while the agent keeps answering — the incident's signature (`kubectl exec` hangs outright).
- **V3 against the faithful hang passed twice**: a virtiofsd-frozen member deleted → `Terminating`
  8.5 min with `stop_*` errors climbing and `EnvNodeRuntimeStopErrors` pending → three
  `reap stage=1` lines (clh + both virtiofsd) → pod gone within 30 s, zero residual processes,
  `/run/vc/sbs/<id>` and the shared dir gone, PV reclaimed, replacement Ready. Repeated at the
  production setting (120 s): reaped at ~+180 s. Stage 2 was never needed and is untested.
- In one virtiofsd-freeze run Kata's agent health check itself timed out after 34 s, the shim
  marked both containers exited (255) and the kubelet restarted them in a fresh sandbox — a
  self-heal path that exists for SOME stall shapes; the incident's did not take it. Recorded for
  the root-cause follow-up.
- **`kubectl --request-timeout` disables in-cluster config** (the reaper dialled localhost:8080
  on every iteration until the flag was dropped). The outer `timeout` is the bound.
- Kata process placement differs from the plan's assumption: only cloud-hypervisor is in the pod
  cgroup; virtiofsd and the shim live in `/kata_overhead/<sandbox-id>`. The reaper derives the
  sandbox id from the VMM's cgroup (or `persist.json` once the VMM is dead) and matches the others
  by that id.
- `TestpoolEnvTeardownStuck` could not be observed firing pre-merge: Flux's drift correction
  reverted the hand-applied `prometheusrules/testpool` within its 10 m interval. The expression
  evaluated live returned the stuck pod (`357 > 300`), and the promtool fixtures cover firing,
  recovery and the negative case; the first real firing will be post-merge.
- V6 (7-day soak) is deliberately still open at merge time.

<!-- codex-review-status: finalized -->
