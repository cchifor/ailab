# AgentForge v2 — fully-IaC activation (no manual/physical ops)

## Context
The entire v2 stack is built but DORMANT on ailab `feat/p2-unlock` (96 commits ahead of `main`; Flux
reconciles `main`, so nothing is live). Live state (verified 2026-07-13): 3 CPs Ready; NO agent-nodes; NO
openbao/external-secrets/keda/kro pods; only the P1 `agentforge` namespace. agent-nodes tofu never applied.
**Nested virt is ALREADY active (`nested=1`) on all 3 hosts** (Ansible `pve_base` owns it) — so Kata has NO
host-reboot gate. A prior status wrongly called activation "physical ops"; it is 100% IaC (`just`/`tofu`/Flux/
GitOps). User decisions: **build + execute live** (checkpoint each irreversible step); OpenBao =
**auto-init+unseal Job**, unseal key in an in-cluster Secret.

Codex Phase A raised the material gaps this revision now folds in: **TLS mismatch** (admission pins
`https://openbao.openbao.svc:8200` but the chart default listener is HTTP), **root-token custody** (must not
sit in the unsealer-readable Secret), **split RBAC identities**, **OpenBao provisioning is in-scope** (KV
mount + k8s auth backend + policies/roles, not just init/unseal), **staged merge** (not one 96-commit
big-bang), **bootstrap image ordering** (built+pinned before the init Job merges), and several idempotency/
fail-closed hardenings.

## Approach

### BUILD (codified gaps) — on `feat/p2-unlock`, TDD where code, codex Phase B before executing

**0. Bootstrap image (`openbao-bootstrap`) — FIRST, built+digest-pinned before anything else.**
First-party image: `FROM ghcr.io/openbao/openbao:<pinned>` (ships the official `bao` CLI — avoids hand-rolled
HTTP/JSON mistakes) + `COPY --from=bitnami/kubectl` (or registry.k8s.io/kubectl) the `kubectl` binary + a
minimal shell. Built by the image-CI (C) on the EXISTING act_runner VMs, pushed to `registry.chifor.me/
agentforge/openbao-bootstrap@sha256:…`. Used by the init/unseal/provision Jobs (B). No runtime `apk`, no
third-party image holding Secret-write RBAC. (Registry is independent of OpenBao → no circular bootstrap.)

**A. Nested-virt — VERIFY only (already Ansible-owned + already active). No new script.**
`pve_base` (`ansible/roles/pve_base/tasks/main.yml`) writes `/etc/modprobe.d/kvm-nested.conf` (source of
truth). Do NOT add a second paramiko writer (would duplicate state/path — codex). Add `just nested-virt-verify`:
a READ-ONLY probe (node-ssh.py) asserting `/sys/module/kvm_amd/parameters/nested` ∈ {Y, y, 1} on .2/.3/.4
(accept all kernel truthy reprs, not exactly `Y` — codex r2, else a ready host false-blocks) — a HARD
activation gate (fail → Kata blocked). It passes today. (If a future host reports N, the fix is re-run the
Ansible role + a maintenance reboot — documented, not this session's path.)

**B. OpenBao TLS + auto-init/unseal + provisioning — `security/openbao/{tls,unseal-rbac,unseal-job,
unsealer,provision-rbac,provision-job,provisioner-deploy}.yaml` + HelmRelease values.**
- **TLS (fix the https:// pin mismatch):** cert-manager `ailab-ca` ClusterIssuer (present, Ready 24d) issues
  a cert whose SANs cover EVERY DNS name reached while SEALED, not just the Service (codex r2): the
  bootstrap/init target `openbao-0.openbao-internal.openbao.svc[.cluster.local]` (headless per-pod DNS —
  publishes not-ready pods) AND `openbao.openbao.svc[.cluster.local]` (the ESO/admission-pinned name).
  Mount it; chart listener HCL → `tls_cert_file`/`tls_key_file`. **`caProvider` (the ailab-ca CA) must be
  added to EVERY operator SecretStore AND the tenant renderer's rendered SecretStore** (not only the new
  bootstrap Jobs) — an explicit sweep, else those SecretStores fail TLS verify. HTTPS everywhere (do NOT
  weaken the CP-unwritable admission pin to HTTP). All Jobs use `https://…:8200` + the CA.
- **Split RBAC identities (4 SAs, exact verbs — codex r2):** `openbao-init` (Role: `create`+`get` on
  `openbao-keys` and `openbao-bootstrap-token` — it both writes them and reads back to unseal/verify);
  `openbao-unsealer` (Role: `get` ONLY `openbao-keys`); `openbao-provision` (Role: `get`+`delete` on
  `openbao-bootstrap-token`, `create`+`get` on `openbao-provisioner-token`); `agentforge-provisioner` (Role:
  `get` ONLY `openbao-provisioner-token`). The root/bootstrap token is thus NEVER readable by the unsealer or
  the long-running controller. No `list pods` (HTTP/DNS polling). (`create` can't be resourceName-scoped →
  the create-bearing Roles are tiny + single-purpose.)
- **Init Job `openbao-init`** (idempotent, negative-tested). Against `https://openbao-0.openbao-internal…`,
  TOLERATING NXDOMAIN/refused/not-ready (retry loop): (1) `GET seal-status`; (2) if `!initialized`:
  **preflight a k8s API write** (SelfSubjectAccessReview / dry-run) BEFORE calling init; then `operator init
  -key-shares=1 -key-threshold=1 -format=json`; **immediately, atomically** create `openbao-keys`
  (`stringData`, unseal key ONLY) and `openbao-bootstrap-token` (root token, SEPARATE Secret, separate RBAC),
  retrying the create without logging key material; (3) if `initialized && keys-Secret MISSING` → FAIL
  (can't unseal / disaster); (4) if `keys-Secret present && !initialized` (stale key over empty PV) → FAIL
  LOUDLY (never re-init); (5) unseal from `openbao-keys`, then **assert `sealed==false`** (wrong key → fail
  nonzero, no success-loop). **Partial-state / sentinel (codex r2):** the two Secrets are NOT created
  atomically, so a durable **`provisioned` sentinel** (the provision Job writes vault `cluster_id` +
  `provisioned=true` to `af/data/operator/provisioner/state` at completion) anchors truth: the init Job treats
  `initialized && keys present && bootstrap-token MISSING` as OK **only if** the sentinel says provisioned
  (root legitimately revoked); otherwise it's a partial-write/disaster state → FAIL, never declare agreement.
  Already-unsealed agreement is checked against the sentinel `cluster_id`, NOT a lookup on the (possibly
  revoked) root token.
- **Unsealer `openbao-unsealer` Deployment** (replicas 1, restricted SC, tiny): loop 15s — `GET seal-status`;
  if `sealed && initialized` → unseal from `openbao-keys`; back off + redact on error. + a default-deny
  egress NetworkPolicy (OpenBao + kube-apiserver + DNS only). Header notes HA needs per-pod discovery.
- **One-time provision Job `openbao-provision`** (idempotent via the sentinel): uses `openbao-bootstrap-token`.
  **FIRST check the `provisioned` sentinel** — if already provisioned, exit 0 (no-op) WITHOUT needing the
  now-revoked root (codex r2: a mount/auth existence check via the revoked token would fail on re-run). Else:
  enable `af` KV-v2 mount; enable+configure the `kubernetes` auth backend — **incl. TokenReview plumbing
  (codex r2): bind a dedicated reviewer SA to `system:auth-delegator` (tokenreviews.create) and set
  `token_reviewer_jwt`/`kubernetes_host`/CA/issuer/audience** (without it every SecretStore stays NotReady);
  write base operator policies; **mint a scoped `provisioner` token** (NOT root: policy limited to
  `af/data/operator/*` + policy/auth-role write) into Secret `openbao-provisioner-token`; write the
  `provisioned` sentinel (cluster_id); then **REVOKE the root token** and delete `openbao-bootstrap-token`.
  Fail-closed. **Test an ESO login end-to-end** (a Stage-1 canary SecretStore, below) before declaring done.
- **Provisioner controller Deployment `agentforge-provisioner`** (the R-2 controller, `agentforge`
  image `provisioner` subcommand): env `AF_PROVISIONER_OPENBAO_URL=https://…` + token from
  `openbao-provisioner-token` (ESO or direct Secret ref); reconciles per-tenant policies/auth-roles bound to
  exact ESO SAs/namespaces. Restricted SC + egress NetworkPolicy. **Its image (the agentforge worker image,
  `provisioner` subcommand) is BOOTSTRAP-class — built+pinned in Stage 0, NOT deferred to Stage 4 (codex r2:
  Stage 1 health depends on this controller Running, so its digest must be real before the Stage-1 merge).**
- Wire all into `security/openbao/kustomization.yaml`. **Ordering** via Flux `dependsOn`/health: cert →
  Helm → init → provision → provisioner; the openbao Flux Kustomization stays `wait:false`.

**C. Image-build Gitea CI + split digest-pin — `.gitea/workflows/images.yml` (agentforge + platform).**
On the existing act_runner VMs (confirm they need NO OpenBao/ESO). Build+push to registry.chifor.me:
`openbao-bootstrap` + `worker` (deploy/Dockerfile) + `broker` + `reaper` (agentforge) + `agentforge-platform`.
Emit each pushed digest. **Two SEPARATE pin paths** (`scripts/pin-image-digests.py` + `just pin-bootstrap` /
`just pin-workloads`): the BOOTSTRAP pin lands (and merges) BEFORE the OpenBao init Job; the WORKLOAD pin is
a later, separate commit that MUST NOT re-list any gated Deployment/ScaledJob. Registry-push creds scoped.

**D. Reframe + orchestration — `docs/runbooks/agentforge-activation.md` + `just activate-*` + header fixes.**
Runbook of the staged sequence + rollback + the accepted residuals (in-cluster key custody; etcd-encryption
preflight). `just activate-*` recipes wrap the milestones WITH explicit STOP/health predicates (not bare
command wrappers): block on bootstrap-digest-pinned, nested=Y, HelmRelease Ready, init/provision Job Succeeded,
SecretStore Ready, zero Flux drift. Fix the OpenBao HelmRelease + agent-nodes runbook headers (drop "manual").

### PREFLIGHTS (verified live 2026-07-13)
- **etcd Secret encryption at rest**: NOT configured (no EncryptionConfiguration/secretbox in Talos
  machine-config; no systemDiskEncryption) → the unseal-key Secret is recoverable from a raw etcd/disk read.
  ACCEPTED RESIDUAL (documented): the user chose in-cluster key custody for the homelab; enabling Talos disk
  encryption is a CP-rolling change out of this session's scope. Documented in the activation runbook.
- **cert-manager**: present + `ailab-ca` ClusterIssuer Ready (24d) → OpenBao TLS cert is issuable. ✓
- **Host free memory**: .2=24G, .3=26G, .4=68G avail; agent-node = 16 GiB each → fits on all 3 (tightest on
  .2, ~8G host headroom). ✓ (reduce `agent_node_memory_mib` if a host is under pressure at apply time.)
- **Nested virt**: `nested=1` live on all 3 hosts. ✓ (no reboot gate.)
- act_runner build path has no OpenBao/ESO dependency (builds the bootstrap image pre-OpenBao). ✓

### EXECUTE (live, STAGED — ⛔ = STOP for confirmation; each stage verified before the next)
- **Stage 0 — bootstrap-class images**: CI builds+pushes BOTH `openbao-bootstrap` AND the agentforge worker
  image (used by the provisioner controller — codex r2); `just pin-bootstrap` pins both (its own commit, does
  NOT un-gate any workload).
- **⛔ Stage 1 — operators/security merge**: merge the openbao/eso/keda/kro/security subset (incl. TLS +
  init/unseal/provision + provisioner controller + a **canary SecretStore/ExternalSecret** that logs in via
  the k8s auth backend, bootstrap+worker digests real) to `main`. THIS MERGE triggers the irreversible
  `/sys/init` → it IS the OpenBao-init checkpoint. Verify: HelmRelease Ready → init Job Succeeded →
  seal-status initialized+unsealed → provision Job Succeeded (af mount + k8s auth + TokenReview + policies +
  scoped token; sentinel written; root revoked) → provisioner controller Running → **the canary SecretStore
  reports Ready** (proves the k8s-auth login end-to-end, not just an unsealed vault — codex r2). `flux diff`
  first.
- **⛔ Stage 2 — Kata pool**: `just nested-virt-verify` (must be Y) → `just agent-nodes-apply` (tofu creates
  .14–.16 on the Kata image) → nodes Ready; `kubectl get runtimeclass kata gvisor`; a probe pod on the pool
  sees `/dev/kvm` + the Kata guest kernel. Checkpoint: creates VMs.
- **⛔ Stage 3 — agentforge layer merge**: merge the agentforge-broker/sandbox/workers/ci-runners/
  runtimeclasses/tenants subset to `main` (workloads still gated: unlisted manifests + paused ScaledJob +
  placeholder digests). Verify Kustomizations reconcile; ESO ExternalSecrets Ready; ledger schema/grants.
- **⛔ Stage 4 — un-gate workloads**: `just pin-workloads` (separate commit) → commit re-listing the gated
  worker/broker/reaper/dispatcher/CI manifests (only after: OpenBao roles/policies/KV seeded, ExternalSecrets
  Ready, KEDA targets present, ledger ready). tenant-zero worker scales 0→N on `forge_pending`.
- **Stage 5 — boundary tests → ⛔ v1.1 flip**: ADR-0018 canary (no cred mounts, `--network none`, Kata guest
  kernel, egress matrix) all green → flip `privilege_hardening: v1.1`. **Rollback if canary fails**: pause
  ScaledObjects/ScaledJobs, re-comment Deployments, confirm no sandbox Jobs remain, do NOT flip.

## Critical files
- NEW `.gitea/workflows/images.yml` (agentforge + platform) + agentforge `deploy/bootstrap.Dockerfile`;
  NEW `scripts/pin-image-digests.py`; `justfile` recipes.
- NEW `security/openbao/{tls,unseal-rbac,unseal-job,unsealer,provision-rbac,provision-job,provisioner-deploy}.yaml`
  + kustomization + HelmRelease TLS values; edit its header.
- NEW `docs/runbooks/agentforge-activation.md`; edit `docs/runbooks/agent-nodes.md` + OpenBao header.
- (verify only) `ansible/roles/pve_base` (nested-virt source of truth); `just nested-virt-verify`.

## Verification / negative tests
- B (TDD the bootstrap logic against a fake OpenBao+API): stale-Secret+empty-PV → FAIL; initialized+missing
  Secret → FAIL; wrong unseal key → FAIL nonzero (no success-loop); init/unseal logs contain NO key/token;
  re-run init Job = no-op; provision Job re-run = no-op; root token revoked after provision; SecretStore Ready
  ONLY after mount/auth/role provisioning.
- A: `nested=Y` on all three hosts (else Kata blocked).
- C: CI pushes `…@sha256:…`; bootstrap pin ≠ workload pin; neither un-gates a dormant manifest.
- EXEC: `flux diff`/SSA dry-run (not just `kubectl kustomize`) per stage; wait on specific HelmReleases/Jobs/
  SecretStores/ExternalSecrets/RuntimeClasses/nodes/KEDA before proceeding; boundary canary before v1.1.

## Risks / accepted residuals
- **Staged merge is mandatory** (codex): 3 scoped merges, not one 96-commit big-bang. Digest+unlisted+paused
  gates are defense-in-depth, but a merge still creates CRDs/webhooks/NS/PV/RBAC/NotReady ESO — hence staged.
- **Key/root custody**: unseal key in `openbao-keys` (in-cluster); root token used only by the one-time
  provision Job then REVOKED; the long-running unsealer reads unseal-key only; the provisioner uses a scoped
  token, never root. etcd-encryption residual documented.
- **Kata**: no reboot gate (nested already Y); if that ever regresses, it becomes a maintenance-reboot (still
  IaC, but blocking) — probe-gated so we never apply Kata against a non-Y host.

<!-- codex-review-status: finalized -->

<!-- Phase A round 1: codex raised TLS mismatch, root-token custody, split RBAC, provisioning-in-scope,
staged merge, bootstrap ordering, nested-virt Ansible ownership, idempotency/fail-closed hardenings, etcd
encryption preflight, rollback. ALL folded into this revision. Live finding: nested=1 already active (Kata
reboot gate removed). -->
