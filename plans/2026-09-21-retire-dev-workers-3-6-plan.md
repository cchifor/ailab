# Retire dev-worker-6 and dev-worker-3; re-slot the survivors to dev-worker-1..4 (.8–.11)

Repo `ailab` · branch `ops/retire-dev-worker-3-6` off `gitea/main` `9b775872` (worktree
`.worktrees/retire-dw36`) · parent plan: `plans/2026-09-20-env-pool-root-cause-followup-plan.md`
§T4a (the RAM for `env-node-2`, §T4b) · precedent: `ci-runner-7` retirement, PR #746 (`af4cca83`).

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
- Backups: `dev_worker_enable_restic` is **false** in the role defaults — there is no automatic
  backup to fall back on. Anything on dw3/dw6 that must survive is copied off by hand before the
  destroy (gate).
- #746's rules carry over: a retirement is a **drain, not a kill**; the monitoring endpoint is
  removed **in the same change** (a target left behind fires `NodeExporterDown` forever); the VM keeps
  running until the PR merges, so nothing scrapes a dead address; `tofu plan` must show exactly the
  expected destroy count and nothing else.

## Approach

Two PRs, one per VM, because dw3's timing is the operator's and dw6 can go now. Each PR is
repo-complete for its slot(s) (tofu + Flux manifests + inventory/secrets + docs in one change) and
is followed by an ordered apply sequence at its gate.

### PR-C1 — retire dev-worker-6 (slot 6), gate G3a-1 (now)

Repo change (all in one PR):

- `kubernetes/infra/dev-workers/variables.tf`: drop the `dev-worker-6` map entry with a dated
  retirement comment (the #746 form); the 12 GiB-ceiling POC note becomes history (its outcome:
  the 12 GiB ceiling was never the constraint — dw6 idled at 3.7 GiB RSS); placement note → dw1/4 on
  node1, dw2/5 on node2, **none on node3** (node3 hosts `env-node-2`).
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
- `inventory/hosts.yml` (host removed, dated comment), `ansible/host_vars/dev-worker-6.yml`
  (deleted), `ansible/secrets/{dev-worker,tep-tokens}.sops.yaml` and
  `security/openbao/devworker-seeds.sops.yaml` (the `dev-worker-6` keys removed via `sops`, so the
  files stay decryptable and the MAC stays valid), `ansible/roles/dev_worker/tasks/tep.yml` comment,
  `scripts/fleet-converge-daily.sh` (the dw6 `--skip-tags herdr` special case goes),
  `scripts/oom-protect-guests.sh` (4206 out of the list). (`.gitea/workflows/dev-worker-scripts.yaml`
  and `scripts/validate-codex-fleet.sh` only mention workers in comments — untouched.)
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
2. Merge PR-C1 → Flux prunes `helmtest-dw6`, `tep-dw6` (+ token Secret), the k8stoken-sync objects,
   the `.13` scrape targets, the tunnel ingress and the homepage tile; the reconciled revision is
   recorded.
3. `tofu apply` (dev-workers) destroys 4206 (`stop_on_destroy = true` → a clean stop, then delete
   incl. both disks and the cloud-init disk); `tofu apply` (cloudflare) removes `dw6.chifor.me`.
4. OpenBao ceremony (`openbao-dev-workers.md`, breakglass): `bao auth/approle/role/dev-worker-6`
   deleted, policy deleted, `af/dev-workers/dev-worker-6` KV subtree deleted (after reading it back
   once for anything the operator wants to keep); the provision job re-run from the merged manifest.
5. `ansible-playbook dev-workers.yml --limit dev_workers` converges the remaining hosts (nothing
   should change on them; the run proves the inventory/secrets edits are consistent).
6. Verify (V-C1 below); `MemAvailable` on ai-node3 read after the destroy.

### PR-C2 — retire dev-worker-3 (slot 3's VM) and re-slot dw4 → 3, dw5 → 4, gate G3a-2 (when dw3 is free)

Starts only when the operator declares dev-worker-3's implementation tasks finished and its state
copied off (compose volumes, repos, tmux). Repo change:

- `variables.tf`: `dev-worker-3 = { ai-node1, vm_id = 4204, ip = .10, hostname = dev-worker-3,
  memory_floating_mib = 12288 }` (ex-dw4 keeps the node1 12 GiB floor), `dev-worker-4 = { ai-node2,
  vm_id = 4205, ip = .11, hostname = dev-worker-4 }` (ex-dw5), `dev-worker-5` and the old
  `dev-worker-3` (4203) removed; `moved.tf` (new) with the two `moved` blocks; the vmid-invariant and
  placement comments rewritten ("slot N ≠ vmid 420N since 2026-09-2x").
- Slot-5 removal = the PR-C1 list applied to `dw5`/`.12`/`tep-dw5`/`helmtest-dw5`/`dev-worker-5`
  (Cloudflare `dw5`, tunnel, homepage, monitoring, helmtest, k8stoken-sync, tep-access, inventory,
  host_vars `dev-worker-5.yml` → **renamed** `dev-worker-4.yml` (it is the herdr pilot host's
  config and the host keeps herdr), secrets re-keyed `dev-worker-4/5` → `dev-worker-3/4` with **new**
  values minted at the gate, seeds, scripts, docs incl. the IPAM table and `CLAUDE.md`
  (`.8–.11`, vmids `4201, 4202, 4204, 4205`), the ADR 0018 seat map (Max#1 dw1/dw2, Max#2 dw3 =
  ex-dw4, Codex Pro dw4 = ex-dw5), `agentforge.md`, `openbao-dev-workers.md` table).
- The slot-3/4 artifacts that stay: `dw3`/`dw4` Cloudflare + tunnel + homepage + monitoring targets
  (`.10`/`.11` unchanged), `helmtest-dw3`/`dw4`, `tep-dw3`/`dw4` (tokens rotated — the old dw3 VM
  held `tep-dw3`'s token; the old dw5 VM held `tep-dw5`'s, which is deleted).

Gate G3a-2 sequence:

1. Pre-flight: operator's written "dev-worker-3 is free and empty"; `tofu plan` (dev-workers) on the
   branch = `1 to destroy` (`4203`), `2 to change` (`name` on 4204/4205), `0 to add`, the two
   `moved` lines shown; cloudflare plan = `dw5` record + app destroyed only.
2. **Drain, not kill:** `qm shutdown 4203` (ACPI works on Ubuntu) → `.10` is free.
3. In-guest on 4204 (still `dev-worker-4`, `.11`), over SSH: `hostnamectl set-hostname dev-worker-3`,
   `/etc/hosts`, netplan `.11` → `.10` (add `.10` as a second address first, `netplan apply`,
   confirm SSH on `.10`, then drop `.11`), reboot to prove it sticks. Then 4205: `dev-worker-5` →
   `dev-worker-4`, `.12` → `.11` the same way (only after 4204 has left `.11`).
4. Merge PR-C2 → Flux prunes slot 5; `tofu apply` (dev-workers: destroy 4203, rename 4204/4205;
   cloudflare: `dw5`).
5. OpenBao ceremony: delete role/policy/KV `dev-worker-5`; **rotate** `dev-worker-3` and
   `dev-worker-4` secret-ids (new machines hold them) and the tep tokens for `tep-dw3`/`tep-dw4`;
   decide the fate of `af/dev-workers/dev-worker-3.strive_test_*` (inherit or move); provision job
   re-run; `ansible-playbook dev-workers.yml` converges the re-slotted hosts under their new names
   (renders the new AppRole login, tep token, helmtest kubeconfig for `helmtest-dw3`/`dw4`; the
   restic repo URL follows the hostname — no restic is enabled, so nothing to migrate).
6. Operator's `~/.ssh/known_hosts`: the host keys behind `.10`/`.11` changed (they moved with the
   VMs) — `ssh-keygen -R` both, re-accept.
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
- **V-C2:** `tofu plan` = `No changes.`; `hostname` / `hostname -I` on `.10` = `dev-worker-3`, on
  `.11` = `dev-worker-4`; `.12` and `.13` unanswered (ping/ARP); `qm list` shows 4204 named
  `dev-worker-3`, 4205 `dev-worker-4`, no 4203; herdr active on the new dw4; `cred list` works on
  both re-slotted hosts with the new secret-ids; `tep` lease smoke test from the new dw3;
  `helmtest-dw3` kubeconfig on the new dw3 works; monitoring `nodename` labels match the new names;
  `ansible dev_workers -m ping` = 4 hosts; the dashboards' dev-worker rows render 4 workers;
  ai-node3's 24 h floor (model idle) **≥ 43 GiB** before G3b.
- Repo gates on every push: `scripts/manifest-lint.sh`, `scripts/rules-lint.sh`, the manifests
  workflow's unittest set, `sops` decrypt of every edited secret, `tofu fmt -check` + `validate` for
  `dev-workers` and `cloudflare` (no CI gate covers `kubernetes/infra/**/*.tf`).

## Gates

| Gate | Mutation | Rollback |
|---|---|---|
| G3a-0 | this plan + codex review, PR-C1 opened | none |
| G3a-1 | merge PR-C1; Flux prune; `tofu apply` dev-workers (destroy 4206) + cloudflare (`dw6`); OpenBao revoke; Ansible converge | VM rebuild from the module + Ansible (workspace lost — hence the pre-flight copy-off) |
| G3a-2 | operator declares dw3 free; `qm shutdown 4203`; in-guest re-IP/rename of 4204, 4205; merge PR-C2; `tofu apply` (destroy 4203, rename); OpenBao rotate/revoke; converge | re-IP back (in-guest), `moved` blocks reversed before any apply; 4203 rebuild from the module |
| G3b | (parent plan) `env-node-2` after the 24 h floor re-measure | parent plan |

## Skill phases (feature-implementation)

Phase 0 ground (footprint grep, precedents #746 / `variables.tf` history, live budgets and
per-worker activity), Phase 1 plan (this file; codex round via LiteLLM, max 2). Phases 2–4 per PR
after approval; each gate waits for the operator. Skipped: UI proposal (no UI).

<!-- codex-review-status: pending -->
