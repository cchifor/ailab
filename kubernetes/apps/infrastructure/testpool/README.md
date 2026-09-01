# testpool — the leasable test-environment pool

Design: agentforge repo, `plans/2026-09-01-test-env-pool-k8s-plan.md` (codex-finalized; spikes
0-4 PASSED — see `kubernetes/infra/env-pool/SPIKE-REPORT.md`). Agents on the dev-workers lease a
pristine Kata DinD environment (`tep lease`), sync a worktree over exec, run suites *inside* it,
and release; the volume restores from an immutable golden snapshot (warm docker cache), and every
release destroys the environment. Warm adoption ≈ 0.2-1 s; cold/refill ≈ 90-150 s off the lease
path. Nodes: the dedicated `env-pool` Talos workers (`kubernetes/infra/env-pool/`, tofu).

## Activation order (first rollout)

1. Merge this tree; Flux applies `agent-sandbox` (adopts the spike-2 hand-applied operator) and
   `testpool`. Warm-pool members will cycle Pending under the 15 m readiness grace — expected —
   until step 2.
2. Bootstrap the first golden: `hack/golden-refresh.sh golden-v1` (creates + verifies the
   snapshot; the template already points at `golden-v1`).
3. Watch `env-std-pool` fill (one Ready member) and lease once end-to-end from a worker
   (`tep lease` / the spike's `tep-mini.sh`).
4. Distribute worker kubeconfigs (tokens in `tep-dwN-token` Secrets) via ansible
   `roles/dev_worker` — follow-up alongside the full `tep` CLI.
5. Decommission the spike leftovers (namespace `testpool-spike`, RuntimeClass `kata-env-spike`,
   StorageClass `testpool-spike-iscsi`, snapshot `golden-spike-v2`) once the churn soak has been
   read out.

## Golden snapshot publication protocol

Immutable `golden-vN`; the SandboxTemplate `dataSource.name` IS the pointer; a bump is a git PR
(never silent). `hack/golden-refresh.sh` builds + verifies vN+1 (populate → pull
`hack/golden-images.txt` → quiesce → snapshot → scratch-restore verify), prints the rollout
steps. Keep vN-1 until unreferenced. Refresh monthly or on toolchain/image-set changes; a
staleness alert is a monitoring follow-up.

## Deliberate posture notes

- **Egress amendment** (recorded in `networkpolicy.yaml`): cluster-ward deny (pod/svc CIDRs,
  apiserver VIP, metadata) with world egress allowed — the audited suites pull ghcr/mcr/npm/pypi
  at runtime, so a mirror-only allowlist would break them; cluster isolation is the boundary that
  matters. Trusted-code-only pool, same posture as a dev-worker.
- **v1 images** are upstream `docker:28.3` digest-pinned; the bespoke toolchain image (uv, node,
  Playwright deps, rsync, tep-supervisor, e2fsprogs baked — kills the runtime `apk add` and
  enables real L/XL host-side suites) is the next iteration and slots into the template + pre-pull
  DaemonSet without shape changes.
- **Flavors**: only `env-std` (16 Gi limit) exists; `env-big` (24 Gi) is gated on freeing host RAM
  for a larger env node (companion plan `2026-09-01-dynamic-dev-infra-plan.md`).
- **tep** here = RBAC only; the CLI (supervisor runs, extend-on-submit TTL, drained-pool
  queueing, lost-race protocol) ships with the dev_worker ansible role.
