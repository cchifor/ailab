# S2S identity: Codex and Fable alignment

The owner wants **genuine agreement between Codex and Fable on the best solution**. That is not
deference to a decision already taken. Earlier rounds told Codex that design B was settled;
ignore that framing here. Judge on the merits.

The plan under review is `plans/2026-10-06-s2s-identity-openbao-plan.md`: design B with Codex's
round-2 inline comments. The platform code is `cchifor/platform` at gitea main 78c0f56b3.

## Positions so far

| Round | Codex | Fable |
|---|---|---|
| 1 (design open) | **A-full**: OpenBao preshared secret, ESO, hot-reloaded extras. "Projected tokens require broader client and verifier changes." | **B**: projected SA tokens. "Deletes most of what makes the plan hard; roughly the same gatekeeper effort." A-lite is the fallback. |
| 2 (told B is chosen) | "**B is sound and implementable: yes.** Rollout blockers are fixable within B." | No blockers to B. 5 rollout-blocking defects and 6 ranked changes. |

### The designs
- **A-full**: the proposal plus Codex's round-1 hardening. Gatekeeper hot-reloads an
  ESO-rendered extras Secret holding a `sha256:` digest. OpenBao holds a generated
  `client-secret`. Rotation runs through barriers.
- **A-lite**: an OpenBao secret and an ESO digest Secret, with grants in a chart ConfigMap.
  Extras load at startup, with a re-read on a lookup miss. One SecretStore.
- **B**: a projected SA token, `audience: <aud>`, read per mint by the harness. Gatekeeper
  verifies it with TokenReview: the cluster serves no OIDC discovery or JWKS (NotFound;
  k8s v1.31.4; issuer `https://192.168.0.40:6443`). An explicit per-entry `auth_method`
  dispatches the verifier. Grants live in a chart-rendered ConfigMap loaded at startup and
  rolled by checksum. The DSN comes from an ESO template on the existing `strive-pg-harness-store`.

### Facts both of you accepted
- `client_credentials` gives cross-tenant authority (`service_token.py:197-245`).
- The harness speaks `client_secret_post` only.
- weld-auth supports `client_secret` only.
- ESO serves `v1` only.
- No Reloader is installed.
- Gitea `main` on both repos requires 1 approval from anyone, with no protected file patterns.
- ailab Flux is cluster-admin into `strive-ailab`.

## What each of you must answer

For **Q0** and **every item**, answer `AGREE` / `DISAGREE` / `AMEND: <exact replacement>`, each
with a one-line reason. Cite `file:line` or upstream docs when you disagree. Keep it terse.

### Q0: the design verdict on the merits
Which is best for this estate: A-full, A-lite, or B with the round-2 fixes below? Steelman the
design you do **not** pick in two sentences, and name the specific evidence that would make you
switch.

### Raised by both reviewers in round 2 (confirm the merged wording)
- **C1.** Gatekeeper re-reads its own rotating API token for each uncached review. The HTTPS
  header uses gatekeeper's API-audience token. The harness's `<aud>` token goes only into
  `TokenReview.spec.token`. Keep CA verification and a bounded timeout.
- **C2.** Map only a **completed** TokenReview to a caller verdict:
  - `authenticated=false`, audiences missing `<aud>`, or a username that does not equal the
    entry's `k8s_subject` gives 401 or 403.
  - Any non-2xx from the API (including its 401, 403 and 429), transport errors and timeouts
    give **503** `temporarily_unavailable`. They never extend or create a cache entry.
- **C3.** Phase 0 protects `deploy/gitops/flux/clusters/ailab/app/helmrelease.yaml` in addition
  to the s2s values file. A required CI check fails if `gatekeeper.serviceRegistry.extras` (or
  the backend and extras-path keys) is set in any values file other than the protected one. The
  extent of the gate is in **K1**.
- **C4.** Test the Gitea gate with the real bot merge identity and every merge route (merge,
  squash, `force_merge`, direct push). Assert the bot identity is not a repo admin. The owner's
  path is force-merge or admin. Verify every glob family on the running Gitea version.
- **C5.** Probe expectations: `client_id=svc-deepagent` with only a harness Bearer gives **401**
  (dispatch selects preshared, and there is no fallback). The 403 subject-mismatch case needs a
  second k8s test entry. Assert on the JWT's `sub` and `azp`, since it has no `client_id` claim.
- **C6.** Render exactly one `SVC_AUTH_BACKEND` by templating the existing chart env entry. Test
  the merged ailab render and the default-off render.
- **C7.** Set `HARNESS_CLIENT_SECRET: null` in ailab values, or use mode-aware chart defaults.
  The render guard runs with `harness.enabled=true` and all three `valuesFiles`, and asserts the
  final container env has no `HARNESS_CLIENT_SECRET` from any source.
- **C8.** The harness token-file mode threads through `HarnessEnv`, `s2s.ts:82,260`,
  `index.ts:79`, POST mint/issue/exchange and DELETE revoke. Token-file mode takes precedence. A
  read failure never falls back to the secret.
- **C9.** The kill switch is scaling to 0 or setting `enabled: false`. Deleting the pod is not
  one, because `Recreate` brings back a fresh valid identity. A deleted pod's token stays valid
  until `deletionTimestamp` plus the API server's leeway, or until the object is removed, plus
  up to 60 s of cache. Already-issued JWTs stay valid for 300 s after the **last** successful
  mint.

### Raised by Fable only
- **F-a.** Replace `system:auth-delegator` with a one-rule ClusterRole for `tokenreviews: create`.
  Name the ClusterRoleBinding `<namespace>-gatekeeper-tokenreview`, since the chart has never
  emitted cluster-scoped objects.
- **F-b.** Use an audience of `strive-gatekeeper` instead of `gatekeeper`, because OPA
  Gatekeeper is a common name.
- **F-c.** Before calling TokenReview, run cheap checks: the token is a three-part JWT; its
  unverified `aud` contains `<aud>`; its unverified `sub` equals the entry's `k8s_subject`; its
  `exp` is in the future. Keep a short negative cache. This is an amplification guard only;
  authorization still comes from TokenReview.
- **F-d.** Every pre-authentication failure (unknown client, k8s entry with no Bearer, preshared
  entry with no secret, pre-check failure) returns one identical generic 401 message.
- **F-e.** Document the residual risk: the gatekeeper image digest pin in `ailab.yaml` remains
  bot-approvable, so the gate covers grants, not code.

### Raised by Codex only
- **X-a.** The cache stores the reviewed identity and audiences, not a success boolean. Every
  hit re-checks that the requested `client_id` maps to the cached `k8s_subject`. Expiry is
  `min(review time + 60 s, token exp)`, with no sliding window.
- **X-b.** Replace the single startup load at `lifecycle.py:129` with the combined loader, and
  pass the same object to `app.state` and both verifier branches. Tests: the base is read once;
  extras are not read after a base failure; an empty base is distinguishable from the error
  fallback.
- **X-c.** Add subject-collision tests, because `_registry_entry_for_subject` returns the first
  match.
- **X-d.** Readiness does not check the registry. Accepting a roll requires, for each replica:
  the effective extras hash equals the intended rendered hash; the base hashes agree; a
  preshared mint and a k8s mint both succeed.
- **X-e.** An image-only rollback fails at startup, because old settings reject `composite`.
  Rollback means: darken the harness first, revert the image **and** the composite and extras
  config together, then verify a base preshared mint.
- **X-f.** A pre-flip probe needs a temporary SA, because a dark harness has no SA. Either
  arrange and clean up that SA, or drop the pre-flip probe in favour of the post-flip probe.
- **X-g.** Pick one documented behaviour for "preshared backend plus k8s extras". The plan text
  contradicts itself ("refuses to start, and logs that it is ignoring them"). Add `composite` to
  `GatekeeperSettings.svc_auth_backend`. The choice is in **K2**.
- **X-h.** Delegation issue, exchange and revoke authenticate in
  `service_token.py:566,681,827`; `delegation_grant.py` is storage. Test success and failure
  for k8s clients on all of them. Composite tests assert the other branch is never called.
- **X-i.** The DSN ExternalSecret is `data.secretKey=password` plus
  `template.data.database-url` only, with `mergePolicy: Replace`. Assert the sole key is
  `database-url`. Force-sync is asynchronous, so wait for a fresh successful reconcile of both
  ExternalSecrets before the bootstrap Job. Keep the hex-password invariant. Fix the
  `infra-pg.md:134-144` text that still names `harness-secrets`.
- **X-j.** The parity test also rejects extras `client_id`s missing from the dev registry, runs
  in required CI, and is shown to fail on a new `svc-X`, a changed scope or audience, and a
  changed `may_act_for_audiences`.
- **X-k.** The rotation drill must observe a change in the token's fingerprint (never log the
  token) and drive an uncached mint. The harness's token cache is about 240 s, not 300.
  Exercise rotation of gatekeeper's own reviewer token too.
- **X-l.** The revocation drill is owner-run, with a surviving probe that holds the old pod's
  bearer and tests both replica IPs.
- **X-m.** F1 survives under B for the token's lifetime. Correct the misleading dev-registry
  comment at `service_registry.yaml:306-309`. Deferring `allowed_grant_types` and encrypted
  transport requires the owner to accept that explicitly; if user-bound authority is required,
  grant-type restriction becomes a pre-flip item.

### Apparent conflicts: say which side you take and why
- **K1. How much the gate covers.**
  - Codex: cover the deployment, build and authentication code paths that can bypass the
    protected registry (deployment template env, lifecycle wiring), or require owner approval on
    the effective `main` rule.
  - Fable: protect `helmrelease.yaml`, the s2s values file, and the registry, verifier and
    token modules, plus a CI check, and *document* that the digest pin and other code remain
    bot-approvable.
  - Opus's proposal to react to: protect the minimal authority set. That is the s2s values
    file, `helmrelease.yaml`, the gatekeeper chart's `templates/deployment.yaml`,
    `registry-extras*` and `tokenreview-rbac*`, and the gatekeeper modules
    `service_{registry,verifier,token}.py`, `tokenreview_verifier.py`, `core/lifecycle.py` and
    `config.py`. Add the CI check. Document the digest-pin and ailab-Flux residuals rather than
    gating all of `main`, since all of `main` would end bot merges for every platform PR.
- **K2. The misconfiguration contract** (k8s entries in extras while the backend is not
  `composite`).
  - Option 1: boot, ignore extras, log an error and set an `extras_rejected` metric.
  - Option 2: refuse to start.
  - Opus leans to option 1, which matches lifecycle's best-effort registry handling: user-facing
    auth must survive an S2S misconfiguration.
- **K3. The pre-flip probe.** X-f against Fable's view that skipping it is acceptable. Opus
  leans to dropping the pre-flip probe, keeping the post-flip owner-run probe plus #2092's
  checks, and making X-d's per-replica mint checks the Phase 3 gate.

## Output format (both of you)

```
Q0: <A-full|A-lite|B> — reason; steelman of the other; switch evidence
C1: AGREE|DISAGREE|AMEND … (one line each, C1–C9, F-a–F-e, X-a–X-m, K1–K3)
NEW: <any blocker neither raised, or "none">
```
