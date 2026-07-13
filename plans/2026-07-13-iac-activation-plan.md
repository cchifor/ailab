# AgentForge v2 — fully-IaC activation (no manual/physical ops)

## Context
The entire v2 stack is built but DORMANT on ailab `feat/p2-unlock` (96 commits ahead of `main`; Flux
reconciles `main`, so nothing is live). Live cluster state (verified 2026-07-13): 3 CPs Ready; NO
agent-nodes joined; NO openbao/external-secrets/keda/kro pods; only the P1 `agentforge` namespace. The
agent-nodes tofu has NEVER been applied (no state).

A prior status update wrongly called activation "physical ops." This is a 100%-IaC lab. Activation must be
codified: `just` recipes + `tofu` + Flux + a GitOps merge — no ad-hoc SSH/console commands. Three items are
genuinely not-yet-IaC and are the BUILD scope; the rest is reframing + an execution runbook. User decisions
(2026-07-13): **build + execute live** (checkpoint before each irreversible step); OpenBao = **auto-init+unseal
Job** storing keys in an in-cluster Secret.

## Approach

### BUILD (codified gaps) — all on `feat/p2-unlock`, codex Phase B before executing

**A. Proxmox host nested-virt (`kvm_amd nested=1`) — `scripts/enable-nested-virt.py` + `just nested-virt`.**
Kata needs `/dev/kvm` in the worker VM → the Proxmox HOSTS need nested AMD-V. Today a runbook step; codify
it. A paramiko script (mirrors `scripts/node-ssh.py`, reads `.env` `NODE_ROOT_PASSWORD`/`PVE_NODES`) that,
per host .2/.3/.4, IDEMPOTENTLY: writes `/etc/modprobe.d/kvm.conf` = `options kvm_amd nested=1`, then
`modprobe -r kvm_amd && modprobe kvm_amd` IF no VM currently uses it (else defer to next reboot — never
force-reboot a host), and asserts `/sys/module/kvm_amd/parameters/nested` ∈ {Y,1}. Re-runnable, reports
per-host state. `just nested-virt` recipe. (AMD Strix Halo → `kvm_amd`; guard if `kvm_intel`.)

**B. OpenBao auto-init/unseal — `security/openbao/{unseal-rbac,unseal-job,unsealer}.yaml`.**
Replaces the HelmRelease header's manual `bao operator init`/`unseal`. Design:
- **SA `openbao-unsealer`** (openbao ns) + **Role** (namespaced): `get/list` pods; `get/create/update` the
  ONE Secret `openbao-keys` (pin resourceName where the verb allows). NO cluster RBAC.
- **Init Job `openbao-init`** (Flux-applied into the openbao kustomization; Helm `post-install`-style via a
  Flux `dependsOn`/health gate is not needed — the Job self-waits). IDEMPOTENT script against
  `http://openbao-0.openbao-internal.openbao.svc:8200`:
  1. Poll `GET /v1/sys/seal-status` until reachable.
  2. If `.initialized == false` → `PUT /v1/sys/init {secret_shares:1, secret_threshold:1}` → capture
     `unseal_keys_b64[0]` + `root_token` → **create Secret `openbao-keys`** via the k8s API (SA token +
     in-cluster CA). If `openbao-keys` ALREADY exists but bao is uninitialized (disaster: PV lost) → FAIL
     LOUDLY (don't re-init over a stale key). If initialized AND the Secret is missing → FAIL (can't unseal).
  3. If `.sealed == true` → read `openbao-keys` → `PUT /v1/sys/unseal {key}`.
- **Unsealer `openbao-unsealer` Deployment** (1 replica, tiny): loop every 15s — `GET seal-status`; if
  `sealed` and initialized → unseal from `openbao-keys`. Covers pod restarts without re-init. (A Deployment,
  not a CronJob: sub-minute restart coverage; requests/limits tiny; restricted securityContext.)
- **Image**: a FIRST-PARTY `openbao-bootstrap` (alpine + curl + jq, digest-pinned), built by the image-CI
  (C) — no `bao`/kubectl/runtime-apk, everything via the OpenBao HTTP API + k8s REST API. Rationale: no
  third-party image holding Secret-write RBAC; no runtime egress; reproducible. (Registry is independent of
  OpenBao, so no circular bootstrap.) Until built, the Job image is gated like the other placeholder digests.
- Wire all three into `security/openbao/kustomization.yaml`. Security note: keys in `openbao-keys` (etcd,
  encrypted-at-rest if the cluster has it) — accepted single-node homelab posture; the unseal key never
  leaves the ns; documented.

**C. Image-build Gitea CI + digest-pin — `.gitea/workflows/images.yml` (agentforge, agentforge-platform).**
Gitea is the master forge; the EXISTING act_runner VMs run CI (the CI ScaledJob is a future replacement, not
a prereq). Build + push to `registry.chifor.me` (anonymous-pull, no imagePullSecret):
- agentforge: `worker` (deploy/Dockerfile) + `openbao-bootstrap` (a tiny deploy/bootstrap.Dockerfile) +
  `broker` + `reaper`/CI-runner as applicable. platform: `agentforge-platform` (Dockerfile).
- Tag `:<git-sha>` + emit the pushed **digest** as a job output/artifact.
- **Digest-pin**: `scripts/pin-image-digests.py` reads the pushed `repo@sha256:…` for each and rewrites the
  ailab manifests' placeholder digests (worker/broker/reaper/dispatcher/CI/bootstrap) in one commit. A
  `just pin-digests` recipe. (Runs on demand at the un-gate step, not every push.)

**D. Reframe + orchestration — `docs/runbooks/agentforge-activation.md` + `just activate-*` + header fixes.**
- One runbook documenting the fully-IaC sequence (below) + rollback. Update the OpenBao HelmRelease header
  and `docs/runbooks/agent-nodes.md` to point at B/A (drop "manual"/"operator step" language).
- `just` recipes framing the ordered milestones so each is one command (they wrap `tofu`, `flux reconcile`,
  `kubectl wait`, the scripts) — NOT new logic, just the codified sequence.

### EXECUTE (live, checkpointed — STOP for confirmation before each ⛔ irreversible gate)
Ordered so each milestone is verified before the next; every step is a committed/coded action.
1. **⛔ Merge the operators+security layer to `main`** (or a scoped subset first) → Flux deploys
   openbao/external-secrets/keda/kro (dormant agentforge workloads stay gated on placeholder digests).
   Checkpoint: this is the first irreversible GitOps step.
2. **OpenBao**: auto-init/unseal (B) runs → `GET seal-status` shows initialized+unsealed; `openbao-keys`
   present. Verify ESO can auth. (Depends on the bootstrap image from C — build it first, step 0.)
3. **⛔ Kata pool**: `just nested-virt` (A) → `just agent-nodes-apply` (tofu creates .14–.16 with the
   Kata image) → nodes Ready; `kubectl get runtimeclass kata gvisor`; a probe pod on the pool sees `/dev/kvm`.
   Checkpoint: creates VMs + (re)loads a host kernel module.
4. **Images**: `just pin-digests` after CI pushes → the worker/broker/reaper/dispatcher digests are real.
5. **⛔ Un-gate workloads**: commit un-listing the gated manifests (worker/broker/reaper/dispatcher/CI
   ScaledJob) + the OpenBao provisioner seeds paths → ESO syncs per-tenant Secrets → the tenant-zero worker
   scales 0→N on `forge_pending`. Checkpoint: real agent workloads begin running.
6. **Sandbox boundary tests** (ADR 0018 canary: no cred mounts, `--network none`, Kata guest kernel, egress
   matrix) all green → **⛔ flip `privilege_hardening: v1.1`** → unlock non-playground repos.

## Critical files
- NEW `scripts/enable-nested-virt.py`, `scripts/pin-image-digests.py`; `justfile` (+recipes).
- NEW `kubernetes/apps/infrastructure/security/openbao/{unseal-rbac,unseal-job,unsealer}.yaml` +
  `kustomization.yaml`; edit `helmrelease.yaml` header.
- NEW `.gitea/workflows/images.yml` in agentforge + agentforge-platform; NEW agentforge
  `deploy/bootstrap.Dockerfile`.
- NEW `docs/runbooks/agentforge-activation.md`; edit `docs/runbooks/agent-nodes.md`.

## Verification
- A: re-run is a no-op; `/sys/module/kvm_amd/parameters/nested` = Y on .2/.3/.4; no host rebooted with running VMs.
- B: `kubectl -n openbao get secret openbao-keys` exists; `seal-status` initialized+unsealed after a pod
  delete (unsealer re-seals-recovery); a second Job run is a no-op (no re-init).
- C: CI pushes `registry.chifor.me/agentforge/worker@sha256:…`; `just pin-digests` yields a clean manifest diff.
- D+EXEC: `kubectl kustomize` builds everywhere; Flux Kustomizations Ready; the ADR-0018 boundary canary
  passes; only after that does v1.1 flip.

## Risks
- **Merging 96 commits to main** is the big-bang. Mitigate: verify every `kubectl kustomize` + Flux dry-run
  first; the agentforge workloads are DIGEST-gated so they can't run until step 4/5 even once merged.
- **OpenBao key custody**: single unseal share in an in-cluster Secret — acceptable homelab posture (user
  chose it); documented; rotate/reshard is a follow-up.
- **Kata host module reload**: never force-reboot a host with running VMs; defer to a maintenance reboot if in use.
- **Big-bang blast radius**: prefer merging the operators/security subset FIRST (validate OpenBao auto-unseal
  live), THEN the agentforge layer — reduces the irreversible step size. (Open question for codex: one merge
  vs. staged.)

<!-- codex-review-status: pending -->
