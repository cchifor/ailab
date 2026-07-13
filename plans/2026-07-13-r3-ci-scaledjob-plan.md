# R-3 — k8s-native ScaledJob CI runners + Cilium egress canary/FQDN hardening

## Context

Two R-3 tail items on ailab `feat/p2-unlock` (DORMANT/gated). The CI-runner tranche is HEAVILY
PREFLIGHT-GATED (codex Phase A): three activation preflights (scale-trigger compat, docker://-vs-:host
workflow contract, the live gitea queued-jobs signal) gate live use — this plan lands the dormant
SCAFFOLDING (namespace + CI-specific admission + the ScaledJob/runner shape + operator ESO + CI egress)
with those preflights documented, PAUSED so a placeholder image can never schedule a failing Job.

1. **k8s-native CI runners** — replace the flaky host-mode act_runner VMs with a KEDA **ScaledJob**: one
   EPHEMERAL Kata runner + privileged DinD per queued CI job. SEPARATE, HARDER threat model than the
   tenant sandbox (a CI runner runs arbitrary repo CI + privileged DinD + needs package egress).
2. **Cilium egress FQDN hardening + a boundary canary** (R-3 §8) — apply `toFQDNs` where it actually
   strengthens policy (the BROKER→UPSTREAM external egress), and land a boundary canary rendered as a
   REAL sandbox-admitted shape (not an ad-hoc probe the VAP would reject).

## Approach

### A. Scale trigger — KEDA `forgejo-runner` scaler FIRST, custom poller as fallback (both preflight)
KEDA 2.20 ships a built-in **`forgejo-runner`** scaler (scales on pending jobs by runner labels). Gitea
1.26 ≠ Forgejo, so **PREFLIGHT #1**: verify the scaler's API against the live gitea (the endpoint +
required token scope). If incompatible, FALLBACK: a small **ci-dispatcher** poller (mirrors af-dispatcher)
querying gitea for queued Actions tasks-by-label → `gitea_ci_pending{label}` gauge → a Prometheus trigger.
Either way the ScaledJob shape is stable; only the trigger block differs. Trigger tuning: `threshold "1"`,
`activationThreshold "0"` (or omit), a single-scalar query, and — because a pending-only metric under-scales
while runners are busy — the ScaledJob `scalingStrategy: accurate` (or include locked/running backlog).

### B. CI runner ScaledJob — new dir `kubernetes/apps/infrastructure/agentforge-ci-runners/` + a CI VAP
- `namespace.yaml`: a SEPARATE `agentforge-ci` namespace (NOT agentforge-sandbox — its restricted PSA/VAP
  reject privileged DinD and must not be widened). PSA **privileged** on THIS ns ONLY, fenced by a
  CI-specific VAP (below) + a default-deny netpol.
- `ci-guard.yaml` (NEW VAP): pins the EXACT CI pod shape and rejects everything else — exactly two
  containers (the Kata `runner` + the privileged `dind`), `runtimeClassName: kata`, `automountServiceAccountToken:
  false`, a tokenless SA, NO host namespaces (hostNetwork/PID/IPC), NO hostPath, NO extra containers,
  digest-pinned images, bounded resources, and the reg-token Secret mounted ONLY into the `runner`
  container (never `dind`/job). Privileged is allowed ONLY for the `dind` container of an admitted CI pod.
- `scaledjob.yaml` (GATED, PAUSED — placeholder runner image + `autoscaling.keda.sh/paused: "true"` so no
  live trigger can schedule it): KEDA `ScaledJob`, `jobTargetRef` a Kata Job (label `self-hosted-hv`) with
  act_runner in **EPHEMERAL mode** (`GITEA_RUNNER_EPHEMERAL=1`/`--ephemeral`) + a UNIQUE runner name per
  Job (downward-API pod name), NO persistent `/data` (emptyDir, `.runner` state clean by construction),
  `ttlSecondsAfterFinished` + `successfulJobsHistoryLimit`/`failedJobsHistoryLimit`, `maxReplicaCount`
  (concurrent cap). The runner registers, runs ONE job, exits.
- `externalsecret-regtoken.yaml`: the reg token from an operator ESO (`af/data/operator/ci/runner-registration`,
  separate operator SecretStore, unreadable by tenant roles), mounted ONLY into the `runner` container.
  PREFLIGHT #3: a plain gitea registration token is REUSABLE until reset — prefer a startup wrapper that
  MINTS a fresh single-use token via the admin/runner API if available; the per-workflow `GITEA_TOKEN` is
  gitea-issued (do NOT add any PAT to the runner pod).
- `ci-dispatcher-*.yaml` (GATED, only if the forgejo scaler fails preflight): read-only poller; pin its
  gitea token to the MINIMUM scope for queued-job visibility (may need actions/admin read), disjoint from
  the reg/admin-write creds.
- `cilium.yaml`: CI egress = forge + the package registry/proxy + the DinD image registry ONLY, explicit
  DENY of kube-apiserver, OpenBao, ESO, and all cluster-private ranges + metadata/node-local/IPv6. NOTE
  (residual): CI egress-allowlisting does NOT close exfil like the sandbox model-only boundary (a package
  proxy is an exfil path) — untrusted PR workflows must receive NO sensitive secrets; the operator logs/
  proxies outbound package traffic. PREFLIGHT #2: `docker://`+DinD must be proven against the platform
  workflows that today require `self-hosted-hv:host` (they bind-mount `${{ github.workspace }}` + drive the
  host Docker daemon) + actions/cache reachability from DinD job containers — else the workflow contract
  changes; `--network none` for job containers breaks normal checkout/install unless via explicit proxies.
- `kustomization.yaml` (ScaledJob + ci-dispatcher UNLISTED/paused) + `clusters/ai/agentforge-ci-runners.yaml`
  (Flux Kustomization, dependsOn infrastructure [Cilium/ServiceMonitor CRDs] + keda + external-secrets).

### C. Egress FQDN hardening + canary (correctly targeted)
- **FQDN on BROKER→UPSTREAM** (`agentforge-broker/ciliumnetworkpolicy.yaml`): the broker's egress to the
  external provider/OAuth hosts gets an explicit `toFQDNs` allow (a DNS-resolved external destination — the
  correct `toFQDNs` use). The sandbox agent→broker path STAYS DNS-L7-exact-name + `toServices` (cluster-local;
  `toFQDNs` there adds nothing — codex). Do NOT touch the already-correct sandbox-agent egress.
- **Boundary canary** (`agentforge-sandbox/egress-canary.yaml`, GATED/documented, NOT auto-run): rendered
  as a VALID sandbox Job shape (matching sandbox-guard: kata, tokenless SA, the {workspace,home} volumes,
  digest-pinned, the exact securityContext + labels) whose command ATTEMPTS a denied target (world IP,
  metadata 169.254.169.254, an alt-DNS, another pool's broker) and MUST FAIL — the ADR-0018 boundary
  assertion, run at activation via the real SandboxExecutor path (or kept as documented `test_cmd`s).

## Critical files
- NEW `kubernetes/apps/infrastructure/agentforge-ci-runners/**` (namespace/ci-guard[VAP]/scaledjob[paused]/
  externalsecret-regtoken/cilium/[ci-dispatcher-*]/kustomization) + `clusters/ai/agentforge-ci-runners.yaml`.
- `kubernetes/apps/infrastructure/agentforge-broker/ciliumnetworkpolicy.yaml` — add broker→upstream `toFQDNs`.
- NEW `kubernetes/apps/infrastructure/agentforge-sandbox/egress-canary.yaml` (gated, sandbox-Job-shaped).
- (agentforge follow-up, activation-gated) the `ci-dispatcher` subcommand + gauge — ONLY if the KEDA
  forgejo scaler fails the gitea-compat preflight.

## Verification
- `kubectl kustomize` builds; the ScaledJob + ci-dispatcher are UNLISTED **and** paused; placeholder digests;
  no `privilege_hardening` flip; the CI VAP/PSA changes are scoped to `agentforge-ci` only.
- ci-guard VAP: admits ONLY the exact 2-container Kata CI pod (runner+dind, kata, tokenless, no host-ns/
  hostPath/extra-container, digest-pinned, bounded, reg-token→runner-only); rejects non-Kata privileged,
  hostPath, host namespaces, extra containers, a secret mount into dind, an automounted token, a non-digest
  image, and privileged on the runner container.
- ScaledJob: ephemeral flag set, unique runner name, emptyDir /data, ttl+history limits, maxReplicaCount,
  the (forgejo-scaler-OR-prometheus) trigger with threshold 1 / activationThreshold 0 / accurate strategy.
- reg-token: operator ESO path under `operator/ci/*`, mounted ONLY in the runner container, never visible
  from a job/dind container (env/volume/inspect).
- CI egress: forge + registry/proxy only; DENY apiserver/OpenBao/ESO/cluster-private/metadata/IPv6.
- FQDN: applied to broker→upstream external hosts; sandbox-agent egress UNCHANGED. Canary: a valid
  sandbox-admitted Job that fails-to-reach a denied target.

## Preflight gates (activation — documented, NOT resolved here)
1. KEDA `forgejo-runner` scaler ↔ gitea 1.26 compatibility (endpoint + token scope); else the poller query.
2. `docker://`+DinD ↔ the platform workflows that require `:host` (`${{ github.workspace }}` bind-mount +
   host-Docker + actions/cache) — prove or change the workflow contract.
3. The reg-token single-use/rotation story + non-exposure to job containers.
This tranche does NOT delete the working ansible host-mode runners; the operator cuts over at activation
(coexist during migration). On `feat/p2-unlock`, NOT a PR to main.

<!-- codex-review-status: complete -->
