# P2 R-2 — model-gateway broker + credential split + isolated OpenBao provisioner

## Codex Review
- **CLOSED — 1 Ownership:** broker, broker OAuth, provisioner, policy, and admission objects are operator-owned; the CP only needs its existing ConfigMap/render authority for non-secret references, so no tenant-path widening is required.
- **PARTIALLY-CLOSED — 2 Capability delivery; 6 merged-code fixes:** the two-volume VAP shape is coherent, and the identified runner/router/env fixes are correct, but shared-NFS tenant isolation, reserved-file handling, copied-capability revocation, stdin transport, and typed path translation remain unspecified.
- **PARTIALLY-CLOSED — 3 Capability protocol; 7 ledger/capacity; 8 Cilium:** asymmetric signing, operator ceilings, shared atomic reservations, and the network-policy split are sound directions. Broker-visible pod identity, signer-to-tenant binding, session closure, reservation recovery, HA placement, and refresh-related egress remain unresolved.
- **OPEN — 4 ESO wildcard; PARTIALLY-CLOSED — 5 provisioner:** `af/data/broker/...` still overlaps the allowed tenant template when a valid tenant uses `org=broker`; merged tenant admission also leaves critical ESO names/roles/SAs unpinned. Immutable synthesized bodies address confused-deputy escalation, but lifecycle ordering, ownership, collision handling, and signing-key lifecycle are incomplete.
- **PARTIALLY-CLOSED — 9 Preflight:** the real-Kata recording test is valuable, but a fake broker cannot prove real provider OAuth use/refresh, and the gate currently requires unimplemented R-2 SandboxExecutor/VAP changes before “ANY Phase-B build.” Exact auth/capability suppression belongs in R-2; generic publish-sink redaction remains R-3.

<!-- codex-review-status: complete -->

## Context
R-1 (merged) delivered the sandbox boundary CORE: untrusted code runs in a separate ephemeral Kata Job
(tokenless SA, restricted PSA, **zero egress** — R-1 exercised test/setup jobs + the import path, NOT
live agent runs). R-2 = "creds/model": it lets the **agent** profile call the model **without the durable
inference OAuth ever entering the sandbox**, by routing the sandbox through a standalone **broker** that
holds the OAuth. R-2 also (a) fixes real merged-code gaps that block live agents (Codex stdin + host→
`/workspace` path translation; the `litellm_local` route; a typed exec contract; an env allowlist), (b)
splits credentials into three disjoint tiers with **no ESO wildcard escape**, and (c) gives per-tenant
secret provisioning an **operator-owned isolated authority** (the CP never holds OpenBao root).

This round-2 design resolves codex Phase-A round-1. Binding constraints unchanged (subscriptions + local
models only; codex cross-review to alignment, cap 3).

### R-2 deployment/exercise model (not merely "rendered dormant")
The 6 boundary proofs need a live v1.1-shaped target, so R-2 is exercised in a **shadow deployment**: an
operator-applied, non-tenant-facing shadow of the broker + one playground agent Job, run against the real
Kata node pool with the digest-pinned image, BEFORE the production `privilege_hardening: v1.1` flip. The
CP tenant render path stays gated (dormant) until the flip; the shadow is operator-owned (ailab) and is
where the preflight + boundary proofs actually run.

## Approach

### 1. Ownership map (single authoritative owner per object)
The broker, its OAuth, the OpenBao provisioner, and all Cilium/admission policy are **operator-owned
(ailab), CP-UNWRITABLE**, mirroring the R-1 admission split. The CP renderer contributes ONLY
non-secret, validated *references* (the broker's in-cluster URL + the model/route/budget policy for a
pool) into the orchestrator's config — never the broker workload, never a Secret, never a capability.

| Object | Owner | Notes |
|---|---|---|
| broker Namespace + Deployment + Service + SA | ailab (operator) | dedicated ns, `automountServiceAccountToken:false`, default-deny netpol, pinned admission, no CP/reconciler write |
| `broker-oauth` Secret (ESO) + its SecretStore/auth-role | ailab (operator) | distinct OpenBao path + auth role NOT readable by any tenant SecretStore (see §4) |
| OpenBao provisioner controller + templates | ailab (operator) | synthesizes policy/role bodies from immutable templates (see §5) |
| Cilium policies (broker ingress/egress, agent egress) | ailab (operator) | §6 |
| capability keypair (public half) | ailab (operator) → broker | broker holds only PUBLIC verify keys (`kid` set) |
| capability keypair (private half) | orchestrator (ESO, in `orchestrator-creds`) | orchestrator signs; see §3 |
| broker URL + per-pool route/model/budget policy | CP renderer → orchestrator config | non-secret references only |

### 2. Three-pod trust topology
- **orchestrator** (trusted): forge/CP/git-push creds; creates/reaps Jobs; trusted-checkout export; forge
  writes; **mints capabilities** (signs with its private key — see §3). Mounts `orchestrator-creds` only.
- **broker** (trusted-but-ISOLATED): a minimal dedicated **broker image target** (NOT the CLI-heavy
  sandbox image nor the orchestrator image — no git/k8s-client/dashboard/agent-CLI, minimizing the
  dependency surface around the durable OAuth). Own operator ns, dedicated SA (token automount off),
  default-deny netpol. Mounts `broker-oauth` only. Holds only PUBLIC capability-verify keys. Runs the
  model gateway (§ below). **One broker Deployment per (provider, account)** — the account/provider is a
  fixed property of the broker instance, NOT a request field, so a sandbox can never select another
  account by mutating a request; the capability's `aud` names exactly one broker instance.
- **sandbox** Job (untrusted): agent profile → egress to that broker's ClusterIP + **DNS restricted (L7)
  to the broker name only**; capability delivered as a file in the workspace (see delivery below).
  test/setup → R-1 zero egress. Volumes: **just `{workspace, home}`** — the broker-cap emptyDir is
  REMOVED (it was un-populatable), so the agent and test profiles now share the same 2-volume shape.

**Capability delivery (resolves the un-populatable emptyDir blocker):** the orchestrator writes the signed
capability as a file `<job_dir>/.af/broker-cap.jwt` **into the workspace subPath on shared NFS BEFORE
creating the Job** (the workspace mount already exists in the merged VAP; no init container, no projected/
secret volume, no VAP change to add a producer). The agent CLI is pointed at it via
`AF_BROKER_CAP_FILE=/workspace/.af/broker-cap.jwt` (env is a non-secret path). The capability is
intentionally readable by hostile sandbox code (it is short-TTL, single-job, single-account, quota-capped),
BUT it must never leave the sandbox: `.af/` is added to the import SKIP set so it is never imported back,
and a boundary proof asserts it never appears in imported files, argv, published logs, or headers sent
elsewhere. This removes the broker-cap volume entirely; the sandbox-guard/job-guard VAPs are simplified to
the 2-volume shape (a SMALLER attack surface, changed in the operator repo atomically with the agentforge
manifest contract).

<!-- codex: round-2: The merged pod/job guards will admit the proposed shape once both broker-cap branches are removed atomically, but the actual merged storage contract is not tenant-isolated. The operator PVs expose the same NFS export root, tenant orchestrators mount that root, and the renderer/VAP key sandbox PVC identity only by workspace slug, not org+workspace. Another tenant orchestrator can therefore enumerate other job directories/capabilities, and equal workspace slugs collide. Production R-2 needs server-side per-org/workspace export directories (or equivalent enforced isolation), org-qualified PVC/identity names, and cross-tenant negative tests. Unguessable job IDs and Kubernetes subPath do not constrain another orchestrator mounting the export root. -->

<!-- codex: round-2: `.af/` must be a reserved staging namespace, not copied from the hostile checkout. The merged `stage()` preserves nested symlinks, so a repository-supplied `.af/broker-cap.jwt` symlink can redirect a naive write. Reject a source `.af`, create the directory/file atomically with no-follow/exclusive semantics and restrictive modes, and clean it on every pre-Job failure. Also, skipping `.af/` does not stop hostile code copying the readable bearer token into another imported file or stdout. Close/revoke the ledger session before import/log publication and either suppress exact capability bytes or weaken the claim from “never appears” to “cannot remain usable”; generic redaction can remain R-3. -->

### 3. Capability protocol (session capability + asymmetric signature + operator ceilings)
- **Reusable session capability, not single-use.** Claude Code and Codex make MANY streamed model calls
  per job (incl. helper/compact/aux-model calls), so a single-use nonce is wrong. The capability is a
  **per-job session token** valid for the job's duration with a hard `exp`; the broker enforces
  quotas/rate/concurrency over the SESSION (see ledger). Anti-replay is the ledger's job/session state +
  the Cilium source-identity correlation, NOT a one-shot nonce. (A per-request monotonic sequence + the
  session id is logged for audit/idempotency, but replay across pods is stopped by Cilium identity + the
  session's pod-binding, not by burning the token after one call.)
- **Asymmetric signature (orchestrator signs, broker verifies public-only).** The orchestrator holds a
  PRIVATE signing key (Ed25519, in `orchestrator-creds` via ESO); the broker holds only the PUBLIC key(s)
  with a `kid` header for rotation. This means a compromised broker CANNOT mint capabilities (it has no
  private key), and neither side needs OpenBao transit connectivity (avoids the "no OpenBao access"
  contradiction). Key rotation = publish a new `kid` public key to the broker, switch the orchestrator's
  active `kid`, retire the old.
- **Bound claims** (all signed): `iss` (orchestrator instance), `aud` (exactly one broker instance =
  provider+account), `tenant`+`workspace`, immutable `job_id` (`^[a-z0-9]{32}$`), `pool`/role, the
  ALLOWED model set (small explicit allowlist — capturing Claude Code's helper/compact models, see
  preflight), allowed route/method set, `nbf`/`exp`, and requested quota ceilings.
- **Broker applies OPERATOR-owned ceilings independently.** The trusted orchestrator can request a budget,
  but the broker clamps every claim to operator-configured per-(provider,account) and per-tenant ceilings
  (max tokens, rate, concurrency, duration, model set). The orchestrator can never over-mint authority the
  operator didn't grant. Threat-model note (explicit): the capability + Cilium identity are both
  issued/selected by the orchestrator, so they are independent against a compromised SANDBOX but NOT
  against a compromised orchestrator — the orchestrator is a trusted tier; the broker ceilings + audit are
  the backstop against orchestrator misbehavior.

<!-- codex: round-2: Reusable-session semantics are correct, but the anti-replay binding is not implementable as written. Cilium enforces an endpoint selector; it does not deliver an authenticated Cilium identity/job label to the ASGI broker. The broker normally sees only a source IP, and the design gives it no Kubernetes/Cilium lookup, authenticated proxy header, mTLS identity, or post-scheduling registration protocol. The capability is minted before the Pod IP exists. Define a trustworthy broker-visible binding and session open/close/revoke state; otherwise any allowed agent pod that obtains a token can replay it until expiry/budget exhaustion. A logged sequence that the CLI does not authenticate is not an anti-replay control. -->

<!-- codex: round-2: The key registry must bind each `kid` to an operator-owned issuer, tenant/workspace, broker audience, and ceilings; do not select a tenant ceiling from the token's self-asserted `tenant` claim alone. Keypair generation, delivery, reload, overlap, revocation, and retirement-after-max-TTL are also absent from the provisioner lifecycle. Public-only verification does cryptographically prevent JWT signing by a compromised broker, but it does not prevent that broker from ignoring verification/ledger checks and directly spending its mounted OAuth. State that residual trust explicitly rather than treating “cannot mint” as containment of a compromised broker. -->

### 4. Model gateway broker
A standalone application-layer gateway (not a proxy, not a sidecar), in the dedicated broker image.
- **Exact request reconstruction (JSON-schema, not header allowlist).** Parse the inbound body against the
  provider's exact request schema; reconstruct a fresh upstream request from validated fields only (model
  ∈ capability allowlist; normalized path/query; bounded/known headers only). Reject: CONNECT, absolute-
  form targets, duplicate/control-character headers, conflicting Content-Length/Transfer-Encoding,
  compressed-body bombs (decompression-size limit), any provider field that triggers uploads / tools /
  server-side URL fetches unless explicitly allowed, unknown fields. Strip every inbound credential/cookie
  header before reconstruction; never echo upstream request headers in errors.
- **Upstream client**: redirects DISABLED; pinned CA/SPKI + SNI + hostname verification for the exact
  upstream host(s) (enumerated from preflight); documented rotation; DNS-rebinding-safe (re-resolve +
  re-pin, deny private/link-local/metadata/service/node ranges at the client too, belt to Cilium).
- **Streaming (SSE) bounded protocol**: max event/line/total bytes, idle + wall deadlines, backpressure,
  client-disconnect cancellation (cancel upstream), error-frame handling, decompression limits, and
  **cumulative token/quota accounting that continues after the stream starts** (reserve worst-case at
  dispatch, reconcile from the stream's usage).
- **Auth injection**: the broker injects the real `Authorization` from `broker-oauth`; the sandbox never
  sees it. The broker also OWNS the subscription-OAuth refresh (see §credential split) — it is the single
  writer of its own `auth.json`.
- **Smuggling surface**: if any ingress proxy (Envoy/Service) sits in front of the ASGI server, the
  raw-socket HTTP/1.1+HTTP/2 conformance/fuzz suite runs through THAT path, not just the app test client.

<!-- codex: round-2: Mandatory SPKI pinning for third-party subscription APIs is operationally brittle unless the provider publishes a supported pin set and overlap/rotation contract; an ordinary SaaS certificate rotation would fail-close every agent. CA validation + exact hostname/SNI + fixed reconstructed destinations + Cilium FQDN/CIDR controls are the R-2 boundary. Keep SPKI as an optional provider-supported hardening item rather than an unconditional blocker; broader network hardening belongs in R-3. -->

### 5. Bounds, ledger, capacity, audit (fail-closed, replica-safe)
- **Shared atomic ledger** (NOT broker memory): nonce/session, per-request sequence, rate, concurrency,
  cumulative tokens, and spend live in a small atomic store shared across broker replicas + surviving
  restarts. Candidate = a dedicated Postgres table on the existing CNPG `infra-pg` (the broker gets a
  narrow DB role; adds a broker→infra-pg egress dependency — enumerated in the netpol, §6) OR OpenBao (but
  that contradicts broker isolation) — **Postgres ledger chosen**; review to confirm. **Reserve worst-case
  tokens/spend BEFORE dispatch** so concurrent requests can't overshoot; reconcile actuals from the
  response/stream; **fail closed if the ledger is unavailable** (reject, don't pass through).
- **Enforceable spend unit = TOKENS, not dollars.** Subscription OAuth does not report per-call dollar
  cost, and post-response usage arrives too late to be a hard pre-call bound. The hard bound is a **token
  budget** reserved pre-call from the capability's quota against a trusted model-alias→limit table; a
  best-effort dollar estimate (from a pinned pricing source) is AUDIT-only, never a gate.
- **Capacity (not "TBD")**: fixed replica count (start 2) + PDB; bounded request queue with load-shed
  (429/503 + `Retry-After`); max open streams + connection-pool caps; per-tenant fairness (token-bucket
  keyed by tenant); defined rollout (Recreate or surge with the ledger as the shared source of truth so a
  rolling replica can't double-spend); recovery path when the subscription credential or upstream is down
  (fail closed, readiness false, alert).
- **Audit**: sink = the CP ingest endpoint OR a local structured log scraped by Alloy (chosen: structured
  stdout log + Prometheus counters — no extra egress dependency; the CP ingest is best-effort). Buffered +
  bounded cardinality; sanitized (NO auth headers, capabilities, OAuth-refresh material, upstream bodies,
  or full URLs — only job/tenant/model/decision/token-counts). **Security decisions never depend on audit
  availability** — a full/broken audit buffer drops audit lines but still enforces + serves (or fails
  closed on the ENFORCEMENT store, not the audit sink).

<!-- codex: round-2: “Postgres ledger” is not yet an enforceable replica-safe specification. Define a transactional session/request schema with unique `jti`/request-id semantics, atomic reserve-and-concurrency acquisition, authenticated session state, close/revoke, and expiring concurrency leases for broker crashes. Worst-case reservation must conservatively include validated input tokens plus maximum output/auxiliary usage, not only a model alias limit. Fairness/rate state must be global in the ledger rather than per-replica, and uncertain dispatched requests must retain their reservation. Two replicas plus a PDB is not HA without anti-affinity/topology spread and confirmation that `infra-pg` itself has a failover topology; otherwise a node or database outage remains a service-wide fail-closed DoS. -->

### 6. Credential split + no ESO wildcard escape
- `orchestrator-creds` (forge PATs + CP bearer + git-push + the capability PRIVATE signing key) →
  **orchestrator** ns/SA only, from OpenBao `af/data/<org>/<workspace>/orchestrator` via the tenant
  SecretStore.
- `broker-oauth` (subscription OAuth) → **broker** ns/SA only, from a **DISTINCT operator OpenBao path**
  `af/data/broker/<provider>/<account>/oauth` via a **separate operator-owned SecretStore + auth role**
  that the tenant SecretStore's policy CANNOT read (the tenant role is scoped to
  `af/data/<org>/<workspace>/*`, which does not include `af/data/broker/*`). This closes the wildcard
  escape: no tenant-controlled ExternalSecret can name a key outside its subtree, and tenant-guard is
  tightened (see below) to pin ExternalSecret `target.name` ∈ the allowed set + `key` ∈ the per-tenant
  subtree — so the CP path cannot materialize `broker-oauth`.
- **tenant-guard tightening (operator repo)**: pin ExternalSecret `target.name` to the allowed per-pool
  names, `secretStoreRef.name` to the tenant SecretStore, and the source key to the per-tenant subtree
  ONLY (already partly done in R-1; extend to pin target names). Add NEGATIVE proofs (SubjectAccessReview
  + rejected-create) that the CP reconciler, tenant SAs, the orchestrator SA, and the sandbox Job creator
  CANNOT create/mutate any workload or ExternalSecret referencing another tier's Secret.
- **codex auth.json refresh (single owner)**: the broker is the SOLE writer of its `auth.json`. Seeded by
  an init/entry step from the ESO Secret into a writable `emptyDir`; the broker refreshes in-process with
  a single-writer lock; **on refresh it writes the rotated refresh-token back to OpenBao** (the broker's
  auth role gets write on its own `af/data/broker/<provider>/<account>/oauth` path) so a pod restart
  re-seeds the CURRENT token, not a stale ESO snapshot. Exactly ONE broker replica per account performs
  refresh (leader-elected or a single refresh-owner replica); malformed/partial auth.json fails closed.
  (Whether Codex/Claude subscription OAuth actually supports broker-side refresh is a PREFLIGHT gate.)

<!-- codex: round-2: The broker prefix is not actually disjoint from the tenant template. The identifier regex permits `org=broker`; a tenant with workspace equal to a provider gets policy `af/data/broker/<provider>/*`, which covers `af/data/broker/<provider>/<account>/oauth`. Move tenant data under an unambiguous reserved prefix such as `af/data/tenants/<org>/<workspace>/*` and operator data under `af/data/operator/broker/...`, or at minimum reserve every operator top-level slug in validation and migration tests. Until then the ESO wildcard escape remains OPEN. -->

<!-- codex: round-2: The merged tenant-guard confirms the admission half is still incomplete: `ExternalSecret.target.name` and `secretStoreRef.name` are unpinned; `SecretStore` metadata name, Kubernetes auth role, and serviceAccountRef value are unpinned; source keys are only syntactically checked and are not correlated to the namespace's org/workspace. The revision promises only some of these pins and provides no trustworthy org/workspace map for CEL. Use deterministic names plus an operator-owned VAP parameter/mapping (or equivalent immutable operator metadata), pin the SecretStore role/SA/version and exact source key, and test updates as well as creates. OpenBao ACL containment remains mandatory even after admission hardening. -->

<!-- codex: round-2: Refresh persistence contradicts the isolation and egress design. ESO is read-only from the application's perspective; with token automount off, the broker has no specified credential for writing OpenBao, and §8 does not allow broker→OpenBao egress. Supplying such authority also increases the broker's blast radius and invalidates “neither side needs OpenBao transit connectivity.” Further, each of two replicas has a private writable emptyDir: refreshing only the leader leaves the other replica's copied auth.json stale. Specify broker authentication to OpenBao, narrowly scoped write policy, egress/admission volume shape, CAS/versioned writes, leader failover, and how every serving replica reloads the refreshed token—or use a separate operator refresh controller so the request broker remains read-only. -->

### 7. Isolated OpenBao provisioner (operator controller, immutable templates)
**Chosen: option (b), hardened.** A small **operator-owned controller** (ailab) reconciles per-tenant
OpenBao objects. It reads ONLY validated IDENTIFIERS from the CP's desired state (the `<org>`,
`<workspace>`, `<pool>` slugs — regex-validated, no policy/role BODIES from the CP) and SYNTHESIZES the
policy + k8s-auth-role bodies from **immutable operator templates**. It never applies a CP-supplied policy
or role body (that would just move the confused-deputy escalation into the controller). Its OpenBao token
is itself scoped, but — per codex — **ACL-path scoping is NOT confinement** (it gates which objects are
written, not the privileges inside them), so the CONTROLLER (not the ACL) is the confinement: the template
is the only source of policy content, and the template grants only `af/data/<org>/<workspace>/*` read to
exactly the tenant `<ns>`+`eso-auth SA` (and the broker path to the broker role). Spec: input schema =
`{org, workspace, pool}` validated `^[a-z0-9][a-z0-9-]{0,62}$` (no traversal/Unicode/collision); authed
source = the operator git desired-state (not a CP API); reconciliation ordering (namespace → SA → role →
policy); idempotent create-or-update; rotation; deletion/revocation on workspace removal; orphan/drift
repair; audit of every OpenBao write; documented break-glass (operator root, out-of-band).
**Path terminology normalized** (matching R-1): KV mount `af`; mount-relative key
`<org>/<workspace>/orchestrator`; ACL API path `af/data/<org>/<workspace>/*`; broker path
`af/data/broker/<provider>/<account>/oauth`. Separate orchestrator vs broker auth roles/policies; the
broker role is operator-owned and unreachable via any tenant SecretStore.

<!-- codex: round-2: Immutable in-image templates establish non-escalation against CP-supplied policy/role bodies, but the lifecycle spec is not adequate. The stated order is backwards: create namespace/SA, then policy, then the auth role that references it; delete/revoke in reverse with token/lease handling. Clarify whether “operator git desired-state” is a separate operator-reviewed source—the merged tenant repo is CP-written—and define stable collision-free object names, reserved identifiers, ownership/adoption records, CAS/ETag updates, retry/backoff, partial-failure recovery, finalizers/tombstones, rotation overlap, drift conflict policy, deletion grace, audit durability, and break-glass reconciliation suspension. The per-tenant reconciler must not synthesize the broker role from tenant inputs. Capability keypair issuance/publication/revocation must also be included or assigned to a separate operator controller. -->

### 8. Cilium (agent→broker + broker→upstream are R-2 BLOCKERS, not R-3)
Per codex, the minimum enforceable policies to make LIVE agents safe are R-2, not R-3 (R-3 adds canary +
FQDN-on-broker hardening only):
- **agent-profile egress**: ONLY the broker ClusterIP (that pool's broker) + **DNS L7 restricted to the
  broker's exact name** (no arbitrary query names → no DNS tunneling; alternate resolvers denied). No
  direct-IP/IPv6/alt-DNS/metadata/node-local/service-CIDR.
- **broker ingress**: a Cilium selector admitting ONLY sandbox pods of the matching job/tenant identity;
  capability verification is BROKER application logic (Cilium NetworkPolicy does not validate the token
  "at L7" without an explicit Envoy rule — not designed here), so the doc states network-identity (Cilium)
  and application-capability (broker) as the two SEPARATE controls, correlated by the broker checking the
  source identity against the capability `job_id`/pod-binding.
- **broker egress**: the enumerated upstream API + auth/OAuth hosts (from preflight; FQDN L7 + explicit
  deny of private/link-local/service/node ranges; test IPv4/IPv6/CNAME/DNS-rebinding/stale-IP) + the
  ledger (infra-pg) + DNS. No other cluster-internal reach.

<!-- codex: round-2: The three policy directions are the correct R-2 minimum, and capability verification correctly belongs in broker logic. However, “broker checking the source identity” still lacks an identity handoff, so a selector admitting agent pods cannot prove that a request came from the token's exact `job_id`; resolve that before calling replay denial proven. Broker egress must also include the narrowly defined OpenBao endpoint/auth path if broker-side refresh writeback remains, contradicting “No other cluster-internal reach.” Keep high-cardinality per-job labels out of Cilium security-identity allocation unless the cluster is explicitly configured and load-tested for that churn. -->

### 9. Merged-code fixes in agentforge (real bugs blocking live agents)
- **Codex stdin + path translation (BLOCKER)**: `SandboxExecutor` currently drops `ExecSpec.stdin` and
  passes orchestrator-host `job.cwd` paths in argv/`--cd`/`--output-last-message`. Fix: pipe stdin into
  the Job (Codex reads its prompt from stdin), and translate host paths → `/workspace` for argv + output.
  Real-agent tests: stdin EOF, `/workspace` translation, output import, writable-home discovery, BOTH CLIs.
- **`litellm_local` route (BLOCKER)**: the merged `EngineRouter` sends `litellm_local` through the same
  SandboxExecutor, which strips its auth + overwrites its base-URL. Fix: the broker gains a **local-model
  route** (a second broker instance / route fronting litellm-local with a broker-held litellm key), so
  local models are equally constrained (never a raw key in the sandbox). The capability `aud`/model-set
  selects the local route.
- **Typed exec contract**: extend `ExecSpec`/the runner to carry engine/provider/account/model/budget
  (from trusted role+account CONFIG, not by parsing argv/env), so `SandboxExecutor` mints tightly-bound
  capability claims from trusted inputs.
- **Env allowlist (not denylist)**: replace `_container_env`'s denylist with an explicit **per-engine
  allowlist** (agent profile: exactly `AF_BROKER_URL` + `AF_BROKER_CAP_FILE` + the CLI's required non-
  secret vars; no credential-named var can cross). Fail-fast reject an empty broker URL / missing capability
  before Job creation. Test that unknown/new provider credential names cannot cross.

<!-- codex: round-2: Inspection confirms all four merged defects: `ExecSpec` has no typed model contract; `SandboxExecutor` ignores `stdin`, passes host argv unchanged, uses a denylist, and renders an unpopulated broker-cap emptyDir; `litellm_local` injects a key/base URL that the sandbox strips/overwrites. The proposed directions are right, but “pipe stdin into the Job” is not an implementation: a Pod spec with `stdin:false` has no create-time byte stream, and the current client has no attach/exec channel. Define a reserved staged stdin file plus a fixed wrapper that reads it and supplies EOF, or an equally bounded authenticated transport, and skip/remove it like other control files. Path conversion must be structural—only `Path`-typed fields proven beneath `spec.cwd`, including Codex output scratch—not global string replacement in argv/shell/env. Choose one capability env name (`AF_BROKER_CAP_FILE` versus merged `AF_BROKER_CAPABILITY_FILE`) and make the CLI wrapper/helper consume it. -->

### 10. Preflight (HARD Phase-B entry gate, recorded artifacts)
Before ANY Phase-B build, prove — with **digest/version-pinned production CLI binaries run through a REAL
Kata Job against a RECORDING fake broker** (not a local CLI call) — and record transcripts + automated
regression tests, for BOTH Claude Code and Codex:
- exact base-URL/config mechanism; endpoints + query strings; the auth header it populates from the
  capability; request/response JSON schemas; SSE + cancellation behavior; auxiliary/helper model calls +
  the FULL model set used (so the capability model-allowlist is correct — "one-model" is likely wrong);
  retries/redirects; telemetry/update traffic; OAuth-refresh traffic (domains/scopes); and **whether the
  capability is persisted to home/workspace/logs/argv** (must not leak).
- prove **broker-side use + refresh of the subscription credential** is technically + operationally
  supported (required OAuth domains/scopes/provider constraints) — accepting a custom base-URL alone does
  NOT prove the broker can impersonate the CLI's upstream.
- prove the fixed SandboxExecutor stdin + `/workspace` path mapping actually runs both CLIs end-to-end.
If a CLI needs `apiKeyHelper` / immutable config / a wrapper / a different volume shape, BOTH sandbox-guard
and sandbox-job-guard change together (operator repo) — and the current VAPs forbid init containers + all
projected/Secret/ConfigMap volumes, so any such need is designed + admitted explicitly.

<!-- codex: round-2: This gate conflates two different proofs. A recording fake broker can discover CLI→broker behavior, but cannot prove that the real provider accepts broker-side subscription OAuth use/refresh; that needs a separately authorized real-provider end-to-end test through the deployed broker, with saved artifacts sanitized of refresh/access tokens. The sequencing is also impossible as written: the merged agent Job is rejected/unpopulatable and cannot transport stdin or translate paths until R-2 code and both VAPs change, yet those results are required “before ANY Phase-B build.” Split the gate into (A) an operator-owned real-Kata CLI discovery harness allowed during Phase A and (B) a Phase-B integration gate after the minimal SandboxExecutor/VAP implementation, before production rollout. -->

## Critical files
- **agentforge** (merged main → new branch): NEW `src/agentforge/broker/{gateway,capability,bounds,
  ledger,audit}.py` + a dedicated broker image target in `deploy/`; `main.py` (`broker` subcommand +
  capability mint on the agent path); `adapters/exec/sandbox.py` (stdin + host→/workspace translation;
  capability file write into the job subPath + import-skip; env allowlist; agent broker wiring);
  `ports/executor.py` + the runners (typed engine/provider/account/model/budget contract; litellm_local
  broker route); `infra/settings.py` (broker/upstream/capability/ledger knobs); secret-safe logging
  middleware.
- **agentforge-platform** (renderer): provide ONLY validated non-secret broker *references* (the pool's
  broker URL + route/model/budget policy) into the orchestrator config. **Render NO broker workload, NO
  `broker-oauth`, NO Service, NO capability** — those are operator-owned. Keep the tenant path unwidened;
  add the pool→broker reference + a cross-repo test.
- **ailab** (operator): `kubernetes/apps/.../agentforge-broker/**` = broker Namespace (dedicated, pinned
  PSA) + Deployment (dedicated image digest, SA token-automount off, PDB, resource quota) + Service +
  default-deny + the 3 Cilium policies + `broker-oauth` operator SecretStore/ExternalSecret + the ledger
  DB grant; the **OpenBao provisioner controller + templates**; the capability public-key material to the
  broker; the **broker admission policy** (pin image/SA/volumes/env/Secret refs); tenant-guard tightening
  (pin ExternalSecret target names); the sandbox-guard/job-guard simplification (drop broker-cap, 2-volume
  agent+test shape) applied atomically with the agentforge manifest contract.

## Verification (R-2 boundary proofs — extend the 6; run in the shadow deployment)
- **Capability lifecycle**: token reuse across legitimate multi-turn + concurrent calls; broker restart +
  replica failover + rollout (ledger survives, no double-spend); expiry mid-stream; job deletion; replay
  from another agent/test pod DENIED (Cilium identity + ledger session-binding); over-budget/over-rate/
  over-concurrency/expired/wrong-`aud`/wrong-model DENIED; a compromised broker cannot mint (no private key).
- **Absence of the OAuth in the sandbox**: proven via Pod spec, `/proc`, env, argv, mounted files,
  writable home/workspace, stdout/stderr, events, audit/log/metric output, provider error responses, and
  the IMPORTED result — NOT just "the Secret volume is absent". The capability file never enters imported
  files/argv/logs/foreign headers.
- **Gateway conformance/fuzz** through the DEPLOYED ingress path (raw HTTP/1.1 + HTTP/2): duplicate CL,
  CL+TE, obs-fold/control bytes, encoded traversal, oversized/chunked/compressed bombs, slowloris, SSE
  floods, disconnects, redirect chains, CONNECT/absolute-form — all rejected/bounded. Auth headers,
  capabilities, and OAuth-refresh material NEVER logged (independent of the R-3 publish-sink redactor).
- **Credential split (cross-tier)**: SubjectAccessReviews + rejected workload/ExternalSecret mutations +
  an adversarial CP commit attempting to materialize `broker-oauth` into the orchestrator ns — all DENIED;
  the two Deployments mount different Secrets AND no path can cross the ESO wildcard.
- **OpenBao provisioner non-escalation**: malicious policy/role bodies (the controller ignores CP bodies
  and uses templates); auth roles referencing existing admin policies; foreign/wildcard bound SAs/ns/
  audiences; traversal/Unicode/collision identifiers; role-policy relink; post-create update; delete/
  recreate; compromised/stale desired state — all confined to `af/data/<org>/<ws>/*`. ESO TokenRequest
  audience + TTL verified end-to-end against the installed ESO CRD/controller version + OpenBao role.
- **Preflight PROVEN** (named CLI versions/digests + saved transcripts + regression tests): multi-turn,
  retry/error paths, aux model calls, long SSE, capability-helper behavior, writable-home restart, and the
  real SandboxExecutor stdin/path mapping — for Claude Code AND Codex AND the litellm-local route.
- **Regression**: all existing R-1/P1 tests green; cross-repo rendered-Job→VAP admission tests; live Kata
  Claude/Codex canaries; broker restart/replica + ESO refresh/rotation tests; negative CP/operator
  ownership tests. The production flip stays gated; the SHADOW is where these run.