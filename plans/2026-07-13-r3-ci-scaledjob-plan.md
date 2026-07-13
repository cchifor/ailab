# R-3 — k8s-native ScaledJob CI runners + Cilium egress canary/FQDN hardening

## Context

Two R-3 tail items, both ailab `feat/p2-unlock` (dormant/gated):
1. **k8s-native CI runners** — replace the flaky host-mode act_runner VMs (ansible `gitea_runner`,
   capacity 1, load-sensitive — the source of this session's transient CI failures) with a KEDA
   **ScaledJob**: one EPHEMERAL Kata runner + privileged DinD per queued CI job (fresh microVM/job,
   deleting v1's workspace-reclaim machinery). SEPARATE threat model from the workers (a CI runner runs
   arbitrary repo CI + privileged DinD, so it MUST be a Kata microVM). Registration token from OpenBao/ESO.
2. **Cilium egress canary/FQDN hardening** (R-3 §8) — the base egress landed in Wave B-ii/worker-autoscaling;
   R-3 adds (a) FQDN-on-broker tightening and (b) a NetworkPolicy CANARY that ASSERTS the sandbox/worker
   egress boundary is enforced (a probe pod that must FAIL to reach anything but its allowlist).

## Approach

### A. The scale trigger (the key design decision — BEST-GUESS pending preflight)
KEDA has no built-in gitea-Actions scaler. Mirror the af-dispatcher pattern: a small **ci-dispatcher**
poller (agentforge code, `agentforge ci-dispatcher` OR a tiny standalone) that queries the gitea API for
QUEUED Actions tasks by runner label and exports `gitea_ci_pending{label,repo}` on a metrics port; a KEDA
ScaledJob's **prometheus** trigger scales `sum(gitea_ci_pending{label="self-hosted-hv"})` → one runner Job
per queued task. PREFLIGHT-GATED UNKNOWN (documented): whether gitea exposes queued-Actions-by-label
cleanly via `/api/v1/.../actions/tasks` or the admin runners API — the poller's exact query is confirmed
against the live gitea at activation; until then the ScaledJob is dormant (gated image, unlisted). ALT
noted: if gitea's own `/metrics` exposes `gitea_actions_tasks{status="waiting"}`, use that directly and
drop the poller.

### B. CI runner ScaledJob — new dir `kubernetes/apps/infrastructure/agentforge-ci-runners/`
- `namespace.yaml`: `agentforge-ci` (or reuse agentforge-sandbox's Kata posture), restricted-ish + a
  default-deny netpol.
- `scaledjob.yaml` (GATED — placeholder runner image): KEDA `ScaledJob`, `jobTargetRef` a Job template
  running the gitea act_runner (label `self-hosted-hv`, `docker://` schema) in a Kata microVM
  (`runtimeClassName: kata`) with a per-pod privileged DinD sidecar (safe INSIDE the microVM),
  `automountServiceAccountToken:false`, tokenless SA, restricted PSA-exempt-for-Kata. `maxReplicaCount`
  (concurrent runners cap), `successfulJobsHistoryLimit`/`failedJobsHistoryLimit`, `scalingStrategy`.
  The runner registers with the token, picks up ONE job, exits (ephemeral — no reclaim machinery).
- `externalsecret-regtoken.yaml`: the gitea runner REGISTRATION token from OpenBao via ESO
  (operator path `af/data/operator/ci/runner-registration`), separate operator SecretStore.
- `ci-dispatcher-deployment.yaml` (GATED) + `service.yaml` + `servicemonitor.yaml` + `netpol.yaml`:
  the poller (read-only gitea API, exposes `gitea_ci_pending`), like af-dispatcher.
- `cilium.yaml`: runner egress = the CI's needs (forge + the package proxy + DinD registry) — a runner
  runs untrusted CI, so egress is broader than a worker but still allowlisted (NO metadata/node-local);
  its DinD is `--network none` per-exec by default with an opt-in pull-through proxy.
- `kustomization.yaml` (ScaledJob + ci-dispatcher Deployment gated/unlisted) + a Flux Kustomization
  `clusters/ai/agentforge-ci-runners.yaml` (dependsOn keda + external-secrets).

### C. Egress canary/FQDN hardening — extend `agentforge-sandbox/` + `agentforge-broker/`
- FQDN-on-broker: where the sandbox agent egress currently allows the broker by ClusterIP/Service +
  DNS-to-broker-name, ADD an explicit Cilium FQDN (`toFQDNs`) restriction so only the broker's exact FQDN
  resolves (defense-in-depth vs DNS tunneling; R-3 §8).
- Canary: `egress-canary.yaml` (GATED CronJob or a documented manual probe, NOT auto-run): a probe pod
  carrying the sandbox `trust-class=agent` identity that ATTEMPTS to reach a denied target (world IP,
  metadata 169.254.169.254, another pool's broker, an alt-DNS) and MUST FAIL — the ADR-0018 boundary
  assertion, run at activation as the "sandbox-boundary tests green" gate. Documented as an
  activation-time verification, not a running workload.

## Critical files
- NEW `kubernetes/apps/infrastructure/agentforge-ci-runners/**` + `clusters/ai/agentforge-ci-runners.yaml`.
- `agentforge-sandbox/cilium-egress.yaml` + `agentforge-broker/ciliumnetworkpolicy.yaml` — add toFQDNs.
- NEW `agentforge-sandbox/egress-canary.yaml` (gated/documented probe).
- (agentforge follow-up, noted) the `ci-dispatcher` subcommand + `gitea_ci_pending` gauge — a small code
  addition, activation-gated with the poller; OR use gitea's native `/metrics` if it exposes the signal.

## Verification
- `kubectl kustomize` builds; ScaledJob + ci-dispatcher Deployment UNLISTED/gated with placeholder digests;
  no `privilege_hardening` flip.
- ScaledJob: `jobTargetRef` = a Kata runner + privileged DinD (privileged ONLY inside the Kata microVM),
  tokenless SA, ephemeral (one job → exit, no reclaim); registration token from the operator ESO (path
  under `operator/ci/*`, unreadable by tenant roles); the prometheus trigger queries `gitea_ci_pending`.
- ci-dispatcher: read-only gitea API, exposes the gauge, ServiceMonitor scrapes it, netpol egress = forge + DNS.
- FQDN: the sandbox agent + broker egress carry an explicit `toFQDNs` allow for the exact upstream/broker
  names; the canary asserts a denied target FAILS (documented activation gate).
- Deny world/metadata/node-local/IPv6 on the runner + canary paths.

## Notes / residuals
- The CI runner image (Kata-capable act_runner + DinD) is operator-built + digest-pinned at activation
  (gated placeholder now).
- The scale-trigger query is a BEST-GUESS confirmed against live gitea at activation (preflight-ish) — if
  gitea's queued-Actions signal differs, the ci-dispatcher query is adjusted; the ScaledJob shape is stable.
- This tranche does NOT delete the existing ansible host-mode runners — it lands the k8s-native replacement
  dormant; the operator cuts over at activation (both can coexist during migration).
- On `feat/p2-unlock`, NOT a PR to main. codex Phase A on this plan, then Phase B on the manifests (cap 3).

<!-- codex-review-status: pending -->
