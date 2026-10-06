# S2S service identities without shared secrets (projected ServiceAccount tokens)

**Status: agreed.** Codex (gpt-6-astra) and Fable both signed design B with every item below,
in alignment round A2 (2026-10-06). The trail is under "Review trail" at the end. Three owner
decisions are still open, listed under "Owner decisions"; B does not activate before they are
made.

Source: dev-worker-1's proposal (2026-10-06), an automatable path for new platform service
identities on ailab with no SOPS edit, no age key and no checksum bump. First consumer:
`svc-harness` (platform PR #2092, H9.1b). #2092 waits for B; there are no further SOPS edits.

Repos: **ailab** (this repo) and **platform** (`cchifor/platform`, gitea main 78c0f56b3).

## Verified facts

| # | Fact |
|---|---|
| F1 | `svc-harness` is a **cross-tenant service authority**. `client_credentials` (`service_token.py:197-245`) mints every granted scope for any caller-supplied `tenant_id`, without a user. The dev registry comment at `service_registry.yaml:306-309` claims otherwise and is wrong. |
| F2 | The cluster serves **no** OIDC discovery or `/openid/v1/jwks` (NotFound). The SA token issuer is `https://192.168.0.40:6443` (k8s v1.31.4). The JWKS-based `ProjectedSATokenVerifier` cannot be used, so verification goes through **TokenReview**. |
| F3 | Gatekeeper's SA has automount on. Its token is a kubelet-rotated bound token. |
| F4 | Under Cilium, egress to the API server needs `toEntities: [kube-apiserver]`; `ipBlock` cannot admit it. Precedent: ailab `apps/ci-rerun-watchdog/api-egress-cilium.yaml`. The platform chart already renders CNPs (`charts/airlock`). No platform chart emits ClusterRoles or ClusterRoleBindings today. |
| F5 | The harness speaks only `client_secret_post`: `s2s.ts:82` (boot), `:260` (POST guard), `:268` (mint), and `:465-470` (the DELETE revoke). `index.ts:79` warns. It treats 401/403 as a verdict and anything else as `down()` (`s2s.ts:275-283`). Its token cache is `expires_in - 60 s`, so about 240 s (`s2s.ts:226`, `env.ts:48`). |
| F6 | weld-auth's `S2SClient` supports only `client_secret`, so Python services follow later. |
| F7 | ESO serves only `external-secrets.io/v1`. `strive-pg-harness-store` is live and already reads `af/data/strive/pg-harness`. |
| F8 | Gitea is **1.26.1**. Both repos' `main` require 1 approval from anyone; every bot has non-admin **write** permission, and `reviewer-codex` merges PRs. Platform `main` requires the status checks `CI / ci-gate*`, `E2E Preflight / preflight*`, `E2E Tests / smoke*` and `Contract Tests / contract-gate*`; ailab has none. ailab Flux is cluster-admin into `strive-ailab`. |
| F9 | `owner-ack.md:100-112` records as an accepted residual that **the owner and the dev worker share one Gitea login**. Any Gitea owner gate is circular until that is split. Today's SOPS step is a *real* human gate, because workers do not hold the age key. |
| F10 | `check-ailab-pins.py` (wired through `ci.yml`; `ailab-pin-guard.yml` is a manual audit) makes a PR-introduced pin resolve to a main-ancestor `sha-` tag **or** an `ailab`-family tag. `ailab`-family tags pass **without** an ancestry check (`:140`). `build.yml` publishes on push to `main` only. It is a retention and ancestry filter, not proof of provenance. |
| F11 | The HelmRelease merges `valuesFiles` (`helmrelease.yaml:22-24`). The gatekeeper chart already renders `SVC_AUTH_BACKEND=preshared` from its `env` list, with `extraEnv` appended (`values.yaml:168`, `deployment.yaml:92-103`). The harness chart defaults `secretEnv.HARNESS_CLIENT_SECRET` (`charts/harness/values.yaml:155`). A dark harness renders nothing, including its SA (`Chart.yaml:76`). The harness Deployment is `Recreate`. |
| F12 | Gatekeeper's readiness checks only Redis (`health.py:57-80`). On ailab it runs 2 replicas with PDB 1 and `maxUnavailable: 0`. `/auth/token` is not on the IngressRoute, and the netpol admits only `allowedClients`. It has no request limiter (`service_token.py:152`). |
| F13 | Changes in the last 30 days: |

F13 detail:

| Files | Commits |
|---|---|
| Gatekeeper auth modules | 0–2 each |
| `charts/gatekeeper` | 1 |
| `_helpers.tpl` | 3 |
| `helmrelease.yaml` | 0 |
| `ailab.yaml` | 151 |
| `ci.yml` | 219 |
| all of `infra/gatekeeper` | 86 |

## Decision

**B.** A new service's identity is its pod's projected ServiceAccount token, verified by
TokenReview. Its grants are config: an owner-gated values file, rendered into a ConfigMap that
gatekeeper loads at startup. Existing SOPS identities are untouched.

**Fallback:** the slimmed-down preshared design (A-lite), used only if one of these holds:
- TokenReview availability or latency on the 3-CP API server misses the S2S need;
- consumers cannot adopt projected tokens;
- the owner refuses cluster-scoped RBAC from the app chart.

### How it works

**Provisioning an identity.** There is no secret.
1. The service's chart creates its SA, giving `system:serviceaccount:strive-ailab:harness`.
2. Its pod mounts a projected token with `audience: strive-gatekeeper` and
   `expirationSeconds: 3600` at `/var/run/secrets/tokens/gatekeeper`. The kubelet refreshes it,
   and it is bound to the pod.
3. `deploy/helm/values/providers/ailab-s2s-registry.yaml` (owner-gated, and appended to the
   HelmRelease `valuesFiles`) holds the entry: `client_id`, `auth_method: k8s`, `k8s_subject`,
   the audiences and scopes, and `may_act_for_audiences`.
4. The gatekeeper chart renders it into ConfigMap `gatekeeper-registry-extras` and adds a
   `checksum/registry-extras` annotation. One Helm upgrade applies the ConfigMap and rolls
   gatekeeper, so there is no cross-controller race.
5. At startup, **one** combined loader replaces `lifecycle.py:129`.
   - It validates the SOPS base first.
   - It then validates the extras document as a whole.
   - It merges extras only if the base loaded.
   - The same object goes to `app.state` and to both verifier branches.

**Authenticating a call.** This covers `/auth/token`, delegation issue and exchange
(`service_token.py:566,681`) and revoke (`:827`).
1. The harness reads its token file on **every** call. It sends `Authorization: Bearer` with
   the form `client_id` and no `client_secret`. A read failure never falls back to a secret.
2. Gatekeeper (`SVC_AUTH_BACKEND=composite`) looks up the form `client_id`. The entry's explicit
   `auth_method` selects exactly one verifier, with no fallback:
   - `preshared`: today's argon2 path, unchanged.
   - `k8s`:
     1. Run the cheap prechecks: a three-part JWT; the unverified `aud` contains
        `strive-gatekeeper`; the unverified `sub` equals the entry's `k8s_subject`; `exp` in
        the future. These only guard against amplification.
     2. Check the positive cache. It is keyed by `sha256(token)`, holds the reviewed
        `(username, audiences)`, re-binds the requested `client_id` on every hit, and expires at
        `min(review + 60 s, token exp)` with no sliding.
     3. Pass the rate limiter: a process-wide semaphore (default 4) and a token bucket (default
        10/s, bounded burst), both `GatekeeperSettings` fields.
     4. Call **TokenReview**. The HTTPS header carries gatekeeper's own API token, re-read on
       each uncached review. The harness token goes only in `spec.token`, with
       `spec.audiences: ["strive-gatekeeper"]`. Keep CA verification and a bounded timeout.
3. Mapping of results:
   - A completed review with `authenticated=false`, missing audiences, or a different username,
     and any precheck failure, gives a **generic 401**. Every pre-authentication failure shares
     one message, so registry membership cannot be probed.
   - A completed review whose subject maps to a different k8s client gives 403. Only a mocked
     review can reach this path.
   - Any non-2xx from the API (including 401, 403 and 429), a transport error, a timeout, a
     malformed response, or limiter saturation gives **503** `temporarily_unavailable` with
     `Retry-After`. These are never cached, never extend a cache entry, and each one increments
     a metric.
   - The negative cache holds only refusals from completed reviews. It is keyed by
     `(sha256(token), client_id)`, has a TTL of at most 10 s, and is a bounded LRU.
4. The rest is unchanged: audience check, scope intersection, the grant, and a 300 s JWT.

**The DSN.** ExternalSecret `strive-pg-harness-dsn` is added to ailab's existing
`infrastructure/strive-pg-harness/eso.yaml` on the existing store.
- `data.secretKey=password` plus `template.data.database-url`, with `engineVersion: v2` and
  `mergePolicy: Replace`. The only key is `database-url`.
- No new OpenBao objects.

**Misconfiguration contract.**
- `k8s` entries present while the backend is not `composite`: the **whole** extras document is
  rejected. Gatekeeper boots on the validated base, logs an error and sets
  `gatekeeper_service_registry_extras_rejected 1`.
- An unknown backend string stays fatal.
- An extras collision with the base or within extras, on `client_id` **or** `k8s_subject`,
  refuses that entry. Base lookups are first-match, so this matters.

**Revocation.**
- The kill switch is scaling the service to 0 or `enabled: false`. Deleting the pod does not
  work, because `Recreate` brings up a fresh valid identity.
- An old bearer stops being accepted after `deletionTimestamp` plus the API leeway (or object
  removal), plus at most 60 s of cache, and never later than its `exp`.
- Issued JWTs stop being accepted within 300 s of the **last** mint plus consumer skew. weld-auth
  allows 30 s (`auth_guard.py:98,228`).
- Removing the entry gives 401 on each replica after the roll.

### Pros and cons

**Pros**
- No long-lived harness credential exists anywhere. This matters because of F1.
- No secret lifecycle: nothing to generate, escrow, rotate or recover.
- No new OpenBao paths.
- Grants are reviewable config with no hot reload and no checksum bumps.
- It matches gatekeeper's documented production model.

**Cons**
- New security-critical code on the mint path, in both gatekeeper and the harness.
- Uncached k8s mints depend on the API server. During an outage they get a 503; preshared
  clients are unaffected.
- One new cluster-scoped Role and Binding, plus a Cilium egress rule.
- Only the harness benefits until weld-auth gains the mode.
- **The owner gate is only as real as account separation (F9).**

## Owner decisions (required before activation)

**D1. Identity separation (K1′.3).** Both reviewers' position: before Phase 0 can pass, the
automation gets its own non-admin Gitea identities, and workers' copies of the owner's
credentials are removed and revoked.
- The **only** exception is the owner's **direct** confirmation, given outside the shared login,
  that S2S grants become worker-approvable.
- That is weaker than today's SOPS gate. It is then recorded in the ADR as exactly that.

**D2. Accepting the residual bypasses (F-e′, K1′.2, K1′.4).** These remain bot-approvable after
Phase 0:
- unprotected gatekeeper modules (`routes.py`, `helpers.py`, and the rest of
  `infra/gatekeeper/**` outside the protected set);
- `ailab.yaml` digest pins and `ci.yml`, which carries the pin-check wiring;
- `build.yml`, `protect-ailab-images.yml` and the provenance of `ailab`-family tags (F10);
- the ailab Flux cluster-admin path into `strive-ailab`.

The owner accepts these explicitly in the ADR, or B does not activate.

**D3. svc-harness authority (X-m).** B bounds how long a credential can be exposed, but leaves
two things in place:
- the cross-tenant `client_credentials` authority (F1);
- the plain-HTTP replay window on the harness-to-gatekeeper hop.

The owner either accepts deferring a per-entry `allowed_grant_types` restriction and transport
encryption, or makes grant-type restriction a **pre-flip** item.

## Approach

PRs land strictly in order. Per the reviewbot rule, a dependent PR is not opened before its
prerequisite is live.

### Phase 0: identity separation and the owner gate (owner actions, then tested)
1. Complete D1.
2. On platform `main`, set `protected_file_patterns` to:
   - `deploy/helm/values/providers/ailab-s2s-registry.yaml`
   - `deploy/gitops/flux/clusters/ailab/app/helmrelease.yaml`
   - `deploy/helm/charts/gatekeeper/**`
   - `deploy/helm/templates/_helpers.tpl`
   - `infra/gatekeeper/src/app/gatekeeper/{service_registry,service_verifier,service_token,tokenreview_verifier,config}.py`
   - `infra/gatekeeper/src/app/core/lifecycle.py`
   - `infra/gatekeeper/src/app/core/config/**`
   - `infra/gatekeeper/src/app/{main,__main__}.py`
   - `infra/gatekeeper/src/app/cli/**`
   - `infra/gatekeeper/{Dockerfile,pyproject.toml,uv.lock}`
   - `.github/workflows/s2s-authority-guard.yml` and its script
   - `scripts/ci/{check,list}-ailab-pins.py`

   That is about 7 owner reviews a month per F13.
3. Add `S2S Authority Guard / *` to platform `main`'s `status_check_contexts`. The workflow has
   no `paths:` filter, so it reports on every PR.
4. **Test it with the real automation identities and every merge route** (merge, squash,
   `force_merge`, direct push):
   - a bot-approved PR touching each protected pattern family is unmergeable by automation;
   - a red guard blocks the merge;
   - an unrelated PR still merges;
   - the owner's merge path works.
5. Record the version and the results in the runbook.
6. Fallback if the patterns do not hold: change the **effective** `main` rule (Gitea applies
   only the first matching rule) with `required_approvals >= 1`, keeping existing protections,
   and repeat the tests. Do not proceed with an untested gate.

### Phase 1: ailab PR, the DSN (harmless while the harness is dark)
- Add `strive-pg-harness-dsn` as described above.
- `docs/runbooks/infra-pg.md`:
  - Rotation order:
    1. `bao kv patch`, keeping the 48-hex invariant;
    2. force-sync both ExternalSecrets and **wait for a fresh successful reconcile of each**;
    3. re-run the bootstrap Job, whose password is read from its startup env (`bootstrap.py:66`);
    4. restart the harness and wait for `/ready`.
  - Fix the wipe-recovery text at `:129-144` that still names `harness-secrets`.
- Verify `SecretSynced`. The owner may read the Secret; workers cannot.

### Phase 2a: platform PR, gatekeeper code (parallel with 2b; both modes default off)
- `service_registry.py`:
  - `auth_method: Literal["preshared","k8s"] = "preshared"`, explicit and never inferred.
  - The combined loader per "How it works" step 5.
  - Extras rules: `auth_method: k8s`, a `k8s_subject` of the form
    `system:serviceaccount:<ns>:<name>`, no `secret_hash`, and `client_id` matching
    `^svc-[a-z0-9-]+$`.
  - Collision refusal on `client_id` and on `k8s_subject`.
- `tokenreview_verifier.py` (new): everything in "Authenticating a call", step 2's `k8s` branch.
  The prechecks, both caches, the limiter, own-token re-read, CA, timeout, and
  `ClientAuthUnavailable` producing 503.
- `service_verifier.py`: `CompositeVerifier` dispatches on `auth_method`. It never calls the
  other branch. Pre-authentication failures return the same generic 401 as today.
- `config.py`: add `composite` to the `svc_auth_backend` Literal, plus
  `SERVICE_REGISTRY_EXTRAS_PATH`, `SVC_AUTH_TOKENREVIEW_AUDIENCE` (default `strive-gatekeeper`)
  and the limiter settings.
- `service_token.py`: catch `ClientAuthUnavailable` before generic authentication errors at all
  four call sites.
- Metrics:
  - `gatekeeper_service_registry_info{base_sha,extras_sha}`, nonsecret;
  - `..._extras_refused_total` and `..._extras_rejected`;
  - TokenReview outcome counters, limiter saturation, and cache hits.
- Fix the misleading dev-registry comment at `service_registry.yaml:306-309`.
- Tests:
  - Every validation rule.
  - Base read once; extras not read after a base failure; an empty base is distinguishable from
    the error fallback.
  - Subject collisions.
  - The misconfiguration contract.
  - Composite "never calls the other branch" for: a bad secret with a valid Bearer, a bad Bearer
    with a supplied secret, and an unknown client. The existing argon2 negative cases are kept.
  - TokenReview cases:
    - unauthenticated;
    - audiences missing or wrong;
    - right `sub` but the wrong namespace;
    - subject mismatch, as a mocked 403;
    - API 401, 403, 429 and 5xx, a timeout and a malformed response, each giving 503 and not
      cached;
    - limiter saturation giving 503;
    - the same token claiming another client is rejected on a cache hit;
    - expiry at `min(+60 s, exp)` and rejection at `exp`;
    - a rotated token gets a fresh review;
    - the negative cache is never poisoned across clients;
    - rotation of gatekeeper's own token.
  - Delegation issue, exchange and revoke with k8s clients, covering success, failure, outage
    and cross-client grant ownership. The fixture at
    `test_service_token_delegation_endpoints.py:171` must take the composite verifier.

### Phase 2b: platform PR, harness Bearer mode (parallel with 2a)
- `HARNESS_CLIENT_TOKEN_FILE` threads through `env.ts`, the `HarnessEnv` type
  (`sdk/env.ts:60`), `s2s.ts:82,260`, `index.ts:79`, POST mint/issue/exchange, and the DELETE
  revoke.
- Token-file mode takes precedence. A read failure is `IDENTITY_NOT_CONFIGURED` and never falls
  back to a secret. The token is never logged.
- Tests: the header is present and no secret is in the body, on all four transports; a rotated
  file between calls; a missing or empty file; precedence over a configured secret; 503 goes to
  `down()`.

### Phase 3: platform PR, charts and values (after the 2a and 2b images are built; harness still dark)
- `charts/gatekeeper`:
  - `registry-extras-configmap.yaml` and the checksum annotation.
  - The extras mount (`optional: true`).
  - Template the **existing** `SVC_AUTH_BACKEND` env entry from a value, so exactly one renders.
  - `SERVICE_REGISTRY_EXTRAS_PATH`.
  - `tokenreview-rbac.yaml`: a one-rule ClusterRole for `tokenreviews: create` and its Binding,
    both named `<namespace>-gatekeeper-tokenreview`.
  - `ciliumnetworkpolicy-apiserver.yaml` (`toEntities: [kube-apiserver]`, TCP 443/6443).
  - Failing-render guards: an extras entry without `k8s_subject`, or with `secret_hash`.
- `charts/harness`:
  - The projected token volume (`audience: strive-gatekeeper`).
  - `HARNESS_CLIENT_TOKEN_FILE`.
  - A failing render if token mode coexists with `HARNESS_CLIENT_SECRET` from any source:
    `secretEnv`, `config`, `extraEnv` or `_env.tpl`.
- `helmrelease.yaml`: append `ailab-s2s-registry.yaml` to `valuesFiles`. The new file holds the
  svc-harness entry, with grants copied from dev `service_registry.yaml:281`.
- `ailab.yaml`:
  - Pin both new digests.
  - Enable composite and the CNP.
  - Harness token mode.
  - `secretEnv.HARNESS_CLIENT_SECRET: null`.
  - `HARNESS_DATABASE_URL` from `strive-pg-harness-dsn`.
  - Rewrite the H9.1 comment.
- `s2s-authority-guard.yml` and its script (required, protected). It fails when any of these is
  set outside `ailab-s2s-registry.yaml`, in values keys, `env`/`extraEnv`, or rendered
  volumes/volumeMounts landing a file on either registry path:
  - `gatekeeper.serviceRegistry.extras`;
  - the `SVC_AUTH_BACKEND`, `SERVICE_REGISTRY_EXTRAS_PATH` or `SERVICE_REGISTRY_PATH` env;
  - the TokenReview audience;
  - `gatekeeper.envFromGatekeeper.secretName` or `refMap.service-registry`.

  It checks the **rendered** ailab manifests with all three values files. Fixed chart defaults
  are allowed.
- Grants-parity test (required CI):
  - every extras entry must exist in the dev registry with identical audiences, scopes and
    `may_act_for_audiences`;
  - an unknown `client_id` fails;
  - it is shown to fail on a new `svc-X` and on each kind of grant drift.

  Parity is a drift check, not authorization. Runtime collision refusal stays.
- `check-harness-chart-contract.sh` renders with `harness.enabled=true` and all three values
  files, and asserts that no `HARNESS_CLIENT_SECRET` is in the final env.
- Docs:
  - `SECRETS.md`: fix the stale "no ESO" line and add a section on secretless identities.
  - The **ADR** records the decision, F1, D1–D3 as the owner decided them, the residuals, and
    the weld-auth follow-up.
- **Roll acceptance (pre-flip)**, per gatekeeper replica:
  - the effective `extras_sha` equals the rendered hash;
  - the `base_sha` values agree;
  - a preshared mint succeeds. Workers can check this through the Service plus both pods'
    `service_token_minted` logs.
  - The e2e lane passes, and `report-ailab-pin-drift` shows 0 torn.

### Phase 4: the flip (#2092) and activation checks
- Rebase #2092 (`harness.enabled: true`) and rewrite its preconditions.
- After the flip, the owner runs these **mandatory** checks against each gatekeeper pod IP:
  - a k8s mint, with a complete request and assertions on `sub` and `azp`;
  - refusals:
    - `svc-deepagent` with the harness Bearer gives 401 (no fallback);
    - a second k8s entry claimed with the harness token gives 401 (precheck, generic message);
    - a token with the wrong audience gives 401;
    - no token gives 401.
- Then #2092's own checks: Ready, migrate completed, 401 rather than 302, the `@api` journeys,
  and 0 torn.
- **On any failure, darken the harness.**
- Every later roll of an active registry repeats both the pre-flip and the post-flip checks.

## Verification and drills
- Phase 0's gate tests (above).
- **Cold start:** extras absent, so gatekeeper boots on the base and svc-harness gets 401.
- **Deletion and restore during a roll:** a pod that already loaded extras keeps them;
  restoring them needs a roll.
- **Rollback** (not image-only): darken the harness, then revert the image **and** the
  composite and extras config together, then verify a base preshared mint. The DSN Secret stays
  until no consumer uses it.
- **Token rotation:** observe a change in token fingerprint without logging the token, and
  drive an uncached mint past both caches. Do this for the harness token and for gatekeeper's
  own reviewer token.
- **Revocation:** owner-run, with a surviving probe that holds the old pod's bearer. Test both
  replica IPs through rejection, using the timing in "Revocation".

## Residuals and follow-ups
- Residual bypasses: D2.
- Bounded F1 and replay exposure: D3.
- Follow-up: a weld-auth projected-token mode, after which Python services migrate and the SOPS
  registry shrinks.
- Follow-up: a per-entry `allowed_grant_types` restriction, unless D3 makes it pre-flip.

## Review trail
- **Round 1 (design open).** Codex picked A-full: an OpenBao preshared secret, ESO, and hot
  reload. Fable picked B. The owner was asked and chose B.
- **Round 2.** Codex: "B is sound and implementable", with rollout fixes. Fable: no blockers,
  with fixes.
- **A1** (neutral framing, both reviewers given the same evidence pack):
  - both chose **B** independently;
  - all items agreed or amended compatibly;
  - one disagreement, K1, on how much the owner gate covers.
- **A2** (new facts F8–F13 included):
  - K1 resolved by the scoped protected set plus D1 and D2;
  - all amendments are additive;
  - **both signed**: "I endorse B with all items as agreed/amended above as the best solution."
- Raw answers: `plans/2026-10-06-s2s-identity-alignment*.md`.

<!-- codex-review-status: complete -->
