# Runbook: dev-worker VMs (Claude Code + Codex)

Six interactive developer VMs (`dev-worker-1..6`, **two per Proxmox node**) that run **Claude Code**
and **Codex** inside tmux, with the homelab claude-worker feature set ported to ailab's idiom: a
tofu module creates the VMs, the `dev_worker` Ansible role configures them.

- tofu: `kubernetes/infra/dev-workers/`
- role: `ansible/roles/dev_worker/` · playbook: `ansible/dev-workers.yml`
- inventory group: `dev_workers` (`.37/.38/.39` + `.5/.6/.7`) · secrets: `ansible/secrets/dev-worker.sops.yaml`

**The base spec is shared** — cores + ceiling + floor are module-wide scalars in
`kubernetes/infra/dev-workers/variables.tf` (`dev_worker_cores`, `dev_worker_memory_mib`,
`dev_worker_memory_floating_mib`); the `dev_worker_nodes` map carries identity plus two optional
per-worker overrides: `memory_floating_mib` (12 GiB floors on dw1/dw3 — node1 mitigation — and 6 GiB on dw4, node2) and
`memory_mib` (unused since dev-worker-6's retirement; it carried the 12 GiB-ceiling POC, see below).

| Host | Node | vmid | IP | Sizing |
|---|---|---|---|---|
| dev-worker-1 | ai-node1 | 4201 | 192.168.0.8  | 8 vCPU / 16 GiB (**12**–16 balloon, node1 floor) / 40+128 GiB |
| dev-worker-2 | ai-node2 | 4202 | 192.168.0.9  | 8 vCPU / 16 GiB (4–16 balloon) / 40+128 GiB |
| dev-worker-3 | ai-node1 | 4204 | 192.168.0.10 | 8 vCPU / 16 GiB (**12**–16 balloon, node1 floor) / 40+128 GiB |
| dev-worker-4 | ai-node2 | 4205 | 192.168.0.11 | 8 vCPU / 16 GiB (**6**–16 balloon, node2 floor) / 40+128 GiB |

**Slot ≠ vmid since 2026-09-23.** **dev-worker-6** (ai-node3, 4206, 192.168.0.13) was **retired
2026-09-21** and **slot 3's original VM** (ai-node3, 4203) on **2026-09-23**, both to fund the second
testpool env node on ai-node3; the survivors were re-slotted the same day to close the numbering
gap — vmid 4204 (ex-dw4) is `dev-worker-3`/`.10` and vmid 4205 (ex-dw5) is `dev-worker-4`/`.11`
(`plans/2026-09-21-retire-dev-workers-3-6-plan.md` — the retirement/re-slot procedure, gates and
rollbacks live there). No dev-worker runs on ai-node3 any more.

> **IP renumber (consecutive .8–.11).** cloud-init fixes the IP at create and the tofu module has
> `lifecycle.ignore_changes = [initialization]`, so the live IPs were changed **in-guest** (not by tofu):
> per worker — add the new IP live, rewrite the address in `/etc/netplan/50-cloud-init.yaml`, write
> `/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg` (`network: {config: disabled}`) so cloud-init
> won't revert it, then `netplan apply`. The map IPs above are kept in sync as documentation.
>
> **Learned at the 2026-09-23 re-slot (4204 → dev-worker-3/.10, 4205 → dev-worker-4/.11):**
> (1) the third edit — `qm set <vmid> --ipconfig0 … --name <slot>` + `qm cloudinit update` — gives
> the guest a NEW cloud-init instance-id, so the next boot re-runs the per-instance modules: cloud-init
> **regenerates the SSH host keys** and, with `preserve_hostname: false`, sets the hostname from the
> VM name. Set `--name` together with the address, expect `REMOTE HOST IDENTIFICATION HAS CHANGED`
> on the first SSH after the reboot, confirm the identity out of band (`ip neigh show <ip>` on the
> PVE host must be the VM's `net0` MAC; `/etc/machine-id` unchanged) and only then re-pin
> `known_hosts` — on the workstation AND in the WSL converge clone (`ansible.cfg` has
> `host_key_checking = True`). (2) A converge of a host whose `openbao-agent` was stopped on
> purpose fails at "Check that the vault is reachable under this host's own AppRole" (`cred list`
> needs the agent's token) before the play-end handler would start the agent: once the new
> credentials are in place, `systemctl enable --now openbao-agent` by hand, then converge.
> `enable --now` is the un-quiesce — it reverses both halves of the `disable --now` below (enabled
> at boot again AND started); `start` alone would leave the unit disabled after the next reboot.
> `systemctl mask` is not available for these units (they live in `/etc/systemd/system`);
> `disable --now` plus the disabled daily converge is the quiesce.

## Pre-flight gate (clear BEFORE `tofu apply`)

**On-demand heavyweight LLMs (the prerequisite for the 2nd worker per node).** The 2nd worker per
host fits only because the rarely-used heavyweight models on node2/node3 (gpt-oss ~59 GiB, Qwen3.5-122B
~71 GiB GTT) are now **idle-unloaded via llama-swap** rather than pinned resident — see
`docs/runbooks/ai-model-swap.md`. With the model idle, the host drops to ~45% used and ballooning
actually works, so a worker inflates toward the ceiling on demand. Dev-worker memory defaults to a
**16 GiB ceiling with a 4 GiB floor** (module scalars
`dev_worker_memory_mib` / `dev_worker_memory_floating_mib`; per-worker overrides on dw1/dw3 floors
and the dw4 floor, node2) — low floor by design, because ballooning
now inflates busy workers and 4 GiB is what lets a node hold its on-demand heavyweight **plus** its
two workers-at-floor at once.

(IPs `.37/.38/.39` + `.5/.6/.7` are free static addresses inside the `.2`–`.50` reserve, below the
DHCP pool — no router change is needed.)

## Post-testpool ceiling downsize (POC on dev-worker-6, 2026-09-01 — closed 2026-09-21)

> **Closed with dev-worker-6's retirement.** The POC host idled at ~3.7 GiB RSS for its whole run
> (7-day CPU 1.7 %), so the 12 GiB ceiling was never exercised and proves nothing about a busy
> worker. The fleet-wide reduction below is decided from the survivors' measured working sets
> (`node_memory_MemTotal - MemAvailable`, weekly max per worker), not from this POC. The
> `memory_mib` override stays in the module for that change.

Since the test-env pool went live (`kubernetes/apps/infrastructure/testpool/`, `tep`), the heavy
compose stacks (L/XL/Playwright class) lease kata envs on talos-env-node-1 instead of running on
the worker; only S-class (plain pytest, 2–4 GiB) and the small M-class docker tiers stay local, so
the 16 GiB ceiling is oversized. Measured over the 10 days ending 2026-09-01 (node_exporter,
pre-pool load included): peak used was 7.9 GiB (dw4) / 6.9 GiB (dw1), and ≤2.5 GiB on the other
four.

**POC (historical):** dev-worker-6 ran a **12 GiB ceiling** (`memory_mib = 12288` override in
`kubernetes/infra/dev-workers/variables.tf`), hand-applied 2026-09-01 (`qm set 4206 --memory 12288`
+ `qm reboot 4206`) and codified the same day — the first `tofu apply` after the merge no-op'd.
Post-resize checks passed: prometheus-node-exporter :9100 up, local `docker run` fine, `tep list`
reaches the pool. **Fleet-wide plan** (after the POC soaks): drop the ceiling scalar to 12288 for
all workers, freeing 4 GiB × 2 workers of worst-case commitment per node — headroom that feeds the
planned env-big (24 GiB) testpool node. On dw1/dw4 a 12 GiB ceiling meets their codified 12 GiB
floor (floor == ceiling: effectively fixed memory), which matches how node1 already behaves —
ballooning never inflates guests there. Do NOT lower the dw1/dw4 floors as part of this; that
mitigation stands until node1 capacity is fixed (see the note in variables.tf).

Per-node RAM budget (~125 GiB usable): Talos CP (**cp1 24 / cp2 24 / cp3 28 GiB hard** —
`kubernetes/infra/variables.tf`) + ai-llm LXC (96 GiB cap; **~0 GiB when idle-unloaded**, ~59/71 GiB
when a heavyweight is loaded on demand) + runner (24 GiB ceiling / **10 GiB floor**, ×2 node1/node2,
×1 node3) + dev-worker (16 GiB ceiling / **4 GiB floor** — **12 GiB on dw1/dw3**, 6 GiB on dw4,
×2 per node; node1's two raised floors add 16 GiB of guaranteed allocation there). In steady
state (heavyweight unloaded) node3 sits ~45% used and its workers balloon freely toward the
ceiling. **node2 no longer has that headroom**: `talos-env-node-1` (16 GiB fixed, the test-env
pool node — `kubernetes/infra/env-pool/`) joined it 2026-09-01 and steady-state sits ~93% used,
above PVE's ~80% auto-balloon threshold — node2's workers are effectively floor-pinned. That
floor-pinned the node2 worker (dw5 then, dw4 since the 2026-09-23 re-slot — vmid 4205) into swap-death during a working session the same day (the dw1 2026-08-11
signature: swap full, huge major-fault rate, SSH banner timeouts while ping answers); it now
carries a codified 6 GiB floor (`memory_floating_mib = 6144`). Recovery that worked, twice now:
raise the floor (`qm set <vmid> --balloon <MiB>`) so pvestatd cannot re-pin, then force-inflate
via `qm monitor <vmid>` → `balloon <MiB>` — `qm set` alone never inflates a running guest. **Time-share rule:** a
node serves *either* its on-demand heavyweight *or* its two workers at full tilt — not both. Loading
the 122B on node3 (71 GiB) fits alongside cp3 28 + runner 10 + 2×dev-worker-at-floor 4 = 117 < 125,
with the co-located workers pinned near their 4 GiB floor for that session. If a host shows sustained
`node_pressure_memory`, prefer unloading its heavyweight (or shortening its llama-swap TTL) over
starving a worker.

## Provision

```bash
# 1. tofu — create the 3 VMs (separate state from runners/Talos)
cp kubernetes/infra/dev-workers/terraform.tfvars.example kubernetes/infra/dev-workers/terraform.tfvars
#   fill pve_api_token + dev_worker_ssh_public_key (reuse the runners' values)
just dev-workers-plan      # expect 1 download_file + 3 VMs (scsi0 40G import + scsi1 128G blank)
just dev-workers-apply

# 2. reach the guests (c4 is created by cloud-init on first boot)
just ping-dev-workers      # or: ssh c4@192.168.0.37

# 3. ansible — configure (Claude Code + Codex + docker + tmux + ttyd/Caddy + dashboard …)
just dev-workers
just dev-workers           # run twice — the 2nd run should report near-zero changed (idempotency)
```

## One-time manual steps (per worker)

Auth is **subscription OAuth** — provisioning injects no keys, and these three logins stay manual
**by design**: they are interactive browser flows against personal accounts, not distributable
secrets, so there is nothing a vault could hold on their behalf. The credentials that *are*
distributable (the Gitea forge PAT, and anything added later) move out of Ansible and into OpenBao
once `dev_worker_enable_openbao` is on — see § "Credentials via OpenBao (ADR 0020)" below.

By default **everything runs as `c4`**
(the SSH console, the ttyd web UI, the dashboard, and any agent jobs are all the one `c4` identity),
so you log in **once as `c4`** and both the console and the web UI are authenticated:

```bash
ssh c4@192.168.0.37          # (.38/.39) — the ttyd web UI is the SAME c4 session
claude                       # Claude (Max/Pro) OAuth login  → ~/.claude
codex login                  # Codex (ChatGPT) login         → ~/.codex
gh auth login                # for the dashboard 'github' window (gh-dash)
```

No second account, no ACL re-run. `c4` owns its own token store, so **tokens refresh cleanly** during
normal use. The web UI (ttyd) and SSH attach the same tmux `main` session, so a `claude`/`codex` task
started in one continues seamlessly in the other.

Verify:
```bash
ssh c4@192.168.0.37 'claude --version && codex --version'   # both resolve from ~/.npm-global/bin
```

**Codex version + model are ansible-managed** (role `dev_worker`, `tasks/codex.yml`, tag `codex`):
`dev_worker_codex_version` is a floor — an older CLI is upgraded to exactly it, a newer one is left
alone (codex does not self-update, and `gpt-6-astra` is refused upstream from CLIs < 0.153.0 —
measured 2026-09-05: 0.152.1 rejected, 0.153.0 accepted) —
and `dev_worker_codex_model` / `dev_worker_codex_reasoning_effort` (`gpt-6-astra` / `xhigh`) are
written as top-level keys into each user's `~/.codex/config.toml` in place and section-aware
(`ini_file` with no section touches only the region above the first `[table]`, so the
`[projects.*]` trust tables codex appends and any `[profiles.*]` are never edited). Roll out alone with
`ansible-playbook dev-workers.yml -t codex`. The reviewer VMs get the same floor from
`reviewer_codex_version` in `reviewers.yml` (its probe/upgrade tasks carry the `reviewbot` tag, so
the documented `-t reviewbot` rollout includes them), and reviewer-2's review model is
`pr_reviewer_llm_model` in its host_vars.

**Codex's managed sandbox needs a bubblewrap AppArmor profile** (`tasks/codex_sandbox.yml`, tags
`codex` / `codex-sandbox`). Ubuntu 24.04 sets `kernel.apparmor_restrict_unprivileged_userns=1`, so
without it every command in a sandboxed session (anything but `--yolo`) dies before starting with
`bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`; dmesg shows
`apparmor="DENIED" profile="unprivileged_userns" capname="net_admin"`. The role installs the distro
`bubblewrap` (codex prefers a `bwrap` on PATH over its bundled copy) and loads
`/etc/apparmor.d/bwrap-userns` (Ubuntu's own `bwrap-userns-restrict`, renamed): `/usr/bin/bwrap` may
create the namespace, but everything it runs is stacked under `unpriv_bwrap`, which denies every
capability. That way bwrap is not a general way to get a full-capability user namespace. The role
refuses to run if the stock profile is also loaded (two profiles on one path). Check a worker with
`codex sandbox -c sandbox_mode='"workspace-write"' -- sh -c 'touch x && echo ok'` from a scratch dir.

### Optional: sandboxed separate agent account
To isolate the headless agent from `c4`'s sudo, set `dev_worker_agent_user: claude-agent` in
`group_vars/dev_workers.yml` and re-run. That restores the homelab two-user split: `claude-agent`
owns the credentials and runs ttyd + `claude-job@`, and `c4` gets read-only shared access (via ACL +
`CLAUDE_HOME`/`CODEX_HOME`). **Caveat:** read-only sharing means `c4` **can't refresh tokens** — log
in as `claude-agent` (`sudo -iu claude-agent`) and re-login when they expire (or grant `c4` write on
`auth.json`). The unified default avoids this entirely; only opt in if you specifically need the
sandbox.

## Credentials via OpenBao (ADR 0020)

Off by default (`dev_worker_enable_openbao: false`). When it is on, the worker stops carrying a
hand-distributed copy of the shared forge PAT and instead gets its **own AppRole identity** in
OpenBao, held by a root-owned `bao agent`. Three things change on the box:

- **`~/.git-credentials` becomes agent-rendered**, from `af/dev-workers/common` in the vault, instead
  of being written by `git_forge.yml` (that task yields ownership of the file — two writers of one
  path would flap). Rotation becomes a vault + seeds change, not a playbook run against all six hosts.
- **`/usr/local/bin/cred` appears** for everything else: `cred list`, `cred get <name> <field>`, and
  `cred exec <name> <field> <ENV_VAR> -- <cmd>` — the last of which hands a secret to a child process
  without it ever appearing in the terminal, which is the form agents are told to prefer. It reads
  the agent's group-readable sink token, so users must be in the `openbao-agent` group (the role adds
  them; group membership needs a fresh login to take effect).
- **A managed block lands in each user's `~/.claude/CLAUDE.md`** documenting the above and stating the
  rule: never print credential values into the conversation, logs, or files — names and lengths only.

The vault is reached over the LAN at `https://openbao.lan.chifor.me:30820` (a NodePort on the Talos
node IPs, pinned in `/etc/hosts`), not over the Cloudflare tunnel. The tokens are **periodic**, so
they renew indefinitely with no max-TTL cliff.

Activation is not just a toggle: the cluster side (Service, cert SAN, provision Job, KV seeds) has to
land first, and each worker's secret-id is minted by hand once. Full ceremony, rotation, and failure
modes: **`docs/runbooks/openbao-dev-workers.md`**.

Since ADR 0028 the same plumbing also carries **read-only access to the running Strive platform** —
`/usr/local/bin/platform` (`kubectl`, `psql`, `pf`) against namespace `strive-ailab` and the platform
databases, with no secrets, no exec and no writes:
**`docs/runbooks/dev-worker-platform-access.md`**.

## Optional features (off by default)

Enable in `ansible/group_vars/dev_workers.yml`, add the secret, re-run `just dev-workers`:

| Toggle | Secret (in `dev-worker.sops.yaml`) | Notes |
|---|---|---|
| `dev_worker_enable_restic` | `dev_worker_restic_password` | Targets a restic REST server on the QNAP by default (`dev_worker_restic_backend: rest`); QNAP-side rest-server setup is out of scope. `nfs` and `none` backends also supported. |
| `dev_worker_enable_cloudflared` | `dev_worker_cf_tunnel_token` | Public access via CF tunnel + CF Access. |
| `dev_worker_enable_password_auth` | `dev_worker_admin_password` | Enables sshd PasswordAuthentication for c4. |
| `dev_worker_enable_herdr` | — (no secret) | PILOT — per-host in `host_vars/dev-worker-4.yml`, not group_vars. See § "herdr pilot" below. |
| `dev_worker_enable_openbao` | `dev_worker_openbao_credentials` (per-host `role_id`/`secret_id` map) | Per-VM OpenBao AppRole + `bao agent` + `cred` helper (ADR 0020). Not a pure toggle: the cluster side must be live and each worker's secret-id minted first — `docs/runbooks/openbao-dev-workers.md`. |

Create the encrypted secrets file:

```bash
cp ansible/secrets/dev-worker.sops.yaml.example ansible/secrets/dev-worker.sops.yaml
#   edit values, then encrypt in place (uses the .sops.yaml dev-worker creation_rule)
sops --encrypt --in-place ansible/secrets/dev-worker.sops.yaml
git add ansible/secrets/dev-worker.sops.yaml
```

## reviewers (dedicated PR-review VMs)

reviewer-1 (.24, claude persona) and reviewer-2 (.25, codex persona) — **vmid 4501/4502** on
ai-node3, 2 vCPU / 4 GiB FIXED, tofu module kubernetes/infra/reviewers, guest config
ansible/reviewers.yml (deliberately minimal: node + LLM CLIs, node_exporter, ufw, pr_reviewer
role — no docker/tmux/toolchains). Migrated off dev-worker-2/-3 2026-09-02 so reviews never
contend with feature work. Claude auth (~/.claude) was seeded once from the old hosts and is
NOT ansible-managed — a subscription re-login is manual; since 2026-09-18 the persona's reviews
run on three seat tokens instead (§ "Seats on reviewer-1" below), and c4's own login stays for
`claude auth status` only. Codex auth on reviewer-2 is, since
2026-09-12, rendered by the bao agent from the estate's ONE shared login (`pr_reviewer_enable_openbao`;
docs/runbooks/openbao-dev-workers.md § "The shared codex login") — never `codex login` there.
**Still true as of 2026-09-16, and it is what exhausts:** sharing one seat with the dev agents is
why the codex persona was quota-blocked 20h21m of one 32h span. ADR 0024 decides to give the
reviewer its own seat — that seat is NOT provisioned yet, so this paragraph describes the live
state, not a superseded one. Org webhooks 38/39 point at
.24/.25:8477; scrape via monitoring/reviewers-node.yaml (job=reviewer-node); the AI Lab Fleet
dashboard "PR Reviewers" row reads the reviewbot_* textfile metrics. Tofu state: applied from
the session scratchpad clone — hand the tfstate to the main checkout and verify a no-op plan
(same handover as env-pool; see backend.tf).

> vmid 4501/4502, NOT 4301/4302 — those are talos-agent-node-1/2. Pointing a reviewer apply at
> 4301/4302 is what destroyed the agent nodes and cost ~6h of agentforge downtime. Verify with
> `python scripts/node-ssh.py 192.168.0.4 "qm list"` before any destructive tofu run.

> **The reviewers converge daily — since 2026-09-16, and not before.** From 2026-09-03 to
> 2026-09-16 they converged NOWHERE, while both this runbook and the repo said otherwise. The
> repo's `scripts/fleet-converge-daily.sh` did run `reviewers.yml` (added 09-06), but Task
> Scheduler was not running the repo's copy: `ailab-fleet-converge` executes
> `wsl.exe -e bash -lc "~/.ailab-converge/fleet-converge-daily.sh"`, and that path held a
> STANDALONE copy frozen at 2026-09-03 13:26 which predated the reviewers step and never
> self-updated. Proof at the time: zero occurrences of `reviewer-1`/`reviewer-2` anywhere in
> `~/.ailab-converge/converge.log`, against two dev-worker PLAY RECAPs per run, and reviewbot.py
> on both hosts stamped from a hand-run rather than the 03:35 UTC converge (the "06:35" in the
> script is WSL-local, UTC+3).
>
> It stayed invisible because the same frozen copy predated two guards added in the same commit:
> no `set -o pipefail`, so a failed `ansible-playbook | tail` reported the status of `tail`; and
> no `exit "$rc"`, so Task Scheduler's LastTaskResult was 0 whatever happened. Three fixes were in
> main for ten days and none of them ever ran.
>
> **The scheduled path is now a bootstrap** (`scripts/fleet-converge-bootstrap.sh`): it updates the
> clone, copies the current converge script out to `~/.ailab-converge/.run-converge.sh`, and execs
> it — so the logic is always current main, and git never rewrites the file bash is mid-way through
> reading (which is why the converge script cannot simply be scheduled directly). The bootstrap has
> no logic of its own, so it cannot drift the way its predecessor did. Verified on install: the
> first run put `reviewer-1 : ok=40 changed=4` / `reviewer-2 : ok=42 changed=4` in the log for the
> first time, and surfaced a real `rc=4` that the old copy would have reported as success.
>
> To deploy a reviewbot change without waiting for 03:35 UTC, run the same entry point the
> scheduler does: `wsl.exe -e bash -lc "~/.ailab-converge/fleet-converge-daily.sh"`. The
> tolerant-diff-decode fix once went 22 hours undeployed while platform#1074 failed 66 times on
> both personas — that is the cost of assuming deployment happened. If you suspect drift:
> `ssh c4@192.168.0.24 md5sum /usr/local/lib/reviewbot/reviewbot.py` against
> `md5sum ansible/roles/pr_reviewer/files/reviewbot.py` on main.

### Seats: the codex persona holds several subscriptions

reviewer-2 runs `pr_reviewer_llm_seats` — a list of `{name, sudo_user, home?}`, one ChatGPT
subscription each, one OS user each — plus `pr_reviewer_llm_seats_staged`, the same shape for a
seat that is provisioned but not yet served. Live since 2026-09-16 (a, b, c) and 2026-09-19 (d,
staged in the morning, logged in and activated the same day); emails from
`reviewbot_llm_seat_info` on 2026-09-19:

| seat | user | account | email | feeds |
|---|---|---|---|---|
| `a` | `codexrun` | `cfdea639…` (shared with the AgentForge dev agents) | `chifor@gmail.com` | reviews only |
| `b` | `codexrun2` | `9c8a8cfb…` | `realjaysage@gmail.com` | reviews + dsh's native `openai-codex` provider (`af/dsh/credentials`, `docs/runbooks/dsh.md` § Codex subscriptions) |
| `c` | `codexrun3` | `11c52fea…` | `constantin.chifor@strive.us` | reviews only |
| `d` | `codexrun4` | `841dae14…` | `realjaynesage@gmail.com` | reviews + LiteLLM's `chatgpt/` route (`af/litellm/chatgpt`, ADR 0026); logged in 2026-09-19, active since PR 3 the same day |

**One user per seat is not tidiness.** `_run_llm` binds the isolated 0700 tmpdir, the answer
read-back, the credential scan and the cleanup to a single sudo user for a whole invocation, so a
shared home would put every credential within reach of one prompt-injected diff.

**Selection is STICKY**: seat `a` carries everything until it refuses, then `b`, and it stays on
`b` — it does not drift back when `a`'s park lapses. Round-robin would drive both accounts to
their walls simultaneously and turn two staggered recoveries into one synchronised outage.

`pr_reviewer_llm_seats: []` (reviewer-1, and any host that has not been migrated) is not a
special case: reviewbot synthesises one seat named `default` from `pr_reviewer_llm_sudo_user` and
runs the identical code path.

**Adding a seat. THE ORDER IS THE PROCEDURE** — get it wrong and reviewbot runs with a seat that
has no credential, which fails as an ORDINARY error rather than a `RateLimited`, so it never parks
and burns the PR's attempts toward quarantine. Since 2026-09-19 the order is expressed in
configuration rather than in the operator's memory: `pr_reviewer_llm_seats_staged` (ADR 0026)
provisions a seat exactly like a served one — user, `0700 ~/.codex`, model pin, sudoers — but does
NOT render it into reviewbot's `config.json`, so reviewbot never learns of it until it is moved.
`pr_reviewer_seats_effective` (the list the provisioning tasks iterate) is the active list plus the
staged ones. The `seats` tag provisions WITHOUT touching reviewbot.py or restarting the service
(none of those tasks notify the restart handler).

1. **Stage it in `ansible/host_vars/reviewer-2.yml`**: add `{name, sudo_user, home?}` to
   `pr_reviewer_llm_seats_staged`. If the seat feeds a consumer, add its projection to
   `dsh_codex_publisher.projections` with `optional: true` (`docs/runbooks/dsh.md` § Codex
   subscriptions).
2. **Provision the user only** — no config change, no restart, so the not-yet-credentialled seat is
   never live:
   ```
   ansible-playbook reviewers.yml -l reviewer-2 -t seats          # add ,dsh-codex if a projection was added
   ```
   This creates the user, its `0700 ~/.codex`, the model pin, and rewrites
   `/etc/sudoers.d/reviewbot-llm` with every seat user (active and staged) in one validated file.
3. **Log in DIRECTLY as the seat user** — no scratch HOME, no copy, nothing to delete:
   ```
   ssh c4@192.168.0.25
   sudo -n -u codexrunN HOME=/home/codexrunN setsid nohup /usr/bin/codex login --device-auth \
       > /tmp/seat-N-login.log 2>&1 < /dev/null &
   sleep 10 && cat /tmp/seat-N-login.log    # prints the URL and a one-time code, then polls
   tail -f /tmp/seat-N-login.log            # WAIT for the CLI to report success before anything else
   ```
   `--device-auth` is the headless flow and is **absent from `codex login --help`** at 0.153.4 —
   the CLI only names it after a browser login fails. Authenticate against the NEW licence.
4. **Verify the credential as the seat**, reading JSON only:
   ```
   sudo -n -u codexrunN HOME=/home/codexrunN /usr/local/lib/reviewbot/codex-usage.py
   ```
   It exits 0 either way; `"ok": true` and the expected `"email"` are the check. A projection with
   `optional: true` publishes on the publisher's next minute from here.
5. **Activate**: move the entry from `pr_reviewer_llm_seats_staged` to `pr_reviewer_llm_seats`
   (and drop `optional` from its projection), then `-t reviewbot` (`-t reviewbot,dsh-codex` with a
   projection) or a full converge re-renders the config and restarts the service. Confirm with
   `journalctl -u reviewbot | grep "^.*seats:"` and the textfile's `reviewbot_llm_seats_total` /
   `_distinct` / `_available`.

**Why the login is direct and there is no copy step (changed 2026-09-19).** OpenAI refresh tokens
are single-use, so two copies of one family revoke each other on first refresh — that is the
2026-09-10 outage, which took BOTH personas down and did not surface until the access token
expired days later. The earlier procedure logged in under a scratch HOME and `install`ed the
`auth.json` into the seat, one forgotten `rm -rf` away from that outage; logging in as the seat
user means the family has exactly one home from its first second.

**Never add a seat whose account already appears.** `resolve_seats()` reads `tokens.account_id`
from each seat at startup and COLLAPSES duplicates, because rotating inside one account is the
doomed-retry loop the park exists to prevent — it cost 463 refused calls over four days in
September. A collapse is not silent: `reviewbot_llm_seats_distinct` drops below
`reviewbot_llm_seats_total` and **ReviewbotSeatsDegraded** fires. A seat that cannot be `sudo`'d
to is dropped the same way; if NO seat passes its probe, all of them are parked rather than one
being handed work it cannot do.

> **All four licences exist and are served (2026-09-19).** `reviewbot_llm_seat_info` reported
> three distinct codex accounts on reviewer-2 that morning — `chifor@gmail.com` (a),
> `realjaysage@gmail.com` (b), `constantin.chifor@strive.us` (c) — and the same day seat b read
> 100 % of its weekly window (`reviewbot_llm_usage_percent{persona="codex",seat="b"}`), which is
> what dsh's Astra route was living on. Seat d (`codexrun4`, `realjaynesage@gmail.com`, account
> `841dae14…`, plan `prolite`) went through the staged procedure above in one day: staged, logged
> in directly as its user (the ceremony in `docs/runbooks/dsh.md` § Codex subscriptions — the
> publisher's first projection landed and LiteLLM served the route), then moved into
> `pr_reviewer_llm_seats` (sticky order a→b→c→d) with its projection required. The check is
> `reviewbot_llm_seats_distinct{persona="codex"} == 4`. This note replaced a 2026-09-16 one that
> said only two of three licences existed.

**Usage watchdog for codex** (2026-09-19, `pr_reviewer_usage_poll_s: 3600` on reviewer-2). The
same hourly poll as on reviewer-1, with `codex-usage.py` as the probe: run AS each seat, it asks
the CLI's own app server — `codex app-server`, JSON-RPC over stdio, `initialize` →
`account/rateLimits/read` — for the account's windows; no model call, no quota, and the app
server refreshes the ChatGPT token itself, so the claude keepalive has nothing to do here (a
`chatgpt` credential never triggers it). The backend URL behind it answers a script with a 403
challenge page; the CLI is the only door. A window shorter than a day is `session`, the 10080
minute one is `weekly_all`, so the same parks apply: a spent weekly window parks the seat until
the API's own reset. Identity (email, plan, account id) is read from the seat's access-token JWT
claims in `~/.codex/auth.json` — never verified, never printed. Same metrics and panels as the
claude persona under `persona="codex"`; `ReviewbotUsageProbeFailing` fires after 3 h of failed
probes, and the manual check is `sudo -n -u <seat user> HOME=/home/<seat user>
/usr/local/lib/reviewbot/codex-usage.py`. Re-login a seat with `codex login` as its user.

### Seats on reviewer-1: the claude persona holds three subscriptions

Since 2026-09-18 (`plans/2026-09-18-claude-seat-rotation-plan.md`, ADR 0025) reviewer-1 runs the
same `pr_reviewer_llm_seats` rotation as reviewer-2 — sticky selection, lossless park, duplicate
collapse — after its single account spent a whole weekly window and the persona idled for 21 h
with 9 jobs queued and 7 PRs merge-blocked on reviewer-2.

| seat | user | account (`account.uuid` from the probe; browser login since 2026-09-18) |
|---|---|---|
| `a` | `clauderun` | `16976f97…` — the account behind the brokers' `claude-max-1` token |
| `b` | `clauderun2` | `fe33cddd…` — behind `claude-max-2`; also dev-worker-1's interactive login |
| `c` | `clauderun3` | `0ce2e93c…` — also dev-worker-4's interactive login; the seat that carried the persona out of the 2026-09-18 outage |

Three things differ from the codex seats:

1. **Each seat is a browser login in its own HOME** (`/home/<user>/.claude/.credentials.json`),
   made once *as that user*. One login per seat means one refresh-token family per seat, so
   nothing can invalidate anything else — the hazard that makes codex seeding a ceremony is a
   hazard of *copies*, and there are none. **Not the OpenBao setup-tokens** the agentforge
   brokers use: those authenticate for inference but carry only `user:inference`, and
   `/api/oauth/usage` and `/profile` answer `403 oauth_scope_insufficient` — measured on all
   three on 2026-09-18, the day the seats went live on them and were moved off them the same
   afternoon. A seat can still run on a token file (`~/.claude/oauth-token`, which the wrapper
   prefers when present); such a seat is blind to usage and unknown to the identity collapse.

   **Logging a seat in** — the CLI prompts for a pasted code on stdin, so the flow runs detached
   with its stdin held open on a fifo (`/home/c4/seat-login.sh`, kept on the host):
   ```
   ssh c4@192.168.0.24 '/home/c4/seat-login.sh start clauderun2'      # prints the URL to open
   # sign in as THE SEAT'S ACCOUNT in a private window (a warm browser session reuses the
   # wrong account silently - it did, on the first attempt), then:
   ssh c4@192.168.0.24 '/home/c4/seat-login.sh code clauderun2 <pasted code>'
   ssh c4@192.168.0.24 'sudo -n rm -f /home/clauderun2/.claude/oauth-token'     # BEFORE the check
   ssh c4@192.168.0.24 'sudo -n -u clauderun2 HOME=/home/clauderun2 /usr/local/lib/reviewbot/claude-usage.py'
   ```
   The last line is the check that matters: `"ok": true` with the expected `email`. The
   token file is removed first because both the wrapper and the probe PREFER it when present —
   a leftover setup-token keeps answering 403 and the check would be testing the wrong
   credential.
2. **The entry point is `/usr/local/lib/reviewbot/claude-seat.sh`**, not the CLI: with a token
   file it exports it into `CLAUDE_CODE_OAUTH_TOKEN` (sudo resets the environment and shows argv
   to every process, so the HOME is the only place a token may come from); without one it
   simply execs `/usr/bin/claude`, which uses the seat's login.
3. **Identity comes from the API, not a file.** `resolve_seats()` runs
   `/usr/local/lib/reviewbot/claude-usage.py` as each seat (`GET /api/oauth/profile` and
   `/usage` — the calls behind the CLI's `/usage`; they consume no quota) and collapses seats
   that share an `account.uuid`. The same command is the operator's probe (above): it prints the
   account's email and every usage window with its percent and reset time.

**Adding or replacing a seat — the same order as codex, for the same reason** (a seat with no
credential fails as an ordinary error, never parks, and burns the PR's attempts):
edit `host_vars/reviewer-1.yml` → `ansible-playbook reviewers.yml -l reviewer-1 -t seats`
(user, 0700 `~/.claude`, sudoers, the two helpers — NO restart) → log the seat in as above →
`-t reviewbot` (or the converge) to restart → `journalctl -u reviewbot | grep "seats:"` shows
`['a', 'b', 'c']` and the textfile's `reviewbot_llm_seats_distinct{persona="claude"}` reads 3.

**The ladder and the watchdog** (`pr_reviewer_llm_models: [fable, opus, sonnet]`,
`pr_reviewer_usage_poll_s: 3600`). Selection is tier-major, then the sticky seat, then seat
order: any seat that can serve `fable` beats the current seat on `opus`. A refusal moves the
persona DOWN or ACROSS — a model-scoped one (`/usage-credits … switch models`, or a model the
CLI cannot serve) parks that `(seat, model)` pair and the same tier is tried on the next seat;
an account-scoped one (`weekly limit`, `session limit`) parks the whole seat; a tier is left only
when every seat is parked for it, and the descent stays on the sticky seat. The hourly
watchdog moves the persona UP before a park lapses: it runs the probe as every seat, parks and
unparks seats and tiers from the API's own `percent`/`resets_at` (a window at 100 % parks until
its reset, below 100 % clears the park whatever text-derived guess set it), then climbs to the
best tier that is free anywhere. It never moves within a tier. A park that lapses on its own
(or a transient non-limit failure, which parks nothing) lets the next review try the higher
tier again by itself — a refused call costs nothing. Parks stay clamped to 6 h and the next
poll re-extends them, so a dead poller cannot leave a week-long park behind. Read it on the AI
Lab Fleet dashboard: *Active Claude Account* (email), *Active Claude Model*, *Usage Probe*,
*Claude Seats — usage per account and window* (one row per account, with *Login valid until*),
*Parked per Seat — account and per tier*; in the journal: `grep -E "usage|climbing|limited"`.

**The credential keepalive** (`pr_reviewer_usage_keepalive: true`, part of the poll). A browser
login's access token lives ~7–8 h and ONLY the CLI renews it, when it runs as that seat. The
2026-09-18 assumption that "a refused call keeps a parked seat's credential fresh" was wrong for
a seat that is never chosen: selection is sticky, so seats a and b — parked on their weekly
walls while c served — were never run, their tokens expired (18:30Z, 18:48Z) and their probes
answered `profile: HTTP 401; usage: HTTP 401` from 19:10Z on, with nothing to renew them.
`claude auth status` does not refresh. What does: any non-interactive CLI call — the CLI renews
the token BEFORE its API request and persists it even when that request is then refused. So
when the probe reports an expired login, the poll runs `claude-seat.sh -p ok --model haiku
--max-turns 1 --output-format json` as that seat (stdin closed, output discarded, 60 s
timeout), then probes again; measured on seat a: 2 s, refused with 429 at zero cost, token
renewed. Never for a setup-token (no expiry, no refresh); only after the probe actually failed
(a 401, not the clock alone); never while a review's CLI is running as that seat — both paths
hold a per-seat lock (`SEAT_LOCKS`), so a review that rotates onto a seat mid-keepalive waits
for it (≤ 60 s) instead of racing it on the credential file; once per poll. Journal:
`grep keepalive`; metrics: `reviewbot_llm_seat_credential_expires_at_seconds`,
`reviewbot_llm_seat_keepalives_total`, `reviewbot_llm_seat_keepalive_failures_total`.
`ReviewbotUsageProbeFailing` (3 h) therefore now means a login the keepalive could NOT renew —
revoked, or a CLI that changed its non-interactive behaviour — and the answer is a re-login.
Manual equivalent: `sudo -n -u <seat user> env HOME=/home/<seat user> claude -p ok --model
haiku --max-turns 1 --output-format json`.
If `ReviewbotUsageProbeFailing` fires, the seat's login is what needs attention (401 = expired
or revoked, 403 = a token file without `user:profile`) — re-login as above.

`~/.claude/projects` under each seat grows by one directory per review — the CLI keys them by
working directory and reviewbot hands it a fresh tmpdir every run; c4's held 1 926 on
2026-09-18. Pre-existing behaviour, now per seat; harmless until the disk says otherwise.

### When a persona is parked on a subscription rate limit

**Do nothing. It self-heals, and the two obvious interventions both make it worse.** This is the
one failure mode here that is not a fault: the account's quota is spent, reviewbot has noticed, and
it is waiting with the queue intact.

What it looks like:

```
subscription rate-limited; parking the worker for 900s (queue left intact)
job 1526 cchifor/ailab#737 deferred: subscription rate-limited, waiting until ~15m
  (no attempt consumed) [upstream: ...]
```

* **ReviewbotAllSeatsExhausted** is the outage: no seat left to run on. **ReviewbotSeatExhausted**
  is one seat spent while others still serve — capacity, not an outage, and it arrives first.
  `ReviewbotRateLimited` remains the backstop; its counter still means "a job no seat could
  serve", because a refusal run_llm routes around never reaches the worker. Expect
  `ReviewbotQueueBacklog` and `ReviewbotStalled` alongside it; those detect the stall, this one
  attributes it.
* `reviewbot_rate_limited_seconds_remaining` counts down 900 → 0 per cycle. It is a **sawtooth**,
  briefly 0 between parks, so do not read a single 0 as recovery — `reviewbot_llm_rate_limited_total`
  is the honest signal.
* `waiting until ~15m` is `DEFAULT_PARK_S`, **not** a parsed reset: it means upstream named no
  reset we could read, so we re-probe every 15 minutes. `waiting until HH:MM UTC` means we did
  parse one. The `[upstream: ...]` tail is the CLI's own refusal — that is the text that tells you
  whether this is a blip or a spent weekly window.

Why not to intervene:

* **Do NOT `--requeue`.** Nothing is quarantined: the park charges no attempt precisely so a
  rate-limit window cannot exhaust a PR's retry budget. Requeue is a no-op at best.
* **Do NOT restart the service to "clear" it.** The seat park table is in memory, so a
  restart does drop the park — and the worker then walks straight back into the same wall, one
  refused call later — once per SPENT SEAT, since a restart forgets every seat's deadline and
  re-probes them in order. (This is how reviewer-1 came back 4h35m early on 2026-09-13: a service
  restart at 06:38:27 UTC dropped the park, rather than the park lapsing at 11:13. It was NOT the
  daily converge — that runs 03:35 UTC and, per the box above, has never run `reviewers.yml` at
  all; on the evidence it was a hand-run of the playbook. Whoever restarts it gets this for free,
  which is fine when the window has reopened and pointless when it has not.)

Measured behaviour, so you know what normal looks like: the 2026-09-15 codex episode ran
**46 parks over 11h36m**, held 7 PRs, consumed zero attempts, quarantined nothing, and on recovery
drained the whole queue in **~60 seconds**, auto-merging 4 PRs.

What DOES need a human is the recurrence. The limit is account-scoped, so no fallback model can
rescue it (a fallback on the same seat shares the spent budget — `reviewbot.py` L697-698, and the
codex branch has no fallback path at all). If parks recur on a roughly daily cadence, the answer is
quota, not code: see **ADR 0024**, which records the decision to give reviewer-2 its own Codex seat
rather than cut review coverage. Note the refusal text's `try again at <date>` is misleading — what
actually reopens is a rolling window hours out, measured recovering at ~06:17–06:20 UTC on two
consecutive days against a message naming Sep 21.

While a persona is parked, **automerge stops estate-wide** — merging needs every persona in
`merge_personas` clean at the current head, so the healthy reviewer logs `codex=no review` holds
(313 of them during that episode) and `reviewbot_merge_blocked_seconds` climbs. Merging past the
gate by hand is the intended escape hatch; it is silent by design.

### When a review never lands (deadlines, quarantine, requeue)

A persona that produces no verdict blocks the automerge lane: merging requires EVERY configured
persona clean at the current head. The 2026-09-04 incident was exactly this — platform#1067 was
attempted 9 times across 3 heads over 71 minutes and never reviewed, and the operator merged by
hand. Order of diagnosis:

1. **Grafana first, SSH second.** The "PR Reviewers" row on the AI Lab Fleet dashboard has a
   *Reviewer Errors* panel reading both hosts' journals out of Loki (roles/journal_ship ->
   monitoring/loki-lan.yaml). In Explore:
   `{job="host-journal", unit="reviewbot.service"} |~ "(?i)(failed|error|skipped)"`, and add
   `|= "1074"` to follow one PR. Only if that is empty is `sudo journalctl -u reviewbot -n 50`
   on the VM worth the trip. `llm deadline exceeded after <n>s` means the review needs more
   budget than `llm_timeout_s` allows.
   - **Is the host running main?** A `codec can't decode`, or the same failure on every
     attempt, usually means the VM is behind: compare the md5 above and converge before
     debugging any further.
2. `curl -s localhost:9100/metrics | grep reviewbot_` — `reviewbot_llm_seconds_max` and
   `reviewbot_llm_output_tokens_max` say how long the worst real run took and why. Run length
   tracks REASONING, not diff size: a 2 KB diff has timed out and a 342 KB one has passed.
3. If the deadline is genuinely too tight, raise `pr_reviewer_llm_timeout_s` in the pr_reviewer
   **role defaults**, not in host_vars — a default that is only survivable because two hosts
   override it is what produced this incident. Redeploy with
   `ansible-playbook reviewers.yml -l <host> -t reviewbot`.

**Large PRs are partially reviewed, not skipped whole.** The diff is split into whole files
before the size cap is applied. Generated/vendored files and binaries by EXTENSION are
excluded without downgrading the verdict — those rules live in the role, not in the repo under
review, and a reviewer cannot vouch for a lockfile either way. Three exclusions DO cap the
verdict at `partial`, which never automerges: bytes that do not decode as UTF-8 (`non-utf8`),
a path that cannot be read (`unparsable path`), and git's own binary marker on a path the
extension rules would have read (`binary content` — one NUL inside `payload.sh` is enough to
produce it). All three triggers are bytes the PR author controls inside an otherwise reviewable
file, so leaving them at `clean` would let a PR quarantine its own payload and merge it unread.
Prose shed largest-first for capacity caps the verdict the same way. Code is never truncated:
if the code alone is over the cap the review is skipped and the notice names the largest files
so the PR can be split. Paths git C-quotes (any non-ASCII byte, e.g. `"a/caf\303\251.py"`) are
decoded and reviewed normally, so an accented filename is not a self-exclusion vector.

Every filtered review carries a "Not reviewed" table above the model's summary, so a partial
review can never be mistaken for a full one. Verdicts are `clean | findings | partial |
skipped`; the merge gate is an allowlist requiring `clean` from every persona, so the three
others are non-merging by construction. Tune with `pr_reviewer_exclude_globs` /
`pr_reviewer_doc_globs` in the role — never from the repo under review, or a PR could exclude
its own payload.

**Quarantine is now sticky by design.** `enqueue()` dedupes against `quarantined`, which is what
stops the reconciler re-queueing a hopeless job every 300 s forever (each round costs
`max_timeout_attempts` x `llm_timeout_s` of a single-threaded worker). A quarantined head is
therefore never retried on its own. Clear it either by pushing a new head — which retires the
row automatically — or explicitly:

```bash
sudo -u c4 python3 /usr/local/lib/reviewbot/reviewbot.py /etc/reviewbot/config.json \
  --requeue cchifor/platform 1067
```

That is safe for the `deadline exhausted` / `attempts exhausted` classes: `review_job()`
re-checks the Gitea marker before doing any work, so a review that actually landed costs one API
call rather than a second post.

It **refuses** the `ambiguous POST` class unless you add `--force`. The marker pre-check makes a
retry cheap, not idempotent: after a client-side POST timeout Gitea may still commit the original
request, and it can do so *after* the requeued worker checks for the marker and *before* it posts
its own — which double-posts the review. Open the PR in Gitea and confirm no review from that
persona is present at that head; only then use `--force`.

### When a PR is "skipped" (diff over the size cap)

A diff over `pr_reviewer_max_diff_bytes` (400 KB) is not reviewed. Since 2026-09-06 the skip is
VISIBLE: the persona posts a COMMENT review ("Not reviewed: the diff at <head> is N bytes, over
this reviewer's cap...") carrying `verdict=skipped`. That marker dedupes the head like a real
review and keeps the automerge lane shut (merging needs `verdict=clean` from every persona), so
the PR shows *why* nothing landed. Before that, `review_job()` returned `done` with no Gitea
write and agentforge-platform#194 (480 KB) sat with no review at all, indistinguishable from an
outage. Fix on the PR side: split it, or mark generated/binary-ish files `-diff` in
`.gitattributes`. The cap is measured on the raw bytes of the `.diff` endpoint and the body is
decoded tolerantly — platform#1074 (a PDF corpus diffed as text) used to raise
`UnicodeDecodeError` before the cap was consulted and quarantined the job.

`ReviewbotQuarantined` fires on `reviewbot_quarantined_recent_jobs` (a 24 h window), not the
cumulative gauge — the cumulative one never falls for a PR that was closed rather than pushed
to, so alerting on it would latch on forever.

## Daily fleet converge (scheduled, GitOps-true)

Windows Task Scheduler task **ailab-fleet-converge** on the operator workstation runs
`scripts/fleet-converge-daily.sh` (staged at `~/.ailab-converge/` in WSL) daily at 06:35:
it fetches + hard-resets a PRISTINE dedicated clone (`~/.ailab-converge/repo`) to
origin/main and converges every worker in the inventory (full role everywhere; the dw6
`--skip-tags herdr` special case went with dev-worker-6's retirement on 2026-09-21). Never converge the fleet from a working checkout — the 2026-09-03
incident: an earlier ~06:05 job ran from the operator checkout (stale at Aug 31, on a
dirty WIP branch), reverting merged work every morning. That stale job's scheduler is
STILL UNLOCATED — the 06:35 run wins each morning regardless, but remove the old job when
found. Logs: `~/.ailab-converge/converge.log`.

## herdr pilot (dev-worker-4)

> dev-worker-6 was the second pilot host (managed takeover completed 2026-09-03) until its
> retirement on 2026-09-21; the pilot host is the VM that was dev-worker-5 (vmid 4205) — `dev-worker-4`
> on `.11` since the 2026-09-23 re-slot — fully role-managed.

[Herdr](https://herdr.dev/) — an agent-native terminal multiplexer — runs on dev-worker-4
**beside** tmux. This is an evaluation, not a migration: tmux keeps everything load-bearing (the
shared `main` session, ttyd/SSH parity, the `sessions` dashboard, resurrect/continuum reboot
persistence). Herdr adds the two things tmux cannot express: an attention queue over agent panes
(working / blocked / done / idle) and native conversation resume (`claude --resume <id>`) after a
server restart. The 2026-08-31 evaluation that led here concluded **keep tmux**: herdr's reboot
restore is shape-only (non-agent panes return as fresh shells), it has no selective-restore control
(the same collision class as the dev-worker-4 dashboard incident), and it is pre-1.0 from a
one-person company — so it gets one host, a memory cap, and a kill switch.

- **Enable/disable:** `dev_worker_enable_herdr` (default off; flipped only in
  `host_vars/dev-worker-4.yml`). Deploy with `just dev-workers`, or targeted (the explicit
  `ANSIBLE_CONFIG` matters — on WSL the world-writable `/mnt/c` CWD makes an implicit
  `ansible.cfg` silently ignored, which drops the inventory and "deploys" to zero hosts; same
  trap as the ci-runners runbook):
  `cd ansible && ANSIBLE_CONFIG="$(pwd)/ansible.cfg" ansible-playbook dev-workers.yml -l dev-worker-4 -t herdr`.
  A `-t herdr` run needs an already-provisioned worker (it asserts `/workspace/c4` rather than
  creating it).
- **What it installs:** pinned static binary `/usr/local/bin/herdr-<version>` + `herdr` symlink
  (sha256-pinned in the role defaults — upstream ships no checksum file, so a version bump must
  recompute the hash), an ansible-managed `~c4/.config/herdr/config.toml` (pane-history off:
  secrets; agent-resume on: the point of the pilot), the `herdr.service` system unit (runs
  `herdr server` headless as c4, memory-capped like agentforge), and the `herdr-pilot-reset` hatch.
- **Attach:** SSH in (you land in tmux `main` via the auto-attach) and run `herdr` in a pane — or
  bypass tmux entirely with `ssh -t c4@192.168.0.11 herdr` (a remote command runs a non-login
  shell, so the `/etc/profile.d` hook is never sourced; the hook itself fires for login shells
  with an SSH tty). **Prefix collision:** tmux and herdr both use `ctrl+b`; inside a tmux pane,
  `ctrl+b ctrl+b <key>` reaches herdr.
- **What to evaluate:** does the attention queue change how many parallel agents are comfortable;
  does `claude --resume` actually survive `systemctl restart herdr` and a VM reboot; how the TUI
  behaves inside tmux/ttyd; server memory over weeks (`systemctl status herdr` shows the cgroup).
- **Reset:** `sudo herdr-pilot-reset` — stops the server, wipes session state (`session.json`,
  `session-history.json`, `sessions/`), restarts clean, keeps `config.toml`. For when a restore
  goes bad (agents resumed in the wrong cwd and restores wedged on git discovery are both known
  upstream at 0.8.x).
- **Threat model:** herdr's control socket (`~c4/.config/herdr/herdr.sock`) accepts any process
  running as c4 — under the unified single-user model that is root-equivalent (c4 is a
  passwordless sudoer), the same trust boundary as the tmux server socket in `/tmp/tmux-*`. The
  0700 config dir keeps other Unix users out; it does not sandbox c4's own agents, which can
  drive panes and other agents through it.
- **Config is ansible-managed:** settings changed in herdr's TUI are written to `config.toml` and
  will be reverted — with a pane-killing server restart — on the next ansible run. Persist
  changes by editing `templates/herdr-config.toml.j2` instead.

### Conductor pattern (multi-agent orchestration on the pilot)

Herdr's payoff over "an agent in tmux" is a **conductor**: an interactive Claude Code session
in a herdr pane (`HERDR_ENV=1`) that plans, spawns, watches, and verifies worker agents
through the `herdr` CLI — and never implements. Codex-validated design (2026-09-01); the
cross-repo conventions (global ~2-Claude cap, worktree hygiene, tep head-of-line, gitea PRs)
live in the agentforge `AGENTS.md`.

- **Setup (ansible, `-t herdr`):** the claude+codex integrations
  (`~c4/.claude/hooks/herdr-agent-state.sh`, `~c4/.codex/herdr-agent-state.sh` — session
  identity for native resume; lifecycle detection stays HEURISTIC at 0.8.2) and the
  version-matched conductor skill (`~c4/.claude/skills/herdr/SKILL.md`, generated from
  `herdr --skill`).
- **Shape:** one workspace per feature-run, conductor in its root pane; one worktree
  workspace per worker — `herdr worktree create --workspace <run> --branch
  feat/<run>-<role> --base <SHA> --no-focus`, resolving `origin/main` to ONE sha shared by
  every worker. Start workers with `herdr agent start <role> --kind claude|codex --pane
  <id>`; mix providers — codex workers do not burn the Claude subscription.
- **Drive:** briefs as files in the worktree; `agent prompt <role> --wait --until idle
  --until done --until blocked` (all three — focusing a tab flips done→idle); workers write
  `reports/<role>.md` and commit explicit paths. `agent_prompt_stalled` means delivery
  UNCERTAIN — poll for artifacts, never resend blind.
- **Trust:** lifecycle state is a signal, never success. Verify report + `git -C <worktree>
  diff --stat` + S-tier checks via a plain `pane run`; on `blocked`, inspect with `agent
  read --source detection` / `agent explain` and escalate unclear dialogs via
  `notification show`. Workers run acceptEdits + the Bash sandbox; merge/push authority
  stays with the conductor; disable auto-memory for ephemeral workers (one shared project
  memory dir across worktrees leaks context between them).
- **Capacity:** plan for conductor + ONE active heavy worker — dw4's balloon can pin near
  the 4 GiB floor under node load, and the 1-member tep pool serializes PW-class runs anyway.
- **Teardown:** `herdr worktree remove --workspace <ws>` only after verification (never
  `--force` first); keep briefs/reports out of product commits.
- **Upgrade:** bump `dev_worker_herdr_version` + `dev_worker_herdr_sha256` together. A herdr server
  restart kills every pane process (pre-1.0, no compatibility guarantee across versions), so treat
  a bump as a maintenance action on the pilot host, not a background refresh.
- **Rollback:** flip the toggle off (or delete `host_vars/dev-worker-4.yml`), then on the VM:
  `systemctl disable --now herdr`, remove `/usr/local/bin/herdr*`, `/usr/local/bin/herdr-pilot-reset`,
  `/etc/systemd/system/herdr.service`, and `~c4/.config/herdr/`. The role installs but — like the
  other optional features — never uninstalls.

## Pasting images from Windows (remote agents)

Claude Code's native `Ctrl+V` image paste cannot work over plain SSH: on Linux it shells out to
xclip/wl-paste, which need a display server, and the common Windows X servers forward text only
(Anthropic closed the OSC-based proposals as not-planned). Everything below therefore works by
materializing the image as a **remote file** and handing the agent its **path** — a bracketed
paste of an image path auto-attaches as `[Image #N]` in Claude Code; Codex also attaches pasted
paths, or takes `codex -i <path>`.

**Path 1 — herdr remote attach (dev-worker-4).** Install herdr ≥ 0.8.2 on the workstation
(`powershell -ExecutionPolicy Bypass -c "irm https://herdr.dev/install.ps1 | iex"` — 0.8.2 is the
first stable with Windows `--remote`), then attach with `herdr --remote ssh://c4@192.168.0.11`.
Copy a screenshot, focus the pane running the agent, press `ctrl+v`: herdr ships the PNG over the
existing SSH connection (16 MiB cap), stages it on the worker under
`/tmp/herdr-clipboard-images-<uid>/` (0600; deleted when the client disconnects and after 24h — so
have the agent read it before detaching), and bracket-pastes the path into the pane. No
server-side config. If the terminal swallows `ctrl+v`, rebind `keys.remote_image_paste` in the
**local** `%APPDATA%\herdr\config.toml` (e.g. `"ctrl+alt+v"`).

**Path 2 — plain tmux, any worker: `scripts/dw-paste.ps1`.** Copy a screenshot (or copy an image
file in Explorer), run `powershell -File scripts\dw-paste.ps1` (defaults to dev-worker-4; override
with `-SshTarget c4@192.168.0.N`). It saves the clipboard image as PNG (a copied image file is
uploaded as-is, original extension kept), scp's it to
`/workspace/c4/pastes/` (created by the role, 0700, aged out after 14 days via tmpfiles.d),
preloads the remote tmux paste buffer, and puts the same path on the local clipboard. In the
remote tmux, `prefix+]` pastes the path into the agent prompt (tmux ≥ 3.2 pastes bracketed, which
triggers Claude Code's auto-attach).

## Verify

- `/workspace` mounted: `mountpoint -q /workspace && echo ok`
- docker: `docker run --rm hello-world`
- tmux: `tmux ls` shows `main`; the dashboard is the `sessions` session (`claude-dashboard`)
- ttyd: `https://dw1.chifor.me` (CF Access login) from anywhere, or `https://192.168.0.37/` on LAN/Tailscale (trust the Caddy local-CA cert)
- metrics: `curl -s localhost:9100/metrics | head`
- agents (both `c4` + `claude-agent`): `which claude codex` resolve under `~/.npm-global/bin`;
  `claude --version`, `codex --version`; `getfacl ~/.claude ~/.codex` shows c4 `rx`
- persistence: start a tmux pane, reboot the VM, confirm tmux-continuum restored the session
- dashboard layout: `tmux list-windows -t sessions -F '#{window_name}'` must be exactly
  `home system jobs github docker cluster cheats` — see the dashboard/resurrect note below
- **memory watch:** node_exporter `node_memory_MemAvailable` + `node_pressure_*`. The 4 GiB
  balloon floor (12 GiB on dw1/dw4) guarantees each guest's idle working set; a busy worker inflates
  toward its ceiling (16 GiB) when the node's LLM is idle-unloaded. If a host shows sustained pressure, the first lever is its
  heavyweight LLM — confirm it idle-unloaded (or shorten the llama-swap TTL, `docs/runbooks/ai-model-swap.md`)
  — then, only if still pressured, downsize that node's Talos CP VM (`control_planes{}`, rolling reboot
  via `talosctl shutdown` — see `ai-host-setup.md`) rather than starving a dev-worker.

## Remote access (web terminals)

The ttyd terminals are published as `dw1/dw2/dw3.chifor.me` through the **existing in-cluster
Cloudflare tunnel**, each gated by a **Cloudflare Access** policy (allow-list = `allow_email`):

- ingress: `kubernetes/apps/apps/edge/cloudflared.yaml` routes `dwN.chifor.me` → `https://192.168.0.3N`
  (the VM's Caddy; `noTLSVerify` + `httpHostHeader` for the local-CA cert).
- DNS + Access: `kubernetes/infra/cloudflare/` (`dns.tf` CNAMEs + `access.tf` apps). The DNS records
  `depends_on` the Access apps, so Access is enforcing **before** any `dwN.chifor.me` resolves — never
  an unauthenticated window to the passwordless-sudo shell.

From anywhere: open the Homepage **Dev Workers** tile (or `https://dw1.chifor.me`) → Cloudflare Access
login → terminal. On the LAN/Tailscale, `https://192.168.0.37/` still works directly.

**Apply order:** merge → Flux applies the ingress → `kubectl -n edge rollout restart deploy/cloudflared`
→ `tofu -chdir=kubernetes/infra/cloudflare apply` (creates Access **then** DNS) → `kubectl -n homepage
rollout restart deploy/homepage`. The ingress is inert until a `dwN` name resolves (DNS is created only
by the tofu apply, after Access), so the ingress/cloudflared step ordering is not security-sensitive.
(The `dev_worker_enable_cloudflared` role toggle — per-VM cloudflared on its own tunnel — is an
ALTERNATIVE, not used here.)

**Threat model.** The only thing between the internet and a passwordless-sudo shell is the CF Access
gate, which trusts `allow_email`'s identity + an 8h browser session. So: **enable 2FA on the Access
login method** (the email account / IdP), treat the Access session cookie as root-equivalent, and
prefer the LAN/Tailscale path when you can. A compromised `allow_email` mailbox or session cookie =
shell access for the session window. Consider mTLS / device posture in CF Zero Trust if you want a
second factor at the edge.

## Notes

- The role replaces the homelab 1,833-line `claude-worker-bootstrap.sh` with idempotent Ansible.
- Docker data-root is `/workspace/docker` (set via `daemon.json`) — not a `/var/lib/docker` bind.
- `tmp_hygiene` ships only simple tmpfiles.d aging; the homelab loopback `/tmp` cap + LRU evictor are
  intentionally not ported (gated by `dev_worker_tmp_hygiene_full`, a follow-up if ever needed).
- Scoped kubeconfig fan-out into `~/.kube/config` is operator/tofu work (out of scope for the role);
  the dashboard's k9s window degrades gracefully without one.
- **The `sessions` dashboard is deliberately excluded from tmux-resurrect snapshots.** It is code
  (`claude-dashboard`) rebuilt at every boot, whereas resurrect's restore renames windows **by index**
  and does not check that the window at that index is the one it saved. Since the launcher builds the
  dashboard exactly as the tmux server starts — which is when continuum fires its restore — both write
  the same session, and a snapshot whose window list has drifted wins. `@resurrect-hook-post-save-layout`
  (`/usr/local/bin/tmux-resurrect-filter`) strips the dashboard from each snapshot before resurrect
  repoints `last` at it. **`main` and ad-hoc sessions are still saved and restored** — this is not a
  persistence opt-out. Renaming `$SESSION` in `claude-dashboard.sh` without renaming
  `DASHBOARD_SESSION` in the filter silently re-arms the bug; `just test-dev-worker` pins them together.
  - How it presented (dev-worker-4, 2026-08-02 → 2026-08-16): `home` is the one window left as a plain
    shell, so exiting it closed it for good; `renumber-windows on` slid the rest into indices 1–6 and
    the next snapshot recorded those six. At the following boot the launcher rebuilt all seven windows
    correctly and the restore then relabelled 1–6 from that stale snapshot, sliding every name one
    window left (the `home` shell became "system", htop became "jobs", …) while index 7 kept its own
    name, so `cheats` appeared twice. The mislabelled result was saved again 15 minutes later, which is
    what made it survive three reboots. Nothing goes red in this state — the unit is `active`, no log
    line is written, and there are still seven windows — so `tmux list-windows` is the only check.
  - Repairing a worker already in that state: the windows are in the right order and only the *names*
    are wrong, so rename in place (`tmux rename-window -t sessions:<i> <name>`) rather than killing the
    session — window 1 is `home` and usually has a live `claude` in it. Also kill any pane whose
    `pane_start_command` mentions `resurrect/restore/pane_contents` (restore debris), and run the
    filter once over `$(readlink -f ~/.local/share/tmux/resurrect/last)` so a reboot before the next
    save cannot restore the corruption.
