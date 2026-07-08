# Runbook — self-hosted GitHub Actions runners (Proxmox)

5 ephemeral runner VMs (2 on node1/node2, 1 on node3) joining the `cchifor/platform` **`self-hosted-hv`** pool.
See ADR 0013. IaC: `kubernetes/infra/runners/` (VMs) + `ansible/roles/github_runner/` (config).

| | |
|---|---|
| VMs | `gha-runner-1..5` — vmid 4101-4105, **2 on node1/node2, 1 on node3** (gha-runner-6 reserved .19), consecutive IPs .14-.18, 8 vCPU / 24 GiB / 120 GiB each |
| Memory | balloon **12-24 GiB** (floor 12 GiB so host RAM pressure can't starve a running job — see #620) + 8 GiB guest swap (`swappiness=10`) |
| OS | Ubuntu 24.04 cloud image (cloud-init: static IP + `ubuntu` user + the ansible SSH key) |
| Label | `self-hosted-hv` (repo var `RUNNER_LABEL`; workflows use `runs-on: ${{ vars.RUNNER_LABEL }}`) |
| Registration | Ephemeral, GitHub App auth → `ephem-<host>-<epoch>-<8hex>`, one job per cycle |
| Toolchain | Docker CE + Compose v2.31.0 + Buildx, uv, k6, Node 20 (Playwright runs in-container) |

## 1. Create the GitHub App (one-time)
1. GitHub → Settings → Developer settings → **GitHub Apps → New GitHub App**. Name it (e.g.
   `ailab-ci-runners`). Webhook: **uncheck Active**. Permissions → **Repository**:
   **Administration: Read & write** (to mint runner registration tokens) + **Metadata: Read-only**.
   No other permissions, no org/account perms.
2. Create the App. Note the **App ID**. Generate a **private key** → downloads a `.pem`.
3. **Install** the App on **only** `cchifor/platform` (Install App → Only select repositories). Open
   the installation; its URL ends in `/installations/<INSTALLATION_ID>` — note the installation ID.

## 2. Provide the credentials
- Non-secret IDs → `ansible/group_vars/github_runners.yml`: set `github_runner_app_id` and
  `github_runner_app_installation_id` (replace `CHANGE_ME`).
- Secret key → copy the example, paste the `.pem`, encrypt in place:
  ```bash
  cp ansible/secrets/github-runner.sops.yaml.example ansible/secrets/github-runner.sops.yaml
  # paste the .pem into github_app_private_key, then:
  SOPS_AGE_KEY_FILE=kubernetes/infra/_out/age.agekey \
    sops --encrypt --in-place ansible/secrets/github-runner.sops.yaml
  # confirm it shows ENC[...] before committing — NEVER commit the key in plaintext
  ```

## 3. Create the VMs (OpenTofu)
Prereq: the `qnap-nfs` datastore must have the **import** content type enabled (Datacenter → Storage
→ qnap-nfs → Content). The module downloads the Ubuntu cloud image (qcow2) into `import/` and the VM
disks `import_from` it — PVE rejects importing from an `iso`-typed source, and the file must end in
`.qcow2`/`.raw` (not `.img`). Enable it via the UI, or:
`pvesm set qnap-nfs --content backup,vztmpl,iso,images,import`.
```bash
cd kubernetes/infra/runners
cp terraform.tfvars.example terraform.tfvars   # set pve_api_token + runner_ssh_public_key
tofu init
tofu plan
tofu apply        # downloads the Ubuntu image once (qnap-nfs), creates the new runner VMs
tofu output runner_vms
```

## 4. Configure + register (Ansible)
Prereqs on the control node (WSL): `ansible-galaxy collection install -r ansible/requirements.yml -p
ansible/collections` (adds `community.sops`), plus `sops` + the age key.

**QEMU guest agent — now codified.** VMs are created with the agent disabled (so `tofu apply` doesn't
hang on an agent that isn't installed yet), then `terraform_data.enable_guest_agent`
(`kubernetes/infra/runners/guest-agent.tf`) enables it via the **PVE API** (same api_token, no SSH) and
cold-reboots to attach the virtio-serial channel — no manual step in the normal flow. It's
`on_failure = continue`, so if that API call is skipped (e.g. applied from a shell without `sh`+`curl`),
fall back to the manual step on the VM's Proxmox host: `qm set <vmid> --agent enabled=1 && qm reboot
<vmid>`. (`agent` is in the tofu module's `ignore_changes`, so the enable isn't reverted on re-apply.)
```bash
just ping-runners   # SSH reachability (ansible_user=ubuntu)
just runners        # installs Docker/toolchain + the ported runner contract, registers ephemerally
```

## 5. Verify
- **Registered:** GitHub → `cchifor/platform` → Settings → Actions → Runners → `self-hosted-hv` shows
  `ephem-gha-runner-{1..5}-…` **Idle** (Proxmox-only pool; the legacy Hyper-V runners are retired — §6).
- **Canary:** Actions → **Runner pool health** → Run workflow. Re-run until it lands on a `gha-runner-N`
  host (check the "Runner identity" step); it asserts the `ephem-*` name, `memory.max == 10G`, the
  wrapper, the hook env, **and (2026-07-01) that `~/.docker` is runner-owned + `docker buildx build`
  works** — the buildx self-heal regression gate (cchifor/platform#682).
- **On a VM:**
  ```bash
  ssh ubuntu@192.168.0.14 'systemctl status actions.runner.cchifor-platform.service --no-pager; \
    docker compose version; docker buildx version; uv --version; k6 version; \
    systemctl show -p Environment -p ExecStartPre actions.runner.cchifor-platform.service'
  ```
  Expect the service active, Compose **v2.31.0**, the `ACTIONS_RUNNER_HOOK_JOB_STARTED` env + the
  `runner-reclaim.sh` `ExecStartPre`.
- **Real job:** open a PR on `cchifor/platform`; confirm `e2e`/`build` land on a Proxmox runner with no
  `EACCES … .hatchet-config` checkout flake.
- **Metrics:** the VMs appear as `job=ci-runner-node` scrape targets in Prometheus (CI load on the
  dashboards).
- **Alerts:** `kubernetes/apps/infrastructure/monitoring/ci-runners-rules.yaml` (PrometheusRule
  `ci-runners`, routed to ntfy via Alertmanager): **CIRunnerNodeDown** (VM/exporter down 10m),
  **CIRunnerDiskFilling** + **CIRunnerDiskWillFillSoon** (root fs <12% / projected to fill in 4h),
  **CIRunnerLowMemory** + **CIRunnerSwapPressure** (#620 precursors), **CIRunnerDockerConfigRootOwned**
  (the buildx self-heal regressed). The last is fed by a health beacon that `runner-reclaim.sh` writes to
  node_exporter's textfile collector (`/var/lib/prometheus/node-exporter/runner_health.prom`) — enabled
  for the runner group only via `node_exporter_extra_args` in `group_vars/github_runners.yml`.

## 6. Hyper-V pool — RETIRED (2026-06-16)
The legacy 4-runner Multipass/Hyper-V pool on `BEAST` was drained: its runner services stopped, and the
`ephem-hv-runner-*` registrations removed from GitHub. The `self-hosted-hv` pool is now the **5 Proxmox
runners only** (no `RUNNER_LABEL`/workflow change). The powered-off Multipass VMs on `BEAST` can be purged
to reclaim disk when convenient: `multipass delete --purge hv-runner-{1..4}` (irreversible).

## Day-2
- **Runner version bump:** set `github_runner_version` (role defaults) → `just runners` (re-extracts;
  the agent also auto-updates on connect).
- **App key rotation:** generate a new key on the App, re-encrypt `github-runner.sops.yaml`, `just
  runners`. Revoke the old key.
- **Rebuild a VM (DR):** `tofu apply` (recreates) → `just runners`. Stateless/ephemeral — no data loss.
- **Teardown:** `tofu -chdir=kubernetes/infra/runners destroy`, then remove the offline runners in the
  GitHub UI.

## Troubleshooting
- **`just runners` / a direct `ansible-playbook` does nothing ("skipping: no hosts matched"):** on WSL,
  `/mnt/c` is world-writable, so Ansible silently ignores `ansible.cfg` (and thus the inventory). The
  `just` recipes now set `ANSIBLE_CONFIG` explicitly; invoking `ansible-playbook` by hand needs
  `ANSIBLE_CONFIG="$(pwd)/ansible.cfg"` from the `ansible/` dir.
- **Role fails at `Enable + start qemu-guest-agent` ("A dependency job … failed"):** the VM was created
  with the agent disabled, so Proxmox never attached the guest-agent virtio-serial channel. Enable it +
  reboot (§4: `qm set <vmid> --agent enabled=1 && qm reboot <vmid>`), then re-run.
- **Docker/Compose jobs fail with `sudo: a password is required` (Python jobs still pass):** the
  `runner` user lacks passwordless sudo. Platform workflows `sudo` to install the pinned compose
  binary, start dockerd, free ports, and reclaim root-owned `_work`. The role installs
  `/etc/sudoers.d/90-github-runner` (`runner ALL=(ALL) NOPASSWD:ALL`); to fix a live runner:
  `echo 'runner ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/90-github-runner && sudo chmod 0440 /etc/sudoers.d/90-github-runner` (sudoers is read per-call — no restart needed). Related symptom:
  `open ~/.docker/config.json: permission denied` → `sudo chown -R runner:runner /home/runner/.docker`.
- **Docker/Buildx jobs fail with `stat ~/.docker/buildx/instances: permission denied` or `EACCES …
  mkdir ~/.docker/buildx/certs` (Python jobs still pass):** a job ran `docker buildx` under `sudo` and
  left `~/.docker/buildx` **root-owned**; because the runner *VM* persists across ephemeral jobs, every
  later docker-build job on it then fails. `runner-reclaim.sh` now **self-heals** this — it runs as root
  before every job (`20-reclaim.conf` `ExecStartPre=+`) and does `chown -R runner:runner
  /home/runner/.docker` (plus `docker buildx rm --all-inactive` + `docker volume prune -af` to reap the
  named `buildx_buildkit_builder-*_state` volumes that `prune -f` skips and that otherwise fill the
  disk). To fix a live runner immediately: `sudo chown -R runner:runner /home/runner/.docker; sudo docker
  buildx rm --all-inactive --force; sudo docker volume prune -af`. Alert **CIRunnerDockerConfigRootOwned**
  fires if the self-heal ever fails to restore ownership.
- **Runner not appearing:** check `journalctl -u actions.runner.cchifor-platform -n 100` on the VM —
  usually a bad App ID / installation ID, a key that isn't for this App, or the App missing
  `Administration: Read & write`. The wrapper logs `[ephemeral-runner] ERROR: …` on token failures.
- **`tofu apply` hangs / VM unreachable:** the module sets `agent { enabled = false }` precisely so
  apply doesn't wait on a guest agent the minimal cloud image lacks (the role installs it later). The
  static IP comes from cloud-init, so if a VM is unreachable after apply, check cloud-init on the
  console (`qm terminal <vmid>` on the host) and that `runner_ssh_public_key` was set before apply.
- **`just runners` can't reach a host:** the VM must exist (`tofu apply`) and the `runner_ssh_public_key`
  must match `~/.ssh/id_ed25519`; `just ping-runners` to isolate SSH vs config issues.
- **Canary fails `memory.max`:** the `20-reclaim`/unit drop-ins didn't apply — `systemctl daemon-reload`
  + re-run `just runners`. `MemoryMax` must be `10G` (10737418240 bytes). NB: `MemoryMax=10G` is the
  service **cgroup** cap, unrelated to the balloon floor below — don't conflate them.
- **Jobs OOM / "self-hosted runner lost communication" / exit 137 (cchifor/platform#620):** the
  Proxmox host is under RAM pressure and `pvestatd` ballooned the guest down toward its floor, starving
  the running job. The VM resource (`kubernetes/infra/runners`) pins the **balloon floor to 12 GiB**
  (`runner_memory_floating_mib = 12288`, was the bpg default 1 GiB) so a running guest can't drop below
  12 GiB, and the `github_runner` role adds an **8 GiB guest swapfile** (`swappiness=10`) so a peak
  spills to the VM's own disk instead of OOM-killing. Verify on a VM: `free -h` shows ~12 GiB+ and an
  8 GiB swap; on the host `qm config <vmid> | grep -E 'memory|balloon'` shows `memory: 24576` +
  `balloon: 12288`. To re-assert: `tofu apply` (floor) + `just runners` (swap).
