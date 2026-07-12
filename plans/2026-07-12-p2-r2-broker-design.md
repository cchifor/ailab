# P2 R-2 — model-gateway broker + credential split + isolated OpenBao provisioner

## Context
R-1 (merged) delivered the sandbox boundary CORE: untrusted code runs in a separate ephemeral Kata Job
(tokenless SA, restricted PSA, **zero egress** — R-1 exercises test/setup jobs + the import path, NOT
live agent runs). R-2 is "creds/model": it lets the **agent** profile actually call the model **without
ever putting the durable inference OAuth in the sandbox**, by routing the sandbox through a standalone
**broker** that holds the OAuth. It also splits the platform's credentials into three disjoint trust
tiers and gives per-tenant secret provisioning an **isolated authority** (the CP must not hold OpenBao
root). R-2 does NOT flip `privilege_hardening: v1.1` (that is gated on the 6 boundary proofs + the
image-build activation); it makes the agent path exist behind the boundary.

Binding constraints still in force: engines = subscriptions + local models only (litellm = local models
only; the broker fronts the **subscription** inference OAuth). codex cross-review of design/impl/review
until alignment, cap 3.

## Approach

### 1. The three-pod trust topology (R-2 makes the broker real)
- **orchestrator** pod (trusted): forge PATs + CP bearer + git-push; creates/reaps sandbox Jobs; does the
  trusted-checkout export + forge writes. Mounts `orchestrator-creds` ONLY. Never holds the inference
  OAuth; never runs the gateway. (R-1, unchanged.)
- **broker** pod (trusted-but-ISOLATED): a NEW `Deployment` + `ClusterIP` Service, its own namespace or a
  dedicated SA in the sandbox-adjacent ns. Mounts `broker-oauth` ONLY (the durable inference credential).
  No forge/CP/OpenBao access, no git, no filesystem sharing with the orchestrator. RBAC: none beyond its
  own pod. Runs the **model gateway** (below).
- **sandbox** Job (untrusted): agent profile gets egress to the **broker ClusterIP + DNS ONLY** and a
  **per-job broker capability** (a short-TTL token in a projected/emptyDir mount) — NOT a durable cred.
  test/setup profile keeps R-1's zero egress. Mounts: job subPath + writable home + (agent only) broker-cap.

### 2. Model gateway broker (durable OAuth never in the sandbox)
A **standalone application-layer gateway** (NOT a generic HTTP/TCP proxy, NOT a sidecar). A new service in
the `agentforge` repo (`src/agentforge/broker/`), shipped in the SAME worker image (subcommand
`agentforge broker`), deployed by the operator (ailab) as its own Deployment.
- **Request reconstruction**: hard-code upstream scheme/host/port + verified TLS (pinned CA); reconstruct
  each upstream request from a validated ALLOWLIST of path/method/headers. Reject CONNECT, absolute-form
  targets, redirects, proxy/hop-by-hop headers, conflicting Content-Length/Transfer-Encoding, unsupported
  upgrades, arbitrary upstream headers. The sandbox agent CLI is pointed at the broker base-URL; the
  broker injects the real `Authorization` from `broker-oauth` (the sandbox never sees it).
- **Per-job capability**: the orchestrator mints a short-TTL, **one-job / one-model** capability (signed
  token bound to `job_id` + model + expiry + a single-use nonce, HMAC or the OpenBao transit engine — TBD
  in review). The broker verifies valid + unexpired + **unreplayed** (nonce cache / one-shot). Cilium
  additionally restricts broker ingress to sandbox pods carrying that job's Cilium identity. Layered:
  network identity (Cilium) + application capability (broker), so neither alone is the boundary.
- **Bounds (fail-closed, per job/account)**: request/response size, model set, token budget, concurrency,
  rate, duration, and **spend**. Readiness + capacity so the broker is not an unbounded SPOF (bounded
  queue, load-shed, HPA-or-fixed-replicas TBD). Audited: every request → an audit event (job, model,
  tokens, decision) with NO secret/payload.
- **Preflight (do FIRST, before building)**: verify Claude Code (`CLAUDE_CODE_...`/`ANTHROPIC_BASE_URL`)
  and Codex actually support pointing at a custom base-URL with the broker's streaming + auth behavior.
  If a CLI won't accept a broker base-URL, the design must adapt (apiKeyHelper, or a per-CLI shim). This
  is the single biggest unknown — the review MUST confirm the CLI contract before Phase B.

### 3. Credential split (three disjoint secrets, ESO-provisioned)
- `orchestrator-creds` (forge PATs + CP bearer + git-push) → **orchestrator** ns/SA only. (R-1.)
- `broker-oauth` (the subscription inference OAuth: `CLAUDE_CODE_OAUTH_TOKEN`, codex `auth.json`) →
  **broker** ns/SA only. Codex's auth.json auto-refreshes → a writable `emptyDir` seeded by an init
  container from the ESO Secret, not an RO mount (per the design's recorded caveat).
- sandbox → **nothing** (agent profile: only the per-job broker capability; test/setup: nothing).
- Each secret is a per-consumer ESO `ExternalSecret` from a per-tenant OpenBao path; NO secret is
  reachable by a pod outside its tier (enforced by ns/SA + the sandbox-guard VAP forbidding secret volumes
  + no env valueFrom).

### 4. Isolated OpenBao provisioner (the CP must not hold OpenBao root)
The CP writes DESIRED state (git); it must NOT have broad OpenBao admin. A **scoped provisioner** creates
per-tenant OpenBao auth roles + policies bound to exactly `af/<org>/<workspace>/*` + the tenant eso-auth
SA, with authority limited to that subtree (no cross-tenant, no root). Options to weigh in review:
(a) a small operator/controller with a narrowly-scoped OpenBao token (policy: create child policies +
k8s-auth roles ONLY under `af/<org>/<ws>/*`); (b) OpenBao's own templated policies + a per-tenant
`kubernetes` auth role created by a Job the CP renders but an operator-owned controller applies. Either
way: the CP's git commit must not be able to escalate OpenBao authority; the provisioner is operator-owned
(ailab) and CP-unwritable, mirroring the admission split. Per-tenant isolation = namespaced SecretStore +
OpenBao role pinned to ns+SA + policy scoped to the subtree + NetworkPolicy (workers reach only ESO/OpenBao).

### 5. Cilium (broker ingress/egress + agent-profile egress)
- **broker ingress**: ONLY from sandbox pods carrying the matching job Cilium identity (label selector +
  the capability at L7). Deny everything else.
- **broker egress**: ONLY to the pinned model upstream (FQDN/CIDR) + DNS. No cluster-internal reach.
- **sandbox agent profile egress**: ONLY the broker ClusterIP + cluster DNS. Direct-IP / IPv6 / alt-DNS /
  metadata / node-local / service-CIDR all DENIED (belt to the broker's own request reconstruction).
- **sandbox test/setup profile**: ZERO egress (R-1, unchanged).

### 6. Integration with R-1 (what changes in the merged code)
- **agentforge**: add `src/agentforge/broker/` (the gateway app + `agentforge broker` subcommand);
  extend `build_job_manifest` so the **agent** profile adds the broker-cap volume/mount + the broker
  base-URL env + points the agent CLI at it (test/setup unchanged); capability minting in the orchestrator
  run path (before creating an agent Job). Keep LocalExecutor + all existing tests green.
- **agentforge-platform (renderer)**: render the broker `Deployment` + `ClusterIP` + `broker-oauth`
  ExternalSecret + the agent-profile broker wiring env, gated by `privilege_hardening == v1.1` (dormant
  until the flip). Extend `assert_allowlisted` for the new GVKs (Service). Cross-repo test as in R-1.
- **ailab**: the broker Deployment/Service/NetworkPolicies + the isolated OpenBao provisioner (operator-
  owned) + Cilium policies + the broker-oauth secret plumbing; sandbox-guard VAP already allows the
  broker-cap volume (R-1) — verify the agent-profile shape matches.

## Critical files
- agentforge: NEW `src/agentforge/broker/{gateway,capability,bounds,audit}.py`, `main.py` (`broker`
  subcommand + agent-profile capability mint), `adapters/exec/sandbox.py` (agent-profile broker wiring),
  `infra/settings.py` (broker/upstream/capability knobs), `deploy/` (broker runs from the worker image).
- agentforge-platform: `adapters/gitops/renderer.py` (broker Deployment/Service/ExternalSecret + agent
  env), `settings.py`, tests.
- ailab: `kubernetes/apps/.../agentforge-broker/**` (Deployment/Service/NetworkPolicy/ESO), the isolated
  OpenBao provisioner (operator-owned), Cilium policies, `broker-oauth` ESO/SecretStore.

## Verification (R-2 boundary proofs — extend the 6)
- The sandbox agent profile can reach the model ONLY via the broker; a hostile agent CANNOT read the
  inference OAuth (not mounted), CANNOT reach the upstream directly (Cilium), CANNOT replay/forge a
  capability (broker nonce + Cilium identity), CANNOT reach forge/OpenBao/CP.
- The broker rejects CONNECT/absolute-form/redirect/smuggling/arbitrary-header/oversize/over-budget/
  over-rate/expired-capability; scanner/verify failures fail closed; the broker cannot read
  orchestrator-creds and the orchestrator cannot read broker-oauth.
- The isolated provisioner cannot create OpenBao authority outside `af/<org>/<ws>/*`; a CP git commit
  cannot escalate OpenBao; cross-tenant secret read is denied (RLS × ns × OpenBao policy).
- Preflight PROVEN: Claude Code + Codex work against the broker base-URL with streaming + injected auth.
- All existing R-1/P1 tests stay green; the flip stays gated (R-2 renders dormant until v1.1).

<!-- codex-review-status: pending -->
