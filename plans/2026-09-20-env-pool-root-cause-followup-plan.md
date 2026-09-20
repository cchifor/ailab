# env-pool follow-up: root-cause the guest freeze, close the pool SPOF, file upstream, run V6

## Codex Review

- The adoption precedents, debug-first approach, and faithful virtiofsd fault injection are sound and incorporate the previous plan’s implementation findings.
- Adoption needs a complete machine-config comparison: `resolved_apply_mode = auto` proves no reboot is required, not that nothing changes. The new local state also needs a durable home and protected backups.
- Evidence collection currently misses the Talos host debug logs central to this investigation. Document complete, retention-aware queries first; a new soak script is justified if it reliably exports evidence and detects gaps.
- The second node needs a host-memory budget that includes the existing lazy-loaded model, explicit scheduling behavior during claims and node loss, and verification of storage and runtime operation.
- Gates need clearer commit/apply/merge ordering, explicit fault-injection scope, and concrete rollback steps. The soak clock must account for T4’s template rotation and guest restarts as well as the reboot.

Repo `ailab` · new branch `ops/env-pool-root-cause-followup` off fresh `gitea/main` (in a worktree) ·
this file: `plans/2026-09-20-env-pool-root-cause-followup-plan.md` (approved by the operator on
2026-09-20) · prior plan: `plans/2026-09-20-env-pool-frozen-guest-outage-plan.md` (PR **#800, merged
2026-09-20 15:09 UTC** = soak day 0).

## Context

PR #800 shipped the *prevention* for the 2026-09-20 outage (in-guest `ready-watchdog`, the
`env-reaper` that bounds a Kata teardown to ~2 min, precursor alerts, runbook). Its follow-up section
left four things open; this plan is those four:

1. **Root cause of the guest freeze is unknown.** Single-threaded virtiofsd (`--thread-pool-size=1`,
   `cache=auto`) is the suspect, not proven. Today *no* kata-agent, cloud-hypervisor or guest-console
   line reaches the host log, so a recurrence teaches nothing. Kata debug needs a custom
   `configuration.toml`, which needs `talos-env-node-1`'s machine config under tofu.
2. **The pool is a single point of failure** (one Talos worker, one warm member, one host).
3. **T6 upstream issue** (agent-sandbox warm-pool GC deletes a days-Ready member on one NotReady
   observation) was never drafted beyond two sentences; the operator wants text to file.
4. **V6, the 7-day soak**, is running: the member is deliberately allowed past the 24 h / 66 h marks
   at which the two freezes happened. A recurrence must now appear as a ~2-min blip with a
   `reap stage=1` line, not an outage — and it is only *useful* if it is captured with evidence.

<!-- codex: Separate freeze-to-NotReady, delete-to-reap, and refill latency: env-reaper.yaml measures its 120 s threshold from deletionTimestamp, adds polling delay, and documents roughly 150 s from the deletion request before cleanup and refill. The prior implementation also observed self-healing without a reap, so neither a two-minute total capacity gap nor a mandatory stage-1 line is a valid universal acceptance criterion. -->

### What exploration established (verified live today, 2026-09-20)

- **The spike's tofu state is lost, not misplaced.** `kubernetes/infra/env-pool/` has the `.tf`
  files (tracked) but no state; the scratchpad clone recorded in `spike/mk_kubeconfig.py:4` survives
  only as empty directories, and a content search of every JSON-like file under
  `%LOCALAPPDATA%\Temp\claude`, `~/.claude/jobs`, `~/work/home` for a state naming vmid 4401 found
  nothing. The 09-07 plan's W6 precondition ("handover, never a second state") is therefore moot:
  this is a fresh **adoption** (runners precedent, `plans/2026-09-07-estate-anomalies-plan.md:342-398`).
- **Talos delivery mechanics** (`siderolabs/talos` v1.11.2 `v1alpha1_sequencer_tasks.go` WriteUserFiles):
  `machine.files` `op: create` is allowed only under `/var/…` **or** at exactly
  `/etc/cri/conf.d/20-customization.part` (injected as the CRI customization part and rendered into
  `cri.toml`); files are written in the boot sequence → **one node reboot** applies them.
- **Live node layout** (`talosctl read`): `10-kata-containers.part` registers runtime `kata` with
  `options.ConfigPath = "/usr/local/share/kata-containers/configuration.toml"` (read-only extension
  squashfs); `20-customization.part` exists and is **empty**. The live Kata config (469 lines,
  `[hypervisor.clh]`/`[agent.kata]`/`[runtime]`) has every `enable_debug` commented out,
  `virtio_fs_extra_args = ["--thread-pool-size=1", "--announce-submounts"]`, `virtio_fs_cache = "auto"`,
  `virtio_fs_cache_size = 0`, `enable_annotations = ["enable_iommu", "virtio_fs_extra_args", "kernel_params"]`.
- **Kata 3.20 loads drop-ins** `<dir-of-ConfigPath>/config.d/*.toml` on top of the main file
  (`katautils/config.go` `decodeDropIns`), so the debug delta can be a 10-line drop-in next to a
  verbatim copy of the extension's file. The `virtio_fs_extra_args` pod annotation is **appended**
  to the config list (`oci/utils.go:883-889`) — `--thread-pool-size` would appear twice — so the
  thread-pool experiment also goes through a drop-in, not an annotation.
- **Upstream** `kubernetes-sigs/agent-sandbox` `main` (post v1.0.3, 2026-09-17,
  `extensions/controllers/sandboxwarmpool_controller.go:519-566`) still measures the stuck-member age
  from `sb.CreationTimestamp` with no look at the Ready condition, and the unschedulable hold
  (#1215) does not reset that clock. No open issue covers this (#1274 only made the period
  configurable). T6 is still novel and still accurate.
- **Soak baseline**: node `Ready` (19 d), pool `1/1`, member `env-std-pool-htxxt` created ~15:10 UTC
  today (the #800 template rotation), reaper started 15:10:26Z, **0** `reap stage=` lines. Loki
  retention is **exactly 168 h** — soak-day-1 log lines expire on day 8, so evidence is pulled
  during the soak, not after it.
- **Host RAM now** (`free -g`, `available`): ai-node1 25 GiB, ai-node2 22 GiB, ai-node3 26 GiB; the
  Kata image `local:import/talos-v1.11.2-agent-nocloud-amd64.raw` is staged on all three. A second
  16 GiB fixed env node fits on **ai-node3** at the same ~10 GiB margin the first node was accepted
  with (`main.tf:45-49`).

<!-- codex: The snapshot is accepted, but the capacity conclusion conflicts with kubernetes/infra/ai-lxc/models.yaml:109-128: ai-node3's reclaimed headroom funds a lazy 22.45 GiB qwen3.8 model load. Before G3, establish whether that model was resident during this measurement and demonstrate sufficient headroom under model load and representative CI demand; the same RAM cannot fund both reservations. -->

- **Codex review path works**: the previous plan's round-2 dispatch died on the native seat's usage
  limit ("try again at Sep 23rd"), but all three profiles now run `gpt-6-astra-realjaynesage`
  through LiteLLM (`~/.codex/config.toml`, key from the cluster's `litellm-secret`); a read-only
  `codex exec -p review` ping answered today. The stale `codex-20260920-161056` worktree/branch from
  that failed dispatch is removed in G0.

<!-- codex: G0 lists worktree creation and review, not deletion; keep stale-worktree cleanup separate and verify its owner, uncommitted work, and ignored state before removing it. In particular, do not repeat the state-loss problem this adoption repairs. -->

## Approach

Five tasks, ordered so that the reboot happens **once**, as early as possible, and every mutation
has its own operator gate. T1 → T2 (+ T3's script, T5's text) are one PR; T3 is passive evidence
collection with check-ins; T4 (second node, approved) is a separate PR after T1/T2's no-op plan.

<!-- codex: Name a T1-only commit and require its full no-op verification before applying a separate T2 commit; applying the combined PR head during G1 would already include the debug configuration. Record the exact commit and reviewed plan for each manual apply, since merging infra files does not execute these checkpoints. -->

### T1 — Adopt `talos-env-node-1` into `kubernetes/infra/env-pool` (state only, no node change)

Files: `kubernetes/infra/env-pool/{imports.tf (new), main.tf, talos.tf, terraform.tfvars.example (new)}`,
`justfile`, `docs/runbooks/env-pool.md:5-7`, `docs/network-plan.md:45,62-72`.

- `imports.tf`: `import { to = proxmox_virtual_environment_vm.env["env-node-1"]  id = "ai-node2/4401" }`
  — the committed, reviewable form (`kubernetes/infra/cloudflare/imports.tf` precedent; inert after
  the first apply). `main.tf` `lifecycle.ignore_changes` gains `disk[0].import_from` (create-only,
  never read back — `runners/main.tf:107`); any other post-import drift is **reconciled in the .tf**,
  not ignored, unless the attribute is ForceNew (then it goes to `ignore_changes` with a comment).
  Live `qm config 4401` (read today) matches `main.tf` field-for-field — cores 8, `cpu: host`,
  16384 MiB `balloon: 0`, `virtio-scsi-single`, scsi0 60G `discard=on,iothread=1`, `serial0: socket`,
  pool `ailab`, tags `env-pool;k8s;talos;worker`, agent enabled, `onboot: 1` — so the expected
  post-import diff is `import_from` plus, at most, tag ordering (PVE stores tags sorted; reconcile
  the list order in `main.tf` rather than ignoring it).

<!-- codex: ForceNew alone is not justification for ignoring a mismatch: reconcile the declared value to the verified live identity, and document any genuinely create-only exception individually. G1 must reject every VM update or replacement before apply, including in-place changes that could reboot it; separately verify initialization/ipconfig0 because the existing ignore rule hides that drift. -->

- `terraform.tfvars.example` documents the four required inputs (`pve_endpoint`, `pve_api_token`,
  absolute `infra_state_path`, absolute `kubeconfig_path`); the real `terraform.tfvars` and the
  state stay gitignored (state is sensitive: it embeds the Talos client cert).

<!-- codex: Choose the authoritative env-pool state location before adoption: backend.tf currently uses checkout-relative local state, so another worktree would otherwise create another state. Document protected backups, exclusive apply ownership, and any verified transfer before worktree removal; update backend.tf's obsolete handover instruction. -->

<!-- codex: The sensitivity includes private keys and cluster machine secrets cached through terraform_remote_state, not merely a client certificate. Protect state, backups, saved plans, and rendered machine-config dumps with restricted local access; commit only redacted summaries, and never commit _out/. -->

- `talos_machine_configuration_apply.worker` cannot be imported; it is **created** with the
  *unchanged* template. `apply_mode` becomes a per-node field of `env_nodes`
  (`optional(string, "auto")`; provider 0.11.0 values `auto | reboot | no_reboot | staged |
  staged_if_needing_reboot`), set to **`staged_if_needing_reboot`** for `env-node-1`: the provider
  dry-runs the apply and only applies live when no reboot is needed, otherwise it stages. The
  resource's `resolved_apply_mode` is the evidence, and it is computed **at plan time** (the
  provider dry-runs against the node): `auto` = the generated config matched the running one
  (expected: the node was built from this exact template + provider 0.11.0 + the same secrets on
  09-01); `staged` + a "Reboot prevented" warning = something differs → **stop before any reboot**
  and diff `talosctl get machineconfig -o yaml` against the rendered template. Caveat from the
  provider source: if the dry-run itself fails it falls back to `auto` with a "Cannot check reboot
  requirement … may reboot" warning — G1's rule is *apply only when the plan shows `auto` and no
  warning*. Before the apply the same diff is done by hand on
  `nodeLabels/nodeTaints/kernel.modules/network/registries`.

<!-- codex: Blocking: resolved auto establishes only that no reboot is required; live-applicable differences can still change the node. Compare the complete generated-and-patched machine configuration against the running configuration, accounting explicitly for defaults and redaction, rather than comparing only these five sections. -->

<!-- codex: Retain the committed lockfile's Talos 0.11.0 during adoption, reject an unknown resolved mode or failed dry-run, and repeat the check if the reviewed plan becomes stale. An unreadable node must never turn the provider's fallback to auto into permission to apply. -->

  `kubernetes_labels.env_pool` and `kubernetes_node_taint.env` adopt the existing label/taint via
  SSA (the taint already has `field_manager = "env-pool-tofu"`, `force = true`; the label gets
  `force = true` too if the first plan reports a manager conflict).

<!-- codex: SSA ownership conflicts may surface only during apply; inspect managedFields before taking ownership and limit any force operation to the intended label or taint. Also pin or verify the Kubernetes provider's admin@ai context: node-labels.tf currently sets only config_path, so supplying a merged kubeconfig can select its unrelated default context. -->

- `just env-pool-plan` / `env-pool-apply` mirror `agent-nodes-plan/apply` (`justfile:91-95`) incl. the
  `nested-virt-verify` gate.
- Docs: runbook lines 5-7 and the IPAM `.37` row lose the "state never moved / unmanaged" claims
  **only in the commit that follows a green no-op plan**, with the plan output quoted in the commit.

Exit criterion: full non-targeted `tofu plan` = `No changes.`; the VM is never destroyed/replaced;
the node never reboots; `kubectl get node talos-env-node-1 --show-labels` unchanged.

### T2 — Kata debug logging via `machine.files` (one reboot)

Files: `kubernetes/infra/env-pool/machine-config/{worker.yaml.tftpl, kata/configuration.toml (new,
verbatim), kata/config.d/10-debug.toml (new), cri-20-customization.part (new)}`, `talos.tf`,
`docs/runbooks/env-pool.md`.

- `kata/configuration.toml` = byte-for-byte copy of the extension's file (sha256 recorded in a
  header comment of the drop-in and re-checked in V2 against `talosctl read
  /usr/local/share/kata-containers/configuration.toml` — if the extension is ever upgraded the
  checksum check fails loudly instead of silently running a stale base).

<!-- codex: A checksum comment and a one-time V2 check do not fail automatically on a later extension upgrade. Make this comparison an explicit upgrade precondition and record the extension/image identity alongside the hash, without adding a general configuration-management framework. -->

- `kata/config.d/10-debug.toml`:
  `[hypervisor.clh] enable_debug = true` (cloud-hypervisor log **and the guest serial console** →
  `vmconsole` lines: kernel hung-task traces are the smoking gun for a D-state on virtio-fs),
  `[agent.kata] enable_debug = true` (agent lines forwarded to the shim log),
  `[runtime] enable_debug = true` (shim debug). `debug_console_enabled` stays off (needs
  `kata-runtime exec`, not present on Talos).

<!-- codex: Kata debug output can expose workload commands, environment values, and mount details; inspect the emitted fields before leaving it enabled for real leases. Keep raw evidence in protected local storage and publish only reviewed, redacted excerpts, since this repository is mirrored publicly. -->

- `cri-20-customization.part`: overrides **only** `ConfigPath` for the existing `kata` runtime →
  `/var/etc/kata-containers/configuration.toml`. Verified in Talos source (`internal/pkg/toml/merge.go`
  + `pkg/machinery/config/merge`): parts are sorted by name, decoded to maps and merged with
  "right replaces left unless zero value", so the later part's non-empty `ConfigPath` wins and no
  other `kata` key is touched. V2 still reads the rendered `cri.toml` to prove it.
  The agent-nodes pool is untouched (separate module/template); `runtimeClassName: kata-env`
  keeps handler `kata`, so no k8s object changes and no template-hash rotation from this task.

<!-- codex: Preserve the shared RuntimeClass handler and compare the complete rendered kata runtime entry, including runtime_type, before and after; the override affects every pod using handler kata on these env nodes. Verify a new sandbox actually runs the Kata guest rather than treating ConfigPath text alone as proof. -->

- `worker.yaml.tftpl` gets three `machine.files` entries (`op: create`, `permissions: 0o644`);
  content is passed in from `talos.tf` via `templatefile` vars (`file()` on module-relative paths,
  `indent()` for the YAML block scalars). The same template will serve env-node-2, so both nodes
  run identical Kata settings by construction.

<!-- codex: Validate the rendered YAML and TOML, numeric permissions, exact paths, and content read back after boot; block-scalar indentation and newline handling can invalidate the promised byte-for-byte copy. Keep these world-readable files configuration-only, with no credentials, and remember a future 20-virtiofs.toml needs its own delivery entry rather than merely a new repository file. -->

- Apply = the same `staged_if_needing_reboot` from T1: `machine.files` needs a reboot, so the
  provider **stages** the new config (`resolved_apply_mode = staged`) without touching the running
  system, and the reboot is a separate, explicitly approved `talosctl reboot -n 192.168.0.37` at
  the operator's window. Pre-checks: no `SandboxClaim` in `testpool`, no Terminating pods, node
  Ready. Cost: one member rotation (~3 min warm gap) — **and the V6 age clock restarts** (see
  Gates). `env-node-2` (T4) keeps `auto`, which a fresh node's first apply needs.

<!-- codex: An empty claim list is a point-in-time check, not a maintenance lock: quiesce new lease acquisition for the window and verify all affected workloads before rebooting. Cordon alone does not prevent adoption of an existing warm member; specify how acquisition resumes after recovery. -->

<!-- codex: Verify the expected staged result and pending configuration before reboot, then verify the running configuration afterward; a no-op tofu plan alone cannot prove staged files were activated. Use the matching talosctl-1112.exe and define a bounded failure path if shutdown stalls, rather than assuming the previous stopAllPods failure is impossible. -->

- Rollback: delete the three `machine.files` entries, apply, reboot; the node returns to the
  extension defaults with nothing left behind except files under `/var`.

<!-- codex: Verify rollback clears the special CRI customization and restores the extension ConfigPath; removing machine.files entries is not a general deletion mechanism for persistent /var files. Document how stale config.d drop-ins are neutralized before that directory is reused, or a later rollout can silently reactivate an old experiment. -->

- Runbook additions: "what the debug log contains and how to pull it on a blip"
  (`talosctl logs cri | grep <sandbox-id>` for shim/agent lines, `vmconsole` for the guest kernel,
  the reaper's `sandbox=<id>` field is the join key), the log-growth figure measured in V2, and
  "changing Kata settings = edit a drop-in, `tofu apply`, reboot".
- Deliberately **not** changed yet: `virtio_fs_extra_args` / `virtio_fs_cache`. Changing the suspect
  before capturing a recurrence would censor the only experiment that can name the cause
  (same reasoning that removed T5 in the previous plan). The second drop-in
  (`config.d/20-virtiofs.toml`) is written **when V6 produces evidence**, as a follow-up commit.

### T3 — V6 soak with evidence capture

Files: `scripts/env-pool-soak.py` (new, read-only), `docs/runbooks/env-pool.md`, this plan's
`## Soak record` (appended per check-in).

<!-- codex: For three scheduled check-ins, documented queries plus a fixed report template are sufficient unless the script adds reliable interval export, pagination, and explicit incomplete-data reporting. Keep any implementation a small read-only helper using existing endpoints, without adding an agent, controller, or new monitoring service. -->

<!-- codex: The referenced Soak record section is currently absent; add a day-0 record with UTC timestamps, commit/config hashes, node boot identity, member and pod UIDs, sandbox ID, and planned check-in owner. Those identities are needed to distinguish continuous aging from replacement or restart. -->

- The script prints one markdown block per run, from Prometheus (port-forward, CLAUDE.md recipe)
  and Loki (LAN NodePort `30310`, `monitoring/loki-lan.yaml`): node Ready timeline and any NotReady
  intervals (`kube_node_status_condition{node=~"talos-env-node-.*"}`), current member creation
  time + age and the max age reached (`kube_pod_created` × `kube_pod_info{created_by_kind="Sandbox"}`),
  firing/pending `ALERTS{alertname=~"Testpool.*|EnvNode.*"}`, `increase(kubelet_runtime_operations_errors_total{
  node=~"talos-env-node-.*",operation_type=~"stop_.*"})`, the reaper's `reap stage=`/`api error`
  lines (`{namespace="kube-system", app="env-reaper"}`) and the watchdog's `check hung|failed|
  ready-port closed` lines (`{namespace="testpool", container="control"}`). Nothing writes.

<!-- codex: Blocking evidence gap: monitoring/alloy.yaml collects /var/log/pods, not the Talos cri service log, and this report never exports the shim/agent/vmconsole evidence T2 enables. Establish durable host-log capture before V2/day 0, prove it retains the injected incident through teardown, and collect node/time context as well as sandbox-filtered lines. -->

<!-- codex: Supply executable queries: select condition="Ready", status="true", scope pod series to testpool with explicit vector matching, and give increase() a range selector. Query historical alerts and readiness with a stated resolution, include kubelet up and reaper heartbeats, and report missing targets or failed queries as incomplete evidence rather than zero failures. -->

<!-- codex: Sandbox-owned pod age does not distinguish warm from leased members and does not reset when a guest sandbox restarts inside the same Pod. Track warm-pool membership plus guest/sandbox restarts, and compute historical maximum age at each sample time rather than subtracting old creation timestamps from the final query time. -->

- Check-ins at **day 1, day 3 (past 66 h), day 7**, each appended to the soak record with the
  verdict. Because Loki keeps 168 h, every check-in also pastes the raw reaper/watchdog lines.

<!-- codex: The demonstrated --since 24h leaves gaps between day 1, day 3, and day 7; export from the last successful checkpoint with overlap, explicit UTC bounds, and pagination or truncation detection. Retention is rolling from each event, so secure day-0 evidence before its exact 168 h boundary and retain only redacted excerpts in this plan. -->

<!-- codex: Check actual Prometheus history too: kube-prometheus-stack.yaml documents a binding retentionSize cap despite retention: 15d. Persist the needed metric evidence at check-ins and mark intervals outside retained or successfully scraped history as unknown. -->

- **Day-7 decision tree** (recorded in the plan, decided by the operator):
  - recurrence captured → root-cause section written from the debug log + watchdog discriminator
    (`virtiofs check hung` vs `dockerd check hung`); if virtio-fs → `20-virtiofs.toml`
    (`--thread-pool-size=<N>` first, `cache` mode second, one variable at a time), reboot, second
    7-day soak with the same script;

<!-- codex: A blocked virtio-fs check or guest D-state identifies a failure path, not the specific cause of a single-thread deadlock or cache bug. Preserve guest stacks and available host virtiofsd state before cleanup, label any thread-count change as a hypothesis test, and replace the complete argument array while preserving --announce-submounts. -->

  - recurrence *not* captured as a blip (alert fired / node NotReady) → the prevention is the bug;
    stop and re-plan;

<!-- codex: Make prevention failure take precedence even when the recurrence was captured, and invoke the existing recovery runbook immediately rather than waiting for day 7. Distinguish failed containment from an evidence-capture failure and from the already-observed self-healing path. -->

  - no recurrence → keep debug on, extend the soak; age-based rotation stays rejected (no evidence
    that age matters).

### T4 — Second env node `env-node-2` (approved 2026-09-20; separate PR after T1/T2's no-op plan)

Files: `kubernetes/infra/env-pool/variables.tf:106-120`, `docs/network-plan.md` (`.38` allocated,
free list), `CLAUDE.md` inventory row, `scripts/gen-reporting-dashboard.py:563` (`ENVNODE` →
`instance=~"192.168.0.3[78]:9100"`, regenerated ConfigMap + `dashboard-preview.py check --row "Test
Env Pool"`), `kubernetes/apps/infrastructure/testpool/sandboxtemplate-std.yaml` (pool `replicas: 2`
+ `topologySpreadConstraints` on `app: env-std` / `kubernetes.io/hostname`, `DoNotSchedule` — the
podTemplate is a full PodSpec, the CRD accepts it), `docs/runbooks/env-pool.md` (two nodes).

<!-- codex: Adding topologySpreadConstraints changes the SandboxTemplate hash and rotates existing warm members, resetting their age after T2's stated day 0. Either complete T4 before starting the definitive soak or record separate epochs and require the final cohort to pass the age milestones. -->

<!-- codex: Provision and validate env-node-2 from the reviewed infra commit before merging the Flux template/replica change; merging the combined PR first can rotate the only warm member while the second node is absent. Record the mirrored Flux revision actually reconciled before evaluating placement. -->

- `env-node-2 = { node_name = "ai-node3", vm_id = 4402, ip = "192.168.0.38", host_ip = "192.168.0.4",
  hostname = "env-node-2" }` — a pure create in the existing `for_each`, which also exercises the
  module's create path that T1's import cannot. Expect the spike's first-apply race
  (label/taint `node not found`; second apply converges — `SPIKE-REPORT.md:24-25`).

<!-- codex: Recheck .38 and cluster-wide VMID 4402 availability at execution time, including IPAM and the shared-LAN checks required by docs/network-plan.md. Keep the new node unavailable for workloads until the authoritative taint and label, CNI/CSI, Kata runtime, and reaper are ready; a failed first apply otherwise leaves an untainted scheduling window. -->

- What it buys: the pool survives a wedge, a Talos upgrade, or ai-node2 maintenance; the reaper,
  prepull and alloy DaemonSets land automatically (they select the label). What it costs: 16 GiB
  fixed + 8 vCPU on ai-node3 (26 GiB available today → ~10 GiB), a second 60 GiB iSCSI LUN, and
  doubled warm-member footprint. Namespace quota (`pods: 6`, `requests.memory 24Gi`) fits two members.

<!-- codex: Namespace quota proves admission headroom, not host capacity or storage availability: include active leases, refill/Terminating overlap, RuntimeClass overhead, current PVC/storage quota usage, and the separate 60 GiB local VM disk. The 16 GiB node must also tolerate the intended concurrently active workload limits, not merely two parked members' requests. -->

<!-- codex: Scope the availability claim to retaining usable warm capacity after one worker/host failure; existing leases on that host are not migrated and QNAP remains shared. Node 3 uses a different storage-fabric path, so validate snapshot restore and block-volume operation there before claiming maintenance tolerance. -->

- Placement is `RuntimeClass kata-env.scheduling` (label + toleration) — nothing else pins members;
  without the spread constraint both members could land on one node.

<!-- codex: Specify maxSkew and the eligible-domain policy, including nodeTaintsPolicy and any minDomains setting; DoNotSchedule alone does not define behavior when a node becomes unavailable. Validate both normal two-node spread and the chosen degraded refill behavior without accidentally requiring an unavailable second domain forever. -->

<!-- codex: app=env-std counts leased pods as well as warm members, so balanced total pods can still leave both warm members on one node after claims and refills. Exercise that sequence and either establish a controller-supported way to spread warm capacity or narrow the promised guarantee; an initial one-per-node snapshot is insufficient. -->

### T5 — Upstream issue draft (operator files it)

Text in **Appendix A** below and in the PR description; once filed, the issue URL goes into the
comment at `kubernetes/apps/infrastructure/agent-sandbox/kustomization.yaml:15-25` (the same
comment gets its stale "60 s of sustained failure" corrected to "~30 s" — the shipped TCP probe is
5 s × 6).

### Gates (each needs the operator's explicit OK for exactly this scope)

<!-- codex: Record which existing operator decisions already satisfy each gate instead of requesting the same approval again for reversible preparation or read-only review. The missing operational scope is V2's deliberate freeze and lease smoke test, plus any emergency reset or additional reboot needed for rollback or log-level changes. -->

| Gate | Mutation | Scope / commands | Rollback |
|---|---|---|---|
| G0 | branch + plan file + codex review | `git worktree add` off `gitea/main`; `plans/…-plan.md`; `codex exec -s read-only` | none |
| G1 | T1 adoption | `tofu -chdir=kubernetes/infra/env-pool init/plan/apply` (Windows `~/.tofubin/tofu.exe`) — writes local state, one SSA label/taint on `talos-env-node-1` via `admin@ai`, one `staged_if_needing_reboot` config apply expected to resolve `auto` with no reboot | `tofu state rm`; nothing on the node changed |
| G2 | T2 config + reboot | same module; the apply resolves `staged` (no live change), then `talosctl reboot -n 192.168.0.37` once, in a window agreed by the operator (pool idle) | remove `machine.files`, apply (stages), reboot |
| G3 | T4 second node | `tofu apply` creates vmid 4402 on ai-node3 (.38); Flux applies `replicas: 2` | `tofu destroy -target`, revert manifests |
| — | Flux-applied manifests (T4, kustomization comment) | merge to `main` on Gitea; nothing applied by hand | revert PR |

<!-- codex: G1's state rm only forgets ownership; it cannot undo a live or staged machine-config change or restore SSA ownership, and the retained import block can immediately adopt the VM again. Preserve the state backup and diagnose partial apply results before choosing an explicitly addressed state operation. -->

<!-- codex: G3 needs an ordered rollback: stop new acquisitions, reconcile the reduced pool/template, drain env-node-2 and verify its volume cleanup, then review removal of that node's complete resource set. Prefer removing its map entry and inspecting a full plan over an unspecified targeted destroy; account for broad depends_on edges in talos.tf and node-labels.tf, and confirm node 1 remains unchanged. -->

**Operator decisions taken 2026-09-20:** (1) **reboot now** — G2 follows G1 as soon as the PR's
codex round converges; the V6 age clock restarts from the reboot (24 h / 66 h marks re-reached
~2 and ~3 days later, day 7 ≈ reboot + 7 d), so a recurrence is caught *with* evidence;
(2) **second node: yes**, as its own PR after T1/T2 have a green no-op plan; (3) codex review runs
through LiteLLM (verified) — no login change needed.

<!-- codex: Use the final replacement guest's actual start time after injection, lease verification, and any T4 rotation, not the reboot time; 24 h is one day and 66 h is two days eighteen hours from that baseline. Record every subsequent replacement or guest restart and report per-member exposure alongside the overall observation window. -->

## Critical files

| Path | Role |
|---|---|
| `kubernetes/infra/env-pool/imports.tf` (new) | T1: adopt vmid 4401 (`ai-node2/4401`) |
| `kubernetes/infra/env-pool/main.tf` | T1: `ignore_changes += disk[0].import_from`; reconcile post-import drift |
| `kubernetes/infra/env-pool/talos.tf` | T1: `apply_mode`; T2: pass Kata file contents into the template |
| `kubernetes/infra/env-pool/terraform.tfvars.example` (new) | T1: the four required inputs |
| `kubernetes/infra/env-pool/machine-config/worker.yaml.tftpl` | T2: three `machine.files` entries |
| `kubernetes/infra/env-pool/machine-config/kata/configuration.toml` (new) | T2: verbatim extension copy (sha256 pinned) |
| `kubernetes/infra/env-pool/machine-config/kata/config.d/10-debug.toml` (new) | T2: the three `enable_debug` toggles |
| `kubernetes/infra/env-pool/machine-config/cri-20-customization.part` (new) | T2: `kata` runtime `ConfigPath` override |
| `kubernetes/infra/env-pool/variables.tf` | T1: per-node `apply_mode` field; T4: `env-node-2` entry |
| `justfile` | T1: `env-pool-plan/apply` |
| `scripts/env-pool-soak.py` (new) | T3: read-only soak report |
| `docs/runbooks/env-pool.md` | T1 state claim, T2 debug-log recipe, T4 two nodes |
| `docs/network-plan.md`, `CLAUDE.md` | T1 `.37` managed; T4 `.38` allocated |
| `kubernetes/apps/infrastructure/testpool/sandboxtemplate-std.yaml` | T4: `replicas: 2`, spread constraint |
| `scripts/gen-reporting-dashboard.py` + `monitoring/reporting-dashboard.yaml` | T4: `ENVNODE` covers `.38` |
| `kubernetes/apps/infrastructure/agent-sandbox/kustomization.yaml` | T5: issue link; "60 s" → "~30 s" |
| `plans/2026-09-20-env-pool-root-cause-followup-plan.md` | this plan + soak record + Appendix A |

<!-- codex: Include backend.tf and node-labels.tf in the review surface for the state-location and provider-context changes above. CLAUDE.md's unmanaged .37 inventory claim should be corrected at T1's successful handover rather than waiting for T4. -->

Reused, not rewritten: `env-reaper.yaml` (its `sandbox=<id>` log field is the join key into the
debug log), `ready-watchdog.yaml` (its `virtiofs`/`dockerd` discriminator), `testpool-rules.yaml`
+ `env-node-rules.yaml` (soak alert set), `scripts/dashboard-preview.py` (T4 render gate),
`scripts/manifest-lint.sh` / `rules-lint.sh` (CI gates — note **no CI gate covers
`kubernetes/infra/**/*.tf`**; `tofu fmt -check` + `tofu validate` are run locally before every
commit, per `feedback_terraform_fmt`).

<!-- codex: Carry forward the prior implementation's explicit limits: stage 2 remains untested, as do containerd restart during reap and simultaneous hangs. Reusing the DaemonSet on two nodes is reasonable, but it does not establish these stronger guarantees; retain the namespace boundary, restricted RBAC, and sandbox identity checks. -->

## Verification

- **V1 (T1):** after `apply`, a full non-targeted `tofu plan` prints `No changes.`; `tofu state list`
  shows the VM, the config-apply, the label and the taint; `qm config 4401` on ai-node2 unchanged
  (diff before/after); node uptime unchanged (`talosctl get … uptime` / `kubectl get node` age); the
  live machine config sections diffed against the rendered template before the apply were identical.

<!-- codex: Kubernetes Node AGE is object age and survives reboots, so it cannot verify this criterion. Compare boot ID or node_boot_time_seconds and actual Talos uptime before/after, plus the effective config and taint values, rather than relying on node age or plan cleanliness. -->

- **V2 (T2):** sha256 of `kata/configuration.toml` == sha256 of the node's extension file; after the
  reboot `talosctl read /etc/cri/conf.d/cri.toml` shows `ConfigPath = '/var/etc/kata-containers/
  configuration.toml'` under runtime `kata`; `talosctl ls /var/etc/kata-containers/config.d` lists
  `10-debug.toml`; the replacement member is Ready within ~3 min (`tep` lease from a dev-worker runs
  `docker version`); `talosctl logs cri` shows for the new sandbox id: shim `level=debug` lines,
  `kata-agent` lines, and `vmconsole` lines (guest kernel boot messages prove the console capture);
  the containerd log growth over the first hour is measured and recorded in the runbook (threshold
  to drop `[runtime] enable_debug` while keeping hypervisor/agent debug: >50 MiB/day).

<!-- codex: Separate startup/fault bursts from steady log volume and state the local retention/rotation bound, since a projected MiB/day figure alone does not ensure evidence survives. If runtime debug is disabled, repeat the agent/console capture check because forwarded debug records may be filtered, and account for the additional reboot and new soak epoch. -->

  A **faithful fault injection** (runbook §"Fault injection": `kill -STOP` the member's virtiofsd
  from the hack reaper) is run once with debug on, to confirm the log actually shows the guest
  hung-task trace / agent timeouts for a virtio-fs stall — this is what a real recurrence must
  produce. Teardown then reaps at ~+150 s as in #800's V3. This injection rotates the member once
  more, so it is done **immediately after the reboot**; the V6 clock starts at the creation time of
  the member that replaces it (recorded in the soak record as day 0).

<!-- codex: The runbook's hack DaemonSet uses REAP_AFTER_SECONDS=5 and would race the production reaper, invalidating this production-timing check and potentially truncating the evidence window. Use the existing reaper pod only as the signaling tool, or a non-reaping diagnostic copy pinned to the selected node, while leaving the production recovery loop at 120 s. -->

<!-- codex: Scope injection to an identified idle member and its node/pod UID/sandbox ID, revalidate each virtiofsd PID immediately before signaling, and keep any privileged helper in kube-system with distinct selectors and explicit cleanup. Copying an unconstrained hostPID DaemonSet after T4 would create diagnostic root access on both nodes. -->

<!-- codex: A guest hung-task trace depends on its detection timeout and on the guest remaining alive long enough; the previous implementation sometimes self-healed after 34 s, so this exact signature is not guaranteed. Set an evidence criterion for the observed recovery path without delaying recovery to force a trace, and repeat the prior process, sandbox-directory, PVC/PV, and iSCSI residual checks. -->

- **V3 (T3):** `scripts/env-pool-soak.py --since 24h` runs read-only against the live stack; the
  day-1/3/7 blocks are in the soak record; on day 7 the operator has the decision tree's inputs.

<!-- codex: Validate the report against the known V2 injection, including its archived host logs, rather than only a quiet successful query. Also demonstrate that a failed endpoint, missing expected node, or truncated response produces an incomplete verdict and cannot be reported as a clean soak. -->

- **V4 (T4):** second apply converges; `kubectl get nodes -l ailab.io/env-pool=true`
  shows two Ready nodes; `swp` `2/2` with one member per node (`kubectl -n testpool get pods -o wide`);
  `env-reaper`/`env-image-prepull`/`alloy` pods present on `talos-env-node-2`; dashboard
  `check --row "Test Env Pool"` renders with both instances; `talos-env-node-1` untouched (`tofu plan`
  before and after shows only the new node).

<!-- codex: Require healthy DaemonSets, a fresh reaper heartbeat and Loki log from node 2, effective debug-config parity, a real node-2 lease running docker, and snapshot/PV cleanup after release; pod presence alone proves none of these. Finish with a full no-op plan, and verify new-node exporter/kubelet series and acceptable ai-node3 memory pressure under representative load. -->

<!-- codex: Add a bounded, explicitly scoped maintenance check showing usable surviving warm capacity and claim/refill behavior with one node unavailable, followed by recovery to two nodes. Include the earlier claim/refill placement case; a dashboard render and initial 2/2 state do not establish the promised failure tolerance. -->

- **V5 (T5):** the draft is reviewed against upstream `main` line numbers on filing day.
- Repo gates on every push: `scripts/manifest-lint.sh`, `scripts/rules-lint.sh`, the manifests
  workflow's unittest set; `tofu fmt -check -recursive` and `tofu validate` locally for `env-pool`.

## Skill phases (feature-implementation)

Ran: Phase 0 (ground: repo/branch/PR state, AGENTS/TESTING absent → CLAUDE.md + README conventions,
prior art in `plans/`, reuse check), Phase 1 plan (this file; the codex round runs at G0 through
LiteLLM, max 2 rounds per `codex-reviewed-planning`). Not run yet: Phases 2–4 (implement, validate,
PR) — start after this plan is approved; each mutation waits for its gate. Skipped: HTML UI
proposal (no UI change).

## Appendix A — upstream issue draft (kubernetes-sigs/agent-sandbox)

**Title:** SandboxWarmPool stuck-member GC measures the readiness grace period from
`CreationTimestamp`, so a member that was Ready for days is deleted on a single NotReady observation

**Version:** controller v1.0.2 (behaviour unchanged on `main` as of 2026-09-17,
`extensions/controllers/sandboxwarmpool_controller.go:519-566`).

<!-- codex: Link a pinned upstream commit and the deployed controller version, since main line numbers move. Attach a minimal sanitized event/condition timeline so maintainers can reproduce the GC behavior independently of the Kata teardown failure. -->

**What happens.** In the reconcile loop every active pool sandbox that is `!isSandboxReady` and older
than `readinessGracePeriod()` (measured as `now - sb.CreationTimestamp`) is deleted as "stuck"
(`logger.Info("Deleting stuck warm pool sandbox", …)`), unless its pod is currently Unschedulable.
The check does not look at how long the sandbox had been Ready before, nor at the Ready condition's
`lastTransitionTime`. Consequences we hit in production (Kata Containers runtime, one warm member,
`--sandbox-warm-pool-readiness-grace-period=15m`):

1. A member that had been Ready for **66 h** flipped NotReady for one readiness-probe window; the next
   reconcile deleted it. The deletion of a *frozen* Kata guest then hung in `StopPodSandbox`, which
   is what turned a blip into a node outage — but the trigger was the GC treating a transient
   NotReady on a long-Ready member exactly like a member that never became Ready. (Two occurrences:
   a 24 h-old and a 66 h-old member.)

<!-- codex: The incident proves deletion on the first observed NotReady state, not that the underlying freeze was transient or would have recovered after one window. Describe that distinction explicitly so the upstream GC report does not overclaim the outage evidence. -->

2. The unschedulable hold (#1215) keeps a Pending member alive past the grace period, but the age
   clock keeps running: when capacity returned, the held member scheduled and was deleted **34 s
   later** — before it could possibly become Ready (fill time here is ~86 s) — producing another
   Pending replacement. The hold defeats its own purpose under any fill time longer than the
   remaining grace.

**Expected.** The grace period should be a bound on *time spent not-Ready*, not on age:

- measure it from the Ready condition's `lastTransitionTime` when the sandbox has ever been Ready
  (i.e. delete only after `now - readyCondition.lastTransitionTime > grace` while `status=False`);
- for a sandbox that has never been Ready, start the clock when its pod is first scheduled (or reset
  it when the unschedulable hold ends), so a hold cannot expire the grace before fill can begin.

<!-- codex: Present this as proposed semantics rather than a complete algorithm: the current Ready condition alone does not encode whether the sandbox was ever Ready, and missing/Unknown conditions need defined behavior. Include pod replacement, repeated unschedulable transitions, and continuously non-Ready states in the upstream test cases so resetting a clock cannot grant indefinite grace. -->

**Workaround in use.** In-guest readiness that only fails for sustained faults, plus a node-side
reaper that bounds the hung teardown; neither changes the controller's semantics.

**Repro sketch.** `replicas: 1`, grace `1m`, a template whose readiness probe can be made to fail for
one probe window on demand (e.g. a TCP port the container closes for 30 s). Wait until the member
has been Ready > 1 m, close the port for one window: the member is deleted on the next reconcile.
For (2): make the pod Unschedulable for > grace, then free capacity: it is deleted within seconds of
scheduling.

<!-- codex: Provide a minimal runnable template with explicit probe settings and wait for the actual Ready=False transition; a 30 s closure at a six-failure/five-second threshold is sensitive to probe alignment. For the scheduling case, make startup deliberately exceed the post-scheduling reconcile interval so the reproduction does not depend on the estate's measured fill time. -->

<!-- codex-review-status: complete -->