# Runbook: dev-worker VMs (Claude Code + Codex)

Six interactive developer VMs (`dev-worker-1..6`, **two per Proxmox node**) that run **Claude Code**
and **Codex** inside tmux, with the homelab claude-worker feature set ported to ailab's idiom: a
tofu module creates the VMs, the `dev_worker` Ansible role configures them.

- tofu: `kubernetes/infra/dev-workers/`
- role: `ansible/roles/dev_worker/` · playbook: `ansible/dev-workers.yml`
- inventory group: `dev_workers` (`.37/.38/.39` + `.5/.6/.7`) · secrets: `ansible/secrets/dev-worker.sops.yaml`

**All six share one uniform spec** — cores + ceiling + floor are module-wide scalars in
`kubernetes/infra/dev-workers/variables.tf` (`dev_worker_cores`, `dev_worker_memory_mib`,
`dev_worker_memory_floating_mib`); the `dev_worker_nodes` map carries only identity.

| Host | Node | vmid | IP | Sizing |
|---|---|---|---|---|
| dev-worker-1 | ai-node1 | 4201 | 192.168.0.8  | 8 vCPU / 16 GiB (4–16 balloon) / 40+128 GiB |
| dev-worker-2 | ai-node2 | 4202 | 192.168.0.9  | 8 vCPU / 16 GiB (4–16 balloon) / 40+128 GiB |
| dev-worker-3 | ai-node3 | 4203 | 192.168.0.10 | 8 vCPU / 16 GiB (4–16 balloon) / 40+128 GiB |
| dev-worker-4 | ai-node1 | 4204 | 192.168.0.11 | 8 vCPU / 16 GiB (4–16 balloon) / 40+128 GiB |
| dev-worker-5 | ai-node2 | 4205 | 192.168.0.12 | 8 vCPU / 16 GiB (4–16 balloon) / 40+128 GiB |
| dev-worker-6 | ai-node3 | 4206 | 192.168.0.13 | 8 vCPU / 16 GiB (4–16 balloon) / 40+128 GiB |

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
actually works, so a worker inflates toward the 16 GiB ceiling on demand. Dev-worker memory is a
**uniform 16 GiB ceiling with a uniform 4 GiB floor** (module scalars
`dev_worker_memory_mib` / `dev_worker_memory_floating_mib`) — low floor by design, because ballooning
now inflates busy workers and 4 GiB is what lets a node hold its on-demand heavyweight **plus** its
two workers-at-floor at once.

(IPs `.37/.38/.39` + `.5/.6/.7` are free static addresses inside the `.2`–`.50` reserve, below the
DHCP pool — no router change is needed.)

Per-node RAM budget (~125 GiB usable): Talos CP (**cp1 24 / cp2 24 / cp3 28 GiB hard** —
`kubernetes/infra/variables.tf`) + ai-llm LXC (96 GiB cap; **~0 GiB when idle-unloaded**, ~59/71 GiB
when a heavyweight is loaded on demand) + runner (24 GiB ceiling / **10 GiB floor**, ×2 node1/node2,
×1 node3) + dev-worker (16 GiB ceiling / **4 GiB floor**, ×2 per node). In steady state (heavyweight
unloaded) node2/node3 sit ~45% used and workers balloon freely toward 16 GiB. **Time-share rule:** a
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

Auth is **subscription OAuth** — provisioning injects no keys. By default **everything runs as `c4`**
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

## Optional features (off by default)

Enable in `ansible/group_vars/dev_workers.yml`, add the secret, re-run `just dev-workers`:

| Toggle | Secret (in `dev-worker.sops.yaml`) | Notes |
|---|---|---|
| `dev_worker_enable_restic` | `dev_worker_restic_password` | Targets a restic REST server on the QNAP by default (`dev_worker_restic_backend: rest`); QNAP-side rest-server setup is out of scope. `nfs` and `none` backends also supported. |
| `dev_worker_enable_cloudflared` | `dev_worker_cf_tunnel_token` | Public access via CF tunnel + CF Access. |
| `dev_worker_enable_password_auth` | `dev_worker_admin_password` | Enables sshd PasswordAuthentication for c4. |

Create the encrypted secrets file:

```bash
cp ansible/secrets/dev-worker.sops.yaml.example ansible/secrets/dev-worker.sops.yaml
#   edit values, then encrypt in place (uses the .sops.yaml dev-worker creation_rule)
sops --encrypt --in-place ansible/secrets/dev-worker.sops.yaml
git add ansible/secrets/dev-worker.sops.yaml
```

## Verify

- `/workspace` mounted: `mountpoint -q /workspace && echo ok`
- docker: `docker run --rm hello-world`
- tmux: `tmux ls` shows `main`; the dashboard is the `sessions` session (`claude-dashboard`)
- ttyd: `https://dw1.chifor.me` (CF Access login) from anywhere, or `https://192.168.0.37/` on LAN/Tailscale (trust the Caddy local-CA cert)
- metrics: `curl -s localhost:9100/metrics | head`
- agents (both `c4` + `claude-agent`): `which claude codex` resolve under `~/.npm-global/bin`;
  `claude --version`, `codex --version`; `getfacl ~/.claude ~/.codex` shows c4 `rx`
- persistence: start a tmux pane, reboot the VM, confirm tmux-continuum restored the session
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
