# ADR 0019 — AgentForge v2: self-service, multi-tenant, sandboxed control plane

**Status:** PROPOSED (2026-07-11). Plan adversarially reviewed (codex, finalized). This ailab PR
carries the **P1 infra/deploy companion** only — the applications land in `cchifor/agentforge` (v1,
extended) and the NEW `cchifor/agentforge-platform`. Full plan:
`plans/2026-07-11-agentforge-v2-plan.md`. **Supersedes/extends** ADR 0018 (AgentForge v1 —
host-systemd agents on dev-workers). **Relates to:** ADR 0017 (Gitea master forge), 0016 (app HA
tiering — infra-pg, cloudflared), 0015 (Headlamp Flux safe-ops — the field-scoping gap this ADR
closes for the CP path), 0012 (Authelia SSO), 0013 (self-hosted runners).

## Activation status (2026-07-21)

The **P2 unlock is PROVEN LIVE end-to-end on tenant-zero**. Driving the playground repo
(`cchifor/agentforge-playground`) issues through the autonomous loop exercised the whole v2 thesis —
the **credential-injecting broker**, the **Kata sandbox executor**, **capability-JWT minting +
verification**, **KEDA scale-to-zero**, and **ESO + OpenBao credential sync** — with real subscription
inference, no static keys ever handed to agent code:

- **Credential broker (ns `agentforge-broker`).** Per-account Deployments
  `broker-anthropic-max1` / `broker-anthropic-max2` (`claude_max`) and `broker-openai-codex`
  (`codex_pro`) each mount ONE operator OAuth credential (ESO-synced from OpenBao, reloaded every
  5 min) and inject it only after verifying the caller's capability JWT. Sandbox agents reach the
  broker by **pod IP** over the model-only Cilium egress; they never see a token.
- **Kata sandbox executor (ns `agentforge-sandbox`).** Untrusted agent runs execute in ephemeral
  Kata Job pods (scoped SA, no automounted token, zero Secret RBAC), stream their logs live
  (the microVM purges the container log ~8 s after exit), and export a validated workspace tree.
- **PROVEN transactions.** Real **plans** are generated via the anthropic broker (sandbox agent →
  pod-IP → broker → capability-verify → OAuth-inject → Anthropic `200` → valid `{plan_md,
  tests_needed}` envelope), and real **codex cross-review critiques** post through
  `broker-openai-codex` (plan-stage gate → `/v1/responses` → kid-policy PASS → gateway model PASS →
  forward, audited `decision:granted status:200`, e.g. `🔀 cross-review [plan] round 1`).
- **KEDA scale-to-zero** over the planner worker (ScaledObject `af-orch-playground-planner`) scales
  the pool 0↔N on `forge_pending` and holds stably once the forge is healthy (see finding (a)).

### Decisions / findings that emerged during activation

- **(a) Gitea SQLite could not survive the polling load → WAL + busy-timeout (ailab PR #67);
  Postgres is the durable follow-up.** Gitea runs `DB_TYPE=sqlite3` (single-writer) on a
  `qnap-iscsi` block PVC. The AgentForge poll loops (planner + dispatcher + reaper reads plus
  claim/plan/state writes) starved the default 500 ms busy-timeout + rollback journal → Gitea `500`d
  on issue-comment writes → the orchestrator crashed and issues never advanced (it also masked a
  KEDA scale-flap: a missing `forge_pending` sample reads as 0 → scale-to-0). Fix = `WAL` journal +
  `SQLITE_TIMEOUT=10000` (safe on a block PVC), which heals instantly and keeps `forge_pending`
  steady. The **agreed durable follow-up is a CNPG-Postgres migration** (reversible: keep the sqlite
  file, `pgloader`, flip `DB_TYPE`).
- **(b) The codex CLI needs a `/v1` base_url AND a forced, policy-allowed model.** codex uses
  `wire_api="responses"` and POSTs to `{base_url}/responses` WITHOUT prepending `/v1` (unlike the
  claude CLI, which appends `/v1/messages` to `ANTHROPIC_BASE_URL` itself). The broker serves the
  codex route at inbound `/v1/responses`, so the launcher must set `base_url = {broker_url}/v1`
  (agentforge PR #43) or codex `404`s. Separately, codex's default model `gpt-5.6-sol` is OUTSIDE the
  operator **kid-policy** allowlist `{gpt-5.3-codex, gpt-5.5, gpt-5.6}` (a second, OpenBao-published
  model allowlist enforced by the broker on top of the capability `model_set`), so the runner must
  force codex onto an allowed model via `-c model=<job.model>` (agentforge PR #44) with the config set
  to `gpt-5.6` (config PR #4). Without both, the broker returns `403 model-not-allowed` /
  `capability policy rejected`. **The estate codex model is `gpt-6-astra`** (GPT-6 Astra, decided
  2026-09-05): the kid-policy allowlist entitles it (this repo), the sandbox/orchestrator images ship
  codex ≥ 0.153.1 (agentforge #309), and `cross_review.model` + the cross-reviewer role in
  `cchifor/agentforge-config` — the source of truth for what the gate actually sends — move to it
  last (agentforge-config #8). The upstream rejects an older CLI for this model with
  `400 … requires a newer version of Codex`, which is why the image bump must land BEFORE the flip.
- **(c) OpenBao 2.5.5 has DISABLED the legacy `generate-root` flow.** `bao operator generate-root`
  and raw `sys/generate-root/attempt` return **`405 "unsupported operation"`** — there is NO
  root-token recovery from the unseal key on this version. Operator-path writes (the broker OAuth
  seeds, new operator roles) therefore require an **operator-scoped token**, not root; the running
  `agentforge-provisioner` (k8s-auth, ns `openbao`) is the sanctioned operator write path.

### Single remaining open item

The **codex OAuth token must be refreshed and written to OpenBao**
(`af/operator/broker/openai/codex-pro/oauth`, KV v2, mount `af`) to complete the first live
`state:2` transition — the broker grants + forwards correctly, but chatgpt.com currently returns
`401 token expired` (a stale credential, NOT a code/model bug). The **durable fix is creating the
`af-codex-refresher` OpenBao role** so the already-deployed `af-codex-refresh` CronJob
(ns `agentforge-broker`, SA `af-codex-refresher`) can self-rotate the ~10-day token; it presently
fails `HTTP 400` because that operator role is missing (a bootstrap-sentinel gap — see finding (c),
it rides the same operator-token / provisioner re-run path). Everything upstream and downstream of
this credential is proven.

## Update (2026-07-22) — CI runners = HOST-MODE (P2 "k8s-native CI runners" override)
The §Phasing **P2 "k8s-native CI runners"** item (a KEDA `ScaledJob` on the Kata pool) is **RESOLVED by
reusing the existing host-mode Gitea `act_runner` pool** (ADR 0013 VMs, ADR 0017 forge migration —
`ci-runner-1..5`, vmid 4101–4105, live IPs .14–.18, label `self-hosted-hv:host`), **not** a new k8s-native
runner. Rationale: the AgentForge image builds run **privileged host docker** (`agentforge/deploy/*.
Dockerfile`), and the Kata/gVisor sandbox pool **deliberately cannot host privileged DinD** (this ADR:
DinD "must fail closed onto Kata nodes … never silently fall back to gVisor, which can't host privileged
DinD"); a k8s-native runner would duplicate a proven pool and still could not do the one job Stage 0 needs.
The pool builds `orchestrator` (= the OpenBao init/unseal/provision Jobs + broker + provisioner image),
`sandbox`, and `p1-worker` via `cchifor/agentforge:.gitea/workflows/images.yml`, which `ailab: just
pin-bootstrap`/`pin-workloads` then digest-pin. Fitness is gated by **PREFLIGHT #2**
(`just ci-runners-preflight` → `scripts/check-ci-runners.py`; the runner-pool sibling of
`just nested-virt-verify`). Full detail + the Gitea org-config/token/security prerequisites:
`docs/runbooks/ci-runners.md` §8.

## Update (2026-07-24) — self-service CP product layer + subscription lifecycle (SHIPPED)

The P1 control plane grew a full self-service product surface, delivered as four codex-gated
agentforge-platform PRs (#10 shell/Overview/logout/CSRF · #11 templates/multi-repo/Gitea-PAT/
onboarding · #12 AG-UI chat wizard · #13 LLM-subscriptions settings) plus Stage-0 companions
(agentforge #52, ailab #103) — all deployed. Decisions of record:

- **Templates over hand-edited config**: a server-side versioned registry renders the full
  AgentForgeConfig; subscription-engine templates are entitlement-gated to tenant-zero
  (`engines_for_org`); every template is contract-tested through `bind_workspace_policy`.
- **Multi-repo workspaces**: bindings store canonical `(gitea_host, repository_id)` with a GLOBAL
  one-active-workspace-owner index; config `repos` must be ⊆ the bound set. Runtime access is
  ENFORCED via per-repo bot collaborator grants made with the user's PAT at onboarding (the PAT is
  Fernet-encrypted per `(org,user,host)`, used only through a 5-op allowlisted adapter).
- **Chat wizard = deterministic executor**: AG-UI SSE (pinned client 0.0.51) with HITL option
  buttons; every mutation option is an opaque server-issued single-use `option_id` (atomic claim,
  role re-check at execution). The local-LLM free-text path (litellm qwen3.6) has ZERO mutation
  ability by construction.
- **Subscription lifecycle (operator creds)**: CP reads STATUS only via OpenBao **metadata**
  (role `af-cp-sub-status`); rotations CAS-write via `af-cp-sub-rotator` (server-side
  `cas_required=true`; neither role can list/read-data/delete). Status is generation-consistent
  (custom_metadata `credential_version` must equal the KV version — else UNKNOWN/PENDING, never
  ACTIVE). Rotation completion is OBSERVED via each broker's `/readyz` `credential_generation`
  (sha256[:8] of the mounted cred; broker CNPs admit the CP read-only on :8700 — ailab #108).
  "Refresh now" (codex) = Job-from-CronJob under a ValidatingAdmissionPolicy that pins the Job
  shape to the reviewed refresher.
- **Broker add/remove is PR-GATED GitOps, not CP-writable state**: the CP renders the existing
  per-account manifest shape and opens an ailab PR via the restricted `agentforge-infra-bot`
  using **AGit** (`refs/for/main` — READ collaborator, no branch write). ailab `main` is
  branch-protected (push whitelist {chifor, gitea_admin}, 1 required approval, dismiss-stale);
  `agentforge-reviewer-bot` (restricted, write collaborator) supplies the non-author approval
  after the review gate. Async operations carry states
  `awaiting_review → merged_waiting_flux → waiting_eso → verifying_broker → active`.
- **OpenBao operational reality** (see `docs/runbooks/openbao-recovery.md`): 2.5.5 disables BOTH
  `generate-root` and `operator rekey`; the only privileged-recovery/seal-rotation path is the
  wipe+re-bootstrap ceremony (executed twice on 2026-07-24: once to land the af-cp roles, once to
  rotate the seal after a brief escrow exposure — leaked objects purged, escrow re-encrypted
  Secret-shaped).

## Context

AgentForge v1 (ADR 0018) works but is IaC-manual: agents run as host systemd units on 6 dev-worker
VMs under one `c4` UID, driven by a hand-edited config repo, with **no self-service, no real
isolation of agent-generated code, no dynamic sizing, and no UI beyond a dashboard**. v1's own threat
model calls it *playground-only, enforced in code* until a v1.1 hardening gate (dedicated user +
containerized `test_cmd`).

v2 turns it into a **self-service platform**: a user logs in, connects **Infrastructure** (a k8s
cluster; the platform sizes + provisions sandboxed workers/runners), creates **Workspaces** (repos +
keys in a real vault), and gets a **Monitoring** dashboard. **The data/auth model is multi-tenant with
RLS + org-membership from P1**; only the *compute + secrets vault* start as tenant-zero (the ailab
cluster) and generalize in P2/P3. v1 is reused, not rewritten.

## Decision

Build the control plane as `cchifor/agentforge-platform` (FastAPI OIDC RP + webapp + resource
analyzer + GiteaClient + reconcilers), running on the hub in ns `agentforge`, with the compute on a
**dedicated Kata-capable Talos node pool**, secrets in **OpenBao + ESO** (P2), and provisioning via
**GitOps** behind BOTH a server-side write scope AND an admission gate. The governing principles:

1. **The security boundary is the POD, not the container.** Untrusted agent + `test_cmd` execution
   runs in a **separate ephemeral Kata sandbox pod** with its own scoped SA
   (`automountServiceAccountToken: false`, zero Secret RBAC), a default-deny + model-only
   CiliumNetworkPolicy, and no path to the orchestrator's creds, forge route, or Docker API. This is
   what actually satisfies ADR 0018's v1.1 gate (proven by boundary tests). **P2** delivers it; P1
   makes no sandbox-hardening claim.

2. **The web app writes desired state; a reconciler converges it — behind a server-side write scope
   AND an admission gate.** The CP bot pushes ONLY to a **separate `cchifor/agentforge-tenants`
   Gitea repo** (Gitea repo-level permission enforces this server-side — the bot has **no write to
   `cchifor/ailab`**, where the root Flux config, the tenant Flux Kustomization, the reconciler
   RBAC, and the admission policies live). In ailab:
   - a **Flux `GitRepository`** (`clusters/ai/agentforge-tenants-source.yaml`) clones
     `agentforge-tenants` over SSH with a **read-only deploy key**;
   - a **per-tenant `Kustomization`** (`clusters/ai/agentforge-tenants.yaml`, `wait: false` so tenant
     churn can't wedge the apps layer, `prune: true`) reconciles `tenants/`, **impersonating a
     dedicated minimal-RBAC SA** (`agentforge-tenants-reconciler`: enumerated namespaced GVKs only —
     no ClusterRole/-Binding, no wildcard verbs, no Flux source objects);
   - a **ValidatingAdmissionPolicy** (`apps/agentforge/admission/tenant-guard`) rejects anything that
     SA applies outside the tenant GVK/field allowlist.
   So even a fully compromised CP can neither redirect the reconciler nor commit cluster-admin: its
   token physically cannot reach the root/admission paths, and admission rejects out-of-allowlist
   objects. A second VAP (`cp-flux-guard`) field-locks the CP's own `patch` grant on Flux
   Kustomizations (no `spec.sourceRef`/`serviceAccountName` change) — closing the ADR 0015 gap.

3. **Multi-tenant data with RLS from the first migration.** New `agentforge_platform` DB/role on CNPG
   `infra-pg`. `postInitSQL` does NOT run on the already-bootstrapped cluster → a **migration Job**
   (platform `migrate` = schema + RLS) + a **superuser bootstrap SQL** (role/DB create) is the
   provisioning path; RLS (`SET LOCAL app.current_org`) is on from migration #1, with tests for the
   bypass traps (missing-SET, background jobs, pooled connections, ingest endpoints, a dedicated
   audited non-RLS admin role). This is application-layer; the only infra change is the DB/role +
   the 5Gi→10Gi PVC bump.

4. **Tightened hub read access — NOT Headlamp's wildcard.** The analyzer reads
   nodes/pods/metrics/workload-status via `agentforge-cp-readonly` (enumerated, **no `secrets`
   read**); the only write is `patch` on Flux Kustomizations to nudge reconcile
   (`agentforge-cp-flux-safeops`), field-locked by `cp-flux-guard`. NetworkPolicy admits only
   cloudflared.

5. **Every boundary is enforced in code + admission, never UI-only** (engine gating, org membership,
   tenant scoping, allowed-GVK).

### Infrastructure decisions specific to this ailab PR (P1)

- **Dedicated Talos agent node pool** — new `kubernetes/infra/agent-nodes/` module: 3 Talos
  **worker** VMs (`.47–.49` live — originally planned `.14–.16`, renumbered into the CI-runner-vacated
  block once the runners moved to `.14–.18`; the `agent-nodes/variables.tf` map is the source of truth —
  vmids `4301–4303`) that JOIN the existing `ai` cluster, labelled
  `ailab.io/agent-pool` and tainted `dedicated=agent`. **Machine-secrets: chosen Option B over the
  spec's recommended Option A.** A Talos worker MUST reuse the existing cluster `machine_secrets`
  (a fresh `talos_machine_secrets` forks the PKI and never joins). The spec recommends **Option A**
  (fold the pool into the CP-critical `infra/` root module for a direct `talos_machine_secrets.this`
  reference). We chose **Option B** (separate `agent-nodes/` module that reads `machine_secrets` +
  `client_configuration` **read-only** from `infra/` state via `terraform_remote_state` + two new
  sensitive outputs) because it (a) honours the repo's load-bearing convention that **every non-CP
  VM class is a separate root module with separate state** specifically to never plan/mutate the
  CP-critical `infra/` module (documented in `dev-workers/backend.tf`), (b) matches the plan's stated
  `agent-nodes/` directory + the `just agent-nodes-plan/apply` recipe, and (c) is equally
  join-correct. Option B's one cost — adding the two sensitive outputs to `infra/outputs.tf` and a
  one-time `tofu apply` in `infra/` to expose them — changes **no** infrastructure (outputs don't
  touch the CP VMs; `for_each` keys are stable, no CP reboot).
- **P1 pool is PLAIN (no Kata/gVisor).** Kata & gVisor are Talos **system extensions** baked into the
  boot image via the Image Factory (`talos_extensions`), NOT machine-config patches — **P2**. The
  `worker.yaml.tftpl` carries the P1 label + taint and keeps the P2 `vhost_net`/`vhost_vsock` kernel
  modules commented. `cpu.type=host` is set so P2 adds Kata to the SAME VMs without a reshape.
- **Nested virtualization is a NEW Proxmox-host prerequisite** (`kvm_amd nested=1`, configured
  nowhere in the repo today). It is an **operator step** (Ansible `pve_base` or `node-ssh.py`),
  documented in `docs/runbooks/agent-nodes.md`, **not applied by this PR**. Without `/dev/kvm`, Kata
  fails; DinD/sandbox scheduling must **fail closed** onto Kata nodes (never silently fall back to
  gVisor, which can't host privileged DinD).
- **Exposure is Access-free.** `agentforge.chifor.me` → cloudflared → CP. NO Cloudflare Access app:
  the CP does its own Authelia OIDC + per-route org enforcement, so an Access gate would double-login
  (estate convention: Gitea, Vaultwarden apex are Access-free).
- **kro is treated as P2.** The plan's P1 "CRs → kro materializes" is resolved by making the P1
  tenant manifests **plain hand-authored** (ns/SA/Deployment/RBAC/NetworkPolicy committed to the
  tenant repo); the kro RGD that DRYs the expansion arrives in P2 (spec §0.2 option B).

## Threat model (explicit)

- **Compromised CP.** Its Gitea token can write ONLY `agentforge-tenants`; it has no write to `ailab`
  (root Flux + admission). Its k8s SA is read-only-minus-secrets + `patch`-on-Flux-Kustomizations,
  field-locked. Even a full RCE cannot redirect the reconciler, mint cluster-admin, or read Secrets.
  Required tests: malicious-manifest rejection + write-outside-tenant-repo rejection.
- **Malicious tenant manifest.** Bounded by two independent layers: the reconciler SA's minimal RBAC
  (enumerated namespaced GVKs, no cluster RBAC) AND the `tenant-guard` VAP allowlist (GVK/field). A
  tenant path cannot create a ClusterRole, a wildcard-verb Role, a Flux source (redirect), or (P1) a
  privileged pod.
- **Untrusted agent/`test_cmd` (P2 sandbox).** Separate Kata pod, no Secret RBAC, no automounted
  token, model-only egress, no orchestrator Docker-API reachability; brokered ephemeral inference
  creds + outbound redaction close the readback-exfil channel before the v1.1 flip.
- **RLS bypass.** Tested from P1 (missing-SET, background jobs, ingest, admin/service paths).
- **Subscription OAuth.** `claude_max` is tenant-zero-only, enforced at API + config + worker +
  admission; external tenants get per-tenant LiteLLM virtual keys with budget caps (P3).

## Verification — the sandbox-boundary CANARY suite (the v1.1 gate proof)

The threat model above is only as good as its proof. A **repeatable, adversarial boundary canary
suite** is the STANDING evidence that flipping `privilege_hardening: v1.1` (agents on real repos) is
safe. It has two tiers and a live matrix; a **green suite is the gate**, and **any breach is a P0**
(stop, do not flip/keep v1.1, escalate with the reproduction).

- **Unit / CI tier (always-on, `cchifor/agentforge`).** `tests/unit/test_sandbox_boundary.py`
  renders the exact Job the orchestrator submits — for BOTH trust classes, and even with the caller
  env stuffed with every credential shape — and locks the bytes that reach the apiserver: the
  three-tier credential split (orchestrator holds the PATs/OpenBao SA/signing key; the **agent** tier
  carries only the broker ROUTE + the capability **file path** — never a bearer in env/argv; the
  **test/setup** tier carries NOTHING — no creds, no broker wiring, no capability), `runtimeClassName:
  kata`, the tokenless SA + `automountServiceAccountToken:false`, only workspace/home volumes (no
  Secret/projected/hostPath), no host namespaces, and no privileged/added-caps/unmasked-proc surface
  (the per-pod-DinD sidecar design is **superseded** by ephemeral per-job Kata Jobs — there is no
  DinD and no cap to nest one, so "privileged DinD escapes the microVM" is unconstructible).
- **Live tier (activation-time, `admin@ai`).** `scripts/verify-sandbox-boundary.sh` (this repo) runs
  the matrix against the ACTUAL flipped sandbox on a **disposable** throwaway `tenant-zero/playground`
  workspace (it HARD-REFUSES `platform-dev`, which drives the real repos) and tears the canaries down.
  It asserts: (i) the static boundary (kata RuntimeClass, both egress CNPs + default-deny, separate
  restricted-PSA ns, tokenless SA with no RoleBinding); (ii) **negative admission** — the pinned
  pod-shape VAPs REJECT `privileged`, `automountServiceAccountToken:true`, `hostNetwork`, a **non-kata
  runtimeClass**, and a `hostPath` volume (so Kata + the credential-free shape are ENFORCED, not
  merely what a compliant canary happens to use); (iii) the **egress matrix EXACTLY** via two Kata
  canaries — the agent tier reaches ONLY its pool broker (a **positive** reachability control, so a
  netless pod can't false-pass) and is denied world/metadata/alt-DNS/other-pool-broker/**OpenBao**,
  while the test tier has **zero egress** (even the broker unreachable = the `test_cmd --network none`
  equivalent); (iv) `uname -r` inside the sandbox = the **Kata guest kernel**, provably different from
  the host node's kernel; (v) `/proc/1/environ` carries no orchestrator credential (separate PID/mount
  namespace). The pytest companion `tests/integration/test_sandbox_boundary.py` re-checks the deployed
  egress policy shape (endpoint-identity broker allow + the three per-account broker DNS names, OpenBao
  outside the allowlist) and the same live matrix under `AF_BOUNDARY_LIVE=1`.

**Live matrix result (2026-07-24, `admin@ai`, playground):** every vector PASSED = blocked/denied/proven
(23/23) — the sandbox pod ran `runtimeClassName=kata` with guest kernel `6.12.42` vs host
`6.12.48-talos` (real microVM isolation), the agent reached ONLY its broker, and the test tier had
zero egress. See `docs/runbooks/sandbox-boundary-canary.md` for the full matrix, how to run it, and
the P0 procedure.

## Rejected / out of scope (P1)

- **OpenBao / ESO / KEDA / kro operators** — P2 (`kubernetes/apps/infrastructure/{security,autoscaling}`).
- **RuntimeClasses `kata`/`gvisor` + the Kata/gVisor image extensions + the SandboxExecutor** — P2
  (need nested virt + the extension image + node handlers first).
- **Cloudflare Access in front of the CP** — deliberately omitted (own OIDC; double-login).
- **Folding the agent pool into `infra/` (Option A)** — rejected in favour of the separate-module
  convention (see above).
- **Wiring the migration Job into Flux** — it is an operator-run one-shot (a failing migration under
  the `wait: true` apps Kustomization would wedge the apps layer + everything that dependsOn it).

## Consequences

- New `kubernetes/infra/agent-nodes/` module + `just agent-nodes-plan/apply` + two sensitive outputs
  on `infra/`. New inventory band (.47–.49 / 4301–4303, live; planned .14–.16). New host prereq (nested virt, operator).
- `infra-pg` gains the `agentforge_platform` DB/role + a 10Gi PVC; one Authelia OIDC client + pod
  restart; a new cloudflared route + CNAME.
- New `agentforge` app (ns/CP/RBAC/NetworkPolicy/admission) + the `agentforge-tenants` Flux source +
  per-tenant Kustomization + reconciler bootstrap. The CP bot must be provisioned with a **write**
  token to `agentforge-tenants` only, and a **read-only deploy key** installed for the GitRepository.
- **Operator follow-ups (P1 go-live):** stage the Talos image + expose the `infra/` outputs + apply
  `agent-nodes`; generate the real Authelia client secret (or keep the committed pair); create the
  role/DB as superuser + run the migration Job; fill the real deploy-key + `known_hosts`; pin the
  real CP image digest; seed `tenants/` in `agentforge-tenants`; roll `deploy/authelia`.

## Phasing

- **P1 (this slice):** CP app + Authelia OIDC + Postgres (RLS + migration Job) + the tightened read
  SA + plain-manifest tenant path behind the admission policy + per-tenant Flux Kustomization + the
  dedicated agent node pool (plain, no Kata) + Access-free exposure. One shadow orchestrator proves
  OIDC login → create workspace → git commit → Flux applies → config fetch → 1→2 transition. **No
  sandbox-hardening or full multi-tenant-compute claim.**
- **P2 (the unlock):** OpenBao + ESO + KEDA + kro; secrets → OpenBao; Kata node extensions +
  RuntimeClasses; the ephemeral sandbox-pod SandboxExecutor + admission-pinned pod shape + per-pod
  Cilium egress + brokered ephemeral inference creds + outbound redaction + the epoch-safe account
  lease + k8s-native CI runners; kro RGD DRYs the tenant expansion. Boundary tests green → flip
  `privilege_hardening: v1.1`.
- **P3:** external clusters (pull model, read-only deploy keys, hostile-payload handling); per-org
  quotas; per-tenant LiteLLM virtual keys w/ budget caps; dogfood.
