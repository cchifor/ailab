# Retire dev-worker-6 and dev-worker-3; re-slot the survivors to dev-worker-1..4 (.8–.11)

## Codex Review

- Good: the slot/VM distinction, preserved VMIDs, two operator gates, copy-off requirement, and coordinated monitoring removal follow the useful parts of PR #746.
- Blocking: the overlapping `moved` blocks cannot perform this occupied-key permutation; C2 also omits VM 4205’s existing 6144 MiB balloon floor.
- Security: issuing new credentials does not revoke old OpenBao tokens or Kubernetes TokenRequest tokens; both retired and reassigned identities need explicit revocation and negative tests.
- Integration: the token-sync worker loop, Cloudflared rollout trigger, guest-rendered configuration, scheduled Ansible convergence, and possible external AgentForge mappings need explicit handling.
- Gates need stronger pre-flight evidence, protection for busy workers, reconciliation barriers, and separate rollback procedures before and after state migration and VM destruction.

Repo `ailab` · branch `ops/retire-dev-worker-3-6` off `gitea/main` `9b775872` (worktree
`.worktrees/retire-dw36`) · parent plan: `plans/2026-09-20-env-pool-root-cause-followup-plan.md`
§T4a (the RAM for `env-node-2`, §T4b) · precedent: `ci-runner-7` retirement, PR #746 (`af4cca83`).

<!-- codex: This checkout contains the review input at repository root, not at the requested plans/ path, and the referenced parent plan is absent. This is a repository/precedent review, not live verification; make the parent’s ENV_POOL/T4a/T4b acceptance criteria available before approving G3b. -->

## Context

`env-node-2` (16 GiB fixed, ai-node3) is the pool's second node. The parent plan's margin test says
ai-node3 must keep, with the qwen3.8 model **not** resident, a floor of
`16 + 0.5 (VMM) + 22.45 (lazy model load) + ≥ 4 (margin) = 43 GiB` available. Measured 2026-09-21
09:00Z (`node-ssh.py 192.168.0.4`, Prometheus `node_memory_MemAvailable_bytes`):

| ai-node3 | now (model idle) | 7-day floor |
|---|---|---|
| `MemAvailable` | **25.2 GiB** of 124.9 | 6.9 GiB (model resident + runner bursts) |

Configured guests: talos-cp3 28 GiB · ci-runner-3 / -6 24 + 24 GiB (RSS 20.7 / 21.8) · dev-worker-3
16 GiB (RSS **16.3**) · dev-worker-6 12 GiB (RSS **3.7**) · talos-agent-node-3 16 GiB (RSS 4.1) ·
reviewer-1 / -2 4 + 4 GiB · ai-llm-3 LXC 96 GiB limit (1.3 GiB, model unloaded) = 128 GiB + the LXC
on 125 GiB physical (the "122 %" of `dev-workers/variables.tf`).

**Operator decisions (2026-09-21):** retire **both** dev-worker-3 and dev-worker-6 — the only
combination that passes the test (`25.2 + 16.3 + 3.7 ≈ 45 GiB`; dw3 alone = 41.5, dw6 alone = 29)
while keeping the required **four** workers. **dev-worker-3 is busy with implementation tasks and
is not touched until the operator says it is free.** The survivors are **renamed to close the gaps
and re-addressed contiguously**: `dev-worker-1..4` on `.8`–`.11`.

<!-- codex: Current MemAvailable plus QEMU RSS is an estimate, not proof of the required floor or of exactly how much memory destruction will release. Define the 24-hour model-idle measurement, representative runner load, and coverage requirements explicitly; reject insufficient samples rather than treating an idle snapshot as G3b evidence. -->

### What exploration established (live, 2026-09-21)

| VM | node | vmid | IP | 7-d CPU | load | state |
|---|---|---|---|---|---|---|
| dev-worker-1 | ai-node1 | 4201 | .8 | 31.8 % | — | active |
| dev-worker-2 | ai-node2 | 4202 | .9 | 1.7 % | 0.2 | near idle |
| **dev-worker-3** | ai-node3 | 4203 | .10 | 24.6 % | 1.8 | **busy**: 11 tmux sessions, compose stacks (`model-linked-qualification`, `storage-browser`), text-embeddings (1.2 GiB), codex |
| dev-worker-4 | ai-node1 | 4204 | .11 | 25.0 % | 2.0 | active, 2 tmux sessions |
| dev-worker-5 | ai-node2 | 4205 | .12 | 27.7 % | 2.3 | active, **herdr pilot host** (`herdr` active) |
| **dev-worker-6** | ai-node3 | 4206 | .13 | 1.7 % | 0.01 | **idle**: one `claude` tmux session, k9s, bao; the 12 GiB-ceiling POC; herdr takeover done 2026-09-03 |

- **A worker is a *slot* plus a *VM*.** The slot name `dev-worker-N` (`dwN`) is the key of
  everything around the VM: the tofu map key (`dev_worker_nodes`), the Ansible `inventory_hostname`
  (and through it the OpenBao AppRole + policy `dev-worker-N`, KV subtree `af/dev-workers/dev-worker-N`,
  the `tep-dwN` ServiceAccount + token, the `helmtest-dwN` namespace/SA/RBAC/NetworkPolicy, the
  restic repo `dev-worker-dev-worker-N`), the terminal `dwN.chifor.me` (Cloudflare DNS + Access app
  in `infra/cloudflare`, tunnel ingress in `apps/edge/cloudflared.yaml`, homepage tile), the
  monitoring targets (`dev-workers-node.yaml`, `agentforge.yaml`) and the seat map (ADR 0018: Max#1
  dw1/dw2, Max#2 dw3/dw4, Codex Pro dw5/dw6). The VM is identified by its vmid; seat logins
  (`~/.claude`, `~/.codex`), workspaces and tmux state live in the VM.
- **Renaming a VM into a lower slot needs no rebuild.** The map key is the `for_each` key, so a bare
  edit would be destroy + create (the reason #746 refused to renumber runners) — but OpenTofu
  `moved { from = proxmox_virtual_environment_vm.dev_worker["dev-worker-4"] to = …["dev-worker-3"] }`
  re-keys the state, `name` is updated in place (`qm set --name`), and `initialization` (IP,
  hostname) is `ignore_changes` — the live IP/hostname are changed **in-guest**, exactly as the
  2026-08 renumber to `.8`–`.13` was done (runbook: add the new IP live, rewrite
  `/etc/netplan/50-cloud-init.yaml`, `99-disable-network-config.cfg` is already present on dw4/dw5,
  `netplan apply`). Unlike a Gitea runner, a dev-worker has no external registration that pins its
  name.

  <!-- codex: The in-place name update is correct, but these moved blocks cannot implement the proposed permutation: slot 3 is occupied, and 5→4→3 forms a historical move chain while slot 4 remains configured. Use a rehearsed, backed-up sequence of state mv operations on this resource (3→temporary-retired-4203, 4→3, 5→4, leaving the temporary instance to be destroyed), without the overlapping moved blocks, or explicitly staged configurations. -->

  <!-- codex: Check whether the v1 AgentForge runtime is active: its environment uses AF_WORKER_NAME, while docs/runbooks/agentforge.md identifies external agentforge-config worker/seat mappings and Gitea org webhooks. Verify and update any live mappings or retired webhook URLs; absence of Gitea runner registration does not establish absence of external identity dependencies. -->

- **vmids cannot be renamed in place** (Proxmox has no `qm rename`; a vmid change in tofu is
  destroy + create of the 40 + 128 GiB disks; the manual `mv 4204.conf` + `lvrename` route lives
  outside tofu and ends in `state rm` + `import`). The re-slotted VMs keep 4204 / 4205; the
  `vmid == 4200 + N` invariant is dropped and documented in the IPAM table. (Escalated at G3a-2 —
  default: keep.)
- **Slots 1–4 keep their IPs, so most slot artifacts do not move.** After the re-slot, `dw3` is
  still `.10` and `dw4` still `.11`: the tunnel ingress, homepage, DNS/Access, monitoring targets,
  helmtest namespaces and tep SAs of slots 3 and 4 are **unchanged**; only slots 5 and 6 are
  removed, and the *credentials* of slots 3 and 4 rotate because different machines now hold them
  (OpenBao secret-ids, tep tokens; the helmtest SA tokens are re-synced by `openbao-k8stoken-sync`).
- Flux prunes (`clusters/ai/helmtest.yaml` `prune: true`; the Kyverno guard
  `helmtest-protect-reserved` excludes the Flux controllers, so pruning a `helmtest-dwN` namespace is
  allowed). `af/dev-workers/dev-worker-3` holds an estate-class credential
  (`strive_test_user/password`, `openbao-estate-credentials.md:102`): it stays with **slot 3** and is
  therefore inherited by the machine that becomes dev-worker-3 (ex-dw4) — flagged for the operator.

  <!-- codex: Flux’s exemption covers its direct requests, not namespace-controller/garbage-collector requests to delete protected children; remaining protected objects, including chart-created NetworkPolicies, can leave a namespace Terminating. Sequence protected-child removal through an allowed principal across the helmtest and openbao inventories, then verify namespace deletion without weakening survivor guards. -->

- Backups: `dev_worker_enable_restic` is **false** in the role defaults — there is no automatic
  backup to fall back on. Anything on dw3/dw6 that must survive is copied off by hand before the
  destroy (gate).

  <!-- codex: Verify effective host variables and live backup timers rather than inferring backup status solely from role defaults. Record protected copy-off locations and a restore spot-check, including unpushed work, compose volumes, and required helmtest releases/PVC data before Flux prunes their namespaces. -->

- #746's rules carry over: a retirement is a **drain, not a kill**; the monitoring endpoint is
  removed **in the same change** (a target left behind fires `NodeExporterDown` forever); the VM keeps
  running until the PR merges, so nothing scrapes a dead address; `tofu plan` must show exactly the
  expected destroy count and nothing else.

## Approach

Two PRs, one per VM, because dw3's timing is the operator's and dw6 can go now. Each PR is
repo-complete for its slot(s) (tofu + Flux manifests + inventory/secrets + docs in one change) and
is followed by an ordered apply sequence at its gate.

<!-- codex: Make successful G3a-1/V-C1 completion and rebasing C2 onto merged C1 explicit prerequisites for G3a-2. Otherwise C2’s destroy count, credential inventory, and rollback baseline can still include uncompleted C1 changes. -->

### PR-C1 — retire dev-worker-6 (slot 6), gate G3a-1 (now)

Repo change (all in one PR):

- `kubernetes/infra/dev-workers/variables.tf`: drop the `dev-worker-6` map entry with a dated
  retirement comment (the #746 form); the 12 GiB-ceiling POC note becomes history (its outcome:
  the 12 GiB ceiling was never the constraint — dw6 idled at 3.7 GiB RSS); placement note → dw1/4 on
  node1, dw2/5 on node2, **none on node3** (node3 hosts `env-node-2`).

  <!-- codex: C1 still leaves busy VM 4203 on node3, and env-node-2 remains gated by T4b. Describe that intermediate placement accurately and defer “none on node3” until C2. -->

- `kubernetes/infra/cloudflare/{access.tf,variables.tf}`: `dw6` out of the Access `for_each` set and
  of `tunnel_hostnames` (DNS record + Access app destroyed by that module's apply).
- `kubernetes/apps/apps/edge/cloudflared.yaml` (ingress `dw6.chifor.me`), `apps/homepage/configmap.yaml`
  (tile), `monitoring/dev-workers-node.yaml` + `monitoring/agentforge.yaml` (`.13` targets),
  `infrastructure/helmtest/{namespaces,rbac,networkpolicy}.yaml` (`helmtest-dw6` objects),
  `security/openbao/k8stoken-sync.yaml` (`helmtest-dw6` RBAC + `tep-dw6` in `resourceNames`),
  `security/openbao/devworker-provision-job.yaml` (host loop; the job is re-run at the gate so the
  `dev-worker-6` policy/role are **deleted**, not just no longer upserted — the script gains an
  explicit revoke step for retired slots), `testpool/tep-access.yaml` (`tep-dw6` SA, token Secret,
  RoleBinding subject), `backup/velero/helmrelease.yaml` (the "6 Secrets tep-dw1..dw6" comments →
  5, then 4 in PR-C2).

  <!-- codex: Also change k8stoken-sync.yaml’s executable range(1, 7) and remove retired-path grants from the k8stoken-sync policy in devworker-provision-job.yaml. Removing only RBAC/SAs leaves the sync targeting missing identities and failing before publication; require fresh success for 10 fields after C1 and 8 after C2. -->

  <!-- codex: Bump cloudflared.yaml’s chifor.me/config-revision annotation in each PR and wait for both connectors to roll out. Its static ConfigMap does not reload running ingress routes, so a reconciled ConfigMap alone is insufficient before removing the Access gate. -->

  <!-- codex: This provision Job is automatically recreated after its TTL, so adding revocation means it may run immediately after merge, before the manual ceremony. Complete the KV retention decision beforehand and define one idempotent owner for revocation that cannot race an old seed or token-sync run. -->

- `inventory/hosts.yml` (host removed, dated comment), `ansible/host_vars/dev-worker-6.yml`
  (deleted), `ansible/secrets/{dev-worker,tep-tokens}.sops.yaml` and
  `security/openbao/devworker-seeds.sops.yaml` (the `dev-worker-6` keys removed via `sops`, so the
  files stay decryptable and the MAC stays valid), `ansible/roles/dev_worker/tasks/tep.yml` comment,
  `scripts/fleet-converge-daily.sh` (the dw6 `--skip-tags herdr` special case goes),
  `scripts/oom-protect-guests.sh` (4206 out of the list). (`.gitea/workflows/dev-worker-scripts.yaml`
  and `scripts/validate-codex-fleet.sh` only mention workers in comments — untouched.)

  <!-- codex: scripts/tep-render-kubeconfigs.py also hard-codes six workers and will fail against the retired Secret. Update its supported worker set in both PRs if retaining this recovery path, while preserving the OpenBao ownership marker that prevents competing kubeconfig writers. -->

- Docs: `docs/runbooks/dev-workers.md` (table, POC section closed, herdr pilot = dw5 only),
  `docs/network-plan.md` (`.13` free), `CLAUDE.md` inventory row (`.8–.12`, `4201–4205` until PR-C2),
  `README.md` (its dev-worker row is stale — `.37/.38/.39`, `4201–4203` — corrected to the live
  fleet), `docs/runbooks/agentforge.md` + ADR 0018 seat map (Codex Pro = dw5 only), ADR 0020 ("six"
  → the live count, as an amendment note, not a rewrite), `docs/runbooks/openbao-dev-workers.md`
  table, `docs/runbooks/passkeys.md` / `cloudflare-access-apps.md` lists.

Gate G3a-1 sequence (operator present; each step read back before the next):

1. Pre-flight: operator confirms nothing on dw6 must survive (its `claude` tmux session is the
   only state seen); `tmux` sessions closed; `qm status 4206` running; `tofu -chdir=…/dev-workers
   plan` on the branch = `0 to add, 0 to change, 1 to destroy` keyed `["dev-worker-6"]`;
   `tofu -chdir=…/cloudflare plan` = the `dw6` DNS record + Access app to destroy and nothing else.

   <!-- codex: Identify and securely back up each module’s authoritative state and installed provider versions before planning; dev-workers uses a gitignored local backend, so a fresh worktree does not automatically contain its state. Verify state address→node/VMID/disk mappings, correct cluster/account contexts, SOPS decryption, and baseline Flux/OpenBao health; never initialize an empty replacement state or upgrade providers during this gate. -->

   <!-- codex: Refresh the activity check at the gate and prevent new work before closing sessions: include herdr panes, background agents/compose jobs, active TEP leases, and helmtest workloads. An idle snapshot or closing tmux is not evidence that these tasks drained or that their remote resources can be deleted. -->

2. Merge PR-C1 → Flux prunes `helmtest-dw6`, `tep-dw6` (+ token Secret), the k8stoken-sync objects,
   the `.13` scrape targets, the tunnel ingress and the homepage tile; the reconciled revision is
   recorded.

   <!-- codex: Merge does not provide an ordering barrier across Flux Kustomizations, and helmtest has wait:false. Before proceeding, verify each affected inventory at the merged revision, completed namespace/SA deletion, a successful updated token sync, both active monitoring target sets, and the Cloudflared rollout. -->

3. `tofu apply` (dev-workers) destroys 4206 (`stop_on_destroy = true` → a clean stop, then delete
   incl. both disks and the cloud-init disk); `tofu apply` (cloudflare) removes `dw6.chifor.me`.

   <!-- codex: stop_on_destroy = true selects a stop instead of a graceful guest shutdown; it does not establish a clean ACPI shutdown. After the monitoring-removal barrier, explicitly shut down 4206 and wait for stopped status before applying the reviewed destruction plan. -->

4. OpenBao ceremony (`openbao-dev-workers.md`, breakglass): `bao auth/approle/role/dev-worker-6`
   deleted, policy deleted, `af/dev-workers/dev-worker-6` KV subtree deleted (after reading it back
   once for anything the operator wants to keep); the provision job re-run from the merged manifest.

   <!-- codex: Capture and revoke retired workers’ outstanding SecretID accessors and issued OpenBao token accessors before removing their roles; role/policy deletion is not an explicit revocation of already-issued periodic tokens. Apply this to slot 5 in C2 as well, and verify old credentials fail without printing them. -->

   <!-- codex: Specify KV-v2 retention/deletion precisely: kv delete soft-deletes the current version and deleting one path does not recursively remove descendants. Inventory and preserve approved values before merge, then explicitly handle historical versions/metadata and descendants so seeds or stale jobs cannot recreate retired credentials. -->

5. `ansible-playbook dev-workers.yml --limit dev_workers` converges the remaining hosts (nothing
   should change on them; the run proves the inventory/secrets edits are consistent).

   <!-- codex: This includes busy dw3 and contradicts the explicit instruction not to touch it before operator release; a full role run can restart services or update software even when this PR only removes another host. Validate inventory/secrets without mutating dw3, and limit any necessary convergence to approved idle hosts. -->

6. Verify (V-C1 below); `MemAvailable` on ai-node3 read after the destroy.

### PR-C2 — retire dev-worker-3 (slot 3's VM) and re-slot dw4 → 3, dw5 → 4, gate G3a-2 (when dw3 is free)

Starts only when the operator declares dev-worker-3's implementation tasks finished and its state
copied off (compose volumes, repos, tmux). Repo change:

<!-- codex: C2 also reboots two active survivors, including the herdr pilot, so obtain an idle window and save/drain their work before the gate. Herdr’s resume setting and tmux persistence do not guarantee preservation of arbitrary live processes across reboot or service restart. -->

- `variables.tf`: `dev-worker-3 = { ai-node1, vm_id = 4204, ip = .10, hostname = dev-worker-3,
  memory_floating_mib = 12288 }` (ex-dw4 keeps the node1 12 GiB floor), `dev-worker-4 = { ai-node2,
  vm_id = 4205, ip = .11, hostname = dev-worker-4 }` (ex-dw5), `dev-worker-5` and the old
  `dev-worker-3` (4203) removed; `moved.tf` (new) with the two `moved` blocks; the vmid-invariant and
  placement comments rewritten ("slot N ≠ vmid 420N since 2026-09-2x").

  <!-- codex: Preserve memory_floating_mib = 6144 on VM 4205’s new slot-4 entry; omitting it resets this host to the 4096 MiB default and reverses the documented swap-starvation mitigation. Require the survivor plan to show name-only updates with unchanged node, VMID, memory, disks, and network-device identity. -->

- Slot-5 removal = the PR-C1 list applied to `dw5`/`.12`/`tep-dw5`/`helmtest-dw5`/`dev-worker-5`
  (Cloudflare `dw5`, tunnel, homepage, monitoring, helmtest, k8stoken-sync, tep-access, inventory,
  host_vars `dev-worker-5.yml` → **renamed** `dev-worker-4.yml` (it is the herdr pilot host's
  config and the host keeps herdr), secrets re-keyed `dev-worker-4/5` → `dev-worker-3/4` with **new**
  values minted at the gate, seeds, scripts, docs incl. the IPAM table and `CLAUDE.md`
  (`.8–.11`, vmids `4201, 4202, 4204, 4205`), the ADR 0018 seat map (Max#1 dw1/dw2, Max#2 dw3 =
  ex-dw4, Codex Pro dw4 = ex-dw5), `agentforge.md`, `openbao-dev-workers.md` table).

  <!-- codex: The C1 removal list cannot be mechanically applied to VMIDs: remove 4203 from scripts/oom-protect-guests.sh and retain surviving 4205. Also update scripts/dw-paste.ps1, whose executable default still targets c4@192.168.0.12. -->

  <!-- codex: AppRole role IDs belong to the destination roles, so do not merely move the old role_id/secret_id pairs between YAML keys. Mint against roles dev-worker-3/dev-worker-4 and define how their SOPS-encrypted values enter the reviewed C2 revision before merge; minting only in post-merge step 5 otherwise requires an unplanned additional commit. -->

- The slot-3/4 artifacts that stay: `dw3`/`dw4` Cloudflare + tunnel + homepage + monitoring targets
  (`.10`/`.11` unchanged), `helmtest-dw3`/`dw4`, `tep-dw3`/`dw4` (tokens rotated — the old dw3 VM
  held `tep-dw3`'s token; the old dw5 VM held `tep-dw5`'s, which is deleted).

  <!-- codex: Reissuing TokenRequest tokens does not invalidate previous tokens: this sync requests 30-day tokens for both TEP and helmtest, and old dw4 also retains slot-4 credentials when becoming dw3. Define a narrowly scoped, Kyverno-compatible replacement of the slot-3/4 ServiceAccounts with new UIDs, including legacy TEP token Secrets, then remint; ordinary administrator deletion of protected helmtest SAs is denied by the guard. -->

Gate G3a-2 sequence:

1. Pre-flight: operator's written "dev-worker-3 is free and empty"; `tofu plan` (dev-workers) on the
   branch = `1 to destroy` (`4203`), `2 to change` (`name` on 4204/4205), `0 to add`, the two
   `moved` lines shown; cloudflare plan = `dw5` record + app destroyed only.

   <!-- codex: This expected plan is unattainable with the proposed overlapping moved blocks; rehearse the corrected state migration against an isolated copy of authoritative state before authorizing live changes. After the gated state migration, regenerate and inspect the real plan, explicitly proving that only 4203 is destroyed and neither survivor is replaced; an earlier saved plan becomes stale after state changes. -->

   <!-- codex: Hold the fleet-converge lock or suspend and verify the scheduled converge before changing any addresses, and keep it blocked until the merged inventory and credentials are deployed. During pre-merge re-IP, old main still maps .10/.11 to different machines and could install the wrong identities or pilot configuration. -->

2. **Drain, not kill:** `qm shutdown 4203` (ACPI works on Ubuntu) → `.10` is free.

   <!-- codex: Wait for Proxmox to report 4203 stopped and verify .10 has no remaining owner before assigning it; issuing shutdown alone is not proof of either condition. Prevent an automatic/manual restart of 4203 while its former IP is in use, and abort on a shutdown timeout instead of forcing the transition. -->

3. In-guest on 4204 (still `dev-worker-4`, `.11`), over SSH: `hostnamectl set-hostname dev-worker-3`,
   `/etc/hosts`, netplan `.11` → `.10` (add `.10` as a second address first, `netplan apply`,
   confirm SSH on `.10`, then drop `.11`), reboot to prove it sticks. Then 4205: `dev-worker-5` →
   `dev-worker-4`, `.12` → `.11` the same way (only after 4204 has left `.11`).

   <!-- codex: Stage saved network/hostname files and working Proxmox console access before the first SSH-disrupting change, and validate netplan before applying it. Verify cloud-init hostname handling separately: network: {config: disabled} only disables network regeneration, so ensure the new hostname survives reboot while Proxmox/cloud-init metadata still carries the old identity. -->

   <!-- codex: Keep credential-consuming services and automatic job intake quiesced through this transition so reboot does not resume work using the previous slot’s credentials. Re-render and validate Caddy’s fact-derived IP/hostname site, ttyd’s inventory-derived title, OpenBao templates, and any enabled AgentForge AF_WORKER_NAME before reopening the hosts. -->

4. Merge PR-C2 → Flux prunes slot 5; `tofu apply` (dev-workers: destroy 4203, rename 4204/4205;
   cloudflare: `dw5`).

   <!-- codex: Required pre-merge re-IP of 4205 leaves the still-configured .12 scrape target dead until C2 merges and reconciles, so C2 cannot also satisfy the stated “nothing scrapes a dead address” invariant. Define a bounded maintenance window with scoped alert silencing and an immediate pre-apply rollback if merge/reconciliation fails, while retaining the required shutdown→4204 move→4205 move→apply ordering. -->

5. OpenBao ceremony: delete role/policy/KV `dev-worker-5`; **rotate** `dev-worker-3` and
   `dev-worker-4` secret-ids (new machines hold them) and the tep tokens for `tep-dw3`/`tep-dw4`;
   decide the fate of `af/dev-workers/dev-worker-3.strive_test_*` (inherit or move); provision job
   re-run; `ansible-playbook dev-workers.yml` converges the re-slotted hosts under their new names
   (renders the new AppRole login, tep token, helmtest kubeconfig for `helmtest-dw3`/`dw4`; the
   restic repo URL follows the hostname — no restic is enabled, so nothing to migrate).

   <!-- codex: Minting new SecretIDs does not revoke old SecretIDs or already-authenticated periodic OpenBao tokens; explicitly destroy stale accessors and revoke tokens issued under the previous slot-3/4 ownership. Stop the old agents first, install the destination identities, restart them, and verify fresh authentication plus denial of the captured old credentials. -->

   <!-- codex: Decide the estate credential’s destination before merge or granting the replacement host slot-3 access, and update its encrypted seed consistently. devworker-provision-job.yaml uses seed-wins patching, so a live-only move can be undone on its next run and removing a seed field alone does not remove an existing KV field. -->

   <!-- codex: Re-running the provision Job does not run openbao-k8stoken-sync; explicitly run the updated sync after SA replacement and wait for all 8 fields to validate before host convergence. Verify both users’ rendered kubeconfigs and their actual authenticated SA/namespace, then limit convergence to the two reassigned hosts unless other hosts have an approved need. -->

6. Operator's `~/.ssh/known_hosts`: the host keys behind `.10`/`.11` changed (they moved with the
   VMs) — `ssh-keygen -R` both, re-accept.

   <!-- codex: This must precede new-address SSH and Ansible in steps 3/5 because ansible.cfg enables host-key checking. Capture each survivor’s fingerprints while its old identity is trusted, verify the same fingerprints at the new IP via that evidence or the Proxmox console, and update every relevant controller’s IP/name entries rather than blindly re-accepting keys. -->

7. Verify (V-C2); then the 24 h floor re-measurement that gates T4b (G3b).

### Not in scope

Retiring or shrinking `ci-runner-3`/`-6` (24 GiB each — the largest block on node3) stays the
fallback if the re-measured floor still misses 43 GiB; `env-node-2` itself (PR-B, §T4b).

## Critical files

| Path | PR | Role |
|---|---|---|
| `kubernetes/infra/dev-workers/variables.tf` | C1, C2 | map entries, retirement comments, placement/vmid notes |
| `kubernetes/infra/dev-workers/moved.tf` (new) | C2 | `moved` blocks 4 → 3, 5 → 4 |
| `kubernetes/infra/cloudflare/{access.tf,variables.tf}` | C1, C2 | `dw6` / `dw5` Access apps + DNS |
| `kubernetes/apps/apps/edge/cloudflared.yaml`, `apps/homepage/configmap.yaml` | C1, C2 | tunnel ingress, tiles |
| `kubernetes/apps/infrastructure/monitoring/{dev-workers-node,agentforge}.yaml` | C1, C2 | `.13` / `.12` targets (same change as the retirement) |
| `kubernetes/apps/infrastructure/helmtest/{namespaces,rbac,networkpolicy}.yaml` | C1, C2 | `helmtest-dw6` / `dw5` |
| `kubernetes/apps/infrastructure/security/openbao/{k8stoken-sync,devworker-provision-job}.yaml`, `devworker-seeds.sops.yaml` | C1, C2 | slot RBAC, host loop + revoke step, seeds |
| `kubernetes/apps/infrastructure/testpool/tep-access.yaml` | C1, C2 | `tep-dw6` / `dw5` |
| `kubernetes/apps/backup/velero/helmrelease.yaml` | C1, C2 | Secret-count comments |
| `inventory/hosts.yml`, `ansible/host_vars/*`, `ansible/secrets/{dev-worker,tep-tokens}.sops.yaml`, `ansible/roles/dev_worker/tasks/tep.yml` | C1, C2 | hosts, per-host config, credentials |
| `scripts/{fleet-converge-daily,oom-protect-guests}.sh` | C1, C2 | host / vmid lists |
| `docs/runbooks/{dev-workers,agentforge,openbao-dev-workers,cloudflare-access-apps,passkeys}.md`, `docs/network-plan.md`, `CLAUDE.md`, `README.md`, ADR 0018 / 0020 | C1, C2 | the fleet as documented |

## Verification

- **V-C1:** `tofu plan` (dev-workers) = `No changes.` after the apply; `qm list` on ai-node3 has no
  4206; `pvesm`/`lvs` show no `vm-4206-*`; Prometheus `up{instance="192.168.0.13:9100"}` **absent**
  (not 0) and no `NodeExporterDown`; `kubectl get ns helmtest-dw6` / `sa tep-dw6 -n testpool` gone;
  `bao list auth/approle/role` and `bao policy list` without `dev-worker-6`; `dig dw6.chifor.me`
  NXDOMAIN and the Access app gone from the dashboard; `ansible dev_workers -m ping` = 5 hosts;
  ai-node3 `MemAvailable` ≥ 25.2 + ~3.7 GiB with the model idle.

  <!-- codex: Also require a clean Cloudflare plan, absence of the retired AgentForge scrape target and token Secret, successful 10-field token sync, and negative authentication tests for retired credentials. Verify authoritative DNS removal and that both tunnel connectors no longer route the hostname before Access deletion; recursive DNS results alone can reflect cached state. -->

- **V-C2:** `tofu plan` = `No changes.`; `hostname` / `hostname -I` on `.10` = `dev-worker-3`, on
  `.11` = `dev-worker-4`; `.12` and `.13` unanswered (ping/ARP); `qm list` shows 4204 named
  `dev-worker-3`, 4205 `dev-worker-4`, no 4203; herdr active on the new dw4; `cred list` works on
  both re-slotted hosts with the new secret-ids; `tep` lease smoke test from the new dw3;
  `helmtest-dw3` kubeconfig on the new dw3 works; monitoring `nodename` labels match the new names;
  `ansible dev_workers -m ping` = 4 hosts; the dashboards' dev-worker rows render 4 workers;
  ai-node3's 24 h floor (model idle) **≥ 43 GiB** before G3b.

  <!-- codex: Repeat the relevant V-C1 removal checks for slot 5 and VM 4203, including orphan disks, Cloudflare, namespace completion, OpenBao credentials/KV, and both scrape endpoints. Verify actual VMID/node/MAC identity behind each new IP and the preserved 12/6 GiB balloon floors, not just matching hostnames. -->

  <!-- codex: cred list is insufficient proof of the destination identity because worker policies permit listing shared metadata. Test fresh destination AppRole login, both workers’ TEP acquire/release and Helm install/uninstall, denied cross-slot access, rejected old OpenBao/TEP/helmtest credentials, continuing Kyverno protections, authenticated web-terminal routing, and usable herdr sessions; clean up smoke resources before measuring headroom. -->

- Repo gates on every push: `scripts/manifest-lint.sh`, `scripts/rules-lint.sh`, the manifests
  workflow's unittest set, `sops` decrypt of every edited secret, `tofu fmt -check` + `validate` for
  `dev-workers` and `cloudflare` (no CI gate covers `kubernetes/infra/**/*.tf`).

  <!-- codex: Add rendered Kustomize inventory checks for retired objects and survivor guards, plus consistency checks across inventory, sync targets, policies, and per-slot secret keys. Validate edited SOPS files with plaintext output suppressed; formatting/validate cannot substitute for a real state-backed plan or prove runtime revocation. -->

## Gates

| Gate | Mutation | Rollback |
|---|---|---|
| G3a-0 | this plan + codex review, PR-C1 opened | none |
| G3a-1 | merge PR-C1; Flux prune; `tofu apply` dev-workers (destroy 4206) + cloudflare (`dw6`); OpenBao revoke; Ansible converge | VM rebuild from the module + Ansible (workspace lost — hence the pre-flight copy-off) |
| G3a-2 | operator declares dw3 free; `qm shutdown 4203`; in-guest re-IP/rename of 4204, 4205; merge PR-C2; `tofu apply` (destroy 4203, rename); OpenBao rotate/revoke; converge | re-IP back (in-guest), `moved` blocks reversed before any apply; 4203 rebuild from the module |
| G3b | (parent plan) `env-node-2` after the 24 h floor re-measure | parent plan |

<!-- codex: G3a-1 rollback needs more than a VM rebuild: restore the reviewed IaC/Flux definitions, recreate identities with fresh credentials, restore retained namespace/workspace data, and restore DNS/ingress only with Access protection present. Disable any declarative retired-slot revoker before reprovisioning that slot, and do not restore obsolete state over an already-destroyed VM. -->

<!-- codex: G3a-2 needs separate rollback procedures before state migration, after state migration, and after destruction; simply reversing the two moved blocks still creates overlapping addresses and cannot recover disks. With convergence paused, free .11 by returning 4205 to .12 before returning 4204 to .11, free .10 before restoring 4203, and reverse state mappings through a temporary key using current VMIDs before reviewing any apply. -->

<!-- codex: Post-apply C2 rollback must additionally restore Proxmox names, inventory/host_vars, guest configuration, slot-5 Flux/Cloudflare resources, and freshly issued credentials rather than revoked SOPS values. Record a deadline for aborting a partial gate and resume scheduled convergence and workloads only after the chosen forward or rollback state passes verification. -->

## Skill phases (feature-implementation)

Phase 0 ground (footprint grep, precedents #746 / `variables.tf` history, live budgets and
per-worker activity), Phase 1 plan (this file; codex round via LiteLLM, max 2). Phases 2–4 per PR
after approval; each gate waits for the operator. Skipped: UI proposal (no UI).

<!-- codex-review-status: complete -->