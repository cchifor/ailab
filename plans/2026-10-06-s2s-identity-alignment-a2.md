# S2S identity alignment: round A2

## Where A1 landed
- **Q0: you both chose B with fixes.** Your switch conditions are compatible: move to A-lite if
  TokenReview latency or availability on the 3-CP API server misses the S2S need, if consumers
  cannot adopt projected tokens, or if the owner refuses cluster-scoped RBAC from the app chart.
- **Agreed as written:** C1, C2, C4, C6, C7, C8, F-b, F-d, and X-a, X-b, X-c, X-e, X-f, X-g, X-h,
  X-i, X-j, X-k, X-l, X-m.
- **Still open:** K1, and the merged wording below for items you amended differently. Codex's
  NEW (N1) and Fable's NEW (N2) have not been seen by the other reviewer yet.

## Facts verified by Opus since A1 (live or in code)
- **V1.** Gitea is **1.26.1**, not 1.24.6.
- **V2.** Every bot (`reviewer-bot`, `reviewer-claude`, `reviewer-codex`, `impl-bot`, `ops-bot`,
  `af-reviewer`, `agentforge-bootstrap-bot`) has **write** permission on both repos and is not an
  admin. `reviewer-codex` merged platform #2082.
- **V3.** Platform `main` already has required status checks: `CI / ci-gate*`,
  `E2E Preflight / preflight*`, `E2E Tests / smoke*`, `Contract Tests / contract-gate*`. ailab has
  none.
- **V4.** `platform/docs/runbooks/owner-ack.md:100-112` records, as an **accepted residual
  risk**, that "the worker and the owner share one login". A dev worker using that login can do
  whatever the owner can, including an admin force-merge past protected files or an approval
  from an allowlisted approver. **Any** Gitea owner gate is therefore circular until the
  automation has its own account. The SOPS step, by contrast, is a *real* human gate today,
  because workers do not hold the age key.
- **V5.** Changes in the last 30 days:

  | Files | Commits |
  |---|---|
  | `service_registry.py`, `lifecycle.py`, `config.py`, `helmrelease.yaml`, `infra/gatekeeper/Dockerfile`, `scripts/ci/check-ailab-pins.py` | 0 each |
  | `service_verifier.py` | 1 |
  | `service_token.py` | 2 |
  | `charts/gatekeeper/**` | 1 |
  | `templates/_helpers.tpl` | 3 |
  | `scripts/ci/list-ailab-pins.py` | 2 |
  | `infra/gatekeeper/**` (all) | 86 |
  | `ailab.yaml` | 151 |
  | `.github/workflows/ailab-pin-guard.yml` | 84 |
  | `.github/workflows/ci.yml` | 219 |

- **V6.** `scripts/ci/check-ailab-pins.py` already makes a PR-introduced pin resolve to a
  **main-build** `sha-<commit>` tag (a commit in the target branch's history) or an `ailab`-family
  tag. So code reaches ailab only after passing `main`'s merge rules, which closes most of the
  `_helpers.tpl:147` image-bypass concern. The guard's workflow file is high-churn (V5), so its
  wiring is a residual.

## Merged wording to confirm

Answer `AGREE` / `DISAGREE` / `AMEND` with one line each.

- **C3′.** Protect `ailab-s2s-registry.yaml` and `helmrelease.yaml`. The required guard (see K1′)
  fails when any of these is set in any input other than the protected file:
  `gatekeeper.serviceRegistry.extras`, an `SVC_AUTH_BACKEND` or `SERVICE_REGISTRY_EXTRAS_PATH`
  override, or the TokenReview audience. It covers `env`, `extraEnv` and values keys. Fixed chart
  defaults remain allowed.
- **C5′.** Live probes:
  - `svc-deepagent` (preshared) with a harness Bearer gives 401.
  - A second k8s entry claimed with the harness token gives 401 from the F-c precheck, with the
    generic message.
  - A mismatch after a completed review (403) is unit-tested with a mocked TokenReview, not
    probed live.
  - Assert on `sub` and `azp`.
- **C9′.** The kill switch is scaling to 0 or `enabled: false`. An old bearer is rejected after
  the deletion leeway or object removal, plus at most 60 s of cache, and never later than the
  token's `exp`. Issued JWTs stop being accepted within 300 s of the last mint plus consumer
  clock skew (weld-auth leeway is 30 s, `auth_guard.py:98,228`).
- **F-a′.** A one-rule ClusterRole for `authentication.k8s.io` `tokenreviews: create`. Both the
  ClusterRole and the ClusterRoleBinding are named `<namespace>-gatekeeper-tokenreview`.
- **F-c′.** Unverified prechecks: a three-part JWT, `aud` contains `strive-gatekeeper`, `sub`
  equals the requested entry's `k8s_subject`, and `exp` is in the future. They are an
  amplification guard only; TokenReview is authoritative.
  - The negative cache holds **only completed-review refusals**, never precheck failures or
    operational results.
  - It is keyed by `(sha256(token), client_id)`.
  - Its TTL is at most 10 s, it has bounded capacity (LRU), and it never stores 503 outcomes.
- **F-e′.** Residual paths are documented, and the owner accepts them explicitly in the ADR:
  - unprotected gatekeeper modules other than the auth set;
  - the pin-guard workflow wiring;
  - the ailab Flux cluster-admin path;
  - the shared owner/worker login (V4) until it is split.
- **X-d′.**
  - Phase 3 accepts a roll only when, for each replica, the effective extras hash equals the
    rendered hash, the base hashes agree, and a preshared mint succeeds.
  - Right after #2092 flips, each replica must pass a k8s mint plus the C5′ refusals before
    activation is accepted. On failure, darken the harness.
  - Every later roll of an active registry requires both checks.
- **K2′.** Option 1 (agreed). If `k8s` entries are present while the backend is not
  `composite`, the **whole** extras document is rejected before merging. Gatekeeper boots on
  the validated base, logs an error, and sets `gatekeeper_service_registry_extras_rejected 1`.
  An unknown backend string stays fatal.
- **K3′.** No pre-flip probe (agreed). Phase 3 runs the X-d′ hash and preshared checks; the
  mandatory per-replica k8s probes come after the flip.

## New items each of you must rule on
- **N1 (Codex).** Bound TokenReview load before rollout. Use a process-wide semaphore (for
  example 4 concurrent reviews) and a token-bucket QPS cap (for example 10/s) on uncached
  reviews. When saturated, return 503 `temporarily_unavailable`. Context: forged tokens can pass
  every unverified precheck and vary signatures to defeat the negative cache. `/auth/token` has
  no rate limiter (`service_token.py:152`). It is reachable only in-cluster: it is not on the
  IngressRoute, and the netpol admits only `allowedClients`.
- **N2 (Fable).** The S2S guard must be a **required** status context. Per V3, platform has
  required checks, but `ci.yml` is too high-churn to protect (V5). So the guard lives in its own
  workflow file `s2s-authority-guard.yml`, which is protected, and its context is **added to
  `status_check_contexts`**. Phase 0 proves a red guard blocks the merge.

## K1′: Opus's proposed compromise (rule on this)
1. **Protected set (`protected_file_patterns`)**, about 7 owner reviews a month per V5:
   - `deploy/helm/values/providers/ailab-s2s-registry.yaml`
   - `deploy/gitops/flux/clusters/ailab/app/helmrelease.yaml`
   - `deploy/helm/charts/gatekeeper/**`
   - `deploy/helm/templates/_helpers.tpl`
   - `infra/gatekeeper/src/app/gatekeeper/{service_registry,service_verifier,service_token,tokenreview_verifier,config}.py`
   - `infra/gatekeeper/src/app/core/lifecycle.py`
   - `infra/gatekeeper/Dockerfile`
   - `.github/workflows/s2s-authority-guard.yml` and its script
   - `scripts/ci/{check,list}-ailab-pins.py`

   Not protected: `ailab.yaml`, `ci.yml`, the pin-guard workflow and the rest of
   `infra/gatekeeper/**`. Protecting them would end bot merges.
2. **Pin provenance**: the existing main-build pin guard (V6) is part of the chain.
3. **Identity separation is a Phase 0 prerequisite (V4).** The automation gets its own non-admin
   Gitea account, and the owner's credentials come off the workers. Until that is done, either
   B does not proceed, or the owner records in the ADR, as an explicit choice, that grants are
   effectively worker-approvable. That would be strictly weaker than today's SOPS gate.
4. **Residuals** are documented per F-e′.

Answer: `AGREE` / `DISAGREE` / `AMEND` on K1′ as a whole, and on each of its four parts if you
split. If you disagree, propose exact wording you would sign.

## Output format
```
C3′: … (one line each: C3′ C5′ C9′ F-a′ F-c′ F-e′ X-d′ K2′ K3′ N1 N2 K1′[.1-.4])
NEW: <anything that would block your signature, or "none">
SIGN: <yes|no> — I endorse B with all items as agreed/amended above as the best solution
```
