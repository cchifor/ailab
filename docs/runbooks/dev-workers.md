# Runbook: dev-worker VMs (Claude Code + Codex)

Three interactive developer VMs (`dev-worker-1/2/3`, one per Proxmox node) that run **Claude Code**
and **Codex** inside tmux, with the homelab claude-worker feature set ported to ailab's idiom: a
tofu module creates the VMs, the `dev_worker` Ansible role configures them.

- tofu: `kubernetes/infra/dev-workers/`
- role: `ansible/roles/dev_worker/` · playbook: `ansible/dev-workers.yml`
- inventory group: `dev_workers` (`.50/.51/.52`) · secrets: `ansible/secrets/dev-worker.sops.yaml`

| Host | Node | vmid | IP | Sizing |
|---|---|---|---|---|
| dev-worker-1 | ai-node1 | 4201 | 192.168.0.37 | 8 vCPU / 16 GiB (2–16 balloon) / 40+128 GiB |
| dev-worker-2 | ai-node2 | 4202 | 192.168.0.38 | 8 vCPU / 16 GiB (2–16 balloon) / 40+128 GiB |
| dev-worker-3 | ai-node3 | 4203 | 192.168.0.39 | 8 vCPU / 16 GiB (2–16 balloon) / 40+128 GiB |

## Pre-flight gate (clear BEFORE `tofu apply`)

**GPU VRAM carve** — confirm the per-node BIOS VRAM reservation (`docs/runbooks/ai-host-setup.md`;
up to ~64 GiB). This sets the real system-RAM budget. The default dev-worker memory is a **16 GiB
ballooned ceiling / 2 GiB floor**; if the carve is large and you run heavy local builds alongside
the runner VM, lower `dev_worker_memory_mib` further (e.g. `12288`) in
`kubernetes/infra/dev-workers/terraform.tfvars`.

(IPs `.37/.38/.39` are free static addresses inside the `.2`–`.50` reserve, below the DHCP pool —
no router change is needed.)

Per-node RAM budget: Talos CP **32 GiB hard** + ai-llm LXC (24 GiB cap, ~0.5 GiB real) + runner
(24 GiB ceiling / 1 GiB floor) + dev-worker (16 GiB ceiling / 2 GiB floor). Idle footprint is small;
the pressure point is simultaneous heavy CI + dev build on one node (balloon + swap absorb it).

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
- **memory watch (1–2 weeks):** node_exporter `node_memory_MemAvailable` + `node_pressure_*`. If
  pressure appears, lower `dev_worker_memory_mib` (16→12 GiB), rolling one node at a time.

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

**Apply order (security-critical):** merge → Flux applies the ingress → `kubectl -n edge rollout
restart deploy/cloudflared` → `tofu -chdir=kubernetes/infra/cloudflare apply` (creates Access **then**
DNS) → `kubectl -n homepage rollout restart deploy/homepage`. (The `dev_worker_enable_cloudflared`
role toggle — per-VM cloudflared on its own tunnel — is an ALTERNATIVE, not used here.)

## Notes

- The role replaces the homelab 1,833-line `claude-worker-bootstrap.sh` with idempotent Ansible.
- Docker data-root is `/workspace/docker` (set via `daemon.json`) — not a `/var/lib/docker` bind.
- `tmp_hygiene` ships only simple tmpfiles.d aging; the homelab loopback `/tmp` cap + LRU evictor are
  intentionally not ported (gated by `dev_worker_tmp_hygiene_full`, a follow-up if ever needed).
- Scoped kubeconfig fan-out into `~/.kube/config` is operator/tofu work (out of scope for the role);
  the dashboard's k9s window degrades gracefully without one.
