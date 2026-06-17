# Runbook: dev-worker VMs (Claude Code + Codex)

Three interactive developer VMs (`dev-worker-1/2/3`, one per Proxmox node) that run **Claude Code**
and **Codex** inside tmux, with the homelab claude-worker feature set ported to ailab's idiom: a
tofu module creates the VMs, the `dev_worker` Ansible role configures them.

- tofu: `kubernetes/infra/dev-workers/`
- role: `ansible/roles/dev_worker/` · playbook: `ansible/dev-workers.yml`
- inventory group: `dev_workers` (`.50/.51/.52`) · secrets: `ansible/secrets/dev-worker.sops.yaml`

| Host | Node | vmid | IP | Sizing |
|---|---|---|---|---|
| dev-worker-1 | ai-node1 | 4201 | 192.168.0.50 | 8 vCPU / 24 GiB (6–24 balloon) / 40+128 GiB |
| dev-worker-2 | ai-node2 | 4202 | 192.168.0.51 | 8 vCPU / 24 GiB (6–24 balloon) / 40+128 GiB |
| dev-worker-3 | ai-node3 | 4203 | 192.168.0.52 | 8 vCPU / 24 GiB (6–24 balloon) / 40+128 GiB |

## Pre-flight gates (clear BEFORE `tofu apply`)

1. **Router DHCP pool** — shrink the pool start from `.51` to **`.53`** in the router admin UI (no
   IaC for the router) so `.51/.52` are safe static addresses. `docs/network-plan.md` already records
   the new boundary. Skipping this risks a DHCP lease collision on `.51/.52`.
2. **GPU VRAM carve** — confirm the per-node BIOS VRAM reservation (`docs/runbooks/ai-host-setup.md`;
   up to ~64 GiB). This sets the real system-RAM budget. The default dev-worker memory is a **24 GiB
   ballooned ceiling / 6 GiB floor**; if the carve is large and you run heavy local builds alongside
   the runner VM, drop `dev_worker_memory_mib` to `16384` in
   `kubernetes/infra/dev-workers/terraform.tfvars`.

Per-node RAM budget: Talos CP **32 GiB hard** + ai-llm LXC (24 GiB cap, ~0.5 GiB real) + runner
(24 GiB ceiling / 1 GiB floor) + dev-worker (24 GiB ceiling / 6 GiB floor). Idle footprint is small;
the pressure point is simultaneous heavy CI + dev build on one node (balloon + swap absorb it).

## Provision

```bash
# 1. tofu — create the 3 VMs (separate state from runners/Talos)
cp kubernetes/infra/dev-workers/terraform.tfvars.example kubernetes/infra/dev-workers/terraform.tfvars
#   fill pve_api_token + dev_worker_ssh_public_key (reuse the runners' values)
just dev-workers-plan      # expect 1 download_file + 3 VMs (scsi0 40G import + scsi1 128G blank)
just dev-workers-apply

# 2. reach the guests (c4 is created by cloud-init on first boot)
just ping-dev-workers      # or: ssh c4@192.168.0.50

# 3. ansible — configure (Claude Code + Codex + docker + tmux + ttyd/Caddy + dashboard …)
just dev-workers
just dev-workers           # run twice — the 2nd run should report near-zero changed (idempotency)
```

## One-time manual steps (per worker)

Auth is **subscription OAuth** — provisioning injects no keys. Once per worker:

```bash
ssh c4@192.168.0.50
sudo -iu claude-agent      # the credential-owner account

claude                     # complete the Claude (Max/Pro) OAuth login once  → ~/.claude
codex login                # complete the Codex (ChatGPT) login once         → ~/.codex
gh auth login              # for the dashboard 'github' window (gh-dash)
exit
```

Then **re-run the playbook once** so the recursive ACL task re-asserts read on the freshly-written
token files (POSIX default ACLs don't apply retroactively):

```bash
just dev-workers
# verify c4 can read the shared creds:
ssh c4@192.168.0.50 'claude --version && codex --version'
```

> **CODEX_HOME caveat:** the role exports `CODEX_HOME=/home/claude-agent/.codex` for c4. If the
> installed `@openai/codex` build ignores `CODEX_HOME`, replace that export (in
> `/etc/profile.d/01-claude-shared-oauth.sh`, managed by the role's
> `profile.d-01-claude-shared-oauth.sh.j2` template) with a per-user symlink
> `ln -s /home/claude-agent/.codex ~/.codex` for c4.

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
- ttyd: browse `https://192.168.0.50/` (trust the Caddy local-CA cert)
- metrics: `curl -s localhost:9100/metrics | head`
- agents (both `c4` + `claude-agent`): `which claude codex` resolve under `~/.npm-global/bin`;
  `claude --version`, `codex --version`; `getfacl ~/.claude ~/.codex` shows c4 `rx`
- persistence: start a tmux pane, reboot the VM, confirm tmux-continuum restored the session
- **memory watch (1–2 weeks):** node_exporter `node_memory_MemAvailable` + `node_pressure_*`. If
  pressure appears, lower `dev_worker_memory_mib` (24→16 GiB), rolling one node at a time.

## Notes

- The role replaces the homelab 1,833-line `claude-worker-bootstrap.sh` with idempotent Ansible.
- Docker data-root is `/workspace/docker` (set via `daemon.json`) — not a `/var/lib/docker` bind.
- `tmp_hygiene` ships only simple tmpfiles.d aging; the homelab loopback `/tmp` cap + LRU evictor are
  intentionally not ported (gated by `dev_worker_tmp_hygiene_full`, a follow-up if ever needed).
- Scoped kubeconfig fan-out into `~/.kube/config` is operator/tofu work (out of scope for the role);
  the dashboard's k9s window degrades gracefully without one.
