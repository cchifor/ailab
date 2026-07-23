# AgentForge v2 activation — status snapshot (2026-07-23)

Dated record of where the AgentForge v2 tenant-zero activation stands after the 2026-07-22/23 push.
Historical — do not rewrite; supersede with a newer dated file. Operational detail lives in the
runbooks/ADRs cross-referenced below; this is the consolidated "where we are".

## Shipped this cycle

### Gate 3 — sandbox hostile-import blocker: FIXED + LIVE
The mutating-role sandbox import failed `ImportRejected: unexpected owner uid`. Root cause: the sandbox
image (`deploy/sandbox.Dockerfile`) declared `VOLUME ["/workspace/agentforge/jobs"]` + a nested `WORKDIR`
+ `AF_JOBS_ROOT` **under the `/workspace` mount**, so the container runtime materialized root-owned
`agentforge/`+`agentforge/jobs/` scaffold dirs inside the exported per-job subPath, which the import
validator (correctly) rejects on uid.

Fix = **remove the vestigial VOLUME/WORKDIR/env** — the sandbox works in the staged `/workspace` subPath,
never `AF_JOBS_ROOT` (that is the *orchestrator's* clone tree). `import_validator.py` / `sandbox.py` are
untouched (the uid ownership invariant is fully intact). A first "skip the scaffold" `openat2` approach was
**codex-rejected** (missing `RESOLVE_NO_SYMLINKS`/`O_NOFOLLOW`, missing `RESOLVE_NO_XDEV`, and it pruned on
path+emptiness rather than provenance) — root-cause removal is the correct fix. The regression guard is a
fail-closed Docker-semantic evaluator (ENV/`$VAR` chaining, `VOLUME` JSON+shell forms, relative-`WORKDIR`
accumulation, multi-stage inheritance, tab separators) plus a release-time `imagetools inspect` ground-truth
check on the built image config.

- `cchifor/agentforge` **#51** merged (`6dabe6f`); sandbox image rebuilt (`images.yml`).
- Verify a built image **by digest** (config `Volumes` empty, `WorkingDir=/workspace`) before pinning — this
  refutes the docker tag→digest race that mispinned an image earlier in the project.
- ailab **#88** repin (`AF_SANDBOX_IMAGE` → `@sha256:02711119…`) merged; Flux reconciled; the planner
  `af-orch-playground-planner` rolled to the fixed image. **The bug is fixed on-cluster.**

### P1 control-plane activation (agentforge-platform)
Most P1 scaffolding was already merged/live (DB roles+DSNs, OIDC client + Authelia block, RBAC/SA/Service/
NetworkPolicy/VAPs, cloudflared route, `agentforge-tenants` repo). The remaining **activation gap** shipped as
two stacked, codex-reviewed PRs:
- **#89 (PR-A)** — MERGED. Safe prerequisites that deploy nothing: CP image pinned to
  `agentforge-platform@sha256:85a4a3c7…` (provenance-verified: tag→digest match, linux/amd64, entrypoint),
  `readinessProbe`→`/readyz` (DB gate) with liveness on `/healthz`, `db-migrate` + CNPG init-container
  digest-pinned, `af:tenant-zero:owner` seeded onto `chifor`, and `docs/runbooks/agentforge-platform-activation.md`.
- **#90 (PR-B)** — OPEN, **gated**. The single `- deployment.yaml` go-live line. Must NOT merge until the two
  restricted bot tokens are minted + filled on the PR-B branch and the `agentforge_platform` DB is created +
  migrated (runbook steps).

Notes: exposure is **Access-free** per ADR 0019 (the CP does its own Authelia OIDC; CF Access would
double-login). kro is P2 — P1 DoD is Flux materializing plain per-tenant manifests. Bot-token isolation needs
**dedicated restricted users** (Gitea PATs are user+scope, not per-repo).

### Gate 2 CI runners — host-mode (#92, MERGED)
The user overrode the P2 k8s-native/KEDA-ScaledJob proposal in favour of the existing host-mode `act_runner`
VMs. Delivered a PREFLIGHT #2 read-only health-gate (`scripts/check-ci-runners.py` + 49 tests +
`just ci-runners-preflight`), an ADR 0019 update recording host-mode as the P2 override, and stale-IP
corrections. No apply needed (comment-only tofu edit; runner IPs are `lifecycle.ignore_changes`).

**Inventory correction:** runner VMs are **192.168.0.14–.18**; the vacated **.47/.48/.49** are now the ADR 0019
agent-nodes — CLAUDE.md had the two rows swapped (now fixed).

## Remaining / staged
1. **#90 (PR-B) go-live** — after: mint 2 restricted bot tokens (`agentforge-cp-bot` write:repository on ONLY
   `cchifor/agentforge-tenants`; `agentforge-bootstrap-bot` write:issue) + fill `agentforge-runtime.sops.yaml`
   on the PR-B branch → create `agentforge_platform` DB (resolve `.status.currentPrimary`, do not assume
   `infra-pg-1`) + run the migration Job → merge #90 → roll authelia → verify `/readyz` + login →
   create-workspace → tenants-commit → Flux materializes the tenant namespace. Full commands:
   `docs/runbooks/agentforge-platform-activation.md`.
2. **Final Gate-3 lifecycle proof** — the fix is live, but exercising it end-to-end needs a poisoned-budget
   reset (delete stale `af:run`/`af:xrev` markers, spaced to dodge the edge rate-limit) OR a fresh
   `1-needs-plan` playground issue, plus a full autonomous LLM run. Watch an issue reach `5-completed`.

## Operational learnings (this run)
- **Classifier gate boundaries (headless):** Gitea PR/issue creation and `workflow_dispatch` are allowed;
  PR **merge**, shared-DB mutations, and in-pod user creation are blocked. So autonomous work reaches
  merge-ready and the human runs the merges + live operator steps.
- **Forge remote:** the local ailab `origin` is the GitHub backup mirror; Flux reconciles from the `gitea`
  remote — always `git push gitea <branch>` for ailab.
- **Gitea token hygiene:** a token cannot delete token endpoints (needs basic-auth) — reuse one named PAT and
  revoke leftovers via the UI. The org runners API needs both `read:admin` and `read:organization`.

## Cross-references
- ADR `docs/decisions/0019-*` — AgentForge v2 control plane (+ 2026-07-22 host-mode CI-runner update).
- Runbooks: `docs/runbooks/agentforge-platform-activation.md`, `docs/runbooks/ci-runners.md` §8.
- Activation plan (Stage 0–5): `plans/2026-07-13-iac-activation-plan.md`.
