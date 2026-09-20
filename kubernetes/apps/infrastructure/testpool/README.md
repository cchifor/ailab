# testpool — the leasable test-environment pool

> **This namespace is for LEASING, not deploying.** `tep-worker` grants sandboxclaim CRUD,
> read-only pods/sandboxes, and exec/attach/portforward — no create on secrets, deployments or pods,
> so `helm install` cannot work here and is not meant to. That is deliberate: pod-create in this
> namespace would let an agent schedule a plain pod alongside the Kata-isolated leases and bypass
> the boundary the pool exists to provide.
>
> **To deploy a chart, use `helmtest-dw<N>`** — six per-worker namespaces with PSA `restricted`, a
> quota, and a Helm-capable Role: `kubernetes/apps/infrastructure/helmtest/`,
> `docs/runbooks/helmtest.md`, ADR 0021. Agents reach it with `~/.helmtest/kubeconfig`.

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

## Readiness, teardown, and the env node (2026-09-20 outage)

Runbook: `docs/runbooks/env-pool.md`. Plan with the evidence chain:
`plans/2026-09-20-env-pool-frozen-guest-outage-plan.md`.

- **Readiness = "a lease can run docker here."** `control`'s readiness probe is an exec of
  `docker version` through the kata-agent (10 s period, 6 failures ≈ 60 s); the tcp ready-port
  9099 is only the startup gate. This matters because the warm-pool GC deletes any member older
  than the 15 m grace the moment it is observed NotReady — a 6 s blip on a healthy 66 h-old env
  is what started the outage. Known gap: the kubelet prober discards non-timeout CRI transport
  errors without counting them; the two stalls that matter surface as timeouts.
- **Teardown is bounded.** `env-reaper.yaml` (DaemonSet in `kube-system`, Role here) SIGKILLs the
  Cloud Hypervisor VM of a pod `Terminating` > ~2 min, and its shim 2 min later. A frozen guest
  cannot be killed through the agent, and it was the *accumulation* of such hangs (two, plus a
  create) that wedged the kubelet. Alerts `TestpoolEnvTeardownStuck{,Critical}` are the outcome
  check; `env-node-rules.yaml` names the node-side precursors.
- **Entrypoints trap TERM** and end in `sleep infinity & wait $!`, so a normal stop takes ~1 s
  instead of the 30 s grace + SIGKILL it used to.
- **Root cause of the guest freeze is still open** (single-threaded virtiofsd is the suspect);
  it needs Kata debug logging, which needs the env node's machine config under tofu.

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
