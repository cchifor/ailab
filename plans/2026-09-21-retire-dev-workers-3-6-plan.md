# Retire dev-worker-6 and dev-worker-3; re-slot the survivors to dev-worker-1..4 (.8–.11)

Repo `ailab` · branch `ops/retire-dev-worker-3-6` off `gitea/main` `9b775872` (worktree
`.worktrees/retire-dw36`) · parent plan: `plans/2026-09-20-env-pool-root-cause-followup-plan.md`
(on `main`) §T4a (the RAM for `env-node-2`, §T4b) · precedent: `ci-runner-7` retirement, PR #746
(`af4cca83`).

<!-- codex: This checkout contains the review input at repository root, not at the requested plans/ path, and the referenced parent plan is absent. This is a repository/precedent review, not live verification; make the parent's ENV_POOL/T4a/T4b acceptance criteria available before approving G3b. -->
<!-- opus-pushback: The parent plan is tracked at plans/2026-09-20-env-pool-root-cause-followup-plan.md on main (merged in #806/#807) and its T4a/T4b criteria are quoted in Context below; the dispatcher's worktree layout is not a property of this plan. -->

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
combination whose *estimate* passes the test (`25.2 + 16.3 + 3.7 ≈ 45 GiB`; dw3 alone = 41.5, dw6
alone = 29) while keeping the required **four** workers. **dev-worker-3 is busy with implementation
tasks and is not touched until the operator says it is free.** The survivors are **renamed to close
the gaps and re-addressed contiguously**: `dev-worker-1..4` on `.8`–`.11`.

**The estimate is not the evidence.** `MemAvailable + QEMU RSS` says what a destroy *should* release;
the G3b input is a **measurement**: after G3a-2, `min_over_time(node_memory_MemAvailable_bytes
{instance="192.168.0.4:9100"}[24h])` with (a) the model idle for the whole window — `llama-server`
RSS in `ai-llm-3` < 2 GiB at every sample (`process_resident_memory_bytes` / `lxc-exec` spot checks
at start, middle, end), (b) at least one CI run on ci-runner-3 and one on ci-runner-6 inside the
window (runner load is part of the floor), (c) ≥ 95 % of the window's 30 s samples present. A
window that fails (a)–(c) is re-run, not read. The result must be ≥ 43 GiB; if it is not, the
fallback (§Not in scope) is decided by the operator before any `env-node-2` work.

### What exploration established (live, 2026-09-21)

| VM | node | vmid | IP | floor | 7-d CPU | load | state |
|---|---|---|---|---|---|---|---|
| dev-worker-1 | ai-node1 | 4201 | .8 | 12 GiB | 31.8 % | — | active |
| dev-worker-2 | ai-node2 | 4202 | .9 | 4 GiB | 1.7 % | 0.2 | near idle |
| **dev-worker-3** | ai-node3 | 4203 | .10 | 12 GiB | 24.6 % | 1.8 | **busy**: 11 tmux sessions, compose stacks (`model-linked-qualification`, `storage-browser`), text-embeddings (1.2 GiB), codex |
| dev-worker-4 | ai-node1 | 4204 | .11 | 12 GiB | 25.0 % | 2.0 | active, 2 tmux sessions |
| dev-worker-5 | ai-node2 | 4205 | .12 | **6 GiB** | 27.7 % | 2.3 | active, **herdr pilot host** (`herdr` active) |
| **dev-worker-6** | ai-node3 | 4206 | .13 | 4 GiB (12 GiB ceiling) | 1.7 % | 0.01 | **idle**: one `claude` tmux session, k9s, bao; the 12 GiB-ceiling POC; herdr takeover done 2026-09-03 |

- **A worker is a *slot* plus a *VM*.** The slot name `dev-worker-N` (`dwN`) is the key of
  everything around the VM: the tofu map key (`dev_worker_nodes`), the Ansible `inventory_hostname`
  (and through it the OpenBao AppRole + policy `dev-worker-N`, KV subtree `af/dev-workers/dev-worker-N`,
  the `tep-dwN` ServiceAccount + token, the `helmtest-dwN` namespace/SA/RBAC/NetworkPolicy, the
  restic repo `dev-worker-dev-worker-N`, `AF_WORKER_NAME` in the guest's `agentforge.env`, Caddy's
  fact-derived site and ttyd's inventory-derived title), the terminal `dwN.chifor.me` (Cloudflare DNS
  + Access app in `infra/cloudflare`, tunnel ingress in `apps/edge/cloudflared.yaml`, homepage
  tile), the monitoring targets (`dev-workers-node.yaml`, `agentforge.yaml`), the
  `openbao-k8stoken-sync` target list (`range(1, 7)` in the job, RBAC per namespace,
  `resourceNames` for `tep-dw?`), `scripts/tep-render-kubeconfigs.py` (`range(1, 7)`) and the seat
  map (ADR 0018: Max#1 dw1/dw2, Max#2 dw3/dw4, Codex Pro dw5/dw6). The VM is identified by its vmid;
  seat logins (`~/.claude`, `~/.codex`), workspaces and tmux state live in the VM. The AgentForge v1
  runtime on the workers is dead (`monitoring/agentforge.yaml`: the `agentforge` job has zero active
  targets; AgentForge v2 runs on the Talos agent nodes), so the only v1 residue is the rendered
  `agentforge.env`, re-rendered by the role from `inventory_hostname`; the Gitea org webhooks point
  at the in-cluster broker, not at workers — verified at G3a-1 pre-flight by listing them.
- **Renaming a VM into a lower slot needs no rebuild, but it is a state migration, not a `moved`
  block.** The map key is the `for_each` key, so a bare edit would be destroy + create (the reason
  #746 refused to renumber runners). `moved` blocks cannot express this permutation either: slot 3
  is occupied (4203 is still in state when C2 is planned) and `4 → 3` + `5 → 4` read as one chain
  `5 → 4 → 3`. The re-key is a **rehearsed `tofu state mv` sequence** on a backed-up copy of the
  authoritative state, through a temporary key that the config never declares:
  `["dev-worker-3"] → ["retired-4203"]`, `["dev-worker-4"] → ["dev-worker-3"]`,
  `["dev-worker-5"] → ["dev-worker-4"]`; the C2 config then plans as *destroy `retired-4203`,
  update `name` on 4204 and 4205, nothing else*. `name` updates in place (`qm set --name`);
  `initialization` (IP, hostname) is `ignore_changes`, so the live IP/hostname are changed
  **in-guest** as the 2026-08 renumber to `.8`–`.13` was done (add the new IP live, rewrite
  `/etc/netplan/50-cloud-init.yaml` — `99-disable-network-config.cfg` is already present on dw4/dw5
  — `netplan try`, then `netplan apply`) **plus the third edit `CLAUDE.md` insists on**:
  `qm set <vmid> --ipconfig0 ip=<new>/24,gw=192.168.0.1` + `qm cloudinit update <vmid>`, so
  tofu-blind cloud-init metadata cannot re-apply the old address on a later reboot; the cloud-init
  hostname follows the VM `name` that tofu sets, and `hostnamectl` sets it now.
- **vmids cannot be renamed in place** (Proxmox has no `qm rename`; a vmid change in tofu is
  destroy + create of the 40 + 128 GiB disks; the manual `mv 4204.conf` + `lvrename` route lives
  outside tofu and ends in `state rm` + `import`). The re-slotted VMs keep 4204 / 4205; the
  `vmid == 4200 + N` invariant is dropped and documented in the IPAM table. (Escalated at G3a-2 —
  default: keep.)
- **Slots 1–4 keep their IPs, so most slot artifacts do not move.** After the re-slot, `dw3` is
  still `.10` and `dw4` still `.11`: the tunnel ingress, homepage, DNS/Access, monitoring targets,
  helmtest namespaces and tep SAs of slots 3 and 4 are **unchanged**; only slots 5 and 6 are
  removed, and the *credentials* of slots 3 and 4 rotate because different machines now hold them
  (see "Credentials" below).
- **Flux prunes** (`clusters/ai/helmtest.yaml` `prune: true`, `wait: false`). The Kyverno guard
  `helmtest-protect-reserved` excludes only the two Flux controllers: Flux itself deletes the
  objects it manages, but the **namespace controller** deletes whatever is left (chart-created
  NetworkPolicies, quotas) and would be **denied**, leaving `helmtest-dwN` `Terminating`. The PR
  therefore adds `system:serviceaccount:kube-system:namespace-controller` and
  `…:generic-garbage-collector` to the guard's `exclude` (they act only while a namespace is already
  being deleted by cluster-admin, which the guard never protected against), and the gate checks
  `helm ls -n helmtest-dw6` is empty **before** merge (a release is uninstalled by the worker, which
  is allowed, or its PVC data is copied off).
- **Credentials.** Issuing new credentials revokes nothing: an AppRole *periodic* token (72 h,
  renewed by the worker's `bao agent`), a SecretID, a 30-day TokenRequest token (`tep-dwN`,
  `helmtest-dwN`) all stay valid after a re-mint. Retired slots therefore get an explicit
  **revocation** (SecretID accessors destroyed, issued token accessors revoked by role, role + policy
  deleted, KV subtree removed with its metadata and descendants — KV v2 `kv delete` is a soft delete
  of one version and is not recursive), and reassigned slots get **rotation with revocation of the
  previous holder's material**. The owner of the retired-slot revocation is the
  `devworker-provision-job` (declarative, idempotent, re-created by Flux after its 24 h TTL): a
  `RETIRED_SLOTS` list in its ConfigMap, processed *before* the upsert loop, so a seed or a stale
  token-sync run cannot resurrect a retired slot — which also means the KV retention decision is
  taken **before merge**, because the Job may run at the first reconcile after it.
- `af/dev-workers/dev-worker-3` holds an estate-class credential (`strive_test_user/password`,
  `openbao-estate-credentials.md:102`) and the seed job patches **seed-wins**: its destination (stay
  with slot 3 = inherited by ex-dw4, or move to another slot) is decided by the operator **before
  PR-C2 merges** and expressed in `devworker-seeds.sops.yaml` (removing a seed field does not
  remove the KV field — the ceremony deletes it explicitly if it moves).
- **Backups.** `dev_worker_enable_restic` is `false` in the role defaults and no `host_vars`
  override it (verified at pre-flight with `ansible-inventory --host`), and no backup timer exists
  on dw3/dw6 (`systemctl list-timers` at pre-flight) — there is no automatic copy to fall back on.
  The copy-off before a destroy is recorded (what, to where, a restore spot-check) and covers
  unpushed git work (`git status` in every repo under `/workspace`), compose volumes (`docker volume
  ls`), and any helmtest release/PVC data the worker still needs.
- #746's rules carry over: a retirement is a **drain, not a kill**; the monitoring endpoint is
  removed **in the same change** (a target left behind fires `NodeExporterDown` forever); the VM keeps
  running until the PR merges, so nothing scrapes a dead address; `tofu plan` must show exactly the
  expected destroy count and nothing else. `stop_on_destroy = true` is a *stop*, not an ACPI
  shutdown, so the VM is shut down explicitly (`qm shutdown`, wait for `stopped`) before the apply.

## Approach

Two PRs, one per VM, because dw3's timing is the operator's and dw6 can go now. Each PR is
repo-complete for its slot(s) (tofu + Flux manifests + inventory/secrets + docs in one change) and
is followed by an ordered apply sequence at its gate. **G3a-2 starts only after V-C1 has passed and
PR-C2 is rebased on the merged PR-C1**, so C2's destroy count, credential inventory and rollback
baseline never include unfinished C1 work.

### PR-C1 — retire dev-worker-6 (slot 6), gate G3a-1 (now)

Repo change (all in one PR):

- `kubernetes/infra/dev-workers/variables.tf`: drop the `dev-worker-6` map entry with a dated
  retirement comment (the #746 form); the 12 GiB-ceiling POC note becomes history (its outcome:
  the 12 GiB ceiling was never the constraint — dw6 idled at 3.7 GiB RSS); placement note → dw1/4 on
  node1, dw2/5 on node2, **dw3 alone on node3 until PR-C2** (after which node3 hosts no dev-worker
  and, after G3b, `env-node-2`).
- `kubernetes/infra/cloudflare/{access.tf,variables.tf}`: `dw6` out of the Access `for_each` set and
  of `tunnel_hostnames` (DNS record + Access app destroyed by that module's apply).
- `kubernetes/apps/apps/edge/cloudflared.yaml` (ingress `dw6.chifor.me` removed **and the
  `chifor.me/config-revision` annotation bumped** — the static ConfigMap does not reload running
  connectors; the rollout of both connectors is waited for before the Access app is deleted),
  `apps/homepage/configmap.yaml` (tile), `monitoring/dev-workers-node.yaml` + `monitoring/agentforge.yaml`
  (`.13` targets), `infrastructure/helmtest/{namespaces,rbac,networkpolicy}.yaml` (`helmtest-dw6`
  objects), `infrastructure/helmtest/kyverno-protect-reserved.yaml` (the namespace-controller /
  garbage-collector `exclude`), `security/openbao/k8stoken-sync.yaml` (`range(1, 7)` → the live
  slot list, `helmtest-dw6` RBAC and `tep-dw6` in `resourceNames` removed — otherwise the sync targets
  a missing identity and fails before publishing anything; after C1 it must report **10 fields**
  written/validated, after C2 **8**), `security/openbao/devworker-provision-job.yaml` (host loop
  without `dev-worker-6`; the `k8stoken-sync` policy loses the `dev-worker-6` paths; the new
  `RETIRED_SLOTS` revocation step), `testpool/tep-access.yaml` (`tep-dw6` SA, token Secret,
  RoleBinding subject), `backup/velero/helmrelease.yaml` (the "6 Secrets tep-dw1..dw6" comments →
  5, then 4 in PR-C2), `scripts/tep-render-kubeconfigs.py` (`range(1, 7)` → the live slot list, the
  OpenBao ownership marker untouched).
- `inventory/hosts.yml` (host removed, dated comment), `ansible/host_vars/dev-worker-6.yml`
  (deleted), `ansible/secrets/{dev-worker,tep-tokens}.sops.yaml` and
  `security/openbao/devworker-seeds.sops.yaml` (the `dev-worker-6` keys removed via `sops`, so the
  files stay decryptable and the MAC stays valid; validated with `sops -d … > /dev/null`),
  `ansible/roles/dev_worker/tasks/tep.yml` comment, `scripts/fleet-converge-daily.sh` (the dw6
  `--skip-tags herdr` special case goes), `scripts/oom-protect-guests.sh` (4206 out of the list).
  (`.gitea/workflows/dev-worker-scripts.yaml` and `scripts/validate-codex-fleet.sh` only mention
  workers in comments — untouched.)
- Docs: `docs/runbooks/dev-workers.md` (table, POC section closed, herdr pilot = dw5 only),
  `docs/network-plan.md` (`.13` free), `CLAUDE.md` inventory row (`.8–.12`, `4201–4205` until PR-C2),
  `README.md` (its dev-worker row is stale — `.37/.38/.39`, `4201–4203` — corrected to the live
  fleet), `docs/runbooks/agentforge.md` + ADR 0018 seat map (Codex Pro = dw5 only), ADR 0020 ("six"
  → the live count, as an amendment note, not a rewrite), `docs/runbooks/openbao-dev-workers.md`
  table + the revocation step, `docs/runbooks/passkeys.md` / `cloudflare-access-apps.md` lists.

Gate G3a-1 sequence (operator present; each step read back before the next):

1. **Pre-flight (state and context).** The authoritative states are the gitignored local backends
   in the **main checkout** (`kubernetes/infra/dev-workers/terraform.tfstate`,
   `kubernetes/infra/cloudflare/terraform.tfstate`): both copied to `_out/<module>.tfstate.<stamp>.before-G3a-1`;
   `tofu init` in each module from the worktree with `-backend-config="path=<main checkout path>"`
   and **no provider upgrade** (`.terraform.lock.hcl` unchanged); `tofu state list` + `state show`
   confirm `["dev-worker-6"]` = node `ai-node3`, vmid 4206, disks `vm-4206-disk-*`; `kubectl config
   current-context` = `admin@ai`; the Cloudflare account/zone of the plan output match the live
   zone; every edited SOPS file decrypts; Flux Kustomizations `helmtest`, `testpool`, `security`,
   `monitoring`, `edge`, `apps` are Ready at the current revision; OpenBao unsealed and the last
   `openbao-k8stoken-sync` run succeeded (12 fields).
2. **Pre-flight (activity, refreshed at the gate, not from the survey).** On dw6: `who`, `tmux
   list-sessions`, `pgrep -af "claude|codex|herdr"`, `docker ps`, `docker compose ls`, `tep list`
   (no lease held by dw6), `helm ls -n helmtest-dw6` (empty or uninstalled by the worker first).
   New intake is stopped before sessions are closed (herdr is not on dw6; the `claude` tmux session
   is ended by the operator). The operator confirms in writing that nothing on dw6 must survive, or
   the copy-off (Context) is done and spot-checked. Gitea org webhooks listed: none targets a worker.
3. **Plans on the branch:** `tofu plan` (dev-workers) = `0 to add, 0 to change, 1 to destroy`, keyed
   `["dev-worker-6"]`, saved (`-out`); `tofu plan` (cloudflare) = exactly the `dw6` DNS record + Access
   app to destroy, saved.
4. **Merge PR-C1**, then the **reconciliation barrier** — merge orders nothing across Flux
   Kustomizations and helmtest has `wait: false`, so each of these is verified at the merged revision
   before anything is destroyed: `flux get kustomizations` all Ready at `main@<merge sha>`;
   `helmtest-dw6` namespace gone (not `Terminating`); `tep-dw6` SA and `tep-dw6-token` Secret gone;
   the k8stoken-sync Job re-ran on the new manifest and reports 10 fields; Prometheus has **no**
   `192.168.0.13:9100` / `:9464` targets (`/api/v1/targets`); both `cloudflared` connectors rolled to
   the new revision and `curl -sI https://dw6.chifor.me` no longer reaches an origin; the homepage
   tile gone.
5. **Shut down, then destroy.** `qm shutdown 4206` and wait for `qm status 4206` = `stopped` (abort
   on timeout, never `qm stop` a VM that is still writing); then `tofu apply` of the **saved**
   dev-workers plan (destroys 4206 incl. both disks and the cloud-init disk); `tofu apply` of the
   saved cloudflare plan (removes `dw6.chifor.me` DNS + Access).
6. **OpenBao (breakglass, `openbao-dev-workers.md`).** The declarative revocation already ran if the
   provision Job reconciled after merge (its log is read: `retired dev-worker-6: N secret-id
   accessors destroyed, M tokens revoked, role/policy deleted, KV metadata deleted`); if not, the
   Job is triggered (`kubectl create job --from`) and its log read. Then the negative test: the old
   `role_id`/`secret_id` pair from the pre-merge SOPS file fails to log in (checked without printing
   either value), and `bao kv get af/dev-workers/dev-worker-6` = not found (metadata deleted, the
   `common` subtree untouched).
7. **Ansible.** No convergence of the busy dw3. The inventory/secrets edits are proven with
   `ansible-inventory --graph` (5 hosts), `ansible-playbook dev-workers.yml --syntax-check`, and a
   `--check --diff --limit dev-worker-2` run (idle host) that reports no changes; a full run stays
   scheduled for the daily converge, whose 06:35 job is left untouched in C1.
8. Verify (V-C1 below); `MemAvailable` on ai-node3 read after the destroy and recorded.

### PR-C2 — retire dev-worker-3 (slot 3's VM) and re-slot dw4 → 3, dw5 → 4, gate G3a-2 (when dw3 is free)

Starts only when: V-C1 passed; the operator declares dev-worker-3's implementation tasks finished and
its state copied off (compose volumes, repos, tmux); **and an idle window is agreed for dw4 and dw5
too** — both are active and both reboot during the re-slot, herdr's resume and tmux persistence do
not preserve arbitrary live processes (the pilot host's herdr panes are saved/drained first).

Repo change:

- `variables.tf`: `dev-worker-3 = { ai-node1, vm_id = 4204, ip = .10, hostname = dev-worker-3,
  memory_floating_mib = 12288 }` (ex-dw4 keeps the node1 12 GiB floor), `dev-worker-4 = { ai-node2,
  vm_id = 4205, ip = .11, hostname = dev-worker-4, memory_floating_mib = 6144 }` (ex-dw5 **keeps its
  6 GiB floor** — the swap-starvation mitigation), `dev-worker-5` and the old `dev-worker-3` (4203)
  removed; the vmid-invariant and placement comments rewritten ("slot N ≠ vmid 420N since
  2026-09-2x"); **no `moved.tf`** — the state migration is the rehearsed `state mv` sequence in the
  gate. The expected plan after the migration: `1 to destroy` (`retired-4203`), `2 to change` (`name`
  only, on 4204 and 4205 — node, vmid, memory, disks and network device unchanged), `0 to add`.
- Slot-5 removal = the PR-C1 list applied to `dw5`/`.12`/`tep-dw5`/`helmtest-dw5`/`dev-worker-5`
  (Cloudflare `dw5`, tunnel ingress + revision bump, homepage, monitoring, helmtest, k8stoken-sync
  (8 fields), tep-access, tep-render-kubeconfigs, provision job loop + policy + `RETIRED_SLOTS`,
  inventory, host_vars `dev-worker-5.yml` → **renamed** `dev-worker-4.yml` (the herdr pilot config
  follows the host), seeds, docs incl. the IPAM table and `CLAUDE.md` (`.8–.11`, vmids
  `4201, 4202, 4204, 4205`), the ADR 0018 seat map (Max#1 dw1/dw2, Max#2 dw3 = ex-dw4, Codex Pro
  dw4 = ex-dw5), `agentforge.md`, `openbao-dev-workers.md` table). vmid lists are edited by
  **value**, not by slot: `scripts/oom-protect-guests.sh` loses 4203 and keeps 4204/4205;
  `scripts/dw-paste.ps1`'s default `c4@192.168.0.12` becomes `.11` (the same machine).
- Secrets for the reassigned slots are **minted before merge and committed encrypted in C2**: new
  SecretIDs against the *destination* roles `dev-worker-3` / `dev-worker-4` (role_ids belong to the
  roles and do not move between YAML keys; the old pairs are not re-keyed), new `tep-dw3`/`tep-dw4`
  tokens (below), so the merged revision is complete and no post-merge commit is needed.
- The slot-3/4 artifacts that stay: `dw3`/`dw4` Cloudflare + tunnel + homepage + monitoring targets
  (`.10`/`.11` unchanged), `helmtest-dw3`/`dw4` namespaces, `tep-dw3`/`dw4` SAs.

  <!-- codex: Reissuing TokenRequest tokens does not invalidate previous tokens: this sync requests 30-day tokens for both TEP and helmtest, and old dw4 also retains slot-4 credentials when becoming dw3. Define a narrowly scoped, Kyverno-compatible replacement of the slot-3/4 ServiceAccounts with new UIDs, including legacy TEP token Secrets, then remint; ordinary administrator deletion of protected helmtest SAs is denied by the guard. -->
  <!-- opus-pushback: Only two machines ever held slot-3/4 tokens: 4203, which is destroyed (its tokens have no holder), and 4204, which becomes dw3 and is the same trusted survivor whose rendered kubeconfigs the converge overwrites. The unguarded `tep-dw3`/`tep-dw4` SAs are recreated (delete → Flux re-creates → new UID invalidates every earlier token) at no cost; replacing the guarded `helmtest-dw3`/`dw4` SAs would mean loosening the guard for a token whose only holder is a VM we are about to delete — the plan records the residual (4204's old slot-4 helmtest token, 30-day expiry, held by a survivor) instead of touching the guard. -->

Gate G3a-2 sequence:

1. **Pre-flight.** V-C1 passed; PR-C2 rebased on merged C1; the operator's written "dev-worker-3 is
   free and empty" plus the copy-off record; the idle window for dw4/dw5 agreed and their work
   saved/drained (herdr paused on dw5: `systemctl stop herdr`, panes saved); **the daily converge is
   suspended** (`schtasks /Change /DISABLE` on the Windows task that runs
   `~/.ailab-converge/fleet-converge-daily.sh`, verified with `/Query`) and stays disabled until the
   merged inventory and credentials are deployed — old `main` maps `.10`/`.11` to different machines
   and a converge in between would install the wrong identities; state and context checks as in
   G3a-1 step 1 (state copies `…before-G3a-2`); each survivor's SSH host-key fingerprints captured
   **now**, while their old identities are trusted (`ssh-keyscan` from the controller, compared to
   `ssh-keygen -lf` inside the guest); Proxmox console access to 4204 and 4205 tested (`qm terminal`)
   before any SSH-disrupting change; staged copies of the current netplan/hostname files kept.
2. **Rehearsal.** On an isolated copy of the dev-workers state: the three `tofu state mv` commands
   (`["dev-worker-3"] → ["retired-4203"]`, `["dev-worker-4"] → ["dev-worker-3"]`,
   `["dev-worker-5"] → ["dev-worker-4"]`) against the C2 config, then `tofu plan` = the expected
   `1 destroy / 2 change (name) / 0 add` with node/vmid/memory/disks/network unchanged on both
   survivors. Only then is the same sequence run on the authoritative state (backup taken first);
   the plan is **regenerated** after the live migration and inspected again — a saved plan from before
   a state change is stale.
3. **Drain, not kill:** `qm shutdown 4203`, wait for `stopped` (abort on timeout — no forced stop),
   `qm set 4203 --onboot 0`; then prove `.10` has no owner (`arping -c 3 192.168.0.10` from a
   host, no reply) before it is reassigned.
4. **Re-IP 4204 → slot 3.** Credential-consuming services quiesced first (`systemctl stop
   bao-agent` and any claude-job/herdr units on the host, so nothing resumes work under slot-4
   credentials after the reboot); then, over SSH on `.11`: add `.10` as a second address, `netplan
   try` → `netplan apply`, confirm SSH on `.10` **using the fingerprint captured in step 1** (the
   controller's `known_hosts` entries for `.10`/`dev-worker-3` updated from that evidence, not
   blindly accepted), drop `.11`; `hostnamectl set-hostname dev-worker-3` + `/etc/hosts`; the third
   edit on the host: `qm set 4204 --ipconfig0 ip=192.168.0.10/24,gw=192.168.0.1` +
   `qm cloudinit update 4204`; reboot, confirm hostname + IP survive. Then **4205 → slot 4** the same
   way (`.12` → `.11`, only after 4204 has left `.11`; `qm set 4205 --ipconfig0 …`). During this
   window the `.12` scrape target is dead until C2 reconciles: a **bounded maintenance window**
   with a scoped Alertmanager silence (`instance="192.168.0.12:9100"`, `NodeExporterDown`, ≤ 2 h)
   and a hard abort deadline — if C2 has not merged and reconciled by then, step 4 is reversed
   (rollback B below) before the silence expires.
5. **Merge PR-C2**, reconciliation barrier as in G3a-1 (slot 5 pruned everywhere; both connectors
   rolled; no `.12` targets; k8stoken-sync at 8 fields **after** the SA recreation below);
   `tofu apply` of the regenerated dev-workers plan (destroy 4203; `name` on 4204/4205) and of the
   cloudflare plan (`dw5`).
6. **Identities.** Retired slot 5: the declarative revocation (Job log read, negative login test,
   KV gone). Reassigned slots 3/4: the *previous* SecretID accessors of roles `dev-worker-3` and
   `dev-worker-4` destroyed and their issued token accessors revoked (by role, via
   `auth/token/accessors` lookups — never by the `auth/approle/login` path prefix, which would revoke
   every worker); `tep-dw3` and `tep-dw4` SAs deleted and re-created by Flux (new UIDs invalidate
   every earlier TokenRequest token, including 4203's); the k8stoken-sync Job run explicitly (the
   provision Job does not run it) and its 8 fields validated; then the converge **limited to the two
   re-slotted hosts** (`--limit dev-worker-3,dev-worker-4`) renders the new AppRole login, tep
   token, helmtest kubeconfig, `agentforge.env`, Caddy site and ttyd title; `bao-agent`/herdr
   started again only after that; the estate credential's destination (Context) applied; the daily
   converge re-enabled.
7. Verify (V-C2); then the 24 h floor measurement (Context) that gates T4b (G3b).

### Not in scope

Retiring or shrinking `ci-runner-3`/`-6` (24 GiB each — the largest block on node3) stays the
fallback if the measured floor still misses 43 GiB; `env-node-2` itself (PR-B, §T4b).

## Critical files

| Path | PR | Role |
|---|---|---|
| `kubernetes/infra/dev-workers/variables.tf` | C1, C2 | map entries (floors preserved), retirement comments, placement/vmid notes |
| `kubernetes/infra/cloudflare/{access.tf,variables.tf}` | C1, C2 | `dw6` / `dw5` Access apps + DNS |
| `kubernetes/apps/apps/edge/cloudflared.yaml`, `apps/homepage/configmap.yaml` | C1, C2 | tunnel ingress + `config-revision` bump, tiles |
| `kubernetes/apps/infrastructure/monitoring/{dev-workers-node,agentforge}.yaml` | C1, C2 | `.13` / `.12` targets (same change as the retirement) |
| `kubernetes/apps/infrastructure/helmtest/{namespaces,rbac,networkpolicy,kyverno-protect-reserved}.yaml` | C1, C2 | `helmtest-dw6` / `dw5`; the namespace-controller exclude |
| `kubernetes/apps/infrastructure/security/openbao/{k8stoken-sync,devworker-provision-job}.yaml`, `devworker-seeds.sops.yaml` | C1, C2 | slot loop + RBAC, provision loop + `RETIRED_SLOTS` revocation, seeds |
| `kubernetes/apps/infrastructure/testpool/tep-access.yaml`, `scripts/tep-render-kubeconfigs.py` | C1, C2 | `tep-dw6` / `dw5`; the recovery renderer's slot list |
| `kubernetes/apps/backup/velero/helmrelease.yaml` | C1, C2 | Secret-count comments |
| `inventory/hosts.yml`, `ansible/host_vars/*`, `ansible/secrets/{dev-worker,tep-tokens}.sops.yaml`, `ansible/roles/dev_worker/tasks/tep.yml` | C1, C2 | hosts, per-host config, credentials (new values for slots 3/4 in C2) |
| `scripts/{fleet-converge-daily,oom-protect-guests}.sh`, `scripts/dw-paste.ps1` | C1, C2 | host / vmid lists, default target |
| `docs/runbooks/{dev-workers,agentforge,openbao-dev-workers,cloudflare-access-apps,passkeys}.md`, `docs/network-plan.md`, `CLAUDE.md`, `README.md`, ADR 0018 / 0020 | C1, C2 | the fleet as documented |

## Verification

- **V-C1:** `tofu plan` (dev-workers **and** cloudflare) = `No changes.` after the applies; `qm list`
  on ai-node3 has no 4206; `lvs` shows no `vm-4206-*`; Prometheus `/api/v1/targets` has no
  `192.168.0.13` target for either job and `up{instance=~"192.168.0.13:.*"}` is **absent** (not 0),
  no `NodeExporterDown`; `kubectl get ns helmtest-dw6` / `sa tep-dw6 -n testpool` /
  `secret tep-dw6-token` gone; k8stoken-sync last run = 10 fields OK; `bao list auth/approle/role`
  and `bao policy list` without `dev-worker-6`, the old pair's login denied, KV metadata gone;
  `dw6.chifor.me` absent from the **authoritative** zone (Cloudflare API / `dig @<zone ns>`, not a
  resolver cache), neither connector routes it, the Access app gone; `ansible-inventory --graph` =
  5 hosts; ai-node3 `MemAvailable` ≥ 25.2 + ~3.7 GiB with the model idle (recorded, not yet the G3b
  evidence).
- **V-C2:** the V-C1 checks repeated for slot 5 and VM 4203 (no `vm-4203-*`/`vm-4203` config, no
  `.12` targets, `helmtest-dw5`/`tep-dw5` gone, `dw5.chifor.me` gone from the zone, slot-5 OpenBao
  role/policy/KV gone and its old pair denied); `tofu plan` = `No changes.`; on `.10`: `hostname` =
  `dev-worker-3` **and** the VM behind it is 4204 (the MAC of `.10` in the host's ARP table = 4204's
  `net0`), on `.11` = `dev-worker-4` = 4205's MAC; `.12`/`.13` unanswered (ARP); `qm config` shows
  `name: dev-worker-3` on 4204 with `balloon: 12288`, `name: dev-worker-4` on 4205 with
  `balloon: 6144`; herdr active on the new dw4 with usable sessions; on both re-slotted hosts a
  **fresh** AppRole login as the destination role (not `cred list`, which only proves the shared
  metadata grant), `cred exec` of a slot-scoped field, denied read of the other slot's subtree, the
  old SecretID pair denied; `tep` acquire + release from each; a helm install + uninstall in
  `helmtest-dw3`/`dw4` from each; the old `tep-dw3` token denied (SA UID changed); Kyverno still
  denies an admin delete of `helmtest-dw3`'s SA; `dw3.chifor.me`/`dw4.chifor.me` reach the right
  ttyd behind Access; monitoring `nodename` labels match; `ansible-inventory --graph` = 4 hosts; the
  dashboards' dev-worker rows render 4 workers; smoke resources cleaned up **before** the 24 h floor
  window starts; the floor measurement (Context) ≥ 43 GiB before G3b.
- Repo gates on every push: `scripts/manifest-lint.sh`, `scripts/rules-lint.sh`, the manifests
  workflow's unittest set; locally `sops -d <file> > /dev/null` for every edited secret (plaintext
  never printed), `tofu fmt -check` + `validate` for `dev-workers` and `cloudflare`, the rendered
  `kustomize build` of `helmtest`, `testpool`, `security`, `monitoring`, `edge` grepped for the
  retired slot (zero hits) and for every surviving slot (the expected hit counts), and one
  consistency grep across inventory, k8stoken-sync targets, provision policies and the per-slot
  secret keys (the same slot set everywhere). None of this substitutes for the state-backed plans
  or the runtime negative tests in the gates.

## Gates and rollbacks

| Gate | Mutation | Rollback |
|---|---|---|
| G3a-0 | this plan + codex review, PR-C1 opened | none |
| G3a-1 | merge PR-C1; reconciliation barrier; `qm shutdown 4206`; `tofu apply` dev-workers (destroy 4206) + cloudflare (`dw6`); declarative OpenBao revocation; inventory validation | **before the destroy:** revert the PR (Flux re-creates slot 6's cluster objects; the `RETIRED_SLOTS` entry is removed *first* so the provision Job re-mints instead of revoking), re-mint a SecretID for the re-created role, restore DNS/ingress only with the Access app present, `tofu plan` = no changes. **After the destroy:** the VM is gone — re-add the map entry, `tofu apply` creates a fresh 4206, Ansible bootstraps it, workspace restored from the copy-off; never apply the `before-G3a-1` state copy over a destroyed VM. |
| G3a-2 | converge suspended; rehearsal; `qm shutdown 4203`; live `state mv`; in-guest re-IP/rename of 4204, 4205 (+ `ipconfig0`); merge PR-C2; `tofu apply` (destroy 4203, rename); revocation/rotation; limited converge | **A — before the state migration:** nothing changed; re-enable the converge. **B — after the migration, before merge/apply** (incl. the re-IP window): reverse in the only collision-free order — 4205 back to `.12` (+ `ipconfig0`), then 4204 back to `.11`, then `.10` is free and 4203 may be started again; state reversed through the temporary key with the *current* vmids (`["dev-worker-4"] → ["dev-worker-5"]`, `["dev-worker-3"] → ["dev-worker-4"]`, `["retired-4203"] → ["dev-worker-3"]`) against the *old* config, `tofu plan` = no changes; expire the silence; re-enable the converge. **C — after apply/destroy:** forward is the only way for 4203 (rebuild from the module if the slot must come back); reversing the survivors means the in-guest steps of B, Proxmox names via the old map, inventory/host_vars/secrets from the pre-merge revision with **freshly minted** SecretIDs (the revoked ones cannot be restored), slot-5 Flux/Cloudflare objects re-created by reverting the PR, Access before DNS. Every partial gate has a written abort deadline (the silence length); workloads and the converge resume only after the chosen end state passes its verification. |
| G3b | (parent plan) `env-node-2` after the 24 h floor measurement | parent plan |

## Skill phases (feature-implementation)

Phase 0 ground (footprint grep, precedents #746 / `variables.tf` history, live budgets and
per-worker activity), Phase 1 plan (this file; codex round via LiteLLM, max 2). Phases 2–4 per PR
after approval; each gate waits for the operator. Skipped: UI proposal (no UI).

<!-- codex-review-status: complete -->
