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

**Balloon shares (2026-10-02, set outside tofu).** All four workers carry `shares: 3000` (`qm set
<vmid> --shares 3000`); runners keep the default 1000. When pvestatd grows guests it serves the ones
under memory pressure first (guest free memory <= 25% of the floor) and splits the rest of its goal by
`shares` (`PVE::AutoBalloon::compute_alg1`), so an interactive worker gets 3x a runner's slice of
the headroom. Nothing restarts: `shares` is a fast-plug option. bpg/proxmox has **no** attribute for
it (`memory.shared` is the PVE `shared` setting, a different thing) and never sends it, so `tofu apply`
leaves it alone, but a recreated VM comes back at the default. Reapply it after any rebuild. Context:
`plans/2026-10-02-balloon-headroom-plan.md`.

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

**The Codex sandbox has network** (`[sandbox_workspace_write] network_access = true` in each user's
`~/.codex/config.toml`, set by `tasks/codex.yml` from `dev_worker_codex_sandbox_network`). Without it,
every sandboxed command runs in an empty network namespace, and `git push` fails with
`Could not resolve host: git.chifor.me` although the worker's own DNS is fine. That was the
2026-09-26 dev-worker-4 report, which read as a DNS outage. Inside the sandbox, git's `store` credential
helper also logs `unable to get credential storage lock ... Read-only file system` after a successful
auth. That line is harmless: the file is rendered by the OpenBao agent, not by git.

### Codex through the router (since 2026-10-09)

**All four workers' Codex runs on the estate's LLM router** (router.chifor.me), on ONE ChatGPT
subscription the router holds: account `codex-5`. The workers do not use their own ChatGPT logins
for this. Role `dev_worker`, `tasks/codex.yml`, `dev_worker_codex_router_enabled: true` in
`group_vars/dev_workers.yml`. Each user's `~/.codex/config.toml` gets:

```toml
model_provider = "llm-router"            # top level, next to the model pin

[model_providers.llm-router]
name = "llm-router"
base_url = "https://router.chifor.me/v1"
wire_api = "responses"
auth = { command = "/usr/local/bin/codex-router-key" }

[profiles.chatgpt]                       # escape hatch: the host's own ChatGPT login
model_provider = "openai"
```

How a request travels:

1. Codex runs `/usr/local/bin/codex-router-key` for its bearer. The helper reads this host's router
   key with `cred get <hostname> llm_router_codex_key` (OpenBao `af/dev-workers/<hostname>`). It
   keeps the last good value in `~/.config/llm-router/codex.key` (0600) and prints that copy when
   the vault cannot be read. It says so on stderr: `... failed; using the cached key`.
   `CODEX_ROUTER_NO_CACHE=1` skips the fallback, which is how the fleet validator proves the live
   vault path. The key is never in the config file, and never in the environment of
   the CLI or its tools.
2. Codex sends its own Responses request (the real model name, `gpt-6-astra`) to
   `https://router.chifor.me/v1/responses`.
3. The router's **Codex-native routes** pass that request through as sent. These are the
   `codexNative` setting in `ROUTER_PLUGIN_CONFIG`, `kubernetes/apps/apps/llm-router/router.yaml`,
   and `docs/runbooks/llm-router.md` § "Codex-native routes". Each key is limited to the six `dw-*`
   routes, one per model, all on `codex-5`. The router maps the CLI's model name to the route that
   serves it (`gpt-6-astra` → `dw-gpt-6-astra`).

The router's console (Activity, API keys) shows each worker's requests under its own key:
`dev-worker-N codex (ailab dev worker, <ip>)`.

| host | router key | prefix |
|---|---|---|
| dev-worker-1 | `key_aa2543746716` | `lrk_w7bR` |
| dev-worker-2 | `key_43132b508eaa` | `lrk_UxUf` |
| dev-worker-3 | `key_65e07ec0f75a` | `lrk_meXV` |
| dev-worker-4 | `key_b06fc4e10a3c` | `lrk__1Wv` |

All four keys expire on **2027-10-09** (365 days). The values live only in
`kubernetes/apps/infrastructure/security/openbao/devworker-seeds.sops.yaml`
(`dev-worker-N.json` → `af/dev-workers/dev-worker-N`, seed-wins). From there the
`openbao-devworker-provision` Job writes them to the vault.

**Shared quota and capacity.** The four workers share one subscription's windows. The router shows
them on `codex-5` (Status; `GET /admin/v1/config` → account `codex-5`). `codex-5` serves only these
routes: it was taken out of the shared `codex` route the same day. Its `concurrency` (6) caps how
many requests the four workers run at once. A seventh gets `429 busy` with `Retry-After`, which
Codex retries. A spent window answers 429 until it resets. Run `codex -p chatgpt` to work on the
host's own login meanwhile.

**After any change to the provider settings,** restart the managed app-server daemon as the user:
`codex app-server daemon restart`, then restart open TUIs. Codex >= 0.158 keeps the provider the
daemon started with. `dev_worker_codex_version` is 0.160.0, the version on which the `auth` command
was measured.

**Check a worker:** `scripts/validate-codex-fleet.sh dev-worker-3` (or the whole fleet). Router mode
reports `router(llm-router)`: the key helper must yield a key, then a real `codex exec` goes through
the router.

**Rotate a worker's key.**

1. Mint the new key (router admin API `POST /admin/v1/keys` with name, the six `dw-*` routes and
   `expiresInDays`).
2. Re-encrypt it into `devworker-seeds.sops.yaml` (decrypt, edit, `sops -e --filename-override`,
   every leaf `ENC[`).
3. Merge, then delete Job `openbao-devworker-provision` so Flux re-runs it.
4. The helper picks the new value up on its next call. Revoke the old key.

**Turn it off** (all workers, or one host in `host_vars`): `dev_worker_codex_router_enabled: false`
→ `ansible-playbook dev-workers.yml -t codex`, then `codex app-server daemon restart` per user. The
provider table, the top-level key and each user's cached key are removed, and Codex is back on
the host's own ChatGPT login. Revoke the keys on the router if they are no longer wanted.

| symptom | cause / fix |
|---|---|
| `401 UNAUTHORIZED` from the router | key revoked or expired: rotate (above) |
| `codex-router-key: no router key` | vault unreachable and no cached copy yet: `cred list` (sink token, sealed vault), see `openbao-dev-workers.md` |
| `403 ROUTE_NOT_ALLOWED ... none of them serves "X"` | the CLI asked for a model with no `dw-X` route: add the route (one per model, `codex-5` only) and the key's routes |
| `400 UNSUPPORTED_PARAMETER: include` | the request reached a route that is not in `codexNative.routes` (subset parser): check the router's `ROUTER_PLUGIN_CONFIG` |
| `429 ... busy` | all `codex-5` slots in use by the four workers: wait, or raise the account's `concurrency` |

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

**A seat whose login is dead parks itself** (since 2026-10-01). The CLI's `Not logged in ·
Please run /login` is account-scoped: the seat parks for 6 h (`seat '<x>' login unusable (...);
parked 6h` in the journal) and the same review moves on to the next seat — it used to walk the
model ladder on the dead seat and charge the PR, which quarantined platform#1842 and ailab#1005
while two healthy seats sat idle. The hourly probe does the same for a seat with no credential
file or a 401 its keepalive could not cure, and clears the park the first time the seat answers,
so a re-login (above) is picked up within the hour without a restart. A 403 never parks: that is
a token-file seat, which serves. `ReviewbotUsageProbeFailing` still fires for the dead seat, and
it is now a capacity warning rather than an outage — but the re-login is still yours to do.

`~/.claude/projects` under each seat grows by one directory per review — the CLI keys them by
working directory and reviewbot hands it a fresh tmpdir every run; c4's held 1 926 on
2026-09-18. Pre-existing behaviour, now per seat; harmless until the disk says otherwise.

### Router seats: reviews through the LLM router (API key, not a subscription login)

Since 2026-10-07 a seat may be an **API key on the estate's LLM router** (router.chifor.me,
cchifor/llm-router) instead of a CLI login: an entry in `pr_reviewer_llm_seats` carrying
`router_url` (reviewbot.py § router seats). The review is one streamed
`POST /v1/chat/completions` with the same tool-less prompt; the key sits in
`/etc/reviewbot/router-key` (0600, c4), installed from `reviewbot_<persona>_router_key` in
`ansible/secrets/reviewbot.sops.yaml` and read per call (a rotated key needs no restart).

| persona | seat | key | route | state |
|---|---|---|---|---|
| codex (reviewer-2) | `r` | `key_d7adf31fb93e` | `codex` (round-robin gpt-6-luna + 3x gpt-6-astra) | **serving** |
| claude (reviewer-1) | `r` | `key_f856495c9b0c` | the ladder's own names: `fable`, `opus`, `sonnet` (router Claude models; a `claude` route = 4x sonnet also exists) | **serving** since 2026-10-07 |

How it behaves - the existing park machinery, unchanged:

- **Preferred, never sticky.** Within a tier a router seat is always tried first; a subscription
  seat that served while the router was parked hands back as soon as the park lapses.
- **401** (key wrong/expired/revoked) or an unreadable key file: the seat parks 6 h as a dead
  login (`seat 'r' login unusable (...)` in the journal). Re-mint the key (below).
- **403 `ROUTE_NOT_ALLOWED` / 404 `MODEL_NOT_FOUND`**: that (seat, tier) pair parks 6 h; nothing
  else does. A 403 without the router's code (Cloudflare's 1010 page) is an ordinary failure.
- **429 reason `busy`**: waited out in place (Retry-After, at most 6 x 30 s). Any other 429 parks
  the seat until Retry-After (else 15 min) and the review moves to the subscription seats.
- **5xx / 504 / a stream error chunk / a truncated answer**: an ordinary failure, no park.
- The usage poller never probes a router seat (no window to read); the router's own console shows
  its usage per key. **Keep the subscription seats configured on reviewer-2**: the hourly probe
  is what refreshes the seat logins dsh and LiteLLM read (dsh.md § Codex subscriptions).

**Minting / rotating a key** - never print it; the router's helper writes it to a 0600 file:

```bash
# on the reviewer, as c4 (the router skill: https://router.chifor.me/agent/skill.md)
curl -fsS https://router.chifor.me/agent/access.py -o /tmp/router-access.py
python3 /tmp/router-access.py --url https://router.chifor.me --name "reviewer-<persona> ..."   --purpose "Automated PR code review ..." --key-file ~/.config/llm-router/key
# -> prints a user code; the router admin approves it (console: Settings, API keys) and picks routes
```

Then escrow it into `reviewbot.sops.yaml` as `reviewbot_<persona>_router_key` - decrypt, add the
leaf, re-encrypt with `sops -e --filename-override ansible/secrets/reviewbot.sops.yaml` and
assert every top-level leaf is `ENC[` (a plain `sops set` writes PLAINTEXT for a key the file's
stored `encrypted_regex` predates) - deploy `reviewers.yml -t reviewbot`, delete the
`~/.config/llm-router/key` copy, and have the admin revoke the old key.

**How the claude persona was turned over** (2026-10-07, after the router gained Claude accounts;
before that its `claude` model answered 429 "No eligible account"): the seat at the top of
`host_vars/reviewer-1.yml` is `{ name: r, sudo_user: "", router_url: "https://router.chifor.me",
key_file: "{{ pr_reviewer_router_key_file }}" }`. It has no `models` map, so every ladder tier
(`fable`, `opus`, `sonnet`) is requested under its own name, which the router serves as a Claude
model. Each tier was probed with a real review first (a 429 there means the router has no eligible
Claude account). Re-probe after any router change. Never point the claude persona at a Codex route:
merges need BOTH personas clean, and two GPT reviews are not two independent reviews.

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

## Pasting images and files into agents

The agents run on the worker, so a file has to exist **on the worker** before Claude Code or Codex can
use it. Every path below does the same two things: put the file in `/workspace/c4/pastes/` (0700,
aged out after 14 days by tmpfiles.d) and paste its **path** into the agent's pane as a bracketed
paste. A pasted image path becomes `[Image #N]` in both agents; a PDF/document path arrives as text
and the agent reads it (Claude's Read handles PDFs; `pdftotext` / `pandoc` are installed for Codex
and for DOCX/ODT). Measured 2026-09-28: **Codex only attaches when the paste holds exactly one path**,
so every tool here pastes one path per paste. Claude Code's own Ctrl+V cannot work remotely: it reads
the clipboard with xclip/wl-paste, which need a display on the worker.

**Web terminal (`https://dwN.chifor.me` or `https://192.168.0.N/`) — any device, both agents.**
Focus the agent's pane, then:
- **Ctrl+V** (Cmd+V on a Mac) with a screenshot or copied files on the clipboard,
- **drag and drop** files onto the terminal, or
- the **📎 button** (top right) — the reliable way on iPhone/iPad (Photos, Camera, Files).

Each file uploads (≤ 64 MB, one at a time), and its path is pasted into the focused pane followed
by a space. **Nothing sends Enter**: add your question and send it yourself. An upload that takes more
than 3 s is not pasted automatically (you may have switched panes meanwhile): its toast offers
**Paste path**. Plain-text pastes behave exactly as before. In the web terminal Ctrl+V is the
browser's paste; `^V` (literal-next) is not available there (it is over SSH). An expired Access session
shows "your login has expired — reload".

How it works (`ansible/roles/dev_worker`, tag `web-gate`, behind the gate in § "Remote access"):
Caddy serves ttyd's own page with `files/dw_paste.js` appended (spliced at converge from the running
ttyd; the converge fails if a ttyd upgrade stops exposing `window.term`), and routes `/_dw/upload` to
`dw-upload` (`files/dw_upload.py`, 127.0.0.1:7683, runs as c4, can write only the pastes directory):
strict `Content-Length` framing, the worker's own Origin + an `X-DW-Upload` header, a 2 GiB / 1000-file
directory quota reserved before reading, 0600 files published without ever replacing one. Each
converge proves it end to end through Caddy (the page carries the script; a foreign-Origin upload is
403; an own-page upload is stored, then deleted).

**SSH (Windows Terminal, macOS, Linux): Ctrl+Shift+V via `scripts/dw-paste/`.** Over SSH the terminal
itself handles the paste, so a screenshot arrives as the *laptop's* path (the Snipping Tool's
`…\ScreenClip\{GUID}.png`), which the worker cannot open. Install the helper for your OS once
(`scripts/dw-paste/README.md`: Windows = logon task + tray icon, macOS = Hammerspoon, Linux = a
desktop shortcut), then press **Ctrl+Shift+V** (Cmd+Shift+V on a Mac) in the agent's pane: the
clipboard image or files are scp'd to the pastes directory and each worker path is pasted with the
terminal's own (bracketed) paste, one per paste, never Enter; the clipboard is restored. Text and
non-dev-worker windows paste as before. The helper recognises a dev-worker window by the `[user@ip]`
marker the role's tmux puts in the terminal title (`#h [#{client_user}@192.168.0.N] #S:#W`; roll out a title
change with `-t tmux`).

**herdr remote attach (dev-worker-4).** Install herdr ≥ 0.8.2 on the workstation
(`powershell -ExecutionPolicy Bypass -c "irm https://herdr.dev/install.ps1 | iex"` — 0.8.2 is the
first stable with Windows `--remote`), attach with `herdr --remote ssh://c4@192.168.0.11`, copy a
screenshot, focus the agent's pane, press `ctrl+v`: herdr ships the PNG over the SSH connection
(16 MiB cap), stages it under `/tmp/herdr-clipboard-images-<uid>/` (0600; deleted when the client
disconnects and after 24 h — have the agent read it before detaching) and bracket-pastes the path. If
the terminal swallows `ctrl+v`, rebind `keys.remote_image_paste` in the **local**
`%APPDATA%\herdr\config.toml` (e.g. `"ctrl+alt+v"`).

**Claude only, no infrastructure: Remote Control.** Start the session with `claude --remote-control`
(or run `/remote-control` in a running one) and continue it from claude.ai/code or the Claude phone
app, which can attach photos and files directly (other files are downloaded to the worker and passed as
`@` references). Needs the claude.ai subscription login the workers already use; does nothing for
Codex; the transcript, attachments included, is stored at Anthropic — mind screenshots of credentials.

**Checks** (manual, on a dev-worker; not in CI — the runners have no Caddy/ttyd/Chromium):
- `PLAYWRIGHT_NODE_PATH=/workspace/c4/platform/tests/e2e/node_modules bash
  ansible/roles/dev_worker/tests/e2e-web-paste.sh` — a throwaway copy of the real stack (the role's
  Caddyfile template, ttyd, dw-upload, a private tmux pane) driven by Chromium: LAN login, the
  WebSocket, Ctrl+V, clipboard image, text, drag-and-drop order, paperclip, oversize refusal, no Enter.
  Run it after changing the gate, `dw_paste.js` or `dw_upload.py`, and after a ttyd bump.
- `bash ansible/roles/dev_worker/tests/check-agent-image-paste.sh` (per-agent trusted dirs via
  `CLAUDE_DIR` / `CODEX_DIR`) — does a bracketed-pasted image path still attach in both agents? Run it
  after agent upgrades: Claude self-updates and Codex is only a version floor. It reports NOT RUN
  instead of answering startup dialogs for you.

## Disk full (`/workspace` or `/`)

`/workspace` is its own disk (scsi1) and holds both the agents' worktrees and the docker/containerd
data-root, so it is the one that fills. At 100% the agent cannot start any command — cleanup
included — and has to ask for help. `DevWorkerDiskFilling` fires at <12% free on `/` or `/workspace`
(before 2026-10-01 it watched `/` only, which is how dev-worker-3 reached 100% and dev-worker-2 0.7%
free without a page).

**Automatic, under pressure: `dev-worker-disk-guard`** (`ansible/roles/dev_worker/files/disk-guard`,
timer every 5 min, since 2026-10-06). When `/` or `/workspace` is under 15% free it walks a ladder —
unused build cache → unreferenced anonymous volumes → stopped non-compose containers (>24h) and
images unused 3+ days → merged idle worktrees → deps of worktrees idle 3+ days — and stops at 25%.
Each step runs only while its own filesystem is still short (a full `/` searches `/home` only, never
costs `/workspace` a worktree). Docker steps wait while a docker client is building/pulling, or has
been starting a container for <10 min (a prune's containerd GC kills in-flight pulls, measured on the
CI runners), unless the disk is under 5%. Compose stacks,
named volumes, source, git state and untracked files are never touched. `journalctl -u
dev-worker-disk-guard`, `disk-guard --dry-run`; metrics `dev_worker_disk_guard_*` (textfile),
alerts `DevWorkerDiskGuardExhausted` (ladder ran cleanly, still low: live work — a human decides),
`DevWorkerDiskGuardFailing` (steps exit non-zero: the guard itself needs fixing) and
`DevWorkerDiskGuardStale`. Thresholds: `dev_worker_disk_guard_*` in the role defaults. Why it
exists: every reclaim before it ran on a calendar, and dev-worker-3's /workspace refilled from 95%
to 100% within a day on 2026-10-06 (180 anonymous postgres volumes created on 10-04 alone).
Agents are told the same rules (`docker run --rm`, `docker rm -v`, `compose down -v`) in a managed
block of `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`.

What reclaims it by hand, safest first (`cleanup` is the role's tool, `ansible/roles/dev_worker/files/cleanup`):

1. `sudo cleanup --no-docker --deps --dry-run`, then without `--dry-run` — node_modules, `.venv`
   (with `pyvenv.cfg`) and Rust `target` (beside a `Cargo.toml`) in git worktrees under `/workspace`
   and `/home` idle for 14+ days (`--deps-days N`). Never source, git state or untracked files; a
   worktree any process or running container is using is skipped, as is a dep dir holding a `.git`
   or a mount. This was ~42 GB per worker on 2026-10-01. The `dev-worker-deps-prune` timer runs it
   daily at ~03:30 (`journalctl -u dev-worker-deps-prune`; disable with
   `dev_worker_deps_prune_enabled: false`).
2. `sudo cleanup --no-docker --worktrees --dry-run`, then without `--dry-run` — whole **linked**
   worktrees (`.git` is a file; main clones and submodules are never touched) that hold nothing git
   does not already have, and either
   - **merged**: idle 3+ days (`--merged-days`) and merging HEAD into a network remote's default
     branch would not change it — squash merges count; or
   - **stale**: idle 30+ days (`--worktree-days`) and HEAD is on a network remote's branch.

   "Nothing to lose" means: no changes, no untracked files, no assume-unchanged/skip-worktree
   entries, no merge/rebase/cherry-pick/bisect in progress, no worktree-private refs, not locked,
   not shallow, no nested checkout, no file modified within the idle window (dependency dirs
   included), no process, mount or container (running or stopped, compose working dir included)
   using it, and only regenerable ignored files (node_modules, .venv, build/dist/out, coverage and
   test output, caches — the `DISPOSABLE` list in the tool). A `.env` or any other ignored file
   keeps the worktree. Remotes count only with a network URL (`https`, `ssh`, `git@host:`), and
   only as of their last fetch: no network call is made.

   Removal is `git worktree remove` without `--force`, run as the worktree's owner (never root), so
   git refuses anything dirty a second time. **What survives:** branch refs and everything on the
   forge. **What does not:** the working copy, its regenerable ignored files, and the worktree's
   HEAD reflog — commits only that reflog referenced (an earlier detached HEAD, a pre-rebase state)
   become unreachable and are eventually garbage-collected; and anything written into the worktree
   during the seconds of the removal itself, after the last check (accepted). To keep a worktree,
   `git worktree lock <path>` before the run. Worktrees idle 30+ days that do not qualify are printed
   as `Kept worktree … — <reason>`: that list is the owner's to settle. The `dev-worker-deps-prune`
   timer runs this step first (`journalctl -u dev-worker-deps-prune`); `dev_worker_worktree_prune_mode`
   is `remove` since 2026-10-06 (`report` = log the plan only, as it did from 10-02; `off` skips it). Assumes one rootful dockerd per worker and no rootless container runtime.
3. `cleanup --dry-run`, then `cleanup` — docker: stale compose stacks, old stopped containers,
   unused images and anonymous volumes, build cache beyond 10 GB. `--caches` adds npm/uv/Playwright.
   The `docker-buildx-prune` timer prunes the build cache toward 20 GB daily (weekly until
   2026-10-02, which let dev-worker-1 reach 38 GB). It is a best-effort target, not a hard cap:
   records still in use are kept. The same unit then runs `docker volume prune -f --filter
   label=com.docker.volume.anonymous` — anonymous volumes no container references; named volumes
   are never touched (`dev_worker_anon_volume_prune: false` turns it off). Before 2026-10-06 nothing
   reaped them: test loops that `docker rm` a postgres container without `-v` left one PGDATA volume
   per run, and dev-worker-3 hit 100% with 240 of them (12.1 GB).
4. What is left is live work: `du -xh --max-depth=2 /workspace | sort -rh | head`.

If the disk is at 100%, `docker builder prune -af` is the quickest few GB to get the agent moving
again (build cache only; nothing running depends on it), then `docker volume prune -f --filter
label=com.docker.volume.anonymous` (unreferenced anonymous volumes; check
`docker system df` → Local Volumes RECLAIMABLE first).

## Verify

- `/workspace` mounted: `mountpoint -q /workspace && echo ok`
- docker: `docker run --rm hello-world`
- tmux: `tmux ls` shows `main`; the dashboard is the `sessions` session (`claude-dashboard`)
- ttyd: `https://dwN.chifor.me` (CF Access login) from anywhere, or `https://192.168.0.N/` on
  LAN/Tailscale (trust the Caddy local-CA cert, then the `c4` web login) — see § "Remote access"
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

Each worker's ttyd terminal is a **passwordless-sudo shell** (it attaches c4's `main` tmux session).
Every path to it is authenticated. Until 2026-09-28 the direct LAN path answered anyone on
192.168.0.0/24 — a LAN shared with cloudlab and its CI runners — with no credentials, and nothing
checked the WebSocket's Origin (plans/2026-09-28-dw-web-terminal-auth-and-file-paste-plan.md).

| Path | URL | Authentication |
|---|---|---|
| Public (anywhere) | `https://dwN.chifor.me` (Homepage **Dev Workers** tile) | Cloudflare Access login; the worker then validates Access's JWT itself |
| LAN / Tailscale | `https://192.168.0.N/` (`https://dev-worker-N/`) | web login `c4` + the fleet LAN password, once per browser session |
| SSH | `ssh c4@192.168.0.N` | unchanged (keys) |

**How the gate works** (`ansible/roles/dev_worker/tasks/web_gate.yml`, `templates/Caddyfile.j2`).
One Caddy site answers all three names, inside a single `route` (written order):

1. **Origin guard** — a WebSocket upgrade must carry exactly one of the worker's own origins
   (`https://dwN.chifor.me`, `https://192.168.0.N`, `https://dev-worker-N`); missing or foreign → 403.
   Any other request carrying a foreign Origin → 403. This is what stops a page on another site
   (or a compromised sibling `*.chifor.me` app) from driving the shell with your session.
2. **Tunnel path** — a request carrying `Cf-Access-Jwt-Assertion` goes to `dw-access-verify`
   (127.0.0.1:7682, `forward_auth`): RS256 against the team keys
   (`https://chifor.cloudflareaccess.com/cdn-cgi/access/certs`), `aud` = this worker's Access app,
   exact issuer, `exp` required. Invalid → 403 (never a fallback to the LAN login); no signing keys
   → 503; validator down → 502. So a LAN host or a cluster pod cannot get in by inventing the header.
3. **LAN path** — no JWT header → Basic auth (`c4` + `dev_worker_web_lan_password`). The authenticated
   response sets `dw_lan` (`Secure; HttpOnly; SameSite=Strict`, 12 h in the browser), and a request
   presenting it skips the prompt. The cookie is not optional: **Safari/iOS do not send cached Basic
   credentials on the WebSocket handshake** (ttyd#1437), so without it the terminal would load and
   never connect on Apple devices.

The tunnel itself is unchanged: cloudflared still sends `Host: 192.168.0.N` with `noTLSVerify`
(`kubernetes/apps/apps/edge/cloudflared.yaml`), because the gate is chosen by the header a request
carries, not by the name it used. The dw Access apps set `same_site_cookie_attribute = "lax"`
(`kubernetes/infra/cloudflare/access.tf`; unset, Cloudflare sends the Access cookie with
`SameSite=None`).

**The LAN password.** One credential for the fleet, in `ansible/secrets/dev-worker.sops.yaml`:
`sops -d --extract '["dev_worker_web_lan_password"]' ansible/secrets/dev-worker.sops.yaml` — store it
in the password manager. The `dw_lan` cookie value is a bearer credential **equal to the password**
and does **not** expire server-side (the 12 h is only how long the browser keeps it). Rotate the
password, its bcrypt and the cookie secret **together** (`dev_worker_web_lan_password`,
`_password_bcrypt`, `_cookie_secret` — the `.example` file says how to mint each), then
`ansible-playbook dev-workers.yml -t web-gate`; every browser re-prompts once.

**Every converge proves the gate** from the worker itself: no credentials → 401, a forged JWT → 403
(even with a valid cookie), a cross-site WebSocket → 403 (even with a valid cookie), the LAN
password → 200 + cookie. The play fails otherwise. Gatus (`Dev workers` group) independently expects
**401** from each `https://192.168.0.N/` every 2 min and pages via `GatusEndpointDown` if a path
reopens — it proves the gate is shut, not that the terminal works.

**Rollout / rollback.** `ansible-playbook dev-workers.yml -l dev-worker-N -t web-gate` (the SOPS
pre_tasks carry the tag). A Caddy reload drops open web terminals; the browser reconnects to the same
tmux session. **Never roll back to the old Caddyfile** — it is an open root shell on the LAN. If the
gate misbehaves, contain instead: `sudo systemctl stop caddy` (web terminal off; SSH unaffected),
fix, re-run `-t web-gate`. If only the tunnel path fails, check `journalctl -u dw-access-verify` (a
recreated Access app changes its AUD: `tofu -chdir=kubernetes/infra/cloudflare output
dev_worker_access_aud` → `group_vars/dev_workers.yml`).

**Publishing a new worker** (`dwN.chifor.me`): ingress in `cloudflared.yaml` → `tofu -chdir=
kubernetes/infra/cloudflare apply` (creates Access **then** DNS — the DNS records `depends_on` the
Access apps) → add its AUD to `dev_worker_web_access_aud` → converge → `kubectl -n homepage rollout
restart deploy/homepage`. (The `dev_worker_enable_cloudflared` role toggle — per-VM cloudflared on
its own tunnel — is an ALTERNATIVE, not used here.)

**Threat model.** Public path: Cloudflare Access (`allow_email` + an 8 h session) and the worker's
own JWT check; enable 2FA on the Access login method and treat the Access session as
root-equivalent. LAN path: the fleet password (and the `dw_lan` cookie, which is equivalent).
Remaining gap: cloudflared → Caddy uses `noTLSVerify`, so an attacker able to intercept traffic on
the LAN could lift a JWT (a bearer token for its session) — follow-up: `originServerName` +
`caPool` with the workers' Caddy root CAs.

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
