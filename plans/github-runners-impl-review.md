# Implementation review — github-runners — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- **site.yml wiring breaks unscoped `just net`**: the new second play targets `github_runners` hosts, but gather_facts runs unconditionally and will fail if VMs don't exist yet, interrupting the full playbook flow.
- **Plan mismatch on agent{enabled}**: the plan specifies `agent { enabled = true }`, but implementation has `enabled = false` with correct rationale (cloud image lacks agent). Plan needs updating to match the implementation choice.
- **SOPS github-runner.sops.yaml**: the `.sops.yaml.example` is correct and acts as a template; the real file is encrypted by sops before commit. No blocker.
- **JWT exp window tight but correct**: `exp = now + 540` (9 min) is within GitHub's 10-min JWT window and the `iat - 60` clock-skew grace is prudent.
- **Docker group membership acceptable for ephemeral CI**: high-privilege, but ephemeral single-job execution limits exposure; same model as the existing Hyper-V pool.

## Findings

### site.yml second play gather_facts blocks full playbook before VMs exist

**Location:** ansible/site.yml:57

**Severity:** important

<!-- codex: The second play targets github_runners with gather_facts: true unconditionally. If the VMs don't exist yet (before tofu apply), `just net` will fail trying to connect to .47-.49. Solution: gate the entire play or move the runners play to its own playbook so the host-config flow is unaffected. -->

### Plan document specifies agent { enabled = true } but implementation has false

**Location:** plans/2026-06-16-github-runners-plan.md, kubernetes/infra/runners/main.tf:35-37

**Severity:** nit

<!-- codex: The plan says `agent { enabled = true }` as though deliberate, but the implementation correctly has `enabled = false` (cloud image doesn't ship qemu-guest-agent). Update the plan to reflect `enabled = false`. Doc mismatch, not a defect. -->

### content_type = "iso" for .img file is unconventional but correct per Proxmox

**Location:** kubernetes/infra/runners/main.tf:14

**Severity:** nit

<!-- codex: Using `content_type = "iso"` for a .img/.qcow2 cloud image is unusual but correct — Proxmox accepts disk images under the iso content type. The existing comment is adequate. -->

### Ephemeral runner name regex matches the canary exactly

**Location:** ansible/roles/github_runner/templates/ephemeral-runner.sh.j2

**Severity:** nit (verified correct)

<!-- codex: `ephem-$(hostname)-${ts}-${suffix}` (8-char hex suffix) matches the canary regex `^ephem-[a-z0-9-]+-[0-9]+-[0-9a-f]{8}$`. Jinja-default/bash-expansion rendering is correct — no quoting bugs. -->

### MemoryMax=10G cap is present and correct

**Location:** ansible/roles/github_runner/templates/actions.runner.cchifor-platform.service.j2

**Severity:** nit (verified correct)

<!-- codex: MemoryMax renders to "10G" (defaults/main.yml), matching the canary's cgroup assertion (memory.max == 10737418240). -->

### github_app_private_key encryption via SOPS is correctly scoped

**Location:** .sops.yaml

**Severity:** nit (verified correct)

<!-- codex: The ansible/secrets path-specific rule (encrypted_regex github_app_private_key) is ordered before the generic data|stringData rule (first match wins). The .example won't match the path_regex. Sound. -->

### Remaining verified-correct items

**Severity:** nit (verified correct)

<!-- codex: Compose plugin path /usr/local/lib/docker/cli-plugins (standard); NodeSource suites: nodistro (correct for Node 20); IP .47-.49 + vmid 4101-3 collision-free; ci-runners-node.yaml mirrors proxmox-node.yaml; runner user in docker group is an acceptable ephemeral-CI tradeoff; become from ansible_user=ubuntu works (cloud image sudo); `set -euo pipefail` + `|| true` on config.sh remove is the correct idiom. No action. -->

## Diff stat

 .sops.yaml                                         |   7 +
 ansible/group_vars/github_runners.yml              |  11 +
 ansible/requirements.yml                           |   2 +
 ansible/roles/github_runner/defaults/main.yml      |  47 ++++
 ansible/roles/github_runner/handlers/main.yml      |  11 +
 ansible/roles/github_runner/tasks/main.yml         | 295 +++++++++++++++++++++
 .../templates/10-job-started-hook.conf.j2          |   5 +
 .../github_runner/templates/20-reclaim.conf.j2     |   7 +
 .../actions.runner.cchifor-platform.service.j2     |  33 +++
 .../roles/github_runner/templates/daemon.json.j2   |   5 +
 .../github_runner/templates/ephemeral-runner.sh.j2 |  65 +++++
 .../github_runner/templates/job-started.sh.j2      |  27 ++
 .../github_runner/templates/runner-reclaim.sh.j2   |  24 ++
 ansible/secrets/github-runner.sops.yaml.example    |  14 +
 ansible/site.yml                                   |  27 ++
 docs/decisions/0013-ci-self-hosted-runners.md      |  69 +++++
 docs/runbooks/ci-runners.md                        |  98 +++++++
 inventory/hosts.yml                                |  17 ++
 justfile                                           |  11 +
 .../infrastructure/monitoring/ci-runners-node.yaml |  50 ++++
 kubernetes/infra/runners/backend.tf                |   6 +
 kubernetes/infra/runners/main.tf                   |  92 +++++++
 kubernetes/infra/runners/outputs.tf                |  10 +
 kubernetes/infra/runners/providers.tf              |  16 ++
 kubernetes/infra/runners/terraform.tfvars.example  |  15 +
 kubernetes/infra/runners/variables.tf              | 120 +++++++++
 kubernetes/infra/runners/versions.tf               |   9 +
 28 files changed, 1094 insertions(+)
