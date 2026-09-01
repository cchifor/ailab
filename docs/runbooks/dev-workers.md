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
per-worker overrides: `memory_floating_mib` (12 GiB floors on dw1/dw4 — node1 mitigation) and
`memory_mib` (12 GiB ceiling on dw6 — downsize POC, see the section below).

| Host | Node | vmid | IP | Sizing |
|---|---|---|---|---|
| dev-worker-1 | ai-node1 | 4201 | 192.168.0.8  | 8 vCPU / 16 GiB (4–16 balloon) / 40+128 GiB |
| dev-worker-2 | ai-node2 | 4202 | 192.168.0.9  | 8 vCPU / 16 GiB (4–16 balloon) / 40+128 GiB |
| dev-worker-3 | ai-node3 | 4203 | 192.168.0.10 | 8 vCPU / 16 GiB (4–16 balloon) / 40+128 GiB |
| dev-worker-4 | ai-node1 | 4204 | 192.168.0.11 | 8 vCPU / 16 GiB (4–16 balloon) / 40+128 GiB |
| dev-worker-5 | ai-node2 | 4205 | 192.168.0.12 | 8 vCPU / 16 GiB (4–16 balloon) / 40+128 GiB |
| dev-worker-6 | ai-node3 | 4206 | 192.168.0.13 | 8 vCPU / **12 GiB** (4–12 balloon, downsize POC) / 40+128 GiB |

> **IP renumber (consecutive .8–.13).** cloud-init fixes the IP at create and the tofu module has
> `lifecycle.ignore_changes = [initialization]`, so the live IPs were changed **in-guest** (not by tofu):
> per worker — add the new IP live, rewrite the address in `/etc/netplan/50-cloud-init.yaml`, write
> `/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg` (`network: {config: disabled}`) so cloud-init
> won't revert it, then `netplan apply`. The map IPs above are kept in sync as documentation.

## Pre-flight gate (clear BEFORE `tofu apply`)

**On-demand heavyweight LLMs (the prerequisite for the 2nd worker per node).** The 2nd worker per
host fits only because the rarely-used heavyweight models on node2/node3 (gpt-oss ~59 GiB, Qwen3.5-122B
~71 GiB GTT) are now **idle-unloaded via llama-swap** rather than pinned resident — see
`docs/runbooks/ai-model-swap.md`. With the model idle, the host drops to ~45% used and ballooning
actually works, so a worker inflates toward the ceiling on demand. Dev-worker memory defaults to a
**16 GiB ceiling with a 4 GiB floor** (module scalars
`dev_worker_memory_mib` / `dev_worker_memory_floating_mib`; per-worker overrides on dw1/dw4 floors
and the dw6 ceiling) — low floor by design, because ballooning
now inflates busy workers and 4 GiB is what lets a node hold its on-demand heavyweight **plus** its
two workers-at-floor at once.

(IPs `.37/.38/.39` + `.5/.6/.7` are free static addresses inside the `.2`–`.50` reserve, below the
DHCP pool — no router change is needed.)

## Post-testpool ceiling downsize (POC on dev-worker-6, 2026-09-01)

Since the test-env pool went live (`kubernetes/apps/infrastructure/testpool/`, `tep`), the heavy
compose stacks (L/XL/Playwright class) lease kata envs on talos-env-node-1 instead of running on
the worker; only S-class (plain pytest, 2–4 GiB) and the small M-class docker tiers stay local, so
the 16 GiB ceiling is oversized. Measured over the 10 days ending 2026-09-01 (node_exporter,
pre-pool load included): peak used was 7.9 GiB (dw4) / 6.9 GiB (dw1), and ≤2.5 GiB on the other
four.

**POC:** dev-worker-6 runs a **12 GiB ceiling** (`memory_mib = 12288` override in
`kubernetes/infra/dev-workers/variables.tf`), hand-applied 2026-09-01 (`qm set 4206 --memory 12288`
+ `qm reboot`) and codified the same day — the first `tofu apply` after the merge no-ops.
Post-resize checks passed: prometheus-node-exporter :9100 up, local `docker run` fine, `tep list`
reaches the pool. **Fleet-wide plan** (after the POC soaks): drop the ceiling scalar to 12288 for
all six, freeing 4 GiB × 2 workers of worst-case commitment per node — headroom that feeds the
planned env-big (24 GiB) testpool node. On dw1/dw4 a 12 GiB ceiling meets their codified 12 GiB
floor (floor == ceiling: effectively fixed memory), which matches how node1 already behaves —
ballooning never inflates guests there. Do NOT lower the dw1/dw4 floors as part of this; that
mitigation stands until node1 capacity is fixed (see the note in variables.tf).

Per-node RAM budget (~125 GiB usable): Talos CP (**cp1 24 / cp2 24 / cp3 28 GiB hard** —
`kubernetes/infra/variables.tf`) + ai-llm LXC (96 GiB cap; **~0 GiB when idle-unloaded**, ~59/71 GiB
when a heavyweight is loaded on demand) + runner (24 GiB ceiling / **10 GiB floor**, ×2 node1/node2,
×1 node3) + dev-worker (16 GiB ceiling — 12 GiB on dw6 / **4 GiB floor**, ×2 per node). In steady
state (heavyweight unloaded) node2/node3 sit ~45% used and workers balloon freely toward the ceiling. **Time-share rule:** a
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

## Optional features (off by default)

Enable in `ansible/group_vars/dev_workers.yml`, add the secret, re-run `just dev-workers`:

| Toggle | Secret (in `dev-worker.sops.yaml`) | Notes |
|---|---|---|
| `dev_worker_enable_restic` | `dev_worker_restic_password` | Targets a restic REST server on the QNAP by default (`dev_worker_restic_backend: rest`); QNAP-side rest-server setup is out of scope. `nfs` and `none` backends also supported. |
| `dev_worker_enable_cloudflared` | `dev_worker_cf_tunnel_token` | Public access via CF tunnel + CF Access. |
| `dev_worker_enable_password_auth` | `dev_worker_admin_password` | Enables sshd PasswordAuthentication for c4. |
| `dev_worker_enable_herdr` | — (no secret) | PILOT — per-host in `host_vars/dev-worker-5.yml`, not group_vars. See § "herdr pilot" below. |
| `dev_worker_enable_openbao` | `dev_worker_openbao_credentials` (per-host `role_id`/`secret_id` map) | Per-VM OpenBao AppRole + `bao agent` + `cred` helper (ADR 0020). Not a pure toggle: the cluster side must be live and each worker's secret-id minted first — `docs/runbooks/openbao-dev-workers.md`. |

Create the encrypted secrets file:

```bash
cp ansible/secrets/dev-worker.sops.yaml.example ansible/secrets/dev-worker.sops.yaml
#   edit values, then encrypt in place (uses the .sops.yaml dev-worker creation_rule)
sops --encrypt --in-place ansible/secrets/dev-worker.sops.yaml
git add ansible/secrets/dev-worker.sops.yaml
```

## herdr pilot (dev-worker-5 only)

[Herdr](https://herdr.dev/) — an agent-native terminal multiplexer — runs on dev-worker-5
**beside** tmux. This is an evaluation, not a migration: tmux keeps everything load-bearing (the
shared `main` session, ttyd/SSH parity, the `sessions` dashboard, resurrect/continuum reboot
persistence). Herdr adds the two things tmux cannot express: an attention queue over agent panes
(working / blocked / done / idle) and native conversation resume (`claude --resume <id>`) after a
server restart. The 2026-08-31 evaluation that led here concluded **keep tmux**: herdr's reboot
restore is shape-only (non-agent panes return as fresh shells), it has no selective-restore control
(the same collision class as the dev-worker-4 dashboard incident), and it is pre-1.0 from a
one-person company — so it gets one host, a memory cap, and a kill switch.

- **Enable/disable:** `dev_worker_enable_herdr` (default off; flipped only in
  `host_vars/dev-worker-5.yml`). Deploy with `just dev-workers`, or targeted (the explicit
  `ANSIBLE_CONFIG` matters — on WSL the world-writable `/mnt/c` CWD makes an implicit
  `ansible.cfg` silently ignored, which drops the inventory and "deploys" to zero hosts; same
  trap as the ci-runners runbook):
  `cd ansible && ANSIBLE_CONFIG="$(pwd)/ansible.cfg" ansible-playbook dev-workers.yml -l dev-worker-5 -t herdr`.
  A `-t herdr` run needs an already-provisioned worker (it asserts `/workspace/c4` rather than
  creating it).
- **What it installs:** pinned static binary `/usr/local/bin/herdr-<version>` + `herdr` symlink
  (sha256-pinned in the role defaults — upstream ships no checksum file, so a version bump must
  recompute the hash), an ansible-managed `~c4/.config/herdr/config.toml` (pane-history off:
  secrets; agent-resume on: the point of the pilot), the `herdr.service` system unit (runs
  `herdr server` headless as c4, memory-capped like agentforge), and the `herdr-pilot-reset` hatch.
- **Attach:** SSH in (you land in tmux `main` via the auto-attach) and run `herdr` in a pane — or
  bypass tmux entirely with `ssh -t c4@192.168.0.12 herdr` (a remote command runs a non-login
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
- **Upgrade:** bump `dev_worker_herdr_version` + `dev_worker_herdr_sha256` together. A herdr server
  restart kills every pane process (pre-1.0, no compatibility guarantee across versions), so treat
  a bump as a maintenance action on the pilot host, not a background refresh.
- **Rollback:** flip the toggle off (or delete `host_vars/dev-worker-5.yml`), then on the VM:
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

**Path 1 — herdr remote attach (dev-worker-5).** Install herdr ≥ 0.8.2 on the workstation
(`powershell -ExecutionPolicy Bypass -c "irm https://herdr.dev/install.ps1 | iex"` — 0.8.2 is the
first stable with Windows `--remote`), then attach with `herdr --remote ssh://c4@192.168.0.12`.
Copy a screenshot, focus the pane running the agent, press `ctrl+v`: herdr ships the PNG over the
existing SSH connection (16 MiB cap), stages it on the worker under
`/tmp/herdr-clipboard-images-<uid>/` (0600; deleted when the client disconnects and after 24h — so
have the agent read it before detaching), and bracket-pastes the path into the pane. No
server-side config. If the terminal swallows `ctrl+v`, rebind `keys.remote_image_paste` in the
**local** `%APPDATA%\herdr\config.toml` (e.g. `"ctrl+alt+v"`).

**Path 2 — plain tmux, any worker: `scripts/dw-paste.ps1`.** Copy a screenshot (or copy an image
file in Explorer), run `powershell -File scripts\dw-paste.ps1` (defaults to dev-worker-5; override
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
- **memory watch:** node_exporter `node_memory_MemAvailable` + `node_pressure_*`. The uniform 4 GiB
  balloon floor guarantees each guest's idle working set; a busy worker inflates toward 16 GiB when
  the node's LLM is idle-unloaded. If a host shows sustained pressure, the first lever is its
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
