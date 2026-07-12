# P2 R-2 — model-gateway broker + credential split + isolated OpenBao provisioner

## Codex Review

- The three-way credential split and application-layer reconstruction are the right boundary shape, and the design correctly identifies CLI compatibility as load-bearing.
- Phase B must remain blocked until version-pinned Claude Code and Codex binaries complete real end-to-end sandbox runs; current R-1 code cannot deliver the capability and cannot run Codex correctly through `SandboxExecutor`.
- The capability protocol is not implementation-ready: single-use conflicts with multi-turn agents, HMAC/transit conflicts with the stated trust topology, and replay/budget enforcement lacks replica-safe durable state.
- The credential split is not currently enforced end-to-end: the tenant ESO role can read the workspace wildcard, while the CP-controlled renderer/admission path can materialize sibling secrets.
- The proposed OpenBao provisioner alternatives do not yet prove non-escalation; policy/role payloads must be synthesized from fixed operator templates, with lifecycle and adversarial verification specified.

## Context
R-1 (merged) delivered the sandbox boundary CORE: untrusted code runs in a separate ephemeral Kata Job
(tokenless SA, restricted PSA, **zero egress** — R-1 exercises test/setup jobs + the import path, NOT
live agent runs). R-2 is "creds/model": it lets the **agent** profile actually call the model **without
ever putting the durable inference OAuth in the sandbox**, by routing the sandbox through a standalone
**broker** that holds the OAuth. It also splits the platform's credentials into three disjoint trust
tiers and gives per-tenant secret provisioning an **isolated authority** (the CP must not hold OpenBao
root). R-2 does NOT flip `privilege_hardening: v1.1` (that is gated on the 6 boundary proofs + the
image-build activation); it makes the agent path exist behind the boundary.
<!-- codex: The gate is underspecified operationally: the six proofs require a live v1.1-shaped shadow deployment, so define how R-2 is deployed and exercised before the production flip rather than merely “rendered dormant.” -->

Binding constraints still in force: engines = subscriptions + local models only (litellm = local models
only; the broker fronts the **subscription** inference OAuth). codex cross-review of design/impl/review
until alignment, cap 3.
<!-- codex: BLOCKER: the merged `EngineRouter` sends `litellm_local` through the same SandboxExecutor, which strips its `ANTHROPIC_AUTH_TOKEN` and overwrites its base URL with the subscription broker. R-2 must define a broker route and broker-held LiteLLM credential for local models, or a separate equally constrained local-model path. -->

## Approach

### 1. The three-pod trust topology (R-2 makes the broker real)
- **orchestrator** pod (trusted): forge PATs + CP bearer + git-push; creates/reaps sandbox Jobs; does the
  trusted-checkout export + forge writes. Mounts `orchestrator-creds` ONLY. Never holds the inference
  OAuth; never runs the gateway. (R-1, unchanged.)
<!-- codex: Capability signing changes this trust tier and must be named explicitly: an HMAC signing key is another durable credential, while OpenBao transit would contradict “no OpenBao access” unless the orchestrator receives a narrowly scoped transit identity. -->
- **broker** pod (trusted-but-ISOLATED): a NEW `Deployment` + `ClusterIP` Service, its own namespace or a
  dedicated SA in the sandbox-adjacent ns. Mounts `broker-oauth` ONLY (the durable inference credential).
  No forge/CP/OpenBao access, no git, no filesystem sharing with the orchestrator. RBAC: none beyond its
  own pod. Runs the **model gateway** (below).
<!-- codex: “Own namespace or dedicated SA” is not an interchangeable choice for this boundary. Require an operator-owned namespace, `automountServiceAccountToken: false`, default-deny policy, pinned workload admission, and no CP/reconciler write authority; an SA alone does not stop a workload creator from mounting the Secret. -->
<!-- codex: The design alternates between one broker and per-tenant/per-account controls without defining capability→provider/account→credential selection. Decide whether deployments are per account/provider or shared, and ensure the sandbox cannot choose another account by changing a request field. -->
- **sandbox** Job (untrusted): agent profile gets egress to the **broker ClusterIP + DNS ONLY** and a
  **per-job broker capability** (a short-TTL token in a projected/emptyDir mount) — NOT a durable cred.
  test/setup profile keeps R-1's zero egress. Mounts: job subPath + writable home + (agent only) broker-cap.
<!-- codex: BLOCKER: merged R-1 permits only a read-only `broker-cap` emptyDir, forbids init containers, and rejects projected volumes; an emptyDir has no producer before the sole sandbox container starts. Choose a workable delivery protocol and update both VAPs and the manifest contract atomically. -->
<!-- codex: Allowing unrestricted DNS to an untrusted pod permits DNS tunneling through CoreDNS. Use a direct broker service address or Cilium DNS L7 rules restricted to the exact broker name, and prove arbitrary query names and alternate resolvers are denied. -->

### 2. Model gateway broker (durable OAuth never in the sandbox)
A **standalone application-layer gateway** (NOT a generic HTTP/TCP proxy, NOT a sidecar). A new service in
the `agentforge` repo (`src/agentforge/broker/`), shipped in the SAME worker image (subcommand
`agentforge broker`), deployed by the operator (ailab) as its own Deployment.
<!-- codex: The merged repository deliberately separates a CLI-free orchestrator image from the CLI-heavy sandbox image. Specify which image target contains the broker and consider a minimal dedicated broker target so the durable OAuth is not exposed to unnecessary git, Kubernetes, dashboard, or agent-CLI dependencies. -->
- **Request reconstruction**: hard-code upstream scheme/host/port + verified TLS (pinned CA); reconstruct
  each upstream request from a validated ALLOWLIST of path/method/headers. Reject CONNECT, absolute-form
  targets, redirects, proxy/hop-by-hop headers, conflicting Content-Length/Transfer-Encoding, unsupported
  upgrades, arbitrary upstream headers. The sandbox agent CLI is pointed at the broker base-URL; the
  broker injects the real `Authorization` from `broker-oauth` (the sandbox never sees it).
<!-- codex: Header/path allowlisting alone is incomplete. Parse and reconstruct an exact JSON schema, including normalized raw path/query handling, model and token fields, duplicate/control-character headers, compressed-body bombs, and any provider fields that trigger uploads, tools, or server-side URL fetches. -->
<!-- codex: Disable redirects in the upstream client, strip every inbound credential/cookie header before reconstruction, and never return upstream request headers in errors. “Pinned CA” is ordinary trust-store pinning unless the exact CA/SPKI, SNI, hostname verification, rotation procedure, and DNS-rebinding behavior are specified. -->
<!-- codex: Streaming needs its own bounded protocol: maximum SSE event/line/total bytes, idle and wall deadlines, backpressure, disconnect cancellation, error-frame handling, decompression limits, and cumulative accounting after a stream starts. Raw-socket smuggling tests must also cover any ingress proxy in front of the ASGI server. -->
- **Per-job capability**: the orchestrator mints a short-TTL, **one-job / one-model** capability (signed
  token bound to `job_id` + model + expiry + a single-use nonce, HMAC or the OpenBao transit engine — TBD
  in review). The broker verifies valid + unexpired + **unreplayed** (nonce cache / one-shot). Cilium
  additionally restricts broker ingress to sandbox pods carrying that job's Cilium identity. Layered:
  network identity (Cilium) + application capability (broker), so neither alone is the boundary.
<!-- codex: BLOCKER: a single-use nonce authorizes only one HTTP request, while Claude Code and Codex perform multiple streamed model calls per job. Define a reusable session capability plus per-request sequencing, or another protocol that supports multiple calls without making restart replay protection illusory. -->
<!-- codex: Do not leave HMAC versus transit open for Phase B. HMAC lets a compromised verifier broker mint capabilities; transit requires broker/orchestrator OpenBao connectivity, while an asymmetric orchestrator signature lets the broker hold only public verification keys and supports `kid` rotation. -->
<!-- codex: Bind issuer, audience, tenant/workspace, account/provider, immutable job identifier, allowed route/method/model set, not-before/expiry, and hard quota ceilings. The broker must apply operator-owned ceilings independently because the trusted orchestrator can otherwise over-mint budget, duration, or model authority. -->
<!-- codex: A dynamic job-id label is not automatically visible to the broker, and a static Cilium selector cannot compare it with a token claim. Specify who creates per-job policy or how source identity is attested to the broker; the current orchestrator RBAC cannot create Cilium policies, and per-job identities risk identity/policy churn. -->
<!-- codex: Both controls are issued or selected by the orchestrator, so they are independent against a compromised sandbox but not against a compromised orchestrator. State that threat-model limitation instead of claiming unconditional independence. -->
- **Bounds (fail-closed, per job/account)**: request/response size, model set, token budget, concurrency,
  rate, duration, and **spend**. Readiness + capacity so the broker is not an unbounded SPOF (bounded
  queue, load-shed, HPA-or-fixed-replicas TBD). Audited: every request → an audit event (job, model,
  tokens, decision) with NO secret/payload.
<!-- codex: BLOCKER: nonce, rate, concurrency, cumulative token, and spend state cannot live only in broker memory if restarts or replicas are allowed. Select an atomic shared ledger and reserve worst-case tokens/spend before dispatch so concurrent requests cannot overshoot; define fail-closed behavior when that store is unavailable. -->
<!-- codex: “HPA-or-fixed-replicas TBD” is not a capacity design. Specify replica count/PDB, bounded queue depth, per-tenant fairness, admission response (429/503), maximum open streams, connection-pool limits, rollout behavior, and recovery when the subscription credential or upstream is unavailable. -->
<!-- codex: Dollar spend may not be reported for subscription OAuth and post-response usage arrives too late to be a hard pre-call bound. Define the enforceable unit, trusted pricing/model-alias source, reservation/reconciliation rules, and persistence semantics rather than promising an unverified spend boundary. -->
<!-- codex: Audit delivery is a hidden egress and availability dependency. Define the sink, buffering/drop policy, cardinality bounds, and sanitization of URLs, exceptions, upstream bodies, capabilities, and OAuth refresh failures; security decisions must not depend on an unavailable audit sink unless explicitly fail-closed. -->
- **Preflight (do FIRST, before building)**: verify Claude Code (`CLAUDE_CODE_...`/`ANTHROPIC_BASE_URL`)
  and Codex actually support pointing at a custom base-URL with the broker's streaming + auth behavior.
  If a CLI won't accept a broker base-URL, the design must adapt (apiKeyHelper, or a per-CLI shim). This
  is the single biggest unknown — the review MUST confirm the CLI contract before Phase B.
<!-- codex: This is honestly identified as load-bearing, but “before building” must be a hard Phase-B entry gate with recorded artifacts. Test digest/version-pinned production binaries through the real Kata Job and a recording fake broker, not only a local CLI invocation. -->
<!-- codex: For each CLI record the exact base-URL/config mechanism, endpoints and query strings, auth header it can populate from the capability, request/response schemas, SSE and cancellation behavior, auxiliary model calls, retries, redirects, telemetry/update traffic, OAuth-refresh traffic, and whether the capability is persisted to home/workspace/logs. -->
<!-- codex: The preflight must also prove broker-side use and refresh of subscription credentials is technically and operationally supported, including required OAuth domains/scopes and provider constraints; accepting a custom CLI base URL alone does not prove the broker can impersonate the CLI upstream. -->
<!-- codex: Claude Code may use more than one configured model for helper/compact calls, so “one-model” can break valid jobs. Capture the actual model set and either bind an explicit small allowlist or prove the pinned CLI uses exactly one model. -->
<!-- codex: BLOCKER independent of the HTTP contract: merged `SandboxExecutor` discards `ExecSpec.stdin`, while Codex reads its prompt from stdin, and Codex argv/output paths contain orchestrator-host `job.cwd` paths rather than `/workspace`. The gate must include fixing and testing stdin plus path translation. -->

### 3. Credential split (three disjoint secrets, ESO-provisioned)
- `orchestrator-creds` (forge PATs + CP bearer + git-push) → **orchestrator** ns/SA only. (R-1.)
- `broker-oauth` (the subscription inference OAuth: `CLAUDE_CODE_OAUTH_TOKEN`, codex `auth.json`) →
  **broker** ns/SA only. Codex's auth.json auto-refreshes → a writable `emptyDir` seeded by an init
  container from the ESO Secret, not an RO mount (per the design's recorded caveat).
<!-- codex: A refreshed auth.json in emptyDir is lost on restart; if refresh-token rotation invalidates the ESO seed, the next pod cannot authenticate. Define secure write-back/rotation or a dedicated refresh owner, and prevent multiple replicas from racing refreshes against one credential. -->
<!-- codex: The gateway is not the Codex CLI, so “Codex auto-refreshes” is an unverified assumption unless the broker deliberately runs a supported helper. Specify who parses, refreshes, locks, validates, and atomically replaces auth.json and how malformed or partially written state fails closed. -->
- sandbox → **nothing** (agent profile: only the per-job broker capability; test/setup: nothing).
<!-- codex: The capability is intentionally readable by hostile sandbox code, but its delivery through an API-key helper or CLI config may copy it into writable home, the staged workspace, crash output, or command arguments. Require a test that it never enters imported files, process argv, provider headers sent elsewhere, or published logs. -->
- Each secret is a per-consumer ESO `ExternalSecret` from a per-tenant OpenBao path; NO secret is
  reachable by a pod outside its tier (enforced by ns/SA + the sandbox-guard VAP forbidding secret volumes
  + no env valueFrom).
<!-- codex: CRITICAL: merged R-1 grants the tenant SecretStore role read access to `af/data/<org>/<workspace>/*`, while tenant-guard permits CP-written ExternalSecrets with arbitrary target names and any sibling key in that subtree. The CP path can therefore materialize `broker-oauth` into the orchestrator namespace unless broker credentials use a distinct auth role/policy not readable by tenant ESO. -->
<!-- codex: Namespace/SA placement does not itself prevent a pod from mounting a Secret; workload-create/update authority and admission do. Add negative proofs that the CP reconciler, tenant SAs, orchestrator SA, and sandbox Job creator cannot create or mutate any workload/ExternalSecret that references the other tier's Secret. -->

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
<!-- codex: BLOCKER: option (a) does not establish confinement merely by restricting policy/role names. OpenBao ACL paths control which policy/role objects may be written, not the privileges or bound identities inside their payloads, so such a token can be root-equivalent unless the server enforces allowed parameters/content. -->
<!-- codex: Option (b) is safe only if the operator controller ignores CP-supplied policy/role bodies and synthesizes them from validated identifiers and immutable templates. Blindly applying a CP-rendered Job or policy simply moves the confused-deputy escalation into the controller. -->
<!-- codex: Choose one mechanism before implementation and specify its input schema, authenticated source, reconciliation ordering, idempotency, rotation, deletion/revocation, orphan/drift repair, audit, and break-glass procedure. Leaving a security-boundary controller as two broad options makes R-2 non-implementation-ready and invites scope expansion. -->
<!-- codex: Normalize the path terminology: R-1 uses KV mount `af`, mount-relative key `<org>/<workspace>/...`, and ACL API path `af/data/<org>/<workspace>/*`; `af/<org>/<ws>/*` is ambiguous and can produce an incorrectly scoped policy. -->
<!-- codex: A single workspace-wildcard ESO policy is incompatible with the claimed per-consumer credential split. Define separate orchestrator and broker auth roles/policies, with the broker role operator-owned and unavailable through the CP-controlled tenant SecretStore. -->

### 5. Cilium (broker ingress/egress + agent-profile egress)
- **broker ingress**: ONLY from sandbox pods carrying the matching job Cilium identity (label selector +
  the capability at L7). Deny everything else.
<!-- codex: Cilium NetworkPolicy does not validate the application capability “at L7” unless an Envoy rule explicitly implements that protocol, which is not designed here. Treat capability verification as broker logic and specify the concrete Cilium selector/source-identity correlation separately. -->
- **broker egress**: ONLY to the pinned model upstream (FQDN/CIDR) + DNS. No cluster-internal reach.
<!-- codex: FQDN and CIDR are not equivalent under CDN rotation, and OAuth refresh may require separate auth hosts. Enumerate provider/API/auth/DNS destinations from the preflight, deny private/link-local/service/node ranges explicitly, and test IPv4, IPv6, CNAME, DNS-rebinding, and stale-IP behavior. -->
<!-- codex: Shared quota/audit stores, metrics, readiness probes, and OpenBao transit would add ingress or egress dependencies that contradict “only upstream + DNS.” Resolve those dependencies before writing policy. -->
- **sandbox agent profile egress**: ONLY the broker ClusterIP + cluster DNS. Direct-IP / IPv6 / alt-DNS /
  metadata / node-local / service-CIDR all DENIED (belt to the broker's own request reconstruction).
<!-- codex: The parent design assigns the sandbox agent/test Cilium profiles and FQDN egress hardening to R-3, while this R-2 correctly needs agent→broker and broker→upstream enforcement to make live agents safe. Resolve the phase contract: minimum enforceable policies are R-2 blockers; only additional canary/hardening work may remain R-3. -->
- **sandbox test/setup profile**: ZERO egress (R-1, unchanged).

### 6. Integration with R-1 (what changes in the merged code)
- **agentforge**: add `src/agentforge/broker/` (the gateway app + `agentforge broker` subcommand);
  extend `build_job_manifest` so the **agent** profile adds the broker-cap volume/mount + the broker
  base-URL env + points the agent CLI at it (test/setup unchanged); capability minting in the orchestrator
  run path (before creating an agent Job). Keep LocalExecutor + all existing tests green.
<!-- codex: The merged manifest already adds the broker-cap emptyDir/mount and `ANTHROPIC_BASE_URL`/`AF_BROKER_URL` placeholders; R-2 must replace the placeholder with an actual producer/auth integration, not merely “extend” the same shape. Add a fail-fast check rejecting an empty broker URL or missing capability before Job creation. -->
<!-- codex: `ExecSpec` currently carries only coarse `creds`/`egress`, not engine, provider, account, model, or budget, so `SandboxExecutor` cannot mint the claimed tightly bound capability without parsing argv/env. Extend the typed execution contract and derive claims from trusted role/account configuration. -->
<!-- codex: Merged `SandboxExecutor` ignores stdin and performs no host→sandbox path rewriting; Codex therefore receives no prompt and references nonexistent host paths for `--cd` and `--output-last-message`. Add real-agent tests covering stdin EOF, `/workspace` translation, output import, writable home discovery, and both CLIs. -->
<!-- codex: The R-1 VAPs do not pin exact env names/values; they only prohibit `valueFrom`, and `_container_env` forwards nearly every literal from `spec.env` via a denylist. Replace credential-name denial with an explicit per-engine environment allowlist and test unknown/new provider credential names cannot cross the boundary. -->
<!-- codex: If preflight requires `apiKeyHelper`, immutable CLI configuration, extra env, a wrapper, or a different volume shape, both sandbox-guard and sandbox-job-guard must change together in the operator repo. The current VAPs forbid init containers and every projected/Secret/ConfigMap volume. -->
- **agentforge-platform (renderer)**: render the broker `Deployment` + `ClusterIP` + `broker-oauth`
  ExternalSecret + the agent-profile broker wiring env, gated by `privilege_hardening == v1.1` (dormant
  until the flip). Extend `assert_allowlisted` for the new GVKs (Service). Cross-repo test as in R-1.
<!-- codex: CRITICAL ownership contradiction: this renderer writes only CP-controlled `af-tenant-*` manifests, while the topology says the broker and its OAuth are operator-owned and CP-unwritable. Rendering the broker here either fails the namespace assertion or gives the CP workload authority over the durable OAuth; broker Deployment/Service/ExternalSecret must stay in ailab/operator ownership. -->
<!-- codex: Simply adding Service to `ALLOWED_GVKS` is unsafe and unnecessary for an operator-owned broker; the current deployment allowlist also does not pin image, SA, volumes, env, or Secret references. Do not widen the tenant path for this boundary without exact field pins in both renderer assertions and tenant-guard. -->
<!-- codex: “Agent-profile broker wiring env” is runtime Job content produced by agentforge, not a platform-rendered pod field. The renderer should only provide validated non-secret broker configuration to the orchestrator; capability values must never be committed to Git. -->
- **ailab**: the broker Deployment/Service/NetworkPolicies + the isolated OpenBao provisioner (operator-
  owned) + Cilium policies + the broker-oauth secret plumbing; sandbox-guard VAP already allows the
  broker-cap volume (R-1) — verify the agent-profile shape matches.
<!-- codex: A single authoritative owner is required: this bullet correctly assigns broker resources to ailab, conflicting with the preceding renderer bullet. Keep the CP renderer limited to references/configuration and add operator admission plus reconciliation tests for the broker namespace. -->
<!-- codex: “Already allows” hides the fatal emptyDir issue: it allows exactly a disk-backed read-only-mounted emptyDir but no mechanism that can populate it. The verification must assert the selected delivery shape is both admitted and contains the intended token at process start. -->

## Critical files
- agentforge: NEW `src/agentforge/broker/{gateway,capability,bounds,audit}.py`, `main.py` (`broker`
  subcommand + agent-profile capability mint), `adapters/exec/sandbox.py` (agent-profile broker wiring),
  `infra/settings.py` (broker/upstream/capability knobs), `deploy/` (broker runs from the worker image).
<!-- codex: Missing critical surfaces include the typed `ExecSpec`/runner changes for model/account/budget, Codex stdin/path translation, OAuth refresh handling, shared replay/quota storage, provider-specific schema adapters, and secret-safe logging middleware. -->
- agentforge-platform: `adapters/gitops/renderer.py` (broker Deployment/Service/ExternalSecret + agent
  env), `settings.py`, tests.
<!-- codex: Remove operator-owned broker resources and OAuth from this CP-controlled file list; retain only validated broker endpoint/policy references if needed. Otherwise this directly violates the stated admission and credential ownership split. -->
- ailab: `kubernetes/apps/.../agentforge-broker/**` (Deployment/Service/NetworkPolicy/ESO), the isolated
  OpenBao provisioner (operator-owned), Cilium policies, `broker-oauth` ESO/SecretStore.
<!-- codex: Add the broker namespace/admission policy, dedicated SA with token automount disabled, PDB/resource quota, TLS/config material ownership, and ESO role/policy bootstrap ordering to the critical manifests. -->

## Verification (R-2 boundary proofs — extend the 6)
- The sandbox agent profile can reach the model ONLY via the broker; a hostile agent CANNOT read the
  inference OAuth (not mounted), CANNOT reach the upstream directly (Cilium), CANNOT replay/forge a
  capability (broker nonce + Cilium identity), CANNOT reach forge/OpenBao/CP.
<!-- codex: Exercise token reuse across legitimate multi-turn calls, duplicate requests, concurrent requests, broker restart, replica failover, rollout, expiry during a stream, job deletion, and replay from another agent/test pod. Also prove DNS queries cannot become an exfiltration channel. -->
<!-- codex: Prove absence through Pod specs, `/proc`, env, argv, mounted files, writable home/workspace, stdout/stderr, events, audit/log/metric output, and provider error responses—not only by checking that the OAuth Secret volume is absent. -->
- The broker rejects CONNECT/absolute-form/redirect/smuggling/arbitrary-header/oversize/over-budget/
  over-rate/expired-capability; scanner/verify failures fail closed; the broker cannot read
  orchestrator-creds and the orchestrator cannot read broker-oauth.
<!-- codex: Use raw HTTP/1.1 and HTTP/2 conformance/fuzz cases through the deployed ingress path, including duplicate CL, CL+TE variants, obs-fold/control bytes, encoded path traversal, oversized/chunked/compressed bodies, slowloris, SSE floods, disconnects, and redirect chains. Framework-level test clients will not prove smuggling resistance. -->
<!-- codex: Redaction “scanner” failure belongs to R-3 and should not be an R-2 acceptance criterion unless this means broker protocol validation. R-2 must still guarantee that auth headers, capabilities, and OAuth refresh material are never logged, independent of the later publish-sink redactor. -->
<!-- codex: Cross-tier proof must include Kubernetes SubjectAccessReviews and rejected workload mutations/ExternalSecrets, plus an adversarial CP commit attempting to materialize the broker path. Merely showing the two running Deployments mount different named Secrets misses the ESO wildcard escape. -->
- The isolated provisioner cannot create OpenBao authority outside `af/<org>/<ws>/*`; a CP git commit
  cannot escalate OpenBao; cross-tenant secret read is denied (RLS × ns × OpenBao policy).
<!-- codex: Test malicious policy bodies granting root paths, auth roles referencing existing admin policies, foreign/wildcard bound SAs/namespaces/audiences, traversal/Unicode/collision identifiers, role-policy relinking, updates after creation, deletion/recreation, and compromised/stale desired state. RLS is not part of an OpenBao authorization proof unless the provisioner actually consumes an RLS-protected source. -->
<!-- codex: Verify the ESO TokenRequest audience and TTL end-to-end against the exact installed ESO CRD/controller version and OpenBao role configuration; the merged renderer currently does not render an audience. -->
- Preflight PROVEN: Claude Code + Codex work against the broker base-URL with streaming + injected auth.
<!-- codex: Acceptance must name exact CLI versions/digests and preserve request transcripts plus automated regression tests. Include multiple turns, retry/error paths, auxiliary model calls, long SSE streams, capability helper behavior, writable-home restart, and the real SandboxExecutor stdin/path mapping. -->
- All existing R-1/P1 tests stay green; the flip stays gated (R-2 renders dormant until v1.1).
<!-- codex: Existing tests are insufficient because R-1 intentionally never exercised a live agent. Add cross-repo rendered-Job→VAP admission tests, live Kata Claude/Codex canaries, local-LiteLLM routing, broker restart/replica tests, ESO refresh/rotation tests, and negative CP/operator ownership tests before calling R-2 complete. -->

<!-- codex-review-status: complete -->