# 3 self-hosted GitHub Actions runners on the Proxmox lab

## Context

`cchifor/platform` (a private multi-tenant SaaS) runs its CI on a self-hosted **ephemeral runner pool** today: 4 VMs (`hv-runner-1..4`), Multipass-managed **Ubuntu 24.04** on the Windows **Hyper-V** host `BEAST`, label **`self-hosted-hv`**, selected via the repo variable `RUNNER_LABEL`. The workload is Docker-heavy — `docker compose` (pinned **v2.31.0**) multi-service stacks, Buildx image builds pushed to ghcr.io, Playwright e2e (in a containerized `e2e-runner` image), Python via `uv`, and `k6` load tests.

That pool is tied to a Windows host (UAC + Multipass), and its docs record real pain: disks hitting 100%, constrained dynamic memory, and "red on unrelated PR" flakes from root-owned `_work` leftovers. The goal: **stand up 3 equivalent ephemeral runners on the codified Proxmox AI lab** (192.168.0.2/.3/.4 — ~96 threads / 187 GiB each, large headroom), bring them into the **same `self-hosted-hv` pool additively** (no workflow change), validate with the existing `runner-health.yml` canary, then **retire the Hyper-V VMs**.

The platform repo already version-controls the runner contract under `infra/runner/` and `.github/runner-hooks/`, and a nightly `runner-health.yml` canary asserts it. **This plan replicates that proven contract on Proxmox QEMU VMs** — the only intentional change is upgrading registration auth from a static PAT to a **GitHub App** (the user's choice).

Decisions locked with the user: **full QEMU VMs** (not LXC), **additive into `self-hosted-hv` then retire Hyper-V**, **GitHub App auth**, **Docker required**. `cchifor` is a **User** account, so runners are necessarily **repo-scoped** to `cchifor/platform`.

## Approach

Provision and configure entirely from the **`ailab` IaC repo** (the platform repo is unchanged; `RUNNER_LABEL` stays `self-hosted-hv`):

- **OpenTofu** creates 3 Ubuntu VMs (one per Proxmox host).
- **Ansible role `github_runner`** installs the toolchain + the GitHub Actions agent + the **ported platform runner contract** (ephemeral wrapper, systemd unit + drop-ins, job-started hook, between-jobs reclaim, `daemon.json`), with App-based registration.
- A SOPS-encrypted GitHub App private key is the only new secret.

### VM spec (one per host, `for_each`)

| host | name | vmid | IP | size |
|---|---|---|---|---|
| ai-node1 (.2) | gha-runner-1 | 4101 | 192.168.0.47 | 8 vCPU / 24 GiB / 120 GiB |
| ai-node2 (.3) | gha-runner-2 | 4102 | 192.168.0.48 | 8 vCPU / 24 GiB / 120 GiB |
| ai-node3 (.4) | gha-runner-3 | 4103 | 192.168.0.49 | 8 vCPU / 24 GiB / 120 GiB |

IPs are inside the reserved static block `.2–.50` and outside the DHCP pool (avoids the prior `.51–.53` lease conflict). vmids `4101–4103` don't collide (Talos `4001–4003`, AI LXCs `5001–5003`). 24 GiB (vs the cramped Hyper-V VMs) leaves ~14 GiB for Compose containers after the runner cgroup cap; 120 GiB disk + the reclaim/`daemon.json` GC fix the disk-fill problem.

### Registration & ephemerality (faithful to the canary contract)

Keep the exact contract `runner-health.yml` validates — **only swap PAT → GitHub App**:
- systemd `actions.runner.cchifor-platform.service` (`User=runner`, `Restart=always`) → `ExecStart=/home/runner/ephemeral-runner.sh`.
- Each cycle: mint a **GitHub App installation token** (JWT signed by the App key → `POST /app/installations/{id}/access_tokens`) → `POST /repos/cchifor/platform/actions/runners/registration-token` → `./config.sh --url https://github.com/cchifor/platform --token <reg> --name "ephem-$(hostname)-<epoch>-<8hex>" --labels self-hosted-hv --ephemeral --unattended` → `exec ./run.sh` (one job, then exit; systemd restarts).
- Name format unchanged → matches the canary regex `^ephem-[a-z0-9-]+-[0-9]+-[0-9a-f]{8}$` for any hostname. `MemoryMax=10G` drop-in unchanged → matches the canary's cgroup assertion. **No platform-repo edits.** (Optional future hardening: JIT `generate-jitconfig` to drop the registration-token round-trip — keep the same `ephem-*` name so the canary is unaffected.)

### Host toolchain (ansible role installs)

Docker CE + **docker-compose-plugin pinned 2.31.0** + docker-buildx-plugin (apt pin, idempotent) · `uv` · `k6` · Node LTS · git/jq/curl/ca-certificates · the GitHub Actions runner agent (pinned version) under `/home/runner/actions-runner` as user `runner`. Playwright is **containerized** in platform's `e2e-runner` image, so no host browser deps. Plus `/etc/docker/daemon.json` (log rotation + build-cache GC) and the between-jobs reclaim + job-started hook **ported verbatim** from `cchifor/platform:infra/runner/` (kept in sync; the platform canary catches drift).

## Critical files

**New — OpenTofu module `kubernetes/infra/runners/`** (mirror `kubernetes/infra/vms.tf`, reuse `bpg/proxmox ~> 0.109`, local backend, Proxmox **API token** auth):
- `versions.tf`, `providers.tf`, `backend.tf`, `variables.tf`, `outputs.tf`, `main.tf`.
- `main.tf`: `proxmox_virtual_environment_download_file` for the Ubuntu **24.04 noble** cloud image (`content_type = "import"`) + `proxmox_virtual_environment_vm` with `for_each = var.runner_nodes`, `agent { enabled = true }`, `cpu { cores = 8 }`, `memory { dedicated = 24576 }`, `disk { import_from = <cloudimg>, size = 120, … }`, and:
  ```hcl
  initialization {
    ip_config { ipv4 { address = "${each.value.ip}/24"  gateway = "192.168.0.1" } }
    user_account { username = "ubuntu"  keys = [var.runner_ssh_public_key] }   # ansible reaches the guest
  }
  tags = ["vm", "ci", "github-runner", "ailab"]
  ```

**New — Ansible role `ansible/roles/github_runner/`** (same skeleton as `ansible/roles/cpu_performance/` and `ansible/roles/node_exporter/`):
- `defaults/main.yml`: `github_runner_enabled: false`; `github_runner_repo: cchifor/platform`; `github_runner_label: self-hosted-hv`; pinned `github_runner_version`, `docker_compose_version: "2.31.0"`; `github_runner_memory_max: 10G`; `github_runner_cpu_quota: 760%`; toolchain versions.
- `tasks/main.yml` (every task idempotent, gated `when: github_runner_enabled | bool`): install Docker stack (apt-pinned compose + buildx) · uv · k6 · Node · create `runner` user · unarchive the pinned runner agent (guard with a version marker file) · template `ephemeral-runner.sh` (App-auth) + the base systemd unit + the two drop-ins (`10-job-started-hook.conf`, `20-reclaim.conf`) + `hooks/job-started.sh` + `/usr/local/bin/runner-reclaim.sh` + `/etc/docker/daemon.json` · install the App `.pem` to `/etc/runner/app.pem` (0400, owner `runner`, `no_log`) · enable the service.
- `handlers/main.yml`: `Restart github-runner`. `templates/`: `ephemeral-runner.sh.j2`, `actions.runner.cchifor-platform.service.j2`, the two `*.conf.j2`, `job-started.sh.j2`, `runner-reclaim.sh.j2`, `daemon.json.j2` — content ported from platform `infra/runner/`.

**Reuse (don't reinvent):**
- `kubernetes/infra/vms.tf` — the VM resource pattern to mirror.
- `ansible/roles/node_exporter/` — **apply this existing role to the runner VMs too** (the lab scrapes everything); add a `proxmox-node`-style scrape Service/Endpoints/ServiceMonitor for `.47/.48/.49:9100`.
- `cchifor/platform:infra/runner/{provision.sh,reference/ephemeral-runner.sh,reference/actions.runner.cchifor-platform.service}` + `.github/runner-hooks/job-started.sh` — the source-of-truth contract to port.

**Edits — wiring:**
- `inventory/hosts.yml`: add group `github_runners` (hosts `gha-runner-1/2/3` at `.47/.48/.49`) with `ansible_user: ubuntu`, `ansible_become: true`.
- `ansible/site.yml`: add a **second play** targeting `hosts: github_runners` with roles `node_exporter` + `github_runner`, `tags: [runners]`, `when: github_runner_enabled | default(false)`.
- `ansible/group_vars/runners.yml`: `github_runner_enabled: true`, `github_runner_app_id`, `github_runner_app_installation_id` (non-secret).
- `ansible/secrets/github-runner.sops.yaml`: SOPS-encrypted `github_app_private_key` (add a path-specific rule in `.sops.yaml` for `ansible/secrets/.*\.sops\.ya?ml$`). `just runners` decrypts it into `--extra-vars` (no plaintext on disk).
- `justfile`: `runners` (`ansible-playbook site.yml --tags runners` with the SOPS-decrypt extra-var) + `ping-runners`.

**Docs:** `docs/decisions/0013-ci-self-hosted-runners.md` (ADR — VM-over-LXC + over-ARC rationale: circularity, Talos doctrine, Docker isolation; records the ported contract + PAT→App swap) and `docs/runbooks/ci-runners.md` (GitHub App creation + scopes `Administration:R/W` + `Metadata:read`; `tofu apply`; `just runners`; verify; **HV decommission**; key rotation; teardown).

## Migration (additive, then retire Hyper-V)

1. Create the GitHub App, install on `cchifor/platform`, SOPS-encrypt the `.pem`.
2. `tofu -chdir=kubernetes/infra/runners apply` → 3 VMs.
3. `just runners` → role configures + registers them into the **`self-hosted-hv`** pool. GitHub Settings → Actions → Runners now shows 7 (4 HV + 3 PVE).
4. Manually run `runner-health.yml` against the pool; confirm green on the new hosts.
5. Let real CI route across all 7; once the Proxmox 3 prove stable, **decommission Hyper-V**: stop their service, drain in-flight jobs, delete the offline runners (API/UI), delete the Multipass VMs on `BEAST`.

## Verification (end-to-end)

- **Registered & ephemeral**: `Settings → Actions → Runners` lists `ephem-gha-runner-{1,2,3}-…` Idle with labels `self-hosted, Linux, X64, self-hosted-hv`; after a job, that runner disappears and a fresh `ephem-…` reappears within seconds (systemd cycle).
- **Canary green**: manually dispatch `runner-health.yml` (`runs-on: ${{ vars.RUNNER_LABEL }}`) and land it on a Proxmox runner — asserts the `ephem-*` name, `memory.max == 10G`, the wrapper, and the job-started hook env.
- **Real workload**: open a PR on `cchifor/platform`; confirm `e2e`/`build`/`contract` jobs run on a Proxmox runner (`hostname` = `gha-runner-N`), Compose v2.31.0 + Buildx work, ghcr push succeeds, and no `EACCES … .hatchet-config` checkout flake (hook + reclaim working).
- **Toolchain**: on a VM, `docker compose version` = 2.31.0, `docker buildx version`, `uv --version`, `k6 version`, `node --version` all succeed.
- **Observability**: the runner VMs appear as `node_exporter` scrape targets (CPU/mem/disk during builds on the dashboards).
- **Idempotency**: re-run `just runners` → `changed=0` except a deliberate version bump.

## Codex cross-validation (per request)

Two read-only codex passes were run during planning. Codex **confirmed**: VMs (not LXC/ARC) are right for this Docker-heavy, infra-adjacent, isolation-sensitive workload; the bpg VM pattern, IP/vmid safety, and PAT→App flow are sound. **Accepted** its refinements (24 GiB RAM, `ansible_user: ubuntu`+become, qemu-guest-agent, apt-pinned compose/buildx, node_exporter reuse, scoped SOPS, teardown/DR). **Rejected** its "must switch to JIT + rewrite the canary" point — verified the canary regex is host-agnostic, so the faithful `--ephemeral`+`ephem-*` replication needs **zero platform-repo changes** (JIT noted as optional, name-format-preserving).

Plan-mode blocks commits, so the **formal** codex-reviewed-planning loop runs at implementation: commit this plan on a feature branch → codex **Phase A** (plan review) → implement → codex **Phase B** (impl review of the diff). The contract is additionally self-verified by platform's nightly `runner-health.yml`.

<!-- codex-review-status: finalized -->
<!-- Codex Phase A done at planning time (2 read-only passes); see the "Codex cross-validation" section. Phase B (impl review) runs on the diff below. -->
