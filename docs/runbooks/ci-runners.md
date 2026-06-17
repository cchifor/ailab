# Runbook — self-hosted GitHub Actions runners (Proxmox)

3 ephemeral runner VMs on the Proxmox lab, joining the `cchifor/platform` **`self-hosted-hv`** pool.
See ADR 0013. IaC: `kubernetes/infra/runners/` (VMs) + `ansible/roles/github_runner/` (config).

| | |
|---|---|
| VMs | `gha-runner-1/2/3` — vmid 4101-4103, one per Proxmox host, .47/.48/.49, 8 vCPU / 24 GiB / 120 GiB |
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
tofu apply        # downloads the Ubuntu image once (qnap-nfs), creates the 3 VMs
tofu output runner_vms
```

## 4. Configure + register (Ansible)
Prereqs on the control node (WSL): `ansible-galaxy collection install -r ansible/requirements.yml -p
ansible/collections` (adds `community.sops`), plus `sops` + the age key.
```bash
just ping-runners   # SSH reachability (ansible_user=ubuntu)
just runners        # installs Docker/toolchain + the ported runner contract, registers ephemerally
```

## 5. Verify
- **Registered:** GitHub → `cchifor/platform` → Settings → Actions → Runners → `self-hosted-hv` shows
  `ephem-gha-runner-{1,2,3}-…` **Idle** (alongside the 4 Hyper-V runners during transition).
- **Canary:** Actions → **Runner pool health** → Run workflow. Re-run until it lands on a `gha-runner-N`
  host (check the "Runner identity" step); it asserts the `ephem-*` name, `memory.max == 10G`, the
  wrapper, and the hook env.
- **On a VM:**
  ```bash
  ssh ubuntu@192.168.0.47 'systemctl status actions.runner.cchifor-platform.service --no-pager; \
    docker compose version; docker buildx version; uv --version; k6 version; \
    systemctl show -p Environment -p ExecStartPre actions.runner.cchifor-platform.service'
  ```
  Expect the service active, Compose **v2.31.0**, the `ACTIONS_RUNNER_HOOK_JOB_STARTED` env + the
  `runner-reclaim.sh` `ExecStartPre`.
- **Real job:** open a PR on `cchifor/platform`; confirm `e2e`/`build` land on a Proxmox runner with no
  `EACCES … .hatchet-config` checkout flake.
- **Metrics:** the VMs appear as `job=ci-runner-node` scrape targets in Prometheus (CI load on the
  dashboards).

## 6. Decommission the Hyper-V pool (after the Proxmox 3 are proven)
On `BEAST` (elevated shell): stop each runner service so it doesn't re-register, let in-flight jobs
drain, then delete the Multipass VMs. In GitHub → Settings → Actions → Runners, remove the now-offline
`ephem-hv-runner-*` entries. No `RUNNER_LABEL`/workflow change — the pool just shrinks to the Proxmox 3.

## Day-2
- **Runner version bump:** set `github_runner_version` (role defaults) → `just runners` (re-extracts;
  the agent also auto-updates on connect).
- **App key rotation:** generate a new key on the App, re-encrypt `github-runner.sops.yaml`, `just
  runners`. Revoke the old key.
- **Rebuild a VM (DR):** `tofu apply` (recreates) → `just runners`. Stateless/ephemeral — no data loss.
- **Teardown:** `tofu -chdir=kubernetes/infra/runners destroy`, then remove the offline runners in the
  GitHub UI.

## Troubleshooting
- **Docker/Compose jobs fail with `sudo: a password is required` (Python jobs still pass):** the
  `runner` user lacks passwordless sudo. Platform workflows `sudo` to install the pinned compose
  binary, start dockerd, free ports, and reclaim root-owned `_work`. The role installs
  `/etc/sudoers.d/90-github-runner` (`runner ALL=(ALL) NOPASSWD:ALL`); to fix a live runner:
  `echo 'runner ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/90-github-runner && sudo chmod 0440 /etc/sudoers.d/90-github-runner` (sudoers is read per-call — no restart needed). Related symptom:
  `open ~/.docker/config.json: permission denied` → `sudo chown -R runner:runner /home/runner/.docker`.
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
  + re-run `just runners`. `MemoryMax` must be `10G` (10737418240 bytes).
