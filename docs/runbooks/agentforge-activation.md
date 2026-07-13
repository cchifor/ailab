# Runbook — AgentForge v2 activation (100% IaC, no manual/physical ops)

Activates the dormant v2 stack (built on `feat/p2-unlock`) via `just` + `tofu` + Flux + GitOps merges.
There are NO ad-hoc console commands — every step is codified. Design + rationale: `plans/2026-07-13-
iac-activation-plan.md` (codex-reviewed). Cluster: `kubectl --context admin@ai`. Hosts: `scripts/node-ssh.py`.

## Preconditions (verified 2026-07-13)
- Nested virt live on all hosts (`just nested-virt-verify` → PASS) — Kata has NO reboot gate.
- cert-manager `ailab-ca` ClusterIssuer Ready → OpenBao internal TLS.
- **etcd Secret encryption at rest is NOT configured** (accepted homelab residual): the OpenBao unseal key
  in Secret `openbao-keys` is recoverable from a raw etcd/disk read. Acceptable for tenant-zero; enabling
  Talos disk encryption (CP rolling reboots) is a separate hardening.

## Staged sequence (⛔ = irreversible; stop + confirm before each)

### Stage 0 — bootstrap images
CI (`.gitea/workflows/images.yml` in agentforge + agentforge-platform) builds+pushes to registry.chifor.me.
Then pin the two bootstrap-class digests (own commit, un-gates nothing):
```
just pin-bootstrap sha256:<orchestrator> sha256:<agentforge-platform>
```

### ⛔ Stage 1 — operators/security merge (triggers the live OpenBao init)
Merge the openbao/eso/keda/kro/security subset of `feat/p2-unlock` to `main`. Flux deploys OpenBao (TLS via
`openbao-tls`), then the Jobs run automatically:
- `openbao-init` → initialize + unseal, writing `openbao-keys` (unseal key + cluster_id) and
  `openbao-bootstrap-token` (root). Idempotent + fail-closed (never re-inits over a live vault; cluster_id
  disagreement or partial-death → hard fail).
- `openbao-unsealer` Deployment → re-unseals on any restart (reads `openbao-keys` only).
- `openbao-provision` → `af` KV mount + `kubernetes` auth backend (server-SA TokenReview, no static JWT) +
  base policies + canary role/seed + scoped `openbao-provisioner-token`; writes the `openbao-state`
  sentinel; **revokes root + deletes `openbao-bootstrap-token`**.
- `agentforge-provisioner` controller → per-tenant policies/roles.
Verify (`just openbao-status`): init+provision Jobs Succeeded; `openbao-state.provisioned=true`;
`openbao-bootstrap-token` gone; the **`openbao-canary` ExternalSecret Ready=True** (end-to-end k8s-auth proof).
`flux diff` before merging.

### ⛔ Stage 2 — Kata agent-node pool
```
just nested-virt-verify        # gate (already passes)
just agent-nodes-apply         # tofu creates .14–.16 on the Kata image (depends on the gate)
```
Verify: nodes Ready; `kubectl get runtimeclass kata gvisor`; a probe pod on the pool sees `/dev/kvm`.
(Host RAM headroom: .2/.3 ~24–26G free, agent-node = 16G — reduce `agent_node_memory_mib` if a host is tight.)

### ⛔ Stage 3 — agentforge layer merge
Merge the agentforge-broker/sandbox/workers/ci-runners/runtimeclasses/tenants subset to `main`. Workloads
stay gated (unlisted manifests + paused ScaledJob + placeholder digests). **KNOWN pre-Stage-3 TODO:** the 4
operator SecretStores (broker/ci/reaper/dispatcher) carry a `caProvider` referencing `openbao-tls` in their
OWN namespaces — the ailab-ca CA must be distributed there (per-ns cert-manager Certificate or trust-manager)
before they go Ready. The Stage-1 canary is unaffected (same ns as the cert).

### ⛔ Stage 4 — un-gate workloads
`just pin-workloads <img>=sha256:… …` (separate commit) then a commit re-listing the gated manifests — ONLY
after ExternalSecrets Ready, KEDA targets present, ledger schema/grants applied. tenant-zero worker scales
0→N on `forge_pending`.

### ⛔ Stage 5 — boundary tests → v1.1
ADR-0018 canary (no cred mounts, `--network none`, Kata guest kernel, egress matrix) all green → flip
`privilege_hardening: v1.1`. **Rollback on canary failure:** pause ScaledObjects/ScaledJobs, re-comment
Deployments, confirm no sandbox Jobs remain, do NOT flip.

## Disaster notes
- `openbao-init` refuses to re-init if `openbao-keys` exists but the vault is uninitialized (stale key / lost
  PV) — it fails loudly rather than minting a NEW vault that orphans every stored secret.
- Losing `openbao-keys` after init = unrecoverable seal → restore the vault PV from backup or re-key.
- The unseal key + (pre-revocation) root live only in the `openbao` namespace; the unsealer reads the unseal
  key only; the provisioner uses a scoped token, never root.
