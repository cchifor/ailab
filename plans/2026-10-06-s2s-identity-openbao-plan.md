# S2S service identities without shared secrets (projected ServiceAccount tokens)

Source: dev-worker-1's proposal (2026-10-06): an automatable path for new platform service
identities on ailab that needs no SOPS edit, no age key and no checksum bump. First consumer:
`svc-harness` (platform PR #2092, H9.1b, blocked on owner runbook steps 9 and 10).

Repos: **ailab** (this repo, origin/main 2a201189) and **platform** (`cchifor/platform`, gitea
main 78c0f56b3; a read-only checkout is at
`C:/Users/chifo/AppData/Local/Temp/claude/C--Users-chifo-work-ailab/28ad994b-62ac-44f1-8418-fdb632fe1a57/scratchpad/pmain`).

## Review trail (round 1)

Three designs were reviewed:
- **A-full**: the proposal plus Codex's hardening. An OpenBao-generated preshared secret, ESO
  rendering, and a hot-reloaded extras registry.
- **A-lite**: the preshared design trimmed down. A digest Secret, grants in a ConfigMap, and a
  re-read on a lookup miss.
- **B**: projected ServiceAccount tokens.

The reviewers split. **Codex** picked A-full. **Fable** picked B. Most of Codex's 33 round-1
findings (rotation barriers, reload coherence, last-good authority, ESO failure domains, wildcard
plaintext reads, recovery drills) are costs of having a long-lived shared secret and an
ESO-rendered, hot-reloaded registry. B removes those categories outright.

**The owner chose B (2026-10-06)**, and #2092 waits until B is live with no further SOPS edits.
Round-1 findings that still apply under B are folded in below and tagged with their source.

## Context

Today, adding an S2S identity on ailab takes the age private key twice:
1. `deploy/secrets/ailab/gatekeeper-secrets.enc.yaml` gets the registry entry, holding an
   argon2id `secret_hash` and the grants. Gatekeeper loads the registry once at startup
   (`infra/gatekeeper/src/app/core/lifecycle.py:129`), so `gatekeeper.serviceRegistry.checksum`
   has to be bumped to roll the pods.
2. `harness-secrets.enc.yaml` holds the plaintext client secret plus a DSN whose password lives in
   OpenBao `af/strive/pg-harness`.

No agent holds the age key, and none should.

### Verified facts (2026-10-06)

| # | Fact | Bearing |
|---|---|---|
| F1 | `svc-harness` is a **cross-tenant service authority**. The `client_credentials` path (`service_token.py:197-245`) mints every granted scope for any caller-supplied `tenant_id`, with no user token. The dev registry comment saying "a stolen credential mints nothing without a live person" (`service_registry.yaml:306-309`) is wrong. | A long-lived harness secret is a high-value credential. Not having one at all is the main payoff of B. |
| F2 | The cluster **does not serve** OIDC discovery or `/openid/v1/jwks` (NotFound). The SA token issuer is `https://192.168.0.40:6443` (k8s v1.31.4). | The existing JWKS-based `ProjectedSATokenVerifier` cannot be used as it is. B verifies with the **TokenReview API**. |
| F3 | Gatekeeper's SA `gatekeeper` has automount on, so its pods already hold an API token. `system:service-account-issuer-discovery` is bound to `system:serviceaccounts`. | Gatekeeper can call TokenReview once it is granted `system:auth-delegator`. |
| F4 | Under Cilium, the API server carries the `kube-apiserver` entity, and k8s `ipBlock` rules cannot admit it. Precedent: ailab `apps/ci-rerun-watchdog/api-egress-cilium.yaml`. The platform chart already renders CiliumNetworkPolicies (`charts/airlock/templates/ciliumnetworkpolicy.yaml`). | Gatekeeper needs a CNP `toEntities: [kube-apiserver]`, rendered by its own chart behind an ailab value. |
| F5 | The harness authenticates only with `client_secret_post`: the token mint at `s2s.ts:268` and the delegation-grant revoke at `s2s.ts:470`. It treats a 401 as a verdict, not an outage (`s2s.ts:275-279`). | The harness needs a token-file Bearer mode. Gatekeeper must answer an API-server outage with 503, not 401. |
| F6 | Python services use `weld-auth`'s `S2SClient`, which supports only `client_secret` (`sdks/weld-auth/src/weld/auth/s2s_client.py:98-121,400`). | Only the harness benefits now. Python services follow after a weld-auth change (a follow-up). |
| F7 | ESO (chart 2.7.0) serves only `external-secrets.io/v1`. The `strive-pg-harness-store` SecretStore and ExternalSecret are live and synced, and the store's policy already reads `af/data/strive/pg-harness`. | The harness DSN needs **one** templated ExternalSecret on the existing store, with no new OpenBao objects. |
| F8 | Both repos' `main` require 1 approval from anyone (bots included), with no protected file patterns. ailab has no required status checks. ailab's Flux applies into `strive-ailab` as cluster-admin (`clusters/ai/strive-pg-harness.yaml` has no `serviceAccountName`). | The SOPS step is today's only human gate on S2S grants, and it was never a complete one. B needs an explicit owner gate on the grant file. ailab Flux remains a residual trust path. |
| F9 | The HelmRelease takes a `valuesFiles` list (`deploy/gitops/flux/clusters/ailab/app/helmrelease.yaml:22-24`). | The S2S grants can live in their own small values file that is easy to protect. |
| F10 | Gatekeeper on ailab runs 2 replicas, PDB minAvailable 1, `maxUnavailable: 0`. | A pod roll when the registry changes costs no availability. Function is verified per replica after each roll. |

## Decision

**B: service identity comes from Kubernetes, not from a secret.** A new service proves who it is
with its pod's projected ServiceAccount token. Gatekeeper verifies the token through TokenReview
and maps the SA to a registry entry. The grants are plain config in git, rendered by the gatekeeper
chart into a ConfigMap and gated by owner approval. The SOPS registry and its existing
preshared identities stay exactly as they are.

### How it works

**Provisioning a service identity.** There is no secret to provision.
1. The service's chart already creates its ServiceAccount: `harness` in `strive-ailab` gives
   `system:serviceaccount:strive-ailab:harness`.
2. Its pod gets a projected token volume with `audience: gatekeeper`,
   `expirationSeconds: 3600`, mounted at `/var/run/secrets/tokens/gatekeeper`. The kubelet mints
   the token and refreshes it before it expires. The token is bound to the pod, so it dies when
   the pod is deleted.
3. One entry in `deploy/helm/values/providers/ailab-s2s-registry.yaml`, the owner-gated file,
   declares `client_id: svc-harness`, `auth_method: k8s`,
   `k8s_subject: system:serviceaccount:strive-ailab:harness`, and the audiences, scopes and
   `may_act_for_audiences`.
4. The gatekeeper chart renders that list into ConfigMap `gatekeeper-registry-extras` and adds
   its checksum as a pod annotation. Helm applies the ConfigMap and rolls gatekeeper in the same
   upgrade. One controller does both, so the SOPS-vs-Helm race documented in
   `charts/gatekeeper/templates/deployment.yaml:44-69` does not apply.
5. At startup gatekeeper loads the SOPS base registry and validates it. Only then does it merge
   the extras document.

Adding the next service is therefore one owner-approved values entry plus a client that sends
its token. There is no OpenBao path, provision Job, ExternalSecret, checksum bump or rotation.

**Authenticating a call** (`POST /auth/token`; delegation-grant issue and revoke work the same):
1. The harness reads the token file **on every mint**, because it rotates. It sends
   `Authorization: Bearer <token>` together with the form fields `client_id=svc-harness`,
   `grant_type`, `audience`, and `scope`/`tenant_id`/`subject_token`. It sends no `client_secret`.
2. Gatekeeper (`SVC_AUTH_BACKEND=composite`) looks up the entry for the form `client_id`. The
   entry's explicit `auth_method` picks exactly one verifier. There is no fallback, so an attacker
   cannot downgrade a k8s identity to a guessed preshared secret, or the reverse.
   - `preshared`, the default and every SOPS entry: argon2 against `secret_hash`, unchanged.
   - `k8s`: a TokenReview to the in-cluster API with `spec.audiences: ["gatekeeper"]`. It must
     return `authenticated: true`, include `gatekeeper` in `status.audiences`, and give a
     `user.username` equal to the entry's `k8s_subject`. Otherwise the call gets a 401 or 403
     with today's RFC 6749 codes. If the API server is unreachable, the call gets a **503**, so
     the harness treats it as an outage. A successful review is cached by `sha256(token)` for at
     most 60 s and never past the token's `exp`.
3. From the registry lookup onward, the existing flow is unchanged: the audience check, scope
   intersection, `client_credentials` or token exchange, and a JWT with the 300 s TTL.

**The database URL.** A new ExternalSecret `strive-pg-harness-dsn` sits in ailab's existing
`infrastructure/strive-pg-harness/` tree and reads through the existing store. It renders
`database-url: postgres://harness:{{ .password }}@strive-pg-rw.strive-ailab.svc.cluster.local:5432/harness`
into Secret `strive-pg-harness-dsn`. The harness values point `HARNESS_DATABASE_URL` at it.

**Revocation.**
- Normal: remove the values entry. Helm rolls gatekeeper, and new mints get a 401 once each
  replica has rolled.
- Emergency: delete the harness pod or scale it to 0. Its bound token stops passing TokenReview
  immediately, and the 60 s cache bounds the tail.
- In both cases, JWTs already issued live out their 300 s TTL. There is no S2S token revocation
  list; that is recorded, not changed.

### Pros and cons

**Pros**
- **Nothing to steal at rest.** No long-lived harness credential exists in OpenBao, a Secret,
  the pod's env, or an escrow. This matters because of F1. The token on the wire is short-lived
  and bound to its audience and its pod.
- **No secret lifecycle.** Nothing to generate, escrow or rotate, no GENERATED-ONCE recovery
  row, and no dual-secret overlap. The kubelet rotates tokens.
- **No new OpenBao authority paths.** The only OpenBao read is the existing pg-harness password.
- **Grants are reviewable config.** A ConfigMap rendered by Helm and gated by the owner, with no
  hot reload, no cross-controller race, and no manual checksum.
- **An instant kill switch**: delete the pod.
- **It matches gatekeeper's documented production model** (the k8s verifier) and the GKE/AWS
  direction.

**Cons**
- **New security-critical code on the mint path**: the composite verifier, the TokenReview
  verifier, and the harness Bearer mode. These need thorough negative tests.
- **Gatekeeper depends on the API server** for uncached mints. The 3-CP HA API server, the
  harness's own 300 s token cache, and the 60 s review cache keep the load trivial. During an
  API-server outage, k8s identities cannot mint (503), while preshared identities are unaffected.
- **New cluster-scoped RBAC**: a ClusterRoleBinding of the gatekeeper SA to
  `system:auth-delegator`. It is rendered by the chart and gated behind a value. It also needs a
  Cilium egress rule.
- **Only the harness benefits now.** Python services stay on preshared until weld-auth gains the
  mode (F6), so the SOPS registry shrinks only after that follow-up.
- **Trust in the cluster.** Anyone who can create a pod as SA `harness`, or call TokenRequest on
  it, in `strive-ailab` can become svc-harness: Flux controllers and cluster admins. Dev workers
  cannot (`platform-access/rbac.yaml`). That is the same tier that can read Secrets today.

### Rejected
- **A-full / A-lite** (the preshared secret in OpenBao). This is Codex's round-1 verdict. It
  keeps a high-value cross-tenant secret (F1) and its whole lifecycle, which is where most of
  round 1's risk sat. If B stalls, A-lite is the fallback.
- **JWKS-based `ProjectedSATokenVerifier`**: not served on this cluster (F2). Enabling discovery
  would mean changing the Talos API server config for no gain over TokenReview.
- **A ValidatingAdmissionPolicy on the extras ConfigMap** (Fable). The adversary it would stop
  is ailab's cluster-admin Flux, and that same Flux could delete the policy. The real control is
  the owner gate on the platform file. The ailab Flux path is recorded as residual risk (F8).
- **A second age key, or gatekeeper reading OpenBao directly**: as in round 1.

## Approach

PRs land strictly in order. Per the reviewbot rule, a dependent PR is not opened before its
prerequisite is live.

### Phase 0: the owner gate (implemented and tested before any authority path lands)
<!-- codex round-1 [P1], adapted: the gate must be real, tested, and cover the authority path. -->
- On `cchifor/platform` `main`, add Gitea `protected_file_patterns` for:
  - `deploy/helm/values/providers/ailab-s2s-registry.yaml`
  - `deploy/helm/charts/gatekeeper/templates/registry-extras*.yaml`
  - `deploy/helm/charts/gatekeeper/templates/tokenreview-rbac.yaml`
  - `infra/gatekeeper/src/app/gatekeeper/service_{registry,verifier,token}.py`
  - `infra/gatekeeper/src/app/gatekeeper/tokenreview_verifier.py`
- This is an owner action; the API token may lack admin scope.
- **Test it**: a throwaway PR that touches one protected file, approved by the reviewer bot,
  must be unmergeable by the bot's merge credentials. The owner can still merge it. Record the
  Gitea version and the observed behaviour in the runbook.
- If `protected_file_patterns` does not block the bot merge, fall back to
  `enable_approvals_whitelist` with the owner as the only whitelisted approver on a dedicated
  branch rule, or stop and re-plan. Do **not** proceed with an untested gate.

### Phase 1: ailab PR, the harness DSN (harmless while the harness is dark)
- `kubernetes/apps/infrastructure/strive-pg-harness/eso.yaml` gets ExternalSecret
  `strive-pg-harness-dsn` on `strive-pg-harness-store`, using `target.template`
  (`engineVersion: v2`, `mergePolicy: Replace`) to render the key `database-url` only.
- This is the existing tree, so its Flux `dependsOn: external-secrets` and its
  `scripts/tests/test_manifest_paths.py` entry already exist (codex round-1 [P2]).
- `docs/runbooks/infra-pg.md`: the DB password rotation order becomes
  1. `bao kv patch`;
  2. force-sync both ExternalSecrets (`strive-pg-harness`, `strive-pg-harness-dsn`);
  3. re-run the bootstrap Job and confirm it applied the new password;
  4. restart the harness and wait for `/ready`.

  (codex round-1 [P1], DSN rotation coordination.)
- Verify: `SecretSynced` on both ExternalSecrets. The rendered DSN is proven by the harness
  `migrate` init container on the flip. Dev workers cannot read Secrets there, so the owner
  inspects `kubectl get secret … -o jsonpath` if a pre-flip check is wanted.

### Phase 2a: platform PR, gatekeeper code (parallel with 2b)
- `service_registry.py`:
  - `ServiceClient.auth_method: Literal["preshared", "k8s"] = "preshared"`. This is explicit,
    never inferred from `k8s_subject`: dev and SOPS entries carry `k8s_subject` values for the
    `platform` namespace, and inferring would flip them.
  - `load_registry_with_extras(base_path, extras_path)`:
    - The base must load and validate first. If it fails, extras are **not** merged and the
      registry stays empty, failing closed as today (codex round-1 [P1]).
    - The extras document uses the existing `services:` schema and is validated whole. If it is
      invalid, no extras are merged and the error is logged.
    - Each extras entry must have `auth_method: k8s` and a `k8s_subject` of the form
      `system:serviceaccount:<ns>:<name>`, must have **no** `secret_hash`, and must have a
      `client_id` matching `^svc-[a-z0-9-]+$`.
    - A `client_id` or `k8s_subject` already present in the base, or duplicated within extras,
      is refused for that entry, with an error log and metric. The base is never shadowed.
    - A missing extras file (an optional ConfigMap volume) means no extras.
  - The registry stays immutable for the life of the process (startup load only), so no
    snapshot or swap machinery is needed.
- `tokenreview_verifier.py` (new): `TokenReviewVerifier(registry, audience)`.
  - It uses in-cluster config: `KUBERNETES_SERVICE_HOST`, the SA token and CA under
    `/var/run/secrets/kubernetes.io/serviceaccount/`, and `httpx` with that CA.
  - It handles the review result and maps the subject to the registry entry. The form
    `client_id` must equal the mapped entry (the same anti-impersonation rule as
    `ProjectedSATokenVerifier`).
  - It keeps the TTL cache.
  - Transport or 5xx errors raise a new `ClientAuthUnavailable`. `service_token.py` and the
    delegation-grant routes map it to **503** `temporarily_unavailable`.
- `service_verifier.py`: `CompositeVerifier(preshared, k8s)` dispatches on the entry's
  `auth_method`. An unknown client gets the same generic 401 as today. `build_verifier` gains
  `backend="composite"`. The `preshared` backend refuses to start if extras contain k8s entries,
  and logs that it is ignoring them, so it fails closed.
- Every route that authenticates a client goes through `app.state.service_verifier`. Audit the
  delegation-grant issue/exchange/revoke routes and test each with a k8s client.
- Config: `SERVICE_REGISTRY_EXTRAS_PATH` and `SVC_AUTH_TOKENREVIEW_AUDIENCE` (default
  `gatekeeper`). When both are unset, behaviour is exactly today's.
- Metric: `gatekeeper_service_registry_info{base_sha, extras_sha}`, nonsecret content hashes, so
  replicas can be compared (codex round-1 [P2]). Also
  `gatekeeper_service_registry_extras_refused_total`.
- Tests:
  - Every validation rule above.
  - Base failure leaves extras unmerged.
  - Composite dispatch: a k8s entry ignores `client_secret`; a preshared entry ignores Bearer.
  - TokenReview cases: unauthenticated, wrong audience, wrong subject, `client_id` mismatch,
    API 5xx or timeout returning 503, cache hit and cache expiry.
  - The delegation-grant routes with a k8s client.

### Phase 2b: platform PR, harness Bearer mode (parallel with 2a)
- `services/harness/src/env.ts`: `HARNESS_CLIENT_TOKEN_FILE`. When set, `HARNESS_CLIENT_SECRET`
  is not required.
- `identity-gatekeeper/s2s.ts`: when the token file is configured, read it fresh on every mint,
  issue and revoke (`s2s.ts:268`, `:470`, and the grant issue path), send
  `Authorization: Bearer`, and omit `client_secret`. An unreadable or empty file is a configured
  failure (`IDENTITY_NOT_CONFIGURED`), never a silent unauthenticated call. The token value is
  never logged.
- Gatekeeper's 503 is handled as an outage (`down()`), and existing outage backoff applies.
- Tests: token-file mode (header present, no secret in the body), a rotated file between mints,
  a missing or empty file, and 503 handling.

### Phase 3: platform PR, charts and ailab values (after the 2a and 2b images are built; harness still dark)
- `charts/gatekeeper`:
  - `templates/registry-extras-configmap.yaml` renders `serviceRegistry.extras` into one
    `registry.yaml`.
  - The deployment mounts it `optional: true` at `/etc/gatekeeper/registry-extras/` and adds the
    annotation `checksum/registry-extras`.
  - Env: `SERVICE_REGISTRY_EXTRAS_PATH` and `SVC_AUTH_BACKEND=composite`, set when
    `serviceAuth.composite.enabled`.
  - `templates/tokenreview-rbac.yaml`: a ClusterRoleBinding of the gatekeeper SA to
    `system:auth-delegator`, gated by the same value.
  - `templates/ciliumnetworkpolicy-apiserver.yaml`: `toEntities: [kube-apiserver]` on TCP
    443/6443, gated by `networkPolicy.ciliumApiserverEgress`.
  - Template guards: an extras entry without `k8s_subject`, or with `secret_hash`, fails the
    render.
- `charts/harness`: `serviceAccountToken.gatekeeper.enabled` adds the projected volume
  (`audience: gatekeeper`, `expirationSeconds: 3600`) and `HARNESS_CLIENT_TOKEN_FILE`. When it
  is on, `secretEnv.HARNESS_CLIENT_SECRET` must be absent, and the render fails otherwise.
  Update `check-harness-chart-contract.sh`.
- `deploy/gitops/flux/clusters/ailab/app/helmrelease.yaml`: append the `valuesFiles` entry
  `deploy/helm/values/providers/ailab-s2s-registry.yaml`. This is a new file holding only
  `gatekeeper.serviceRegistry.extras` with the svc-harness entry and its grants, copied from the
  dev registry `service_registry.yaml:281`.
- `values/providers/ailab.yaml`:
  - Pin the new gatekeeper and harness digests.
  - `gatekeeper.serviceAuth.composite.enabled: true` and
    `networkPolicy.ciliumApiserverEgress: true`.
  - Harness: `serviceAccountToken.gatekeeper.enabled: true`. `secretEnv` drops
    `HARNESS_CLIENT_SECRET` and points `HARNESS_DATABASE_URL` at
    `{name: strive-pg-harness-dsn, key: database-url}`. The router key stays `llm-router`.
  - Rewrite the H9.1 comment: steps 9 and 10 are gone.
- A CI test (platform `deploy/helm/scripts/tests/`) asserts that every `ailab-s2s-registry.yaml`
  entry's **grants** (audiences, scopes, `may_act_for_audiences`) equal the dev registry entry
  with the same `client_id`, comparing grants only (Fable). There is no CI collision check:
  the SOPS registry is one encrypted scalar, and gatekeeper refuses collisions at runtime
  (codex round-1 [P1]).
- Docs:
  - `deploy/secrets/ailab/SECRETS.md`: fix the stale "no ESO on ailab" claim and add
    "Service identities without secrets (projected SA tokens)".
  - A short ADR in the platform repo's decisions directory: the S2S auth method selection, F1,
    and the weld-auth follow-up.

### Phase 4: verify, then #2092
- After Phase 3 rolls (harness still dark):
  - Both gatekeeper pods log the registry load and show
    `gatekeeper_service_registry_info{extras_sha=…}` with the same values on both.
  - `..._extras_refused_total` is 0.
  - Existing S2S is unaffected: the e2e lane passes and `report-ailab-pin-drift` shows 0 torn.
- Optional pre-flip probe, **owner-run**, because dev workers cannot create pods in
  `strive-ailab` (codex round-1 [P2]): a one-off pod as SA `harness` with the projected token.
  It POSTs a **complete** request (`grant_type=client_credentials`, `client_id=svc-harness`, an
  allowed `audience` and `scope`, a test `tenant_id`) to each gatekeeper pod IP and expects a
  200 whose JWT carries `client_id` svc-harness. It then checks four refusals:
  - a wrong audience: 403;
  - a token for another audience: 401;
  - `client_id=svc-deepagent` with harness's token: 403;
  - no token: 401.

  Run it with the harness netpol labels, or skip it and rely on the flip's own checks: the flip
  is reversible (`enabled: false`) and has a small blast radius.
- Rebase #2092 (the one `harness.enabled: true` line), rewrite its precondition section, and
  merge it. Then run its post-merge checks: pod Ready, migrate completed, 401 not 302, the `@api`
  journeys, and 0 torn.

## Critical files

ailab:
- `kubernetes/apps/infrastructure/strive-pg-harness/eso.yaml`
- `docs/runbooks/infra-pg.md`

platform:
- `infra/gatekeeper/src/app/gatekeeper/{service_registry,service_verifier,service_token,tokenreview_verifier,config}.py`
- `infra/gatekeeper/src/app/core/lifecycle.py`
- the delegation-grant route module(s)
- `infra/gatekeeper/tests/unit/…`
- `services/harness/src/{env.ts,plugins/identity-gatekeeper/s2s.ts,plugins/identity-gatekeeper/index.ts}` and their tests
- `deploy/helm/charts/gatekeeper/{values.yaml,templates/deployment.yaml,templates/registry-extras-configmap.yaml,templates/tokenreview-rbac.yaml,templates/ciliumnetworkpolicy-apiserver.yaml}`
- `deploy/helm/charts/harness/{values.yaml,templates/deployment.yaml}`
- `deploy/helm/scripts/tests/check-harness-chart-contract.sh` and the grants-parity check
- `deploy/gitops/flux/clusters/ailab/app/helmrelease.yaml`
- `deploy/helm/values/providers/{ailab.yaml,ailab-s2s-registry.yaml}`
- `deploy/secrets/ailab/SECRETS.md`
- an ADR

## Verification

- Phase 0: the bot-merge refusal test passes and is recorded.
- Phase 1: both ExternalSecrets show `SecretSynced`. The DSN is proven by the migrate init
  container on the flip.
- Phase 2a/2b: unit tests are green in CI.
  - A compose run with `SVC_AUTH_BACKEND=composite` and a stubbed TokenReview endpoint mints
    for a k8s extras client and refuses a colliding one.
  - Every preshared client still mints.
- Phase 3: Helm render tests and the chart contract are green, including the failing-render
  guards. Live checks are as in Phase 4.
- **Cold start**: start gatekeeper with the extras ConfigMap absent. It boots on the base
  registry, and svc-harness gets a 401.
- **Rollback**: re-pin the pre-2a gatekeeper image, which drops extras, so svc-harness gets a
  401 and the harness S2S path is off. Documented, not drilled (codex round-1 [P2]).
- **Token rotation drill** (after the flip): keep the harness running past one kubelet refresh
  (more than 48 min at 3600 s) and confirm fresh mints still succeed. Force a fresh mint by
  waiting out the harness's 300 s token cache (codex round-1 [P1], adapted).
- **Revocation drill** (after the flip, in a quiet window): delete the harness pod. Mints with
  the dead pod's token fail within the 60 s cache. Already-issued JWTs expire within 300 s.
  Removing the values entry gives a 401 on each replica after the roll (codex round-1 [P1]).

## Out of scope, recorded
- **svc-harness's `client_credentials` authority** is cross-tenant (F1). Whether the harness
  needs `client_credentials` at all, beyond H9.2's capability publish to svc-mcp, is a grants
  decision for the owner in the Phase 3 review. A per-entry `allowed_grant_types` restriction is
  a follow-up (codex round-1 [P1]).
- **The harness-to-gatekeeper hop is plain HTTP**, and Cilium encryption is not enabled. B
  narrows the exposure to a short-lived, audience-bound, pod-bound token, but does not remove
  it (codex round-1 [P1]).
- **The ailab Flux cluster-admin path** into `strive-ailab` is the residual trust path (F8).
- **Follow-up: weld-auth projected-token mode**, after which Python services can migrate and
  the SOPS registry shrinks toward grants-only.

<!-- codex-review-status: complete -->
