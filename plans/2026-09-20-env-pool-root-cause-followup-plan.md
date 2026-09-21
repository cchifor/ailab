# env-pool follow-up: root-cause the guest freeze, close the pool SPOF, file upstream, run V6

Repo `ailab` · branch `ops/env-pool-root-cause-followup` off `gitea/main` `af16cc83` (worktree
`.worktrees/env-pool-followup`) · this file: `plans/2026-09-20-env-pool-root-cause-followup-plan.md`
(approved by the operator on 2026-09-20; codex round 1 addressed 2026-09-21) · prior plan:
`plans/2026-09-20-env-pool-frozen-guest-outage-plan.md` (PR **#800, merged 2026-09-20 15:09 UTC**).

## Context

PR #800 shipped the *prevention* for the 2026-09-20 outage (in-guest `ready-watchdog`, the
`env-reaper` that bounds a Kata teardown, precursor alerts, runbook). Its follow-up section left
four things open; this plan is those four:

1. **Root cause of the guest freeze is unknown.** Single-threaded virtiofsd (`--thread-pool-size=1`,
   `cache=auto`) is the suspect, not proven. Today *no* kata-agent, cloud-hypervisor or guest-console
   line reaches any log, so a recurrence teaches nothing. Kata debug needs a custom
   `configuration.toml` **and** containerd at debug level, which needs `talos-env-node-1`'s machine
   config under tofu — and the host log it lands in must be captured off-node (see below).
2. **The pool is a single point of failure** (one Talos worker, one warm member, one host).
3. **T6 upstream issue** (agent-sandbox warm-pool GC deletes a days-Ready member on one NotReady
   observation) was never drafted beyond two sentences; the operator wants text to file.
4. **V6, the 7-day soak**: the member is deliberately allowed past the 24 h / 66 h marks at which the
   two freezes happened. What a recurrence must look like now, separated by stage: watchdog closes
   the ready-port within ~10 s of a stall, the pod is NotReady ~30 s later, the GC deletes it on its
   next reconcile, the reaper's stage 1 fires ~150 s after the delete request (120 s from
   `deletionTimestamp` + ≤30 s poll), the replacement is Ready ~90–140 s after that: a **~4–5 min
   warm-capacity gap**, node `Ready` throughout, no `TestpoolEnvTeardownStuck`. A stall that Kata
   self-heals (agent health-check timeout → containers exited 255 → restarted in a fresh sandbox,
   observed once in #800's validation) is also a valid outcome and must be recorded as such — a
   `reap stage=1` line is evidence of *one* path, not the acceptance criterion.

### What exploration established (verified 2026-09-20/21)

- **The spike's tofu state is lost, not misplaced.** `kubernetes/infra/env-pool/` has the `.tf`
  files (tracked) but no state; the scratchpad clone recorded in `spike/mk_kubeconfig.py:4` survives
  only as empty directories, and a content search of every JSON-like file under
  `%LOCALAPPDATA%\Temp\claude`, `~/.claude/jobs`, `~/work/home` for a state naming vmid 4401 found
  nothing. The 09-07 plan's W6 precondition ("handover, never a second state") is therefore moot:
  this is a fresh **adoption** (runners precedent, `plans/2026-09-07-estate-anomalies-plan.md:342-398`).
- **Talos delivery mechanics** (`siderolabs/talos` v1.11.2 `v1alpha1_sequencer_tasks.go` WriteUserFiles):
  `machine.files` `op: create` is allowed only under `/var/…` **or** at exactly
  `/etc/cri/conf.d/20-customization.part` (injected as the CRI customization part and rendered into
  `cri.toml`); files are (re)written on every boot → **one node reboot** applies a change. A wrong
  path fails the `userSetup` boot phase (35-min pause, then reboot) rather than apply-time validation.
- **`talosctl logs cri` is a ~1 MiB in-memory ring, not a file** (`logging/circular.go`: 64 KiB live
  + 15 zstd chunks; `/var/log/` holds only `audit/ containers/ pods/`). Today's rate with debug off is
  ~115 KB/h; with debug on the ring will hold **minutes**. Alloy tails `/var/log/pods` only. So the
  shim/agent/console evidence that T2 enables has **no durable home unless T2 also ships it off-node**.
- **The kata shim's log level is set by containerd, not by Kata's `enable_debug`** (Kata 3.20
  `containerd-shim-v2/service.go:84-88`: Warn unless containerd passes `-debug`, which containerd
  2.1.4 does only at `[debug] level = "debug"|"trace"`). `[hypervisor.clh] enable_debug` wires the
  guest serial console into the shim, but each console line is emitted at Debug. Therefore the CRI
  part **must** carry `[debug] level = "debug"`; without it the three Kata toggles produce nothing.
  Containerd at debug is not a flood: the kubelet's polling CRI calls are logged at Trace.
- **Live node layout** (`talosctl read`): `10-kata-containers.part` registers runtime `kata` with
  `options.ConfigPath = "/usr/local/share/kata-containers/configuration.toml"` (read-only extension
  squashfs); `20-customization.part` exists and is **empty**. The live Kata config (469 lines,
  19 860 bytes, sha256 `38d1e30b0dc4ad59742bf807ddbc9363deb354e94a27c06883ca6f5d8074a4c6`, sections
  `[hypervisor.clh]`/`[agent.kata]`/`[runtime]`) has every `enable_debug` commented out,
  `virtio_fs_extra_args = ["--thread-pool-size=1", "--announce-submounts"]`, `virtio_fs_cache = "auto"`,
  `virtio_fs_cache_size = 0`, `enable_annotations = ["enable_iommu", "virtio_fs_extra_args", "kernel_params"]`.
  Extension identity: `kata-containers 3.20.0`, schematic `0839748e…`, Talos v1.11.2.
- **Kata 3.20 loads drop-ins** `<dir-of-ConfigPath>/config.d/*.toml` on top of the main file
  (`katautils/config.go` `decodeDropIns`), so the debug delta can be a small drop-in next to a
  verbatim copy of the extension's file. The `virtio_fs_extra_args` pod annotation is **appended**
  to the config list (`oci/utils.go:883-889`) — `--thread-pool-size` would appear twice — so any
  thread-pool experiment also goes through a drop-in, replacing the whole array.
- **Talos CRI part merge** (`internal/pkg/toml/merge.go` + `pkg/machinery/config/merge`): parts are
  sorted, decoded to maps, merged "right replaces left unless zero value"; **slices concatenate**, so
  the part must not restate `pod_annotations`.
- **Provider facts** (siderolabs/talos 0.11.0 source): `talos_machine_configuration_apply` has no
  import and a no-op `Read`; `Create`/`Update` both send `ApplyConfiguration` with `apply_mode` ∈
  `auto|reboot|no_reboot|staged|staged_if_needing_reboot` (default `auto`); the rendered
  `machine_configuration` is a known value in the saved plan (`tofu show -json`). Talos: `no_reboot`
  rejects a non-immediate change with the diff in the error; `staged` writes only the persisted
  config; **maintenance mode accepts only `auto|reboot|try`**, so a brand-new node's first apply
  cannot be `staged`/`no_reboot`. `staged_if_needing_reboot` falls back to `auto` (may reboot) when
  the plan-time dry-run cannot reach the node — not acceptable on the pool's only node.
  bpg/proxmox 0.113.1: `disk[0].import_from` is never read back (permanent in-place diff, not
  ForceNew); ForceNew triggers are `vm_id`, `node_name`, `clone`, `disk[*].file_id`,
  `initialization.{type,*_file_id}`; `reboot_after_update` defaults to **true** — any in-place change
  the provider flags `rebootRequired` (agent, serial_device, cpu, memory, scsi_hardware, …) makes it
  stop-and-start the VM.
- **Live VM** `qm config 4401` matches `main.tf` field-for-field (cores 8, `cpu: host`, 16384 MiB
  `balloon: 0`, `virtio-scsi-single`, scsi0 60G `discard=on,iothread=1`, `serial0: socket`, pool
  `ailab`, tags `env-pool;k8s;talos;worker`, `agent enabled=1,type=virtio`, `onboot: 1`,
  `ipconfig0: gw=192.168.0.1,ip=192.168.0.37/24`); `qm pending` is empty. Node managedFields: label
  `ailab.io/env-pool` owned by manager `Terraform` (the `kubernetes_labels` default, 2026-09-01);
  `spec.taints` owned by `cilium-operator-generic`.
- **Upstream** `kubernetes-sigs/agent-sandbox` `main` (post v1.0.3, 2026-09-17,
  `extensions/controllers/sandboxwarmpool_controller.go:519-566`) still measures the stuck-member age
  from `sb.CreationTimestamp` with no look at the Ready condition, and the unschedulable hold
  (#1215) does not reset that clock. No open issue covers this (#1274 only made the period
  configurable). T6 is still novel and still accurate.
- **Soak baseline**: node `Ready`, pool `1/1`, member `env-std-pool-htxxt` created ~15:10 UTC
  2026-09-20 (the #800 template rotation), reaper started 15:10:26Z, **0** `reap stage=` lines since.
  Loki retention is **exactly 168 h**; Prometheus `retention: 15d` is bounded by
  `retentionSize: 36GB` (~12 d estimated). Both LAN NodePorts answer from the workstation
  (Loki `:30310`, Prometheus `:30090`), the reaper's lines carry `{namespace="kube-system",
  app="env-reaper"}`, the watchdog's `{namespace="testpool", container="control"}`.
- **Host RAM and the second node**: `free -g` on 2026-09-20 showed ai-node3 with 26 GiB available —
  measured with the **lazy 22.45 GiB qwen3.8 model not resident**. `kubernetes/infra/ai-lxc/models.yaml:109-128`
  funds that model from ai-node3's weekly floor (~28.6 GiB after ci-runner-7's retirement, +6.1 GiB
  margin). A 16 GiB fixed env-node-2 on ai-node3 would push the floor to ~12.6 GiB and the model
  could no longer load there; ai-node1 (floor 4.73 GiB) and ai-node2 (11.70 GiB, hosts env-node-1)
  are worse. **Operator decision 2026-09-21: free the RAM by retiring dev-worker(s), keeping ≥4 of
  the 6.** ai-node3 hosts `dev-worker-3` (vmid 4203, 16 GiB, 12 GiB balloon floor, RSS 16.3 GiB)
  and `dev-worker-6` (4206, 12 GiB, floor 4 GiB, RSS 6.5 GiB) — dw3 alone frees ≈ the node's need.
- **Codex review path works** through LiteLLM (`gpt-6-astra-realjaynesage`, key from the cluster's
  `litellm-secret`). The stale `codex-20260920-161056` worktree from the previous plan's failed
  dispatch was removed on 2026-09-20 after confirming its branch tip (`4ad0eb17`) was already in
  `main` and it held only `codex-prompt.txt`/`codex.log` — no state or uncommitted work.

## Approach

Six tasks, ordered so that the env node reboots **once**, as early as possible, and every mutation
has its own operator gate. **PR-A** = T1 + T2 + T3 + T5 (+ docs). **PR-C** = T4a (dev-worker
retirement, its own plan/review). **PR-B** = T4b (env-node-2). Commit/apply ordering inside PR-A is
part of the contract (G1 applies a T1-only tree; G2 applies the T2 tree) and every manual apply
records the exact commit in the soak record.

### T1 — Adopt `talos-env-node-1` into `kubernetes/infra/env-pool` (state only, no node change)

Files: `kubernetes/infra/env-pool/{imports.tf (new), main.tf, talos.tf, variables.tf, node-labels.tf,
backend.tf, outputs.tf, terraform.tfvars.example (new), SPIKE-REPORT.md}`, `justfile`,
`docs/runbooks/env-pool.md`, `docs/network-plan.md`, `CLAUDE.md`.

- **State location (decided before any apply):** the authoritative state is
  `kubernetes/infra/env-pool/terraform.tfstate` **in the main checkout** (`C:\Users\chifo\work\home\ailab`),
  like every other module. Applies from a worktree pass
  `-backend-config="path=C:/Users/chifo/work/home/ailab/kubernetes/infra/env-pool/terraform.tfstate"`
  at `init`; a worktree is never removed while it holds the only copy. Before every apply:
  `Copy-Item … kubernetes/infra/_out/env-pool.tfstate.<utc-stamp>` (gitignored dir). One operator
  applies at a time (local backend, no lock across worktrees). `backend.tf`'s obsolete "move the
  state after merge" note and `SPIKE-REPORT.md` "State / handover" get a dated correction.
- **Sensitivity:** the state and every saved plan embed the Talos client key and the cluster machine
  secrets (via `terraform_remote_state`); rendered machine-config dumps embed them too. All of these
  live only under gitignored paths (`terraform.tfstate*`, `_out/`), are deleted when no longer
  needed, and only **redacted** excerpts go into commits, this plan or the PR.
- `imports.tf`: `import { to = proxmox_virtual_environment_vm.env["env-node-1"]  id = "ai-node2/4401" }`
  (`kubernetes/infra/cloudflare/imports.tf` precedent; inert after the first apply; `tofu import`
  CLI is the fallback if `-target` does not honour it). Applied with `-target` first so the import
  and its diff are the whole plan.
- `main.tf`: `lifecycle.ignore_changes = [initialization, disk[0].import_from]` — `import_from` is the
  **one** documented create-only exception (never read back); `initialization` was already ignored,
  so `ipconfig0` is verified out-of-band (`qm config` read: matches). **`reboot_after_update = false`**
  so tofu can never power-cycle the node on an in-place diff; every other declared value is
  reconciled to the live identity, not ignored. Plan-review rule for G1: the VM may show at most
  one `~ update in-place` whose changed lines are provider-local attributes with no PVE write
  (`stop_on_destroy`, `reboot_after_update`, `timeout_*`, …); **any** line touching `agent`,
  `serial_device`, `cpu`, `memory`, `scsi_hardware`, `disk` (other than `import_from`),
  `network_device`, `operating_system`, `tags`, `pool_id`, `on_boot`, `started`, or any `-/+` /
  "must be replaced" → stop.
- `variables.tf`: `env_nodes` object gains `apply_mode = optional(string, "auto")`; `env-node-1`
  gets **`no_reboot`** for the adoption apply (Talos refuses anything that would reboot, with the
  diff in the error) and **`staged`** from T2 on (steady state for running env nodes: tofu never
  reboots or live-edits the node; every config change = plan → apply → explicit `talosctl reboot`).
  `auto` stays the default only for a node's very first apply (maintenance mode). New `kata_debug`
  (bool, default `false` until T2's gate) and `kata_agent_debug` (bool, default `true`) gate the
  template block so the T1 tree renders **exactly today's config**.
- `talos.tf`: `apply_mode = each.value.apply_mode`; `outputs.tf`: `rendered_machine_configuration`
  (sensitive, per node) so the complete generated-and-patched config can be extracted without
  parsing plan JSON. **Complete comparison before G1's config apply** (codex blocking item): extract
  the rendered config to `_out/`, run `talosctl-1112.exe … apply-config --dry-run --mode=auto -f …`
  against the node — it must report "Applied configuration without a reboot (skipped in dry-run)"
  **and an empty diff**. The provider's own `no_reboot` is the second guard. Lockfile stays at
  talos 0.11.0 / bpg 0.113.1 / kubernetes 3.2.1; a stale plan is re-planned, never applied.
- `node-labels.tf`: `provider "kubernetes"` gets `config_context = "admin@ai"` (a merged kubeconfig
  would otherwise select its default context, which is a different cluster). `kubernetes_labels` and
  `kubernetes_node_taint` are "create = adopt" resources (no import); managedFields were inspected:
  the label is already owned by the default manager `Terraform` → same owner; the taint's `force =
  true` under `env-pool-tofu` takes over from `cilium-operator-generic` without changing the value.
  Force stays limited to those two objects.
- `terraform.tfvars.example` documents the four required inputs (`pve_endpoint`, `pve_api_token`,
  absolute `infra_state_path`, absolute `kubeconfig_path` = `…/_out/kubeconfig`, whose only context
  is `admin@ai`); the real `terraform.tfvars` stays gitignored.
- `just env-pool-plan` / `env-pool-apply: nested-virt-verify` mirror `agent-nodes-plan/apply`.
- Docs (`docs/runbooks/env-pool.md:5-7`, `docs/network-plan.md:45,62-72`, `CLAUDE.md` inventory row
  "out-of-band, not in tofu") lose the unmanaged/state-never-moved claims **in the commit after a
  green no-op plan**, with the redacted plan output quoted in the commit message.

Exit criterion (V1): full non-targeted plan `No changes.`; the VM never destroyed/replaced/rebooted
(boot id and Talos uptime unchanged — not Node AGE, which survives reboots); label/taint values
unchanged; kubelet not restarted.

### T2 — Kata debug evidence: config + containerd debug + durable capture (one reboot)

Files: `kubernetes/infra/env-pool/machine-config/{worker.yaml.tftpl, kata/configuration.toml (new,
verbatim), kata/config.d/10-debug.toml (new)}`, `talos.tf`, `variables.tf`,
`kubernetes/apps/infrastructure/testpool/{cri-log-relay.yaml (new), env-reaper.yaml}`,
`kubernetes/apps/infrastructure/testpool/kustomization.yaml`, `docs/runbooks/env-pool.md`.

- **T2a Kata config.** `kata/configuration.toml` = byte-for-byte copy of the extension's file
  (captured with `MSYS_NO_PATHCONV=1 … talosctl read … > file` in Git Bash — PowerShell redirection
  re-encodes; sha256 above, identical on two reads; single trailing newline). The drop-in
  `config.d/10-debug.toml` sets `[hypervisor.clh] enable_debug = true` (cloud-hypervisor `-v` and the
  guest serial console into the shim log as `vmconsole` lines — guest kernel hung-task traces are the
  evidence a virtio-fs stall leaves), `[agent.kata] enable_debug = ${kata_agent_debug}` (the chatty
  one; the volume lever), `[runtime] enable_debug = true`. `debug_console_enabled` stays off (no
  `kata-runtime` on Talos; a frozen guest hangs shells anyway). Its header records the base file's
  sha256, the extension version (`kata-containers 3.20.0`), schematic and Talos version;
  **re-verifying that hash against the node is a documented precondition of any env-node image or
  extension upgrade** (runbook), and V2 checks it after boot. The files are world-readable
  configuration only — no credentials.
- **T2b containerd + CRI part** (rendered inline in the template, not a separate file):
  ```toml
  [debug]
    level = "debug"
  [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.kata.options]
    ConfigPath = "/var/etc/kata-containers/configuration.toml"
  ```
  Only `ConfigPath` (a scalar; later part wins) and `[debug]` are set; `runtime_type`,
  `pod_annotations`, `privileged_without_host_devices` are inherited. This affects every pod using
  handler `kata` **on these env nodes only** (the agent-nodes module has its own template);
  `RuntimeClass kata-env` keeps handler `kata`, so no k8s object changes. V2 diffs the complete
  rendered `kata` runtime table in `cri.toml` before/after (only `ConfigPath` may differ) and proves
  a new member runs the Kata guest (`uname -r` = 6.12.42 ≠ node 6.12.48) — the ConfigPath text alone
  is not proof.
- **Template.** `worker.yaml.tftpl` gets a `%{ if kata_debug }` block with three `machine.files`
  entries (`op: create`, `permissions: 0o644`, paths exactly `/etc/cri/conf.d/20-customization.part`,
  `/var/etc/kata-containers/configuration.toml`, `/var/etc/kata-containers/config.d/10-debug.toml`);
  the TOML content is passed in from `talos.tf` (`file()` on module-relative paths) and placed with
  `indent(8, …)` inside `content: |` block scalars (blank lines stay empty, clip chomping leaves one
  trailing newline → byte-identical). Before G2 the rendered YAML is parsed and the three contents
  are extracted and diffed against the source files; after the reboot each file is read back and
  sha256-compared. A future `config.d/20-virtiofs.toml` needs its **own** `machine.files` entry — a
  repo file alone ships nothing. Every drop-in that may exist on the node is an explicit entry
  (declarative), and V2/V3 list `config.d/` to catch strays. The same template serves env-node-2, so
  both nodes run identical Kata settings by construction.
- **T2c durable capture (blocking item).** A one-replica Deployment `cri-log-relay` in `testpool`'s
  Flux Kustomization but scheduled **off** the env node (no `dedicated=env` toleration → lands on a
  CP), image `ghcr.io/siderolabs/talosctl:v1.11.2` (digest-pinned via the estate mirror), running
  `while true; do talosctl -n 192.168.0.37 -e 192.168.0.41 logs cri -f --tail 200; sleep 5; done`
  with a **read-only `os:reader` talosconfig** minted by `talosctl config new --roles os:reader`
  (a new credential → G2 scope) stored as a SOPS Secret. Its stdout is a pod log → Alloy → Loki
  (`{namespace="testpool", app="cri-log-relay"}`, 168 h). Reconnects re-emit ≤200 lines (dedupe by
  timestamp when reading). This is the only durable home for shim/agent/`vmconsole` lines; it must be
  Ready **before** the reboot, and V2 proves it retained the injected incident through teardown.
  Alternative kept in reserve: `machine.logging.destinations` (immediate-apply) into an Alloy TCP
  receiver — more plumbing, no reboot; chosen only if the relay proves unreliable.
- **T2d reaper evidence dump.** `env-reaper.yaml` `reap.sh`: immediately before each stage-1
  SIGKILL, log (same line format, `evidence pid=<n> exe=<path> wchan=<…> threads=<n>` plus the
  first 20 lines of `/proc/<pid>/stack`) for the cloud-hypervisor and virtiofsd PIDs, after
  re-validating the identity chain; failures to read are logged and never block the kill. This is the
  "host virtiofsd state before cleanup" that a 150 s window otherwise loses. Namespace boundary, RBAC
  and identity checks are unchanged; #800's untested limits (stage 2, containerd restart mid-reap,
  simultaneous hangs) stay untested and are restated in the runbook.
- **Privacy of debug output.** Shim debug logs contain exec commands, environment and mount
  details of leases. V2 inspects the emitted fields for one real `tep` lease before debug stays on;
  raw evidence lives only in `_out/` / Loki, and this repository (mirrored publicly) receives
  redacted excerpts only. If the fields are unacceptable, `kata_agent_debug = false` and, if still
  unacceptable, `[debug] level` is dropped — accepting the loss of guest lines — and the decision is
  recorded.
- **Apply and reboot (G2).** `kata_debug = true` + `apply_mode = "staged"` in the T2 commit;
  `tofu plan` shows exactly one in-place change on `talos_machine_configuration_apply.worker["env-node-1"]`;
  the extracted rendered config dry-run must say "with a reboot" and diff **only** the three
  `machine.files`. Apply stages it (`talosctl read /system/state/config.yaml` mentions the three
  paths; `talosctl get mc` does not — running config untouched). Pre-reboot, in the agreed window:
  no `SandboxClaim`, no Terminating pods on the node, node Ready, `kubectl exec <member> -c control
  -- true` answers (a frozen guest hangs `talosctl reboot`), relay pod Ready and shipping. Lease
  acquisition is quiesced by **announcing the window and pausing the dev-worker fan-out**
  (the estate's shared-budget convention) rather than scaling the pool to 0 (Flux drift correction
  would re-set `replicas: 1` within 10 min); the claim check is repeated immediately before
  `talosctl reboot --wait -n 192.168.0.37` (the pinned `talosctl-1112.exe`). Acquisition resumes by
  itself: `tep` waits on `readyReplicas > 0`. Bounded failure path: if the reboot stalls in
  `stopAllPods` for > 5 min, `qm reset 4401` per the runbook (already authorised as the recovery
  step). Expected: NotReady 1–3 min, one member rotation.
- **Rollback.** `kata_debug = false` → plan (one in-place change) → apply (staged) → reboot. The
  CRI part is regenerated from the machine config on every boot, so the override and `[debug]`
  disappear and `ConfigPath` reverts to the extension's path (verified by reading `cri.toml`); the
  files under `/var/etc/kata-containers/` persist but are inert once nothing references them —
  and because `op: create` rewrites them every boot, a later re-enable ships exactly the repo
  content, never a stale experiment. Any additional reboot (rollback, log-level change) starts a
  new soak epoch (T3).
- **Log volume tiers** (measured in V2, not projected): tier 1 = as designed; if the idle rate
  exceeds ~10 MB/h or one lease produces > ~100 MB, tier 2 = `kata_agent_debug = false` (keeps the
  guest console, the highest-value signal); there is no tier 3 short of dropping `[debug] level`,
  which silences the guest. Startup/fault bursts are reported separately from steady state; the
  relay + Loki bound is 168 h regardless of rate (Loki ingestion headroom checked in V2).
- **Deliberately not changed yet:** `virtio_fs_extra_args` / `virtio_fs_cache`. Changing the suspect
  before capturing a recurrence would censor the only experiment that can name the cause. When V6
  produces evidence, `config.d/20-virtiofs.toml` replaces the **complete** array (keeping
  `--announce-submounts`), one variable at a time, labelled as a hypothesis test — a blocked
  virtio-fs check identifies the failure path, not the specific cause.

### T3 — V6 soak with evidence capture

Files: `scripts/env-pool-soak.py` (new, read-only), `scripts/tests/test_env_pool_soak.py` (new,
wired into `.gitea/workflows/manifests.yaml`), `docs/runbooks/env-pool.md` (queries + report
template), this plan's `## Soak record`.

- **Why a script and not only documented queries:** the check-ins need interval export from the
  last checkpoint with overlap, Loki pagination (5 000-line pages walked by timestamp), truncation
  detection and an explicit **INCOMPLETE** verdict when an endpoint fails, a target is missing or a
  window exceeds retention — properties a hand-run query set does not have. It stays a small
  read-only helper against the existing NodePorts (no agent, controller or new monitoring service);
  the queries it runs are also written out in the runbook so a human can reproduce any number.
- **Inputs/outputs:** `--from/--to` (UTC ISO) or `--checkpoint <file>` (last successful `to`,
  re-queried with 1 h overlap); Prometheus `query_range` at 60 s resolution; raw Loki lines and
  Prometheus series are exported to `kubernetes/infra/_out/soak/<from>_<to>/` (gitignored); stdout is
  the markdown block for the soak record. Queries (all scoped and vector-matched):
  - node readiness: `kube_node_status_condition{node=~"talos-env-node-.*",condition="Ready",status="true"}`
    (NotReady intervals listed with start/end), `up{job="kubelet",metrics_path="/metrics",node=~"talos-env-node-.*"}`,
    `node_boot_time_seconds{instance=~"192.168.0.3[78]:9100"}` (reboots = epochs);
  - members: `kube_pod_created{namespace="testpool"} * on(namespace,pod) group_left() kube_pod_info{namespace="testpool",created_by_kind="Sandbox"}`
    with age computed **per sample** (`max_over_time` of `time() - created`), plus
    `kube_pod_container_status_restarts_total{namespace="testpool"}` (a guest sandbox restart does
    not create a new pod); warm vs leased membership from `kubectl get sandboxes -l
    agents.x-k8s.io/warm-pool-sandbox` at check-in time and from claim lines in Loki;
  - alerts: `ALERTS{alertname=~"Testpool.*|EnvNode.*"}` over the window (pending and firing);
  - runtime: `sum by (node,operation_type) (increase(kubelet_runtime_operations_errors_total{node=~"talos-env-node-.*",operation_type=~"stop_.*"}[<window>]))`;
  - Loki: reaper `reap stage=|evidence |api error:|heartbeat iter=` (heartbeat gaps > 15 min are
    reported), watchdog `check hung|check failed|ready-port closed|checks recovered`, relay
    `vmconsole|kata-agent|level=debug` counts per sandbox id (join key = the reaper's `sandbox=`).
  - **Verdicts:** `OK` (no NotReady, no alert, no reap, no watchdog closure), `RECURRENCE-CONTAINED`
    (watchdog closure → reap or self-heal, node Ready, no alert), `PREVENTION-FAILED` (any node
    NotReady, `TestpoolEnvTeardownStuck*` or `EnvNode*` firing), `INCOMPLETE` (failed endpoint,
    missing expected series/node, truncated page, window outside retention). The unit test feeds
    canned responses and asserts each verdict, including that a failed endpoint can never yield `OK`.
- **Check-ins:** day 1, day 3 (past 66 h = 2 d 18 h), day 7 from the epoch baseline; each appends
  the block plus raw reaper/watchdog/relay lines (redacted) to the soak record. Loki is a rolling
  168 h from each event, so day-0 evidence (the V2 injection) is exported before its own boundary.
- **Epoch baseline** = the creation time of the member that exists *after* the V2 injection and
  lease smoke test — not the reboot time. Every later replacement, guest restart or reboot starts a
  new epoch; the record lists per-member exposure (which member passed 24 h / 66 h) alongside the
  overall window. T4b's template change (spread constraint) rotates the member; it is scheduled at a
  check-in boundary and the **final cohort must pass the age milestones** in its own epoch.
- **Decision tree (in priority order, decided by the operator):**
  1. `PREVENTION-FAILED` at any time → the recovery runbook **immediately**, then re-plan; the
     record distinguishes failed containment from an evidence-capture failure (relay/Loki gap).
  2. Recurrence contained **with** evidence → root-cause section from the relay log (`vmconsole`
     traces, agent timeouts, reaper `evidence` lines) + watchdog discriminator (`virtiofs` vs
     `dockerd`); a virtio-fs finding → `20-virtiofs.toml` hypothesis test, reboot, new epoch.
  3. Recurrence contained **without** usable evidence → fix the capture (tier, relay) first;
     no config experiment on a guess.
  4. Self-heal path observed → recorded as its own outcome; evidence criteria are the agent
     health-check lines, not a hung-task trace (which needs the guest to survive long enough).
  5. No recurrence by day 7 → keep debug on, extend; age-based rotation stays rejected.

### T4a — Free the RAM: retire dev-worker(s) on ai-node3 (own plan + PR-C, gate G3a)

Operator decision 2026-09-21: retire dev-worker(s) to fund env-node-2, keeping **≥ 4 dev-workers**.
Candidates on ai-node3: `dev-worker-3` (4203, `.10`, 16 GiB / 12 GiB floor, RSS 16.3 GiB — frees
≈ the whole need) first; `dev-worker-6` (4206, `.13`, 12 GiB, RSS 6.5 GiB) only if the measured
post-retirement floor leaves the qwen3.8 load less than ~4 GiB margin (models.yaml's own test:
"a lazy load arrives all at once, so the test is each node's worst case"). A retirement is the
runners precedent (remove the map entry, `tofu apply`, never `qm destroy`) **plus** the worker's
foot-print across the repo — ~20 tracked files each: `inventory/hosts.yml`,
`ansible/secrets/{dev-worker,tep-tokens}.sops.yaml`, `ansible/host_vars/*`, helmtest tenant
namespace/RBAC/NetworkPolicy/kyverno entries, `edge/cloudflared.yaml`, `homepage/configmap.yaml`,
`monitoring/{agentforge,dev-workers-node}.yaml`, Velero, the agentforge/dsh/openbao runbooks and
ADR 0018/0020, and the OpenBao credentials/tokens of that worker. It gets its own dated plan
(`plans/2026-09-2x-retire-dev-worker-3-plan.md`), codex round and PR; the decision of *which*
worker(s) is confirmed with the operator at that plan's gate with the live seat/agent usage of each.
Ordering: PR-C merged and applied (floor re-measured over ≥ 24 h) **before** G3b.

### T4b — Second env node `env-node-2` on ai-node3 (PR-B, gate G3b)

Files: `kubernetes/infra/env-pool/variables.tf`, `docs/network-plan.md` (`.38` allocated, free list),
`CLAUDE.md` inventory row, `scripts/gen-reporting-dashboard.py:563` (`ENVNODE` →
`instance=~"192.168.0.3[78]:9100"`, regenerated ConfigMap + `dashboard-preview.py check --row "Test
Env Pool"`), `kubernetes/apps/infrastructure/testpool/sandboxtemplate-std.yaml` (pool `replicas: 2`
+ spread constraint), `docs/runbooks/env-pool.md` (two nodes).

- `env-node-2 = { node_name = "ai-node3", vm_id = 4402, ip = "192.168.0.38", host_ip = "192.168.0.4",
  hostname = "env-node-2", apply_mode = "auto" }` (first apply is maintenance mode); flipped to
  `staged` in a follow-up apply (one in-place change). `.38` and vmid 4402 are re-verified free at
  execution time (ping/ARP, `qm list` on all three hosts, IPAM table, cloudlab's `.20–.28`).
- **Ordering (codex):** infra first — `tofu apply` from the reviewed PR-B commit creates the VM and
  joins it; only after node 2 is Ready **with** taint and label, CNI/CSI, the reaper, prepull and
  alloy pods, and the relay/debug parity verified, is the manifests half (replicas, spread) merged
  and its reconciled Flux revision recorded. Merging manifests first could rotate the only warm
  member while node 2 is absent.
- **Untainted window:** the spike's first apply failed the label/taint with `node not found` and
  converged on the second apply. To close the window in which a fresh, untainted node could accept
  arbitrary pods, the node is **cordoned the moment it registers** (`kubectl cordon
  talos-env-node-2` in a loop started before the apply) and uncordoned only after `tofu plan` is
  clean and the taint/label are verified.
- **Spread:** `topologySpreadConstraints: [{maxSkew: 1, topologyKey: kubernetes.io/hostname,
  whenUnsatisfiable: DoNotSchedule, labelSelector: {app: env-std}, nodeTaintsPolicy: Honor,
  nodeAffinityPolicy: Honor}]` (no `minDomains`): with node 2 NotReady its `unreachable`/`not-ready`
  taints remove it from the eligible domains, so a replacement still schedules on node 1 (validated
  in V4). **The guarantee is narrowed honestly:** the selector counts leased pods too, so after a
  claim + refill both *warm* members can sit on one node; what the second node buys is that a
  replacement can always be scheduled after one worker/host is lost and that a wedge on one node no
  longer empties the pool — existing leases on the lost node are not migrated, and QNAP stays shared.
- **Capacity, not quota:** per node one warm member (requests 2.3 Gi + overhead 160 Mi; limits
  12 Gi + 1 Gi) plus a refill overlapping a Terminating one; the 16 GiB guest tolerates one active
  lease at full limits, not two. Storage: a second 60 GiB iSCSI LUN from the golden snapshot and the
  node's own 60 GB `local-lvm` disk; PVC quota (8) and `requests.storage` (400 Gi) checked live.
  ai-node3's memory pressure is verified **under model load** after T4a (the 22.45 GiB lazy load
  triggered deliberately) and under a representative lease.
- Node 3's storage-fabric route differs (`host_ip` `.4`); V4 proves snapshot restore and block
  volume attach there before any tolerance claim.

### T5 — Upstream issue draft (operator files it)

Text in **Appendix A** below and in PR-A's description; once filed, the issue URL goes into the
comment at `kubernetes/apps/infrastructure/agent-sandbox/kustomization.yaml:15-25` (the same
comment gets its stale "60 s of sustained failure" corrected to "~30 s" — the shipped TCP probe is
5 s × 6).

### Gates

Decisions already taken by the operator (2026-09-20/21) and what they cover: the plan approval
covers G0 (branch, plan file, codex rounds — reversible, read-only on the estate); "reboot now"
covers scheduling G2 right after G1; "second node: yes, fund it by retiring dev-workers (≥ 4 kept)"
covers T4a/T4b **as plans**, not their applies. Each row below still needs an explicit OK for
exactly that scope, at the time it is run.

| Gate | Mutation | Scope / commands | Rollback |
|---|---|---|---|
| G1 | T1 adoption, at the T1 commit (`kata_debug=false`) | `tofu init -backend-config=<main-checkout state>`, `plan -target=<VM>`, `apply -target=<VM>` (import only), full `plan`, dry-run diff via `talosctl-1112.exe apply-config --dry-run` (empty diff required), `apply` (creates the `no_reboot` config apply + label/taint SSA under `admin@ai`) | state backup in `_out/` before each apply; `tofu state rm` only forgets ownership — it cannot undo an apply, so a partial apply is diagnosed from the backup before any state operation; the import block would re-adopt on the next apply, so it is removed together with the state entry if adoption is abandoned |
| G2 | T2, at the T2 commit | mint the `os:reader` talosconfig (new read-only credential, SOPS), Flux-apply `cri-log-relay` + the reaper `evidence` change (merge PR-A first, or hand-apply from the branch as #800 did — the record says which), `tofu apply` (stages), `talosctl reboot --wait -n 192.168.0.37` in the announced window, **V2's deliberate virtiofsd freeze of the new member from the production reaper pod** and one `tep` lease smoke test, `qm reset 4401` if the reboot stalls > 5 min | `kata_debug=false` → apply → reboot (new epoch); relay/reaper changes revert by PR |
| G3a | T4a retirement | per its own plan: remove the map entry, `tofu apply` in `dev-workers`, secrets/inventory/manifests PR | its own plan |
| G3b | T4b node create | `tofu apply` creates vmid 4402 on ai-node3 (`.38`); cordon loop; second apply for label/taint; then the manifests merge | ordered: stop new acquisitions, revert the manifests (pool back to 1, spread removed), drain node 2 and verify its PVC/PV/iSCSI cleanup, remove its map entry and inspect the **full** plan (talos.tf/node-labels.tf `depends_on` edges) before applying the removal; node 1 must show no change |
| — | Flux-applied manifests (T2c/T2d, T4b, kustomization comment) | merge to `main` on Gitea; nothing applied by hand unless the record says so | revert PR |

**Codex round 2:** this revision adds material codex has not seen (ring buffer/relay, containerd
`[debug]`, T2d, T4a, the verdict machine); it is sent for a second round before finalize.

## Critical files

| Path | Role |
|---|---|
| `kubernetes/infra/env-pool/imports.tf` (new) | T1: adopt vmid 4401 (`ai-node2/4401`) |
| `kubernetes/infra/env-pool/main.tf` | T1: `ignore_changes += disk[0].import_from`, `reboot_after_update = false` |
| `kubernetes/infra/env-pool/variables.tf` | T1: per-node `apply_mode`, `kata_debug`, `kata_agent_debug`; T4b: `env-node-2` |
| `kubernetes/infra/env-pool/talos.tf` | T1: `apply_mode`; T2: Kata file contents into the template |
| `kubernetes/infra/env-pool/outputs.tf` | T1: sensitive `rendered_machine_configuration` for the dry-run diff |
| `kubernetes/infra/env-pool/node-labels.tf` | T1: `config_context = "admin@ai"` |
| `kubernetes/infra/env-pool/backend.tf`, `SPIKE-REPORT.md` | T1: state location + dated correction of the handover note |
| `kubernetes/infra/env-pool/terraform.tfvars.example` (new) | T1: the four required inputs |
| `kubernetes/infra/env-pool/machine-config/worker.yaml.tftpl` | T2: gated `machine.files` block (CRI part inline) |
| `kubernetes/infra/env-pool/machine-config/kata/configuration.toml` (new) | T2a: verbatim extension copy (sha256 + extension identity pinned) |
| `kubernetes/infra/env-pool/machine-config/kata/config.d/10-debug.toml` (new) | T2a: the debug toggles |
| `kubernetes/apps/infrastructure/testpool/cri-log-relay.yaml` (new) + SOPS Secret | T2c: durable `logs cri` capture → Loki |
| `kubernetes/apps/infrastructure/testpool/env-reaper.yaml` | T2d: `evidence` lines before stage-1 kills |
| `kubernetes/apps/infrastructure/testpool/kustomization.yaml` | register the relay |
| `justfile` | T1: `env-pool-plan/apply` |
| `scripts/env-pool-soak.py`, `scripts/tests/test_env_pool_soak.py` (new) | T3: read-only soak report + verdict tests |
| `.gitea/workflows/manifests.yaml` | T3: run the new unittest module |
| `docs/runbooks/env-pool.md` | T1 state, T2 debug/capture/upgrade precondition/injection correction, T3 queries + template, T4 two nodes |
| `docs/network-plan.md`, `CLAUDE.md` | T1 `.37` managed; T4b `.38` allocated |
| `kubernetes/apps/infrastructure/testpool/sandboxtemplate-std.yaml` | T4b: `replicas: 2`, spread constraint |
| `scripts/gen-reporting-dashboard.py` + `monitoring/reporting-dashboard.yaml` | T4b: `ENVNODE` covers `.38` |
| `kubernetes/apps/infrastructure/agent-sandbox/kustomization.yaml` | T5: issue link; "60 s" → "~30 s" |
| `plans/2026-09-20-env-pool-root-cause-followup-plan.md` | this plan + soak record + Appendix A |

Reused, not rewritten: `ready-watchdog.yaml` (its `virtiofs`/`dockerd` discriminator),
`testpool-rules.yaml` + `env-node-rules.yaml` (soak alert set), `scripts/dashboard-preview.py`
(T4b render gate), `scripts/manifest-lint.sh` / `rules-lint.sh` (CI gates — **no CI gate covers
`kubernetes/infra/**/*.tf`**; `tofu fmt -check -recursive` + `tofu validate` run locally before
every commit, per `feedback_terraform_fmt`).

## Verification

- **V1 (T1):** targeted import plan shows `1 to import`, ≤ 1 provider-local in-place update,
  `0 to destroy`; after the import `qm pending 4401` empty and `qm status --verbose` uptime
  continuous; the extracted rendered config's `talosctl apply-config --dry-run` reports no reboot
  and an **empty diff**; after the full apply: `tofu plan` = `No changes.`, `tofu state list` shows
  VM + config apply + label + taint, `talosctl get … uptime`/boot id unchanged before vs after,
  `talosctl services` shows kubelet `Running/OK` with no restart, `kubectl get node
  talos-env-node-1 --show-labels` and `.spec.taints` unchanged, `kubectl -n testpool get
  sandboxes,swp` still `1/1`. The redacted plan output is quoted in the docs commit.
- **V2 (T2):** pre-reboot: repo `kata/configuration.toml` sha256 == node's extension file; rendered
  template parses and its three contents match the source files byte-for-byte; plan shows exactly
  one in-place change; dry-run diff = the three files only; staged config confirmed in
  `/system/state/config.yaml`; relay pod Ready and its lines in Loki. Post-reboot: `cri.toml`'s
  `kata` table differs only in `ConfigPath` and carries `[debug] level = 'debug'`; the three files
  read back with matching sha256, mode 0644, `config.d/` lists exactly `10-debug.toml`; the new
  member Ready ≤ 3 min; `talosctl processes` shows cloud-hypervisor with `-v`; Loki (relay stream)
  shows for the new sandbox id shim `level=debug` lines, `kata-agent` lines and `vmconsole` lines
  with the guest kernel banner `Linux version 6.12.42`; a `tep` lease from a dev-worker runs `docker
  version` and its debug fields are inspected (privacy decision recorded); log volume measured over
  1 h idle and during that lease, startup burst reported separately → tier decision. **Injection**:
  from the **production** reaper pod on this node (never a 5 s hack copy alongside it — the runbook
  is corrected to say so), against the identified idle member (pod UID + sandbox id recorded),
  re-validating the virtiofsd PIDs immediately before `kill -STOP`: watchdog `virtiofs check hung`
  → NotReady → GC delete → reaper `evidence` lines then `reap stage=1` at ~+150 s → pod gone ≤ 60 s
  later, zero residuals (processes, `/run/vc/sbs/<id>`, shared dir, PV/PVC, iSCSI session count),
  replacement Ready; the relay stream retained the whole incident (agent health-check timeouts and,
  if the guest lived long enough, the hung-task trace — the recovery is never delayed to force
  one); `scripts/env-pool-soak.py` over that window reports `RECURRENCE-CONTAINED` with the
  evidence counts. Epoch baseline = the replacement's creation time; the day-0 record is written.
- **V3 (T3):** unit tests pass in CI (`OK`/`RECURRENCE-CONTAINED`/`PREVENTION-FAILED`/`INCOMPLETE`,
  failed endpoint ≠ `OK`); the live run against V2's window matches the archived relay lines; a run
  with a deliberately wrong endpoint reports `INCOMPLETE`.
- **V4 (T4b):** node 2 Ready with taint/label; reaper heartbeat, prepull, alloy pods healthy on it
  and a Loki stream from node 2; debug-config parity (same `cri.toml` `kata` table, same sha256s);
  `swp` `2/2` with one member per node; a real lease on node 2 runs `docker version` (proves iSCSI
  + snapshot restore over node 3's route) and its PVC/PV are reclaimed on release; dashboard
  `check --row "Test Env Pool"` renders both instances; new node's exporter/kubelet series present;
  ai-node3 `MemAvailable` under a forced qwen3.8 load + one lease stays above the model's own
  floor rule; `tofu plan` = `No changes.` **Degraded-mode check (bounded, announced):** cordon node
  2, delete its member → the replacement schedules on node 1 within ~3 min; uncordon → the pool
  returns to one member per node; the claim+refill placement sequence is exercised once and the
  outcome (which node holds warm capacity afterwards) is recorded rather than assumed.
- **V5 (T5):** the draft cites a pinned upstream commit and the deployed controller version on
  filing day.
- Repo gates on every push: `scripts/manifest-lint.sh`, `scripts/rules-lint.sh`, the manifests
  workflow's unittest set (now including `test_env_pool_soak`); `tofu fmt -check -recursive` and
  `tofu validate` locally for `env-pool`.

## Soak record

| Field | Value |
|---|---|
| Epoch 0 baseline (UTC) | *(set at G2: creation time of the member after the V2 injection + lease smoke test)* |
| Applied commits | T1: *(sha)* · T2: *(sha)* · relay/reaper Flux revision: *(sha)* |
| Node boot id / `node_boot_time_seconds` | *(after the G2 reboot)* |
| Kata base sha256 / extension | `38d1e30b…a4c6` / kata-containers 3.20.0, schematic `0839748e…`, Talos v1.11.2 |
| Member pod name / UID / sandbox id | *(epoch 0)* |
| Check-in owner | operator + this session |

| Check-in | Window (UTC) | Verdict | Node Ready | Alerts | Reap / watchdog / relay | Per-member exposure | Notes |
|---|---|---|---|---|---|---|---|
| day 0 | | | | | | | |

## Skill phases (feature-implementation)

Ran: Phase 0 (ground: repo/branch/PR state, AGENTS/TESTING absent → CLAUDE.md + README conventions,
prior art in `plans/`, reuse check), Phase 1 plan (this file; codex round 1 addressed, round 2
pending). Not run yet: Phases 2–4 (implement, validate, PR) — each mutation waits for its gate.
Skipped: HTML UI proposal (no UI change).

## Appendix A — upstream issue draft (kubernetes-sigs/agent-sandbox)

**Title:** SandboxWarmPool stuck-member GC measures the readiness grace period from
`CreationTimestamp`, so a member that had been Ready for days is deleted on its first observed
NotReady

**Version:** deployed controller `registry.k8s.io/agent-sandbox/agent-sandbox-controller:v1.0.2`;
behaviour unchanged on `main` at commit *(pinned on filing day)* —
`extensions/controllers/sandboxwarmpool_controller.go`, the "Deleting stuck warm pool sandbox" block
in the reconcile loop (lines 519-566 at the pinned commit).

**What happens.** In the reconcile loop every active pool sandbox that is `!isSandboxReady` and older
than `readinessGracePeriod()` (measured as `now - sb.CreationTimestamp`) is deleted as "stuck",
unless its pod is currently Unschedulable. The check does not consider how long the sandbox had
been Ready, nor the Ready condition's `lastTransitionTime`. Consequences we hit (Kata Containers
runtime, one warm member, `--sandbox-warm-pool-readiness-grace-period=15m`):

1. A member that had been Ready for **66 h** was observed NotReady once (its readiness probe failed
   for one window); the next reconcile deleted it. We do **not** claim the underlying fault was
   transient — in our case the guest had frozen and the deletion then hung in `StopPodSandbox` —
   the point is that the GC applied the *fill-time* grace period to a member days past fill, so a
   single NotReady observation on a long-Ready member is treated exactly like a member that never
   became Ready. (Two occurrences: a 24 h-old and a 66 h-old member.)
2. The unschedulable hold (#1215) keeps a Pending member alive past the grace period, but the age
   clock keeps running: when capacity returned, the held member scheduled and was deleted **34 s**
   later — before it could become Ready (fill takes ~86 s here) — producing another Pending
   replacement.

Sanitized timeline (UTC, 2026-09-20; sandbox names shortened): `k6h5m` created 09-17 15:36, Ready
from 15:38; 09-20 09:27:48 pod Ready=False; 09:28:5x Sandbox deleted by the pool ("Deleting stuck
warm pool sandbox … age 66h"); replacement `fsm72` created 09:28:59, Pending (node NotReady) until
12:39:xx, scheduled 12:40:0x, deleted 12:40:4x (age 191 m > 15 m) before its containers started.

**Proposed semantics (not a complete algorithm).** The grace period should bound *time spent
not-Ready*, not age:

- for a sandbox that has been Ready before, measure from the Ready condition's `lastTransitionTime`
  (delete only after `now - lastTransitionTime > grace` while `status=False`); a missing or
  `Unknown` Ready condition on a sandbox that was Ready before should count from when it was last
  observed Ready, which the controller may need to track itself since the condition alone does not
  encode "ever Ready";
- for a sandbox that has never been Ready, start the clock when its pod is first scheduled (or reset
  it when the unschedulable hold ends), so the hold cannot expire the grace before fill can begin;
- test cases should include: pod replacement under the same sandbox, repeated unschedulable
  transitions, and a continuously non-Ready member — a clock reset must never grant indefinite grace.

**Workaround in use.** In-guest readiness that only fails for sustained faults, plus a node-side
reaper that bounds the hung teardown; neither changes the controller's semantics.

**Minimal reproduction.** `SandboxWarmPool` `replicas: 1`, grace `1m`; template with one container
(`busybox`) running `nc -lk -p 9099` under `readinessProbe: {tcpSocket: {port: 9099}, periodSeconds:
5, failureThreshold: 3}`. Wait until the Sandbox has been Ready > 1 m, then `kubectl exec` and kill
`nc` for ≥ 20 s (three probe failures) and watch for the pod's Ready=False transition: on the next
reconcile the Sandbox is deleted although it was NotReady for well under the grace period. For (2):
give the template a resource request larger than any node can satisfy, wait > grace, then lower the
request (or add capacity) and use a container with `sleep 120` before listening so startup exceeds
the reconcile interval: the sandbox is deleted seconds after scheduling.

<!-- codex-review-status: complete -->
