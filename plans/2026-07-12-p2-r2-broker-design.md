# P2 R-2 — model-gateway broker + credential split + isolated OpenBao provisioner

## Codex Review — round 3 (final)

- **NOT CONVERGED** — one true blocker survives; R-2 is not yet safe to enter Phase B.
- Source-IP TOFU is sound for the declared deployment: this repo configures kube-proxy-free Cilium, the broker is an internal ClusterIP with no L7 ingress hop, and no in-cluster SNAT path is configured that would hide the originating pod IP; pod-IP reuse alone does not enable replay without possession of the still-live token. [Cilium documentation](https://docs.cilium.io/en/stable/network/kubernetes/kubeproxy-free/)
- **BLOCKER:** no authenticated, network-admitted writer can perform the promised job-end ledger close. §5 grants the ledger role to the broker, §8 admits only sandbox pods to broker ingress, and the tokenless broker has no Kubernetes Job visibility; therefore the orchestrator/reaper cannot close a session before import/log publication as designed.
- Phase B is blocked until the design assigns an explicit close authority and path—for example, a separately authenticated orchestrator/reaper close endpoint admitted by policy, or a narrow direct ledger role plus egress—with idempotent retry/fail-closed behavior and job-end/reaper tests.

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
| capability keypair (public half) | ailab (operator keypair controller, §7) → broker | broker holds only PUBLIC verify keys; `kid`→operator record binds issuer/tenant/aud/ceilings (§3) |
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
`AF_BROKER_CAPABILITY_FILE=/workspace/.af/broker-cap.jwt` (env is a non-secret path; the env NAME is the
**merged `AF_BROKER_CAPABILITY_FILE`**, not `AF_BROKER_CAP_FILE` — §9). This removes the broker-cap volume
entirely; the sandbox-guard/job-guard VAPs are simplified to the 2-volume `{workspace, home}` shape (a
SMALLER attack surface, changed in the operator repo atomically with the agentforge manifest contract).

**Multi-tenant NFS isolation — org-qualify ALL sandbox storage identity (R-2 change to the merged R-1
manifests).** Unguessable job IDs and the Kubernetes `subPath` do NOT constrain a *different* tenant
orchestrator that mounts the shared export root, and equal workspace slugs across orgs collide, so R-2
makes cross-tenant isolation correct-by-construction:
- **Org-qualified names.** The workspace PVC becomes `af-sbx-ws-<org>-<ws>` (was `af-sbx-ws-<ws>`), the
  staging PVC `af-sbx-stage-<org>-<ws>` (was `af-sbx-stage-<ws>`), and the static staging PV
  `af-sbx-stage-<org>-<ws>`. This REVISES the merged R-1 renderer (`sandbox_workspace_pvc` /
  `sandbox_staging_pvc` / `sandbox_staging_pv`) AND the sandbox-guard/job-guard VAP subPath+PVC pins, which
  now key on **org+workspace**, never the workspace slug alone.
- **Server-side per-org/workspace export layout.** Each org/workspace gets its OWN export directory under
  `/pve-nfs/agentforge-sandbox/<org>/<ws>/`, and each consumer's static PV (the orchestrator staging PV +
  the sandbox workspace PV) points at ITS OWN `<org>/<ws>` subdir — so one tenant orchestrator's PV cannot
  reach another org's tree at the server, not merely by name convention.
- **Cross-tenant negative tests.** Prove an orchestrator/PV for `(orgA, ws)` cannot enumerate or read
  `(orgB, ws)` job dirs/capabilities, and that equal workspace slugs in different orgs resolve to disjoint
  exports.

(Tenant-zero-shadow is a SINGLE org, so this is not yet exercised across two live tenants, but the naming +
server-side isolation must be correct-by-construction before any multi-tenant flip.)

**`.af/` is a RESERVED staging namespace (not copied from the hostile checkout).** The merged `stage()`
skips only top-level `.git`/symlinks and copies directories with `symlinks=True`, so a repository-supplied
`.af/broker-cap.jwt` symlink — or a `.af` directory — would be PRESERVED and could redirect a naive
capability write. R-2:
- `stage()` **REJECTS a source `.af` entry** (a `.af` symlink OR a `.af` directory specifically) — a repo
  can never supply one. (An R-2 change to the merged `stage()` in `adapters/exec/sandbox.py`.)
- The orchestrator CREATES `.af/` and writes the cap file **atomically with `O_NOFOLLOW`+`O_EXCL`, mode
  `0600`**, and **CLEANS `.af/` on every pre-Job failure** so a partial cap never lingers.
- `.af/` is added to the import SKIP set (like `.af-result.json`), so it is never imported back.

**Copied-capability revocation — "cannot remain usable", not "never appears".** Skipping `.af/` on import
does NOT stop hostile code copying the readable bearer token into another imported file or into stdout. So
the capability is NOT claimed to be un-exfiltrable; it is made *useless once the job ends*: the
orchestrator/reaper **CLOSES/REVOKES the ledger session (marks it closed) BEFORE the import + log/artifact
publication** (§5), so a copied token is dead by the time anything leaves the sandbox. Combined with the
short TTL, the session-close, and the per-org/workspace NFS cross-read isolation (above — another job can't read a live token),
the claim is WEAKENED from "the capability never appears in imported files/argv/logs/foreign headers" to
**"a copied capability cannot remain usable"**. Generic publish-sink redaction of the exact capability
bytes stays R-3.

### 3. Capability protocol (session capability + asymmetric signature + operator ceilings)
- **Reusable session capability, not single-use.** Claude Code and Codex make MANY streamed model calls
  per job (incl. helper/compact/aux-model calls), so a single-use nonce is wrong. The capability is a
  **per-job session token** valid for the job's duration with a hard `exp`; the broker enforces
  quotas/rate/concurrency over the SESSION (see ledger).
- **Anti-replay = source-IP TOFU + session close (a broker-VISIBLE binding, NO Cilium handoff).** The ASGI
  broker sees the peer (source) IP directly at L3 — that IS a broker-visible binding; there is NO
  Cilium→broker identity handoff (Cilium enforces an endpoint selector; it does not hand the app an
  authenticated per-job identity/label, and the capability is minted before the Pod IP exists). On the
  FIRST request of a session the broker BINDS the session (trust-on-first-use) to the observed source IP in
  the ledger; every later request in that session MUST come from the same IP or is rejected. The
  orchestrator/reaper CLOSES the session in the ledger when the Job completes, so post-job replay is
  rejected even before `exp`. A per-request monotonic sequence + session id is logged for audit/idempotency
  but is NOT the anti-replay control.
- **Residual (stated explicitly).** Within a LIVE session the capability IS a **bearer credential usable
  from the bound IP up to budget/TTL**. That residual is bounded by four independent controls: (i) the
  per-org/workspace NFS cross-read isolation (§2) — another job cannot read this job's token; (ii) session-IP pinning — a different pod
  (different source IP) cannot use it; (iii) session-close on job-end — a copied token is dead after the
  Job; (iv) short TTL + token budget. Cilium ensures only tenant agent pods can reach the broker at all
  (§8); the source-IP TOFU + session-close is the APP-LAYER control layered on top.
- **Asymmetric signature (orchestrator signs, broker verifies public-only).** The orchestrator holds a
  PRIVATE signing key (Ed25519, in `orchestrator-creds` via ESO); the broker holds only the PUBLIC key(s)
  with a `kid` header for rotation. Public-only verification cryptographically prevents a compromised broker
  from *minting* capabilities (it has no private key), and neither side needs OpenBao transit connectivity
  (avoids the "no OpenBao access" contradiction). Key rotation = publish a new `kid` public key to the
  broker, switch the orchestrator's active `kid`, retire the old.
  **Residual — do NOT equate "cannot mint" with "containment of a compromised broker":** a compromised
  broker can simply IGNORE its own verification/ledger checks and directly spend its mounted OAuth. The
  broker is a TRUSTED tier; containment of a *compromised broker* is via its operator ceilings, audit, and
  isolation (§2/§4/§8) — NOT via the capability.
- **`kid` registry is OPERATOR-owned (broker does not trust the self-asserted `tenant`).** The broker maps
  each `kid` → an OPERATOR-owned record `{issuer, allowed tenants/workspaces, audience (broker instance),
  ceilings}` from operator config. It does NOT select a tenant ceiling from the token's self-asserted
  `tenant` claim: a capability is accepted only if its `iss`/`aud`/`tenant`/`workspace` match the operator
  record bound to its `kid`, and the ceilings enforced are that record's — not the token's requested ones.
  Capability keypair lifecycle (generation, delivery, reload, overlap, revocation, retire-after-max-TTL) is
  owned by the operator keypair controller (§7), never a tenant/CP input.
- **Bound claims** (all signed): `iss` (orchestrator instance), `aud` (exactly one broker instance =
  provider+account), `tenant`+`workspace`, immutable `job_id` (`^[a-z0-9]{32}$`), `pool`/role, the
  ALLOWED model set (small explicit allowlist — capturing Claude Code's helper/compact models, see
  preflight), allowed route/method set, `nbf`/`exp`, and requested quota ceilings.
- **Broker applies OPERATOR-owned ceilings independently.** The trusted orchestrator can request a budget,
  but the broker clamps every claim to the operator-owned per-`kid` ceilings (per-(provider,account) and
  per-tenant: max tokens, rate, concurrency, duration, model set). The orchestrator can never over-mint
  authority the operator didn't grant. Threat-model note (explicit): the capability + the Cilium pod
  identity are both issued/selected by the orchestrator, so they are independent against a compromised
  SANDBOX but NOT against a compromised orchestrator — the orchestrator is a trusted tier; the broker
  ceilings + audit are the backstop against orchestrator misbehavior.

### 4. Model gateway broker
A standalone application-layer gateway (not a proxy, not a sidecar), in the dedicated broker image.
- **Exact request reconstruction (JSON-schema, not header allowlist).** Parse the inbound body against the
  provider's exact request schema; reconstruct a fresh upstream request from validated fields only (model
  ∈ capability allowlist; normalized path/query; bounded/known headers only). Reject: CONNECT, absolute-
  form targets, duplicate/control-character headers, conflicting Content-Length/Transfer-Encoding,
  compressed-body bombs (decompression-size limit), any provider field that triggers uploads / tools /
  server-side URL fetches unless explicitly allowed, unknown fields. Strip every inbound credential/cookie
  header before reconstruction; never echo upstream request headers in errors.
- **Upstream client**: redirects DISABLED; **standard CA validation + exact hostname/SNI verification** for
  the enumerated upstream host(s) (from preflight); requests are reconstructed to fixed, validated
  destinations only; Cilium FQDN/CIDR egress is the network backstop (§8). **SPKI pinning is an OPTIONAL,
  provider-supported hardening item — NOT an unconditional blocker:** an ordinary SaaS certificate rotation
  must not fail-close every agent, so a pin set is enabled only where the provider publishes a supported
  pin set + overlap/rotation contract. DNS-rebinding-safe (re-resolve + re-pin the resolved address, deny
  private/link-local/metadata/service/node ranges at the client too, belt to Cilium). Broader network
  hardening belongs in R-3.
- **Streaming (SSE) bounded protocol**: max event/line/total bytes, idle + wall deadlines, backpressure,
  client-disconnect cancellation (cancel upstream), error-frame handling, decompression limits, and
  **cumulative token/quota accounting that continues after the stream starts** (reserve worst-case at
  dispatch, reconcile from the stream's usage).
- **Auth injection**: the broker injects the real `Authorization` from `broker-oauth`; the sandbox never
  sees it. Subscription-OAuth refresh is owned by a SEPARATE operator refresh controller (§6), NOT the
  broker; the broker's `broker-oauth` Secret is READ-ONLY and every replica RELOADS it on change (no
  broker→OpenBao egress, no broker write path).
- **Smuggling surface**: if any ingress proxy (Envoy/Service) sits in front of the ASGI server, the
  raw-socket HTTP/1.1+HTTP/2 conformance/fuzz suite runs through THAT path, not just the app test client.

### 5. Bounds, ledger, capacity, audit (fail-closed, replica-safe)
<!-- codex: round-3: BLOCKER — no component has both authority and network reachability to close ledger sessions on Job end: only the broker is given the DB role, broker ingress admits only sandbox pods, and the broker cannot observe Kubernetes Job lifecycle. Assign an authenticated orchestrator/reaper close path or a narrow direct ledger role+egress, and prove idempotent retry/fail-closed closure before import/log publication. -->
- **Shared TRANSACTIONAL ledger on Postgres (NOT broker memory).** Session/request/rate/concurrency/spend
  state lives in a transactional Postgres schema on the existing CNPG `infra-pg` (the broker gets a narrow
  DB role; adds a broker→infra-pg egress dependency — enumerated in the netpol, §8), chosen over OpenBao
  (which would contradict broker isolation). Schema:
  - **`sessions`** — `session_id` (PK), `job_id`, `tenant`, `workspace`, `aud`, `model_set`,
    `bound_source_ip`, `opened_at`, `closed_at`, `exp`, `token_budget`, `tokens_reserved`, `tokens_used`.
    First use binds `bound_source_ip` (TOFU, §3); job-end stamps `closed_at` (close/revoke).
  - **`requests`** — `jti`/`request_id` (UNIQUE — idempotency + replay dedupe), `session_id` (FK), `seq`,
    `reserved`, `used`, `status`.
  - **Atomic reserve-and-concurrency acquisition in ONE transaction:** a request checks budget
    (`tokens_reserved + worst_case ≤ token_budget`) AND concurrency (open in-flight < ceiling) and inserts
    the `requests` row with its reservation atomically, so two concurrent replicas cannot both overshoot.
  - **Expiring concurrency leases:** each in-flight reservation carries a lease/expiry, so a CRASHED
    broker's in-flight reservation EXPIRES and its concurrency + budget are reclaimed rather than leaked.
  - **Session open/close/revoke** are ledger operations; **fail closed if the ledger is unavailable**
    (reject, don't pass through).
- **Enforceable spend unit = TOKENS; worst-case reservation is CONSERVATIVE.** Subscription OAuth does not
  report per-call dollar cost, and post-response usage arrives too late to be a hard pre-call bound, so the
  hard bound is a **token budget** reserved pre-call from the capability's quota. The reserved worst-case =
  **validated INPUT tokens + MAX output tokens + auxiliary/helper usage** for the request — NOT merely a
  model-alias limit — reconciled to actuals from the response/stream. A best-effort dollar estimate (from a
  pinned pricing source) is AUDIT-only, never a gate.
- **Global fairness/rate + retained reservations.** Per-tenant fairness + rate state is GLOBAL in the
  ledger (token-bucket rows), not per-replica, so a rolling/replicated broker can't multiply a tenant's
  rate. A dispatched request whose outcome is UNCERTAIN (broker crash, lost stream) RETAINS its reservation
  (never optimistically rolled back) and is reconciled later — preventing double-spend across the
  uncertainty window.
- **Capacity (not "TBD")**: fixed replica count (start 2) + PDB; bounded request queue with load-shed
  (429/503 + `Retry-After`); max open streams + connection-pool caps; per-tenant fairness (token-bucket
  keyed by tenant, GLOBAL in the ledger); defined rollout (Recreate or surge with the ledger as the shared
  source of truth so a rolling replica can't double-spend); recovery path when the subscription credential
  or upstream is down (fail closed, readiness false, alert).
- **HA / placement.** Two replicas + a PDB is NOT HA by itself: add broker **anti-affinity +
  topology-spread** across nodes so a single node loss doesn't take out all replicas. For the ledger,
  EITHER confirm `infra-pg` (CNPG) runs a failover topology, OR document that an `infra-pg` outage = a
  service-wide **fail-closed DoS** — ACCEPTABLE for tenant-zero, a P3 hardening (fail-closed rejects rather
  than passing through, so it is an availability risk, not a credential/data-integrity risk).
- **Audit**: sink = the CP ingest endpoint OR a local structured log scraped by Alloy (chosen: structured
  stdout log + Prometheus counters — no extra egress dependency; the CP ingest is best-effort). Buffered +
  bounded cardinality; sanitized (NO auth headers, capabilities, OAuth-refresh material, upstream bodies,
  or full URLs — only job/tenant/model/decision/token-counts). **Security decisions never depend on audit
  availability** — a full/broken audit buffer drops audit lines but still enforces + serves (or fails
  closed on the ENFORCEMENT store, not the audit sink).

### 6. Credential split + no ESO wildcard escape
- `orchestrator-creds` (forge PATs + CP bearer + git-push + the capability PRIVATE signing key) →
  **orchestrator** ns/SA only, from OpenBao **`af/data/tenants/<org>/<workspace>/orchestrator`** via the
  tenant SecretStore. (R-2 change: the merged R-1 path `af/data/<org>/<ws>/orchestrator` moves under the
  reserved `tenants/` prefix — see the reserved-prefix bullet — touching the renderer's
  `openbao_orchestrator_key` + the provisioner template.)
- `broker-oauth` (subscription OAuth) → **broker** ns/SA only, from a **DISTINCT operator OpenBao path**
  **`af/data/operator/broker/<provider>/<account>/oauth`** via a **separate operator-owned SecretStore +
  auth role** that the tenant SecretStore's policy CANNOT read (the tenant role is scoped to
  `af/data/tenants/<org>/<workspace>/*`, which cannot name any `af/data/operator/*` key).
- **Reserved-prefix split CLOSES the ESO wildcard escape (was OPEN).** The R-1 identifier regex permits
  `org=broker`, so a tenant with `org=broker`, `workspace=<provider>` would get policy
  `af/data/broker/<provider>/*` — which COVERS `af/data/broker/<provider>/<account>/oauth`. R-2 removes the
  overlap by construction: ALL tenant data lives under `af/data/tenants/<org>/<workspace>/*` and ALL
  operator data under `af/data/operator/...` (broker OAuth at `af/data/operator/broker/<provider>/<account>/*`);
  and identifier validation RESERVES `tenants`, `operator`, and every operator top-level slug as FORBIDDEN
  org slugs (rejected by the provisioner + the renderer). Add migration tests for the existing
  `af/data/<org>/<ws>/*` → `af/data/tenants/<org>/<ws>/*` move. No tenant-controlled ExternalSecret can now
  name a key outside its subtree, so the CP path cannot materialize `broker-oauth`.
- **tenant-guard tightening (operator repo) — pin what the merged VAP still leaves open.** The merged
  `tenant-guard.yaml` still leaves UNPINNED: `ExternalSecret.target.name`, `ExternalSecret.spec.secretStoreRef.name`
  (only `.kind` is pinned), the `SecretStore` `metadata.name`, the kubernetes-auth `role`, and the
  `serviceAccountRef.name` VALUE (only its presence is required); and the source-key `<org>/<workspace>`
  segments are only syntactically checked, NOT correlated to the namespace. R-2 pins ALL of these to
  DETERMINISTIC per-pool/per-workspace names and correlates the source key to the namespace's org/workspace
  via an **OPERATOR-owned VAP parameter/mapping** (a `paramRef` to an operator-owned mapping ConfigMap, or
  equivalent immutable operator metadata / deterministic naming) — NOT a CEL guess, since the tenant-guard
  carries no trustworthy org→namespace map. Specifically pin: `target.name` ∈ the per-pool allowed names;
  `secretStoreRef.name` = the per-tenant SecretStore; `SecretStore.metadata.name`, its kubernetes-auth
  `role`, and `serviceAccountRef.name` = the deterministic per-workspace values; and the source key ∈
  `tenants/<org>/<workspace>/…` for THIS namespace's org/workspace. **Test UPDATES as well as CREATEs.**
  OpenBao ACL-path containment (the per-workspace role grants read on `af/data/tenants/<org>/<workspace>/*`
  only, authenticating as the per-ns eso SA) remains MANDATORY even after admission hardening — admission is
  defense-in-depth, not the confinement. Keep the NEGATIVE proofs (SubjectAccessReview + rejected-create)
  that the CP reconciler, tenant SAs, the orchestrator SA, and the sandbox Job creator CANNOT create/mutate
  any workload or ExternalSecret referencing another tier's Secret.
- **auth.json refresh — a SEPARATE operator refresh controller; the broker stays READ-ONLY.** ESO is
  read-only from the app's perspective, the broker has token-automount off, and §8 allows NO
  broker→OpenBao egress — so the broker CANNOT be the writer of a rotated token without breaking its own
  isolation and its "neither side needs OpenBao transit" property; and with two replicas, refreshing only a
  leader leaves the other replica's copy stale. R-2 resolves this with a small **operator-owned refresh
  controller** (a sibling of the OpenBao provisioner, §7) that OWNS subscription `auth.json` rotation: it
  performs the out-of-band OAuth refresh and writes the rotated token to OpenBao under its OWN
  narrowly-scoped write policy (`af/data/operator/broker/<provider>/<account>/*`, CAS/versioned). ESO then
  re-syncs the broker's READ-ONLY `broker-oauth` Secret, and EVERY broker replica RELOADS on change (watch
  the mounted file / periodic reload) — so multi-replica is handled uniformly with NO
  leader-election-for-refresh in the broker, NO broker write policy, and NO broker→OpenBao egress. (If a
  CLI's subscription OAuth cannot be refreshed out-of-band by a controller — only in-process by the CLI
  itself — that is a PREFLIGHT finding, §10.)

### 7. Isolated OpenBao provisioner (operator controller, immutable templates)
**Chosen: option (b), hardened.** A small **operator-owned controller** (ailab) reconciles per-tenant
OpenBao objects. It reads ONLY validated IDENTIFIERS from the CP's desired state (the `<org>`,
`<workspace>`, `<pool>` slugs — regex-validated, no policy/role BODIES from the CP) and SYNTHESIZES the
policy + k8s-auth-role bodies from **immutable operator templates**. It never applies a CP-supplied policy
or role body (that would just move the confused-deputy escalation into the controller). Its OpenBao token
is itself scoped, but — per codex — **ACL-path scoping is NOT confinement** (it gates which objects are
written, not the privileges inside them), so the CONTROLLER (not the ACL) is the confinement: the template
is the only source of policy content, and the template grants only `af/data/tenants/<org>/<workspace>/*`
read to exactly the tenant `<ns>`+`eso-auth SA`. The BROKER role/policy is OPERATOR-FIXED — a distinct
operator-owned object, NEVER synthesized from tenant `{org, workspace, pool}` inputs.

**Spec.**
- **The CP-written source is UNTRUSTED input.** The merged per-tenant tenant repo is CP-WRITTEN, so the
  provisioner treats the CP's identifiers as UNTRUSTED: it reads ONLY the `{org, workspace, pool}` slugs
  (regex `^[a-z0-9][a-z0-9-]{0,62}$`; no traversal/Unicode/collision; `tenants`, `operator`, and every
  operator top-level slug RESERVED/forbidden as an org, §6) and SYNTHESIZES every policy + auth-role BODY
  from immutable operator templates. It NEVER consumes a CP-supplied policy/role body, and it must NOT
  synthesize the broker role from tenant inputs (operator-fixed, above).
- **Create order (CORRECTED):** namespace → SA → **policy → auth-role** (the auth-role REFERENCES the
  policy, so the policy must exist first). The merged "namespace → SA → role → policy" was backwards.
- **Delete order = REVERSE:** auth-role → policy → SA → namespace, with token/lease revocation/handling
  (revoke issued tokens/leases before removing the role/policy).
- **Idempotent, collision-free, recoverable:** deterministic collision-free object names (org+workspace
  qualified); ownership/adoption records so the controller only manages objects it owns; CAS/ETag
  (versioned) updates; retry/backoff; partial-failure recovery (resume mid-sequence); finalizers/tombstones
  for ordered deletion; rotation overlap (new before old retired); drift conflict policy (operator template
  wins); deletion grace; DURABLE audit of every OpenBao write; documented break-glass = reconciliation
  SUSPEND (operator root, out-of-band).
- **Capability keypair lifecycle** (generation, publication of the public half to the broker, rotation
  overlap, revocation, retire-after-max-TTL — the "keypair controller" referenced in §3) is owned by THIS
  controller OR a named sibling operator controller; it is NEVER a tenant/CP input.

**Path terminology normalized** (matching R-1 + the §6 reserved-prefix move): KV mount `af`; tenant
mount-relative key `tenants/<org>/<workspace>/orchestrator`; tenant ACL API path
`af/data/tenants/<org>/<workspace>/*`; broker path `af/data/operator/broker/<provider>/<account>/oauth`.
Separate orchestrator vs broker auth roles/policies; the broker role is operator-owned and unreachable via
any tenant SecretStore.

### 8. Cilium (agent→broker + broker→upstream are R-2 BLOCKERS, not R-3)
Per codex, the minimum enforceable policies to make LIVE agents safe are R-2, not R-3 (R-3 adds canary +
FQDN-on-broker hardening only):
- **agent-profile egress**: ONLY the broker ClusterIP (that pool's broker) + **DNS L7 restricted to the
  broker's exact name** (no arbitrary query names → no DNS tunneling; alternate resolvers denied). No
  direct-IP/IPv6/alt-DNS/metadata/node-local/service-CIDR.
- **broker ingress**: a Cilium selector admitting ONLY sandbox pods of the matching pool/tenant identity,
  keyed on a **STABLE per-pool/tenant identity label — NOT the high-cardinality per-job label** (per-job
  labels are kept OUT of Cilium security-identity allocation to avoid identity churn/blowup). Cilium is the
  NETWORK-identity control (only tenant agent pods can reach the broker); capability verification +
  anti-replay are BROKER application logic — the broker binds the session to the source IP it sees at L3
  (TOFU) and closes it on job-end (§3). Cilium does NOT hand the broker an authenticated per-job identity,
  and none is needed. The two controls (network-identity via Cilium, application-capability + source-IP
  session-binding in the broker) are SEPARATE and layered.
- **broker egress**: the enumerated upstream API + auth/OAuth hosts (from preflight; FQDN L7 + explicit
  deny of private/link-local/service/node ranges; test IPv4/IPv6/CNAME/DNS-rebinding/stale-IP) + the
  ledger (infra-pg) + DNS. **NO broker→OpenBao egress** — the operator refresh controller (§6/§7), NOT the
  broker, writes OpenBao, so there is no contradiction with "no other cluster-internal reach"; the only
  cluster-internal dependency is the infra-pg ledger.

### 9. Merged-code fixes in agentforge (real bugs blocking live agents)
- **Codex stdin + path translation (BLOCKER)**: `SandboxExecutor` currently drops `ExecSpec.stdin` and
  passes orchestrator-host `job.cwd` paths in argv/`--cd`/`--output-last-message`.
  - **stdin via a STAGED FILE + a fixed WRAPPER, NOT a byte stream.** A Pod spec with `stdin:false` has NO
    create-time byte stream and the client has NO attach/exec channel, so "pipe stdin into the Job" is not
    implementable. Instead the orchestrator STAGES the stdin bytes to a reserved file `<job_dir>/.af/stdin`,
    and a fixed WRAPPER (the sandbox image entrypoint) reads that file, feeds it to the CLI, and supplies
    EOF. `.af/stdin` lives in the SAME reserved `.af/` staging namespace as the capability (§2) — created
    atomically by the orchestrator, rejected as a source entry, skipped on import, and removed.
  - **STRUCTURAL (typed) path translation, NOT string replacement.** Only `Path`-typed `ExecSpec` fields
    PROVEN to be under `spec.cwd` are rewritten to `/workspace/<relative>` — INCLUDING Codex's
    `--output-last-message` scratch path. NEVER a global string replacement across argv/shell/env (which
    would corrupt unrelated substrings and miss/mangle real paths).
  - Real-agent tests: stdin EOF via the wrapper, `/workspace` translation of the typed path fields (incl.
    Codex output scratch), output import, writable-home discovery, BOTH CLIs.
- **`litellm_local` route (BLOCKER)**: the merged `EngineRouter` sends `litellm_local` through the same
  SandboxExecutor, which strips its auth + overwrites its base-URL. Fix: the broker gains a **local-model
  route** (a second broker instance / route fronting litellm-local with a broker-held litellm key), so
  local models are equally constrained (never a raw key in the sandbox). The capability `aud`/model-set
  selects the local route.
- **Typed exec contract**: extend `ExecSpec`/the runner to carry engine/provider/account/model/budget
  (from trusted role+account CONFIG, not by parsing argv/env), so `SandboxExecutor` mints tightly-bound
  capability claims from trusted inputs.
- **Env allowlist (not denylist)**: replace `_container_env`'s denylist with an explicit **per-engine
  allowlist** (agent profile: exactly `AF_BROKER_URL` + **`AF_BROKER_CAPABILITY_FILE`** (the MERGED env
  name — the whole doc uses this, NOT `AF_BROKER_CAP_FILE`) + the CLI's required non-secret vars; the CLI
  wrapper/helper consumes `AF_BROKER_CAPABILITY_FILE`; no credential-named var can cross). Fail-fast reject
  an empty broker URL / missing capability before Job creation. Test that unknown/new provider credential
  names cannot cross.

### 10. Preflight — SPLIT into a Phase-A discovery harness + a Phase-B integration gate + a real-provider test
The R-1 gate as written was impossible: it required results from the fixed SandboxExecutor + both VAPs
(unimplemented R-2 code) "before ANY Phase-B build", yet the merged agent Job is rejected/un-populatable
and cannot transport stdin or translate paths until that R-2 code + the VAPs land. And a recording FAKE
broker can DISCOVER CLI→broker behavior but CANNOT prove the real provider accepts broker-side subscription
OAuth use/refresh. So the gate is split:

**(A) Phase-A CLI discovery harness (operator-owned, runnable NOW).** With **digest/version-pinned
production CLI binaries run through a REAL Kata Job against a RECORDING fake broker** (not a local CLI
call), record transcripts + automated regression tests for BOTH Claude Code and Codex: exact
base-URL/config mechanism; endpoints + query strings; the auth header it populates from the capability;
request/response JSON schemas; SSE + cancellation behavior; auxiliary/helper model calls + the FULL model
set used (so the capability model-allowlist is correct — "one-model" is likely wrong); retries/redirects;
telemetry/update traffic; OAuth-refresh traffic (domains/scopes); and **whether the capability is persisted
to home/workspace/logs/argv** (must not leak). This is Phase-A-ALLOWED — operator-owned, no tenant path, no
real provider credential — and does NOT require the R-2 SandboxExecutor/VAP changes.

**(B) Phase-B integration gate (AFTER the minimal SandboxExecutor + VAP changes land).** Once the fixed
SandboxExecutor (staged `.af/stdin` + wrapper, typed `/workspace` path translation, capability file write +
import-skip, env allowlist) AND both sandbox-guard/sandbox-job-guard changes are merged, prove the real
agent Job runs end-to-end in Kata: stdin EOF via the wrapper, `/workspace` path mapping (incl. Codex output
scratch), output import, writable-home restart — for both CLIs + the litellm-local route. This runs BEFORE
production rollout, NOT before "any Phase-B build" (fixing the impossible sequencing).

**(C) Separately-authorized REAL-PROVIDER end-to-end test (through the DEPLOYED broker).** A fake broker
cannot prove the real provider accepts broker-side subscription OAuth USE + REFRESH, so a
separately-authorized test drives the real provider through the DEPLOYED broker (in the shadow), proving
broker-side use + refresh is technically + operationally supported (required OAuth domains/scopes/provider
constraints) — accepting a custom base-URL alone does NOT prove the broker can impersonate the CLI's
upstream. Saved artifacts are SANITIZED of refresh/access tokens. Exact auth/capability suppression in
these artifacts is R-2; generic publish-sink redaction stays R-3.

If a CLI needs `apiKeyHelper` / immutable config / a wrapper / a different volume shape, BOTH sandbox-guard
and sandbox-job-guard change together (operator repo) — and the current VAPs forbid init containers + all
projected/Secret/ConfigMap volumes, so any such need is designed + admitted explicitly.

## Critical files
- **agentforge** (merged main → new branch): NEW `src/agentforge/broker/{gateway,capability,bounds,
  ledger,audit}.py` + a dedicated broker image target in `deploy/`; `main.py` (`broker` subcommand +
  capability mint on the agent path); `adapters/exec/sandbox.py` (**reject a source `.af`**; staged
  `.af/stdin` + wrapper; typed host→`/workspace` translation; capability file write into the `.af/` subPath
  with `O_NOFOLLOW`+`O_EXCL`+`0600` + import-skip + pre-Job cleanup; env allowlist using
  `AF_BROKER_CAPABILITY_FILE`; drop the broker-cap emptyDir; agent broker wiring); `ports/executor.py` +
  the runners (typed engine/provider/account/model/budget contract; litellm_local broker route);
  `infra/settings.py` (broker/upstream/capability/ledger knobs); secret-safe logging middleware.
- **agentforge-platform** (renderer): provide ONLY validated non-secret broker *references* (the pool's
  broker URL + route/model/budget policy) into the orchestrator config. **Render NO broker workload, NO
  `broker-oauth`, NO Service, NO capability** — those are operator-owned. Keep the tenant path unwidened;
  add the pool→broker reference + a cross-repo test. **R-2 renderer changes:** org-qualify the sandbox
  PVC/PV names (`af-sbx-ws-<org>-<ws>`, `af-sbx-stage-<org>-<ws>` in `sandbox_workspace_pvc` /
  `sandbox_staging_pvc` / `sandbox_staging_pv`); move the orchestrator OpenBao key under the reserved
  `tenants/` prefix (`openbao_orchestrator_key` → `tenants/<org>/<workspace>/orchestrator`); reserve
  `tenants`/`operator`/every operator top-level slug as forbidden org slugs in identifier validation (+
  migration tests).
- **ailab** (operator): `kubernetes/apps/.../agentforge-broker/**` = broker Namespace (dedicated, pinned
  PSA) + Deployment (dedicated image digest, SA token-automount off, PDB, resource quota, anti-affinity +
  topology-spread) + Service + default-deny + the 3 Cilium policies (broker ingress keyed on the STABLE
  per-pool identity label) + `broker-oauth` operator SecretStore/ExternalSecret (READ-ONLY to the broker) +
  the ledger DB grant + the transactional ledger schema on `infra-pg`; the **OpenBao provisioner controller
  + templates** (corrected create/delete ordering; reserved slugs); the **operator refresh controller**
  (subscription `auth.json` rotation → OpenBao write; §6); the **capability keypair controller** (generate
  + publish the public half to the broker; §3/§7); the **broker admission policy** (pin
  image/SA/volumes/env/Secret refs); tenant-guard tightening (pin ExternalSecret `target.name` +
  `secretStoreRef.name`, SecretStore `metadata.name`/`role`/`serviceAccountRef`, org/workspace-correlated
  source key via an operator-owned param map; test UPDATEs); per-org/workspace NFS export dirs
  (`/pve-nfs/agentforge-sandbox/<org>/<ws>/`) + org-qualified static PVs; the sandbox-guard/job-guard
  simplification (drop broker-cap, 2-volume agent+test shape; org+workspace subPath/PVC pins) applied
  atomically with the agentforge manifest contract.

## Verification (R-2 boundary proofs — extend the 6; run in the shadow deployment)
- **Capability lifecycle**: token reuse across legitimate multi-turn + concurrent calls; broker restart +
  replica failover + rollout (ledger survives, no double-spend); expiry mid-stream; job deletion; replay
  from another agent/test pod DENIED (source-IP session-binding + ledger session-close; Cilium restricts
  WHICH pods can reach the broker); post-job replay DENIED (session closed at job-end);
  over-budget/over-rate/over-concurrency/expired/wrong-`aud`/wrong-model DENIED; a compromised broker
  cannot MINT (no private key) — but can spend its own OAuth (a trusted tier, contained by ceilings/audit).
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