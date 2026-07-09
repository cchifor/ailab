# ADR 0017 — Self-hosted Gitea as master forge; GitHub as backup; CI on Gitea Actions

**Status:** PROPOSED (2026-07-09). Plan approved + adversarially reviewed; Phase 0–1 code landing on
`feat/gitea-actions-migration`. Nothing cut over yet — GitHub Actions stays primary until Gitea CI is
proven. **Relates to:** ADR 0013 (self-hosted GitHub runners — this is its successor), 0010 (versitygw
S3 on the QNAP — reused for artifacts), 0012 (Authelia SSO — Gitea login), 0014 (Zot registry — already
forge-independent).

## Context
A GitHub Actions **quota outage** blocked CI for `cchifor/platform` — the CI-heavy repo (17 active
workflows: lint/type/test of ~12 Python services + 13 SDKs + a Vue frontend, `docker buildx bake` of 14
images, e2e/contract/eval/load). `cchifor/ailab` has no workflows; it only *provisions* the runner fleet
that serves `platform` (ADR 0013). We already run **Gitea 1.26.1** in-cluster at `git.chifor.me` (Flux
HelmRelease, chart 12.6.0, Authelia OIDC, Cloudflare Tunnel — not behind Access so the git CLII works),
but Gitea Actions was off. The Zot registry (`registry.chifor.me`) is already the image target — CI never
depended on ghcr.io, which is the one big free win for a forge move.

## Decision
Make **Gitea the source of truth** for all cchifor repos; GitHub becomes a force-synced **read-only
backup** with Actions **disabled but re-enablable**. CI runs on **Gitea Actions** on the existing runner
VMs. Chosen after two independent adversarial reviews; the load-bearing corrections are captured below.

1. **Topology.** One-time import of each repo into Gitea, then devs push **directly to Gitea** (NOT an
   ongoing pull-mirror — mirrored commits don't reliably trigger Gitea Actions: go-gitea #24824/#24926).
   Each Gitea repo **push-mirrors → GitHub** ("sync on push"; PAT with **`repo`+`workflow`** scope — the
   `workflow` scope is *required* or GitHub rejects a force-push touching `.github/workflows`). Dormancy =
   **Actions disabled on the GitHub repo**, and GitHub branch protection **off** (a protected branch
   rejects the `git push --mirror` force-push and breaks the mirror).

2. **Gitea org.** `cchifor` is a GitHub *User* (no orgs → GitHub runners are repo-scoped). Gitea supports
   orgs regardless, so we create one: **org-scoped runners + secrets shared across all repos** — strictly
   better than the per-repo GitHub App registration.

3. **Runners = `act_runner` in HOST mode**, co-located on the existing VMs (ADR 0013) via a **new
   `gitea_runner` Ansible role** (NOT an extension of `github_runner`: act_runner is a different binary
   with a static org registration token, a persistent daemon, and no `ACTIONS_RUNNER_HOOK_JOB_STARTED`).
   **Host mode is mandatory** — platform workflows host-bind-mount `${{ github.workspace }}`
   (`docker run -v "$WS:/w"` reclaim steps) and `sudo systemctl start docker`; under act_runner's default
   *container* execution the host daemon can't bind-mount an in-container path. The runner registers the
   label **`self-hosted-hv:host`** so platform's `runs-on: ${{ vars.RUNNER_LABEL || 'ubuntu-latest' }}`
   seam is untouched (set the Gitea Actions org variable `RUNNER_LABEL=self-hosted-hv`). Pilot on
   **node1/node2 VMs only** — node3's 122b LLM leaves no RAM for a second heavy runner (ADR 0013 /
   platform#620). Daemon `capacity: 1` + a systemd `MemoryMax` bound job children; the sum of the
   co-located GitHub (10G) + Gitea runners must stay under the VM's 24 GiB.

4. **One portable `.github/workflows` tree — no second `.gitea/` tree.** Gitea reads `.github/workflows`
   directly; keeping one tree (a) sidesteps the disputed question of whether `.gitea/workflows` *masks*
   `.github/workflows` on Gitea, (b) avoids double-runs, and (c) keeps the GitHub fallback **always
   current** (re-enable = flip the repo Actions toggle) instead of a frozen, drifted copy. The few
   forge-divergent steps are guarded (`if: ${{ github.server_url == 'https://github.com' }}`).

5. **Artifacts on the existing S3 (ADR 0010), not the DB PVC.** Gitea Actions artifacts + logs on the
   single 10 GiB SQLite RWO PVC would fill it in days and contend with DB writes. Target =
   `[storage.actions_artifacts]`/`actions_log` at the **versitygw S3 on the QNAP**
   (`https://192.168.1.225:7070`, path-style, self-signed TLS) in a new `gitea-actions` bucket.
   **Done (2026-07-09):** created a scoped versitygw `gitea` IAM user + a `gitea-actions` bucket owned
   by it (via `versitygw admin create-user`/`create-bucket`); the S3 block in `gitea.yaml` is live with
   creds in the `gitea-actions-s3` secret. Retention `ARTIFACT_RETENTION_DAYS=14`/`LOG_RETENTION_DAYS=30`
   still applies. (Interim before this: artifacts on the local PVC.)

6. **Required checks via always-run aggregator jobs.** platform's path-filtered jobs (`frontend`,
   `gatekeeper`, `openapi-drift`; `contract / producer`) **skip** on unrelated PRs, and GitHub counts a
   skipped required check as passing. Gitea likely treats a skipped/absent context as unmet → blocks the
   merge. Fix: a per-workflow **always-run aggregator gate** (`needs:` its PR jobs, `if: always()`, fails
   only on a real sub-job failure) becomes the sole required check. **⚠ VERIFY on a staging repo before
   cutover** (this and: `DEFAULT_ACTIONS_URL=github` so `uses:` resolve; artifact up/download on 1.26;
   the exact Gitea status-context strings).

## Rejected / out of scope
- **Deploy MinIO for artifacts** — versitygw already exists (ADR 0010); a second object store is waste.
- **Keep GitHub as master, Gitea CI-only** — doesn't meet the "Gitea master" goal; and GitHub quota is
  the very thing we're routing around.
- **`workflows-pending/*` (GCP OIDC/WIF, GitHub Environments approvals, cosign keyless, auto-merge API)**
  — **no Gitea equivalent** (no OIDC token issuer, no Environments, no `deployments`/`checks`/`id-token`
  scopes). These stay GitHub-only or get redesigned. **`cosign` keyless supply-chain signing is
  permanently lost on Gitea** (no OIDC IdP for Actions jobs) — a real security regression, recorded here,
  not a deferral.
- **`registry-mirror-verify.yml`** — deliberately the only github-hosted job (needs *fresh* Docker Hub
  quota + privileged dind, must not run on the quota-exhausted pool). No fresh-quota pool exists on Gitea;
  its fate (drop, or one egress-capable act_runner with its own Docker Hub creds) is an open decision.

## Consequences
- CI no longer depends on GitHub quota; PRs/reviews/merge-gating move to Gitea.
- Two runner agents co-exist on the node1/node2 VMs during the bake-in; heavy e2e must be kept off one
  side at a time (the `heavy-compose-stack` concurrency throttle is per-forge and won't coordinate a
  GitHub e2e stack with a Gitea e2e stack on the same VM — the platform#620 double-heavyweight OOM).
- The `github_runner` role + platform's `runner-health.yml` canary stay live until the GitHub agent is
  actually retired; a **new** act_runner canary must be written (the GitHub one asserts `ephem-*` names /
  the job-started hook, which act_runner has no analog for).
- Gitea remains a single-instance SQLite forge on an RWO PVC with `Recreate` — a Gitea rollout interrupts
  in-flight Actions jobs; artifacts are offloaded to S3, but DB/HA hardening is a known follow-up.
- Coverage-delta PR comments degrade to same-run only initially (no cross-workflow artifact fetch on
  Gitea); the main-branch/nightly-e2e baseline columns return via an object-store follow-up.
- Post-cutover follow-up: extract a shared `runner_common` Ansible role (Docker/uv/k6/Node + the `runner`
  user) once `github_runner` is retired, so `gitea_runner` is standalone.

## Validation (2026-07-09, executed live)
Stood up and verified end-to-end: Gitea Actions enabled (chart values merged, Flux-reconciled); org
`cchifor` created; **4 act_runners** online on gha-runner-1/2/4/5 (host mode, `self-hosted-hv:host`);
versitygw scoped `gitea` user + `gitea-actions` bucket, **S3 artifact/log storage verified** (run logs
land in the bucket); `platform` imported; org **vars** (`RUNNER_LABEL`, `STATIC_RUNNER_LABEL` =
`self-hosted-hv`) + **secrets** (`SOPS_AGE_KEY`, `REGISTRY_USERNAME`/`PASSWORD`, `OPENAI_API_KEY`) set;
platform **cutover** done (Gitea branch protection, push-mirror → GitHub, GitHub Actions disabled);
platform CI confirmed **running on Gitea** (full CI + Contract matrix scheduled on the runners).

**Empirical findings (from a live pilot repo + the platform run):**
- **V1 confirmed** — a path-filtered job SKIPS and the always-run aggregator gate reports SUCCESS, so the
  PR is **mergeable** with the gate as the required check. The `ci-gate`/`contract-gate` design works.
- **V3 confirmed** — Gitea's status-check context is **`<workflow name> / <job> (pull_request)`** (e.g.
  `CI / ci-gate (pull_request)`), NOT the bare job name. Gitea branch protection uses glob patterns
  (`CI / ci-gate*`); the GitHub-shaped `branch-protection.json` keeps the bare names for the dormant fallback.
- **`STATIC_RUNNER_LABEL` required** — `contract.yml` routes PR micro-jobs to `vars.STATIC_RUNNER_LABEL ||
  ubuntu-latest`; on Gitea `ubuntu-latest` has no runner, so the org sets `STATIC_RUNNER_LABEL=self-hosted-hv`.
- **Artifacts** — `checkout@v4` / `paths-filter@v3` resolve from github.com and run, but
  **`upload-artifact@v4+` / `download-artifact@v4+` are unsupported on Gitea** (it reports as GHES). We do
  NOT pin to `@v3` (GitHub deprecated v3, and `uses:` can't be forge-conditional) — platform's uploads are
  `continue-on-error`, so they degrade (no artifacts on Gitea) without failing the gates. Native artifacts
  on Gitea are a follow-up (Gitea-compatible action or the coverage-baseline-via-registry work).
